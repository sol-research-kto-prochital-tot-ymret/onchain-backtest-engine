"""Minimal worker environments: credentials are explicit acquisition capabilities."""

from collections.abc import Mapping

_RUNTIME_KEYS = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
        "WINDIR",
        "VIRTUAL_ENV",
        # Source-checkout workers need this; the launcher controls its value.
        "PYTHONPATH",
    }
)


def worker_environment(
    parent: Mapping[str, str], *, permitted_secret_refs: tuple[str, ...] = ()
) -> dict[str, str]:
    """Never forward arbitrary API keys, SSH agents, or Python startup injection."""
    selected = {name: value for name, value in parent.items() if name in _RUNTIME_KEYS}
    for name in permitted_secret_refs:
        if name in parent:
            selected[name] = parent[name]
    return selected
