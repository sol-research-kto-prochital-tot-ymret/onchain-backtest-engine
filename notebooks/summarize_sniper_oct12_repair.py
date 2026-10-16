"""Validate recovery evidence and compare repaired cohorts without conflating PnL."""
from collections import Counter, defaultdict
from decimal import Decimal
import csv
import json
from pathlib import Path

from sniper_oct12_repair import OUT, BASE, START, ENGINE, load_helper, decode_trades, original_rows
from sniper_carbon_day import save, utc, file_hash
from backtest.bootstrap.sniper_window import write_csv


def evidence_summary():
    helper=load_helper();births=json.loads((OUT/'births.json').read_text())
    values={}
    for filename in ('recovered-trades.json','tail-trades.json'):
        for row in json.loads((OUT/filename).read_text()):
            identity=row['signature'],tuple(row['instruction_path'])
            if identity in values:assert values[identity]==row
            values[identity]=row
    known={(r['signature'],tuple(r['instruction_path'])):r for r in original_rows() if r['kind']=='trade'}
    checked={};exports=[]
    for identity,row in values.items():
        assert identity not in known
        path=OUT/row['rpc_evidence_file'];receipt=json.loads(path.with_suffix('.receipt.json').read_text())
        assert receipt['status']==200 and receipt['request']['params'][1]['commitment']=='finalized'
        assert file_hash(path)==receipt['response_sha256']==row['rpc_evidence_sha256']
        if row['signature'] not in checked:
            tx=json.loads(path.read_text())['result']
            checked[row['signature']]={(r['signature'],tuple(r['instruction_path'])):r for r in decode_trades(tx,helper,births)}
        assert checked[row['signature']][identity]==row
        transaction=json.loads(path.read_text())['result']
        decimals={b['uiTokenAmount']['decimals'] for name in ('preTokenBalances','postTokenBalances')
                  for b in transaction['meta'][name] if b['mint']==row['mint']}
        assert len(decimals)==1
        precision=next(iter(decimals))
        exports.append(dict(symbol=births[row['mint']]['symbol'],mint=row['mint'],gmgn_url='https://gmgn.ai/sol/token/'+row['mint'],
            slot=row['slot'],tx_idx=row['tx_idx'],instruction_path=json.dumps(row['instruction_path']),signature=row['signature'],
            time_utc=utc(row['block_time']),side=row['side'],trader=row['trader'],trader_gmgn='https://gmgn.ai/sol/address/'+row['trader'],
            tokens_atomic=row['tokens'],decimals=precision,tokens=str(Decimal(row['tokens'])/(10**precision)),
            quote_lamports=row['quote'],quote_sol=format(Decimal(row['quote'])/10**9,'.9f'),
            category='missing_within_original_day' if row['block_time']<START+86400 else 'new_followup_after_original_cache_end',
            evidence_file=str(path),evidence_sha256=row['rpc_evidence_sha256']))
    write_csv(OUT/'recovered_swaps.csv',exports)
    missing=[r for r in exports if r['category']=='missing_within_original_day']
    tail=[r for r in exports if r['category']!='missing_within_original_day']
    write_csv(OUT/'missing_swaps.csv',missing)
    summary=dict(complete=True,missing_swaps_within_original_day=len(missing),missing_swap_transactions=len({r['signature'] for r in missing}),
                 affected_tokens=len({r['mint'] for r in missing}),missing_buys=sum(r['side']=='buy' for r in missing),
                 missing_sells=sum(r['side']=='sell' for r in missing),new_followup_swaps_after_midnight=len(tail),
                 verified_external_transfers=len(json.loads((OUT/'verified-transfers.json').read_text())),
                 per_token=[dict(symbol=births[m]['symbol'],mint=m,gmgn_url='https://gmgn.ai/sol/token/'+m,missing_swaps=n)
                            for m,n in Counter(r['mint'] for r in missing).most_common()],
                 scope='Recovered transaction and acquisition histories of the 78 originally open tokens; not a complete full-day chain audit.')
    save(OUT/'missing-swaps-summary.json',summary)
    print('MISSING_SWAPS',json.dumps(summary),flush=True)
    return summary


def results_summary(revision=1):
    comparisons=[];changes=[];positions=[];verified=[];results={}
    for hours in (6,24):
        name=f'{hours}h-full-cohort-repaired-v{revision}';folder=OUT/name
        prior_name='control-6h-wallet1000' if hours==6 else 'full24h-wallet10000'
        baseline=json.loads((BASE/prior_name/'result.json').read_text())
        current=json.loads((folder/'result.json').read_text());results[hours]=current
        manifest=json.loads((folder/'manifest.json').read_text());assert manifest['complete']
        for filename,digest in manifest['files'].items():assert file_hash(folder/filename)==digest
        summary=current['summary'];assert summary['policy']==baseline['summary']['policy']
        assert summary['implementation']==baseline['summary']['implementation']
        assert summary['cash_reconciliation_verified'] and summary['creator_fee_allocation_conservation_verified']
        assert summary['synthetic_liquidity_reconciliation_verified'] and summary['synthetic_shortfall_lamports']==0
        lookup={p['mint']:p for p in baseline['positions']};new_lookup={p['mint']:p for p in current['positions']}
        first_errors={}
        for error in csv.DictReader((folder/'conditional_errors.csv').open()):
            position=new_lookup[error['mint']]
            if position['status']=='CLOSED' and int(error['boundary_ordinal'])>>32>=position['exit_slot']:continue
            first_errors.setdefault(error['mint'],error)
        original_open=[p for p in baseline['positions'] if p['status']=='OPEN']
        resolutions=Counter(new_lookup[p['mint']]['status'] for p in original_open)
        for old in original_open:
            new=new_lookup[old['mint']];first=first_errors.get(old['mint'],{})
            changes.append(dict(run=name,symbol=new['symbol'],mint=new['mint'],gmgn_url=new['gmgn_url'],
                 original_reason=old['reason'],repaired_status=new['status'],repaired_reason=new['reason'],
                 original_entry_cash_sol=old['entry_cash_sol'],original_entry_slot=old.get('entry_slot',''),
                 original_tracked_buyer=old.get('sniper_wallet',''),repaired_tracked_buyer=new.get('sniper_wallet',''),
                 first_causal_blocker=first.get('reason',''),blocker_boundary=first.get('boundary_ordinal',''),
                 entry_cash_sol=new['entry_cash_sol'],closed_pnl_sol=new.get('closed_pnl_sol',''),
                 entry_slot=new.get('entry_slot',''),exit_slot=new.get('exit_slot',''),
                 entry_utc=utc(new['entry_time']) if 'entry_time' in new else '',exit_utc=utc(new['exit_time']) if 'exit_time' in new else ''))
        closed=[p for p in current['positions'] if p['status']=='CLOSED'];opened=[p for p in current['positions'] if p['status']=='OPEN']
        pnl=sum(p['closed_pnl_lamports'] for p in closed)-summary['failed_entry_cost_lamports']
        cost=sum(p['entry_cash_lamports']+p['failed_execution_cost_lamports'] for p in opened)
        assert pnl==summary['realized_net_pnl_lamports'] and cost==summary['open_cash_cost_lamports']
        assert int(Decimal(summary['final_available_sol'])*10**9)==summary['policy']['bankroll_lamports']+pnl-cost
        previously_closed_changes=[]
        for p in baseline['positions']:
            q=new_lookup[p['mint']]
            keys=('status','reason','entry_slot','entry_cash_lamports','exit_slot','closed_pnl_lamports')
            if p['status']=='CLOSED' and any(p.get(k)!=q.get(k) for k in keys):previously_closed_changes.append(p['mint'])
        for p in current['positions']:
            if 'signal_slot' not in p:continue
            positions.append(dict(run=name,symbol=p['symbol'],mint=p['mint'],gmgn_url=p['gmgn_url'],status=p['status'],reason=p['reason'],
                first_causal_blocker=first_errors.get(p['mint'],{}).get('reason',''),entry_slot=p.get('entry_slot',''),exit_slot=p.get('exit_slot',''),
                entry_utc=utc(p['entry_time']) if 'entry_time' in p else '',exit_utc=utc(p['exit_time']) if 'exit_time' in p else '',
                entry_cash_sol=p['entry_cash_sol'],closed_pnl_sol=p.get('closed_pnl_sol',''),failed_execution_cost_sol=p['failed_execution_cost_sol'],
                remaining_tokens_atomic=p['remaining_tokens_atomic']))
        comparisons.append(dict(run=name,wallet_sol=summary['policy']['bankroll_lamports']//10**9,signals=summary['signals'],filled=summary['filled_entries'],
              closed=len(closed),wins=sum(p['closed_pnl_lamports']>0 for p in closed),losses=sum(p['closed_pnl_lamports']<0 for p in closed),
              original_open=len(original_open),original_open_now_closed=resolutions.get('CLOSED',0),open=len(opened),
              failed_entries=summary['status_counts'].get('ENTRY_FAILED',0),capital_rejections=summary['engine_summary']['rejected_order_count'],
              original_realized_net_pnl_sol=baseline['summary']['realized_net_pnl_sol'],realized_net_pnl_sol=summary['realized_net_pnl_sol'],
              open_cash_cost_sol=summary['open_cash_cost_sol'],final_portfolio_pnl_sol=summary['realized_net_pnl_sol'] if not opened else '',
              open_reasons=json.dumps(dict(Counter(p['reason'] for p in opened))),previously_closed_changed=len(previously_closed_changes)))
        verified.append(dict(run=name,manifest_sha256=file_hash(folder/'manifest.json'),policy_unchanged=True,engine_source_unchanged=True,
                             cash_verified=True,previously_closed_changed=previously_closed_changes))
    fields=('status','signal_slot','entry_slot','entry_cash_lamports','exit_slot','closed_pnl_lamports')
    full={p['mint']:p for p in results[24]['positions']}
    discrepancies=[p['mint'] for p in results[6]['positions'] if any(p.get(k)!=full[p['mint']].get(k) for k in fields)]
    reason_diagnostics=[dict(mint=p['mint'],status=p['status'],control_reason=p['reason'],full_day_reason=full[p['mint']]['reason'])
                        for p in results[6]['positions'] if p['reason']!=full[p['mint']]['reason']]
    # A longer replay can observe a later reserve warning on a token that never
    # signaled. Preserve those diagnostics without treating them as changed trades.
    assert all(p['status']=='NO_SIGNAL' for p in reason_diagnostics),reason_diagnostics
    write_csv(OUT/'comparison.csv',comparisons);write_csv(OUT/'repaired_cases.csv',changes);write_csv(OUT/'positions.csv',positions)
    write_csv(OUT/'unresolved_positions.csv',[p for p in positions if p['status']=='OPEN'])
    write_csv(OUT/'profitable_positions.csv',[p for p in positions if p['status']=='CLOSED' and Decimal(p['closed_pnl_sol'])>0])
    write_csv(OUT/'losing_positions.csv',[p for p in positions if p['status']=='CLOSED' and Decimal(p['closed_pnl_sol'])<0])
    save(OUT/'validation.json',dict(complete=True,runs=verified,overlap_prefix_equal=not discrepancies,
         overlap_prefix_discrepancies=discrepancies,original_control_preserved=True,
         overlap_later_no_signal_reason_diagnostics=reason_diagnostics,
         global_chain_coverage_certified=False,open_positions_not_counted_as_realized_losses=True))
    print(json.dumps(comparisons,indent=2))


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--evidence-only',action='store_true');parser.add_argument('--revision',type=int,default=1)
    args=parser.parse_args();evidence_summary()
    if not args.evidence_only:results_summary(args.revision)
