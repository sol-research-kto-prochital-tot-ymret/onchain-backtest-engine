"""Strict, bounded JSONL replay of explicitly resolved Pump state, not raw events."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, fields
from pathlib import Path
from typing import Literal, Union, cast, get_args, get_origin, get_type_hints

from backtest.plugins.protocols.pumpfun.modern_runtime import (
    PumpReplayRuntime,
    ReplayOrder,
    ReplayPosition,
    StateObservation,
)
from backtest.plugins.protocols.pumpfun.quotes import FeeConfig, QuoteState

MAX_LINE_BYTES = 1024 * 1024
CAVEAT = (
    "Conditional replay against supplied historical states; not exact historical execution. "
    "No latency, transaction landing, MEV, or counterfactual market response is inferred."
)


def _canonical(value: object) -> bytes:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return (text + "\n").encode()


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("expected JSON object")
    return cast(dict[str, object], value)


def _keys(value: dict[str, object], required: set[str], optional: set[str] | None = None) -> None:
    if missing := required - value.keys():
        raise ValueError(f"missing fields: {', '.join(sorted(missing))}")
    if extra := value.keys() - required - (optional or set()):
        raise ValueError(f"unknown fields: {', '.join(sorted(extra))}")


def _matches(value: object, annotation: object) -> bool:
    origin = get_origin(annotation)
    if origin is Literal:
        return any(type(value) is type(item) and value == item for item in get_args(annotation))
    if origin is Union or str(origin) == "<class 'types.UnionType'>":
        return any(_matches(value, item) for item in get_args(annotation))
    if annotation is type(None):
        return value is None
    if annotation in (int, str, bool):
        return type(value) is annotation
    return False


def _typed(value: object, cls: type[ReplayPosition] | type[QuoteState] | type[FeeConfig]) -> object:
    data = _object(value).copy()
    hints = get_type_hints(cls)
    for name, item in data.items():
        if name == "fees" and cls is QuoteState:
            data[name] = _fees(item)
        elif name not in hints or not _matches(item, hints[name]):
            raise ValueError(f"invalid {cls.__name__}.{name}")
    allowed = {field.name for field in fields(cls)}
    if cls is QuoteState:
        required = {
            "kind",
            "version",
            "base_mint",
            "quote_mint",
            "virtual_base_reserves",
            "virtual_quote_reserves",
            "real_base_reserves",
            "real_quote_reserves",
            "fees",
            "protocol_fees",
            "creator_fees",
            "cashback",
            "mayhem",
            "canonical",
            "creator_beneficiary",
        }
    else:
        required = allowed
    _keys(data, required, allowed - required)
    for name, item in data.items():
        if isinstance(item, str) and not item:
            raise ValueError(f"empty {cls.__name__}.{name}")
        if type(item) is int and item < 0 and name != "virtual_quote_reserves":
            raise ValueError(f"negative {cls.__name__}.{name}")
    if cls is QuoteState:
        versions = {"curve": ("legacy", "v2", "v3"), "pool": ("legacy", "v1", "v2")}
        if data["version"] not in versions[str(data["kind"])]:
            raise ValueError("unknown state version")
        if data["virtual_quote_reserves"] is None:
            raise ValueError("explicit virtual_quote_reserves required")
        if data["kind"] == "pool" and data.get("quote_vault_amount") is None:
            raise ValueError("explicit pool quote_vault_amount required")
    return cls(**data)  # type: ignore[arg-type]


def _fees(value: object) -> FeeConfig:
    return cast(FeeConfig, _typed(value, FeeConfig))


def _position(value: object) -> ReplayPosition:
    return cast(ReplayPosition, _typed(value, ReplayPosition))


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("expected nonempty string")
    return value


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("expected nonnegative integer")
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate field: {key}")
        result[key] = value
    return result


def _event(runtime: PumpReplayRuntime, event: dict[str, object]) -> object | None:
    kind = event.get("kind")
    if kind == "state":
        _keys(event, {"kind", "venue_id", "state", "position", "event_id", "state_timing"})
        timing = event["state_timing"]
        if timing not in ("after_transaction", "after_outer_instruction"):
            raise ValueError("invalid state_timing")
        runtime.observe(
            StateObservation(
                venue_id=_text(event["venue_id"]),
                state=cast(QuoteState, _typed(event["state"], QuoteState)),
                position=_position(event["position"]),
                event_id=_text(event["event_id"]),
                state_timing=timing,
            )
        )
    elif kind == "order":
        _keys(
            event,
            {
                "kind",
                "order_id",
                "position",
                "venue_ids",
                "input_mint",
                "output_mint",
                "amount_in",
                "network_fee_lamports",
            },
            {"min_amount_out", "mode"},
        )
        venues = event["venue_ids"]
        if not isinstance(venues, list) or not venues:
            raise ValueError("venue_ids must be a nonempty array")
        mode = event.get("mode", "sequential")
        if mode not in ("sequential", "multi_hop"):
            raise ValueError("invalid order mode")
        receipt = runtime.execute(
            ReplayOrder(
                order_id=_text(event["order_id"]),
                position=_position(event["position"]),
                venue_ids=tuple(_text(venue) for venue in venues),
                input_mint=_text(event["input_mint"]),
                output_mint=_text(event["output_mint"]),
                amount_in=_integer(event["amount_in"]),
                min_amount_out=_integer(event.get("min_amount_out", 0)),
                mode=mode,
                network_fee_lamports=_integer(event["network_fee_lamports"]),
            )
        )
        return asdict(receipt)
    elif kind == "sweep":
        _keys(event, {"kind", "venue_id", "position"}, {"component"})
        component = event.get("component", "all")
        if component not in ("all", "protocol", "creator"):
            raise ValueError("invalid sweep component")
        runtime.sweep(
            _text(event["venue_id"]),
            _position(event["position"]),
            component=component,
        )
    else:
        raise ValueError("expected state, order, or sweep event")
    return None


def run_cli(input_path: Path, out: Path) -> dict[str, object]:
    """Publish a complete hash-manifested replay, or remove only our private staging."""
    if out.exists() or out.is_symlink():
        raise FileExistsError("output destination already exists")
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out.name}-", dir=out.parent))
    input_hash = hashlib.sha256()
    receipt_hash = hashlib.sha256()
    runtime: PumpReplayRuntime | None = None
    line_number = 0
    try:
        with input_path.open("rb") as source, (staging / "receipts.jsonl").open("wb") as receipts:
            while raw := source.readline(MAX_LINE_BYTES + 1):
                line_number += 1
                try:
                    if len(raw) > MAX_LINE_BYTES:
                        raise ValueError("JSONL line exceeds byte limit")
                    input_hash.update(raw)
                    event = _object(json.loads(raw, object_pairs_hook=_pairs))
                    if runtime is None:
                        _keys(event, {"kind", "balances"})
                        if event["kind"] != "balances":
                            raise ValueError("first event must be balances")
                        balances = _object(event["balances"])
                        runtime = PumpReplayRuntime(
                            {_text(mint): _integer(amount) for mint, amount in balances.items()}
                        )
                    else:
                        receipt = _event(runtime, event)
                        if receipt is not None:
                            encoded = _canonical(receipt)
                            receipts.write(encoded)
                            receipt_hash.update(encoded)
                except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
                    raise ValueError(f"line {line_number}: {exc}") from None
            if runtime is None:
                raise ValueError("line 1: expected balances event; empty input")
            receipts.flush()
            os.fsync(receipts.fileno())
        summary: dict[str, object] = {
            "exact_execution": False,
            "historical_input_path": str(input_path),
            "replay_caveat": CAVEAT,
            "input_sha256": input_hash.hexdigest(),
            "receipts_sha256": receipt_hash.hexdigest(),
            "receipt_count": runtime.receipt_count,
            "balances": dict(runtime.balances),
            "implementation_provenance": {
                "quote_source_docs_commit": "639b87fb45c3adeffa6f7c4a1bf0cb85e392bf22",
                "replay_cli_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "runtime_sha256": hashlib.sha256(
                    (
                        Path(__file__).parents[1] / "plugins/protocols/pumpfun/modern_runtime.py"
                    ).read_bytes()
                ).hexdigest(),
                "quote_code_sha256": hashlib.sha256(
                    (Path(__file__).parents[1] / "plugins/protocols/pumpfun/quotes.py").read_bytes()
                ).hexdigest(),
            },
        }
        summary_bytes = _canonical(summary)
        _write(staging / "summary.json", summary_bytes)
        _write(
            staging / "COMMITTED",
            _canonical(
                {
                    "input_sha256": input_hash.hexdigest(),
                    "receipts_sha256": receipt_hash.hexdigest(),
                    "summary_sha256": hashlib.sha256(summary_bytes).hexdigest(),
                }
            ),
        )
        if out.exists() or out.is_symlink():
            raise FileExistsError("output destination already exists")
        staging.rename(out)
        return summary
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _write(path: Path, payload: bytes) -> None:
    with path.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        summary = run_cli(args.input, args.out)
    except (OSError, ValueError, TypeError) as exc:
        print(f"pump-replay: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
