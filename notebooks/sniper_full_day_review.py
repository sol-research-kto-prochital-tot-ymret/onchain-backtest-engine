"""Read-only notebook views of the frozen October 12 run without the dev gate."""
from bisect import bisect_right
from collections import defaultdict
import csv
import gzip
import html
import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from IPython.display import HTML, Markdown, display

from sniper_interactive_review import NativeReview, outcome, boundary, sha, utc, GOOD, BAD, HIST, REPLAY

ROOT = Path(__file__).absolute().parents[1]
DATA = ROOT / 'var/dnipro/sniper-oct12-no-dev-flat-review-20261016-v1'
LABEL = 'without dev sell all'
SOL = 'So11111111111111111111111111111111111111112'
L = 10**9


def csv_rows(path):
    with path.open() as stream:
        return list(csv.DictReader(stream))


class FullDayReview:
    def __init__(self, data=DATA):
        self.data = Path(data)
        receipt = json.loads((self.data / 'context-receipt.json').read_text())
        assert receipt['complete']
        for name, digest in receipt['files'].items():
            assert sha(self.data / name) == digest, name
        self.receipt = receipt
        self.result = json.loads((self.data / 'native/result.json').read_text())
        self.summary = self.result['summary']
        assert self.summary['source_input_sha256'] == receipt['source_input_sha256']
        self.positions = {LABEL: {p['mint']: p for p in self.result['positions'] if 'signal_slot' in p}}
        self.orders = {r['order_id']: r for r in csv_rows(self.data / 'order_mapping.csv')}
        self.births = json.loads((self.data / 'births.json').read_text())
        with gzip.open(self.data / 'clocks.json.gz', 'rt') as stream:
            self.clock = {int(k): v for k, v in json.load(stream).items()}
        for p in self.positions[LABEL].values():
            for field in ('signal', 'entry', 'exit'):
                if field+'_time' in p:
                    self.clock.setdefault(p[field+'_slot'], p[field+'_time'])
        self.traces, self.errors, self.repriced, self.transfers = {}, {}, {}, {}
        self.traces[LABEL] = defaultdict(list)
        for row in csv_rows(self.data / 'native/strategy_trace.csv'):
            for key in ('slot', 'time', 'boundary_ordinal'):
                row[key] = int(row[key])
            self.traces[LABEL][row['mint']].append(row)
        for name, target in [('conditional_errors', self.errors), ('conditional_repriced', self.repriced),
                             ('inventory_transfers_applied', self.transfers)]:
            target[LABEL] = csv_rows(self.data / 'native' / (name+'.csv'))
        self.audits = csv_rows(self.data / 'native/order_audit.csv')
        self.failures = csv_rows(self.data / 'native/execution_failures.csv')
        self.tapes, self.inventory_cache = {}, {}
        self.cash_events, self.fill_markers, self.pnl_events, self.exposure_events = [], [], [], []
        settlements = defaultdict(list)
        for transaction in self.result['ledger']:
            conservation = defaultdict(int)
            for posting in transaction['postings']:
                conservation[posting['asset_id']] += posting['amount_atomic']
            assert all(value == 0 for value in conservation.values())
            order = self.orders[transaction['correlation_id']]
            delta = sum(p['amount_atomic'] for p in transaction['postings'] if p['asset_id']==SOL
                        and p['account_kind'] in ('PORTFOLIO_AVAILABLE', 'PORTFOLIO_RESERVED'))
            event = dict(mint=order['mint'], boundary=transaction['boundary_ordinal'],
                         slot=transaction['boundary_ordinal'] >> 32, cash_delta_lamports=delta,
                         reason=transaction['reason'], side=order['side'])
            self.cash_events.append(event)
            if transaction['reason']=='ORDER_SETTLEMENT':
                settlements[transaction['correlation_id']].append(transaction)
            if transaction['reason']=='ORDER_RELEASE' and order['side']=='SELL' and delta<0:
                self.exposure_events.append(dict(boundary=event['boundary'], delta=-delta))
            if transaction['reason']=='ORDER_RELEASE' and order['side']=='BUY' and delta<0:
                self.pnl_events.append(dict(boundary=event['boundary'], slot=event['slot'],
                    mint=event['mint'], pnl_lamports=delta, event='Submitted failed entry fee'))
        for fill in self.result['fills']:
            side = 'BUY' if fill['sold_asset_id']==SOL else 'SELL'
            values = [t for t in settlements[fill['order_id']] if any(p['account_kind']=='VENUE' for p in t['postings'])]
            assert len(values)==1
            curve = sum(p['amount_atomic'] for p in values[0]['postings'] if p['asset_id']==SOL and p['account_kind']=='VENUE')
            tokens = fill['amount_out_atomic'] if side=='BUY' else fill['amount_in_atomic']
            assert (curve>0)==(side=='BUY')
            marker = dict(mint=fill['pool_id'], side=side, boundary=fill['boundary_ordinal'],
                slot=fill['boundary_ordinal'] >> 32, tokens_raw=tokens, curve_sol_lamports=abs(curve),
                average_curve_price_sol=abs(curve)/tokens/1000, order_id=fill['order_id'])
            self.fill_markers.append(marker)
            p = self.positions[LABEL][fill['pool_id']]
            if side=='BUY':
                self.exposure_events.append(dict(boundary=marker['boundary'], delta=p['entry_cash_lamports']))
            else:
                self.exposure_events.append(dict(boundary=marker['boundary'], delta=-p['entry_cash_lamports']-p['failed_execution_cost_lamports']))
                self.pnl_events.append(dict(boundary=marker['boundary'], slot=marker['slot'], mint=p['mint'],
                    pnl_lamports=p['closed_pnl_lamports'], event='Position closed'))
        s = self.summary
        assert sum(r['pnl_lamports'] for r in self.pnl_events)==s['realized_net_pnl_lamports']
        assert sum(r['delta'] for r in self.exposure_events)==s['open_cash_cost_lamports']
        assert sum(r['cash_delta_lamports'] for r in self.cash_events)==s['realized_net_pnl_lamports']-s['open_cash_cost_lamports']
        first = {}
        for row in self.errors[LABEL]:
            if self.positions[LABEL].get(row['mint'], {}).get('status')=='OPEN':
                first.setdefault(row['mint'], row['reason'])
        self.entry_failure_reason = {}
        for row in self.failures:
            order = self.orders[row['order_id']]
            if order['side']=='BUY':self.entry_failure_reason.setdefault(order['mint'], row['reason'])
        self.table = pd.DataFrame([dict(mint=p['mint'], token=p['symbol'], group=outcome(p),
            reason=p['reason'], first_causal_blocker=first.get(p['mint'], ''),
            execution_failure_reason=self.entry_failure_reason.get(p['mint'], ''),
            entry_SOL=p['entry_cash_lamports']/L, closed_PnL_SOL=p['closed_pnl_lamports']/L if p['status']=='CLOSED' else None,
            failed_fees_SOL=p['failed_execution_cost_lamports']/L,
            unresolved_cost_SOL=(p['entry_cash_lamports']+p['failed_execution_cost_lamports'])/L if p['status']=='OPEN' else 0,
            signal_slot=p['signal_slot'], entry_slot=p.get('entry_slot'), exit_slot=p.get('exit_slot'),
            tracked_wallet=p['sniper_wallet'], developer=p['creator'], gmgn_url=p['gmgn_url'],
            detailed_trace_available=bool(self.traces[LABEL].get(p['mint'])))
            for p in self.positions[LABEL].values()]).sort_values(['signal_slot', 'mint'])
        assert self.table.group.value_counts().to_dict()=={'Losing':318, 'Profitable':251, 'Entry failed':238, 'Unresolved':80}

    def overview(self):
        s = self.summary
        return pd.DataFrame([dict(period='October 12, 2026 — full UTC day', wallet_SOL=10000,
            signals=s['signals'], buys=s['filled_entries'], profitable=251, losing=318,
            unresolved=80, failed_entries=238, risk_rejected=0,
            realized_net_SOL=s['realized_net_pnl_sol'], unresolved_cost_SOL=s['open_cash_cost_sol'],
            final_portfolio_PnL='Unknown: unresolved positions are not marked')])

    def display_table(self, group=None):
        rows = self.table if group is None else self.table[self.table.group==group]
        frame = rows.copy()
        for name in ('signal_slot', 'entry_slot', 'exit_slot'):
            frame[name] = frame[name].astype('Int64')
        text = frame.to_html(index=False, escape=True, float_format=lambda x:f'{x:.9f}', na_rep='—')
        for url in frame.gmgn_url:
            text = text.replace(html.escape(url), f'<a href="{html.escape(url)}">GMGN</a>')
        display(HTML('<div style="max-height:600px;overflow:auto">'+text+'</div>'))

    def tape(self, mint):
        if mint not in self.tapes:
            with gzip.open(self.data/'token-context'/(mint+'.json.gz'), 'rt') as stream:
                rows = json.load(stream)
            self.tapes[mint] = [dict(r, boundary=boundary(r['slot'], r['tx_idx'])) for r in rows if r['kind']=='trade']
        return self.tapes[mint]

    def inventory(self, mint):
        self.tape(mint)
        end = self.births[mint]['chart_end_unix']
        return [r for r in NativeReview.inventory(self, LABEL, mint)
                if self.clock.get(r['slot'], end+1) <= end]

    def strategy_figure(self):
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=.10,
            subplot_titles=['Realized net PnL — unresolved exposure excluded',
                            'Cash change — available plus reserved SOL; tokens and posted rent excluded',
                            'Open committed cost — exposure, not a realized loss'])
        start, end = min(self.clock), max(self.clock)
        series = [(self.pnl_events, 'pnl_lamports', '#1768b3'),
                  (self.cash_events, 'cash_delta_lamports', '#69738b'),
                  (self.exposure_events, 'delta', REPLAY)]
        for panel, (events, field, color) in enumerate(series, 1):
            total = 0; xs = [0]; ys = [0]; hover = [['Start', '']]
            for event in sorted(events, key=lambda r:r['boundary']):
                total += event[field]
                slot = event['boundary'] >> 32
                xs.append(slot-start); ys.append(total/L)
                hover.append([str(slot), str(event['boundary'])])
            xs.append(end-start); ys.append(total/L); hover.append([str(end), 'End of retained clock'])
            fig.add_trace(go.Scatter(x=xs, y=ys, customdata=hover, mode='lines', line_shape='hv',
                line={'color':color}, name=['Realized PnL', 'Cash change', 'Open cost'][panel-1],
                hovertemplate='Slot %{customdata[0]}<br>%{y:.9f} SOL<extra>%{fullData.name}</extra>'), row=panel, col=1)
            fig.add_hline(y=0, line_color='#bbb', row=panel, col=1)
            fig.update_yaxes(title_text='SOL', row=panel, col=1)
        fig.update_xaxes(title_text=f'Slots since {start}', row=3, col=1)
        fig.update_layout(height=900, template='plotly_white', hovermode='x unified',
            title='October 12 — without dev sell all · realized net −109.602394368 SOL')
        return fig

    def figure(self, mint):
        p = self.positions[LABEL][mint]; tape = self.tape(mint); launch = p['launch_slot']
        end = self.births[mint]['chart_end_unix']
        traces = [r for r in self.traces[LABEL].get(mint, []) if r['time'] <= end]
        fills = [r for r in self.fill_markers if r['mint']==mint]
        orders = [r for r in self.orders.values() if r['mint']==mint]
        failed = [r for r in self.failures if self.orders[r['order_id']]['mint']==mint]
        errors = [r for r in self.errors[LABEL] if r['mint']==mint
                  and self.clock.get(int(r['boundary_ordinal']) >> 32, end+1) <= end]
        inventory = self.inventory(mint)
        ordinals = sorted({*[r['boundary'] for r in tape], *[r['boundary'] for r in fills],
            *[int(r['decision_boundary']) for r in orders], *[int(r['boundary_ordinal']) for r in failed+errors],
            *[r['boundary_ordinal'] for r in traces], *[r['boundary'] for r in inventory]})
        rank = {b:i for i,b in enumerate(ordinals)}
        bounds = [r['boundary'] for r in tape]
        def custom(ordinal, detail=''):
            slot = ordinal >> 32; tx = (ordinal & (2**32-1))-1
            return [str(slot), str(tx), utc(self.clock[slot]) if slot in self.clock else 'No retained timestamp', str(ordinal), detail]
        def historic_price(ordinal):
            i = bisect_right(bounds, ordinal)-1
            return tape[i]['virtual_quote']/tape[i]['virtual_base']/1000 if i>=0 else None
        hover = 'Slot %{customdata[0]} · tx %{customdata[1]}<br>%{customdata[2]}<br>%{customdata[4]}<br>%{y:.9g}<extra>%{fullData.name}</extra>'
        fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=.065,
            row_heights=[.34,.19,.23,.24], specs=[[{}],[{}],[{'secondary_y':True}],[{}]],
            subplot_titles=['Historical marginal price and actual simulated fill VWAP', 'Real SOL reserves',
                            'Modeled external inventory — incomplete transfer and holder coverage',
                            'Native liquidation evaluations where retained; actual closed PnL'])
        def line(records, field, name, panel, color, scale=1, secondary=False):
            if not records:return
            records = [r for r in records if r['boundary'] in rank]
            if not records:return
            fig.add_trace(go.Scatter(x=[rank[r['boundary']] for r in records],
                y=[r[field]/scale if r.get(field) is not None else None for r in records],
                customdata=[custom(r['boundary']) for r in records], name=name, mode='lines',
                line={'color':color, 'shape':'hv', 'width':1.5}, connectgaps=False, hovertemplate=hover),
                row=panel, col=1, **({'secondary_y':secondary} if panel==3 else {}))
        historical = []
        birth = self.births[mint]
        old = tuple(int(birth[k] or 0) for k in ('initial_virtual_quote_raw', 'initial_virtual_base_raw',
                                                'initial_real_quote_raw', 'initial_real_base_raw'))
        gaps = 0
        for r in tape:
            sign = 1 if r['side']=='buy' else -1
            expected = (old[0]+sign*r['quote'], old[1]-sign*r['tokens'], old[2]+sign*r['quote'], old[3]-sign*r['tokens'])
            actual = tuple(r[k] for k in ('virtual_quote','virtual_base','real_quote','real_base'))
            if expected[1:]!=actual[1:] or abs(expected[0]-actual[0])>1:
                historical.append(dict(boundary=r['boundary'], price=None, real_quote=None));gaps+=1
            historical.append(dict(r, price=r['virtual_quote']/r['virtual_base']/1000));old=actual
        line(historical, 'price', 'Observed historical price', 1, HIST)
        line(historical, 'real_quote', 'Observed real SOL', 2, HIST, L)
        repriced = [dict(r, boundary=int(r['boundary_ordinal']), real=int(r['real_sol_after']))
                    for r in self.repriced[LABEL] if r['mint']==mint and int(r['boundary_ordinal'])<=max(bounds)]
        line(repriced, 'real', 'Replayed real SOL after counterpart trades', 2, REPLAY, L)
        for side, symbol, color in [('BUY','triangle-up',GOOD), ('SELL','triangle-down',BAD)]:
            records = [r for r in fills if r['side']==side]
            fig.add_trace(go.Scatter(x=[rank[r['boundary']] for r in records],
                y=[r['average_curve_price_sol'] for r in records], mode='markers',
                marker={'symbol':symbol, 'size':13, 'color':color}, name=side+' fill · average curve price',
                customdata=[custom(r['boundary'], f"{side}: {r['tokens_raw']/1e6:,.6f} tokens; curve SOL {r['curve_sol_lamports']/L:.9f}; fees separate") for r in records],
                hovertemplate=hover), row=1, col=1)
        for side, color in [('BUY','#b88719'),('SELL',BAD)]:
            records = [r for r in orders if r['side']==side]
            fig.add_trace(go.Scatter(x=[rank[int(r['decision_boundary'])] for r in records],
                y=[historic_price(int(r['decision_boundary'])) for r in records], mode='markers',
                marker={'symbol':'diamond-open', 'size':10, 'color':color}, name=side+' decision · historical reference',
                customdata=[custom(int(r['decision_boundary']), 'Recorded order acceptance; marker uses historical reference price, not a fill') for r in records],
                hovertemplate=hover), row=1, col=1)
        if failed:
            fig.add_trace(go.Scatter(x=[rank[int(r['boundary_ordinal'])] for r in failed],
                y=[historic_price(int(r['boundary_ordinal'])) for r in failed], mode='markers',
                marker={'symbol':'x', 'size':11, 'color':'#333'}, name='Failed/rejected attempt · no fill',
                customdata=[custom(int(r['boundary_ordinal']), r['reason']) for r in failed], hovertemplate=hover), row=1, col=1)
        line(inventory, 'dev_tokens_raw', 'Developer · million tokens', 3, '#333', 10**12)
        line(inventory, 'reference_tokens_raw', 'Tracked buyer · million tokens', 3, '#239c99', 10**12)
        line(inventory, 'positive_observed_wallets', 'Observed positive external wallets', 3, '#a387cf', secondary=True)
        checks = [dict(boundary=r['boundary_ordinal'], pnl=int(r['pnl_lamports']))
                  for r in traces if r['action']=='EXIT_EVALUATION']
        line(checks, 'pnl', 'Our native net liquidation estimate', 4, '#1768b3', L)
        if p['entry_cash_lamports']:
            target = ((p['entry_cash_lamports']-self.summary['policy']['account_rent_refund_lamports'])*500+9999)//10000
            fig.add_hline(y=target/L, line_color=GOOD, line_dash='dot', row=4, col=1, exclude_empty_subplots=False)
        if p['status']=='CLOSED':
            marker = next(r for r in fills if r['side']=='SELL')
            fig.add_trace(go.Scatter(x=[rank[marker['boundary']]], y=[p['closed_pnl_lamports']/L],
                mode='markers', marker={'symbol':'diamond','size':10,'color':BAD}, name='Actual closed PnL',
                customdata=[custom(marker['boundary'], p['reason'])], hovertemplate=hover), row=4, col=1)
        if not checks:
            low, high = fig.layout.yaxis5.domain
            fig.add_annotation(text='Detailed native exit evaluations were not retained for this token.',
                xref='paper', yref='paper', x=.5, y=low+.85*(high-low), showarrow=False, font={'size':10})
        if errors:
            blocked = min(int(r['boundary_ordinal']) for r in errors)
            for panel in (2,3,4):fig.add_vline(x=rank[blocked], line_color='#333', line_dash='dash', row=panel, col=1)
        ticks = list(range(0, len(ordinals), max(1, len(ordinals)//8)))
        fig.update_xaxes(tickvals=ticks, ticktext=[str((ordinals[i] >> 32)-launch) for i in ticks])
        fig.update_xaxes(title_text=f'Transaction order; ticks = slots since launch {launch}. Hover for exact slot and tx.', row=4, col=1)
        fig.update_yaxes(title_text='SOL/token', row=1, col=1)
        fig.update_yaxes(title_text='SOL', row=2, col=1)
        fig.update_yaxes(title_text='Million tokens', row=3, col=1, secondary_y=False)
        fig.update_yaxes(title_text='Observed wallets', row=3, col=1, secondary_y=True)
        fig.update_yaxes(title_text='SOL', row=4, col=1)
        reason = self.entry_failure_reason.get(mint, p['reason']) if p['status']=='ENTRY_FAILED' else p['reason']
        fig.update_layout(height=1300, template='plotly_white', hovermode='x unified',
            title=f"{html.escape(p['symbol'])} · {outcome(p)} · {html.escape(reason)}",
            margin={'t':110, 'b':190}, legend={'orientation':'h','y':-.12,'font':{'size':10}},
            meta={'mint':mint, 'buy_fills':sum(r['side']=='BUY' for r in fills),
                  'sell_fills':sum(r['side']=='SELL' for r in fills), 'detailed_native_trace':bool(traces)})
        fig.layout.meta['raw_reserve_discontinuities'] = gaps
        return fig

    def show(self, mint, logs=True):
        p = self.positions[LABEL][mint]
        display(HTML(f'<h3>{html.escape(p["symbol"])} · {outcome(p)}</h3><a href="{p["gmgn_url"]}">Token on GMGN</a> · '
                     f'<a href="https://gmgn.ai/sol/address/{p["sniper_wallet"]}">Tracked buyer</a> · '
                     f'<a href="https://gmgn.ai/sol/address/{p["creator"]}">Developer</a>'))
        fields = ('status', 'reason', 'signal_slot', 'entry_slot', 'exit_signal_slot', 'exit_slot',
                  'budget_lamports', 'entry_cash_sol', 'closed_pnl_sol', 'failed_execution_cost_sol',
                  'sniper_wallet', 'sniper_tokens', 'sniper_trading_cash_cost_sol', 'sniper_creator_fee_income_sol',
                  'signal_our_threshold', 'signal_sniper_threshold', 'exit_attempts')
        display(pd.Series({k:p.get(k) for k in fields}, name='Recorded metadata').to_frame())
        if p['status']=='ENTRY_FAILED':display(Markdown('**Recorded entry failure:** '+self.entry_failure_reason[mint]))
        display(self.figure(mint))
        if logs:
            ids = {r['order_id'] for r in self.orders.values() if r['mint']==mint}
            display(Markdown('**Complete recorded order lifecycle for this token**'))
            display(pd.DataFrame([r for r in self.audits if r['order_id'] in ids]))
            traces = self.traces[LABEL].get(mint, [])
            if traces:
                display(Markdown('**Retained strategy evaluations — first and last rows**'))
                display(pd.DataFrame(traces[:6]+traces[-10:]))
            else:
                display(Markdown('Detailed step-by-step strategy evaluations were not retained. Order lifecycle and signal metadata above are recorded evidence.'))
            errors = [r for r in self.errors[LABEL] if r['mint']==mint]
            if errors:display(pd.DataFrame(errors))

    def browser(self):
        import ipywidgets as widgets
        group = widgets.Dropdown(options=['All','Profitable','Losing','Unresolved','Entry failed'], description='Outcome:')
        search = widgets.Text(description='Search:', placeholder='Token symbol or mint')
        token = widgets.Dropdown(description='Token:', layout=widgets.Layout(width='95%'))
        output = widgets.Output()
        def choices(*_):
            rows = self.table
            if group.value!='All':rows=rows[rows.group==group.value]
            if search.value:
                text = search.value.casefold()
                rows=rows[rows.token.str.casefold().str.contains(text, regex=False)|rows.mint.str.casefold().str.contains(text, regex=False)]
            token.options=[(f'{r.token} · {r.group} · {r.mint[:10]}',r.mint) for r in rows.itertuples()]
        def render(*_):
            with output:
                output.clear_output(wait=True)
                if token.value:self.show(token.value)
        group.observe(choices, names='value');search.observe(choices,names='value')
        token.observe(render,names='value');choices()
        return widgets.VBox([widgets.HBox([group,search]),token,output])

    def export(self):
        folder = self.data / 'csv'; folder.mkdir(exist_ok=True)
        self.table.to_csv(folder/'all_positions.csv', index=False)
        for label in self.table.group.unique():
            self.table[self.table.group==label].to_csv(folder/(label.lower().replace(' ','_')+'.csv'), index=False)
        for name, rows in [('fill_markers',self.fill_markers),('pnl_events',self.pnl_events),
                           ('cash_events',self.cash_events),('exposure_events',self.exposure_events)]:
            pd.DataFrame(rows).to_csv(folder/(name+'.csv'), index=False)
        return folder
