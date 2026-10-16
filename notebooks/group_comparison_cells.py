"""Notebook views of causal features and separate subsequent market observations."""


def add_group_comparison_cells(md,code):
    md('''
    ## All profitable positions versus all liquidity shortfalls

    This compares **all 13 profitable positions**, **all 11 shortfall cases**, and
    the **three other losses** from the same completed native replay. No new
    entry rule, cutoff or strategy is applied. Each dot is a token; overlapping
    distributions and counterexamples remain visible.

    Signal features use complete retained transactions through the decision,
    including its triggering transaction. Landing features use only transactions
    strictly before our modeled buy. Later observations are in a separate CSV:
    an outcome difference is not information available at entry. Inventories
    refer to observed wallet swaps, and developer refers to the launch creator
    address; linked wallets and transfers remain unproven.
    ''')
    code('''
    COMPARISON = ROOT/'var/dnipro/sniper-window-20261010-group-comparison-v3'
    comparison_receipt = json.loads((COMPARISON/'receipt.json').read_text())
    assert comparison_receipt['native_result_sha256']==sha(RUN_DIRS['virtual']/'result.json')
    assert comparison_receipt['source_input_sha256']==runs['virtual']['summary']['source_input_sha256']
    for name,digest in comparison_receipt['csv_hashes'].items():
        assert sha(COMPARISON/name)==digest,name
    entry_comparison = pd.read_csv(COMPARISON/'entry_features.csv')
    later_comparison = pd.read_csv(COMPARISON/'post_entry_outcomes.csv')
    assert entry_comparison['group'].value_counts().to_dict()=={'profitable':13,'shortfall':11,'other_loss':3}
    assert entry_comparison['signal_window_still_valid'].all()
    group_order=['profitable','shortfall','other_loss']
    group_labels=['Profitable (13)','Shortfall (11)','Other loss (3)']
    group_colors=['#16875c','#b34343','#6d7279']
    comparison_rows=[]
    for group in group_order:
        f=entry_comparison[entry_comparison['group']==group]
        future=later_comparison[later_comparison['group']==group]
        comparison_rows.append(dict(group=group,n=len(f),
            no_observed_creator_buy_by_signal=int((~f['signal_launch_creator_bought']).sum()),
            creator_sold_all_by_signal=int(f['signal_launch_creator_sold_all'].sum()),
            creator_sold_all_by_landing=int(f['landing_launch_creator_sold_all'].sum()),
            real_SOL_at_signal_median=f['signal_actual_sol_reserve_SOL'].median(),
            unique_buyers_at_signal_median=f['signal_unique_buyers'].median(),
            wallets_with_positive_inventory_median=f['signal_positive_inventory_wallets'].median(),
            top3_pct_observed_inventory_median=f['signal_top3_pct_observed_inventory'].median(),
            other_profitable_holder_exists=int((f['signal_other_profitable_wallets']>0).sum()),
            additional_target_net_SOL_to_actual_reserve_pct_median=f['signal_additional_target_flow_to_real_reserve_ratio'].median()*100,
            original_window_no_longer_passes_at_landing=int((~f['landing_window_still_valid']).sum()),
            AFTER_ENTRY_5s_new_buyers_median=future['postentry_5s_new_buyers'].median(),
            AFTER_ENTRY_5s_gross_curve_buys_SOL_median=future['postentry_5s_gross_buy_SOL'].median(),
            AFTER_ENTRY_5s_peak_running_net_flow_SOL_median=future['postentry_5s_max_running_net_flow_SOL'].median()))
    group_comparison = pd.DataFrame(comparison_rows)
    display(group_comparison.T)
    linked_table(entry_comparison[['token','gmgn_url','group','signal_launch_creator_bought','signal_launch_creator_sold_all','signal_actual_sol_reserve_SOL','signal_unique_buyers','signal_other_profitable_wallets','signal_additional_target_flow_to_real_reserve_ratio']].sort_values(['group','signal_actual_sol_reserve_SOL']))
    group_comparison.to_csv(COMPARISON/'display_comparison.csv',index=False)
    ''')
    code('''
    fig,axes=plt.subplots(2,3,figsize=(16,9),layout='constrained')
    panels=[
        (entry_comparison,'signal_actual_sol_reserve_SOL','Real SOL reserves at signal','SOL',False),
        (entry_comparison,'signal_top3_pct_observed_inventory','Top three observed holders at signal','% of observed circulating inventory',False),
        (entry_comparison,'signal_other_profitable_supply_pct','Other wallets that can profit after our buy','% of total supply',False),
        (entry_comparison,'signal_additional_target_flow_to_real_reserve_ratio','Additional target net flow / current real SOL','Ratio',False),
        (later_comparison,'postentry_5s_new_buyers','AFTER ENTRY: new buying wallets in first 5s','Wallets',True),
        (later_comparison,'postentry_5s_gross_buy_SOL','AFTER ENTRY: gross curve buys in first 5s','SOL',True),
    ]
    for ax,(frame,feature,title,ylabel,is_future) in zip(axes.flat,panels):
        for i,(group,color) in enumerate(zip(group_order,group_colors)):
            selected=frame[frame['group']==group].sort_values('mint')
            y=selected[feature].dropna().to_numpy()
            x=i+np.linspace(-.15,.15,len(y))
            ax.scatter(x,y,color=color,s=45,alpha=.85,edgecolor='white',linewidth=.5)
            if len(y):
                ax.plot([i-.22,i+.22],[np.median(y)]*2,color=color,linewidth=3)
        ax.set_xticks(range(3),group_labels,rotation=12)
        ax.set(ylabel=ylabel,title=title)
        ax.grid(axis='y',alpha=.18)
        if is_future:
            ax.set_facecolor('#fff6e9')
    fig.suptitle('All 27 native positions — signal-time features versus later demand\\nBars mark medians; later demand cannot be used as an entry-time filter',fontsize=13)
    fig.savefig(COMPARISON/'group_comparison.png',dpi=160)
    plt.show()
    ''')
    md('''
    ## What separates the groups in this sample

    **Subsequent demand is the largest difference.** During the first five
    timestamp seconds after entry, winners had a median of 12 new buying wallets
    and 7.637 SOL of gross curve buys; shortfall cases had one new buyer and
    0.099 SOL. The median maximum running net historical inflow was +3.983 SOL
    for winners and zero for shortfalls. These windows may include activity after
    a winner has already exited. They describe the market's continuation, not a
    feature known at the initial signal or proof of causation by our order.

    Volume alone has important counterexamples. UNBOX received 18.136 SOL of
    curve buys in those five seconds, but historical selling left net flow at
    -8.170 SOL. Qweb received 27.582 SOL of buys, but net flow was -5.934 SOL.
    The ordinary loser 毒チワワ had 64 new buyers and 46.053 SOL of curve buys,
    yet still lost in the replay. The order and size of selling matter alongside
    incoming demand; simple volume or trader-count confirmation is insufficient.

    **An observable difference is developer participation.** Seven of thirteen
    winners had no observed launch-creator buy by the signal, while every
    shortfall case had one. The losing token 毒チワワ also had no observed creator
    buy, so this is not a guaranteed profit condition. Dev sellout by the signal
    appears in three winners and four shortfalls; by landing it appears in five
    shortfalls. Waiting for sellout alone does not separate the outcomes.

    **Shortfalls generally had thinner pools and greater holder concentration.**
    Median actual SOL at the signal was 3.584 versus 8.979 for winners. The top
    three wallets held 85.45% versus 73.47% of observed circulating inventory.
    Additional net flow needed for our target was 37.12% versus 17.20% of the
    current actual reserve. The groups overlap substantially: BETDRONE won with
    just 1.213 SOL of historical actual liquidity, while UNBOX and Qweb lost
    despite roughly 17 SOL. A simple minimum-liquidity cutoff cannot capture this.

    **Several suspected warnings also appear in winners.** Profitable exits for
    other holders existed in eleven of thirteen winners and all eleven
    shortfalls. The original reference sizing window failed a fresh calculation
    at landing in six winners and three shortfalls. All signals were deliberately
    sized near the same 0.001 SOL reserve margin. Initial unique-buyer counts were
    similar: medians of seven and eight. Those facts do not independently explain
    which launches receive the next wave of demand.

    These are exploratory observations from one hour and 27 filled positions.
    No model or optimized threshold has been fitted and no new filter has been
    validated on a later day. The replay's reserve-impact limitation still
    applies; a shortfall label does not establish a real-world failed sale.
    ''')
