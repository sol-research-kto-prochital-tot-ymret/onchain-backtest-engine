# Sniper break even replay through the forked engine

The Sniper Break-Even Window V0 strategy runs through this fork's
`ReferenceBacktestEngine`, with its scheduler, order reservations and
`PortfolioState` double-entry ledger. Buys and sells call the existing Pump.fun
`buy_quote` and `sell_quote` functions. The earlier standalone replay loop and
portfolio projection are not imported.

The development cohort is October 10, 2026, 16:00–17:00 UTC / 19:00–20:00 Kyiv,
with a follow-up tail through 17:16 UTC. It retains all 2,342 observed launches.
Non-SOL quotes and special curve modes remain visibly unsupported in the output.

## Run the native replay

From the fork's root, using its existing Python 3.13 environment:

```bash
.venv/bin/dnipro-backtest sniper-window \
  --snapshot ../solana-live-indexer/research/sniper_break_even/data/2026-10-10-1600-1700 \
  --empty-block-evidence var/dnipro/sniper-gap-455328000.json \
  --out var/dnipro/sniper-window-my-run
```

Choose a fresh output directory. The adapter verifies source hashes, row counts,
SQL receipts and publication stability before replay. Completed outputs are
published from a staging directory with a manifest written last. The result
records the source snapshot, empty-block evidence, engine source hash and native
engine audit/fill/ledger hashes.

Optional flags change `--target-return-bps`, `--delay-slots`,
`--holding-seconds` and `--initial-sol-lamports`. Entry and exit delays must be
equal in this integration, as required by the existing `SlotLatencyModel`.
`--require-exact` refuses this research source; retained program capture cannot
establish exact execution admission.

Creator-fee income credit is enabled by default. `--without-creator-fee-credit`
provides an explicit buy/sell-only cost comparison without changing the frozen
input snapshot. Fee-aware result directories remain separate from older runs.

`--execution-mode EXOGENOUS_VIRTUAL_SETTLEMENT` opts into the fork's existing
virtual-curve settlement policy. The default remains `EXOGENOUS_REPLAY`, which
caps sells by historical real-SOL reserves. Both use the native reference runner;
the generic runner now admits the fork's virtual-settlement enum alongside its
existing strict and shadow modes.

## Execution assumptions

This uses the fork's **EXOGENOUS_REPLAY** mode. Each hypothetical fill includes
its own execution-size impact. Later market reserves remain the recorded
historical reserves, rather than a prediction of how other traders would
react to our transaction. It does not propagate our reserve changes through
later historical trades or rewrite those traders' sell quantities.

The later historical real-SOL reserve also excludes our hypothetical deposit.
A full sell can therefore be refused by this replay's historical liquidity cap
even though adding our deposit would change that liquidity in a conditional
market simulation. Open positions with this reason are model outcomes, not
confirmed real-world trapped positions.

In the explicit virtual-settlement scenario, the same upstream integer sell
formula supplies output even when it exceeds historical real-SOL reserves. The
shortfall is booked to a distinct `synthetic-liquidity:pumpfun:...` external
account, using the fork's existing account identity helper. Historical venue
funding, synthetic funding, user proceeds, fees and rent remain separate balanced
postings. Neither mode propagates our own cash or token reserve changes through
later historical orders. Virtual settlement therefore completes a modeled
historical-price result; it does not prove that every exit could land on-chain.

The strategy detects cumulative early buy cost of at least 1 SOL within the
launch slot and its next two slots. After a complete observed transaction, it
evaluates eligible buyer positions, sizes the first feasible entry using the
corrected formula, and requests an order one slot later. Creators remain
eligible and labeled; future ATH, trader count and eventual migration do not
select the birth cohort. There is at most one attempted position per mint.

The primary target is 5% net return, with a 0.001 SOL reserve buffer and a
five-minute timeout. A profit signal compares full-position net liquidation
with entry cost. Execution happens after the configured delay, with a 5%
minimum-output tolerance; actual net return can fall below the signal's target.
Failed exit submissions can retry up to three times. Open positions retain
tokens and cost basis rather than becoming zero-PnL completed trades.

The declared execution scenario starts with 10 SOL and charges 105,000 lamports
per transaction, including network and priority fees, zero tips, and 2,039,280
lamports of refundable token-account rent. Nonzero tips are refused until a
separate native tip settlement model is supplied. These are configured costs,
not measurements of a bot's actual execution expenses.

The native intent contract reserves the execution fee separately from principal.
Successful plans settle the reserved fee; failed submissions atomically release
principal and pay their declared network fee. An order rejected for capital or
an exhausted horizon pays no submission fee. Fee reservations prevent concurrent
orders from spending the same SOL. Refundable rent remains in a locked portfolio
account until a successful full exit.

Protocol and creator fees come from the last observed execution event's basis
points and the upstream integer quote model. Buyback is part of protocol fees.
Historical fee-tier configuration is not reconstructed; crossing a tier in a
hypothetical order remains a modeling assumption. Sniper holdings and cost
basis assume no unseen transfers and assign an observed network fee once per
signature within the token ledger when the sniper is fee payer. Multi-token
transactions, unseen failed attempts and separate creator income need further
corroboration.

## Source corrections

The original publication receipts leave slot 455328000 uncertified. A cached
public `getBlock` request using finalized commitment and **full** transaction
details returned an existing block with an empty transaction array. Its block
time supplies an evidenced clock boundary and proves there were no token
transactions to collect in that block. The original snapshot remains unchanged;
the evidence is a separately hashed input. Missing or omitted transaction
details, non-finalized responses and nonempty blocks cannot clear a coverage
hole.

`MigrateBondingCurveCreatorEvent` changes the creator/fee-recipient account;
it is not curve graduation. Only `curve_complete`, `amm_migration`, or exhausted
real token reserves block this pre-migration strategy. Within a transaction,
source causal IDs retain the observed event-index order so the native observation
queue delivers creation and trade events in that order. Entry decisions run
after the complete retained transaction group.

All of this remains a retained program subset. It does not establish complete
holder transfers, canonical finality for every captured transaction, actual
landing rank, or full-wallet PnL. Source reserve discontinuities and genuinely
uncertified intervals block dependent trades.

## Read the outputs

| File | Meaning |
| --- | --- |
| `token_results.csv` | Every launch, signals, funded entries, exits, reasons, open inventory and GMGN links |
| `fills.csv` | Fills produced by the native engine and Pump quote adapter |
| `ledger.csv` | Native balanced postings, including fees and locked/refunded rent |
| `execution_failures.csv` | Native rejected or failed orders |
| `result.json` | Summary, native hashes, positions, fills and complete ledger |
| `manifest.json` | Completion marker and output/source/code hashes |

Closed rows additionally record the gross curve output, observed real-SOL
reserve, synthetic shortfall and selected sell-liquidity policy. The summary
reconciles total synthetic shortfall to the external ledger accounts. These
fields keep research settlement distinguishable from actual historical liquidity.

In `fills.csv`, a buy's allocated input includes posted rent. Network fees use
the separate intent reservation. A sell's input asset is the token, so its
`fee_amount_atomic` is zero; SOL-denominated sell fees appear in `ledger.csv`.
Do not sum that fill field across different assets to estimate total fees.

The exact cash reconciliation is:

```text
final available SOL - initial SOL
  = closed net PnL - failed entry fees - cash cost of remaining open positions
```

The zero-recovery stress result assumes open inventory is worthless. It is
reported separately from realized PnL. SOL display values are decimal strings
derived from integer lamports; semantic result hashes contain no binary floats.

## Pilot results

The verified native run is in `var/dnipro/sniper-window-20261010-primary-v2/`.
With the declared 10 SOL initial budget, it found 38 signals, filled 22 entries,
closed 15 positions and left seven open. Eleven closed positions were profitable
and four lost money. Five submissions failed their minimum output; 11 entries
were rejected by capital constraints.

Closed-trade PnL was +0.434483070 SOL. After 0.000525 SOL of failed-entry fees,
realized PnL was **+0.433958070 SOL**. The seven open positions retain
**9.701282638 SOL** of cash cost. They cannot fully liquidate against the
historical real-SOL reserve cap; this partial realized result does not establish
overall profitability. Available cash is 0.732675432 SOL, and the native ledger
reconciliation passes exactly.

## Completed exits under virtual settlement

The October 14 investigation established that the seven open positions were
not cut off by the end of the dataset. Their five-minute exit deadlines were
October 10, 16:26:18–16:50:41 UTC; the retained snapshot includes clocks and trades
through 17:16 UTC. The strict adapter rejected a full-position quote before
creating a sell order, even after timeout, because the historical real reserve
excluded our hypothetical added liquidity. All seven show zero submitted exit
attempts in the strict result.

The same source and policy replayed with `EXOGENOUS_VIRTUAL_SETTLEMENT` filled
27 entries and closed all 27, with no open inventory or locked rent. Thirteen
positions won and 14 lost. Net modeled PnL was **−3.759502143 SOL**, including
0.000840 SOL of failed-entry fees; 10 SOL initial capital ended as 6.240497857 SOL.
Eight entries failed their minimum output and three were rejected for capital.
Timely exits released cash for five additional filled entries.

The original seven open positions all closed at their configured five-minute
timeout. Their entry times, sizes and costs match the strict run exactly; their
combined modeled PnL is **−3.561762342 SOL**. Two other original positions also
exit earlier under the changed liquidity policy, so the full replay is not the
old partial result plus only seven additional sales.

Eleven exits include **7.942450970 SOL** of explicitly synthetic gross liquidity
shortfall. This amount is already part of the modeled settlement, not another
loss to subtract from PnL. It measures what historical real reserves alone could
not pay. A replay that carries our own reserve changes forward still needs
declared rules for counterparty orders, balances, slippage and migration. No
missing follow-up data prevents evaluating these five-minute exits.

Artifacts are in `var/dnipro/sniper-window-20261010-virtual-v1/`, with identical
repeat outputs in `var/dnipro/sniper-window-20261010-virtual-repeat-v1/`.
`var/dnipro/sniper-window-20261010-exit-audit/` contains the original seven
positions and a comparison of all 38 signal outcomes.

Reproduce the completed modeled result with:

```bash
.venv/bin/dnipro-backtest sniper-window \
  --snapshot ../solana-live-indexer/research/sniper_break_even/data/2026-10-10-1600-1700 \
  --empty-block-evidence var/dnipro/sniper-gap-455328000.json \
  --execution-mode EXOGENOUS_VIRTUAL_SETTLEMENT \
  --out var/dnipro/sniper-window-completed-exits
```

The result, positions, fills and ledger reproduced byte-for-byte in a second
native replay. The existing suite passed 1,658 tests inside the sandbox; its two
localhost-binding tests then passed outside it. Six added empty-block evidence
checks and the 28 targeted strategy/source/reference tests passed. Ruff and
Mypy checks passed for the changed research components.

## Components

- [`SniperWindowStrategy`](../src/backtest/plugins/strategies/sniper_window.py): detection, sizing and exit decisions.
- [`PumpSniperExecutionModel`](../src/backtest/plugins/execution/pump_sniper.py): execution plans using upstream Pump quotes.
- [`SilverSniperSource`](../src/backtest/adapters/source/sniper_silver.py): verified source and empty-block evidence.
- [`Native composition`](../src/backtest/bootstrap/sniper_window.py): calls the existing engine and publishes results.
- [`ReferenceBacktestEngine`](../src/backtest/engine/reference.py): reused replay, scheduler and ledger reducer.

Run the strategy tests with:

```bash
.venv/bin/python -m pytest \
  tests/unit/plugins/strategies/test_sniper_window.py \
  tests/unit/adapters/source/test_sniper_silver.py \
  tests/unit/engine/test_reference_engine.py -q
```

## Creator fee income in the reference buyer calculation

The reference buyer's unrecovered cost now equals observed buy costs and paid
trading fees, minus net observed sell proceeds and creator-fee income accrued to
that wallet. Only events delivered before the decision can reduce that cost.
Eligibility still requires the observed early buy; fee income alone does not
create a trader position. Buyers are considered in descending fee-adjusted cost.
A wallet already covered by its fee income does not supply a positive remaining
break-even threshold for this strategy.

Each TradeEvent identifies its fee recipient. An empty shareholder list credits
its event `creator`, while a valid nonempty list credits each named wallet by
`share_bps`. The launch creator is not substituted for a missing recipient or a
sharing-config PDA. Duplicate event identities cannot add income twice. Shared
integer floor-rounding dust and unproven recipients remain unallocated, and
allocated plus unallocated amounts conserve every creator fee. Claim events are
not credited again.

These amounts are **event-time vault accrual**, not independently proven wallet
withdrawals. The official [creator fee documentation](https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_CREATOR_FEE_README.md)
describes the vault and separate collection instruction; [fee sharing](https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/CREATOR_FEE_SHARING.md)
describes recipient shares and their distribution. Event-time share attribution
is a receivable estimate; actual distribution and collection expenses are not
reconstructed here. Future fees, projected outside volume and our planned order's
fees are not credited to a buyer before its entry signal.

Signal rows expose the trading cost before credit, accrued creator income and
adjusted cost. Every launch row additionally exposes the launch creator's
observed trade cost, remaining swap-derived tokens and historical fee accrual.
`creator_fee_recipients.csv` retains the actual named recipient wallets, GMGN
links, integer fee accrual and observed cashflow. Unsupported modes stay visible
with unknown developer accounting rather than zero-valued earnings. Trading
cashflow plus accrued fees is not full realized or marked PnL.

The updated notebook separately attributes creator fees paid by our filled
hypothetical orders using the last evidenced route at each fill. It adds those
fees to developer-side diagnostic cashflow, while keeping them out of our
trading wallet. Our PnL already pays these fees and does not receive them.

The same frozen October 10 cohort accrued 154.260004195 SOL of creator fees over
its supported ordinary curves, with 71 lamports left unallocated as shared
rounding residual. No selected reference buyer had fee income by its signal:
all 38 signals, buy sizes, exits and trading results match the older runs.
Strict realized PnL remains +0.433958070 SOL with seven open positions. The
complete virtual-settlement scenario remains −3.759502143 SOL across 27 closed
positions, under the previously documented liquidity assumptions.

Fee-aware native outputs are in
`var/dnipro/sniper-window-20261010-creator-fees-strict-v1/` and
`var/dnipro/sniper-window-20261010-creator-fees-virtual-v1/`. The updated notebook
exports to `var/dnipro/sniper-window-20261010-creator-fees-notebook/`; its previous
version is retained under
`var/dnipro/sniper-window-20261010-notebook-before-creator-fees/`.

## Decision tracing and launch age

Repeat `--trace-mint ADDRESS` to retain entry candidates, selected signal inputs,
exit evaluations, quote preflight rejections and execution notifications for
specific tokens. `strategy_trace.csv` records exact boundary ordinals and slot
numbers; `order_audit.csv` retains native order scheduler decisions for the run.
Tracing does not enter the strategy policy identity or change its decisions.

`--minimum-entry-age-seconds 1` is a separate policy sensitivity test. It waits
for the launch timestamp to age by one second and reevaluates current wallet and
market state at eligible slot boundaries, while preserving the original
launch-plus-two-slot window. Its default is zero. Historical block times have
integer second resolution, so it does not simulate an exact one-second timer.

Passing the reference buyer sizing inequality does not enforce an all-holder
condition. Lower-cost holders and the creator can have profitable exits before
our target even when the chosen reference buyer remains below break-even. A
minimum-output condition at order landing also does not revalidate the original
reference-buyer inequality. Slot and holder diagnostics make these distinct
conditions explicit without adding an unapproved liquidity filter.

## Frozen strategy revisions and paired tests

`--policy PATH` supplies explicit JSON overrides for the strategy policy. The
October 14 experiment keeps the baseline and three revisions in
`var/dnipro/sniper-window-redesign-v1/experiment_plan.json`. A selection receipt
is written on development data before validation outcomes are opened. The
before/after runs use identical input, starting cash, fees and execution mode.
The extension period is declared separately and does not change the policy.

The revised policy can cap gross entry budget, reject a recorded launch-creator
buy, watch an initial feasible window, and require subsequent net buying and new
buying addresses before recomputing the entry. It can extend the decision window
while keeping original early-buyer eligibility unchanged. Quote-based stop loss
and shorter timeout schedule a native delayed sale; neither guarantees that the
sale will land or that loss will remain within its trigger.

`CONDITIONAL_PROTOCOL_REPLAY` now requires an explicitly configured protocol
reducer in the native reference engine. The Pump reducer carries our actual
native fill's SOL and token reserve changes into later groups. Historical
exact-quote buys retain their fee-inclusive budget, exact-token buys retain
requested quantity, and sells retain token input by default. Groups commit
atomically. Our native wallet ledger and the venue/counterpart token inventories
reconcile; synthetic funding is never used by this mode.

The separate `counterpart_sell_fraction=1` scenario preserves each observed
seller's fraction of their observed inventory, including a full exit, while
scaling quantity to the replayed inventory. It is an assumption about sale
sizing and is shown separately from fixed recorded token input. Neither scenario
reconstructs unknown historical slippage limits, SOL funding constraints,
unobserved transfers or private reactions. Incompatible orders and inventories
block dependent open positions rather than forcing a favorable outcome.

`conditional_repriced.csv` retains recorded and replayed quantities and reserve
receipts; `conditional_errors.csv` retains conflicts. Source files and historical
reference events remain unchanged. Read-only exports, policy files, source and
output manifests, all attempted signals, failed orders and unresolved costs stay
available alongside the aggregate result. Do not interpret positive closed PnL
with open exposure, or a zero-trade revision, as a validated profitable strategy.


## Entry decisions by token age (October 15 extension)

`--minimum-entry-age-seconds 10 --maximum-entry-age-seconds 20` searches
for the first feasible original-strategy signal at token ages 10 through
20 seconds inclusive. A nonzero maximum selects timestamp-based decision
eligibility instead of the older slot-based entry horizon. The early reference
buyer still qualifies within launch+2 slots. Holdings, unrecovered cost, creator
fee accrual, reserves and size are recomputed at each causal decision. A buyer
that has already exited is not treated as still holding its initial purchase.

The interval uses retained whole-second slot timestamps; it is not a precise
wall-clock millisecond measurement. The order still executes one slot after
the signal, so a signal at age 20 seconds may fill after age 20 seconds. A zero
maximum keeps the original slot window and policy identity. Default timing,
fees, bankroll, sizing and exits are otherwise unchanged.

The bounded first-hour experiment is recorded in
`var/dnipro/sniper-entry-age-10-20-20261015-v1/experiment-plan.json`. It uses the
50 SOL policy and the existing verified transfer overlay. That overlay covers
known original blockers and does not prove transfer completeness for newly
selected tokens. Keep any new unresolved positions separate from completed PnL.
