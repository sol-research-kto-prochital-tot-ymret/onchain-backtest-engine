"""Build and execute the original-versus-dev-flat interactive review notebook."""
from pathlib import Path
import json
import nbformat as nbf
from nbclient import NotebookClient

ROOT=Path(__file__).absolute().parents[1]
PATH=ROOT/'notebooks/sniper_dev_flat_interactive_20261015.ipynb'
nb=nbf.v4.new_notebook()
md=nbf.v4.new_markdown_cell;code=nbf.v4.new_code_cell
nb.cells=[
md('''# Original +5% strategy — developer-flat entry test

**October 10, 2026 · 16:00–17:00 UTC launch cohort.** The new version enters only
after the literal launch creator has zero observed inventory. Developers with
no observed buy also qualify. The original first-two-slot entry window, entry
sizing, +5% own take-profit, five-minute timeout, 50 SOL wallet, 5% slippage and
one-slot execution delay remain unchanged.

Green ▲ / red ▼ show actual simulated fills at average curve execution prices.
Hollow diamonds show decisions; black crosses show failed attempts. Historical
and replayed marginal prices are separate curves. Zoom and hover are interactive.
Ticks show slots since launch, with exact slot, transaction and UTC in hover.

**Accounting:** strategy PnL is cumulative realized closes and failed-entry fees.
Open committed cost is displayed separately. Per-position liquidation quotes
are estimates after configured fees, not completed sales. Wallet inventory/counts
are modeled from retained swaps and the bounded transfer overlay, not exact holder
coverage. A transfer out is not classified as a developer sale.

Use **Python 3.13.12 (Sniper research)**. All data is local; running this notebook
does not query ClickHouse/RPC or run another backtest.'''),
code('''from pathlib import Path
import sys
import pandas as pd
import plotly.io as pio
from IPython.display import display, Markdown
ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p/'src/backtest').is_dir())
sys.path.insert(0, str(ROOT/'notebooks'))
from sniper_interactive_review import NativeReview, outcome
pio.renderers.default = 'plotly_mimetype'
pd.set_option('display.max_columns', 30)
pd.set_option('display.max_colwidth', 80)
review = NativeReview()
display(review.summary())'''),
md('''## Strategy PnL and exposure

The first panel excludes unresolved tokens. The cash and open-cost panels show
where capital remains committed. Equal slot labels can contain multiple exact
transaction boundaries; per-token charts preserve that order.'''),
code('display(review.strategy_figure())'),
md('''## All entries and filtered control opportunities

`No entry signal` means the new strategy did not submit an order. It is distinct
from a risk rejection, submitted execution failure, closed loss or unresolved
position. The table includes every token signaled by either version.'''),
code("display(review.table)"),
md('''## Interactive token browser

Choose the run, outcome and token. An active notebook kernel is needed to change
the widget selection. The complete saved charts are also displayed below.'''),
code('display(review.browser())'),
md('''## Original control — all submitted positions

Every chart includes exact metadata and a sample of its decision log. Complete
logs remain in each native run's `strategy_trace.csv`. Reference PnL is computed
only at retained exit evaluations, using the native Pump quote function; it is
not forward-filled past an inventory conflict.'''),
code('''for group in ['Profitable', 'Losing', 'Unresolved', 'Entry failed', 'Entry rejected']:
    rows = review.table[(review.table.run == 'control') & (review.table.group == group)]
    if len(rows):
        display(Markdown(f'## {group} — {len(rows)}'))
        for mint in rows.mint:
            review.show('control', mint)'''),
md('''## Developer-flat version — all submitted positions

Entry metadata records whether the developer had sold all observed tokens or had
no observed buy. Zero inventory is checked at the decision boundary. It is not
a guarantee that the developer remains flat during the one-slot execution delay.'''),
code('''for group in ['Profitable', 'Losing', 'Unresolved', 'Entry failed', 'Entry rejected']:
    rows = review.table[(review.table.run == 'dev-flat') & (review.table.group == group)]
    if len(rows):
        display(Markdown(f'## {group} — {len(rows)}'))
        for mint in rows.mint:
            review.show('dev-flat', mint)'''),
md('''## Which original opportunities were filtered?

These tokens remain inspectable through the browser. The gate does not expand
the original two-slot search window. Literal creator inventory does not establish
that linked or unobserved developer wallets have exited.'''),
code("display(review.table[(review.table.run == 'dev-flat') & (review.table.group == 'No entry signal')])"),
md('''## Reproducible CSVs and figure data

The export contains comparison, position/fill tables, realized PnL events,
reference valuation diagnostics, modeled inventory snapshots, Plotly figure JSON
and hash receipts. It creates no separate HTML report.'''),
code("receipt = review.export_all()\ndisplay(receipt)\nprint(review.export)"),
]
nb.metadata={'kernelspec':{'display_name':'Python 3.13.12 (Sniper research)','language':'python','name':'sniper-research'},
             'language_info':{'name':'python','version':'3.13.12'}}
nbf.write(nb,PATH)
NotebookClient(nb,timeout=900,kernel_name='sniper-research',resources={'metadata':{'path':str(ROOT)}}).execute()
nbf.write(nb,PATH)
qa={'complete':True,'notebook':str(PATH),'cells':len(nb.cells),
    'executed_code_cells':sum(c.cell_type=='code' and c.execution_count is not None for c in nb.cells),
    'errors':[o.get('evalue') for c in nb.cells for o in c.get('outputs',[]) if o.output_type=='error'],
    'plotly_figures':sum('application/vnd.plotly.v1+json' in o.get('data',{}) for c in nb.cells for o in c.get('outputs',[]))}
assert not qa['errors']
(ROOT/'var/dnipro/sniper-dev-flat-20261015-v1/notebook-qa.json').write_text(json.dumps(qa,indent=2)+'\n')
print(json.dumps(qa),flush=True)
