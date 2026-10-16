"""One additional paired period; frozen strategy policies are unchanged."""
from pathlib import Path
import subprocess,json,sys
from concurrent.futures import ThreadPoolExecutor
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'var/dnipro/sniper-window-redesign-v1'
SOURCE=ROOT.parent/'solana-live-indexer/research/sniper_break_even/data/2026-10-10-1730-1830'
selected=json.loads((OUT/'selection.json').read_text())['selected']
policies=json.loads((OUT/'experiment_plan.json').read_text())['variants']

def run(args):
    scenario,mode,fraction,label=args
    values=dict(policies['baseline' if label=='before' else selected]);values['counterpart_sell_fraction']=fraction
    policy=OUT/('additional-'+scenario+'-'+label+'-policy.json');policy.write_text(json.dumps(values,indent=2)+'\n')
    name='additional-'+scenario+'-'+label
    cmd=[str(ROOT/'.venv/bin/python'),'-m','backtest.bootstrap.dnipro','sniper-window','--snapshot',str(SOURCE),'--policy',str(policy),'--execution-mode',mode,'--out',str(OUT/name)]
    print('Starting',name,flush=True)
    with (OUT/(name+'.log')).open('w') as stream:subprocess.run(cmd,cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT,check=True)
    s=json.loads((OUT/name/'result.json').read_text())['summary']
    print(name,'net',s['realized_net_pnl_sol'],'filled',s['filled_entries'],'open',s['open_positions'],flush=True)

tasks=[(scenario,mode,fraction,label) for scenario,mode,fraction in [('virtual','EXOGENOUS_VIRTUAL_SETTLEMENT',0),('strict','EXOGENOUS_REPLAY',0),('conditional_fraction','CONDITIONAL_PROTOCOL_REPLAY',1),('conditional_fixed','CONDITIONAL_PROTOCOL_REPLAY',0)] for label in ['before','after']]
with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(run,tasks))
