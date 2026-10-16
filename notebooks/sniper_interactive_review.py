"""Interactive, read-only review of native SniperWindow artifacts.

Prices, actual fills, liquidation estimates and realized cash accounting keep
separate labels. This module never places orders or replays a strategy.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
import csv
import gzip
import hashlib
import html
import json
import sys

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from IPython.display import HTML, Markdown, display

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from backtest.plugins.execution.pump_sniper import profile
from backtest.plugins.protocols.pumpfun.model import (
    PumpCurveStateV1, PumpCurveLifecycle, PumpMode, PumpQuoteError, sell_quote,
)

SOL = 'So11111111111111111111111111111111111111112'
LAMPORTS = 10**9
GOOD, BAD, HIST, REPLAY = '#16804a', '#cf3344', '#7562a5', '#db8c22'
EXPERIMENT = ROOT / 'var/dnipro/sniper-dev-flat-20261015-v1'
SOURCE = ROOT.parent / 'solana-live-indexer/research/sniper_break_even/data/2026-10-10-1600-1700'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def csv_rows(path):
    with Path(path).open() as stream:
        return list(csv.DictReader(stream))


def utc(timestamp):
    return datetime.fromtimestamp(int(timestamp), timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def boundary(slot, transaction_index):
    return (int(slot) << 32) + int(transaction_index) + 1


def compact_steps(rows, field):
    """Drop unchanged evaluations; preserve every step transition and endpoint."""
    if len(rows)<3:return rows
    selected=[rows[0]]
    for previous,current in zip(rows,rows[1:]):
        if current.get(field)!=previous.get(field):selected.append(current)
    if selected[-1] is not rows[-1]:selected.append(rows[-1])
    return selected


def outcome(position):
    if position['status'] == 'CLOSED':
        return 'Profitable' if position['closed_pnl_lamports'] > 0 else 'Losing' if position['closed_pnl_lamports'] < 0 else 'Breakeven'
    return {'OPEN':'Unresolved', 'ENTRY_FAILED':'Entry failed', 'ENTRY_REJECTED':'Entry rejected',
            'NO_SIGNAL':'No entry signal'}.get(position['status'], position['status'])


class NativeReview:
    def __init__(self, runs=None, source=SOURCE, export=None):
        self.folders = runs or {'control':EXPERIMENT/'control', 'dev-flat':EXPERIMENT/'dev-flat'}
        self.source = Path(source)
        self.export = Path(export or EXPERIMENT/'interactive-review')
        self.export.mkdir(parents=True, exist_ok=True)
        self.results, self.positions, self.traces, self.errors, self.audits = {}, {}, {}, {}, {}
        self.repriced, self.transfers = {}, {}
        for label, folder in self.folders.items():
            manifest = json.loads((folder/'manifest.json').read_text())
            assert manifest['complete'], label
            for filename,digest in manifest['files'].items():
                assert sha(folder/filename) == digest, (label,filename)
            result = json.loads((folder/'result.json').read_text())
            assert result['summary']['cash_reconciliation_verified']
            self.results[label] = result
            self.positions[label] = {p['mint']:p for p in result['positions']}
        self.wanted = {p['mint'] for result in self.results.values() for p in result['positions'] if 'signal_slot' in p}
        for label, folder in self.folders.items():
            self.traces[label] = defaultdict(list)
            with (folder/'strategy_trace.csv').open() as stream:
                for row in csv.DictReader(stream):
                    if row['mint'] in self.wanted:
                        for key in ('slot','time','boundary_ordinal'):
                            row[key] = int(row[key])
                        self.traces[label][row['mint']].append(row)
            self.errors[label] = csv_rows(folder/'conditional_errors.csv')
            self.audits[label] = csv_rows(folder/'order_audit.csv')
            self.repriced[label] = csv_rows(folder/'conditional_repriced.csv')
            self.transfers[label] = csv_rows(folder/'inventory_transfers_applied.csv')
        self.tapes, self.births, self.clock = defaultdict(list), {}, {}
        manifest = json.loads((self.source/'manifest.json').read_text())
        self.source_sha256 = sha(self.source/'manifest.json')
        for result in self.results.values():
            assert result['summary']['source_manifest_sha256'] == self.source_sha256
        launch_path = self.source/'snapshot/launches.jsonl.gz'
        assert sha(launch_path) == manifest['births']['file_sha256']
        with gzip.open(launch_path,'rt') as stream:
            self.births = {row['mint']:row for line in stream if (row:=json.loads(line))['mint'] in self.wanted}
        for part in manifest['parts']:
            if not part['name'].endswith(('-clock','-trades')):
                continue
            path = self.source/'snapshot'/part['file']
            assert sha(path) == part['file_sha256']
            count = 0
            with gzip.open(path,'rt') as stream:
                for line in stream:
                    count += 1; row = json.loads(line)
                    if part['name'].endswith('-clock'):
                        self.clock[int(row['slot'])] = int(datetime.fromisoformat(row['observed_block_time']).replace(tzinfo=timezone.utc).timestamp())
                    elif row['mint'] in self.wanted:
                        row['boundary'] = boundary(row['slot'],row['tx_idx'])
                        row['payload'] = json.loads(row['decoded_extension_json'])
                        self.tapes[row['mint']].append(row)
            assert count == part['rows']
        for tape in self.tapes.values():
            tape.sort(key=lambda r:(r['slot'],r['tx_idx'],tuple(r['instruction_path']),r['event_uid']))
        self.fill_markers, self.cash_events, self.pnl_events, position_rows = [], [], [], []
        self.reference_cache, self.inventory_cache = {}, {}
        for label,result in self.results.items():
            mapping = {f['order_id']:f['pool_id'] for f in result['fills']}
            # Failed-entry order IDs are directly recorded in strategy traces.
            for mint, traces in self.traces[label].items():
                for row in traces:
                    if row.get('order_id'):
                        mapping[row['order_id']] = mint
            for transaction in result['ledger']:
                assert transaction['correlation_id'] in mapping
                delta = sum(p['amount_atomic'] for p in transaction['postings'] if p['asset_id']==SOL and p['account_kind'] in ('PORTFOLIO_AVAILABLE','PORTFOLIO_RESERVED'))
                self.cash_events.append(dict(run=label,mint=mapping[transaction['correlation_id']],
                    boundary=transaction['boundary_ordinal'],slot=transaction['boundary_ordinal']>>32,
                    cash_delta_lamports=delta,reason=transaction['reason']))
            expected = result['summary']['realized_net_pnl_lamports']-result['summary']['open_cash_cost_lamports']
            assert sum(r['cash_delta_lamports'] for r in self.cash_events if r['run']==label)==expected
            for fill in result['fills']:
                mint = fill['pool_id']; side = 'BUY' if fill['sold_asset_id']==SOL else 'SELL'
                tx = next(t for t in result['ledger'] if t['correlation_id']==fill['order_id'] and t['reason']=='ORDER_SETTLEMENT')
                curve = sum(p['amount_atomic'] for p in tx['postings'] if p['account_kind']=='VENUE' and p['asset_id']==SOL)
                tokens = fill['amount_out_atomic'] if side=='BUY' else fill['amount_in_atomic']
                assert (curve>0) == (side=='BUY')
                self.fill_markers.append(dict(run=label,mint=mint,side=side,boundary=fill['boundary_ordinal'],
                    slot=fill['boundary_ordinal']>>32,order_id=fill['order_id'],tokens_raw=tokens,
                    curve_sol_lamports=abs(curve),average_curve_price_sol=abs(curve)/tokens/1000))
            for p in result['positions']:
                if p['mint'] in self.wanted:
                    position_rows.append(dict(run=label,mint=p['mint'],token=p['symbol'].strip(),
                        group=outcome(p),status=p['status'],reason=p['reason'],gmgn_url=p['gmgn_url'],
                        creator_gmgn='https://gmgn.ai/sol/address/'+p['creator'],
                        reference_gmgn=p.get('sniper_gmgn_url'),signal_slot=p.get('signal_slot'),
                        entry_slot=p.get('entry_slot'),exit_slot=p.get('exit_slot'),
                        entry_sol=p['entry_cash_lamports']/LAMPORTS,
                        closed_pnl_sol=p['closed_pnl_lamports']/LAMPORTS if p['status']=='CLOSED' else None,
                        open_cost_sol=p['entry_cash_lamports']/LAMPORTS if p['status']=='OPEN' else 0,
                        creator_status_at_signal=p.get('creator_status_at_signal')))
                if p['status']=='CLOSED':
                    sell = next(f for f in result['fills'] if f['pool_id']==p['mint'] and f['sold_asset_id']!=SOL)
                    self.pnl_events.append(dict(run=label,mint=p['mint'],boundary=sell['boundary_ordinal'],
                        slot=p['exit_slot'],pnl_lamports=p['closed_pnl_lamports'],event='Position closed'))
                elif not p['entry_cash_lamports'] and p['failed_execution_cost_lamports']:
                    self.pnl_events.extend(dict(run=label,mint=p['mint'],boundary=r['boundary'],slot=r['slot'],
                        pnl_lamports=r['cash_delta_lamports'],event='Failed entry fee')
                        for r in self.cash_events if r['run']==label and r['mint']==p['mint'] and r['cash_delta_lamports']<0)
            assert sum(e['pnl_lamports'] for e in self.pnl_events if e['run']==label)==result['summary']['realized_net_pnl_lamports']
        self.table = pd.DataFrame(position_rows).sort_values(['run','signal_slot','mint'],na_position='last')
        self.table.to_csv(self.export/'positions.csv',index=False)
        pd.DataFrame(self.fill_markers).to_csv(self.export/'native_fill_markers.csv',index=False)
        pd.DataFrame(self.pnl_events).to_csv(self.export/'pnl_events.csv',index=False)
        self.summary().to_csv(self.export/'comparison.csv',index=False)

    def summary(self):
        rows=[]
        for label,result in self.results.items():
            s=result['summary'];closed=[p for p in result['positions'] if p['status']=='CLOSED']
            rows.append(dict(run=label,signals=s['signals'],filled=s['filled_entries'],closed=s['closed_positions'],
                profitable=sum(p['closed_pnl_lamports']>0 for p in closed),losing=sum(p['closed_pnl_lamports']<0 for p in closed),
                unresolved=s['open_positions'],failed_entries=sum(p['status']=='ENTRY_FAILED' for p in result['positions']),
                risk_rejections=sum(p['status']=='ENTRY_REJECTED' for p in result['positions']),
                realized_pnl_sol=s['realized_net_pnl_sol'],open_cost_sol=s['open_cash_cost_sol'],
                zero_recovery_stress_sol=s['zero_recovery_stress_pnl_sol']))
        return pd.DataFrame(rows)

    def reference_quotes(self,label,mint):
        key=label,mint
        if key in self.reference_cache:return self.reference_cache[key]
        p=self.positions[label][mint];birth=self.births[mint];policy=self.results[label]['summary']['policy']
        tape=self.tapes[mint];bounds=[r['boundary'] for r in tape];records=[]
        for row in self.traces[label][mint]:
            if row['action'] not in ('EXIT_EVALUATION','EXIT_PREFLIGHT_REJECTED') or not row.get('wallet_tokens'):
                continue
            tokens=int(row['wallet_tokens']);cost=int(row['wallet_adjusted_cost']);net=None;quality='AVAILABLE'
            index=bisect_right(bounds,row['boundary_ordinal'])-1
            if tokens<0 or index<0:quality='UNPROVEN_INVENTORY_OR_PROFILE'
            elif tokens==0:net=-cost;quality='REALIZED_CASHFLOW'
            else:
                try:
                    vsol,vtokens=int(row['virtual_sol']),int(row['virtual_tokens'])
                    initial_vsol=int(birth['initial_virtual_quote_raw']);initial_real=int(birth.get('initial_real_quote_raw') or 0)
                    offset=int(birth['initial_virtual_base_raw'])-int(birth['initial_real_base_raw'])
                    state=PumpCurveStateV1(vtokens,vsol,vtokens-offset,vsol-initial_vsol+initial_real,
                        int(birth['supply_raw']),PumpCurveLifecycle.ACTIVE,
                        PumpMode.TOKEN_2022 if str(birth['token_program']).startswith('Tokenz') else PumpMode.NORMAL)
                    payload=tape[index]['payload'];fees=profile((int(payload['fee_basis_points']),int(payload['creator_fee_basis_points'])),row['time'])
                    quote=sell_quote(state,tokens_in_atomic=tokens,fee_profile=fees,effective_at_unix_s=row['time'])
                    net=quote.sol_out_lamports-policy['exit_network_fee_lamports']-cost
                except (PumpQuoteError,ValueError) as error:
                    quality=error.code.value if isinstance(error,PumpQuoteError) else 'INVALID_STATE'
                    if cost<=0:net=-cost;quality='RECOVERED_CASH_LOWER_BOUND'
            records.append(dict(run=label,mint=mint,boundary=row['boundary_ordinal'],slot=row['slot'],
                time=row['time'],tokens_raw=tokens,cost_lamports=cost,net_pnl_lamports=net,quality=quality))
        self.reference_cache[key]=records
        return records

    def inventory(self,label,mint):
        key=label,mint
        if key in self.inventory_cache:return self.inventory_cache[key]
        p=self.positions[label][mint];anchor=p.get('sniper_wallet') or self.positions['control'].get(mint,{}).get('sniper_wallet')
        override=defaultdict(deque)
        for row in self.repriced[label]:
            if row['mint']==mint:
                override[(int(row['boundary_ordinal']),row['trader'],row['side'],int(row['historical_tokens']),int(row['historical_curve_sol']))].append(int(row['replayed_tokens']))
        groups=defaultdict(list);moves=defaultdict(list)
        for raw in self.tapes[mint]:groups[raw['boundary']].append(raw)
        for move in self.transfers[label]:
            if move['mint']==mint:moves[boundary(move['slot'],move['tx_idx'])].append(move)
        blocked=min((int(e['boundary_ordinal']) for e in self.errors[label] if e['mint']==mint),default=10**30)
        wallets=defaultdict(int);records=[]
        for ordinal in sorted(groups.keys()|moves.keys()):
            if ordinal>=blocked:break
            for move in moves[ordinal]:
                wallets[move['source_wallet']]-=int(move['tokens']);wallets[move['destination_wallet']]+=int(move['tokens'])
            for raw in groups[ordinal]:
                k=(ordinal,raw['trader'],raw['side'],int(raw['tokens']),int(raw['quote']))
                tokens=override[k].popleft() if override[k] else int(raw['tokens'])
                wallets[raw['trader']]+=tokens*(1 if raw['side']=='buy' else -1)
            records.append(dict(run=label,mint=mint,boundary=ordinal,slot=ordinal>>32,
                dev_tokens_raw=wallets[p['creator']],reference_tokens_raw=wallets[anchor] if anchor else None,
                positive_observed_wallets=sum(n>0 for n in wallets.values()),
                negative_unproven_wallets=sum(n<0 for n in wallets.values())))
        self.inventory_cache[key]=records
        return records

    def strategy_figure(self):
        fig=make_subplots(rows=3,cols=1,shared_xaxes=True,vertical_spacing=.09,
            subplot_titles=['Cumulative realized PnL — unresolved positions excluded',
                            'Cash available plus reserved — excludes tokens and posted rent',
                            'Open committed cash cost — exposure, not an assumed loss'])
        start=min(self.clock);last=max(self.clock)
        for label,result in self.results.items():
            events=sorted((e for e in self.pnl_events if e['run']==label),key=lambda e:(e['boundary'],e['mint']))
            total=0;points=[(start,0)]
            for e in events:total+=e['pnl_lamports'];points.append((e['slot'],total/LAMPORTS))
            points.append((last,total/LAMPORTS))
            fig.add_trace(go.Scatter(x=[x-start for x,y in points],y=[y for x,y in points],mode='lines',
                line_shape='hv',name=label,legendgroup=label),row=1,col=1)
            running=result['summary']['policy']['bankroll_lamports'];cash=[(start,running/LAMPORTS)]
            for e in sorted((e for e in self.cash_events if e['run']==label),key=lambda e:e['boundary']):
                running+=e['cash_delta_lamports'];cash.append((e['slot'],running/LAMPORTS))
            cash.append((last,running/LAMPORTS))
            fig.add_trace(go.Scatter(x=[x-start for x,y in cash],y=[y for x,y in cash],mode='lines',
                line_shape='hv',name=label+' cash',legendgroup=label,showlegend=False),row=2,col=1)
            deltas=[]
            for p in result['positions']:
                if p['entry_cash_lamports']:
                    deltas.append((p['entry_slot'],p['entry_cash_lamports']))
                    if p['status']=='CLOSED':deltas.append((p['exit_slot'],-p['entry_cash_lamports']))
            total=0;exposure=[(start,0)]
            for slot,amount in sorted(deltas):total+=amount;exposure.append((slot,total/LAMPORTS))
            exposure.append((last,total/LAMPORTS))
            fig.add_trace(go.Scatter(x=[x-start for x,y in exposure],y=[y for x,y in exposure],mode='lines',
                line_shape='hv',name=label+' exposure',legendgroup=label,showlegend=False),row=3,col=1)
        fig.add_hline(y=0,line_color='#aaa',row=1,col=1)
        fig.update_yaxes(title_text='SOL')
        fig.update_xaxes(title_text=f'Slots since {start}',row=3,col=1)
        fig.update_layout(height=850,template='plotly_white',hovermode='x unified',title='Original +5% strategy: control vs developer flat')
        return fig

    def figure(self,label,mint):
        p=self.positions[label][mint];birth=self.births[mint];launch=p['launch_slot']
        tape=self.tapes[mint];trace=self.traces[label][mint]
        markers=[f for f in self.fill_markers if f['run']==label and f['mint']==mint]
        ref=self.reference_quotes(label,mint);inventory=self.inventory(label,mint)
        blocked=min((int(e['boundary_ordinal']) for e in self.errors[label] if e['mint']==mint),default=None)
        ordinals=sorted({boundary(birth['slot'],birth['tx_idx']),*[r['boundary'] for r in tape],
            *[r['boundary_ordinal'] for r in trace],*[r['boundary'] for r in markers],
            *[r['boundary'] for r in inventory],*([blocked] if blocked else [])})
        rank={n:i for i,n in enumerate(ordinals)}
        native_times={r['slot']:r['time'] for r in trace if not r['action'].startswith('ORDER_')}
        def hover(ordinal,detail=''):
            slot=ordinal>>32;tx=(ordinal&((1<<32)-1))-1
            timestamp=self.clock.get(slot,native_times.get(slot))
            return [str(slot),str(tx),'clock' if tx<0 else 'transaction',utc(timestamp) if timestamp is not None else 'No retained clock',str(ordinal),detail]
        template='Slot %{customdata[0]} · tx %{customdata[1]}<br>%{customdata[3]}<br>%{customdata[5]}<br>Value: %{y:.9g}<extra>%{fullData.name}</extra>'
        fig=make_subplots(rows=6,cols=1,shared_xaxes=True,vertical_spacing=.045,
            row_heights=[.28,.16,.16,.14,.12,.14],subplot_titles=[
                'Curve marginal price and actual average fill prices',
                'Our executable net liquidation PnL and closed PnL',
                'Tracked buyer fee-adjusted liquidation PnL · zero = break-even',
                'Modeled developer and reference inventory',
                'Observed external wallets with positive modeled inventory · incomplete coverage',
                'Real SOL reserves'])
        def line(rows,y,caption,color,panel,dash=None):
            if not rows:return
            rows=compact_steps(rows,y)
            fig.add_trace(go.Scatter(x=[rank[r['boundary']] for r in rows],y=[r.get(y) for r in rows],
                customdata=[hover(r['boundary'],r.get('detail',r.get('quality',''))) for r in rows],
                mode='lines',line={'color':color,'width':1.5,'shape':'hv',**({'dash':dash} if dash else {})},
                name=caption,connectgaps=False,hovertemplate=template),row=panel,col=1)
        historical=[dict(boundary=r['boundary'],price=int(r['virtual_quote'])/int(r['virtual_base'])/1000,
                         reserve=int(r['real_quote'])/LAMPORTS) for r in tape]
        line(historical,'price','Observed historical marginal price',HIST,1)
        modeled=[dict(boundary=r['boundary_ordinal'],price=int(r['virtual_sol'])/int(r['virtual_tokens'])/1000,
                      reserve=(int(r['virtual_sol'])-int(birth['initial_virtual_quote_raw'])+int(birth.get('initial_real_quote_raw') or 0))/LAMPORTS)
                 for r in trace if r.get('virtual_sol') and r.get('virtual_tokens')]
        line(modeled,'price','Replayed marginal price at evaluations',REPLAY,1)
        for side,symbol,color in [('BUY','triangle-up',GOOD),('SELL','triangle-down',BAD)]:
            values=[m for m in markers if m['side']==side]
            if values:
                fig.add_trace(go.Scatter(x=[rank[r['boundary']] for r in values],y=[r['average_curve_price_sol'] for r in values],
                    customdata=[hover(r['boundary'],f"{side} filled · {r['tokens_raw']/10**6:,.6f} tokens · {r['curve_sol_lamports']/LAMPORTS:.9f} curve SOL") for r in values],
                    mode='markers',marker={'symbol':symbol,'size':12,'color':color,'line':{'width':1,'color':'white'}},
                    name=side+' fill — average curve price',hovertemplate=template),row=1,col=1)
        signals=[r for r in trace if r['action']=='ENTRY_SIGNAL']
        for r in signals[:1]:
            fig.add_vline(x=rank[r['boundary_ordinal']],line_color='#b88719',line_dash='dot',row='all',col=1)
            fig.add_trace(go.Scatter(x=[rank[r['boundary_ordinal']]],y=[int(r['virtual_sol'])/int(r['virtual_tokens'])/1000],
                mode='markers',marker={'symbol':'diamond-open','size':11,'color':'#b88719'},name='Entry signal · decision',
                customdata=[hover(r['boundary_ordinal'],f"Budget {int(r['budget_lamports'])/LAMPORTS:.9f} SOL")],hovertemplate=template),row=1,col=1)
        exits=[r for r in trace if r['action']=='ORDER_ACCEPTED' and r.get('side')=='SELL']
        for r in exits:
            fig.add_trace(go.Scatter(x=[rank[r['boundary_ordinal']]],y=[int(r['virtual_sol'])/int(r['virtual_tokens'])/1000],
                mode='markers',marker={'symbol':'diamond-open','size':10,'color':BAD},name='Exit order accepted · decision',
                customdata=[hover(r['boundary_ordinal'],'Sell scheduled; fill follows configured latency')],hovertemplate=template),row=1,col=1)
        failed=[r for r in trace if r['action'] in ('ORDER_EXECUTION_FAILED','ORDER_REJECTED')]
        for r in failed:
            if r.get('virtual_sol'):
                fig.add_trace(go.Scatter(x=[rank[r['boundary_ordinal']]],y=[int(r['virtual_sol'])/int(r['virtual_tokens'])/1000],
                    mode='markers',marker={'symbol':'x','size':11,'color':'#333'},name='Failed/rejected attempt · no fill',
                    customdata=[hover(r['boundary_ordinal'],r.get('reports',''))],hovertemplate=template),row=1,col=1)
        checks=[dict(boundary=r['boundary_ordinal'],pnl=int(r['pnl_lamports'])/LAMPORTS,
                     target=int(r['target_lamports'])/LAMPORTS,detail='Native exit evaluation; '+('timeout reached' if r.get('timeout')=='True' else 'before timeout'))
                for r in trace if r['action']=='EXIT_EVALUATION']
        line(checks,'pnl','Our net liquidation estimate','#1768b3',2)
        if self.results[label]['summary']['policy'].get('own_take_profit_enabled',1):
            line(checks,'target','Own +5% exit target',GOOD,2,'dot')
        if p['status']=='CLOSED':
            marker=next(m for m in markers if m['side']=='SELL')
            fig.add_trace(go.Scatter(x=[rank[marker['boundary']]],y=[p['closed_pnl_lamports']/LAMPORTS],mode='markers',
                marker={'symbol':'diamond','size':10,'color':BAD},name='Actual simulated closed PnL',
                customdata=[hover(marker['boundary'],p['reason'])],hovertemplate=template),row=2,col=1)
        ref_rows=[dict(r,pnl=r['net_pnl_lamports']/LAMPORTS if r['net_pnl_lamports'] is not None else None) for r in ref]
        line(ref_rows,'pnl','Tracked buyer net liquidation estimate','#8c459a',3)
        fig.add_hline(y=0,line_color='#999',row=2,col=1);fig.add_hline(y=0,line_color='#999',row=3,col=1)
        inv=[dict(r,dev=r['dev_tokens_raw']/10**12,reference=r['reference_tokens_raw']/10**12 if r['reference_tokens_raw'] is not None else None,
                  detail=f"{r['negative_unproven_wallets']} addresses have unproven negative inventory") for r in inventory]
        line(inv,'dev','Developer inventory · million tokens','#333',4)
        line(inv,'reference','Reference inventory · million tokens','#239c99',4)
        line(inv,'positive_observed_wallets','Observed wallets with positive inventory','#7764a8',5)
        line(historical,'reserve','Observed historical real SOL',HIST,6)
        line(modeled,'reserve','Replayed real SOL at evaluations',REPLAY,6)
        if blocked is not None:
            for panel in (2,3,4,5):fig.add_vline(x=rank[blocked],line_dash='dash',line_color='#333',row=panel,col=1)
        ticks=list(range(0,len(ordinals),max(1,len(ordinals)//8)))
        fig.update_xaxes(tickvals=ticks,ticktext=[str((ordinals[i]>>32)-launch) for i in ticks])
        fig.update_xaxes(title_text=f'Transaction order · tick labels = slots since launch {launch}; hover for exact slot/tx',row=6,col=1)
        fig.update_yaxes(title_text='SOL / token',row=1,col=1)
        fig.update_yaxes(title_text='SOL',row=2,col=1);fig.update_yaxes(title_text='SOL',row=3,col=1)
        fig.update_yaxes(title_text='Million tokens',row=4,col=1);fig.update_yaxes(title_text='Observed wallets',row=5,col=1)
        fig.update_yaxes(title_text='SOL',row=6,col=1)
        start=p.get('signal_slot',launch)-3
        end=(p['exit_slot']+25 if p.get('exit_slot') is not None else
             (blocked>>32)+25 if p['status']=='OPEN' and blocked is not None else
             tape[-1]['slot'] if p['status']=='OPEN' else p.get('signal_slot',launch)+40)
        low=max(0,bisect_right(ordinals,start<<32)-1);high=min(len(ordinals)-1,bisect_right(ordinals,(end+1)<<32))
        for axis in range(1,7):fig.layout['xaxis'+(str(axis) if axis>1 else '')].range=[low,high]
        around={f'xaxis{n if n>1 else ""}.range':[low,high] for n in range(1,7)}
        full={f'xaxis{n if n>1 else ""}.autorange':True for n in range(1,7)}
        fig.update_layout(height=1550,template='plotly_white',hovermode='x unified',
            title=dict(text=f"{html.escape(p['symbol'])} · {label} · {outcome(p)}<br><sup>{p['reason']} · curves differ from average fills · historical prices may continue beyond modeled coverage</sup>",font={'size':16},y=.99,yanchor='top'),
            legend={'orientation':'h','y':-.10,'yanchor':'top','font':{'size':10}},margin={'t':150,'r':40,'l':90,'b':230},
            updatemenus=[{'buttons':[{'label':'Around trade','method':'relayout','args':[around]},
                                    {'label':'Full history','method':'relayout','args':[full]}],
                         'type':'buttons','direction':'left','x':0,'y':1.055}],
            meta={'run':label,'mint':mint,'buy_fills':sum(m['side']=='BUY' for m in markers),
                  'sell_fills':sum(m['side']=='SELL' for m in markers),'boundary_order':'exact integer ranks; raw boundaries in hover strings'})
        return fig

    def show(self,label,mint,logs=True):
        p=self.positions[label][mint]
        display(Markdown(f"### {p['symbol']} · {label} · {outcome(p)}\n[Token on GMGN]({p['gmgn_url']}) · [Developer](https://gmgn.ai/sol/address/{p['creator']})"))
        fields=('status','reason','signal_slot','entry_slot','exit_signal_slot','exit_slot','budget_lamports',
            'entry_cash_lamports','closed_pnl_lamports','creator_status_at_signal','creator_tokens_at_signal',
            'sniper_wallet','sniper_cost_lamports','sniper_tokens')
        metadata={key:p.get(key) for key in fields}
        signals=[r for r in self.traces[label][mint] if r['action']=='ENTRY_SIGNAL']
        metadata['signal_boundary']=str(signals[0]['boundary_ordinal']) if signals else None
        for fill in self.fill_markers:
            if fill['run']==label and fill['mint']==mint:
                metadata[fill['side'].lower()+'_fill_boundary']=str(fill['boundary'])
        display(pd.Series(metadata,name='Recorded native metadata').to_frame())
        display(self.figure(label,mint))
        if logs:
            rows=[r for r in self.traces[label][mint] if r['action'] in ('ENTRY_CREATOR_FLAT_CHECK','ENTRY_SIGNAL','EXIT_EVALUATION') or r['action'].startswith('ORDER_')]
            # Full decisions remain in CSV; selected rows expose first signal and final decisions.
            selected=rows[:8]+rows[-12:] if len(rows)>20 else rows
            frame=pd.DataFrame(selected)
            columns=['slot','time','boundary_ordinal','action','creator_status_at_signal','creator_tokens_at_signal',
                     'wallet_tokens','wallet_adjusted_cost','budget_lamports','pnl_lamports','target_lamports','timeout','side','reports']
            display(frame[[c for c in columns if c in frame]])

    def browser(self):
        import ipywidgets as widgets
        run=widgets.Dropdown(options=list(self.results),description='Run:')
        group=widgets.Dropdown(options=['All','Profitable','Losing','Unresolved','Entry failed','Entry rejected','No entry signal'],description='Outcome:')
        token=widgets.Dropdown(description='Token:',layout=widgets.Layout(width='95%'))
        output=widgets.Output()
        def refresh_tokens(*_):
            rows=self.table[self.table['run']==run.value]
            if group.value!='All':rows=rows[rows['group']==group.value]
            token.options=[(f"{r.token} · {r.group} · {r.mint[:8]}",r.mint) for r in rows.itertuples()]
        def render(*_):
            with output:
                output.clear_output(wait=True)
                if token.value:self.show(run.value,token.value)
        run.observe(refresh_tokens,names='value');group.observe(refresh_tokens,names='value')
        token.observe(render,names='value');refresh_tokens()
        return widgets.VBox([widgets.HBox([run,group]),token,output])

    def export_all(self):
        graphs=self.export/'plotly';graphs.mkdir(exist_ok=True)
        index=[]
        for label in self.results:
            for mint in sorted(self.wanted):
                p=self.positions[label][mint]
                if 'signal_slot' not in p:continue
                fig=self.figure(label,mint);filename=f'{label}-{mint}.json';fig.write_json(graphs/filename)
                index.append(dict(run=label,mint=mint,token=p['symbol'],group=outcome(p),file='plotly/'+filename,
                                  buy_fills=fig.layout.meta['buy_fills'],sell_fills=fig.layout.meta['sell_fills']))
        pd.DataFrame(index).to_csv(self.export/'chart_index.csv',index=False)
        pd.DataFrame([r for records in self.reference_cache.values() for r in records]).to_csv(self.export/'reference_quote_diagnostics.csv',index=False)
        pd.DataFrame([r for records in self.inventory_cache.values() for r in records]).to_csv(self.export/'modeled_inventory_snapshots.csv',index=False)
        receipt={'complete':True,'source_manifest_sha256':self.source_sha256,'charts':len(index),
                 'fill_markers':len(self.fill_markers),'run_result_hashes':{k:sha(v/'result.json') for k,v in self.folders.items()},
                 'files':{str(p.relative_to(self.export)):sha(p) for p in self.export.rglob('*') if p.is_file() and p.name!='verification.json'}}
        (self.export/'verification.json').write_text(json.dumps(receipt,indent=2)+'\n')
        return receipt
