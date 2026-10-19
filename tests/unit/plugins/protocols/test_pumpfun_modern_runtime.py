from dataclasses import replace

import pytest

from backtest.plugins.protocols.pumpfun.modern_runtime import (
    PumpReplayRuntime,
    ReplayError,
    ReplayOrder,
    ReplayPosition,
    StateObservation,
)
from backtest.plugins.protocols.pumpfun.quotes import SOL_MINT, FeeConfig, QuoteState


def position(slot: int = 10, tx: int = 2, outer: int = 1, signature: str = "tx") -> ReplayPosition:
    return ReplayPosition(slot, tx, outer, 100 + slot, signature)


def state(base: str = "A", quote: str = SOL_MINT) -> QuoteState:
    return QuoteState(
        kind="pool",
        version="v2",
        base_mint=base,
        quote_mint=quote,
        virtual_base_reserves=1_000_000,
        virtual_quote_reserves=-30,
        real_base_reserves=1_000_000,
        real_quote_reserves=1_000_000,
        quote_vault_amount=1_000_030,
        protocol_fees=20,
        creator_fees=10,
        fees=FeeConfig(protocol_bps=30, creator_bps=20),
        creator_beneficiary="creator",
    )


def observe(
    runtime: PumpReplayRuntime,
    venue: str = "pool",
    value: QuoteState | None = None,
    stamp: ReplayPosition | None = None,
) -> StateObservation:
    observation = StateObservation(
        venue,
        value or state(),
        stamp or position(tx=1, signature="source"),
        "source:" + venue,
        "after_transaction",
    )
    runtime.observe(observation)
    return observation


def order(**changes: object) -> ReplayOrder:
    values: dict[str, object] = {
        "order_id": "buy",
        "position": position(),
        "venue_ids": ("pool",),
        "input_mint": SOL_MINT,
        "output_mint": "A",
        "amount_in": 1000,
        "network_fee_lamports": 5,
    }
    values.update(changes)
    return ReplayOrder(**values)  # type: ignore[arg-type]


def test_atomic_fill_native_units_and_fee_once() -> None:
    runtime = PumpReplayRuntime({SOL_MINT: 10_000})
    observe(runtime)
    request = order()
    receipt = runtime.execute(request)
    assert receipt.status == "filled"
    assert runtime.balances[SOL_MINT] == 8995
    assert runtime.balances["A"] == receipt.amount_out
    assert runtime.execute(request) == receipt
    assert runtime.balances[SOL_MINT] == 8995
    assert runtime.receipt_count == 1


def test_slippage_failure_keeps_all_route_state_and_principal() -> None:
    runtime = PumpReplayRuntime({SOL_MINT: 10_000})
    before = observe(runtime)
    receipt = runtime.execute(order(min_amount_out=1_000_000))
    assert receipt.status == "slippage_failed"
    assert receipt.amount_in == receipt.amount_out == 0
    assert runtime.balances == {SOL_MINT: 9995}
    assert runtime.observation("pool") == before


def test_missing_pool_offset_is_unresolved_not_zero() -> None:
    runtime = PumpReplayRuntime({SOL_MINT: 10_000})
    observe(runtime, value=replace(state(), virtual_quote_reserves=None))
    assert runtime.execute(order()).status == "unresolved"
    assert runtime.balances == {SOL_MINT: 10_000}


def test_future_or_same_transaction_after_state_not_executable() -> None:
    runtime = PumpReplayRuntime({SOL_MINT: 10_000})
    observe(runtime, stamp=position())
    assert runtime.execute(order()).status == "unresolved"


def test_later_outer_can_use_explicit_after_outer_state() -> None:
    runtime = PumpReplayRuntime({SOL_MINT: 10_000})
    runtime.observe(
        StateObservation("pool", state(), position(outer=0), "event", "after_outer_instruction")
    )
    assert runtime.execute(order()).status == "filled"


def test_multi_hop_has_no_intermediate_wallet_balance_or_double_network_fee() -> None:
    runtime = PumpReplayRuntime({SOL_MINT: 10_000})
    observe(runtime)
    observe(runtime, "child", state("B", "A"))
    receipt = runtime.execute(order(venue_ids=("pool", "child"), output_mint="B", mode="multi_hop"))
    assert receipt.status == "filled"
    assert "A" not in runtime.balances
    assert runtime.balances[SOL_MINT] == 8995
    assert receipt.route is not None and len(receipt.route.legs) == 2
    assert all(runtime.observation(venue).position == position() for venue in ("pool", "child"))


def test_native_quote_is_not_converted_to_sol() -> None:
    runtime = PumpReplayRuntime({SOL_MINT: 100, "USDC": 5000})
    observe(runtime, value=state("A", "USDC"))
    receipt = runtime.execute(order(input_mint="USDC"))
    assert receipt.status == "filled"
    assert runtime.balances["USDC"] == 4000
    assert runtime.balances[SOL_MINT] == 95


def test_missing_ancestor_is_unresolved_and_atomic() -> None:
    runtime = PumpReplayRuntime({SOL_MINT: 10_000})
    before = observe(runtime, value=state("B", "A"))
    assert runtime.execute(order(output_mint="B")).status == "unresolved"
    assert runtime.observation("pool") == before
    assert runtime.balances == {SOL_MINT: 10_000}


def test_insufficient_network_reserve_is_rejected_without_submission() -> None:
    runtime = PumpReplayRuntime({SOL_MINT: 1000})
    observe(runtime)
    assert runtime.execute(order()).status == "rejected"
    assert runtime.balances == {SOL_MINT: 1000}


def test_repeated_signature_across_slots_does_not_reapply_snapshot() -> None:
    runtime = PumpReplayRuntime({})
    original = observe(runtime)
    duplicate = replace(
        original, position=replace(original.position, slot=12), event_id="changed-slot-id"
    )
    assert runtime.observe(duplicate) is False
    assert runtime.observation("pool") == original


def test_conflicting_snapshot_identity_rejected() -> None:
    runtime = PumpReplayRuntime({})
    original = observe(runtime)
    with pytest.raises(ReplayError, match="conflicting"):
        runtime.observe(replace(original, state=replace(state(), real_base_reserves=99)))


def test_sweep_changes_no_trader_balance_or_pool_price() -> None:
    runtime = PumpReplayRuntime({SOL_MINT: 100})
    before = observe(runtime)
    after = runtime.sweep("pool", position())
    assert after.state.effective_quote_reserves == before.state.effective_quote_reserves
    assert after.state.protocol_fees == after.state.creator_fees == 0
    assert runtime.balances == {SOL_MINT: 100}


def test_holder_reward_accrual_is_not_developer_or_trader_income() -> None:
    runtime = PumpReplayRuntime({SOL_MINT: 10_000})
    observe(runtime, value=replace(state(), creator_beneficiary="holder_rewards"))
    assert runtime.execute(order()).status == "filled"
    assert runtime.balances[SOL_MINT] == 8995
    assert runtime.observation("pool").state.creator_fees > 10


def test_order_identity_conflict_and_duplicate_transaction_rejected() -> None:
    runtime = PumpReplayRuntime({SOL_MINT: 10_000})
    observe(runtime)
    runtime.execute(order())
    with pytest.raises(ReplayError, match="different execution inputs"):
        runtime.execute(order(amount_in=1001))
    with pytest.raises(ReplayError, match="one atomic route"):
        runtime.execute(order(order_id="another"))


def crossing_state(base: str = "A", quote: str = SOL_MINT) -> QuoteState:
    return QuoteState(
        kind="curve",
        version="v3",
        base_mint=base,
        quote_mint=quote,
        virtual_base_reserves=10000,
        virtual_quote_reserves=10000,
        real_base_reserves=1000,
        real_quote_reserves=1000,
        quote_vault_amount=1000,
        curve_base_token_balance=6000,
        pool_migration_fee=100,
        fees=FeeConfig(5, 30),
        creator_beneficiary="creator",
    )


def test_synthetic_unspent_quote_is_not_debited() -> None:
    runtime = PumpReplayRuntime({SOL_MINT: 10_000})
    observe(runtime, value=crossing_state())
    receipt = runtime.execute(order(amount_in=1119))
    assert receipt.status == "filled"
    assert receipt.route is not None
    assert receipt.amount_in == receipt.route.legs[0].quote.amount_in == 1117
    assert runtime.balances[SOL_MINT] == 8878


def test_sequential_intermediate_unspent_quote_stays_in_native_mint() -> None:
    runtime = PumpReplayRuntime({SOL_MINT: 10_000})
    observe(runtime, value=replace(state(), fees=FeeConfig(0, 0)))
    observe(runtime, "child", crossing_state("B", "A"))
    receipt = runtime.execute(order(amount_in=1122, venue_ids=("pool", "child"), output_mint="B"))
    assert receipt.status == "filled"
    assert runtime.balances[SOL_MINT] == 8873
    assert runtime.balances["A"] == 2
    assert runtime.balances["B"] == 1000


def test_submitted_transactions_cannot_share_a_chain_position() -> None:
    runtime = PumpReplayRuntime({SOL_MINT: 10_000})
    observe(runtime)
    assert runtime.execute(order(min_amount_out=1_000_000)).status == "slippage_failed"
    with pytest.raises(ReplayError, match="conflicting transaction identity"):
        runtime.execute(order(order_id="second", position=position(signature="other")))
    assert runtime.balances == {SOL_MINT: 9995}


def test_observations_across_venues_must_agree_on_transaction_identity() -> None:
    runtime = PumpReplayRuntime({})
    observe(runtime)
    conflicting = StateObservation(
        "other-pool",
        state("B"),
        position(tx=1, signature="different-source"),
        "other-event",
        "after_transaction",
    )
    with pytest.raises(ReplayError, match="conflicting transaction identity"):
        runtime.observe(conflicting)


def test_protocol_and_creator_sweeps_are_independent_and_price_neutral() -> None:
    runtime = PumpReplayRuntime({SOL_MINT: 100})
    original = observe(runtime)
    protocol = runtime.sweep("pool", position(), component="protocol")
    assert protocol.state.protocol_fees == 0
    assert protocol.state.creator_fees == 10
    assert protocol.state.quote_vault_amount == 1_000_010
    assert protocol.state.virtual_quote_reserves == -10
    creator = runtime.sweep("pool", position(outer=2), component="creator")
    assert creator.state.creator_fees == 0
    assert creator.state.quote_vault_amount == 1_000_000
    assert creator.state.virtual_quote_reserves == 0
    assert creator.state.effective_quote_reserves == original.state.effective_quote_reserves
    assert runtime.balances == {SOL_MINT: 100}
