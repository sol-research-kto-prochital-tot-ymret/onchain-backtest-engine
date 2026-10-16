"""Paired native evaluation on the two previously used independent windows."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime,timezone
from pathlib import Path
import gzip
import hashlib
import json
import subprocess
import time

ROOT=Path(__file__).absolute().parents[1]
OUT=ROOT/'var/dnipro/sniper-dev-flat-other-periods-20261015-v1'
DATA=ROOT.parent/'solana-live-indexer/research/sniper_break_even/data'


def replay(period,variant):
    plan=json.loads((OUT/'experiment-plan.json').read_text())
    source=DATA/plan['periods'][period]
    name=period+'-'+variant;destination=OUT/name
    assert not destination.exists(),name
    cmd=[str(ROOT/'.venv/bin/python'),'-m','backtest.bootstrap.dnipro','sniper-window',
        '--snapshot',str(source),'--policy',str(OUT/f'{variant}-policy.json'),
        '--execution-mode','CONDITIONAL_PROTOCOL_REPLAY','--out',str(destination)]
    with gzip.open(source/'snapshot/launches.jsonl.gz','rt') as stream:
        for line in stream:cmd+=['--trace-mint',json.loads(line)['mint']]
    began=time.monotonic();print('START',name,flush=True)
    with (OUT/(name+'.log')).open('x') as log:
        subprocess.run(cmd,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,check=True)
    manifest=json.loads((destination/'manifest.json').read_text());assert manifest['complete']
    for filename,digest in manifest['files'].items():
        assert hashlib.sha256((destination/filename).read_bytes()).hexdigest()==digest,(name,filename)
    result=json.loads((destination/'result.json').read_text());s=result['summary']
    requested=json.loads((OUT/f'{variant}-policy.json').read_text())
    assert s['policy']==requested and s['cash_reconciliation_verified']
    assert s['source_manifest_sha256']==hashlib.sha256((source/'manifest.json').read_bytes()).hexdigest()
    if variant=='dev-flat':
        for p in result['positions']:
            if 'signal_slot' in p:
                assert p['creator_flat_entry_allowed'] and p['creator_tokens_at_signal']==0
                assert p['signal_slot']<=p['launch_slot']+2
    receipt=dict(complete=True,period=period,variant=variant,seconds=round(time.monotonic()-began,3),
        observed_at=datetime.now(timezone.utc).isoformat(),result_sha256=hashlib.sha256((destination/'result.json').read_bytes()).hexdigest(),
        manifest_sha256=hashlib.sha256((destination/'manifest.json').read_bytes()).hexdigest(),summary=s)
    (OUT/(name+'-receipt.json')).write_text(json.dumps(receipt,indent=2)+'\n')
    print('DONE',name,json.dumps({k:s[k] for k in ['signals','filled_entries','closed_positions','open_positions',
          'realized_net_pnl_sol','open_cash_cost_sol','zero_recovery_stress_pnl_sol']}),flush=True)
    return name


def main():
    plan=json.loads((OUT/'experiment-plan.json').read_text())
    tasks=[(period,variant) for period in plan['periods'] for variant in ['dev-flat','control']]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(replay,*task) for task in tasks]
        for future in as_completed(futures):future.result()
    print('ALL_FOUR_NATIVE_REPLAYS_COMPLETE',flush=True)


if __name__=='__main__':main()
