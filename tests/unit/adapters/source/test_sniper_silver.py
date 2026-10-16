import copy

import pytest

from backtest.adapters.source.sniper_silver import verify_empty_block


def evidence():
    return {
        "slot": 455328000,
        "request": {
            "method": "getBlock",
            "params": [455328000, {"commitment": "finalized", "transactionDetails": "full"}],
        },
        "response": {
            "result": {
                "transactions": [],
                "blockhash": "example",
                "parentSlot": 455327999,
                "blockTime": 1791651159,
            }
        },
    }


def test_empty_finalized_block_supplies_an_evidenced_clock():
    assert verify_empty_block(evidence()) == (455328000, 1791651159)


@pytest.mark.parametrize(
    "change",
    ["missing_transactions", "has_transactions", "not_full", "not_finalized", "wrong_slot"],
)
def test_missing_transaction_details_cannot_clear_a_coverage_hole(change):
    value = copy.deepcopy(evidence())
    if change == "missing_transactions":
        del value["response"]["result"]["transactions"]
    elif change == "has_transactions":
        value["response"]["result"]["transactions"] = [{"transaction": "data"}]
    elif change == "not_full":
        value["request"]["params"][1]["transactionDetails"] = "none"
    elif change == "not_finalized":
        value["request"]["params"][1]["commitment"] = "confirmed"
    else:
        value["slot"] += 1
    with pytest.raises(ValueError):
        verify_empty_block(value)
