"""Build an executed notebook for the original strategy's repaired 50 SOL run."""
from pathlib import Path
from textwrap import dedent
import json
import os
import nbformat
from nbclient import NotebookClient

ROOT = Path(__file__).absolute().parents[1]
TARGET = ROOT / 'notebooks/sniper_repaired_wallet50_20261015.ipynb'
cells = []


def md(source):
    cells.append(nbformat.v4.new_markdown_cell(dedent(source).strip()))


def code(source):
    cells.append(nbformat.v4.new_code_cell(dedent(source).strip()))


md('''
# Original sniper strategy — repaired 50 SOL run

**October 10, 2026, 16:00–17:00 UTC (19:00–20:00 Kyiv) launches only.**
50 SOL starting wallet; unchanged original strategy and 5% entry/exit slippage.
Native conditional protocol replay carries our reserve impact forward and
preserves observed counterpart sell fractions. Verified external token transfers
retain their fixed historical amounts and conserve inventory.

**Realized net PnL: −3.289255885 SOL**, including nine failed-entry fees.
28 positions closed: 14 profitable and 14 losing. One position remains unresolved
with **1.207495000 SOL** cash cost, so final portfolio PnL is unknown. Assigning
zero recovery to that position gives **−4.496750885 SOL**; that is a stress scenario.

Seven previously unresolved positions are now closed, including UP at
**+0.551637313 SOL**. The last position has a verified historical transfer whose
fixed amount exceeds its sender's counterfactual inventory after our intervention.
It has no invented exit or assigned terminal loss.

This notebook reads preserved local results, traces and source evidence. It
does not query production, collect data, trade or run the backtest when reopened.
Its dedicated full-trace replay exactly matches the repaired result's positions,
fills, ledger and entire summary. Earlier notebooks remain preserved separately.
''')
code('''
from pathlib import Path
import sys
ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p/'src/backtest').is_dir())
sys.path.insert(0, str(ROOT/'notebooks'))
import sniper_repaired_review as review
from IPython.display import Markdown, display
summary = review.load()
display(summary)
''')
md('''
## Entry and exit rules

An early reference buyer spends at least 1 SOL during the first two launch slots.
The strategy requires positive modeled inventory and unrecovered fee-adjusted
cost. It sizes the buy using the original formula so our 5% net target is below
that buyer's break-even by the configured epsilon. The entry logs record the
actual reference wallet, token inventory, cash cost, creator-fee credit, thresholds,
size, reserves, quoted token output and minimum accepted output.

Orders fill one slot after the decision. Exit is triggered by quoted net PnL
meeting the 5% target, or the 300-second timeout; execution remains subject to
liquidity and 5% minimum-output tolerance. Each token includes the recorded exit
evaluation and actual fill. A failed entry never becomes a losing position:
only its submitted execution fee is charged.

Creator income is modeled event-time accrual for the reference buyer's break-even
calculation. Another wallet's developer fees are not our trading profit. Network
charges and rent are configured scenarios; tips are zero.
''')
code('''
display(review.pd.DataFrame([{'parameter':k, 'value':v}
                           for k,v in review.RESULT['summary']['policy'].items()]))
''')
md('''
## PnL and cash by slot

The first curve books completed positions and failed-entry fees. The second
shows cash movement, including capital still spent on the unresolved position.
Neither curve assigns a market value to open tokens.
''')
code('''
review.pnl_chart()
display(review.EVENTS[['token','slot','utc','event','pnl_SOL','cumulative_pnl_SOL']])
''')
md('''
## Every signal: actual simulated entry and exit

All 38 signals remain visible, including nine submitted failed entries. A missing
exit on the unresolved position is intentional. Full metadata is available in
the CSV/JSON exports described at the bottom.
''')
code('''
columns = ['token','group','reason','signal_slot','entry_slot','entry_utc',
           'exit_slot','exit_utc','entry_SOL','exit_receipt_SOL','net_pnl_SOL',
           'open_cost_SOL','failed_execution_fees_SOL','gmgn_url']
review.linked_table(review.ENTRY_EXIT[columns])
''')
md('''
## Reading each token chart

- Green upward triangles are actual simulated buy fills; red downward triangles
  are actual simulated sell fills. Vertical lines show signal, buy and sell slots.
- Purple is observed historical marginal curve price; orange shows replayed
  state at strategy evaluations. Marker heights are average curve execution prices,
  excluding external fees, so they can differ from the marginal-price curves.
- Real SOL reserves distinguish executable liquidity from virtual pricing reserves.
- Exit quote PnL and the target show why the strategy scheduled a sale; the diamond
  shows the eventual closed net result after the one-slot delay and costs.
- The last panel shows the selected reference buyer's modeled inventory at
  evaluations. This is **not complete holder coverage**. Transfers and full
  holder history outside the recovered evidence remain unproven.

The tables below each chart retain the entry signal, exit evaluation, costs,
thresholds, creator, reference buyer, order notifications and any replay conflicts.
The default log table shows entry candidates, all order notifications and the last
eight exit checks. Every evaluation is retained in the per-token decisions CSV;
use the inspector at the bottom to display all rows for a selected token.
''')
for group, title in [('Profitable', 'Profitable positions — 14'),
                     ('Unprofitable', 'Losing positions — 14'),
                     ('Undecided', 'Unresolved position — 1'),
                     ('Entry failed', 'Failed entries — 9')]:
    md('## ' + title)
    code(f'''
    selected = review.ENTRY_EXIT[review.ENTRY_EXIT['group'] == {group!r}]
    review.linked_table(selected[columns])
    for mint in selected['mint']:
        display(Markdown('---'))
        review.inspect(mint)
    ''')
md('''
## Optional inspector: every logged evaluation for one token

Change `TOKEN` to a mint from the table and rerun this cell. The saved example
shows UP's final decision checks. Set `ALL_ROWS=True` to display its complete log.
''')
code('''
TOKEN = '2y6MTtkMkHdSP51enRDJay25NKLgGo5mX7ZVW6KEpump'
ALL_ROWS = False
review.linked_table(review.decisions(TOKEN, all_rows=ALL_ROWS))
''')
md('''
## Evidence, exports and reproduction

The original result is `var/dnipro/sniper-inventory-repair-20261015-v1/repaired-v3`.
The equal full-trace result is its sibling `development-repaired-before`.
No revised entry/exit strategy or additional date sample is mixed into this study.

The remaining unresolved token is 毒チワワ. The real sender transfers
158,769.203521 tokens; its simulated earlier buy obtains 155,358.106307 tokens,
a 3,411.097214-token shortfall. Fixed transfer replay stops at that conflict.
The real historical sale is verified; this is not evidence that our own sell
was submitted and failed. Scaling that transfer is a separate behavioral assumption.

Exports are under `var/dnipro/sniper-repaired-review-20261015-v1/`:
`all_positions_entry_exit.csv`, `native_fill_markers.csv`, `decision_log_all.csv`,
`pnl_events.csv`, `cash_events.csv`, result-group CSVs, and `chart_index.csv`.
Each token has a PNG, a complete decision-log CSV and a JSON containing its full
position metadata, entry signal, exit evaluation and counterparty errors.
The receipt pins hashes and validates 38 charts, 29 buy markers and 28 sell markers.

Use **Python 3.13.12 (Sniper research)** to rerun notebook cells. Saved outputs
are already rendered, so reading the notebook does not require executing it.
''')
code('''
receipt = review.save_receipt()
display({k:v for k,v in receipt.items() if k != 'output_hashes'})
print('Verified 38 charts, all signal logs, 29 buys, 28 exits and unchanged repaired PnL.')
''')

nb = nbformat.v4.new_notebook(cells=cells)
nb.metadata['kernelspec'] = dict(display_name='Python 3.13.12 (Sniper research)',
                                  language='python', name='sniper-research')
for index, cell in enumerate(nb.cells):
    if cell.cell_type == 'code':
        compile(cell.source, f'cell-{index}', 'exec')
for key, name in {'MPLCONFIGDIR':'matplotlib','IPYTHONDIR':'ipython',
                  'JUPYTER_RUNTIME_DIR':'jupyter'}.items():
    folder = ROOT/'var/dnipro/notebook-runtime'/name
    folder.mkdir(parents=True, exist_ok=True)
    os.environ[key] = str(folder)
client = NotebookClient(nb, kernel_name='sniper-research', timeout=240,
                        resources={'metadata':{'path':str(ROOT)}})
try:
    client.execute()
except Exception:
    nbformat.write(nb, TARGET.with_suffix('.partial.ipynb'))
    raise
nbformat.validate(nb)
stage = TARGET.with_suffix('.staging.ipynb')
nbformat.write(nb, stage)
stage.replace(TARGET)
print(json.dumps(dict(notebook=str(TARGET), cells=len(nb.cells),
                      executed_code_cells=sum(c.cell_type=='code' for c in nb.cells))))
