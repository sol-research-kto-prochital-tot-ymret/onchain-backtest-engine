"""Reconcile visual markers/valuations against retained native evidence."""
from bisect import bisect_right
from collections import Counter
from pathlib import Path
import json
import os
import pandas as pd

from sniper_interactive_review import NativeReview, ROOT, EXPERIMENT, SOL, sha, compact_steps


def main():
    original=[dict(boundary=i,value=v) for i,v in enumerate([0,0,1,1,1,2,2,0,0])]
    reduced=compact_steps(original,'value');xs=[r['boundary'] for r in reduced]
    assert all(reduced[bisect_right(xs,r['boundary'])-1]['value']==r['value'] for r in original)
    review=NativeReview();control=review.results['control'];variant=review.results['dev-flat']
    before=control['summary']['policy'];after=variant['summary']['policy']
    assert {k:v for k,v in after.items() if k!='require_creator_flat_before_entry'}==before
    assert after['require_creator_flat_before_entry']==1
    saved=json.loads((ROOT/'var/dnipro/sniper-reference-profit-exit-20261015-v1/control-v4/result.json').read_text())
    assert all(control[k]==saved[k] for k in ('positions','fills','ledger'))
    old={p['mint']:p for p in control['positions'] if 'signal_slot' in p}
    new={p['mint']:p for p in variant['positions'] if 'signal_slot' in p}
    assert new.keys()<=old.keys()
    assert all(p['creator_tokens_at_signal']==0 and p['creator_flat_entry_allowed']
               and p['signal_slot']<=p['launch_slot']+2 for p in new.values())
    comparisons=[]
    creator_nonzero_at_fill=[]
    for mint,p in old.items():
        q=review.positions['dev-flat'][mint]
        comparisons.append(dict(mint=mint,token=p['symbol'],gmgn_url=p['gmgn_url'],
            kept=mint in new,control_status=p['status'],control_reason=p['reason'],
            control_pnl_sol=p['closed_pnl_lamports']/1e9 if p['closed_pnl_lamports'] is not None else None,
            new_status=q['status'],new_reason=q['reason'],new_pnl_sol=q['closed_pnl_lamports']/1e9 if q['closed_pnl_lamports'] is not None else None,
            creator_status_at_signal=q.get('creator_status_at_signal'),
            original_signal_slot=p['signal_slot'],new_signal_slot=q.get('signal_slot'),
            original_budget_sol=p['budget_lamports']/1e9,new_budget_sol=q.get('budget_lamports',0)/1e9,
            original_reference=p['sniper_wallet'],new_reference=q.get('sniper_wallet')))
    pd.DataFrame(comparisons).to_csv(EXPERIMENT/'positions_comparison.csv',index=False)
    for mint,p in new.items():
        fill=next(f for f in review.fill_markers if f['run']=='dev-flat' and f['mint']==mint and f['side']=='BUY')
        records=review.inventory('dev-flat',mint)
        index=bisect_right([r['boundary'] for r in records],fill['boundary']-1)-1
        if index>=0 and records[index]['dev_tokens_raw']!=0:
            creator_nonzero_at_fill.append(dict(mint=mint,token=p['symbol'],creator_tokens_raw=records[index]['dev_tokens_raw']))
    receipt=review.export_all()
    chart_index=pd.read_csv(review.export/'chart_index.csv')
    assert chart_index.buy_fills.sum()==48 and chart_index.sell_fills.sum()==46
    checked_failed=0
    for label,result in review.results.items():
        for p in result['positions']:
            if 'signal_slot' not in p:continue
            fig=review.figure(label,p['mint'])
            assert fig.layout.meta['buy_fills']==int(p['entry_cash_lamports']>0)
            assert fig.layout.meta['sell_fills']==int(p['status']=='CLOSED')
            if p['status']=='ENTRY_FAILED':
                assert any(t.name=='Failed/rejected attempt · no fill' for t in fig.data)
                checked_failed+=1
            for trace in fig.data:
                for row in trace.customdata if trace.customdata is not None else []:
                    assert isinstance(row[4],str), 'Raw boundary must not lose precision in browser JSON'
    # Compare the plotted reference-inventory series to the native strategy ledger.
    inventory_checks=0
    for label in review.results:
        for mint in review.wanted:
            records=review.inventory(label,mint);boundaries=[r['boundary'] for r in records]
            for trace in review.traces[label][mint]:
                if trace['action']!='EXIT_EVALUATION':continue
                index=bisect_right(boundaries,trace['boundary_ordinal'])-1
                if index>=0:
                    assert records[index]['reference_tokens_raw']==int(trace['wallet_tokens']), (label,mint)
                    inventory_checks+=1
    # A prior native reference-exit run contains independently recorded valuation
    # values. Check the post-processing quote function against those integers.
    reference=NativeReview(runs={'control':ROOT/'var/dnipro/sniper-reference-profit-exit-20261015-v1/reference-profit-v4'},
                           export=EXPERIMENT/'reference-valuation-qa')
    quote_checks=0
    for mint in reference.wanted:
        expected={r['boundary_ordinal']:int(r['reference_profit_lamports'])
            for r in reference.traces['control'][mint] if r['action']=='REFERENCE_PROFIT_EVALUATION'
            and r.get('reference_valuation_status')=='AVAILABLE'}
        for record in reference.reference_quotes('control',mint):
            if record['boundary'] in expected:
                assert record['net_pnl_lamports']==expected[record['boundary']], (mint,record['boundary'])
                quote_checks+=1
    summary=dict(complete=True,control_exact_positions_fills_ledger_match=True,
        changed_policy_fields=['require_creator_flat_before_entry'],
        results=review.summary().to_dict('records'),retained_signal_count=len(new),
        filtered_signal_count=len(old.keys()-new.keys()),
        creator_status_counts=dict(Counter(p['creator_status_at_signal'] for p in new.values())),
        creator_nonzero_before_buy_fill=creator_nonzero_at_fill,
        native_reference_inventory_checks=inventory_checks,native_reference_quote_checks=quote_checks,
        charts=receipt['charts'],buy_fill_markers=48,sell_fill_markers=46,failed_attempt_markers_checked=checked_failed,
        source_manifest_sha256=review.source_sha256,
        run_result_hashes={k:sha(v/'result.json') for k,v in review.folders.items()})
    (EXPERIMENT/'verification.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2))
    browser=next((Path.home() / 'Library/Caches/ms-playwright').glob('chromium_headless_shell-*/chrome-mac*/headless_shell'), None)
    if browser is not None and browser.exists() and not os.environ.get('BROWSER_PATH'):
        os.environ['BROWSER_PATH']=str(browser)
        for name,label,p in [('profitable','dev-flat',next(p for p in new.values() if p['status']=='CLOSED' and p['closed_pnl_lamports']>0)),
                ('failed','control',next(p for p in old.values() if p['status']=='ENTRY_FAILED')),
                ('unresolved','dev-flat',next(p for p in new.values() if p['status']=='OPEN'))]:
            review.figure(label,p['mint']).write_image(review.export/f'visual-qa-{name}.png',width=1550,height=1750)
        review.strategy_figure().write_image(review.export/'visual-qa-strategy.png',width=1400,height=900)
    print('VISUAL_AND_ACCOUNTING_QA_COMPLETE',flush=True)


if __name__=='__main__':main()
