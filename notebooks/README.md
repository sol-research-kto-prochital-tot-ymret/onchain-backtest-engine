# Sniper Window result notebook

## Start with the latest full day review

Open [sniper_oct12_no_dev_sell_all_20261016.ipynb](sniper_oct12_no_dev_sell_all_20261016.ipynb)
for the October 12 full UTC day, original entry/exit rules, a 10,000 SOL wallet,
and no developer-sold-all entry requirement. It includes saved outputs, the PnL
curve, all 887 signals, token charts, entry/exit markers and decision logs.
The older `sniper_oct12_no_dev_flat_20261016.ipynb` reviews the same saved run
through an alternative chart helper. The other dated notebooks below preserve
earlier samples and explicit scenario comparisons.

The notebooks and their Python helpers are versioned here. Full source caches,
native run folders, receipts and CSV exports remain separate, ignored artifacts.
Saved notebook tables and figures can be viewed directly; rerunning cells or
using the token selector requires the matching data folders described below.
Cloning this repository alone does not download those datasets.

Install the engine with `uv sync --all-groups --frozen`, then create the research
kernel using the commands under **Execute and inspect**. Put the full-day frozen
review folder at `var/dnipro/sniper-oct12-no-dev-flat-review-20261016-v1/`.
Its context receipt, native artifacts and bounded chart facts must remain intact;
the review verifies their hashes. `notebooks/sniper_carbon_day.py` is the Carbon
cache projection and streaming replay entry point. The older `sniper_silver`
adapter reads retained historical files; it does not require a running Silver
collector or recreate retired production tables.

Historical experiment helpers retain explicit sample directories and some
KINGSTON storage defaults. Check their inputs and choose a fresh output path
before running a new experiment. Helpers named `finish_*` or `publish_*` also
publish selected research notes or copy backups; they are not needed to view a
notebook. Native implementation hashes are byte-sensitive: publication formatting
and typing changes produce a new source hash. Older completed results keep their
original source identity and must not be rewritten to match a newer checkout.

Interrupted `*.partial.ipynb` copies, Python environments and generated artifacts
are kept outside the published snapshot. Plotly/ipywidgets are in
`requirements-sniper.txt`; Kaleido PNG export additionally needs a local Chrome
or Chromium installation, optionally selected with `BROWSER_PATH`.

## Original strategy and PnL review

`sniper_positions_before_after_20261014.ipynb` is the focused review of the
original strategy on **October 10, 16:00–17:00 UTC only**.
It includes cumulative net closed PnL, realized-curve drawdown, cash charts,
all 38 original signals, GMGN links, entry/exit costs and native order logs.
Full entry-to-exit charts cover all 18 filled positions, split into six
profitable, six unprofitable and six undecided positions. Buy/sell triangles
use native average curve execution prices, with full holding history and an
early-entry zoom for longer positions. Entry/exit slots and UTC times appear
in the grouped tables. Open positions have no invented exit. Separate attempt
galleries cover the three failed and 17 rejected entries.
The `TOKEN` cell inspects any original selected position, including profitable,
losing, failed, risk-rejected or open outcomes. Open positions remain separate
from closed PnL. Original entry/exit rules are verified from the saved policy.

The default is conditional replay preserving observed sell fractions. Its
original has -1.150398400 SOL in closed contributions/failed entry fees and
six open positions with 8.731632158 SOL committed. Its total PnL is unresolved.
The appendix includes only the original strategy under four execution
assumptions for the same hour. Results from different models are not combined.
Tables and PNGs are in `var/dnipro/sniper-original-position-charts-20261014-v4/`.
`all_positions_entry_exit.csv` lists all signals; `native_fill_markers.csv`
preserves 18 buy and 12 sell markers. Individual charts and their token/group
index are retained with the CSV evidence. The earlier original-only notebook
and supporting files are preserved in
`var/dnipro/sniper-original-notebook-before-position-charts-v1/`.
The prior first-hour comparison notebook, builder, helper and README are
preserved in `var/dnipro/sniper-position-review-before-original-only-v1/`.
Earlier comparisons, native runs and CSVs/PNGs remain intact. No standalone
HTML is created.

Rebuild and execute with the existing research interpreter:

```bash
var/dnipro/sniper-notebook-venv/bin/python \
  notebooks/build_sniper_position_review.py --execute
```

The notebook verifies native completion hashes, double-entry ledgers,
per-position cash accounting and cumulative PnL. Source data and native results
remain unchanged. It makes no ClickHouse/RPC calls and does not run a strategy.

## Original investigation notebook

[sniper_window_20261010.ipynb](sniper_window_20261010.ipynb) contains executed
tables and charts for the October 10, 2026, 16:00–17:00 UTC native backtest cohort.
It reads the saved forked-engine runs and frozen Silver tape; it does not run a
new strategy or query production.

The notebook includes 49 filled buy records across both runs, the 27 completed
virtual-settlement positions, charts with early-buyer and hypothetical fill
markers, all 38 signals per run, failed submissions, fees and PnL. Seven separate
liquidity charts and native quote checks explain the positions that remain open
under strict historical reserve caps. The CSV exports preserve GMGN links.

Those seven had zero submitted sell attempts. Their full quotes were rejected
before submission; no failed-sell fee was paid. Smaller sells can fit some of
the recorded reserves. Both backtests omit our buys' reserve changes from later
history, so these diagnostics do not prove a real position could never sell.
Virtual settlement includes explicitly assumed funding and modeled PnL.

## Local inputs

Keep these completed artifacts in their existing locations:

- `var/dnipro/sniper-window-20261010-creator-fees-strict-v1/`: strict native result.
- `var/dnipro/sniper-window-20261010-creator-fees-virtual-v1/`: explicit virtual settlement.
- `../solana-live-indexer/research/sniper_break_even/data/2026-10-10-1600-1700/`:
  frozen launches, clocks and trades, with hash and row-count receipts.

The notebook verifies completion manifests, hashes, integer ledger conservation,
entry costs, cash reconciliation and the seven full-position quote rejections.
Raw data and generated CSVs remain outside the Git snapshot.

## Execute and inspect

From this repository's root, the configured interpreter is
`var/dnipro/sniper-notebook-venv/bin/python`. Select it in IntelliJ to rerun the
notebook. Execution outputs are already saved for inspection without a kernel.

```bash
var/dnipro/sniper-notebook-venv/bin/python \
  notebooks/build_sniper_window_notebook.py --execute
```

The builder uses the workspace-local `sniper-research` kernel. It writes a staged
notebook and publishes the final file only after execution succeeds. On error,
the incomplete executed notebook is retained separately for diagnostics.

To recreate this Mac Python 3.13 research environment:

```bash
.venv/bin/python -m venv var/dnipro/sniper-notebook-venv
var/dnipro/sniper-notebook-venv/bin/python -m pip install \
  -r notebooks/requirements-sniper.txt
var/dnipro/sniper-notebook-venv/bin/python -m ipykernel install \
  --prefix var/dnipro/notebook-kernels --name sniper-research \
  --display-name 'Python 3.13 (Sniper research)'
```

Exports and the evidence receipt are in
`var/dnipro/sniper-window-20261010-creator-fees-notebook/`: buy and outcome tables, all signal
outcomes, simulated order failures, liquidity diagnostics, selected historical
curve trades and chart PNGs. No standalone HTML report is generated.

Chart values are in SOL; historical curve valuation is a reserve/supply pricing
proxy rather than USD market cap. Dashed tails explicitly show a last observed
state carried forward in the replay, not another observed transaction. The
`TOKEN` cell can inspect any mint with a filled position.

## Developer fee accounting

The current notebook includes historical developer fee accrual, fee-adjusted
reference buyer cost at the signal, and additional developer fees generated by
our simulated filled orders. These are recipient-attributed receivables, not
proven withdrawals or income in our trading wallet. It compares the native
fee-aware results with the preserved original runs. The previous executed
notebook and builder remain in
`var/dnipro/sniper-window-20261010-notebook-before-creator-fees/`.

## Native decisions and all liquidity shortfalls

The updated notebook includes all eleven completed-replay liquidity-shortfall
cases, with native strategy and order logs, charts by slot, observed wallet
inventory snapshots and exact signal/landing boundary checkpoints. It compares
all observed holders' break-even thresholds, including the creator and creator
fee accrual, rather than treating the selected reference buyer as every holder.

The traced native reruns preserve every previous token outcome and cost. Their
outputs are `var/dnipro/sniper-window-20261010-slot-traces-strict-v1/` and
`var/dnipro/sniper-window-20261010-slot-traces-virtual-v1/`. The notebook now
exports to `var/dnipro/sniper-window-20261010-slot-traces-notebook-v1/`.
Older results, exports, and the previous notebook/builder remain preserved in
`var/dnipro/sniper-window-20261010-notebook-before-slot-traces/`.

A separate native run tests `--minimum-entry-age-seconds 1` with the same two-slot
entry window. Integer block times measure timestamp age, not precise wall-clock
latency. This experiment rechecks the current observed state; it does not just
postpone an old order. It is still an exogenous replay, with the same market
impact and execution limitations.

Repeat the three native runs with `.venv/bin/python var/dnipro/run_slot_traces.py`
only after choosing fresh output directory names in that script. The CLI refuses
to overwrite completed evidence. `--trace-mint ADDRESS` may be repeated for any
research sample; native traces do not change policy or order behavior.


## Compare profitable positions and liquidity shortfalls

The notebook compares all 13 completed-run winners, all 11 shortfall exits and
three ordinary losses. Signal-time features are separated from landing checks
and post-entry demand; every token remains visible. No entry filters, optimized
thresholds or fitted models are applied.

The reproducible feature extractor is `notebooks/compare_position_groups.py`.
Its outputs are in `var/dnipro/sniper-window-20261010-group-comparison-v3/`:
`entry_features.csv`, `feature_summary.csv`, `holder_economics.csv`, separate
`post_entry_outcomes.csv` and `post_entry_summary.csv`, and hashed provenance.
The notebook adds an aggregate comparison table and chart. Its previous executed
version is preserved in
`var/dnipro/sniper-window-20261010-notebook-before-group-comparison/`.

```bash
var/dnipro/sniper-notebook-venv/bin/python notebooks/compare_position_groups.py \
  --out var/dnipro/sniper-window-my-fresh-comparison
```

The extractor refuses to overwrite a completed comparison. All signal sizing
margins reconcile to the original native results, and observed token inventory
reconciles after every retained event. The one-hour cohort and exogenous replay
assumptions do not establish a profitable rule for future launches.

## Before and after strategy revisions

The notebook also includes the original and selected revised version on the
same independent periods and execution scenarios. The development plan,
selection receipt and all runs are in `var/dnipro/sniper-window-redesign-v1/`.
`notebooks/run_sniper_redesign.py` runs the fixed development candidates and
paired October 11 tests; `notebooks/run_sniper_additional_period.py` runs one
additional fixed October 10 period without changing the selection. Use fresh
output directories when rerunning a new implementation.

The October 11 input has coverage gaps. These remain unresolved exposure in the
results and are shown next to closed PnL. A native conditional protocol reducer
also keeps own reserve impact and reports unknown/incompatible counterpart
orders. Fixed-token and observed-position-fraction sell assumptions are separate
scenarios. These are research simulations, not verified real executions.

## October 12 full day without dev sell all

Open `sniper_oct12_no_dev_sell_all_20261016.ipynb` with the existing
**Python 3.13.12 (Sniper research)** kernel. Its interpreter is
`var/dnipro/sniper-notebook-venv/bin/python`. All 13 code cells have saved outputs.

The notebook reads the frozen 10,000-SOL full-day replay: 251 profitable tokens,
318 losing, 80 unresolved and 238 submitted failed entries. Realized net is
−109.602394368 SOL; 152.319150420 SOL remains committed to unresolved positions.
It does not rerun the strategy or query ClickHouse/RPC.

The outcome/search browser can inspect all 887 signals. Native fills, recorded
order decisions and failed attempts have separate markers. Tables include GMGN
links, entry/exit slots, tracked wallets and first causal blockers. Detailed
per-event strategy traces are available for 78 tokens; their absence for other
tokens is labelled. Modeled inventory is not complete holder coverage.

Frozen notebook inputs and CSV exports live in
`var/dnipro/sniper-oct12-no-dev-flat-review-20261016-v1/`, with a verified copy on
KINGSTON. `prepare_sniper_oct12_review.py` creates the bounded chart cache using
the engine interpreter and PyArrow. `build_sniper_full_day_review.py` executes a
new notebook with the research interpreter. Both preserve completed artifacts;
use fresh output paths to rebuild a separate delivery.
