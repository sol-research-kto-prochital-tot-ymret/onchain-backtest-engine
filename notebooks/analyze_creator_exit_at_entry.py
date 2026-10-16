"""Audit launch-creator swap inventory at native signal and buy boundaries."""
from __future__ import annotations
import csv
import gzip
import hashlib
import json
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / 'solana-live-indexer/research/sniper_break_even/data/2026-10-10-1600-1700'
OUT = ROOT / 'var/dnipro/sniper-window-20261010-creator-exit-at-entry-v1'
RUNS = {name: ROOT / ('var/dnipro/sniper-window-20261010-slot-traces-' + name + '-v1') for name in ['virtual', 'strict']}

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def inventory(rows, boundary, inclusive):
    bought = sold = 0
    ever_negative = False
    last_exit = None
    last_sale = None
    for row in rows:
        ordinal = (int(row['slot']) << 32) + int(row['tx_idx']) + 1
        if ordinal > boundary or (ordinal == boundary and not inclusive):
            break
        amount = int(row['tokens'])
        if row['side'] == 'buy':
            bought += amount
        else:
            sold += amount
            last_sale = row
            if bought > 0 and bought == sold:
                last_exit = row
        ever_negative |= sold > bought
    remaining = bought - sold
    status = ('COVERAGE_WARNING' if ever_negative else 'SOLD_ALL' if bought > 0 and sold > 0 and remaining == 0 else 'NO_OBSERVED_DEV_BUY' if bought == 0 and sold == 0 else 'PARTIALLY_SOLD' if sold > 0 and remaining > 0 else 'HOLDING')
    return dict(status=status, bought=bought, sold=sold, remaining=remaining,
                last_exit=last_exit, last_sale=last_sale, ever_negative=ever_negative)

def main():
    OUT.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((SOURCE / 'manifest.json').read_text())
    runs = {}
    creators = {}
    for name, directory in RUNS.items():
        m = json.loads((directory / 'manifest.json').read_text())
        assert m['complete']
        assert sha(directory / 'result.json') == m['files']['result.json']
        run = json.loads((directory / 'result.json').read_text())
        assert sha(SOURCE / 'manifest.json') == run['summary']['source_manifest_sha256']
        runs[name] = run
        creators.update({p['mint']: p['creator'] for p in run['positions'] if 'entry_slot' in p})
    tape = {mint: [] for mint in creators}
    for part in manifest['parts']:
        if not part['name'].endswith('-trades'):
            continue
        path = SOURCE / 'snapshot' / part['file']
        assert sha(path) == part['file_sha256']
        count = 0
        with gzip.open(path, 'rt') as stream:
            for line in stream:
                count += 1
                row = json.loads(line)
                if row['mint'] in creators and row['trader'] == creators[row['mint']]:
                    tape[row['mint']].append(row)
        assert count == part['rows']
    for rows in tape.values():
        rows.sort(key=lambda r: (r['slot'], r['tx_idx'], tuple(r['instruction_path']), r['event_uid']))
    result_rows = []
    for name, run in runs.items():
        buy_fills = {f['pool_id']: f for f in run['fills'] if f['sold_asset_id'] == 'So11111111111111111111111111111111111111112'}
        reservations = {t['correlation_id']: t['boundary_ordinal'] for t in run['ledger'] if t['reason'] == 'ORDER_RESERVATION'}
        for p in run['positions']:
            if p['mint'] not in buy_fills:
                continue
            buy = buy_fills[p['mint']]
            at_signal = inventory(tape[p['mint']], reservations[buy['order_id']], True)
            at_buy = inventory(tape[p['mint']], buy['boundary_ordinal'], False)
            sale = at_buy['last_exit']
            result_rows.append(dict(
                run=name, token=p['symbol'], mint=p['mint'], gmgn_url=p['gmgn_url'],
                launch_creator=p['creator'], creator_gmgn='https://gmgn.ai/sol/address/'+p['creator'],
                signal_slot=p['signal_slot'], signal_boundary=reservations[buy['order_id']],
                entry_slot=p['entry_slot'], entry_boundary=buy['boundary_ordinal'],
                entry_boundary_tx_idx=(buy['boundary_ordinal'] & ((1<<32)-1))-1,
                status=p['status'], net_pnl_lamports=p.get('closed_pnl_lamports'),
                profitable=p['status']=='CLOSED' and p['closed_pnl_lamports'] > 0,
                creator_status_at_signal=at_signal['status'], creator_status_at_entry=at_buy['status'],
                dev_bought_raw_by_entry=at_buy['bought'], dev_sold_raw_by_entry=at_buy['sold'],
                dev_observed_remaining_raw_at_entry=at_buy['remaining'],
                dev_last_full_exit_slot=sale['slot'] if sale else None,
                dev_last_full_exit_tx_idx=sale['tx_idx'] if sale else None,
                dev_last_full_exit_signature=sale['signature'] if sale else None,
                inventory_basis='retained launch-creator swaps only; transfers and linked wallets unproven',
            ))
    with (OUT / 'creator_exit_at_entry.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(result_rows[0]))
        writer.writeheader(); writer.writerows(result_rows)
    summaries={}
    for name in runs:
        rows=[r for r in result_rows if r['run']==name]
        winners=[r for r in rows if r['profitable']]
        losers=[r for r in rows if r['status']=='CLOSED' and r['net_pnl_lamports']<0]
        numerator=sum(r['creator_status_at_entry']=='SOLD_ALL' for r in winners)
        summaries[name]=dict(profitable_tokens=len(winners), dev_sold_all_before_entry=numerator,
            fraction=numerator/len(winners), profitable_dev_status_counts=dict(Counter(r['creator_status_at_entry'] for r in winners)),
            dev_sold_all_before_signal=sum(r['creator_status_at_signal']=='SOLD_ALL' for r in winners),
            losing_tokens=len(losers), losing_dev_sold_all_before_entry=sum(r['creator_status_at_entry']=='SOLD_ALL' for r in losers),
            all_filled=len(rows), all_filled_dev_sold_all_before_entry=sum(r['creator_status_at_entry']=='SOLD_ALL' for r in rows),
            sold_all_winner_tokens=[r['token'] for r in winners if r['creator_status_at_entry']=='SOLD_ALL'],
            sum_sold_all_winner_pnl_lamports=sum(r['net_pnl_lamports'] for r in winners if r['creator_status_at_entry']=='SOLD_ALL'),
            total_winner_pnl_lamports=sum(r['net_pnl_lamports'] for r in winners))
    receipt=dict(schema='dnipro.creator-exit-before-entry/v1',summary=summaries,
        run_result_sha256={name:sha(directory/'result.json') for name,directory in RUNS.items()},
        source_manifest_sha256=sha(SOURCE/'manifest.json'),csv_sha256=sha(OUT/'creator_exit_at_entry.csv'),
        criterion='creator previously bought and sold positive quantities; observed remaining zero; no negative prefix; all transactions strictly before buy landing',
        exact_holders=False)
    (OUT/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt,indent=2))
    for r in result_rows:
        if r['run']=='virtual' and r['profitable']:
            print(json.dumps(r))

if __name__=='__main__':
    main()
