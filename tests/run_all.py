"""Run every Daily Brief test suite. Network-free except test_location's
geocode checks (it hits Open-Meteo and postcodes.io, both keyless).

    python tests\\run_all.py
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUITES = ["test_render.py", "test_hardening.py", "test_location.py", "test_ics.py",
          "test_plan.py"]

failed = []
for suite in SUITES:
    r = subprocess.run([sys.executable, str(HERE / suite)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    tail = (r.stdout or "").strip().splitlines()
    verdict = tail[-1] if tail else "(no output)"
    print(f"{suite:22} {verdict}")
    if r.returncode != 0:
        failed.append(suite)
        for line in tail:
            if "FAIL" in line:
                print("   " + line.strip())
        if r.stderr:
            print("   stderr: " + r.stderr.strip().splitlines()[-1])

print()
if failed:
    print(f"FAILED: {', '.join(failed)}")
    sys.exit(1)
print("all suites passed")
