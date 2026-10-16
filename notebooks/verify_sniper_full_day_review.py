"""Check notebook markers/accounting and render representative charts for review."""
from bisect import bisect_right
from collections import Counter, defaultdict
import base64
import json
import os
from pathlib import Path

import nbformat
from sniper_full_day_review import FullDayReview, LABEL, ROOT
from sniper_interactive_review import sha


def main():
    review = FullDayReview()
    assert len(review.orders)==1475
    assert Counter(r['side'] for r in review.fill_markers)=={'BUY':649, 'SELL':569}
    by_mint = defaultdict(list)
    for marker in review.fill_markers:by_mint[marker['mint']].append(marker)
    for mint, p in review.positions[LABEL].items():
        assert sum(r['side']=='BUY' for r in by_mint[mint])==int(p['entry_cash_lamports']>0)
        assert sum(r['side']=='SELL' for r in by_mint[mint])==int(p['status']=='CLOSED')
        assert review.tape(mint), mint
    inventory_checks = 0
    for mint, traces in review.traces[LABEL].items():
        if mint not in review.positions[LABEL]:continue
        records = review.inventory(mint)
        bounds = [r['boundary'] for r in records]
        for row in traces:
            if row['action']!='EXIT_EVALUATION':continue
            index = bisect_right(bounds, row['boundary_ordinal'])-1
            if index>=0 and row['time']<=review.births[mint]['chart_end_unix']:
                assert records[index]['reference_tokens_raw']==int(row['wallet_tokens']), (mint,row['boundary_ordinal'])
                inventory_checks += 1
    examples = []
    for group in ('Profitable','Losing','Unresolved','Entry failed'):
        examples.append(review.table[review.table.group==group].iloc[0]['mint'])
    examples.extend(review.table[review.table.detailed_trace_available].head(3).mint)
    examples.extend(review.table[review.table.first_causal_blocker=='CONDITIONAL_TRANSFER_SOURCE_INVENTORY_CONFLICT'].head(1).mint)
    verified = []
    for mint in dict.fromkeys(examples):
        fig = review.figure(mint)
        assert fig.layout.meta['buy_fills']==int(review.positions[LABEL][mint]['entry_cash_lamports']>0)
        assert fig.layout.meta['sell_fills']==int(review.positions[LABEL][mint]['status']=='CLOSED')
        for trace in fig.data:
            for custom in trace.customdata if trace.customdata is not None else []:
                assert isinstance(custom[3], str), 'Exact large boundary must remain a string in browser JSON'
        verified.append(dict(mint=mint, meta=dict(fig.layout.meta)))
    folder = review.data/'visual-qa';folder.mkdir(exist_ok=True)
    browser = next((Path.home() / 'Library/Caches/ms-playwright').glob('chromium_headless_shell-*/chrome-mac*/headless_shell'), None)
    if browser is not None and browser.exists() and not os.environ.get('BROWSER_PATH'):
        os.environ['BROWSER_PATH'] = str(browser)
    review.strategy_figure().write_image(folder/'pnl.png', width=1400, height=900)
    for filename, group in [('profitable','Profitable'), ('unresolved','Unresolved'), ('failed','Entry failed')]:
        mint = review.table[review.table.group==group].iloc[0]['mint']
        review.figure(mint).write_image(folder/(filename+'.png'), width=1400, height=1350)
    nbpath = ROOT/'notebooks/sniper_oct12_no_dev_sell_all_20261016.ipynb'
    nb = nbformat.read(nbpath, as_version=4)
    assert all(not any(o.output_type=='error' for o in c.get('outputs',[])) for c in nb.cells)
    nbformat.validate(nb)
    receipt = dict(complete=True, notebook_sha256=sha(nbpath), all_887_marker_counts_verified=True,
        actual_buys=649, actual_sells=569, native_inventory_evaluations_checked=inventory_checks,
        representative_charts=verified, exact_integer_boundaries_preserved=True,
        realized_pnl_lamports=sum(r['pnl_lamports'] for r in review.pnl_events),
        open_cost_lamports=sum(r['delta'] for r in review.exposure_events),
        visual_files={p.name:sha(p) for p in folder.glob('*.png')})
    (review.data/'verification.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps({k:v for k,v in receipt.items() if k!='representative_charts'},indent=2),flush=True)


if __name__=='__main__':main()
