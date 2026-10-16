"""Bind KUSO's recorded round trip to finalized RPC and the saved control replay."""
from collections import defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).absolute().parents[1]
OUT = ROOT / 'var/dnipro/kuso-tracked-buyer-20261016-v1'
CONTROL = ROOT / 'var/dnipro/sniper-dev-flat-20261015-v1/control'


def main():
    rows = json.loads((OUT/'tracked-wallet-trades.json').read_text())
    p = json.loads((OUT/'position.json').read_text())
    mint, wallet = p['mint'], p['sniper_wallet']
    assert [r['side'] for r in rows] == ['buy', 'sell']
    assert rows[0]['tokens'] == rows[1]['tokens'] == p['sniper_tokens']
    recorded_cost = net_receipt = actual_cash = 0
    proofs, timeline = [], []
    for row in rows:
        path = OUT/'rpc'/(row['side']+'.json')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        receipt = json.loads(path.with_suffix('.receipt.json').read_text())
        assert receipt['response_sha256'] == digest
        assert receipt['request']['params'][0] == row['signature']
        assert receipt['request']['params'][1]['commitment'] == 'finalized'
        tx = json.loads(path.read_text())['result']
        assert tx['meta']['err'] is None and tx['slot'] == row['slot']
        keys = [a['pubkey'] if isinstance(a, dict) else a for a in tx['transaction']['message']['accountKeys']]
        index = keys.index(wallet)
        delta = tx['meta']['postBalances'][index]-tx['meta']['preBalances'][index]
        before = sum(int(b['uiTokenAmount']['amount']) for b in tx['meta']['preTokenBalances'] if b['mint']==mint and b.get('owner')==wallet)
        after = sum(int(b['uiTokenAmount']['amount']) for b in tx['meta']['postTokenBalances'] if b['mint']==mint and b.get('owner')==wallet)
        assert after-before == row['tokens']*(1 if row['side']=='buy' else -1)
        actual_cash += delta
        network = row['network_fee'] if row['fee_payer']==wallet else 0
        if row['side']=='buy':
            recorded_cost += row['quote']+row['protocol_fee']+row['creator_fee']+network
        else:
            net_receipt += row['quote']-row['protocol_fee']-row['creator_fee']-network
        event = dict(event='Tracked wallet '+row['side'], evidence_kind='observed finalized transaction',
            slot=row['slot'], transaction_index=row['tx_idx'], signature=row['signature'],
            tokens=row['tokens']/10**6, curve_SOL=row['quote']/10**9, wallet_SOL_delta=delta/10**9,
            wallet_gmgn='https://gmgn.ai/sol/address/'+wallet, token_gmgn=p['gmgn_url'])
        timeline.append(event)
        proofs.append(dict(side=row['side'], file=str(path), sha256=digest, successful_finalized=True,
                           token_balance_before=before, token_balance_after=after, wallet_delta_lamports=delta))
    result = json.loads((CONTROL/'result.json').read_text())
    for fill in result['fills']:
        if fill['pool_id'] != mint:continue
        buy = fill['sold_asset_id'].startswith('So111')
        timeline.append(dict(event='Our simulated '+('buy' if buy else 'sell'), evidence_kind='conditional replay; assumed next-slot landing',
            slot=fill['boundary_ordinal'] >> 32, transaction_index=(fill['boundary_ordinal'] & (2**32-1))-1,
            signature='', tokens=(fill['amount_out_atomic'] if buy else fill['amount_in_atomic'])/10**6,
            curve_SOL='', wallet_SOL_delta='', wallet_gmgn='', token_gmgn=p['gmgn_url']))
    trace = [r for r in csv.DictReader((CONTROL/'strategy_trace.csv').open()) if r['mint']==mint]
    signal = next(r for r in trace if r['action']=='ENTRY_SIGNAL')
    timeline.append(dict(event='Our entry decision', evidence_kind='native strategy trace', slot=int(signal['slot']),
        transaction_index=(int(signal['boundary_ordinal']) & (2**32-1))-1, signature='', tokens='', curve_SOL='',
        wallet_SOL_delta='', wallet_gmgn='', token_gmgn=p['gmgn_url']))
    timeline.sort(key=lambda r:(r['slot'],r['transaction_index']))
    with (OUT/'timeline.csv').open('w', newline='') as stream:
        writer=csv.DictWriter(stream, fieldnames=list(timeline[0]));writer.writeheader();writer.writerows(timeline)
    with (OUT/'strategy_trace.csv').open('w', newline='') as stream:
        writer=csv.DictWriter(stream, fieldnames=list(trace[0]));writer.writeheader();writer.writerows(trace)
    after_sell = next(r for r in trace if r['action']=='EXIT_EVALUATION' and int(r['slot'])==rows[1]['slot']
                      and int(r['wallet_tokens'])==0)
    summary = dict(complete=True, mint=mint, token_gmgn=p['gmgn_url'], wallet=wallet,
        wallet_gmgn='https://gmgn.ai/sol/address/'+wallet,
        control='October 10, 2026, 16:00–17:00 UTC original +5% control',
        buy_slot=rows[0]['slot'], sell_slot=rows[1]['slot'], holding_slots=rows[1]['slot']-rows[0]['slot'],
        sold_fraction=1, token_quantity=rows[0]['tokens']/10**6,
        recorded_swap_and_network_cost_SOL=recorded_cost/10**9, recorded_swap_and_network_receipt_SOL=net_receipt/10**9,
        recorded_swap_and_network_cashflow_SOL=(net_receipt-recorded_cost)/10**9,
        actual_wallet_transaction_cash_delta_SOL=actual_cash/10**9,
        actual_cash_scope='Includes 0.0073+0.0011 SOL additional transfers and 0.002039280 SOL account rent; rent is not assumed permanently lost',
        replay_reference_remaining_tokens=int(after_sell['wallet_tokens']),
        replay_reference_cash_loss_SOL=int(after_sell['wallet_adjusted_cost'])/10**9,
        our_entry_slot=p['entry_slot'], our_exit_slot=p['exit_slot'], our_exit_reason=p['reason'], our_pnl_SOL=p['closed_pnl_lamports']/10**9,
        our_holding_seconds=p['exit_time']-p['entry_time'],
        limitations=['Both observed transactions share the same second-resolution block timestamp; no millisecond latency claim',
                     'Next-slot start for our fill is a replay assumption, not an observed onchain order',
                     'No private wallet motive or general behavior is inferred from one round trip'],
        proof=proofs, control_manifest_sha256=hashlib.sha256((CONTROL/'manifest.json').read_bytes()).hexdigest(),
        observed_at=datetime.now(timezone.utc).isoformat())
    (OUT/'verification.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps(summary, indent=2))


if __name__=='__main__':main()
