"""Paired development and untouched-period results for the frozen revisions."""


def add_redesign_cells(md,code):
    md('''
    ## Revised rules and paired independent-period test

    The revised strategy was selected on the previously investigated October 10
    hour. Both the original and revised version then use exactly the same
    October 11, 10:00-11:00 UTC birth cohort, with a tail through 11:16 UTC.
    Selection is recorded before any validation run. No validation outcomes
    change the rules. This is the requested **before and after comparison**.

    The selected revision requires no recorded buy by the launch creator, caps
    gross buy budget at 0.2 SOL, and watches an initial feasible reference window.
    It waits at least one additional slot, requires two new buying wallet
    addresses and at least 0.1 SOL net curve inflow since that watch began, then
    recomputes sizing and thresholds. Reference buyers still qualify in the
    original first two slots; entry decisions may continue through slot eight.
    Target remains +5% net. A -10% quote-based loss trigger or five-second timeout
    schedules a sale one slot later. These triggers do not guarantee that a
    delayed sale can fill at that loss or price.

    Both versions start with 10 SOL and use the same network, program, creator,
    rent and minimum-output scenarios. The original remains otherwise unchanged.
    All three proposed revisions and the development baseline are retained.
    The best development revision remained slightly negative in exogenous
    settlement; calling it the selected candidate does not imply profitability.

    **Execution comparisons:** strict replay and explicit virtual settlement
    retain historical reserves. Conditional replay now keeps our own reserve
    effects, reprices historical exact-quote purchases and exact-token buys,
    and checks counterpart inventories. Two conditional sell assumptions are
    shown separately: fixed recorded token input, or the observed fraction of
    the seller's retained position. The latter is an assumption about a trader's
    sell sizing, not proof of their response. Unknown buy intent, unsupported
    counterpart inventory, liquidity and source gaps retain open exposure.
    Conditional modes never add synthetic funding.
    ''')
    code('''
    REDESIGN = ROOT/'var/dnipro/sniper-window-redesign-v1'
    experiment_plan=json.loads((REDESIGN/'experiment_plan.json').read_text())
    selection=json.loads((REDESIGN/'selection.json').read_text())
    assert sha(REDESIGN/'experiment_plan.json')==selection['experiment_plan_sha256']
    redesign_rows=[]
    redesign_positions=[]
    validation_source_hashes=set()
    for folder in sorted(REDESIGN.iterdir()):
        if not folder.is_dir() or not (folder/'manifest.json').exists():
            continue
        manifest=json.loads((folder/'manifest.json').read_text())
        assert manifest['complete']
        for filename,digest in manifest['files'].items():
            assert sha(folder/filename)==digest,(folder.name,filename)
        data=json.loads((folder/'result.json').read_text())
        s=data['summary']
        for transaction in data['ledger']:
            totals=defaultdict(int)
            for posting in transaction['postings']:
                totals[posting['asset_id']]+=posting['amount_atomic']
            assert all(value==0 for value in totals.values())
        if s['execution_mode']=='CONDITIONAL_PROTOCOL_REPLAY':
            assert s['synthetic_shortfall_lamports']==0
            assert s['own_reserve_impact_propagated']
        if folder.name.startswith('validation-'):
            validation_source_hashes.add(s['source_input_sha256'])
        redesign_rows.append(dict(run=folder.name,mode=s['execution_mode'],
            modeled_closed_net_PnL_SOL=s['realized_net_pnl_lamports']/LAMPORTS,
            filled=s['filled_entries'],closed=s['closed_positions'],open=s['open_positions'],
            unresolved_cost_SOL=s['open_cash_cost_lamports']/LAMPORTS,
            zero_recovery_stress_SOL=(s['realized_net_pnl_lamports']-s['open_cash_cost_lamports'])/LAMPORTS,
            synthetic_shortfall_SOL=s['synthetic_shortfall_lamports']/LAMPORTS,
            source_gaps=len(s['publication_gaps']),conditional_errors=s['conditional_repricing_errors'],
            counterpart_sell_fraction=s['policy']['counterpart_sell_fraction']))
        for p in data['positions']:
            if 'signal_slot' in p:
                redesign_positions.append(dict(run=folder.name,token=p['symbol'],gmgn_url=p['gmgn_url'],
                    mint=p['mint'],status=p['status'],reason=p['reason'],
                    signal_slot=p['signal_slot'],entry_slot=p.get('entry_slot'),exit_slot=p.get('exit_slot'),
                    gross_buy_budget_SOL=p['budget_lamports']/LAMPORTS,
                    modeled_closed_PnL_SOL=p['closed_pnl_lamports']/LAMPORTS if p.get('closed_pnl_lamports') is not None else None,
                    remaining_tokens_raw=p['remaining_tokens_atomic']))
    assert len(validation_source_hashes)==1
    paired_results=pd.DataFrame(redesign_rows)
    paired_positions=pd.DataFrame(redesign_positions)
    display(Markdown('Selected development candidate: **'+selection['selected']+'**.'))
    display(paired_results[paired_results['run'].str.startswith('development-')])
    display(paired_results[paired_results['run'].str.startswith('validation-')])
    display(paired_results[paired_results['run'].str.startswith('additional-')])
    paired_results.to_csv(REDESIGN/'notebook_before_after.csv',index=False)
    paired_positions.to_csv(REDESIGN/'all_variant_signal_positions.csv',index=False)
    linked_table(paired_positions[paired_positions['run']=='validation-virtual-after'])
    ''')
    md('''
    ## How to interpret the before and after results

    A positive closed-position total with open positions is an incomplete
    portfolio result. Open costs and zero-recovery stress are shown alongside it;
    zero recovery is a stress scenario rather than an observed loss. Virtual
    settlement exposes assumed funding and cannot establish on-chain solvency.

    The October 11 snapshot has 25 unproven publication intervals. Both
    versions use the same fail-closed coverage behavior; affected positions are
    retained, never removed as inconvenient trades. A further fixed October 10
    17:30-18:30 UTC period contains 3,146 launches, 175,172 trades and no unproven
    publication gaps. Both unchanged versions and the same execution scenarios
    are tested there; the first independent result remains included.
    Conditional counterpart
    conflicts likewise cannot be relabeled as clean profitable completions.

    The paired tests establish how these particular versions behaved on this
    bounded sample. They do not establish exact landing, full holder history,
    historical slippage limits, trader reactions or a guaranteed profitable
    strategy. Network fees are a configured scenario, tips are zero, and the
    physical conditional replay has separate assumptions from unchanged-price
    historical settlement. Original results and notebooks remain preserved.
    ''')
