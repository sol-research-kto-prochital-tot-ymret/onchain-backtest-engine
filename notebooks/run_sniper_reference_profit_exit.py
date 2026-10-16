"""Native paired replay: own +5% exit versus reference fee-adjusted break-even."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import hashlib
import json
import subprocess
import time

ROOT = Path(__file__).absolute().parents[1]
OUT = ROOT / 'var/dnipro/sniper-reference-profit-exit-20261015-v1'
CONTROL = ROOT / 'var/dnipro/sniper-entry-age-10-20-20261015-v1/control'
REPAIRED = ROOT / 'var/dnipro/sniper-inventory-repair-20261015-v1'
SOURCE = ROOT.parent / 'solana-live-indexer/research/sniper_break_even/data/2026-10-10-1600-1700'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('variant', choices=['control', 'reference-profit'])
    args = parser.parse_args()
    saved = json.loads((CONTROL / 'result.json').read_text())
    policy = dict(saved['summary']['policy'])
    if args.variant == 'reference-profit':
        policy.update(own_take_profit_enabled=0, reference_profit_exit_enabled=1)
    policy_path = OUT / (args.variant + '-policy.json')
    with policy_path.open('x') as stream:
        json.dump(policy, stream, indent=2)
        stream.write('\n')
    destination = OUT / args.variant
    assert not destination.exists(), 'Refuse existing result directory'
    command = [str(ROOT / '.venv/bin/python'), '-m', 'backtest.bootstrap.dnipro', 'sniper-window',
               '--snapshot', str(SOURCE), '--policy', str(policy_path),
               '--execution-mode', 'CONDITIONAL_PROTOCOL_REPLAY',
               '--empty-block-evidence', str(ROOT / 'var/dnipro/sniper-gap-455328000.json'),
               '--inventory-transfers', str(REPAIRED / 'transfers-v3.json'), '--out', str(destination)]
    traced = [p['mint'] for p in saved['positions'] if 'signal_slot' in p]
    for mint in traced:
        command += ['--trace-mint', mint]
    began = time.monotonic()
    print('START', args.variant, flush=True)
    with (OUT / (args.variant + '.log')).open('x') as log:
        subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
    manifest = json.loads((destination / 'manifest.json').read_text())
    assert manifest['complete']
    for name, digest in manifest['files'].items():
        assert sha(destination / name) == digest, name
    result = json.loads((destination / 'result.json').read_text())
    if args.variant == 'control':
        assert all(result[key] == saved[key] for key in ['positions', 'fills', 'ledger'])
        assert result['summary']['policy'] == saved['summary']['policy']
        print('EXACT_ORIGINAL_POSITIONS_FILLS_LEDGER_MATCH', flush=True)
    else:
        before = {p['mint']: p for p in saved['positions'] if 'signal_slot' in p}
        after = {p['mint']: p for p in result['positions'] if 'signal_slot' in p}
        assert before.keys() == after.keys(), 'Signal membership changed'
        for mint in before:
            for key in ['signal_slot', 'sniper_wallet', 'budget_lamports']:
                assert before[mint][key] == after[mint][key], (mint, key)
        print('EXACT_ENTRY_SIGNALS_REFERENCE_WALLETS_AND_SIZING_MATCH', flush=True)
    record = dict(finished_utc=datetime.now(timezone.utc).isoformat(), variant=args.variant,
                  wall_seconds=round(time.monotonic() - began, 3),
                  result_sha256=sha(destination / 'result.json'), manifest_sha256=sha(destination / 'manifest.json'),
                  implementation=result['summary']['implementation'], summary=result['summary'])
    (OUT / (args.variant + '-receipt.json')).write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps({key:result['summary'].get(key) for key in ['signals', 'filled_entries',
        'closed_positions', 'open_positions', 'realized_net_pnl_sol', 'open_cash_cost_sol',
        'zero_recovery_stress_pnl_sol', 'conditional_repricing_errors']}), flush=True)


if __name__ == '__main__':
    main()
