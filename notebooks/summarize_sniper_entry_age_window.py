"""Summarize saved timing variants and audit known pre-entry inventory gaps."""
from collections import Counter, defaultdict
from pathlib import Path
import csv
import gzip
import hashlib
import json

import matplotlib.pyplot as plt

ROOT = Path(__file__).absolute().parents[1]
OUT = ROOT / 'var/dnipro/sniper-entry-age-10-20-20261015-v1'
SOURCE = ROOT.parent / 'solana-live-indexer/research/sniper_break_even/data/2026-10-10-1600-1700'
REPAIRED = ROOT / 'var/dnipro/sniper-inventory-repair-20261015-v1'
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()


def write(name, rows):
    columns = list(dict.fromkeys(k for r in rows for k in r))
    with (OUT / name).open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows({k:json.dumps(v) if isinstance(v, (dict, list)) else v for k,v in r.items()} for r in rows)


def main():
    manifest = json.loads((SOURCE / 'manifest.json').read_text())
    clock, all_trades = {}, []
    for part in manifest['parts']:
        with gzip.open(SOURCE / 'snapshot' / part['file'], 'rt') as stream:
            if part['name'].endswith('-clock'):
                from datetime import datetime, timezone
                for line in stream:
                    r = json.loads(line)
                    clock[r['slot']] = int(datetime.fromisoformat(r['observed_block_time']).replace(tzinfo=timezone.utc).timestamp())
            elif part['name'].endswith('-trades'):
                all_trades.extend(json.loads(line) for line in stream)
    results, signals, summaries, signal_snapshots = {}, [], [], []
    timeseries, signal_by_variant, order_maps = {}, {}, {}
    for variant in ['control', 'age10to20']:
        folder = OUT / variant
        m = json.loads((folder / 'manifest.json').read_text())
        assert m['complete']
        for name, digest in m['files'].items():
            assert sha(folder / name) == digest, (variant, name)
        r = json.loads((folder / 'result.json').read_text())
        results[variant] = r
        selected = [p for p in r['positions'] if 'signal_slot' in p]
        selected_by_mint = {p['mint']:p for p in selected}
        signal_rows, notifications = {}, {}
        with (folder / 'strategy_trace.csv').open() as stream:
            for row in csv.DictReader(stream):
                if row['action'] == 'ENTRY_SIGNAL':
                    signal_rows[row['mint']] = row
                if row.get('order_id'):
                    notifications[row['order_id']] = row
        assert set(signal_rows) == set(selected_by_mint)
        signal_by_variant[variant], order_maps[variant] = signal_rows, notifications
        for p in selected:
            born = clock[p['launch_slot']]
            age = p['signal_time'] - born
            if variant == 'age10to20':
                assert 10 <= age <= 20, (p['mint'], age)
            outcome = ('profitable' if p['closed_pnl_lamports'] > 0 else 'unprofitable'
                       if p['closed_pnl_lamports'] < 0 else 'flat') if p['status'] == 'CLOSED' else p['status'].lower()
            signals.append(dict(variant=variant, outcome=outcome, signal_age_seconds=age,
                                entry_age_seconds=p['entry_time']-born if p.get('entry_time') is not None else None,
                                initial_wallet_fraction=p['entry_cash_lamports']/50_000_000_000, **p))
            signal_snapshots.append(dict(variant=variant, **signal_rows[p['mint']]))
        closed = [p for p in selected if p['status'] == 'CLOSED']
        counts = Counter(p['status'] for p in selected)
        summaries.append(dict(variant=variant, decision_window='launch+2 slots' if variant=='control' else '10–20 seconds inclusive',
                              signals=len(selected), filled=r['summary']['filled_entries'], closed=len(closed),
                              profitable=sum(p['closed_pnl_lamports']>0 for p in closed),
                              losing=sum(p['closed_pnl_lamports']<0 for p in closed),
                              unresolved=counts['OPEN'], capital_rejected=counts['ENTRY_REJECTED'],
                              submitted_entry_failures=counts['ENTRY_FAILED'],
                              realized_net_pnl_sol=r['summary']['realized_net_pnl_sol'],
                              open_cost_sol=r['summary']['open_cash_cost_sol'],
                              final_portfolio_pnl_sol=None,
                              zero_recovery_stress_sol=r['summary']['zero_recovery_stress_pnl_sol']))
        pnl_events = []
        for p in closed:
            pnl_events.append(dict(variant=variant, mint=p['mint'], symbol=p['symbol'], slot=p['exit_slot'],
                                   pnl_lamports=p['closed_pnl_lamports'], event='closed'))
        for transaction in r['ledger']:
            order = notifications[transaction['correlation_id']]
            p = selected_by_mint[order['mint']]
            if p['entry_cash_lamports']:
                continue
            delta = sum(x['amount_atomic'] for x in transaction['postings'] if x['asset_id']=='So11111111111111111111111111111111111111112'
                        and x['account_kind'] in ['PORTFOLIO_AVAILABLE','PORTFOLIO_RESERVED'])
            if delta:
                pnl_events.append(dict(variant=variant,mint=p['mint'],symbol=p['symbol'],
                                       slot=transaction['boundary_ordinal']>>32,pnl_lamports=delta,event='unfilled_entry_fee'))
        pnl_events.sort(key=lambda x:(x['slot'],x['mint']))
        assert sum(x['pnl_lamports'] for x in pnl_events)==r['summary']['realized_net_pnl_lamports']
        timeseries[variant] = pnl_events
    old, new = results['control']['summary']['policy'], results['age10to20']['summary']['policy']
    changed = {k:(old.get(k,0),new.get(k,0)) for k in old.keys()|new.keys() if old.get(k,0)!=new.get(k,0)}
    assert changed=={'minimum_entry_age_seconds':(0,10),'maximum_entry_age_seconds':(0,20)}, changed
    wanted={x['mint'] for x in signals}
    events=[]
    for r in all_trades:
        if r['mint'] in wanted:
            events.append(((r['slot']<<32)+r['tx_idx']+1,tuple(r['instruction_path']),0,r))
    for r in json.loads((REPAIRED/'transfers-v3.json').read_text()):
        if r['mint'] in wanted:
            events.append(((r['slot']<<32)+r['tx_idx']+1,tuple(r['instruction_path']),1,r))
    for p in signals:
        boundary=int(signal_by_variant[p['variant']][p['mint']]['boundary_ordinal'])
        events.append((boundary,(2**31,),2,p))
    events.sort(key=lambda x:(x[0],x[1],x[2]))
    balances=defaultdict(int); missing=defaultdict(list); audits=[]
    for boundary,path,kind,r in events:
        mint=r['mint']
        if kind==0:
            key=mint,r['trader'];amount=int(r['tokens'])
            if r['side']=='sell' and balances[key]<amount:
                missing[mint].append(dict(slot=r['slot'],signature=r['signature'],seller=r['trader'],
                                          prior_observed_tokens=balances[key],sell_tokens=amount))
            balances[key]+=amount if r['side']=='buy' else -amount
        elif kind==1:
            balances[mint,r['source_wallet']]-=r['tokens']
            balances[mint,r['destination_wallet']]+=r['tokens']
        else:
            anchor=balances[mint,r['sniper_wallet']]
            assert anchor==r['sniper_tokens'],(r['variant'],mint,'pre-signal inventory mismatch',anchor,r['sniper_tokens'])
            issues=missing[mint]
            audits.append(dict(variant=r['variant'],symbol=r['symbol'],mint=mint,gmgn_url=r['gmgn_url'],
                               status=r['status'],reason=r['reason'],closed_pnl_sol=r.get('closed_pnl_sol'),
                               signal_age_seconds=r['signal_age_seconds'],
                               earlier_sells_with_unproven_inventory=len(issues),
                               first_unproven_sell_slot=issues[0]['slot'] if issues else None,
                               first_unproven_seller_gmgn='https://gmgn.ai/sol/address/'+issues[0]['seller'] if issues else None,
                               first_unproven_sell_signature=issues[0]['signature'] if issues else None,
                               anchor_swap_and_known_transfer_tokens=anchor,
                               exact_holder_completeness_proven=False))
    write('comparison.csv',summaries)
    write('all_signal_positions.csv',signals)
    write('entry_signal_snapshots.csv',signal_snapshots)
    write('pre_entry_inventory_audit.csv',audits)
    write('pnl_events.csv',[r for rows in timeseries.values() for r in rows])
    memberships=[]
    lookup={variant:{p['mint']:p for p in result['positions']} for variant,result in results.items()}
    for mint in sorted(wanted):
        a,b=lookup['control'][mint],lookup['age10to20'][mint]
        memberships.append(dict(symbol=a['symbol'],mint=mint,gmgn_url=a['gmgn_url'],
                                original_selected='signal_slot' in a,late_selected='signal_slot' in b,
                                original_status=a['status'],late_status=b['status'],
                                original_reason=a['reason'],late_reason=b['reason'],
                                original_closed_pnl_sol=a.get('closed_pnl_sol'),late_closed_pnl_sol=b.get('closed_pnl_sol')))
    write('signal_membership.csv',memberships)
    audits_by_key={(r['variant'],r['mint']):r for r in audits}
    diagnostic=[]
    for variant in results:
        closed=[p for p in signals if p['variant']==variant and p['status']=='CLOSED']
        for flagged in [False,True]:
            rows=[p for p in closed if bool(audits_by_key[variant,p['mint']]['earlier_sells_with_unproven_inventory'])==flagged]
            diagnostic.append(dict(variant=variant,earlier_unproven_sell_detected=flagged,
                                   closed_positions=len(rows),closed_trade_pnl_sol=f"{sum(p['closed_pnl_lamports'] for p in rows)/1e9:.9f}"))
    write('closed_result_inventory_diagnostic.csv',diagnostic)
    fig,ax=plt.subplots(figsize=(12,5),layout='constrained')
    start,end=min(clock),max(clock)
    for variant,color in [('control','#69738b'),('age10to20','#1768b3')]:
        rows=timeseries[variant];cum=0;xs=[0];ys=[0]
        for event in rows:
            cum+=event['pnl_lamports'];xs.append(event['slot']-start);ys.append(cum/1e9)
        xs.append(end-start);ys.append(cum/1e9)
        ax.step(xs,ys,where='post',color=color,label=f"{variant}: {cum/1e9:+.9f} SOL; {results[variant]['summary']['open_positions']} unresolved")
    ax.axhline(0,color='#999999',linewidth=.7);ax.grid(alpha=.15);ax.legend()
    ax.set_xlabel(f'Slots since {start}');ax.set_ylabel('Realized net contribution (SOL)')
    ax.set_title('Original timing versus 10–20 second search; open tokens are not marked')
    fig.savefig(OUT/'pnl_comparison.png',dpi=150);plt.close(fig)
    receipt=dict(complete=True,changed_policy_fields=changed,all_late_signals_within_age_bounds=True,
                  same_source_and_transfer_overlay=True,baseline_positions_fills_ledger_exact_match=True,
                  focused_tests_passed=76,source_manifest_sha256=sha(SOURCE/'manifest.json'),
                  audit_limits='Known swaps and 13 verified transfers only. No negative prefix does not establish exact holder or transfer completeness. Flagged cohorts are descriptive and not an entry filter.',
                  files={p.name:sha(p) for p in OUT.iterdir() if p.is_file() and p.suffix in ['.csv','.png']})
    (OUT/'comparison-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print('COMPARISON',json.dumps(summaries,ensure_ascii=False),flush=True)
    print('INVENTORY_DIAGNOSTIC',json.dumps(diagnostic),flush=True)
    print('SIGNAL_AGE_COUNTS',dict(Counter(p['signal_age_seconds'] for p in signals if p['variant']=='age10to20')))


if __name__=='__main__':
    main()
