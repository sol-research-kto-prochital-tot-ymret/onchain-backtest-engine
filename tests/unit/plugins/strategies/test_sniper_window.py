from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from backtest.adapters.source.sniper_silver import SOL, _envelope
from backtest.bootstrap.sniper_window import run
from backtest.domain.execution import ExecutionMode, ExecutionRejected
from backtest.domain.hashing import canonical_json_bytes, domain_digest
from backtest.domain.identifiers import (
    AccountId,
    AssetId,
    DatasetRevisionId,
    LogicalContentHash,
    OrderId,
    PoolId,
    ProtocolPayloadSchemaId,
    VenueId,
)
from backtest.domain.intents import SwapExactInIntent
from backtest.domain.market_events import (
    REFERENCE_AMM_TRADE_PAYLOAD_SCHEMA_ID,
    BlockEvent,
    TokenLaunchEvent,
    VenueTradeEvent,
    canonical_event_sort_key,
    reference_amm_trade_payload,
)
from backtest.engine.portfolio import PortfolioState
from backtest.engine.reference import _release_transaction, _reservation_transaction
from backtest.engine.replay import ReplayBoundary
from backtest.plugins.execution.pump_sniper import PumpSniperExecutionModel
from backtest.plugins.protocols.pumpfun.model import (
    PumpCurveLifecycle,
    PumpCurveStateV1,
    PumpFeeProfile,
    PumpMode,
    buy_quote,
    sell_quote,
)
from backtest.plugins.strategies.sniper_window import SniperWindowPolicy, SniperWindowStrategy

T = 1_791_648_000


class MemorySource:
    """Small hermetic source exercising the actual fork's replay and ledger."""

    def __init__(
        self,
        organic: bool = True,
        gaps: tuple[tuple[int, int], ...] = (),
        creator_address: str = "creator",
        creator_sale_bps: int = 10_000,
        creator_sale_slot: int = 102,
    ) -> None:
        self.manifest = {"from_slot": 100}
        self.source_hash = "1" * 64
        self.manifest_hash = self.source_hash
        self.empty_block_evidence_hash = None
        self.proven_empty_slots = []
        self.input_policy = {
            "cohort_start_utc": "2026-10-10T16:00:00Z",
            "cohort_end_utc": "2026-10-10T17:00:00Z",
        }
        self.times = {slot: T + slot - 100 for slot in range(100, 500)}
        self.gaps, self.unsupported = gaps, {}
        birth = {
            "mint": "TOKEN",
            "symbol": "TEST",
            "creator": creator_address,
            "slot": 100,
            "launch_signer": "creator",
            "initial_virtual_quote_raw": 30_000_000_000,
            "initial_virtual_base_raw": 1_073_000_000_000_000,
            "initial_real_base_raw": 793_100_000_000_000,
            "initial_real_quote_raw": 0,
            "supply_raw": 1_000_000_000_000_000,
            "token_program": "Tokenz-test",
        }
        self.births, self.birth_by_mint = [birth], {"TOKEN": birth}
        self.meta: dict[str, dict[str, Any]] = {}
        self.trade_rows: list[dict[str, Any]] = []
        values = []
        envelope = _envelope(100, 0, 0, "launch")
        values.append(
            TokenLaunchEvent(
                envelope,
                AssetId("TOKEN"),
                AccountId(creator_address),
                AccountId("creator"),
                VenueId("TOKEN"),
                SOL,
                ProtocolPayloadSchemaId("test-launch-v1"),
                canonical_json_bytes({"kind": "launch"}),
            )
        )
        self.meta[envelope.canonical_event_id.hex] = {"kind": "launch", "row": birth}
        state = PumpCurveStateV1(
            birth["initial_virtual_base_raw"],
            birth["initial_virtual_quote_raw"],
            birth["initial_real_base_raw"],
            0,
            birth["supply_raw"],
            PumpCurveLifecycle.ACTIVE,
            PumpMode.TOKEN_2022,
        )
        fees = PumpFeeProfile.static_95_30(
            profile_id="test", effective_from_unix_s=T, effective_until_unix_s=T + 500
        )
        creator_tokens = 0
        for slot, wallet, budget in sorted(
            (
                (100, "creator", 5_000_000_000),
                (101, "sniper", 10_000_000_000),
                (creator_sale_slot, "creator", 0),
                (103, "organic", 20_000_000_000),
            ),
            key=lambda item: item[0],
        ):
            if wallet == "organic" and not organic:
                continue
            if budget:
                q = buy_quote(
                    state,
                    spendable_gross_sol_lamports=budget,
                    fee_profile=fees,
                    effective_at_unix_s=T + slot - 100,
                )
                amount, tokens = q.net_curve_sol_lamports, q.tokens_out_atomic
                after = replace(
                    state,
                    virtual_sol_reserves_lamports=state.virtual_sol_reserves_lamports + amount,
                    virtual_token_reserves_atomic=state.virtual_token_reserves_atomic - tokens,
                    real_sol_reserves_lamports=state.real_sol_reserves_lamports + amount,
                    real_token_reserves_atomic=state.real_token_reserves_atomic - tokens,
                )
                if wallet == "creator":
                    creator_tokens = tokens
            else:
                q = sell_quote(
                    state,
                    tokens_in_atomic=creator_tokens * creator_sale_bps // 10_000,
                    fee_profile=fees,
                    effective_at_unix_s=T + slot - 100,
                )
                amount, tokens = (
                    q.gross_curve_sol_lamports,
                    creator_tokens * creator_sale_bps // 10_000,
                )
                after = replace(
                    state,
                    virtual_sol_reserves_lamports=state.virtual_sol_reserves_lamports - amount,
                    virtual_token_reserves_atomic=state.virtual_token_reserves_atomic + tokens,
                    real_sol_reserves_lamports=state.real_sol_reserves_lamports - amount,
                    real_token_reserves_atomic=state.real_token_reserves_atomic + tokens,
                )
            envelope = _envelope(slot, 0 if slot == 100 else 1, 1 if slot == 100 else 0, str(slot))
            sold, bought = (SOL, AssetId("TOKEN")) if budget else (AssetId("TOKEN"), SOL)
            values.append(
                VenueTradeEvent(
                    envelope,
                    VenueId("TOKEN"),
                    sold,
                    bought,
                    amount if budget else tokens,
                    tokens if budget else amount,
                    (),
                    REFERENCE_AMM_TRADE_PAYLOAD_SCHEMA_ID,
                    reference_amm_trade_payload(
                        asset_a_id=SOL,
                        asset_b_id=AssetId("TOKEN"),
                        reserve_a_after_atomic=after.virtual_sol_reserves_lamports,
                        reserve_b_after_atomic=after.virtual_token_reserves_atomic,
                    ),
                )
            )
            row = {
                "mint": "TOKEN",
                "trader": wallet,
                "side": "buy" if budget else "sell",
                "quote": amount,
                "tokens": tokens,
                "virtual_quote": after.virtual_sol_reserves_lamports,
                "virtual_base": after.virtual_token_reserves_atomic,
                "real_quote": after.real_sol_reserves_lamports,
                "real_base": after.real_token_reserves_atomic,
                "protocol_fee": q.fees.protocol_fee_lamports,
                "creator_fee": q.fees.creator_fee_lamports,
                "signature": str(slot),
                "fee_payer": wallet,
                "network_fee": 5000,
                "payload": {"fee_basis_points": 95, "creator_fee_basis_points": 30},
            }
            self.trade_rows.append(row)
            self.meta[envelope.canonical_event_id.hex] = {"kind": "trade", "row": row}
            state = after
        self.last_in_group = {
            e.envelope.canonical_event_id.hex for e in values if isinstance(e, VenueTradeEvent)
        }
        for slot, timestamp in self.times.items():
            values.append(BlockEvent(_envelope(slot, -1, 0, "clock"), timestamp * 10**9, None))
        self.values = tuple(sorted(values, key=canonical_event_sort_key))

    @property
    def dataset_revision_id(self):
        return DatasetRevisionId(self.source_hash)

    @property
    def logical_content_hash(self):
        return LogicalContentHash(self.source_hash)

    @property
    def replay_semantics_id(self):
        return domain_digest("test-replay", {})

    def boundaries(self):
        positions = {e.envelope.boundary_ordinal: e.envelope.position for e in self.values}
        return tuple(ReplayBoundary.from_position(positions[k]) for k in sorted(positions))

    def events(self):
        yield from self.values


def test_actual_reference_engine_closes_and_reconciles_ledger():
    summary, rows, sink = run(MemorySource(), SniperWindowPolicy())
    assert (
        summary["implementation"]["runner"] == "backtest.engine.reference.ReferenceBacktestEngine"
    )
    assert rows[0]["status"] == "CLOSED"
    assert rows[0]["entry_slot"] == 103
    assert rows[0]["exit_slot"] >= 104
    assert summary["cash_reconciliation_verified"]
    assert len(sink.fills) == 2
    for transaction in sink.ledger:
        assets = {p["asset_id"] for p in transaction["postings"]}
        assert all(
            sum(p["amount_atomic"] for p in transaction["postings"] if p["asset_id"] == a) == 0
            for a in assets
        )


def test_no_future_buy_still_times_out_with_native_clock():
    summary, rows, _ = run(
        MemorySource(organic=False), replace(SniperWindowPolicy(), holding_seconds=5)
    )
    assert rows[0]["status"] == "CLOSED"
    assert rows[0]["reason"] == "TIMEOUT"
    assert rows[0]["exit_slot"] == 109
    assert summary["realized_net_pnl_lamports"] < 0


def test_future_changes_do_not_change_entry_size():
    _, a, _ = run(MemorySource(), SniperWindowPolicy())
    _, b, _ = run(MemorySource(organic=False), SniperWindowPolicy())
    assert a[0]["budget_lamports"] == b[0]["budget_lamports"]
    assert a[0]["signal_slot"] == b[0]["signal_slot"]


def test_trace_preserves_native_orders_and_contains_causal_signal_state():
    policy = SniperWindowPolicy()
    a, rows_a, sink_a = run(MemorySource(), policy)
    b, rows_b, sink_b = run(MemorySource(), policy, trace_mints={"TOKEN"})
    assert a == b
    assert rows_a == rows_b
    assert sink_a.fills == sink_b.fills
    assert sink_a.ledger == sink_b.ledger
    assert not sink_a.strategy_trace
    signal = next(r for r in sink_b.strategy_trace if r["action"] == "ENTRY_SIGNAL")
    assert signal["slot"] == rows_b[0]["signal_slot"]
    assert signal["wallet_tokens"] == rows_b[0]["sniper_tokens"]
    assert signal["wallet_adjusted_cost"] == rows_b[0]["sniper_cost_lamports"]
    assert int(signal["after_real_sol"]) > 0
    assert any(r["action"] == "EXIT_EVALUATION" for r in sink_b.strategy_trace)
    assert any(r["record_type"] == "ORDER_FILLED_ATOMICALLY" for r in sink_b.order_audit)


def test_launch_age_wait_reevaluates_with_current_observed_state():
    _, rows, sink = run(
        MemorySource(),
        replace(SniperWindowPolicy(), minimum_entry_age_seconds=3),
        trace_mints={"TOKEN"},
    )
    assert "signal_slot" not in rows[0]  # Three seconds exceeds this source's early window.
    assert not sink.fills
    assert any(r["action"] == "WAIT_LAUNCH_AGE" for r in sink.strategy_trace)


def timestamp_window_source(first_later_age: int, organic: bool = False):
    source = MemorySource(organic=organic)
    for slot in source.times:
        if slot >= 103:
            source.times[slot] = T + first_later_age + slot - 103
    source.values = tuple(
        replace(event, block_time_ns=source.times[event.envelope.position.block_ordinal] * 10**9)
        if isinstance(event, BlockEvent)
        else event
        for event in source.values
    )
    return source


@pytest.mark.parametrize("age,expected_signal", [(9, 104), (10, 103), (20, 103), (21, None)])
def test_entry_window_uses_actual_seconds_and_inclusive_bounds(age, expected_signal):
    policy = replace(
        SniperWindowPolicy(),
        minimum_entry_age_seconds=10,
        maximum_entry_age_seconds=20,
        holding_seconds=5,
    )
    _, rows, sink = run(timestamp_window_source(age), policy, trace_mints={"TOKEN"})
    assert rows[0].get("signal_slot") == expected_signal
    signals = [r for r in sink.strategy_trace if r["action"] == "ENTRY_SIGNAL"]
    assert all(10 <= r["time"] - T <= 20 for r in signals)
    if expected_signal is not None:
        assert rows[0]["entry_slot"] == expected_signal + 1
    else:
        assert not sink.fills


def test_timestamp_window_clock_cannot_see_a_later_purchase_in_its_slot():
    policy = replace(
        SniperWindowPolicy(),
        minimum_entry_age_seconds=10,
        maximum_entry_age_seconds=20,
        holding_seconds=5,
    )
    _, a, _ = run(timestamp_window_source(10, organic=True), policy)
    _, b, _ = run(timestamp_window_source(10, organic=False), policy)
    assert a[0]["signal_slot"] == b[0]["signal_slot"] == 103
    assert a[0]["budget_lamports"] == b[0]["budget_lamports"]


def test_timestamp_window_validation_and_disabled_identity_compatibility():
    with pytest.raises(ValueError, match="maximum entry age"):
        replace(SniperWindowPolicy(), minimum_entry_age_seconds=20, maximum_entry_age_seconds=10)
    assert "maximum_entry_age_seconds" not in SniperWindowPolicy().document()
    active = replace(
        SniperWindowPolicy(), minimum_entry_age_seconds=10, maximum_entry_age_seconds=20
    )
    assert active.document()["maximum_entry_age_seconds"] == 20


def test_insufficient_capital_rejects_entry_without_fake_fill():
    summary, rows, sink = run(
        MemorySource(), replace(SniperWindowPolicy(), bankroll_lamports=1_000_000)
    )
    assert rows[0]["status"] == "ENTRY_REJECTED"
    assert not sink.fills
    assert summary["final_available_sol"] == "0.001000000"


def test_gap_keeps_native_inventory_and_cost_basis_open():
    summary, rows, _ = run(MemorySource(organic=False, gaps=((104, 104),)), SniperWindowPolicy())
    assert rows[0]["status"] == "OPEN"
    assert rows[0]["reason"] == "UNCERTIFIED_SOURCE_RANGE"
    assert summary["open_cash_cost_lamports"] > 0
    assert summary["realized_net_pnl_lamports"] == 0


def test_fee_reservation_and_failed_submission_are_one_balanced_release():
    token = AssetId("TOKEN")
    wallet = PortfolioState({token: 100, SOL: 10})
    intent = SwapExactInIntent(OrderId("1" * 64), PoolId("TOKEN"), token, SOL, 100, 0, 1, SOL, 7)
    wallet.apply(_reservation_transaction(intent, 1))
    assert wallet.available(SOL) == 3
    assert wallet.reserved(SOL) == 7
    release = _release_transaction(intent, 2, "MINIMUM_OUTPUT_NOT_MET", execution_fee_atomic=5)
    wallet.apply(release)
    assert wallet.available(SOL) == 5
    assert wallet.available(token) == 100
    assert wallet.reserved(SOL) == wallet.reserved(token) == 0


def test_unreserved_failed_fee_is_refused():
    intent = SwapExactInIntent(OrderId("1" * 64), PoolId("TOKEN"), SOL, AssetId("TOKEN"), 100, 0, 1)
    with pytest.raises(RuntimeError, match="exceeds its reservation"):
        _release_transaction(intent, 2, "FAILED", execution_fee_atomic=1)


def test_causal_delivery_id_preserves_order_within_a_transaction():
    a, b = _envelope(100, 1, 0, "hash-large"), _envelope(100, 1, 1, "hash-small")
    assert a.stable_causal_id.hex < b.stable_causal_id.hex


def test_rejects_unsupported_exact_tip_and_delay_claims():
    with pytest.raises(ValueError, match="tips"):
        SniperWindowPolicy(entry_tip_lamports=1)
    with pytest.raises(ValueError, match="equal positive"):
        SniperWindowPolicy(exit_delay_slots=2)
    assert ExecutionRejected("FAILED", execution_fee_atomic=7).execution_fee_atomic == 7


def test_native_engine_charges_failed_entry_fee_and_refunds_principal(monkeypatch):
    def fail(self, intent, *args, **kwargs):
        raise ExecutionRejected(
            "MINIMUM_OUTPUT_NOT_MET", execution_fee_atomic=intent.execution_fee_reserve_atomic
        )

    monkeypatch.setattr(PumpSniperExecutionModel, "execute", fail)
    summary, rows, sink = run(MemorySource(), SniperWindowPolicy())
    assert rows[0]["status"] == "ENTRY_FAILED"
    assert summary["realized_net_pnl_lamports"] == -105_000
    assert summary["final_available_sol"] == "9.999895000"
    assert not sink.fills


def test_native_reserved_fee_prevents_two_orders_spending_the_same_cash():
    wallet = PortfolioState({SOL: 10})
    first = SwapExactInIntent(
        OrderId("1" * 64), PoolId("TOKEN"), SOL, AssetId("TOKEN"), 5, 0, 1, SOL, 4
    )
    second = replace(first, order_id=OrderId("2" * 64))
    wallet.apply(_reservation_transaction(first, 1))
    from backtest.engine.contracts import PortfolioView
    from backtest.plugins.strategies.sniper_window import SniperWindowRisk

    assert not SniperWindowRisk().accept(second, PortfolioView(wallet))


def drained_source() -> MemorySource:
    """A real historical curve empties after our exogenous hypothetical buy."""
    source = MemorySource(organic=False)
    birth, last = source.births[0], source.trade_rows[-1]
    state = PumpCurveStateV1(
        last["virtual_base"],
        last["virtual_quote"],
        last["real_base"],
        last["real_quote"],
        birth["supply_raw"],
        PumpCurveLifecycle.ACTIVE,
        PumpMode.TOKEN_2022,
    )
    tokens = birth["initial_virtual_base_raw"] - last["virtual_base"]
    fees = PumpFeeProfile.static_95_30(
        profile_id="test", effective_from_unix_s=T, effective_until_unix_s=T + 500
    )
    quote = sell_quote(state, tokens_in_atomic=tokens, fee_profile=fees, effective_at_unix_s=T + 4)
    row = dict(
        last,
        slot=104,
        trader="historical-holders",
        side="sell",
        quote=quote.gross_curve_sol_lamports,
        tokens=tokens,
        virtual_quote=state.virtual_sol_reserves_lamports - quote.gross_curve_sol_lamports,
        virtual_base=state.virtual_token_reserves_atomic + tokens,
        real_quote=state.real_sol_reserves_lamports - quote.gross_curve_sol_lamports,
        real_base=state.real_token_reserves_atomic + tokens,
        protocol_fee=quote.fees.protocol_fee_lamports,
        creator_fee=quote.fees.creator_fee_lamports,
        signature="drain",
        fee_payer="historical-holders",
    )
    envelope = _envelope(104, 1, 0, "drain")
    event = VenueTradeEvent(
        envelope,
        VenueId("TOKEN"),
        AssetId("TOKEN"),
        SOL,
        tokens,
        quote.gross_curve_sol_lamports,
        (),
        REFERENCE_AMM_TRADE_PAYLOAD_SCHEMA_ID,
        reference_amm_trade_payload(
            asset_a_id=SOL,
            asset_b_id=AssetId("TOKEN"),
            reserve_a_after_atomic=row["virtual_quote"],
            reserve_b_after_atomic=row["virtual_base"],
        ),
    )
    source.trade_rows.append(row)
    source.meta[envelope.canonical_event_id.hex] = {"kind": "trade", "row": row}
    source.last_in_group.add(envelope.canonical_event_id.hex)
    source.values = tuple(sorted((*source.values, event), key=canonical_event_sort_key))
    return source


def test_strict_liquidity_does_not_manufacture_exit_on_empty_curve():
    summary, rows, sink = run(drained_source(), replace(SniperWindowPolicy(), holding_seconds=5))
    assert rows[0]["status"] == "OPEN"
    assert rows[0]["reason"] == "INSUFFICIENT_REAL_SOL_RESERVES"
    assert rows[0]["exit_attempts"] == 0
    assert summary["synthetic_shortfall_lamports"] == 0
    assert not any(p["account_kind"] == "EXTERNAL" for tx in sink.ledger for p in tx["postings"])


def test_virtual_exit_finishes_at_timeout_with_explicit_conserved_shortfall():
    policy = replace(SniperWindowPolicy(), holding_seconds=5)
    summary, rows, sink = run(drained_source(), policy, ExecutionMode.EXOGENOUS_VIRTUAL_SETTLEMENT)
    row = rows[0]
    assert row["status"] == "CLOSED"
    assert row["reason"] == "TIMEOUT"
    assert row["exit_time"] == row["entry_time"] + 6
    assert summary["open_positions"] == 0
    assert summary["realized_net_pnl_lamports"] < 0
    assert row["exit_synthetic_shortfall_lamports"] > 0
    assert row["exit_gross_curve_lamports"] == (
        row["exit_observed_real_sol_lamports"] + row["exit_synthetic_shortfall_lamports"]
    )
    external = [p for tx in sink.ledger for p in tx["postings"] if p["account_kind"] == "EXTERNAL"]
    assert -sum(p["amount_atomic"] for p in external) == summary["synthetic_shortfall_lamports"]
    assert all(p["account_id"].startswith("synthetic-liquidity:pumpfun:") for p in external)
    assert summary["synthetic_liquidity_reconciliation_verified"]
    assert summary["cash_reconciliation_verified"]
    _, strict, _ = run(drained_source(), policy)
    for key in ("entry_slot", "entry_cash_lamports", "budget_lamports", "remaining_tokens_atomic"):
        if key != "remaining_tokens_atomic":
            assert row[key] == strict[0][key]
    for tx in sink.ledger:
        for asset in {p["asset_id"] for p in tx["postings"]}:
            assert sum(p["amount_atomic"] for p in tx["postings"] if p["asset_id"] == asset) == 0


def conditional_source(organic=True):
    source = MemorySource(organic=organic)
    for row in source.trade_rows:
        row["payload"]["ix_name"] = "buy_exact_sol_in" if row["side"] == "buy" else "sell"
    return source


def test_reference_exit_flags_preserve_default_policy_identity():
    from dataclasses import asdict

    policy = SniperWindowPolicy()
    old = asdict(policy)
    for key in (
        "maximum_entry_age_seconds",
        "own_take_profit_enabled",
        "reference_profit_exit_enabled",
        "require_creator_flat_before_entry",
    ):
        old.pop(key)
    assert policy.document() == old
    active = replace(policy, own_take_profit_enabled=0, reference_profit_exit_enabled=1)
    assert active.document()["own_take_profit_enabled"] == 0
    assert active.document()["reference_profit_exit_enabled"] == 1
    with pytest.raises(ValueError, match="exit trigger"):
        replace(policy, reference_profit_exit_enabled=2)


@pytest.mark.parametrize(
    "source_args,expected",
    [
        ({}, "CREATOR_SOLD_ALL_OBSERVED_TOKENS"),
        ({"creator_address": "absent-creator"}, "NO_OBSERVED_CREATOR_BUY"),
    ],
)
def test_creator_flat_entry_keeps_original_sizing_and_profit_exit(source_args, expected):
    policy = SniperWindowPolicy()
    _, original, _ = run(MemorySource(**source_args), policy)
    _, gated, _ = run(
        MemorySource(**source_args), replace(policy, require_creator_flat_before_entry=1)
    )
    a, b = original[0], gated[0]
    assert b["creator_status_at_signal"] == expected
    assert b["creator_tokens_at_signal"] == 0
    assert b["creator_flat_entry_allowed"]
    assert b["reason"] == "TARGET"
    for key in ("signal_slot", "entry_slot", "exit_slot", "budget_lamports", "closed_pnl_lamports"):
        assert a[key] == b[key]


def test_creator_partial_sale_blocks_an_otherwise_valid_entry():
    _, original, _ = run(MemorySource(creator_sale_bps=5000), SniperWindowPolicy())
    _, gated, sink = run(
        MemorySource(creator_sale_bps=5000),
        replace(SniperWindowPolicy(), require_creator_flat_before_entry=1),
        trace_mints={"TOKEN"},
    )
    assert "entry_slot" in original[0]
    assert gated[0]["status"] == "NO_SIGNAL"
    assert not sink.fills
    assert any(
        e["action"] == "ENTRY_CREATOR_FLAT_CHECK" and e["creator_tokens_at_signal"] > 0
        for e in sink.strategy_trace
    )


def test_creator_full_sale_after_early_window_does_not_reopen_entry():
    _, rows, sink = run(
        MemorySource(creator_sale_slot=105),
        replace(SniperWindowPolicy(), require_creator_flat_before_entry=1),
        trace_mints={"TOKEN"},
    )
    assert rows[0]["status"] == "NO_SIGNAL"
    assert not sink.fills
    assert all(
        e["slot"] <= 102 for e in sink.strategy_trace if e["action"] == "ENTRY_CREATOR_FLAT_CHECK"
    )


@pytest.mark.parametrize(
    "tokens,bought,sold,outgoing", [(-1, 1, 1, 0), (1, 1, 1, 0), (0, 1, 0, 1), (0, 1, 0, 0)]
)
def test_creator_unknown_held_or_transferred_inventory_is_not_a_sellout(
    tokens, bought, sold, outgoing
):
    strategy, _, _, _, _ = reference_valuation_fixture()
    strategy.wallets["TOKEN"]["creator"].update(
        tokens=tokens, bought=bought, sold_tokens=sold, transferred_out_tokens=outgoing
    )
    assert not strategy.creator_entry_gate("TOKEN")["creator_flat_entry_allowed"]


def test_creator_flat_flag_preserves_old_policy_identity_when_disabled():
    assert "require_creator_flat_before_entry" not in SniperWindowPolicy().document()
    with pytest.raises(ValueError, match="creator flat"):
        replace(SniperWindowPolicy(), require_creator_flat_before_entry=2)


def test_disabling_own_target_waits_until_timeout_despite_large_profit():
    policy = replace(
        SniperWindowPolicy(), holding_seconds=5, maximum_entry_budget_lamports=200_000_000
    )
    _, before, _ = run(conditional_source(), policy, ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY)
    _, after, _ = run(
        conditional_source(),
        replace(policy, own_take_profit_enabled=0),
        ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY,
    )
    assert before[0]["reason"] == "TARGET"
    assert after[0]["reason"] == "TIMEOUT"
    assert after[0]["exit_slot"] == 109 > before[0]["exit_slot"]
    assert after[0]["closed_pnl_lamports"] > 0
    assert before[0]["budget_lamports"] == after[0]["budget_lamports"]


@pytest.mark.parametrize(
    "organic,reason,first_slot", [(True, "REFERENCE_PROFIT", 103), (False, "TIMEOUT", None)]
)
def test_reference_exit_is_causal_and_retains_timeout(organic, reason, first_slot):
    policy = replace(
        SniperWindowPolicy(),
        holding_seconds=5,
        maximum_entry_budget_lamports=200_000_000,
        own_take_profit_enabled=0,
        reference_profit_exit_enabled=1,
    )
    summary, rows, sink = run(
        conditional_source(organic),
        policy,
        ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY,
        trace_mints={"TOKEN"},
    )
    row = rows[0]
    assert row["status"] == "CLOSED"
    assert row["reason"] == reason
    assert row.get("reference_first_profitable_slot") == first_slot
    evaluations = [e for e in sink.strategy_trace if e["action"] == "REFERENCE_PROFIT_EVALUATION"]
    assert evaluations
    if organic:
        assert row["exit_slot"] == first_slot + 1
        assert row["reference_first_profit_lamports"] >= 0
    else:
        assert all(not e["reference_reached"] for e in evaluations)
        assert row["exit_slot"] == 109
    assert not sink.conditional_errors
    assert summary["cash_reconciliation_verified"]


def reference_valuation_fixture():
    from types import SimpleNamespace

    from backtest.plugins.execution.pump_sniper import profile

    policy = replace(
        SniperWindowPolicy(), own_take_profit_enabled=0, reference_profit_exit_enabled=1
    )
    source = MemorySource()
    execution = PumpSniperExecutionModel(
        source.birth_by_mint, policy, ExecutionMode.EXOGENOUS_REPLAY
    )
    execution.now = T + 4
    execution.fees["TOKEN"] = (95, 30)
    strategy = SniperWindowStrategy(source, execution, policy)
    strategy.rows["TOKEN"]["sniper_wallet"] = "sniper"
    raw = source.trade_rows[-1]
    state = PumpCurveStateV1(
        raw["virtual_base"],
        raw["virtual_quote"],
        raw["real_base"],
        raw["real_quote"],
        1_000_000_000_000_000,
        PumpCurveLifecycle.ACTIVE,
        PumpMode.TOKEN_2022,
    )
    context = SimpleNamespace(instant=SimpleNamespace(chain_slot=104, boundary_ordinal=104 << 32))
    tokens = source.trade_rows[1]["tokens"] // 2
    net = (
        sell_quote(
            state,
            tokens_in_atomic=tokens,
            fee_profile=profile((95, 30), execution.now),
            effective_at_unix_s=execution.now,
        ).sol_out_lamports
        - policy.exit_network_fee_lamports
    )
    return strategy, state, context, tokens, net


def test_reference_liquidation_accounts_for_partial_sales_and_sell_network_fee():
    strategy, state, context, tokens, net = reference_valuation_fixture()
    ledger = strategy.wallets["TOKEN"]["sniper"]
    # Half remains; original basis was 10 SOL, with the rest recovered by prior sales.
    ledger.update(tokens=tokens, cost=net + 1, bought=10_000_000_000)
    assert not strategy._reference_profit("TOKEN", state, context)
    ledger["cost"] -= 1
    assert strategy._reference_profit("TOKEN", state, context)  # Exactly fee-adjusted break-even.
    assert strategy.rows["TOKEN"]["reference_first_profit_lamports"] == 0


@pytest.mark.parametrize(
    "tokens,cost,expected", [(0, -1, True), (0, 1, False), (1, -1, True), (-1, -1, False)]
)
def test_reference_realized_cash_recovery_and_unproven_inventory(tokens, cost, expected):
    strategy, state, context, _, _ = reference_valuation_fixture()
    strategy.wallets["TOKEN"]["sniper"].update(tokens=tokens, cost=cost)
    assert strategy._reference_profit("TOKEN", state, context) is expected


def test_reference_unavailable_liquidity_does_not_create_profit_crossing():
    strategy, state, context, tokens, _ = reference_valuation_fixture()
    strategy.wallets["TOKEN"]["sniper"].update(tokens=tokens, cost=1)
    assert not strategy._reference_profit(
        "TOKEN", replace(state, real_sol_reserves_lamports=0), context
    )
    assert "reference_first_profitable_slot" not in strategy.rows["TOKEN"]


def test_conditional_native_replay_keeps_own_deposit_and_quotes_without_synthetic_funding():
    policy = replace(SniperWindowPolicy(), maximum_entry_budget_lamports=200_000_000)
    summary, rows, sink = run(
        conditional_source(), policy, ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY
    )
    assert rows[0]["status"] == "CLOSED"
    assert summary["own_reserve_impact_propagated"]
    assert summary["synthetic_shortfall_lamports"] == 0
    assert summary["conditional_repriced_events"] > 0
    assert not sink.conditional_errors
    assert not any(p["account_kind"] == "EXTERNAL" for tx in sink.ledger for p in tx["postings"])
    assert rows[0]["budget_lamports"] <= 200_000_000


def test_conditional_drained_historical_curve_can_return_our_own_remaining_deposit():
    source = drained_source()
    for row in source.trade_rows:
        row["payload"]["ix_name"] = "buy_exact_sol_in" if row["side"] == "buy" else "sell"
    source.trade_rows[-1]["trader"] = "sniper"
    policy = replace(
        SniperWindowPolicy(), holding_seconds=5, maximum_entry_budget_lamports=200_000_000
    )
    summary, rows, sink = run(source, policy, ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY)
    assert rows[0]["status"] == "CLOSED"
    assert summary["synthetic_shortfall_lamports"] == 0
    assert summary["open_positions"] == 0
    assert not sink.conditional_errors


def test_conditional_unknown_seller_does_not_manufacture_a_closed_profit():
    source = drained_source()
    for row in source.trade_rows:
        row["payload"]["ix_name"] = "buy_exact_sol_in" if row["side"] == "buy" else "sell"
    summary, rows, sink = run(
        source,
        replace(SniperWindowPolicy(), holding_seconds=5),
        ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY,
    )
    assert rows[0]["status"] == "OPEN"
    assert rows[0]["reason"] == "CONDITIONAL_SELLER_INVENTORY_CONFLICT"
    assert summary["open_cash_cost_lamports"] > 0
    assert sink.conditional_errors


def test_creator_filter_and_confirmation_reject_without_hindsight():
    policy = replace(SniperWindowPolicy(), require_creator_never_bought=1)
    _, rows, sink = run(MemorySource(), policy)
    assert not sink.fills and rows[0]["reason"] == "CREATOR_HAS_OBSERVED_BUY"
    policy = replace(
        SniperWindowPolicy(),
        confirmation_slots=1,
        confirmation_new_buyers=2,
        confirmation_net_lamports=100_000_000,
        entry_window_slots=8,
    )
    _, rows, sink = run(MemorySource(organic=False), policy)
    assert not sink.fills and "signal_slot" not in rows[0]


def test_new_budget_cap_and_early_stop_use_native_ledger():
    policy = replace(
        SniperWindowPolicy(),
        maximum_entry_budget_lamports=200_000_000,
        holding_seconds=5,
        stop_loss_bps=100,
    )
    _, rows, _ = run(
        MemorySource(organic=False), policy, ExecutionMode.EXOGENOUS_VIRTUAL_SETTLEMENT
    )
    assert rows[0]["status"] == "CLOSED" and rows[0]["reason"] == "STOP_LOSS"
    assert rows[0]["budget_lamports"] <= 200_000_000


def test_conditional_fraction_sale_preserves_full_exit_and_token_conservation():
    source = conditional_source()
    birth, last = source.births[0], source.trade_rows[-1]
    state = PumpCurveStateV1(
        last["virtual_base"],
        last["virtual_quote"],
        last["real_base"],
        last["real_quote"],
        birth["supply_raw"],
        PumpCurveLifecycle.ACTIVE,
        PumpMode.TOKEN_2022,
    )
    fees = PumpFeeProfile.static_95_30(
        profile_id="test",
        effective_from_unix_s=T,
        effective_until_unix_s=T + 500,
    )
    q = sell_quote(
        state, tokens_in_atomic=last["tokens"], fee_profile=fees, effective_at_unix_s=T + 4
    )
    raw = dict(
        last,
        slot=104,
        side="sell",
        quote=q.gross_curve_sol_lamports,
        virtual_quote=state.virtual_sol_reserves_lamports - q.gross_curve_sol_lamports,
        virtual_base=state.virtual_token_reserves_atomic + last["tokens"],
        real_quote=state.real_sol_reserves_lamports - q.gross_curve_sol_lamports,
        real_base=state.real_token_reserves_atomic + last["tokens"],
        protocol_fee=q.fees.protocol_fee_lamports,
        creator_fee=q.fees.creator_fee_lamports,
        signature="organic-close",
        payload={"ix_name": "sell", "fee_basis_points": 95, "creator_fee_basis_points": 30},
    )
    envelope = _envelope(104, 1, 0, "organic-close")
    event = VenueTradeEvent(
        envelope,
        VenueId("TOKEN"),
        AssetId("TOKEN"),
        SOL,
        raw["tokens"],
        raw["quote"],
        (),
        REFERENCE_AMM_TRADE_PAYLOAD_SCHEMA_ID,
        reference_amm_trade_payload(
            asset_a_id=SOL,
            asset_b_id=AssetId("TOKEN"),
            reserve_a_after_atomic=raw["virtual_quote"],
            reserve_b_after_atomic=raw["virtual_base"],
        ),
    )
    source.trade_rows.append(raw)
    source.meta[envelope.canonical_event_id.hex] = {"kind": "trade", "row": raw}
    source.last_in_group.add(envelope.canonical_event_id.hex)
    source.values = tuple(sorted((*source.values, event), key=canonical_event_sort_key))
    policy = replace(
        SniperWindowPolicy(), maximum_entry_budget_lamports=200_000_000, counterpart_sell_fraction=1
    )
    summary, _, sink = run(source, policy, ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY)
    assert not sink.conditional_errors
    replayed = next(
        r for r in sink.conditional_repriced if r["trader"] == "organic" and r["side"] == "sell"
    )
    assert replayed["replayed_tokens"] < replayed["historical_tokens"]
    assert replayed["real_sol_after"] >= 0
    assert summary["synthetic_shortfall_lamports"] == 0


def test_confirmation_clock_does_not_use_later_transactions_in_its_slot():
    policy = replace(
        SniperWindowPolicy(),
        confirmation_slots=1,
        entry_window_slots=8,
        maximum_entry_budget_lamports=200_000_000,
    )
    _, a, _ = run(MemorySource(organic=True), policy)
    _, b, _ = run(MemorySource(organic=False), policy)
    assert a[0]["signal_slot"] == b[0]["signal_slot"] == 103
    assert a[0]["budget_lamports"] == b[0]["budget_lamports"]
    # The organic purchase is tx 1 of slot 103, after that slot's clock decision.
    # It can change the delayed execution, but cannot change the prior signal.


def proxy_sale_source():
    source = conditional_source(organic=False)
    donor_buy = next(r for r in source.trade_rows if r["trader"] == "sniper")
    last = source.trade_rows[-1]
    state = PumpCurveStateV1(
        last["virtual_base"],
        last["virtual_quote"],
        last["real_base"],
        last["real_quote"],
        source.births[0]["supply_raw"],
        PumpCurveLifecycle.ACTIVE,
        PumpMode.TOKEN_2022,
    )
    fee_profile = PumpFeeProfile.static_95_30(
        profile_id="proxy-sale", effective_from_unix_s=T, effective_until_unix_s=T + 500
    )
    quote = sell_quote(
        state,
        tokens_in_atomic=donor_buy["tokens"],
        fee_profile=fee_profile,
        effective_at_unix_s=T + 4,
    )
    raw = dict(
        last,
        slot=104,
        trader="proxy",
        side="sell",
        tokens=donor_buy["tokens"],
        quote=quote.gross_curve_sol_lamports,
        virtual_quote=state.virtual_sol_reserves_lamports - quote.gross_curve_sol_lamports,
        virtual_base=state.virtual_token_reserves_atomic + donor_buy["tokens"],
        real_quote=state.real_sol_reserves_lamports - quote.gross_curve_sol_lamports,
        real_base=state.real_token_reserves_atomic + donor_buy["tokens"],
        protocol_fee=quote.fees.protocol_fee_lamports,
        creator_fee=quote.fees.creator_fee_lamports,
        signature="proxy-sale",
        instruction_path=[5],
        payload={"ix_name": "sell", "fee_basis_points": 95, "creator_fee_basis_points": 30},
    )
    envelope = _envelope(104, 1, 0, "proxy-sale")
    event = VenueTradeEvent(
        envelope,
        VenueId("TOKEN"),
        AssetId("TOKEN"),
        SOL,
        raw["tokens"],
        raw["quote"],
        (),
        REFERENCE_AMM_TRADE_PAYLOAD_SCHEMA_ID,
        reference_amm_trade_payload(
            asset_a_id=SOL,
            asset_b_id=AssetId("TOKEN"),
            reserve_a_after_atomic=raw["virtual_quote"],
            reserve_b_after_atomic=raw["virtual_base"],
        ),
    )
    source.trade_rows.append(raw)
    source.meta[envelope.canonical_event_id.hex] = {"kind": "trade", "row": raw, "path": [5]}
    source.last_in_group.add(envelope.canonical_event_id.hex)
    source.values = tuple(sorted((*source.values, event), key=canonical_event_sort_key))
    return source, donor_buy


@pytest.mark.parametrize("same_transaction", [True, False])
def test_evidenced_transfer_funds_proxy_sell_without_creating_tokens(same_transaction):
    source, donor_buy = proxy_sale_source()
    source.inventory_transfers = (
        {
            "event_uid": "proven-transfer",
            "mint": "TOKEN",
            "slot": 104,
            "tx_idx": 1 if same_transaction else 0,
            "instruction_path": [4],
            "source_wallet": "sniper",
            "destination_wallet": "proxy",
            "tokens": donor_buy["tokens"],
        },
    )
    policy = replace(
        SniperWindowPolicy(), maximum_entry_budget_lamports=200_000_000, counterpart_sell_fraction=1
    )
    summary, rows, sink = run(source, policy, ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY)
    assert not sink.conditional_errors
    assert summary["inventory_transfers_applied"] == 1
    assert rows[0]["developer_observed_tokens_raw"] == 0
    assert summary["cash_reconciliation_verified"]
    assert summary["synthetic_shortfall_lamports"] == 0
    assert any(r["trader"] == "proxy" and r["side"] == "sell" for r in sink.conditional_repriced)


@pytest.mark.parametrize("path,slot", [([6], 104), ([4], 106)])
def test_later_transfer_cannot_retroactively_fund_an_earlier_sell(path, slot):
    source, donor_buy = proxy_sale_source()
    source.inventory_transfers = (
        {
            "event_uid": "later-transfer",
            "mint": "TOKEN",
            "slot": slot,
            "tx_idx": 1,
            "instruction_path": path,
            "source_wallet": "sniper",
            "destination_wallet": "proxy",
            "tokens": donor_buy["tokens"],
        },
    )
    _, _, sink = run(
        source,
        replace(
            SniperWindowPolicy(),
            maximum_entry_budget_lamports=200_000_000,
            counterpart_sell_fraction=1,
        ),
        ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY,
    )
    assert any(
        e["reason"] == "CONDITIONAL_HISTORICAL_SELLER_BALANCE_UNPROVEN"
        for e in sink.conditional_errors
    )
    assert not sink.inventory_transfers_applied


def test_transfer_with_unknown_sender_balance_remains_unresolved():
    source = conditional_source(organic=False)
    source.inventory_transfers = (
        {
            "event_uid": "unknown-source",
            "mint": "TOKEN",
            "slot": 104,
            "tx_idx": 0,
            "instruction_path": [0],
            "source_wallet": "unseen-wallet",
            "destination_wallet": "proxy",
            "tokens": 1,
        },
    )
    summary, rows, sink = run(
        source,
        replace(SniperWindowPolicy(), maximum_entry_budget_lamports=200_000_000),
        ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY,
    )
    assert any(
        e["reason"] == "CONDITIONAL_TRANSFER_SOURCE_BALANCE_UNPROVEN"
        for e in sink.conditional_errors
    )
    assert not sink.inventory_transfers_applied
    assert rows[0]["status"] == "OPEN"
    assert summary["synthetic_shortfall_lamports"] == 0
