"""Independently bind recovered event ordering to finalized block signature lists."""
from collections import defaultdict
import json

from sniper_oct12_repair import OUT, Rpc, save, file_hash


def main():
    inputs=('recovered-trades.json','verified-transfers.json','tail-trades.json')
    hashes={name:file_hash(OUT/name) for name in inputs}
    required=defaultdict(dict)
    for name in inputs:
        for row in json.loads((OUT/name).read_text()):
            old=required[row['slot']].setdefault(row['signature'],row['tx_idx'])
            assert old==row['tx_idx']
    rpc=Rpc();checked=[]
    for i,(slot,transactions) in enumerate(sorted(required.items())):
        full=OUT/'public-rpc'/f'block-{slot}.json'
        if full.exists():
            receipt=json.loads(full.with_suffix('.receipt.json').read_text())
            assert receipt['status']==200 and receipt['response_sha256']==file_hash(full)
            block=json.loads(full.read_text())['result']
            signatures=[t['transaction']['signatures'][0] for t in block['transactions']]
            evidence=full
        else:
            name=f'block-signatures-{slot}'
            block=rpc.call(name,'getBlock',[slot,dict(commitment='finalized',transactionDetails='signatures',rewards=False,maxSupportedTransactionVersion=1)])
            signatures=block['signatures'];evidence=OUT/'public-rpc'/(name+'.json')
        lookup={s:n for n,s in enumerate(signatures)}
        assert len(lookup)==len(signatures)
        for signature,index in transactions.items():assert lookup[signature]==index,(slot,signature,index,lookup.get(signature))
        checked.append(dict(slot=slot,verified_transactions=len(transactions),blockhash=block['blockhash'],
                            evidence_file=str(evidence.relative_to(OUT)),evidence_sha256=file_hash(evidence)))
        if (i+1)%10==0:print('BLOCK_ORDER_VERIFIED',i+1,'/',len(required),flush=True)
    assert all(file_hash(OUT/name)==digest for name,digest in hashes.items()),'input changed during independent ordering audit'
    save(OUT/'independent-block-order-verification.json',dict(complete=True,input_hashes=hashes,
         finalized_blocks=len(checked),verified_transactions=sum(x['verified_transactions'] for x in checked),
         checked=checked,scope='Recovered swaps and external movements; not a full-day chain coverage assertion'))
    print('ORDER_VERIFICATION_COMPLETE',len(checked),'blocks',sum(x['verified_transactions'] for x in checked),'transactions',flush=True)


if __name__=='__main__':main()
