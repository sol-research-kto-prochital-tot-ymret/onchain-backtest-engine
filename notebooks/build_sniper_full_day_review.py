"""Create and execute the October 12 notebook from frozen artifacts only."""
from pathlib import Path
import json
import os
import time

import nbformat as nbf
from nbclient import NotebookClient

ROOT = Path(__file__).absolute().parents[1]
DATA = ROOT / 'var/dnipro/sniper-oct12-no-dev-flat-review-20261016-v1'
TARGET = ROOT / 'notebooks/sniper_oct12_no_dev_sell_all_20261016.ipynb'


def main():
    assert not TARGET.exists(), 'Preserve an already delivered notebook'
    md, code = nbf.v4.new_markdown_cell, nbf.v4.new_code_cell
    nb = nbf.v4.new_notebook()
    nb.cells = [
        md('''# October 12 full day — without dev sell all

**October 12, 2026, 00:00–24:00 UTC launches; 10,000 SOL starting wallet.**
The developer-sold-all entry requirement is disabled. Original sizing, the first
two-slot entry window, one-slot execution delay, +5% own take profit, five-minute
fallback and 5% slippage remain unchanged. Creator fees and reserve-capped exits
use the saved native backtest implementation.

**Recorded outcome:** 251 profitable tokens, 318 losing, 80 unresolved and 238
submitted failed entries. Realized net is **−109.602394368 SOL**, including failed
entry fees. **152.319150420 SOL** remains committed to unresolved exposure.
Final portfolio PnL is unknown; unresolved tokens are not counted as realized losses.

This notebook reads frozen local results and chart context. It does not query a
server or run another backtest. Select **Python 3.13.12 (Sniper research)** to rerun;
executed tables and example charts are already saved.'''),
        code('''from pathlib import Path
import sys
import pandas as pd
import plotly.io as pio
from IPython.display import display, Markdown
ROOT = next((candidate for p in [Path.cwd(), *Path.cwd().parents]
             for candidate in (p, p/'onchain-backtest-engine')
             if (candidate/'src/backtest').is_dir()), None)
assert ROOT is not None, 'Open the notebook inside the onchain-backtest-engine workspace'
sys.path.insert(0, str(ROOT/'notebooks'))
from sniper_full_day_review import FullDayReview, LABEL
pio.renderers.default = 'plotly_mimetype'
pd.set_option('display.max_columns', 30)
pd.set_option('display.max_colwidth', 90)
review = FullDayReview()
display(review.overview())'''),
        md('''## Strategy settings and accounting

Realized PnL records completed positions at their sell fill, including their
failed sell fees, plus fees for submitted failed entries. Cash change is separate:
it also includes capital spent on open tokens and posted rent. Open committed
cost includes those positions' entry costs and failed execution fees.

Buys and sells use native fills and settlement ledger amounts. Green ▲ and red ▼
mark average **curve execution prices**; external fees are separate. Hollow diamonds
are recorded decisions, shown at historical reference prices. Black × marks an
attempt that failed without a fill. Landing at the start of the next slot is a
simulation assumption, not an observed live order.'''),
        code("display(pd.Series(review.summary['policy'], name='Saved native policy').to_frame())"),
        md('''## PnL curve and capital exposure

The first panel is realized net PnL. The final point must equal the saved native
result. The second panel is cash change, and the third is open committed cost;
neither is a marked portfolio valuation.'''),
        code('display(review.strategy_figure())'),
        md('''## Results by outcome and exit reason

Closed-position PnL below excludes failed entries; the overview and realized
curve include their fees. Submitted failures and risk rejections are distinct
from losing positions.'''),
        code('''display(review.table.groupby('group', dropna=False).agg(
    tokens=('mint','size'), closed_PnL_SOL=('closed_PnL_SOL', lambda x:x.sum(min_count=1)),
    failed_fees_SOL=('failed_fees_SOL','sum'), unresolved_cost_SOL=('unresolved_cost_SOL','sum')))
display(review.table[review.table.group.isin(['Profitable','Losing'])].groupby('reason').agg(
    tokens=('mint','size'), profitable=('closed_PnL_SOL', lambda x:int((x>0).sum())),
    closed_PnL_SOL=('closed_PnL_SOL','sum')))'''),
        md('''## Select any token

Filter by outcome and search by symbol or mint. Each selection shows the entry
and exit, tracked buyer, developer, fees, signal thresholds, complete recorded
order lifecycle and any conditional replay blockers. The selector requires an
active kernel. Saved examples follow below for viewing without one.

Wallet inventory and positive-wallet counts are modeled from retained swaps and
known transfers. They are **not exact holders**. The chart stops inventory at its
first replay blocker. Historical prices may continue after modeled coverage ends.'''),
        code('display(review.browser())'),
        md('''## All entries and attempts

These are all 887 signals. Scroll horizontally for entry/exit slots, the tracked
wallet, developer and GMGN link. Full tables remain available as CSV.'''),
        code('review.display_table()'),
        md('## Profitable tokens — 251'),
        code("review.display_table('Profitable')"),
        md('## Losing tokens — 318'),
        code("review.display_table('Losing')"),
        md('''## Unresolved tokens — 80

The first causal blocker matters: a later migration label can overwrite it.
Seventy-five cases lack sufficient historical seller acquisition/transfer proof.
Five are fixed-transfer conflicts caused by different simulated sender balances.
An unresolved case is not a landed failed sell or a realized loss.'''),
        code("review.display_table('Unresolved')"),
        md('''## Submitted failed entries — 238

These attempts paid the configured execution fee and acquired no token position.
There were zero capital-risk rejections. Failed attempts are shown as × rather
than buy markers.'''),
        code("review.display_table('Entry failed')"),
        md('''## Saved token charts

Examples include the largest closed profit, largest closed loss, one unresolved
position, one failed entry and a token with retained detailed evaluations. All
other tokens can be selected above or opened with `review.show(MINT)`.

Detailed per-event strategy traces exist for 78 tokens. Every token still has
recorded order lifecycle events and signal/outcome metadata. Missing evaluations
are labelled in the chart; no decision log is invented.

Chart context starts at launch and ends shortly after exit, or about 6½ minutes
after entry for unresolved positions. It is a bounded view of retained events,
not a complete token history.'''),
        code('''examples = [
    review.table[review.table.group=='Profitable'].sort_values('closed_PnL_SOL', ascending=False).iloc[0]['mint'],
    review.table[review.table.group=='Losing'].sort_values('closed_PnL_SOL').iloc[0]['mint'],
    review.table[review.table.group=='Unresolved'].iloc[0]['mint'],
    review.table[review.table.group=='Entry failed'].iloc[0]['mint'],
    review.table[review.table.detailed_trace_available].iloc[0]['mint'],
]
for mint in dict.fromkeys(examples):
    review.show(mint)'''),
        md('''## Open a particular token

Replace `TOKEN` with a mint or paste a full GMGN token URL. The code keeps the
address and discards a URL query string.'''),
        code('''TOKEN = None  # e.g. 'https://gmgn.ai/sol/token/<mint>'
if TOKEN:
    mint = TOKEN.rstrip('/').split('/')[-1].split('?')[0]
    assert mint in review.positions[LABEL], 'This token is not among the 887 signals'
    review.show(mint)'''),
        md('''## CSVs and verification

CSV exports contain all positions by outcome, exact fill markers, realized PnL
events, cash changes and open-cost events. The notebook verifies native file
hashes, asset conservation in every ledger transaction, all order identities,
and reconciliation to the saved PnL and exposure totals.

The original cache and native result remain unchanged. This is an evidence review
of one simulated day, not a new backtest or independent validation period.'''),
        code('''csv_folder = review.export()
display(pd.Series({
    'source_input_sha256': review.summary['source_input_sha256'],
    'native_source_sha256': review.summary['implementation']['engine_source_sha256'],
    'orders_matched_by_exact_native_hash': len(review.orders),
    'actual_buy_markers': sum(f['side']=='BUY' for f in review.fill_markers),
    'actual_sell_markers': sum(f['side']=='SELL' for f in review.fill_markers),
    'realized_event_total_SOL': sum(r['pnl_lamports'] for r in review.pnl_events)/1e9,
    'open_cost_event_total_SOL': sum(r['delta'] for r in review.exposure_events)/1e9,
    'csv_folder': str(csv_folder),
}, name='Verification').to_frame())'''),
    ]
    nb.metadata = {'kernelspec':{'display_name':'Python 3.13.12 (Sniper research)',
                                'language':'python', 'name':'sniper-research'},
                   'language_info':{'name':'python','version':'3.13.12'}}
    for i, cell in enumerate(nb.cells):
        if cell.cell_type=='code':compile(cell.source, f'cell-{i}', 'exec')
    began = time.monotonic()
    try:
        NotebookClient(nb, timeout=240, kernel_name='sniper-research',
            resources={'metadata':{'path':str(ROOT)}}).execute()
    except Exception:
        nbf.write(nb, TARGET.with_suffix('.partial.ipynb'))
        raise
    nbf.validate(nb)
    errors = [o for c in nb.cells for o in c.get('outputs',[]) if o.output_type=='error']
    assert not errors
    figures = sum('application/vnd.plotly.v1+json' in o.get('data',{})
                  for c in nb.cells for o in c.get('outputs',[]))
    assert figures >= 5
    stage = TARGET.with_suffix('.staging.ipynb')
    nbf.write(nb, stage); stage.replace(TARGET)
    qa = dict(complete=True, notebook=str(TARGET), cells=len(nb.cells),
        executed_code_cells=sum(c.cell_type=='code' and c.execution_count is not None for c in nb.cells),
        errors=[], plotly_figures=figures, seconds=round(time.monotonic()-began,2))
    (DATA/'notebook-qa.json').write_text(json.dumps(qa,indent=2)+'\n')
    print(json.dumps(qa), flush=True)


if __name__=='__main__':main()
