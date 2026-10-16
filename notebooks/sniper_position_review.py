"""Read and plot preserved native results without rerunning a strategy."""
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import csv
import gzip
import hashlib
import html
import json
import sys

import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd
from IPython.display import HTML, Markdown, display

ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents]
            if (p / 'src/backtest').is_dir() and (p / 'var/dnipro').is_dir())
sys.path.insert(0, str(ROOT / 'src'))
from backtest.domain.hashing import domain_digest

RUN_ROOT = ROOT / 'var/dnipro/sniper-window-redesign-v1'
EXPORT = ROOT / 'var/dnipro/sniper-original-position-charts-20261014-v4'
EXPORT.mkdir(parents=True, exist_ok=True)
SOL = 'So11111111111111111111111111111111111111112'
LAMPORTS = 1_000_000_000
COHORTS = {
    'development': ('October 10, 16:00–17:00 UTC', '2026-10-10-1600-1700'),
}
RUN_IDS = ['development-conditional_fraction-before']
COLORS = {'before': '#69738b', 'after': '#1768b3'}
GOOD, BAD = '#14825e', '#c13e46'
plt.rcParams.update({'figure.dpi': 120, 'font.size': 10,
                     'axes.spines.top': False, 'axes.spines.right': False})
fonts = {f.name for f in font_manager.fontManager.ttflist}
plt.rcParams['font.family'] = [f for f in ['DejaVu Sans', 'Hiragino Sans GB',
                                           'Hiragino Sans'] if f in fonts]
pd.set_option('display.max_rows', 120)
pd.set_option('display.max_colwidth', 65)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def csv_rows(path):
    with Path(path).open() as stream:
        return list(csv.DictReader(stream))


def utc(value):
    return datetime.fromtimestamp(int(value), timezone.utc).isoformat()


def stamp(value):
    return int(datetime.fromisoformat(str(value).replace('Z', '+00:00'))
               .replace(tzinfo=timezone.utc).timestamp())


def linked_table(frame):
    data = frame.copy()
    for field in data.select_dtypes(include=['object', 'str']).columns:
        data[field] = data[field].map(lambda x: html.escape(str(x)) if pd.notna(x) else '')
    if 'gmgn_url' in frame:
        data['gmgn_url'] = [f'<a href="{html.escape(url, quote=True)}" target="_blank" '
                            'rel="noopener">GMGN</a>' for url in frame['gmgn_url']]
    display(HTML('<div style="max-height:650px;overflow:auto">' +
                 data.to_html(index=False, escape=False, na_rep='—',
                              float_format=lambda x: f'{x:,.9f}') + '</div>'))


def finish_plot(fig, filename):
    fig.savefig(EXPORT / filename, dpi=150, bbox_inches='tight')
    plt.show()
    plt.close(fig)


def read_gz(path):
    with gzip.open(path, 'rt') as stream:
        for line in stream:
            yield json.loads(line)


def verify_and_load():
    receipt = json.loads((RUN_ROOT / 'comparison_receipt.json').read_text())
    assert receipt['complete'] and receipt['runs'] == 30
    for name, digest in receipt['files'].items():
        assert sha(RUN_ROOT / name) == digest, name
    for name in RUN_IDS:
        assert sha(RUN_ROOT / name / 'result.json') == receipt['output_result_hashes'][name], name
    runs, positions, summaries = {}, [], []
    for name in RUN_IDS:
        folder = RUN_ROOT / name
        manifest = json.loads((folder / 'manifest.json').read_text())
        assert manifest['complete']
        for filename, digest in manifest['files'].items():
            assert sha(folder / filename) == digest, (name, filename)
        r = json.loads((folder / 'result.json').read_text())
        s = r['summary']
        assert s['own_reserve_impact_propagated'] and s['synthetic_shortfall_lamports'] == 0
        if s['proven_empty_slots']:
            assert s['proven_empty_slots'] == [455328000]
            assert sha(ROOT / 'var/dnipro/sniper-gap-455328000.json') == s['empty_block_evidence_sha256']
        for tx in r['ledger']:
            totals = defaultdict(int)
            for posting in tx['postings']:
                totals[posting['asset_id']] += posting['amount_atomic']
            assert all(v == 0 for v in totals.values()), tx['transaction_id']
        actual = sum(p.get('closed_pnl_lamports') or 0 for p in r['positions'])
        actual -= sum(p['failed_execution_cost_lamports'] for p in r['positions']
                      if not p['entry_cash_lamports'])
        assert actual == s['realized_net_pnl_lamports']
        cohort, version = name.split('-')[0], 'original'
        runs[name] = r
        summaries.append(dict(run=name, sample=COHORTS[cohort][0], version=version,
                              start_utc=s['cohort_start_utc'], end_utc=s['cohort_end_utc'],
                              launches=s['cohort_launches'], filled=s['filled_entries'],
                              closed=s['closed_positions'], open=s['open_positions'],
                              net_closed_pnl_SOL=actual / LAMPORTS,
                              open_cost_SOL=s['open_cash_cost_lamports'] / LAMPORTS,
                              publication_gaps=len(s['publication_gaps']),
                              repricing_conflicts=s['conditional_repricing_errors']))
        for p in r['positions']:
            if 'signal_slot' not in p:
                continue
            outcome = ('Profitable' if p['closed_pnl_lamports'] > 0 else
                       'Losing' if p['closed_pnl_lamports'] < 0 else 'Breakeven') \
                if p['status'] == 'CLOSED' else {
                    'OPEN': 'Open / unresolved', 'ENTRY_FAILED': 'Entry failed',
                    'ENTRY_REJECTED': 'Entry rejected',
                }.get(p['status'], p['status'])
            positions.append(dict(
                run=name, sample=COHORTS[cohort][0], version=version,
                token=p['symbol'].strip(), mint=p['mint'], gmgn_url=p['gmgn_url'],
                outcome=outcome, status=p['status'], reason=p['reason'],
                signal_slot=p['signal_slot'], signal_utc=utc(p['signal_time']),
                entry_slot=p.get('entry_slot'), exit_slot=p.get('exit_slot'),
                entry_SOL=p['entry_cash_lamports'] / LAMPORTS,
                exit_receipt_SOL=p.get('exit_receipt_lamports', 0) / LAMPORTS
                    if p['status'] == 'CLOSED' else None,
                net_pnl_SOL=p['closed_pnl_lamports'] / LAMPORTS
                    if p['status'] == 'CLOSED' else
                    -p['failed_execution_cost_lamports'] / LAMPORTS
                    if not p['entry_cash_lamports'] else None,
                failed_execution_fees_SOL=p['failed_execution_cost_lamports'] / LAMPORTS,
                open_cost_SOL=(p['entry_cash_lamports'] + p['failed_execution_cost_lamports'])
                    / LAMPORTS if p['status'] == 'OPEN' else 0,
                remaining_tokens_raw=p['remaining_tokens_atomic'],
                holding_seconds=p.get('exit_time', p.get('entry_time', 0))
                    - p.get('entry_time', 0) if p['status'] == 'CLOSED' else None,
                creator=p['creator'], reference_wallet=p.get('sniper_wallet'),
            ))
    original = runs[RUN_IDS[0]]['summary']
    assert all(s['cohort_start_utc'] == '2026-10-10T16:00:00Z'
               and s['cohort_end_utc'] == '2026-10-10T17:00:00Z' for s in [original])
    for field, value in dict(require_creator_never_bought=0, confirmation_slots=0,
                             confirmation_new_buyers=0, confirmation_net_lamports=0,
                             maximum_entry_budget_lamports=0, stop_loss_bps=0,
                             entry_window_slots=0, holding_seconds=300).items():
        assert original['policy'][field] == value, field
    return runs, pd.DataFrame(positions), pd.DataFrame(summaries), receipt


def load_source_context(runs):
    clocks, tapes, source_hashes = {}, defaultdict(list), {}
    for cohort, (_, snapshot) in COHORTS.items():
        wanted = {p['mint'] for name, r in runs.items() if name.startswith(cohort + '-')
                  for p in r['positions'] if 'signal_slot' in p}
        folder = ROOT.parent / 'solana-live-indexer/research/sniper_break_even/data' / snapshot
        manifest = json.loads((folder / 'manifest.json').read_text())
        assert manifest['complete'] and manifest['publication_unchanged']
        source_hashes[cohort] = sha(folder / 'manifest.json')
        clock, count = {}, 0
        for part in manifest['parts']:
            if not part['name'].endswith(('-clock', '-trades')):
                continue
            path = folder / 'snapshot' / part['file']
            assert sha(path) == part['file_sha256'], (cohort, part['file'])
            read_count = 0
            for row in read_gz(path):
                read_count += 1
                if part['name'].endswith('-clock'):
                    clock[int(row['slot'])] = stamp(row['observed_block_time'])
                else:
                    count += 1
                    if row['mint'] in wanted:
                        tapes[cohort, row['mint']].append(row)
            assert read_count == part['rows']
        reference = next(r for name, r in runs.items() if name.startswith(cohort + '-'))
        assert count == reference['summary']['retained_input_trade_events']
        clocks[cohort] = clock
    for values in tapes.values():
        values.sort(key=lambda x: (x['slot'], x['tx_idx'], tuple(x['instruction_path']), x['event_uid']))
    return clocks, tapes, source_hashes


def native_order_context(run_id, r):
    audit = csv_rows(RUN_ROOT / run_id / 'order_audit.csv')
    mapping = {f['order_id']: (f['pool_id'], 'BUY' if f['sold_asset_id'] == SOL else 'SELL')
               for f in r['fills']}
    component = domain_digest('dnipro.configured-sniper-window.v1', r['summary']['policy']).hex
    signaled = [p for p in r['positions'] if 'signal_slot' in p]
    for a in audit:
        order_id = a['order_id']
        if order_id in mapping:
            continue
        boundary = int(a['boundary_ordinal'])
        for p in signaled:
            if p['signal_slot'] == boundary >> 32:
                digest = domain_digest('dnipro.sniper-order.v1', dict(
                    mint=p['mint'], side='BUY', boundary=boundary, attempt=0, component=component)).hex
                if digest == order_id:
                    mapping[order_id] = p['mint'], 'BUY'
            if p.get('entry_slot', 10**15) <= boundary >> 32:
                for attempt in range(1, p['exit_attempts'] + 1):
                    digest = domain_digest('dnipro.sniper-order.v1', dict(
                        mint=p['mint'], side='SELL', boundary=boundary,
                        attempt=attempt, component=component)).hex
                    if digest == order_id:
                        mapping[order_id] = p['mint'], 'SELL'
    assert all(a['order_id'] in mapping for a in audit), run_id
    return audit, mapping


def ledger_frames(run_id, r, mapping, clock):
    rows, postings = [], []
    for tx in r['ledger']:
        mint, side = mapping[tx['correlation_id']]
        delta = sum(p['amount_atomic'] for p in tx['postings'] if p['asset_id'] == SOL
                    and p['account_kind'] in ['PORTFOLIO_AVAILABLE', 'PORTFOLIO_RESERVED'])
        fees = sum(p['amount_atomic'] for p in tx['postings'] if p['asset_id'] == SOL
                   and p['account_kind'] == 'NETWORK_FEE')
        slot = tx['boundary_ordinal'] >> 32
        rows.append(dict(run=run_id, mint=mint, side=side, slot=slot,
                         boundary_ordinal=tx['boundary_ordinal'], time=clock[slot],
                         utc=utc(clock[slot]), order_id=tx['correlation_id'],
                         reason=tx['reason'], cash_delta_lamports=delta,
                         cash_delta_SOL=delta / LAMPORTS, network_fee_SOL=fees / LAMPORTS))
        postings.extend(dict(run=run_id, mint=mint, side=side, slot=slot,
                             order_id=tx['correlation_id'], reason=tx['reason'], **p)
                        for p in tx['postings'])
    frame = pd.DataFrame(rows).sort_values('boundary_ordinal', kind='stable')
    s = r['summary']
    assert int(frame['cash_delta_lamports'].sum()) == (
        s['realized_net_pnl_lamports'] - s['open_cash_cost_lamports'])
    for p in r['positions']:
        if 'signal_slot' not in p:
            continue
        expected = p['closed_pnl_lamports'] if p['status'] == 'CLOSED' else \
            -p['entry_cash_lamports'] - p['failed_execution_cost_lamports']
        assert int(frame.loc[frame['mint'] == p['mint'], 'cash_delta_lamports'].sum()) == expected
    return frame, pd.DataFrame(postings)


def pnl_events(run_id, r, ledger):
    events = []
    for p in r['positions']:
        if p['status'] == 'CLOSED':
            events.append(dict(run=run_id, mint=p['mint'], token=p['symbol'].strip(),
                               slot=p['exit_slot'], time=p['exit_time'],
                               pnl_lamports=p['closed_pnl_lamports'], event='Position closed'))
        elif not p['entry_cash_lamports'] and p['failed_execution_cost_lamports']:
            for row in ledger[(ledger['mint'] == p['mint']) &
                              (ledger['cash_delta_lamports'] < 0)].to_dict('records'):
                events.append(dict(run=run_id, mint=p['mint'], token=p['symbol'].strip(),
                                   slot=row['slot'], time=row['time'],
                                   pnl_lamports=row['cash_delta_lamports'], event='Failed entry fee'))
    frame = pd.DataFrame(events).sort_values(['slot', 'mint'], kind='stable')
    assert int(frame['pnl_lamports'].sum()) == r['summary']['realized_net_pnl_lamports']
    frame['pnl_SOL'] = frame['pnl_lamports'] / LAMPORTS
    frame['cumulative_pnl_SOL'] = frame['pnl_lamports'].cumsum() / LAMPORTS
    frame['utc'] = frame['time'].map(utc)
    return frame


def entry_exit_records(run_id):
    """Native fills supply marker prices; no fills are inferred from history."""
    r = RUNS[run_id]
    rows = {p['mint']: p for p in r['positions']}
    cohort = run_id.split('-')[0]
    markers = []
    for fill in r['fills']:
        mint, order_id = fill['pool_id'], fill['order_id']
        p = rows[mint]
        side = 'BUY' if fill['sold_asset_id'] == SOL else 'SELL'
        venue = POSTINGS[run_id]
        cash = venue[(venue['order_id'] == order_id) & (venue['reason'] == 'ORDER_SETTLEMENT') &
                     (venue['account_kind'] == 'VENUE') & (venue['asset_id'] == SOL)]
        assert len(cash) == 1, order_id
        signed_curve = int(cash['amount_atomic'].iloc[0])
        assert signed_curve > 0 if side == 'BUY' else signed_curve < 0
        tokens = int(fill['amount_out_atomic'] if side == 'BUY' else fill['amount_in_atomic'])
        first = TAPES[cohort, mint][0]
        reference = int(first['virtual_quote']) / int(first['virtual_base'])
        average_index = abs(signed_curve) / tokens / reference * 100
        slot = fill['boundary_ordinal'] >> 32
        field = 'entry' if side == 'BUY' else 'exit'
        assert slot == p[field + '_slot']
        markers.append(dict(mint=mint, token=p['symbol'].strip(), side=side, slot=slot,
                            utc=utc(p[field + '_time']), order_id=order_id,
                            average_curve_price_index=average_index,
                            gross_curve_SOL=abs(signed_curve) / LAMPORTS, tokens_raw=tokens))
    marker_frame = pd.DataFrame(markers)
    signaled = []
    groups = {'Profitable': 'Profitable', 'Losing': 'Unprofitable',
              'Open / unresolved': 'Undecided', 'Entry failed': 'Entry failed',
              'Entry rejected': 'Entry rejected', 'Breakeven': 'Breakeven'}
    for row in POSITIONS.to_dict('records'):
        p = rows[row['mint']]
        native = marker_frame[marker_frame['mint'] == row['mint']]
        buy, sell = native[native['side'] == 'BUY'], native[native['side'] == 'SELL']
        assert len(buy) == int(p['entry_cash_lamports'] > 0)
        assert len(sell) == int(p['status'] == 'CLOSED')
        row.update(group=groups[row['outcome']],
                   entry_utc=utc(p['entry_time']) if len(buy) else None,
                   exit_utc=utc(p['exit_time']) if len(sell) else None,
                   entry_average_curve_price_index=float(buy['average_curve_price_index'].iloc[0]) if len(buy) else None,
                   exit_average_curve_price_index=float(sell['average_curve_price_index'].iloc[0]) if len(sell) else None,
                   entry_order_id=buy['order_id'].iloc[0] if len(buy) else None,
                   exit_order_id=sell['order_id'].iloc[0] if len(sell) else None)
        signaled.append(row)
    frame = pd.DataFrame(signaled).sort_values(['signal_utc', 'mint'])
    for field in ['signal_slot', 'entry_slot', 'exit_slot']:
        frame[field] = frame[field].astype('Int64')
    return frame, marker_frame


def draw_entry_exit(ax, run_id, mint):
    p = next(p for p in RUNS[run_id]['positions'] if p['mint'] == mint)
    launch = p['launch_slot']
    cohort = run_id.split('-')[0]
    all_tape = pd.DataFrame(TAPES[cohort, mint])
    markers = FILL_MARKERS[FILL_MARKERS['mint'] == mint]
    audit = [a for a in AUDITS[run_id] if ORDER_MAPS[run_id][a['order_id']][0] == mint]
    attempts = [a for a in audit if a['record_type'] in
                ['ORDER_REJECTED', 'ORDER_EXECUTION_FAILED_AND_RELEASED']]
    if p['status'] == 'CLOSED':
        end = p['exit_slot'] + 30
    elif p['status'] == 'OPEN':
        end = max(p['entry_slot'], int(all_tape['slot'].max()))
    else:
        end = max([p['signal_slot'], *[int(a['boundary_ordinal']) >> 32 for a in attempts]]) + 30
    tape = all_tape[all_tape['slot'] <= end]
    reference = int(all_tape.iloc[0]['virtual_quote']) / int(all_tape.iloc[0]['virtual_base'])
    index = tape['virtual_quote'].astype(float) / tape['virtual_base'].astype(float) / reference * 100
    ax.step(tape['slot'] - launch, index, where='post', color='#7764a8', linewidth=1.4,
            label='Observed historical curve price')
    ax.axvline(p['signal_slot'] - launch, color='#b28725', linestyle=':',
               label=f"Signal: slot {p['signal_slot']}", alpha=.8)
    for marker in markers.to_dict('records'):
        is_buy = marker['side'] == 'BUY'
        color = GOOD if is_buy else BAD
        x = marker['slot'] - launch
        label = ('BUY' if is_buy else 'SELL') + f" filled: slot {marker['slot']} ({marker['utc'][11:19]} UTC)"
        ax.axvline(x, color=color, linestyle='--', alpha=.75)
        ax.scatter([x], [marker['average_curve_price_index']], s=100,
                    marker='^' if is_buy else 'v', color=color, edgecolors='white',
                    linewidths=.7, zorder=5, label=label)
    if not len(markers):
        for attempt in attempts:
            slot = int(attempt['boundary_ordinal']) >> 32
            ax.axvline(slot - launch, color='#777777', linestyle='--',
                       label=f'Unfilled attempt: slot {slot}', alpha=.6)
    span = max(end - launch, 5)
    ax.set_xlim(-max(2, span * .015), span + max(2, span * .015))
    ax.set_xlabel(f'Slots since launch {launch}')
    ax.set_ylabel('Price index (first retained trade = 100)')
    ax.grid(alpha=.15)
    return end, markers


def position_entry_exit_chart(run_id, mint):
    row = ENTRY_EXIT[ENTRY_EXIT['mint'] == mint].iloc[0]
    p = next(p for p in RUNS[run_id]['positions'] if p['mint'] == mint)
    fig, axes = plt.subplots(2, 1, figsize=(12, 6), sharex=True,
                             gridspec_kw={'height_ratios': [3, 1]}, layout='constrained')
    end, markers = draw_entry_exit(axes[0], run_id, mint)
    launch = p['launch_slot']
    axes[0].legend(fontsize=8, loc='upper right')
    if end - launch > 120:
        zoom = axes[0].inset_axes([.25, .43, .32, .50])
        early = pd.DataFrame(TAPES[run_id.split('-')[0], mint])
        early = early[early['slot'] <= launch + 60]
        first = TAPES[run_id.split('-')[0], mint][0]
        reference = int(first['virtual_quote']) / int(first['virtual_base'])
        price = early['virtual_quote'].astype(float) / early['virtual_base'].astype(float) / reference * 100
        zoom.step(early['slot'] - launch, price, where='post', color='#7764a8')
        zoom.axvline(p['signal_slot'] - launch, color='#b28725', linestyle=':', alpha=.8)
        for marker in markers.to_dict('records'):
            if marker['slot'] - launch <= 60:
                is_buy = marker['side'] == 'BUY'
                color = GOOD if is_buy else BAD
                zoom.axvline(marker['slot'] - launch, color=color, linestyle='--', alpha=.8)
                zoom.scatter([marker['slot'] - launch], [marker['average_curve_price_index']],
                             marker='^' if is_buy else 'v', s=45, color=color, zorder=5)
        zoom.set_xlim(-2, 62)
        zoom.set_title('Entry zoom: first 60 slots', fontsize=8)
        zoom.tick_params(labelsize=7)
        zoom.grid(alpha=.15)
    cash = LEDGERS[run_id]
    cash = cash[(cash['mint'] == mint) & (cash['cash_delta_lamports'] != 0)]
    final = int(cash['cash_delta_lamports'].sum()) / LAMPORTS
    axes[1].step([0, *(cash['slot'] - launch), end - launch],
                 [0, *(cash['cash_delta_lamports'].cumsum() / LAMPORTS), final],
                 where='post', color=COLORS['before'])
    axes[1].set_ylabel('Cash change (SOL)')
    axes[1].grid(alpha=.15)
    net = (f"Net closed PnL {row['net_pnl_SOL']:+.9f} SOL" if p['status'] == 'CLOSED' else
           f"No filled exit; committed cost {row['open_cost_SOL']:.9f} SOL" if p['status'] == 'OPEN' else
           f"No filled entry; charged fees {row['failed_execution_fees_SOL']:.9f} SOL")
    axes[1].text(.99, .93, net, transform=axes[1].transAxes, ha='right', va='top', fontsize=9,
                 bbox=dict(facecolor='white', edgecolor='none', alpha=.85))
    axes[1].set_xlabel(f'Slots since launch {launch}')
    fig.suptitle(f"{row['token']} — {row['group']}\n{mint}", fontsize=11)
    folder = EXPORT / 'positions'
    folder.mkdir(exist_ok=True)
    filename = 'positions/' + mint + '.png'
    finish_plot(fig, filename)
    return filename


def inspect_position(run_id, mint):
    r = RUNS[run_id]
    p = next(p for p in r['positions'] if p['mint'] == mint and 'signal_slot' in p)
    display(Markdown(f"### {p['symbol'].strip()} — {p['status']}: {p['reason']}"))
    linked_table(POSITIONS[(POSITIONS['run'] == run_id) & (POSITIONS['mint'] == mint)])
    cohort = run_id.split('-')[0]
    tape = pd.DataFrame(TAPES[cohort, mint])
    launch = p['launch_slot']
    limit = max(p.get('exit_slot', 0), p.get('entry_slot', p['signal_slot']))
    limit = min(max(int(tape['slot'].max()) if len(tape) else limit, limit), limit + 45)
    if p['status'] == 'OPEN' and len(tape):
        limit = max(limit, int(tape['slot'].max()))
    fig, axes = plt.subplots(3, 1, figsize=(11, 8), sharex=True, layout='constrained')
    if len(tape):
        tape = tape[tape['slot'] <= limit]
        price = tape['virtual_quote'].astype(float) / tape['virtual_base'].astype(float)
        axes[0].step(tape['slot'] - launch, price / price.iloc[0] * 100,
                     where='post', color='#7764a8', label='Observed historical curve price index')
        axes[1].step(tape['slot'] - launch, tape['real_quote'].astype(float) / LAMPORTS,
                     where='post', color='#4595a2', label='Observed real SOL reserve')
    cash = LEDGERS[run_id]
    cash = cash[(cash['mint'] == mint) & (cash['cash_delta_lamports'] != 0)].copy()
    final_cash = int(cash['cash_delta_lamports'].sum()) / LAMPORTS
    axes[2].step([0, *(cash['slot'] - launch), limit - launch],
                 [0, *(cash['cash_delta_lamports'].cumsum() / LAMPORTS), final_cash],
                 where='post', color=COLORS[run_id.split('-')[-1]], label='Native position cash change')
    axes[2].annotate(f'{final_cash:+.9f} SOL', (limit - launch, final_cash),
                     xytext=(-8, 12), textcoords='offset points', ha='right')
    for field, label, color in [('signal_slot', 'Signal', '#b28725'),
                                 ('entry_slot', 'Buy filled', GOOD),
                                 ('exit_slot', 'Sell filled', BAD)]:
        if field in p:
            for ax in axes:
                ax.axvline(p[field] - launch, color=color, linestyle='--', alpha=.8,
                           label=f"{label}: slot {p[field]}")
    for ax in axes:
        ax.grid(alpha=.15)
        ax.legend(fontsize=8, loc='best')
    axes[0].set_ylabel('Price index (first trade = 100)')
    axes[1].set_ylabel('Historical reserves (SOL)')
    axes[2].set_ylabel('Cash change (SOL)')
    axes[2].set_xlabel(f'Slots since launch {launch}')
    fig.suptitle(f"{p['symbol'].strip()} — original strategy, {COHORTS[cohort][0]}")
    finish_plot(fig, f"case-{run_id}-{mint[:8]}.png")
    display(Markdown('The upper two plots show historical context. The simulation has different '
                     'reserves after our intervention. Cash change includes capital spent and is '
                     'not marked PnL while the position is open.'))
    audit = [dict(a, mint=ORDER_MAPS[run_id][a['order_id']][0],
                  side=ORDER_MAPS[run_id][a['order_id']][1],
                  slot=int(a['boundary_ordinal']) >> 32) for a in AUDITS[run_id]
             if ORDER_MAPS[run_id][a['order_id']][0] == mint]
    display(Markdown('#### Native order log'))
    display(pd.DataFrame(audit))
    display(Markdown('#### Native cash and fee log'))
    display(LEDGERS[run_id][LEDGERS[run_id]['mint'] == mint]
            [['slot', 'utc', 'side', 'reason', 'cash_delta_SOL', 'network_fee_SOL', 'order_id']])
    conflicts = [e for e in csv_rows(RUN_ROOT / run_id / 'conditional_errors.csv')
                 if e.get('mint') == mint]
    if conflicts:
        display(Markdown('#### Unresolved counterparty context'))
        display(pd.DataFrame(conflicts))
    display(Markdown('Signal snapshot: reference buyer cost **'
                     f"{p['sniper_cost_lamports']/LAMPORTS:.9f} SOL**, "
                     f"gross buy budget **{p['budget_lamports']/LAMPORTS:.9f} SOL**, "
                     f"our threshold **{p['signal_our_threshold']}**, "
                     f"reference threshold **{p['signal_sniper_threshold']}**. "
                     'These are the recorded native signal features. Per-observation strategy '
                     'traces were not enabled in these saved runs.'))
