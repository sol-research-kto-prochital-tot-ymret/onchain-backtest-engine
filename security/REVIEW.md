# Security review: local Dnipro integration

Review date: 2026-10-01. Upstream source:
<https://github.com/happyer29/onchain-backtest-engine>, pinned commit
`7f9c6229e0e2443f45dd109cc6c3370bd2ec5b10`.
Integration branch: `codex/dnipro-integration`. This report records the source
review and validation performed before publication of the research fork.

## Drainer and code execution review

No wallet connection, private-key/seed import, transaction signing, transaction
submission, or drainer path was found in the reviewed application source,
frontend source, build declarations, and installer scripts. References to
"signing wallets" describe historical public actors and simulated accounts.
The new CLI accepts only public wallet addresses and a read-only database
credential. Offline replay performs no network request in the real-data test.

The review covered Python modules, shell/build entrypoints, dependency sources,
frontend request code, subprocess launching, API access controls, artifact
loading, and SQL construction. No application eval/exec, network-loaded Python,
pickle/marshal deserialization, or shell-interpolated worker command was found.
Workers use fixed argument vectors and `shell=False`. The existing local API
uses loopback/Host/Origin/session/CSRF controls. Frontend source was rebuilt and
its shipped static assets matched the upstream checked-in bytes.

This is source review and automated testing, not a formal audit or guarantee of
absence of malicious behavior. Installed binary wheels/native libraries were
not independently rebuilt or exhaustively reverse engineered. Future upstream
changes or dependency updates require a fresh review. This finding does not
extend to unrelated repositories in our workspace.

## Vulnerabilities found and patched

| Locked package | Original | Patched | Relevant advisories |
|---|---:|---:|---|
| urllib3 | 2.7.0 | 2.8.0 | GHSA-8988-9cw3-xx77, GHSA-gh4c-6fx4-qh6g, GHSA-vxq7-64xx-v4gw |
| brace-expansion | 2.1.4 | 2.1.7 | GHSA-6j4f-fj2g-mc7p, GHSA-qhr7-859c-m2p7, GHSA-q2hr-2g5m-vwhr |

The urllib3 issues affect HTTPS proxy TLS configuration and response parsing;
the brace-expansion issues are denial-of-service risks from pathological input.
The latter is in the frontend tool dependency tree. The original scan found
six advisory entries across two packages, not six independent application
exploits. The Python dependency floor and npm override keep these fixes pinned.

All 348 Python/npm lockfile entries were checked against OSV. The patched scan
reported **zero known advisory matches**, and npm audit reported zero findings.
The 49 original third-party Python versions were checked for actual PyPI
availability before installation. npm installation used `--ignore-scripts`;
no unreviewed dependency lifecycle scripts were run. Lockfile registries are
PyPI and npm, and Git hooks/submodules were disabled during cloning.

Evidence:

- [Original advisory scan](osv-lock-audit.json), [full advisory records](osv-advisories.json).
- [Patched OSV scan](osv-lock-audit-patched.json), [patched npm audit](npm-audit-patched.json).
- [PyPI version availability](dependency-availability.json).
- [urllib3 proxy advisory](https://github.com/advisories/GHSA-8988-9cw3-xx77).
- [brace-expansion advisory](https://github.com/advisories/GHSA-6j4f-fj2g-mc7p).

Recheck both ecosystem locks without installing their packages:

```bash
.venv/bin/python scripts/audit-dependency-locks.py --out security/osv-recheck.json
```

## Credential and data hardening

Upstream workers previously inherited the parent environment. The patched
launcher passes a small runtime allowlist and explicit source credentials only
to acquisition jobs. Unrelated API keys, SSH agent references, and Python
startup configuration are removed, with regression coverage. Workers still run
as ordinary processes under the OS user: this change is not an OS/filesystem
sandbox, and it does not prevent that user from reading accessible credential
files. PATH/PYTHONPATH and the installed environment must remain trusted.

Carbon export uses fixed SQL templates and typed address/range parameters,
read-only settings, query/resource bounds, and TLS verification for remote
connections. Database errors are sanitized rather than printing connection
credentials. Use a SELECT-only database account; query settings do not replace
server grants. The production canary used the server's existing client config
for SELECT only and did not copy its credentials into this repository.
The validated `export-server` command uses that existing server configuration
over SSH with explicit read-only/resource flags and strict known-host checking.
It individually shell-quotes remote arguments and sends SQL over stdin. The
underlying collector account is write-capable, so its read-only restriction is
per request; there are no signing keys or local database passwords involved.
The existing research account authenticates over HTTP, but its `readonly=1`
profile rejects changes to query caps. The optional HTTP exporter fails closed
for that profile. The server export succeeds without weakening those settings
or modifying database accounts. This follows the distinction documented in
[ClickHouse permissions](https://clickhouse.com/docs/concepts/features/configuration/settings/permissions-for-queries)
and [setting constraints](https://clickhouse.com/docs/concepts/features/configuration/settings/constraints-on-settings).

Snapshot loading rejects changed hashes, missing commit markers, nonregular
tables, unexpected schemas, and oversized compressed/uncompressed payloads.
Results are published atomically without overwriting previous outputs. JSON
rows are data, not code. Local paths supplied explicitly to the CLI are trusted
operator inputs; this CLI is not a multi-tenant file-upload endpoint.

## Static analysis triage

[Bandit patched scan](bandit-patched.json): zero HIGH, 16 MEDIUM, 66 LOW findings.
The scan is deliberately retained without suppressions. The 15 medium B608
warnings concern SQL construction: queue placeholders are generated from enum
counts and values are bound; Arrow/research table and column operands are
private constants; ClickHouse identifiers are validated/quoted and all user
values are typed parameters. The Carbon templates interpolate only private
field paths. Manual review found no user-controlled SQL text in these paths.

One medium B310 warning is the audit helper's `urlopen` of the fixed HTTPS OSV
endpoint; no caller-supplied URL is accepted. Low findings include fixed worker
subprocess use, pseudorandom research fixtures, and hardcoded protocol labels.
These are reviewed scanner findings, not a claim that the whole application
has zero possible vulnerabilities. Original and patched scanner counts differ
slightly because their reviewed directory scope and the added helper differ.

## Validation

The full Python suite passed 1,642 tests, with one opt-in external ClickHouse
test skipped and three performance tests deselected; its measured coverage was
81.25%. See [test log](python-tests.txt) and [JUnit results](python-tests.xml).
Final adapter changes, including the live timestamp alias fix and SSH transport,
were tested separately in [Dnipro test results](dnipro-tests.txt).
All 35 final adapter/security/real-data regression tests passed.
The frontend passed 79 Vitest and 20 Node tests and built successfully:
[frontend tests](frontend-tests.txt), [build](frontend-build.txt).
Ruff, strict mypy across 328 modules, and all five import-layer contracts passed.
The saved Nya day has 4,141 source trade events;
two offline replays of its selected 1,947 positions produced identical output
and reconciled the closed-PnL/open-inventory ledger.
The actual CLI exported one trader event and its end-of-slot reserves from the
production database over SSH: [canary](clickhouse-ssh-export.json). This narrow
canary proves the transport and source mapping, not completeness of a full day.
`--require-exact` exited with code 2 and published no result:
[exact-admission refusal](exact-admission-refusal.json).

The detailed [integration contract](../docs/dnipro-integration.md) lists fidelity
limits: this first adapter is Pump.fun observational research, not a proven
exact execution backtest, not full-wallet PnL, and not support for every DEX.
