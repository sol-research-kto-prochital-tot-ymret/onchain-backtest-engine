from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backtest.adapters.source.carbon_cache.convert import atomic_json, file_hash
from backtest.adapters.source.raw_parquet import RawParquetSource
from backtest.domain.market_events import BlockEvent
from backtest.engine.replay import HistoricalEventSource


def dataset(root: Path, *, good_audit: bool = True) -> Path:
    records = {}
    for stage in ("raw", "translated"):
        part = root / stage / "00000000"
        part.mkdir(parents=True)
        tables = {}
        if stage == "raw":
            tables["transactions"] = pa.Table.from_pylist(
                [
                    {"slot": 100, "transaction_index": 1, "signature": "second"},
                    {"slot": 100, "transaction_index": 0, "signature": "first"},
                ]
            )
        else:
            tables["transfers"] = pa.Table.from_pylist(
                [
                    {
                        "slot": 100,
                        "transaction_index": 0,
                        "ordinal": 2,
                        "execution_status": "unknown",
                        "amount_raw": 2,
                    },
                    {
                        "slot": 100,
                        "transaction_index": 0,
                        "ordinal": 0,
                        "execution_status": "committed",
                        "amount_raw": 1,
                    },
                    {
                        "slot": 100,
                        "transaction_index": 0,
                        "ordinal": 1,
                        "execution_status": "reverted",
                        "amount_raw": 3,
                    },
                ]
            )
            block = {
                "slot": 100,
                "transaction_index": -1,
                "event_index": 0,
                "signature": "",
                "kind": "BLOCK",
                "block_time": 1700000000,
                "protocol": "raw_finalized_block",
                "event_id": "1" * 64,
                "source_record_id": "2" * 64,
                "block_hash": "retained-block-hash",
                "expected_transactions": 2,
            }
            tables["events"] = pa.Table.from_pylist(
                [
                    {
                        **{
                            k: block[k]
                            for k in ("slot", "transaction_index", "event_index", "signature")
                        },
                        "row_json": json.dumps(block),
                    }
                ]
            )
        files = {}
        for name, table in tables.items():
            path = part / (name + ".parquet")
            pq.write_table(table, path)
            files[name] = {
                "file": path.name,
                "bytes": path.stat().st_size,
                "sha256": file_hash(path),
                "rows": table.num_rows,
            }
        receipt = {"files": files}
        atomic_json(part / "receipt.json", receipt)
        records[stage] = receipt
    atomic_json(
        root / "manifest.json",
        {
            "format": "solana-raw-parquet/v1",
            "raw_manifest_sha256": "3" * 64,
            "converter": "4" * 64,
            "records": [dict(part="00000000", **records)],
        },
    )
    manifest_hash = file_hash(root / "manifest.json")
    atomic_json(root / "COMMITTED", {"manifest_sha256": manifest_hash})
    atomic_json(
        root / "audit.json",
        {
            "all_gates_pass": good_audit,
            "dataset_manifest_sha256": manifest_hash,
            "stable_slot_range": [100, 100],
        },
    )
    return root


def test_native_source_and_explicit_transfer_states(tmp_path):
    source = RawParquetSource(dataset(tmp_path))
    assert isinstance(source, HistoricalEventSource)
    block = next(source.events())
    assert isinstance(block, BlockEvent)
    assert block.tx_count == 2 and block.block_hash == "retained-block-hash"
    assert [r["amount_raw"] for r in source.transfers()] == [1]
    assert [r["amount_raw"] for r in source.transfers(committed_only=False)] == [1, 3, 2]
    assert len(source.boundaries()) == 3  # Actual block boundary plus both transaction boundaries.


def test_rejects_failed_rollout_gates(tmp_path):
    with pytest.raises(ValueError, match="gates"):
        RawParquetSource(dataset(tmp_path, good_audit=False))
    assert list(RawParquetSource(tmp_path, allow_incomplete_pilot=True).events())


def test_audit_is_bound_to_exact_manifest(tmp_path):
    root = dataset(tmp_path)
    atomic_json(root / "audit.json", {"all_gates_pass": True, "dataset_manifest_sha256": "0" * 64})
    with pytest.raises(ValueError, match="bound"):
        RawParquetSource(root)


def test_rejects_corrupt_part(tmp_path):
    root = dataset(tmp_path)
    with (root / "translated/00000000/transfers.parquet").open("ab") as stream:
        stream.write(b"corruption")
    with pytest.raises(ValueError, match="checksum"):
        RawParquetSource(root)


def test_slot_filter_does_not_leak_other_events(tmp_path):
    source = RawParquetSource(
        dataset(tmp_path), start_slot=101, end_slot=102, allow_incomplete_pilot=True
    )
    assert list(source.events()) == []
    assert list(source.transfers()) == []
    assert source.boundaries() == ()


def test_strict_source_cannot_include_unaudited_head_or_tail(tmp_path):
    root = dataset(tmp_path)
    with pytest.raises(ValueError, match="audited coverage"):
        RawParquetSource(root, start_slot=99, end_slot=102)
