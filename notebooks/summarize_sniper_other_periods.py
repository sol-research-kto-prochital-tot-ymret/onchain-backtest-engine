"""Verify the paired native artifacts and export a bounded comparison."""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import csv
import hashlib
import json

ROOT = Path(__file__).absolute().parents[1]
OUT = ROOT / 'var/dnipro/sniper-dev-flat-other-periods-20261015-v1'
DATA = ROOT.parent / 'solana-live-indexer/research/sniper_break_even/data'
RUNS = {
    'late-oct10': {'control': 'late-oct10-control-repaired-v1',
                   'dev-flat': 'late-oct10-dev-flat-repaired-v1'},
    'oct11': {'control': 'oct11-control', 'dev-flat': 'oct11-dev-flat'},
}
LAMPORTS = Decimal(1_000_000_000)


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def sol(amount):
    return format(Decimal(amount) / LAMPORTS, '.9f')


def write_csv(path, rows):
    assert rows
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else ''


def verify(name, variant, source):
    folder = OUT / name
    manifest = json.loads((folder / 'manifest.json').read_text())
    assert manifest['complete'], name
    for filename, digest in manifest['files'].items():
        assert sha(folder / filename) == digest, (name, filename)
    result = json.loads((folder / 'result.json').read_text())
    summary = result['summary']
    assert summary['policy'] == json.loads((OUT / f'{variant}-policy.json').read_text())
    assert summary['source_manifest_sha256'] == sha(source / 'manifest.json')
    assert summary['cash_reconciliation_verified']
    assert summary['creator_fee_allocation_conservation_verified']
    assert summary['synthetic_liquidity_reconciliation_verified']
    closed = [p for p in result['positions'] if p['status'] == 'CLOSED']
    opened = [p for p in result['positions'] if p['status'] == 'OPEN']
    assert len(closed) == summary['closed_positions']
    assert len(opened) == summary['open_positions']
    closed_pnl = sum(p['closed_pnl_lamports'] for p in closed)
    open_cost = sum(p['entry_cash_lamports'] + p['failed_execution_cost_lamports'] for p in opened)
    realized = closed_pnl - summary['failed_entry_cost_lamports']
    assert closed_pnl == summary['closed_trade_pnl_lamports']
    assert open_cost == summary['open_cash_cost_lamports']
    assert realized == summary['realized_net_pnl_lamports']
    assert Decimal(summary['final_available_sol']) * LAMPORTS == (
        summary['policy']['bankroll_lamports'] + realized - open_cost)
    assert sol(realized - open_cost) == summary['zero_recovery_stress_pnl_sol']
    if variant == 'dev-flat':
        for p in result['positions']:
            if 'signal_slot' in p:
                assert p['creator_flat_entry_allowed'] and p['creator_tokens_at_signal'] == 0
                assert p['signal_slot'] <= p['launch_slot'] + 2
    return result


def main():
    plan = json.loads((OUT / 'experiment-plan.json').read_text())
    control = json.loads((OUT / 'control-policy.json').read_text())
    dev_flat = json.loads((OUT / 'dev-flat-policy.json').read_text())
    assert {k: v for k, v in dev_flat.items() if k != 'require_creator_flat_before_entry'} == control
    assert dev_flat['require_creator_flat_before_entry'] == 1
    summaries, positions, comparisons, verification = [], [], [], []
    for period, runs in RUNS.items():
        source = DATA / plan['periods'][period]
        pair = {variant: verify(name, variant, source) for variant, name in runs.items()}
        a, b = (pair[v]['summary'] for v in ['control', 'dev-flat'])
        for key in ['source_input_sha256', 'source_manifest_sha256', 'inventory_enrichment_sha256',
                    'inventory_base_source_sha256', 'publication_gaps', 'retained_input_trade_events']:
            assert a[key] == b[key], (period, key)
        assert a['implementation'] == b['implementation']
        for variant, result in pair.items():
            summary = result['summary']
            closed = [p for p in result['positions'] if p['status'] == 'CLOSED']
            opened = [p for p in result['positions'] if p['status'] == 'OPEN']
            counts = summary['status_counts']
            summaries.append(dict(
                period=period, start_utc=summary['cohort_start_utc'], end_utc=summary['cohort_end_utc'],
                variant=variant, native_run=runs[variant], launches=summary['cohort_launches'],
                trades=summary['retained_input_trade_events'], starting_wallet_sol=50,
                signals=summary['signals'], filled_entries=summary['filled_entries'], closed=len(closed),
                wins=sum(p['closed_pnl_lamports'] > 0 for p in closed),
                losses=sum(p['closed_pnl_lamports'] < 0 for p in closed),
                flat=sum(p['closed_pnl_lamports'] == 0 for p in closed),
                failed_entries=counts.get('ENTRY_FAILED', 0),
                risk_rejected_orders=summary['engine_summary']['rejected_order_count'],
                failed_executions=summary['engine_summary']['failed_order_count'],
                open=len(opened), realized_net_pnl_sol=summary['realized_net_pnl_sol'],
                final_net_pnl_sol=summary['realized_net_pnl_sol'] if not opened else '',
                open_cash_cost_sol=summary['open_cash_cost_sol'],
                zero_recovery_stress_pnl_sol=summary['zero_recovery_stress_pnl_sol'],
                coverage_gap_intervals=len(summary['publication_gaps']),
                coverage_unproven_slots=sum(end-start+1 for start, end in summary['publication_gaps']),
                open_reasons=json.dumps(dict(Counter(p['reason'] for p in opened)), sort_keys=True),
                exit_reasons=json.dumps(dict(Counter(p['reason'] for p in closed)), sort_keys=True),
                source_input_sha256=summary['source_input_sha256'],
                engine_source_sha256=summary['implementation']['engine_source_sha256'],
            ))
            verification.append(dict(run=runs[variant], manifest_sha256=sha(OUT/runs[variant]/'manifest.json'),
                                     result_sha256=sha(OUT/runs[variant]/'result.json'), verified=True))
            for p in result['positions']:
                if 'signal_slot' not in p:
                    continue
                positions.append(dict(
                    period=period, variant=variant, native_run=runs[variant], symbol=p['symbol'],
                    mint=p['mint'], gmgn_url=p['gmgn_url'], status=p['status'], reason=p['reason'],
                    launch_slot=p['launch_slot'], signal_slot=p['signal_slot'],
                    entry_slot=p.get('entry_slot', ''), entry_utc=utc(p.get('entry_time')),
                    exit_slot=p.get('exit_slot', ''), exit_utc=utc(p.get('exit_time')),
                    entry_cash_sol=p['entry_cash_sol'], closed_pnl_sol=p.get('closed_pnl_sol', ''),
                    failed_execution_cost_sol=p['failed_execution_cost_sol'],
                    remaining_tokens_atomic=p['remaining_tokens_atomic'],
                    creator=p['creator'], creator_gmgn_url='https://gmgn.ai/sol/address/'+p['creator'],
                    creator_status_at_signal=p.get('creator_status_at_signal', ''),
                    sniper_wallet=p.get('sniper_wallet', ''), sniper_gmgn_url=p.get('sniper_gmgn_url', ''),
                ))
        lookup = {v: {p['mint']: p for p in r['positions']} for v, r in pair.items()}
        mints = sorted({p['mint'] for r in pair.values() for p in r['positions'] if 'signal_slot' in p})
        for mint in mints:
            first = lookup['control'].get(mint) or lookup['dev-flat'][mint]
            row = dict(period=period, symbol=first['symbol'], mint=mint, gmgn_url=first['gmgn_url'])
            for variant in ['control', 'dev-flat']:
                p = lookup[variant].get(mint, {})
                for key in ['status', 'reason', 'signal_slot', 'entry_slot', 'exit_slot', 'entry_cash_sol', 'closed_pnl_sol']:
                    row[variant+'_'+key] = p.get(key, '')
            comparisons.append(row)
    write_csv(OUT/'period_comparison.csv', summaries)
    write_csv(OUT/'positions.csv', positions)
    write_csv(OUT/'token_comparison.csv', comparisons)
    write_csv(OUT/'unresolved_positions.csv', [p for p in positions if p['status'] == 'OPEN'])
    (OUT/'comparison-verification.json').write_text(json.dumps(dict(
        complete=True, observed_at=datetime.now(timezone.utc).isoformat(),
        policies_differ_only_by_creator_flat_gate=True, paired_inputs_identical=True,
        cash_and_outcome_reconciliation_verified=True, runs=verification,
        incomplete_oct11_transfer_prefetch='Not used; stopped because the terminal blockers are uncertified source ranges',
        limitations=['conditional replay, not exact counterfactual trader behavior',
                     'fixed historical transfer quantities can conflict with counterfactual balances',
                     'Oct11 has 25 unproven publication ranges covering 756 slots',
                     'realized contribution is not final portfolio PnL while positions are open',
                     '50 SOL wallet independently reset per window; no retuning'],
    ), indent=2)+'\n')
    print(json.dumps(summaries, indent=2))


if __name__ == '__main__':
    main()
