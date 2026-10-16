"""Executed notebook cells for all native liquidity-shortfall cases."""


def add_trace_cells(md, code):
    md('''
    ## Every liquidity-shortfall case: native decisions, slots and observed inventory

    The new native reruns persist `strategy_trace.csv` and `order_audit.csv`.
    The old runs logged fills, ledger postings and order failures, but did not
    retain every entry candidate and exit preflight decision. These new traces
    reproduce the same token-level outcomes and costs, and are not inferred logs.

    This section covers **all 11** completed-run exits with a positive shortfall,
    including the seven strict-open positions. Historical wallet inventory means
    **observed buys minus observed sells**. Transfers and complete balance states
    are absent, so this is not an exact holder census. Negative observed inventory
    is retained as a coverage warning. Our hypothetical wallet is separate.

    Slot-end views include all retained transactions in that slot. The signal and
    landing checkpoints use the exact transaction boundary instead; they prevent
    later transactions in the same slot from leaking into entry explanations.
    ''')
    code('''
    from backtest.plugins.strategies.sniper_window import threshold
    from backtest.plugins.protocols.pumpfun.model import buy_quote
    from decimal import Decimal
    from bisect import bisect_right

    shortfall_mints = [m for m in filled_mints if rows_by_run['virtual'][m].get('exit_synthetic_shortfall_lamports', 0) > 0]
    assert len(shortfall_mints) == 11 and set(blocked_mints).issubset(shortfall_mints)
    trace_records = []
    audit_records = []
    for name, directory in RUN_DIRS.items():
        with (directory / 'strategy_trace.csv').open() as stream:
            records = list(csv.DictReader(stream))
        for record in records:
            for field in ['boundary_ordinal', 'slot', 'time']:
                record[field] = int(record[field])
            trace_records.append(dict(run=name, **record))
        with (directory / 'order_audit.csv').open() as stream:
            audit_records.extend(dict(run=name, **r) for r in csv.DictReader(stream))
    strategy_trace = pd.DataFrame(trace_records)
    native_order_audit = pd.DataFrame(audit_records)
    # Prove instrumentation preserved every saved per-token field and economic result.
    for name in RUN_DIRS:
        old_dir = ROOT / ('var/dnipro/sniper-window-20261010-creator-fees-' + name + '-v1')
        previous = json.loads((old_dir / 'result.json').read_text())
        assert previous['positions'] == runs[name]['positions']
        for field in ['realized_net_pnl_lamports', 'filled_entries', 'closed_positions', 'open_positions', 'synthetic_shortfall_lamports']:
            assert previous['summary'][field] == runs[name]['summary'][field]
    assert set(strategy_trace['mint']) == set(shortfall_mints)
    print(f'Native decision records: {len(strategy_trace):,}; native order audit records: {len(native_order_audit):,}.')

    def state_from(mint, observed=None):
        b = births[mint]
        x = int(observed['virtual_quote']) if observed else int(b['initial_virtual_quote_raw'])
        y = int(observed['virtual_base']) if observed else int(b['initial_virtual_base_raw'])
        real = int(observed['real_quote']) if observed else int(b.get('initial_real_quote_raw') or 0)
        real_y = int(observed['real_base']) if observed else int(b['initial_real_base_raw'])
        return PumpCurveStateV1(y, x, real_y, real, int(b['supply_raw']),
                               PumpCurveLifecycle.ACTIVE if real_y else PumpCurveLifecycle.COMPLETED,
                               PumpMode.TOKEN_2022 if b['token_program'].startswith('Tokenz') else PumpMode.NORMAL)

    def recorded_quote(mint, quantity, observed, now):
        if quantity <= 0 or observed is None:
            return None
        return sell_quote(state_from(mint, observed), tokens_in_atomic=quantity,
            fee_profile=profile((int(observed['payload']['fee_basis_points']), int(observed['payload']['creator_fee_basis_points'])), now),
            effective_at_unix_s=now,
            liquidity_policy=PumpSellLiquidityPolicy.VIRTUAL_RESERVE_OUTPUT_WITH_EXPLICIT_SYNTHETIC_SHORTFALL_V1)

    wallet_event_records, holder_slot_records, holder_checkpoint_records, slot_records, case_records, holder_economic_records = [], [], [], [], [], []
    for mint in shortfall_mints:
        p = rows_by_run['virtual'][mint]
        b = births[mint]
        anchor = p['sniper_wallet']
        buy = fill_by_run_mint['virtual'][mint]['BUY']
        sell = fill_by_run_mint['virtual'][mint]['SELL']
        signal_boundary = reservations['virtual'][buy['order_id']]
        entry_boundary = buy['boundary_ordinal']
        exit_boundary = sell['boundary_ordinal']
        checkpoints = [('signal_after_transaction', signal_boundary, True), ('buy_landing_before_slot_trades', entry_boundary, False), ('exit_landing_before_slot_trades', exit_boundary, False)]
        ledgers = defaultdict(lambda: {'tokens': 0, 'cost': 0, 'bought': 0})
        paid = set()
        income = CreatorFeeIncome()
        last = None
        snapshots = []
        boundaries = []
        tape = trades_by_mint[mint]
        for trade in tape:
            w = trade['trader']
            before = dict(ledgers[w])
            fee = 0
            if trade['fee_payer'] == w and trade['signature'] not in paid and trade.get('network_fee') is not None:
                fee = int(trade['network_fee'])
                paid.add(trade['signature'])
            charges = int(trade['protocol_fee']) + int(trade['creator_fee'])
            before_adjusted_cost = before['cost'] - income.income(mint, w)
            full_before = recorded_quote(mint, before['tokens'], last, trade['time']) if trade['side'] == 'sell' else None
            sign = 1 if trade['side'] == 'buy' else -1
            ledgers[w]['tokens'] += sign * int(trade['tokens'])
            ledgers[w]['cost'] += int(trade['quote']) + charges + fee if sign == 1 else -(int(trade['quote']) - charges - fee)
            if sign == 1:
                ledgers[w]['bought'] += int(trade['quote']) + charges
            income.observe(trade['event_uid'], trade)
            if trade['side'] == 'sell':
                net_receipt = int(trade['quote']) - charges - fee
                full_pnl = full_before.sol_out_lamports - fee - before_adjusted_cost if full_before else None
            else:
                net_receipt = None
                full_pnl = None
            wallet_event_records.append({
                'token': p['symbol'], 'mint': mint, 'gmgn_url': p['gmgn_url'],
                'slot': int(trade['slot']), 'tx_idx': int(trade['tx_idx']), 'boundary_ordinal': trade['boundary'],
                'time_UTC': utc(trade['time']), 'event_uid': trade['event_uid'], 'signature': trade['signature'],
                'wallet': w, 'wallet_gmgn': 'https://gmgn.ai/sol/address/' + w,
                'is_anchor': w == anchor, 'is_launch_creator': w == p['creator'], 'side': trade['side'],
                'tokens_raw': int(trade['tokens']), 'curve_quote_lamports': int(trade['quote']),
                'protocol_fee_lamports': int(trade['protocol_fee']), 'creator_fee_lamports': int(trade['creator_fee']),
                'attributed_network_fee_lamports': fee, 'net_sell_receipt_lamports': net_receipt,
                'observed_tokens_before': before['tokens'], 'observed_tokens_after': ledgers[w]['tokens'],
                'observed_cash_cost_after': ledgers[w]['cost'],
                'creator_fee_accrual_after': income.income(mint, w),
                'full_exit_pnl_at_pretrade_state_lamports': full_pnl,
                'sold_entire_observed_position': sign == -1 and int(trade['tokens']) == before['tokens'],
                'closed_observed_cashflow_lamports': -ledgers[w]['cost'] if ledgers[w]['tokens'] == 0 else None,
                'coverage_negative_inventory': ledgers[w]['tokens'] < 0,
            })
            last = trade
            assert sum(value['tokens'] for value in ledgers.values()) == int(b['initial_virtual_base_raw'])-int(trade['virtual_base']), (mint,trade['event_uid'],'observed inventory conservation')
            boundaries.append(trade['boundary'])
            snapshots.append({w: dict(value, creator_income=income.income(mint, w)) for w, value in ledgers.items()})
        assert len(boundaries) == len(snapshots)

        def snapshot_at(boundary, inclusive):
            i = (bisect_right(boundaries, boundary) if inclusive else bisect_left(boundaries, boundary)) - 1
            return (snapshots[i], tape[i]) if i >= 0 else ({}, None)

        for name, boundary, inclusive in checkpoints:
            snapshot, observed = snapshot_at(boundary, inclusive)
            for w, ledger in snapshot.items():
                holder_checkpoint_records.append(dict(
                    token=p['symbol'], mint=mint, gmgn_url=p['gmgn_url'], checkpoint=name,
                    boundary_ordinal=boundary, slot=boundary >> 32, wallet=w,
                    wallet_gmgn='https://gmgn.ai/sol/address/' + w, is_anchor=w == anchor,
                    is_launch_creator=w == p['creator'], observed_tokens_raw=ledger['tokens'],
                    supply_pct=ledger['tokens'] / int(b['supply_raw']) * 100,
                    observed_cash_cost_lamports=ledger['cost'], creator_fee_accrual_lamports=ledger['creator_income'],
                    coverage_negative_inventory=ledger['tokens'] < 0))
            observed_state = state_from(mint, observed)
            checkpoint_bps = (int(observed['payload']['fee_basis_points']), int(observed['payload']['creator_fee_basis_points']))
            if name == 'signal_after_transaction':
                our_quote = buy_quote(observed_state, spendable_gross_sol_lamports=p['budget_lamports'],
                    fee_profile=profile(checkpoint_bps,p['signal_time']), effective_at_unix_s=p['signal_time'])
                own_q, deposit = our_quote.tokens_out_atomic, our_quote.net_curve_sol_lamports
            else:
                own_q = buy['amount_out_atomic']
                deposit = sum(posting['amount_atomic'] for posting in settlements['virtual'][buy['order_id']]['postings'] if posting['account_kind']=='VENUE' and posting['asset_id']==SOL)
            if name != 'exit_landing_before_slot_trades':
                hypothetical = PumpCurveStateV1(observed_state.virtual_token_reserves_atomic-own_q,
                    observed_state.virtual_sol_reserves_lamports+deposit,
                    observed_state.real_token_reserves_atomic-own_q,observed_state.real_sol_reserves_lamports+deposit,
                    observed_state.token_total_supply_atomic,observed_state.lifecycle,observed_state.mode)
                for w, ledger in snapshot.items():
                    net_cost = ledger['cost']-ledger['creator_income']
                    amount = ledger['tokens']
                    q = sell_quote(hypothetical,tokens_in_atomic=amount,
                        fee_profile=profile(checkpoint_bps,p['signal_time']),effective_at_unix_s=p['signal_time'],
                        liquidity_policy=PumpSellLiquidityPolicy.VIRTUAL_RESERVE_OUTPUT_WITH_EXPLICIT_SYNTHETIC_SHORTFALL_V1) if amount>0 else None
                    break_even = threshold(hypothetical,amount,net_cost+105000,checkpoint_bps) if amount>0 and net_cost+105000>0 else None
                    hypothetical_pnl = q.sol_out_lamports-net_cost-105000 if q else None
                    holder_economic_records.append(dict(token=p['symbol'],mint=mint,gmgn_url=p['gmgn_url'],checkpoint=name,
                        slot=boundary>>32,wallet=w,wallet_gmgn='https://gmgn.ai/sol/address/'+w,is_anchor=w==anchor,
                        is_launch_creator=w==p['creator'],observed_tokens_raw=amount,supply_pct=amount/int(b['supply_raw'])*100,
                        historical_trade_cashflow_lamports=-ledger['cost'],historical_creator_fee_accrual_lamports=ledger['creator_income'],
                        historical_cashflow_plus_fee_accrual_lamports=-net_cost,
                        hypothetical_full_exit_pnl_after_our_buy_lamports=hypothetical_pnl,
                        hypothetical_exit_shortfall_after_our_buy_lamports=q.synthetic_shortfall_lamports if q else None,
                        profitable_full_exit_after_our_buy=q is not None and hypothetical_pnl>=0 and q.synthetic_shortfall_lamports==0,
                        profitable_threshold_below_our_target=amount>0 and (break_even is None or break_even<Decimal(p['signal_our_threshold'])),
                        wallet_break_even_virtual_SOL=float(break_even)/LAMPORTS if break_even is not None else None,
                        our_signal_target_virtual_SOL=float(p['signal_our_threshold'])/LAMPORTS,
                        assumed_exit_network_fee_lamports=105000,scenario='our buy applied once; other orders unchanged'))
            if name == 'signal_after_transaction':
                assert snapshot[anchor]['tokens'] == p['sniper_tokens']
                assert snapshot[anchor]['cost'] - snapshot[anchor]['creator_income'] == p['sniper_cost_lamports']
            if name == 'buy_landing_before_slot_trades':
                entry_snapshot, entry_observed = snapshot, observed

        entry_state = state_from(mint, entry_observed)
        # Obtain integer deposit directly from the native ledger, not the display float.
        own_deposit = sum(posting['amount_atomic'] for posting in settlements['virtual'][buy['order_id']]['postings'] if posting['account_kind'] == 'VENUE' and posting['asset_id'] == SOL)
        after_entry = PumpCurveStateV1(entry_state.virtual_token_reserves_atomic - buy['amount_out_atomic'],
            entry_state.virtual_sol_reserves_lamports + own_deposit,
            entry_state.real_token_reserves_atomic - buy['amount_out_atomic'],
            entry_state.real_sol_reserves_lamports + own_deposit,
            entry_state.token_total_supply_atomic, entry_state.lifecycle, entry_state.mode)
        entry_ledger = entry_snapshot.get(anchor, {'tokens': 0, 'cost': 0, 'creator_income': 0})
        bps = (int(entry_observed['payload']['fee_basis_points']), int(entry_observed['payload']['creator_fee_basis_points']))
        needed = ((p['entry_cash_lamports'] - runs['virtual']['summary']['policy']['account_rent_refund_lamports']) * 10500 + 9999) // 10000 + 105000
        xs_entry = threshold(after_entry, entry_ledger['tokens'], entry_ledger['cost'] - entry_ledger['creator_income'], bps) if min(entry_ledger['tokens'], entry_ledger['cost'] - entry_ledger['creator_income']) > 0 else None
        xt_entry = threshold(after_entry, buy['amount_out_atomic'], needed, bps)
        epsilon = runs['virtual']['summary']['policy']['epsilon_lamports']
        completion = Decimal(after_entry.virtual_sol_reserves_lamports * after_entry.virtual_token_reserves_atomic) / (after_entry.virtual_token_reserves_atomic - after_entry.real_token_reserves_atomic)
        window_at_fill = xs_entry is not None and xs_entry - xt_entry >= epsilon and xs_entry > after_entry.virtual_sol_reserves_lamports and xt_entry < completion
        one_token_events = [r for r in wallet_event_records if r['mint'] == mint]
        anchor_sells = [r for r in one_token_events if r['is_anchor'] and r['side'] == 'sell']
        first_sell = anchor_sells[0] if anchor_sells else None
        prefill_sells = [r for r in one_token_events if r['side'] == 'sell' and signal_boundary < r['boundary_ordinal'] < entry_boundary]
        first_full_loss = next((r for r in anchor_sells if r['sold_entire_observed_position'] and r['closed_observed_cashflow_lamports'] is not None and r['closed_observed_cashflow_lamports'] < 0), None)
        # One state per retained slot; historical state/inventory carry forward explicitly.
        wanted_slots = [s for s in sorted(clock) if int(b['slot']) <= s <= int(p['exit_slot']) + 2]
        changed_slots = {int(r['slot']) for r in tape}
        for slot in wanted_slots:
            snapshot, observed = snapshot_at(((slot + 1) << 32), False)
            state = state_from(mint, observed)
            holding = snapshot.get(anchor, {'tokens': 0, 'cost': 0, 'creator_income': 0})
            positives = sorted([value['tokens'] for value in snapshot.values() if value['tokens'] > 0], reverse=True)
            full = recorded_quote(mint, buy['amount_out_atomic'], observed, clock[slot]) if slot >= p['entry_slot'] else None
            pnl = full.sol_out_lamports + 2039280 - 105000 - p['entry_cash_lamports'] - p['failed_execution_cost_lamports'] if full else None
            slot_records.append(dict(token=p['symbol'], mint=mint, slot=slot, relative_slot=slot-int(b['slot']),
                time_UTC=utc(clock[slot]), view='end_of_slot', observed_trade_this_slot=slot in changed_slots,
                last_observed_trade_slot=int(observed['slot']) if observed else None,
                virtual_sol_lamports=state.virtual_sol_reserves_lamports, real_sol_lamports=state.real_sol_reserves_lamports,
                virtual_tokens_raw=state.virtual_token_reserves_atomic,
                anchor_tokens_raw=holding['tokens'], anchor_cash_cost_lamports=holding['cost'], anchor_fee_accrual_lamports=holding['creator_income'],
                positive_observed_wallets=len(positives), negative_observed_wallets=sum(v['tokens'] < 0 for v in snapshot.values()),
                top1_observed_supply_pct=sum(positives[:1])/int(b['supply_raw'])*100,
                top3_observed_supply_pct=sum(positives[:3])/int(b['supply_raw'])*100,
                hypothetical_full_sell_gross_lamports=full.gross_curve_sol_lamports if full else None,
                hypothetical_full_sell_pnl_lamports=pnl,
                synthetic_shortfall_lamports=full.synthetic_shortfall_lamports if full else None))
            if slot in changed_slots or slot in [p['signal_slot'], p['entry_slot'], p['exit_slot']]:
                for w, ledger in snapshot.items():
                    holder_slot_records.append(dict(token=p['symbol'], mint=mint, slot=slot, view='end_of_slot', wallet=w,
                        wallet_gmgn='https://gmgn.ai/sol/address/'+w, is_anchor=w == anchor, is_launch_creator=w == p['creator'],
                        observed_tokens_raw=ledger['tokens'], supply_pct=ledger['tokens']/int(b['supply_raw'])*100,
                        observed_cash_cost_lamports=ledger['cost'], creator_fee_accrual_lamports=ledger['creator_income'], coverage_negative_inventory=ledger['tokens'] < 0))
        profitable_others = [r for r in holder_economic_records if r['mint']==mint and r['checkpoint']=='signal_after_transaction' and not r['is_anchor'] and r['profitable_full_exit_after_our_buy']]
        case_records.append(dict(
            other_profitable_wallets_after_our_signal_buy=len(profitable_others),
            other_profitable_wallets_supply_pct=sum(r['supply_pct'] for r in profitable_others),
            token=p['symbol'], mint=mint, gmgn_url=p['gmgn_url'], strict_status=rows_by_run['strict'][mint]['status'],
            launch_slot=int(b['slot']), launch_UTC=utc(clock[int(b['slot'])]), signal_slot=p['signal_slot'],
            signal_tx_idx=(signal_boundary & ((1 << 32)-1))-1,
            entry_slot=p['entry_slot'], entry_age_seconds=p['entry_time']-clock[int(b['slot'])],
            entry_cost_SOL=p['entry_cash_lamports']/LAMPORTS, anchor_wallet=anchor,
            anchor_gmgn='https://gmgn.ai/sol/address/'+anchor,
            signal_window_margin_SOL=float(Decimal(p['signal_sniper_threshold'])-Decimal(p['signal_our_threshold']))/LAMPORTS,
            entry_window_still_valid=window_at_fill,
            entry_window_margin_SOL=float(xs_entry-xt_entry)/LAMPORTS if xs_entry is not None else None,
            sells_between_signal_and_landing=len(prefill_sells),
            anchor_first_sell_slot=first_sell['slot'] if first_sell else None,
            anchor_first_sell_relative_slot=first_sell['slot']-int(b['slot']) if first_sell else None,
            anchor_first_sell_pretrade_full_exit_pnl_SOL=first_sell['full_exit_pnl_at_pretrade_state_lamports']/LAMPORTS if first_sell and first_sell['full_exit_pnl_at_pretrade_state_lamports'] is not None else None,
            anchor_closed_observed_cashflow_SOL=first_full_loss['closed_observed_cashflow_lamports']/LAMPORTS if first_full_loss else None,
            anchor_full_loss_sale_observed=first_full_loss is not None,
            modeled_net_pnl_SOL=p['closed_pnl_lamports']/LAMPORTS,
            exit_slot=p['exit_slot'], exit_reason=p['reason'],
            exit_required_gross_SOL=p['exit_gross_curve_lamports']/LAMPORTS,
            exit_historical_real_SOL=p['exit_observed_real_sol_lamports']/LAMPORTS,
            exit_shortfall_SOL=p['exit_synthetic_shortfall_lamports']/LAMPORTS,
            own_ignored_curve_deposit_SOL=own_deposit/LAMPORTS,
            shortfall_exceeds_own_ignored_deposit=p['exit_synthetic_shortfall_lamports']>own_deposit,
        ))

    holder_economics = pd.DataFrame(holder_economic_records)
    cases = pd.DataFrame(case_records)
    wallet_events = pd.DataFrame(wallet_event_records)
    holder_slots = pd.DataFrame(holder_slot_records)
    holder_checkpoints = pd.DataFrame(holder_checkpoint_records)
    slot_states = pd.DataFrame(slot_records)
    assert len(cases) == 11
    linked_table(cases[['token','gmgn_url','signal_slot','signal_tx_idx','entry_slot','entry_age_seconds','sells_between_signal_and_landing','entry_window_still_valid','anchor_full_loss_sale_observed','modeled_net_pnl_SOL']])
    linked_table(cases[['token','gmgn_url','other_profitable_wallets_after_our_signal_buy','other_profitable_wallets_supply_pct','anchor_closed_observed_cashflow_SOL']])
    print('All eleven signal margins (SOL):', cases['signal_window_margin_SOL'].tolist())
    print('Entry window fails when recomputed at landing:', int((~cases['entry_window_still_valid']).sum()))
    print('Anchor full loss sale observed:', int(cases['anchor_full_loss_sale_observed'].sum()))
    ''')
    md('''
    ## The developer and other profitable wallets are different from the reference buyer

    The strategy credits observed creator fees to each wallet's break-even cost.
    However, it accepts an entry if **one eligible early buyer** passes the sizing
    formula. It does not require every holder to stay below break-even, nor veto
    cheaper holders whose sale is already profitable. The developer can therefore
    make money while the chosen reference buyer loses money.

    The table below uses actual historical wallet cashflow and accrued creator
    fees. The two hypothetical columns apply our buy once to the current curve
    and ask whether each remaining position could already sell profitably.
    They assume a 0.000105 SOL exit network fee and do not predict reactions.
    Accrual is an estimated receivable, not a confirmed withdrawal.
    ''')
    code('''
    example = 'J1sN6aVejzKWXPUmMc2u7RNhS5b9Xs5c6PXfrkoQpump'
    e = holder_economics[(holder_economics['mint']==example) & (holder_economics['checkpoint']=='signal_after_transaction')].copy()
    for raw,label in [('historical_trade_cashflow_lamports','historical_trade_cashflow_SOL'),('historical_creator_fee_accrual_lamports','accrued_creator_fee_SOL'),('hypothetical_full_exit_pnl_after_our_buy_lamports','hypothetical_exit_PnL_after_our_buy_SOL')]:
        e[label] = e[raw]/LAMPORTS
    display(e[['wallet_gmgn','is_launch_creator','is_anchor','supply_pct','historical_trade_cashflow_SOL','accrued_creator_fee_SOL','hypothetical_exit_PnL_after_our_buy_SOL','wallet_break_even_virtual_SOL','our_signal_target_virtual_SOL','profitable_full_exit_after_our_buy']])
    ec = cases[cases['mint']==example].iloc[0]
    display(Markdown(f"For [DEGENTOWNFUN]({rows_by_run['virtual'][example]['gmgn_url']}), the native signal was created **after tx {int(ec['signal_tx_idx'])} in slot {int(ec['signal_slot'])}**. This is after another early wallet sold profitably, not immediately after the reference wallet's buy. The creator still held tokens at that decision. **{int(ec['other_profitable_wallets_after_our_signal_buy'])} other observed wallet(s)** could already make a profitable full exit in the isolated post-buy quote diagnostic."))
    display(developers[developers['mint']==example][['developer_gmgn','dev_historical_trade_cashflow_SOL','dev_historical_creator_fee_accrual_SOL','dev_cashflow_plus_accrual_with_our_fees_SOL']])
    ''')
    md('''
    ## Slot charts for all eleven cases

    Each token has an early transaction view, observed wallet inventory, and the
    full holding-period liquidity check. Early transaction x positions include
    a fraction within the slot ordered by transaction index. Dashed vertical
    markers show our signal, assumed next-slot buy, and exit. The price line is
    recorded virtual SOL reserves, the variable used by the strategy's thresholds;
    horizontal thresholds were calculated at the signal's hypothetical post-buy state.

    Wallet balances here are observed inventory as a percentage of total supply.
    Curves and inventories carried between observations are explicitly modeled
    carry-forward states. End-of-slot inventories include transactions after a
    buy landing at the beginning of that slot; use the boundary checkpoint CSV
    for the causal position at entry.
    ''')
    code('''
    for mint in shortfall_mints:
        p = rows_by_run['virtual'][mint]
        launch = int(births[mint]['slot'])
        tape = trades_by_mint[mint]
        events = wallet_events[wallet_events['mint'] == mint]
        slots = slot_states[slot_states['mint'] == mint]
        buy = fill_by_run_mint['virtual'][mint]['BUY']
        signal_boundary = reservations['virtual'][buy['order_id']]
        max_tx = {s: max(r['tx_idx'] for r in tape if r['slot'] == s) + 2 for s in {r['slot'] for r in tape}}
        def tx_x(slot, index):
            return slot - launch + (index + 1) / max_tx[slot]
        signal_slot, signal_tx = signal_boundary >> 32, (signal_boundary & ((1 << 32)-1))-1
        signal_x = tx_x(signal_slot, signal_tx)
        early = [r for r in tape if r['slot'] <= launch + 25]
        fig, axes = plt.subplots(2, 2, figsize=(16, 9), layout='constrained')
        ax = axes[0, 0]
        ax.step([0]+[tx_x(r['slot'],r['tx_idx']) for r in early],
                [int(births[mint]['initial_virtual_quote_raw'])/LAMPORTS]+[int(r['virtual_quote'])/LAMPORTS for r in early], where='post', color='#555', label='Recorded virtual SOL')
        for side, marker, color in [('buy','^','#48875e'),('sell','v','#b84040')]:
            selected = [r for r in early if r['trader']==p['sniper_wallet'] and r['side']==side]
            ax.scatter([tx_x(r['slot'],r['tx_idx']) for r in selected], [int(r['virtual_quote'])/LAMPORTS for r in selected], marker=marker, color=color, s=75, label='Reference '+side, zorder=4)
        ax.axhline(float(p['signal_sniper_threshold'])/LAMPORTS, linestyle=':', color='#b84040', label='Reference break-even at signal')
        ax.axhline(float(p['signal_our_threshold'])/LAMPORTS, linestyle='--', color='#48875e', label='Our +5% threshold at signal')
        dev = holder_economics[(holder_economics['mint']==mint) & (holder_economics['checkpoint']=='signal_after_transaction') & holder_economics['is_launch_creator']]
        if len(dev) and pd.notna(dev.iloc[0]['wallet_break_even_virtual_SOL']):
            ax.axhline(dev.iloc[0]['wallet_break_even_virtual_SOL'],color='#c07824',linestyle='-.',label='Creator fee-adjusted break-even')
        ax.axvline(signal_x, color='#9b57aa', linestyle='--', label='Signal after tx')
        ax.axvline(p['entry_slot']-launch, color='#1766a7', linestyle='--', label='Our assumed buy landing')
        ax.set(xlabel=f'Slots after launch {launch} (fraction orders transactions)', ylabel='Virtual SOL', title='Entry and actual early-buyer transactions', xlim=(-.5,25.5))
        ax.legend(fontsize=7)
        ax = axes[0, 1]
        h = holder_slots[(holder_slots['mint']==mint) & (holder_slots['slot']<=launch+25)]
        top = h.groupby('wallet')['supply_pct'].max().sort_values(ascending=False).head(5).index.tolist()
        if p['sniper_wallet'] not in top:
            top.append(p['sniper_wallet'])
        if p['creator'] not in top and p['creator'] in h['wallet'].values:
            top.append(p['creator'])
        for wallet in top:
            w = h[h['wallet']==wallet].sort_values('slot')
            label = wallet[:6]+'…'+wallet[-4:] + (' reference' if wallet==p['sniper_wallet'] else '') + (' creator' if wallet==p['creator'] else '')
            ax.step([0]+(w['slot']-launch).tolist(), [0]+w['supply_pct'].tolist(), where='post', label=label, linewidth=2 if wallet==p['sniper_wallet'] else 1)
        ax.axvline(p['entry_slot']-launch, color='#1766a7', linestyle='--')
        ax.set(xlabel='Slots after launch; inventories at END of slot', ylabel='Observed inventory / supply (%)', title='Top observed wallets, reference and creator', xlim=(-.5,25.5))
        ax.legend(fontsize=7)
        ax = axes[1, 0]
        ax.step(slots['relative_slot'],slots['real_sol_lamports']/LAMPORTS, where='post', label='Historical actual SOL', color='#1766a7')
        ax.step(slots['relative_slot'],slots['hypothetical_full_sell_gross_lamports']/LAMPORTS, where='post', label='Gross SOL for our full sell', color='#c07824')
        ax.axvline(p['entry_slot']-launch,color='#1766a7',linestyle='--')
        ax.axvline(p['exit_slot']-launch,color='#b84040',linestyle='--',label='Completed-replay exit')
        ax.set(xlabel='Slots after launch; historical state carried forward', ylabel='SOL', title='Full holding window: recorded reserve shortfall')
        ax.legend(fontsize=8)
        ax = axes[1, 1]
        ax.step(slots['relative_slot'],slots['hypothetical_full_sell_pnl_lamports']/LAMPORTS, where='post', label='Modeled full-liquidation PnL', color='#784c99')
        ax.axhline(0,color='#777',linewidth=1)
        target = ((p['entry_cash_lamports']-2039280)*500+9999)//10000
        ax.axhline(target/LAMPORTS,color='#16875c',linestyle=':',label='Configured net +5% target')
        ax.axvline(p['exit_slot']-launch,color='#b84040',linestyle='--')
        ax.set(xlabel='Slots after launch; END of slot', ylabel='SOL', title='Executable quote value vs target (virtual settlement)')
        ax.legend(fontsize=8)
        for ax in axes.flat:
            ax.grid(alpha=.18)
        fig.suptitle(f"{p['symbol']} — {mint}\\nModel net PnL {p['closed_pnl_lamports']/LAMPORTS:+.6f} SOL; not an observed trade", fontsize=12)
        fig.savefig(EXPORT/f'slot_trace_{mint}.png',dpi=150)
        plt.show()
        details = cases[cases['mint']==mint].iloc[0]
        display(Markdown(f"[{p['symbol']}]({p['gmgn_url']}): signal slot **{p['signal_slot']}**, tx **{int(details['signal_tx_idx'])}**; buy slot **{p['entry_slot']}**. Sells before landing: **{int(details['sells_between_signal_and_landing'])}**. Entry window still passes at landing: **{bool(details['entry_window_still_valid'])}**. Reference full-position loss sale observed: **{bool(details['anchor_full_loss_sale_observed'])}**."))
        relevant = strategy_trace[(strategy_trace['run']=='virtual') & (strategy_trace['mint']==mint) & (strategy_trace['action'].isin(['ENTRY_CANDIDATE','ENTRY_SIGNAL','ORDER_ACCEPTED','ORDER_FILLED','ORDER_FAILED','ORDER_REJECTED']))]
        display(relevant[['slot','boundary_ordinal','time','action','status','wallet','wallet_tokens','wallet_adjusted_cost','budget_lamports']])
        display(events[(events['is_anchor'] | events['is_launch_creator'])][['slot','tx_idx','wallet','side','tokens_raw','observed_tokens_after','closed_observed_cashflow_lamports','full_exit_pnl_at_pretrade_state_lamports']])
    ''')
    md('''
    ## Does waiting one second remove the problem?

    This is a **separate native strategy run**, with the minimum age changed to
    one second, the same two-slot early-buyer window, and the same one-slot order
    latency. It rechecks the current observed wallet costs and curve state after
    waiting. Original baseline results remain preserved. Block times have integer
    second resolution, so this tests timestamp age, not a guaranteed 1000 ms
    wall-clock delay. A token can age out of the two-slot window before it becomes
    eligible: such suppression is reported, not silently removed from the sample.

    The profitable subset below is selected using **exit-time shortfall**. It is
    an explanatory hindsight calculation, not an entry filter or a backtest of
    a deployable rule. Removing positions changes capital availability as well.
    ''')
    code('''
    wait_dir = ROOT / 'var/dnipro/sniper-window-20261010-wait-one-second-virtual-v1'
    wait_manifest = json.loads((wait_dir/'manifest.json').read_text())
    assert wait_manifest['complete']
    for filename,digest in wait_manifest['files'].items():
        assert sha(wait_dir/filename)==digest,filename
    wait_run = json.loads((wait_dir/'result.json').read_text())
    assert wait_run['summary']['source_input_sha256']==runs['virtual']['summary']['source_input_sha256']
    wait_by_mint = {r['mint']:r for r in wait_run['positions']}
    wait_comparison = pd.DataFrame([dict(token=cases[cases['mint']==mint]['token'].iloc[0], mint=mint,
        gmgn_url=rows_by_run['virtual'][mint]['gmgn_url'], baseline_pnl_SOL=rows_by_run['virtual'][mint]['closed_pnl_lamports']/LAMPORTS,
        waited_status=wait_by_mint[mint]['status'], waited_reason=wait_by_mint[mint]['reason'],
        waited_signal_slot=wait_by_mint[mint].get('signal_slot'), waited_entry_slot=wait_by_mint[mint].get('entry_slot'),
        waited_pnl_SOL=wait_by_mint[mint]['closed_pnl_lamports']/LAMPORTS if wait_by_mint[mint].get('closed_pnl_lamports') is not None else np.nan) for mint in shortfall_mints])
    linked_table(wait_comparison)
    wait_summary = wait_run['summary']
    display(pd.DataFrame([dict(run=name,signals=s['signals'],filled=s['filled_entries'],closed=s['closed_positions'],net_modeled_pnl_SOL=s['realized_net_pnl_lamports']/LAMPORTS,shortfall_positions=s['synthetic_shortfall_positions']) for name,s in [('baseline',runs['virtual']['summary']),('minimum_age_one_second',wait_summary)]]))
    pnl_without_shortfall = sum(r['closed_pnl_lamports'] for r in runs['virtual']['positions'] if r['status']=='CLOSED' and not r.get('exit_synthetic_shortfall_lamports',0))-runs['virtual']['summary']['failed_entry_cost_lamports']
    investigation = dict(cases=len(cases), entry_windows_invalid_at_landing=int((~cases['entry_window_still_valid']).sum()),
        anchors_with_full_loss_sale=int(cases['anchor_full_loss_sale_observed'].sum()),
        cases_with_profitable_other_holders_at_signal=int((cases['other_profitable_wallets_after_our_signal_buy']>0).sum()),
        cases_with_sells_before_our_landing=int((cases['sells_between_signal_and_landing']>0).sum()),
        shortfalls_greater_than_ignored_own_deposit=int(cases['shortfall_exceeds_own_ignored_deposit'].sum()),
        hindsight_nonshortfall_pnl_lamports=pnl_without_shortfall,
        baseline_net_modeled_pnl_lamports=runs['virtual']['summary']['realized_net_pnl_lamports'],
        waited_net_modeled_pnl_lamports=wait_summary['realized_net_pnl_lamports'],
        waited_filled=wait_summary['filled_entries'], waited_shortfall_positions=wait_summary['synthetic_shortfall_positions'],
        exact_holder_coverage=False, order_landing_assumed=True, counterfactual_market_impact_propagated=False)
    display(Markdown(f"The ex-post nonshortfall subset, after all baseline failed-entry fees, is **{pnl_without_shortfall/LAMPORTS:+.9f} SOL**. It does not establish an observable entry rule."))
    print(json.dumps(investigation,indent=2))
    display(Markdown(f"**Entry explanation:** all {len(cases)} selected signals passed the reference buyer formula with approximately 0.001 SOL reserve margin. In all {int((cases['other_profitable_wallets_after_our_signal_buy']>0).sum())} cases, another observed holder could already sell profitably in an isolated post-buy quote. No all-holder break-even condition is enforced. **{int((~cases['entry_window_still_valid']).sum())}** windows also failed a fresh calculation at landing (CRD, MUNA and TOGA), while the order's minimum-output condition still allowed a fill. Historical reference wallets subsequently sold their observed positions at losses; this does not establish how they would react to our additional buy."))
    display(Markdown(f"**Liquidity interpretation:** all eleven exit shortfalls were smaller than the SOL deposit that this replay omits from later states. Adding that deposit alone would cover the recorded cap, but is not a consistent counterfactual replay: it must also update token reserves and reprice subsequent orders. Neither a price chart nor these reserve diagnostics prove that a real sell would fail. **Waiting one timestamp second** produced {wait_summary['filled_entries']} fills and {wait_summary['realized_net_pnl_lamports']/LAMPORTS:+.6f} SOL modeled net PnL; it did not validate a profitable strategy."))
    strategy_trace.to_csv(EXPORT/'strategy_decision_trace.csv',index=False)
    native_order_audit.to_csv(EXPORT/'native_order_audit.csv',index=False)
    cases.to_csv(EXPORT/'all_liquidity_shortfall_cases.csv',index=False)
    wallet_events.to_csv(EXPORT/'shortfall_wallet_event_log.csv',index=False)
    holder_slots.to_csv(EXPORT/'observed_holder_slot_snapshots.csv',index=False)
    holder_checkpoints.to_csv(EXPORT/'observed_holder_boundary_checkpoints.csv',index=False)
    holder_economics.to_csv(EXPORT/'holder_break_even_comparison.csv',index=False)
    slot_states.to_csv(EXPORT/'shortfall_slot_states.csv',index=False)
    wait_comparison.to_csv(EXPORT/'one_second_wait_case_comparison.csv',index=False)
    (EXPORT/'slot_trace_findings.json').write_text(json.dumps(dict(investigation=investigation,cases=case_records),indent=2)+'\\n')
    ''')
