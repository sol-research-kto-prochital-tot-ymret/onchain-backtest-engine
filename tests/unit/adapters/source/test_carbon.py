"""Security and correctness checks for the Carbon source boundary."""

import json
from types import SimpleNamespace

import pytest

from backtest.adapters.source.clickhouse.carbon import (
    LEADER_SQL,
    STATE_SQL,
    deduplicate,
    fetch_leader,
    fetch_states,
    normalize_trade,
    public_key,
    uint,
)

WALLET = "nya666pQkP3PzWxi7JngU3rRMHuc7zbLK8c8wxQ4qpT"
MINT = "CR4cuWRKzTULkBoYhnSGLT59VzUs8Yn9cbTm418Kpump"


def trade() -> dict:
    return {
        "slot": 100,
        "transaction_index": 2,
        "absolute_path": [3, 1],
        "signature": "fixture-signature",
        "block_time": 1000,
        "fee_payer": WALLET,
        "fee_lamports": 5000,
        "mint": MINT,
        "source_priority": 1,
        "is_buy": True,
        "mayhem_mode": False,
        "sol_amount": 100_000_000,
        "token_amount": 100,
        "protocol_fee": 950_000,
        "creator_fee": 300_000,
        "cashback": 0,
        "protocol_fee_bps": 95,
        "creator_fee_bps": 30,
        "cashback_fee_bps": 0,
        "virtual_sol_reserves": 30_100_000_000,
        "virtual_token_reserves": 1_000_000_000_000_000,
        "real_sol_reserves": 100_000_000,
        "real_token_reserves": 700_000_000_000_000,
    }


def test_legacy_attribution_is_explicit_and_source_priority_wins() -> None:
    one = normalize_trade(trade(), WALLET)
    two = {**one, "source_priority": 2, "sol_amount": 200_000_000}
    assert one["actor_attribution"] == "fee_payer_proxy"
    assert deduplicate([one, one, two])[0]["sol_amount"] == 200_000_000


def test_conflicting_authoritative_events_are_rejected() -> None:
    one = normalize_trade(trade(), WALLET)
    with pytest.raises(ValueError, match="conflicting"):
        deduplicate([one, {**one, "token_amount": 101}])


@pytest.mark.parametrize("value", [True, 1.0, -1, "-1", "nan", 1 << 64])
def test_atomic_amounts_do_not_accept_floats_booleans_or_overflow(value: object) -> None:
    with pytest.raises(ValueError):
        uint(value, "amount")


def test_unknown_launch_mode_is_not_silently_normal() -> None:
    row = trade()
    del row["mayhem_mode"]
    with pytest.raises(ValueError, match="mayhem_mode"):
        normalize_trade(row)


def test_raw_event_actor_is_not_replaced_by_fee_payer() -> None:
    event = trade()
    raw = {
        "decoder": "pumpfun",
        "success": True,
        "fee_payer": WALLET,
        "decoded": {"data": {"data": {"TradeEvent": event}}},
    }
    with pytest.raises(ValueError, match="public key"):
        normalize_trade(raw, WALLET)


def test_zero_byte_public_key_roundtrips_and_invalid_length_is_rejected() -> None:
    assert public_key([0] * 32) == "1" * 32
    with pytest.raises(ValueError):
        public_key([1] * 31)


def test_sql_is_read_only_parameterized_and_truncation_is_detected() -> None:
    class Client:
        def query(self, query: str, *, parameters: dict, settings: dict) -> object:
            assert query == LEADER_SQL
            assert WALLET not in query
            assert parameters["wallet"] == WALLET
            assert settings["readonly"] == 1
            return SimpleNamespace(result_rows=[("unused",), ("unused",)])

    with pytest.raises(ValueError, match="row limit"):
        fetch_leader(Client(), WALLET, 100, 101, maximum_rows=1)
    with pytest.raises(ValueError):
        fetch_leader(Client(), "'; DROP TABLE solana.instructions; --", 100, 101)


def test_state_query_does_not_alias_aggregate_over_filter_column() -> None:
    class Client:
        def query(self, query: str, *, parameters: dict, settings: dict) -> object:
            assert query == STATE_SQL
            assert "AS block_time," not in query
            assert parameters["pairs"] == [(100, MINT)]
            assert settings["readonly"] == 1
            return SimpleNamespace(
                column_names=[
                    "slot",
                    "mint",
                    "state_block_time",
                    "last_transaction_index",
                    "last_absolute_path",
                    "virtual_sol_reserves",
                    "virtual_token_reserves",
                    "real_sol_reserves",
                    "real_token_reserves",
                ],
                result_rows=[
                    (
                        100,
                        MINT,
                        1000,
                        2,
                        [3, 1],
                        30_100_000_000,
                        1_000_000_000_000_000,
                        100_000_000,
                        700_000_000_000_000,
                    )
                ],
            )

    states = fetch_states(Client(), [(100, MINT)])
    assert states[0]["block_time"] == 1000


def test_valid_raw_trade_preserves_event_user() -> None:
    row = trade()
    event = {**row, "user": WALLET}
    raw = {
        **row,
        "decoder": "pumpfun",
        "success": True,
        "decoded": {"data": {"data": {"TradeEvent": event}}},
    }
    normalized = normalize_trade({"raw_json": json.dumps(raw)}, WALLET)
    assert normalized["actor_attribution"] == "trade_event_user"
    assert normalized["token_amount"] == 100


def test_carbon_fee_names_are_mapped_without_counting_buyback_twice() -> None:
    row = trade()
    event = {**row, "user": WALLET}
    aliases = {
        "protocol_fee": "fee",
        "protocol_fee_bps": "fee_basis_points",
        "creator_fee_bps": "creator_fee_basis_points",
        "cashback_fee_bps": "cashback_fee_basis_points",
    }
    for logical, physical in aliases.items():
        event[physical] = event.pop(logical)
    event["buyback_fee"] = 475_000
    raw = {
        **row,
        "decoder": "pumpfun",
        "success": True,
        "decoded": {"data": {"data": {"TradeEvent": event}}},
    }
    result = normalize_trade(raw, WALLET)
    assert result["protocol_fee"] == 950_000
    assert result["protocol_fee_bps"] == 95
