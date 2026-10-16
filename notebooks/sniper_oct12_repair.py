"""Evidence-bound repair revisions for the retained October 12 sniper experiment.

Original caches/results remain immutable. RPC response bodies and request receipts
are retained, recovered trades use emitted amounts/reserves, and external token
movements pass the existing inventory validator. No strategy rules are changed.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter, defaultdict
from datetime import datetime
import csv
import gzip
import hashlib
import heapq
import importlib.util
from itertools import groupby
import json
from pathlib import Path
import shutil
import sys
import time
import urllib.error
import urllib.request

import pyarrow.parquet as pq

from sniper_carbon_day import (
    StreamingSniperSource, PROJECTION, START, save, publish, utc, CountedRows,
)
from backtest.adapters.source.sniper_inventory import enrich_inventory
from backtest.adapters.source.sniper_silver import file_hash, _envelope
from backtest.bootstrap.sniper_window import run, write_csv
from backtest.domain.execution import ExecutionMode
from backtest.domain.hashing import domain_digest
from backtest.engine.replay import ReplayBoundary
from backtest.plugins.strategies.sniper_window import SniperWindowPolicy

ENGINE = Path(__file__).absolute().parents[1]
OUT = ENGINE/'var/dnipro/sniper-oct12-repair-20261016-v1'
BASE = ENGINE/'var/dnipro/sniper-oct12-6h-24h-20261015-v1'
PILOT = ENGINE/'var/dnipro/sniper-oct12-unresolved-diagnosis-20261016-v1'
NATIVE = Path('/Volumes/KINGSTON/solana-research/cache-2026-10-12/translated')
PUMP = '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
SYSTEM = '11111111111111111111111111111111'
SOL = 'So11111111111111111111111111111111111111112'
EVENT = bytes([189,219,127,211,78,230,97,238])
PREFIX = bytes([228,69,165,46,81,203,154,29])
ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def un58(value):
    n=0
    for character in value:n=n*58+ALPHABET.index(character)
    return b'\0'*(len(value)-len(value.lstrip('1')))+n.to_bytes((n.bit_length()+7)//8,'big')


def load_helper():
    # The established owner/path/conservation parser is retained alongside inputs.
    sys.path.insert(0,str(PILOT))
    import collect_transfer_evidence as helper
    helper.ROOT=OUT
    helper.births={mint:dict(mint=mint,decoded_extension_json=json.dumps({'bonding_curve':b['curve']}))
                   for mint,b in json.loads((OUT/'births.json').read_text()).items()}
    return helper


def prepare():
    OUT.mkdir(parents=True,exist_ok=True)
    if (OUT/'prepared.json').exists():return
    positions=list(csv.DictReader((BASE/'unresolved_positions.csv').open()))
    positions=[p for p in positions if p['run']=='full24h-wallet10000']
    wanted={p['mint']:p for p in positions}
    births={b['mint']:b for b in json.loads((PROJECTION/'births.json').read_text()) if b['mint'] in wanted}
    native=json.loads((NATIVE/'manifest.json').read_text());remaining=set(births)
    for part in native['records']:
        selected=[mint for mint in remaining if part['from_slot']<=births[mint]['slot']<part['to_slot_exclusive']]
        if not selected:continue
        file=part['files']['events'];path=NATIVE/file['file'];assert file_hash(path)==file['sha256']
        for raw in pq.read_table(path,columns=['row_json'],filters=[('slot','in',[births[m]['slot'] for m in selected])]).column(0).to_pylist():
            item=json.loads(raw)
            if item['kind']=='TOKEN_LAUNCH' and item['mint'] in selected:
                b=births[item['mint']];b['curve']=item['venue_id'];b['payload']['bonding_curve']=item['venue_id']
                b['curve_evidence']=dict(file=str(path),sha256=file['sha256'],event_id=item['event_id'])
                remaining.remove(item['mint'])
    assert not remaining
    save(OUT/'births.json',births);save(OUT/'original-open-positions.json',positions)
    kept=[]
    for part in json.loads((PROJECTION/'manifest.json').read_text())['parts']:
        path=PROJECTION/part['file'];assert file_hash(path)==part['sha256']
        for batch in pq.ParquetFile(path).iter_batches(batch_size=8192,columns=['mint','row_json']):
            for row in batch.to_pylist():
                if row['mint'] in wanted:
                    value=json.loads(row['row_json'])
                    # Include all cached facts, allowing late retries to be diagnosed.
                    kept.append(value)
    with gzip.open(OUT/'original-token-rows.jsonl.gz','wt') as f:
        for row in kept:f.write(json.dumps(row,separators=(',',':'))+'\n')
    rpc=OUT/'public-rpc';rpc.mkdir(exist_ok=True)
    for file in (PILOT/'public-rpc').glob('*.json'):shutil.copy2(file,rpc/file.name)
    save(OUT/'prepared.json',dict(original_open_count=len(wanted),retained_token_rows=len(kept),
         projection_manifest_sha256=file_hash(PROJECTION/'manifest.json'),
         retained_rows_sha256=file_hash(OUT/'original-token-rows.jsonl.gz'),
         births_sha256=file_hash(OUT/'births.json')))
    print('PREPARED',len(wanted),'tokens',len(kept),'rows',flush=True)


class Rpc:
    def __init__(self):self.last=0;self.calls=0
    def call(self,name,method,params):
        folder=OUT/'public-rpc';folder.mkdir(exist_ok=True)
        path=folder/(name+'.json');receipt=path.with_suffix('.receipt.json')
        request=dict(jsonrpc='2.0',id=1,method=method,params=params)
        if receipt.exists():
            old=json.loads(receipt.read_text());assert old['request']==request
            assert old['response_sha256']==file_hash(path)
            cached=json.loads(path.read_text())
            if old['status']==200 and 'error' not in cached and cached.get('result') is not None:return cached['result']
        for attempt in range(6):
            time.sleep(max(0,.4-(time.monotonic()-self.last)));self.last=time.monotonic()
            req=urllib.request.Request('https://api.mainnet-beta.solana.com',data=json.dumps(request).encode(),headers={'Content-Type':'application/json'})
            try:
                with urllib.request.urlopen(req,timeout=30) as response:status=response.status;body=response.read()
            except urllib.error.HTTPError as e:status=e.code;body=e.read()
            except (OSError,TimeoutError):
                if attempt==5:raise
                time.sleep(min(2**attempt,20));continue
            self.calls+=1
            path.write_bytes(body)
            save(receipt,dict(endpoint='https://api.mainnet-beta.solana.com',request=request,status=status,
                 response_file=path.name,response_sha256=file_hash(path),fetched_at=utc(int(time.time()))))
            doc=json.loads(body)
            if status==200 and 'error' not in doc and doc.get('result') is not None:return doc['result']
            if status not in (429,500,502,503,504) and 'error' not in doc:break
            time.sleep(min(2**(attempt+1),30))
        raise RuntimeError(f'{method}: HTTP {status}, error={doc.get("error")}, nonnull={doc.get("result") is not None}')
    def transaction(self,signature):
        return self.call('tx-'+signature,'getTransaction',[signature,dict(encoding='jsonParsed',commitment='finalized',maxSupportedTransactionVersion=1)])
    def history(self,account,before,launch_slot):
        result=[];cursor=before
        for _ in range(8):
            key=hashlib.sha256((account+cursor).encode()).hexdigest()[:24]
            rows=self.call('history-'+key,'getSignaturesForAddress',[account,dict(before=cursor,limit=1000,commitment='finalized')])
            result.extend(x for x in rows if x['slot']>=launch_slot and x.get('err') is None)
            if len(rows)<1000 or rows[-1]['slot']<launch_slot:return result
            cursor=rows[-1]['signature']
        raise ValueError('token-account history exceeds bounded pagination')


def decode_trades(tx,helper,births):
    sys.path.insert(0,str(ENGINE.parent/'solana-live-indexer/deploy/silver'))
    from idl import Reader
    schema=json.loads((ENGINE/'tests/golden/pumpfun/program-v1/upstream/pump-current.events.idl.json').read_text())
    types={x['name']:x['type'] for x in schema['types']}
    assert tx['meta']['err'] is None
    signature=tx['transaction']['signatures'][0];values=[]
    for path,ix in helper.instructions(tx):
        if ix.get('programId')!=PUMP or not ix.get('data'):continue
        data=un58(ix['data'])
        if data.startswith(PREFIX):data=data[8:]
        if not data.startswith(EVENT):continue
        reader=Reader(data[8:],types);p=reader.read({'defined':{'name':'TradeEvent'}})
        if p['mint'] not in births:continue
        if p.get('quote_mint') not in (SOL,SYSTEM):raise ValueError('recovered non-SOL quote')
        # Carbon also decodes this known prefix. Preserve appended fields verbatim.
        trailing=reader.data[reader.offset:].hex()
        if p.get('mayhem_mode'):raise ValueError('recovered special curve mode')
        buy=p['is_buy'];quote=int(p.get('quote_amount',p['sol_amount']))
        raw=dict(slot=tx['slot'],tx_idx=tx['transactionIndex'],instruction_path=path,
                 event_uid=hashlib.sha256(json.dumps([signature,path,'rpc-pump-trade']).encode()).hexdigest(),
                 block_time=tx['blockTime'],kind='trade',mint=p['mint'],signature=signature,
                 trader=p['user'],side='buy' if buy else 'sell',quote_mint=SOL,
                 quote=quote,tokens=p['token_amount'],virtual_quote=p['virtual_quote_reserves'],
                 virtual_base=p['virtual_token_reserves'],real_quote=p['real_quote_reserves'],
                 real_base=p['real_token_reserves'],protocol_fee=p['fee'],creator_fee=p['creator_fee'],
                 buyback_fee=p['buyback_fee'],cashback=p['cashback'],network_fee=tx['meta']['fee'],
                 fee_payer=helper.keys(tx)[0],payload={k:p[k] for k in ('timestamp','ix_name','fee_basis_points','creator_fee_basis_points','creator','shareholders')},
                 rpc_evidence_file='public-rpc/tx-'+signature+'.json',
                 rpc_evidence_sha256=file_hash(OUT/'public-rpc'/('tx-'+signature+'.json')),
                 decoded_suffix_hex=trailing)
        assert quote>=0 and raw['tokens']>0 and raw['virtual_quote']>0 and raw['virtual_base']>0
        assert p['sol_amount']==quote and p['virtual_sol_reserves']==raw['virtual_quote'] and p['real_sol_reserves']==raw['real_quote']
        values.append(raw)
    # Check emitted token quantities against transaction-level curve token balance.
    balances=helper.token_map(tx)
    supply_changes=defaultdict(int)
    for _,ix in helper.instructions(tx):
        parsed=ix.get('parsed',{})
        if not isinstance(parsed,dict):continue
        kind=parsed.get('type');info=parsed.get('info',{})
        if kind in ('mintTo','mintToChecked','burn','burnChecked'):
            amount=int(info.get('amount',info.get('tokenAmount',{}).get('amount')))
            account=info.get('account')
            if account:supply_changes[account]+=amount*(1 if kind.startswith('mintTo') else -1)
    for mint,group in groupby(sorted(values,key=lambda r:r['mint']),lambda r:r['mint']):
        curve=births[mint]['curve'];delta=sum(r['tokens']*(-1 if r['side']=='buy' else 1) for r in group)
        accounts=[b for b in balances.values() if b['mint']==mint and b['owner']==curve]
        assert accounts,(mint,signature,'missing curve token balance')
        assert sum(b['post']-b['pre']-supply_changes[b['account']] for b in accounts)==delta,(mint,signature,'curve transfer/event mismatch')
    return values


def original_rows():
    with gzip.open(OUT/'original-token-rows.jsonl.gz','rt') as f:return [json.loads(line) for line in f]


def historical_wallet_balance(rows,recovered,movements,mint,wallet,before):
    amount=0
    for row in [*rows,*recovered.values()]:
        if row['kind']=='trade' and row['mint']==mint and row['trader']==wallet and (row['slot'],row['tx_idx'])<before:
            amount+=int(row['tokens'])*(1 if row['side']=='buy' else -1)
    for row in movements.values():
        if row['mint']==mint and (row['slot'],row['tx_idx'])<before:
            if row['source_wallet']==wallet:amount-=row['tokens']
            if row['destination_wallet']==wallet:amount+=row['tokens']
    return amount


def prioritized_history(history,account,target,rpc,helper):
    """Prioritize actual balance transitions; every accepted fact is still decoded.

    The binary probes are only a fetch-order heuristic, never a completeness or
    monotonic-balance assertion. A normal scan remains the fallback, and callers
    stop only after reconstructed inventory matches an evidenced pre-balance.
    """
    seen=set()
    for h in history:
        path=OUT/'public-rpc'/('tx-'+h['signature']+'.json')
        if not path.exists():continue
        try:
            tx=json.loads(path.read_text()).get('result')
            if not tx or tx['meta']['err'] is not None:continue
            balance=helper.token_map(tx).get(account)
            if balance and balance['pre']!=balance['post']:
                seen.add(h['signature']);yield h
        except (ValueError,KeyError):continue
    chronological=list(reversed(history));left=-1;right=len(chronological)-1
    while right-left>1:
        mid=(left+right)//2;h=chronological[mid];tx=rpc.transaction(h['signature'])
        if h['signature'] not in seen:seen.add(h['signature']);yield h
        balance=helper.token_map(tx).get(account)
        if not balance:break
        if balance['post']==target:right=mid
        else:left=mid
    for index in range(max(0,right-3),min(len(chronological),right+4)):
        h=chronological[index]
        if h['signature'] not in seen:seen.add(h['signature']);yield h
    for h in history:
        if h['signature'] not in seen:seen.add(h['signature']);yield h


def collect(revision,only_mint=None):
    helper=load_helper();rpc=Rpc();births=json.loads((OUT/'births.json').read_text())
    if only_mint:births={only_mint:births[only_mint]}
    state_path=OUT/'collection-state.json';state=json.loads(state_path.read_text()) if state_path.exists() else dict(processed=[],histories=[],errors=[])
    processed=set(state['processed']);histories=set(state['histories']);errors=[]
    recovered_path=OUT/'recovered-trades.json';movement_path=OUT/'verified-transfers.json'
    recovered={r['event_uid']:r for r in json.loads(recovered_path.read_text())} if recovered_path.exists() else {}
    movements={r['event_uid']:r for r in json.loads(movement_path.read_text())} if movement_path.exists() else {}
    rows=[r for r in original_rows() if r['mint'] in births];identities={(r['signature'],tuple(r['instruction_path']),r['kind']) for r in rows}
    cutoff={mint:births[mint]['block_time']+(420 if only_mint else 900) for mint in births}
    def checkpoint():
        save(recovered_path,sorted(recovered.values(),key=lambda r:(r['slot'],r['tx_idx'],tuple(r['instruction_path']))))
        save(movement_path,sorted(movements.values(),key=lambda r:(r['slot'],r['tx_idx'],tuple(r['instruction_path']))))
        save(state_path,dict(processed=sorted(processed),histories=sorted(histories),errors=errors,
                           rpc_calls_this_run=rpc.calls,recovered_trades=len(recovered),verified_transfers=len(movements)))
    def ingest(signature):
        tx=rpc.transaction(signature)
        if signature not in processed:
            for trade in decode_trades(tx,helper,births):
                if trade['block_time']>cutoff[trade['mint']]:continue
                if (signature,tuple(trade['instruction_path']),'trade') not in identities:recovered[trade['event_uid']]=trade
            for movement in helper.transfers(tx,signature):
                if movement['mint'] in births and movement['block_time']<=cutoff[movement['mint']]:movements[movement['event_uid']]=movement
            processed.add(signature)
        return tx
    def acquisition(mint,wallet,signature):
        tx=ingest(signature);key='|'.join((mint,wallet,signature))
        if key in histories:return
        accounts=[b for b in helper.token_map(tx).values() if b['mint']==mint and b['owner']==wallet and b['pre']>0]
        target=sum(account['pre'] for account in accounts);before=tx['slot'],tx['transactionIndex']
        for account in accounts:
            if historical_wallet_balance(rows,recovered,movements,mint,wallet,before)==target:break
            history=rpc.history(account['account'],signature,births[mint]['slot'])
            # Fetch recent acquisitions first; native application still sorts all
            # evidence causally. Repeated successful bot probes have no token flow.
            for h in prioritized_history(history,account['account'],target,rpc,helper):
                ingest(h['signature'])
                if historical_wallet_balance(rows,recovered,movements,mint,wallet,before)==target:break
        histories.add(key)
    # Baseline stopping sellers, then every historical negative-inventory edge.
    opened={p['mint']:p for p in json.loads((OUT/'original-open-positions.json').read_text())}
    initial=[]
    for error in csv.DictReader((BASE/'full24h-wallet10000/conditional_errors.csv').open()):
        if error['mint'] not in opened or error['reason']!='CONDITIONAL_HISTORICAL_SELLER_BALANCE_UNPROVEN':continue
        o=int(error['boundary_ordinal']);slot=o>>32;index=(o&((1<<32)-1))-1
        initial.extend((r['mint'],r['trader'],r['signature']) for r in rows
                       if r['kind']=='trade' and r['mint']==error['mint'] and r['slot']==slot and r['tx_idx']==index and r['side']=='sell')
    for index,case in enumerate(dict.fromkeys(initial)):
        try:acquisition(*case)
        except Exception as e:errors.append(dict(case=case,error=str(e)));print('EVIDENCE_ERROR',case,str(e),flush=True)
        if (index+1)%5==0:
            checkpoint();print('ACQUISITIONS',index+1,'/',len(initial),'swaps',len(recovered),'transfers',len(movements),'rpc',rpc.calls,flush=True)
    # Reconstruct histories causally. Missing donors and additional sellers are
    # acquired recursively; no balance is seeded from transaction metadata.
    for wave in range(6):
        events=[r for r in rows if r['kind']=='trade' and r['block_time']<=cutoff[r['mint']]]+[r for r in recovered.values() if r['mint'] in births and r['block_time']<=cutoff[r['mint']]]
        timeline=[(r['slot'],r['tx_idx'],tuple(r['instruction_path']),1,r) for r in events]
        timeline += [(r['slot'],r['tx_idx'],tuple(r['instruction_path']),0,r) for r in movements.values() if r['mint'] in births and r['block_time']<=cutoff[r['mint']]]
        balances=defaultdict(int);needed={}
        for _,_,_,kind,row in sorted(timeline,key=lambda x:x[:4]):
            mint=row['mint'];amount=int(row['tokens'])
            if kind==0:
                source=(mint,row['source_wallet']);dest=(mint,row['destination_wallet'])
                if balances[source]<amount:needed.setdefault((mint,row['source_wallet']),row['signature'])
                balances[source]-=amount;balances[dest]+=amount
            else:
                wallet=(mint,row['trader'])
                if row['side']=='sell' and balances[wallet]<amount:needed.setdefault(wallet,row['signature'])
                balances[wallet]+=amount*(1 if row['side']=='buy' else -1)
        todo=[(mint,wallet,sig) for (mint,wallet),sig in needed.items() if '|'.join((mint,wallet,sig)) not in histories]
        print('HISTORY_WAVE',wave,'unproven_wallets',len(needed),'new_histories',len(todo),flush=True)
        if not todo:break
        for i,case in enumerate(todo):
            try:acquisition(*case)
            except Exception as e:errors.append(dict(case=case,error=str(e)));print('EVIDENCE_ERROR',case,str(e),flush=True)
            if (i+1)%5==0:checkpoint();print('WAVE_PROGRESS',wave,i+1,'/',len(todo),'swaps',len(recovered),'transfers',len(movements),flush=True)
        checkpoint()
    checkpoint()
    save(OUT/f'evidence-v{revision}.json',dict(complete=not errors,errors=errors,recovered_trades=len(recovered),verified_transfers=len(movements),
         trades_sha256=file_hash(recovered_path),transfers_sha256=file_hash(movement_path),
         remaining_historical_inventory_candidates=[dict(mint=m,wallet=w,signature=s) for (m,w),s in needed.items()]))
    print('EVIDENCE_COMPLETE',len(recovered),len(movements),'errors',len(errors),flush=True)
    for name in ('recovered-trades','verified-transfers'):shutil.copy2(OUT/(name+'.json'),OUT/(name+f'-v{revision}.json'))


def tail():
    """Recover all retained successful curve transactions for the midnight holds."""
    helper=load_helper();rpc=Rpc();births=json.loads((OUT/'births.json').read_text())
    positions=json.loads((OUT/'original-open-positions.json').read_text())
    selected=[p for p in positions if p['reason']=='HOLDING']
    results=[]
    # Keep tail evidence separate while acquisition collection is writing its own
    # checkpoint files. Both processes only create independent RPC cache entries.
    values={}
    for position in selected:
        mint=position['mint'];birth=births[mint];cursor=None;history=[];complete=False
        for page in range(30):
            options=dict(limit=1000,commitment='finalized')
            if cursor:options['before']=cursor
            name='curve-history-'+hashlib.sha256(json.dumps([birth['curve'],cursor]).encode()).hexdigest()[:24]
            rows=rpc.call(name,'getSignaturesForAddress',[birth['curve'],options])
            history.extend(x for x in rows if birth['slot']<=x['slot']<456240500 and x.get('err') is None)
            if len(rows)<1000 or rows[-1]['slot']<birth['slot']:complete=True;break
            cursor=rows[-1]['signature']
        assert complete,(mint,'curve history pagination incomplete')
        for item in reversed(history):
            tx=rpc.transaction(item['signature'])
            for row in decode_trades(tx,helper,births):
                if row['mint']==mint and row['block_time']<START+86400+900:values[row['event_uid']]=row
        results.append(dict(mint=mint,symbol=position['symbol'],successful_curve_transactions=len(history),
                            decoded_trades=sum(v['mint']==mint for v in values.values()),history_complete_back_to_birth=complete))
        print('TAIL_TOKEN',json.dumps(results[-1]),flush=True)
    original={(r['signature'],tuple(r['instruction_path']),'trade') for r in original_rows() if r['kind']=='trade'}
    new=[r for r in values.values() if (r['signature'],tuple(r['instruction_path']),'trade') not in original]
    save(OUT/'tail-trades.json',sorted(new,key=lambda r:(r['slot'],r['tx_idx'],tuple(r['instruction_path']))))
    save(OUT/'tail-receipt.json',dict(complete=True,tokens=results,additional_trades=len(new),
         before_midnight_recovered=sum(r['block_time']<START+86400 for r in new),
         after_midnight=sum(r['block_time']>=START+86400 for r in new),trades_sha256=file_hash(OUT/'tail-trades.json')))


def curves():
    """Compare curve-address transaction histories through each exit horizon."""
    helper=load_helper();rpc=Rpc();births=json.loads((OUT/'births.json').read_text())
    positions=json.loads((OUT/'original-open-positions.json').read_text())
    latest=OUT/'24h-targeted-repaired-v2/result.json'
    outcomes={p['mint']:p for p in json.loads(latest.read_text())['positions']} if latest.exists() else {}
    known=defaultdict(set)
    for row in original_rows():known[row['mint']].add(row['signature'])
    trades={};movements={};findings=[]
    for i,position in enumerate(positions):
        mint=position['mint'];birth=births[mint];cursor=None;history=[];complete=False
        deadline=int(datetime.fromisoformat(position['entry_utc'].replace('Z','+00:00')).timestamp())+360
        outcome=outcomes.get(mint,{})
        if outcome.get('status')=='CLOSED':deadline=min(deadline,outcome['exit_time']+1)
        for page in range(30):
            options=dict(limit=1000,commitment='finalized')
            if cursor:options['before']=cursor
            name='curve-history-'+hashlib.sha256(json.dumps([birth['curve'],cursor]).encode()).hexdigest()[:24]
            rows=rpc.call(name,'getSignaturesForAddress',[birth['curve'],options])
            history.extend(x for x in rows if x['slot']>=birth['slot'] and x.get('err') is None
                           and (x.get('blockTime') is None or x['blockTime']<=deadline))
            if len(rows)<1000 or rows[-1]['slot']<birth['slot']:complete=True;break
            cursor=rows[-1]['signature']
        assert complete,(mint,'curve history pagination incomplete')
        missing=[h for h in history if h['signature'] not in known[mint]]
        # Successful bot probes often mention the curve without executing a swap.
        # One finalized full block screens a crowded slot before fetching each
        # actual emitted swap as its own getTransaction evidence record.
        grouped=defaultdict(list)
        for h in missing:grouped[h['slot']].append(h)
        swap_signatures=set()
        for slot,group in grouped.items():
            if len(group)<4:
                swap_signatures.update(h['signature'] for h in group);continue
            block=rpc.call('block-'+str(slot),'getBlock',[slot,dict(encoding='jsonParsed',commitment='finalized',transactionDetails='full',rewards=False,maxSupportedTransactionVersion=1)])
            by_signature={t['transaction']['signatures'][0]:t for t in block['transactions']}
            for h in group:
                transaction=by_signature[h['signature']]
                if transaction['meta']['err'] is not None:raise ValueError('signature history and block success disagree')
                instructions=list(transaction['transaction']['message']['instructions'])
                instructions += [ix for inner in transaction['meta'].get('innerInstructions',[]) for ix in inner['instructions']]
                for ix in instructions:
                    if ix.get('programId')!=PUMP or not ix.get('data'):continue
                    data=un58(ix['data'])
                    if data.startswith(PREFIX):data=data[8:]
                    if data.startswith(EVENT):swap_signatures.add(h['signature']);break
        count=0
        for h in reversed([h for h in missing if h['signature'] in swap_signatures]):
            tx=rpc.transaction(h['signature'])
            for row in decode_trades(tx,helper,births):
                if row['mint']==mint and row['block_time']<=deadline:trades[row['event_uid']]=row;count+=1
            for movement in helper.transfers(tx,h['signature']):
                if movement['mint']==mint and movement['block_time']<=deadline:movements[movement['event_uid']]=movement
        findings.append(dict(mint=mint,symbol=position['symbol'],history_transactions=len(history),
                             absent_transactions=len(missing),additional_swap_events=count,deadline_utc=utc(deadline)))
        save(OUT/'curve-trades.json',list(trades.values()));save(OUT/'curve-transfers.json',list(movements.values()))
        save(OUT/'curve-progress.json',dict(complete=i+1==len(positions),completed=i+1,total=len(positions),findings=findings))
        if (i+1)%5==0:print('CURVE_AUDIT',i+1,'/',len(positions),'additional_swaps',len(trades),flush=True)
    save(OUT/'curve-receipt.json',dict(complete=True,scope='Missing curve transactions through repaired exit plus one second, or entry plus 360 seconds if still open; retained transaction CPI completeness not certified',
         trades_sha256=file_hash(OUT/'curve-trades.json'),transfers_sha256=file_hash(OUT/'curve-transfers.json'),findings=findings))


def collect_actual(revision,from_run):
    """Acquire only histories that currently stop a still-open native position."""
    helper=load_helper();rpc=Rpc();births=json.loads((OUT/'births.json').read_text())
    result=json.loads((OUT/from_run/'result.json').read_text())
    opened={p['mint']:p for p in result['positions'] if p['status']=='OPEN'}
    recovered={r['event_uid']:r for r in json.loads((OUT/'recovered-trades.json').read_text())}
    movements={r['event_uid']:r for r in json.loads((OUT/'verified-transfers.json').read_text())}
    retained=original_rows();identities={(r['signature'],tuple(r['instruction_path']),r['kind']) for r in retained}
    rows=retained+list(recovered.values());cases=[];errors=[];first=set()
    for error in csv.DictReader((OUT/from_run/'conditional_errors.csv').open()):
        mint=error['mint']
        if mint not in opened or mint in first:continue
        first.add(mint);boundary=int(error['boundary_ordinal']);slot=boundary>>32;index=(boundary&((1<<32)-1))-1
        if error['reason']=='CONDITIONAL_HISTORICAL_SELLER_BALANCE_UNPROVEN':
            cases.extend((mint,r['trader'],r['signature']) for r in rows if r['kind']=='trade' and r['mint']==mint and r['slot']==slot and r['tx_idx']==index and r['side']=='sell')
        elif error['reason']=='CONDITIONAL_TRANSFER_SOURCE_BALANCE_UNPROVEN':
            cases.extend((mint,r['source_wallet'],r['signature']) for r in movements.values() if r['mint']==mint and r['slot']==slot and r['tx_idx']==index)
    def ingest(signature):
        tx=rpc.transaction(signature)
        for row in decode_trades(tx,helper,births):
            if (signature,tuple(row['instruction_path']),'trade') not in identities:recovered[row['event_uid']]=row
        for movement in helper.transfers(tx,signature):movements[movement['event_uid']]=movement
        return tx
    findings=[]
    for i,(mint,wallet,sig) in enumerate(dict.fromkeys(cases)):
        try:
            tx=ingest(sig);accounts=[b for b in helper.token_map(tx).values() if b['mint']==mint and b['owner']==wallet]
            assert accounts,(mint,wallet,'no evidenced token account')
            history=set();target=sum(account['pre'] for account in accounts);before=tx['slot'],tx['transactionIndex']
            for account in accounts:
                if account['pre']==0:continue
                if historical_wallet_balance(retained,recovered,movements,mint,wallet,before)==target:break
                history_rows=rpc.history(account['account'],sig,births[mint]['slot'])
                for h in prioritized_history(history_rows,account['account'],target,rpc,helper):
                    ingest(h['signature']);history.add(h['signature'])
                    if historical_wallet_balance(retained,recovered,movements,mint,wallet,before)==target:break
            findings.append(dict(mint=mint,wallet=wallet,signature=sig,history_transactions=len(history),
                                 evidenced_tx_pre_balance=target,reconstructed_wallet_before_tx=historical_wallet_balance(retained,recovered,movements,mint,wallet,before),
                                 scope='Bounded acquisition proof; token-account history may stop once known wallet inventory equals evidenced transaction pre-balance'))
        except Exception as e:errors.append(dict(mint=mint,wallet=wallet,signature=sig,error=str(e)))
        print('CAUSAL_ACQUISITION',i+1,'/',len(cases),'swaps',len(recovered),'transfers',len(movements),'errors',len(errors),flush=True)
    for filename,values in [('recovered-trades.json',recovered),('verified-transfers.json',movements)]:
        save(OUT/filename,sorted(values.values(),key=lambda r:(r['slot'],r['tx_idx'],tuple(r['instruction_path']))))
        shutil.copy2(OUT/filename,OUT/(Path(filename).stem+f'-v{revision}.json'))
    save(OUT/f'evidence-v{revision}.json',dict(complete=not errors,from_native_run=from_run,cases=findings,errors=errors,
         recovered_trades=len(recovered),verified_transfers=len(movements),
         trades_sha256=file_hash(OUT/'recovered-trades.json'),transfers_sha256=file_hash(OUT/'verified-transfers.json')))


def reserve_repair(revision,from_run):
    """Recover pool-state gaps through the currently modeled holding horizon."""
    helper=load_helper();rpc=Rpc();births=json.loads((OUT/'births.json').read_text())
    outcomes={p['mint']:p for p in json.loads((OUT/from_run/'result.json').read_text())['positions']}
    retained=original_rows();known={(r['signature'],tuple(r['instruction_path'])) for r in retained if r['kind']=='trade'}
    recovered={r['event_uid']:r for r in json.loads((OUT/'recovered-trades.json').read_text())}
    movements={r['event_uid']:r for r in json.loads((OUT/'verified-transfers.json').read_text())}
    extra=list(recovered.values())+json.loads((OUT/'tail-trades.json').read_text())
    states={m:tuple(int(b[k]) for k in ('initial_virtual_quote_raw','initial_virtual_base_raw','initial_real_quote_raw','initial_real_base_raw')) for m,b in births.items()}
    previous=dict(births);gaps=[];dust=[]
    for row in sorted([r for r in retained+extra if r['kind']=='trade'],key=lambda r:(r['slot'],r['tx_idx'],tuple(r['instruction_path']))):
        mint=row['mint'];p=outcomes[mint];deadline=p.get('exit_time',p.get('entry_time',births[mint]['block_time'])+360)+1
        if row['block_time']>deadline:continue
        old=states[mint];sign=1 if row['side']=='buy' else -1;q=row['quote'];t=row['tokens']
        expected=(old[0]+sign*q,old[1]-sign*t,old[2]+sign*q,old[3]-sign*t)
        actual=tuple(row[k] for k in ('virtual_quote','virtual_base','real_quote','real_base'))
        if expected!=actual:
            gap=dict(mint=mint,previous=previous[mint],next=row,expected=expected,actual=actual)
            (dust if expected[1]==actual[1] and max(abs(a-b) for a,b in zip(expected,actual))<=1 else gaps).append(gap)
        states[mint]=actual;previous[mint]=row
    save(OUT/f'reserve-gaps-v{revision}.json',dict(material=gaps,one_lamport_quote_residuals=dust))
    errors=[];parsed=set();blocked_slots={}
    def ingest(signature):
        if signature in parsed:return
        tx=rpc.transaction(signature)
        for row in decode_trades(tx,helper,births):
            if (signature,tuple(row['instruction_path'])) not in known:recovered[row['event_uid']]=row
        for movement in helper.transfers(tx,signature):movements[movement['event_uid']]=movement
        parsed.add(signature)
    for i,gap in enumerate(gaps):
        mint=gap['mint'];lo=gap['previous'];hi=gap['next']
        try:
            ingest(hi['signature']);ingest(lo['signature'])
            history=rpc.history(births[mint]['curve'],hi['signature'],lo['slot'])
            chosen=[h for h in history if (h['slot'],h.get('transactionIndex',-1))>=(lo['slot'],lo['tx_idx'])]
            grouped=defaultdict(list)
            for h in chosen:
                if h['signature'] not in parsed:grouped[h['slot']].append(h)
            selected=[]
            for slot,items in grouped.items():
                if len(items)<4:selected.extend(h['signature'] for h in items);continue
                if slot not in blocked_slots:
                    block=rpc.call('block-'+str(slot),'getBlock',[slot,dict(encoding='jsonParsed',commitment='finalized',transactionDetails='full',rewards=False,maxSupportedTransactionVersion=1)])
                    block_swaps=set()
                    for tx in block['transactions']:
                        if tx['meta']['err'] is not None:continue
                        instructions=list(tx['transaction']['message']['instructions'])
                        instructions += [ix for inner in tx['meta'].get('innerInstructions',[]) for ix in inner['instructions']]
                        for ix in instructions:
                            if ix.get('programId')!=PUMP or not ix.get('data'):continue
                            data=un58(ix['data'])
                            if data.startswith(PREFIX):data=data[8:]
                            if data.startswith(EVENT):block_swaps.add(tx['transaction']['signatures'][0]);break
                    blocked_slots[slot]=block_swaps
                selected.extend(h['signature'] for h in items if h['signature'] in blocked_slots[slot])
            for sig in reversed(selected):ingest(sig)
        except Exception as e:errors.append(dict(mint=mint,previous_slot=lo['slot'],next_slot=hi['slot'],error=str(e)))
        save(OUT/'recovered-trades.json',sorted(recovered.values(),key=lambda r:(r['slot'],r['tx_idx'],tuple(r['instruction_path']))))
        save(OUT/'verified-transfers.json',sorted(movements.values(),key=lambda r:(r['slot'],r['tx_idx'],tuple(r['instruction_path']))))
        print('RESERVE_GAP_REPAIR',i+1,'/',len(gaps),'swaps',len(recovered),'transfers',len(movements),'errors',len(errors),flush=True)
    save(OUT/f'evidence-v{revision}.json',dict(complete=not errors,from_native_run=from_run,scope='Material historical reserve gaps through modeled exits; one-lamport residuals retained separately',
         material_gaps=len(gaps),errors=errors,recovered_trades=len(recovered),verified_transfers=len(movements),
         trades_sha256=file_hash(OUT/'recovered-trades.json'),transfers_sha256=file_hash(OUT/'verified-transfers.json')))
    for name in ('recovered-trades','verified-transfers'):shutil.copy2(OUT/(name+'.json'),OUT/(name+f'-v{revision}.json'))


def merge_curves():
    assert json.loads((OUT/'curve-receipt.json').read_text())['complete']
    for existing,additional,key in [('recovered-trades.json','curve-trades.json','event_uid'),('verified-transfers.json','curve-transfers.json','event_uid')]:
        values={x[key]:x for x in json.loads((OUT/existing).read_text())}
        for x in json.loads((OUT/additional).read_text()):
            if x[key] in values:assert values[x[key]]==x
            values[x[key]]=x
        save(OUT/existing,sorted(values.values(),key=lambda r:(r['slot'],r['tx_idx'],tuple(r['instruction_path']))))
    print('CURVE_EVIDENCE_MERGED',flush=True)


class RepairedSource(StreamingSniperSource):
    def __init__(self,*,hours,only_repair_tokens=False):
        end=START+hours*3600;data_end=end+900
        super().__init__(PROJECTION,start=START,end=end,data_end=data_end)
        self.only_repair_tokens=only_repair_tokens
        self.original_clock_slots=set(self.times)
        births=json.loads((OUT/'births.json').read_text())
        if only_repair_tokens:
            self.births=[b for b in self.births if b['mint'] in births]
            self.birth_by_mint={b['mint']:b for b in self.births}
            self.supported &= set(self.birth_by_mint)
            self.unsupported={m:v for m,v in self.unsupported.items() if m in self.birth_by_mint}
        for mint,b in births.items():
            if mint in self.birth_by_mint:self.birth_by_mint[mint]['payload']['bonding_curve']=b['curve']
        self.base_source_hash=self.source_hash
        original={(r['signature'],tuple(r['instruction_path']),'trade'):r for r in original_rows() if r['kind']=='trade'}
        extra={}
        for file in ('recovered-trades.json','tail-trades.json'):
            for row in json.loads((OUT/file).read_text()):
                if row['mint'] not in self.supported or not START<=row['block_time']<data_end:continue
                identity=row['signature'],tuple(row['instruction_path']),'trade'
                assert identity not in original,'recovered overlay duplicates a cached swap'
                evidence=OUT/row['rpc_evidence_file'];receipt=json.loads(evidence.with_suffix('.receipt.json').read_text())
                assert row['rpc_evidence_sha256']==file_hash(evidence)==receipt['response_sha256']
                assert receipt['request']['method']=='getTransaction' and receipt['request']['params'][1]['commitment']=='finalized'
                if identity in extra:assert extra[identity]==row
                extra[identity]=row
        self.recovered_trade_count=len(extra)
        clocks={}
        if hours==24:
            clock_file=OUT/'clickhouse/midnight-observed-slot-clocks-v3.jsonl.gz'
            clock_receipt=json.loads(clock_file.with_suffix('.receipt.json').read_text())
            assert file_hash(clock_file)==clock_receipt['file_sha256']
            with gzip.open(clock_file,'rt') as f:
                for line in f:
                    row=json.loads(line);slot=int(row['slot']);when=int(row['observed_time'])
                    assert when==int(row['max_observed_time']),'conflicting slot timestamps'
                    if START<=when<data_end:clocks[slot]=when
        for row in extra.values():clocks.setdefault(row['slot'],row['block_time'])
        added_clocks=[]
        for slot,when in clocks.items():
            if slot not in self.times:
                added_clocks.append(dict(kind='clock',slot=slot,tx_idx=-1,block_time=when,
                                         event_uid='repaired-observed-slot-clock:'+str(slot),mint=''))
                self.times[slot]=when
        self.extra_rows=sorted([*extra.values(),*added_clocks],key=self.order)
        self.manifest=dict(from_slot=min(self.times),to_slot=max(self.times))
        self.source_hash=domain_digest('dnipro.sniper-recovered-carbon-window.v1',dict(
            base=self.base_source_hash,hours=hours,only_repair_tokens=only_repair_tokens,
            births=file_hash(OUT/'births.json'),trades=file_hash(OUT/'recovered-trades.json'),
            tail=file_hash(OUT/'tail-trades.json'),adapter=file_hash(Path(__file__)),
            clocks=file_hash(OUT/'clickhouse/midnight-observed-slot-clocks-v3.jsonl.gz') if hours==24 else None)).hex
        overlay=[x for x in json.loads((OUT/'verified-transfers.json').read_text())
                 if x['mint'] in self.birth_by_mint and x['slot'] in self.times]
        if overlay:
            transfer_path=OUT/('transfer-overlay-'+str(hours)+('h-targets' if only_repair_tokens else 'h-full')+'.json')
            save(transfer_path,overlay);enrich_inventory(self,transfer_path)

    @staticmethod
    def order(row):return row['slot'],row['tx_idx'],tuple(row.get('instruction_path',[])),row['kind']

    def raw_rows(self):
        if self.only_repair_tokens:
            clocks=(dict(kind='clock',slot=s,tx_idx=-1,block_time=self.times[s],mint='') for s in sorted(self.original_clock_slots))
            retained=(r for r in original_rows() if r['mint'] in self.supported and self.start<=r['block_time']<self.data_end)
            base=heapq.merge(clocks,retained,key=self.order)
        else:base=super().raw_rows()
        yield from heapq.merge(base,self.extra_rows,key=self.order)

    def boundaries(self):
        if self._boundaries is not None:return self._boundaries
        if self.only_repair_tokens:
            seen={};count=0
            for row in self.raw_rows():
                b=ReplayBoundary.from_position(_envelope(row['slot'],row['tx_idx'],0,'repaired-timeline').position)
                seen[b.boundary_ordinal]=b
                count+=row['kind']=='trade'
            self._boundaries=tuple(seen[k] for k in sorted(seen));self.trade_rows=CountedRows(count)
            print('TARGET_TIMELINE',len(seen),'groups',len(self.births),'launches',count,'trades',flush=True)
            return self._boundaries
        original=super().boundaries()
        # ReplayBoundary has an explicit integer boundary_ordinal API.
        new={b.boundary_ordinal:b for b in original}
        for row in self.extra_rows:
            b=ReplayBoundary.from_position(_envelope(row['slot'],row['tx_idx'],0,'repaired-timeline').position)
            new[b.boundary_ordinal]=b
        self._boundaries=tuple(new[k] for k in sorted(new))
        self.trade_rows=CountedRows(len(self.trade_rows)+self.recovered_trade_count)
        return self._boundaries


def replay(hours,revision,only_repair_tokens=False):
    evidence=json.loads((OUT/f'evidence-v{revision}.json').read_text())
    assert evidence['complete'],'acquisition evidence collection has pending errors'
    assert evidence['trades_sha256']==file_hash(OUT/'recovered-trades.json')
    assert evidence['transfers_sha256']==file_hash(OUT/'verified-transfers.json')
    assert json.loads((OUT/'tail-receipt.json').read_text())['complete']
    bankroll=1000 if hours==6 else 10000
    prior_name='control-6h-wallet1000' if hours==6 else 'full24h-wallet10000'
    policy=SniperWindowPolicy.load(json.loads((BASE/(prior_name+'-policy.json')).read_text()))
    assert policy.bankroll_lamports==bankroll*10**9
    name=f'{hours}h-'+('targeted' if only_repair_tokens else 'full-cohort')+f'-repaired-v{revision}'
    destination=OUT/name;assert not destination.exists()
    source=RepairedSource(hours=hours,only_repair_tokens=only_repair_tokens);source.boundaries()
    began=time.monotonic();print('REPLAY_START',name,'extra_swaps',source.recovered_trade_count,flush=True)
    # Observe the original reducer's exception without changing its arguments,
    # return value, staged balances or exception. This records concrete donor
    # shortfalls instead of inferring them from a final reason label.
    from backtest.plugins.execution.pump_conditional import PumpConditionalReducer
    original_move=PumpConditionalReducer._move_inventory;conflicts=[]
    def observe_move(reducer,movement,balances,historical,applied):
        key=movement['mint'],movement['source_wallet']
        historical_amount=historical.get(key,reducer.historical_balances.get(key,0))
        simulated_amount=balances.get(key,reducer.balances.get(key,0))
        try:return original_move(reducer,movement,balances,historical,applied)
        except ValueError as error:
            conflicts.append(dict(mint=movement['mint'],slot=movement['slot'],tx_idx=movement['tx_idx'],
                signature=movement['signature'],source_wallet=movement['source_wallet'],destination_wallet=movement['destination_wallet'],
                required_tokens_atomic=movement['tokens'],historical_source_tokens_atomic=historical_amount,
                simulated_source_tokens_atomic=simulated_amount,shortfall_tokens_atomic=max(0,movement['tokens']-simulated_amount),
                reason=str(error),instruction_path=movement['instruction_path'],evidence_sha256=movement['evidence_sha256']))
            raise
    PumpConditionalReducer._move_inventory=observe_move
    try:
        summary,rows,sink=run(source,policy,ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY,trace_mints=set(json.loads((OUT/'births.json').read_text())))
    finally:PumpConditionalReducer._move_inventory=original_move
    conflict_path=OUT/(name+'-inventory-conflicts.csv');write_csv(conflict_path,conflicts)
    summary.update(input_format='committed-carbon-projection-plus-finalized-rpc-recovery',
                   repair_scope='original-unresolved-token-histories',full_chain_coverage=False,
                   targeted_diagnostic=only_repair_tokens,recovered_swap_count=source.recovered_trade_count,
                   last_available_time_utc=utc(max(source.times.values())))
    publish(destination,summary,rows,sink)
    manifest=json.loads((destination/'manifest.json').read_text())
    for file,digest in manifest['files'].items():assert file_hash(destination/file)==digest
    save(OUT/(name+'-receipt.json'),dict(complete=True,seconds=round(time.monotonic()-began,3),manifest_sha256=file_hash(destination/'manifest.json'),summary=summary,
         inventory_conflicts_file=conflict_path.name,inventory_conflicts_sha256=file_hash(conflict_path),adapter_sha256=file_hash(Path(__file__))))
    print('REPLAY_COMPLETE',name,json.dumps({k:summary[k] for k in ('filled_entries','closed_positions','open_positions','realized_net_pnl_sol','open_cash_cost_sol')}),flush=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['prepare','collect','causal-collect','reserve-repair','tail','curves','merge-curves','replay']);parser.add_argument('--revision',type=int,default=1)
    parser.add_argument('--hours',type=int,choices=[6,24],default=6);parser.add_argument('--targets',action='store_true')
    parser.add_argument('--from-run',default='24h-targeted-repaired-v1')
    parser.add_argument('--only-mint')
    args=parser.parse_args()
    if args.command=='prepare':prepare()
    elif args.command=='collect':collect(args.revision,args.only_mint)
    elif args.command=='tail':tail()
    elif args.command=='curves':curves()
    elif args.command=='merge-curves':merge_curves()
    elif args.command=='causal-collect':collect_actual(args.revision,args.from_run)
    elif args.command=='reserve-repair':reserve_repair(args.revision,args.from_run)
    else:replay(args.hours,args.revision,args.targets)


if __name__=='__main__':main()
