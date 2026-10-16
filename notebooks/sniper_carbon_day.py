"""Project an immutable Carbon cache and stream it through the native sniper engine.

Projection changes storage only. Strategy, quotes, reservations, fees, counterfactual
reduction and portfolio accounting remain the existing native implementations.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import lru_cache
from itertools import groupby
from pathlib import Path
import gzip
import hashlib
import json
import os
import time

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from backtest.adapters.source.clickhouse.carbon import public_key
from backtest.adapters.source.sniper_silver import _envelope, SOL, file_hash
from backtest.bootstrap.sniper_window import run, write_csv, implementation_identity
from backtest.domain.execution import ExecutionMode
from backtest.domain.hashing import canonical_json_bytes, domain_digest
from backtest.domain.identifiers import AccountId, AssetId, ContentDigest, DatasetRevisionId, LogicalContentHash, ProtocolPayloadSchemaId, VenueId
from backtest.domain.market_events import BlockEvent, TokenLaunchEvent, VenueLifecycleEvent, VenueLifecycleKind, VenueTradeEvent, REFERENCE_AMM_TRADE_PAYLOAD_SCHEMA_ID, reference_amm_trade_payload
from backtest.engine.replay import ReplayBoundary
from backtest.plugins.strategies.sniper_window import SniperWindowPolicy

ENGINE = Path(__file__).absolute().parents[1]
CACHE = Path('/Volumes/KINGSTON/solana-research/cache-2026-10-12')
OUT = ENGINE/'var/dnipro/sniper-oct12-6h-24h-20261015-v1'
PROJECTION = Path('/Volumes/KINGSTON/solana-research/sniper-oct12-projection-20261015-v1')
START = 1791763200
SCHEMA = pa.schema([('slot', pa.uint64()), ('tx_idx', pa.int64()), ('block_time', pa.int64()),
                    ('kind', pa.string()), ('mint', pa.string()), ('row_json', pa.string())])


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False)+'\n')
    tmp.replace(path)


@lru_cache(maxsize=131072)
def key_tuple(value):
    return public_key(list(value))


def key(value):
    return key_tuple(tuple(value)) if isinstance(value, list) else public_key(value)


def compact(native):
    """Keep native facts and the exact fields consumed by the current strategy."""
    kind = native['kind']
    payload = native.get('decoded_payload', {})
    when = native.get('block_time')
    if when is None:
        when = payload.get('timestamp')
    if when is None:
        raise ValueError('event has no evidenced timestamp')
    row = dict(slot=native['slot'], tx_idx=native['transaction_index'],
               instruction_path=native['instruction_path'], event_uid=native['event_id'],
               block_time=int(when), mint=native.get('mint', ''), signature=native.get('signature', ''),
               native_event_index=native['event_index'])
    if kind == 'BLOCK':
        row['kind'] = 'clock'
        return row
    if native['protocol'] != 'pumpfun' or native.get('transaction_success') is not True:
        raise ValueError('projection accepts only successful native Pump facts and clocks')
    if kind == 'TOKEN_LAUNCH':
        p = {k: payload.get(k) for k in ('is_mayhem_mode', 'is_cashback_enabled', 'is_holder_reward')}
        row.update(kind='launch', name=payload.get('name'), symbol=payload.get('symbol'),
                   creator=native['developer'], launch_signer=native['creation_user'],
                   quote_mint=native['quote_mint'], token_program=key(payload['token_program']),
                   supply_raw=int(payload['token_total_supply']),
                   initial_virtual_base_raw=int(payload['virtual_token_reserves']),
                   initial_virtual_quote_raw=int(payload.get('virtual_quote_reserves', payload.get('virtual_sol_reserves'))),
                   initial_real_base_raw=int(payload['real_token_reserves']),
                   initial_real_quote_raw=int(payload.get('real_quote_reserves', payload.get('real_sol_reserves', 0))), payload=p)
    elif kind == 'VENUE_LIFECYCLE':
        row.update(kind='lifecycle', migration_stage='amm_migration' if native['lifecycle']=='MIGRATED' else 'curve_complete')
    elif kind == 'VENUE_TRADE':
        p = {k: payload.get(k) for k in ('timestamp', 'ix_name', 'fee_basis_points', 'creator_fee_basis_points')}
        p['creator'] = key(payload['creator']) if payload.get('creator') is not None else None
        p['shareholders'] = [dict(s, address=key(s['address'])) for s in payload.get('shareholders', [])]
        buy = native['side'] == 'buy'
        row.update(kind='trade', trader=native['actor'], side=native['side'],
                   quote_mint=native['quote_mint'],
                   quote=int(native['sold_amount'] if buy else native['bought_amount']),
                   tokens=int(native['bought_amount'] if buy else native['sold_amount']),
                   virtual_quote=int(native['reserve_quote_after']), virtual_base=int(native['reserve_base_after']),
                   real_quote=int(native['real_quote_after']), real_base=int(native['real_base_after']),
                   protocol_fee=int(payload.get('fee', 0)), creator_fee=int(payload.get('creator_fee', 0)),
                   buyback_fee=int(payload.get('buyback_fee', 0)), cashback=int(payload.get('cashback', 0)),
                   network_fee=native.get('fee_lamports'), fee_payer=native.get('fee_payer'), payload=p)
    else:
        raise ValueError('unhandled Pump event kind '+kind)
    return row


def project_part(task):
    index, native_root, part, output = task
    pa.set_cpu_count(1); pa.set_io_thread_count(1)
    native_root, output = Path(native_root), Path(output)
    file = output/f'part-{index:04d}.parquet'; receipt = output/f'part-{index:04d}.json'
    expected = part['files']['events']
    if receipt.exists():
        saved = json.loads(receipt.read_text())
        assert saved['input_sha256'] == expected['sha256'] and file_hash(file) == saved['sha256']
        return saved
    path = native_root/expected['file']
    assert file_hash(path) == expected['sha256'], str(path)
    records, births, clocks = [], [], []
    counts = Counter()
    for batch in pq.ParquetFile(path).iter_batches(batch_size=2048, columns=['row_json']):
        values = batch.column(0)
        mask = pc.or_(pc.match_substring(values, '"protocol":"pumpfun"'), pc.match_substring(values, '"kind":"BLOCK"'))
        for text in pc.filter(values, mask).to_pylist():
            native = json.loads(text)
            if native['protocol'] != 'pumpfun' and native['kind'] != 'BLOCK':
                continue
            row = compact(native)
            records.append({k: row[k] for k in ('slot','tx_idx','block_time','kind','mint')} | {'row_json': json.dumps(row, separators=(',',':'), ensure_ascii=False)})
            counts[row['kind']] += 1
            if row['kind']=='launch': births.append(row)
            if row['kind']=='clock': clocks.append([row['slot'], row['block_time']])
    temp = file.with_suffix('.partial.parquet')
    pq.write_table(pa.Table.from_pylist(records, schema=SCHEMA), temp, compression='zstd', row_group_size=4096)
    temp.replace(file)
    saved = dict(index=index, file=file.name, sha256=file_hash(file), input_sha256=expected['sha256'],
                 from_slot=part['from_slot'], to_slot_exclusive=part['to_slot_exclusive'],
                 counts=dict(counts), births=births, clocks=clocks)
    save(receipt, saved)
    return saved


def prepare(limit=None):
    native = CACHE/'translated'
    manifest = json.loads((native/'manifest.json').read_text())
    marker = json.loads((native/'COMMITTED').read_text())
    assert manifest['complete'] and file_hash(native/'manifest.json')==marker['manifest_sha256']
    output = PROJECTION if limit is None else OUT/f'projection-pilot-{limit}'
    output.mkdir(parents=True, exist_ok=True)
    plan = dict(native_manifest_sha256=file_hash(native/'manifest.json'), adapter_sha256=file_hash(Path(__file__)), limit=limit)
    if (output/'plan.json').exists():
        assert json.loads((output/'plan.json').read_text())==plan, 'projection implementation changed; select a new output root'
    else: save(output/'plan.json', plan)
    parts = manifest['records'] if limit is None else manifest['records'][:limit]
    began = time.monotonic(); results = {}
    with ProcessPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(project_part, (i,str(native),p,str(output))) for i,p in enumerate(parts)]
        for future in as_completed(futures):
            result=future.result(); results[result['index']]=result
            if len(results)%10==0 or len(results)==len(parts):
                print('PROJECTION',len(results),'/',len(parts),'seconds',round(time.monotonic()-began,1),flush=True)
                save(output/'progress.json',dict(phase='projecting',complete=False,completed=len(results),total=len(parts),seconds=round(time.monotonic()-began,2)))
    ordered=[results[i] for i in range(len(parts))]
    # Do not repeat large clock arrays and metadata in the central part list.
    births=[b for p in ordered for b in p['births']]
    clocks=[c for p in ordered for c in p['clocks']]
    assert len({b['mint'] for b in births})==len(births)
    save(output/'births.json', births)
    save(output/'clocks.json', clocks)
    doc=dict(complete=True, format='sniper-carbon-projection/v1', **plan,
             source_capabilities=manifest['capabilities'],
             from_slot=parts[0]['from_slot'], to_slot_exclusive=parts[-1]['to_slot_exclusive'],
             births_sha256=file_hash(output/'births.json'), clocks_sha256=file_hash(output/'clocks.json'),
             parts=[{k:v for k,v in p.items() if k not in ('births','clocks')} for p in ordered],
             counters=dict(sum((Counter(p['counts']) for p in ordered),Counter())),
             seconds=round(time.monotonic()-began,3))
    if limit is None:
        assert doc['counters']['trade']==manifest['counters']['pumpfun:VENUE_TRADE']
        assert doc['counters']['launch']==manifest['counters']['pumpfun:TOKEN_LAUNCH']
        assert doc['counters']['lifecycle']==manifest['counters']['pumpfun:VENUE_LIFECYCLE']
        assert doc['counters']['clock']==manifest['counters']['derived_slot_clocks']
    save(output/'manifest.json',doc); save(output/'COMMITTED',dict(manifest_sha256=file_hash(output/'manifest.json')))
    save(output/'progress.json',dict(phase='complete',complete=True,completed=len(parts),total=len(parts),seconds=doc['seconds']))
    print('PROJECTION_COMMITTED',str(output),doc['counters'],flush=True)
    return output


class CountedRows:
    def __init__(self, count): self.count=count
    def __len__(self): return self.count


class StreamingSniperSource:
    """Native source port with bounded event metadata, observation delay fixed at zero."""
    zero_latency_stream = True

    def __init__(self, projection, *, start, end, data_end):
        self.root=Path(projection)
        self.projection=json.loads((self.root/'manifest.json').read_text())
        assert self.projection['complete']
        assert file_hash(self.root/'manifest.json')==json.loads((self.root/'COMMITTED').read_text())['manifest_sha256']
        for filename,key_name in [('births.json','births_sha256'),('clocks.json','clocks_sha256')]:
            assert file_hash(self.root/filename)==self.projection[key_name]
        self.births=[b for b in json.loads((self.root/'births.json').read_text()) if start<=b['block_time']<end]
        self.birth_by_mint={b['mint']:b for b in self.births}
        self.unsupported={}
        for b in self.births:
            if b['quote_mint']!=SOL.value: self.unsupported[b['mint']]='NON_SOL_QUOTE'
            elif any(b['payload'].get(k) for k in ('is_mayhem_mode','is_cashback_enabled','is_holder_reward')):
                self.unsupported[b['mint']]='SPECIAL_CURVE_MODE'
        self.supported=set(self.birth_by_mint)-self.unsupported.keys()
        self.times={int(s):int(t) for s,t in json.loads((self.root/'clocks.json').read_text()) if start<=t<data_end}
        for b in self.births:self.times.setdefault(b['slot'],b['block_time'])
        assert self.times and self.births
        self.input_policy=dict(cohort_start_utc=utc(start),cohort_end_utc=utc(end),data_end_utc=utc(data_end))
        self.manifest=dict(from_slot=min(self.times),to_slot=max(self.times))
        self.start,self.end,self.data_end=start,end,data_end
        self.manifest_hash=file_hash(self.root/'manifest.json')
        self.source_hash=domain_digest('dnipro.sniper-carbon-window.v1',dict(projection=self.manifest_hash,start=start,end=end,data_end=data_end,adapter=file_hash(Path(__file__)))).hex
        self.empty_block_evidence_hash=None;self.proven_empty_slots=[]
        # This archive certifies retained query chunks, not Silver publication or all chain slots.
        self.gaps=[]
        self.meta={};self.last_in_group=set();self._recent=deque()
        self._boundaries=None;self.trade_rows=CountedRows(0)

    def _parts(self):
        for p in self.projection['parts']:
            if p['to_slot_exclusive']>self.manifest['from_slot'] and p['from_slot']<=self.manifest['to_slot']:
                yield p

    def boundaries(self):
        if self._boundaries is not None:return self._boundaries
        boundaries=[];previous=None;trades=0
        for p in self._parts():
            path=self.root/p['file'];assert file_hash(path)==p['sha256']
            for batch in pq.ParquetFile(path).iter_batches(batch_size=8192,columns=['slot','tx_idx','block_time','kind','mint']):
                for r in batch.to_pylist():
                    if not self.start<=r['block_time']<self.data_end:continue
                    eligible=r['kind']=='clock' or (r['kind']=='launch' and r['mint'] in self.birth_by_mint) or (r['mint'] in self.supported)
                    if r['kind']=='trade' and r['mint'] in self.birth_by_mint:trades+=1
                    if not eligible:continue
                    boundary=(r['slot']<<32)+r['tx_idx']+1
                    if boundary!=previous:
                        assert previous is None or boundary>previous
                        boundaries.append(ReplayBoundary.from_position(_envelope(r['slot'],r['tx_idx'],0,'timeline').position));previous=boundary
        self.trade_rows=CountedRows(trades);self._boundaries=tuple(boundaries)
        print('SOURCE_TIMELINE',len(boundaries),'groups',len(self.births),'launches',trades,'retained cohort trades',flush=True)
        return self._boundaries

    def raw_rows(self):
        for p in self._parts():
            for batch in pq.ParquetFile(self.root/p['file']).iter_batches(batch_size=2048):
                for r in batch.to_pylist():
                    if not self.start<=r['block_time']<self.data_end:continue
                    if r['kind']!='clock' and r['mint'] not in (self.birth_by_mint if r['kind']=='launch' else self.supported):continue
                    yield json.loads(r['row_json'])

    def events(self):
        last_progress=time.monotonic()
        for (slot,tx),rows in groupby(self.raw_rows(),lambda r:(r['slot'],r['tx_idx'])):
            group=list(rows)
            if tx==-1:
                assert len(group)==1
                yield BlockEvent(_envelope(slot,-1,0,'retained-slot-clock'),self.times[slot]*10**9,None)
                continue
            group.sort(key=lambda r:(tuple(r['instruction_path']),r['kind']))
            events=[];identities=[]
            for index,row in enumerate(group):
                event=event_for(row,index)
                identity=event.envelope.canonical_event_id.hex
                self.meta[identity]=dict(kind=row['kind'],row=row,path=row['instruction_path'])
                events.append(event);identities.append(identity)
            self.last_in_group.add(identities[-1]);self._recent.append(identities)
            # groupby in the engine reads one transaction ahead. Retain three whole
            # groups, including an arbitrarily large atomic multi-event transaction.
            if len(self._recent)>3:
                for identity in self._recent.popleft():
                    self.meta.pop(identity,None);self.last_in_group.discard(identity)
            if time.monotonic()-last_progress>20:
                print('SOURCE_AT',utc(self.times[slot]),'metadata_rows',len(self.meta),flush=True);last_progress=time.monotonic()
            yield from events

    @property
    def dataset_revision_id(self):return DatasetRevisionId(self.source_hash)
    @property
    def logical_content_hash(self):return LogicalContentHash(self.source_hash)
    @property
    def replay_semantics_id(self):return domain_digest('dnipro.sniper-carbon-replay.v1',dict(source=self.source_hash,scope='conditional-retained-subset',delivery='zero-observation-delay'))


def event_for(row,index):
    e=_envelope(row['slot'],row['tx_idx'],index,row['event_uid']);mint=row['mint']
    if row['kind']=='launch':
        return TokenLaunchEvent(e,AssetId(mint),AccountId(row['creator']),AccountId(row['launch_signer']),VenueId(mint),AssetId(row['quote_mint']),ProtocolPayloadSchemaId('dnipro-retained-launch-control-v1'),canonical_json_bytes({'kind':'launch'}))
    if row['kind']=='lifecycle':
        stage=VenueLifecycleKind.MIGRATED if row['migration_stage']=='amm_migration' else VenueLifecycleKind.COMPLETED
        return VenueLifecycleEvent(e,VenueId(mint),stage,ProtocolPayloadSchemaId('dnipro-retained-lifecycle-v1'),canonical_json_bytes({'stage':row['migration_stage']}))
    buy=row['side']=='buy';asset=AssetId(mint)
    return VenueTradeEvent(e,VenueId(mint),SOL if buy else asset,asset if buy else SOL,
                           int(row['quote'] if buy else row['tokens']),int(row['tokens'] if buy else row['quote']),(),
                           REFERENCE_AMM_TRADE_PAYLOAD_SCHEMA_ID,reference_amm_trade_payload(asset_a_id=SOL,asset_b_id=asset,reserve_a_after_atomic=int(row['virtual_quote']),reserve_b_after_atomic=int(row['virtual_base'])))


def publish(destination,summary,rows,sink):
    assert not destination.exists()
    stage=destination.with_name(destination.name+'.partial-'+str(os.getpid()));stage.mkdir(parents=True)
    write_csv(stage/'token_results.csv',rows)
    for name,values in [('fills',sink.fills),('execution_failures',sink.failures),('creator_fee_recipients',sink.creator_fee_recipients),('strategy_trace',sink.strategy_trace),('order_audit',sink.order_audit),('conditional_errors',sink.conditional_errors),('conditional_repriced',sink.conditional_repriced),('inventory_transfers_applied',sink.inventory_transfers_applied)]:write_csv(stage/(name+'.csv'),values)
    ledger=[dict(transaction_id=tx['transaction_id'],correlation_id=tx['correlation_id'],boundary_ordinal=tx['boundary_ordinal'],reason=tx['reason'],**p) for tx in sink.ledger for p in tx['postings']]
    write_csv(stage/'ledger.csv',ledger)
    save(stage/'result.json',dict(summary=summary,positions=rows,ledger=sink.ledger,fills=sink.fills))
    save(stage/'manifest.json',dict(complete=True,implementation=summary['implementation'],source_manifest_sha256=summary['source_manifest_sha256'],source_input_sha256=summary['source_input_sha256'],files={p.name:file_hash(p) for p in stage.iterdir()}))
    stage.rename(destination)


def replay(hours,projection=PROJECTION,name=None):
    assert hours in (6,24)
    end=START+hours*3600;data_end=min(end+900,START+86400)
    bankroll=1000 if hours==6 else 10000
    old=json.loads((ENGINE/'var/dnipro/sniper-dev-flat-other-periods-20261015-v1/dev-flat-policy.json').read_text())
    policy=SniperWindowPolicy.load(old|dict(bankroll_lamports=bankroll*10**9))
    assert policy.document()==old|dict(bankroll_lamports=bankroll*10**9)
    name=name or ('control-6h-wallet1000' if hours==6 else 'full24h-wallet10000')
    destination=OUT/name;assert not destination.exists()
    save(OUT/(name+'-policy.json'),policy.document())
    started=time.monotonic();print('NATIVE_START',name,'wallet_SOL',bankroll,flush=True)
    source=StreamingSniperSource(projection,start=START,end=end,data_end=data_end)
    source.boundaries()
    # Retain every eligible decision trace for review. No chart generation is done here.
    summary,rows,sink=run(source,policy,ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY,trace_mints=set(source.birth_by_mint))
    summary['input_format']='committed-carbon-parquet-projection'
    summary['input_capabilities']=source.projection['source_capabilities']
    summary['publication_coverage']='not certified by this Bronze cache; empty publication_gaps is not a gap-free chain assertion'
    summary['last_available_time_utc']=utc(max(source.times.values()))
    summary['right_edge_followup_complete']=data_end>=end+policy.holding_seconds+2
    summary['continuous_wallet']=True
    publish(destination,summary,rows,sink)
    for p,digest in json.loads((destination/'manifest.json').read_text())['files'].items():assert file_hash(destination/p)==digest
    save(OUT/(name+'-receipt.json'),dict(complete=True,hours=hours,bankroll_sol=bankroll,seconds=round(time.monotonic()-started,3),observed_at=utc(int(time.time())),manifest_sha256=file_hash(destination/'manifest.json'),summary=summary))
    print('NATIVE_COMPLETE',name,json.dumps({k:summary[k] for k in ('signals','filled_entries','closed_positions','open_positions','realized_net_pnl_sol','open_cash_cost_sol')}),flush=True)


def main():
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('prepare');p.add_argument('--limit',type=int)
    p=sub.add_parser('run');p.add_argument('--hours',type=int,choices=[6,24],required=True)
    args=parser.parse_args()
    OUT.mkdir(parents=True,exist_ok=True)
    if args.command=='prepare':prepare(args.limit)
    else:replay(args.hours)


if __name__=='__main__':main()
