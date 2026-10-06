"""Offline canary using the existing real ClickHouse export, when available."""

import json
import socket
from pathlib import Path

import pytest

from backtest.adapters.artifacts.dnipro import load, publish
from backtest.adapters.source.clickhouse.carbon import deduplicate, normalize_state, normalize_trade
from backtest.domain.hashing import canonical_json_bytes
from backtest.plugins.strategies.leader_slot_copy import LeaderSlotPolicy, replay


def test_real_nya_day_is_reproducible_and_needs_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Path(__file__).resolve().parents[4] / (
        "solana-live-indexer/reports/2026-09-30-nya-next-block-backtest"
    )
    if not source.is_dir():
        pytest.skip("local frozen ClickHouse export is not present")

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("offline backtest attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setenv("SQD_API_KEY", "sentinel-not-a-real-secret")
    wallet = "nya666pQkP3PzWxi7JngU3rRMHuc7zbLK8c8wxQ4qpT"
    trades = deduplicate(
        normalize_trade(json.loads(line), wallet)
        for line in (source / "nya_trades_24h.jsonl").read_text().splitlines()
    )
    states = [
        normalize_state(json.loads(line))
        for line in (source / "market_slot_states.jsonl").read_text().splitlines()
    ]
    snapshot = tmp_path / "snapshot"
    publish(snapshot, trades, states, {"wallet": wallet})
    _, trades, states = load(snapshot)
    policy = LeaderSlotPolicy(1_790_631_266, 1_790_717_666, 1_000_000_000_000)
    first = replay(trades, states, policy)
    second = replay(trades, states, policy)
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    summary, positions, ledger = first
    assert summary["source_trade_count"] == 4141
    assert summary["selected_pairs"] == 1947
    assert summary["statuses"] == {"CLOSED": 1864, "OPEN": 83}
    assert len(ledger) == 3811
    assert all(p["remaining_tokens_atomic"] > 0 for p in positions if p["status"] == "OPEN")
    assert summary["cash_delta_lamports"] == (
        summary["closed_trade_pnl_lamports"] - summary["open_position_cost_basis_lamports"]
    )
