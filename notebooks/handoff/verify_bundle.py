"""Verify a portable backtest bundle with the Python standard library only."""
from collections import Counter, defaultdict
from pathlib import Path
import csv
import gzip
import hashlib
import json

ROOT = Path(__file__).resolve().parent


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def verify():
    manifest = json.loads((ROOT / 'MANIFEST.json').read_text())
    for name, record in manifest['files'].items():
        p = (ROOT / name).resolve()
        if not p.is_relative_to(ROOT) or p.is_symlink() or not p.is_file():
            raise ValueError('Invalid bundle member: ' + name)
        assert p.stat().st_size == record['bytes'] and sha(p) == record['sha256'], name
    engine = ROOT / 'onchain-backtest-engine'
    experiment = engine / 'var/dnipro/sniper-inventory-repair-20261015-v1'
    result = json.loads((experiment / 'development-repaired-before/result.json').read_text())
    assert result == json.loads((experiment / 'repaired-v3/result.json').read_text())
    for path in engine.glob('var/dnipro/**/manifest.json'):
        native = json.loads(path.read_text())
        if 'files' not in native or not native.get('complete'):
            continue
        for name, digest in native['files'].items():
            assert sha(path.parent / name) == digest, (str(path), name)
    source = ROOT / 'solana-live-indexer/research/sniper_break_even/data/2026-10-10-1600-1700'
    frozen = json.loads((source / 'manifest.json').read_text())
    for part in [frozen['births'], *frozen['parts'], *frozen['metadata']]:
        p = source / 'snapshot' / part['file']
        assert sha(p) == part['file_sha256'], part['name']
        with gzip.open(p, 'rt') as stream:
            assert sum(1 for _ in stream) == part['rows'], part['name']
        sql = (source / 'snapshot' / (part['name'] + '.sql')).read_text().removesuffix('\n')
        assert hashlib.sha256(sql.encode()).hexdigest() == part['query_sha256']
    for transaction in result['ledger']:
        totals = defaultdict(int)
        for posting in transaction['postings']:
            totals[posting['asset_id']] += posting['amount_atomic']
        assert all(v == 0 for v in totals.values())
    selected = [p for p in result['positions'] if 'signal_slot' in p]
    counts = Counter(p['status'] for p in selected)
    assert counts == {'CLOSED': 28, 'OPEN': 1, 'ENTRY_FAILED': 9}, counts
    summary = result['summary']
    assert sum(p['closed_pnl_lamports'] for p in selected if p['status'] == 'CLOSED') \
        - sum(p['failed_execution_cost_lamports'] for p in selected if p['status'] == 'ENTRY_FAILED') \
        == summary['realized_net_pnl_lamports'] == -3_289_255_885
    assert summary['open_cash_cost_lamports'] == 1_207_495_000
    with (experiment / 'development-repaired-before/strategy_trace.csv').open() as stream:
        trace = list(csv.DictReader(stream))
    assert len(trace) == 27_894
    assert {r['mint'] for r in trace if r['action'] == 'ENTRY_SIGNAL'} == {p['mint'] for p in selected}
    review = engine / 'var/dnipro/sniper-repaired-review-20261015-v1'
    assert len(list((review / 'positions').glob('*.png'))) == 38
    with (review / 'native_fill_markers.csv').open() as stream:
        markers = Counter(r['side'] for r in csv.DictReader(stream))
    assert markers == {'BUY': 29, 'SELL': 28}, markers
    code = hashlib.sha256()
    for path in sorted((engine / 'src/backtest').rglob('*.py')):
        code.update(str(path.relative_to(engine / 'src/backtest')).encode() + b'\0' + path.read_bytes() + b'\0')
    assert code.hexdigest() == summary['implementation']['engine_source_sha256']
    print(json.dumps(dict(verified_files=len(manifest['files']), signals=len(selected),
                           launches=frozen['births']['rows'], retained_trades=summary['retained_input_trade_events'],
                           trace_rows=len(trace), buy_markers=markers['BUY'], sell_markers=markers['SELL'],
                           realized_net_PnL_SOL=summary['realized_net_pnl_sol'], unresolved_positions=1,
                           exact_engine_source_verified=True), indent=2))


if __name__ == '__main__':
    verify()
