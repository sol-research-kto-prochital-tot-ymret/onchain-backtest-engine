"""Only add a creator-flat gate to the original two-slot native entry policy."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import hashlib
import json
import subprocess
import time

ROOT = Path(__file__).absolute().parents[1]
OUT = ROOT/'var/dnipro/sniper-dev-flat-20261015-v1'
PREVIOUS = ROOT/'var/dnipro/sniper-reference-profit-exit-20261015-v1'
SOURCE = ROOT.parent/'solana-live-indexer/research/sniper_break_even/data/2026-10-10-1600-1700'


def main():
    parser=argparse.ArgumentParser();parser.add_argument('variant',choices=['control','dev-flat'])
    variant=parser.parse_args().variant
    saved=json.loads((PREVIOUS/'control-v4/result.json').read_text())
    policy=dict(saved['summary']['policy'])
    assert policy.get('own_take_profit_enabled',1)==1 and not policy.get('reference_profit_exit_enabled',0)
    if variant=='dev-flat':policy['require_creator_flat_before_entry']=1
    policy_path=OUT/f'{variant}-policy.json'
    with policy_path.open('x') as stream:json.dump(policy,stream,indent=2)
    destination=OUT/variant;assert not destination.exists()
    cmd=[str(ROOT/'.venv/bin/python'),'-m','backtest.bootstrap.dnipro','sniper-window',
        '--snapshot',str(SOURCE),'--policy',str(policy_path),'--execution-mode','CONDITIONAL_PROTOCOL_REPLAY',
        '--empty-block-evidence',str(ROOT/'var/dnipro/sniper-gap-455328000.json'),
        '--inventory-transfers',str(PREVIOUS/'transfers-v4.json'),'--out',str(destination)]
    for p in saved['positions']:cmd+=['--trace-mint',p['mint']]
    started=time.monotonic();print('START',variant,flush=True)
    with (OUT/f'{variant}.log').open('x') as log:
        subprocess.run(cmd,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,check=True)
    manifest=json.loads((destination/'manifest.json').read_text());assert manifest['complete']
    for filename,digest in manifest['files'].items():
        assert hashlib.sha256((destination/filename).read_bytes()).hexdigest()==digest
    result=json.loads((destination/'result.json').read_text())
    if variant=='control':
        assert all(result[k]==saved[k] for k in ('positions','fills','ledger'))
        print('EXACT_CONTROL_POSITIONS_FILLS_LEDGER_MATCH',flush=True)
    else:
        for p in result['positions']:
            if 'signal_slot' in p:
                assert p['creator_flat_entry_allowed'] and p['creator_tokens_at_signal']==0
                assert p['signal_slot']<=p['launch_slot']+2
    receipt={'complete':True,'variant':variant,'seconds':round(time.monotonic()-started,3),
        'observed_at':datetime.now(timezone.utc).isoformat(),
        'result_sha256':hashlib.sha256((destination/'result.json').read_bytes()).hexdigest(),
        'summary':result['summary']}
    (OUT/f'{variant}-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps({k:result['summary'][k] for k in ('signals','filled_entries','closed_positions',
        'open_positions','realized_net_pnl_sol','open_cash_cost_sol')}),flush=True)


if __name__=='__main__':main()
