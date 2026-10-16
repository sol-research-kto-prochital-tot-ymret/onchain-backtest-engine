"""Descriptive comparison of saved native winners and liquidity-shortfall cases.

No trading decisions are changed. Signal/landing features use only their causal
prefix. Post-entry observations are exported in a separate table.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from decimal import Decimal
from datetime import datetime, timezone

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from backtest.plugins.strategies.creator_income import CreatorFeeIncome
from backtest.plugins.strategies.sniper_window import threshold
from backtest.plugins.execution.pump_sniper import profile
from backtest.plugins.protocols.pumpfun.model import (
    PumpCurveStateV1, PumpCurveLifecycle, PumpMode, PumpSellLiquidityPolicy,
    buy_quote, sell_quote,
)

SOL = 'So11111111111111111111111111111111111111112'
LAMPORTS = 10**9
SOURCE = ROOT.parent/'solana-live-indexer/research/sniper_break_even/data/2026-10-10-1600-1700'
RUN = ROOT/'var/dnipro/sniper-window-20261010-slot-traces-virtual-v1'
DEFAULT_OUT = ROOT/'var/dnipro/sniper-window-20261010-group-comparison-v3'
VIRTUAL = PumpSellLiquidityPolicy.VIRTUAL_RESERVE_OUTPUT_WITH_EXPLICIT_SYNTHETIC_SHORTFALL_V1


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_gz(path):
    with gzip.open(path,'rt') as stream:
        return [json.loads(line) for line in stream]


def timestamp(value):
    if isinstance(value,int):
        return value
    return int(datetime.fromisoformat(value.replace('Z','+00:00')).replace(tzinfo=timezone.utc).timestamp())


def curve(birth,row):
    return PumpCurveStateV1(int(row['virtual_base']),int(row['virtual_quote']),int(row['real_base']),int(row['real_quote']),
        int(birth['supply_raw']),PumpCurveLifecycle.ACTIVE, PumpMode.TOKEN_2022 if birth['token_program'].startswith('Tokenz') else PumpMode.NORMAL)


def state_after_buy(state,q,deposit):
    return PumpCurveStateV1(state.virtual_token_reserves_atomic-q,state.virtual_sol_reserves_lamports+deposit,
        state.real_token_reserves_atomic-q,state.real_sol_reserves_lamports+deposit,state.token_total_supply_atomic,state.lifecycle,state.mode)


def quote_sell(state,q,bps,now):
    return sell_quote(state,tokens_in_atomic=q,fee_profile=profile(bps,now),effective_at_unix_s=now,liquidity_policy=VIRTUAL)


def causal_snapshot(tape,boundary,inclusive,birth):
    rows=[r for r in tape if r['boundary']<boundary or (inclusive and r['boundary']==boundary)]
    assert rows
    ledgers=defaultdict(lambda:{'tokens':0,'cost':0,'bought':0,'sold':0,'buy_curve':0,'sell_curve':0,'network':0})
    paid=set()
    fees=CreatorFeeIncome()
    for row in rows:
        w=ledgers[row['trader']]
        network=0
        if row.get('fee_payer')==row['trader'] and row['signature'] not in paid and row.get('network_fee') is not None:
            network=int(row['network_fee']);paid.add(row['signature'])
        charges=int(row['creator_fee'])+int(row['protocol_fee'])
        q=int(row['quote']);tokens=int(row['tokens'])
        w['network']+=network
        if row['side']=='buy':
            w['tokens']+=tokens;w['cost']+=q+charges+network;w['bought']+=tokens;w['buy_curve']+=q
        else:
            w['tokens']-=tokens;w['cost']-=q-charges-network;w['sold']+=tokens;w['sell_curve']+=q
        fees.observe(row['event_uid'],row)
    assert sum(w['tokens'] for w in ledgers.values())==int(birth['initial_virtual_base_raw'])-int(rows[-1]['virtual_base'])
    fees.verify()
    return rows,ledgers,fees


def extract_checkpoint(p,birth,tape,boundary,inclusive,now,fill,deposit,policy,label):
    rows,wallets,income=causal_snapshot(tape,boundary,inclusive,birth)
    last=rows[-1]
    state=curve(birth,last)
    bps=int(last['payload']['fee_basis_points']),int(last['payload']['creator_fee_basis_points'])
    supply=int(birth['supply_raw'])
    quantities=sorted([w['tokens'] for w in wallets.values() if w['tokens']>0],reverse=True)
    positive_sum=sum(quantities)
    dev=wallets.get(p['creator'],{'tokens':0,'cost':0,'bought':0,'sold':0,'network':0})
    anchor=wallets[p['sniper_wallet']]
    if label=='signal':
        own=buy_quote(state,spendable_gross_sol_lamports=p['budget_lamports'],fee_profile=profile(bps,now),effective_at_unix_s=now)
        own_q,own_deposit,own_cash=own.tokens_out_atomic,own.net_curve_sol_lamports,own.gross_sol_spent_lamports+policy['entry_network_fee_lamports']
    else:
        own_q,own_deposit=fill['amount_out_atomic'],deposit
        own_cash=p['entry_cash_lamports']-policy['account_rent_refund_lamports']
    after=state_after_buy(state,own_q,own_deposit)
    needed=(own_cash*(10000+policy['target_return_bps'])+9999)//10000+policy['exit_network_fee_lamports']
    xt=threshold(after,own_q,needed,bps)
    anchor_cost=anchor['cost']-income.income(p['mint'],p['sniper_wallet'])
    xs=threshold(after,anchor['tokens'],anchor_cost,bps) if min(anchor['tokens'],anchor_cost)>0 else None
    completion=Decimal(after.virtual_sol_reserves_lamports*after.virtual_token_reserves_atomic)/(after.virtual_token_reserves_atomic-after.real_token_reserves_atomic)
    valid=xs is not None and xs-xt>=policy['epsilon_lamports'] and xs>after.virtual_sol_reserves_lamports and xt<completion
    profitable=[]; economic_rows=[]
    for wallet,ledger in wallets.items():
        if ledger['tokens']<=0:
            continue
        net_cost=ledger['cost']-income.income(p['mint'],wallet)
        sale=quote_sell(after,ledger['tokens'],bps,now)
        pnl=sale.sol_out_lamports-policy['exit_network_fee_lamports']-net_cost
        can_profit=pnl>=0 and sale.synthetic_shortfall_lamports==0
        if wallet!=p['sniper_wallet'] and can_profit:
            profitable.append(ledger['tokens'])
        economic_rows.append(dict(mint=p['mint'],token=p['symbol'],checkpoint=label,boundary=boundary,wallet=wallet,
            wallet_gmgn='https://gmgn.ai/sol/address/'+wallet,is_creator=wallet==p['creator'],is_anchor=wallet==p['sniper_wallet'],
            observed_tokens_raw=ledger['tokens'],supply_pct=ledger['tokens']/supply*100,
            unrecovered_cash_cost_lamports=ledger['cost'],creator_fee_accrual_lamports=income.income(p['mint'],wallet),
            hypothetical_full_exit_pnl_lamports=pnl,hypothetical_shortfall_lamports=sale.synthetic_shortfall_lamports,
            profitable_after_our_buy=can_profit))
    buys=[r for r in rows if r['side']=='buy'];sells=[r for r in rows if r['side']=='sell']
    buy_flow=sum(int(r['quote']) for r in buys);sell_flow=sum(int(r['quote']) for r in sells)
    peak_x=max([int(birth['initial_virtual_quote_raw'])]+[int(r['virtual_quote']) for r in rows])
    data=dict(
        observed_trades=len(rows),buy_events=len(buys),sell_events=len(sells),unique_buyers=len({r['trader'] for r in buys}),
        positive_inventory_wallets=len(quantities),negative_inventory_wallets=sum(w['tokens']<0 for w in wallets.values()),
        circulating_observed_supply_pct=positive_sum/supply*100,top1_supply_pct=sum(quantities[:1])/supply*100,
        top3_supply_pct=sum(quantities[:3])/supply*100,top3_pct_observed_inventory=sum(quantities[:3])/positive_sum*100 if positive_sum else None,
        inventory_hhi=sum((q/positive_sum)**2 for q in quantities) if positive_sum else None,
        actual_sol_reserve_SOL=state.real_sol_reserves_lamports/LAMPORTS,
        gross_curve_buy_SOL=buy_flow/LAMPORTS,gross_curve_sell_SOL=sell_flow/LAMPORTS,
        sell_to_buy_flow_ratio=sell_flow/buy_flow if buy_flow else None,
        spot_drawdown_from_prior_peak_pct=(1-(state.virtual_sol_reserves_lamports/peak_x)**2)*100,
        launch_creator_bought=dev['bought']>0,
        launch_creator_sold_all=dev['bought']>0 and dev['sold']>0 and dev['tokens']==0,
        launch_creator_holds=dev['tokens']>0,launch_creator_supply_pct=dev['tokens']/supply*100,
        launch_creator_fee_accrual_SOL=income.income(p['mint'],p['creator'])/LAMPORTS,
        anchor_supply_pct=anchor['tokens']/supply*100,anchor_cash_cost_SOL=anchor_cost/LAMPORTS,
        anchor_paid_network_SOL=anchor['network']/LAMPORTS,
        anchor_network_to_net_cost_pct=anchor['network']/anchor_cost*100 if anchor_cost>0 else None,
        other_profitable_wallets=len(profitable),other_profitable_supply_pct=sum(profitable)/supply*100,
        other_profitable_pct_observed_inventory=sum(profitable)/positive_sum*100 if positive_sum else None,
        own_curve_deposit_SOL=own_deposit/LAMPORTS,own_buy_to_real_reserve_ratio=own_deposit/state.real_sol_reserves_lamports if state.real_sol_reserves_lamports else None,
        own_tokens_supply_pct=own_q/supply*100,window_still_valid=valid,
        reserve_margin_SOL=float(xs-xt)/LAMPORTS if xs is not None else None,
        anchor_still_observed_holding=anchor['tokens']>0,
        window_fail_anchor_closed=anchor['tokens']<=0,
        window_fail_margin=xs is not None and xs-xt<policy['epsilon_lamports'],
        window_fail_anchor_already_break_even=xs is not None and xs<=after.virtual_sol_reserves_lamports,
        window_fail_target_past_completion=xt>=completion,
        additional_net_SOL_needed_for_target=float(xt-after.virtual_sol_reserves_lamports)/LAMPORTS,
        additional_target_flow_to_real_reserve_ratio=float(xt-after.virtual_sol_reserves_lamports)/state.real_sol_reserves_lamports if state.real_sol_reserves_lamports else None,
    )
    return data,economic_rows


def analyze(out):
    out.mkdir(parents=True,exist_ok=False)
    run_manifest=json.loads((RUN/'manifest.json').read_text())
    assert run_manifest['complete']
    assert sha(RUN/'result.json')==run_manifest['files']['result.json']
    run=json.loads((RUN/'result.json').read_text())
    manifest=json.loads((SOURCE/'manifest.json').read_text())
    assert sha(SOURCE/'manifest.json')==run['summary']['source_manifest_sha256']
    births_file=SOURCE/'snapshot'/manifest['births']['file']
    assert sha(births_file)==manifest['births']['file_sha256']
    births={b['mint']:b for b in read_gz(births_file)}
    positions={p['mint']:p for p in run['positions'] if p['status']=='CLOSED'}
    assert len(positions)==27
    tape=defaultdict(list);clock={}
    for part in manifest['parts']:
        file=SOURCE/'snapshot'/part['file']
        assert sha(file)==part['file_sha256']
        rows=read_gz(file);assert len(rows)==part['rows']
        if part['name'].endswith('-clock'):
            clock.update({int(r['slot']):timestamp(r['observed_block_time']) for r in rows})
        if part['name'].endswith('-trades'):
            for row in rows:
                if row['mint'] in positions:
                    row['payload']=json.loads(row['decoded_extension_json'] or '{}')
                    row['time']=timestamp(row['block_time'] if row['block_time'] is not None else row['payload']['timestamp'])
                    row['boundary']=(int(row['slot'])<<32)+int(row['tx_idx'])+1
                    tape[row['mint']].append(row)
    for rows in tape.values():
        rows.sort(key=lambda r:(r['slot'],r['tx_idx'],tuple(r['instruction_path']),r['event_uid']))
    reservations={t['correlation_id']:t['boundary_ordinal'] for t in run['ledger'] if t['reason']=='ORDER_RESERVATION'}
    settlements={t['correlation_id']:t for t in run['ledger'] if t['reason']=='ORDER_SETTLEMENT'}
    fills=defaultdict(dict)
    for f in run['fills']:
        fills[f['pool_id']]['BUY' if f['sold_asset_id']==SOL else 'SELL']=f
    features=[];outcomes=[];holders=[]
    for mint,p in positions.items():
        b=births[mint];buy=fills[mint]['BUY'];policy=run['summary']['policy']
        deposit=sum(e['amount_atomic'] for e in settlements[buy['order_id']]['postings'] if e['account_kind']=='VENUE' and e['asset_id']==SOL)
        label='profitable' if p['closed_pnl_lamports']>0 else 'shortfall' if p.get('exit_synthetic_shortfall_lamports',0)>0 else 'other_loss'
        feature=dict(token=p['symbol'],mint=mint,gmgn_url=p['gmgn_url'],group=label,launch_creator=p['creator'],anchor_wallet=p['sniper_wallet'],
            launch_slot=int(b['slot']),signal_slot=p['signal_slot'],entry_slot=p['entry_slot'],entry_delay_slots=p['entry_slot']-p['signal_slot'],
            signal_age_slots=p['signal_slot']-int(b['slot']),entry_age_slots=p['entry_slot']-int(b['slot']),
            signal_age_seconds=p['signal_time']-clock[int(b['slot'])],entry_age_seconds=p['entry_time']-clock[int(b['slot'])],
            entry_cost_SOL=p['entry_cash_lamports']/LAMPORTS)
        for checkpoint,boundary,inclusive,now in [('signal',reservations[buy['order_id']],True,p['signal_time']),('landing',buy['boundary_ordinal'],False,p['entry_time'])]:
            data,economic=extract_checkpoint(p,b,tape[mint],boundary,inclusive,now,buy,deposit,policy,checkpoint)
            feature.update({checkpoint+'_'+key:value for key,value in data.items()})
            holders.extend(dict(group=label,**row) for row in economic)
        features.append(feature)
        historical_after=[r for r in tape[mint] if r['boundary']>=buy['boundary_ordinal']]
        after=dict(token=p['symbol'],mint=mint,gmgn_url=p['gmgn_url'],group=label,
            net_pnl_lamports=p['closed_pnl_lamports'],net_pnl_SOL=p['closed_pnl_lamports']/LAMPORTS,
            exit_synthetic_shortfall_SOL=p.get('exit_synthetic_shortfall_lamports',0)/LAMPORTS,
            holding_seconds=p['exit_time']-p['entry_time'])
        prebuyers={r['trader'] for r in tape[mint] if r['side']=='buy' and r['boundary']<buy['boundary_ordinal']}
        for seconds in [5,30]:
            window=[r for r in historical_after if r['time']<p['entry_time']+seconds]
            after[f'postentry_{seconds}s_net_historical_curve_flow_SOL']=sum(int(r['quote'])*(1 if r['side']=='buy' else -1) for r in window)/LAMPORTS
            after[f'postentry_{seconds}s_buy_events']=sum(r['side']=='buy' for r in window)
            after[f'postentry_{seconds}s_gross_buy_SOL']=sum(int(r['quote']) for r in window if r['side']=='buy')/LAMPORTS
            running=0;highest=0;lowest=0
            for r in window:
                running+=int(r['quote'])*(1 if r['side']=='buy' else -1)
                highest=max(highest,running);lowest=min(lowest,running)
            after[f'postentry_{seconds}s_max_running_net_flow_SOL']=highest/LAMPORTS
            after[f'postentry_{seconds}s_min_running_net_flow_SOL']=lowest/LAMPORTS
            after[f'postentry_{seconds}s_sell_events']=sum(r['side']=='sell' for r in window)
            after[f'postentry_{seconds}s_new_buyers']=len({r['trader'] for r in window if r['side']=='buy'}-prebuyers)
        anchor_sells=[r for r in historical_after if r['trader']==p['sniper_wallet'] and r['side']=='sell']
        if anchor_sells:
            after['anchor_first_sell_after_entry_slots']=anchor_sells[0]['slot']-p['entry_slot']
            after['anchor_first_sell_after_entry_seconds']=anchor_sells[0]['time']-p['entry_time']
        outcomes.append(after)
    frame=pd.DataFrame(features);future=pd.DataFrame(outcomes)
    assert frame['group'].value_counts().to_dict()=={'profitable':13,'shortfall':11,'other_loss':3}
    # Check signal sizes/holdings against recorded native selected signals.
    assert frame['signal_window_still_valid'].all()
    for record in features:
        p=positions[record['mint']]
        assert abs(record['signal_reserve_margin_SOL']-float(Decimal(p['signal_sniper_threshold'])-Decimal(p['signal_our_threshold']))/LAMPORTS)<1e-12
    frame.to_csv(out/'entry_features.csv',index=False)
    future.to_csv(out/'post_entry_outcomes.csv',index=False)
    pd.DataFrame(holders).to_csv(out/'holder_economics.csv',index=False)
    summary=[]
    for feature in frame.select_dtypes(include=['number','bool']).columns:
        if feature.endswith('_slot') or feature in ['launch_slot','signal_slot','entry_slot']:
            continue
        for group in ['profitable','shortfall','other_loss']:
            x=frame[frame['group']==group][feature].dropna().astype(float)
            summary.append(dict(feature=feature,group=group,n=len(x),median=x.median(),q25=x.quantile(.25),q75=x.quantile(.75),minimum=x.min(),maximum=x.max(),mean=x.mean()))
    pd.DataFrame(summary).to_csv(out/'feature_summary.csv',index=False)
    outcome_summary=future.groupby('group').agg({c:'median' for c in future.select_dtypes('number').columns}).reset_index()
    outcome_summary.to_csv(out/'post_entry_summary.csv',index=False)
    receipt=dict(schema='dnipro.position-group-comparison/v1',native_result_sha256=sha(RUN/'result.json'),
        source_manifest_sha256=sha(SOURCE/'manifest.json'),source_input_sha256=run['summary']['source_input_sha256'],
        cohort_start_utc=run['summary']['cohort_start_utc'],cohort_end_utc=run['summary']['cohort_end_utc'],group_counts=frame['group'].value_counts().to_dict(),
        signal_features='all complete historical transactions through signal boundary inclusive',
        landing_features='strict historical prefix before modeled buy landing',
        future_observations='separate post-entry file; unavailable at entry',
        policy_changed=False,all_observed_inventory_sums_reconcile=True,exact_holders=False,
        models_fit=0,thresholds_optimized=0,out_of_sample_validation=False,
        script_sha256=sha(Path(__file__)),csv_hashes={p.name:sha(p) for p in sorted(out.glob('*.csv'))})
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt,indent=2))
    print(frame[['token','group','signal_launch_creator_bought','signal_launch_creator_sold_all','signal_unique_buyers','signal_actual_sol_reserve_SOL','signal_other_profitable_supply_pct','signal_top3_supply_pct','signal_additional_net_SOL_needed_for_target']].to_string(index=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,default=DEFAULT_OUT)
    analyze(parser.parse_args().out)
