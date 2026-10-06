"""Causal quote placement, accounting, and retained inventory regressions."""

from collections import defaultdict

from backtest.plugins.strategies.leader_slot_copy import LeaderSlotPolicy, replay

MINT = "CR4cuWRKzTULkBoYhnSGLT59VzUs8Yn9cbTm418Kpump"


def fixtures() -> tuple[list[dict], list[dict]]:
    common = {
        "mint": MINT,
        "user": "leader",
        "fee_payer": "leader",
        "absolute_path": [1],
        "transaction_index": 2,
        "token_amount": 100,
        "sol_amount": 100_000_000,
        "protocol_fee": 950_000,
        "creator_fee": 300_000,
        "protocol_fee_bps": 95,
        "creator_fee_bps": 30,
        "fee_lamports": 5000,
        "mayhem_mode": False,
        "cashback": 0,
        "cashback_fee_bps": 0,
    }
    trades = [
        {**common, "slot": 100, "block_time": 1000, "is_buy": True, "signature": "buy"},
        {**common, "slot": 110, "block_time": 1005, "is_buy": False, "signature": "sell"},
    ]
    reserve = {
        "mint": MINT,
        "last_transaction_index": 2,
        "last_absolute_path": [1],
        "virtual_sol_reserves": 31_000_000_000,
        "virtual_token_reserves": 1_000_000_000_000_000,
        "real_sol_reserves": 1_000_000_000,
        "real_token_reserves": 700_000_000_000_000,
    }
    states = [
        {**reserve, "slot": 100, "block_time": 1000},
        {**reserve, "slot": 110, "block_time": 1005},
    ]
    return trades, states


def test_copy_matches_gross_size_and_exits_after_leader_signal() -> None:
    trades, states = fixtures()
    summary, positions, ledger = replay(trades, states, LeaderSlotPolicy(999, 1001))
    assert positions[0]["status"] == "CLOSED"
    assert positions[0]["assumed_copy_entry_slot"] == 101
    assert positions[0]["assumed_copy_exit_slot"] == 111
    assert positions[0]["entry_cost_lamports"] <= 101_255_000
    assert summary["cash_delta_lamports"] == summary["closed_trade_pnl_lamports"]
    for transaction in ledger:
        totals = defaultdict(int)
        for posting in transaction["postings"]:
            totals[posting["asset"]] += posting["amount_atomic"]
        assert set(totals.values()) == {0}


def test_rejected_exit_keeps_inventory_and_separates_cashflow_from_pnl() -> None:
    trades, states = fixtures()
    states[1]["real_sol_reserves"] = 0
    summary, positions, ledger = replay(trades, states, LeaderSlotPolicy(999, 1001))
    assert positions[0]["status"] == "OPEN"
    assert positions[0]["remaining_tokens_atomic"] > 0
    assert positions[0]["closed_pnl_lamports"] is None
    assert positions[0]["exit_failure"] == "INSUFFICIENT_REAL_SOL_RESERVES"
    assert len(ledger) == 1
    assert summary["closed_trade_pnl_lamports"] == 0
    assert summary["cash_delta_lamports"] == -summary["open_position_cost_basis_lamports"]


def test_unavailable_capital_and_missing_states_do_not_create_fills() -> None:
    trades, states = fixtures()
    summary, positions, ledger = replay(trades, states, LeaderSlotPolicy(999, 1001, 1))
    assert positions[0]["status"] == "REJECTED_ENTRY"
    assert not ledger and summary["cash_delta_lamports"] == 0
    summary, positions, ledger = replay(trades, [], LeaderSlotPolicy(999, 1001))
    assert positions[0]["status"] == "EXCLUDED_MISSING_STATE"
    assert not ledger and summary["cash_delta_lamports"] == 0


def test_partial_leader_exit_is_explicitly_excluded() -> None:
    trades, states = fixtures()
    trades[1]["token_amount"] = 99
    summary, positions, ledger = replay(trades, states, LeaderSlotPolicy(999, 1001))
    assert summary["selection_exclusions"] == {"not_full_leader_exit": 1}
    assert not positions and not ledger


def test_inconsistent_observed_fees_are_excluded() -> None:
    trades, states = fixtures()
    trades[0]["protocol_fee"] += 1
    summary, positions, ledger = replay(trades, states, LeaderSlotPolicy(999, 1001))
    assert summary["selection_exclusions"] == {"fee_amount_does_not_match_model": 1}
    assert not positions and not ledger


def test_sponsored_network_fee_is_not_charged_to_leader_pnl() -> None:
    trades, states = fixtures()
    _, baseline, _ = replay(trades, states, LeaderSlotPolicy(999, 1001))
    trades[0]["fee_payer"] = "sponsor"
    _, sponsored, _ = replay(trades, states, LeaderSlotPolicy(999, 1001))
    assert (
        sponsored[0]["leader_observed_pnl_lamports"]
        == baseline[0]["leader_observed_pnl_lamports"] + 5000
    )


def test_state_from_an_earlier_instruction_cannot_price_the_copy() -> None:
    import pytest

    trades, states = fixtures()
    trades[0]["absolute_path"] = [2]
    with pytest.raises(ValueError, match="predates"):
        replay(trades, states, LeaderSlotPolicy(999, 1001))
