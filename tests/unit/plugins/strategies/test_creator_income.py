from __future__ import annotations

from dataclasses import replace

import pytest
from tests.unit.plugins.strategies.test_sniper_window import MemorySource

from backtest.bootstrap.sniper_window import run
from backtest.plugins.strategies.creator_income import CreatorFeeIncome, allocate_creator_fee
from backtest.plugins.strategies.sniper_window import SniperWindowPolicy


def row(fee=7, **payload):
    return {"mint": "TOKEN", "creator_fee": fee, "payload": payload}


def test_single_event_recipient_gets_income_once_without_birth_guess():
    ledger = CreatorFeeIncome()
    trade = row(7, creator="new-recipient")
    ledger.observe("event-1", trade)
    ledger.observe("event-1", trade)
    assert ledger.income("TOKEN", "new-recipient") == 7
    assert ledger.income("TOKEN", "birth-creator") == 0
    ledger.verify()


def test_shared_fees_conserve_and_do_not_credit_config_pda():
    trade = row(
        7,
        creator="sharing-config-pda",
        shareholders=[
            {"address": "a", "share_bps": 3333},
            {"address": "b", "share_bps": 6667},
        ],
    )
    allocation = allocate_creator_fee(trade)
    assert allocation.credits == (("a", 2), ("b", 4))
    assert allocation.unallocated_lamports == 1
    ledger = CreatorFeeIncome()
    ledger.observe("event-1", trade)
    assert ledger.income("TOKEN", "sharing-config-pda") == 0
    ledger.verify()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"creator": "11111111111111111111111111111111"},
        {"creator": "fallback", "shareholders": [{"address": "a", "share_bps": 5000}]},
        {
            "shareholders": [
                {"address": "a", "share_bps": 5000},
                {"address": "a", "share_bps": 5000},
            ]
        },
    ],
)
def test_unknown_recipient_is_unallocated_not_assigned_to_launch_creator(payload):
    allocation = allocate_creator_fee(row(7, **payload))
    assert allocation.credits == ()
    assert allocation.unallocated_lamports == 7


def credited_source(organic=True):
    source = MemorySource(organic=organic)
    for trade in source.trade_rows:
        trade["payload"]["creator"] = "sniper"
    return source


def test_native_anchor_cost_subtracts_only_fees_observed_by_signal():
    source = credited_source()
    _, rows, _ = run(source, SniperWindowPolicy())
    position = rows[0]
    assert position["sniper_wallet"] == "sniper"
    accrued_by_signal = sum(
        t["creator_fee"]
        for t in source.trade_rows
        if int(t["signature"]) <= position["signal_slot"]
    )
    assert position["sniper_creator_fee_income_lamports"] == accrued_by_signal
    assert position["sniper_cost_lamports"] == (
        position["sniper_trading_cash_cost_lamports"] - accrued_by_signal
    )
    _, no_future_buy, _ = run(credited_source(organic=False), SniperWindowPolicy())
    assert position["budget_lamports"] == no_future_buy[0]["budget_lamports"]
    assert (
        position["sniper_creator_fee_income_lamports"]
        == no_future_buy[0]["sniper_creator_fee_income_lamports"]
    )


def test_disabled_credit_preserves_buy_only_anchor_cost_but_reports_income():
    _, rows, _ = run(credited_source(), replace(SniperWindowPolicy(), creator_fee_credit_enabled=0))
    assert rows[0]["sniper_creator_fee_income_lamports"] > 0
    assert rows[0]["sniper_cost_lamports"] == rows[0]["sniper_trading_cash_cost_lamports"]


def test_fee_income_is_not_deposited_to_our_simulated_trading_wallet():
    source = MemorySource()
    for trade in source.trade_rows:
        trade["payload"]["creator"] = "creator"
    with_credit, _, _ = run(source, SniperWindowPolicy())
    no_credit, _, _ = run(source, replace(SniperWindowPolicy(), creator_fee_credit_enabled=0))
    assert with_credit["observed_creator_fee_accrual_lamports"] > 0
    assert with_credit["realized_net_pnl_lamports"] == no_credit["realized_net_pnl_lamports"]
    assert with_credit["cash_reconciliation_verified"]
