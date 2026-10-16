"""Publish only the selected shared-brain note and retain a verified audit copy."""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys

from audit_sniper_oct12_fees import OUT, ROOT
from prepare_sniper_oct12_review import save, sha

VAULT = ROOT.parent/'research-brain'
NOTE = 'Findings/2026-10-16-sniper-strategy-fees-execution-assumptions-and-october-12-ledger-total-4514d88e.md'


def main():
    audit = json.loads((OUT/'audit.json').read_text())
    assert audit['complete']
    a, b = audit['summaries']
    assert a['total_fee_lamports']==32634196786 and b['total_fee_lamports']==22874895395
    note = VAULT/NOTE
    fields = {line.split(':',1)[0]:json.loads(line.split(':',1)[1])
              for line in note.read_text().split('---',2)[1].strip().splitlines()}
    prefix = '../../onchain-backtest-engine/var/dnipro/'+OUT.name+'/'
    fields.update(status='observed', observed_at=datetime.now(timezone.utc).isoformat(), sources=[
        prefix+'original-sniper-break-even-v0.md', prefix+'audit.json', prefix+'fee_totals.csv',
        prefix+'fees_by_order.csv', prefix+'fees_by_position_status.csv', prefix+'buyback-evidence.json',
        prefix+'implementation/docs/sniper-window.md',
        prefix+'implementation/src/backtest/plugins/execution/pump_sniper.py',
        prefix+'implementation/src/backtest/plugins/strategies/sniper_window.py',
        prefix+'implementation/src/backtest/plugins/strategies/creator_income.py',
        prefix+'implementation/src/backtest/plugins/protocols/pumpfun/model.py'])
    body = '''# Sniper strategy fees execution assumptions and October 12 ledger totals

The latest full-day replay **without dev-sell-all** paid **32.634196786 SOL in protocol, creator and transaction fees**. A further **0.163142400 SOL is refundable rent still locked in open positions**, not a realized fee. These amounts are already in the native PnL or open cost and must not be deducted again. The audit covers October 12, 2026, 00:00–24:00 UTC launches, the saved follow-up through October 13 00:15, and a 10,000 SOL starting wallet.

## What the original strategy already included

The supplied `sniper_break_even_v0_corrected.md` includes aggregate buy/sell trading fees in `f_buy`, `f_sell`, entry costs and liquidation proceeds. It also models the effect of our own buy and sell on the AMM price. Protocol/creator fees and AMM price impact therefore are **not new penalties invented outside V0**.

The paragraph after the liquidation inequalities explicitly leaves priority fees and other fixed expenses out of the algebra and says implementation should add them. The implementation chooses particular transaction/rent values; those values were not calibrated by the document. The paper illustrates a 5% net return and requires a positive epsilon, but does not specify our exact two-slot/1-SOL detector, five-minute timeout, slippage tolerance, retries or creator-income adjustment.

## Exact ledger charges

Only the hypothetical trading wallet's payments are counted below. This is not total market-wide creator revenue, observed developers' profit, or a live provider bill. Rates in all 1,218 filled orders are consistent with **95 bps protocol plus 30 bps creator** (0.95% + 0.30% of the quote model's curve/fee basis), with integer rounding. The executor uses the last retained event's fee rates; historical fee-tier configuration and hypothetical tier transitions are not independently reconstructed.

| Component | Without dev-sell-all, SOL | With dev-sell-all, SOL |
|---|---:|---:|
| Protocol trading fees | 24.684284246 | 17.318766106 |
| Creator/developer trading fees | 7.795037540 | 5.469084289 |
| Network/priority charges on successful attempts | 0.127890000 | 0.078645000 |
| Network/priority charges on failed attempts | 0.026985000 | 0.008400000 |
| **Total charged fees** | **32.634196786** | **22.874895395** |
| Jito/relay tips | 0.000000000 | 0.000000000 |
| Rent posted on filled entries | 1.323492720 | 0.768808560 |
| Rent refunded on closed positions | 1.160350320 | 0.758612160 |
| **Rent still locked, separate from fees** | **0.163142400** | **0.010196400** |

**Transaction charge:** 105,000 lamports = **0.000105 SOL per submitted simulated attempt**, on both entry and exit. This is one configured combined network/priority allowance. The ledger does not establish separate base-versus-priority amounts, compute-unit price/limit, or the fees needed for a real landing target. Do not add another priority fee on top of it as if none were included.

The no-gate run has **1,218 successful attempts** (649 buys and 569 sells) plus **257 paid failures**: 238 failed buys cost 0.024990000 SOL and 19 failed sells cost 0.001995000 SOL. Together, 1,475 × 0.000105 = **0.154875000 SOL** in transaction charges. A failed entry releases its buy principal and rent reservation and pays only the transaction charge. A failed sell releases the reserved tokens and pays its transaction charge; the rent from the successful entry stays locked until a successful full exit. Pump trading fees are not charged on either failed attempt. Pre-submission quote rejection, a capital risk rejection, or an unproven-data blocker is not automatically a paid on-chain attempt; the saved ledger decides whether a fee was charged. There were zero capital risk rejections in these two full-day runs.

**Rent:** 0.002039280 SOL is allocated for one token account per filled entry. The model refunds the same amount with a successful full exit; unresolved positions retain it. Actual account sizing and account-creation/closure mechanics are not independently reconstructed, and no separate closing transaction fee is added. This is an execution assumption, not proof that every live token account has that cost/refund behavior.

**Buyback:** the model has protocol and creator trading-fee buckets, not a third additional buyback charge. Two retained finalized buy transactions show `curve SOL + protocol fee + creator fee` already equals the parsed outgoing trading transfers, despite a positive `buyback_fee` field. In those examples the buyback field describes a routed portion of protocol fees; adding it again would double count. The local receipt retains both transaction bodies, request receipts, amounts and GMGN links. This evidence concerns the supported ordinary Pump.fun route, not every launchpad or token mode.

## Where fees appear in the reported PnL

| No-gate outcome bucket | Tokens | Fees charged, SOL | Treatment |
|---|---:|---:|---|
| Closed positions | 569 | 30.721922670 | Already included in closed net outcomes |
| Failed entries | 238 | 0.024990000 | Already deducted from realized net |
| Open/unresolved positions | 80 | 1.887284116 | Already included in open committed cost |
| Risk-rejected entries | 0 | 0.000000000 | No submitted fee |

Realized net remains **−109.602394368 SOL** and open committed cost remains **152.319150420 SOL**. All-time fees include payments on open positions, so adding 32.634196786 SOL to realized PnL would not even produce a comparable gross closed result. Removing fees would also change thresholds, sizing, fills and exit timing; an actual zero-fee strategy result requires a separately labelled replay.

The cash identity independently reconciles:

```text
229.124205602 SOL net cash sent to curve reserves
+32.634196786 SOL total fees
+ 0.163142400 SOL net locked rent
=261.921544788 SOL decrease in available + reserved cash
=109.602394368 SOL realized loss + 152.319150420 SOL open committed cost
```

## Implementation choices beyond the paper

| Choice | Saved setting or behavior | Meaning |
|---|---|---|
| Detector | At least 1 SOL cumulative early trading buy cost; eligible through launch slot + 2 | Numerical definition of the paper's “large early sniper” |
| Target and epsilon | +5% own net return; 0.001 SOL reserve threshold buffer | Concrete values for V0 variables; epsilon is not a fee |
| Slippage guard | 5% minimum-output tolerance on entry and exit | Rejects insufficient output; it does **not** deduct a flat 5% from every trade |
| Landing latency | One-slot delay on both sides; zero observation delay | Conditional execution timing, not a charged fee; within-slot ordering is assumed |
| Timeout | 300 seconds after entry | An exit trigger absent from the mathematical V0 algorithm |
| Failed exits | At most three exit submissions | Submitted failures can consume the configured transaction fee |
| Creator income credit | Enabled; accrued recipient income reduces a tracked wallet's unrecovered trade cost | Extra economic adjustment to the reference buyer, not a refund to our trader |
| Tracked wallet cost | Known buys/fees minus known net sales, with known transaction fees when the wallet is payer | Accounts for observed partial sales; wallet inventory/acquisitions remain coverage-limited |
| Historical network fee allocation | Once per mint/signature when the tracked wallet is fee payer | Separate from our fixed 0.000105 scenario; do not sum token ledgers into exact whole-wallet spending across multi-token transactions |
| Capital | 10,000 SOL with shared portfolio reservations and risk checks | A bankroll constraint, not an expense |
| Own price impact and other traders | Our reserve impact is propagated; observed counterpart buys retain supported intent/budget and sells use observed inventory fractions | Conditional replay assumption; other bots' real reactions/slippage are unknown |
| Real liquidity and migration | Real-reserve-capped sells, no synthetic SOL; pre-migration ordinary SOL-quoted Pump curves | Physical execution constraint; unsupported modes/migration do not receive invented fills |
| Dev-sell-all gate | Disabled in the current run; enabled only in the comparison | Additional filter outside V0; the gated version also allows developers who never bought |
| Quote arithmetic | Integer lamports, component fee ceiling and native quote rounding | Embedded execution effects, not a separately charged rounding fee |

Creator-fee income is a causal event-time vault receivable, not proven withdrawal cash. Recipient/share evidence is required; future fees are not credited before a signal, and claim events are not credited twice. Our own creator-fee payments remain expenses and are not rebated into our portfolio. All 887 selected reference buyers in the no-gate run had **zero creator-income credit at their signal**; the with-gate selections also had zero. This does not prove that disabling the credit would preserve every candidate or signal, because rejected/unselected candidates can differ.

**Not separately measured or charged:** adaptive live priority bidding, nonzero Jito/relay tips, router/platform/referral subscriptions or service fees, extra token/wrapped-SOL account setup beyond the fixed rent allowance, creator withdrawal/distribution costs, and server/RPC/feed infrastructure bills. Unsupported cashback/Mayhem/holder-reward modes are excluded rather than assigned ordinary-curve economics. These omissions are model scope, not a claim that live operation makes them free.

## Evidence and reproduction

The fee audit reads the frozen native `result.json` ledger and validates every native manifest/file hash, the original engine identity, unique ledger transaction IDs, asset conservation, per-order cash identities and final cash/PnL/open-cost reconciliation. Exact order hashes map every charged failure to its token and side. Both runs share the same input; only the dev-flat gate differs. `fee_totals.csv`, `fees_by_order.csv` and `fees_by_position_status.csv` retain integer lamports and exact nine-decimal SOL strings. Fee sums come from ledger postings, not `fills.csv`'s fee field: that field does not contain SOL sell fees.

The original strategy text, relevant implementation files, audit program and two finalized buyback examples are archived in `onchain-backtest-engine/var/dnipro/sniper-oct12-fee-audit-20261016-v1`. The original backtest artifacts and notebook remain unchanged. Reproduction uses `.venv/bin/python notebooks/audit_sniper_oct12_fees.py`; choose a fresh `OUT` directory before repeating so the completed audit is preserved. No backtest or production service is run by the audit.

[[Experiments/2026-10-15-october-12-full-day-sniper-backtest-without-the-developer-sellout-gate-a55fd5a2]]

[[Experiments/2026-10-14-sniper-window-creator-fee-income-adjustment-on-october-10-2026-fa9d4e58]]
'''
    note.write_text('---\n'+'\n'.join(f'{key}: {json.dumps(value, ensure_ascii=False)}' for key,value in fields.items())+'\n---\n\n'+body)
    publication = subprocess.run([sys.executable, 'scripts/brain.py', 'publish', NOTE,
        '--message', 'Record sniper execution costs and full-day fee reconciliation'], cwd=VAULT,
        text=True, capture_output=True)
    save(OUT/'note-publication.json', dict(note=str(note), note_sha256=sha(note), returncode=publication.returncode,
        stdout=publication.stdout, stderr=publication.stderr))
    assert publication.returncode==0, publication.stderr
    shutil.copy2(Path(__file__), OUT/Path(__file__).name)
    files = {str(p.relative_to(OUT)):dict(sha256=sha(p), bytes=p.stat().st_size)
             for p in sorted(OUT.rglob('*')) if p.is_file() and p.name not in ('delivery-manifest.json','COMMITTED')}
    save(OUT/'delivery-manifest.json', dict(complete=True, files=files))
    save(OUT/'COMMITTED', dict(delivery_manifest_sha256=sha(OUT/'delivery-manifest.json')))
    backup = Path('/Volumes/KINGSTON/solana-research')/OUT.name
    stage = backup.with_name(backup.name+'.partial')
    assert backup.parent.is_dir() and not backup.exists() and not stage.exists()
    subprocess.run(['/usr/bin/rsync', '-a', str(OUT)+'/', str(stage)+'/'], check=True)
    for name, facts in files.items():
        assert sha(stage/name)==facts['sha256'], name
    assert sha(stage/'delivery-manifest.json')==sha(OUT/'delivery-manifest.json')
    assert sha(stage/'COMMITTED')==sha(OUT/'COMMITTED')
    stage.rename(backup)
    print(json.dumps(dict(complete=True, note=str(note), backup=str(backup), verified_files=len(files)), indent=2))


if __name__=='__main__':
    main()
