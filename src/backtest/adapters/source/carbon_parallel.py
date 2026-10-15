"""Process-parallel scheduling of the unchanged Carbon partition normalizer.

Only the coordinator publishes partitions/manifests. Keeping scheduling outside
carbon_cache preserves the normalization fingerprint and existing checkpoints.
"""

from __future__ import annotations

import fcntl
import multiprocessing
import os
import shutil
import time
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa

from .carbon_cache.convert import (
    FORMAT,
    atomic_json,
    code_identity,
    convert_cache,
    convert_part,
    file_hash,
    now,
    read_json,
    source_identity,
    sync_directory,
    validate_raw_inventory,
    verify_cache,
    verify_part,
)
from .carbon_cache.normalize import VERSION


def partition_name(raw: dict[str, Any]) -> str:
    return f"{raw['from_slot']:012d}-{raw['to_slot_exclusive']:012d}"


def prepare_partition(
    raw_root: Path, output: Path, raw: dict[str, Any], memory_mb: int
) -> dict[str, Any]:
    """Workers write only their own uncommitted staging directory."""
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    staged = output / "parts" / (partition_name(raw) + ".partial")
    if staged.exists():
        shutil.rmtree(staged)
    return convert_part(raw_root, staged, raw, memory_mb)


def initial_manifest(source: dict[str, Any], identity: str, revision: str) -> dict[str, Any]:
    # Same format/capabilities as the sequential coordinator. Normalization,
    # validation, receipts and engine consumption remain shared implementations.
    return {
        "format": FORMAT,
        "normalizer_version": VERSION,
        "normalizer_code_sha256": revision,
        "source_identity": identity,
        "requested_from_utc": datetime.fromtimestamp(source["config"]["start"], UTC).isoformat(),
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
            "pump_swap": "trades; before reserves retained in fact; after state unavailable",
            "raydium_launchpad": "trade events; pricing model unverified",
            "meteora_dbc": "swap v2 preferred; curve configuration required",
            "other_decoders": "audit references retained; adapters pending",
        },
    }


def check_prepared(
    output: Path, prepared: dict[str, Any], raw: dict[str, Any], identity: str, revision: str
) -> None:
    name = partition_name(raw)
    if (
        prepared.get("raw_sha256") != raw["sha256"]
        or prepared.get("source_identity") != identity
        or prepared.get("normalizer_code_sha256") != revision
        or prepared.get("from_slot") != raw["from_slot"]
        or prepared.get("to_slot_exclusive") != raw["to_slot_exclusive"]
        or set(prepared.get("files", {})) != {"events", "transactions", "evidence", "boundaries"}
        or any(
            item.get("file") != f"parts/{name}/{table}.parquet"
            for table, item in prepared["files"].items()
        )
    ):
        raise ValueError("prepared partition belongs to another input or revision")
    verify_part(output, prepared)


def convert_cache_parallel(
    raw_root: Path,
    output: Path,
    *,
    workers: int,
    memory_mb: int = 512,
    reserve_gb: int = 100,
) -> dict[str, Any]:
    if workers < 1 or memory_mb < 64 or reserve_gb < 0:
        raise ValueError("workers must be >=1, memory >=64 MB, reserve nonnegative")
    if workers == 1:
        return convert_cache(raw_root, output, memory_mb=memory_mb, reserve_gb=reserve_gb)
    raw_root, output = raw_root.resolve(), output.resolve()
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
        identity, revision = source_identity(source), code_identity()
        manifest_file = output / "manifest.json"
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
            manifest = initial_manifest(source, identity, revision)
            manifest["raw_manifest_sha256"] = file_hash(raw_root / "manifest.json")
            atomic_json(manifest_file, manifest)
        (output / "parts").mkdir(exist_ok=True)
        records = source["records"]
        if len(manifest["records"]) > len(records):
            raise ValueError("converted prefix exceeds input")
        ready: dict[int, dict[str, Any]] = {}
        for index, raw in enumerate(records):
            final = output / "parts" / partition_name(raw)
            if index < len(manifest["records"]):
                prepared = manifest["records"][index]
                check_prepared(output, prepared, raw, identity, revision)
                if read_json(final / "receipt.json") != prepared:
                    raise ValueError("partition receipt differs from manifest")
                ready[index] = prepared
            elif final.exists():
                prepared = read_json(final / "receipt.json")
                check_prepared(output, prepared, raw, identity, revision)
                ready[index] = prepared
        if manifest["complete"]:
            if len(manifest["records"]) != len(records):
                raise ValueError("complete manifest has an incomplete inventory")
            if not (output / "COMMITTED").exists():
                atomic_json(
                    output / "COMMITTED",
                    {"manifest_sha256": file_hash(manifest_file), "source_identity": identity},
                )
            verify_cache(output)
            return manifest
        pending = iter(i for i in range(len(records)) if i not in ready)
        started = time.monotonic()
        session_done = 0
        session_rows = 0
        initial_ready = len(ready)
        active: dict[Future[dict[str, Any]], int] = {}

        def progress(phase: str = "translating", error: str | None = None) -> None:
            elapsed = time.monotonic() - started
            atomic_json(
                output / "progress.json",
                {
                    "phase": phase,
                    "updated_at": now(),
                    "workers": workers,
                    "memory_mb_per_worker": memory_mb,
                    "executor_code_sha256": file_hash(Path(__file__)),
                    "completed_parts": len(ready),
                    "committed_prefix_parts": len(manifest["records"]),
                    "prepared_out_of_order_parts": len(ready) - len(manifest["records"]),
                    "total_parts": len(records),
                    "completed_through_slot_exclusive": manifest["records"][-1]["to_slot_exclusive"]
                    if manifest["records"]
                    else source["from_slot"],
                    "active_workers": len(active),
                    "session_completed_parts": session_done,
                    "session_processed_rows": session_rows,
                    "rows_per_second": session_rows / elapsed if elapsed else None,
                    "session_wall_seconds": elapsed,
                    "initial_completed_parts": initial_ready,
                    "eta_seconds": elapsed
                    / session_rows
                    * sum(record["rows"] for i, record in enumerate(records) if i not in ready)
                    if session_rows
                    else None,
                    **({"error": error} if error else {}),
                },
            )

        def advance_prefix() -> None:
            while len(manifest["records"]) in ready:
                manifest["records"].append(ready[len(manifest["records"])])
            manifest["updated_at"] = now()
            atomic_json(manifest_file, manifest)

        advance_prefix()
        progress()
        try:
            # Spawn avoids inheriting coordinator file locks or native-library
            # state. Each process has one DuckDB thread and one raw partition.
            with ProcessPoolExecutor(
                max_workers=workers, mp_context=multiprocessing.get_context("spawn")
            ) as pool:

                def submit() -> None:
                    while len(active) < workers:
                        index = next(pending, None)
                        if index is None:
                            break
                        # Reserve a conservative temporary footprint for every
                        # active task in addition to the operator's free floor.
                        staging = sum(records[i]["bytes"] * 16 for i in (*active.values(), index))
                        if shutil.disk_usage(output).free < reserve_gb * 10**9 + staging:
                            raise ValueError("disk reserve reached; resume after freeing space")
                        future = pool.submit(
                            prepare_partition, raw_root, output, records[index], memory_mb
                        )
                        active[future] = index

                submit()
                progress()
                while active:
                    finished, _ = wait(active, return_when=FIRST_COMPLETED)
                    for future in finished:
                        index = active.pop(future)
                        prepared = future.result()
                        raw = records[index]
                        name = partition_name(raw)
                        staged = output / "parts" / (name + ".partial")
                        final = output / "parts" / name
                        prepared.update(source_identity=identity, normalizer_code_sha256=revision)
                        for item in prepared["files"].values():
                            item["file"] = "parts/" + name + "/" + item["file"]
                        atomic_json(staged / "receipt.json", prepared)
                        sync_directory(staged)
                        os.rename(staged, final)
                        sync_directory(final.parent)
                        ready[index] = prepared
                        session_done += 1
                        session_rows += raw["rows"]
                        advance_prefix()
                        progress()
                        print(
                            f"{now()} TRANSLATING {len(ready)}/{len(records)} | "
                            f"workers={workers} | part={index + 1} | "
                            f"{prepared['wall_seconds']:.1f}s worker time",
                            flush=True,
                        )
                    submit()
                    progress()
        except BaseException as exc:
            for future in active:
                future.cancel()
            progress("error", str(exc))
            raise
    # Reuse the sequential coordinator's final accounting and commit protocol.
    # It only verifies/adopts these prepared parts; no normalization is repeated.
    return convert_cache(raw_root, output, memory_mb=memory_mb, reserve_gb=reserve_gb)
