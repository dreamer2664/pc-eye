#!/usr/bin/env python3
"""Run everything that can run without a PC. Non-zero on any red.

This is what CI runs and what `ship` demands. 1.0.2 shipped with no runner at all, which is how two
Windows-only bugs (AUDIT.md F16) reached a human's machine through a chat window at 23:00.

The suite runs twice, in two orders, on purpose. `unittest discover` loads classes A→Z; pytest loads them in
source order. AUDIT.md F43 was a test that poisoned a shared class seam and only the second order could see it —
so the gate had a runner-shaped hole, and green CI meant "green in the order CI happens to pick". One order is a
coin flip; a gate may not be a coin flip. The third pass runs each class alone in its own process, which is
how you catch the inverse: a test that is green only because something before it left a stub behind.

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
    # encoding="utf-8" because on Windows text=True means "decode with cp1252", and our own log lines
    # contain em-dashes: the harness would mangle or die on the output of a perfectly healthy run.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, errors="replace",
                       encoding="utf-8", env=env)
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    return r.returncode, round(time.time() - t0, 1), out


MAX_BLOCKS = 40        # never more than this, but never FEWER than the failures either


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
                # F53: the 14-line cap was written for hand-written unittest blocks, and on CI it cut the
                # harness's `hard failures: N` summary out of the only failing block — I read a red whose cause
                # had been truncated away, in the file whose docstring says a red that cannot be read cannot be
                # fixed. Keep the head of the block, then any line that states a verdict or a count.
                bl = b.rstrip().splitlines()
                # F53, second half: keeping only the *summary* line still hid the payload. CI printed
                # `hard failures: 3` and nothing else, because the three items are the next three lines and the
                # cap stopped at 14. The first block gets more room (it explains the rest) and any line that
                # reads like a finding or a verdict is kept from anywhere in the block.
                head = 34 if keep == [] else 14
                extra = [l for l in bl[head:]
                         if re.search(r"hard failures:|VERDICT|status codes:|server alive after|races \(|"
                                      r"failures=|errors=|Ran |non-JSON body|client-side |stopped answering|"
                                      r"Traceback \(most recent call last\)", l)]
                keep.append("\n".join(bl[:head] + extra[:20]))
    else:                                   # no failure names: show everything, something is wrong
        keep = ["\n".join(lines[-12:])]
    summary = [l for l in lines if re.match(r"^(Ran |OK|FAILED|SKIPPED|VERDICT|\d+/\d+)", l.strip())]
    body = keep if full or len(keep) <= MAX_BLOCKS else keep[:MAX_BLOCKS]
    for b in body:
        for l in b.splitlines():
            print(f"      {l}")
    if len(names) > MAX_BLOCKS:
        print(f"      … {len(names) - MAX_BLOCKS} more blocks suppressed (run with --full)")
    if len(names) > len(body):            # always list the names, even past the block cap
        print("      failing: " + "; ".join(n.split(" ")[1] for n in names))
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
        [sys.executable, str(HERE / "run_ordered.py")],
        [sys.executable, str(HERE / "run_ordered.py"), "--isolated"],
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
