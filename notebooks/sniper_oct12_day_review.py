"""Read-only notebook views of the completed October 12 no-dev-flat run."""
from bisect import bisect_right
from collections import defaultdict, deque
import csv
import gzip
import html
import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from IPython.display import HTML, Markdown, display

from prepare_sniper_oct12_review import OUT, sha
from sniper_interactive_review import outcome, compact_steps, GOOD, BAD, HIST, REPLAY

SOL = 'So11111111111111111111111111111111111111112'
LAMPORTS = 10**9
GROUPS = ['Profitable', 'Losing', 'Unresolved', 'Entry failed', 'Entry rejected', 'Breakeven']


def rows(path):
    with Path(path).open() as stream:
        return list(csv.DictReader(stream))


def boundary(slot, index):
    return (int(slot) << 32)+int(index)+1


class FullDayReview:
    def __init__(self, data=OUT):
        self.data = Path(data)
        self.receipt = json.loads((self.data/'context-receipt.json').read_text())
        assert self.receipt['complete']
        for name, digest in self.receipt['files'].items():
            assert sha(self.data/name) == digest, name
        self.native = self.data/'native'
        manifest = json.loads((self.native/'manifest.json').read_text())
        assert manifest['complete']
        assert sha(self.native/'manifest.json') == self.receipt['native_manifest_sha256']
        for name, digest in manifest['files'].items():
            assert sha(self.native/name) == digest, name
        self.result = json.loads((self.native/'result.json').read_text())
        self.summary = self.result['summary']
        assert self.summary['source_input_sha256'] == self.receipt['source_input_sha256']
        self.positions = {p['mint']: p for p in self.result['positions'] if 'signal_slot' in p}
        self.births = json.loads((self.data/'births.json').read_text())
        with gzip.open(self.data/'clocks.json.gz', 'rt') as stream:
            self.clock = {int(k): v for k, v in json.load(stream).items()}
        for p in self.positions.values():
            for slot, when in [('signal_slot', 'signal_time'), ('entry_slot', 'entry_time'), ('exit_slot', 'exit_time')]:
                if slot in p:
                    self.clock.setdefault(p[slot], p[when])
        self.orders = {r['order_id']: r for r in rows(self.data/'order_mapping.csv')}
        self.audits = rows(self.native/'order_audit.csv')
        self.failures = rows(self.native/'execution_failures.csv')
        self.errors = rows(self.native/'conditional_errors.csv')
        self.repriced = defaultdict(list)
        for r in rows(self.native/'conditional_repriced.csv'):
            self.repriced[r['mint']].append(r)
        self.transfers = defaultdict(list)
        for r in rows(self.native/'inventory_transfers_applied.csv'):
            self.transfers[r['mint']].append(r)
        self.traces = defaultdict(list)
        for r in rows(self.native/'strategy_trace.csv'):
            for k in ('boundary_ordinal', 'slot', 'time'):
                r[k] = int(r[k])
            self.traces[r['mint']].append(r)
        self.first_errors = {}
        for r in self.errors:
            self.first_errors.setdefault(r['mint'], r)
        self.cash, settlements = [], defaultdict(list)
        for tx in self.result['ledger']:
            totals = defaultdict(int)
            for posting in tx['postings']:
                totals[posting['asset_id']] += posting['amount_atomic']
            assert all(v == 0 for v in totals.values()), tx['transaction_id']
            order = self.orders[tx['correlation_id']]
            amount = sum(p['amount_atomic'] for p in tx['postings'] if p['asset_id']==SOL
                         and p['account_kind'] in ('PORTFOLIO_AVAILABLE', 'PORTFOLIO_RESERVED'))
            self.cash.append(dict(mint=order['mint'], order_id=order['order_id'], side=order['side'],
                boundary=tx['boundary_ordinal'], slot=tx['boundary_ordinal'] >> 32,
                amount=amount, reason=tx['reason']))
            if tx['reason']=='ORDER_SETTLEMENT':
                settlements[tx['correlation_id']].append(tx)
        self.markers, self.fills_by_mint = [], defaultdict(list)
        for fill in self.result['fills']:
            order = self.orders[fill['order_id']]
            curve = sum(p['amount_atomic'] for tx in settlements[fill['order_id']] for p in tx['postings']
                        if p['asset_id']==SOL and p['account_kind']=='VENUE')
            tokens = fill['amount_out_atomic'] if order['side']=='BUY' else fill['amount_in_atomic']
            assert (curve > 0) == (order['side']=='BUY')
            record = dict(mint=fill['pool_id'], side=order['side'], order_id=fill['order_id'],
                boundary=fill['boundary_ordinal'], slot=fill['boundary_ordinal'] >> 32,
                tokens_raw=tokens, curve_sol_lamports=abs(curve),
                average_curve_price_sol=abs(curve)/tokens/1000,
                protocol_creator_fee_lamports=fill['fee_amount_atomic'])
            self.markers.append(record)
            self.fills_by_mint[record['mint']].append(record)
        self.pnl, self.exposure = [], []
        self.table = []
        for mint, p in self.positions.items():
            orders = [r for r in self.orders.values() if r['mint']==mint]
            own_fills = self.fills_by_mint[mint]
            assert sum(r['side']=='BUY' for r in own_fills) == int(p['entry_cash_lamports']>0)
            assert sum(r['side']=='SELL' for r in own_fills) == int(p['status']=='CLOSED')
            if p['entry_cash_lamports']:
                buy = next(r for r in own_fills if r['side']=='BUY')
                self.exposure.append(dict(mint=mint, boundary=buy['boundary'], slot=buy['slot'],
                                          amount=p['entry_cash_lamports'], reason='Entry committed cost'))
            for e in self.cash:
                if e['mint']==mint and e['side']=='SELL' and e['reason']=='ORDER_RELEASE' and e['amount']<0:
                    self.exposure.append(dict(mint=mint, boundary=e['boundary'], slot=e['slot'],
                                              amount=-e['amount'], reason='Failed exit fee'))
            if p['status']=='CLOSED':
                sell = next(r for r in own_fills if r['side']=='SELL')
                self.pnl.append(dict(mint=mint, boundary=sell['boundary'], slot=sell['slot'],
                                     amount=p['closed_pnl_lamports'], reason='Position closed'))
                self.exposure.append(dict(mint=mint, boundary=sell['boundary'], slot=sell['slot'],
                    amount=-p['entry_cash_lamports']-p['failed_execution_cost_lamports'], reason='Position closed'))
            elif not p['entry_cash_lamports']:
                self.pnl.extend(dict(e, reason='Submitted failed-entry fee') for e in self.cash
                                if e['mint']==mint and e['amount']<0)
            causal = self.first_errors.get(mint, {}) if p['status']=='OPEN' else {}
            self.table.append(dict(token=p['symbol'].strip(), mint=mint, group=outcome(p), status=p['status'],
                final_reason=p['reason'], first_causal_blocker=causal.get('reason', ''),
                entry_slot=p.get('entry_slot'), exit_slot=p.get('exit_slot'),
                entry_utc=self.time(p['entry_slot']) if p.get('entry_slot') else None,
                exit_utc=self.time(p['exit_slot']) if p.get('exit_slot') else None,
                entry_sol=p['entry_cash_lamports']/LAMPORTS,
                closed_pnl_sol=p['closed_pnl_lamports']/LAMPORTS if p['status']=='CLOSED' else None,
                failed_execution_fees_sol=p['failed_execution_cost_lamports']/LAMPORTS,
                open_committed_cost_sol=(p['entry_cash_lamports']+p['failed_execution_cost_lamports'])/LAMPORTS if p['status']=='OPEN' else 0,
                detailed_native_log=bool(self.traces[mint]), gmgn_url=p['gmgn_url'],
                developer_gmgn='https://gmgn.ai/sol/address/'+p['creator'],
                tracked_buyer_gmgn=p.get('sniper_gmgn_url'), signal_slot=p['signal_slot']))
        self.table = pd.DataFrame(self.table).sort_values(['signal_slot', 'mint']).reset_index(drop=True)
        for column in ('entry_slot', 'exit_slot'):
            self.table[column] = pd.to_numeric(self.table[column]).astype('Int64')
        for name in ('cash', 'pnl', 'exposure'):
            getattr(self, name).sort(key=lambda r: r['boundary'])
        assert sum(r['amount'] for r in self.pnl)==self.summary['realized_net_pnl_lamports']
        assert sum(r['amount'] for r in self.exposure)==self.summary['open_cash_cost_lamports']
        assert sum(r['amount'] for r in self.cash)==self.summary['realized_net_pnl_lamports']-self.summary['open_cash_cost_lamports']
        self._tapes, self._inventory = {}, {}
        self.export = self.data/'csv'
        self.export.mkdir(exist_ok=True)
        self.export_tables()

    def time(self, slot):
        when = self.clock.get(int(slot))
        return pd.Timestamp(when, unit='s', tz='UTC').isoformat() if when is not None else 'No retained slot timestamp'

    def hover(self, ordinal, detail=''):
        slot, tx = int(ordinal) >> 32, (int(ordinal) & (2**32-1))-1
        return [str(slot), str(tx), self.time(slot), str(ordinal), detail]

    def summary_table(self):
        s = self.summary
        return pd.DataFrame([dict(date_utc='2026-10-12 00:00–24:00', wallet_sol=10000,
            dev_sell_all_required=False, signals=len(self.positions), filled=s['filled_entries'],
            profitable=int((self.table.group=='Profitable').sum()), losing=int((self.table.group=='Losing').sum()),
            unresolved=s['open_positions'], failed_entries=int((self.table.group=='Entry failed').sum()),
            risk_rejections=int((self.table.group=='Entry rejected').sum()),
            realized_net_pnl_sol=s['realized_net_pnl_sol'], open_committed_cost_sol=s['open_cash_cost_sol'],
            final_portfolio_pnl='Unknown — 80 unresolved', detailed_log_tokens=sum(bool(v) for v in self.traces.values()))])

    def export_tables(self):
        self.table.to_csv(self.export/'all_positions.csv', index=False)
        for group in GROUPS:
            self.table[self.table.group==group].to_csv(self.export/(group.lower().replace(' ', '_')+'.csv'), index=False)
        for name, values in [('native_fill_markers', self.markers), ('realized_pnl_events', self.pnl),
                             ('cash_events', self.cash), ('committed_cost_events', self.exposure)]:
            pd.DataFrame([{**r, 'boundary':str(r['boundary'])} for r in values]).to_csv(self.export/(name+'.csv'), index=False)

    def display_table(self, group=None):
        frame = self.table if group is None else self.table[self.table.group==group]
        fields = ['token', 'group', 'entry_slot', 'exit_slot', 'entry_sol', 'closed_pnl_sol',
                  'open_committed_cost_sol', 'failed_execution_fees_sol', 'first_causal_blocker', 'gmgn_url']
        display(HTML(frame[fields].to_html(index=False, escape=True, render_links=True,
            float_format=lambda v:f'{v:.9f}', na_rep='—')))

    def strategy_figure(self):
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=.11,
            subplot_titles=['Realized net PnL · unresolved positions excluded',
                'Cash change · available + reserved SOL, excludes token inventory',
                'Committed cost · exposure, not an assumed loss'])
        start, end = min(self.clock), max(self.clock)
        for panel, name, color in [(1, 'pnl', '#1768b3'), (2, 'cash', '#69738b'), (3, 'exposure', '#c58921')]:
            values = getattr(self, name)
            total, x, y, custom = 0, [0], [0], [self.hover(start << 32, 'Start')]
            for r in values:
                if r['amount']==0:
                    continue
                total += r['amount']
                x.append(r['slot']-start); y.append(total/LAMPORTS)
                custom.append(self.hover(r['boundary'], r['reason']+' · '+self.positions[r['mint']]['symbol']))
            x.append(end-start); y.append(total/LAMPORTS); custom.append(self.hover(end << 32, 'End of retained follow-up'))
            fig.add_trace(go.Scatter(x=x, y=y, customdata=custom, mode='lines', line_shape='hv',
                name=name, line_color=color, hovertemplate='Slot %{customdata[0]} · %{customdata[2]}<br>%{customdata[4]}<br>%{y:.9f} SOL<extra></extra>'), row=panel, col=1)
            fig.add_hline(y=0, line_color='#aaa', row=panel, col=1)
            fig.add_annotation(x=x[-1], y=y[-1], text=f'{y[-1]:+.9f} SOL', showarrow=False,
                               xanchor='right', yshift=12, row=panel, col=1)
        fig.update_yaxes(title_text='SOL')
        fig.update_xaxes(title_text=f'Slots since {start} · exact UTC and boundary in hover', row=3, col=1)
        fig.update_layout(template='plotly_white', height=880, showlegend=False, hovermode='x unified',
            title='October 12 · full day without dev-sell-all · 10,000 SOL wallet')
        return fig

    def tape(self, mint):
        if mint not in self._tapes:
            with gzip.open(self.data/'token-context'/(mint+'.json.gz'), 'rt') as stream:
                self._tapes[mint] = [dict(r, boundary=boundary(r['slot'], r['tx_idx'])) for r in json.load(stream) if r['kind']=='trade']
        return self._tapes[mint]

    def inventory(self, mint):
        if mint in self._inventory:
            return self._inventory[mint]
        p = self.positions[mint]
        overrides = defaultdict(deque)
        for r in self.repriced[mint]:
            overrides[(int(r['boundary_ordinal']), r['trader'], r['side'], int(r['historical_tokens']), int(r['historical_curve_sol']))].append(int(r['replayed_tokens']))
        groups, movements = defaultdict(list), defaultdict(list)
        for r in self.tape(mint):
            groups[r['boundary']].append(r)
        for r in self.transfers[mint]:
            movements[boundary(r['slot'], r['tx_idx'])].append(r)
        stop = int(self.first_errors.get(mint, {}).get('boundary_ordinal', 10**30))
        balances, records = defaultdict(int), []
        for ordinal in sorted(groups.keys() | movements.keys()):
            if ordinal >= stop:
                break
            for r in movements[ordinal]:
                balances[r['source_wallet']] -= int(r['tokens']); balances[r['destination_wallet']] += int(r['tokens'])
            for r in groups[ordinal]:
                key = ordinal, r['trader'], r['side'], int(r['tokens']), int(r['quote'])
                amount = overrides[key].popleft() if overrides[key] else int(r['tokens'])
                balances[r['trader']] += amount * (1 if r['side']=='buy' else -1)
            records.append(dict(boundary=ordinal, developer=balances[p['creator']]/10**12,
                tracked=balances[p['sniper_wallet']]/10**12, wallets=sum(v>0 for v in balances.values()),
                unproven=sum(v<0 for v in balances.values())))
        self._inventory[mint] = records
        return records

    def figure(self, mint):
        p, birth, tape = self.positions[mint], self.births[mint], self.tape(mint)
        trace = [r for r in self.traces[mint] if r['time'] <= birth['chart_end_unix']]
        fills = self.fills_by_mint[mint]
        orders = [r for r in self.orders.values() if r['mint']==mint]
        failed = [{**r, 'boundary':int(r['boundary_ordinal'])} for r in self.failures if self.orders[r['order_id']]['mint']==mint]
        inventory = self.inventory(mint)
        blocked = int(self.first_errors.get(mint, {}).get('boundary_ordinal', 0)) or None
        if blocked and self.clock.get(blocked >> 32, 10**30) > birth['chart_end_unix']:
            blocked = None
        ordinals = sorted({boundary(birth['slot'], birth['tx_idx']), *[r['boundary'] for r in tape],
            *[r['boundary'] for r in fills], *[int(r['decision_boundary']) for r in orders],
            *[r['boundary'] for r in failed], *[r['boundary_ordinal'] for r in trace],
            *[r['boundary'] for r in inventory], *([blocked] if blocked else [])})
        rank = {v:i for i,v in enumerate(ordinals)}
        fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=.075,
            specs=[[{}], [{}], [{'secondary_y':True}], [{}]], row_heights=[.34,.19,.23,.24],
            subplot_titles=['Historical marginal price and actual simulated average fill prices',
                'Real SOL reserves · historical and native conditional replay',
                'Developer / tracked buyer inventory · incomplete modeled wallet coverage',
                'Native exit evaluations and actual closed PnL · estimates are not fills'])
        template = 'Slot %{customdata[0]} · tx %{customdata[1]}<br>%{customdata[2]}<br>%{customdata[4]}<br>%{y:.9g}<extra>%{fullData.name}</extra>'

        def line(values, field, title, color, panel, dash=None, secondary=False):
            if not values:
                return
            values = compact_steps(values, field)
            args = dict(x=[rank[r['boundary']] for r in values], y=[r.get(field) for r in values],
                customdata=[self.hover(r['boundary'], r.get('detail', title)) for r in values],
                mode='lines', name=title, connectgaps=False, hovertemplate=template,
                line=dict(color=color, shape='hv', width=1.5, **({'dash':dash} if dash else {})))
            if panel==3:
                fig.add_trace(go.Scatter(**args), row=panel, col=1, secondary_y=secondary)
            else:
                fig.add_trace(go.Scatter(**args), row=panel, col=1)

        historical, old = [], (birth['initial_virtual_quote_raw'], birth['initial_virtual_base_raw'])
        discontinuities = 0
        for r in tape:
            sign = 1 if r['side']=='buy' else -1
            expected = old[0]+sign*r['quote'], old[1]-sign*r['tokens']
            actual = r['virtual_quote'], r['virtual_base']
            if max(abs(a-b) for a,b in zip(expected, actual))>1:
                historical.append(dict(boundary=r['boundary'], price=None, reserve=None, detail='Retained reserve discontinuity'))
                discontinuities += 1
            historical.append(dict(boundary=r['boundary'], price=r['virtual_quote']/r['virtual_base']/1000,
                reserve=r['real_quote']/LAMPORTS, detail=f"Historical {r['side']} · {r['signature']}"))
            old = actual
        line(historical, 'price', 'Historical marginal price', HIST, 1)
        line(historical, 'reserve', 'Historical real SOL', HIST, 2)
        modeled = [dict(boundary=r['boundary_ordinal'], price=int(r['virtual_sol'])/int(r['virtual_tokens'])/1000)
                   for r in trace if r.get('virtual_sol') and r.get('virtual_tokens')]
        line(modeled, 'price', 'Native state at retained evaluations', REPLAY, 1)
        conditional = [dict(boundary=int(r['boundary_ordinal']), reserve=int(r['real_sol_after'])/LAMPORTS)
                       for r in self.repriced[mint] if int(r['boundary_ordinal']) in rank]
        line(conditional, 'reserve', 'Native repriced real SOL after counterpart trades', REPLAY, 2)
        for side, symbol, color in [('BUY','triangle-up',GOOD), ('SELL','triangle-down',BAD)]:
            selected = [r for r in fills if r['side']==side]
            fig.add_trace(go.Scatter(x=[rank[r['boundary']] for r in selected], y=[r['average_curve_price_sol'] for r in selected],
                customdata=[self.hover(r['boundary'], f"{side} fill · {r['curve_sol_lamports']/LAMPORTS:.9f} curve SOL · {r['tokens_raw']/10**6:,.6f} tokens; fees shown in metadata") for r in selected],
                mode='markers', marker=dict(symbol=symbol, size=13, color=color, line=dict(color='white', width=1)),
                name=side+' fill · average curve price', hovertemplate=template), row=1, col=1)
        tape_bounds = [r['boundary'] for r in tape]

        def reference_price(ordinal):
            index = bisect_right(tape_bounds, ordinal)-1
            return tape[index]['virtual_quote']/tape[index]['virtual_base']/1000 if index>=0 else birth['initial_virtual_quote_raw']/birth['initial_virtual_base_raw']/1000

        for side, color in [('BUY', '#b88719'), ('SELL', BAD)]:
            selected = [r for r in orders if r['side']==side]
            fig.add_trace(go.Scatter(x=[rank[int(r['decision_boundary'])] for r in selected],
                y=[reference_price(int(r['decision_boundary'])) for r in selected],
                customdata=[self.hover(int(r['decision_boundary']), 'Entry signal' if side=='BUY' else 'Exit order submitted; historical reference price, not a fill') for r in selected],
                mode='markers', marker=dict(symbol='diamond-open', size=10, color=color),
                name=('Entry signal' if side=='BUY' else 'Exit decision')+' · historical reference', hovertemplate=template), row=1, col=1)
        fig.add_trace(go.Scatter(x=[rank[r['boundary']] for r in failed], y=[reference_price(r['boundary']) for r in failed],
            customdata=[self.hover(r['boundary'], r['reason']+'; no fill; historical reference price') for r in failed],
            mode='markers', marker=dict(symbol='x', size=12, color='#222'), name='Failed/rejected attempt · no fill', hovertemplate=template), row=1, col=1)
        line(inventory, 'developer', 'Developer · million tokens', '#333', 3)
        line(inventory, 'tracked', 'Tracked buyer · million tokens', '#239c99', 3)
        line(inventory, 'wallets', 'Observed external wallets with positive modeled inventory', '#9986b9', 3, secondary=True)
        checks = [dict(boundary=r['boundary_ordinal'], pnl=int(r['pnl_lamports'])/LAMPORTS)
                  for r in trace if r['action']=='EXIT_EVALUATION']
        line(checks, 'pnl', 'Native net liquidation estimate', '#1768b3', 4)
        if p['entry_cash_lamports']:
            target = ((p['entry_cash_lamports']-self.summary['policy']['account_rent_refund_lamports'])*self.summary['policy']['target_return_bps']+9999)//10000/LAMPORTS
            fig.add_hline(y=target, line_color=GOOD, line_dash='dot', row=4, col=1, exclude_empty_subplots=False)
        if p['status']=='CLOSED':
            sell = next(r for r in fills if r['side']=='SELL')
            fig.add_trace(go.Scatter(x=[rank[sell['boundary']]], y=[p['closed_pnl_lamports']/LAMPORTS],
                customdata=[self.hover(sell['boundary'], 'Closed net PnL · '+p['reason'])],
                mode='markers', marker=dict(symbol='diamond', size=11, color=GOOD if p['closed_pnl_lamports']>0 else BAD),
                name='Closed PnL · actual simulated exit', hovertemplate=template), row=4, col=1)
        if not checks:
            fig.add_annotation(x=.5, y=.65, xref='x domain', yref='y domain', row=4, col=1, showarrow=False,
                text='No detailed native exit evaluations retained for this token.<br>See saved fills, order audit and position metadata.', font=dict(size=11, color='#666'))
        fig.add_hline(y=0, line_color='#aaa', row=4, col=1, exclude_empty_subplots=False)
        if blocked is not None:
            for panel in (2,3,4):
                fig.add_vline(x=rank[blocked], line_color='#333', line_dash='dash', row=panel, col=1)
        ticks = list(range(0, len(ordinals), max(1, len(ordinals)//8)))
        fig.update_xaxes(tickvals=ticks, ticktext=[str((ordinals[i] >> 32)-p['launch_slot']) for i in ticks])
        fig.update_xaxes(title_text=f'Transaction order · ticks = slots since launch {p["launch_slot"]} · exact UTC/slot/boundary in hover', row=4, col=1)
        fig.update_yaxes(title_text='SOL / token', row=1, col=1)
        fig.update_yaxes(title_text='SOL', row=2, col=1)
        fig.update_yaxes(title_text='Million tokens', row=3, col=1, secondary_y=False)
        fig.update_yaxes(title_text='Observed wallets', row=3, col=1, secondary_y=True)
        fig.update_yaxes(title_text='Net SOL', row=4, col=1)
        reason = self.first_errors.get(mint, {}).get('reason', p['reason']) if p['status']=='OPEN' else p['reason']
        fig.update_layout(template='plotly_white', height=1350, hovermode='x unified',
            title=dict(text=f'{html.escape(p["symbol"])} · {outcome(p)}<br><sup>{html.escape(reason)} · {discontinuities} retained reserve discontinuities in chart window</sup>', font=dict(size=15)),
            legend=dict(orientation='h', y=-.13, font=dict(size=10)), margin=dict(t=120, b=240),
            meta=dict(mint=mint, buy_fills=sum(r['side']=='BUY' for r in fills), sell_fills=sum(r['side']=='SELL' for r in fills),
                      failed_attempts=len(failed), detailed_evaluations=len(checks), raw_boundaries='strings in hover; chart x uses small integer ranks'))
        return fig

    def metadata(self, mint):
        p = self.positions[mint]
        fields = ['status', 'reason', 'signal_slot', 'signal_time', 'entry_slot', 'entry_time', 'exit_signal_slot', 'exit_slot', 'exit_time',
            'budget_lamports', 'entry_cash_lamports', 'closed_pnl_lamports', 'exit_receipt_lamports', 'failed_execution_cost_lamports',
            'sniper_wallet', 'sniper_cost_lamports', 'sniper_trading_cash_cost_lamports', 'sniper_creator_fee_income_lamports',
            'sniper_is_launch_creator', 'sniper_tokens', 'signal_sniper_threshold', 'signal_our_threshold', 'exit_attempts']
        data = {k:p.get(k) for k in fields}
        data.update(first_causal_blocker=self.first_errors.get(mint, {}).get('reason') if p['status']=='OPEN' else None,
            detailed_native_log_retained=bool(self.traces[mint]),
            entry_rule='Original quadratic break-even window; no developer-flat requirement',
            exit_rule='Own +5% net take profit or 300 second timeout; one-slot execution',
            chart_history_end_utc=pd.Timestamp(self.births[mint]['chart_end_unix'], unit='s', tz='UTC').isoformat())
        return data

    def show(self, mint, logs=True):
        p = self.positions[mint]
        display(HTML(f'<h3>{html.escape(p["symbol"])} · {outcome(p)}</h3>'
            f'<a href="{p["gmgn_url"]}">Token on GMGN</a> · '
            f'<a href="https://gmgn.ai/sol/address/{p["creator"]}">Developer</a> · '
            f'<a href="{p["sniper_gmgn_url"]}">Tracked buyer</a>'))
        display(pd.Series(self.metadata(mint), name='Recorded native metadata').to_frame())
        display(self.figure(mint))
        if logs:
            orders = {r['order_id'] for r in self.orders.values() if r['mint']==mint}
            display(Markdown('**Complete order audit for this token**'))
            display(pd.DataFrame([r for r in self.audits if r['order_id'] in orders]))
            selected = [r for r in self.traces[mint] if r['action'] in ('ENTRY_SIGNAL','EXIT_EVALUATION') or r['action'].startswith('ORDER_')]
            if selected:
                display(Markdown('**Retained detailed evaluations: first and last rows; full log stays in native/strategy_trace.csv**'))
                display(pd.DataFrame(selected if len(selected)<24 else selected[:10]+selected[-14:]))
            else:
                display(Markdown('Detailed step logs were not retained for this token. The signal record, fills and order audit above are original saved evidence.'))

    def browser(self):
        import ipywidgets as w
        group = w.Dropdown(options=['All', *GROUPS], description='Outcome:')
        search = w.Text(description='Find:', placeholder='Token symbol or mint')
        token = w.Dropdown(description='Token:', layout=w.Layout(width='95%'))
        output = w.Output()

        def refresh(*_):
            frame = self.table
            if group.value!='All':
                frame = frame[frame.group==group.value]
            if search.value.strip():
                q = search.value.strip().lower()
                frame = frame[frame.token.str.lower().str.contains(q, regex=False) | frame.mint.str.lower().str.contains(q, regex=False)]
            token.options = [(f'{r.token} · {r.group} · {r.mint[:10]}', r.mint) for r in frame.itertuples()]

        def render(*_):
            with output:
                output.clear_output(wait=True)
                if token.value:
                    self.show(token.value)

        group.observe(refresh, names='value'); search.observe(refresh, names='value')
        token.observe(render, names='value'); refresh()
        return w.VBox([w.HBox([group, search]), token, output])
