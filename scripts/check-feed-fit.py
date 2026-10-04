"""Run the current integrated-petal fit review (replaces the old shoe/backer check)."""
import runpy,subprocess
from pathlib import Path
root=Path(__file__).resolve().parents[1]
subprocess.run(['node','scripts/check-feed-envelope.mjs'],cwd=root,check=True)
runpy.run_path(str(root/'scripts/check-feed-envelope.py'),run_name='__main__')
