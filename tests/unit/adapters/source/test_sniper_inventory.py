import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backtest.adapters.source.sniper_inventory import enrich_inventory


def evidence_fixture(tmp_path: Path):
    program = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

    def balance(index, owner, amount):
        return {
            "accountIndex": index,
            "mint": "mint",
            "owner": owner,
            "uiTokenAmount": {"amount": str(amount), "decimals": 6},
        }

    document = {
        "result": {
            "slot": 10,
            "transactionIndex": 4,
            "transaction": {
                "signatures": ["signature"],
                "message": {
                    "accountKeys": [{"pubkey": "source"}, {"pubkey": "destination"}],
                    "instructions": [
                        {
                            "programId": program,
                            "parsed": {
                                "type": "transferChecked",
                                "info": {
                                    "source": "source",
                                    "destination": "destination",
                                    "mint": "mint",
                                    "tokenAmount": {"amount": "60", "decimals": 6},
                                },
                            },
                        }
                    ],
                },
            },
            "meta": {
                "err": None,
                "innerInstructions": [],
                "preTokenBalances": [balance(0, "alice", 100)],
                "postTokenBalances": [balance(0, "alice", 40), balance(1, "bob", 60)],
            },
        }
    }
    evidence = tmp_path / "transaction.json"
    evidence.write_text(json.dumps(document))
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    receipt = {
        "status": 200,
        "response_sha256": digest,
        "request": {
            "method": "getTransaction",
            "params": ["signature", {"commitment": "finalized"}],
        },
    }
    evidence.with_suffix(".receipt.json").write_text(json.dumps(receipt))
    movement = {
        "event_uid": "movement",
        "mint": "mint",
        "slot": 10,
        "tx_idx": 4,
        "instruction_path": [0],
        "source_wallet": "alice",
        "destination_wallet": "bob",
        "source_token_account": "source",
        "destination_token_account": "destination",
        "tokens": 60,
        "signature": "signature",
        "evidence_file": evidence.name,
        "evidence_sha256": digest,
    }
    path = tmp_path / "transfers.json"
    path.write_text(json.dumps([movement]))
    source = SimpleNamespace(
        source_hash="base-source",
        times={10: 1},
        birth_by_mint={"mint": {"slot": 10, "payload": {"bonding_curve": "curve"}}},
    )
    return source, path, movement, evidence


def test_transfer_overlay_is_bound_to_verified_transaction_and_source(tmp_path):
    source, path, _, _ = evidence_fixture(tmp_path)
    enrich_inventory(source, path)
    assert source.inventory_base_source_sha256 == "base-source"
    assert source.source_hash != "base-source"
    assert source.inventory_transfers[0]["tokens"] == 60


@pytest.mark.parametrize(
    "change,reason",
    [
        ("amount", "disagrees"),
        ("duplicate", "duplicate"),
        ("bytes", "hash mismatch"),
        ("future", "outside"),
    ],
)
def test_unproven_transfer_repairs_are_refused(tmp_path, change, reason):
    source, path, movement, evidence = evidence_fixture(tmp_path)
    rows = [movement]
    if change == "amount":
        movement["tokens"] = 61
    elif change == "duplicate":
        rows.append(dict(movement))
    elif change == "bytes":
        evidence.write_text(evidence.read_text() + " ")
    elif change == "future":
        movement["slot"] = 11
    path.write_text(json.dumps(rows))
    with pytest.raises(ValueError, match=reason):
        enrich_inventory(source, path)
