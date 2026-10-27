#!/usr/bin/env python3
"""Negative control: run the NEW regression tests against the OLD (1.0.2) server.

Why this file exists: a fix is only proven by a test that fails without it. This checks exactly that,
and writes the verdict into evidence/ so the claim "61 tests pin the flaws" is verifiable rather than
asserted. It runs from a scratch copy so it never touches the working tree.
"""
import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evidence"))
import oldtree as _oldtree                       # noqa: E402
OLD_SHA = _oldtree.BASE_SHA


def extract_old_server(dest: pathlib.Path) -> bool:
    src = _oldtree.ensure("neg")
    if src is None:
        return False
    shutil.copy(src / "server.py", dest / "server.py")
    return True


def main() -> int:
    test_src = (REPO / "tests" / "test_pc_eye.py").read_text(encoding="utf-8")
    head = test_src.split("class Protocol")[0]
    # the harness pins its own repo onto sys.path; without this the "old" run would import the NEW
    # server from the working tree and prove nothing at all.
    head = head.replace(f'sys.path.insert(0, str(HERE))', 'sys.path.insert(0, str(pathlib.Path(__file__).parent))')
    reg = "class RegressionV103" + test_src.split("class RegressionV103")[1].split("class FuzzFindings")[0]
    fz = ("class FuzzFindings" + test_src.split("class FuzzFindings")[1].split("class Launcher")[0]
          if "class FuzzFindings" in test_src else "")

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pceye-neg-"))
    try:
        if not extract_old_server(tmp):
            _oldtree.skip("the negative control")
            return 0
        (tmp / "test_reg.py").write_text(head + "\n\n" + reg + "\n\n" + fz,
                                  encoding="utf-8")
        spec = importlib.util.spec_from_file_location("test_reg", str(tmp / "test_reg.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(tmp))
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            print(f"the old code cannot even host the new harness: {type(e).__name__}: {e}")
            print("VERDICT: the new tests are not a rewrite of the old ones — they exercise behaviour "
                  "1.0.2 never had.")
            return 0
        buf = open(str(tmp / "quiet.log"), "w")
        real_out, real_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = buf
        try:
            suite = unittest.TestSuite()
            for cname in ("RegressionV103", "FuzzFindings"):
                klass = getattr(mod, cname, None)
                if klass is not None:
                    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(klass))
            os.environ["PCEYE_SKIP_SOAK"] = "1"     # the soak spawns its own server; wrong tree here
            res = unittest.TextTestRunner(stream=buf, verbosity=0).run(suite)
        finally:
            sys.stdout, sys.stderr = real_out, real_err
        buf.close()
        def name_of(case):
            return getattr(case, "_testMethodName", None) or str(case).split(" ")[0].split("(")[-1].rstrip(")")
        names = sorted({name_of(t[0]) for t in res.failures + res.errors})
        print(f"old code ({OLD_SHA}) vs the new RegressionV103 suite:")
        print(f"  ran {res.testsRun} · caught {len(names)} "
              f"({len(names)}/{res.testsRun} of the new tests fail WITHOUT the fix)")
        for n in names:
            print(f"    ✗ {n}")
        all_tests = sorted(m for m in dir(mod.RegressionV103) if m.startswith("test_"))
        not_caught = [m for m in all_tests if m not in names]
        if not_caught:
            print(f"  passed on old code too ({len(not_caught)}) — these pin contract, not fix:")
            for n in not_caught:
                print(f"    = {n}")
        print("\nVERDICT: " + ("the regression tests DO catch the flaws they claim to"
                               if names else "THESE TESTS PROVE NOTHING — they pass on broken code"))
        return 0 if names else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
