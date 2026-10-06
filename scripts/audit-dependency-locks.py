"""Query OSV for pinned Python/npm packages without installing or running them."""

from __future__ import annotations

import argparse
import json
import tomllib
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    python = tomllib.loads((root / "uv.lock").read_text())["package"]
    queries = [
        {"package": {"name": p["name"], "ecosystem": "PyPI"}, "version": p["version"]}
        for p in python
        if "registry" in p.get("source", {})
    ]
    node = json.loads((root / "frontend/package-lock.json").read_text())["packages"]
    queries.extend(
        {
            "package": {"name": name.split("node_modules/")[-1], "ecosystem": "npm"},
            "version": value["version"],
        }
        for name, value in node.items()
        if name and value.get("version") and not value.get("link")
    )
    request = urllib.request.Request(
        "https://api.osv.dev/v1/querybatch",
        data=json.dumps({"queries": queries}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        results = json.load(response)["results"]
    findings = [
        {**query, "vulnerabilities": result["vulns"]}
        for query, result in zip(queries, results, strict=True)
        if result.get("vulns")
    ]
    report = {
        "source": "https://api.osv.dev/v1/querybatch",
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "queries": len(queries),
        "findings": findings,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    raise SystemExit(bool(findings))


if __name__ == "__main__":
    main()
