#!/usr/bin/env python3
"""Run everything that can run without a PC. Non-zero on any red.

This is what CI runs and what `ship` demands. 1.0.2 shipped with no runner at all, which is how two
Windows-only bugs (AUDIT.md F16) reached a human's machine through a chat window at 23:00.

  python3 tests/run_all.py            suite + docs check (fast, default)
  python3 tests/run_all.py --all      also run the measurement harnesses (bench, capture count, A/B)
  python3 tests/run_all.py --full     print the whole log, not just the failures
"""
import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def run(cmd):
    t0 = time.time()
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, errors="replace")
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    return r.returncode, round(time.time() - t0, 1), out


def report(out, full=False):
    """Every FAIL/ERROR block, by name, plus the summary line.

    The first version of this file printed `tail -3`. On the windows-latest leg that meant the CI log said
    "FAILED (failures=5)" with no names at all — useless to anyone who cannot reproduce the platform
    locally, which is the entire reason the leg exists. A CI harness that hides the failure is a harness
    that cannot be acted on.
    """
    lines = out.splitlines()
    names = re.findall(r"^(?:FAIL|ERROR)(?:\d+)?: (\S+ \(\S+\))", out, re.M)
    keep = []
    if names:
        blocks = re.split(r"\n(?=(?:FAIL|ERROR)(?:\d+)?: )", out)
        for b in blocks:
            if re.match(r"^(FAIL|ERROR)(?:\d+)?: ", b):
                keep.append("\n".join(b.rstrip().splitlines()[:14]))
    else:                                   # no failure names: show everything, something is wrong
        keep = ["\n".join(lines[-12:])]
    summary = [l for l in lines if re.match(r"^(Ran |OK|FAILED|SKIPPED|VERDICT|\d+/\d+)", l.strip())]
    body = keep if full or len(keep) <= 6 else keep[:6]
    for b in body:
        for l in b.splitlines():
            print(f"      {l}")
    if len(names) > 6:
        print(f"      … {len(names) - 6} more (run with --full)")
    shown = "\n".join(body)
    for l in summary[-3:]:
        if l.strip() not in shown.splitlines():      # do not print the same line twice
            print(f"      {l}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="also run the bench + capture harnesses")
    ap.add_argument("--full", action="store_true", help="print every failure block in full")
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
        code, secs, out = run(j)
        name = " ".join(j[1:]).replace(str(ROOT) + os.sep, "").replace(str(ROOT), "")
        print(f"{'PASS' if code == 0 else 'FAIL'}  {name}  ({secs}s)")
        if code != 0 or a.full:
            report(out, a.full)
        bad += code != 0
    osname = os.uname().sysname if hasattr(os, "uname") else "windows"
    total = len(jobs)
    print(f"\n{total - bad}/{total} green · pc-eye · python {sys.version_info.major}.{sys.version_info.minor}"
          f" on {osname}")
    if bad:
        print("A red here is a finding. It is not proof that a click landed — see SHIP_GATE.md.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
