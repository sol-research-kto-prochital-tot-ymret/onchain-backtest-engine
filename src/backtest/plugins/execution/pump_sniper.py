"""Pump quote adapter for the existing ReferenceBacktestEngine ports.

Scheduling, reservations, ledger commits and rejected order release remain
owned by the fork's engine. This adapter supplies upstream Pump quote plans.
"""

from __future__ import annotations

from typing import Any, cast

from backtest.domain.chain import SOLANA_MAINNET_NETWORK_ID
from backtest.domain.execution import (
    ExecutionMode,
    ExecutionPlan,
    ExecutionRejected,
    Fill,
    VenueTransition,
)
from backtest.domain.hashing import domain_digest
from backtest.domain.identifiers import AccountId, AssetId, BundleId, PoolId, VenueId
from backtest.domain.intents import SwapExactInIntent
from backtest.domain.ledger import AccountKind, LedgerAccount, Posting
from backtest.engine.portfolio import available_account_id, reserved_account_id
from backtest.engine.sniping_contracts import (
    liquidity_policy_id_for_execution_mode,
    synthetic_liquidity_account_id,
)
from backtest.engine.state import PoolSnapshot, SimulationVenueState
from backtest.plugins.protocols.pumpfun.model import (
    PUMP_BUY_EXACT_GROSS_SOL_FORMULA_V1,
    PUMP_SELL_EXACT_TOKEN_IN_FORMULA_V1,
    PUMP_STATIC_PROGRAM_CONTRACT_V1,
    PumpCurveLifecycle,
    PumpCurveStateV1,
    PumpFeeProfile,
    PumpMode,
    PumpQuoteError,
    PumpSellLiquidityPolicy,
    buy_quote,
    sell_quote,
)

SOL = AssetId("So11111111111111111111111111111111111111112")


def curve_state(pool: PoolSnapshot, birth: dict[str, Any]) -> PumpCurveStateV1:
    real_quote = (
        pool.reserve_a_atomic
        - int(birth["initial_virtual_quote_raw"])
        + int(birth.get("initial_real_quote_raw") or 0)
    )
    real_base = (
        pool.reserve_b_atomic
        - int(birth["initial_virtual_base_raw"])
        + int(birth["initial_real_base_raw"])
    )
    if min(real_quote, real_base) < 0:
        raise ValueError("historical real reserve shortfall")
    mode = PumpMode.TOKEN_2022 if birth["token_program"].startswith("Tokenz") else PumpMode.NORMAL
    return PumpCurveStateV1(
        pool.reserve_b_atomic,
        pool.reserve_a_atomic,
        real_base,
        real_quote,
        int(birth["supply_raw"]),
        PumpCurveLifecycle.ACTIVE if real_base else PumpCurveLifecycle.COMPLETED,
        mode,
    )


def profile(bps: tuple[int, int], now: int) -> PumpFeeProfile:
    return PumpFeeProfile(
        "retained-event-observed-bps-v1",
        PUMP_STATIC_PROGRAM_CONTRACT_V1,
        PUMP_BUY_EXACT_GROSS_SOL_FORMULA_V1,
        PUMP_SELL_EXACT_TOKEN_IN_FORMULA_V1,
        now,
        now + 1,
        *bps,
    )


def posting(account: str, kind: AccountKind, asset: AssetId, amount: int) -> Posting:
    return Posting(LedgerAccount(AccountId(account), kind), asset, amount)


class PumpSniperExecutionModel:
    def __init__(
        self,
        births: dict[str, dict[str, Any]],
        policy: Any,
        mode: ExecutionMode = ExecutionMode.EXOGENOUS_REPLAY,
    ):
        self.births, self.policy = births, policy
        self.mode = mode
        self.liquidity_policy = PumpSellLiquidityPolicy(
            "real-reserve-capped-v1"
            if mode is ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY
            else liquidity_policy_id_for_execution_mode(mode)
        )
        self.fees: dict[str, tuple[int, int]] = {}
        self.blocked: dict[str, str] = {}
        self.now = 0
        self.quotes: dict[str, dict[str, int]] = {}
        self.conditional: Any = None

    def reconcile_conditional_group(self, events: Any, historical: Any, venue: Any) -> None:
        if self.conditional is None:
            raise ValueError("Pump conditional reducer has not been configured")
        self.conditional.reconcile(events, historical, venue)

    def market_pool(self, mint: str, context: Any) -> PoolSnapshot | None:
        if self.conditional is not None:
            return cast(PoolSnapshot | None, self.conditional.venue.pool(PoolId(mint)))
        return cast(PoolSnapshot | None, context.observed_market.pool(PoolId(mint)))

    @property
    def bundle_id(self) -> BundleId:
        return BundleId(
            domain_digest(
                "dnipro.pump-sniper-execution.v1",
                {
                    "quotes": "upstream-pump-model",
                    "fees": "reserved-native-ledger",
                    "execution_mode": self.mode.value,
                    "liquidity_policy": self.liquidity_policy.value,
                },
            ).hex
        )

    def execute(
        self,
        intent: SwapExactInIntent,
        venue_state: SimulationVenueState,
        *,
        boundary_ordinal: int,
        mode: ExecutionMode,
    ) -> ExecutionPlan:
        if mode is not self.mode:
            raise ExecutionRejected("UNSUPPORTED_REPLAY_MODE")
        mint = intent.pool_id.value
        if mint in self.blocked:
            # An uncertified source interval cannot establish a landed transaction.
            fee = (
                0
                if self.blocked[mint] == "UNCERTIFIED_SOURCE_RANGE"
                or self.blocked[mint].startswith("CONDITIONAL_")
                else intent.execution_fee_reserve_atomic
            )
            raise ExecutionRejected(self.blocked[mint], execution_fee_atomic=fee)
        pool = venue_state.pool(intent.pool_id)
        if pool is None:
            raise ExecutionRejected(
                "UNKNOWN_POOL", execution_fee_atomic=intent.execution_fee_reserve_atomic
            )
        try:
            state = curve_state(pool, self.births[mint])
            fee_profile = profile(self.fees[mint], self.now)
            buying = intent.sold_asset_id == SOL
            rent = self.policy.account_rent_lamports if buying else 0
            network = intent.execution_fee_reserve_atomic
            shortfall = observed_real_sol = 0
            if buying:
                quote = buy_quote(
                    state,
                    spendable_gross_sol_lamports=intent.amount_in_atomic - rent,
                    fee_profile=fee_profile,
                    effective_at_unix_s=self.now,
                )
                tokens, gross, curve = (
                    quote.tokens_out_atomic,
                    quote.gross_sol_spent_lamports,
                    quote.net_curve_sol_lamports,
                )
                protocol_fee, creator_fee = (
                    quote.fees.protocol_fee_lamports,
                    quote.fees.creator_fee_lamports,
                )
                if tokens < intent.minimum_amount_out_atomic:
                    raise ExecutionRejected("MINIMUM_OUTPUT_NOT_MET", execution_fee_atomic=network)
                entries = [
                    posting(
                        reserved_account_id(SOL).value,
                        AccountKind.PORTFOLIO_RESERVED,
                        SOL,
                        -intent.amount_in_atomic,
                    ),
                    posting("venue:" + mint, AccountKind.VENUE, SOL, curve),
                    posting("venue:" + mint, AccountKind.VENUE, AssetId(mint), -tokens),
                    posting(
                        available_account_id(AssetId(mint)).value,
                        AccountKind.PORTFOLIO_AVAILABLE,
                        AssetId(mint),
                        tokens,
                    ),
                ]
                if rent:
                    entries.append(posting("rent:" + mint, AccountKind.PORTFOLIO_LOCKED, SOL, rent))
                change = intent.amount_in_atomic - rent - gross
                if change:
                    entries.append(
                        posting(
                            available_account_id(SOL).value,
                            AccountKind.PORTFOLIO_AVAILABLE,
                            SOL,
                            change,
                        )
                    )
                output, fill_fee = tokens, protocol_fee + creator_fee
            else:
                sale = sell_quote(
                    state,
                    tokens_in_atomic=intent.amount_in_atomic,
                    fee_profile=fee_profile,
                    effective_at_unix_s=self.now,
                    liquidity_policy=self.liquidity_policy,
                )
                tokens, curve, output = (
                    intent.amount_in_atomic,
                    sale.gross_curve_sol_lamports,
                    sale.sol_out_lamports,
                )
                protocol_fee, creator_fee = (
                    sale.fees.protocol_fee_lamports,
                    sale.fees.creator_fee_lamports,
                )
                shortfall = sale.synthetic_shortfall_lamports
                observed_real_sol = sale.observed_real_sol_reserves_lamports
                if output < intent.minimum_amount_out_atomic:
                    raise ExecutionRejected("MINIMUM_OUTPUT_NOT_MET", execution_fee_atomic=network)
                entries = [
                    posting(
                        reserved_account_id(AssetId(mint)).value,
                        AccountKind.PORTFOLIO_RESERVED,
                        AssetId(mint),
                        -tokens,
                    ),
                    posting("venue:" + mint, AccountKind.VENUE, AssetId(mint), tokens),
                    posting(
                        available_account_id(SOL).value,
                        AccountKind.PORTFOLIO_AVAILABLE,
                        SOL,
                        output,
                    ),
                ]
                # Match the fork's Sniping settlement: distinguish historical
                # venue funding from the explicitly synthetic research shortfall.
                if curve > shortfall:
                    entries.append(
                        posting("venue:" + mint, AccountKind.VENUE, SOL, shortfall - curve)
                    )
                if shortfall:
                    account = synthetic_liquidity_account_id(
                        protocol_namespace="pumpfun",
                        network_id=SOLANA_MAINNET_NETWORK_ID,
                        venue_id=VenueId(mint),
                    )
                    entries.append(posting(account.value, AccountKind.EXTERNAL, SOL, -shortfall))
                refund = self.policy.account_rent_refund_lamports
                if refund:
                    entries += [
                        posting("rent:" + mint, AccountKind.PORTFOLIO_LOCKED, SOL, -refund),
                        posting(
                            available_account_id(SOL).value,
                            AccountKind.PORTFOLIO_AVAILABLE,
                            SOL,
                            refund,
                        ),
                    ]
                gross, fill_fee = output, 0  # Sell fee is in quote, not the sold token.
            for fee, kind, label in (
                (protocol_fee, AccountKind.PROTOCOL_FEE, "protocol"),
                (creator_fee, AccountKind.CREATOR_FEE, "creator"),
            ):
                if fee:
                    entries.append(posting(label + ":" + mint, kind, SOL, fee))
            if network:
                entries += [
                    posting(
                        reserved_account_id(SOL).value,
                        AccountKind.PORTFOLIO_RESERVED,
                        SOL,
                        -network,
                    ),
                    posting("network:successful", AccountKind.NETWORK_FEE, SOL, network),
                ]
            self.quotes[intent.order_id.hex] = {
                "tokens": tokens,
                "user_quote": gross,
                "protocol_fee": protocol_fee,
                "creator_fee": creator_fee,
                "network_fee": network,
                "gross_curve_quote": curve,
                "observed_real_sol": observed_real_sol,
                "synthetic_shortfall": shortfall,
            }
            fill = Fill(
                intent.order_id,
                intent.pool_id,
                intent.sold_asset_id,
                intent.bought_asset_id,
                intent.amount_in_atomic,
                output,
                fill_fee,
                boundary_ordinal,
            )
            return ExecutionPlan(
                VenueTransition(
                    intent.pool_id,
                    state.virtual_sol_reserves_lamports + (curve if buying else -curve),
                    state.virtual_token_reserves_atomic + (-tokens if buying else tokens),
                )
                if self.mode is ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY
                else None,
                (fill,),
                tuple(entries),
                ("UPSTREAM_PUMP_QUOTE", self.mode.value, self.liquidity_policy.value),
            )
        except ExecutionRejected:
            raise
        except (PumpQuoteError, ValueError, KeyError) as error:
            code = (
                error.code.value
                if isinstance(error, PumpQuoteError)
                else "INVALID_HISTORICAL_STATE"
            )
            raise ExecutionRejected(
                code, execution_fee_atomic=intent.execution_fee_reserve_atomic
            ) from error
