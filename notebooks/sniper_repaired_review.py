"""Review the repaired 50 SOL replay without changing strategy or raw inputs."""
from collections import defaultdict
from pathlib import Path
import hashlib
import json

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from IPython.display import Markdown, display
import sniper_position_review as prior

ROOT = prior.ROOT
EXPERIMENT = ROOT / 'var/dnipro/sniper-inventory-repair-20261015-v1'
RUN = 'development-repaired-before'
EXPORT = ROOT / 'var/dnipro/sniper-repaired-review-20261015-v1'
SOL, LAMPORTS = prior.SOL, prior.LAMPORTS
GOOD, BAD = prior.GOOD, prior.BAD
linked_table = prior.linked_table


def load():
    global RESULT, POSITIONS, ENTRY_EXIT, FILLS, TRACE, ERRORS, CASH, POSTINGS, EVENTS
    global CLOCK, TAPES, SOURCE_HASHES, CHART_INDEX
    EXPORT.mkdir(parents=True, exist_ok=True)
    (EXPORT / 'positions').mkdir(exist_ok=True)
    for name in ('repaired-v3', RUN):
        folder = EXPERIMENT / name
        manifest = json.loads((folder / 'manifest.json').read_text())
        assert manifest['complete']
        for filename, digest in manifest['files'].items():
            assert prior.sha(folder / filename) == digest, (name, filename)
    RESULT = json.loads((EXPERIMENT / RUN / 'result.json').read_text())
    baseline = json.loads((EXPERIMENT / 'repaired-v3/result.json').read_text())
    assert RESULT == baseline, 'Full-trace run must preserve the entire saved result'
    s = RESULT['summary']
    assert s['realized_net_pnl_lamports'] == -3_289_255_885
    assert s['open_cash_cost_lamports'] == 1_207_495_000
    assert s['policy']['bankroll_lamports'] == 50 * LAMPORTS
    assert s['policy']['entry_slippage_bps'] == s['policy']['exit_slippage_bps'] == 500
    assert s['cash_reconciliation_verified'] and s['synthetic_shortfall_lamports'] == 0
    for transaction in RESULT['ledger']:
        totals = defaultdict(int)
        for posting in transaction['postings']:
            totals[posting['asset_id']] += posting['amount_atomic']
        assert all(value == 0 for value in totals.values())
    rows = []
    for p in RESULT['positions']:
        if 'signal_slot' not in p:
            continue
        outcome = ('Profitable' if p['closed_pnl_lamports'] > 0 else 'Losing'
                   if p['closed_pnl_lamports'] < 0 else 'Breakeven') if p['status'] == 'CLOSED' else {
                       'OPEN': 'Open / unresolved', 'ENTRY_FAILED': 'Entry failed',
                       'ENTRY_REJECTED': 'Entry rejected'}[p['status']]
        rows.append(dict(p, run=RUN, token=p['symbol'].strip(), outcome=outcome,
                         signal_utc=prior.utc(p['signal_time']),
                         entry_SOL=p['entry_cash_lamports'] / LAMPORTS,
                         exit_receipt_SOL=p.get('exit_receipt_lamports', 0) / LAMPORTS
                         if p['status'] == 'CLOSED' else None,
                         net_pnl_SOL=p['closed_pnl_lamports'] / LAMPORTS if p['status'] == 'CLOSED'
                         else -p['failed_execution_cost_lamports'] / LAMPORTS
                         if p['status'] == 'ENTRY_FAILED' else None,
                         failed_execution_fees_SOL=p['failed_execution_cost_lamports'] / LAMPORTS,
                         open_cost_SOL=p['entry_cash_lamports'] / LAMPORTS if p['status'] == 'OPEN' else 0))
    POSITIONS = pd.DataFrame(rows).sort_values(['signal_slot', 'mint'])
    prior.RUN_ROOT, prior.EXPORT = EXPERIMENT, EXPORT
    prior.RUNS, prior.POSITIONS = {RUN: RESULT}, POSITIONS
    clocks, TAPES, SOURCE_HASHES = prior.load_source_context(prior.RUNS)
    CLOCK = clocks['development']
    audit, mapping = prior.native_order_context(RUN, RESULT)
    CASH, POSTINGS = prior.ledger_frames(RUN, RESULT, mapping, CLOCK)
    EVENTS = prior.pnl_events(RUN, RESULT, CASH)
    prior.TAPES, prior.AUDITS, prior.ORDER_MAPS = TAPES, {RUN: audit}, {RUN: mapping}
    prior.LEDGERS, prior.POSTINGS = {RUN: CASH}, {RUN: POSTINGS}
    ENTRY_EXIT, FILLS = prior.entry_exit_records(RUN)
    prior.ENTRY_EXIT, prior.FILL_MARKERS = ENTRY_EXIT, FILLS
    TRACE = pd.DataFrame(prior.csv_rows(EXPERIMENT / RUN / 'strategy_trace.csv'))
    ERRORS = pd.DataFrame(prior.csv_rows(EXPERIMENT / RUN / 'conditional_errors.csv'))
    assert set(TRACE.loc[TRACE['action'] == 'ENTRY_SIGNAL', 'mint']) == set(POSITIONS['mint'])
    assert len(ENTRY_EXIT) == 38 and len(FILLS) == 57
    assert (FILLS['side'] == 'BUY').sum() == 29 and (FILLS['side'] == 'SELL').sum() == 28
    CHART_INDEX = []
    for filename, frame in [('all_positions_entry_exit.csv', ENTRY_EXIT),
                            ('native_fill_markers.csv', FILLS), ('pnl_events.csv', EVENTS),
                            ('cash_events.csv', CASH), ('decision_log_all.csv', TRACE)]:
        frame.to_csv(EXPORT / filename, index=False)
    return pd.DataFrame([dict(sample='2026-10-10 16:00–17:00 UTC launches',
                              starting_wallet_SOL=50, slippage_percent=5, signals=38,
                              buys=29, profitable=14, losing=14, unresolved=1, failed_entries=9,
                              realized_net_PnL_SOL=s['realized_net_pnl_lamports'] / LAMPORTS,
                              unresolved_cost_SOL=s['open_cash_cost_lamports'] / LAMPORTS,
                              final_portfolio_PnL_SOL=None,
                              zero_recovery_stress_SOL=float(s['zero_recovery_stress_pnl_sol']))])


def pnl_chart():
    start, end = min(CLOCK), max(CLOCK)
    values = EVENTS['pnl_lamports'].cumsum() / LAMPORTS
    x, y = np.r_[0, EVENTS['slot'] - start, end - start], np.r_[0, values, values.iloc[-1]]
    fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True, layout='constrained')
    axes[0].step(x, y, where='post', color='#1768b3', linewidth=2)
    axes[0].set_title('Repaired original strategy: realized net −3.289255885 SOL; one unresolved')
    axes[0].set_ylabel('Realized net PnL (SOL)')
    axes[0].annotate(f'{y[-1]:+.9f} SOL', (x[-1], y[-1]), xytext=(-8, 14),
                     textcoords='offset points', ha='right')
    cash = CASH[CASH['cash_delta_lamports'] != 0]
    axes[1].step(np.r_[0, cash['slot'] - start, end - start],
                 np.r_[0, cash['cash_delta_lamports'].cumsum() / LAMPORTS,
                       cash['cash_delta_lamports'].sum() / LAMPORTS], where='post', color='#69738b')
    axes[1].set_ylabel('Cash change (SOL)')
    axes[1].set_xlabel(f'Slots since {start}; unresolved tokens are not marked')
    for ax in axes:
        ax.axhline(0, color='#999999', linewidth=.7)
        ax.grid(alpha=.15)
    prior.finish_plot(fig, 'pnl-and-cash.png')


def decisions(mint, all_rows=False):
    """All native evaluations remain available; default shows entry and exit evidence."""
    trace = TRACE[TRACE['mint'] == mint]
    if all_rows:
        return trace
    entry = trace[trace['action'].str.startswith('ENTRY')]
    orders = trace[trace['action'].str.startswith('ORDER')]
    exit_checks = trace[trace['action'].str.startswith('EXIT')]
    return pd.concat([entry.tail(12), orders, exit_checks.tail(8)]).sort_index()


def metadata(mint):
    p = next(p for p in RESULT['positions'] if p['mint'] == mint)
    trace = TRACE[TRACE['mint'] == mint]
    signal = trace[trace['action'] == 'ENTRY_SIGNAL'].iloc[-1].to_dict()
    checks = trace[trace['action'] == 'EXIT_EVALUATION']
    if p.get('exit_signal_slot') is not None:
        checks = checks[checks['slot'].astype(int) == p['exit_signal_slot']]
    exit_check = checks.iloc[-1].to_dict() if len(checks) else None
    return dict(position=p, entry_signal=signal, exit_evaluation=exit_check,
                counterparty_errors=ERRORS[ERRORS['mint'] == mint].to_dict('records'))


def inspect(mint, show_logs=True):
    row = ENTRY_EXIT[ENTRY_EXIT['mint'] == mint].iloc[0]
    p = next(p for p in RESULT['positions'] if p['mint'] == mint)
    info = metadata(mint)
    trace = TRACE[TRACE['mint'] == mint].copy()
    launch = p['launch_slot']
    fig, axes = plt.subplots(4, 1, figsize=(13, 11), sharex=True,
                             gridspec_kw={'height_ratios': [3, 1.2, 1.3, 1.2]}, layout='constrained')
    end, _ = prior.draw_entry_exit(axes[0], RUN, mint)
    tape = pd.DataFrame(TAPES['development', mint])
    tape = tape[tape['slot'] <= end]
    reference = int(tape.iloc[0]['virtual_quote']) / int(tape.iloc[0]['virtual_base'])
    evaluations = trace[trace['virtual_tokens'] != ''].copy()
    evaluations['slot'] = evaluations['slot'].astype(int)
    axes[0].plot(evaluations['slot'] - launch,
                 evaluations['virtual_sol'].astype(float) / evaluations['virtual_tokens'].astype(float)
                 / reference * 100, color='#d58730', alpha=.7, linewidth=1,
                 label='Replayed state at strategy evaluations')
    axes[0].legend(fontsize=7, loc='upper right')
    if end - launch > 150:
        zoom = axes[0].inset_axes([.16, .48, .32, .43])
        early = tape[tape['slot'] <= launch + 60]
        zoom.step(early['slot'] - launch,
                  early['virtual_quote'].astype(float) / early['virtual_base'].astype(float)
                  / reference * 100, where='post', color='#7764a8', linewidth=1)
        for marker in FILLS[FILLS['mint'] == mint].to_dict('records'):
            if marker['slot'] <= launch + 60:
                buy = marker['side'] == 'BUY'
                zoom.scatter([marker['slot'] - launch], [marker['average_curve_price_index']],
                              color=GOOD if buy else BAD, marker='^' if buy else 'v', s=35, zorder=5)
        zoom.axvline(p['signal_slot'] - launch, color='#b28725', linestyle=':', alpha=.7)
        zoom.set_xlim(-2, 62)
        zoom.set_title('Entry zoom: first 60 slots', fontsize=8)
        zoom.tick_params(labelsize=7)
        zoom.grid(alpha=.15)
    axes[1].step(tape['slot'] - launch, tape['real_quote'].astype(float) / LAMPORTS,
                 where='post', color='#7764a8', label='Historical real SOL reserve')
    reserves = trace[trace['real_sol'] != '']
    axes[1].plot(reserves['slot'].astype(int) - launch, reserves['real_sol'].astype(float) / LAMPORTS,
                 color='#d58730', label='Replayed real SOL at evaluation')
    axes[1].set_ylabel('Real SOL reserve')
    checks = trace[trace['action'] == 'EXIT_EVALUATION']
    if len(checks):
        axes[2].plot(checks['slot'].astype(int) - launch,
                     checks['pnl_lamports'].astype(float) / LAMPORTS,
                     color='#1768b3', label='Quoted liquidation net PnL at decision')
        axes[2].plot(checks['slot'].astype(int) - launch,
                     checks['target_lamports'].astype(float) / LAMPORTS,
                     color=GOOD, linestyle=':', label='5% net target')
    if p['status'] == 'CLOSED':
        axes[2].scatter([p['exit_slot'] - launch], [p['closed_pnl_lamports'] / LAMPORTS],
                        marker='D', color=BAD, zorder=5, label='Actual simulated closed net PnL')
    axes[2].axhline(0, color='#aaaaaa', linewidth=.7)
    axes[2].set_ylabel('Quoted / closed PnL (SOL)')
    anchor = trace[(trace['wallet'] == p['sniper_wallet']) & (trace['wallet_tokens'] != '')]
    axes[3].step(anchor['slot'].astype(int) - launch,
                 anchor['wallet_tokens'].astype(float) / 10**6, where='post', color='#4595a2',
                 label='Reference buyer modeled inventory (not complete holder coverage)')
    axes[3].set_ylabel('Reference tokens')
    axes[3].set_xlabel(f'Slots since launch {launch}')
    for ax in axes[1:]:
        for field, color in [('signal_slot', '#b28725'), ('entry_slot', GOOD), ('exit_slot', BAD)]:
            if field in p:
                ax.axvline(p[field] - launch, color=color, linestyle='--', alpha=.5)
        ax.grid(alpha=.15)
        if ax.get_legend_handles_labels()[0]:
            ax.legend(fontsize=7, loc='best')
    conflicts = info['counterparty_errors']
    if p['status'] == 'OPEN' and conflicts:
        blocked = int(conflicts[0]['boundary_ordinal']) >> 32
        for ax in axes:
            ax.axvline(blocked - launch, color='#222222', linestyle=':', alpha=.8)
    net = f"{row['net_pnl_SOL']:+.9f} SOL" if p['status'] == 'CLOSED' else p['reason']
    fig.suptitle(f"{row['token']} · {row['group']} · {net}\n{mint}", fontsize=11)
    filename = f'positions/{mint}.png'
    prior.finish_plot(fig, filename)
    record = dict(mint=mint, token=row['token'], group=row['group'], chart=filename,
                  entry_slot=p.get('entry_slot'), exit_slot=p.get('exit_slot'))
    if not any(x['mint'] == mint for x in CHART_INDEX):
        CHART_INDEX.append(record)
    (EXPORT / 'positions' / (mint + '.json')).write_text(json.dumps(info, indent=2) + '\n')
    trace.to_csv(EXPORT / 'positions' / (mint + '.decisions.csv'), index=False)
    if show_logs:
        display(Markdown(f"[GMGN token]({p['gmgn_url']}) · "
                         f"[Reference buyer]({p['sniper_gmgn_url']}) · "
                         f"[Creator](https://gmgn.ai/sol/address/{p['creator']})"))
        keys = ['signal_slot', 'entry_slot', 'exit_signal_slot', 'exit_slot', 'reason',
                'budget_lamports', 'entry_cash_sol', 'closed_pnl_sol',
                'sniper_trading_cash_cost_sol', 'sniper_creator_fee_income_sol',
                'signal_our_threshold', 'signal_sniper_threshold', 'exit_attempts',
                'failed_execution_cost_sol', 'developer_creator_fee_accrual_sol']
        display(pd.DataFrame([{'field': k, 'recorded_value': p.get(k)} for k in keys]))
        display(Markdown('**Entry signal: recorded sizing, inventories, costs, reserves and minimum output**'))
        display(pd.DataFrame([info['entry_signal']]).T.rename(columns={0: 'recorded_value'}))
        display(Markdown('**Exit decision**'))
        if info['exit_evaluation']:
            display(pd.DataFrame([info['exit_evaluation']]).T.rename(columns={0: 'recorded_value'}))
        else:
            display(Markdown('No completed exit decision; inspect the recorded failure/conflict below.'))
        display(Markdown('**Entry candidates, native order notifications and final exit evaluations** '
                         '(all rows are saved in the per-token decisions CSV).'))
        linked_table(decisions(mint))
        if conflicts:
            display(Markdown('**Counterparty replay conflicts** — a conflict after an already completed '
                             'exit does not reopen that position.'))
            display(pd.DataFrame(conflicts))
    return record


def save_receipt():
    assert len(CHART_INDEX) == 38 and {x['mint'] for x in CHART_INDEX} == set(POSITIONS['mint'])
    pd.DataFrame(CHART_INDEX).to_csv(EXPORT / 'chart_index.csv', index=False)
    for group, frame in ENTRY_EXIT.groupby('group'):
        frame.to_csv(EXPORT / ('positions_' + group.lower().replace(' ', '_') + '.csv'), index=False)
    receipt = dict(complete=True, native_run=RUN, result_sha256=prior.sha(EXPERIMENT / RUN / 'result.json'),
                   exact_economic_parity_with_repaired_v3=True, all_signal_traces_recorded=True,
                   source_manifest_hashes=SOURCE_HASHES, signals=38, chart_count=len(CHART_INDEX),
                   buy_markers=int((FILLS['side'] == 'BUY').sum()),
                   sell_markers=int((FILLS['side'] == 'SELL').sum()),
                   realized_net_PnL_lamports=RESULT['summary']['realized_net_pnl_lamports'],
                   unresolved_cost_lamports=RESULT['summary']['open_cash_cost_lamports'],
                   final_portfolio_PnL_lamports=None,
                   output_hashes={str(p.relative_to(EXPORT)): prior.sha(p)
                                  for p in sorted(EXPORT.rglob('*'))
                                  if p.is_file() and p.name != 'receipt.json'})
    (EXPORT / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt
