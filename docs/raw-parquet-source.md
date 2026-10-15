# Raw Parquet research source

`RawParquetSource` consumes the collector's `solana-raw-parquet/v1` datasets.
It verifies the combined marker, part receipts and file hashes before reading.
Default access also requires passing audit gates bound to the exact manifest,
and restricts replay to the audited stable slot interval rather than including
the unverified capture head or tail.

```python
from pathlib import Path
from backtest.adapters.source.raw_parquet import RawParquetSource

source = RawParquetSource(Path('/data/research/validated-dataset'))
for event in source.events():
    ...
for movement in source.transfers():
    ...
```

`events()` implements the native historical-source contract for supported
market events and actual retained block clocks. Ordering uses a one-thread,
bounded-memory DuckDB sort that can spill to a private temporary directory.
Choose bounded slot windows for large reference-engine replays; `boundaries()`
materializes that requested boundary timeline.

`transfers()` returns committed transfer instruction facts by default.
`transfers(committed_only=False)` also exposes failed, reverted and unknown
attempts with their explicit execution status. Transfer authorities and token
owners remain separate; unresolved owners are not invented.

The engine's canonical-event union currently has no `TransferEvent`. This
adapter exposes transfer facts for research but does not make the reference
engine automatically apply external transfers to wallet inventory. Exact
holder/PnL capabilities need a separate integration and coverage contract.

`allow_incomplete_pilot=True` explicitly permits research on an incomplete or
failed pilot. It is not production acceptance and must not be used to claim
complete chain coverage or justify deleting an existing pipeline. The October
15 candidate did not meet the end-to-end throughput or semantic rollout gates.
