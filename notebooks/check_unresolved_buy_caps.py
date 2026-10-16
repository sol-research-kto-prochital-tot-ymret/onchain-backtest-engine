"""Bounded one-token native sensitivity; preserves all previous runs and rules."""
from pathlib import Path
from copy import copy
from dataclasses import replace
from datetime import datetime,timezone
import csv
import hashlib
import json
import time

from backtest.adapters.source.sniper_silver import SilverSniperSource
from backtest.adapters.source.sniper_inventory import enrich_inventory
from backtest.bootstrap.sniper_window import run
from backtest.domain.execution import ExecutionMode
from backtest.domain.hashing import domain_digest
from backtest.domain.market_events import BlockEvent
from backtest.engine.replay import ReplayBoundary
from backtest.plugins.strategies.sniper_window import SniperWindowPolicy

ROOT=Path(__file__).absolute().parents[1]
OUT=ROOT/'var/dnipro/sniper-unresolved-buy-caps-20261015-v1'
PREVIOUS=ROOT/'var/dnipro/sniper-dev-flat-20261015-v1'
SOURCE=ROOT.parent/'solana-live-indexer/research/sniper_break_even/data/2026-10-10-1600-1700'
OVERLAY=ROOT/'var/dnipro/sniper-reference-profit-exit-20261015-v1/transfers-v4.json'
MINT='3umCrcTXJShzn33oco5DUorj2EEDrfjonUFzqtp9pump'
DONOR='2ZV5YqGhdATGZkit5WPAbxKXLh1L2cUFJ8T5YFkmZcPd'


def main():
    OUT.mkdir(exist_ok=False)
    original=json.loads((PREVIOUS/'dev-flat/result.json').read_text())
    expected=next(p for p in original['positions'] if p['mint']==MINT)
    policy=SniperWindowPolicy.load(original['summary']['policy'])
    began=time.monotonic();print('LOAD_VERIFIED_SOURCE',flush=True)
    parent=SilverSniperSource(SOURCE,ROOT/'var/dnipro/sniper-gap-455328000.json')
    enrich_inventory(parent,OVERLAY)
    source=copy(parent)
    lo=expected['launch_slot']-1
    cutoff=expected['entry_time']+policy.holding_seconds+60
    hi=min(slot for slot,timestamp in parent.times.items() if timestamp>=cutoff)
    source.births=[parent.birth_by_mint[MINT]];source.birth_by_mint={MINT:source.births[0]}
    source.trade_rows=[r for r in parent.trade_rows if r['mint']==MINT and lo<=r['slot']<=hi]
    source.lifecycle=[r for r in parent.lifecycle if r['mint']==MINT and lo<=r['slot']<=hi]
    source.meta={key:row for key,row in parent.meta.items() if row['row']['mint']==MINT}
    source.times={slot:timestamp for slot,timestamp in parent.times.items() if lo<=slot<=hi}
    source.values=tuple(e for e in parent.values if lo<=e.envelope.position.block_ordinal<=hi
        and (isinstance(e,BlockEvent) or e.envelope.canonical_event_id.hex in source.meta))
    last={}
    for event in source.values:
        if not isinstance(event,BlockEvent):last[event.envelope.boundary_ordinal]=event.envelope.canonical_event_id.hex
    source.last_in_group=set(last.values())
    positions={e.envelope.boundary_ordinal:e.envelope.position for e in source.values}
    source._boundaries=tuple(ReplayBoundary.from_position(positions[b]) for b in sorted(positions))
    source.inventory_transfers=tuple(m for m in parent.inventory_transfers if m['mint']==MINT and lo<=m['slot']<=hi)
    source.unsupported={k:v for k,v in parent.unsupported.items() if k==MINT}
    source.gaps=[(max(a,lo),min(b,hi)) for a,b in parent.gaps if max(a,lo)<=min(b,hi)]
    source.manifest=dict(parent.manifest,from_slot=lo,to_slot=hi)
    source.input_policy=dict(parent.input_policy,data_end_utc=datetime.fromtimestamp(source.times[hi],timezone.utc).isoformat())
    source.source_hash=domain_digest('dnipro.one-mint-window-projection.v1',
        dict(parent=parent.source_hash,mint=MINT,from_slot=lo,to_slot=hi)).hex
    projection=dict(parent_input_sha256=parent.source_hash,projected_input_sha256=source.source_hash,
        mint=MINT,from_slot=lo,to_slot=hi,cutoff_utc=source.input_policy['data_end_utc'],
        trades=len(source.trade_rows),events=len(source.values),rules='original two-slot dev-flat strategy; fixed historical transfers unchanged')
    (OUT/'projection.json').write_text(json.dumps(projection,indent=2)+'\n')
    caps=[0,10_000_000,50_000_000,100_000_000,250_000_000,500_000_000,750_000_000,1_000_000_000]
    rows=[]
    for cap in caps:
        started=time.monotonic()
        summary,positions,sink=run(source,replace(policy,maximum_entry_budget_lamports=cap),
            ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY,trace_mints={MINT})
        p=positions[0]
        if cap==0:
            for key in ('signal_slot','entry_slot','entry_cash_lamports','remaining_tokens_atomic','status','reason'):
                assert p.get(key)==expected.get(key),(key,p.get(key),expected.get(key))
        trades=[r for r in sink.conditional_repriced if r['mint']==MINT and r['trader']==DONOR and r['side']=='buy']
        record=dict(cap_sol=cap/1e9 if cap else None,uncapped=cap==0,
            actual_entry_cash_sol=p['entry_cash_lamports']/1e9,quoted_budget_sol=p.get('budget_lamports',0)/1e9,
            status=p['status'],reason=p['reason'],entry_slot=p.get('entry_slot'),exit_slot=p.get('exit_slot'),
            closed_pnl_sol=p['closed_pnl_lamports']/1e9 if p['closed_pnl_lamports'] is not None else None,
            failed_execution_cost_sol=p['failed_execution_cost_lamports']/1e9,
            donor_replayed_buy_tokens=trades[0]['replayed_tokens']/1e6 if trades else None,
            seconds=round(time.monotonic()-started,3),gmgn_url=p['gmgn_url'])
        rows.append(record)
        name='uncapped' if not cap else f'cap-{cap}'
        (OUT/f'{name}.json').write_text(json.dumps(dict(summary=summary,position=p,donor_trades=trades,
            conditional_errors=sink.conditional_errors,fills=sink.fills,ledger=sink.ledger),indent=2)+'\n')
        with (OUT/'comparison.csv').open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
        print('RESULT',json.dumps(record),flush=True)
    receipt=dict(complete=True,observed_at=datetime.now(timezone.utc).isoformat(),rows=rows,
        projected_uncapped_matches_original_position=True,seconds=round(time.monotonic()-began,3),
        changes='Only maximum_entry_budget_lamports changes; bankroll, execution and transfer rules unchanged',
        files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.iterdir() if p.is_file()})
    (OUT/'verification.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print('COMPLETE',round(time.monotonic()-began,3),flush=True)


if __name__=='__main__':main()
