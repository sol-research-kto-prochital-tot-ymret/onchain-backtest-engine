from dataclasses import replace

import pytest

import backtest.engine.state as state_module
from backtest.adapters.source.sniper_silver import _envelope
from backtest.domain.execution import ExecutionMode
from backtest.domain.identifiers import AssetId, PoolId, VenueId
from backtest.domain.market_events import (
    REFERENCE_AMM_TRADE_PAYLOAD_SCHEMA_ID,
    BlockEvent,
    SwapEvent,
    reference_amm_trade_payload,
)
from backtest.engine.state import (
    HistoricalReferenceState,
    PoolSnapshot,
    SimulationVenueState,
    VenueStateCommit,
)


def swap(slot, pool):
    return SwapEvent(
        _envelope(slot, 0, 0, pool),
        VenueId(pool),
        AssetId("SOL"),
        AssetId(pool),
        1,
        1,
        (),
        REFERENCE_AMM_TRADE_PAYLOAD_SCHEMA_ID,
        reference_amm_trade_payload(
            asset_a_id=AssetId("SOL"),
            asset_b_id=AssetId(pool),
            reserve_a_after_atomic=10,
            reserve_b_after_atomic=20,
        ),
    )


def test_historical_delta_retains_untouched_pools_assets_and_clocks():
    state = HistoricalReferenceState()
    clock = BlockEvent(_envelope(1, -1, 0, "clock"), 1000, None)
    state.apply_group((clock,))
    state.apply_group((swap(1, "A"),))
    state.apply_group((swap(2, "B"),))
    assert state.pool(PoolId("A")).reserve_a_atomic == 10
    assert state.pool(PoolId("B")).reserve_b_atomic == 20
    assert state.block_time_ns(clock.envelope.boundary_ordinal) == 1000
    assert {AssetId("A"), AssetId("B"), AssetId("SOL")} <= state._assets


def test_failed_group_does_not_leak_earlier_staged_pool_or_assets(monkeypatch):
    state = HistoricalReferenceState()
    state.apply_group((swap(1, "A"),))
    pools = dict(state._pools)
    assets = set(state._assets)
    times = dict(state._block_times_ns)
    first = swap(2, "B")
    second = replace(swap(2, "C"), envelope=_envelope(2, 0, 1, "C"))
    constructor = state_module.PoolSnapshot
    calls = 0

    def fail(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("invalid staged reserve")
        return constructor(**kwargs)

    monkeypatch.setattr(state_module, "PoolSnapshot", fail)
    with pytest.raises(ValueError, match="invalid staged reserve"):
        state.apply_group((first, second))
    assert state._pools == pools and state._assets == assets and state._block_times_ns == times


def test_venue_patch_commit_is_atomic_for_untouched_records_and_does_not_alias():
    venue = SimulationVenueState(ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY)
    a = PoolSnapshot(PoolId("A"), AssetId("SOL"), AssetId("A"), 10, 20, 1)
    b = PoolSnapshot(PoolId("B"), AssetId("SOL"), AssetId("B"), 30, 40, 2)
    venue.commit(VenueStateCommit({a.pool_id: a, b.pool_id: b}))
    patch = {a.pool_id: replace(a, reserve_a_atomic=15)}
    venue.commit(VenueStateCommit(patch, replace_all=False))
    patch.clear()
    assert venue.pool(a.pool_id).reserve_a_atomic == 15
    assert venue.pool(b.pool_id) == b
    venue.commit(VenueStateCommit({}, replace_all=False))
    assert venue.pool(b.pool_id) == b


def test_full_venue_commit_keeps_original_replace_semantics():
    venue = SimulationVenueState(ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY)
    a = PoolSnapshot(PoolId("A"), AssetId("SOL"), AssetId("A"), 10, 20, 1)
    venue.commit(VenueStateCommit({a.pool_id: a}))
    venue.commit(VenueStateCommit({}))
    assert venue.pool(a.pool_id) is None
