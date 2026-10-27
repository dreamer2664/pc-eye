#!/usr/bin/env python3
"""Run everything that can run without a PC. Non-zero on any red.

This is what CI runs and what `ship` demands. 1.0.2 shipped with no runner at all, which is how two
Windows-only bugs (AUDIT.md F16) reached a human's machine through a chat window at 23:00.

  python3 tests/run_all.py            suite + docs check (fast, default)
  python3 tests/run_all.py --all      also run the measurement harnesses (bench, capture count, A/B)
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def run(cmd):
    t0 = time.time()
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    tail = [l for l in out.splitlines() if l.strip()][-3:]
    return r.returncode, round(time.time() - t0, 1), tail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="also run the bench + capture harnesses")
    a = ap.parse_args()
    jobs = [
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        [sys.executable, str(ROOT / "tools" / "check_docs.py")],
    ]
    if a.all:
        jobs.append([sys.executable, str(ROOT / "evidence" / "capture_count.py")])
        jobs.append([sys.executable, str(ROOT / "evidence" / "negative_check.py")])
    bad = 0
    for j in jobs:
        code, secs, tail = run(j)
        name = " ".join(j[1:]).replace(str(ROOT) + "/", "")
        print(f"{'PASS' if code == 0 else 'FAIL'}  {name}  ({secs}s)")
        for line in tail:
            print(f"      {line}")
        bad += code != 0
    osname = os.uname().sysname if hasattr(os, "uname") else "windows"
    total = len(jobs)
    print(f"\n{total - bad}/{total} green · pc-eye · python {sys.version_info.major}.{sys.version_info.minor}"
          f" on {osname}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
