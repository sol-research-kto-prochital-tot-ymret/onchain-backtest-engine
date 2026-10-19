# Dnipro Carbon/ClickHouse integration

This local checkout is based on `happyer29/onchain-backtest-engine` commit
`7f9c6229e0e2443f45dd109cc6c3370bd2ec5b10`, branch `codex/dnipro-integration`.
The first working integration reads our existing `solana.instructions` projection
and replays frozen Pump.fun observations with the upstream Pump quote model and
double-entry portfolio ledger. It requires no wallet key or transaction signing.

## Run from the existing collector

From `solana-live-indexer`, use `./research/backtest.sh --help`. The wrapper calls
the sibling engine's existing virtual environment and does not install anything.
The engine also exposes `.venv/bin/dnipro-backtest` directly.

Import the real Nya export already in our workspace:

```bash
cd ../onchain-backtest-engine
.venv/bin/dnipro-backtest import-snapshot \
  --trades ../solana-live-indexer/reports/2026-09-30-nya-next-block-backtest/nya_trades_24h.jsonl \
  --states ../solana-live-indexer/reports/2026-09-30-nya-next-block-backtest/market_slot_states.jsonl \
  --wallet nya666pQkP3PzWxi7JngU3rRMHuc7zbLK8c8wxQ4qpT \
  --out var/dnipro/new-nya-snapshot

.venv/bin/dnipro-backtest verify --snapshot var/dnipro/new-nya-snapshot

.venv/bin/dnipro-backtest copy-slots \
  --snapshot var/dnipro/new-nya-snapshot \
  --start-time 1790631266 --end-time 1790717666 \
  --initial-sol-lamports 1000000000000 \
  --out var/dnipro/new-nya-result.json
```

Outputs refuse to replace existing files. Choose a new destination for each run.
Snapshots contain Parquet tables, source hashes, a manifest, and a synced commit
marker. Replay validates hashes and row counts before loading, then runs offline.
Results record the snapshot hash, policy, engine source hash, Python/PyArrow
versions, positions, and conserving ledger entries. A crash cannot publish a
partial result as a complete one. Hashes detect accidental modification; they
are not a signature proving who created a snapshot.

## Export new data

The server export works with our existing SSH access and server-side ClickHouse
client configuration. Database credentials stay on the server:

```bash
.venv/bin/dnipro-backtest export-server \
  --ssh-target root@collector.example.org \
  --wallet nya666pQkP3PzWxi7JngU3rRMHuc7zbLK8c8wxQ4qpT \
  --start-slot 451480800 --end-slot 451480900 \
  --out var/dnipro/new-server-snapshot
```

Replace `collector.example.org` with your server hostname or IP address. The SSH
target is required; the adapter never selects a production server implicitly.

Both queries send `readonly=1` plus fixed resource limits to `clickhouse-client`,
with typed parameters and SQL over stdin. SSH requires an already trusted host
key and batch authentication. The server-side collector credential is capable
of writes outside this command; this adapter's fixed SELECT queries and
`readonly=1` restrict its requests. There is no new account or database change.

An optional HTTP export is also available. Open the normal SSH tunnel in another terminal:

```bash
ssh -N -L 18125:127.0.0.1:8123 root@collector.example.org
```

Use a ClickHouse account with SELECT-only grants on `solana.instructions` and
`readonly=1`. Set `DNIPRO_CLICKHOUSE_PASSWORD` in your environment using your
existing credential source. Do not put passwords in command arguments or URLs.
Its profile must permit the bounded query settings or already match them. Our
existing `research` profile locks those settings, so use `export-server` for the
current deployment. The HTTP command intentionally refuses to drop its resource
limits silently. See [ClickHouse setting constraints](https://clickhouse.com/docs/concepts/features/configuration/settings/constraints-on-settings).

```bash
.venv/bin/dnipro-backtest export-clickhouse \
  --host 127.0.0.1 --port 18125 --username research \
  --wallet nya666pQkP3PzWxi7JngU3rRMHuc7zbLK8c8wxQ4qpT \
  --start-slot 451480800 --end-slot 451480900 \
  --out var/dnipro/new-carbon-snapshot
```

The narrow range above was used for the production read-only canary, not a
complete trading day. Each export accepts at most 250,000 slots and bounds query
time, memory, rows, and response size. Include an appropriate post-entry exit
tail in your export; the first implementation does not merge multiple snapshots.
An export that exceeds a bound fails rather than publishing a truncated sample.
Remote HTTP without TLS is rejected; loopback HTTP through SSH is supported.

## Execution and accounting contract

- Keep successful Pump.fun `TradeEvent` executions only. Router instructions,
  instruction limits, and CPI events are not summed as separate swaps.
- Attribute raw events to `TradeEvent.user`, independent of `fee_payer`.
  Legacy exports without `user` explicitly retain `fee_payer_proxy` attribution.
- Deduplicate `(slot, signature, absolute_path)` by source priority. Conflicting
  equally authoritative economic payloads fail instead of silently choosing one.
- Reconstruct end-of-signal-slot reserves with the last transaction/instruction
  event, including other actors. State preceding the leader signal is rejected.
- Copy the observed gross SOL buy size and sell all copied inventory one slot
  after each observed signal. Initial capital constrains sequential execution.
- Include observed protocol/creator fees and a network fee proxy, counted once
  per observed leader transaction. Sponsored network fees are not charged to the
  leader. Pump buyback fee is a subdivision of protocol fees, not an extra charge.
- Configure hypothetical follower network costs with
  `--entry-network-fee-lamports` and `--exit-network-fee-lamports`; otherwise each
  signal's observed transaction fee is the stated proxy.
- Quote-rejected exits remain OPEN, with token quantity and cost basis retained.
  Closed PnL and cashflow are distinct. The reconciliation is
  `cash_delta = closed_trade_pnl - open_position_cost_basis`.
- The official PUMP mint is excluded by default, as requested in our earlier
  copy-strategy research.

## What these results can establish

This is an observational comparison, **not exact execution replay**. It prices
the hypothetical next-slot transaction using end-of-signal-slot state. It does
not prove that the following slot exists and contains a landing opportunity,
provide an exact within-slot rank, propagate our hypothetical impact into later
market events, or model competing submissions. Source filters do not establish
complete transaction-clock/history coverage or canonical finality.

The first sample is restricted to one observed buy followed by one full observed
sell per mint. Subsequent trades select that comparison sample, so aggregate
results are not a full-wallet or deployable causal strategy backtest. Multiple
entries, partial exits, unclosed observations, same-slot round trips, Mayhem,
cashback, non-SOL quotes, and unsupported fee profiles are excluded explicitly.
It models the legacy normal Pump curve with observed 95/30 bps fees and fixed
token supply; observed fee amounts must reconcile to that model. Normal token
mode is an assumption, not a creation-transaction proof.

Jito tips, account rent, token transfers, separate creator-fee claims, landed
failed transactions, and post-migration trades are not yet in this adapter's
all-in accounting. It supplies no liquidation value for OPEN inventory.
`--require-exact` deliberately fails even if a manifest's fidelity fields are
edited: this adapter cannot manufacture the upstream exact admission proofs.

Bonk/Raydium LaunchLab, PumpSwap, Jupiter, Orca, and Meteora need independent
normalization and validated execution models before inclusion. The CLI adapter
is not yet connected to the upstream dashboard's exact dataset job workflow.
The production collector and database schema were not changed by this work.

See [security review](../security/REVIEW.md) for the reviewed scope and patches.
