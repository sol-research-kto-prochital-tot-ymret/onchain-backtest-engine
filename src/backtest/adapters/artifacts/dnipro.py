"""Immutable research snapshots, kept separate from exact engine admission."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from backtest.domain.hashing import canonical_json_bytes

FORMAT = "dnipro-carbon-research-snapshot/v1"
_TABLES = ("trades.parquet", "states.parquet")


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def publish(
    path: Path, trades: list[dict[str, Any]], states: list[dict[str, Any]], metadata: dict[str, Any]
) -> dict[str, Any]:
    if path.exists() or path.is_symlink():
        raise ValueError("snapshot destination already exists; immutable data is never replaced")
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".dnipro-snapshot-", dir=path.parent))
    try:
        for filename, rows in zip(_TABLES, (trades, states), strict=True):
            # Unknown source metadata stays in the typed row's canonical JSON payload.
            table = pa.table(
                {
                    "row_json": pa.array(
                        [canonical_json_bytes(row).decode() for row in rows], type=pa.string()
                    )
                }
            )
            pq.write_table(table, staging / filename, compression="zstd")
        manifest = {
            "format": FORMAT,
            "metadata": metadata,
            "files": {
                name: {
                    "sha256": file_hash(staging / name),
                    "bytes": (staging / name).stat().st_size,
                }
                for name in _TABLES
            },
            "trade_count": len(trades),
            "state_count": len(states),
            "fidelity": {
                "global_transaction_clock": "UNPROVEN",
                "settlement_tail": "UNPROVEN",
                "complete_market_history": "UNPROVEN",
                "exact_source_admission": False,
            },
        }
        data = canonical_json_bytes(manifest)
        (staging / "manifest.json").write_bytes(data)
        (staging / "COMMITTED").write_text(hashlib.sha256(data).hexdigest() + "\n")
        for name in (*_TABLES, "manifest.json", "COMMITTED"):
            with (staging / name).open("rb") as handle:
                os.fsync(handle.fileno())
        _sync_directory(staging)
        # rename refuses a nonempty destination; callers must use distinct paths.
        os.rename(staging, path)
        _sync_directory(path.parent)
        return manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def load(
    path: Path, maximum_rows: int = 500_000
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    data = _bounded(path / "manifest.json", 1_048_576)
    if _bounded(path / "COMMITTED", 100).decode().strip() != hashlib.sha256(data).hexdigest():
        raise ValueError("snapshot commit marker does not match manifest")
    manifest = json.loads(data)
    if manifest.get("format") != FORMAT or set(manifest.get("files", {})) != set(_TABLES):
        raise ValueError("unsupported snapshot manifest")
    tables = []
    for name, count_key in zip(_TABLES, ("trade_count", "state_count"), strict=True):
        file = path / name
        if file.is_symlink() or not file.is_file():
            raise ValueError("snapshot tables must be regular files")
        if (
            file.stat().st_size > 268_435_456
            or file_hash(file) != manifest["files"][name]["sha256"]
        ):
            raise ValueError("snapshot table failed bounded integrity verification")
        parquet = pq.ParquetFile(file)
        if (
            parquet.metadata.num_rows > maximum_rows
            or parquet.metadata.num_rows != manifest[count_key]
        ):
            raise ValueError("snapshot table row count is invalid")
        if parquet.schema_arrow.names != ["row_json"]:
            raise ValueError("snapshot table schema is invalid")
        if parquet.schema_arrow.field("row_json").type != pa.string():
            raise ValueError("snapshot row payload must be a string")
        uncompressed = sum(
            parquet.metadata.row_group(index).total_byte_size
            for index in range(parquet.metadata.num_row_groups)
        )
        if uncompressed > 268_435_456:
            raise ValueError("snapshot uncompressed table exceeds the memory bound")
        table = parquet.read()
        tables.append([json.loads(row) for row in table.column("row_json").to_pylist()])
    return manifest, tables[0], tables[1]


def publish_result(path: Path, payload: bytes) -> None:
    """Publish a complete result atomically without replacing an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".dnipro-result-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        # Hard-link publication is atomic and refuses an existing destination.
        os.link(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def require_exact(manifest: dict[str, Any]) -> None:
    # This adapter never manufactures receipts accepted by the upstream exact engine.
    raise ValueError(
        "exact replay unavailable: complete clock, history, signer, and tail proofs required"
    )


def _bounded(path: Path, maximum: int) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("invalid bounded snapshot file")
    return path.read_bytes()


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
