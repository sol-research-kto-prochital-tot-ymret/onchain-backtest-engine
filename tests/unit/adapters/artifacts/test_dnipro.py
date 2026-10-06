"""Immutable publication and adversarial artifact reads."""

from pathlib import Path

import pytest

from backtest.adapters.artifacts.dnipro import load, publish, publish_result, require_exact


def test_snapshot_is_verified_and_cannot_be_replaced(tmp_path: Path) -> None:
    destination = tmp_path / "snapshot"
    publish(destination, [{"amount": 1}], [], {"wallet": "fixture"})
    manifest, trades, states = load(destination)
    assert trades == [{"amount": 1}] and states == []
    assert manifest["fidelity"]["exact_source_admission"] is False
    with pytest.raises(ValueError, match="already exists"):
        publish(destination, [], [], {})


def test_tampered_table_and_manifest_are_rejected(tmp_path: Path) -> None:
    destination = tmp_path / "snapshot"
    publish(destination, [], [], {})
    table = destination / "trades.parquet"
    table.write_bytes(table.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        load(destination)
    (destination / "manifest.json").write_text("{}")
    with pytest.raises(ValueError, match="commit marker"):
        load(destination)


def test_symlink_table_and_forged_exact_claim_are_rejected(tmp_path: Path) -> None:
    destination = tmp_path / "snapshot"
    manifest = publish(destination, [], [], {})
    table = destination / "trades.parquet"
    table.rename(tmp_path / "outside.parquet")
    table.symlink_to(tmp_path / "outside.parquet")
    with pytest.raises(ValueError, match="regular files"):
        load(destination)
    manifest["fidelity"]["exact_source_admission"] = True
    with pytest.raises(ValueError, match="exact replay unavailable"):
        require_exact(manifest)


def test_result_publication_is_complete_and_refuses_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    publish_result(path, b'{"complete":true}')
    with pytest.raises(FileExistsError):
        publish_result(path, b"{}")
    assert path.read_bytes() == b'{"complete":true}'
    assert list(tmp_path.iterdir()) == [path]
