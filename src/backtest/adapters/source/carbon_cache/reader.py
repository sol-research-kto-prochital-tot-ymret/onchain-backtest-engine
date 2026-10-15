"""Streaming HistoricalEventSource for the converted retained-event cache."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from backtest.adapters.source.clickhouse.carbon import uint
from backtest.domain.chain import (
    BLOCK32_TRANSACTION32_POSITION_SCHEMA_ID,
    SOLANA_MAINNET_NETWORK_ID,
    ChainPosition,
)
from backtest.domain.fidelity import OrderingFidelity
from backtest.domain.hashing import canonical_json_bytes, domain_digest
from backtest.domain.identifiers import (
    AccountId,
    AssetId,
    CapabilityId,
    ContentDigest,
    DatasetRevisionId,
    FeeComponentId,
    LogicalContentHash,
    ProtocolPayloadSchemaId,
    VenueId,
)
from backtest.domain.market_events import (
    REFERENCE_AMM_TRADE_PAYLOAD_SCHEMA_ID,
    BlockEvent,
    CanonicalEvent,
    EventEnvelope,
    FeeComponent,
    TokenLaunchEvent,
    VenueLifecycleEvent,
    VenueLifecycleKind,
    VenueTradeEvent,
    canonical_event_sort_key,
    reference_amm_trade_payload,
)
from backtest.engine.replay import ReplayBoundary

from .convert import read_json, safe_path, verify_cache
from .normalize import VERSION, digest


def event_from_record(row: dict[str, Any]) -> CanonicalEvent:
    position = ChainPosition(
        SOLANA_MAINNET_NETWORK_ID,
        BLOCK32_TRANSACTION32_POSITION_SCHEMA_ID,
        row["slot"],
        row["transaction_index"],
        row["event_index"],
    )
    envelope = EventEnvelope(
        position=position,
        transaction_group_id=domain_digest(
            "carbon-cache-transaction-v1",
            {"slot": row["slot"], "signature": row["signature"], "index": row["transaction_index"]},
        ),
        source_record_id=ContentDigest(row["source_record_id"]),
        canonical_event_id=domain_digest("carbon-cache-event-revision-v1", row),
        stable_causal_id=ContentDigest(row["event_id"]),
        capability_id=CapabilityId("carbon-retained-" + row["protocol"]),
        protocol=row["protocol"],
        protocol_version=VERSION,
        # Exact order of the retained subset, not complete-chain/finality proof.
        ordering_fidelity=OrderingFidelity.INSTRUCTION_EXACT,
    )
    if row["kind"] == "BLOCK":
        return BlockEvent(envelope, row["block_time"] * 10**9, None)
    if row["kind"] == "TOKEN_LAUNCH":
        return TokenLaunchEvent(
            envelope,
            AssetId(row["mint"]),
            AccountId(row["developer"]),
            AccountId(row["creation_user"]),
            VenueId(row["venue_id"]),
            AssetId(row["quote_mint"]),
            ProtocolPayloadSchemaId("carbon-retained-launch-v1"),
            canonical_json_bytes(row["decoded_payload"]),
            row.get("decimals"),
        )
    if row["kind"] == "VENUE_LIFECYCLE":
        return VenueLifecycleEvent(
            envelope,
            VenueId(row["venue_id"]),
            VenueLifecycleKind(row["lifecycle"]),
            ProtocolPayloadSchemaId("carbon-retained-lifecycle-v1"),
            canonical_json_bytes(row["decoded_payload"]),
        )
    if row["kind"] != "VENUE_TRADE":
        raise ValueError("unknown canonical event kind")
    fees = tuple(
        sorted(
            FeeComponent(
                FeeComponentId(row["protocol"] + "." + f["component"]),
                AssetId(f["asset"]),
                uint(f["amount"], "fee"),
            )
            for f in row["fees"]
        )
    )
    return VenueTradeEvent(
        envelope,
        VenueId(row["venue_id"]),
        AssetId(row["sold_asset"]),
        AssetId(row["bought_asset"]),
        uint(row["sold_amount"], "sold amount"),
        uint(row["bought_amount"], "bought amount"),
        fees,
        REFERENCE_AMM_TRADE_PAYLOAD_SCHEMA_ID,
        reference_amm_trade_payload(
            asset_a_id=AssetId(row["quote_mint"]),
            asset_b_id=AssetId(row["mint"]),
            reserve_a_after_atomic=row.get("reserve_quote_after"),
            reserve_b_after_atomic=row.get("reserve_base_after"),
        ),
    )


class CarbonParquetSource:
    """Read only committed data; optional slot bounds keep replay runs bounded.

    Events stream in Arrow batches. The current reference engine requests its
    boundary timeline as a tuple; this smaller timeline is materialized by
    boundaries(), never the event payloads. Use slot bounds for large replays.
    """

    def __init__(self, root: Path, *, start_slot: int | None = None, end_slot: int | None = None):
        self.root = root.resolve()
        verify_cache(self.root)
        self.manifest = read_json(self.root / "manifest.json")
        self.start_slot = self.manifest["from_slot"] if start_slot is None else start_slot
        self.end_slot = self.manifest["to_slot_exclusive"] if end_slot is None else end_slot
        if (
            not self.manifest["from_slot"]
            <= self.start_slot
            < self.end_slot
            <= self.manifest["to_slot_exclusive"]
        ):
            raise ValueError("source slot bounds are outside committed cache")
        self._identity = digest(
            {
                "source": self.manifest["source_identity"],
                "normalizer": self.manifest["normalizer_code_sha256"],
                "parts": [r["logical_event_sha256"] for r in self.manifest["records"]],
                "slots": [self.start_slot, self.end_slot],
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
        return domain_digest(
            "carbon-retained-observational-replay-v1",
            {
                "scope": "retained_instruction_subset",
                "slots": [self.start_slot, self.end_slot],
                "normalizer": self.manifest["normalizer_code_sha256"],
            },
        )

    def rows(self, table: str = "events") -> Iterator[dict[str, Any]]:
        if table not in ("events", "transactions", "evidence", "boundaries"):
            raise ValueError("unknown cache table")
        for part in self.manifest["records"]:
            if part["to_slot_exclusive"] <= self.start_slot or part["from_slot"] >= self.end_slot:
                continue
            parquet = pq.ParquetFile(safe_path(self.root, part["files"][table]["file"]))
            for batch in parquet.iter_batches(batch_size=2048):
                for row in batch.to_pylist():
                    if self.start_slot <= row["slot"] < self.end_slot:
                        value = json.loads(row["row_json"])
                        if (
                            value["slot"] != row["slot"]
                            or value["transaction_index"] != row["transaction_index"]
                            or value.get("event_index", 0) != row["event_index"]
                        ):
                            raise ValueError("typed position and JSON position mismatch")
                        yield value

    def events(self) -> Iterator[CanonicalEvent]:
        previous = None
        for row in self.rows():
            event = event_from_record(row)
            key = canonical_event_sort_key(event)
            if previous is not None and key <= previous:
                raise ValueError("canonical events are not strictly ordered")
            previous = key
            yield event

    def iter_boundaries(self) -> Iterator[ReplayBoundary]:
        previous = None
        for row in self.rows("boundaries"):
            position = ChainPosition(
                SOLANA_MAINNET_NETWORK_ID,
                BLOCK32_TRANSACTION32_POSITION_SCHEMA_ID,
                row["slot"],
                row["transaction_index"],
                None,
            )
            if previous is not None and position.boundary_ordinal <= previous:
                raise ValueError("boundaries are not strictly ordered")
            previous = position.boundary_ordinal
            yield ReplayBoundary.from_position(position)

    def boundaries(self) -> tuple[ReplayBoundary, ...]:
        return tuple(self.iter_boundaries())
