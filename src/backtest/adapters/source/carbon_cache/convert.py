"""Checksummed, restartable, one-raw-chunk-at-a-time offline conversion."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import time
from collections import Counter, deque
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .normalize import VERSION, context, digest, normalize, semantics

FORMAT = "dnipro-carbon-event-cache/v1"
SCHEMA = pa.schema(
    [
        ("slot", pa.uint64()),
        ("transaction_index", pa.int64()),
        ("event_index", pa.uint32()),
        ("row_json", pa.string()),
    ]
)


def now() -> str:
    return datetime.now(UTC).isoformat()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024**2), b""):
            h.update(block)
    return h.hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".partial")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("JSON document must be an object")
    return value


def safe_path(root: Path, relative: str) -> Path:
    path = root / relative
    if (
        Path(relative).is_absolute()
        or ".." in Path(relative).parts
        or not path.resolve().is_relative_to(root.resolve())
    ):
        raise ValueError("manifest path escapes dataset")
    return path


def source_identity(source: dict[str, Any]) -> str:
    return digest(
        {
            "window": {
                k: source["config"][k] for k in ("start", "end", "table", "decoders", "null_time")
            },
            "range": [source["from_slot"], source["to_slot_exclusive"]],
            "columns": source["columns"],
            "records": [
                {
                    k: r[k]
                    for k in (
                        "file",
                        "from_slot",
                        "to_slot_exclusive",
                        "rows",
                        "bytes",
                        "sha256",
                        "sql_sha256",
                    )
                }
                for r in source["records"]
            ],
        }
    )


def code_identity() -> str:
    package = Path(__file__).parent
    domain = package.parents[2] / "domain"
    files = {p.name: file_hash(p) for p in sorted(package.glob("*.py"))}
    files.update(
        {
            "domain/" + name: file_hash(domain / name)
            for name in (
                "market_events.py",
                "chain.py",
                "identifiers.py",
                "hashing.py",
                "fidelity.py",
            )
        }
    )
    return digest(files)


def verify_part(root: Path, record: dict[str, Any]) -> None:
    for item in record["files"].values():
        path = safe_path(root, item["file"])
        if path.stat().st_size != item["bytes"] or file_hash(path) != item["sha256"]:
            raise ValueError(f"converted file checksum mismatch: {item['file']}")
        parquet = pq.ParquetFile(path)
        if parquet.metadata.num_rows != item["rows"] or not parquet.schema_arrow.equals(SCHEMA):
            raise ValueError(f"converted file schema/count mismatch: {item['file']}")


class TableWriter:
    def __init__(self, path: Path):
        self.writer = pq.ParquetWriter(path, SCHEMA, compression="zstd")
        self.buffer: list[dict[str, Any]] = []
        self.rows = 0

    def add(self, value: dict[str, Any]) -> None:
        self.buffer.append(
            {
                "slot": value["slot"],
                "transaction_index": value["transaction_index"],
                "event_index": value.get("event_index", 0),
                "row_json": json.dumps(
                    value, separators=(",", ":"), sort_keys=True, allow_nan=False
                ),
            }
        )
        self.rows += 1
        if len(self.buffer) >= 1024:
            self.flush()

    def flush(self) -> None:
        if self.buffer:
            self.writer.write_table(pa.Table.from_pylist(self.buffer, schema=SCHEMA))
            self.buffer.clear()

    def close(self) -> None:
        self.flush()
        self.writer.close()


def sql_literal(value: Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def copy_sorted_range(
    source: Path,
    destination: Path,
    columns: list[str],
    order: str,
    low: int,
    high: int,
    memory_mb: int,
    spill: Path,
) -> None:
    """Finish one bounded sort on disk before making any of its rows visible."""
    with duckdb.connect() as conn:
        conn.execute(f"SET memory_limit='{memory_mb}MB'")
        conn.execute("SET threads=1")
        conn.execute("SET preserve_insertion_order=false")
        conn.execute(f"SET temp_directory={sql_literal(spill)}")
        projection = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
        conn.execute(
            f"COPY (SELECT {projection} FROM read_parquet($input_file) "
            f"WHERE slot >= $low AND slot < $high ORDER BY {order}) "
            "TO $output_file (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 2048)",
            {"input_file": str(source), "output_file": str(destination), "low": low, "high": high},
        )


def sorted_batches(
    source: Path,
    columns: list[str],
    order: str,
    low: int,
    high: int,
    memory_mb: int,
    work: Path,
    counters: Counter[str],
) -> Generator[pa.RecordBatch]:
    """Bisect on memory pressure without splitting an instruction's slot.

    A failed COPY never releases rows. Retrying its two disjoint halves cannot
    duplicate already emitted data, including when failure happened late.
    Connections close before Arrow consumption, keeping both engines' memory
    from overlapping. Temporary payloads are removed after each bounded read.
    """
    work.mkdir()
    pending = deque([(low, high)])
    try:
        while pending:
            start, end = pending.popleft()
            spool = work / "sorted.parquet"
            try:
                copy_sorted_range(
                    source, spool, columns, order, start, end, memory_mb, work / "spill"
                )
            except duckdb.OutOfMemoryException as exc:
                spool.unlink(missing_ok=True)
                shutil.rmtree(work / "spill", ignore_errors=True)
                if end - start <= 1:
                    raise ValueError(
                        f"One slot ({start}) exceeds the {memory_mb} MB sort budget; "
                        "resume with a larger --memory-mb. No rows from that sort were published."
                    ) from exc
                middle = (start + end) // 2
                pending.appendleft((middle, end))
                pending.appendleft((start, middle))
                counters["sort_memory_retries"] += 1
                print(f"{now()} SORT RETRY [{start}, {end}) -> smaller slot ranges", flush=True)
                continue
            counters["sort_ranges"] += 1
            parquet = pq.ParquetFile(spool)
            try:
                yield from parquet.iter_batches(batch_size=2048)
            finally:
                parquet.close()
            spool.unlink()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def convert_part(
    raw_root: Path, target: Path, record: dict[str, Any], memory_mb: int
) -> dict[str, Any]:
    raw_file = safe_path(raw_root, record["file"])
    if raw_file.stat().st_size != record["bytes"] or file_hash(raw_file) != record["sha256"]:
        raise ValueError("raw chunk checksum mismatch")
    if pq.ParquetFile(raw_file).metadata.num_rows != record["rows"]:
        raise ValueError("raw chunk count mismatch")
    receipt = raw_root / "receipts" / (raw_file.stem + ".json")
    if read_json(receipt) != record or record.get("state") != "complete":
        raise ValueError("raw receipt differs from manifest")
    target.mkdir(parents=True)
    started = time.monotonic()
    tables = {
        name: TableWriter(target / (name + ".unordered.parquet"))
        for name in ("events", "transactions", "evidence", "boundaries")
    }
    counters: Counter[str] = Counter()
    times: dict[int, set[int]] = {}
    # Instruction identity is grouped before choosing a version; sorting by
    # transaction_index first would hide conflicting versions of that index.
    # Do not sort decoded_json/absolute_path_json: raw_json already contains
    # that content. A full-size raw part can expand to several GB otherwise.
    wanted = {
        "raw_json",
        "slot",
        "signature",
        "absolute_path",
        "transaction_index",
        "source",
        "source_priority",
        "ingested_at",
        "observed_at",
        "commitment",
        "block_time",
        "block_hash",
        "fee_payer",
        "fee_lamports",
        "success",
        "decoder",
    }
    columns = [name for name in pq.ParquetFile(raw_file).schema_arrow.names if name in wanted]
    reader = sorted_batches(
        raw_file,
        columns,
        "slot, signature, absolute_path, source_priority DESC, ingested_at DESC",
        record["from_slot"],
        record["to_slot_exclusive"],
        memory_mb,
        target / "raw-sort",
        counters,
    )
    tx: list[dict[str, Any]] = []
    current: tuple[int, str] | None = None
    variants: list[dict[str, Any]] = []
    path: tuple[int, ...] | None = None
    physical_rows = 0
    tx_positions: dict[tuple[int, int], str] = {}

    def evidence(raw: dict[str, Any], reason: str, detail: str | None = None) -> None:
        counters[reason] += 1
        tables["evidence"].add(
            {
                "slot": raw["slot"],
                "transaction_index": raw["transaction_index"],
                "signature": raw["signature"],
                "instruction_path": raw["absolute_path"],
                "decoder": raw["decoder"],
                "reason": reason,
                "detail": detail,
                "source": raw.get("source"),
                "source_priority": raw["source_priority"],
                "raw_file": record["file"],
                "raw_file_sha256": record["sha256"],
            }
        )

    def choose() -> None:
        if not variants:
            return
        winner = variants[0]
        priority = winner["source_priority"]
        peers = [x for x in variants[1:] if x["source_priority"] == priority]
        if peers:
            fingerprint = semantics(winner)
            if any(semantics(x) != fingerprint for x in peers):
                raise ValueError(
                    f"conflicting equally authoritative instruction: {winner['slot']} "
                    f"{winner['signature']} {winner['absolute_path']}"
                )
        counters["duplicate_versions"] += len(variants) - 1
        tx.append(winner)
        variants.clear()

    def transaction() -> None:
        if not tx:
            return
        counters["deduplicated_instructions"] += len(tx)
        chosen = max(tx, key=lambda x: x["source_priority"])
        coordinate = chosen["slot"], chosen["transaction_index"]
        if coordinate in tx_positions and tx_positions[coordinate] != chosen["signature"]:
            raise ValueError("transaction index maps to multiple signatures")
        tx_positions[coordinate] = chosen["signature"]
        facts = {tuple(i["absolute_path"]): i for i in tx}
        shared = ("transaction_index", "success", "fee_payer", "fee_lamports", "block_time")
        if any(any(i.get(k) != chosen.get(k) for k in shared) for i in tx):
            raise ValueError("inconsistent retained transaction context")
        tables["transactions"].add(
            {
                k: chosen.get(k)
                for k in ("slot", "signature", *shared, "source", "source_priority", "commitment")
            }
            | {
                "instruction_count": len(tx),
                "fee_grain": "once_per_signature_actual_fee_payer",
                "tips_complete": False,
            }
        )
        counters["transactions"] += 1
        if type(chosen.get("block_time")) is int:
            times.setdefault(chosen["slot"], set()).add(chosen["block_time"])
        event_index = 0
        for raw in sorted(tx, key=lambda x: tuple(x["absolute_path"])):
            try:
                item, reason = normalize(raw, facts)
            except (TypeError, ValueError, KeyError) as exc:
                evidence(raw, "unmapped_event", str(exc))
                continue
            if item is None:
                evidence(raw, reason)
                continue
            item.update(
                event_index=event_index, raw_file=record["file"], raw_file_sha256=record["sha256"]
            )
            event_index += 1
            item["event_id"] = digest(
                {
                    k: item[k]
                    for k in ("slot", "signature", "instruction_path", "protocol", "event_name")
                }
            )
            tables["events"].add(item)
            counters[item["protocol"] + ":" + item["kind"]] += 1
        if event_index:
            tables["boundaries"].add(
                {"slot": chosen["slot"], "transaction_index": chosen["transaction_index"]}
            )
        tx.clear()

    try:
        for batch in reader:
            for row in batch.to_pylist():
                physical_rows += 1
                raw = context(row)
                if not record["from_slot"] <= raw["slot"] < record["to_slot_exclusive"]:
                    raise ValueError("raw row is outside receipt range")
                key = raw["slot"], raw["signature"]
                next_path = tuple(raw["absolute_path"])
                if current != key:
                    choose()
                    transaction()
                    current, path = key, None
                if next_path != path:
                    choose()
                    path = next_path
                variants.append(raw)
                if len(tx) + len(variants) > 10000:
                    raise ValueError("one retained transaction exceeds 10000-instruction bound")
        choose()
        transaction()
        if physical_rows != record["rows"]:
            raise ValueError("streamed raw count differs from receipt")
        for slot, observed in sorted(times.items()):
            if len(observed) != 1:
                raise ValueError("conflicting retained slot clocks")
            value = {
                "slot": slot,
                "transaction_index": -1,
                "event_index": 0,
                "kind": "BLOCK",
                "block_time": next(iter(observed)),
                "protocol": "retained_slot_clock",
                "signature": "",
                "instruction_path": [],
                "event_name": "observed_slot_clock",
                "event_id": digest({"clock_slot": slot}),
                "source_record_id": digest({"raw_sha256": record["sha256"], "clock_slot": slot}),
                "quality_flags": ["derived_from_retained_instructions", "not_full_chain_clock"],
            }
            tables["events"].add(value)
            tables["boundaries"].add({"slot": slot, "transaction_index": -1})
            counters["derived_slot_clocks"] += 1
    finally:
        for table in tables.values():
            table.close()
        reader.close()
    # Small typed rows are sorted externally too; do not materialize a full-day
    # or even full-chunk decoded object list in Python.
    files = {}
    for name, writer in tables.items():
        unsorted = target / (name + ".unordered.parquet")
        final = target / (name + ".parquet")
        # Arrow writes the final schema, avoiding DuckDB narrowing/casting
        # the UInt32/UInt64 interchange fields.
        batches = sorted_batches(
            unsorted,
            SCHEMA.names,
            "slot, transaction_index, event_index",
            record["from_slot"],
            record["to_slot_exclusive"],
            memory_mb,
            target / (name + "-sort"),
            counters,
        )
        try:
            with pq.ParquetWriter(final, SCHEMA, compression="zstd") as output:
                for batch in batches:
                    output.write_batch(batch.cast(SCHEMA))
        finally:
            batches.close()
        unsorted.unlink()
        with final.open("rb") as stream:
            os.fsync(stream.fileno())
        files[name] = {
            "file": final.name,
            "rows": writer.rows,
            "bytes": final.stat().st_size,
            "sha256": file_hash(final),
        }
    # Validate against the engine's actual dataclasses before a partition can
    # be committed, including its fee ordering and protocol-payload rules.
    from .reader import event_from_record

    logical_hash = hashlib.sha256()
    for batch in pq.ParquetFile(target / "events.parquet").iter_batches(
        columns=["row_json"], batch_size=2048
    ):
        for row in batch.to_pylist():
            event_from_record(json.loads(row["row_json"]))
            logical_hash.update(row["row_json"].encode())
            logical_hash.update(b"\n")
    counters["physical_rows"] = physical_rows
    if (
        tables["events"].rows - counters["derived_slot_clocks"] + tables["evidence"].rows
        != counters["deduplicated_instructions"]
        or counters["deduplicated_instructions"] + counters["duplicate_versions"] != physical_rows
    ):
        raise ValueError("raw instruction disposition accounting mismatch")
    return {
        "raw_sha256": record["sha256"],
        "from_slot": record["from_slot"],
        "to_slot_exclusive": record["to_slot_exclusive"],
        "files": files,
        "logical_event_sha256": logical_hash.hexdigest(),
        "counters": dict(counters),
        "wall_seconds": time.monotonic() - started,
    }


def validate_raw_inventory(source: dict[str, Any]) -> None:
    if source.get("version") != 1 or source.get("complete") is not True:
        raise ValueError("raw export must be complete before translation")
    cursor = source["from_slot"]
    for record in source["records"]:
        if (
            record.get("state") != "complete"
            or record["from_slot"] != cursor
            or record["to_slot_exclusive"] <= cursor
        ):
            raise ValueError("raw manifest has an incomplete/noncontiguous inventory")
        cursor = record["to_slot_exclusive"]
    if cursor != source["to_slot_exclusive"] or len(source["records"]) != source["total_chunks"]:
        raise ValueError("raw inventory does not cover frozen plan")


def convert_cache(
    raw_root: Path, output: Path, *, memory_mb: int = 512, reserve_gb: int = 100
) -> dict[str, Any]:
    raw_root, output = raw_root.resolve(), output.resolve()
    if memory_mb < 64 or reserve_gb < 0:
        raise ValueError("memory must be >=64 MB and disk reserve nonnegative")
    if output == raw_root or output.is_relative_to(raw_root) or raw_root.is_relative_to(output):
        raise ValueError("raw and converted datasets must have separate directories")
    output.mkdir(parents=True, exist_ok=True)
    with (
        (raw_root / ".export.lock").open("r") as raw_lock,
        (output / ".convert.lock").open("a+") as lock,
    ):
        fcntl.flock(raw_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        source = read_json(raw_root / "manifest.json")
        validate_raw_inventory(source)
        identity = source_identity(source)
        manifest_file = output / "manifest.json"
        revision = code_identity()
        if manifest_file.exists():
            manifest = read_json(manifest_file)
            if (
                manifest.get("format") != FORMAT
                or manifest.get("source_identity") != identity
                or manifest.get("normalizer_code_sha256") != revision
            ):
                raise ValueError("input or translator revision changed; use a new output directory")
        else:
            names = {p.name for p in output.iterdir() if not p.name.startswith("._")}
            if names - {".convert.lock"}:
                raise ValueError("output contains unrelated files")
            manifest = {
                "format": FORMAT,
                "normalizer_version": VERSION,
                "normalizer_code_sha256": revision,
                "source_identity": identity,
                "raw_manifest_sha256": file_hash(raw_root / "manifest.json"),
                "requested_from_utc": datetime.fromtimestamp(
                    source["config"]["start"], UTC
                ).isoformat(),
                "requested_to_utc_exclusive": datetime.fromtimestamp(
                    source["config"]["end"], UTC
                ).isoformat(),
                "from_slot": source["from_slot"],
                "to_slot_exclusive": source["to_slot_exclusive"],
                "complete": False,
                "created_at": now(),
                "records": [],
                "capabilities": {
                    "chain_complete": False,
                    "exact_execution": False,
                    "full_wallet_history": False,
                    "full_chain_clock": False,
                    "inner_commitment_verified": False,
                    "network_fees": "one row per retained transaction",
                    "router_trades": "audit only; pool legs are execution grain",
                    "pumpfun": "trades, launch, completion, migration, "
                    "virtual after reserves and separate real inventory",
                    "pump_swap": "trades; before reserves retained in fact; "
                    "after state unavailable",
                    "raydium_launchpad": "trade events; pricing model unverified",
                    "meteora_dbc": "swap v2 preferred; curve configuration required",
                    "other_decoders": "audit references retained; adapters pending",
                },
            }
            atomic_json(manifest_file, manifest)
        if manifest["complete"]:
            if not (output / "COMMITTED").exists():
                for record in manifest["records"]:
                    verify_part(output, record)
                atomic_json(
                    output / "COMMITTED",
                    {
                        "manifest_sha256": file_hash(manifest_file),
                        "source_identity": identity,
                    },
                )
            verify_cache(output)
            return manifest
        (output / "parts").mkdir(exist_ok=True)
        session_parts, session_wall = 0, 0.0
        try:
            for index, raw in enumerate(source["records"]):
                name = f"{raw['from_slot']:012d}-{raw['to_slot_exclusive']:012d}"
                final = output / "parts" / name
                staged = output / "parts" / (name + ".partial")
                if index < len(manifest["records"]):
                    previous = manifest["records"][index]
                    if (
                        previous["raw_sha256"] != raw["sha256"]
                        or previous["from_slot"] != raw["from_slot"]
                    ):
                        raise ValueError("converted prefix does not match input")
                    verify_part(output, previous)
                    continue
                if final.exists():
                    prepared = read_json(final / "receipt.json")
                    if (
                        prepared["raw_sha256"] != raw["sha256"]
                        or prepared.get("source_identity") != identity
                        or prepared.get("normalizer_code_sha256") != revision
                    ):
                        raise ValueError("orphan partition belongs to another input")
                    verify_part(output, prepared)
                else:
                    if shutil.disk_usage(output).free < reserve_gb * 10**9:
                        raise ValueError("disk reserve reached; resume after freeing space")
                    if staged.exists():
                        shutil.rmtree(staged)
                    prepared = convert_part(raw_root, staged, raw, memory_mb)
                    session_parts += 1
                    session_wall += prepared["wall_seconds"]
                    prepared.update(source_identity=identity, normalizer_code_sha256=revision)
                    for item in prepared["files"].values():
                        item["file"] = "parts/" + name + "/" + item["file"]
                    atomic_json(staged / "receipt.json", prepared)
                    sync_directory(staged)
                    os.rename(staged, final)
                    sync_directory(final.parent)
                manifest["records"].append(prepared)
                manifest["updated_at"] = now()
                atomic_json(manifest_file, manifest)
                total = len(source["records"])
                average = (
                    session_wall / session_parts
                    if session_parts
                    else sum(r["wall_seconds"] for r in manifest["records"])
                    / len(manifest["records"])
                )
                progress = {
                    "phase": "translating",
                    "updated_at": now(),
                    "completed_parts": index + 1,
                    "total_parts": total,
                    "completed_through_slot_exclusive": raw["to_slot_exclusive"],
                    "eta_seconds": average * (total - index - 1),
                }
                atomic_json(output / "progress.json", progress)
                print(
                    f"{now()} TRANSLATING {index + 1}/{total} | "
                    f"{prepared['files']['events']['rows']} events | "
                    f"{prepared['wall_seconds']:.1f}s",
                    flush=True,
                )
            manifest["complete"] = True
            manifest["updated_at"] = now()
            manifest["counters"] = dict(
                sum((Counter(r["counters"]) for r in manifest["records"]), Counter())
            )
            manifest["event_count"] = sum(r["files"]["events"]["rows"] for r in manifest["records"])
            atomic_json(manifest_file, manifest)
            atomic_json(
                output / "COMMITTED",
                {"manifest_sha256": file_hash(manifest_file), "source_identity": identity},
            )
            atomic_json(
                output / "progress.json",
                {
                    "phase": "complete",
                    "updated_at": now(),
                    "event_count": manifest["event_count"],
                    "completed_parts": len(manifest["records"]),
                    "eta_seconds": 0,
                },
            )
            return manifest
        except BaseException as exc:
            atomic_json(
                output / "progress.json",
                {
                    "phase": "error",
                    "updated_at": now(),
                    "error": str(exc),
                    "completed_parts": len(manifest["records"]),
                    "total_parts": len(source["records"]),
                },
            )
            raise


def verify_cache(root: Path) -> dict[str, Any]:
    manifest = read_json(root / "manifest.json")
    if manifest.get("format") != FORMAT or manifest.get("complete") is not True:
        raise ValueError("converted dataset is not committed")
    marker = read_json(root / "COMMITTED")
    if (
        marker["manifest_sha256"] != file_hash(root / "manifest.json")
        or marker["source_identity"] != manifest["source_identity"]
    ):
        raise ValueError("converted commit marker mismatch")
    cursor = manifest["from_slot"]
    for record in manifest["records"]:
        if record["from_slot"] != cursor:
            raise ValueError("converted partition coverage hole")
        verify_part(root, record)
        cursor = record["to_slot_exclusive"]
    if cursor != manifest["to_slot_exclusive"]:
        raise ValueError("converted final boundary mismatch")
    return {
        "complete": True,
        "parts": len(manifest["records"]),
        "event_count": manifest["event_count"],
        "source_identity": manifest["source_identity"],
    }
