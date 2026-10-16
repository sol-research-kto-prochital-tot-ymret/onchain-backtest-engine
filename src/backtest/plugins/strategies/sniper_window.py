"""Sniper Break-Even Window V0 as an existing engine StrategyInstance.

Only strategy-specific detection, sizing and exit decisions live here. The
fork owns the replay timeline, risk acceptance, fills and portfolio ledger.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, fields
from decimal import Decimal, localcontext
from typing import Any, cast

from backtest.domain.execution import ExecutionNotification, OrderStatus
from backtest.domain.hashing import domain_digest
from backtest.domain.identifiers import AssetId, BundleId, ContentDigest, OrderId, PoolId
from backtest.domain.intents import SwapExactInIntent
from backtest.domain.market_events import BlockEvent, CanonicalEvent
from backtest.engine.contracts import PortfolioView, StrategyContext
from backtest.plugins.execution.pump_sniper import (
    SOL,
    PumpSniperExecutionModel,
    curve_state,
    profile,
)
from backtest.plugins.protocols.pumpfun.model import (
    PumpCurveStateV1,
    PumpQuoteError,
    buy_quote,
    sell_quote,
)
from backtest.plugins.strategies.creator_income import CreatorFeeIncome


@dataclass(frozen=True)
class SniperWindowPolicy:
    early_slots: int = 2
    minimum_sniper_buy_lamports: int = 1_000_000_000
    target_return_bps: int = 500
    epsilon_lamports: int = 1_000_000
    entry_delay_slots: int = 1
    exit_delay_slots: int = 1
    holding_seconds: int = 300
    entry_network_fee_lamports: int = 105_000
    exit_network_fee_lamports: int = 105_000
    entry_tip_lamports: int = 0
    exit_tip_lamports: int = 0
    account_rent_lamports: int = 2_039_280
    account_rent_refund_lamports: int = 2_039_280
    entry_slippage_bps: int = 500
    exit_slippage_bps: int = 500
    bankroll_lamports: int = 10_000_000_000
    maximum_exit_attempts: int = 3
    creator_fee_credit_enabled: int = 1
    minimum_entry_age_seconds: int = 0
    maximum_entry_age_seconds: int = 0
    maximum_entry_budget_lamports: int = 0
    require_creator_never_bought: int = 0
    confirmation_slots: int = 0
    confirmation_new_buyers: int = 0
    confirmation_net_lamports: int = 0
    entry_window_slots: int = 0
    stop_loss_bps: int = 0
    counterpart_sell_fraction: int = 0
    own_take_profit_enabled: int = 1
    reference_profit_exit_enabled: int = 0
    require_creator_flat_before_entry: int = 0

    def __post_init__(self) -> None:
        if any(
            type(getattr(self, f.name)) is not int or getattr(self, f.name) < 0
            for f in fields(self)
        ):
            raise ValueError("non-negative integer policy required")
        if self.entry_delay_slots != self.exit_delay_slots or self.entry_delay_slots < 1:
            raise ValueError("native SlotLatencyModel requires equal positive entry/exit delays")
        if min(self.holding_seconds, self.maximum_exit_attempts) < 1:
            raise ValueError("positive timeout and exit attempts required")
        if self.account_rent_refund_lamports > self.account_rent_lamports:
            raise ValueError("rent refund exceeds posted rent")
        if max(self.entry_slippage_bps, self.exit_slippage_bps) >= 10_000:
            raise ValueError("slippage must be below 100 percent")
        if self.entry_tip_lamports or self.exit_tip_lamports:
            raise ValueError("nonzero tips require a separate native tip reservation model")
        if self.creator_fee_credit_enabled not in (0, 1):
            raise ValueError("creator fee credit flag must be zero or one")
        if self.require_creator_never_bought not in (0, 1) or self.stop_loss_bps >= 10_000:
            raise ValueError("invalid creator flag or stop loss")
        if self.counterpart_sell_fraction not in (0, 1):
            raise ValueError("invalid counterpart sale scenario")
        if self.own_take_profit_enabled not in (0, 1) or self.reference_profit_exit_enabled not in (
            0,
            1,
        ):
            raise ValueError("invalid exit trigger flag")
        if self.require_creator_flat_before_entry not in (0, 1):
            raise ValueError("invalid creator flat flag")
        if (
            self.maximum_entry_age_seconds
            and self.maximum_entry_age_seconds < self.minimum_entry_age_seconds
        ):
            raise ValueError("maximum entry age precedes minimum entry age")

    def document(self) -> dict[str, int]:
        values = asdict(self)
        # The disabled timestamp horizon preserves the original policy identity.
        if not self.maximum_entry_age_seconds:
            del values["maximum_entry_age_seconds"]
        # Preserve saved control identities when the new exit mode is disabled.
        if self.own_take_profit_enabled == 1:
            del values["own_take_profit_enabled"]
        if not self.reference_profit_exit_enabled:
            del values["reference_profit_exit_enabled"]
        if not self.require_creator_flat_before_entry:
            del values["require_creator_flat_before_entry"]
        return values

    @classmethod
    def load(cls, values: dict[str, Any]) -> SniperWindowPolicy:
        return cls(**{f.name: values[f.name] for f in fields(cls) if f.name in values})


def threshold(state: PumpCurveStateV1, tokens: int, cash: int, bps: tuple[int, int]) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 60
        factor = Decimal(10_000 - sum(bps)) / 10_000
        amount = Decimal(cash)
        k = state.virtual_sol_reserves_lamports * state.virtual_token_reserves_atomic
        return (amount + (amount * amount + 4 * factor * amount * k / tokens).sqrt()) / (2 * factor)


def maximum_budget(
    state: PumpCurveStateV1,
    tokens: int,
    cost: int,
    bps: tuple[int, int],
    policy: SniperWindowPolicy,
) -> int:
    with localcontext() as ctx:
        ctx.prec = 60
        x = Decimal(state.virtual_sol_reserves_lamports)
        limit = threshold(state, tokens, cost, bps) - policy.epsilon_lamports
        if limit <= x:
            return 0
        r = Decimal(10_000 + policy.target_return_bps) / 10_000
        fixed = (
            policy.entry_network_fee_lamports
            + policy.account_rent_lamports
            - policy.account_rent_refund_lamports
        ) * r + policy.exit_network_fee_lamports
        d = Decimal(10_000 + sum(bps)) / 10_000 * x * x
        e = x + limit
        a = r * e
        b = r * d + fixed * e - Decimal(10_000 - sum(bps)) / 10_000 * limit * limit
        c = fixed * d
        discriminant = b * b - 4 * a * c
        return (
            0 if b >= 0 or discriminant < 0 else max(0, int((-b + discriminant.sqrt()) / (2 * a)))
        )


class SniperWindowRisk:
    @property
    def bundle_id(self) -> BundleId:
        return BundleId(
            domain_digest(
                "dnipro.sniper-wallet-risk.v1", {"policy": "reserve-principal-and-execution-fee"}
            ).hex
        )

    def accept(self, intent: SwapExactInIntent, portfolio: PortfolioView) -> bool:
        principal = portfolio.available(intent.sold_asset_id)
        if intent.execution_fee_asset_id == intent.sold_asset_id:
            return principal >= intent.amount_in_atomic + intent.execution_fee_reserve_atomic
        return principal >= intent.amount_in_atomic and (
            intent.execution_fee_asset_id is None
            or portfolio.available(intent.execution_fee_asset_id)
            >= intent.execution_fee_reserve_atomic
        )


class SniperWindowStrategy:
    def __init__(
        self, source: Any, execution: PumpSniperExecutionModel, policy: SniperWindowPolicy
    ):
        self.source, self.execution, self.policy = source, execution, policy
        self.rows = {
            b["mint"]: {
                "mint": b["mint"],
                "symbol": b.get("symbol"),
                "creator": b.get("creator"),
                "gmgn_url": "https://gmgn.ai/sol/token/" + b["mint"],
                "launch_slot": int(b["slot"]),
                "status": "UNSUPPORTED" if b["mint"] in source.unsupported else "NO_SIGNAL",
                "reason": source.unsupported.get(b["mint"], "NO_ELIGIBLE_EARLY_BUYER"),
                "entry_cash_lamports": 0,
                "failed_execution_cost_lamports": 0,
                "closed_pnl_lamports": None,
                "remaining_tokens_atomic": 0,
                "exit_attempts": 0,
            }
            for b in source.births
        }
        self.wallets: defaultdict[str, defaultdict[str, dict[str, int]]] = defaultdict(
            lambda: defaultdict(
                lambda: {
                    "tokens": 0,
                    "cost": 0,
                    "bought": 0,
                    "sold_tokens": 0,
                    "transferred_out_tokens": 0,
                }
            )
        )
        self.paid: set[tuple[str, str]] = set()
        self.eligible: defaultdict[str, set[str]] = defaultdict(set)
        self.seen: set[str] = set()
        self.entry_watch: set[str] = set()
        self.active: set[str] = set()
        self.pending_sell: set[str] = set()
        self.touched: set[str] = set()
        self.orders: dict[str, tuple[str, str, str]] = {}
        self.previous_slot = source.manifest["from_slot"] - 1
        self.last_state: dict[str, tuple[int, int, int, int]] = {}
        self.creator_income = CreatorFeeIncome()
        # Diagnostics are outside policy identity and never alter trading decisions.
        self.trace_mints: set[str] = set()
        self.trace: list[dict[str, Any]] = []
        self.candidates: dict[str, dict[str, Any]] = {}
        self.flows: defaultdict[str, list[tuple[int, str, int]]] = defaultdict(list)
        self.inventory_cursor = 0

    def _apply_inventory_transfers(self) -> None:
        conditional = self.execution.conditional
        if conditional is None:
            return
        applied = getattr(conditional, "inventory_applied", ())
        for movement in applied[self.inventory_cursor :]:
            wallets = self.wallets[movement["mint"]]
            wallets[movement["source_wallet"]]["tokens"] -= movement["tokens"]
            wallets[movement["source_wallet"]]["transferred_out_tokens"] += movement["tokens"]
            wallets[movement["destination_wallet"]]["tokens"] += movement["tokens"]
            # External gifts/delegated movements have no observed SOL cashflow.
            # Keep address-level unrecovered cash costs separate from holdings.
        self.inventory_cursor = len(applied)

    def _trace(self, mint: str, context: StrategyContext, action: str, **details: Any) -> None:
        if mint not in self.trace_mints:
            return
        row = self.rows[mint]
        record = {
            "mint": mint,
            "symbol": row["symbol"],
            "boundary_ordinal": context.instant.boundary_ordinal,
            "slot": context.instant.chain_slot,
            "time": self.execution.now,
            "action": action,
            "status": row["status"],
            "reason": row["reason"],
            "eligible_wallets": len(self.eligible[mint]),
            **details,
        }
        pool = self.execution.market_pool(mint, context)
        if pool is not None:
            record.update(virtual_sol=pool.reserve_a_atomic, virtual_tokens=pool.reserve_b_atomic)
        wallet = details.get("wallet") or row.get("sniper_wallet")
        if wallet is not None:
            ledger = self.wallets[mint][wallet]
            record.update(
                wallet=wallet,
                wallet_tokens=ledger["tokens"],
                wallet_cash_cost=ledger["cost"],
                wallet_adjusted_cost=self.anchor_cost(mint, wallet),
                wallet_creator_fee_income=self.creator_income.income(mint, wallet),
            )
        self.trace.append(record)

    @property
    def bundle_id(self) -> BundleId:
        return BundleId(
            domain_digest(
                "dnipro.sniper-window-strategy.v1", {"formula": "corrected-v0-fixed-cost-quadratic"}
            ).hex
        )

    @property
    def component_id(self) -> ContentDigest:
        return domain_digest("dnipro.configured-sniper-window.v1", self.policy.document())

    def _within_entry_horizon(self, mint: str, slot: int) -> bool:
        row = self.rows[mint]
        if self.policy.maximum_entry_age_seconds:
            age = self.execution.now - self.source.times[row["launch_slot"]]
            return cast(bool, age <= self.policy.maximum_entry_age_seconds)
        return cast(
            bool,
            slot
            <= row["launch_slot"] + (self.policy.entry_window_slots or self.policy.early_slots),
        )

    def _intent(
        self, mint: str, side: str, amount: int, minimum: int, context: StrategyContext, reason: str
    ) -> SwapExactInIntent:
        row = self.rows[mint]
        ordinal = context.instant.boundary_ordinal
        order = OrderId(
            domain_digest(
                "dnipro.sniper-order.v1",
                {
                    "mint": mint,
                    "side": side,
                    "boundary": ordinal,
                    "attempt": row["exit_attempts"],
                    "component": self.component_id.hex,
                },
            ).hex
        )
        self.orders[order.hex] = mint, side, reason
        fee = (
            self.policy.entry_network_fee_lamports
            if side == "BUY"
            else self.policy.exit_network_fee_lamports
        )
        return SwapExactInIntent(
            order,
            PoolId(mint),
            SOL if side == "BUY" else AssetId(mint),
            AssetId(mint) if side == "BUY" else SOL,
            amount,
            minimum,
            ordinal,
            SOL if fee else None,
            fee,
        )

    def _entry(self, mint: str, context: StrategyContext) -> SwapExactInIntent | None:
        row = self.rows[mint]
        slot = context.instant.chain_slot
        if (
            row["status"] != "NO_SIGNAL"
            or mint in self.execution.blocked
            or slot is None
            or not self._within_entry_horizon(mint, slot)
        ):
            self._trace(mint, context, "ENTRY_INELIGIBLE")
            return None
        creator_gate = (
            self.creator_entry_gate(mint) if self.policy.require_creator_flat_before_entry else {}
        )
        if self.policy.require_creator_flat_before_entry:
            self._trace(mint, context, "ENTRY_CREATOR_FLAT_CHECK", **creator_gate)
            if not creator_gate["creator_flat_entry_allowed"]:
                row["reason"] = "WAIT_CREATOR_FLAT"
                return None
        if (
            self.policy.require_creator_never_bought
            and self.wallets[mint][row["creator"]]["bought"]
        ):
            row["reason"] = "CREATOR_HAS_OBSERVED_BUY"
            self._trace(mint, context, "ENTRY_CREATOR_FILTER")
            return None
        candidate = self.candidates.get(mint)
        if self.policy.confirmation_slots and candidate is not None:
            subsequent = [r for r in self.flows[mint] if r[0] > candidate["boundary"]]
            new_buyers = {r[1] for r in subsequent if r[2] > 0} - candidate["buyers"]
            net = sum(r[2] for r in subsequent)
            if (
                slot < candidate["slot"] + self.policy.confirmation_slots
                or len(new_buyers) < self.policy.confirmation_new_buyers
                or net < self.policy.confirmation_net_lamports
            ):
                row["reason"] = "WAIT_BUY_CONFIRMATION"
                self._trace(
                    mint, context, "WAIT_BUY_CONFIRMATION", new_buyers=len(new_buyers), net_flow=net
                )
                return None
        birth_time = self.source.times[row["launch_slot"]]
        if self.execution.now - birth_time < self.policy.minimum_entry_age_seconds:
            self._trace(mint, context, "WAIT_LAUNCH_AGE")
            return None
        pool = self.execution.market_pool(mint, context)
        if pool is None or mint not in self.execution.fees:
            return None
        try:
            state = curve_state(pool, self.source.birth_by_mint[mint])
            bps = self.execution.fees[mint]
            fee_profile = profile(bps, self.execution.now)
            for wallet in sorted(
                self.eligible[mint], key=lambda w: (-self.anchor_cost(mint, w), w)
            ):
                ledger = self.wallets[mint][wallet]
                cost = self.anchor_cost(mint, wallet)
                if min(ledger["tokens"], cost) <= 0:
                    continue
                row["reason"] = "NO_POSITIVE_BREAK_EVEN_WINDOW"
                budget = maximum_budget(state, ledger["tokens"], cost, bps, self.policy)
                if self.policy.maximum_entry_budget_lamports:
                    budget = min(budget, self.policy.maximum_entry_budget_lamports)
                self._trace(
                    mint,
                    context,
                    "ENTRY_CANDIDATE",
                    wallet=wallet,
                    budget_lamports=budget,
                    real_sol=state.real_sol_reserves_lamports,
                )
                for adjustment in range(64):
                    if budget - adjustment <= 1:
                        break
                    quote = buy_quote(
                        state,
                        spendable_gross_sol_lamports=budget - adjustment,
                        fee_profile=fee_profile,
                        effective_at_unix_s=self.execution.now,
                    )
                    # The strategy formula concerns the state immediately AFTER our buy.
                    after = PumpCurveStateV1(
                        state.virtual_token_reserves_atomic - quote.tokens_out_atomic,
                        state.virtual_sol_reserves_lamports + quote.net_curve_sol_lamports,
                        state.real_token_reserves_atomic - quote.tokens_out_atomic,
                        state.real_sol_reserves_lamports + quote.net_curve_sol_lamports,
                        state.token_total_supply_atomic,
                        state.lifecycle,
                        state.mode,
                    )
                    entry_cost = (
                        quote.gross_sol_spent_lamports
                        + self.policy.entry_network_fee_lamports
                        + self.policy.account_rent_lamports
                        - self.policy.account_rent_refund_lamports
                    )
                    needed = (
                        entry_cost * (10_000 + self.policy.target_return_bps) + 9999
                    ) // 10_000 + self.policy.exit_network_fee_lamports
                    xs, xt = (
                        threshold(after, ledger["tokens"], cost, bps),
                        threshold(after, quote.tokens_out_atomic, needed, bps),
                    )
                    # Do not size an order whose target requires curve completion.
                    completion = Decimal(
                        after.virtual_sol_reserves_lamports * after.virtual_token_reserves_atomic
                    ) / (after.virtual_token_reserves_atomic - after.real_token_reserves_atomic)
                    if (
                        xs - xt < self.policy.epsilon_lamports
                        or xs <= after.virtual_sol_reserves_lamports
                        or xt >= completion
                    ):
                        continue
                    if self.policy.confirmation_slots and candidate is None:
                        self.candidates[mint] = {
                            "boundary": context.instant.boundary_ordinal,
                            "slot": slot,
                            "buyers": set(self.wallets[mint]),
                        }
                        row["reason"] = "WAIT_BUY_CONFIRMATION"
                        self._trace(mint, context, "CONFIRMATION_WATCH_STARTED")
                        return None
                    row.update(
                        status="PENDING_BUY",
                        reason="SIGNAL",
                        signal_slot=slot,
                        signal_time=self.execution.now,
                        sniper_wallet=wallet,
                        sniper_gmgn_url="https://gmgn.ai/sol/address/" + wallet,
                        sniper_cost_lamports=cost,
                        sniper_trading_cash_cost_lamports=ledger["cost"],
                        sniper_creator_fee_income_lamports=self.creator_income.income(mint, wallet),
                        sniper_is_launch_creator=wallet == row["creator"],
                        creator_fee_credit_enabled=self.policy.creator_fee_credit_enabled,
                        sniper_tokens=ledger["tokens"],
                        budget_lamports=budget - adjustment,
                        signal_sniper_threshold=str(xs),
                        signal_our_threshold=str(xt),
                        **(creator_gate if self.policy.require_creator_flat_before_entry else {}),
                    )
                    self._trace(
                        mint,
                        context,
                        "ENTRY_SIGNAL",
                        wallet=wallet,
                        budget_lamports=budget - adjustment,
                        xs=str(xs),
                        xt=str(xt),
                        epsilon=self.policy.epsilon_lamports,
                        after_virtual_sol=after.virtual_sol_reserves_lamports,
                        after_real_sol=after.real_sol_reserves_lamports,
                        quoted_tokens=quote.tokens_out_atomic,
                        quoted_curve_deposit=quote.net_curve_sol_lamports,
                        min_tokens=quote.tokens_out_atomic
                        * (10_000 - self.policy.entry_slippage_bps)
                        // 10_000,
                    )
                    return self._intent(
                        mint,
                        "BUY",
                        budget - adjustment + self.policy.account_rent_lamports,
                        quote.tokens_out_atomic
                        * (10_000 - self.policy.entry_slippage_bps)
                        // 10_000,
                        context,
                        "ENTRY",
                    )
        except (PumpQuoteError, ValueError) as error:
            self._trace(mint, context, "ENTRY_QUOTE_REJECTED", error=str(error))
            return None
        self._trace(mint, context, "ENTRY_NO_WINDOW")
        return None

    def creator_entry_gate(self, mint: str) -> dict[str, Any]:
        ledger = self.wallets[mint][self.rows[mint]["creator"]]
        tokens = ledger["tokens"]
        if tokens < 0:
            status = "UNPROVEN_CREATOR_INVENTORY"
        elif tokens > 0:
            status = "CREATOR_STILL_HOLDING"
        elif ledger["transferred_out_tokens"]:
            # A gift to another address does not establish a developer sellout.
            status = "CREATOR_TRANSFER_OUT_NOT_SELL_OUT"
        elif ledger["bought"] == 0:
            status = "NO_OBSERVED_CREATOR_BUY"
        elif ledger["sold_tokens"] > 0:
            status = "CREATOR_SOLD_ALL_OBSERVED_TOKENS"
        else:
            status = "UNPROVEN_CREATOR_DISPOSAL"
        return {
            "creator_flat_entry_allowed": status
            in ("NO_OBSERVED_CREATOR_BUY", "CREATOR_SOLD_ALL_OBSERVED_TOKENS"),
            "creator_status_at_signal": status,
            "creator_tokens_at_signal": tokens,
            "creator_gross_bought_lamports_at_signal": ledger["bought"],
            "creator_sold_tokens_at_signal": ledger["sold_tokens"],
            "creator_transferred_out_tokens_at_signal": ledger["transferred_out_tokens"],
        }

    def anchor_cost(self, mint: str, wallet: str) -> int:
        income = (
            self.creator_income.income(mint, wallet)
            if self.policy.creator_fee_credit_enabled
            else 0
        )
        return self.wallets[mint][wallet]["cost"] - income

    def _reference_profit(
        self, mint: str, state: PumpCurveStateV1, context: StrategyContext
    ) -> bool:
        """Mark the reference's entire retained position without executing its sale.

        Cost is unrecovered cash, so prior partial-sale receipts are already
        included. External token transfers never invent acquisition cashflow.
        The mark uses the same real-reserve constraint as our native execution.
        """
        row = self.rows[mint]
        wallet = row["sniper_wallet"]
        tokens = self.wallets[mint][wallet]["tokens"]
        cost = self.anchor_cost(mint, wallet)
        details: dict[str, Any] = {
            "reference_tokens_atomic": tokens,
            "reference_unrecovered_cost_lamports": cost,
            "reference_profit_lamports": None,
            "reference_reached": False,
            "reference_valuation_status": "AVAILABLE",
        }
        if tokens < 0:
            details["reference_valuation_status"] = "UNPROVEN_REFERENCE_INVENTORY"
        elif tokens == 0 or cost <= 0:
            # Already recovered the investment: no additional sale is needed.
            details["reference_profit_lamports"] = -cost
            details["reference_reached"] = cost <= 0
            details["reference_valuation_status"] = (
                "REALIZED_CASH_RECOVERY" if cost <= 0 else "FULLY_SOLD_AT_LOSS"
            )
        else:
            try:
                quote = sell_quote(
                    state,
                    tokens_in_atomic=tokens,
                    fee_profile=profile(self.execution.fees[mint], self.execution.now),
                    effective_at_unix_s=self.execution.now,
                    liquidity_policy=self.execution.liquidity_policy,
                )
                net = quote.sol_out_lamports - self.policy.exit_network_fee_lamports - cost
                details.update(
                    reference_liquidation_lamports=quote.sol_out_lamports,
                    reference_sell_network_fee_lamports=self.policy.exit_network_fee_lamports,
                    reference_profit_lamports=net,
                    reference_reached=net >= 0,
                )
            except (PumpQuoteError, ValueError) as error:
                details["reference_valuation_status"] = (
                    error.code.value
                    if isinstance(error, PumpQuoteError)
                    else "INVALID_HISTORICAL_STATE"
                )
        self._trace(mint, context, "REFERENCE_PROFIT_EVALUATION", **details)
        if details["reference_reached"] and "reference_first_profitable_slot" not in row:
            row.update(
                reference_first_profitable_slot=context.instant.chain_slot,
                reference_first_profitable_time=self.execution.now,
                reference_first_profitable_boundary=context.instant.boundary_ordinal,
                reference_first_profit_lamports=details["reference_profit_lamports"],
                reference_first_valuation_status=details["reference_valuation_status"],
            )
        return cast(bool, details["reference_reached"])

    def _exit(self, mint: str, context: StrategyContext) -> SwapExactInIntent | None:
        if mint in self.pending_sell or mint in self.execution.blocked:
            return None
        row = self.rows[mint]
        if row["exit_attempts"] >= self.policy.maximum_exit_attempts:
            row["reason"] = "EXIT_ATTEMPTS_EXHAUSTED"
            return None
        pool = self.execution.market_pool(mint, context)
        if pool is None:
            return None
        reference_reached = False
        try:
            state = curve_state(pool, self.source.birth_by_mint[mint])
            if self.policy.reference_profit_exit_enabled:
                # Record a crossing even if our own preflight cannot liquidate.
                reference_reached = self._reference_profit(mint, state, context)
            quote = sell_quote(
                state,
                tokens_in_atomic=row["remaining_tokens_atomic"],
                fee_profile=profile(self.execution.fees[mint], self.execution.now),
                effective_at_unix_s=self.execution.now,
                liquidity_policy=self.execution.liquidity_policy,
            )
        except (PumpQuoteError, ValueError) as error:
            if self.execution.now >= row["entry_time"] + self.policy.holding_seconds:
                row["reason"] = (
                    error.code.value
                    if isinstance(error, PumpQuoteError)
                    else "INVALID_HISTORICAL_STATE"
                )
            self._trace(mint, context, "EXIT_PREFLIGHT_REJECTED", error=str(error))
            return None
        receipt = (
            quote.sol_out_lamports
            + self.policy.account_rent_refund_lamports
            - self.policy.exit_network_fee_lamports
        )
        row["last_liquidation_lamports"] = receipt
        target = (
            (row["entry_cash_lamports"] - self.policy.account_rent_refund_lamports)
            * self.policy.target_return_bps
            + 9999
        ) // 10_000
        pnl = receipt - row["entry_cash_lamports"] - row["failed_execution_cost_lamports"]
        timeout = self.execution.now >= row["entry_time"] + self.policy.holding_seconds
        stop = self.policy.stop_loss_bps > 0 and (
            pnl * 10_000
            <= -self.policy.stop_loss_bps
            * (row["entry_cash_lamports"] - self.policy.account_rent_refund_lamports)
        )
        self._trace(
            mint,
            context,
            "EXIT_EVALUATION",
            pnl_lamports=pnl,
            target_lamports=target,
            timeout=timeout,
            gross_curve_sell=quote.gross_curve_sol_lamports,
            real_sol=state.real_sol_reserves_lamports,
            synthetic_shortfall=quote.synthetic_shortfall_lamports,
            **(
                {
                    "reference_reached": reference_reached,
                    "own_take_profit_enabled": self.policy.own_take_profit_enabled,
                }
                if self.policy.reference_profit_exit_enabled
                or not self.policy.own_take_profit_enabled
                else {}
            ),
        )
        own_target = bool(self.policy.own_take_profit_enabled and pnl >= target)
        if not own_target and not reference_reached and not timeout and not stop:
            return None
        reason = (
            "STOP_LOSS"
            if stop
            else "REFERENCE_PROFIT"
            if reference_reached
            else "TARGET"
            if own_target
            else "TIMEOUT"
        )
        row["exit_attempts"] += 1
        row["exit_signal_slot"] = context.instant.chain_slot
        self.pending_sell.add(mint)
        return self._intent(
            mint,
            "SELL",
            row["remaining_tokens_atomic"],
            quote.sol_out_lamports * (10_000 - self.policy.exit_slippage_bps) // 10_000,
            context,
            reason,
        )

    def on_event(
        self, event: CanonicalEvent, context: StrategyContext
    ) -> tuple[SwapExactInIntent, ...]:
        if isinstance(event, BlockEvent):
            self._apply_inventory_transfers()
            slot = event.envelope.position.block_ordinal
            self.execution.now = self.source.times[slot]
            if any(lo <= slot and hi > self.previous_slot for lo, hi in self.source.gaps):
                for mint in self.seen:
                    self.execution.blocked[mint] = "UNCERTIFIED_SOURCE_RANGE"
                    if mint in self.active:
                        self.rows[mint]["reason"] = "UNCERTIFIED_SOURCE_RANGE"
            self.previous_slot = slot
            exits = tuple(
                intent
                for mint in sorted(self.active)
                if (intent := self._exit(mint, context)) is not None
            )
            if not (
                self.policy.minimum_entry_age_seconds
                or self.policy.maximum_entry_age_seconds
                or self.policy.confirmation_slots
            ):
                return exits
            entries = []
            for mint in sorted(self.entry_watch):
                if self.rows[mint]["status"] != "NO_SIGNAL" or not self._within_entry_horizon(
                    mint, slot
                ):
                    self.entry_watch.discard(mint)
                    continue
                if (intent := self._entry(mint, context)) is not None:
                    entries.append(intent)
            return exits + tuple(entries)
        meta = self.source.meta[event.envelope.canonical_event_id.hex]
        raw, kind = meta["row"], meta["kind"]
        mint = raw["mint"]
        if kind == "trade" and self.execution.conditional is not None:
            effective = self.execution.conditional.effective_rows.get(
                event.envelope.canonical_event_id.hex
            )
            if effective is None:
                if mint in self.active:
                    self.rows[mint]["reason"] = self.execution.blocked.get(
                        mint, "CONDITIONAL_GROUP_UNPROVEN"
                    )
                return ()
            raw = effective
        if kind == "launch":
            self.seen.add(mint)
            self.entry_watch.add(mint)
            birth = self.source.birth_by_mint[mint]
            self.last_state[mint] = (
                int(birth["initial_virtual_quote_raw"]),
                int(birth["initial_virtual_base_raw"]),
                int(birth.get("initial_real_quote_raw") or 0),
                int(birth["initial_real_base_raw"]),
            )
        elif kind == "lifecycle":
            self.execution.blocked[mint] = "CURVE_COMPLETE_OR_MIGRATED"
            if mint in self.active:
                self.rows[mint]["reason"] = "CURVE_COMPLETE_OR_MIGRATED"
        else:
            self.touched.add(mint)
            self.creator_income.observe(event.envelope.canonical_event_id.hex, raw)
            payload = raw["payload"]
            bps = int(payload["fee_basis_points"]), int(payload["creator_fee_basis_points"])
            self.execution.fees[mint] = bps
            old = self.last_state[mint]
            sign = 1 if raw["side"] == "buy" else -1
            amount, tokens = int(raw["quote"]), int(raw["tokens"])
            self.flows[mint].append((event.envelope.boundary_ordinal, raw["trader"], sign * amount))
            after = (
                int(raw["virtual_quote"]),
                int(raw["virtual_base"]),
                int(raw["real_quote"]),
                int(raw["real_base"]),
            )
            expected = (
                old[0] + sign * amount,
                old[1] - sign * tokens,
                old[2] + sign * amount,
                old[3] - sign * tokens,
            )
            if expected != after:
                self.execution.blocked[mint] = "RESERVE_DISCONTINUITY"
                self.rows[mint]["reason"] = "RESERVE_DISCONTINUITY"
            self.last_state[mint] = after
            if after[3] == 0:
                self.execution.blocked[mint] = "CURVE_COMPLETE_OR_MIGRATED"
            wallet = self.wallets[mint][raw["trader"]]
            charged = int(raw["protocol_fee"]) + int(raw["creator_fee"])
            fee = 0
            signature_key = mint, raw["signature"]
            if (
                raw.get("fee_payer") == raw["trader"]
                and signature_key not in self.paid
                and raw.get("network_fee") is not None
            ):
                fee = int(raw["network_fee"])
                self.paid.add(signature_key)
            if raw["side"] == "buy":
                wallet["tokens"] += tokens
                wallet["cost"] += amount + charged + fee
                wallet["bought"] += amount + charged
            else:
                wallet["tokens"] -= tokens
                wallet["cost"] -= amount - charged - fee
                wallet["sold_tokens"] += tokens
            if (
                event.envelope.position.block_ordinal
                <= self.rows[mint]["launch_slot"] + self.policy.early_slots
                and wallet["bought"] >= self.policy.minimum_sniper_buy_lamports
            ):
                self.eligible[mint].add(raw["trader"])
        if event.envelope.canonical_event_id.hex not in self.source.last_in_group:
            return ()
        self._apply_inventory_transfers()
        proposed = []
        for touched in sorted(self.touched):
            if touched in self.active:
                intent = self._exit(touched, context)
            else:
                intent = self._entry(touched, context)
            if intent is not None:
                proposed.append(intent)
        self.touched.clear()
        return tuple(proposed)

    def on_execution(self, notification: ExecutionNotification, context: StrategyContext) -> None:
        mint, side, reason = self.orders[notification.order_id.hex]
        row = self.rows[mint]
        self._trace(
            mint,
            context,
            "ORDER_" + notification.status.value,
            side=side,
            order_id=notification.order_id.hex,
            reports=list(notification.reports),
            execution_fee=notification.execution_fee_atomic,
        )
        if notification.status is OrderStatus.ACCEPTED:
            return
        if notification.status is not OrderStatus.FILLED:
            row["failed_execution_cost_lamports"] += notification.execution_fee_atomic
            row["reason"] = notification.reports[0]
            if side == "BUY":
                row["status"] = (
                    "ENTRY_REJECTED"
                    if notification.status is OrderStatus.REJECTED
                    else "ENTRY_FAILED"
                )
            else:
                self.pending_sell.discard(mint)
            return
        quote = self.execution.quotes[notification.order_id.hex]
        if self.execution.conditional is not None:
            self.execution.conditional.own_fill(
                mint, quote["tokens"] if side == "BUY" else -quote["tokens"]
            )
            pool = self.execution.market_pool(mint, context)
            assert pool is not None
            state = curve_state(pool, self.source.birth_by_mint[mint])
            self.last_state[mint] = (
                state.virtual_sol_reserves_lamports,
                state.virtual_token_reserves_atomic,
                state.real_sol_reserves_lamports,
                state.real_token_reserves_atomic,
            )
        if side == "BUY":
            row.update(
                status="OPEN",
                reason="HOLDING",
                entry_slot=notification.boundary_ordinal >> 32,
                entry_time=self.execution.now,
                remaining_tokens_atomic=notification.fills[0].amount_out_atomic,
                entry_cash_lamports=quote["user_quote"]
                + notification.execution_fee_atomic
                + self.policy.account_rent_lamports,
            )
            self.active.add(mint)
        else:
            proceeds = (
                quote["user_quote"]
                + self.policy.account_rent_refund_lamports
                - notification.execution_fee_atomic
            )
            row.update(
                status="CLOSED",
                reason=reason,
                exit_slot=notification.boundary_ordinal >> 32,
                exit_time=self.execution.now,
                remaining_tokens_atomic=0,
                exit_receipt_lamports=proceeds,
                closed_pnl_lamports=proceeds
                - row["entry_cash_lamports"]
                - row["failed_execution_cost_lamports"],
                exit_gross_curve_lamports=quote["gross_curve_quote"],
                exit_observed_real_sol_lamports=quote["observed_real_sol"],
                exit_synthetic_shortfall_lamports=quote["synthetic_shortfall"],
                sell_liquidity_policy=self.execution.liquidity_policy.value,
            )
            self.active.discard(mint)
            self.pending_sell.discard(mint)
