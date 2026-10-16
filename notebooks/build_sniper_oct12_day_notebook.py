"""Build and execute the full-day review; no strategy execution or network reads."""
import base64
from bisect import bisect_right
from collections import Counter
import json
import os
from pathlib import Path
import shutil

import nbformat as nbf
from nbclient import NotebookClient
import plotly.io as pio

from sniper_oct12_day_review import FullDayReview
from prepare_sniper_oct12_review import ROOT, OUT, sha, save

TARGET = ROOT/'notebooks/sniper_oct12_no_dev_flat_20261016.ipynb'


def main():
    review = FullDayReview()
    assert len(review.positions)==887 and len(review.markers)==1218
    assert Counter(review.table.group)=={'Profitable':251, 'Losing':318, 'Unresolved':80, 'Entry failed':238}
    assert sum(r['side']=='BUY' for r in review.markers)==649
    assert sum(r['side']=='SELL' for r in review.markers)==569
    examples = [
        review.table[review.table.group=='Profitable'].sort_values('closed_pnl_sol', ascending=False).iloc[0]['mint'],
        review.table[review.table.group=='Losing'].sort_values('closed_pnl_sol').iloc[0]['mint'],
        review.table[review.table.first_causal_blocker=='CONDITIONAL_HISTORICAL_SELLER_BALANCE_UNPROVEN'].iloc[0]['mint'],
        review.table[review.table.first_causal_blocker=='CONDITIONAL_TRANSFER_SOURCE_INVENTORY_CONFLICT'].iloc[0]['mint'],
        review.table[review.table.group=='Entry failed'].iloc[0]['mint'],
        review.table[(review.table.group=='Profitable') & review.table.detailed_native_log].iloc[0]['mint'],
    ]
    examples = list(dict.fromkeys(examples))
    browser = next((Path.home() / 'Library/Caches/ms-playwright').glob('chromium_headless_shell-*/chrome-mac*/headless_shell'), None)
    if browser is not None and browser.exists() and not os.environ.get('BROWSER_PATH'):
        os.environ['BROWSER_PATH'] = str(browser)
    image_dir = OUT/'visual-qa'
    image_dir.mkdir(exist_ok=True)
    main_fig = review.strategy_figure()
    main_fig.write_image(image_dir/'pnl-and-exposure.png', width=1450, height=950)
    chart_checks = []
    for i, mint in enumerate(examples):
        figure = review.figure(mint)
        p = review.positions[mint]
        assert figure.layout.meta['buy_fills']==int(p['entry_cash_lamports']>0)
        assert figure.layout.meta['sell_fills']==int(p['status']=='CLOSED')
        if p['status']=='ENTRY_FAILED':
            assert figure.layout.meta['failed_attempts']>=1
        for trace in figure.data:
            for custom in trace.customdata if trace.customdata is not None else []:
                assert isinstance(custom[3], str), 'Native boundaries must not lose precision in browser JSON'
        figure.write_image(image_dir/f'example-{i+1}.png', width=1550, height=1450)
        chart_checks.append(dict(status=p['status'], symbol=p['symbol'], **figure.layout.meta))
    inventory_checks = 0
    for mint, trace in review.traces.items():
        values = review.inventory(mint)
        bounds = [r['boundary'] for r in values]
        deadline = review.births[mint]['chart_end_unix']
        for row in trace:
            if row['action']!='EXIT_EVALUATION' or row['time']>deadline:
                continue
            idx = bisect_right(bounds, row['boundary_ordinal'])-1
            if idx>=0:
                # The display series is in millions; compare to native atomic tokens.
                assert abs(values[idx]['tracked']*10**12-int(row['wallet_tokens'])) <= 1, mint
                inventory_checks += 1
    nb = nbf.v4.new_notebook()
    md, code = nbf.v4.new_markdown_cell, nbf.v4.new_code_cell
    nb.cells = [
        md('''# October 12 full day — without dev-sell-all

**October 12, 2026, 00:00–24:00 UTC · 10,000 SOL starting wallet.**
This notebook reviews the completed native fork run with the developer-sell-all entry rule disabled.
Original sizing, the first-two-slot search window, +5% own take profit, five-minute timeout,
5% slippage, creator-fee accounting and one-slot execution delay stay unchanged.

**251 profitable · 318 losing · 80 unresolved · 238 submitted failed entries.**
Realized net is **−109.602394368 SOL**, including failed-entry fees. Another
**152.319150420 SOL** remains committed to unresolved positions. These positions
are not counted as realized losses, and final portfolio PnL remains unknown.

The notebook reads frozen local artifacts. **Run All does not rerun the backtest,
query ClickHouse/RPC, place orders, or change production.** Use the registered
**Python 3.13.12 (Sniper research)** kernel.'''),
        code('''from pathlib import Path
import sys
import pandas as pd
import plotly.io as pio
from IPython.display import display, Markdown

ROOT = next((p if (p/'src/backtest').is_dir() else p/'onchain-backtest-engine')
            for p in [Path.cwd(), *Path.cwd().parents]
            if (p/'src/backtest').is_dir() or (p/'onchain-backtest-engine/src/backtest').is_dir())
sys.path.insert(0, str(ROOT/'notebooks'))
from sniper_oct12_day_review import FullDayReview, GROUPS
DATA = ROOT/'var/dnipro/sniper-oct12-no-dev-flat-review-20261016-v1'
if not (DATA/'context-receipt.json').exists():
    DATA = Path('/Volumes/KINGSTON/solana-research/sniper-oct12-no-dev-flat-review-20261016-v1')
pio.renderers.default = 'plotly_mimetype'
pd.set_option('display.max_columns', 30)
pd.set_option('display.max_colwidth', 100)
review = FullDayReview(DATA)
display(review.summary_table())'''),
        md('''## Realized PnL, cash and committed cost

The first panel recognizes closed-position results at the simulated sell boundary
and charges failed-entry fees when released. Open exposure is excluded.
The second panel shows cash change, not portfolio PnL. The third shows committed
cost, including failed exit fees. All three curves reconcile to the native ledger.
The horizontal axis uses slots; hover shows exact UTC and transaction boundaries.'''),
        code('display(review.strategy_figure())'),
        md('''## Complete position lists

Every signal appears exactly once below. A submitted failed entry has no buy fill
and pays its execution fee; a risk rejection would pay no fees. There were zero
risk rejections. Unresolved exposure is kept separate from losing positions.
Token, developer and tracked-buyer links use GMGN.'''),
        code('''for group in GROUPS:
    count = int((review.table.group == group).sum())
    if count:
        display(Markdown(f'### {group} — {count} tokens'))
        review.display_table(group)'''),
        md('''## Browse any token

Choose an outcome and token, or search a symbol/mint. The browser requires a
running kernel; complete saved examples are below for inspection without one.

Green ▲ and red ▼ are actual simulated buys and sells at average **curve** fill
prices. Hollow diamonds are decisions drawn at a historical reference price;
black crosses are failed attempts, not fills. Curve prices exclude trading fees,
network fees and rent, which remain in the position metadata and ledger.
The dotted green line in the PnL panel is the own +5% take-profit threshold.

Plots preserve transaction order. Tick labels are slots since launch, and hover
keeps the full native boundary as a string. Native block-clock execution points
are simulated landing assumptions, not observed on-chain transactions.'''),
        code('display(review.browser())'),
        md('''## Saved examples

The following cover the biggest closed winner, biggest closed loser, missing
seller-inventory evidence, a fixed-transfer conflict, a failed entry, and a token
with detailed native evaluations. Change `TOKEN` below to inspect any mint from
the tables.

Historical context runs from launch through exit plus 30 seconds. Unresolved
positions retain approximately 6½ minutes after entry; failed entries retain
50 seconds after their signal. Historical reserve discontinuities break the
price line. Continuing historical trading after a blocker is not a proven
continuation of the simulated position.'''),
        code('EXAMPLES = '+repr(examples)+'\nfor mint in EXAMPLES:\n    review.show(mint)'),
        md('''## Inspect another token

Set `TOKEN` to a mint from any outcome table, then rerun the next cell.'''),
        code('TOKEN = EXAMPLES[0]\n# review.show(TOKEN)  # Uncomment after choosing a token.'),
        md('''## Unresolved cases and log coverage

**75 positions** lack enough retained historical seller acquisitions or transfers
to prove inventory at the next sell. **5 positions** have a fixed-transfer
conflict: our simulated intervention leaves the sender with fewer tokens than
the historical transfer requires. Neither proves that our own sell was submitted
and failed. One final lifecycle label was overwritten by migration; the first
causal blocker is preserved in the table.

Detailed per-event strategy evaluations exist for **78 tokens**. All **887 signals**
have native signal metadata and a complete order audit, and all **649 buys and
569 sells** have exact native fill/ledger records. Missing detailed evaluations
are explicitly labeled rather than reconstructed as if they were recorded logs.

Developer and tracked-buyer balances use retained swaps, native repriced amounts,
and the bounded verified transfer overlay. Wallet counts are incomplete modeled
coverage, not exact holders. These series stop at the first conditional blocker.
Historical chart lines are retained observations, not a complete chain audit.'''),
        code('''display(review.table[review.table.group == 'Unresolved'].groupby('first_causal_blocker').agg(
    positions=('mint', 'count'), committed_cost_sol=('open_committed_cost_sol', 'sum')))
display(review.table.groupby(['group', 'detailed_native_log']).size().rename('tokens').to_frame())'''),
        md('''## Parameters and provenance

The notebook binds the immutable native result, all order/fill records, and the
bounded historical context with file hashes. It uses the repaired input version
from the completed experiment; no additional history was silently added for the
new positions. Network fees, fee tiers and within-slot landing remain the saved
simulation assumptions.'''),
        code('''display(pd.Series(review.summary['policy'], name='Saved policy').to_frame())
display(pd.Series({
    'source_input_sha256': review.summary['source_input_sha256'],
    'native_manifest_sha256': review.receipt['native_manifest_sha256'],
    'engine_source_sha256': review.summary['implementation']['engine_source_sha256'],
    'full_chain_coverage': review.summary['full_chain_coverage'],
    'exact_execution': review.summary['exact_execution'],
    'chart_facts': review.receipt['chart_facts'],
    'chart_tokens': review.receipt['positions'],
}, name='Retained provenance').to_frame())'''),
        md('''## CSV exports

The local data folder contains all positions split by outcome, native fill
markers, realized PnL events, cash events and committed-cost events. The `native`
folder preserves the complete order audit, retained detailed logs, conditional
errors, fills and ledger. There is no separate HTML report.'''),
        code('print(review.export)\ndisplay(pd.DataFrame({"CSV file": sorted(p.name for p in review.export.glob("*.csv"))}))'),
    ]
    nb.metadata = {'kernelspec':dict(display_name='Python 3.13.12 (Sniper research)', language='python', name='sniper-research'),
                   'language_info':dict(name='python', version='3.13.12')}
    for index, cell in enumerate(nb.cells):
        if cell.cell_type=='code':
            compile(cell.source, f'cell-{index}', 'exec')
    try:
        NotebookClient(nb, timeout=240, kernel_name='sniper-research', resources={'metadata':{'path':str(ROOT)}}).execute()
    except Exception:
        nbf.write(nb, TARGET.with_suffix('.partial.ipynb'))
        raise
    nbf.validate(nb)
    errors = [o.get('evalue') for c in nb.cells for o in c.get('outputs', []) if o.output_type=='error']
    assert not errors, errors
    # PNG alternatives keep the saved notebook readable in viewers without Plotly.
    figures = 0
    for cell in nb.cells:
        for output in cell.get('outputs', []):
            data = output.get('data', {})
            if 'application/vnd.plotly.v1+json' in data:
                fig = pio.from_json(json.dumps(data['application/vnd.plotly.v1+json']))
                image = fig.to_image(format='png', width=1450, height=950 if len(fig.data)==3 else 1450)
                data['image/png'] = base64.b64encode(image).decode('ascii')
                figures += 1
    stage = TARGET.with_suffix('.staging.ipynb')
    nbf.write(nb, stage)
    stage.replace(TARGET)
    shutil.copy2(TARGET, OUT/TARGET.name)
    source_dir = OUT/'notebook-source'
    source_dir.mkdir(exist_ok=True)
    for name in ('sniper_oct12_day_review.py', 'prepare_sniper_oct12_review.py',
                 'build_sniper_oct12_day_notebook.py', 'sniper_interactive_review.py'):
        shutil.copy2(ROOT/'notebooks'/name, source_dir/name)
    qa = dict(complete=True, notebook=str(TARGET), notebook_sha256=sha(TARGET),
        executed_code_cells=sum(c.cell_type=='code' and c.execution_count is not None for c in nb.cells),
        errors=errors, saved_plotly_figures=figures, png_alternatives=figures,
        signals=887, profitable=251, losing=318, unresolved=80, failed_entries=238,
        mapped_orders=len(review.orders), buy_markers=649, sell_markers=569,
        realized_pnl_lamports=sum(r['amount'] for r in review.pnl),
        open_committed_cost_lamports=sum(r['amount'] for r in review.exposure),
        native_reference_inventory_checks=inventory_checks, visual_examples=chart_checks,
        context_receipt_sha256=sha(OUT/'context-receipt.json'))
    save(OUT/'notebook-qa.json', qa)
    print(json.dumps(qa, indent=2), flush=True)


if __name__=='__main__':
    main()
