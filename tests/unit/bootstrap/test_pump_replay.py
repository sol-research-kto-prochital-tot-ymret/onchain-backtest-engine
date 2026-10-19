"""JSONL boundary and module-command integration tests."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backtest.bootstrap.pump_replay import MAX_LINE_BYTES, run_cli


def _events(kind: str = "pool") -> list[dict[str, object]]:
    position = {
        "slot": 1,
        "transaction_index": 0,
        "outer_instruction_index": 0,
        "unix_s": 1,
        "signature": "state-signature",
    }
    state = {
        "kind": kind,
        "version": "v2" if kind == "pool" else "v3",
        "base_mint": "BASE",
        "quote_mint": "QUOTE",
        "virtual_base_reserves": 100_000,
        "virtual_quote_reserves": 0 if kind == "pool" else 100_000,
        "real_base_reserves": 100_000,
        "real_quote_reserves": 100_000,
        "quote_vault_amount": 100_000,
        "protocol_fees": 0,
        "creator_fees": 0,
        "cashback": False,
        "mayhem": False,
        "canonical": True,
        "creator_beneficiary": "creator",
        "fees": {
            "protocol_bps": 100,
            "creator_bps": 50,
            "lp_bps": 0,
            "buyback_bps": 0,
            "contract": "resolved-pump-fees",
        },
    }
    return [
        {"kind": "balances", "balances": {"QUOTE": 10_000}},
        {
            "kind": "state",
            "venue_id": "venue",
            "event_id": "state-1",
            "position": position,
            "state": state,
            "state_timing": "after_transaction",
        },
        {
            "kind": "order",
            "order_id": "order-1",
            "venue_ids": ["venue"],
            "position": {**position, "slot": 2, "signature": "order-signature"},
            "input_mint": "QUOTE",
            "output_mint": "BASE",
            "amount_in": 1000,
            "network_fee_lamports": 0,
        },
    ]


def _input(tmp_path: Path, events: list[dict[str, object]]) -> Path:
    path = tmp_path / "input.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


@pytest.mark.parametrize("kind", ["pool", "curve"])
def test_replay_and_hash_manifest(tmp_path: Path, kind: str) -> None:
    source = _input(tmp_path, _events(kind))
    out = tmp_path / "result"
    summary = run_cli(source, out)
    assert summary["exact_execution"] is False
    assert summary["receipt_count"] == 1
    assert len((out / "receipts.jsonl").read_text().splitlines()) == 1
    committed = json.loads((out / "COMMITTED").read_bytes())
    for key, path in (
        ("input_sha256", source),
        ("receipts_sha256", out / "receipts.jsonl"),
        ("summary_sha256", out / "summary.json"),
    ):
        assert committed[key] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert json.loads((out / "summary.json").read_bytes()) == summary
    with pytest.raises(FileExistsError):
        run_cli(source, out)


@pytest.mark.parametrize(
    "field,value",
    [
        ("network_fee_lamports", True),
        ("amount_in", 1.5),
        ("amount_in", "1000"),
        ("unexpected", "secret"),
        ("mode", "guess"),
    ],
)
def test_rejects_malformed_order(tmp_path: Path, field: str, value: object) -> None:
    events = _events()
    events[2][field] = value
    source = _input(tmp_path, events)
    with pytest.raises(ValueError, match="line 3"):
        run_cli(source, tmp_path / "out")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["input.jsonl"]


@pytest.mark.parametrize(
    "event_index,field",
    [
        (1, "state_timing"),
        (1, "event_id"),
        (2, "network_fee_lamports"),
    ],
)
def test_required_fields(tmp_path: Path, event_index: int, field: str) -> None:
    events = _events()
    del events[event_index][field]
    with pytest.raises(ValueError, match=f"line {event_index + 1}"):
        run_cli(_input(tmp_path, events), tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{}\n",
        b'{"kind":"balances","balances":{"Q":true}}\n',
        b'{"kind":"balances","kind":"balances","balances":{}}\n',
        b"x" * (MAX_LINE_BYTES + 1),
        b"\xff\n",
    ],
)
def test_bad_first_record(tmp_path: Path, raw: bytes) -> None:
    source = tmp_path / "input.jsonl"
    source.write_bytes(raw)
    with pytest.raises(ValueError, match="line 1"):
        run_cli(source, tmp_path / "out")
    assert list(tmp_path.iterdir()) == [source]


def test_no_later_balances_or_raw_normalizer_state(tmp_path: Path) -> None:
    for event in (
        {"kind": "balances", "balances": {}},
        {"kind": "state", "event_id": "normalizer-only"},
    ):
        source = _input(tmp_path, [_events()[0], event])
        with pytest.raises(ValueError, match="line 2"):
            run_cli(source, tmp_path / "out")


def test_module_command(tmp_path: Path) -> None:
    source = _input(tmp_path, _events())
    out = tmp_path / "out"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src")}
    command = [
        sys.executable,
        "-m",
        "backtest.bootstrap.pump_replay",
        "--input",
        str(source),
        "--out",
        str(out),
    ]
    result = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["receipt_count"] == 1
    rejected = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
    assert rejected.returncode == 2
    assert "already exists" in rejected.stderr


@pytest.mark.parametrize("component", ["protocol", "creator", "all"])
def test_independent_sweep_components(tmp_path: Path, component: str) -> None:
    events = _events()
    events.append(
        {
            "kind": "sweep",
            "venue_id": "venue",
            "component": component,
            "position": {
                "slot": 3,
                "transaction_index": 0,
                "outer_instruction_index": 0,
                "unix_s": 3,
                "signature": "sweep-signature",
            },
        }
    )
    summary = run_cli(_input(tmp_path, events), tmp_path / "out")
    assert summary["receipt_count"] == 1
    assert summary["exact_execution"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "unknown"),
        ("protocol_fees", True),
        ("cashback", 0),
        ("virtual_quote_reserves", None),
        ("quote_vault_amount", None),
        ("source_secret", "do-not-accept"),
    ],
)
def test_strict_state_fields(tmp_path: Path, field: str, value: object) -> None:
    events = _events()
    state = events[1]["state"]
    assert isinstance(state, dict)
    state[field] = value
    with pytest.raises(ValueError, match="line 2"):
        run_cli(_input(tmp_path, events), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_sweep_and_fee_contract_required(tmp_path: Path) -> None:
    events = _events()
    events.append(
        {
            "kind": "sweep",
            "venue_id": "venue",
            "position": {
                "slot": 3,
                "transaction_index": 0,
                "outer_instruction_index": 0,
                "unix_s": 3,
                "signature": "sweep-signature",
            },
        }
    )
    assert run_cli(_input(tmp_path, events), tmp_path / "out")["receipt_count"] == 1
    state = events[1]["state"]
    assert isinstance(state, dict)
    fees = state["fees"]
    assert isinstance(fees, dict)
    del fees["contract"]
    with pytest.raises(ValueError, match="line 2"):
        run_cli(_input(tmp_path, events), tmp_path / "invalid")
