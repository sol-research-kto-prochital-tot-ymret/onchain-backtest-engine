"""Audit saved native ledger costs without rerunning or altering the strategy."""
from collections import Counter, defaultdict
from decimal import Decimal
import csv
import gzip
import json
from pathlib import Path
import shutil

from prepare_sniper_oct12_review import ROOT, sha, save, write_csv, order_mapping
from backtest.bootstrap.sniper_window import implementation_identity

OUT = ROOT/'var/dnipro/sniper-oct12-fee-audit-20261016-v1'
SOL = 'So11111111111111111111111111111111111111112'
ORIGINAL = (Path.home() / 'Downloads/sniper_break_even_v0_corrected.md')
RUNS = {
    'without_dev_sell_all': ROOT/'var/dnipro/sniper-oct12-no-dev-flat-20261016-v1/24h-no-dev-flat-wallet10000',
    'with_dev_sell_all': ROOT/'var/dnipro/sniper-oct12-repair-20261016-v1/24h-full-cohort-repaired-v10',
}


def sol(amount):
    return format(Decimal(amount)/10**9, '.9f')


def money(row):
    return {**row, **{k[:-9]+'_sol':sol(v) for k,v in row.items() if k.endswith('_lamports')}}


def audit(label, folder):
    manifest = json.loads((folder/'manifest.json').read_text())
    assert manifest['complete']
    for name, digest in manifest['files'].items():
        assert sha(folder/name)==digest, name
    result = json.loads((folder/'result.json').read_text())
    assert result['summary']['implementation']==implementation_identity()
    assert result['summary']['source_input_sha256']==manifest['source_input_sha256']
    with (folder/'order_audit.csv').open() as stream:
        mapping = {r['order_id']:r for r in order_mapping(result, list(csv.DictReader(stream)))}
    positions = {p['mint']:p for p in result['positions']}
    fills = {r['order_id']:r for r in result['fills']}
    per_order = {}
    for identity, order in mapping.items():
        p = positions[order['mint']]
        per_order[identity] = dict(run=label, order_id=identity, mint=order['mint'], symbol=p['symbol'], gmgn_url=p['gmgn_url'],
            side=order['side'], position_status=p['status'], execution='FILLED' if identity in fills else 'FAILED_OR_REJECTED',
            protocol_fee_lamports=0, creator_fee_lamports=0, network_success_lamports=0, network_failed_lamports=0,
            rent_posted_lamports=0, rent_refunded_lamports=0, curve_net_outflow_lamports=0, cash_delta_lamports=0)
    seen = set()
    for tx in result['ledger']:
        assert tx['transaction_id'] not in seen
        seen.add(tx['transaction_id'])
        current = per_order[tx['correlation_id']]
        totals = defaultdict(int)
        for posting in tx['postings']:
            totals[posting['asset_id']] += posting['amount_atomic']
            if posting['asset_id']!=SOL:
                continue
            amount, kind = posting['amount_atomic'], posting['account_kind']
            if kind in ('PROTOCOL_FEE', 'CREATOR_FEE'):
                assert amount>=0
                current[('protocol' if kind=='PROTOCOL_FEE' else 'creator')+'_fee_lamports'] += amount
            elif kind=='NETWORK_FEE':
                assert amount>=0
                field = 'network_failed_lamports' if posting['account_id']=='network:failed-execution' else 'network_success_lamports'
                current[field] += amount
            elif kind=='PORTFOLIO_LOCKED':
                current['rent_posted_lamports' if amount>0 else 'rent_refunded_lamports'] += abs(amount)
            elif kind=='VENUE':
                current['curve_net_outflow_lamports'] += amount
            elif kind in ('PORTFOLIO_AVAILABLE', 'PORTFOLIO_RESERVED'):
                current['cash_delta_lamports'] += amount
            else:
                assert amount==0, ('unexpected SOL flow', kind, amount)
        assert all(v==0 for v in totals.values()), tx['transaction_id']
    records = []
    for identity, row in per_order.items():
        row['total_fee_lamports'] = row['protocol_fee_lamports']+row['creator_fee_lamports']+row['network_success_lamports']+row['network_failed_lamports']
        row['rent_net_locked_lamports'] = row['rent_posted_lamports']-row['rent_refunded_lamports']
        assert row['cash_delta_lamports']+row['curve_net_outflow_lamports']+row['total_fee_lamports']+row['rent_net_locked_lamports']==0, identity
        if identity in fills:
            q = abs(row['curve_net_outflow_lamports'])
            # Integer quote amounts can use a buy fee basis up to two lamports
            # above the adjusted curve input when independent fee ceil exceeds budget.
            bases = [q+i for i in (range(3) if row['side']=='BUY' else [0])]
            row['fee_consistent_with_95_protocol_30_creator_bps'] = any(
                (b*95+9999)//10000==row['protocol_fee_lamports'] and
                (b*30+9999)//10000==row['creator_fee_lamports'] for b in bases)
        else:
            assert row['protocol_fee_lamports']==row['creator_fee_lamports']==row['rent_posted_lamports']==0
            row['fee_consistent_with_95_protocol_30_creator_bps'] = ''
        records.append(row)
    totals = {k:sum(r[k] for r in records) for k in records[0] if k.endswith('_lamports')}
    s, policy = result['summary'], result['summary']['policy']
    successes = sum(identity in fills for identity in per_order)
    paid_failures = sum(r['network_failed_lamports']>0 for r in records)
    assert totals['network_success_lamports']==successes*policy['entry_network_fee_lamports']
    assert policy['entry_network_fee_lamports']==policy['exit_network_fee_lamports']==105000
    assert totals['network_failed_lamports']==paid_failures*105000
    assert totals['rent_net_locked_lamports']==s['open_positions']*policy['account_rent_lamports']
    assert totals['cash_delta_lamports']==s['realized_net_pnl_lamports']-s['open_cash_cost_lamports']
    assert sum(r['network_failed_lamports'] for r in records if r['side']=='BUY')==s['failed_entry_cost_lamports']
    assert all(r['fee_consistent_with_95_protocol_30_creator_bps'] for r in records if r['order_id'] in fills)
    anchor_rows = [p for p in result['positions'] if 'signal_slot' in p]
    for p in anchor_rows:
        assert p['sniper_cost_lamports']==p['sniper_trading_cash_cost_lamports']-p['sniper_creator_fee_income_lamports']
    summary = money(dict(run=label, **totals, filled_transactions=successes, paid_failed_transactions=paid_failures,
        failed_buy_transactions=sum(r['network_failed_lamports']>0 and r['side']=='BUY' for r in records),
        failed_sell_transactions=sum(r['network_failed_lamports']>0 and r['side']=='SELL' for r in records),
        buy_fills=s['filled_entries'], sell_fills=s['closed_positions'], open_positions=s['open_positions'],
        tips_lamports=0, rejected_orders=s['engine_summary']['rejected_order_count'],
        realized_net_pnl_lamports=s['realized_net_pnl_lamports'], open_committed_cost_lamports=s['open_cash_cost_lamports'],
        signals_with_creator_income=sum(p['sniper_creator_fee_income_lamports']>0 for p in anchor_rows),
        creator_anchor_signals=sum(bool(p['sniper_is_launch_creator']) for p in anchor_rows)))
    groups = []
    for status in ('CLOSED', 'OPEN', 'ENTRY_FAILED', 'ENTRY_REJECTED'):
        selected = [r for r in records if r['position_status']==status]
        groups.append(money(dict(run=label, position_status=status,
            **{k:sum(r[k] for r in selected) for k in totals}, tokens=len({r['mint'] for r in selected}))))
    return summary, records, groups, dict(run=label, native_folder=str(folder),
        native_manifest_sha256=sha(folder/'manifest.json'), native_result_sha256=sha(folder/'result.json'),
        implementation=s['implementation'], source_input_sha256=s['source_input_sha256'], policy=policy,
        ledger_transactions=len(seen), orders=len(mapping), all_cash_flows_reconciled=True,
        fee_rate_consistency_checked_fills=successes)


def buyback_proofs():
    root = ROOT/'var/dnipro/sniper-oct12-repair-20261016-v1'
    delivery = json.loads((root/'delivery-manifest.json').read_text())
    proofs = []
    with gzip.open(root/'original-token-rows.jsonl.gz', 'rt') as stream:
        for line in stream:
            row = json.loads(line)
            if row['kind']!='trade' or row['side']!='buy' or not row.get('buyback_fee'):
                continue
            relative = 'public-rpc/tx-'+row['signature']+'.json'
            path = root/relative
            if not path.exists():
                continue
            assert sha(path)==delivery['files'][relative]['sha256']
            receipt = json.loads(path.with_suffix('.receipt.json').read_text())
            assert receipt['response_sha256']==sha(path)
            assert receipt['request']['params'][1]['commitment']=='finalized'
            tx = json.loads(path.read_text())['result']
            assert tx['meta']['err'] is None
            instructions = tx['transaction']['message']['instructions'] + [ix for g in tx['meta'].get('innerInstructions', []) for ix in g['instructions']]
            transfers = [ix['parsed']['info'] for ix in instructions if ix.get('program')=='system'
                         and ix.get('parsed', {}).get('type')=='transfer'
                         and ix['parsed']['info']['source']==row['trader']]
            observed = sum(r['lamports'] for r in transfers)
            expected = row['quote']+row['protocol_fee']+row['creator_fee']
            if observed!=expected:
                continue
            target = OUT/'buyback-evidence'/path.name
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(path, target);shutil.copy2(path.with_suffix('.receipt.json'), target.with_suffix('.receipt.json'))
            proofs.append(dict(mint=row['mint'], token_gmgn='https://gmgn.ai/sol/token/'+row['mint'],
                trader=row['trader'], trader_gmgn='https://gmgn.ai/sol/address/'+row['trader'], signature=row['signature'],
                curve_lamports=row['quote'], protocol_fee_lamports=row['protocol_fee'], creator_fee_lamports=row['creator_fee'],
                buyback_routed_part_lamports=row['buyback_fee'], parsed_outgoing_system_transfers_lamports=observed,
                curve_plus_protocol_plus_creator_lamports=expected,
                buyback_is_not_an_additional_fee_in_this_example=True,
                evidence_sha256=sha(target), evidence_file=str(target.relative_to(OUT)), transfers=transfers))
            if len(proofs)==2:
                break
    assert len(proofs)==2
    save(OUT/'buyback-evidence.json', dict(complete=True, scope='Two observed ordinary Pump.fun buys; not a universal venue fee schedule', proofs=proofs))
    return proofs


def main():
    assert not OUT.exists(), 'Keep completed audits immutable'
    OUT.mkdir(parents=True)
    shutil.copy2(ORIGINAL, OUT/'original-sniper-break-even-v0.md')
    sources = ['docs/sniper-window.md', 'src/backtest/plugins/strategies/sniper_window.py',
        'src/backtest/plugins/strategies/creator_income.py', 'src/backtest/plugins/execution/pump_sniper.py',
        'src/backtest/plugins/execution/pump_conditional.py', 'src/backtest/plugins/protocols/pumpfun/model.py']
    for name in sources:
        target = OUT/'implementation'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT/name, target)
    summaries, orders, groups, checks = [], [], [], []
    for label, folder in RUNS.items():
        summary, details, buckets, validation = audit(label, folder)
        summaries.append(summary);orders.extend(money(r) for r in details);groups.extend(buckets);checks.append(validation)
    write_csv(OUT/'fee_totals.csv', summaries)
    write_csv(OUT/'fees_by_order.csv', orders)
    write_csv(OUT/'fees_by_position_status.csv', groups)
    proofs = buyback_proofs()
    shutil.copy2(Path(__file__), OUT/Path(__file__).name)
    save(OUT/'audit.json', dict(complete=True, summaries=summaries, validation=checks,
        original_strategy_path=str(ORIGINAL), original_strategy_sha256=sha(ORIGINAL),
        buyback_routing_proof_examples=len(proofs), arithmetic='integer lamports; fixed nine-decimal SOL display',
        excluded_claims='Not a zero-fee counterfactual replay; not independently measured live execution costs',
        source_hashes={name:sha(ROOT/name) for name in sources}))
    print(json.dumps(summaries, indent=2), flush=True)


if __name__=='__main__':
    main()
