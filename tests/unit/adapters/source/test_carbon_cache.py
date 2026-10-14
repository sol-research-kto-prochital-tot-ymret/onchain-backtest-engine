from __future__ import annotations

import fcntl
import json
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import patch

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backtest.adapters.source.carbon_cache import CarbonParquetSource, convert_cache
from backtest.adapters.source.carbon_cache.convert import atomic_json, file_hash, verify_cache
from backtest.adapters.source.carbon_cache.normalize import SOL, public_key, pump_curve
from backtest.domain.execution import ExecutionMode
from backtest.domain.hashing import domain_digest
from backtest.domain.identifiers import AssetId, BundleId
from backtest.domain.market_events import BlockEvent, VenueTradeEvent
from backtest.engine.reference import ReferenceBacktestEngine, ReferenceRunConfig, SlotLatencyModel
from backtest.engine.replay import HistoricalEventSource
from backtest.plugins.execution import ConstantProductExecutionModel
from backtest.plugins.risk import StaticRiskPolicy

MINT = public_key([1] * 32)
USER = public_key([2] * 32)
CURVE = pump_curve(MINT)
POOL = public_key([3] * 32)


def instruction(
    path: list[int],
    name: str,
    payload: dict[str, Any],
    *,
    protocol: str = "pumpfun",
    tx: int = 0,
    success: bool = True,
    priority: int = 1,
    accounts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = {
        "slot": 100,
        "signature": f"signature-{tx}",
        "transaction_index": tx,
        "absolute_path": path,
        "block_time": 1000,
        "fee_payer": USER,
        "fee_lamports": 5000,
        "success": success,
        "decoder": protocol,
        "decoded": {
            "type": "CpiEvent",
            "data": {"data": {name: payload}, "accounts": accounts or {}},
        },
    }
    return dict(
        raw_json=json.dumps(raw),
        source="sqd" if priority == 2 else "yellowstone",
        source_priority=priority,
        ingested_at="2026-10-14T00:00:00Z",
        commitment="confirmed",
        **{
            k: raw[k]
            for k in (
                "slot",
                "signature",
                "transaction_index",
                "absolute_path",
                "block_time",
                "fee_payer",
                "fee_lamports",
                "success",
                "decoder",
            )
        },
    )


def trade(**changes: Any) -> dict[str, Any]:
    return {
        "mint": MINT,
        "user": USER,
        "quote_mint": SOL,
        "is_buy": True,
        "token_amount": 100_000,
        "sol_amount": 100_000_000,
        "virtual_sol_reserves": 30_100_000_000,
        "virtual_token_reserves": 1_000_000_000,
        "real_sol_reserves": 100_000_000,
        "real_token_reserves": 500_000_000,
        "fee": 950_000,
        "creator_fee": 300_000,
        "cashback": 0,
        "mayhem_mode": False,
    } | changes


def raw_cache(root: Path, rows: list[dict[str, Any]], *, complete: bool = True) -> Path:
    root.mkdir()
    (root / "chunks").mkdir()
    (root / "receipts").mkdir()
    (root / ".export.lock").touch()
    path = root / "chunks" / "000000000100-000000000102.parquet"
    pq.write_table(pa.Table.from_pylist(rows), path)
    record = {
        "state": "complete",
        "file": "chunks/" + path.name,
        "from_slot": 100,
        "to_slot_exclusive": 102,
        "rows": len(rows),
        "bytes": path.stat().st_size,
        "sha256": file_hash(path),
        "sql_sha256": "0" * 64,
        "wall_seconds": 1.5,
    }
    atomic_json(root / "receipts" / (path.stem + ".json"), record)
    atomic_json(
        root / "manifest.json",
        {
            "version": 1,
            "complete": complete,
            "from_slot": 100,
            "to_slot_exclusive": 102,
            "total_chunks": 1,
            "columns": [
                {"name": f.name, "type": str(f.type)} for f in pq.ParquetFile(path).schema_arrow
            ],
            "config": {
                "start": 1000,
                "end": 1100,
                "table": "solana.instructions",
                "decoders": [],
                "null_time": "include",
            },
            "records": [record],
        },
    )
    return root


def converted(tmp_path: Path, rows: list[dict[str, Any]]) -> CarbonParquetSource:
    raw = raw_cache(tmp_path / "raw", rows)
    output = tmp_path / "engine"
    convert_cache(raw, output, reserve_gb=0, memory_mb=64)
    return CarbonParquetSource(output)


def test_deduplicates_with_outer_authority_and_preserves_fees(tmp_path: Path) -> None:
    source = converted(
        tmp_path,
        [
            instruction([0, 1], "TradeEvent", trade(), priority=1),
            instruction([0, 1], "TradeEvent", trade(sol_amount=200_000_000), priority=2),
        ],
    )
    rows = [r for r in source.rows() if r["kind"] == "VENUE_TRADE"]
    assert rows[0]["sold_amount"] == 200_000_000
    assert rows[0]["source"] == "sqd"
    assert rows[0]["source_priority"] == 2
    assert source.manifest["counters"]["duplicate_versions"] == 1
    events = list(source.events())
    assert isinstance(events[0], BlockEvent)
    swap = next(e for e in events if isinstance(e, VenueTradeEvent))
    assert swap.venue_id.value == CURVE
    assert swap.reserve_a_after_atomic == 30_100_000_000
    assert swap.fee_amount_atomic == 1_250_000
    assert len(list(source.rows("transactions"))) == 1


def test_equal_priority_economics_conflict_is_not_committed(tmp_path: Path) -> None:
    raw = raw_cache(
        tmp_path / "raw",
        [
            instruction([0, 1], "TradeEvent", trade()),
            instruction([0, 1], "TradeEvent", trade(sol_amount=2)),
        ],
    )
    with pytest.raises(ValueError, match="equally authoritative"):
        convert_cache(raw, tmp_path / "engine", reserve_gb=0)
    assert not (tmp_path / "engine" / "COMMITTED").exists()


def test_failed_launch_and_trade_never_become_market_events(tmp_path: Path) -> None:
    source = converted(
        tmp_path,
        [
            instruction([0, 1], "CreateEvent", {"mint": MINT}, success=False),
            instruction([1, 1], "TradeEvent", trade(), success=False),
        ],
    )
    assert Counter(e.kind.name for e in source.events()) == {"BLOCK": 1}
    assert Counter(r["reason"] for r in source.rows("evidence")) == {"failed_transaction": 2}
    tx = list(source.rows("transactions"))
    assert len(tx) == 1 and tx[0]["fee_lamports"] == 5000 and tx[0]["success"] is False


def test_router_leg_is_audit_only(tmp_path: Path) -> None:
    source = converted(
        tmp_path,
        [
            instruction(
                [0, 1],
                "SwapEvent",
                {"input_mint": SOL, "output_mint": MINT, "input_amount": 1, "output_amount": 10},
                protocol="jupiter_swap",
            )
        ],
    )
    assert not any(isinstance(e, VenueTradeEvent) for e in source.events())
    assert "router_observation" in next(source.rows("evidence"))["reason"]


def test_pumpswap_before_reserves_do_not_become_after_liquidity(tmp_path: Path) -> None:
    payload = {
        "base_amount_out": 100,
        "quote_amount_in": 1000,
        "user_quote_amount_in": 990,
        "pool": POOL,
        "user": USER,
        "pool_base_token_reserves": 50000,
        "pool_quote_token_reserves": 5000,
        "lp_fee": 2,
        "protocol_fee": 3,
        "coin_creator_fee": 4,
    }
    source = converted(
        tmp_path,
        [
            instruction(
                [0, 1],
                "BuyEvent",
                payload,
                protocol="pump_swap",
                accounts={"base_mint": MINT, "quote_mint": SOL},
            )
        ],
    )
    event = next(e for e in source.events() if isinstance(e, VenueTradeEvent))
    assert event.reserve_a_after_atomic is None and event.reserve_b_after_atomic is None
    fact = next(r for r in source.rows() if r["kind"] == "VENUE_TRADE")
    assert fact["quote_reserve_before"] == 5000 and fact["user_quote_amount"] == 990


def test_unresolved_pumpswap_assets_are_audited(tmp_path: Path) -> None:
    source = converted(
        tmp_path,
        [
            instruction(
                [0, 1],
                "SellEvent",
                {"pool": POOL, "user": USER, "base_amount_in": 10, "quote_amount_out": 1},
                protocol="pump_swap",
            )
        ],
    )
    assert not any(isinstance(e, VenueTradeEvent) for e in source.events())
    assert next(source.rows("evidence"))["reason"] == "unmapped_event"


def test_launch_and_migration_keep_correct_venues(tmp_path: Path) -> None:
    source = converted(
        tmp_path,
        [
            instruction(
                [0, 0],
                "CreateEvent",
                {
                    "mint": MINT,
                    "quote_mint": SOL,
                    "creator": USER,
                    "user": USER,
                    "bonding_curve": CURVE,
                },
            ),
            instruction(
                [1, 0],
                "CompletePumpAmmMigrationEvent",
                {"mint": MINT, "bonding_curve": CURVE, "pool": POOL},
            ),
        ],
    )
    rows = [r for r in source.rows() if r["kind"] != "BLOCK"]
    assert rows[0]["kind"] == "TOKEN_LAUNCH"
    assert rows[1]["venue_id"] == CURVE
    assert rows[1]["destination_venue"] == POOL


def test_duplicate_event_versions_not_double_counted(tmp_path: Path) -> None:
    payload = {
        "pool": POOL,
        "trade_direction": 1,
        "swap_result": {"included_fee_input_amount": 10, "output_amount": 100, "trading_fee": 1},
    }
    roles = {"base_mint": MINT, "quote_mint": SOL, "user": USER}
    source = converted(
        tmp_path,
        [
            instruction([0, 1], "EvtSwap", payload, protocol="meteora_dbc", accounts=roles),
            instruction([0, 2], "EvtSwap2", payload, protocol="meteora_dbc", accounts=roles),
        ],
    )
    assert sum(isinstance(e, VenueTradeEvent) for e in source.events()) == 1
    assert source.manifest["counters"]["superseded_dbc_event_version"] == 1


def test_resuming_is_idempotent_and_raw_is_unchanged(tmp_path: Path) -> None:
    source = converted(tmp_path, [instruction([0, 1], "TradeEvent", trade())])
    original = {
        str(p.relative_to(tmp_path / "raw")): file_hash(p)
        for p in (tmp_path / "raw").rglob("*")
        if p.is_file()
    }
    marker = (source.root / "COMMITTED").read_bytes()
    convert_cache(tmp_path / "raw", source.root, reserve_gb=0)
    assert (source.root / "COMMITTED").read_bytes() == marker
    assert original == {
        str(p.relative_to(tmp_path / "raw")): file_hash(p)
        for p in (tmp_path / "raw").rglob("*")
        if p.is_file()
    }


def test_prepared_partition_recovers_after_manifest_update_crash(tmp_path: Path) -> None:
    source = converted(tmp_path, [instruction([0, 1], "TradeEvent", trade())])
    manifest = json.loads((source.root / "manifest.json").read_text())
    manifest["complete"] = False
    manifest["records"] = []
    atomic_json(source.root / "manifest.json", manifest)
    (source.root / "COMMITTED").unlink()
    convert_cache(tmp_path / "raw", source.root, reserve_gb=0)
    assert verify_cache(source.root)["complete"] is True


def test_corrupt_output_is_not_overwritten_on_resume(tmp_path: Path) -> None:
    source = converted(tmp_path, [instruction([0, 1], "TradeEvent", trade())])
    path = source.root / source.manifest["records"][0]["files"]["events"]["file"]
    path.write_bytes(b"bad")
    with pytest.raises(ValueError, match="checksum"):
        convert_cache(tmp_path / "raw", source.root, reserve_gb=0)
    assert path.read_bytes() == b"bad"


def test_corrupt_raw_refused(tmp_path: Path) -> None:
    raw = raw_cache(tmp_path / "raw", [instruction([0, 1], "TradeEvent", trade())])
    next((raw / "chunks").glob("*.parquet")).write_bytes(b"bad")
    with pytest.raises(ValueError, match="checksum"):
        convert_cache(raw, tmp_path / "engine", reserve_gb=0)


def test_partial_input_and_manifest_holes_refused(tmp_path: Path) -> None:
    raw = raw_cache(tmp_path / "raw", [instruction([0, 1], "TradeEvent", trade())], complete=False)
    with pytest.raises(ValueError, match="complete"):
        convert_cache(raw, tmp_path / "engine", reserve_gb=0)
    manifest = json.loads((raw / "manifest.json").read_text())
    manifest["complete"] = True
    manifest["to_slot_exclusive"] = 103
    atomic_json(raw / "manifest.json", manifest)
    with pytest.raises(ValueError, match="cover frozen plan"):
        convert_cache(raw, tmp_path / "engine", reserve_gb=0)


def test_boundaries_are_ordered_and_source_has_engine_contract(tmp_path: Path) -> None:
    source = converted(
        tmp_path,
        [
            instruction([0, 1], "TradeEvent", trade(), tx=2),
            instruction([0, 1], "TradeEvent", trade(), tx=1),
        ],
    )
    assert isinstance(source, HistoricalEventSource)
    assert [b.transaction_index for b in source.boundaries()] == [-1, 1, 2]
    assert list(CarbonParquetSource(source.root, start_slot=101, end_slot=102).events()) == []


def test_source_window_and_output_directory_guards(tmp_path: Path) -> None:
    source = converted(tmp_path, [instruction([0, 1], "TradeEvent", trade())])
    with pytest.raises(ValueError, match="outside"):
        CarbonParquetSource(source.root, start_slot=99)
    with pytest.raises(ValueError, match="separate directories"):
        convert_cache(tmp_path / "raw", tmp_path / "raw" / "converted", reserve_gb=0)


def test_native_reference_engine_replays_source(tmp_path: Path) -> None:
    source = converted(tmp_path, [instruction([0, 1], "TradeEvent", trade())])

    class ObserveOnly:
        bundle_id = BundleId(domain_digest("carbon-cache-test-observe", {}).hex)
        component_id = domain_digest("carbon-cache-test-component", {})

        def on_event(self, event: Any, context: Any) -> tuple[()]:
            return ()

        def on_execution(self, notification: Any, context: Any) -> None:
            pass

    summary = ReferenceBacktestEngine().run(
        source=source,
        strategy=ObserveOnly(),
        execution_model=ConstantProductExecutionModel(fee_bps=30),
        risk_policy=StaticRiskPolicy(maximum_order_input_atomic=1_000_000),
        config=ReferenceRunConfig(
            execution_mode=ExecutionMode.EXOGENOUS_REPLAY,
            latency=SlotLatencyModel(),
            root_seed=1,
            initial_available={AssetId(SOL): 1_000_000_000},
        ),
    )
    assert summary.historical_event_count == 2
    assert summary.accepted_order_count == 0


def test_commit_marker_crash_recovery(tmp_path: Path) -> None:
    source = converted(tmp_path, [instruction([0, 1], "TradeEvent", trade())])
    (source.root / "COMMITTED").unlink()
    convert_cache(tmp_path / "raw", source.root, reserve_gb=0)
    assert verify_cache(source.root)["complete"] is True


def test_rejects_concurrent_raw_writer(tmp_path: Path) -> None:
    raw = raw_cache(tmp_path / "raw", [instruction([0, 1], "TradeEvent", trade())])
    with (raw / ".export.lock").open("r") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            convert_cache(raw, tmp_path / "engine", reserve_gb=0)


def test_rejects_reused_transaction_index(tmp_path: Path) -> None:
    first = instruction([0, 1], "TradeEvent", trade())
    second = instruction([0, 1], "TradeEvent", trade(), tx=1)
    raw = json.loads(second["raw_json"])
    raw["transaction_index"] = 0
    second.update(transaction_index=0, raw_json=json.dumps(raw))
    root = raw_cache(tmp_path / "raw", [first, second])
    with pytest.raises(ValueError, match="multiple signatures"):
        convert_cache(root, tmp_path / "engine", reserve_gb=0)


def test_converter_ignores_apple_metadata_sidecars(tmp_path: Path) -> None:
    raw = raw_cache(tmp_path / "raw", [instruction([0, 1], "TradeEvent", trade())])
    output = tmp_path / "engine"
    output.mkdir()
    (output / "._.convert.lock").write_bytes(b"metadata")
    convert_cache(raw, output, reserve_gb=0)
    assert verify_cache(output)["complete"] is True


def test_sort_memory_retry_cannot_lose_or_duplicate_rows(tmp_path: Path) -> None:
    from backtest.adapters.source.carbon_cache.convert import copy_sorted_range, sorted_batches

    source = tmp_path / "input.parquet"
    values = [{"slot": 102}, {"slot": 100}, {"slot": 101}, {"slot": 100}]
    pq.write_table(pa.Table.from_pylist(values), source)
    attempts = []

    def limited(src, dst, columns, order, low, high, memory, spill):
        attempts.append((low, high))
        if high - low > 1:
            dst.write_bytes(b"partial COPY output")
            raise duckdb.OutOfMemoryException("test budget")
        copy_sorted_range(src, dst, columns, order, low, high, memory, spill)

    counters = Counter()
    work = tmp_path / "sort-work"
    with patch("backtest.adapters.source.carbon_cache.convert.copy_sorted_range", limited):
        rows = [
            row
            for batch in sorted_batches(source, ["slot"], "slot", 100, 103, 64, work, counters)
            for row in batch.to_pylist()
        ]
    assert rows == [{"slot": 100}, {"slot": 100}, {"slot": 101}, {"slot": 102}]
    assert counters["sort_memory_retries"] == 2
    assert counters["sort_ranges"] == 3
    assert not work.exists()


def test_single_slot_sort_failure_is_bounded_and_cleans_temporary_output(tmp_path: Path) -> None:
    from backtest.adapters.source.carbon_cache.convert import sorted_batches

    source = tmp_path / "input.parquet"
    pq.write_table(pa.Table.from_pylist([{"slot": 100}]), source)
    work = tmp_path / "sort-work"
    with (
        patch(
            "backtest.adapters.source.carbon_cache.convert.copy_sorted_range",
            side_effect=duckdb.OutOfMemoryException("test"),
        ),
        pytest.raises(ValueError, match="One slot"),
    ):
        list(sorted_batches(source, ["slot"], "slot", 100, 101, 64, work, Counter()))
    assert not work.exists()


def test_duplicate_versions_in_one_slot_stay_together_after_memory_retry(tmp_path: Path) -> None:
    from backtest.adapters.source.carbon_cache.convert import copy_sorted_range

    raw = raw_cache(
        tmp_path / "raw",
        [
            instruction([0, 1], "TradeEvent", trade(), priority=1),
            instruction([0, 1], "TradeEvent", trade(sol_amount=2), priority=2),
        ],
    )

    def limited(src, dst, columns, order, low, high, memory, spill):
        if src.name.startswith("000") and high - low > 1:
            raise duckdb.OutOfMemoryException("test budget")
        copy_sorted_range(src, dst, columns, order, low, high, memory, spill)

    output = tmp_path / "engine"
    with patch("backtest.adapters.source.carbon_cache.convert.copy_sorted_range", limited):
        convert_cache(raw, output, memory_mb=64, reserve_gb=0)
    source = CarbonParquetSource(output)
    assert source.manifest["counters"]["duplicate_versions"] == 1
    assert [r["sold_amount"] for r in source.rows() if r["kind"] == "VENUE_TRADE"] == [2]
