"""Publish the completed comparison and verify an independent drive copy."""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys

from sniper_oct12_no_dev_flat import BASE, CONTROL, ENGINE, OUT, RUN_NAME, verify_result
from sniper_carbon_day import save
from backtest.adapters.source.sniper_silver import file_hash
from backtest.bootstrap.sniper_window import implementation_identity

VAULT = ENGINE.parent / 'research-brain'
BACKUP = Path('/Volumes/KINGSTON/solana-research') / OUT.name


def main():
    validation = json.loads((OUT / 'validation.json').read_text())
    analysis = json.loads((OUT / 'opportunity-analysis.json').read_text())
    assert validation['complete'] and analysis['complete']
    assert not analysis['same_signal_economic_differences']
    result = verify_result(OUT / RUN_NAME)
    summary = result['summary']
    assert implementation_identity() == summary['implementation']
    assert summary['source_input_sha256'] == verify_result(CONTROL)['summary']['source_input_sha256']
    # Preserve the exact core and the smaller runner/adapter provenance separately.
    source = OUT / 'implementation-source'
    if not source.exists():
        shutil.copytree(BASE / 'implementation-source-final', source)
    for path in (source / 'src/backtest').rglob('*.py'):
        relative = path.relative_to(source)
        assert file_hash(path) == file_hash(ENGINE / relative), str(relative)
    for name in ('analyze_sniper_oct12_gate_toggle.py', 'finish_sniper_oct12_gate_toggle.py'):
        shutil.copy2(ENGINE / 'notebooks' / name, OUT / 'replay-source' / name)
    note_receipt = OUT / 'note-publication.json'
    if not note_receipt.exists():
        title = 'October 12 full day sniper backtest without the developer sellout gate'
        completed = subprocess.run([sys.executable, 'scripts/brain.py', 'new', '--kind', 'experiment',
            '--title', title, '--project', 'solana', '--author', 'Codex'], cwd=VAULT,
            text=True, capture_output=True, check=True)
        relative = completed.stdout.strip()
        note = VAULT / relative
        front = note.read_text().split('---', 2)[1]
        fields = {line.split(':', 1)[0]: json.loads(line.split(':', 1)[1])
                  for line in front.strip().splitlines()}
        sources = [f'../../onchain-backtest-engine/var/dnipro/{OUT.name}/{name}' for name in (
            'comparison.csv', 'positions.csv', 'opportunity_groups.csv', 'signal_comparison.csv',
            'unresolved_positions.csv', 'validation.json', 'input-receipt.json', 'run-receipt.json',
            'opportunity-analysis.json')]
        fields.update(status='observed', observed_at=datetime.now(timezone.utc).isoformat(), sources=sources)
        table = '\n'.join('| {run} | {signals} | {filled} | {closed} | {open} | {realized_net_pnl_sol} | {open_committed_cost_sol} |'.format(**p)
                          for p in validation['comparison'])
        groups = '\n'.join('| {group} | {tokens} | {dev_flat_realized_net_sol} | {no_dev_flat_realized_net_sol} | {no_dev_flat_open} |'.format(**p)
                           for p in analysis['groups'])
        blockers = '; '.join(f'{key}: {value}' for key, value in analysis['open_first_causal_blockers'].items()) or 'none'
        body = f'''# {title}

Disabling the developer sellout entry rule produces {summary['realized_net_pnl_sol']} SOL realized net and {summary['open_positions']} unresolved positions in the native fork replay. The experiment covers October 12, 2026, 00:00–24:00 UTC with a continuous 10,000 SOL wallet and available follow-up through October 13 00:15.

## Results

| Entry rule | Signals | Filled | Closed | Open | Realized net SOL | Open committed cost SOL |
|---|---:|---:|---:|---:|---:|---:|
{table}

Realized net includes closed-trade outcomes and submitted failed-entry fees. It excludes open committed capital. Open positions are unresolved exposure; they are not silently valued at zero or counted as realized losses. Final portfolio PnL remains unknown while any positions remain open. `comparison.csv` also separates wins, losses, submitted entry failures and risk rejections.

## Unchanged strategy and execution

The first two slots remain the entry window. Entry and exit delay stay one slot, own take profit stays +5%, timeout stays 300 seconds, and entry/exit slippage stays 5%. Original quadratic sizing, creator-fee accounting, real-reserve-capped exits, counterpart sell fractions and fixed external-transfer amounts remain unchanged. Reference-buyer profitability exit stays disabled. The developer may still hold tokens when an entry qualifies in the new run.

The only policy change is disabling `require_creator_flat_before_entry`. Both runs use the identical repaired input hash `{summary['source_input_sha256']}` and engine source `{summary['implementation']['engine_source_sha256']}`.

## Changed opportunity set

| Signal group | Tokens | Developer gate realized net SOL | No gate realized net SOL | No gate open |
|---|---:|---:|---:|---:|
{groups}

The comparison separates newly admitted signals from tokens whose entry timing, tracked buyer or size changes. Common signals retain identical economic outcomes. Groups partition the union of signals and reconcile exactly to both reported realized totals and open costs. The samples overlap and are not independent validation periods.

## Unresolved cases and coverage

First causal blockers among still-open positions: {blockers}. Of the open cases, {analysis['open_tokens_outside_original_repair']} tokens lie outside the original 78-token targeted repair. Final lifecycle reason strings can overwrite earlier blockers; `unresolved_positions.csv` preserves the first causal reason. Warnings after a completed exit do not relabel closed PnL.

An unproven historical seller balance means the retained acquisition trades or wallet transfers do not establish enough inventory to replay that sell. It does not prove a particular number of missing swaps. A transfer inventory conflict instead means our simulated intervention leaves a sender unable to deliver the fixed historical amount; resolving it requires a separate counterparty behavior assumption. Neither category is recorded as a realized loss.

The input preserves 169 recovered swaps within the day, two follow-up swaps after midnight and 161 verified external transfers. That repair was bounded to the original developer-gated unresolved histories. Removing the gate exposes additional histories that have not received the same targeted audit. This is a same-input experiment, not a global completeness certificate or proof of exact live execution. Unknown counterparty reactions, fee-tier history, within-slot landing rank and configured network fees remain model limits.

## Reproduction and files

From `onchain-backtest-engine`, the retained `.venv/bin/python` runs `notebooks/sniper_oct12_no_dev_flat.py prepare`, then `run`, with a fresh output path to preserve immutable results. `notebooks/analyze_sniper_oct12_gate_toggle.py` audits the completed opportunity groups. Policy loading normalizes disabled flags that are omitted from serialized defaults; the full dataclass comparison proves the single change.

`positions.csv`, `profitable_positions.csv`, `losing_positions.csv`, `unresolved_positions.csv`, `failed_entries.csv` and `risk_rejected_entries.csv` separate outcomes and contain GMGN links. Native fills, ledger, execution failures, order audit and conditional errors remain in `{RUN_NAME}`. All native output manifests, integer cash reconciliation, creator-fee conservation and zero synthetic sell funding pass. Exact engine and runner sources are retained alongside the experiment. Original caches, the repaired control, production ClickHouse and collection services remain unchanged by this experiment.

[[Experiments/2026-10-15-october-12-native-backtest-input-repair-recovers-169-missing-swaps-4aedbd78]]
'''
        note.write_text('---\n'+'\n'.join(f'{key}: {json.dumps(value, ensure_ascii=False)}' for key, value in fields.items())+'\n---\n\n'+body)
        publication = subprocess.run([sys.executable, 'scripts/brain.py', 'publish', relative,
            '--message', 'Record October 12 full-day sniper replay without developer sellout gate'],
            cwd=VAULT, text=True, capture_output=True)
        save(note_receipt, dict(note=str(note), relative=relative, returncode=publication.returncode,
             stdout=publication.stdout, stderr=publication.stderr, note_sha256=file_hash(note)))
        assert publication.returncode == 0, publication.stderr
        print('NOTE_PUBLISHED', relative, flush=True)
    assert json.loads(note_receipt.read_text())['returncode'] == 0
    # Bind the immutable deliverables; progress/log files are operational metadata.
    excluded = {'delivery-manifest.json', 'COMMITTED', 'job-status.json', 'run.log', 'finalization.log'}
    files = {}
    for path in sorted(OUT.rglob('*')):
        if path.is_file() and path.name not in excluded and '__pycache__' not in path.parts and '.partial' not in path.name:
            files[str(path.relative_to(OUT))] = dict(sha256=file_hash(path), bytes=path.stat().st_size)
    save(OUT / 'delivery-manifest.json', dict(complete=True, scope='Native same-input developer gate experiment; chain cache retained separately', files=files))
    save(OUT / 'COMMITTED', dict(delivery_manifest_sha256=file_hash(OUT / 'delivery-manifest.json')))
    assert BACKUP.parent.is_dir() and not BACKUP.exists()
    stage = BACKUP.with_name(BACKUP.name+'.partial')
    assert not stage.exists()
    subprocess.run(['/usr/bin/rsync', '-a', str(OUT)+'/', str(stage)+'/'], check=True)
    assert file_hash(stage / 'delivery-manifest.json') == file_hash(OUT / 'delivery-manifest.json')
    for name, facts in files.items():
        assert file_hash(stage / name) == facts['sha256'], name
    assert file_hash(stage / 'COMMITTED') == file_hash(OUT / 'COMMITTED')
    stage.rename(BACKUP)
    print('DELIVERY_COMPLETE', len(files), 'verified files', str(BACKUP), flush=True)


if __name__ == '__main__':
    main()
