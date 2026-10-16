"""Bounded acquisition evidence for paired additional-period native replays."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).absolute().parents[1]
OUT=ROOT/'var/dnipro/sniper-dev-flat-other-periods-20261015-v1'
ORIGINAL=ROOT/'var/dnipro/sniper-inventory-repair-20261015-v1'
DATA=ROOT.parent/'solana-live-indexer/research/sniper_break_even/data'


def collect_evidence(period,revision):
    from backtest.adapters.source.sniper_silver import SilverSniperSource
    plan=json.loads((OUT/'experiment-plan.json').read_text());source_path=DATA/plan['periods'][period]
    evidence=OUT/('repair-'+period);evidence.mkdir(exist_ok=True)
    shutil.copy2(ORIGINAL/'rpc_cache.py',evidence/'rpc_cache.py')
    helper=(ORIGINAL/'collect_transfer_evidence.py').read_text()
    start="SOURCE=ROOT.parents[3]/'solana-live-indexer/research/sniper_break_even/data/2026-10-10-1600-1700'"
    assert start in helper
    (evidence/'collect_transfer_evidence.py').write_text(helper.replace(start,'SOURCE=Path('+repr(str(source_path))+')'))
    sys.path.insert(0,str(evidence));from collect_transfer_evidence import collect
    source=SilverSniperSource(source_path)
    suffix='' if revision==1 else '-repaired-v'+str(revision-1)
    cases={};opened={}
    for variant in ['control','dev-flat']:
        folder=OUT/(period+'-'+variant+suffix)
        result=json.loads((folder/'result.json').read_text())
        positions={p['mint']:p for p in result['positions'] if p['status']=='OPEN'}
        for mint,p in positions.items():
            old=opened.get(mint)
            if old is None or p['entry_time']>old['entry_time']:opened[mint]=p
        with (folder/'conditional_errors.csv').open() as stream:
            for error in csv.DictReader(stream):
                if error['mint'] not in positions or error['reason']!='CONDITIONAL_HISTORICAL_SELLER_BALANCE_UNPROVEN':continue
                boundary=int(error['boundary_ordinal']);slot=boundary>>32;index=(boundary&((1<<32)-1))-1
                for row in source.trade_rows:
                    if row['mint']==error['mint'] and row['slot']==slot and row['tx_idx']==index and row['side']=='sell':
                        case=dict(token=positions[row['mint']]['symbol'],mint=row['mint'],seller=row['trader'],signature=row['signature'],
                            error_slot=slot,error_tx_index=index,error_boundary=boundary,launch_slot=int(source.birth_by_mint[row['mint']]['slot']))
                        cases[row['mint'],row['trader'],row['signature']]=case
    existing=evidence/('transfers-v'+str(revision-1)+'.json') if revision>1 else evidence/'no-prior-overlay.json'
    prior=json.loads(existing.read_text()) if existing.exists() else []
    # Precompute other missing historical acquisitions inside the same holds so
    # several wallets in one routing chain do not require repeated native runs.
    repair_mints={case['mint'] for case in cases.values()}
    grouped=defaultdict(list);movements=defaultdict(list)
    for row in source.trade_rows:
        if row['mint'] in repair_mints and source.times[row['slot']]<=opened[row['mint']]['entry_time']+360:
            grouped[row['mint'],row['slot'],row['tx_idx']].append(row)
    for m in prior:movements[m['mint'],m['slot'],m['tx_idx']].append(m)
    balances=defaultdict(int);seen=set();extra=[]
    for key in sorted(grouped.keys()|movements.keys(),key=lambda k:(k[1],k[2],k[0])):
        for m in movements[key]:
            balances[m['mint'],m['source_wallet']]-=int(m['tokens']);balances[m['mint'],m['destination_wallet']]+=int(m['tokens'])
        for row in grouped[key]:
            wallet=row['mint'],row['trader'];tokens=int(row['tokens'])
            if row['side']=='sell' and balances[wallet]<tokens and wallet not in seen:
                seen.add(wallet)
                extra.append(dict(token=opened[row['mint']]['symbol'],mint=row['mint'],seller=row['trader'],signature=row['signature'],
                    error_slot=row['slot'],error_tx_index=row['tx_idx'],error_boundary=(row['slot']<<32)+row['tx_idx']+1,
                    launch_slot=int(source.birth_by_mint[row['mint']]['slot'])))
            balances[wallet]+=tokens*(1 if row['side']=='buy' else -1)
    for case in extra:
        if len(cases)>=50:break
        cases[case['mint'],case['seller'],case['signature']]=case
    values=list(cases.values())
    (evidence/f'cases-v{revision}.json').write_text(json.dumps(values,indent=2)+'\n')
    print('EVIDENCE_CASES',period,revision,len(values),'open_tokens',len(opened),'prefetch_candidates',len(extra),flush=True)
    if not values:
        print('NO_REPAIRABLE_ACQUISITION_CASES',period,flush=True);return False
    output=evidence/f'transfers-v{revision}.json';assert not output.exists()
    movements=collect(values,existing,output,evidence/f'findings-v{revision}.json')
    print('VERIFIED_TRANSFER_OVERLAY',period,revision,len(movements),flush=True)
    return True


def replay(period,revision):
    plan=json.loads((OUT/'experiment-plan.json').read_text());source=DATA/plan['periods'][period]
    evidence=OUT/('repair-'+period);overlay=evidence/f'transfers-v{revision}.json'
    assert (evidence/f'findings-v{revision}.json').exists(), 'Evidence collection did not complete'
    assert overlay.exists() and json.loads(overlay.read_text())
    def native(variant):
        name=period+'-'+variant+'-repaired-v'+str(revision);destination=OUT/name;assert not destination.exists()
        prior=OUT/(period+'-'+variant)
        original=json.loads((prior/'result.json').read_text())
        cmd=[str(ROOT/'.venv/bin/python'),'-m','backtest.bootstrap.dnipro','sniper-window',
            '--snapshot',str(source),'--policy',str(OUT/(variant+'-policy.json')),
            '--execution-mode','CONDITIONAL_PROTOCOL_REPLAY','--inventory-transfers',str(overlay),'--out',str(destination)]
        for p in original['positions']:cmd+=['--trace-mint',p['mint']]
        print('NATIVE_START',name,flush=True)
        with (OUT/(name+'.log')).open('x') as log:subprocess.run(cmd,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,check=True)
        manifest=json.loads((destination/'manifest.json').read_text());assert manifest['complete']
        for filename,digest in manifest['files'].items():
            assert hashlib.sha256((destination/filename).read_bytes()).hexdigest()==digest
        current=json.loads((destination/'result.json').read_text());s=current['summary']
        assert s['policy']==json.loads((OUT/(variant+'-policy.json')).read_text())
        print('NATIVE_DONE',name,json.dumps({k:s[k] for k in ['signals','filled_entries','closed_positions','open_positions',
              'realized_net_pnl_sol','open_cash_cost_sol','zero_recovery_stress_pnl_sol']}),flush=True)
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(native,['control','dev-flat']))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('period',choices=['late-oct10','oct11'])
    parser.add_argument('--revision',type=int,default=1);parser.add_argument('--replay-only',action='store_true')
    parser.add_argument('--collect-only',action='store_true');args=parser.parse_args()
    if not args.replay_only and not collect_evidence(args.period,args.revision):return
    if not args.collect_only:replay(args.period,args.revision)


if __name__=='__main__':main()
