"""Bounded native replay of raw-first Parquet, plus explicit transfer facts.

The engine's CanonicalEvent union has no TransferEvent yet. transfers() exposes
typed instruction facts without silently pretending that wallet inventory is
already reconciled by the reference engine. Unknown/reverted attempts remain
queryable but are excluded from the default committed-transfer iterator.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb

from backtest.adapters.source.carbon_cache.convert import file_hash, read_json, safe_path
from backtest.adapters.source.carbon_cache.normalize import digest
from backtest.adapters.source.carbon_cache.reader import event_from_record
from backtest.domain.chain import (
    BLOCK32_TRANSACTION32_POSITION_SCHEMA_ID,
    SOLANA_MAINNET_NETWORK_ID,
    ChainPosition,
)
from backtest.domain.hashing import domain_digest
from backtest.domain.identifiers import ContentDigest, DatasetRevisionId, LogicalContentHash
from backtest.domain.market_events import BlockEvent, CanonicalEvent, canonical_event_sort_key
from backtest.engine.replay import ReplayBoundary


class RawParquetSource:
    """Verify immutable files, then sort only a requested replay window offline."""

    def __init__(
        self,
        root: Path,
        *,
        start_slot: int | None = None,
        end_slot: int | None = None,
        allow_incomplete_pilot: bool = False,
        memory_mb: int = 512,
    ):
        self.root = root.resolve()
        self.manifest = read_json(self.root / "manifest.json")
        if self.manifest.get("format") != "solana-raw-parquet/v1":
            raise ValueError("unsupported raw Parquet dataset")
        if read_json(self.root / "COMMITTED") != {
            "manifest_sha256": file_hash(self.root / "manifest.json")
        }:
            raise ValueError("raw Parquet manifest marker mismatch")
        verified_bounds = None
        if not allow_incomplete_pilot:
            audit = read_json(self.root / "audit.json")
            if not audit.get("all_gates_pass"):
                raise ValueError("raw Parquet coverage/semantic gates did not pass")
            if audit.get("dataset_manifest_sha256") != file_hash(self.root / "manifest.json"):
                raise ValueError("raw Parquet audit is not bound to this manifest")
            verified_bounds = audit["stable_slot_range"]
        if memory_mb < 64:
            raise ValueError("sort memory budget must be at least 64 MB")
        self.memory_mb = memory_mb
        self.start_slot = (
            (verified_bounds[0] if verified_bounds else 0) if start_slot is None else start_slot
        )
        self.end_slot = (
            (verified_bounds[1] + 1 if verified_bounds else 1 << 32)
            if end_slot is None
            else end_slot
        )
        if not 0 <= self.start_slot < self.end_slot <= 1 << 32:
            raise ValueError("invalid source slot range")
        if (
            verified_bounds
            and not verified_bounds[0] <= self.start_slot < self.end_slot <= verified_bounds[1] + 1
        ):
            raise ValueError("requested slots extend beyond audited coverage")
        self.files: dict[str, list[str]] = {}
        for part in self.manifest["records"]:
            for stage in ("raw", "translated"):
                part_root = safe_path(self.root, stage + "/" + part["part"])
                if read_json(part_root / "receipt.json") != part[stage]:
                    raise ValueError("raw Parquet part receipt mismatch")
                for name, record in part[stage]["files"].items():
                    path = safe_path(part_root, record["file"])
                    if (
                        path.stat().st_size != record["bytes"]
                        or file_hash(path) != record["sha256"]
                    ):
                        raise ValueError("raw Parquet file checksum mismatch")
                    self.files.setdefault(name, []).append(str(path))
        self._identity = digest(
            {
                "raw": self.manifest["raw_manifest_sha256"],
                "converter": self.manifest["converter"],
                "bounds": [self.start_slot, self.end_slot],
            }
        )

    @property
    def dataset_revision_id(self) -> DatasetRevisionId:
        return DatasetRevisionId(self._identity)

    @property
    def logical_content_hash(self) -> LogicalContentHash:
        return LogicalContentHash(self._identity)

    @property
    def replay_semantics_id(self) -> ContentDigest:
        return domain_digest("solana-raw-parquet-observational-replay/v1", self._identity)

    def _query(self, table: str, *, committed_only: bool = False) -> Iterator[dict[str, Any]]:
        if table not in self.files and table != "boundaries":
            raise ValueError("table absent from committed dataset")
        with (
            tempfile.TemporaryDirectory(prefix="raw-parquet-sort-") as temporary,
            duckdb.connect() as connection,
        ):
            connection.execute(f"SET memory_limit='{self.memory_mb}MB'")
            connection.execute("SET threads=1")
            connection.execute("SET temp_directory=$path", {"path": temporary})
            parameters: dict[str, Any] = {"low": self.start_slot, "high": self.end_slot}
            if table == "boundaries":
                query = (
                    "SELECT DISTINCT slot,transaction_index FROM ("
                    "SELECT slot,transaction_index FROM read_parquet($transactions) UNION ALL "
                    "SELECT slot,transaction_index FROM read_parquet($events) "
                    "WHERE transaction_index=-1"
                    ") WHERE slot >= $low AND slot < $high ORDER BY slot,transaction_index"
                )
                parameters.update(
                    transactions=self.files["transactions"], events=self.files["events"]
                )
            else:
                suffix = (
                    ",event_index"
                    if table == "events"
                    else ",ordinal"
                    if table in {"transfers", "instructions", "evidence"}
                    else ""
                )
                predicate = " AND execution_status='committed'" if committed_only else ""
                query = (
                    "SELECT * FROM read_parquet($files) WHERE slot >= $low AND slot < $high"
                    + predicate
                    + " ORDER BY slot,transaction_index"
                    + suffix
                )
                parameters["files"] = self.files[table]
            cursor = connection.execute(query, parameters)
            assert cursor.description is not None
            names = [column[0] for column in cursor.description]
            while values := cursor.fetchmany(512):
                for row in values:
                    yield dict(zip(names, row, strict=True))

    def events(self) -> Iterator[CanonicalEvent]:
        previous = None
        for row in self._query("events"):
            record = json.loads(row["row_json"])
            if any(
                record[k] != row[k]
                for k in ("slot", "signature", "transaction_index", "event_index")
            ):
                raise ValueError("event position differs from typed columns")
            event = event_from_record(record)
            if isinstance(event, BlockEvent):
                event = BlockEvent(
                    event.envelope,
                    event.block_time_ns,
                    record.get("expected_transactions"),
                    record.get("block_hash"),
                )
            key = canonical_event_sort_key(event)
            if previous is not None and key <= previous:
                raise ValueError("raw Parquet event ordering is not strictly increasing")
            previous = key
            yield event

    def transfers(self, *, committed_only: bool = True) -> Iterator[dict[str, Any]]:
        yield from self._query("transfers", committed_only=committed_only)

    def iter_boundaries(self) -> Iterator[ReplayBoundary]:
        previous = None
        for row in self._query("boundaries"):
            position = ChainPosition(
                SOLANA_MAINNET_NETWORK_ID,
                BLOCK32_TRANSACTION32_POSITION_SCHEMA_ID,
                row["slot"],
                row["transaction_index"],
                None,
            )
            if previous is not None and position.boundary_ordinal <= previous:
                raise ValueError("transaction boundaries are not strictly increasing")
            previous = position.boundary_ordinal
            yield ReplayBoundary.from_position(position)

    def boundaries(self) -> tuple[ReplayBoundary, ...]:
        return tuple(self.iter_boundaries())
