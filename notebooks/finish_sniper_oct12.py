"""Finish the already launched two-run experiment if the chat is interrupted."""
from pathlib import Path
import json
import os
import subprocess
import sys
import time
from sniper_carbon_day import OUT,ENGINE,save,file_hash


def main():
    jobs={'control-6h-wallet1000':int(sys.argv[1]),'full24h-wallet10000':int(sys.argv[2])}
    began=time.monotonic()
    while True:
        completed=[]
        for name,pid in jobs.items():
            manifest=OUT/name/'manifest.json'
            if manifest.exists() and json.loads(manifest.read_text()).get('complete') and (OUT/(name+'-receipt.json')).exists():
                completed.append(name);continue
            try:os.kill(pid,0)
            except ProcessLookupError:
                # The process can finish between the manifest and liveness checks.
                if manifest.exists() and (OUT/(name+'-receipt.json')).exists():continue
                save(OUT/'job-status.json',dict(phase='failed',run=name,reason='Native process exited without complete results',completed=completed));return 1
        if len(completed)==len(jobs):break
        save(OUT/'job-status.json',dict(phase='running',completed=completed,pending=[n for n in jobs if n not in completed],elapsed_finalizer_seconds=round(time.monotonic()-began,1)))
        if time.monotonic()-began>14400:
            save(OUT/'job-status.json',dict(phase='attention',reason='Four hour finalizer bound reached; native processes were not stopped',completed=completed));return 2
        time.sleep(5)
    subprocess.run([sys.executable,str(ENGINE/'notebooks/summarize_sniper_oct12.py')],cwd=ENGINE,check=True)
    save(OUT/'job-status.json',dict(phase='complete',completed=list(jobs),comparison_sha256=file_hash(OUT/'comparison.csv'),verified=True))
    print('BOTH_NATIVE_BACKTESTS_AND_VERIFIED_COMPARISON_COMPLETE',flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
