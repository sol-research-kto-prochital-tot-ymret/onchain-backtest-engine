"""Map evidenced execution events, never instruction limits, to engine facts.

All quantities stay in atomic units. A successful containing transaction does
not prove that every inner call committed: that limitation is retained.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from typing import Any

from backtest.adapters.source.clickhouse.carbon import public_key, uint
from backtest.domain.hashing import canonical_json_bytes

SOL = "So11111111111111111111111111111111111111112"
SYSTEM = "11111111111111111111111111111111"
VERSION = "carbon-cache-normalizer-v1"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


@lru_cache(maxsize=32768)
def pump_curve(mint: str) -> str:
    """The Pump program's bonding-curve PDA; not an inferred pool identity.

    Same seed/Edwards decompression implementation as the collector's
    deploy/silver/semantic_context.py; validated against emitted CreateEvents.
    """
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

    def un58(value: str) -> bytes:
        number = 0
        for character in value:
            number = number * 58 + alphabet.index(character)
        return b"\0" * (len(value) - len(value.lstrip("1"))) + number.to_bytes(
            (number.bit_length() + 7) // 8, "big"
        )

    field = 2**255 - 19
    d = -121665 * pow(121666, field - 2, field) % field
    mint_bytes, program_bytes = un58(public_key(mint)), un58(PUMP_PROGRAM)
    for bump in range(255, -1, -1):
        encoded = hashlib.sha256(
            b"bonding-curve" + mint_bytes + bytes([bump]) + program_bytes + b"ProgramDerivedAddress"
        ).digest()
        y = int.from_bytes(encoded, "little") & (2**255 - 1)
        numerator, denominator = (y * y - 1) % field, (d * y * y + 1) % field
        on_curve = (
            denominator != 0
            and pow(numerator * denominator % field, (field - 1) // 2, field) != field - 1
        )
        if not on_curve:
            return public_key(list(encoded))
    raise ValueError("no Pump curve PDA bump")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def address(value: Any) -> str | None:
    return public_key(value) if value is not None else None


def quote_address(value: Any, *, legacy_sol: bool = False) -> str | None:
    result = address(value)
    return SOL if result == SYSTEM or (result is None and legacy_sol) else result


def decoded(raw: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    body = raw.get("decoded") or {}
    content = body.get("data") or {}
    payload = content.get("data") or {}
    name = body.get("type", "unknown")
    if name == "CpiEvent" and len(payload) == 1:
        name, payload = next(iter(payload.items()))
    if not isinstance(payload, dict):
        raise ValueError("decoded event is not an object")
    return name, payload, content.get("accounts") or {}


def context(row: dict[str, Any]) -> dict[str, Any]:
    raw = json.loads(row["raw_json"])
    if not isinstance(raw, dict):
        raise ValueError("raw_json is not an object")
    # The exporter copies the table's outer metadata too. Do not throw it away
    # as the old normalize_trade(raw_json=...) helper does.
    for name in ("slot", "signature", "transaction_index", "absolute_path", "decoder"):
        if name in raw and name in row and raw[name] != row[name]:
            raise ValueError(f"outer/raw {name} conflict")
    if "success" in raw and type(raw["success"]) is not bool:
        raise ValueError("raw success must be Boolean")
    if row.get("success") not in (0, 1, False, True):
        raise ValueError("outer success is not Boolean")
    if "success" in raw and raw["success"] != bool(row["success"]):
        raise ValueError("outer/raw success conflict")
    for name in (
        "source",
        "source_priority",
        "ingested_at",
        "observed_at",
        "commitment",
        "block_time",
        "block_hash",
        "fee_payer",
        "fee_lamports",
    ):
        if name in row:
            value = row[name]
            raw[name] = value.isoformat() if hasattr(value, "isoformat") else value
    raw["slot"] = uint(row["slot"], "slot")
    raw["transaction_index"] = uint(row["transaction_index"], "transaction_index")
    raw["absolute_path"] = [uint(x, "path") for x in row["absolute_path"]]
    if not raw["absolute_path"]:
        raise ValueError("empty instruction path")
    if raw["slot"] >= 1 << 32 or raw["transaction_index"] >= (1 << 32) - 1:
        raise ValueError("position exceeds engine schema")
    raw["success"] = bool(row["success"])
    raw["source_priority"] = uint(row["source_priority"], "source_priority")
    raw["fee_lamports"] = uint(raw["fee_lamports"], "fee_lamports")
    raw["fee_payer"] = public_key(raw["fee_payer"])
    return raw


def semantics(raw: dict[str, Any]) -> str:
    return digest(
        {
            key: raw.get(key)
            for key in (
                "slot",
                "signature",
                "transaction_index",
                "absolute_path",
                "decoder",
                "success",
                "decoded",
                "fee_payer",
                "fee_lamports",
                "block_time",
            )
        }
    )


def roles_for(
    raw: dict[str, Any], instructions: dict[tuple[int, ...], dict[str, Any]]
) -> dict[str, Any]:
    path = tuple(raw["absolute_path"])
    result: dict[str, Any] = {}
    for length in range(1, len(path)):
        parent = instructions.get(path[:length])
        if parent is not None and parent["decoder"] == raw["decoder"]:
            result.update(decoded(parent)[2])
    result.update(decoded(raw)[2])
    return result


def fact(raw: dict[str, Any], name: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "slot": raw["slot"],
        "transaction_index": raw["transaction_index"],
        "signature": raw["signature"],
        "instruction_path": raw["absolute_path"],
        "protocol": raw["decoder"],
        "event_name": name,
        "block_time": raw.get("block_time"),
        "source": raw.get("source"),
        "source_priority": raw["source_priority"],
        "ingested_at": raw.get("ingested_at"),
        "observed_at": raw.get("observed_at"),
        "commitment": raw.get("commitment"),
        "block_hash": raw.get("block_hash"),
        "fee_payer": raw["fee_payer"],
        "fee_lamports": raw["fee_lamports"],
        "transaction_success": True,
        "instruction_committed": True if len(raw["absolute_path"]) == 1 else None,
        "decoded_payload": payload,
        "source_record_id": digest(
            {
                k: raw.get(k)
                for k in (
                    "slot",
                    "signature",
                    "absolute_path",
                    "source",
                    "source_priority",
                    "ingested_at",
                )
            }
        ),
        "quality_flags": ["retained_instruction_subset", "chain_finality_unverified"]
        + (["inner_commitment_unverified"] if len(raw["absolute_path"]) > 1 else []),
    }


def signed_i128(value: Any) -> int | None:
    """Decode JSON i128 strings without treating absent offsets as zero."""
    if value is None:
        return None
    if type(value) is not int and not (
        isinstance(value, str)
        and value.removeprefix("-").isascii()
        and value.removeprefix("-").isdigit()
    ):
        raise ValueError("signed virtual quote offset is not an integer")
    result = int(value)
    if not -(1 << 127) <= result < (1 << 127):
        raise ValueError("signed virtual quote offset exceeds i128")
    return result


def pump_metadata(
    raw: dict[str, Any],
    p: dict[str, Any],
    out: dict[str, Any],
    instructions: dict[tuple[int, ...], dict[str, Any]],
) -> dict[str, Any] | None:
    """Expose causal modern state, not guessed account layouts or activation dates.

    Pool event inventory is BEFORE whereas its signed offset is AFTER. Retain
    both but never add them. An executable pool snapshot needs independently
    evidenced same-phase inventory and fee buckets; ordinary events lack these.
    """
    path = tuple(raw["absolute_path"])
    route = None
    instruction = p.get("ix_name")
    for length in range(1, len(path)):
        parent = instructions.get(path[:length])
        if parent is None:
            continue
        parent_name = decoded(parent)[0]
        snake = parent_name.replace("_", "").lower()
        if snake in ("multihopswap", "multihopcurveswap"):
            route = list(path[:length])
        if parent["decoder"] == raw["decoder"] and parent_name != "CpiEvent":
            instruction = instruction or parent_name
    ix = str(instruction or "").replace("_", "").lower()
    modern_fields = (
        "is_holder_reward",
        "holder_rewards_bps",
        "holder_rewards",
        "depth",
        "creator_fee_unclaimed",
        "protocol_fees",
        "creator_fees",
        "post_complete_base_out",
        "post_complete_quote_in",
    )
    multi_hop = route is not None or ix in ("multihopswap", "multihopcurveswap")
    modern = (
        multi_hop
        or any(k in p for k in modern_fields)
        or out["event_name"] == "PostCompleteBuyEvent"
    )
    modern = modern or (raw["decoder"] == "pumpfun" and ix.endswith("v3"))
    modern = modern or (
        raw["decoder"] == "pump_swap" and (ix.endswith("v2") or "virtual_quote_reserves" in p)
    )
    if not modern:
        return None
    version = "v3" if ix.endswith("v3") else "v2" if ix.endswith("v2") else None
    retained = multi_hop or (version == ("v3" if raw["decoder"] == "pumpfun" else "v2"))
    holder = p.get("is_holder_reward")
    if holder is not None and type(holder) is not bool:
        raise ValueError("holder reward mode is not Boolean")
    # A positive reward proves the beneficiary; zero does not prove its absence.
    beneficiary = (
        "holder_rewards"
        if holder is True or uint(p.get("holder_rewards", 0), "holder rewards") > 0
        else "creator"
        if holder is False
        else "unknown"
    )
    result: dict[str, Any] = {
        "state_kind": "curve" if raw["decoder"] == "pumpfun" else "pool",
        "instruction": instruction,
        "instruction_version": version,
        "instruction_version_evidence": "route" if route is not None else "instruction_name",
        "quote_mint": out["quote_mint"],
        "quote_units": "mint_atomic",
        "state_timing": "after" if raw["decoder"] == "pumpfun" else "mixed_before_after",
        "executable_state_verified": False,
        "creator_beneficiary": beneficiary,
        "fee_retention": (
            "protocol_and_creator; buyback_paid" if retained else "instruction_dependent"
        ),
        "held_fees_excluded_from_real_quote": True,
        "state_after": None,
        "missing_state": [],
    }
    if route is not None:
        result["route_instruction_path"] = route
        result["route_economic_role"] = "underlying_execution_only"
    for key in (*modern_fields, "creator_fee_bps", "initial_virtual_quote_reserves"):
        if key in p:
            result[key] = p[key]
    if "depth" in p:
        depth = uint(p["depth"], "depth")
        if depth > 2:
            raise ValueError("Pump quote dependency depth exceeds 2")
        result["depth"] = depth
    if out["quote_mint"] is None or not (
        p.get("quote_mint")
        or roles_for(raw, instructions).get("quote_mint")
        or roles_for(raw, instructions).get("quote_token_mint")
    ):
        raise ValueError("modern Pump native quote mint unresolved")
    if raw["decoder"] == "pump_swap":
        offset = signed_i128(p.get("virtual_quote_reserves"))
        result.update(signed_virtual_quote_offset_after=offset, offset_phase="after")
        result["missing_state"] = ["same_phase_after_inventory", "after_fee_buckets"]
        if offset is None:
            result["missing_state"].append("signed_virtual_quote_offset")
    elif out["event_name"] == "PostCompleteBuyEvent":
        result["synthetic_migration"] = {
            "pool_base_reserves_before": uint(p.get("pool_base_reserves_before"), "pool base"),
            "pool_quote_reserves_before": uint(p.get("pool_quote_reserves_before"), "pool quote"),
            "pool_base_reserves_after": uint(p.get("pool_base_reserves_after"), "pool base"),
            "pool_quote_reserves_after": uint(p.get("pool_quote_reserves_after"), "pool quote"),
            "physical_state_evidence": "PostCompleteBuyEvent",
            "destination_pool_exists": False,
        }
        physical = result["synthetic_migration"]
        if physical["pool_base_reserves_after"] + uint(p.get("base_out"), "base out") != physical[
            "pool_base_reserves_before"
        ] or physical["pool_quote_reserves_after"] != physical["pool_quote_reserves_before"] + uint(
            p.get("quote_in"), "quote in"
        ):
            raise ValueError("synthetic migration physical reserve mismatch")
        result["fee_retention"] = "protocol_and_creator; buyback_paid"
        result["missing_state"] = ["destination_pool_identity", "retained_fee_bucket_totals"]
    elif out["event_name"] == "TradeEvent":
        # Curve real_quote_reserves explicitly excludes retained fees. Do not
        # subtract creator_fee_unclaimed again or mistake it for a vault balance.
        result["state_after"] = {
            "virtual_base_reserves": out["reserve_base_after"],
            "virtual_quote_reserves": out["reserve_quote_after"],
            "real_base_reserves": out.get("real_base_after"),
            "real_quote_reserves": out.get("real_quote_after"),
        }
        result["missing_state"] = [
            "causal_creation_modes",
            "physical_base_token_balance",
            "migration_fee",
            "retained_fee_bucket_totals",
        ]
    return result


def normalize(
    raw: dict[str, Any], instructions: dict[tuple[int, ...], dict[str, Any]]
) -> tuple[dict[str, Any] | None, str]:
    if not raw["success"]:
        return None, "failed_transaction"
    name, p, _ = decoded(raw)
    roles = roles_for(raw, instructions)
    protocol = raw["decoder"]
    out = fact(raw, name, p)
    base = address(
        p.get("mint")
        or p.get("base_mint")
        or roles.get("base_mint")
        or roles.get("base_token_mint")
        or roles.get("mint")
    )
    quote = quote_address(
        p.get("quote_mint") or roles.get("quote_mint") or roles.get("quote_token_mint"),
        legacy_sol=protocol == "pumpfun",
    )
    pool = address(
        p.get("pool")
        or p.get("pool_state")
        or p.get("bonding_curve")
        or roles.get("pool")
        or roles.get("pool_state")
        or roles.get("bonding_curve")
    )
    if protocol == "pumpfun" and pool is None and base is not None:
        pool = pump_curve(base)
        out["venue_identity_evidence"] = "derived_pump_bonding_curve_pda"
    actor = address(p.get("user") or p.get("trader") or roles.get("user") or roles.get("payer"))
    out.update(mint=base, quote_mint=quote, actor=actor, venue_id=pool)
    if protocol == "jupiter_swap":
        return None, "router_observation_not_an_additional_pool_trade"
    if protocol in ("pumpfun", "pump_swap") and name in (
        "MultiHopSwap",
        "MultiHopCurveSwap",
        "multi_hop_swap",
        "multi_hop_curve_swap",
    ):
        return None, "router_observation_not_an_additional_pool_trade"
    if protocol in ("pumpfun", "pump_swap") and name in (
        "SweepBondingCurveFeeEvent",
        "SweepPoolFeeEvent",
    ):
        uint(p.get("amount"), "sweep amount")
        bucket = uint(p.get("bucket"), "sweep bucket")
        if bucket not in (0, 1):
            raise ValueError("unknown sweep fee bucket")
        return None, "fee_sweep_price_neutral; retained_bucket_payout_not_trade"
    if protocol == "pumpfun" and name == "CreateEvent":
        if not base or not quote or not pool:
            raise ValueError("launch asset/quote/curve unresolved")
        out.update(
            kind="TOKEN_LAUNCH",
            developer=address(p.get("creator")),
            creation_user=address(p.get("user")),
            decimals=None,
        )
        if not out["developer"] or not out["creation_user"]:
            raise ValueError("launch developer or creation user unresolved")
        modern = pump_metadata(raw, p, out, instructions)
        if modern is not None:
            out["modern_pump"] = modern
        return out, "accepted"
    if protocol == "pumpfun" and name in ("CompleteEvent", "CompletePumpAmmMigrationEvent"):
        # Migration's `pool` is the destination, not the retiring curve.
        curve = address(p.get("bonding_curve") or roles.get("bonding_curve"))
        if not curve:
            raise ValueError("lifecycle curve unresolved")
        out.update(
            kind="VENUE_LIFECYCLE",
            venue_id=curve,
            lifecycle="MIGRATED" if name == "CompletePumpAmmMigrationEvent" else "COMPLETED",
            destination_venue=address(p.get("pool")),
        )
        return out, "accepted"
    fees: list[dict[str, Any]] = []
    reserves: tuple[int | None, int | None] = (None, None)
    if protocol == "pumpfun" and name == "PostCompleteBuyEvent":
        # TradeEvent contains ONLY the curve part. This is a separately committed
        # pool-priced leg, not a router summary and not a pool that already exists.
        buy = True
        amount_in = uint(p.get("quote_in"), "synthetic quote in")
        amount_out = uint(p.get("base_out"), "synthetic base out")
        out.update(
            reserve_semantics="synthetic_pool_after; destination_not_yet_created",
            amount_semantics="synthetic_pool_part_only; quote_excludes_fees",
        )
        for label, field in (("protocol", "fee"), ("creator", "creator_fee")):
            if p.get(field) is not None and uint(p[field], field):
                fees.append({"component": label, "asset": quote, "amount": p[field]})
    elif protocol == "pumpfun" and name == "TradeEvent":
        if type(p.get("is_buy")) is not bool:
            raise ValueError("trade side is not Boolean")
        buy = p["is_buy"]
        quote_amount = uint(p.get("quote_amount", p.get("sol_amount")), "quote amount")
        tokens = uint(p.get("token_amount"), "token amount")
        # Use an evidenced bonding-curve address from the parent instruction;
        # do not assign the mint itself as a fictitious pool address.
        reserves = (
            uint(p.get("virtual_quote_reserves", p.get("virtual_sol_reserves")), "virtual quote"),
            uint(p.get("virtual_token_reserves"), "virtual base"),
        )
        out.update(
            reserve_semantics="virtual_pricing_after; real_inventory_separate",
            real_quote_after=p.get("real_quote_reserves", p.get("real_sol_reserves")),
            real_base_after=p.get("real_token_reserves"),
            amount_semantics="pump_quote_excludes_external_fees",
            mayhem_mode=p.get("mayhem_mode"),
            cashback=p.get("cashback"),
            buyback_fee=p.get("buyback_fee"),
        )
        for label, field in (("protocol", "fee"), ("creator", "creator_fee")):
            if p.get(field) is not None and uint(p[field], field):
                fees.append({"component": label, "asset": quote, "amount": p[field]})
        amount_in, amount_out = (quote_amount, tokens) if buy else (tokens, quote_amount)
    elif protocol == "pump_swap" and name in ("BuyEvent", "SellEvent"):
        buy = name == "BuyEvent"
        tokens = uint(p.get("base_amount_out" if buy else "base_amount_in"), "base amount")
        quote_amount = uint(p.get("quote_amount_in" if buy else "quote_amount_out"), "quote amount")
        amount_in, amount_out = (quote_amount, tokens) if buy else (tokens, quote_amount)
        # These fields are emitted BEFORE the swap. They must never be passed
        # into the generic engine as AFTER reserves or fabricated liquidity.
        out.update(
            reserve_semantics="emitted_before; after_pricing_unverified",
            base_reserve_before=p.get("pool_base_token_reserves"),
            quote_reserve_before=p.get("pool_quote_token_reserves"),
            amount_semantics="pump_swap_emitted_quote_amount; user_settlement_separate",
            user_quote_amount=p.get("user_quote_amount_in" if buy else "user_quote_amount_out"),
        )
        for label, field in (
            ("lp", "lp_fee"),
            ("protocol", "protocol_fee"),
            ("creator", "coin_creator_fee"),
        ):
            if p.get(field) is not None and uint(p[field], field):
                fees.append({"component": label, "asset": quote, "amount": p[field]})
    elif protocol == "raydium_launchpad" and name == "TradeEvent":
        side = p.get("trade_direction")
        if side not in ("Buy", "Sell", "QuoteToBase", "BaseToQuote"):
            raise ValueError("LaunchLab side unsupported")
        buy = side in ("Buy", "QuoteToBase")
        amount_in, amount_out = (
            uint(p.get("amount_in"), "amount_in"),
            uint(p.get("amount_out"), "amount_out"),
        )
        out.update(
            reserve_semantics="launchlab_virtual_parameters; pricing_formula_unverified",
            real_base_after=p.get("real_base_after"),
            real_quote_after=p.get("real_quote_after"),
            amount_semantics="launchlab_event_amounts",
        )
        for label, field in (
            ("protocol", "protocol_fee"),
            ("platform", "platform_fee"),
            ("creator", "creator_fee"),
            ("share", "share_fee"),
        ):
            if p.get(field) is not None and uint(p[field], field):
                fees.append({"component": label, "asset": quote, "amount": p[field]})
    elif protocol == "meteora_dbc" and name in ("EvtSwap", "EvtSwap2"):
        if p.get("trade_direction") not in (0, 1):
            raise ValueError("DBC side unsupported")
        # Older+newer event versions can describe one execution. Prefer v2
        # only within the same parent instruction, never across trades.
        if name == "EvtSwap" and any(
            decoded(other)[0] == "EvtSwap2"
            and other["absolute_path"][:-1] == raw["absolute_path"][:-1]
            for other in instructions.values()
        ):
            return None, "superseded_dbc_event_version"
        buy = p["trade_direction"] == 1
        result = p.get("swap_result") or {}
        amount_in, amount_out = (
            uint(result.get("included_fee_input_amount"), "amount_in"),
            uint(result.get("output_amount"), "amount_out"),
        )
        out.update(
            reserve_semantics="dbc_sqrt_price_after; curve_config_required",
            sqrt_price_after=result.get("next_sqrt_price"),
            amount_semantics="dbc_included_fee_input",
            curve_config=p.get("config"),
        )
        for label, field in (
            ("trading", "trading_fee"),
            ("protocol", "protocol_fee"),
            ("referral", "referral_fee"),
        ):
            if result.get(field) is not None and uint(result[field], field):
                fees.append(
                    {"component": label, "asset": quote if buy else base, "amount": result[field]}
                )
    else:
        return None, "unsupported_or_nonexecution_instruction"
    if not all((base, quote, pool, actor)):
        raise ValueError("trade asset/quote/pool/actor unresolved")
    if base == quote or amount_in == 0 or amount_out == 0:
        raise ValueError("trade assets or amounts invalid")
    out.update(
        kind="VENUE_TRADE",
        side="buy" if buy else "sell",
        sold_asset=quote if buy else base,
        bought_asset=base if buy else quote,
        sold_amount=amount_in,
        bought_amount=amount_out,
        fees=fees,
        reserve_quote_after=reserves[0],
        reserve_base_after=reserves[1],
    )
    if protocol in ("pumpfun", "pump_swap"):
        modern = pump_metadata(raw, p, out, instructions)
        if modern is not None:
            out["modern_pump"] = modern
            if modern["fee_retention"] == "protocol_and_creator; buyback_paid":
                out["amount_semantics"] += "; protocol_creator_fees_retained"
    return out, "accepted"
