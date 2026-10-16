"""Frozen candidate development followed by paired before/after validation.

Each run uses the actual native engine CLI. Validation never selects or changes
rules. Results keep source gaps, open exposure and synthetic settlement explicit.
"""
from pathlib import Path
from datetime import datetime, timezone
import subprocess,json,sys,hashlib,csv
from concurrent.futures import ThreadPoolExecutor
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'var/dnipro/sniper-window-redesign-v1'
PLAN=OUT/'experiment_plan.json'
TRAIN=ROOT.parent/'solana-live-indexer/research/sniper_break_even/data/2026-10-10-1600-1700'
TEST=ROOT.parent/'solana-live-indexer/research/sniper_break_even/data/2026-10-11-1000-1100'
PYTHON=ROOT/'.venv/bin/python'

def native(name,source,policy,mode,train=False):
    folder=OUT/name
    cmd=[str(PYTHON),'-m','backtest.bootstrap.dnipro','sniper-window','--snapshot',str(source),'--policy',str(policy),'--execution-mode',mode,'--out',str(folder)]
    if train:cmd+=['--empty-block-evidence',str(ROOT/'var/dnipro/sniper-gap-455328000.json')]
    if folder.exists():
        manifest=json.loads((folder/'manifest.json').read_text())
        assert manifest['complete']
        for filename,digest in manifest['files'].items():
            assert hashlib.sha256((folder/filename).read_bytes()).hexdigest()==digest
        cached=json.loads((folder/'result.json').read_text())
        assert cached['summary']['execution_mode']==mode
        assert cached['summary']['source_manifest_sha256']==hashlib.sha256((source/'manifest.json').read_bytes()).hexdigest()
        for key,value in json.loads(policy.read_text()).items():assert cached['summary']['policy'][key]==value
        print('Verified completed run',name,flush=True)
        return cached
    log=OUT/(name+'.log')
    print('Starting',name,flush=True)
    with log.open('w') as stream:
        subprocess.run(cmd,cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT,check=True)
    doc=json.loads((folder/'result.json').read_text())
    s=doc['summary'];print(name,'net',s['realized_net_pnl_sol'],'open',s['open_positions'],'filled',s['filled_entries'],flush=True)
    return doc

plan=json.loads(PLAN.read_text())
results={}
with ThreadPoolExecutor(max_workers=2) as pool:
    futures={name:pool.submit(native,'development-'+name,TRAIN,OUT/(name+'.json'),'EXOGENOUS_VIRTUAL_SETTLEMENT',True) for name in plan['variants']}
    results={name:future.result() for name,future in futures.items()}
valid=[name for name in results if results[name]['summary']['open_positions']==0 and name!='baseline']
chosen=max(valid,key=lambda n:results[n]['summary']['realized_net_pnl_lamports']) if valid else None
if chosen is None:raise RuntimeError('No complete development candidate; validation selection cannot ignore open exposure')
selection=dict(selected=chosen,selected_at=datetime.now(timezone.utc).isoformat(),experiment_plan_sha256=hashlib.sha256(PLAN.read_bytes()).hexdigest(),
    selection_basis='largest development net PnL among the three fixed revisions with no open positions; no validation results inspected',
    profitable_on_development=results[chosen]['summary']['realized_net_pnl_lamports']>0)
if (OUT/'selection.json').exists():
    previous=json.loads((OUT/'selection.json').read_text())
    assert previous['selected']==chosen and previous['experiment_plan_sha256']==selection['experiment_plan_sha256']
else:
    (OUT/'selection.json').write_text(json.dumps(selection,indent=2)+'\n')
# Every pair has identical data and execution scenario; only strategy policy differs.
for period,source,training in [('development',TRAIN,True),('validation',TEST,False)]:
    modes=[('strict','EXOGENOUS_REPLAY',0),('conditional_fixed','CONDITIONAL_PROTOCOL_REPLAY',0),('conditional_fraction','CONDITIONAL_PROTOCOL_REPLAY',1)]
    if period=='validation':modes.insert(0,('virtual','EXOGENOUS_VIRTUAL_SETTLEMENT',0))
    for scenario,mode,fraction in modes:
        tasks=[]
        for label,variant in [('before','baseline'),('after',chosen)]:
            values=dict(plan['variants'][variant]);values['counterpart_sell_fraction']=fraction
            policy=OUT/(period+'-'+scenario+'-'+label+'-policy.json');policy.write_text(json.dumps(values,indent=2)+'\n')
            tasks.append((period+'-'+scenario+'-'+label,source,policy,mode,training))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(native,*args) for args in tasks]
            for future in futures:future.result()
# Prespecified cost/latency stresses apply to the selected revision, same validation input.
for name,extra in [('double_network',{'entry_network_fee_lamports':210000,'exit_network_fee_lamports':210000}),('two_slot_latency',{'entry_delay_slots':2,'exit_delay_slots':2})]:
    values=dict(plan['variants'][chosen]);values.update(extra)
    policy=OUT/('validation-'+name+'-policy.json');policy.write_text(json.dumps(values,indent=2)+'\n')
    native('validation-'+name,TEST,policy,'EXOGENOUS_VIRTUAL_SETTLEMENT')
rows=[]
for folder in sorted(OUT.iterdir()):
    if folder.is_dir() and (folder/'manifest.json').exists():
        s=json.loads((folder/'result.json').read_text())['summary']
        rows.append(dict(run=folder.name,execution_mode=s['execution_mode'],net_closed_pnl_SOL=s['realized_net_pnl_sol'],
            filled=s['filled_entries'],closed=s['closed_positions'],open=s['open_positions'],open_cost_SOL=s['open_cash_cost_sol'],
            zero_recovery_stress_pnl_SOL=s['zero_recovery_stress_pnl_sol'],synthetic_shortfall_SOL=s['synthetic_shortfall_sol'],
            source_gaps=len(s['publication_gaps']),conditional_errors=s['conditional_repricing_errors'],
            source_input_sha256=s['source_input_sha256']))
with (OUT/'before_after.csv').open('w',newline='') as stream:
    writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
print('All planned before/after runs completed.',flush=True)
