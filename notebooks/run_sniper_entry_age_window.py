"""Compare the original entry timing with a 10–20 second decision window."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import gzip
import hashlib
import json
import subprocess
import time

ROOT = Path(__file__).absolute().parents[1]
OUT = ROOT / 'var/dnipro/sniper-entry-age-10-20-20261015-v1'
REPAIRED = ROOT / 'var/dnipro/sniper-inventory-repair-20261015-v1'
SOURCE = ROOT.parent / 'solana-live-indexer/research/sniper_break_even/data/2026-10-10-1600-1700'
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('variant', choices=['control', 'age10to20'])
    args = parser.parse_args()
    saved = json.loads((REPAIRED / 'development-repaired-before/result.json').read_text())
    policy = dict(saved['summary']['policy'])
    if args.variant == 'age10to20':
        policy.update(minimum_entry_age_seconds=10, maximum_entry_age_seconds=20)
    policy_path = OUT / (args.variant + '-policy.json')
    if policy_path.exists():
        assert json.loads(policy_path.read_text()) == policy
    else:
        policy_path.write_text(json.dumps(policy, indent=2) + '\n')
    destination = OUT / args.variant
    assert not destination.exists(), 'Refuse existing result directory'
    command = [str(ROOT / '.venv/bin/python'), '-m', 'backtest.bootstrap.dnipro', 'sniper-window',
               '--snapshot', str(SOURCE), '--policy', str(policy_path),
               '--execution-mode', 'CONDITIONAL_PROTOCOL_REPLAY',
               '--empty-block-evidence', str(ROOT / 'var/dnipro/sniper-gap-455328000.json'),
               '--inventory-transfers', str(REPAIRED / 'transfers-v3.json'), '--out', str(destination)]
    if args.variant == 'control':
        traced = [p['mint'] for p in saved['positions'] if 'signal_slot' in p]
    else:
        with gzip.open(SOURCE / 'snapshot/launches.jsonl.gz', 'rt') as stream:
            births = [json.loads(line) for line in stream]
        traced = []
        for birth in births:
            payload = json.loads(birth['decoded_extension_json'])
            if birth['quote_mint'] == 'So11111111111111111111111111111111111111112' and not any(
                payload.get(flag) for flag in ['is_mayhem_mode', 'is_cashback_enabled', 'is_holder_reward']
            ):
                traced.append(birth['mint'])
    for mint in traced:
        command += ['--trace-mint', mint]
    print('START', args.variant, 'trace_mints', len(traced), flush=True)
    began = time.monotonic()
    with (OUT / (args.variant + '.log')).open('x') as log:
        subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
    manifest = json.loads((destination / 'manifest.json').read_text())
    assert manifest['complete']
    for name, digest in manifest['files'].items():
        assert sha(destination / name) == digest, name
    result = json.loads((destination / 'result.json').read_text())
    if args.variant == 'control':
        assert all(result[k] == saved[k] for k in ['positions', 'fills', 'ledger'])
        assert result['summary']['policy'] == saved['summary']['policy']
        print('EXACT_ORIGINAL_POSITIONS_FILLS_LEDGER_MATCH', flush=True)
    else:
        for p in result['positions']:
            if 'signal_time' in p:
                # Launch time comes from the retained slot clock, not estimated slots/s.
                assert p['signal_slot'] >= p['launch_slot']
    summary = result['summary']
    record = dict(finished_utc=datetime.now(timezone.utc).isoformat(), variant=args.variant,
                  wall_seconds=round(time.monotonic() - began, 3), trace_mints=len(traced),
                  result_sha256=sha(destination / 'result.json'), manifest_sha256=sha(destination / 'manifest.json'),
                  facts={key:summary[key] for key in ['signals', 'filled_entries', 'closed_positions',
                         'open_positions', 'realized_net_pnl_sol', 'open_cash_cost_sol',
                         'zero_recovery_stress_pnl_sol', 'conditional_repricing_errors']})
    (OUT / (args.variant + '-receipt.json')).write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record), flush=True)


if __name__ == '__main__':
    main()
