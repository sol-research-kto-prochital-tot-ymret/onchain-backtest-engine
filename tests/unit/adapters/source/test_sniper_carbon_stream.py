import importlib.util
from pathlib import Path

import pytest

from backtest.bootstrap.sniper_window import run
from backtest.domain.execution import ExecutionMode
from backtest.plugins.strategies.sniper_window import SniperWindowPolicy


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROOT = Path(__file__).resolve().parents[4]
adapter = load(ROOT / "notebooks/sniper_carbon_day.py", "carbon_day_adapter")
fixtures = load(
    ROOT / "tests/unit/plugins/strategies/test_sniper_window.py", "sniper_stream_fixtures"
)


def trade():
    return {
        "kind": "VENUE_TRADE",
        "protocol": "pumpfun",
        "transaction_success": True,
        "slot": 100,
        "transaction_index": 3,
        "event_index": 1,
        "instruction_path": [2, 1],
        "event_id": "a" * 64,
        "block_time": 12,
        "mint": "TOKEN",
        "signature": "SIG",
        "actor": "TRADER",
        "quote_mint": adapter.SOL.value,
        "side": "buy",
        "sold_amount": 100,
        "bought_amount": 200,
        "reserve_quote_after": 400,
        "reserve_base_after": 500,
        "real_quote_after": 10,
        "real_base_after": 20,
        "fee_lamports": 5000,
        "fee_payer": "TRADER",
        "decoded_payload": {
            "creator": [1] * 32,
            "shareholders": [{"address": [2] * 32, "share_bps": 10000}],
            "fee": 4,
            "creator_fee": 2,
            "fee_basis_points": 95,
            "creator_fee_basis_points": 30,
            "ix_name": "buy_exact_sol_in",
            "timestamp": 12,
        },
    }


def test_compaction_preserves_atomic_amounts_fees_and_recipient_accounts():
    row = adapter.compact(trade())
    assert (row["quote"], row["tokens"], row["protocol_fee"], row["creator_fee"]) == (
        100,
        200,
        4,
        2,
    )
    assert row["payload"]["creator"] == adapter.public_key([1] * 32)
    assert row["payload"]["shareholders"][0]["address"] == adapter.public_key([2] * 32)
    assert row["instruction_path"] == [2, 1]
    assert row["network_fee"] == 5000 and row["fee_payer"] == "TRADER"
    assert row["real_quote"] == 10 and row["virtual_quote"] == 400


def test_compaction_sell_does_not_reverse_quote_and_token_amounts():
    native = trade()
    native.update(side="sell", sold_amount=200, bought_amount=100)
    row = adapter.compact(native)
    assert row["quote"] == 100 and row["tokens"] == 200


def test_failed_transactions_and_missing_timestamps_are_not_admitted():
    native = trade()
    native["transaction_success"] = False
    with pytest.raises(ValueError):
        adapter.compact(native)
    native = trade()
    native["block_time"] = None
    native["decoded_payload"]["timestamp"] = None
    with pytest.raises(ValueError):
        adapter.compact(native)


@pytest.mark.parametrize("organic", [False, True])
@pytest.mark.parametrize("flat", [0, 1])
def test_bounded_reducer_cache_preserves_native_orders_fills_and_ledger(organic, flat):
    policy = SniperWindowPolicy(counterpart_sell_fraction=1, require_creator_flat_before_entry=flat)
    normal = fixtures.MemorySource(organic=organic)
    bounded = fixtures.MemorySource(organic=organic)
    bounded.zero_latency_stream = True
    before = run(normal, policy, ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY)
    after = run(bounded, policy, ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY)
    assert before[0] == after[0]
    assert before[1] == after[1]
    assert before[2].fills == after[2].fills
    assert before[2].ledger == after[2].ledger
