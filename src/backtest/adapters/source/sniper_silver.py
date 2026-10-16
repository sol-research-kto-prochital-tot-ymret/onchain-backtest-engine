"""Verified local Silver input for the native reference engine research adapter.

This reads the earlier frozen export only; it imports no prototype strategy,
execution loop, quote function, or portfolio implementation.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    LogicalContentHash,
    ProtocolPayloadSchemaId,
    VenueId,
)
from backtest.domain.market_events import (
    REFERENCE_AMM_TRADE_PAYLOAD_SCHEMA_ID,
    BlockEvent,
    CanonicalEvent,
    EventEnvelope,
    TokenLaunchEvent,
    VenueLifecycleEvent,
    VenueLifecycleKind,
    VenueTradeEvent,
    canonical_event_sort_key,
    reference_amm_trade_payload,
)
from backtest.engine.replay import ReplayBoundary

SOL = AssetId("So11111111111111111111111111111111111111112")
TERMINAL_LIFECYCLE_STAGES = frozenset({"curve_complete", "amm_migration"})


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            h.update(block)
    return h.hexdigest()


def read_rows(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt") as stream:
        return [json.loads(line) for line in stream]


def seconds(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int((d.replace(tzinfo=UTC) if d.tzinfo is None else d).timestamp())


def missing_ranges(
    rows: list[dict[str, Any]], target: str, lo: int, hi: int
) -> list[tuple[int, int]]:
    cursor, missing = lo, []
    for start, end in sorted(
        (int(r["from_slot"]), int(r["to_slot"])) for r in rows if r["target_table"] == target
    ):
        if end < cursor:
            continue
        if start > cursor:
            missing.append((cursor, min(hi, start - 1)))
        cursor = max(cursor, end + 1)
        if cursor > hi:
            break
    if cursor <= hi:
        missing.append((cursor, hi))
    return missing


def _envelope(slot: int, tx: int, index: int, identity: str) -> EventEnvelope:
    position = ChainPosition(
        SOLANA_MAINNET_NETWORK_ID, BLOCK32_TRANSACTION32_POSITION_SCHEMA_ID, slot, tx, index
    )
    digest = domain_digest(
        "dnipro.silver-sniper-event.v1",
        {"identity": identity, "slot": slot, "tx": tx, "event": index},
    )
    # The reference observation scheduler breaks same-boundary ties by this ID.
    # Preserve the evidenced within-transaction order rather than hash order.
    causal = ContentDigest(f"{index:08x}" + digest.hex[8:])
    return EventEnvelope(
        position,
        domain_digest("dnipro.silver-transaction.v1", {"slot": slot, "tx": tx}),
        digest,
        digest,
        causal,
        CapabilityId("retained-pump-silver-v1"),
        "pumpfun",
        "retained-observations-v1",
        OrderingFidelity.TRANSACTION_EXACT,
    )


def verify_empty_block(document: dict[str, Any]) -> tuple[int, int]:
    request = document["request"]
    slot, options = request["params"]
    result = document["response"].get("result")
    if (
        request["method"] != "getBlock"
        or options.get("transactionDetails") != "full"
        or options.get("commitment") != "finalized"
        or document.get("slot") != slot
        or not isinstance(result, dict)
        or result.get("transactions") != []
        or not result.get("blockhash")
        or type(slot) is not int
        or type(result.get("parentSlot")) is not int
        or not 0 <= result["parentSlot"] < slot
        or type(result.get("blockTime")) is not int
    ):
        raise ValueError("evidence must contain a full finalized empty-block response")
    return int(slot), result["blockTime"]


class SilverSniperSource:
    def __init__(self, root: Path, empty_block_evidence: Path | None = None):
        self.root = root
        self.manifest = json.loads((root / "manifest.json").read_text())
        self.input_policy = json.loads((root / "config.json").read_text())
        if self.manifest.get("schema") != "sniper-break-even-snapshot/v1" or not self.manifest.get(
            "complete"
        ):
            raise ValueError("a completed frozen Silver snapshot is required")
        config_bytes = json.dumps(
            self.input_policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        if hashlib.sha256(config_bytes).hexdigest() != self.manifest["config_sha256"]:
            raise ValueError("snapshot configuration hash mismatch")
        folder = root / "snapshot"
        for part in [self.manifest["births"], *self.manifest["parts"], *self.manifest["metadata"]]:
            path = folder / part["file"]
            sql = (folder / (part["name"] + ".sql")).read_text().removesuffix("\n")
            if (
                file_hash(path) != part["file_sha256"]
                or hashlib.sha256(sql.encode()).hexdigest() != part["query_sha256"]
            ):
                raise ValueError("snapshot bytes or SQL changed: " + part["name"])
            if len(read_rows(path)) != part["rows"]:
                raise ValueError("snapshot row count changed")
        self.source_hash = file_hash(root / "manifest.json")
        self.manifest_hash = self.source_hash
        self.empty_block_evidence_hash: str | None = None
        self.proven_empty_slots: list[int] = []
        self.births = read_rows(folder / self.manifest["births"]["file"])
        self.birth_by_mint = {r["mint"]: r for r in self.births}
        if len(self.birth_by_mint) != len(self.births):
            raise ValueError("duplicate mint creation")
        self.trade_rows: list[dict[str, Any]] = []
        self.lifecycle: list[dict[str, Any]] = []
        self.times: dict[int, int] = {}
        for part in self.manifest["parts"]:
            data = read_rows(folder / part["file"])
            if part["name"].endswith("-clock"):
                self.times.update({int(r["slot"]): seconds(r["observed_block_time"]) for r in data})
            elif part["name"].endswith("-trades"):
                for row in data:
                    row["payload"] = json.loads(row.pop("decoded_extension_json") or "{}")
                    if row.get("block_time") is None:
                        row["block_time"] = row["payload"].get("timestamp")
                    if row.get("block_time") is None:
                        raise ValueError("trade has no evidenced timestamp")
                    self.times.setdefault(int(row["slot"]), seconds(row["block_time"]))
                self.trade_rows.extend(data)
            else:
                self.lifecycle.extend(data)
        self.unsupported: dict[str, str] = {}
        for row in self.births:
            p = json.loads(row.get("decoded_extension_json") or "{}")
            row["payload"] = p
            if row.get("quote_mint") != SOL.value:
                self.unsupported[row["mint"]] = "NON_SOL_QUOTE"
            elif (
                p.get("is_mayhem_mode") or p.get("is_cashback_enabled") or p.get("is_holder_reward")
            ):
                self.unsupported[row["mint"]] = "SPECIAL_CURVE_MODE"
        publication = read_rows(folder / "publication_before.jsonl.gz")
        if publication != read_rows(folder / "publication_after.jsonl.gz"):
            raise ValueError("publication changed during extraction")
        self.gaps = sorted(
            {
                g
                for target in (
                    "launch_events",
                    "pumpfun_v2_swaps",
                    "transactions",
                    "migration_events",
                )
                for g in missing_ranges(
                    publication, target, self.manifest["from_slot"], self.manifest["to_slot"]
                )
            }
        )
        if empty_block_evidence is not None:
            slot, timestamp = verify_empty_block(json.loads(empty_block_evidence.read_text()))
            if not any(lo <= slot <= hi for lo, hi in self.gaps):
                raise ValueError("empty-block evidence does not match an input coverage hole")
            if any(int(r["slot"]) == slot for r in self.births + self.trade_rows + self.lifecycle):
                raise ValueError("empty-block evidence conflicts with retained transaction events")
            self.empty_block_evidence_hash = file_hash(empty_block_evidence)
            self.source_hash = domain_digest(
                "dnipro.silver-input-with-empty-block.v1",
                {"manifest": self.manifest_hash, "evidence": self.empty_block_evidence_hash},
            ).hex
            self.proven_empty_slots = [slot]
            self.times[slot] = timestamp
            self.gaps = sorted(
                (a, b)
                for lo, hi in self.gaps
                for a, b in ((lo, min(hi, slot - 1)), (max(lo, slot + 1), hi))
                if a <= b
            )
        self.meta: dict[str, dict[str, Any]] = {}
        raw: list[tuple[int, int, tuple[int, ...], str, dict[str, Any]]] = []
        for row in self.births:
            raw.append(
                (
                    int(row["slot"]),
                    int(row["tx_idx"]),
                    tuple(row["instruction_path"]),
                    "launch",
                    row,
                )
            )
        for row in self.trade_rows:
            if row["mint"] not in self.unsupported:
                raw.append(
                    (
                        int(row["slot"]),
                        int(row["tx_idx"]),
                        tuple(row["instruction_path"]),
                        "trade",
                        row,
                    )
                )
        for row in self.lifecycle:
            # A creator/fee-recipient account migration does not complete a curve.
            if (
                row["mint"] not in self.unsupported
                and row["migration_stage"] in TERMINAL_LIFECYCLE_STAGES
            ):
                raw.append(
                    (
                        int(row["slot"]),
                        int(row["tx_idx"]),
                        tuple(row["instruction_path"]),
                        "lifecycle",
                        row,
                    )
                )
        raw.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
        values: list[CanonicalEvent] = []
        group_indices: defaultdict[tuple[int, int], int] = defaultdict(int)
        identities: set[str] = set()
        for slot, tx, path, kind, row in raw:
            index = group_indices[(slot, tx)]
            group_indices[(slot, tx)] += 1
            envelope = _envelope(slot, tx, index, row["event_uid"])
            if envelope.canonical_event_id.hex in identities:
                raise ValueError("duplicate retained event")
            identities.add(envelope.canonical_event_id.hex)
            self.meta[envelope.canonical_event_id.hex] = {
                "kind": kind,
                "row": row,
                "path": list(path),
            }
            mint = row["mint"]
            event: CanonicalEvent
            if kind == "lifecycle":
                stage = (
                    VenueLifecycleKind.MIGRATED
                    if row["migration_stage"] == "amm_migration"
                    else VenueLifecycleKind.COMPLETED
                )
                event = VenueLifecycleEvent(
                    envelope,
                    VenueId(mint),
                    stage,
                    ProtocolPayloadSchemaId("dnipro-retained-lifecycle-v1"),
                    canonical_json_bytes({"stage": row["migration_stage"]}),
                )
            elif kind == "launch":
                event = TokenLaunchEvent(
                    envelope,
                    AssetId(mint),
                    AccountId(row.get("creator") or "unknown"),
                    AccountId(row.get("launch_signer") or "unknown"),
                    VenueId(mint),
                    AssetId(self.birth_by_mint[mint]["quote_mint"]),
                    ProtocolPayloadSchemaId("dnipro-retained-launch-control-v1"),
                    canonical_json_bytes({"kind": kind}),
                )
            else:
                sold, bought = (
                    (SOL, AssetId(mint)) if row["side"] == "buy" else (AssetId(mint), SOL)
                )
                amount_in, amount_out = (
                    (int(row["quote"]), int(row["tokens"]))
                    if row["side"] == "buy"
                    else (int(row["tokens"]), int(row["quote"]))
                )
                event = VenueTradeEvent(
                    envelope,
                    VenueId(mint),
                    sold,
                    bought,
                    amount_in,
                    amount_out,
                    (),
                    REFERENCE_AMM_TRADE_PAYLOAD_SCHEMA_ID,
                    reference_amm_trade_payload(
                        asset_a_id=SOL,
                        asset_b_id=AssetId(mint),
                        reserve_a_after_atomic=int(row["virtual_quote"]),
                        reserve_b_after_atomic=int(row["virtual_base"]),
                    ),
                )
            values.append(event)
        last_by_group: dict[int, str] = {}
        for event in values:
            last_by_group[event.envelope.boundary_ordinal] = event.envelope.canonical_event_id.hex
        self.last_in_group = set(last_by_group.values())
        self.closed_at: dict[str, int] = {}
        for slot in sorted(self.times):
            envelope = _envelope(slot, -1, 0, "retained-slot-clock")
            values.append(BlockEvent(envelope, self.times[slot] * 10**9, None))
        self.values = tuple(sorted(values, key=canonical_event_sort_key))
        self._boundaries = tuple(
            ReplayBoundary.from_position(event.envelope.position)
            for index, event in enumerate(self.values)
            if index == 0
            or event.envelope.boundary_ordinal != self.values[index - 1].envelope.boundary_ordinal
        )

    @property
    def dataset_revision_id(self) -> DatasetRevisionId:
        return DatasetRevisionId(self.source_hash)

    @property
    def logical_content_hash(self) -> LogicalContentHash:
        return LogicalContentHash(self.source_hash)

    @property
    def replay_semantics_id(self) -> ContentDigest:
        return domain_digest(
            "dnipro.retained-silver-reference-replay.v2",
            {
                "source": self.source_hash,
                "scope": "conditional-retained-subset",
                "delivery_order": "transaction-event-index-prefix",
            },
        )

    def boundaries(self) -> tuple[ReplayBoundary, ...]:
        return self._boundaries

    def events(self) -> Iterator[CanonicalEvent]:
        yield from self.values

    @property
    def event_count(self) -> int:
        return len(self.values)

    def event_at(self, event_row_index: int) -> CanonicalEvent:
        return self.values[event_row_index]
