"""One-factor full-day replay against the immutable, repaired October 12 input."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
import json
from pathlib import Path
import shutil
import time

import sniper_oct12_repair as repair
from sniper_carbon_day import save, publish, utc
from backtest.adapters.source.sniper_silver import file_hash
from backtest.bootstrap.sniper_window import implementation_identity, run, sol_string, write_csv
from backtest.domain.execution import ExecutionMode
from backtest.plugins.strategies.sniper_window import SniperWindowPolicy

ENGINE = Path(__file__).absolute().parents[1]
BASE = ENGINE / 'var/dnipro/sniper-oct12-repair-20261016-v1'
OUT = ENGINE / 'var/dnipro/sniper-oct12-no-dev-flat-20261016-v1'
CONTROL = BASE / '24h-full-cohort-repaired-v10'
RUN_NAME = '24h-no-dev-flat-wallet10000'


def verify_result(folder):
    manifest = json.loads((folder / 'manifest.json').read_text())
    assert manifest['complete']
    for name, digest in manifest['files'].items():
        assert file_hash(folder / name) == digest, name
    result = json.loads((folder / 'result.json').read_text())
    assert result['summary']['source_input_sha256'] == manifest['source_input_sha256']
    return result


def prepare():
    assert not OUT.exists(), 'Use a new experiment directory; never overwrite results'
    baseline = verify_result(CONTROL)
    core = implementation_identity()
    assert core == baseline['summary']['implementation']
    committed = json.loads((BASE / 'COMMITTED').read_text())
    assert committed['delivery_manifest_sha256'] == file_hash(BASE / 'delivery-manifest.json')
    delivery = json.loads((BASE / 'delivery-manifest.json').read_text())['files']
    names = {'births.json', 'original-token-rows.jsonl.gz', 'recovered-trades.json',
             'tail-trades.json', 'verified-transfers.json',
             'clickhouse/midnight-observed-slot-clocks-v3.jsonl.gz',
             'clickhouse/midnight-observed-slot-clocks-v3.jsonl.receipt.json'}
    for filename in ('recovered-trades.json', 'tail-trades.json', 'verified-transfers.json'):
        for row in json.loads((BASE / filename).read_text()):
            name = row.get('rpc_evidence_file', row.get('evidence_file'))
            names.add(name)
            names.add(str(Path(name).with_suffix('.receipt.json')))
    OUT.mkdir(parents=True)
    copied = {}
    for name in sorted(names):
        source, target = BASE / name, OUT / name
        digest = file_hash(source)
        assert digest == delivery[name]['sha256'], name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        assert file_hash(target) == digest
        copied[name] = dict(sha256=digest, bytes=target.stat().st_size)
    old_policy = SniperWindowPolicy.load(baseline['summary']['policy'])
    new_policy = SniperWindowPolicy.load(asdict(old_policy) | {'require_creator_flat_before_entry': 0})
    changes = {key: dict(before=asdict(old_policy)[key], after=value)
               for key, value in asdict(new_policy).items() if value != asdict(old_policy)[key]}
    assert changes == {'require_creator_flat_before_entry': {'before': 1, 'after': 0}}
    assert new_policy.bankroll_lamports == 10_000 * 10**9
    save(OUT / 'policy.json', new_policy.document())
    save(OUT / 'control-policy.json', old_policy.document())
    save(OUT / 'input-receipt.json', dict(complete=True, source=str(BASE), files=copied,
         control_manifest_sha256=file_hash(CONTROL / 'manifest.json'),
         control_source_input_sha256=baseline['summary']['source_input_sha256'],
         implementation=core, changes=changes,
         scope='Same frozen repaired archive; recovery was bounded to the original 78 open tokens; global coverage is not certified'))
    source_dir = OUT / 'replay-source'
    source_dir.mkdir()
    for name in ('sniper_oct12_no_dev_flat.py', 'sniper_oct12_repair.py', 'sniper_carbon_day.py'):
        shutil.copy2(ENGINE / 'notebooks' / name, source_dir / name)
    print('PREPARED', len(copied), 'verified input files', flush=True)


def replay():
    receipt = json.loads((OUT / 'input-receipt.json').read_text())
    for name, facts in receipt['files'].items():
        assert file_hash(OUT / name) == facts['sha256'], name
    assert file_hash(CONTROL / 'manifest.json') == receipt['control_manifest_sha256']
    assert implementation_identity() == receipt['implementation']
    assert file_hash(Path(__file__)) == file_hash(OUT / 'replay-source' / Path(__file__).name)
    policy = SniperWindowPolicy.load(json.loads((OUT / 'policy.json').read_text()))
    old = SniperWindowPolicy.load(json.loads((OUT / 'control-policy.json').read_text()))
    assert asdict(policy) == asdict(old) | {'require_creator_flat_before_entry': 0}
    repair.OUT = OUT  # Confined local evidence copies; never write to the committed control.
    source = repair.RepairedSource(hours=24)
    assert source.source_hash == receipt['control_source_input_sha256'], 'Comparison input changed'
    save(OUT / 'job-status.json', dict(phase='preparing_timeline', complete=False, source_hash=source.source_hash))
    source.boundaries()
    began = time.monotonic()
    print('REPLAY_START', RUN_NAME, 'source', source.source_hash, flush=True)
    save(OUT / 'job-status.json', dict(phase='native_replay', complete=False, source_hash=source.source_hash))
    from backtest.plugins.execution.pump_conditional import PumpConditionalReducer
    original = PumpConditionalReducer._move_inventory
    conflicts = []

    def observe(reducer, movement, balances, historical, applied):
        key = movement['mint'], movement['source_wallet']
        historical_amount = historical.get(key, reducer.historical_balances.get(key, 0))
        simulated_amount = balances.get(key, reducer.balances.get(key, 0))
        try:
            return original(reducer, movement, balances, historical, applied)
        except ValueError as error:
            conflicts.append(dict(mint=movement['mint'], slot=movement['slot'], tx_idx=movement['tx_idx'],
                signature=movement['signature'], source_wallet=movement['source_wallet'],
                destination_wallet=movement['destination_wallet'], required_tokens_atomic=movement['tokens'],
                historical_source_tokens_atomic=historical_amount, simulated_source_tokens_atomic=simulated_amount,
                shortfall_tokens_atomic=max(0, movement['tokens']-simulated_amount), reason=str(error),
                instruction_path=movement['instruction_path'], evidence_sha256=movement['evidence_sha256']))
            raise

    PumpConditionalReducer._move_inventory = observe
    try:
        summary, rows, sink = run(source, policy, ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY,
                                  trace_mints=set(json.loads((OUT / 'births.json').read_text())))
    finally:
        PumpConditionalReducer._move_inventory = original
    summary.update(input_format='committed-carbon-projection-plus-finalized-rpc-recovery',
                   repair_scope='original-unresolved-token-histories', full_chain_coverage=False,
                   recovered_swap_count=source.recovered_trade_count,
                   last_available_time_utc=utc(max(source.times.values())))
    publish(OUT / RUN_NAME, summary, rows, sink)
    write_csv(OUT / 'inventory-conflicts.csv', conflicts)
    save(OUT / 'run-receipt.json', dict(complete=True, seconds=round(time.monotonic()-began, 3),
         manifest_sha256=file_hash(OUT / RUN_NAME / 'manifest.json'),
         runner_sha256=file_hash(Path(__file__)),
         inventory_conflicts_sha256=file_hash(OUT / 'inventory-conflicts.csv')))
    summarize()
    print('REPLAY_COMPLETE', json.dumps({k: summary[k] for k in (
        'signals', 'filled_entries', 'closed_positions', 'open_positions',
        'realized_net_pnl_sol', 'open_cash_cost_sol')}), flush=True)


def summarize():
    import csv
    baseline, variant = verify_result(CONTROL), verify_result(OUT / RUN_NAME)
    a, b = baseline['summary'], variant['summary']
    old, new = SniperWindowPolicy.load(a['policy']), SniperWindowPolicy.load(b['policy'])
    assert asdict(new) == asdict(old) | {'require_creator_flat_before_entry': 0}
    assert a['implementation'] == b['implementation'] == implementation_identity()
    assert a['source_input_sha256'] == b['source_input_sha256']
    comparisons, exports, first_errors = [], [], {}
    for error in csv.DictReader((OUT / RUN_NAME / 'conditional_errors.csv').open()):
        first_errors.setdefault(error['mint'], error)
    previous = {p['mint']: p for p in baseline['positions']}
    for label, result in [('dev_flat_required', baseline), ('no_dev_flat', variant)]:
        s, rows = result['summary'], result['positions']
        closed = [p for p in rows if p['status'] == 'CLOSED']
        opened = [p for p in rows if p['status'] == 'OPEN']
        pnl = sum(p['closed_pnl_lamports'] for p in closed) - s['failed_entry_cost_lamports']
        cost = sum(p['entry_cash_lamports']+p['failed_execution_cost_lamports'] for p in opened)
        assert pnl == s['realized_net_pnl_lamports'] and cost == s['open_cash_cost_lamports']
        assert int(Decimal(s['final_available_sol']) * 10**9) == new.bankroll_lamports+pnl-cost
        for key in ('cash_reconciliation_verified', 'creator_fee_allocation_conservation_verified',
                    'synthetic_liquidity_reconciliation_verified'):
            assert s[key]
        assert s['synthetic_shortfall_lamports'] == 0
        comparisons.append(dict(run=label, wallet_sol=new.bankroll_lamports//10**9,
            signals=s['signals'], filled=s['filled_entries'], closed=len(closed), open=len(opened),
            wins=sum(p['closed_pnl_lamports']>0 for p in closed), losses=sum(p['closed_pnl_lamports']<0 for p in closed),
            failed_entries=s['status_counts'].get('ENTRY_FAILED', 0),
            capital_rejections=s['engine_summary']['rejected_order_count'],
            realized_net_pnl_sol=s['realized_net_pnl_sol'], open_committed_cost_sol=s['open_cash_cost_sol'],
            final_portfolio_pnl_sol=s['realized_net_pnl_sol'] if not opened else '',
            open_reasons=dict(Counter(p['reason'] for p in opened))))
    for p in variant['positions']:
        if 'signal_slot' not in p:
            continue
        q = previous[p['mint']]
        exports.append(dict(symbol=p['symbol'], mint=p['mint'], gmgn_url=p['gmgn_url'],
            status=p['status'], reason=p['reason'], first_causal_blocker=first_errors.get(p['mint'], {}).get('reason', ''),
            signal_slot=p['signal_slot'], entry_slot=p.get('entry_slot', ''), exit_slot=p.get('exit_slot', ''),
            entry_utc=utc(p['entry_time']) if 'entry_time' in p else '',
            exit_utc=utc(p['exit_time']) if 'exit_time' in p else '',
            entry_cash_sol=p['entry_cash_sol'], closed_pnl_sol=p.get('closed_pnl_sol', ''),
            failed_execution_cost_sol=p['failed_execution_cost_sol'],
            remaining_tokens_atomic=p['remaining_tokens_atomic'], tracked_buyer=p.get('sniper_wallet', ''),
            tracked_buyer_gmgn='https://gmgn.ai/sol/address/'+p['sniper_wallet'] if p.get('sniper_wallet') else '',
            dev_flat_status=q['status'], dev_flat_reason=q['reason'],
            dev_flat_entry_slot=q.get('entry_slot', ''), dev_flat_entry_cash_sol=q['entry_cash_sol'],
            dev_flat_closed_pnl_sol=q.get('closed_pnl_sol', ''),
            newly_signaled='signal_slot' not in q))
    write_csv(OUT / 'comparison.csv', comparisons)
    write_csv(OUT / 'positions.csv', exports)
    for name, selected in [
        ('profitable_positions', [p for p in exports if p['status']=='CLOSED' and Decimal(p['closed_pnl_sol'])>0]),
        ('losing_positions', [p for p in exports if p['status']=='CLOSED' and Decimal(p['closed_pnl_sol'])<0]),
        ('unresolved_positions', [p for p in exports if p['status']=='OPEN']),
        ('failed_entries', [p for p in exports if p['status']=='ENTRY_FAILED']),
        ('new_signals', [p for p in exports if p['newly_signaled']])]:
        write_csv(OUT / (name+'.csv'), selected)
    save(OUT / 'validation.json', dict(complete=True, same_input=True, same_engine=True,
         only_policy_change={'require_creator_flat_before_entry': {'before': 1, 'after': 0}},
         cash_reconciled=True, creator_fee_conservation=True, synthetic_funding_lamports=0,
         total_portfolio_pnl_known=b['open_positions']==0,
         control_manifest_sha256=file_hash(CONTROL / 'manifest.json'),
         variant_manifest_sha256=file_hash(OUT / RUN_NAME / 'manifest.json'),
         source_input_sha256=b['source_input_sha256'], comparison=comparisons))
    save(OUT / 'job-status.json', dict(complete=True, phase='complete', comparison=comparisons))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['prepare', 'run', 'summarize'])
    command = parser.parse_args().command
    {'prepare': prepare, 'run': replay, 'summarize': summarize}[command]()
