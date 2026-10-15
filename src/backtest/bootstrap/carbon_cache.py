"""Offline translation worker called by the collector caching tool."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from backtest.adapters.source.carbon_cache.convert import (
    verify_cache,
)
from backtest.adapters.source.carbon_cache.reader import CarbonParquetSource
from backtest.adapters.source.carbon_parallel import convert_cache_parallel


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Retain Bronze Parquet and produce a separate engine event cache"
    )
    sub = root.add_subparsers(dest="command", required=True)
    convert = sub.add_parser("translate")
    convert.add_argument("--raw", type=Path, required=True)
    convert.add_argument("--output", type=Path, required=True)
    convert.add_argument("--memory-mb", type=int, default=512)
    convert.add_argument("--reserve-gb", type=int, default=100)
    convert.add_argument("--workers", type=int, default=1, help="Parallel partition processes")
    for name in ("verify", "inspect"):
        item = sub.add_parser(name)
        item.add_argument("--output", type=Path, required=True)
    return root


def main() -> None:
    args = parser().parse_args()
    try:
        if args.command == "translate":
            result = convert_cache_parallel(
                args.raw,
                args.output,
                workers=args.workers,
                memory_mb=args.memory_mb,
                reserve_gb=args.reserve_gb,
            )
            print(
                json.dumps(
                    {
                        k: result[k]
                        for k in (
                            "complete",
                            "event_count",
                            "counters",
                            "requested_from_utc",
                            "requested_to_utc_exclusive",
                        )
                    },
                    indent=2,
                )
            )
        elif args.command == "verify":
            print(json.dumps(verify_cache(args.output), indent=2))
        else:
            source = CarbonParquetSource(args.output)
            counts: dict[str, int] = {}
            for event in source.events():
                counts[event.kind.name] = counts.get(event.kind.name, 0) + 1
            print(
                json.dumps(
                    {
                        "counts": counts,
                        "dataset_revision_id": str(source.dataset_revision_id),
                        "capabilities": source.manifest["capabilities"],
                    },
                    indent=2,
                )
            )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Cache error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
