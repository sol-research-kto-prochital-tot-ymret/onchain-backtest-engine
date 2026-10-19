# Modern Pump replay

The engine includes an explicit modern Pump execution path alongside its unchanged,
SOL-only legacy sniping/copy-buy runtimes. This is production package code,
not an import from the research scripts. Use the modern path for arbitrary quote
mints and route-level accounting; the legacy strategies are **not** silently
converted to route strategies or marked compatible with every new account layout.

## Run offline

From the engine repository:

```bash
PYTHONPATH=src .venv/bin/python -m backtest.bootstrap.dnipro pump-replay \
  --input docs/examples/pump-modern-replay.jsonl \
  --out var/pump-modern-example
```

With an installed package, use `dnipro-backtest pump-replay`. The module entrypoint
`python -m backtest.bootstrap.pump_replay` accepts the same input/output arguments.
Choose a new output directory each time. The bundled input is a synthetic arithmetic
fixture with a symbolic token ID, not a historical performance result.

Output contains streaming `receipts.jsonl`, `summary.json` and a `COMMITTED` hash
manifest. Publication uses private staging and refuses existing destinations. No
network requests, signing keys, database writes, or live trades occur. Balances stay
in each mint's atomic units; no invented SOL/USD conversion or aggregate PnL is added.

## Supported behavior

| Change | Engine behavior |
| --- | --- |
| Pump coins as quotes | Explicit base/quote mints; up to three trade legs (depth-2 quote ancestry). Missing ancestors and cycles fail closed. |
| Multi-hop | Atomic route settlement with final-output slippage. Official multi-hop fee placement differs from separate sequential instructions. No intermediate wallet balances are invented for a fully consumed multi-hop route. |
| Signed PumpSwap offset | Pricing uses vault plus signed i128 offset. Absent offset is unknown, not zero. Raw vault liquidity is never substituted. |
| Retained protocol/creator fees | Fee buckets are venue liabilities, excluded from real quote liquidity. A sweep changes custody, not price, and never credits the simulated trader merely because they triggered it. |
| Holder rewards | Explicit creator/holder/unknown beneficiary. Holder allocations remain within the existing creator-fee amount, not an extra fee or developer income. |
| Curve V3 / pool V2 | Explicit instruction-version state. Existing legacy pricing functions remain unchanged. Exact-base-out buys are available through the typed quote API; the replay CLI submits exact-quote-in route orders. |
| Synthetic migration | Eligible curve V3 crossing buys execute curve and pool-to-be portions using evidenced physical base inventory and migration fees. The completed curve cannot trade again. A pool-to-be is not treated as a live migrated pool. |

Integer arithmetic and independent pinned SDK goldens cover exact-in, exact-out,
signed reserves, route fees and synthetic crossing. A direct synthetic buy may
consume less than its requested budget; unused root and intermediate quote amounts
remain in the correct native-mint balance. Trading fees already included in the
quote are not debited again. An explicitly supplied SOL network fee is charged once
per filled or submitted slippage-failed route, not per hop. Missing-state/preflight
rejections do not fabricate a submitted transaction or a network fee.

## Input contract

JSONL begins with exactly one `{"kind":"balances","balances":{"mint":123}}` record.
Subsequent records are:

- `state`: `venue_id`, `event_id`, `position`, `state_timing`, and complete
  `state`. State contains `kind` (curve/pool), explicit `version`, native mints,
  virtual/real reserve quantities, retained fee buckets, fee configuration, and
  explicit cashback/mayhem/canonical/creator-beneficiary evidence. Pool states also
  require a vault balance and signed offset. Synthetic crossing requires physical
  curve base balance and the applicable SOL migration fee. Fee configuration must
  explicitly specify all fields, including a causally resolved buyback split.
- `order`: `order_id`, `position`, selected `venue_ids`, `input_mint`,
  `output_mint`, `amount_in`, and explicit `network_fee_lamports`; optional
  `min_amount_out` and `mode` (`sequential` or `multi_hop`).
- `sweep`: `venue_id` and `position`, with optional `component` (`protocol`,
  `creator` or default `all`). Independent sweeps preserve the other fee bucket
  and do not change price. They do not assert a creator wallet has received the
  money or model the sweeper's rent/network costs.

Positions contain non-negative integer `slot`, `transaction_index`,
`outer_instruction_index`, `unix_s` and a non-empty `signature`. A state must be
strictly earlier than execution. `after_transaction` state cannot price another
instruction inside that same transaction. `after_outer_instruction` requires a
strictly later outer instruction. Decoder event enumeration alone does not prove
CPI-return order. Repeated signed observations across slots do not reset state;
conflicting identities or backwards observations reject the replay.

JSON rejects unknown fields, duplicate keys, booleans as integers, float/string
quantity coercion and lines larger than one MiB. Unknown signed offsets, fee
beneficiaries and incomplete routes remain unresolved, not zero-valued successes.

## Retained Carbon data is not automatically an executable state snapshot

The normalizer now preserves modern instruction/version evidence, holder flags,
signed offsets, route identity and physical synthetic execution facts. Underlying
multi-hop execution legs are economic events; the router is not another purchase.
Fee sweeps are retained as audit evidence, not fabricated swap volume.

PumpSwap events expose BEFORE inventory and an AFTER signed offset. The engine does
**not** add these together. Ordinary retained events may also omit modes, physical
ATA balances, fee buckets, or fee-resolution state. Modern metadata explicitly
lists missing evidence and marks `executable_state_verified=false`. Supply complete
causal account snapshots to the replay input; never rename raw normalizer output
into a complete `state` record. The generic Carbon reader and legacy primitive
fast paths are not silently changed into modern protocol projectors.

## Historical and unsupported cases

Negative signed offsets are documented from September 30; depth-2 creation from
October 13. Dates are not proof that an individual account/instruction used a new
version. Version and state evidence must be pinned per observation.
Re-normalizing old cache data changes the normalizer fingerprint;
old serial/parallel checkpoints are not reusable under a different fingerprint.

The new quote path fails closed for unknown fee contracts, nonzero buyback-split
rounding not established by the pinned quote SDK, cashback payout simulation,
partial-fill requests, mixed-direction routes and routes longer than three legs.
Those are explicit unsupported cases, not approximated results. Existing legacy
cashback/model behavior is preserved in the legacy path. Creator-fee accrual,
sweeps, actual wallet receipt, and holder distributions remain distinct accounting
stages; exact total creator PnL still requires their actual cashflow evidence.

Later historical snapshots replace the recorded venue state after simulated fills:
this is **conditional recorded-path replay**, not a market-impact counterfactual.
Results always report `exact_execution=false`; the implementation does not claim
complete-chain coverage, exact landing, MEV, latency, rent or future-price knowledge.

## Pinned official documentation

The implementation and arithmetic provenance are pinned to docs commit
`639b87fb45c3adeffa6f7c4a1bf0cb85e392bf22`, Pump SDK 4.1.0 and PumpSwap SDK 2.1.0.

- [Pump coin quote mints](https://github.com/pump-fun/pump-public-docs/blob/639b87fb45c3adeffa6f7c4a1bf0cb85e392bf22/docs/instructions/CREATE_WITH_PUMP_COIN_QUOTE.md)
- [Multi-hop swaps](https://github.com/pump-fun/pump-public-docs/blob/639b87fb45c3adeffa6f7c4a1bf0cb85e392bf22/docs/instructions/MULTI_HOP_SWAP.md)
- [Negative virtual quote reserves](https://github.com/pump-fun/pump-public-docs/blob/639b87fb45c3adeffa6f7c4a1bf0cb85e392bf22/docs/NEGATIVE_VIRTUAL_QUOTE_RESERVES.md)
- [Reserve fee adjustments](https://github.com/pump-fun/pump-public-docs/blob/639b87fb45c3adeffa6f7c4a1bf0cb85e392bf22/docs/VIRTUAL_QUOTE_RESERVES_FEE_ADJUSTMENT.md)
- [Fee sweeps](https://github.com/pump-fun/pump-public-docs/blob/639b87fb45c3adeffa6f7c4a1bf0cb85e392bf22/docs/instructions/SWEEP_FEES.md)
- [Holder rewards](https://github.com/pump-fun/pump-public-docs/blob/639b87fb45c3adeffa6f7c4a1bf0cb85e392bf22/docs/HOLDER_REWARDS_README.md)
- [Curve V3 instructions](https://github.com/pump-fun/pump-public-docs/blob/639b87fb45c3adeffa6f7c4a1bf0cb85e392bf22/docs/instructions/TRADE_V3.md)
- [PumpSwap V2 instructions](https://github.com/pump-fun/pump-public-docs/blob/639b87fb45c3adeffa6f7c4a1bf0cb85e392bf22/docs/instructions/PUMP_SWAP_TRADE_V2.md)
- [Synthetic migration](https://github.com/pump-fun/pump-public-docs/blob/639b87fb45c3adeffa6f7c4a1bf0cb85e392bf22/docs/SYNTHETIC_MIGRATION.md)
