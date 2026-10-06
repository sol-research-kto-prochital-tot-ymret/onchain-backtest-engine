"""CLI composition for our isolated Carbon research adapter."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import platform
from importlib.metadata import version
from pathlib import Path
from typing import Any

from backtest.adapters.artifacts.dnipro import (
    file_hash,
    load,
    publish,
    publish_result,
    require_exact,
)
from backtest.adapters.source.clickhouse.carbon import (
    deduplicate,
    fetch_leader,
    fetch_states,
    normalize_state,
    normalize_trade,
    public_key,
)
from backtest.adapters.source.clickhouse.carbon_ssh import SshCarbonClient
from backtest.domain.hashing import canonical_json_bytes
from backtest.plugins.strategies.leader_slot_copy import LeaderSlotPolicy, replay


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if path.stat().st_size > 268_435_456:
        raise ValueError("input exceeds the 256 MiB bound")
    rows: list[dict[str, Any]] = []
    with path.open() as stream:
        for line in iter(lambda: stream.readline(1_048_577), ""):
            if len(line) > 1_048_576 or len(rows) >= 500_000:
                raise ValueError("input row or row-count limit exceeded")
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("JSONL rows must be objects")
                rows.append(row)
    return rows


def _implementation_identity() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    # Bind research output to all engine source files, in addition to its input snapshot.
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return {
        "engine_source_sha256": digest.hexdigest(),
        "python": platform.python_version(),
        "pyarrow": version("pyarrow"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Carbon/ClickHouse observational backtests; no wallet keys"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    importer = sub.add_parser("import-snapshot")
    importer.add_argument("--trades", type=Path, required=True)
    importer.add_argument("--states", type=Path, required=True)
    importer.add_argument("--wallet", required=True)
    importer.add_argument("--out", type=Path, required=True)
    exporter = sub.add_parser("export-clickhouse")
    exporter.add_argument("--host", default="127.0.0.1")
    exporter.add_argument("--port", type=int, default=18125)
    exporter.add_argument("--secure", action="store_true")
    exporter.add_argument("--username", default="research")
    exporter.add_argument("--password-env", default="DNIPRO_CLICKHOUSE_PASSWORD")
    exporter.add_argument("--start-slot", type=int, required=True)
    exporter.add_argument("--end-slot", type=int, required=True)
    exporter.add_argument("--wallet", required=True)
    exporter.add_argument("--out", type=Path, required=True)
    server = sub.add_parser("export-server")
    server.add_argument("--ssh-target", required=True)
    server.add_argument("--start-slot", type=int, required=True)
    server.add_argument("--end-slot", type=int, required=True)
    server.add_argument("--wallet", required=True)
    server.add_argument("--out", type=Path, required=True)
    copier = sub.add_parser("copy-slots")
    copier.add_argument("--snapshot", type=Path, required=True)
    copier.add_argument("--start-time", type=int, required=True)
    copier.add_argument("--end-time", type=int, required=True)
    copier.add_argument("--initial-sol-lamports", type=int, default=100_000_000_000)
    copier.add_argument("--entry-network-fee-lamports", type=int)
    copier.add_argument("--exit-network-fee-lamports", type=int)
    copier.add_argument("--require-exact", action="store_true")
    copier.add_argument("--out", type=Path, required=True)
    verifier = sub.add_parser("verify")
    verifier.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "import-snapshot":
            wallet = public_key(args.wallet)
            trades = deduplicate(normalize_trade(row, wallet) for row in _jsonl(args.trades))
            states = [normalize_state(row) for row in _jsonl(args.states)]
            manifest = publish(
                args.out,
                trades,
                states,
                {
                    "wallet": wallet,
                    "source": "frozen-clickhouse-jsonl",
                    "input_hashes": {
                        "trades": file_hash(args.trades),
                        "states": file_hash(args.states),
                    },
                    "actor_attribution": sorted({r["actor_attribution"] for r in trades}),
                },
            )
            print(
                json.dumps(
                    {
                        "snapshot": str(args.out),
                        "trade_count": manifest["trade_count"],
                        "state_count": manifest["state_count"],
                        "exact_execution": False,
                    }
                )
            )
        elif args.command == "export-clickhouse":
            _export(args)
        elif args.command == "export-server":
            client = SshCarbonClient(args.ssh_target)
            trades = fetch_leader(client, args.wallet, args.start_slot, args.end_slot)
            states = fetch_states(client, ((r["slot"], r["mint"]) for r in trades))
            result = publish(
                args.out,
                trades,
                states,
                {
                    "wallet": args.wallet,
                    "source": "carbon-clickhouse-ssh",
                    "range": [args.start_slot, args.end_slot],
                    "actor_attribution": ["trade_event_user"],
                },
            )
            print(
                json.dumps(
                    {
                        "snapshot": str(args.out),
                        "trade_count": result["trade_count"],
                        "state_count": result["state_count"],
                        "exact_execution": False,
                    }
                )
            )
        elif args.command == "verify":
            manifest, _, _ = load(args.snapshot)
            print(json.dumps({"verified": True, "fidelity": manifest["fidelity"]}))
        else:
            manifest, trades, states = load(args.snapshot)
            if args.require_exact:
                require_exact(manifest)
            policy = LeaderSlotPolicy(
                args.start_time,
                args.end_time,
                args.initial_sol_lamports,
                args.entry_network_fee_lamports,
                args.exit_network_fee_lamports,
            )
            summary, positions, ledger = replay(trades, states, policy)
            summary["snapshot_manifest_sha256"] = file_hash(args.snapshot / "manifest.json")
            summary["implementation"] = _implementation_identity()
            summary["actor_attribution"] = manifest["metadata"]["actor_attribution"]
            # The hash includes every execution input, rather than only its PnL output.
            summary["policy"] = {
                "start_time": args.start_time,
                "end_time": args.end_time,
                "initial_sol_lamports": args.initial_sol_lamports,
                "entry_network_fee_lamports": args.entry_network_fee_lamports,
                "exit_network_fee_lamports": args.exit_network_fee_lamports,
                "entry_delay_slots": 1,
                "exit_delay_slots": 1,
            }
            document = {"summary": summary, "positions": positions, "ledger": ledger}
            payload = canonical_json_bytes(document)
            publish_result(args.out, payload)
            print(
                json.dumps(
                    {
                        "result": str(args.out),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "summary": summary,
                    },
                    indent=2,
                )
            )
    except (ValueError, OSError) as error:
        # Raw database errors can contain credentials/URLs; _export sanitizes them.
        parser.exit(2, f"Error: {error}\n")


def _export(args: argparse.Namespace) -> None:
    try:
        address = ipaddress.ip_address(args.host)
        loopback = address.is_loopback
    except ValueError:
        loopback = args.host == "localhost"
    if not args.secure and not loopback:
        raise ValueError(
            "remote ClickHouse requires verified TLS; use an SSH tunnel for local HTTP"
        )
    public_key(args.wallet)
    # Import transport only in acquisition, never in offline replay.
    import clickhouse_connect

    client = None
    try:
        client = clickhouse_connect.get_client(
            host=args.host,
            port=args.port,
            username=args.username,
            password=os.environ.get(args.password_env, ""),
            secure=args.secure,
            verify=True,
            database="solana",
            autogenerate_session_id=False,
            connect_timeout=10,
            send_receive_timeout=130,
        )
        trades = fetch_leader(client, args.wallet, args.start_slot, args.end_slot)
        states = fetch_states(client, ((r["slot"], r["mint"]) for r in trades))
    except Exception:
        raise ValueError(
            "bounded ClickHouse export failed; check connectivity, read-only grants, and coverage"
        ) from None
    finally:
        if client is not None:
            client.close()
    result = publish(
        args.out,
        trades,
        states,
        {
            "wallet": args.wallet,
            "source": "carbon-clickhouse",
            "range": [args.start_slot, args.end_slot],
            "actor_attribution": ["trade_event_user"],
        },
    )
    print(
        json.dumps(
            {
                "snapshot": str(args.out),
                "trade_count": result["trade_count"],
                "state_count": result["state_count"],
                "exact_execution": False,
            }
        )
    )


if __name__ == "__main__":
    main()
