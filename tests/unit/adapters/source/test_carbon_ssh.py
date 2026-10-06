"""SSH query transport prevents option/shell injection and credential leakage."""

import shlex
from types import SimpleNamespace

import pytest

from backtest.adapters.source.clickhouse.carbon import LEADER_SQL, QUERY_SETTINGS
from backtest.adapters.source.clickhouse.carbon_ssh import SshCarbonClient


@pytest.mark.parametrize("target", ["-oProxyCommand=evil", "host; touch /tmp/evil", "$(evil)"])
def test_target_cannot_inject_ssh_options_or_commands(target: str) -> None:
    with pytest.raises(ValueError):
        SshCarbonClient(target)


def test_sql_is_stdin_and_remote_parameters_are_individually_quoted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backtest.adapters.source.clickhouse.carbon_ssh as module

    def run(command: list, **kwargs: object) -> object:
        assert kwargs["input"] == LEADER_SQL
        assert "StrictHostKeyChecking=yes" in command
        remote = shlex.split(command[-1])
        assert "--readonly=1" in remote
        assert "--max_execution_time=120" in remote
        assert "--param_wallet=$(touch /tmp/evil)" in remote
        return SimpleNamespace(returncode=0, stdout='{"raw_json":"data"}\n')

    monkeypatch.setattr(module.subprocess, "run", run)
    rows = SshCarbonClient("root@192.0.2.10").query(
        LEADER_SQL, parameters={"wallet": "$(touch /tmp/evil)"}, settings=QUERY_SETTINGS
    )
    assert rows.result_rows == [["data"]]


def test_remote_errors_do_not_expose_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    import backtest.adapters.source.clickhouse.carbon_ssh as module

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="password=private-sentinel"
        ),
    )
    with pytest.raises(ValueError) as error:
        SshCarbonClient("root@192.0.2.10").query(LEADER_SQL, parameters={}, settings=QUERY_SETTINGS)
    assert "private-sentinel" not in str(error.value)


def test_write_queries_and_missing_limits_are_refused() -> None:
    client = SshCarbonClient("root@192.0.2.10")
    with pytest.raises(ValueError):
        client.query("DROP TABLE solana.instructions", parameters={}, settings=QUERY_SETTINGS)
    with pytest.raises(ValueError):
        client.query(LEADER_SQL, parameters={}, settings={})
