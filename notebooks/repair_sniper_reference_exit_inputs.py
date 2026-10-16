"""Bound missing acquisitions locally and retain each paired replay revision."""
from pathlib import Path
import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).absolute().parents[1]
OUT = ROOT / 'var/dnipro/sniper-reference-profit-exit-20261015-v1'
REPAIRED = ROOT / 'var/dnipro/sniper-inventory-repair-20261015-v1'
SOURCE = ROOT.parent / 'solana-live-indexer/research/sniper_break_even/data/2026-10-10-1600-1700'


def verify(revision):
    old = json.loads((OUT/'control/result.json').read_text())
    new = json.loads((OUT/f'control-v{revision}/result.json').read_text())
    # Repair can extend post-exit historical fee-accrual diagnostics. It must
    # leave all control trades and cash accounting unchanged.
    diagnostics = {'developer_creator_fee_accrual_sol','developer_creator_fee_accrual_lamports',
        'developer_trade_cashflow_plus_fee_accrual_lamports','developer_trade_cashflow_plus_fee_accrual_sol',
        'token_total_creator_fee_accrual_sol','token_total_creator_fee_accrual_lamports'}
    assert all(old[key]==new[key] for key in ('fills','ledger')), 'Repair changed control execution'
    for a,b in zip(old['positions'],new['positions'],strict=True):
        changed = {k for k in a.keys()|b.keys() if a.get(k)!=b.get(k)}
        assert changed <= diagnostics, (a['mint'],changed)
    changed = json.loads((OUT/f'reference-profit-v{revision}/result.json').read_text())
    a = {p['mint']:p for p in new['positions'] if 'signal_slot' in p}
    b = {p['mint']:p for p in changed['positions'] if 'signal_slot' in p}
    assert a.keys()==b.keys()
    for mint in a:
        assert all(a[mint][key]==b[mint][key] for key in ('budget_lamports','signal_slot','sniper_wallet'))
    overlay = OUT/f'transfers-v{revision}.json'
    (OUT/f'repair-parity-v{revision}.json').write_text(json.dumps(dict(
        complete=True,revision=revision,transfer_count=len(json.loads(overlay.read_text())),
        original_control_trading_position_fields_match=True,
        original_control_fills_ledger_exact_match=True,
        excluded_post_exit_fee_diagnostic_fields=sorted(diagnostics),paired_entry_signals_sizing_match=True,
        overlay_sha256=hashlib.sha256(overlay.read_bytes()).hexdigest()),indent=2)+'\n')
    print('CONTROL_AND_ENTRY_PARITY_VERIFIED',revision,flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('revision', type=int)
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--extra-cases', type=Path)
    args = parser.parse_args()
    revision = args.revision
    assert revision >= 2
    if args.verify_only:
        verify(revision)
        return
    for name in ('rpc_cache.py', 'collect_transfer_evidence.py'):
        target = OUT / name
        if not target.exists():
            shutil.copy2(REPAIRED / name, target)
    shutil.copytree(REPAIRED / 'public-rpc', OUT / 'public-rpc', dirs_exist_ok=True)
    sys.path.insert(0, str(OUT))
    from collect_transfer_evidence import collect
    from backtest.adapters.source.sniper_silver import SilverSniperSource
    prior = OUT / ('reference-profit' if revision == 2 else f'reference-profit-v{revision-1}')
    existing = REPAIRED / 'transfers-v3.json' if revision == 2 else OUT / f'transfers-v{revision-1}.json'
    source = SilverSniperSource(SOURCE, ROOT / 'var/dnipro/sniper-gap-455328000.json')
    result = json.loads((prior / 'result.json').read_text())
    opened = {p['mint']:p for p in result['positions'] if p['status']=='OPEN'}
    with (prior / 'conditional_errors.csv').open() as stream:
        errors = [e for e in csv.DictReader(stream) if e['mint'] in opened
                  and e['reason']=='CONDITIONAL_HISTORICAL_SELLER_BALANCE_UNPROVEN']
    cases = []
    for error in errors:
        boundary = int(error['boundary_ordinal']); slot = boundary >> 32
        index = (boundary & ((1 << 32)-1))-1
        for row in source.trade_rows:
            if row['mint']==error['mint'] and row['slot']==slot and row['tx_idx']==index and row['side']=='sell':
                cases.append(dict(token=opened[row['mint']]['symbol'],mint=row['mint'],seller=row['trader'],
                    signature=row['signature'],error_slot=slot,error_tx_index=index,error_boundary=boundary,
                    launch_slot=int(source.birth_by_mint[row['mint']]['slot'])))
    assert cases, 'No repairable cases remain'
    if args.extra_cases:
        cases.extend(json.loads(args.extra_cases.read_text()))
        cases = list({(c['mint'],c['seller'],c['signature']):c for c in cases}.values())
    (OUT / f'cases-v{revision}.json').write_text(json.dumps(cases,indent=2)+'\n')
    overlay = OUT / f'transfers-v{revision}.json'
    assert not overlay.exists()
    movements = collect(cases, existing, overlay, OUT / f'acquisition-findings-v{revision}.json')
    assert len(movements)>len(json.loads(existing.read_text())), 'No new transfer evidence'
    print('REPAIRED_INPUT',revision,len(movements),flush=True)
    traced = [p['mint'] for p in result['positions'] if 'signal_slot' in p]
    # Separate native processes on the same input revision; no engine source edits.
    processes = []
    began = time.monotonic()
    for variant in ('control','reference-profit'):
        destination = OUT / f'{variant}-v{revision}'
        assert not destination.exists()
        command = [str(ROOT / '.venv/bin/python'),'-m','backtest.bootstrap.dnipro','sniper-window',
            '--snapshot',str(SOURCE),'--policy',str(OUT / f'{variant}-policy.json'),
            '--execution-mode','CONDITIONAL_PROTOCOL_REPLAY',
            '--empty-block-evidence',str(ROOT/'var/dnipro/sniper-gap-455328000.json'),
            '--inventory-transfers',str(overlay),'--out',str(destination)]
        for mint in traced:
            command += ['--trace-mint',mint]
        log = (OUT / f'{variant}-v{revision}.log').open('x')
        processes.append((variant, subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT),log))
    for variant, process, log in processes:
        assert process.wait()==0, variant
        log.close()
        destination = OUT / f'{variant}-v{revision}'
        manifest = json.loads((destination/'manifest.json').read_text()); assert manifest['complete']
        for name,digest in manifest['files'].items():
            assert hashlib.sha256((destination/name).read_bytes()).hexdigest()==digest
        current = json.loads((destination/'result.json').read_text())
        print('NATIVE_DONE',variant,revision,round(time.monotonic()-began,3),json.dumps({key:current['summary'][key]
            for key in ('filled_entries','closed_positions','open_positions','realized_net_pnl_sol')}),flush=True)
    verify(revision)


if __name__=='__main__':
    main()
