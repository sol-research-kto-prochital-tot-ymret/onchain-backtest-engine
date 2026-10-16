"""Inspect the original strategy on the first launch cohort only."""
from pathlib import Path
from textwrap import dedent
import argparse
import hashlib
import json
import os
import sys
import nbformat

ROOT=Path(__file__).resolve().parents[1]
TARGET=ROOT/'notebooks/sniper_positions_before_after_20261014.ipynb'
cells=[]


def md(text):
    cells.append(nbformat.v4.new_markdown_cell(dedent(text).strip()))


def code(text):
    cells.append(nbformat.v4.new_code_cell(dedent(text).strip()))


md('''
# Original Sniper Break-Even strategy

**October 10, 2026, 16:00–17:00 UTC (19:00–20:00 Kyiv) launch cohort only.**
The study reads the original strategy's saved native engine results for 2,342
launches. Its 38 signals, 18 buy fills, failed/rejected entries and open
positions remain visible. Post-hour observations follow only these launches
through their outcomes. This is the previously investigated development sample.

The main result uses conditional replay that carries own reserve impact forward
and preserves other traders' **observed sell fractions**. This is a modeled
order-sizing assumption; unknown inventory or execution context remains
unresolved. Original results under other execution assumptions appear in a
separate appendix for this same hour. They are not added together.

The original rule set is retained. This notebook reads preserved results and
does not run a strategy or query ClickHouse/RPC. Earlier comparison notebooks,
revised runs and raw data remain archived separately.
''')
code((ROOT/'notebooks/sniper_position_review.py').read_text())
md('''
## Original results

Net closed contribution includes completed-position costs and failed entry
fees. Open token value and final PnL are unresolved. Developer fee accrual is
not credited to our trading wallet. A signal can result in a failed/rejected
entry rather than a filled position.
''')
code('''
RUNS,POSITIONS,SUMMARY,COMPARISON_RECEIPT=verify_and_load()
CLOCKS,TAPES,SOURCE_HASHES=load_source_context(RUNS)
RUN=RUN_IDS[0]
RESULT=RUNS[RUN]
AUDITS,ORDER_MAPS,LEDGERS,POSTINGS,EVENTS={},{},{},{},{}
AUDITS[RUN],ORDER_MAPS[RUN]=native_order_context(RUN,RESULT)
LEDGERS[RUN],POSTINGS[RUN]=ledger_frames(RUN,RESULT,ORDER_MAPS[RUN],CLOCKS['development'])
EVENTS[RUN]=pnl_events(RUN,RESULT,LEDGERS[RUN])
positions=POSITIONS.sort_values(['signal_utc','mint'])
assert len(positions)==38 and set(positions['version'])=={'original'}
assert RESULT['summary']['realized_net_pnl_lamports']==-1_150_398_400
display(SUMMARY)
display(positions.groupby(['outcome','reason']).size().rename('signals').reset_index())
display(Markdown('**12 closed positions, six open, three failed entries and 17 risk-rejected entries.** '
                 'Net closed contribution including failed entry fees: **-1.150398400 SOL**. '
                 'Open committed cost: **8.731632158 SOL**. **Total portfolio PnL remains unresolved.**'))
''')
md('''
## Original entry and exit rules

The reference buyer must buy at least 1 SOL during the first two launch slots
and retain positive observed inventory and unrecovered fee-adjusted cost.
Sizing uses the supplied break-even formula so our 5% net target lies below
that reference buyer's break-even by at least 0.001 SOL in the threshold scale.
The original chooses a feasible size from that condition; entries are considered
through launch+2 slots. Simulated buys and sells have one-slot order latency.
Exit is the 5% target or five-minute timeout, subject to executable quotes and
at most three sell attempts.
''')
code('''
policy=RESULT['summary']['policy']
policy_view=[dict(rule='Early entry window',value=f"{policy['early_slots']} slots"),
             dict(rule='Reference buyer minimum gross buy',value=f"{policy['minimum_sniper_buy_lamports']/LAMPORTS} SOL"),
             dict(rule='Target net return',value=f"{policy['target_return_bps']/100}%"),
             dict(rule='Holding timeout',value=f"{policy['holding_seconds']} seconds"),
             dict(rule='Entry / exit latency',value=f"{policy['entry_delay_slots']} / {policy['exit_delay_slots']} slots"),
             dict(rule='Configured network/priority charge per attempt',value=f"{policy['entry_network_fee_lamports']/LAMPORTS:.9f} SOL"),
             dict(rule='Entry / exit minimum-output tolerance',value=f"{policy['entry_slippage_bps']/100}% / {policy['exit_slippage_bps']/100}%"),
             dict(rule='Maximum sell attempts',value=policy['maximum_exit_attempts']),
             dict(rule='Tips',value='0 SOL'),
             dict(rule='Creator fee credit in reference buyer cost',value=bool(policy['creator_fee_credit_enabled']))]
display(pd.DataFrame(policy_view))
''')
md('''
## PnL and cash graph by slot

Closed PnL is booked at each exit. Failed entry fees are booked at the native
failure slot; failed sell fees are included in eventual closed PnL. Drawdown
uses this realized curve, not marked open tokens. The lower chart shows cash
spent/returned, including capital in open positions. It is not final profit.
''')
code('''
events=EVENTS[RUN]
start,end=min(CLOCKS['development']),max(CLOCKS['development'])
x=np.r_[0,events['slot']-start,end-start]
y=np.r_[0,events['pnl_lamports'].cumsum()/LAMPORTS,events['pnl_lamports'].sum()/LAMPORTS]
fig,axes=plt.subplots(3,1,figsize=(12,9),sharex=True,layout='constrained')
axes[0].step(x,y,where='post',color=COLORS['before'],linewidth=2)
axes[0].set_title('Original strategy: closed contribution -1.150398400 SOL; six positions unresolved')
axes[0].set_ylabel('Net closed PnL + entry fees (SOL)')
axes[0].annotate(f'{y[-1]:+.9f} SOL',(x[-1],y[-1]),xytext=(-8,14),textcoords='offset points',ha='right')
drawdown=y-np.maximum.accumulate(y)
axes[1].step(x,drawdown,where='post',color=BAD)
axes[1].fill_between(x,drawdown,0,step='post',color=BAD,alpha=.12)
axes[1].set_ylabel('Realized-curve drawdown (SOL)')
cash=LEDGERS[RUN]
cash=cash[cash['cash_delta_lamports']!=0]
axes[2].step([0,*(cash['slot']-start),end-start],
             [0,*(cash['cash_delta_lamports'].cumsum()/LAMPORTS),cash['cash_delta_lamports'].sum()/LAMPORTS],
             where='post',color=COLORS['before'])
axes[2].set_ylabel('Native cash change (SOL)')
axes[2].set_xlabel(f'Slots since {start}')
for ax in axes:
    ax.axhline(0,color='#aaaaaa',linewidth=.8)
    ax.grid(alpha=.15)
finish_plot(fig,'original-first-hour-pnl-and-cash.png')
display(events[['token','slot','utc','event','pnl_SOL','cumulative_pnl_SOL']])
''')
md('''
## Every original selected token

All 38 signals remain in chronological order, with GMGN links. Open positions
have an unavailable final PnL rather than zero. Risk rejections have no filled
buy and their recorded cost is shown separately from completed trades.
''')
code('''
ENTRY_EXIT,FILL_MARKERS=entry_exit_records(RUN)
assert len(ENTRY_EXIT)==38 and len(FILL_MARKERS)==30
assert (FILL_MARKERS['side']=='BUY').sum()==18 and (FILL_MARKERS['side']=='SELL').sum()==12
entry_exit_view=['token','group','reason','signal_slot','entry_slot','entry_utc','exit_slot','exit_utc',
                 'entry_SOL','exit_receipt_SOL','net_pnl_SOL','open_cost_SOL','gmgn_url']
display(Markdown('### Entry and exit list for every original signal'))
linked_table(ENTRY_EXIT[entry_exit_view])
view=['token','outcome','reason','signal_slot','entry_slot','exit_slot','entry_SOL',
      'exit_receipt_SOL','net_pnl_SOL','open_cost_SOL','failed_execution_fees_SOL','remaining_tokens_raw','gmgn_url']
linked_table(positions[view])
buys=positions[positions['entry_SOL']>0]
assert len(buys)==18
display(Markdown('### All 18 filled buys'))
linked_table(buys[['token','outcome','signal_slot','entry_slot','exit_slot','entry_SOL',
                   'net_pnl_SOL','open_cost_SOL','holding_seconds','gmgn_url']])
''')
md('''
## Profitable, losing, failed and open outcomes
''')
code('''
for outcome in ['Profitable','Losing','Entry failed','Entry rejected','Open / unresolved']:
    frame=positions[positions['outcome']==outcome]
    if len(frame):
        display(Markdown(f'### {outcome}: {len(frame)}'))
        linked_table(frame[view])
known=positions[(positions['status']=='CLOSED')|(positions['status']=='ENTRY_FAILED')]
fig,ax=plt.subplots(figsize=(12,5),layout='constrained')
ax.bar(np.arange(len(known)),known['net_pnl_SOL'],color=[GOOD if v>0 else BAD for v in known['net_pnl_SOL']])
ax.set_xticks(np.arange(len(known)),known['token'],rotation=40,ha='right',fontsize=8)
ax.set_ylabel('Net contribution (SOL)')
ax.set_title('Original closed positions and failed-entry fees')
ax.axhline(0,color='#aaaaaa',linewidth=.8)
ax.grid(axis='y',alpha=.15)
finish_plot(fig,'original-pnl-by-token.png')
''')
md('''
## Inspect an original position

Change `TOKEN` to a mint in the selection table and rerun this cell. The chart
shows historical price/reserve context and native position cashflow, with signal,
entry and exit slots. The native order and cash logs and signal snapshot follow.
The example is the profitable **NEZARA** position.
''')
code('''
TOKEN='4rUC69MxwFsbFRjWRcKYYd3NtYX2T7z44dDZ1LSUpump'
inspect_position(RUN,TOKEN)
''')
md('''
## Losing original position: Qweb
''')
code('''
inspect_position(RUN,'AhWaFsSporAQC3iRMr7cpTq7A9nz3ZQ2v5YSoBMFpump')
''')
md('''
## Open original position: Snoopy

The conditional replay cannot certify a later historical seller's inventory.
Its unresolved context remains in the logs. Cash spent is not a confirmed
terminal loss, and no synthetic exit is invented.
''')
code('''
inspect_position(RUN,'3qzfRHodFybxKFipAk2Gy7EBcBYr8frWRzo3bLWYpump')
''')
md('''
## Failed original entry: p-
''')
code('''
inspect_position(RUN,'Ej6HmGnz9EsPF5Yh6sVHPgwE2rXv1zt15hHxZRP7pump')
''')
md('''
## Risk-rejected original entry: up
''')
code('''
inspect_position(RUN,'2y6MTtkMkHdSP51enRDJay25NKLgGo5mX7ZVW6KEpump')
''')
md('''
## Every filled position, grouped by result

**Profitable**, **unprofitable** and **undecided** sections each have an entry/exit
list followed by a full chart for every position. Closed charts extend beyond
the actual sell slot, including later timeout exits. Undecided charts show all
retained price context and have a buy marker only. No exit is invented.

Green upward triangles mark native simulated **buy fills**; red downward triangles
mark native simulated **sell fills**. Their y-values are average curve execution
prices from the native ledger, normalized to the first retained historical
trade price. These exclude external fees; entry/exit cash and net PnL in the
tables include configured fees and rent. Historical observations and simulated
execution prices can differ after our reserve impact. The lower panel is
position cashflow, not marked PnL while it remains open. Longer positions also
have an inset showing the first 60 slots so the entry is easy to inspect.
''')
code('''
row_by_mint={p['mint']:p for p in RESULT['positions']}
chart_records=[]
for group in ['Profitable','Unprofitable','Undecided','Breakeven']:
    frame=ENTRY_EXIT[ENTRY_EXIT['group']==group]
    if not len(frame):continue
    display(Markdown(f'## {group}: {len(frame)} positions'))
    linked_table(frame[entry_exit_view])
    for row in frame.itertuples():
        display(Markdown(f'### [{row.token}]({row.gmgn_url})'))
        filename=position_entry_exit_chart(RUN,row.mint)
        chart_records.append(dict(mint=row.mint,token=row.token,group=group,chart=filename,
                                  entry_slot=int(row.entry_slot),exit_slot=int(row.exit_slot) if pd.notna(row.exit_slot) else None))
assert len(chart_records)==18
assert set(c['mint'] for c in chart_records)==set(buys['mint'])
assert sum(c['exit_slot'] is not None for c in chart_records)==12
''')
md('''
## Failed and rejected entries

These orders never established a position. They have signal/attempt lines, with
no buy or sell triangles. Failed entry fees stay in the cash/PnL accounting.
''')
code('''
attempt_mints=[]
for group in ['Entry failed','Entry rejected']:
    frame=ENTRY_EXIT[ENTRY_EXIT['group']==group]
    display(Markdown(f'## {group}: {len(frame)} signals'))
    linked_table(frame[entry_exit_view])
    for offset in range(0,len(frame),6):
        page=frame.iloc[offset:offset+6]
        fig,axes=plt.subplots(3,2,figsize=(13,11),layout='constrained')
        for ax,row in zip(axes.flat,page.itertuples()):
            attempt_mints.append(row.mint)
            end,markers=draw_entry_exit(ax,RUN,row.mint)
            assert markers.empty
            ax.set_title(row.token+' ('+row.mint[:7]+'…)\\n'+row.reason,fontsize=9)
            ax.legend(fontsize=7)
        for ax in list(axes.flat)[len(page):]:ax.set_visible(False)
        filename='attempts-'+group.lower().replace(' ','-')+f'-{offset//6+1}.png'
        finish_plot(fig,filename)
assert len(attempt_mints)==20 and set(attempt_mints).isdisjoint(c['mint'] for c in chart_records)
gallery_mints=[c['mint'] for c in chart_records]+attempt_mints
assert len(gallery_mints)==38 and set(gallery_mints)==set(positions['mint'])
''')
md('''
## Original strategy under other execution assumptions

Every row below uses the original entry/exit rules and this first cohort.
Strict historical reserves and virtual settlement keep historical prices;
virtual settlement adds assumed liquidity. Conditional models carry own reserve
effects and use fixed historical sell quantities or observed sell fractions.
They differ in fills and unresolved outcomes. These are alternate scenarios,
not strategy revisions, and their PnLs are never added together.
''')
code('''
scenario_names=['development-baseline','development-strict-before',
                'development-conditional_fixed-before','development-conditional_fraction-before']
scenarios=pd.read_csv(RUN_ROOT/'before_after.csv')
scenarios=scenarios[scenarios['run'].isin(scenario_names)]
assert len(scenarios)==4 and set(scenarios['cohort_start_utc'])=={'2026-10-10T16:00:00Z'}
display(scenarios[['run','execution_mode','counterpart_sell_fraction','net_closed_pnl_SOL',
                   'filled','closed','open','open_cost_SOL','synthetic_shortfall_SOL']])
''')
md('''
## Coverage and reproduction

Retained finalized empty-block evidence explains the publication interval at
slot 455328000. The main native result has no other publication gaps. Decoded
scope does not prove complete transfers or exact holders. Counterparty
inventory/slippage, reactions and landing remain modeled. Network/rent charges
are configured scenarios and tips are zero. Full per-observation strategy
traces were not enabled; recorded signal snapshots and native order logs are
shown without inventing missing decisions.

Use **Python 3.13.12 (Sniper research)** in VS Code. Saved outputs can be viewed
without running a kernel. Rerunning writes local CSV/PNG evidence only.
''')
code('''
positions.to_csv(EXPORT/'original_all_signals.csv',index=False)
ENTRY_EXIT.to_csv(EXPORT/'all_positions_entry_exit.csv',index=False)
FILL_MARKERS.to_csv(EXPORT/'native_fill_markers.csv',index=False)
pd.DataFrame(chart_records).to_csv(EXPORT/'full_position_chart_index.csv',index=False)
for group,frame in ENTRY_EXIT.groupby('group'):
    frame.to_csv(EXPORT/('positions_'+group.lower().replace(' ','_')+'.csv'),index=False)
buys.to_csv(EXPORT/'original_filled_buys.csv',index=False)
SUMMARY.to_csv(EXPORT/'original_summary.csv',index=False)
events.to_csv(EXPORT/'original_pnl_events.csv',index=False)
LEDGERS[RUN].to_csv(EXPORT/'original_cash_events.csv',index=False)
POSTINGS[RUN].to_csv(EXPORT/'original_ledger_postings.csv',index=False)
scenarios.to_csv(EXPORT/'original_execution_scenarios.csv',index=False)
for label in ['Profitable','Losing','Entry failed','Entry rejected','Open / unresolved']:
    positions[positions['outcome']==label].to_csv(EXPORT/('original_'+label.lower().replace(' / ','_').replace(' ','_')+'.csv'),index=False)
receipt=dict(complete=True,strategy='original',cohort_start_utc='2026-10-10T16:00:00Z',
             cohort_end_utc='2026-10-10T17:00:00Z',selected_runs=RUN_IDS,
             native_result_hashes={n:sha(RUN_ROOT/n/'result.json') for n in RUN_IDS},
             source_manifest_hashes=SOURCE_HASHES,all_native_ledgers_conserve=True,
             all_cash_and_position_contributions_reconcile=True,original_policy_verified=True,
             signals=len(positions),filled_buys=len(buys),gallery_token_count=len(gallery_mints),
             full_position_chart_count=len(chart_records),native_buy_markers=int((FILL_MARKERS['side']=='BUY').sum()),
             native_sell_markers=int((FILL_MARKERS['side']=='SELL').sum()),
             position_groups=ENTRY_EXIT['group'].value_counts().to_dict(),
             closed_contribution_lamports=RESULT['summary']['realized_net_pnl_lamports'],
             open_cost_lamports=RESULT['summary']['open_cash_cost_lamports'],
             full_portfolio_pnl_lamports=None,
             output_hashes={str(p.relative_to(EXPORT)):sha(p) for p in sorted(EXPORT.rglob('*')) if p.suffix in ['.csv','.png']})
(EXPORT/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\\n')
print('Original strategy only: scope, all 38 signals, 18 buys, ledger and PnL totals verified.')
''')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    nb=nbformat.v4.new_notebook(cells=cells)
    nb.metadata['kernelspec']=dict(display_name='Python 3.13.12 (Sniper research)',language='python',name='sniper-research')
    nb.metadata['language_info']=dict(name='python',version=sys.version.split()[0])
    nbformat.validate(nb)
    for index,cell in enumerate(nb.cells):
        if cell.cell_type=='code':compile(cell.source,f'notebook cell {index}','exec')
    if args.execute:
        from jupyter_client import KernelManager
        from nbclient import NotebookClient
        runtime=ROOT/'var/dnipro/notebook-runtime'
        for key,name in {'MPLCONFIGDIR':'matplotlib','IPYTHONDIR':'ipython','JUPYTER_RUNTIME_DIR':'jupyter'}.items():
            folder=runtime/name;folder.mkdir(parents=True,exist_ok=True);os.environ[key]=str(folder)
        km=KernelManager(kernel_name='sniper-research')
        client=NotebookClient(nb,km=km,kernel_name='sniper-research',timeout=180,resources={'metadata':{'path':str(ROOT)}})
        try:client.execute()
        except Exception:
            nbformat.write(nb,TARGET.with_suffix('.partial.ipynb'));raise
    nbformat.validate(nb)
    stage=TARGET.with_suffix('.staging.ipynb');nbformat.write(nb,stage);stage.replace(TARGET)
    qa=dict(notebook=str(TARGET),executed=args.execute,
            code_cells=sum(c.cell_type=='code' for c in nb.cells),
            executed_code_cells=sum(c.cell_type=='code' and c.execution_count is not None for c in nb.cells),
            image_outputs=sum('image/png' in o.get('data',{}) for c in nb.cells if c.cell_type=='code' for o in c.outputs),
            error_outputs=sum(o.get('output_type')=='error' for c in nb.cells if c.cell_type=='code' for o in c.outputs),
            sha256=hashlib.sha256(TARGET.read_bytes()).hexdigest())
    if args.execute:
        (ROOT/'var/dnipro/sniper-original-position-charts-20261014-v4/notebook_qa.json').write_text(json.dumps(qa,indent=2)+'\n')
    print(json.dumps(qa,indent=2))


if __name__=='__main__':main()
