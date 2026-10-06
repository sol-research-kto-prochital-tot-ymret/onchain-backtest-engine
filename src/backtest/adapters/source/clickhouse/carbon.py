"""Bounded, read-only bridge for our Carbon instruction projections.

This is a research source, not an exact-admission OnchainDivers substitute.
TradeEvent.user is preserved as an actor; it is never promoted to a signer.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from backtest.domain.copytrading import require_solana_wallet
from backtest.domain.identifiers import AccountId

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_AMOUNTS = (
    "sol_amount",
    "token_amount",
    "protocol_fee",
    "creator_fee",
    "cashback",
    "protocol_fee_bps",
    "creator_fee_bps",
    "cashback_fee_bps",
    "virtual_sol_reserves",
    "virtual_token_reserves",
    "real_sol_reserves",
    "real_token_reserves",
)
_RESERVES = _AMOUNTS[-4:]
_CARBON_NAMES = {
    "protocol_fee": "fee",
    "protocol_fee_bps": "fee_basis_points",
    "creator_fee_bps": "creator_fee_basis_points",
    "cashback_fee_bps": "cashback_fee_basis_points",
}


class CarbonClient(Protocol):
    def query(self, query: str, *, parameters: dict[str, Any], settings: dict[str, Any]) -> Any: ...


def public_key(value: object) -> str:
    if isinstance(value, list):
        if len(value) != 32 or any(type(x) is not int or not 0 <= x <= 255 for x in value):
            raise ValueError("public key must contain exactly 32 bytes")
        number = int.from_bytes(bytes(value), "big")
        text = ""
        while number:
            number, digit = divmod(number, 58)
            text = _ALPHABET[digit] + text
        text = "1" * (32 - len(bytes(value).lstrip(b"\0"))) + text
    elif isinstance(value, str):
        text = value
    else:
        raise ValueError("public key must be a base58 address or byte array")
    require_solana_wallet(AccountId(text))
    return text


def uint(value: object, name: str) -> int:
    # ClickHouse JSON may spell UInt64 as strings; floats/bools are never amounts.
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is not int or not 0 <= value < 1 << 64:
        raise ValueError(f"{name} must be a UInt64")
    return value


def normalize_trade(row: Mapping[str, Any], wallet: str | None = None) -> dict[str, Any]:
    """Accept Carbon raw_json or our prior flattened, frozen research extracts."""
    result: dict[str, Any]
    if "raw_json" in row:
        row = json.loads(row["raw_json"])
    if "decoded" in row:
        if row.get("decoder") != "pumpfun" or row.get("success") is not True:
            raise ValueError("only successful Pump.fun execution events are accepted")
        event = row["decoded"].get("data", {}).get("data", {}).get("TradeEvent")
        if not isinstance(event, dict):
            raise ValueError("instruction is not a Pump.fun TradeEvent")
        result = {
            name: event.get(name, event.get(_CARBON_NAMES.get(name, name))) for name in _AMOUNTS
        }
        # Buyback is a distribution of the protocol fee, not an additional fee.
        if event.get("quote_mint") is not None and public_key(event["quote_mint"]) not in {
            "11111111111111111111111111111111",
            "So11111111111111111111111111111111111111112",
        }:
            raise ValueError("non-SOL Pump quote assets are outside this research adapter")
        result.update(
            {
                name: row.get(name)
                for name in (
                    "slot",
                    "signature",
                    "transaction_index",
                    "absolute_path",
                    "block_time",
                    "fee_payer",
                    "fee_lamports",
                    "source",
                    "commitment",
                )
            }
        )
        result.update(
            mint=event.get("mint"),
            user=event.get("user"),
            is_buy=event.get("is_buy"),
            mayhem_mode=event.get("mayhem_mode"),
        )
        result["source_priority"] = 2 if result.get("source") == "sqd" else 1
        result["actor_attribution"] = "trade_event_user"
    else:
        result = dict(row)
        # Older extracts contain only fee payer. Make this weaker attribution explicit.
        result["actor_attribution"] = "trade_event_user" if "user" in row else "fee_payer_proxy"
        result["user"] = row.get("user", row.get("fee_payer"))
    result["mint"] = public_key(result.get("mint"))
    result["user"] = public_key(result.get("user"))
    result["fee_payer"] = public_key(result.get("fee_payer"))
    if wallet is not None and result["user"] != public_key(wallet):
        raise ValueError("event actor does not match the selected wallet")
    for name in (*_AMOUNTS, "slot", "transaction_index", "block_time", "fee_lamports"):
        result[name] = uint(result.get(name), name)
    if result["transaction_index"] >= (1 << 32) - 1 or result["slot"] >= (1 << 32) - 1:
        raise ValueError("position exceeds the engine's UInt64 boundary schema")
    for name in ("is_buy", "mayhem_mode"):
        if result.get(name) not in (True, False, 0, 1) or type(result.get(name)) not in (bool, int):
            raise ValueError(f"{name} must be an explicitly observed Boolean")
        result[name] = bool(result[name])
    path = result.get("absolute_path")
    if not isinstance(path, (list, tuple)) or not path:
        raise ValueError("instruction path is required")
    result["absolute_path"] = [uint(x, "instruction path") for x in path]
    signature = result.get("signature")
    if not isinstance(signature, str) or not 1 <= len(signature) <= 128:
        raise ValueError("bounded transaction signature is required")
    result["source_priority"] = uint(result.get("source_priority", 0), "source_priority")
    return result


def order(row: Mapping[str, Any]) -> tuple[int, int, tuple[int, ...]]:
    return row["slot"], row["transaction_index"], tuple(row["absolute_path"])


def deduplicate(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[tuple[object, ...], dict[str, Any]] = {}
    for item in rows:
        row = dict(item)
        key = row["slot"], row["signature"], tuple(row["absolute_path"])
        previous = selected.get(key)
        if previous is None or row["source_priority"] > previous["source_priority"]:
            selected[key] = row
        elif row["source_priority"] == previous["source_priority"]:
            semantics = ("mint", "user", "transaction_index", "is_buy", "mayhem_mode", *_AMOUNTS)
            if any(row.get(k) != previous.get(k) for k in semantics):
                raise ValueError("conflicting equally authoritative execution events")
    return sorted(selected.values(), key=order)


def normalize_state(row: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {name: uint(row.get(name), name) for name in (*_RESERVES, "slot")}
    result["block_time"] = uint(row.get("block_time", row.get("state_block_time")), "block_time")
    result["mint"] = public_key(row.get("mint"))
    result["last_transaction_index"] = uint(
        row.get("last_transaction_index"), "last_transaction_index"
    )
    result["last_absolute_path"] = [uint(x, "instruction path") for x in row["last_absolute_path"]]
    return result


def _pubkey_sql(field: str) -> str:
    # Field paths are private checked-in constants, never user input.
    return (
        "base58Encode(arrayStringConcat(arrayMap(x -> char(x), "
        f"JSONExtract(decoded_json, 'data', 'data', 'TradeEvent', '{field}', 'Array(UInt8)')), ''))"
    )


LEADER_SQL = f"""SELECT raw_json
FROM solana.instructions
PREWHERE slot >= {{start:UInt64}} AND slot < {{end:UInt64}} AND decoder = 'pumpfun'
WHERE success = 1 AND block_time IS NOT NULL
  AND JSONExtractRaw(decoded_json, 'data', 'data', 'TradeEvent') != ''
  AND {_pubkey_sql("user")} = {{wallet:String}}
ORDER BY slot, transaction_index, absolute_path, source_priority
LIMIT {{limit:UInt64}}"""

_RESERVE_SQL = ",\n    ".join(
    "argMax(JSONExtractUInt(decoded_json, 'data', 'data', 'TradeEvent', "
    f"'{name}'), tuple(transaction_index, absolute_path, source_priority)) AS {name}"
    for name in _RESERVES
)
STATE_SQL = f"""SELECT slot, {_pubkey_sql("mint")} AS mint,
    argMax(block_time, tuple(transaction_index, absolute_path, source_priority))
      AS state_block_time,
    max(transaction_index) AS last_transaction_index,
    argMax(absolute_path, tuple(transaction_index, absolute_path, source_priority))
      AS last_absolute_path,
    {_RESERVE_SQL}
FROM solana.instructions
PREWHERE slot IN {{slots:Array(UInt64)}} AND decoder = 'pumpfun'
WHERE success = 1 AND block_time IS NOT NULL
  AND JSONExtractRaw(decoded_json, 'data', 'data', 'TradeEvent') != ''
  AND (slot, mint) IN {{pairs:Array(Tuple(UInt64,String))}}
GROUP BY slot, mint ORDER BY slot, mint"""

QUERY_SETTINGS = {
    "readonly": 1,
    "max_execution_time": 120,
    "max_memory_usage": 1_073_741_824,
    "max_rows_to_read": 50_000_000,
    "max_result_rows": 500_001,
    "max_result_bytes": 268_435_456,
    "result_overflow_mode": "throw",
}


def fetch_leader(
    client: CarbonClient, wallet: str, start: int, end: int, maximum_rows: int = 100_000
) -> list[dict[str, Any]]:
    public_key(wallet)
    uint(start, "start")
    uint(end, "end")
    if not start < end <= start + 250_000:
        raise ValueError("export requires a positive range of at most 250,000 slots")
    if not 1 <= maximum_rows <= 500_000:
        raise ValueError("maximum_rows must be between 1 and 500,000")
    result = client.query(
        LEADER_SQL,
        parameters={
            "start": start,
            "end": end,
            "wallet": wallet,
            "limit": maximum_rows + 1,
        },
        settings=QUERY_SETTINGS,
    )
    if len(result.result_rows) > maximum_rows:
        raise ValueError("leader export exceeds the row limit; narrow the range")
    return deduplicate(normalize_trade({"raw_json": raw[0]}, wallet) for raw in result.result_rows)


def fetch_states(client: CarbonClient, pairs: Iterable[tuple[int, str]]) -> list[dict[str, Any]]:
    wanted = sorted(set(pairs))
    if len(wanted) > 100_000:
        raise ValueError("too many requested states")
    rows: list[dict[str, Any]] = []
    for offset in range(0, len(wanted), 500):
        batch = wanted[offset : offset + 500]
        result = client.query(
            STATE_SQL,
            parameters={
                "slots": sorted({slot for slot, _ in batch}),
                "pairs": batch,
            },
            settings=QUERY_SETTINGS,
        )
        rows.extend(
            normalize_state(dict(zip(result.column_names, row, strict=True)))
            for row in result.result_rows
        )
    return rows
