"""Observational next-slot leader copying using Pump quotes and the core ledger.

This deliberately does not impersonate the upstream exact CopyBuy contract.
It evaluates one-buy/one-full-sell positions on frozen end-of-slot quotes.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from backtest.domain.hashing import domain_digest
from backtest.domain.identifiers import AccountId, AssetId
from backtest.domain.ledger import (
    AccountKind,
    LedgerAccount,
    LedgerCorrelationKind,
    LedgerTransaction,
    Posting,
)
from backtest.engine.portfolio import PortfolioState, available_account_id
from backtest.plugins.protocols.pumpfun.model import (
    PUMP_BUY_LEGACY_EXACT_NET_SOL_FORMULA_V1,
    PUMP_SELL_EXACT_TOKEN_IN_FORMULA_V1,
    PUMP_STATIC_PROGRAM_CONTRACT_V1,
    PumpCurveLifecycle,
    PumpCurveStateV1,
    PumpFeeProfile,
    PumpMode,
    PumpQuoteError,
    buy_quote,
    sell_quote,
)

SOL = AssetId("So11111111111111111111111111111111111111112")
OFFICIAL_PUMP = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"


@dataclass(frozen=True)
class LeaderSlotPolicy:
    start_time: int
    end_time: int
    initial_sol_lamports: int = 100_000_000_000
    entry_network_fee_lamports: int | None = None
    exit_network_fee_lamports: int | None = None
    excluded_mints: tuple[str, ...] = (OFFICIAL_PUMP,)

    def __post_init__(self) -> None:
        if not 0 <= self.start_time < self.end_time:
            raise ValueError("invalid decision window")
        for amount in (
            self.initial_sol_lamports,
            self.entry_network_fee_lamports,
            self.exit_network_fee_lamports,
        ):
            if amount is not None and (type(amount) is not int or not 0 <= amount < 1 << 64):
                raise ValueError("costs and initial capital must be UInt64")


def _order(row: dict[str, Any]) -> tuple[int, int, tuple[int, ...]]:
    return row["slot"], row["transaction_index"], tuple(row["absolute_path"])


def select_pairs(
    trades: list[dict[str, Any]], policy: LeaderSlotPolicy
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], dict[str, int]]:
    groups = defaultdict(list)
    for row in trades:
        groups[row["mint"]].append(row)
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    reasons: Counter[str] = Counter()
    for mint, rows in sorted(groups.items()):
        rows.sort(key=_order)
        if mint in policy.excluded_mints:
            reasons["explicitly_excluded_mint"] += 1
            continue
        entries = [
            r
            for r in rows
            if r["is_buy"] and policy.start_time <= r["block_time"] < policy.end_time
        ]
        if not entries:
            reasons["no_entry_in_window"] += 1
            continue
        if len(entries) != 1:
            reasons["multiple_entries"] += 1
            continue
        buy = entries[0]
        following = [r for r in rows if _order(r) > _order(buy)]
        sells = [r for r in following if not r["is_buy"]]
        if len(sells) != 1 or any(r["is_buy"] for r in following):
            reasons["not_one_buy_one_sell"] += 1
            continue
        sell = sells[0]
        if sell["slot"] <= buy["slot"]:
            reasons["same_slot_roundtrip"] += 1
            continue
        if buy["token_amount"] != sell["token_amount"]:
            reasons["not_full_leader_exit"] += 1
            continue
        if any(r["mayhem_mode"] or r["cashback"] or r["cashback_fee_bps"] for r in (buy, sell)):
            reasons["unsupported_mayhem_or_cashback"] += 1
            continue
        if any(not r["token_amount"] or not r["sol_amount"] for r in (buy, sell)):
            reasons["zero_amount"] += 1
            continue
        # These observed fee components bind the numerical research assumption.
        if any((r["protocol_fee_bps"], r["creator_fee_bps"]) != (95, 30) for r in (buy, sell)):
            reasons["unsupported_fee_profile"] += 1
            continue
        if any(
            r[fee] != (r["sol_amount"] * r[bps] + 9_999) // 10_000
            for r in (buy, sell)
            for fee, bps in (
                ("protocol_fee", "protocol_fee_bps"),
                ("creator_fee", "creator_fee_bps"),
            )
        ):
            reasons["fee_amount_does_not_match_model"] += 1
            continue
        pairs.append((buy, sell))
    return sorted(pairs, key=lambda p: _order(p[0])), dict(sorted(reasons.items()))


def state(row: dict[str, Any]) -> PumpCurveStateV1:
    return PumpCurveStateV1(
        virtual_token_reserves_atomic=row["virtual_token_reserves"],
        virtual_sol_reserves_lamports=row["virtual_sol_reserves"],
        real_token_reserves_atomic=row["real_token_reserves"],
        real_sol_reserves_lamports=row["real_sol_reserves"],
        token_total_supply_atomic=1_000_000_000_000_000,
        lifecycle=PumpCurveLifecycle.ACTIVE
        if row["real_token_reserves"]
        else PumpCurveLifecycle.COMPLETED,
        mode=PumpMode.NORMAL,
    )


def fee_profile(timestamp: int) -> PumpFeeProfile:
    return PumpFeeProfile(
        profile_id="dnipro-observed-95-30-normal-research-v1",
        program_version=PUMP_STATIC_PROGRAM_CONTRACT_V1,
        # Copy the observed gross spend with an explicitly versioned normal-curve model.
        buy_formula_version=PUMP_BUY_LEGACY_EXACT_NET_SOL_FORMULA_V1,
        sell_formula_version=PUMP_SELL_EXACT_TOKEN_IN_FORMULA_V1,
        effective_from_unix_s=timestamp,
        effective_until_unix_s=timestamp + 1,
        protocol_fee_bps=95,
        creator_fee_bps=30,
    )


def replay(
    trades: list[dict[str, Any]], states: list[dict[str, Any]], policy: LeaderSlotPolicy
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    pairs, exclusions = select_pairs(trades, policy)
    by_state = {(r["slot"], r["mint"]): r for r in states}
    if len(by_state) != len(states):
        raise ValueError("duplicate slot/mint state")
    portfolio = PortfolioState({SOL: policy.initial_sol_lamports})
    positions: dict[str, dict[str, Any]] = {}
    ledger: list[dict[str, Any]] = []
    actions: list[tuple[int, int, tuple[int, ...], str, dict[str, Any]]] = []
    used_leader_fees: set[str] = set()
    for buy, sell in pairs:
        mint = buy["mint"]
        network_fees = 0
        for signal in (buy, sell):
            if signal["signature"] not in used_leader_fees:
                used_leader_fees.add(signal["signature"])
                if signal.get("fee_payer") == signal.get("user"):
                    network_fees += signal["fee_lamports"]
        positions[mint] = {
            "mint": mint,
            "leader_entry_slot": buy["slot"],
            "leader_exit_slot": sell["slot"],
            "assumed_copy_entry_slot": buy["slot"] + 1,
            "assumed_copy_exit_slot": sell["slot"] + 1,
            "leader_gross_buy_lamports": buy["sol_amount"]
            + buy["protocol_fee"]
            + buy["creator_fee"],
            "leader_observed_pnl_lamports": (
                sell["sol_amount"]
                - sell["protocol_fee"]
                - sell["creator_fee"]
                - buy["sol_amount"]
                - buy["protocol_fee"]
                - buy["creator_fee"]
                - network_fees
            ),
            "status": "UNSUBMITTED",
            "remaining_tokens_atomic": 0,
            "entry_cost_lamports": 0,
            "exit_proceeds_lamports": 0,
            "closed_pnl_lamports": None,
            "entry_failure": None,
            "exit_failure": None,
        }
        actions.extend(
            (
                signal["slot"] + 1,
                signal["transaction_index"],
                tuple(signal["absolute_path"]),
                side,
                signal,
            )
            for side, signal in (("BUY", buy), ("SELL", sell))
        )
    for slot, _transaction, _path, side, signal in sorted(actions, key=lambda x: x[:4]):
        position = positions[signal["mint"]]
        reserve = by_state.get((signal["slot"], signal["mint"]))
        if side == "SELL" and position["status"] != "OPEN":
            continue
        if reserve is None:
            position["entry_failure" if side == "BUY" else "exit_failure"] = (
                "MISSING_END_OF_SLOT_STATE"
            )
            if side == "BUY":
                position["status"] = "EXCLUDED_MISSING_STATE"
            continue
        # A reserve marked before the trigger cannot be an end-of-signal-slot quote.
        if (reserve["last_transaction_index"], tuple(reserve["last_absolute_path"])) < (
            signal["transaction_index"],
            tuple(signal["absolute_path"]),
        ):
            raise ValueError("slot state predates the leader signal")
        network_fee = (
            policy.entry_network_fee_lamports if side == "BUY" else policy.exit_network_fee_lamports
        )
        if network_fee is None:
            network_fee = signal["fee_lamports"]
        try:
            current = state(reserve)
            profile = fee_profile(reserve["block_time"])
            if side == "BUY":
                quoted_buy = buy_quote(
                    current,
                    spendable_gross_sol_lamports=position["leader_gross_buy_lamports"],
                    fee_profile=profile,
                    effective_at_unix_s=reserve["block_time"],
                )
                gross, curve = (
                    quoted_buy.gross_sol_spent_lamports,
                    quoted_buy.net_curve_sol_lamports,
                )
                tokens, fees = quoted_buy.tokens_out_atomic, quoted_buy.fees
                if portfolio.available(SOL) < gross + network_fee:
                    position.update(status="REJECTED_ENTRY", entry_failure="INSUFFICIENT_CAPITAL")
                    continue
                _post(
                    portfolio,
                    ledger,
                    position,
                    slot,
                    "BUY",
                    (
                        (_available(SOL), SOL, -gross - network_fee),
                        (_external("venue", AccountKind.VENUE), SOL, curve),
                        (
                            _external("protocol", AccountKind.PROTOCOL_FEE),
                            SOL,
                            fees.protocol_fee_lamports,
                        ),
                        (
                            _external("creator", AccountKind.CREATOR_FEE),
                            SOL,
                            fees.creator_fee_lamports,
                        ),
                        (_external("network", AccountKind.NETWORK_FEE), SOL, network_fee),
                        (_available(AssetId(signal["mint"])), AssetId(signal["mint"]), tokens),
                        (_external("venue", AccountKind.VENUE), AssetId(signal["mint"]), -tokens),
                    ),
                )
                position.update(
                    status="OPEN",
                    remaining_tokens_atomic=tokens,
                    entry_cost_lamports=gross + network_fee,
                )
            else:
                tokens = position["remaining_tokens_atomic"]
                quoted_sell = sell_quote(
                    current,
                    tokens_in_atomic=tokens,
                    fee_profile=profile,
                    effective_at_unix_s=reserve["block_time"],
                )
                if portfolio.available(SOL) < network_fee:
                    position["exit_failure"] = "INSUFFICIENT_NETWORK_FEE_CAPITAL"
                    continue
                fees = quoted_sell.fees
                _post(
                    portfolio,
                    ledger,
                    position,
                    slot,
                    "SELL",
                    (
                        (
                            _external("venue", AccountKind.VENUE),
                            SOL,
                            -quoted_sell.gross_curve_sol_lamports,
                        ),
                        (_available(SOL), SOL, quoted_sell.sol_out_lamports - network_fee),
                        (
                            _external("protocol", AccountKind.PROTOCOL_FEE),
                            SOL,
                            fees.protocol_fee_lamports,
                        ),
                        (
                            _external("creator", AccountKind.CREATOR_FEE),
                            SOL,
                            fees.creator_fee_lamports,
                        ),
                        (_external("network", AccountKind.NETWORK_FEE), SOL, network_fee),
                        (_available(AssetId(signal["mint"])), AssetId(signal["mint"]), -tokens),
                        (_external("venue", AccountKind.VENUE), AssetId(signal["mint"]), tokens),
                    ),
                )
                proceeds = quoted_sell.sol_out_lamports - network_fee
                position.update(
                    status="CLOSED",
                    remaining_tokens_atomic=0,
                    exit_proceeds_lamports=proceeds,
                    closed_pnl_lamports=proceeds - position["entry_cost_lamports"],
                )
        except PumpQuoteError as error:
            # Quote rejection happens before submission; no fictitious landed fee is charged.
            position["entry_failure" if side == "BUY" else "exit_failure"] = error.code.value
            if side == "BUY":
                position["status"] = "REJECTED_ENTRY"
    rows = sorted(positions.values(), key=lambda r: (r["leader_entry_slot"], r["mint"]))
    cash_delta = portfolio.available(SOL) - policy.initial_sol_lamports
    closed = sum(r["closed_pnl_lamports"] for r in rows if r["status"] == "CLOSED")
    open_basis = sum(r["entry_cost_lamports"] for r in rows if r["status"] == "OPEN")
    if cash_delta != closed - open_basis:
        raise RuntimeError("ledger cash does not reconcile to closed PnL and open cost basis")
    summary = {
        "format": "dnipro-leader-slot-research-result/v1",
        "exact_execution": False,
        "source_trade_count": len(trades),
        "selected_pairs": len(pairs),
        "selection_exclusions": exclusions,
        "statuses": dict(sorted(Counter(r["status"] for r in rows).items())),
        "initial_sol_lamports": policy.initial_sol_lamports,
        "final_available_sol_lamports": portfolio.available(SOL),
        "cash_delta_lamports": cash_delta,
        "closed_trade_pnl_lamports": closed,
        "open_position_cost_basis_lamports": open_basis,
        "leader_observed_subset_pnl_lamports": sum(r["leader_observed_pnl_lamports"] for r in rows),
        "ledger_transaction_count": portfolio.transaction_count,
        "excluded_mints": list(policy.excluded_mints),
        "assumptions": [
            "One buy and one full sell per mint; future pairing selects a restricted sample.",
            "Own orders land first in signal slot + 1; slot coverage and placement are unproven.",
            "Normal fixed-supply curve math; Token-2022/mode/program evidence is not exact.",
            "Exogenous historical market; hypothetical trades do not change later events.",
            "Quote rejection is pre-submit and retains tokens; no retry or forced zero valuation.",
            "Leader transaction fees are proxies unless explicit copy fees are supplied.",
            "Jito tips, rent, landed failures, slippage competition, and other venues are absent.",
            "This result is not a complete-wallet or all-launchpad PnL backtest.",
        ],
    }
    return summary, rows, ledger


def _available(asset: AssetId) -> LedgerAccount:
    return LedgerAccount(available_account_id(asset), AccountKind.PORTFOLIO_AVAILABLE)


def _external(name: str, kind: AccountKind) -> LedgerAccount:
    return LedgerAccount(AccountId("dnipro:" + name), kind)


def _post(
    portfolio: PortfolioState,
    ledger: list[dict[str, Any]],
    position: dict[str, Any],
    slot: int,
    side: str,
    postings: tuple[tuple[LedgerAccount, AssetId, int], ...],
) -> None:
    identity = domain_digest(
        "dnipro-slot-order/v1", {"mint": position["mint"], "slot": slot, "side": side}
    )
    transaction = LedgerTransaction(
        transaction_id=identity,
        correlation_kind=LedgerCorrelationKind.ORDER,
        correlation_id=identity,
        boundary_ordinal=slot << 32,
        postings=tuple(
            Posting(account, asset, amount) for account, asset, amount in postings if amount
        ),
        reason="DNIPRO_RESEARCH_" + side,
    )
    portfolio.apply(transaction)
    ledger.append(
        {
            "transaction_id": identity.hex,
            "mint": position["mint"],
            "side": side,
            "assumed_slot": slot,
            "postings": [
                {
                    "account": p.account.account_id.value,
                    "kind": p.account.kind.value,
                    "asset": p.asset_id.value,
                    "amount_atomic": p.amount_atomic,
                }
                for p in transaction.postings
            ],
        }
    )
