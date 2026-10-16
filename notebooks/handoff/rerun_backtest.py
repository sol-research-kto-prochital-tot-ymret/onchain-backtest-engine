"""Replay bundled inputs into a fresh directory; never collect network data."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import json
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, help='Fresh output path; default is a timestamped reproduced/ directory')
    args = parser.parse_args()
    engine = ROOT / 'onchain-backtest-engine'
    experiment = engine / 'var/dnipro/sniper-inventory-repair-20261015-v1'
    saved = json.loads((experiment / 'development-repaired-before/result.json').read_text())
    output = args.out.absolute() if args.out else ROOT / 'reproduced' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    if output.exists():
        raise ValueError('Output already exists; choose a fresh path')
    output.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env['PYTHONPATH'] = str(engine / 'src')
    env['PYTHONNOUSERSITE'] = '1'
    command = [sys.executable, '-m', 'backtest.bootstrap.dnipro', 'sniper-window',
               '--snapshot', str(ROOT / 'solana-live-indexer/research/sniper_break_even/data/2026-10-10-1600-1700'),
               '--policy', str(experiment / 'wallet50-policy.json'),
               '--execution-mode', 'CONDITIONAL_PROTOCOL_REPLAY',
               '--empty-block-evidence', str(engine / 'var/dnipro/sniper-gap-455328000.json'),
               '--inventory-transfers', str(experiment / 'transfers-v3.json'), '--out', str(output)]
    for position in saved['positions']:
        if 'signal_slot' in position:
            command += ['--trace-mint', position['mint']]
    subprocess.run(command, cwd=engine, env=env, check=True)
    actual = json.loads((output / 'result.json').read_text())
    for key in ['positions', 'fills', 'ledger', 'summary']:
        if actual[key] != saved[key]:
            raise AssertionError('Reproduced output differs: ' + key)
    proof = dict(exact_positions_fills_ledger_summary_match=True,
                 realized_net_PnL_SOL=actual['summary']['realized_net_pnl_sol'], output=str(output))
    (output / 'reproduction-check.json').write_text(json.dumps(proof, indent=2) + '\n')
    print(json.dumps(proof, indent=2))


if __name__ == '__main__':
    main()
