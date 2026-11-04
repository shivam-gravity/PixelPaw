"""Run every automated test:  python tests/run_all.py

- test_offline.py      fakes for YouTube + AI: guidelines, routines, reports, i18n, live chat, web basics
- test_integration.py  real REST client vs a fake YouTube API: retries, quota, live chat, security, setup, health
- test_obs_lua.py      OBS script against a fake OBS API (skipped unless `pip install lupa`)
Manual: tests/obs_live_check.mjs checks your real, running OBS.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
failed = []
for name in ("test_offline.py", "test_integration.py", "test_obs_lua.py"):
    print(f"\n=== {name} ===", flush=True)
    r = subprocess.run([sys.executable, str(HERE / name)], cwd=HERE.parent, env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    if r.returncode:
        failed.append(name)
print("\n" + ("ALL TEST SUITES PASSED" if not failed else f"FAILED: {', '.join(failed)}"))
sys.exit(1 if failed else 0)
