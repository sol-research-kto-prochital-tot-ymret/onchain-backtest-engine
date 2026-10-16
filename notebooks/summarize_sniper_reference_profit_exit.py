"""Export matched native outcomes and evidence for the reference exit experiment."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import csv
import argparse
import hashlib
import json

ROOT = Path(__file__).absolute().parents[1]
BASE = ROOT / 'var/dnipro/sniper-reference-profit-exit-20261015-v1'
OUT = BASE


def sol(amount):
    return format(Decimal(amount) / 10**9, '.9f') if amount is not None else None


def write_csv(name, rows):
    with (OUT / name).open('x', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(dict.fromkeys(k for row in rows for k in row)))
        writer.writeheader()
        writer.writerows(rows)


def load(variant, revision=1):
    folder = BASE / (variant if revision == 1 else f'{variant}-v{revision}')
    manifest = json.loads((folder / 'manifest.json').read_text())
    assert manifest['complete']
    for filename, digest in manifest['files'].items():
        assert hashlib.sha256((folder / filename).read_bytes()).hexdigest() == digest
    return json.loads((folder / 'result.json').read_text())


def main():
    global OUT
    parser = argparse.ArgumentParser()
    parser.add_argument('--revision', type=int, default=1)
    args = parser.parse_args()
    if args.revision > 1:
        OUT = BASE / f'review-v{args.revision}'
        OUT.mkdir(exist_ok=False)
    results = {key:load(key,args.revision) for key in ('control', 'reference-profit')}
    before = {p['mint']:p for p in results['control']['positions']}
    after = {p['mint']:p for p in results['reference-profit']['positions']}
    traces = defaultdict(list)
    variant_folder = BASE / ('reference-profit' if args.revision == 1 else f'reference-profit-v{args.revision}')
    with (variant_folder / 'strategy_trace.csv').open() as stream:
        for row in csv.DictReader(stream):
            if row['action'] == 'REFERENCE_PROFIT_EVALUATION':
                traces[row['mint']].append(row)
    rows = []
    first_crossings = []
    for mint, position in after.items():
        if 'signal_slot' not in position:
            continue
        old = before[mint]
        evaluations = traces[mint]
        reached = 'reference_first_profitable_slot' in position
        available = [e for e in evaluations if e['reference_profit_lamports'] != '']
        unknown = [e for e in evaluations if e['reference_profit_lamports'] == '']
        first = next((e for e in evaluations if e['reference_reached'] == 'True'), None)
        assert reached == (first is not None)
        if first:
            assert int(first['slot']) == position['reference_first_profitable_slot']
            assert int(first['reference_profit_lamports']) >= 0
            first_crossings.append({
                'symbol':position['symbol'], 'mint':mint, 'gmgn_url':position['gmgn_url'],
                'reference_wallet':position['sniper_wallet'], 'reference_gmgn':position['sniper_gmgn_url'],
                'entry_slot':position['entry_slot'], 'entry_time':position['entry_time'],
                **{k:first[k] for k in ('slot', 'time', 'boundary_ordinal', 'reference_tokens_atomic',
                    'reference_unrecovered_cost_lamports', 'reference_profit_lamports',
                    'reference_valuation_status', 'wallet_creator_fee_income')},
                'seconds_after_entry':int(first['time']) - position['entry_time'],
                'final_status':position['status'], 'final_exit_reason':position['reason'],
                'our_exit_slot':position.get('exit_slot'), 'our_closed_pnl_sol':sol(position['closed_pnl_lamports']),
            })
        if 'entry_slot' not in position:
            assessment = 'NO_FILLED_ENTRY'
        elif reached:
            assessment = 'OBSERVED_REFERENCE_BREAK_EVEN'
        elif position['status'] == 'CLOSED' and position['reason'] == 'TIMEOUT':
            assessment = 'NO_OBSERVED_CROSSING_BEFORE_TIMEOUT'
        else:
            assessment = 'CENSORED_UNRESOLVED'
        rows.append({
            'symbol':position['symbol'], 'mint':mint, 'gmgn_url':position['gmgn_url'],
            'reference_gmgn':position['sniper_gmgn_url'],
            'budget_sol':sol(position['budget_lamports']), 'entry_slot':position.get('entry_slot'),
            'control_status':old['status'], 'control_reason':old['reason'],
            'control_pnl_sol':sol(old['closed_pnl_lamports']),
            'reference_exit_status':position['status'], 'reference_exit_reason':position['reason'],
            'reference_exit_pnl_sol':sol(position['closed_pnl_lamports']),
            'reference_exit_slot':position.get('exit_slot'),
            'open_cash_cost_sol':sol(position['entry_cash_lamports']) if position['status'] == 'OPEN' else None,
            'reference_assessment':assessment, 'reference_reached':reached,
            'reference_first_profitable_slot':position.get('reference_first_profitable_slot'),
            'seconds_to_reference_break_even':int(first['time']) - position['entry_time'] if first else None,
            'reference_evaluations':len(evaluations), 'available_reference_evaluations':len(available),
            'unavailable_reference_evaluations':len(unknown),
            'unavailable_valuation_reasons':json.dumps(dict(Counter(e['reference_valuation_status'] for e in unknown)),sort_keys=True),
            'maximum_observed_reference_pnl_sol':sol(max(int(e['reference_profit_lamports']) for e in available)) if available else None,
        })
    write_csv('positions_comparison.csv', rows)
    write_csv('reference_first_crossings.csv', first_crossings)
    comparison = []
    for name, result in results.items():
        summary = result['summary']
        closed = [p for p in result['positions'] if p['status'] == 'CLOSED']
        reasons = Counter(p['reason'] for p in closed)
        record = {key:summary[key] for key in ['signals', 'filled_entries', 'closed_positions',
                    'open_positions', 'realized_net_pnl_sol', 'open_cash_cost_sol', 'zero_recovery_stress_pnl_sol']}
        record.update(variant=name, profitable=sum(p['closed_pnl_lamports']>0 for p in closed),
                      losing=sum(p['closed_pnl_lamports']<0 for p in closed),
                      target_exits=reasons['TARGET'], reference_profit_exits=reasons['REFERENCE_PROFIT'],
                      timeout_exits=reasons['TIMEOUT'],
                      failed_entries=sum(p['status']=='ENTRY_FAILED' for p in result['positions']),
                      risk_rejections=sum(p['status']=='ENTRY_REJECTED' for p in result['positions']))
        for reason in ('TARGET','REFERENCE_PROFIT','TIMEOUT'):
            record[reason.lower()+'_pnl_sol'] = sol(sum(p['closed_pnl_lamports'] for p in closed if p['reason']==reason))
        comparison.append(record)
    write_csv('comparison.csv', comparison)
    summary = dict(
        observed_at=datetime.now(timezone.utc).isoformat(), sample='2026-10-10 16:00-17:00 UTC launches',
        input_revision=args.revision,
        holding_timeout_seconds=300, entry_timing='ORIGINAL', starting_wallet_sol=50, slippage_bps=500,
        reference_break_even_definition='unrecovered observed cash costs <= executable full liquidation net of sell program and configured network fees, or already recovered cash; no unproven rent refund',
        original_entry_sizing_target_return_bps_unchanged=500,
        comparison=comparison, reference_first_crossing_count=len(first_crossings),
        assessments=dict(Counter(row['reference_assessment'] for row in rows)),
        crossing_seconds_after_entry=sorted(row['seconds_after_entry'] for row in first_crossings),
        crossing_valuation_statuses=dict(Counter(row['reference_valuation_status'] for row in first_crossings)),
        open_positions=[{k:p.get(k) for k in ('symbol','mint','gmgn_url','sniper_gmgn_url','reason',
                         'entry_slot','entry_cash_lamports')} for p in results['reference-profit']['positions'] if p['status']=='OPEN'],
        reference_unavailable_evaluation_reasons=dict(Counter(e['reference_valuation_status']
            for values in traces.values() for e in values if e['reference_profit_lamports']=='')),
        final_portfolio_pnl_known=all(not r['summary']['open_positions'] for r in results.values()),
        limits=['Single development cohort; not an independent holdout.',
                'Reference marks are hypothetical conditional-replay liquidations, not proof of an actual sale or private intent.',
                'Inventory repair uses bounded verified transfers, not full holder balance coverage.',
                'Absent crossings under unavailable quotes or censored model state are not proof that profitability was impossible.',
                'Trigger is evaluated while our position is held; does not monitor reference wallets after our exit.'])
    (OUT / 'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2))


if __name__ == '__main__':
    main()
