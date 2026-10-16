"""Create an allowlisted portable research bundle without environments or credentials."""
from pathlib import Path
from datetime import datetime, timezone
import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile

ENGINE = Path(__file__).absolute().parents[1]
WORKSPACE = ENGINE.parent
NAME = 'solana-sniper-backtest-20261010-wallet50'
STAGE = ENGINE / 'var/dnipro/handoff-20261015-v2' / NAME
EXP = Path('var/dnipro/sniper-inventory-repair-20261015-v1')
REVIEW = Path('var/dnipro/sniper-repaired-review-20261015-v1')
SNAPSHOT = Path('solana-live-indexer/research/sniper_break_even/data/2026-10-10-1600-1700')
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()


def copy_file(source, destination):
    if source.is_symlink():
        raise ValueError('Refuse symlink: ' + str(source))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def copy_tree(source, destination):
    for path in sorted(source.rglob('*')):
        if any(x in ('__pycache__', '.git', '.DS_Store', '.pytest_cache') for x in path.parts):
            continue
        if path.suffix in ('.pyc', '.pyo'):
            continue
        if path.is_file():
            copy_file(path, destination / path.relative_to(source))


def distributions(python):
    script = 'import importlib.metadata as m,json; print(json.dumps(sorted((x.metadata["Name"],x.version) for x in m.distributions())))'
    return json.loads(subprocess.check_output([str(python), '-c', script], text=True))


def category(name):
    if '/src/' in name:
        return 'engine_source'
    if '/tests/' in name:
        return 'tests_and_fixtures'
    if str(SNAPSHOT) in name:
        return 'frozen_replay_input'
    if '/public-rpc/' in name:
        return 'finalized_rpc_evidence'
    if '/clickhouse/' in name:
        return 'clickhouse_query_evidence'
    if '/development-repaired-before/' in name:
        return 'active_native_result_and_full_trace'
    if str(REVIEW) in name:
        return 'charts_decision_logs_and_review_exports'
    if EXP.as_posix() in name or 'slippage_5pct_wallet_50sol' in name:
        return 'repair_audit_and_archived_runs'
    if '/notebooks/' in name:
        return 'notebook_and_review_source'
    if name.startswith('notes/'):
        return 'research_and_strategy_notes'
    if name.startswith('audit/'):
        return 'source_provenance'
    return 'setup_and_documentation'


def build():
    if STAGE.exists():
        raise ValueError('Staging exists; do not overwrite: ' + str(STAGE))
    STAGE.mkdir(parents=True)
    target = STAGE / ENGINE.name
    for folder in ['src', 'tests']:
        copy_tree(ENGINE / folder, target / folder)
    for filename in ['pyproject.toml', 'uv.lock', 'README.md', 'LICENSE',
                     'docs/dnipro-integration.md', 'docs/sniper-window.md',
                     'docs/THIRD_PARTY_NOTICES.md']:
        copy_file(ENGINE / filename, target / filename)
    for filename in ['sniper_repaired_wallet50_20261015.ipynb', 'sniper_repaired_review.py',
                     'sniper_position_review.py', 'build_sniper_repaired_review.py', 'requirements-sniper.txt']:
        copy_file(ENGINE / 'notebooks' / filename, target / 'notebooks' / filename)
    copy_tree(ENGINE / EXP, target / EXP)
    copy_tree(ENGINE / REVIEW, target / REVIEW)
    baseline = Path('var/dnipro/sniper-slippage-wallet-20261015-v1/slippage_5pct_wallet_50sol')
    copy_tree(ENGINE / baseline, target / baseline)
    copy_file(ENGINE / 'var/dnipro/sniper-gap-455328000.json', target / 'var/dnipro/sniper-gap-455328000.json')
    copy_tree(WORKSPACE / SNAPSHOT, STAGE / SNAPSHOT)
    for name in ['README.md', 'verify_bundle.py', 'rerun_backtest.py', 'refresh_notebook.py']:
        copy_file(ENGINE / 'notebooks/handoff' / name, STAGE / name)
    strategy = (Path.home() / 'Downloads/sniper_break_even_v0_corrected.md')
    copy_file(strategy, STAGE / 'notes/sniper_break_even_v0_corrected.md')
    for folder, name in [
        ('Findings', '2026-10-15-repaired-50-sol-sniper-notebook-includes-all-signals-charts-and-decisi-c6fce66f.md'),
        ('Findings', '2026-10-15-verified-wallet-transfers-explain-eight-sniper-replay-inventory-blocke-a2504d98.md'),
        ('Experiments', '2026-10-15-native-sniper-replay-repairs-seven-open-positions-using-verified-trans-60f63a39.md')]:
        copy_file(WORKSPACE / 'research-brain' / folder / name, STAGE / 'notes' / folder / name)
    engine_packages = distributions(ENGINE / '.venv/bin/python')
    notebook_packages = distributions(ENGINE / 'var/dnipro/sniper-notebook-venv/bin/python')
    audit = STAGE / 'audit'
    audit.mkdir()
    (audit / 'installed-engine-versions.json').write_text(json.dumps(engine_packages, indent=2) + '\n')
    (audit / 'installed-notebook-versions.json').write_text(json.dumps(notebook_packages, indent=2) + '\n')
    for label, packages in [('engine', engine_packages), ('notebook', notebook_packages)]:
        lines = ['# Pinned from the working Python 3.13 environments; environments are intentionally separate.']
        for name, version in packages:
            if name.lower() in ('on-chain-backtest-engine', 'pip'):
                continue
            marker = '; sys_platform == "darwin"' if name == 'appnope' else \
                '; sys_platform != "win32"' if name in ('pexpect', 'ptyprocess') else ''
            lines.append(f'{name}=={version}{marker}')
        (STAGE / f'requirements-{label}.txt').write_text('\n'.join(lines) + '\n')
    git = lambda *args: subprocess.check_output(['git', *args], cwd=ENGINE, text=True)
    saved = json.loads((ENGINE / EXP / 'development-repaired-before/result.json').read_text())
    (audit / 'SOURCE_PROVENANCE.json').write_text(json.dumps(dict(
        captured_utc=datetime.now(timezone.utc).isoformat(), head=git('rev-parse', 'HEAD').strip(),
        branch=git('branch', '--show-current').strip(),
        note='Working tree contains necessary uncommitted integration source. Packaged src bytes, not HEAD alone, define the run.',
        git_status=git('status', '--short').splitlines(), implementation=saved['summary']['implementation'],
        notebook_dependencies_are_separate=True), indent=2) + '\n')
    (audit / 'engine-tracked-changes.patch').write_text(git('diff', '--binary', '--', 'src', 'tests', 'docs/dnipro-integration.md'))
    patterns = dict(
        sqd_api_key=re.compile(rb'sqd_data_[A-Za-z0-9_-]{20,}'),
        github_token=re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})'),
        private_key=re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\r?\n[A-Za-z0-9+/=]{40,}'),
        credential_url=re.compile(rb'https?://[^\s/:]{1,100}:[^\s/@]{1,100}@'))
    fixture_urls = {
        'onchain-backtest-engine/tests/unit/adapters/source/test_clickhouse_capability_config.py',
        'onchain-backtest-engine/tests/unit/adapters/source/test_clickhouse_reader.py',
        'onchain-backtest-engine/tests/unit/bootstrap/test_container.py',
        'onchain-backtest-engine/tests/unit/bootstrap/test_preparation_container.py',
    }
    blocked, fixture_exceptions = [], []
    for path in sorted(STAGE.rglob('*')):
        if not path.is_file():
            continue
        if path.name.startswith('.env') or path.suffix in ('.key', '.pem', '.p12', '.pfx'):
            blocked.append((str(path.relative_to(STAGE)), 'credential_file'))
        data = path.read_bytes()
        for kind, pattern in patterns.items():
            if pattern.search(data):
                name = path.relative_to(STAGE).as_posix()
                if kind == 'credential_url' and name in fixture_urls:
                    fixture_exceptions.append(dict(path=name, kind=kind, sha256=sha(path),
                        note='Reviewed fictional credential-URL literals in redaction/validation tests; no runtime configuration.'))
                else:
                    blocked.append((name, kind))
    if blocked:
        print('CREDENTIAL_PATTERN_REVIEW_REQUIRED', json.dumps(blocked), flush=True)
        raise ValueError('Review package patterns before sharing; no secret values printed')
    (audit / 'CREDENTIAL_SCAN.json').write_text(json.dumps(dict(
        blocked_matches=blocked, reviewed_test_fixture_exceptions=fixture_exceptions,
        patterns_checked=list(patterns),
        excluded_by_allowlist=['environment files','SSH keys','API credentials','Git history','Python environments','unrelated research data']), indent=2) + '\n')
    records = []
    for path in sorted(STAGE.rglob('*')):
        if path.is_file():
            name = path.relative_to(STAGE).as_posix()
            records.append(dict(path=name, category=category(name), bytes=path.stat().st_size, sha256=sha(path)))
    with (STAGE / 'CONTENTS.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['path', 'category', 'bytes', 'sha256'])
        writer.writeheader()
        writer.writerows(records)
    files = {row['path']: {k:v for k,v in row.items() if k != 'path'} for row in records}
    files['CONTENTS.csv'] = dict(category='file_inventory', bytes=(STAGE / 'CONTENTS.csv').stat().st_size,
                                sha256=sha(STAGE / 'CONTENTS.csv'))
    manifest = dict(schema='solana-sniper-colleague-bundle/v1', created_utc=datetime.now(timezone.utc).isoformat(),
                    bundle=NAME, snapshot='2026-10-10T16:00:00Z/2026-10-10T17:00:00Z',
                    retained_data_end_utc='2026-10-10T17:16:00Z', scope='all retained inputs and evidence used by this bounded backtest; not full-chain capture',
                    policy=saved['summary']['policy'], realized_net_PnL_SOL=saved['summary']['realized_net_pnl_sol'],
                    unresolved_cost_SOL=saved['summary']['open_cash_cost_sol'], final_portfolio_PnL_SOL=None,
                    active_native_run=(Path(ENGINE.name) / EXP / 'development-repaired-before').as_posix(),
                    files=files)
    (STAGE / 'MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
    subprocess.run([sys.executable, str(STAGE / 'verify_bundle.py')], cwd=STAGE, check=True)
    print(json.dumps(dict(stage=str(STAGE), files=len(files), total_bytes=sum(p.stat().st_size for p in STAGE.rglob('*') if p.is_file()))), flush=True)


if __name__ == '__main__':
    build()
