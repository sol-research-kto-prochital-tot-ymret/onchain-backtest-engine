"""Audit completed native outcomes and isolate the changed opportunity set."""
from collections import Counter
from decimal import Decimal
import csv
import json

from sniper_oct12_no_dev_flat import BASE, CONTROL, OUT, RUN_NAME, verify_result
from sniper_carbon_day import save
from backtest.bootstrap.sniper_window import sol_string, write_csv


def main():
    baseline, variant = verify_result(CONTROL), verify_result(OUT / RUN_NAME)
    old = {p['mint']: p for p in baseline['positions']}
    new = {p['mint']: p for p in variant['positions']}
    assert old.keys() == new.keys()
    repaired = set(json.loads((BASE / 'births.json').read_text()))
    diagnostics = []
    for mint, q in old.items():
        p = new[mint]
        if 'signal_slot' not in q and 'signal_slot' not in p:
            continue
        if 'signal_slot' not in q:
            group = 'new_signal_without_dev_flat'
        elif 'signal_slot' not in p:
            group = 'dev_flat_signal_only'
        elif all(q.get(k) == p.get(k) for k in ('signal_slot', 'sniper_wallet', 'budget_lamports')):
            group = 'same_signal'
        else:
            group = 'changed_entry_signal'
        diagnostics.append(dict(mint=mint, symbol=p['symbol'], gmgn_url=p['gmgn_url'], group=group,
            dev_flat_status=q['status'], no_dev_flat_status=p['status'],
            dev_flat_signal_slot=q.get('signal_slot', ''), no_dev_flat_signal_slot=p.get('signal_slot', ''),
            dev_flat_entry_slot=q.get('entry_slot', ''), no_dev_flat_entry_slot=p.get('entry_slot', ''),
            dev_flat_entry_cost_sol=q['entry_cash_sol'], no_dev_flat_entry_cost_sol=p['entry_cash_sol'],
            dev_flat_pnl_sol=q.get('closed_pnl_sol', ''), no_dev_flat_pnl_sol=p.get('closed_pnl_sol', ''),
            dev_flat_tracked_wallet=q.get('sniper_wallet', ''), no_dev_flat_tracked_wallet=p.get('sniper_wallet', ''),
            no_dev_flat_anchor_is_creator=p.get('sniper_is_launch_creator', False),
            original_repair_token=mint in repaired))
    write_csv(OUT / 'signal_comparison.csv', diagnostics)
    groups = []
    for group in sorted({p['group'] for p in diagnostics}):
        members = [p for p in diagnostics if p['group'] == group]
        row = dict(group=group, tokens=len(members))
        for prefix, positions in [('dev_flat', old), ('no_dev_flat', new)]:
            selected = [positions[p['mint']] for p in members]
            closed = [p for p in selected if p['status'] == 'CLOSED']
            opened = [p for p in selected if p['status'] == 'OPEN']
            failed_fees = sum(p['failed_execution_cost_lamports'] for p in selected if not p['entry_cash_lamports'])
            row.update({prefix+'_signals': sum('signal_slot' in p for p in selected),
                prefix+'_closed': len(closed), prefix+'_open': len(opened),
                prefix+'_wins': sum(p['closed_pnl_lamports']>0 for p in closed),
                prefix+'_losses': sum(p['closed_pnl_lamports']<0 for p in closed),
                prefix+'_realized_net_sol': sol_string(sum(p['closed_pnl_lamports'] for p in closed)-failed_fees),
                prefix+'_open_committed_cost_sol': sol_string(sum(p['entry_cash_lamports']+p['failed_execution_cost_lamports'] for p in opened))})
        groups.append(row)
    write_csv(OUT / 'opportunity_groups.csv', groups)
    for prefix, result in [('dev_flat', baseline), ('no_dev_flat', variant)]:
        assert sum(int(Decimal(row[prefix+'_realized_net_sol'])*10**9) for row in groups) == result['summary']['realized_net_pnl_lamports']
        assert sum(int(Decimal(row[prefix+'_open_committed_cost_sol'])*10**9) for row in groups) == result['summary']['open_cash_cost_lamports']
    first = {}
    for error in csv.DictReader((OUT / RUN_NAME / 'conditional_errors.csv').open()):
        if new[error['mint']]['status'] == 'OPEN':
            first.setdefault(error['mint'], error)
    exported = list(csv.DictReader((OUT / 'positions.csv').open()))
    # Final reason labels can be overwritten by subsequent lifecycle events.
    # Only open exposure gets a causal blocker; later warnings do not relabel closed PnL.
    for p in exported:
        p['first_causal_blocker'] = first.get(p['mint'], {}).get('reason', '')
    for name, selected in [
        ('positions', exported),
        ('profitable_positions', [p for p in exported if p['status']=='CLOSED' and Decimal(p['closed_pnl_sol'])>0]),
        ('losing_positions', [p for p in exported if p['status']=='CLOSED' and Decimal(p['closed_pnl_sol'])<0]),
        ('unresolved_positions', [p for p in exported if p['status']=='OPEN']),
        ('failed_entries', [p for p in exported if p['status']=='ENTRY_FAILED']),
        ('risk_rejected_entries', [p for p in exported if p['status']=='ENTRY_REJECTED']),
        ('new_signals', [p for p in exported if p['newly_signaled']=='True'])]:
        write_csv(OUT / (name+'.csv'), selected)
    same = [p for p in diagnostics if p['group']=='same_signal']
    economic_keys = ('status', 'entry_slot', 'entry_cash_lamports', 'exit_slot',
                     'closed_pnl_lamports', 'failed_execution_cost_lamports', 'remaining_tokens_atomic')
    changes = [p['mint'] for p in same if any(old[p['mint']].get(k)!=new[p['mint']].get(k) for k in economic_keys)]
    save(OUT / 'opportunity-analysis.json', dict(complete=True, groups=groups,
        same_signal_economic_differences=changes,
        open_first_causal_blockers=dict(Counter(p['reason'] for p in first.values())),
        open_tokens_outside_original_repair=sum(p['status']=='OPEN' and p['mint'] not in repaired for p in variant['positions']),
        no_dev_flat_creator_anchor_signals=sum(bool(p.get('sniper_is_launch_creator')) for p in variant['positions']),
        known_limit='Same frozen input; additional histories exposed by the disabled gate have not received the targeted repair applied to the original 78 cases.'))
    assert not changes, changes
    print(json.dumps(dict(groups=groups, open_first_causal_blockers=dict(Counter(p['reason'] for p in first.values()))), indent=2))


if __name__ == '__main__':
    main()
