"""Publish notebook usage evidence and verify the KINGSTON delivery copy."""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys

from prepare_sniper_oct12_review import ROOT, OUT, save, sha

VAULT = ROOT.parent/'research-brain'
NOTE = 'Findings/2026-10-16-reviewing-the-october-12-full-day-sniper-backtest-in-jupyter-ec1412a6.md'
TARGET = ROOT/'notebooks/sniper_oct12_no_dev_flat_20261016.ipynb'
BACKUP = Path('/Volumes/KINGSTON/solana-research')/OUT.name


def main():
    qa = json.loads((OUT/'notebook-qa.json').read_text())
    assert qa['complete'] and not qa['errors'] and qa['saved_plotly_figures']==7
    assert sha(TARGET)==qa['notebook_sha256']==sha(OUT/TARGET.name)
    assert qa['realized_pnl_lamports']==-109602394368
    assert qa['open_committed_cost_lamports']==152319150420
    for name in ('sniper_oct12_day_review.py', 'prepare_sniper_oct12_review.py',
                 'build_sniper_oct12_day_notebook.py', 'sniper_interactive_review.py'):
        assert sha(ROOT/'notebooks'/name)==sha(OUT/'notebook-source'/name)
    note = VAULT/NOTE
    fields = {line.split(':',1)[0]:json.loads(line.split(':',1)[1])
              for line in note.read_text().split('---',2)[1].strip().splitlines()}
    fields.update(status='observed', observed_at=datetime.now(timezone.utc).isoformat(), sources=[
        '../../onchain-backtest-engine/notebooks/'+TARGET.name,
        '../../onchain-backtest-engine/var/dnipro/'+OUT.name+'/notebook-qa.json',
        '../../onchain-backtest-engine/var/dnipro/'+OUT.name+'/context-receipt.json',
        '../../onchain-backtest-engine/var/dnipro/'+OUT.name+'/csv/all_positions.csv'])
    body = '''# Reviewing the October 12 full day sniper backtest in Jupyter

The executed [October 12 notebook](../../onchain-backtest-engine/notebooks/sniper_oct12_no_dev_flat_20261016.ipynb) reviews the full UTC day with the developer-sell-all entry rule disabled and a 10,000 SOL wallet. It contains the realized PnL curve, cash and committed-cost curves, complete outcome tables, a searchable token browser, and six saved token examples. Interactive charts have embedded PNG alternatives for viewers without Plotly support.

## Recorded results and coverage

All 887 signals are included: 251 profitable tokens, 318 losing, 80 unresolved and 238 submitted failed entries. There were no capital risk rejections. Realized net remains −109.602394368 SOL, with 152.319150420 SOL committed to open positions. Open cost is not classified as a realized loss, so final portfolio PnL remains unknown.

Every token has native signal metadata and an order audit. The chart markers cover 649 buys and 569 sells; all 1,475 orders are mapped by their exact saved domain hashes, including failed orders without fills. Detailed per-event evaluations were retained for 78 tokens. The other charts explicitly label unavailable evaluations. Decision markers use a historical reference price and are distinct from average simulated fill prices.

The bounded context contains 93,324 retained trade/lifecycle facts for all 887 tokens. Closed histories extend through exit plus 30 seconds; open histories retain approximately 6½ minutes after entry, and failed entries retain 50 seconds after their signals. Inventory/count panels use observed swaps, native repricing and the bounded verified transfer overlay; they are not exact holder coverage and stop at conditional blockers. Seventy-five open cases need more seller acquisition/transfer evidence; five have counterfactual transfer conflicts.

## Open and run

Select **Python 3.13.12 (Sniper research)** in VS Code or IntelliJ. Its interpreter is `onchain-backtest-engine/var/dnipro/sniper-notebook-venv/bin/python`. Saved tables and examples can be inspected without running a kernel. Run All loads frozen local artifacts and does not execute the strategy again.

The token browser filters outcomes and searches a symbol or mint. `review.show(TOKEN)` displays native signal thresholds, tracked buyer, developer links, sizes, fees, entry/exit markers and order history. The manual token cell includes this call. All explorer links use GMGN.

From the engine root, rebuild the executed notebook with:

```bash
var/dnipro/sniper-notebook-venv/bin/python notebooks/build_sniper_oct12_day_notebook.py
```

The builder requires the completed context folder `var/dnipro/sniper-oct12-no-dev-flat-review-20261016-v1`. Its `csv` directory holds all positions split by outcome, exact fill markers, realized PnL events, cash events and committed-cost events. `native` preserves the original fills, ledger, complete order audit and retained detailed logs. KINGSTON receives a hash-verified copy of the notebook, context, native artifacts and source helpers.

## Verification

All nine code cells executed without errors. Seven Plotly figures and seven PNG alternatives are saved. Integer cash, committed cost and realized PnL reconcile to the native result; each ledger transaction conserves every asset. All buy/sell marker counts agree with position status. There were 77,574 checks of displayed tracked-buyer inventory against native evaluations, within one atomic token unit of the float display series. Exact native boundaries remain strings in browser hover data to avoid JavaScript precision loss. Visual checks covered profitable, losing, both unresolved categories, failed-entry and detailed-log examples.

[[Experiments/2026-10-15-october-12-full-day-sniper-backtest-without-the-developer-sellout-gate-a55fd5a2]]
'''
    note.write_text('---\n'+'\n'.join(f'{key}: {json.dumps(value, ensure_ascii=False)}' for key,value in fields.items())+'\n---\n\n'+body)
    publication = subprocess.run([sys.executable, 'scripts/brain.py', 'publish', NOTE,
        '--message', 'Document executed full-day no-dev-sell-all notebook'], cwd=VAULT,
        text=True, capture_output=True)
    save(OUT/'note-publication.json', dict(note=str(note), note_sha256=sha(note),
        returncode=publication.returncode, stdout=publication.stdout, stderr=publication.stderr))
    assert publication.returncode==0, publication.stderr
    shutil.copy2(Path(__file__), OUT/'notebook-source'/Path(__file__).name)
    files = {str(p.relative_to(OUT)):dict(sha256=sha(p), bytes=p.stat().st_size)
             for p in sorted(OUT.rglob('*')) if p.is_file()
             and p.name not in ('delivery-manifest.json','COMMITTED') and '__pycache__' not in p.parts}
    save(OUT/'delivery-manifest.json', dict(complete=True, files=files))
    save(OUT/'COMMITTED', dict(delivery_manifest_sha256=sha(OUT/'delivery-manifest.json')))
    stage = BACKUP.with_name(BACKUP.name+'.partial')
    assert BACKUP.parent.is_dir() and not BACKUP.exists() and not stage.exists()
    subprocess.run(['/usr/bin/rsync', '-a', str(OUT)+'/', str(stage)+'/'], check=True)
    assert sha(stage/'delivery-manifest.json')==sha(OUT/'delivery-manifest.json')
    for name, facts in files.items():
        assert sha(stage/name)==facts['sha256'], name
    assert sha(stage/'COMMITTED')==sha(OUT/'COMMITTED')
    stage.rename(BACKUP)
    print(json.dumps(dict(complete=True, notebook=str(TARGET), backup=str(BACKUP), note=str(note),
                          verified_delivery_files=len(files)), indent=2))


if __name__=='__main__':
    main()
