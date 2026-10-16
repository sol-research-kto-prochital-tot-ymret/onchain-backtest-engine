# Sniper backtest review package

Start with `onchain-backtest-engine/notebooks/sniper_repaired_wallet50_20261015.ipynb`.
Open it in VS Code with the Jupyter extension. Outputs are already saved; you can
read the charts and tables without executing cells or connecting to a server.
Alternatively open the PNGs and CSVs in the `onchain-backtest-engine/var/dnipro/sniper-repaired-review-20261015-v1/` folder.

## Which experiment is this?

- Original Sniper Break-Even Window strategy; **50 SOL** starting wallet and
  **5%** entry/exit slippage.
- Launch cohort: **October 10, 2026, 16:00–17:00 UTC** / 19:00–20:00 Kyiv.
  Retained observations follow these tokens through approximately 17:16 UTC.
- 2,342 launches and 187,107 retained trade events, plus slot clocks,
  lifecycle/coverage metadata, and 13 verified external token transfers.
- Native conditional protocol replay propagates our reserve impact, preserves
  observed counterpart sell fractions and keeps external transfer amounts fixed.
- **Realized net contribution: -3.289255885 SOL**, including nine failed-entry fees.
  29 buys, 28 closes: 14 profitable and 14 losing. One unresolved position costs
  **1.207495000 SOL**. Total final PnL is unknown; zero recovery on the open position
  gives **-4.496750885 SOL** as a separate stress assumption.

This package contains all retained inputs used in this experiment. It is not a
full Solana archive or complete token-wide holder/transfer history. Unknown
transfers, historical fee tiers, trader reactions and actual transaction landing
remain limitations. Creator fees earned by other wallets are not our profit.

## Data map

Paths below are relative to the extracted package root.

| Path | What it contains |
|---|---|
| `onchain-backtest-engine/notebooks/sniper_repaired_wallet50_20261015.ipynb` | Executed notebook: all 38 signals, grouped charts and decision evidence |
| `onchain-backtest-engine/var/dnipro/sniper-repaired-review-20261015-v1/` | PnL chart, 38 token charts, entry/exit CSVs, all 27,894 decision records, per-token JSON metadata and receipts |
| `onchain-backtest-engine/var/dnipro/sniper-inventory-repair-20261015-v1/development-repaired-before/` | Active full-trace native result: positions, fills, balanced ledger, order audit, failures, repricing and applied transfers |
| `onchain-backtest-engine/var/dnipro/sniper-inventory-repair-20261015-v1/repaired-v3/` | Economically identical repaired result with the earlier narrower trace selection |
| `onchain-backtest-engine/var/dnipro/sniper-inventory-repair-20261015-v1/public-rpc/` | Full finalized transaction responses and request/hash receipts used to prove transfers |
| `onchain-backtest-engine/var/dnipro/sniper-inventory-repair-20261015-v1/clickhouse/` | Bounded query SQL, returned rows and query receipts from the inventory investigation |
| `onchain-backtest-engine/var/dnipro/sniper-inventory-repair-20261015-v1/transfers-v3.json` | Final evidence-bound transfer overlay; 13 supplied, 12 applied, one modeled inventory conflict |
| `solana-live-indexer/research/sniper_break_even/data/2026-10-10-1600-1700/` | Frozen source: configuration, manifest, compressed launch/trade/clock/lifecycle/coverage files, extraction SQL and receipts |
| `onchain-backtest-engine/src/` | Exact Python engine source snapshot used by the repaired run |
| `onchain-backtest-engine/tests/` | Engine tests and fixtures; only the documented focused subset was revalidated for this handoff |
| `notes/` | Strategy specification and sourced investigation notes |
| `audit/` | Source revision, dirty-source provenance and original dependency versions |
| `CONTENTS.csv` | Per-file inventory: category, relative path, size and SHA256 |
| `MANIFEST.json` | Complete package hashes and experiment facts |

Earlier repair iterations and the no-overlay control are retained as audit
evidence. The archived original 50 SOL result is under
`onchain-backtest-engine/var/dnipro/sniper-slippage-wallet-20261015-v1/slippage_5pct_wallet_50sol/`.
Those earlier results are separate scenarios; do not add their PnLs together.
The frozen extraction's `config.json` still records its original 10 SOL setting.
The active replay explicitly overrides that with `wallet50-policy.json`.

## Reading entry and exit decisions

In the review exports, start with `all_positions_entry_exit.csv` and `pnl_events.csv`.
`positions/<mint>.png` is the token chart; the matching `.json` stores the full
position, entry signal, exit evaluation and counterparty errors; `.decisions.csv`
stores every logged evaluation for that token. All wallet/token links use GMGN.

Green upward triangles mark actual simulated buys; red downward triangles mark
actual simulated sells. Failed entries have no buy marker. The unresolved token
has no invented exit. Purple is historical marginal price, orange is replayed
state at strategy evaluations, and fill marker heights are average execution
prices. Costs and net PnL are available separately in the tables and ledger.

Entry records include the reference buyer, inventory, fee-adjusted unrecovered
cost, sizing, break-even thresholds, reserves and minimum token output. Exit
records include quoted net liquidation PnL, target, timeout, decision and fill
slots, fees and order notifications. The inventory panel follows the reference
buyer; it is not a complete holder distribution.

`MINIMUM_OUTPUT_NOT_MET` means a submitted entry failed the configured slippage
test: actual token output was below the recorded minimum. Principal is released;
only the configured 0.000105 SOL submission fee is charged. Nine failures cost
0.000945 SOL total and are already included in realized net PnL.

The remaining open token is 毒チワワ. A verified historical transfer requires
158,769.203521 tokens, but the sender's simulated earlier buy obtains only
155,358.106307 tokens after our intervention. The fixed transfer is short by
3,411.097214 tokens. Replay stops at that incompatibility. This is not an
observed failed sell of our position. Scaling that transfer would be a new
behavioral assumption and has not been enabled.

## Verify the extracted package

From the extracted package root, using Python 3.13:

```bash
python3.13 verify_bundle.py
```

This verifies every packaged file, frozen source receipts, native result
manifests, ledger conservation, PnL and trace/marker counts. It needs only the
Python standard library and does not contact ClickHouse, RPC or any server.
An integrity manifest proves byte consistency, not that the blockchain capture
was complete. Verify the untouched package before rerendering or editing files.

## Rerun the backtest

Use Python **3.13**. macOS and Linux are supported by this handoff; use WSL2 on
Windows. Initial dependency installation needs Internet access, but replay uses
the bundled local inputs and needs no database credentials or API key.

```bash
python3.13 -m venv .venv-engine
.venv-engine/bin/python -m pip install -r requirements-engine.txt
.venv-engine/bin/python rerun_backtest.py
```

The wrapper sets the bundled source path, uses the exact saved policy and
transfer overlay, writes a fresh result directory under `reproduced/`, and
asserts that positions, fills, ledger and entire summary equal the saved result.
Do not invoke the historical collection helpers: they are included for audit,
and some can query public RPC or the original research server.

## Rerun notebook cells or refresh saved outputs

```bash
python3.13 -m venv .venv-review
.venv-review/bin/python -m pip install -r requirements-notebook.txt
.venv-review/bin/python refresh_notebook.py
```

The refresh helper creates a temporary local kernel specification, so it does
not require the original computer's registered `sniper-research` kernel. It
executes the included notebook builder using local data. No persistent global
kernel is registered. Refreshing replaces derived notebook/CSV/PNG outputs;
their hashes can then differ from the original package manifest.

For interactive VS Code use, select `.venv-review/bin/python` as the notebook
kernel. Reading existing outputs does not require installing either environment.

## Focused code checks

After installing engine dependencies:

```bash
cd onchain-backtest-engine
PYTHONPATH=src ../.venv-engine/bin/python -m pytest -q \
  tests/unit/engine/test_reference_engine.py \
  tests/unit/plugins/protocols/test_pumpfun_model.py \
  tests/unit/plugins/strategies/test_sniper_window.py \
  tests/unit/adapters/source/test_sniper_inventory.py \
  tests/unit/adapters/source/test_sniper_silver.py
```

The engine retains its upstream MIT license and third-party notices. No Git
history, Python environment, API secrets or SSH keys are needed for this package.
