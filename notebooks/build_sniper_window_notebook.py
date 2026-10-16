"""Build and optionally execute the native Sniper Window result notebook."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from textwrap import dedent

import nbformat

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / 'notebooks/sniper_window_20261010.ipynb'
cells = []


def md(text):
    cells.append(nbformat.v4.new_markdown_cell(dedent(text).strip()))


def code(text):
    cells.append(nbformat.v4.new_code_cell(dedent(text).strip()))


md('''
# Sniper Window buys and backtest outcomes

This notebook inspects the **actual forked engine's saved results**, not a new
strategy simulation. The cohort is **October 10, 2026, 16:00–17:00 UTC
(19:00–20:00 Kyiv)**, with retained observations through 17:16:22 UTC.

It includes every filled simulated buy, historical curve charts with entry and
exit markers, fees and PnL, both settlement modes, and detailed diagnostics for
the **seven positions left open by the strict historical-liquidity check**.
All wallet and token links go to GMGN.

**Sell failure interpretation:** those seven had **zero submitted sell attempts**.
The strategy rejected their full-position quotes before creating an order. A
full sell submitted against that same recorded state would fail the modeled
real-SOL reserve check, but our replay omits our buy's added SOL and token reserve
changes from later states. This does **not** prove that the real strategy could
never sell. A smaller sale may fit the recorded reserves, another state may have
more liquidity, and a consistent counterfactual replay would have different reserves.

Creator-fee income is included in each reference buyer's observed break-even.
Developer accrual and its effect on cashflow are shown separately below.

The completed virtual-settlement result uses explicit assumed liquidity where
necessary. It is a **modeled result, not actual on-chain PnL**.
''')

code('''
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone
from bisect import bisect_left
import csv, gzip, hashlib, html, json, sys
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from IPython.display import display, HTML, Markdown

candidates = [Path.cwd(), *Path.cwd().parents]
ROOT = next(p for p in candidates if (p / 'src/backtest').is_dir() and (p / 'var/dnipro').is_dir())
sys.path.insert(0, str(ROOT / 'src'))
from backtest.plugins.protocols.pumpfun.model import (
    PumpCurveStateV1, PumpCurveLifecycle, PumpMode, PumpQuoteError,
    PumpQuoteErrorCode, PumpSellLiquidityPolicy, sell_quote,
)
from backtest.plugins.execution.pump_sniper import profile
from backtest.plugins.strategies.creator_income import CreatorFeeIncome, allocate_creator_fee

RUN_DIRS = {
    'strict': ROOT / 'var/dnipro/sniper-window-20261010-slot-traces-strict-v1',
    'virtual': ROOT / 'var/dnipro/sniper-window-20261010-slot-traces-virtual-v1',
}
SOURCE = ROOT.parent / 'solana-live-indexer/research/sniper_break_even/data/2026-10-10-1600-1700'
EXPORT = ROOT / 'var/dnipro/sniper-window-20261010-slot-traces-notebook-v1'
EXPORT.mkdir(parents=True, exist_ok=True)
SOL = 'So11111111111111111111111111111111111111112'
LAMPORTS = 1_000_000_000
pd.set_option('display.max_rows', 60)
pd.set_option('display.max_colwidth', 70)
plt.rcParams.update({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False})
available_fonts = {f.name for f in font_manager.fontManager.ttflist}
plt.rcParams['font.family'] = [name for name in ['DejaVu Sans', 'Hiragino Sans GB', 'Hiragino Sans', 'Noto Sans CJK JP'] if name in available_fonts]

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def utc(seconds):
    return datetime.fromtimestamp(int(seconds), timezone.utc).isoformat()

def parse_time(value):
    if isinstance(value, (int, float)):
        return int(value)
    return int(datetime.fromisoformat(value.replace('Z', '+00:00')).replace(tzinfo=timezone.utc).timestamp())

def read_gz(path):
    with gzip.open(path, 'rt') as stream:
        return [json.loads(line) for line in stream]

def linked_table(frame, link_column='gmgn_url', label_column='token', floats=6):
    data = frame.copy()
    if link_column in data and label_column in data:
        data[label_column] = [f'<a href="{html.escape(str(url), quote=True)}" target="_blank" rel="noopener">{html.escape(str(label))}</a>' for label, url in zip(data[label_column], data[link_column])]
        data = data.drop(columns=link_column)
    display(HTML(data.to_html(index=False, escape=False, float_format=lambda x: f'{x:,.{floats}f}', na_rep='—')))
''')

md('''
## Verify the saved evidence

Every native output and frozen export is checked against its completion
manifest. This notebook makes no ClickHouse or RPC requests. The ledger is
checked in integer lamports and raw token units; SOL values and plots are views.
''')

code('''
runs = {}
for name, folder in RUN_DIRS.items():
    manifest = json.loads((folder / 'manifest.json').read_text())
    assert manifest['complete'], name
    for filename, digest in manifest['files'].items():
        assert sha(folder / filename) == digest, (name, filename)
    runs[name] = json.loads((folder / 'result.json').read_text())
    for transaction in runs[name]['ledger']:
        totals = defaultdict(int)
        for posting in transaction['postings']:
            totals[posting['asset_id']] += posting['amount_atomic']
        assert all(v == 0 for v in totals.values()), transaction['transaction_id']
    summary = runs[name]['summary']
    cash = next(b[3] for b in summary['engine_summary']['final_balances'] if b[0] == 'portfolio:available:' + SOL and b[2] == SOL)
    assert cash - summary['policy']['bankroll_lamports'] == summary['realized_net_pnl_lamports'] - summary['open_cash_cost_lamports']

source_manifest = json.loads((SOURCE / 'manifest.json').read_text())
assert source_manifest['complete']
assert sha(SOURCE / 'manifest.json') == runs['virtual']['summary']['source_manifest_sha256']
assert runs['strict']['summary']['source_input_sha256'] == runs['virtual']['summary']['source_input_sha256']
assert runs['strict']['summary']['policy'] == runs['virtual']['summary']['policy']
parts = [source_manifest['births'], *source_manifest['parts'], *source_manifest['metadata']]
for part in parts:
    path = SOURCE / 'snapshot' / part['file']
    assert sha(path) == part['file_sha256'], part['name']
    assert len(read_gz(path)) == part['rows'], part['name']

birth_list = read_gz(SOURCE / 'snapshot' / source_manifest['births']['file'])
births = {b['mint']: b for b in birth_list}
clock = {}
all_trades = []
for part in source_manifest['parts']:
    rows = read_gz(SOURCE / 'snapshot' / part['file'])
    if part['name'].endswith('-clock'):
        clock.update({int(r['slot']): parse_time(r['observed_block_time']) for r in rows})
    elif part['name'].endswith('-trades'):
        for row in rows:
            row['payload'] = json.loads(row['decoded_extension_json'] or '{}')
            row['time'] = parse_time(row['block_time'] if row['block_time'] is not None else row['payload']['timestamp'])
            row['boundary'] = (int(row['slot']) << 32) + int(row['tx_idx']) + 1
            clock.setdefault(int(row['slot']), row['time'])
        all_trades.extend(rows)
assert len(all_trades) == runs['virtual']['summary']['retained_input_trade_events']
rows_by_run = {name: {r['mint']: r for r in run['positions']} for name, run in runs.items()}
blocked_mints = [r['mint'] for r in runs['strict']['positions'] if r['status'] == 'OPEN' and r['reason'] == 'INSUFFICIENT_REAL_SOL_RESERVES']
filled_mints = [r['mint'] for r in runs['virtual']['positions'] if r['status'] == 'CLOSED']
trades_by_mint = defaultdict(list)
for row in all_trades:
    if row['mint'] in filled_mints:
        trades_by_mint[row['mint']].append(row)
for mint in trades_by_mint:
    trades_by_mint[mint].sort(key=lambda r: (r['slot'], r['tx_idx'], tuple(r['instruction_path']), r['event_uid']))
for mint in blocked_mints:
    assert rows_by_run['strict'][mint]['exit_attempts'] == 0
    assert rows_by_run['strict'][mint]['entry_time'] + runs['strict']['summary']['policy']['holding_seconds'] < max(clock.values())

print(f'Verified {len(births):,} launches, {len(all_trades):,} retained trades and both native ledgers.')
print('Final retained clock:', utc(max(clock.values())))
print('Seven open positions have data beyond their five-minute deadlines; none submitted a sell.')
''')

md('''
## Results and execution assumptions

Strict replay caps a sell by the recorded real-SOL reserve. Virtual settlement
uses the same full-position integer curve quote and records any funding shortfall
separately. Both keep later historical reserves unchanged by our orders.

The policy starts with 10 SOL, targets 5% net return, waits one slot for buys and
sells, exits after five minutes if the target is not reached, and uses 5% minimum
output tolerance. Network/priority cost is 0.000105 SOL per submission, tips are
zero, and account rent is 0.002039280 SOL, refunded on a completed full sale.
''')

code('''
summary_rows = []
for name, run in runs.items():
    s = run['summary']
    summary_rows.append({
        'run': name, 'settlement_mode': s['execution_mode'], 'signals': s['signals'],
        'filled_buys': s['filled_entries'], 'closed': s['closed_positions'], 'open': s['open_positions'],
        'net_realized_or_modeled_pnl_SOL': s['realized_net_pnl_lamports'] / LAMPORTS,
        'open_cash_cost_SOL': s['open_cash_cost_lamports'] / LAMPORTS,
        'available_cash_SOL': float(s['final_available_sol']),
        'assumed_gross_liquidity_SOL': s.get('synthetic_shortfall_lamports', 0) / LAMPORTS,
    })
summary_table = pd.DataFrame(summary_rows)
display(summary_table)
print('The strict PnL is partial. The virtual PnL is complete under its explicit funding assumption.')
''')

md('''
## Every filled simulated buy

These are our hypothetical orders, not a wallet's observed on-chain transactions.
Token quantities stay in raw integer units because launch decimals are not
independently established. Cash cost includes protocol/creator fees, network
cost and posted rent. The selected early buyer's observed wallet is supplied
for comparison; its role does not prove private intent or ownership.
''')

code('''
settlements = {name: {tx['correlation_id']: tx for tx in run['ledger'] if tx['reason'] == 'ORDER_SETTLEMENT'} for name, run in runs.items()}
fill_by_run_mint = {}
buy_records = []
for name, run in runs.items():
    by_mint = defaultdict(dict)
    for fill in run['fills']:
        side = 'BUY' if fill['sold_asset_id'] == SOL else 'SELL'
        by_mint[fill['pool_id']][side] = fill
    fill_by_run_mint[name] = by_mint
    for mint, sides in by_mint.items():
        fill = sides['BUY']
        position = rows_by_run[name][mint]
        tx = settlements[name][fill['order_id']]
        amounts = defaultdict(int)
        for p in tx['postings']:
            if p['asset_id'] == SOL and p['amount_atomic'] > 0:
                amounts[p['account_kind']] += p['amount_atomic']
        cost = sum(amounts[k] for k in ['VENUE', 'PROTOCOL_FEE', 'CREATOR_FEE', 'NETWORK_FEE', 'PORTFOLIO_LOCKED'])
        assert cost == position['entry_cash_lamports']
        buy_records.append({
            'run': name, 'token': position['symbol'], 'mint': mint, 'gmgn_url': position['gmgn_url'],
            'buy_UTC': utc(position['entry_time']), 'buy_slot': position['entry_slot'],
            'tokens_raw': fill['amount_out_atomic'], 'cash_cost_SOL': cost / LAMPORTS,
            'curve_deposit_SOL': amounts['VENUE'] / LAMPORTS,
            'protocol_fee_SOL': amounts['PROTOCOL_FEE'] / LAMPORTS,
            'creator_fee_SOL': amounts['CREATOR_FEE'] / LAMPORTS,
            'network_fee_SOL': amounts['NETWORK_FEE'] / LAMPORTS,
            'posted_rent_SOL': amounts['PORTFOLIO_LOCKED'] / LAMPORTS,
            'early_buyer_gmgn': position['sniper_gmgn_url'],
            'early_buyer_observed_cost_SOL': position['sniper_cost_lamports'] / LAMPORTS,
            'strict_open_due_to_liquidity': mint in blocked_mints,
            'order_id': fill['order_id'],
        })
all_buys = pd.DataFrame(buy_records).sort_values(['run', 'buy_slot'])
virtual_buys = all_buys[all_buys['run'] == 'virtual'].reset_index(drop=True)
linked_table(virtual_buys[['token', 'gmgn_url', 'buy_UTC', 'buy_slot', 'cash_cost_SOL', 'tokens_raw', 'early_buyer_observed_cost_SOL', 'strict_open_due_to_liquidity']])
print('All 49 filled buy records across both runs are exported; this view shows the 27 virtual-run buys.')
''')

md('''
## Per-token PnL and outcomes in both runs

A blank strict PnL means the position remains open or was not entered; it is not
zero profit. The virtual run can enter five additional tokens after earlier
exits release capital. Two original completed positions also change exit time,
so its final result is not the old PnL plus only seven added sales.
''')

code('''
outcome_records = []
for mint in filled_mints:
    v, s = rows_by_run['virtual'][mint], rows_by_run['strict'][mint]
    outcome_records.append({
        'token': v['symbol'], 'mint': mint, 'gmgn_url': v['gmgn_url'],
        'buy_UTC': utc(v['entry_time']), 'buy_cost_SOL': v['entry_cash_lamports'] / LAMPORTS,
        'virtual_exit_UTC': utc(v['exit_time']), 'virtual_exit_reason': v['reason'],
        'virtual_net_pnl_SOL': v['closed_pnl_lamports'] / LAMPORTS,
        'strict_status': s['status'], 'strict_reason': s['reason'],
        'strict_net_pnl_SOL': s['closed_pnl_lamports'] / LAMPORTS if s.get('closed_pnl_lamports') is not None else np.nan,
        'assumed_liquidity_SOL': v.get('exit_synthetic_shortfall_lamports', 0) / LAMPORTS,
        'formerly_open': mint in blocked_mints,
    })
outcomes = pd.DataFrame(outcome_records).sort_values('virtual_net_pnl_SOL', ascending=False).reset_index(drop=True)
linked_table(outcomes[['token', 'gmgn_url', 'buy_cost_SOL', 'virtual_net_pnl_SOL', 'virtual_exit_reason', 'strict_status', 'strict_net_pnl_SOL', 'assumed_liquidity_SOL']])
assert sum(r['closed_pnl_lamports'] for r in runs['virtual']['positions'] if r['status'] == 'CLOSED') - runs['virtual']['summary']['failed_entry_cost_lamports'] == runs['virtual']['summary']['realized_net_pnl_lamports']
''')

code('''
ordered = outcomes.sort_values('virtual_net_pnl_SOL')
fig, ax = plt.subplots(figsize=(12, 9), layout='constrained')
bars = ax.barh(ordered['token'], ordered['virtual_net_pnl_SOL'], color=['#16875c' if n > 0 else '#b34343' for n in ordered['virtual_net_pnl_SOL']])
for bar, shortfall in zip(bars, ordered['assumed_liquidity_SOL']):
    if shortfall > 0:
        bar.set_hatch('///')
        bar.set_edgecolor('#593d1b')
ax.axvline(0, color='#555', linewidth=1)
ax.set_xlabel('Modeled net PnL (SOL), configured costs included')
ax.set_title('All 27 completed positions; hatching marks assumed liquidity')
ax.legend(handles=[Patch(facecolor='white', edgecolor='#593d1b', hatch='///', label='Exit exceeds historical real SOL')], loc='lower right')
fig.savefig(EXPORT / 'pnl_all_positions.png', dpi=160)
plt.show()
''')

md('''
## Historical curves with our buys and exits

The line is **historical curve valuation in SOL**, computed as virtual SOL / raw
virtual tokens × raw total supply. It is a pricing proxy, not a USD GMGN market
cap. The triangle and exit markers use each hypothetical fill's **average curve
execution price**, expressed on the same valuation scale, before external fees.
They can differ from the historical spot line because each order has size impact.

Purple stars show the selected early buyer's observed buys up to the actual
signal transaction. Green triangles mark our simulated buys. Blue exits fit
historical liquidity; orange exits use assumed liquidity. The dashed line marks
the five-minute deadline only for the seven strict-open positions. Each view
covers the launch through the relevant exit/deadline plus 30 seconds. Retained
curve history is kept separate from any migration-pool history.
Dashed horizontal tails show the last observed state carried forward by this
replay; they are not additional observed transactions or interpolated prices.
''')

code('''
reservations = {name: {tx['correlation_id']: tx['boundary_ordinal'] for tx in run['ledger'] if tx['reason'] == 'ORDER_RESERVATION'} for name, run in runs.items()}
buy_lookup = {(r['run'], r['mint']): r for r in buy_records}

def history(mint):
    birth = births[mint]
    launch_time = parse_time(birth['block_time'] if birth['block_time'] is not None else json.loads(birth['decoded_extension_json'])['timestamp'])
    supply = int(birth['supply_raw'])
    rows = trades_by_mint[mint]
    frame = pd.DataFrame({
        'minutes': [0.0] + [(r['time'] - launch_time) / 60 for r in rows],
        'boundary': [(int(birth['slot']) << 32) + int(birth['tx_idx']) + 1] + [r['boundary'] for r in rows],
        'virtual_sol_raw': [int(birth['initial_virtual_quote_raw'])] + [int(r['virtual_quote']) for r in rows],
        'virtual_token_raw': [int(birth['initial_virtual_base_raw'])] + [int(r['virtual_base']) for r in rows],
        'real_sol_raw': [int(birth.get('initial_real_quote_raw') or 0)] + [int(r['real_quote']) for r in rows],
    })
    frame['valuation_SOL'] = frame['virtual_sol_raw'] / frame['virtual_token_raw'] * supply / LAMPORTS
    return frame, launch_time, supply

def token_curve(ax, mint, legend=False):
    p = rows_by_run['virtual'][mint]
    frame, launch_time, supply = history(mint)
    deadline = p['entry_time'] + runs['virtual']['summary']['policy']['holding_seconds']
    end_time = max(p['exit_time'], deadline if mint in blocked_mints else p['exit_time']) + 30
    end_min = (min(end_time, max(clock.values())) - launch_time) / 60
    frame = frame[frame['minutes'] <= end_min]
    ax.step(frame['minutes'], frame['valuation_SOL'], where='post', color='#526b89', linewidth=1.3)
    last = frame.iloc[-1]
    if last['minutes'] < end_min:
        ax.plot([last['minutes'], end_min], [last['valuation_SOL']] * 2, color='#526b89', linestyle='--', alpha=0.6)
    buy = fill_by_run_mint['virtual'][mint]['BUY']
    decision = reservations['virtual'][buy['order_id']]
    selected = [r for r in trades_by_mint[mint] if r['trader'] == p['sniper_wallet'] and r['side'] == 'buy' and r['boundary'] <= decision]
    ax.scatter([(r['time'] - launch_time) / 60 for r in selected], [r['virtual_quote'] / r['virtual_base'] * supply / LAMPORTS for r in selected], marker='*', s=70, color='#8c55ad', zorder=6)
    buy_value = buy_lookup['virtual', mint]['curve_deposit_SOL'] / buy['amount_out_atomic'] * supply
    ax.scatter([(p['entry_time'] - launch_time) / 60], [buy_value], marker='^', s=85, color='#16875c', zorder=7)
    sell = fill_by_run_mint['virtual'][mint]['SELL']
    sell_value = p['exit_gross_curve_lamports'] / LAMPORTS / sell['amount_in_atomic'] * supply
    synthetic = p['exit_synthetic_shortfall_lamports'] > 0
    ax.scatter([(p['exit_time'] - launch_time) / 60], [sell_value], marker='X' if synthetic else 'v', s=85, color='#c07824' if synthetic else '#1766a7', zorder=7)
    if mint in blocked_mints:
        ax.axvline((deadline - launch_time) / 60, color='#c07824', linestyle='--', alpha=0.8)
    ax.set_xlim(-0.04, max(end_min, 0.12))
    ax.set_xlabel('Minutes after launch')
    ax.set_ylabel('Curve valuation (SOL)')
    status = ' | strict OPEN' if mint in blocked_mints else ''
    ax.set_title(f"{p['symbol']} | modeled PnL {p['closed_pnl_lamports'] / LAMPORTS:+.4f} SOL{status}", fontsize=10)
    ax.grid(alpha=0.18)
    if legend:
        ax.legend(handles=[
            Line2D([], [], color='#526b89', label='Historical curve'),
            Line2D([], [], color='#526b89', linestyle='--', label='Last state held in replay'),
            Line2D([], [], marker='*', linestyle='', color='#8c55ad', label='Selected early buyer'),
            Line2D([], [], marker='^', linestyle='', color='#16875c', label='Our buy: average price'),
            Line2D([], [], marker='v', linestyle='', color='#1766a7', label='Our sell: no shortfall'),
            Line2D([], [], marker='X', linestyle='', color='#c07824', label='Our sell: assumed liquidity'),
        ], fontsize=8)

chronological = sorted(filled_mints, key=lambda m: rows_by_run['virtual'][m]['entry_slot'])
for start in range(0, len(chronological), 9):
    fig, axes = plt.subplots(3, 3, figsize=(17, 12), layout='constrained')
    for ax, mint in zip(axes.flat, chronological[start:start + 9]):
        token_curve(ax, mint)
    for ax in list(axes.flat)[len(chronological[start:start + 9]):]:
        ax.set_visible(False)
    page = start // 9 + 1
    fig.suptitle(f'Native backtest entries and exits — page {page}', fontsize=15)
    fig.savefig(EXPORT / f'entry_exit_page_{page}.png', dpi=160)
    plt.show()
''')

md('''
## Seven liquidity-blocked positions and partial-sale capacity

The full-position sell quote below is reconstructed at the virtual run's actual
exit boundary from the last historical transaction **before** that boundary.
The existing Pump quote function confirms the strict rejection. The partial-sale
quantity is the largest raw balance whose gross curve output fits the recorded
real reserve; its quote is then checked by the same upstream function.

Partial-sale proceeds exclude rent refund because the account still holds tokens.
The net amount subtracts one configured network fee. A negative amount means the
small sale would cost more in network fees than it returns. This is a diagnostic
of the **recorded state**, not a repaired counterfactual strategy.
''')

code('''
def state_before(mint, boundary):
    tape = trades_by_mint[mint]
    eligible = [r for r in tape if r['boundary'] < boundary]
    assert eligible, mint
    row = eligible[-1]
    birth = births[mint]
    state = PumpCurveStateV1(
        int(row['virtual_base']), int(row['virtual_quote']), int(row['real_base']), int(row['real_quote']),
        int(birth['supply_raw']), PumpCurveLifecycle.ACTIVE,
        PumpMode.TOKEN_2022 if birth['token_program'].startswith('Tokenz') else PumpMode.NORMAL,
    )
    return row, state

liquidity_records = []
for mint in blocked_mints:
    strict, virtual = rows_by_run['strict'][mint], rows_by_run['virtual'][mint]
    sell_fill = fill_by_run_mint['virtual'][mint]['SELL']
    historical, state = state_before(mint, sell_fill['boundary_ordinal'])
    quantity = int(sell_fill['amount_in_atomic'])
    now = int(virtual['exit_time'])
    fees = profile((int(historical['payload']['fee_basis_points']), int(historical['payload']['creator_fee_basis_points'])), now)
    virtual_quote = sell_quote(state, tokens_in_atomic=quantity, fee_profile=fees, effective_at_unix_s=now, liquidity_policy=PumpSellLiquidityPolicy.VIRTUAL_RESERVE_OUTPUT_WITH_EXPLICIT_SYNTHETIC_SHORTFALL_V1)
    assert virtual_quote.gross_curve_sol_lamports == virtual['exit_gross_curve_lamports']
    assert virtual_quote.synthetic_shortfall_lamports == virtual['exit_synthetic_shortfall_lamports']
    try:
        sell_quote(state, tokens_in_atomic=quantity, fee_profile=fees, effective_at_unix_s=now)
    except PumpQuoteError as error:
        assert error.code is PumpQuoteErrorCode.INSUFFICIENT_REAL_SOL_RESERVES
        rejection = error.code.value
    else:
        raise AssertionError('Expected the native strict reserve check to reject this full balance')
    low, high = 0, quantity
    x, y, real = state.virtual_sol_reserves_lamports, state.virtual_token_reserves_atomic, state.real_sol_reserves_lamports
    while low < high:
        middle = (low + high + 1) // 2
        if middle * x // (y + middle) <= real:
            low = middle
        else:
            high = middle - 1
    maximum_partial = low
    partial_receipt = None
    partial_error = ''
    if maximum_partial:
        try:
            partial = sell_quote(state, tokens_in_atomic=maximum_partial, fee_profile=fees, effective_at_unix_s=now)
            partial_receipt = partial.sol_out_lamports - runs['strict']['summary']['policy']['exit_network_fee_lamports']
        except PumpQuoteError as error:
            partial_error = error.code.value
    deadline = strict['entry_time'] + runs['strict']['summary']['policy']['holding_seconds']
    liquidity_records.append({
        'token': virtual['symbol'], 'mint': mint, 'gmgn_url': virtual['gmgn_url'],
        'buy_cost_SOL': strict['entry_cash_lamports'] / LAMPORTS,
        'full_balance_raw': quantity, 'full_sell_gross_SOL': virtual_quote.gross_curve_sol_lamports / LAMPORTS,
        'recorded_real_reserve_SOL': real / LAMPORTS,
        'assumed_shortfall_SOL': virtual_quote.synthetic_shortfall_lamports / LAMPORTS,
        'our_buy_curve_deposit_SOL': buy_lookup['virtual', mint]['curve_deposit_SOL'],
        'strict_quote_rejection': rejection, 'submitted_strict_sell_attempts': strict['exit_attempts'],
        'strict_failed_sell_fees_SOL': strict['failed_execution_cost_lamports'] / LAMPORTS,
        'virtual_exit_UTC': utc(now), 'deadline_UTC': utc(deadline),
        'follow_up_after_deadline_seconds': max(clock.values()) - deadline,
        'max_partial_sale_raw': maximum_partial, 'max_partial_sale_percent': 100 * maximum_partial / quantity,
        'partial_net_after_network_SOL': partial_receipt / LAMPORTS if partial_receipt is not None else np.nan,
        'partial_quote_error': partial_error,
        'virtual_net_pnl_SOL': virtual['closed_pnl_lamports'] / LAMPORTS,
    })
liquidity = pd.DataFrame(liquidity_records)
linked_table(liquidity[['token', 'gmgn_url', 'full_sell_gross_SOL', 'recorded_real_reserve_SOL', 'assumed_shortfall_SOL', 'submitted_strict_sell_attempts', 'max_partial_sale_percent', 'partial_net_after_network_SOL', 'virtual_net_pnl_SOL']], floats=9)
print('All seven full quotes reject against the recorded real reserves; zero sell submissions and zero failed-sell fees were charged.')
''')

code('''
for mint in blocked_mints:
    p = rows_by_run['virtual'][mint]
    frame, launch_time, supply = history(mint)
    buy_fill = fill_by_run_mint['virtual'][mint]['BUY']
    deadline = p['entry_time'] + runs['strict']['summary']['policy']['holding_seconds']
    end_min = (deadline + 30 - launch_time) / 60
    entered = frame[(frame['boundary'] < buy_fill['boundary_ordinal'])].tail(1).copy()
    entered['minutes'] = (p['entry_time'] - launch_time) / 60
    later = frame[(frame['boundary'] >= buy_fill['boundary_ordinal']) & (frame['minutes'] <= end_min)]
    visible = pd.concat([entered, later]).copy()
    quantity = buy_fill['amount_out_atomic']
    required = np.array([int(quantity) * int(x) // (int(y) + int(quantity)) / LAMPORTS for x, y in zip(visible['virtual_sol_raw'], visible['virtual_token_raw'])])
    observed = visible['real_sol_raw'].to_numpy(dtype=float) / LAMPORTS
    fig, axes = plt.subplots(1, 2, figsize=(14, 4.5), layout='constrained')
    token_curve(axes[0], mint, legend=True)
    axes[1].step(visible['minutes'], observed, where='post', color='#1766a7', label='Historical actual SOL')
    axes[1].step(visible['minutes'], required, where='post', color='#c07824', label='SOL required for our full sell')
    axes[1].fill_between(visible['minutes'], observed, required, where=required > observed, step='post', color='#c07824', alpha=0.15, label='Recorded liquidity shortfall')
    tail_start = visible['minutes'].iloc[-1]
    if tail_start < end_min:
        tail_x = [tail_start, end_min]
        axes[1].plot(tail_x, [observed[-1]] * 2, color='#1766a7', linestyle='--', alpha=0.7)
        axes[1].plot(tail_x, [required[-1]] * 2, color='#c07824', linestyle='--', alpha=0.7)
        axes[1].fill_between(tail_x, [observed[-1]] * 2, [required[-1]] * 2, color='#c07824', alpha=0.12)
    axes[1].axvline((deadline - launch_time) / 60, color='#555', linestyle='--', label='Five-minute deadline')
    axes[1].set(xlabel='Minutes after launch', ylabel='SOL', title='Strict preflight reserve check')
    axes[1].legend(fontsize=8)
    axes[1].grid(alpha=0.18)
    axes[1].set_xlim(axes[0].get_xlim())
    fig.savefig(EXPORT / f'liquidity_{mint}.png', dpi=160)
    plt.show()
''')

md('''
## Inspect one token

Change `TOKEN` to any filled mint from the tables and rerun this cell. Its chart
and both saved position records are shown. Charts remain based on the frozen
historical tape; these orders are not newly simulated here.
''')

code('''
TOKEN = 'AkbJPKrzjP6jwvzukXtndEboymWNcmCYkBXvV3VUpump'
assert TOKEN in filled_mints
fig, ax = plt.subplots(figsize=(12, 5), layout='constrained')
token_curve(ax, TOKEN, legend=True)
plt.show()
display(pd.DataFrame([dict(run=name, **rows_by_run[name][TOKEN]) for name in ['strict', 'virtual']]).T)
''')

md('''
## Failed submissions and rejected entries

These are failures of **our simulated orders**. Minimum-output failures pay the
configured submitted-transaction cost. Capital rejections pay no submission fee.
The seven liquidity preflight rejections are not in this failure log because
there was no sell submission. The notebook does not invent failed-sell charges.
''')

code('''
failure_records = []
for name, folder in RUN_DIRS.items():
    with (folder / 'execution_failures.csv').open() as stream:
        failures = list(csv.DictReader(stream))
    reserve_mint = {}
    paid_failure = defaultdict(int)
    # Reproduce the native strategy's content-derived BUY order identity.
    # This joins entry failures to tokens without guessing from shared slots.
    from backtest.domain.hashing import domain_digest
    component = domain_digest('dnipro.configured-sniper-window.v1', runs[name]['summary']['policy']).hex
    order_mints = {}
    for p in runs[name]['positions']:
        if 'signal_slot' not in p:
            continue
        boundaries = {r['boundary'] for r in trades_by_mint[p['mint']] if r['slot'] == p['signal_slot']}
        # Unfilled mints also need their own evidenced signal transaction.
        if not boundaries:
            boundaries = {r['boundary'] for r in all_trades if r['mint'] == p['mint'] and r['slot'] == p['signal_slot']}
        for boundary in boundaries:
            oid = domain_digest('dnipro.sniper-order.v1', {'mint': p['mint'], 'side': 'BUY', 'boundary': boundary, 'attempt': 0, 'component': component}).hex
            order_mints[oid] = p['mint']
    assert all(order_mints.get(sides['BUY']['order_id']) == mint for mint, sides in fill_by_run_mint[name].items())
    for tx in runs[name]['ledger']:
        for p in tx['postings']:
            if p['account_kind'] == 'NETWORK_FEE' and p['account_id'] == 'network:failed-execution':
                paid_failure[tx['correlation_id']] += p['amount_atomic']
            if tx['reason'] == 'ORDER_RESERVATION' and p['account_kind'] == 'PORTFOLIO_RESERVED' and p['asset_id'] != SOL:
                reserve_mint[tx['correlation_id']] = p['asset_id']
    for row in failures:
        oid = row['order_id']
        slot = int(row['boundary_ordinal']) >> 32
        mint = order_mints.get(oid, reserve_mint.get(oid))
        assert mint is not None, ('unmapped native failure', oid)
        position = rows_by_run[name][mint]
        failure_records.append({
            'run': name, 'token': position['symbol'], 'mint': mint, 'gmgn_url': position['gmgn_url'],
            'order_id': oid, 'slot': slot, 'time_UTC': utc(clock[slot]) if slot in clock else '',
            'failure_type': row['record_type'], 'reason': row['reason'],
            'submitted_network_fee_SOL': paid_failure[oid] / LAMPORTS,
        })
failures = pd.DataFrame(failure_records)
linked_table(failures.drop(columns=['order_id', 'mint']))
assert int(round(failures.loc[failures['run'] == 'virtual', 'submitted_network_fee_SOL'].sum() * LAMPORTS)) == runs['virtual']['summary']['failed_entry_cost_lamports']
signal_records = []
for name, run in runs.items():
    for p in run['positions']:
        if 'signal_slot' in p:
            signal_records.append({'run': name, 'token': p['symbol'], 'mint': p['mint'], 'gmgn_url': p['gmgn_url'], 'status': p['status'], 'reason': p['reason'], 'signal_slot': p['signal_slot'], 'requested_buy_budget_SOL': p['budget_lamports'] / LAMPORTS, 'buy_cash_cost_SOL': p['entry_cash_lamports'] / LAMPORTS, 'closed_pnl_SOL': p['closed_pnl_lamports'] / LAMPORTS if p.get('closed_pnl_lamports') is not None else None})
signals = pd.DataFrame(signal_records)
print('All 38 signals in each run, including unfilled entries, are exported separately.')
''')

md('''
## Developer fees and break-even

Reference-buyer cost now subtracts **creator fees accrued to that wallet before
its signal transaction**. Event creator/recipient fields and explicit shareholder
splits determine attribution; the launch creator is not assumed to receive every
fee. Claims do not create a second income credit. Unknown recipients and shared
rounding dust are retained as unallocated.

The developer table below separates historical trade cashflow, accrued creator
fees and additional fees from our hypothetical filled orders. Vault accrual is a
receivable estimate, not a proven wallet withdrawal. Cashflow plus accrual is
**not full realized or marked PnL**, particularly if observed tokens remain.
Future historical fee volume and fees from our planned buy do not enter the
signal's cost; fees from filled simulated orders appear separately here.

The old snapshots and runs are preserved. Both fee-aware reruns produced the
same entries, sizes and trading PnL: none of the 38 selected reference buyers
received creator-fee income by their entry signal.
''')

code('''
BASELINE_DIRS = {
    'strict': ROOT / 'var/dnipro/sniper-window-20261010-primary-v2',
    'virtual': ROOT / 'var/dnipro/sniper-window-20261010-virtual-v1',
}
comparison = []
for name, folder in BASELINE_DIRS.items():
    old = json.loads((folder / 'result.json').read_text())
    for before in old['positions']:
        after = rows_by_run[name][before['mint']]
        for key, value in before.items():
            assert after.get(key) == value, (name, before['mint'], key)
    assert old['summary']['realized_net_pnl_lamports'] == runs[name]['summary']['realized_net_pnl_lamports']
    comparison.append({'run': name, 'PnL_before_SOL': old['summary']['realized_net_pnl_lamports'] / LAMPORTS,
                       'PnL_after_SOL': runs[name]['summary']['realized_net_pnl_lamports'] / LAMPORTS,
                       'reference_buyers_receiving_creator_income': runs[name]['summary']['fee_receiving_anchor_signals'],
                       'entries_sizes_and_exits_unchanged': True})
fee_comparison = pd.DataFrame(comparison)
display(fee_comparison)

# Attribute each filled order's paid creator fee using its evidenced route.
# This does not add developer income to our trading wallet or rerun execution.
simulated_income = CreatorFeeIncome()
simulated_fee_total = 0
for fill in runs['virtual']['fills']:
    mint = fill['pool_id']
    latest = [r for r in trades_by_mint[mint] if r['boundary'] <= fill['boundary_ordinal']][-1]
    tx = settlements['virtual'][fill['order_id']]
    creator_fee = sum(p['amount_atomic'] for p in tx['postings'] if p['account_kind'] == 'CREATOR_FEE' and p['asset_id'] == SOL)
    simulated_fee_total += creator_fee
    simulated_income.observe('our-fill:' + fill['order_id'], dict(latest, creator_fee=creator_fee))
simulated_income.verify()
assert sum(simulated_income.total.values()) == simulated_fee_total

developer_records = []
for mint in filled_mints:
    p = rows_by_run['virtual'][mint]
    creator = p['creator']
    buy = fill_by_run_mint['virtual'][mint]['BUY']
    signal_boundary = reservations['virtual'][buy['order_id']]
    at_signal = CreatorFeeIncome()
    dev_cost = 0
    paid_signatures = set()
    for trade in trades_by_mint[mint]:
        if trade['boundary'] > signal_boundary:
            break
        at_signal.observe(trade['event_uid'], trade)
        if trade['trader'] == creator:
            fee = 0
            if trade['fee_payer'] == creator and trade['signature'] not in paid_signatures and trade.get('network_fee') is not None:
                fee = int(trade['network_fee'])
                paid_signatures.add(trade['signature'])
            charged = int(trade['protocol_fee']) + int(trade['creator_fee'])
            dev_cost += int(trade['quote']) + charged + fee if trade['side'] == 'buy' else -(int(trade['quote']) - charged - fee)
    chosen_income = at_signal.income(mint, p['sniper_wallet'])
    assert chosen_income == p['sniper_creator_fee_income_lamports']
    additional = simulated_income.income(mint, creator)
    historical = p['developer_creator_fee_accrual_lamports']
    developer_records.append({
        'token': p['symbol'], 'mint': mint, 'gmgn_url': p['gmgn_url'],
        'developer_gmgn': 'https://gmgn.ai/sol/address/' + creator,
        'dev_trade_cost_by_signal_SOL': dev_cost / LAMPORTS,
        'dev_fee_accrual_by_signal_SOL': at_signal.income(mint, creator) / LAMPORTS,
        'dev_adjusted_net_cost_by_signal_SOL': (dev_cost - at_signal.income(mint, creator)) / LAMPORTS,
        'selected_reference_fee_income_SOL': chosen_income / LAMPORTS,
        'dev_historical_trade_cashflow_SOL': -p['developer_observed_net_trade_cost_lamports'] / LAMPORTS,
        'dev_historical_creator_fee_accrual_SOL': historical / LAMPORTS,
        'dev_additional_fees_from_our_fills_SOL': additional / LAMPORTS,
        'dev_cashflow_plus_accrual_with_our_fees_SOL': (-p['developer_observed_net_trade_cost_lamports'] + historical + additional) / LAMPORTS,
        'dev_observed_remaining_tokens_raw': p['developer_observed_tokens_raw'],
        'income_basis': 'accrual, not confirmed withdrawal',
    })
developers = pd.DataFrame(developer_records).sort_values('dev_historical_creator_fee_accrual_SOL', ascending=False)
linked_table(developers[['token', 'gmgn_url', 'dev_historical_trade_cashflow_SOL', 'dev_historical_creator_fee_accrual_SOL', 'dev_additional_fees_from_our_fills_SOL', 'dev_cashflow_plus_accrual_with_our_fees_SOL', 'dev_observed_remaining_tokens_raw']])
print('Full supported cohort creator-fee accrual:', runs['virtual']['summary']['observed_creator_fee_accrual_lamports'] / LAMPORTS, 'SOL')
print('Shared rounding/unallocated remainder:', runs['virtual']['summary']['unallocated_creator_fee_accrual_lamports'], 'lamports')
''')

from slot_trace_cells import add_trace_cells
add_trace_cells(md, code)

from group_comparison_cells import add_group_comparison_cells
add_group_comparison_cells(md, code)

from sniper_redesign_cells import add_redesign_cells
add_redesign_cells(md, code)

md('''
## CSV exports and reproducibility

Every table is saved as a plain CSV. The notebook uses the native result ledgers
and upstream quote function; it does not replace the trusted backtest engine.
Its source manifests and output hashes are recorded in the export receipt.

To rerun in IntelliJ, select the local interpreter
`var/dnipro/sniper-notebook-venv/bin/python` from this repository. The environment
is isolated from the backtest engine; its resolved packages are saved in
`var/dnipro/sniper-notebook-requirements.txt`. The builder can execute this notebook
using the workspace-local `sniper-research` kernel.

**Remaining realism limits:** neither replay propagates our reserve impact
through other traders' orders. Holdings/transfers, historical fee-tier changes,
actual order landing, tips and the configured transaction costs are not exact
execution evidence. Virtual-settlement losses are modeled losses; strict open
inventory remains an unvalued position rather than a confirmed total loss.
''')

code('''
summary_table.to_csv(EXPORT / 'run_summaries.csv', index=False)
all_buys.to_csv(EXPORT / 'simulated_buys.csv', index=False)
outcomes.to_csv(EXPORT / 'position_outcomes.csv', index=False)
liquidity.to_csv(EXPORT / 'liquidity_blocked_positions.csv', index=False)
failures.to_csv(EXPORT / 'simulated_order_failures.csv', index=False)
signals.to_csv(EXPORT / 'all_signal_outcomes.csv', index=False)
developers.to_csv(EXPORT / 'developer_fee_accounting.csv', index=False)
fee_comparison.to_csv(EXPORT / 'creator_fee_rerun_comparison.csv', index=False)
selected_tape = [r for r in all_trades if r['mint'] in filled_mints]
pd.DataFrame([{k: r[k] for k in ['mint', 'slot', 'tx_idx', 'event_uid', 'time', 'side', 'trader', 'quote', 'tokens', 'virtual_quote', 'virtual_base', 'real_quote', 'real_base', 'signature']} for r in selected_tape]).to_csv(EXPORT / 'historical_curve_trades.csv', index=False)
receipt = {
    'schema': 'dnipro.sniper-result-notebook/v1',
    'strict_result_sha256': sha(RUN_DIRS['strict'] / 'result.json'),
    'virtual_result_sha256': sha(RUN_DIRS['virtual'] / 'result.json'),
    'source_manifest_sha256': sha(SOURCE / 'manifest.json'),
    'source_input_sha256': runs['virtual']['summary']['source_input_sha256'],
    'filled_buy_records_across_runs': len(all_buys),
    'virtual_filled_and_closed': len(outcomes), 'strict_liquidity_blocked': len(liquidity),
    'strict_blocked_submitted_sells': int(liquidity['submitted_strict_sell_attempts'].sum()),
    'all_native_ledgers_conserve': True,
    'all_seven_native_full_sell_quotes_rejected': True,
    'partial_quote_diagnostics_use_native_model': True,
    'creator_fee_credit_enabled': True,
    'developer_receivables_are_not_our_trading_PnL': True,
    'native_creator_fee_rerun_economics_match_baseline': True,
    'additional_creator_fees_from_our_fills_lamports': simulated_fee_total,
    'our_fill_creator_fee_allocation_conservation_verified': True,
    'last_retained_clock_utc': utc(max(clock.values())),
    'all_shortfall_cases_traced': len(cases),
    'strategy_decision_records': len(strategy_trace),
    'observed_inventory_conservation_verified': True,
    'holder_break_even_rows': len(holder_economics),
    'wait_result_sha256': sha(wait_dir / 'result.json'),
    'group_comparison_receipt_sha256': sha(COMPARISON/'receipt.json'),
    'group_comparison_counts': comparison_receipt['group_counts'],
    'redesign_selection_sha256': sha(REDESIGN/'selection.json'),
    'redesign_before_after_csv_sha256': sha(REDESIGN/'notebook_before_after.csv'),
    'csv_hashes': {p.name: sha(p) for p in sorted(EXPORT.glob('*.csv'))},
}
(EXPORT / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\\n')
display(Markdown(f'Exports: `{EXPORT}`'))
print(json.dumps(receipt, indent=2))
''')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    nb = nbformat.v4.new_notebook(cells=cells)
    nb.metadata['kernelspec'] = {
        'display_name': 'Python 3.13 (Sniper research)', 'language': 'python', 'name': 'sniper-research',
    }
    nb.metadata['language_info'] = {'name': 'python', 'version': sys.version.split()[0]}
    nbformat.validate(nb)
    if args.execute:
        from jupyter_client import KernelManager
        from jupyter_client.kernelspec import KernelSpecManager
        from nbclient import NotebookClient
        runtime = ROOT / 'var/dnipro/notebook-runtime'
        runtime.mkdir(parents=True, exist_ok=True)
        for key, folder in {'MPLCONFIGDIR': 'matplotlib', 'IPYTHONDIR': 'ipython', 'JUPYTER_RUNTIME_DIR': 'jupyter'}.items():
            path = runtime / folder
            path.mkdir(exist_ok=True)
            os.environ[key] = str(path)
        kernels = ROOT / 'var/dnipro/notebook-kernels/share/jupyter/kernels'
        manager = KernelManager(kernel_name='sniper-research', kernel_spec_manager=KernelSpecManager(kernel_dirs=[str(kernels)]))
        client = NotebookClient(nb, km=manager, kernel_name='sniper-research', timeout=180, resources={'metadata': {'path': str(ROOT)}})
        try:
            client.execute()
        except Exception:
            nbformat.write(nb, TARGET.with_name(TARGET.stem + '.partial.ipynb'))
            raise
    nbformat.validate(nb)
    stage = TARGET.with_name(TARGET.stem + '.staging.ipynb')
    nbformat.write(nb, stage)
    stage.replace(TARGET)
    report = {
        'notebook': str(TARGET), 'executed': args.execute,
        'code_cells': sum(c.cell_type == 'code' for c in nb.cells),
        'executed_code_cells': sum(c.cell_type == 'code' and c.execution_count is not None for c in nb.cells),
        'image_outputs': sum('image/png' in o.get('data', {}) for c in nb.cells if c.cell_type == 'code' for o in c.outputs),
        'sha256': hashlib.sha256(TARGET.read_bytes()).hexdigest(),
    }
    report['error_outputs'] = sum(o.get('output_type') == 'error' for c in nb.cells if c.cell_type == 'code' for o in c.outputs)
    export = ROOT / 'var/dnipro/sniper-window-20261010-slot-traces-notebook-v1'
    if args.execute:
        (export / 'notebook_qa.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
