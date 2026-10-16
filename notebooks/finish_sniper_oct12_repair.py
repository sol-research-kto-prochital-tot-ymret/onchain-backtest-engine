"""Verify final native replays, publish selected research, and retain a checked copy."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import csv
import json
import shutil
import subprocess
import sys
import time

from sniper_oct12_repair import OUT, BASE, ENGINE, save, file_hash, write_csv

VAULT=ENGINE.parent/'research-brain'
NOTE=Path('Experiments/2026-10-15-october-12-native-backtest-input-repair-recovers-169-missing-swaps-4aedbd78.md')
DEST=Path('/Volumes/KINGSTON/solana-research')/OUT.name


def finish():
    receipt=OUT/'24h-full-cohort-repaired-v10-receipt.json'
    while not receipt.exists():time.sleep(5)
    with (OUT/'final-summary.log').open('w') as log:
        subprocess.run([sys.executable,str(ENGINE/'notebooks/summarize_sniper_oct12_repair.py'),'--revision','10'],cwd=ENGINE,stdout=log,stderr=subprocess.STDOUT,check=True)
    validation=json.loads((OUT/'validation.json').read_text());assert validation['complete'] and validation['overlap_prefix_equal']
    assert all(not item['previously_closed_changed'] for item in validation['runs'])
    plan=json.loads((OUT/'repair-plan.json').read_text());assert file_hash(ENGINE/'notebooks/sniper_oct12_repair.py')==plan['adapter_sha256']
    assert all(file_hash(OUT/name)==digest for name,digest in plan['data'].items())
    ordering=json.loads((OUT/'independent-block-order-verification.json').read_text());assert ordering['complete']
    assert all(file_hash(OUT/name)==digest for name,digest in ordering['input_hashes'].items())
    summaries=list(csv.DictReader((OUT/'comparison.csv').open()))
    by_hours={6:json.loads((OUT/'6h-full-cohort-repaired-v10/result.json').read_text()),24:json.loads((OUT/'24h-full-cohort-repaired-v10/result.json').read_text())}
    for expected in json.loads((OUT/'additive-repair-prediction.json').read_text()):
        actual=by_hours[expected['hours']]['summary']
        assert actual['realized_net_pnl_lamports']==expected['realized_net_pnl_lamports']
        assert actual['closed_positions']==expected['closed'] and actual['open_positions']==expected['open']
    full=by_hours[24];opened={p['mint']:p for p in full['positions'] if p['status']=='OPEN'}
    movements=json.loads((OUT/'verified-transfers.json').read_text());conflicts=[]
    for row in csv.DictReader((OUT/'24h-full-cohort-repaired-v10-inventory-conflicts.csv').open()):
        if row['mint'] not in opened:continue
        assert row['reason']=='CONDITIONAL_TRANSFER_SOURCE_INVENTORY_CONFLICT'
        assert int(row['historical_source_tokens_atomic'])>=int(row['required_tokens_atomic'])>int(row['simulated_source_tokens_atomic'])
        movement=next(m for m in movements if m['mint']==row['mint'] and m['signature']==row['signature'] and m['source_wallet']==row['source_wallet'] and m['instruction_path']==json.loads(row['instruction_path']))
        position=opened[row['mint']];scale=10**movement['decimals']
        conflicts.append(dict(symbol=position['symbol'],mint=row['mint'],gmgn_url=position['gmgn_url'],entry_cost_sol=position['entry_cash_sol'],source_wallet=row['source_wallet'],source_gmgn='https://gmgn.ai/sol/address/'+row['source_wallet'],destination_wallet=row['destination_wallet'],destination_gmgn='https://gmgn.ai/sol/address/'+row['destination_wallet'],required_tokens=str(Decimal(row['required_tokens_atomic'])/scale),simulated_available_tokens=str(Decimal(row['simulated_source_tokens_atomic'])/scale),shortfall_tokens=str(Decimal(row['shortfall_tokens_atomic'])/scale),actual_source_pre_tokens=str(Decimal(movement['source_pre_tokens'])/scale),actual_source_post_tokens=str(Decimal(movement['source_post_tokens'])/scale),slot=row['slot'],tx_idx=row['tx_idx'],signature=row['signature'],evidence_file=movement['evidence_file'],evidence_sha256=movement['evidence_sha256']))
    assert len(conflicts)==len(opened)==5
    write_csv(OUT/'unresolved_inventory_conflicts.csv',conflicts)
    swaps=json.loads((OUT/'missing-swaps-summary.json').read_text());assert swaps['missing_swaps_within_original_day']==169
    original=json.loads((BASE/'full24h-wallet10000/result.json').read_text());old={p['mint']:p for p in original['positions']};entry_changes=[]
    for position in full['positions']:
        prior=old[position['mint']]
        if prior.get('entry_cash_lamports')!=position.get('entry_cash_lamports'):
            entry_changes.append(dict(symbol=position['symbol'],mint=position['mint'],gmgn_url=position['gmgn_url'],original_entry_cost_sol=prior['entry_cash_sol'],repaired_entry_cost_sol=position['entry_cash_sol'],original_tracked_buyer=prior.get('sniper_wallet'),original_tracked_buyer_gmgn=prior.get('sniper_gmgn_url'),repaired_tracked_buyer=position.get('sniper_wallet'),repaired_tracked_buyer_gmgn=position.get('sniper_gmgn_url')))
    assert len(entry_changes)==3
    write_csv(OUT/'entry_sizing_changes.csv',entry_changes)
    save(OUT/'repaired-control-reference.json',dict(period='2026-10-12T00:00:00Z/2026-10-12T06:00:00Z',bankroll_sol=1000,data_repair_revision=10,original_reference=str(BASE/'fixed-control-reference.json'),native_manifest_sha256=file_hash(OUT/'6h-full-cohort-repaired-v10/manifest.json'),policy=by_hours[6]['summary']['policy'],source_input_sha256=by_hours[6]['summary']['source_input_sha256']))
    prefix='../../onchain-backtest-engine/var/dnipro/'+OUT.name+'/'
    sources=[prefix+p for p in ('comparison.csv','repaired_cases.csv','missing-swaps-summary.json','missing_swaps.csv','entry_sizing_changes.csv','unresolved_inventory_conflicts.csv','validation.json','independent-block-order-verification.json','repair-plan.json','evidence-v10.json')]
    now=datetime.now(timezone.utc).isoformat();note=VAULT/NOTE;front=note.read_text().split('---',2)[1].strip().splitlines()
    front=[line.replace('status: "draft"','status: "observed"') for line in front if not line.startswith(('observed_at:','sources:'))]
    front+=['observed_at: '+json.dumps(now),'sources: '+json.dumps(sources)]
    body='''# October 12 native backtest input repair recovers 169 missing swaps

The repaired input revision recovers **169 missing Pump.fun swaps across 21 tokens: 90 buys and 79 sells**, plus **161 verified external token transfers**. Two additional swaps after midnight are follow-up data beyond the original cache boundary, not omissions from the requested day. This is a count of recovered facts for the 78 previously unresolved token histories, not a full-chain missing-data estimate.

## Scope and unchanged rules

The fixed control remains October 12, 2026, 00:00–06:00 UTC with a 1,000 SOL wallet and data through 06:15. The full run covers 00:00–24:00 UTC with a continuous 10,000 SOL wallet and follow-up through October 13 00:15. The full day contains the control; these are overlapping samples.

The original V0 sizing and developer-flat gate remain: include developers who never bought, use the first two slots, one-slot execution, own +5% take profit, five-minute fallback and 5% slippage. Reference-buyer profitability exit stays disabled. Creator-fee accounting and real-reserve-capped sells remain unchanged. Native engine source is still `f93a8de0259cfd5de2fea152df072ad80a09aa72adb5bedbfebbcc2766df62f0`; all original frozen runs and caches are preserved.

## Native results

| Period | Wallet SOL | Filled | Closed | Open | Realized net SOL | Open committed cost SOL |
|---|---:|---:|---:|---:|---:|---:|
'''
    for hours in (6,24):
        s=by_hours[hours]['summary'];body+=f"| {hours} hours | {s['policy']['bankroll_lamports']//10**9} | {s['filled_entries']} | {s['closed_positions']} | {s['open_positions']} | {s['realized_net_pnl_sol']} | {s['open_cash_cost_sol']} |\n"
    body+='''
All 28 previously open control positions now close. Of the full day's 78 originally open positions, 73 close and five remain model conflicts. Previously closed positions retain their original economic outcomes, and the repaired six-hour outcomes match the overlapping full-day prefix. Realized contribution includes submitted failed-entry costs; open positions are not counted as realized losses. Risk rejections and submitted execution failures remain separate CSV categories.

## Evidence and repair method

Finalized parsed RPC transactions prove emitted trade quantities, fees, reserve states, token-account owners and wallet transfers. Pool-settlement transfers are excluded from the external-transfer overlay. Added pool legs are deduplicated by signature and instruction path; an economic-identity audit found no duplicate cached swaps. All 171 new swap transactions, including the two follow-up swaps, have successful transaction metadata and complete logs without program-failure messages. Decoder parity matched 392 existing cached trades on amounts, reserves, fees, actors and paths. Finalized block signature lists separately verify the indexes of **289 evidence transactions across 214 blocks**.

Acquisition collection prioritizes balance-changing cached transactions and token-balance probes, then stops only when the reconstructed wallet inventory matches the evidenced pre-balance. These probes choose fetch order; they do not seed tokens or certify unqueried wallet history. Historical reserve continuity is checked through modeled exits, or entry plus 360 seconds for still-open cases. All known material discontinuities in that scope are repaired. Three quote-only one-lamport residuals remain explicitly recorded; no trade quantity or fee is invented to remove them.

The original native cache remains `/Volumes/KINGSTON/solana-research/cache-2026-10-12`, and the original compact projection remains `/Volumes/KINGSTON/solana-research/sniper-oct12-projection-20261015-v1`. New facts are a separate hashed overlay. Production ClickHouse tables and collection services were not changed by this local repair.

## Three corrected entry sizes

Restoring missing trades and gifts changes the strategy inputs, not its parameters. The before/after CSV records these changes:

| Token | Original entry cost SOL | Repaired entry cost SOL |
|---|---:|---:|
'''
    for item in entry_changes:body+=f"| [{item['symbol']}]({item['gmgn_url']}) | {item['original_entry_cost_sol']} | {item['repaired_entry_cost_sol']} |\n"
    body+='''
Both cases previously labelled `CURVE_COMPLETE_OR_MIGRATED`, mexicans and LUCKY, had earlier acquisition blockers that later lifecycle events overwrote in the final reason. Repairing those acquisitions lets the unchanged strategy exit before migration. No PumpSwap execution model was added.

## Five remaining counterfactual conflicts

'''
    for item in conflicts:body+=f"- [{item['symbol']}]({item['gmgn_url']}): transfer requires {item['required_tokens']} tokens; the simulated sender holds {item['simulated_available_tokens']}.\n"
    body+='''
The original-world source inventory is proven sufficient. Our simulated intervention leaves the donor with fewer tokens than the historical fixed transfer requires. This is not evidence that our sell was submitted and failed. More acquisition data does not establish how the other bot would resize or retry its transaction. Numeric closure needs a separately labelled counterparty-failure or full-balance-transfer behavior model; neither was silently enabled. Raw inventory errors occurring after a position has already exited are excluded from its unresolved classification.

## Reproduction and retained outputs

Run from `onchain-backtest-engine` with its `.venv/bin/python`. The source and policies are retained in `replay-source`, `implementation-source-final`, the original baseline folder and the native receipts. To repeat without overwriting results, copy `evidence-v10.json` to a fresh evidence revision number, keep the input hashes unchanged, and invoke `notebooks/sniper_oct12_repair.py replay --hours 6` or `--hours 24` with that new `--revision`. The runner refuses an existing result destination.

`comparison.csv` holds both cohort totals. `positions.csv`, `profitable_positions.csv`, `losing_positions.csv` and `unresolved_positions.csv` separate outcomes. `repaired_cases.csv` compares the original open cases, `entry_sizing_changes.csv` records changed signals, `missing_swaps.csv` lists the 169 recovered swaps, and `unresolved_inventory_conflicts.csv` contains the five donor shortfalls and GMGN links. Native result folders retain fills, ledger, order audit and strategy traces.

All output manifests, policy equality, integer cash reconciliation, creator-fee conservation and zero synthetic sell funding pass. The preserved input capabilities still do not certify global chain coverage, full wallet history, unseen round trips or exact future bot behavior.

## Related experiments

[[Experiments/2026-10-15-october-12-unresolved-pilot-resolves-rayls-and-proves-two-missing-acqu-cc9ceae7]]

[[Experiments/2026-10-15-october-12-six-hour-control-and-full-day-developer-flat-native-replay-cb5399e9]]
'''
    note.write_text('---\n'+'\n'.join(front)+'\n---\n\n'+body)
    result=subprocess.run([sys.executable,'scripts/brain.py','publish',str(NOTE),'--message','Publish October 12 missing swap repairs and native replay results'],cwd=VAULT,text=True,capture_output=True)
    save(OUT/'note-publication.json',dict(returncode=result.returncode,note_path=str(note),stdout=result.stdout,stderr=result.stderr))
    if result.returncode:raise RuntimeError('shared note publication failed; see note-publication.json')
    assert Path('/Volumes/KINGSTON').is_mount()
    skip={'COMMITTED','delivery-manifest.json','job-status.json','finalization.log'}
    files={str(p.relative_to(OUT)):dict(sha256=file_hash(p),bytes=p.stat().st_size) for p in OUT.rglob('*') if p.is_file() and p.name not in skip and '__pycache__' not in p.parts and 'sqd' not in p.relative_to(OUT).parts and '.partial' not in str(p.relative_to(OUT))}
    save(OUT/'delivery-manifest.json',dict(complete=True,scope='Selected repair artifacts and evidence; original chain caches remain separately retained',files=files))
    save(OUT/'COMMITTED',dict(delivery_manifest_sha256=file_hash(OUT/'delivery-manifest.json')))
    stage=DEST.with_name(DEST.name+'.partial');stage.mkdir(exist_ok=True)
    if DEST.exists():raise RuntimeError('refuse to replace an existing KINGSTON repair copy')
    subprocess.run(['rsync','-a','--exclude=__pycache__','--exclude=sqd','--exclude=*.partial*','--exclude=job-status.json','--exclude=finalization.log',str(OUT)+'/',str(stage)+'/'],check=True)
    for relative,item in files.items():assert file_hash(stage/relative)==item['sha256'],relative
    assert file_hash(stage/'delivery-manifest.json')==file_hash(OUT/'delivery-manifest.json')
    stage.rename(DEST)
    status=dict(phase='complete',complete=True,repair_revision=10,known_missing_swaps=169,verified_transfers=161,original_unresolved_resolved=73,remaining_model_cases=5,kingston_copy=str(DEST),shared_note=str(note),summaries=summaries)
    save(OUT/'job-status.json',status);save(DEST/'job-status.json',status)
    print('REPAIR_FINISHED',json.dumps(status),flush=True)


if __name__=='__main__':
    try:finish()
    except Exception as error:
        save(OUT/'job-status.json',dict(phase='finalization_failed',complete=False,error=str(error)))
        raise
