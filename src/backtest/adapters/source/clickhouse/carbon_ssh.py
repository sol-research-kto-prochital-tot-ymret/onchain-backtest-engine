"""Bounded SELECT through our existing server client; credentials stay on server."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any, cast

from backtest.adapters.source.clickhouse.carbon import QUERY_SETTINGS


@dataclass(frozen=True)
class Rows:
    column_names: list[str]
    result_rows: list[list[Any]]


class SshCarbonClient:
    def __init__(self, target: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]{0,252}", target):
            raise ValueError("SSH target must be an existing trusted user@host")
        self.target = target

    def query(self, query: str, *, parameters: dict[str, Any], settings: dict[str, Any]) -> Rows:
        if settings != QUERY_SETTINGS or not query.lstrip().startswith("SELECT "):
            raise ValueError("SSH export accepts the bounded SELECT contract only")
        command = [
            "clickhouse-client",
            "--config-file=/etc/solana-collector/clickhouse-client.xml",
            "--format=JSONEachRow",
            *(f"--{name}={value}" for name, value in settings.items()),
            *(f"--param_{name}={value!s}" for name, value in parameters.items()),
        ]
        # shlex quotes every remote argument; SQL travels over stdin, not a shell string.
        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-T",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    self.target,
                    shlex.join(command),
                ],
                input=query,
                capture_output=True,
                text=True,
                timeout=150,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError(
                "bounded SSH export failed; check the existing SSH connection"
            ) from None
        if result.returncode or len(result.stdout.encode()) > cast(
            int, QUERY_SETTINGS["max_result_bytes"]
        ):
            # Remote errors can contain configuration details; do not copy them to reports.
            raise ValueError("bounded SSH SELECT failed; check server access and query limits")
        objects = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        if len(objects) > cast(int, QUERY_SETTINGS["max_result_rows"]) or any(
            not isinstance(row, dict) for row in objects
        ):
            raise ValueError("invalid bounded SSH query response")
        columns = list(objects[0]) if objects else []
        if any(set(row) != set(columns) for row in objects):
            raise ValueError("inconsistent SSH query response schema")
        return Rows(columns, [[row[name] for name in columns] for row in objects])
