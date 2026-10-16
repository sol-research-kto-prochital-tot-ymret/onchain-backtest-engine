"""Freeze bounded chart context for the saved full-day run; never replay trades."""
from collections import defaultdict
import csv
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from backtest.domain.hashing import domain_digest

RUN = ROOT / 'var/dnipro/sniper-oct12-no-dev-flat-20261016-v1'
NATIVE = RUN / '24h-no-dev-flat-wallet10000'
PROJECTION = Path('/Volumes/KINGSTON/solana-research/sniper-oct12-projection-20261015-v1')
OUT = ROOT / 'var/dnipro/sniper-oct12-no-dev-flat-review-20261016-v1'


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(2**20), b''):
            h.update(block)
    return h.hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False)+'\n')


def read_csv(path):
    with path.open() as stream:
        return list(csv.DictReader(stream))


def write_csv(path, values):
    keys = list(dict.fromkeys(k for r in values for k in r))
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows(values)


def order_mapping(result, audits):
    """Match the recorded domain hash, including failed orders with no fill."""
    component = domain_digest('dnipro.configured-sniper-window.v1', result['summary']['policy']).hex
    positions = [p for p in result['positions'] if 'signal_slot' in p]
    fills = {f['order_id']: f for f in result['fills']}
    records = []
    for row in audits:
        if row['record_type'] not in ('ORDER_ACCEPTED_AND_RESERVED', 'ORDER_REJECTED'):
            continue
        ordinal = int(row['boundary_ordinal'])
        matches = []
        for p in positions:
            for side, attempts in [('BUY', [0]), ('SELL', range(1, p['exit_attempts']+1))]:
                if side == 'BUY' and p['signal_slot'] != ordinal >> 32:
                    continue
                if side == 'SELL' and (not p.get('entry_slot') or not p['entry_slot'] <= ordinal >> 32 <= p.get('exit_slot', 10**12)):
                    continue
                for attempt in attempts:
                    digest = domain_digest('dnipro.sniper-order.v1', dict(mint=p['mint'], side=side,
                        boundary=ordinal, attempt=attempt, component=component)).hex
                    if digest == row['order_id']:
                        matches.append(dict(order_id=digest, mint=p['mint'], side=side, attempt=attempt,
                            decision_boundary=str(ordinal), decision_slot=ordinal >> 32,
                            eligible_boundary=row.get('eligible_boundary_ordinal', '')))
        assert len(matches) == 1, (row, matches)
        match = matches[0]
        if match['order_id'] in fills:
            fill = fills[match['order_id']]
            assert fill['pool_id'] == match['mint']
            assert (fill['sold_asset_id'].startswith('So111')) == (match['side'] == 'BUY')
        records.append(match)
    assert len({r['order_id'] for r in records}) == len(records)
    assert {t['correlation_id'] for t in result['ledger']} <= {r['order_id'] for r in records}
    return records


def main():
    import pyarrow.parquet as pq
    began = time.monotonic()
    assert not (OUT / 'context-receipt.json').exists(), 'Preserve completed notebook inputs'
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((NATIVE / 'manifest.json').read_text())
    assert manifest['complete']
    for filename, digest in manifest['files'].items():
        assert sha(NATIVE / filename) == digest
    result = json.loads((NATIVE / 'result.json').read_text())
    summary = result['summary']
    assert summary['policy'].get('require_creator_flat_before_entry', 0) == 0
    assert summary['policy']['bankroll_lamports'] == 10_000 * 10**9
    wanted = {p['mint']: p for p in result['positions'] if 'signal_slot' in p}
    assert len(wanted) == 887
    if not (OUT / 'native').exists():
        shutil.copytree(NATIVE, OUT / 'native')
    orders = order_mapping(result, read_csv(NATIVE / 'order_audit.csv'))
    write_csv(OUT / 'order_mapping.csv', orders)
    print('ORDERS_MAPPED', len(orders), flush=True)
    projection = json.loads((PROJECTION / 'manifest.json').read_text())
    assert sha(PROJECTION / 'manifest.json') == summary['source_manifest_sha256']
    assert json.loads((PROJECTION / 'COMMITTED').read_text())['manifest_sha256'] == sha(PROJECTION / 'manifest.json')
    for filename, key in [('births.json', 'births_sha256'), ('clocks.json', 'clocks_sha256')]:
        assert sha(PROJECTION / filename) == projection[key]
    births = {p['mint']: p for p in json.loads((PROJECTION / 'births.json').read_text()) if p['mint'] in wanted}
    repaired_births = json.loads((RUN / 'births.json').read_text())
    for mint in births.keys() & repaired_births.keys():
        births[mint] = repaired_births[mint]
    deadlines = {m: p.get('exit_time', p.get('entry_time', p['signal_time']) + (360 if p['status']=='OPEN' else 20)) + 30
                 for m, p in wanted.items()}
    for mint, birth in births.items():
        birth['chart_end_unix'] = deadlines[mint]
        birth['token_decimals'] = 6
    save(OUT / 'births.json', births)
    clocks = {int(s): int(t) for s, t in json.loads((PROJECTION / 'clocks.json').read_text())}
    clock_path = RUN / 'clickhouse/midnight-observed-slot-clocks-v3.jsonl.gz'
    assert sha(clock_path) == json.loads(clock_path.with_suffix('.receipt.json').read_text())['file_sha256']
    with gzip.open(clock_path, 'rt') as stream:
        for line in stream:
            row = json.loads(line)
            clocks[int(row['slot'])] = int(row['observed_time'])
    with gzip.open(OUT / 'clocks.json.gz', 'wt') as stream:
        json.dump(clocks, stream, separators=(',', ':'))
    tapes, seen = defaultdict(list), set()
    keep = ('slot', 'tx_idx', 'instruction_path', 'event_uid', 'block_time', 'kind', 'mint',
            'signature', 'trader', 'side', 'quote', 'tokens', 'virtual_quote', 'virtual_base',
            'real_quote', 'real_base', 'protocol_fee', 'creator_fee', 'network_fee', 'fee_payer', 'migration_stage')

    def retain(row):
        mint = row['mint']
        if mint not in wanted or row['kind'] not in ('trade', 'lifecycle') or row['block_time'] > deadlines[mint]:
            return
        identity = row['signature'], tuple(row['instruction_path']), row['kind']
        assert identity not in seen, identity
        seen.add(identity)
        tapes[mint].append({key: row[key] for key in keep if key in row})

    for i, part in enumerate(projection['parts']):
        path = PROJECTION / part['file']
        assert sha(path) == part['sha256'], part['file']
        table = pq.read_table(path, columns=['row_json'], filters=[('mint', 'in', list(wanted))])
        for raw in table.column(0).to_pylist():
            retain(json.loads(raw))
        if (i+1) % 40 == 0:
            print('CONTEXT', i+1, '/', len(projection['parts']), 'facts', len(seen), flush=True)
    added = 0
    for name in ('recovered-trades.json', 'tail-trades.json'):
        for row in json.loads((RUN / name).read_text()):
            before = len(seen)
            retain(row)
            added += len(seen)-before
    (OUT / 'token-context').mkdir(exist_ok=True)
    for mint, values in tapes.items():
        values.sort(key=lambda r: (r['slot'], r['tx_idx'], tuple(r['instruction_path']), r['kind']))
        with gzip.open(OUT / 'token-context' / (mint+'.json.gz'), 'wt') as stream:
            json.dump(values, stream, separators=(',', ':'))
    assert wanted.keys() <= tapes.keys()
    files = {str(p.relative_to(OUT)): sha(p) for p in OUT.rglob('*') if p.is_file()}
    save(OUT / 'context-receipt.json', dict(complete=True, native_manifest_sha256=sha(NATIVE / 'manifest.json'),
        source_input_sha256=summary['source_input_sha256'], projection_manifest_sha256=sha(PROJECTION / 'manifest.json'),
        positions=len(wanted), orders=len(orders), chart_facts=len(seen), recovered_chart_facts=added,
        seconds=round(time.monotonic()-began, 2), files=files,
        chart_scope='Launch through exit plus 30 seconds; open positions through entry plus 390 seconds; failed entries through signal plus 50 seconds',
        limits='Retained trade subset; chart windows do not certify missing transfers, complete holders, or full-chain coverage'))
    print('CONTEXT_COMPLETE', len(wanted), 'tokens', len(seen), 'facts', flush=True)


if __name__ == '__main__':
    main()
