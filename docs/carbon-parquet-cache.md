# Carbon instruction cache translation

The collector's `solana-export` caching tool first exports Bronze Parquet, then
calls this translator automatically. Translation is required by default;
`--no-translate` explicitly creates a raw-only dataset. A normal job retains:

```text
<dataset>/
  dataset.json                 combined job, window, stage and worker config
  COMMITTED                    only after both requested stages verify
  raw/                         original instruction columns, hashes and SQL
  translated/
    manifest.json              immutable input inventory and converter revision
    COMMITTED                  verified translated manifest hash
    progress.json              partition progress and translation ETA
    parts/<slot-range>/
      events.parquet           engine market events plus their retained context
      transactions.parquet     one fee/status record per retained transaction
      evidence.parquet         failed, unsupported, unresolved and router records
      boundaries.parquet       observed replay boundaries
      receipt.json             counts, input/output hashes, timing, disposition
```

These are **retained instruction observations**, not a certificate of complete
canonical-chain history. No cohort, market-cap, liquidity, migration-speed or
wallet filter is applied by the translator. Non-SOL quote assets remain distinct
assets, without invented decimals or USD prices.

## Offline use

The engine requires Python 3.13 and its normal dependencies, including DuckDB
and PyArrow. This implementation consumes the new UTC-window exporter manifest.
It does not scan arbitrary Parquet globs or accept incomplete raw exports.

```bash
python -m backtest.bootstrap.carbon_cache translate \
  --raw /path/to/dataset/raw \
  --output /path/to/dataset/translated

python -m backtest.bootstrap.carbon_cache verify --output /path/to/dataset/translated
python -m backtest.bootstrap.carbon_cache inspect --output /path/to/dataset/translated
```

`translate` is resumable with exactly the same command. It verifies committed
parts, recovers a prepared part after an interrupted manifest update, and replays
only its own unfinished part. Input or converter changes require a new output
directory. Checksums, raw receipts, partition continuity, all instruction
dispositions and the engine's actual event constructors are checked before
publication. The default keeps 100 decimal GB free and limits DuckDB to one
thread and 512 MB; sorting can spill within the current temporary partition.
The sorter projects only the columns used by the translator and finishes each
sort into temporary Parquet before reading Arrow batches. Memory failures
bisect the slot interval, keeping every transaction's instructions together.
A failed sort releases no rows; retrying cannot publish its partial output or
duplicate rows. The same protection applies to the converted output tables.

## Native engine source

```python
from pathlib import Path
from backtest.adapters.source.carbon_cache import CarbonParquetSource

source = CarbonParquetSource(Path('/path/to/dataset/translated'))
# source implements the engine's HistoricalEventSource contract.
# Pass it to ReferenceBacktestEngine.run with the chosen strategy/execution model.
for event in source.events():
    ...

# Rich actor, reserve, fee and provenance context remains available:
for fact in source.rows('events'):
    ...
for transaction in source.rows('transactions'):
    ...
```

Optional `start_slot`/`end_slot` bounds restrict a replay. Events stream in
2,048-row batches. The current reference-engine API materializes its smaller
boundary timeline as a tuple; choose bounded replay windows for very large
datasets. This is a source adapter, not the old wallet-only 500,000-row
`dnipro-carbon-research-snapshot/v1` format or a registered canonical-distribution
artifact. Use the source object with the reference engine; do not pass the
directory to the legacy `import-snapshot` CLI.

## Current normalization scope

- Pump.fun: successful `CreateEvent`, `TradeEvent`, `CompleteEvent`, and
  `CompletePumpAmmMigrationEvent`. The actual event user stays separate from
  the fee payer. The curve address is from context or its deterministic Pump
  PDA. Virtual after reserves and real inventory are separate facts. Buyback
  distributions are preserved without charging the protocol fee twice.
- PumpSwap: successful Buy/Sell execution events when mint context is retained.
  Emitted **before** reserves stay in facts; generic after-reserve fields are
  null. They are not promoted to verified executable liquidity.
- Raydium LaunchLab: TradeEvents with evidenced mint/actor context, amounts and
  fees. Pricing parameters remain unverified, so generic after liquidity is
  unavailable. Brand ownership is not inferred from the decoder.
- Meteora DBC: swap result amounts and fees, with v2 preferred over the older
  event emitted by the same call. Curve configuration is still needed for
  executable quotes. No guessed constant-product reserve pair is created.
- Jupiter: router observations stay in `evidence.parquet`, avoiding an extra
  economic trade on top of underlying pool executions. These records are not
  a complete user-route reconstruction.
- Other instructions/venues: retain audit pointers to the raw signature/path
  and a reason. A decoder record or instruction limit alone is not promoted
  to an executed swap. Missing PumpSwap asset context is explicitly unresolved.

Higher source priority wins for the same slot/signature/instruction path.
Equally authoritative economic/context conflicts stop publication. Retained
events are sorted by slot, transaction index and path. Fees/status are grouped
at transaction grain; repeated instruction copies do not repeat network fees.
Fork/finality reconciliation is not proven from these instruction projections.

The canonical event envelope provides the order of the retained subset. A
successful enclosing transaction does not prove every caught inner call
committed; unverified inner commitment remains a quality flag. Synthetic block
events are clocks for observed slots only, not fabricated empty-block proof.
Exact execution, complete funding/holder history, tips and bundle membership
remain unavailable unless separate source evidence supplies them.

A successful format/replay validation does not establish a profitable strategy
or exact execution costs. Venue execution models must respect these capabilities
and the retained amount/reserve semantics.
