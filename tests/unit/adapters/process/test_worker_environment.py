"""Regression for credentials accidentally inherited by backtest workers."""

from backtest.adapters.process.environment import worker_environment


def test_execution_receives_runtime_settings_without_unrelated_credentials() -> None:
    parent = {
        "PATH": "/usr/bin",
        "PYTHONPATH": "/checked/source",
        "LC_ALL": "C",
        "SQD_API_KEY": "must-not-leak",
        "AWS_SECRET_ACCESS_KEY": "must-not-leak",
        "SSH_AUTH_SOCK": "/agent",
        "PYTHONSTARTUP": "/injection.py",
        "BACKTEST_INDEXER_PASSWORD": "source-secret",
    }
    assert worker_environment(parent) == {
        "PATH": "/usr/bin",
        "PYTHONPATH": "/checked/source",
        "LC_ALL": "C",
    }


def test_acquisition_receives_only_explicit_source_secret() -> None:
    assert worker_environment(
        {"PATH": "/usr/bin", "CH_PASSWORD": "allowed", "SQD_API_KEY": "blocked"},
        permitted_secret_refs=("CH_PASSWORD",),
    ) == {"PATH": "/usr/bin", "CH_PASSWORD": "allowed"}
