"""Verify and summarize the fixed Oct12 control and overlapping full day."""
from collections import Counter
from decimal import Decimal
from pathlib import Path
import csv
import json
from sniper_carbon_day import OUT, file_hash, save, utc

RUNS={'control-6h-wallet1000':1000,'full24h-wallet10000':10000}


def csv_file(path, rows):
    assert rows
    with path.open('w', newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)


def main():
    summaries=[];positions=[];evidence=[]
    base=None;results={};implementation=None;source_manifest=None
    for name,wallet in RUNS.items():
        folder=OUT/name
        m=json.loads((folder/'manifest.json').read_text());assert m['complete']
        for filename,digest in m['files'].items():assert file_hash(folder/filename)==digest,(name,filename)
        r=json.loads((folder/'result.json').read_text());s=r['summary'];results[name]=r
        if implementation is None:
            implementation=s['implementation'];source_manifest=s['source_manifest_sha256']
        else:
            assert s['implementation']==implementation
            assert s['source_manifest_sha256']==source_manifest
        policy=json.loads((OUT/(name+'-policy.json')).read_text());assert s['policy']==policy
        assert policy['bankroll_lamports']==wallet*10**9
        comparable={k:v for k,v in policy.items() if k!='bankroll_lamports'}
        if base is None:base=comparable
        else:assert comparable==base
        assert s['cash_reconciliation_verified'] and s['creator_fee_allocation_conservation_verified'] and s['synthetic_liquidity_reconciliation_verified']
        closed=[p for p in r['positions'] if p['status']=='CLOSED'];opened=[p for p in r['positions'] if p['status']=='OPEN']
        assert len(closed)==s['closed_positions'] and len(opened)==s['open_positions']
        realized=sum(p['closed_pnl_lamports'] for p in closed)-s['failed_entry_cost_lamports']
        committed=sum(p['entry_cash_lamports']+p['failed_execution_cost_lamports'] for p in opened)
        assert realized==s['realized_net_pnl_lamports'] and committed==s['open_cash_cost_lamports']
        assert int(Decimal(s['final_available_sol'])*10**9)==wallet*10**9+realized-committed
        for p in r['positions']:
            if 'signal_slot' not in p:continue
            assert p['signal_slot']<=p['launch_slot']+2
            assert p['creator_flat_entry_allowed'] and p['creator_tokens_at_signal']==0
            positions.append(dict(run=name,symbol=p['symbol'],mint=p['mint'],gmgn_url=p['gmgn_url'],status=p['status'],reason=p['reason'],launch_slot=p['launch_slot'],signal_slot=p['signal_slot'],entry_slot=p.get('entry_slot',''),entry_utc=utc(p['entry_time']) if 'entry_time' in p else '',exit_slot=p.get('exit_slot',''),exit_utc=utc(p['exit_time']) if 'exit_time' in p else '',entry_cash_sol=p['entry_cash_sol'],closed_pnl_sol=p.get('closed_pnl_sol',''),failed_execution_cost_sol=p['failed_execution_cost_sol'],remaining_tokens_atomic=p['remaining_tokens_atomic'],creator=p['creator'],creator_gmgn_url='https://gmgn.ai/sol/address/'+p['creator'],creator_status_at_signal=p.get('creator_status_at_signal',''),tracked_buyer=p.get('sniper_wallet',''),tracked_buyer_gmgn=p.get('sniper_gmgn_url','')))
        summaries.append(dict(run=name,start_utc=s['cohort_start_utc'],end_utc=s['cohort_end_utc'],data_end_utc=s['data_end_utc'],starting_wallet_sol=wallet,launches=s['cohort_launches'],retained_trades=s['retained_input_trade_events'],signals=s['signals'],fills=s['filled_entries'],closed=len(closed),wins=sum(p['closed_pnl_lamports']>0 for p in closed),losses=sum(p['closed_pnl_lamports']<0 for p in closed),open=len(opened),failed_entries=s['status_counts'].get('ENTRY_FAILED',0),risk_rejected_orders=s['engine_summary']['rejected_order_count'],realized_net_pnl_sol=s['realized_net_pnl_sol'],final_net_pnl_sol=s['realized_net_pnl_sol'] if not opened else '',open_cash_cost_sol=s['open_cash_cost_sol'],zero_recovery_stress_pnl_sol=s['zero_recovery_stress_pnl_sol'],open_reasons=json.dumps(dict(Counter(p['reason'] for p in opened)),sort_keys=True),exit_reasons=json.dumps(dict(Counter(p['reason'] for p in closed)),sort_keys=True),seconds=json.loads((OUT/(name+'-receipt.json')).read_text())['seconds'],right_edge_followup_complete=s['right_edge_followup_complete'],source_input_sha256=s['source_input_sha256']))
        evidence.append(dict(run=name,result_sha256=file_hash(folder/'result.json'),manifest_sha256=file_hash(folder/'manifest.json'),integer_cash_verified=True,policy_verified=True))
    csv_file(OUT/'comparison.csv',summaries);csv_file(OUT/'positions.csv',positions)
    for label,filter_fn in [('profitable',lambda p:p['status']=='CLOSED' and Decimal(p['closed_pnl_sol'])>0),('losing',lambda p:p['status']=='CLOSED' and Decimal(p['closed_pnl_sol'])<0),('unresolved',lambda p:p['status']=='OPEN'),('failed_entries',lambda p:p['status']=='ENTRY_FAILED')]:
        subset=[p for p in positions if filter_fn(p)]
        if subset:csv_file(OUT/(label+'_positions.csv'),subset)
    # Check the overlapping first six hours without claiming the full day is an
    # independent holdout. The different bankrolls may change capital admission.
    control=results['control-6h-wallet1000'];day=results['full24h-wallet10000']
    by_mint={p['mint']:p for p in day['positions']}
    fields=['signal_slot','budget_lamports','entry_slot','entry_cash_lamports']
    discrepancies=[];closed_discrepancies=[]
    for p in control['positions']:
        q=by_mint[p['mint']]
        changed={k:[p.get(k),q.get(k)] for k in fields if p.get(k)!=q.get(k)}
        if changed:discrepancies.append(dict(mint=p['mint'],gmgn_url=p['gmgn_url'],changed=changed))
        if p['status']=='CLOSED':
            changed={k:[p.get(k),q.get(k)] for k in ['status','exit_slot','exit_receipt_lamports','closed_pnl_lamports'] if p.get(k)!=q.get(k)}
            if changed:closed_discrepancies.append(dict(mint=p['mint'],gmgn_url=p['gmgn_url'],changed=changed))
    save(OUT/'comparison-verification.json',dict(complete=True,runs=evidence,policy_differs_only_by_bankroll=True,matched_projection_manifest=True,matched_native_implementation=True,overlap_prefix_entry_discrepancies=discrepancies,overlap_control_closed_result_discrepancies=closed_discrepancies,scope='Full day contains control; not independent samples',source_coverage='Retained decoded archive; canonical chain coverage not certified',open_positions_are_not_realized_losses=True))
    save(OUT/'fixed-control-reference.json',dict(period='2026-10-12T00:00:00Z/2026-10-12T06:00:00Z',bankroll_sol=1000,baseline_run='control-6h-wallet1000',manifest_sha256=evidence[0]['manifest_sha256'],source_input_sha256=control['summary']['source_input_sha256'],policy=control['summary']['policy'],future_comparisons='Preserve baseline; use fresh output directories for variants'))
    print(json.dumps(summaries,indent=2))


if __name__=='__main__':main()
