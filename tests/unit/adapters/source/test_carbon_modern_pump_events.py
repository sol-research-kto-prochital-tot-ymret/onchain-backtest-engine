from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from backtest.adapters.source.carbon_cache.normalize import (
    SOL,
    digest,
    normalize,
    public_key,
    signed_i128,
)

MINT = public_key([1] * 32)
USER = public_key([2] * 32)
POOL = public_key([3] * 32)
QUOTE = public_key([4] * 32)


def raw(
    name: str, payload: dict[str, Any], *, protocol: str = "pumpfun", path: tuple[int, ...] = (0,)
) -> dict[str, Any]:
    return {
        "slot": 100,
        "transaction_index": 0,
        "signature": "sig",
        "absolute_path": list(path),
        "decoder": protocol,
        "success": True,
        "source_priority": 1,
        "fee_payer": USER,
        "fee_lamports": 5000,
        "decoded": {"type": name, "data": {"data": payload}},
    }


def curve(**updates: Any) -> dict[str, Any]:
    return {
        "mint": MINT,
        "user": USER,
        "is_buy": True,
        "token_amount": 10,
        "sol_amount": 20,
        "virtual_sol_reserves": 100,
        "virtual_token_reserves": 200,
        "real_sol_reserves": 20,
        "real_token_reserves": 30,
        "fee": 2,
        "creator_fee": 1,
    } | updates


def event(
    value: dict[str, Any], parents: dict[tuple[int, ...], dict[str, Any]] | None = None
) -> dict[str, Any]:
    out, reason = normalize(value, parents or {})
    assert reason == "accepted"
    assert out is not None
    return out


def test_legacy_output_is_unchanged() -> None:
    value = event(raw("TradeEvent", curve()))
    assert "modern_pump" not in value
    assert value["reserve_quote_after"] == 100
    assert value["quote_mint"] == SOL
    assert value["amount_semantics"] == "pump_quote_excludes_external_fees"
    # Golden digest includes every field, not only selected legacy outputs.
    assert digest(value) == "dc3dee30f145846573db2698fb2e56b05c7bf302552ceae6d23bacb6da27afc9"


def test_curve_v3_native_quote_and_retained_fee_metadata() -> None:
    value = event(
        raw(
            "TradeEvent",
            curve(
                ix_name="buy_v3",
                quote_mint=QUOTE,
                quote_amount=40,
                virtual_quote_reserves=140,
                real_quote_reserves=60,
                holder_rewards=1,
                creator_fee_unclaimed=7,
            ),
        )
    )
    modern = value["modern_pump"]
    assert value["sold_asset"] == QUOTE
    assert value["sold_amount"] == 40
    assert modern["instruction_version"] == "v3"
    assert modern["creator_beneficiary"] == "holder_rewards"
    assert modern["state_after"]["real_quote_reserves"] == 60
    assert "protocol_and_creator" in modern["fee_retention"]
    assert value["fees"] == [
        {"component": "protocol", "asset": QUOTE, "amount": 2},
        {"component": "creator", "asset": QUOTE, "amount": 1},
    ]


@pytest.mark.parametrize("offset", [-100, "-100", "0"])
def test_pool_signed_after_offset_never_added_to_before_reserves(offset: int | str) -> None:
    value = event(
        raw(
            "BuyEvent",
            {
                "pool": POOL,
                "base_mint": MINT,
                "quote_mint": QUOTE,
                "user": USER,
                "base_amount_out": 10,
                "quote_amount_in": 20,
                "ix_name": "buy_v2",
                "pool_base_token_reserves": 1000,
                "pool_quote_token_reserves": 2000,
                "virtual_quote_reserves": offset,
            },
            protocol="pump_swap",
        )
    )
    assert value["reserve_quote_after"] is None
    assert value["quote_reserve_before"] == 2000
    modern = value["modern_pump"]
    assert modern["signed_virtual_quote_offset_after"] == int(offset)
    assert modern["state_after"] is None
    assert "same_phase_after_inventory" in modern["missing_state"]


def test_missing_pool_offset_is_unknown_not_zero() -> None:
    value = event(
        raw(
            "SellEvent",
            {
                "pool": POOL,
                "base_mint": MINT,
                "quote_mint": QUOTE,
                "user": USER,
                "base_amount_in": 10,
                "quote_amount_out": 20,
                "ix_name": "sell_v2",
            },
            protocol="pump_swap",
        )
    )
    assert value["modern_pump"]["signed_virtual_quote_offset_after"] is None
    assert "signed_virtual_quote_offset" in value["modern_pump"]["missing_state"]


@pytest.mark.parametrize("value", [True, 1.2, "1.0", "+2", "", 1 << 127, -(1 << 127) - 1])
def test_invalid_signed_offsets_fail_closed(value: Any) -> None:
    with pytest.raises(ValueError):
        signed_i128(value)


@pytest.mark.parametrize(
    "name,protocol",
    [
        ("SweepBondingCurveFeeEvent", "pumpfun"),
        ("SweepPoolFeeEvent", "pump_swap"),
    ],
)
def test_sweeps_are_price_neutral_audit_not_trades(name: str, protocol: str) -> None:
    value = raw(name, {"amount": 30, "bucket": 0}, protocol=protocol)
    before = deepcopy(value)
    result, reason = normalize(value, {})
    assert result is None
    assert reason == "fee_sweep_price_neutral; retained_bucket_payout_not_trade"
    assert value == before


def test_multihop_router_limits_not_execution_but_children_are() -> None:
    router = raw("MultiHopSwap", {"amount_in": 100, "min_amount_out": 1}, protocol="pump_swap")
    result, reason = normalize(router, {})
    assert result is None
    assert "not_an_additional_pool_trade" in reason
    child = raw("TradeEvent", curve(quote_mint=QUOTE), path=(0, 1, 0))
    value = event(child, {(0,): router})
    assert value["sold_amount"] == 20
    assert value["modern_pump"]["route_instruction_path"] == [0]
    assert value["modern_pump"]["instruction_version"] is None
    assert value["instruction_committed"] is None


def test_create_holder_reward_depth_preserved_and_explicit() -> None:
    payload = {
        "mint": MINT,
        "user": USER,
        "creator": POOL,
        "quote_mint": QUOTE,
        "is_holder_reward": True,
        "creator_fee_bps": 30,
        "depth": 2,
    }
    value = event(raw("CreateEvent", payload))
    assert value["decoded_payload"] == payload
    assert value["modern_pump"]["creator_beneficiary"] == "holder_rewards"
    assert value["modern_pump"]["depth"] == 2
    assert value["developer"] == POOL


def test_postcomplete_pool_part_has_physical_state_and_is_not_curve_part() -> None:
    payload = {
        "mint": MINT,
        "user": USER,
        "quote_mint": QUOTE,
        "base_out": 7,
        "quote_in": 9,
        "pool_base_reserves_before": 100,
        "pool_quote_reserves_before": 200,
        "pool_base_reserves_after": 93,
        "pool_quote_reserves_after": 209,
        "fee": 2,
        "creator_fee": 1,
        "buyback_fee": 1,
    }
    value = event(raw("PostCompleteBuyEvent", payload))
    assert value["bought_amount"] == 7
    assert value["sold_amount"] == 9
    assert value["reserve_quote_after"] is None
    state = value["modern_pump"]["synthetic_migration"]
    assert state["pool_base_reserves_after"] == 93
    assert state["pool_quote_reserves_after"] == 209
    assert state["destination_pool_exists"] is False


def test_instruction_limits_and_failed_transactions_never_execute() -> None:
    assert normalize(raw("BuyV3", {"amount": 10, "max_sol_cost": 100}), {})[0] is None
    with pytest.raises(ValueError, match="quote mint unresolved"):
        normalize(raw("TradeEvent", curve(ix_name="buy_v3")), {})
    failed = raw("TradeEvent", curve(ix_name="buy_v3"))
    failed["success"] = False
    assert normalize(failed, {}) == (None, "failed_transaction")
