"""Render the bundled notebook using a temporary, computer-local kernel spec."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def main():
    engine = ROOT / 'onchain-backtest-engine'
    with tempfile.TemporaryDirectory(prefix='sniper-review-kernel-') as folder:
        spec = Path(folder) / 'kernels/sniper-research'
        spec.mkdir(parents=True)
        (spec / 'kernel.json').write_text(json.dumps(dict(
            argv=[sys.executable, '-m', 'ipykernel_launcher', '-f', '{connection_file}'],
            display_name='Sniper review (temporary local kernel)', language='python')))
        env = dict(os.environ)
        env['JUPYTER_PATH'] = str(Path(folder)) + (os.pathsep + env['JUPYTER_PATH'] if env.get('JUPYTER_PATH') else '')
        env['PYTHONPATH'] = str(engine / 'src')
        env['PYTHONNOUSERSITE'] = '1'
        subprocess.run([sys.executable, str(engine / 'notebooks/build_sniper_repaired_review.py')],
                       cwd=engine, env=env, check=True)
    print('Notebook refreshed from bundled local inputs.')


if __name__ == '__main__':
    main()
