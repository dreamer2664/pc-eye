#!/usr/bin/env python3
"""Run the test module with its classes in SOURCE order, in one process.  Exit non-zero on any red.

Why this file exists (AUDIT.md F43): `unittest discover` — what CI runs — sorts test classes alphabetically,
while pytest keeps them in file order. A test class that leaves a patched class attribute behind therefore
breaks only the classes that follow it, and for a day the two runners disagreed about whether pc-eye was fine:
red on the laptop, green in CI, both telling the truth about the order they ran in. A gate that samples one
order is a gate that rolls a die.

pytest is not a dependency here (it is absent from the Windows images CI uses), so the order comes from the AST:
parse the file, take the TestCase classes as a reader meets them, hand them to unittest one at a time.

  python3 tests/run_ordered.py [-v]
"""
import ast
import os
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
TARGET = HERE / "test_pc_eye.py"
MODULE = "test_pc_eye"

os.environ["PCEYE_ORDERED"] = "1"          # tests that shell back into this runner check it, to stop recursion


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


def main():
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
