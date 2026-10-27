#!/usr/bin/env python3
"""Run the test module with its classes in SOURCE order, in one process.  Exit non-zero on any red.

Why this file exists (AUDIT.md F43): `unittest discover` — what CI runs — sorts test classes alphabetically,
while pytest keeps them in file order. A test class that leaves a patched class attribute behind therefore
breaks only the classes that follow it, and for a day the two runners disagreed about whether pc-eye was fine:
red on the laptop, green in CI, both telling the truth about the order they ran in. A gate that samples one
order is a gate that rolls a die.

pytest is not a dependency here (it is absent from the Windows images CI uses), so the order comes from the AST:
parse the file, take the TestCase classes as a reader meets them, hand them to unittest one at a time.

  python3 tests/run_ordered.py [-v]              whole module, source order, one process
  python3 tests/run_ordered.py --isolated       every class in its own process (can it stand alone?)
"""
import ast
import os
import pathlib
import subprocess
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
TARGET = HERE / "test_pc_eye.py"
MODULE = "test_pc_eye"
PKG = "tests."            # how `python -m unittest tests.test_pc_eye.X` wants it

os.environ["PCEYE_ORDERED"] = "1"          # tests that shell back into this runner check it, to stop recursion
# One soak per CI job, not one per pass. The soak is the suite's only heavy, timing-sensitive step, and since
# F52 the job runs three passes — tripling contention on a shared runner made the soak itself go red at a
# docs-only commit (F54). Ordering coverage does not need it: the primary pass owns it.
os.environ.setdefault("PCEYE_SKIP_SOAK", "1")


def classes_in_source_order(path=TARGET):
    """Every top-level TestCase class, in the order the file defines them."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [n.name for n in tree.body
            if isinstance(n, ast.ClassDef) and any("TestCase" in ast.unparse(b) for b in n.bases)]


def suite():
    sys.path.insert(0, str(HERE.parent))
    loader = unittest.TestLoader()
    names = classes_in_source_order()
    if not names:
        # "0 tests" printed as a success is the failure mode this repo has now been bitten by three times
        print(f"VERDICT: no TestCase classes found in {TARGET} — the order check is checking nothing",
              file=sys.stderr)
        return None
    return unittest.TestSuite([loader.loadTestsFromName(f"{MODULE}.{name}") for name in names])


def isolated():
    """Run every class in its OWN process, and fail if any of them cannot stand alone.

    The aggregate runs answer "does anything break when it has company?" This answers the other question, the
    one that found F44: `Protocol.test_state_reports_screen_cursor_and_last_action` asserted a fake screen size
    it had not asked for — it was inheriting a `display_rect` stub that a later class welded onto the shared
    handler, and on Windows the honest answer (the real desktop, 1024x768) made that visible. A test that is
    green only in company is not green.
    """
    # A module that cannot be imported looks exactly like a class that cannot pass: one red line each. Print
    # the first real error before the verdict so the reader is not chasing a fake failure (this version of the
    # bug was mine: `tests.Protocol` instead of `tests.test_pc_eye.Protocol` gave 8 "failures" and one
    # ImportError, and the 8 were noise).
    probe = subprocess.run([sys.executable, "-c", f"import {PKG}{MODULE}"],
                           capture_output=True, text=True, errors="replace", encoding="utf-8",
                           cwd=str(HERE.parent), env={**os.environ, "PCEYE_ORDERED": "1"})
    # `python -m unittest <module>` is NOT a usable probe: unittest converts a broken import into a synthetic
    # `_FailedTest`, so it reports "Ran 1 test … FAILED (errors=1)" and looks like a test result. Import the
    # module and read the exit code, which has exactly one meaning.
    if probe.returncode != 0:
        print(f"{MODULE} does not import — every class below would be noise:\n"
              + "\n".join(((probe.stderr or "") + (probe.stdout or "")).strip().splitlines()[-8:]),
              file=sys.stderr)
        return 1
    rc = 0
    names = classes_in_source_order()
    if not names:
        print(f"VERDICT: no TestCase classes found in {TARGET}", file=sys.stderr)
        return 1
    for name in names:
        r = subprocess.run([sys.executable, "-m", "unittest", f"{PKG}{MODULE}.{name}"],
                           capture_output=True, text=True, errors="replace", encoding="utf-8",
                           cwd=str(HERE.parent), env={**os.environ, "PCEYE_ORDERED": "1"})
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        print(f"{'PASS' if r.returncode == 0 else 'FAIL'}  {name:20} {tail[-1][:60] if tail else ''}")
        rc |= r.returncode
    print(f"VERDICT {len(names)} classes, each alone: {'green' if rc == 0 else 'RED'}")
    return 1 if rc else 0


def main():
    if "--isolated" in sys.argv:
        return isolated()
    s = suite()
    if s is None:
        return 1
    res = unittest.TextTestRunner(verbosity=2 if "-v" in sys.argv else 1).run(s)
    ok = res.wasSuccessful()
    print(f"VERDICT {res.testsRun} tests in {len(list(s))} classes, source order: "
          f"{'green' if ok else 'RED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
