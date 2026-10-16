"""Validate a bounded, immutable token-transfer overlay for a retained replay."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from backtest.domain.hashing import domain_digest

TOKEN_PROGRAMS = {
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
}


def _instructions(transaction: dict[str, Any]) -> Iterator[tuple[tuple[int, ...], dict[str, Any]]]:
    inner = {g["index"]: g["instructions"] for g in transaction["meta"]["innerInstructions"]}
    for index, instruction in enumerate(transaction["transaction"]["message"]["instructions"]):
        yield (index,), instruction
        active: dict[int, tuple[int, ...]] = {1: (index,)}
        counters: dict[tuple[int, ...], int] = {}
        for child in inner.get(index, ()):
            height = child.get("stackHeight")
            if not isinstance(height, int) or height < 2 or height - 1 not in active:
                raise ValueError("transfer evidence lacks inner instruction ordering")
            parent = active[height - 1]
            number = counters.get(parent, 0)
            counters[parent] = number + 1
            path = (*parent, number)
            active[height] = path
            for depth in list(active):
                if depth > height:
                    del active[depth]
            yield path, child


def _owners(transaction: dict[str, Any]) -> dict[str, tuple[str, str]]:
    keys = [
        a["pubkey"] if isinstance(a, dict) else a
        for a in transaction["transaction"]["message"]["accountKeys"]
    ]
    owners: dict[str, tuple[str, str]] = {}
    for side in ("preTokenBalances", "postTokenBalances"):
        for balance in transaction["meta"][side]:
            account = keys[balance["accountIndex"]]
            value = balance["mint"], balance["owner"]
            if account in owners and owners[account] != value:
                raise ValueError("token ownership changes need a separate causal decoder")
            owners[account] = value
    for _, instruction in _instructions(transaction):
        parsed = instruction.get("parsed")
        if not isinstance(parsed, dict):
            continue
        info, kind = parsed.get("info", {}), parsed.get("type")
        if kind in ("initializeAccount", "initializeAccount2", "initializeAccount3"):
            value = info.get("mint"), info.get("owner")
        elif (
            kind in ("create", "createIdempotent")
            and instruction.get("program") == "spl-associated-token-account"
        ):
            value = info.get("mint"), info.get("wallet")
        else:
            continue
        account = info.get("account")
        if account and all(value):
            if account in owners and owners[account] != value:
                raise ValueError("conflicting token owner evidence")
            owners[account] = value
    return owners


def enrich_inventory(source: Any, path: Path) -> None:
    transfers = json.loads(path.read_text())
    if not isinstance(transfers, list) or not transfers:
        raise ValueError("nonempty transfer evidence list required")
    seen = set()
    for movement in transfers:
        uid = movement["event_uid"]
        if uid in seen:
            raise ValueError("duplicate token transfer")
        seen.add(uid)
        if (
            type(movement["tokens"]) is not int
            or movement["tokens"] <= 0
            or movement["source_wallet"] == movement["destination_wallet"]
        ):
            raise ValueError("invalid external transfer")
        mint = movement["mint"]
        if mint not in source.birth_by_mint or movement["slot"] not in source.times:
            raise ValueError("transfer lies outside retained mint/clock scope")
        if not source.birth_by_mint[mint]["slot"] <= movement["slot"]:
            raise ValueError("transfer precedes token launch")
        evidence = (path.parent / movement["evidence_file"]).resolve()
        if not evidence.is_relative_to(path.parent.resolve()):
            raise ValueError("transfer evidence must remain within its artifact folder")
        if hashlib.sha256(evidence.read_bytes()).hexdigest() != movement["evidence_sha256"]:
            raise ValueError("transfer evidence hash mismatch")
        receipt = json.loads(evidence.with_suffix(".receipt.json").read_text())
        request = receipt["request"]
        if (
            receipt["status"] != 200
            or request["method"] != "getTransaction"
            or request["params"][0] != movement["signature"]
            or request["params"][1]["commitment"] != "finalized"
            or receipt["response_sha256"] != movement["evidence_sha256"]
        ):
            raise ValueError("transfer needs finalized matching transaction evidence")
        transaction = json.loads(evidence.read_text())["result"]
        if (
            transaction["meta"]["err"] is not None
            or transaction["slot"] != movement["slot"]
            or transaction["transactionIndex"] != movement["tx_idx"]
            or transaction["transaction"]["signatures"][0] != movement["signature"]
        ):
            raise ValueError("transfer transaction identity mismatch")
        instructions = dict(_instructions(transaction))
        instruction = instructions[tuple(movement["instruction_path"])]
        parsed = instruction.get("parsed", {})
        if instruction.get("programId") not in TOKEN_PROGRAMS or parsed.get("type") not in (
            "transfer",
            "transferChecked",
        ):
            raise ValueError("evidence is not a token transfer")
        info = parsed["info"]
        amount = int(info.get("amount", info.get("tokenAmount", {}).get("amount")))
        owners = _owners(transaction)
        if (
            info["source"] != movement["source_token_account"]
            or info["destination"] != movement["destination_token_account"]
            or amount != movement["tokens"]
            or owners[info["source"]] != (mint, movement["source_wallet"])
            or owners[info["destination"]] != (mint, movement["destination_wallet"])
        ):
            raise ValueError("normalized transfer disagrees with transaction evidence")
        curve = source.birth_by_mint[mint]["payload"]["bonding_curve"]
        if curve in (movement["source_wallet"], movement["destination_wallet"]):
            raise ValueError("pool settlement transfer would duplicate a swap leg")
    source.inventory_transfers = tuple(
        sorted(
            transfers,
            key=lambda m: (m["slot"], m["tx_idx"], tuple(m["instruction_path"]), m["event_uid"]),
        )
    )
    source.inventory_base_source_sha256 = source.source_hash
    source.inventory_enrichment_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    source.source_hash = domain_digest(
        "dnipro.inventory-enriched-silver.v1",
        {
            "base_source": source.inventory_base_source_sha256,
            "transfers": source.inventory_enrichment_sha256,
            "semantics": "fixed-amount-conserving-wallet-transfers-at-transaction-order",
        },
    ).hex
