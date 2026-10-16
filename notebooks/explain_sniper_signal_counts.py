"""Compare token membership and terminal reasons in preserved paired runs."""
from collections import Counter
from pathlib import Path
import csv
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'var/dnipro/sniper-window-redesign-v1'
OUT = ROOT / 'var/dnipro/sniper-signal-counts-20261014-v1'
OUT.mkdir(parents=True, exist_ok=True)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_csv(name, rows):
    with (OUT / name).open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    summary, tokens, inputs = [], [], {}
    for cohort in ['development', 'additional', 'validation']:
        pair = {}
        for version in ['before', 'after']:
            name = f'{cohort}-conditional_fraction-{version}'
            folder = SOURCE / name
            manifest = json.loads((folder / 'manifest.json').read_text())
            assert manifest['complete']
            assert sha(folder / 'result.json') == manifest['files']['result.json']
            pair[version] = json.loads((folder / 'result.json').read_text())
            inputs[name] = sha(folder / 'result.json')
        before, after = pair['before'], pair['after']
        assert before['summary']['source_input_sha256'] == after['summary']['source_input_sha256']
        assert before['summary']['cohort_launches'] == after['summary']['cohort_launches']
        indexed = {v: {p['mint']: p for p in r['positions']} for v, r in pair.items()}
        sets = {v: {mint for mint, p in ps.items() if 'signal_slot' in p}
                for v, ps in indexed.items()}
        lost = sets['before'] - sets['after']
        reasons = Counter(indexed['after'][mint]['reason'] for mint in lost)
        assert len(sets['before']) - len(lost) + len(sets['after']-sets['before']) == len(sets['after'])
        summary.append(dict(cohort=cohort, start_utc=before['summary']['cohort_start_utc'],
                            before_signals=len(sets['before']), after_signals=len(sets['after']),
                            before_fills=before['summary']['filled_entries'],
                            after_fills=after['summary']['filled_entries'],
                            original_signals_lost=len(lost), shared_signals=len(sets['before'] & sets['after']),
                            revised_only_signals=len(sets['after'] - sets['before']),
                            lost_terminal_reason_counts=json.dumps(dict(sorted(reasons.items())))))
        for mint in sorted(sets['before'] | sets['after']):
            b, a = indexed['before'][mint], indexed['after'][mint]
            tokens.append(dict(cohort=cohort, token=a['symbol'], mint=mint, gmgn_url=a['gmgn_url'],
                               before_signal=mint in sets['before'], after_signal=mint in sets['after'],
                               before_status=b['status'], before_reason=b['reason'],
                               after_status=a['status'], after_terminal_reason=a['reason'],
                               before_signal_slot=b.get('signal_slot'), after_signal_slot=a.get('signal_slot')))
    write_csv('signal_counts.csv', summary)
    write_csv('signal_membership.csv', tokens)
    receipt = dict(complete=True, source_result_hashes=inputs, token_rows=len(tokens),
                   original_signals=sum(r['before_signals'] for r in summary),
                   revised_signals=sum(r['after_signals'] for r in summary),
                   interpretation='Terminal reason grouping, not isolated causal ablation or original-signal-time rejection trace.',
                   output_hashes={p.name: sha(p) for p in OUT.glob('*.csv')})
    (OUT / 'receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
