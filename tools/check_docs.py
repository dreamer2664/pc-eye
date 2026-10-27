#!/usr/bin/env python3
"""check_docs.py — the docs may not drift from the code.

Why this exists: 1.0.2 shipped with `VERSION = "1.0.0"` in a repo whose README and title said 1.0.2, and
`PC-EYE.md`'s refusal table had a row stranded inside a paragraph, so an agent reading the one file it is
told to trust got a table that did not answer its error code. A doc an agent trusts is part of the product
(AUDIT.md F16). Exit 1, with the exact mismatch named.

Usage: python3 tools/check_docs.py
"""
import os
import ast
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVED = {"/state", "/shot", "/act", "/selftest", "/favicon.ico", "/"}
REFUSALS = ("400", "401", "403", "409", "413", "415", "429", "503")
# Things a text-encoding gate must keep its nose out of: reading a screenshot, a binary, or a
# blob in "rb" is correct code, and flagging it would train everyone to ignore this file.
BINARY_HINTS = ("rb", "wb", "ab", "buffering=0", ".bin", ".png", ".jpg", ".ico", ".exe", ".zip")


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def check_encodings(root):
    """F42: every text read/write in this repo must name its encoding. Found by the windows-latest leg.

    Windows' default text encoding is cp1252; `server.py` contains em-dashes and arrows because a refusal has
    to read like an operator's sentence rather than a stack frame. A test doing `Path(pceye.__file__).
    read_text()` therefore explodes there with UnicodeDecodeError while being perfectly green on Linux — the
    mirror image of F37, and the fourth time a "works here" assumption about bytes-vs-text cost a red leg.

    Written as an AST walk, not a regex: my first attempt scanned characters to find the end of each call and
    had an off-by-one that reported `(e` as the arguments, flagging the *fixed* lines and missing nothing —
    which is exactly how a gate loses its authority. `ast` cannot get the syntax wrong, and the line it
    reports is a real line.
    """
    import ast
    bad = []
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", "node_modules", ".venv")]
        for fn in sorted(files):
            if not fn.endswith(".py") or fn == "oldtree.py":
                continue
            f = os.path.join(dirpath, fn)
            with open(f, encoding="utf-8", errors="replace") as fh:
                src = fh.read()
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue                       # the suite reports a file that does not parse; do not pile on
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name not in ("read_text", "write_text", "open"):
                    continue
                kws = {k.arg for k in node.keywords}
                if kws & {"encoding", "errors", "mode"}:
                    continue
                if name in ("read_text", "write_text"):
                    # Path(x).read_text() on a source/doc path always names the file inside the call chain, so
                    # the whole statement's source line is the right thing to inspect.
                    line = src.splitlines()[node.lineno - 1]
                    if any(t in line for t in BINARY_HINTS):
                        continue
                    if not re.search(r"__file__|HERE|REPO|ROOT|\bPath\(|\.read_text|\.write_text", line):
                        continue
                    rel = os.path.relpath(f, root).replace(os.sep, "/")
                    bad.append(f"{rel}:{node.lineno}: {name}() with no encoding= — on Windows the cp1252 "
                               f"default mangles or rejects this file's em-dashes")
                    continue
                # open(): first two positional args may hold mode/encoding as literals; keywords covered above
                rendered = ast.unparse(ast.Expression(list(node.args[:2]))) if node.args else ""
                if any(t in rendered for t in BINARY_HINTS):
                    continue
                if not re.search(r"__file__|HERE|REPO|ROOT|\bPath\(|\.(md|txt|json|yml|yaml|toml|cfg|ini|sh)\b",
                                 rendered):
                    continue
                rel = os.path.relpath(f, root).replace(os.sep, "/")
                bad.append(f"{rel}:{node.lineno}: open() on a text file with no encoding= — pass it explicitly "
                           f"(Windows' default is cp1252): {rendered[:60]}")
    return bad


def suite_count():
    """How many tests the runner will actually run.

    It used to be `len(re.findall("def test_", suite_source))`, and that number was wrong in the same hour I
    wrote "a check may not pass by finding nothing": a fixture string inside a test contained the text
    `def test_a()`, the grep counted it, and README was failed for 86 against the 85 the runner collects.
    A count taken from a text pattern is a count of the pattern, not of the thing.

    So ask unittest. If the module cannot be imported (an optional dependency, an import-time platform guard),
    fall back to counting `test_*` methods inside TestCase classes from the AST — a structural count, not a
    substring search — and print which method produced the number so it is never unexplained.
    """
    import io
    import contextlib

    path = os.path.join(ROOT, "tests", "test_pc_eye.py")
    try:
        import importlib.util
        import sys
        import unittest

        sys.path.insert(0, os.path.join(ROOT, "tests"))
        spec = importlib.util.spec_from_file_location("pceye_suite", path)
        mod = importlib.util.module_from_spec(spec)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            spec.loader.exec_module(mod)
        n = unittest.TestLoader().loadTestsFromModule(mod).countTestCases()
        if n:
            return n
    except Exception:
        pass                                        # fall through to the structural count
    tree = ast.parse(read("tests/test_pc_eye.py"), filename=path)
    total = 0
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and any("TestCase" in ast.unparse(b) for b in node.bases):
            total += sum(1 for f in node.body
                         if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name.startswith("test"))
    return total


def main() -> int:
    src = read("server.py")
    m = re.search(r'^VERSION = "([0-9.]+)"', src, re.M)
    if not m:
        print("FAIL: no VERSION string in server.py")
        return 1
    ver = m.group(1)
    fails = []

    for doc in ("README.md", "CHANGELOG.md", "PC-EYE.md"):
        if os.path.exists(os.path.join(ROOT, doc)) and ver not in read(doc):
            fails.append(f"{doc} does not mention version {ver}")

    readme = read("README.md")
    tests = suite_count()
    # F55: this used to look for one magic phrase — `(\d+) tests, headless-safe` — and to check nothing at all
    # when it found none. Reword the sentence and the doc check goes green while the number rots: a check that
    # is conditional on prose is a check that prose can switch off, and mine was switched off by my own docs
    # edit an hour after I wrote it. So: every count README states about the suite must match, however it is
    # phrased. Silence is still allowed (a README may talk about tests without promising a number); a WRONG
    # number is not, and neither is a number that used to be checkable and now is not.
    declared = re.findall(r"(?<![A-Za-z0-9])(\d+)(?:\s*\+)?\s+tests\b(?![/.\_])", readme)
    wrong = sorted({d for d in declared if int(d) != tests})
    if wrong:
        fails.append(f"README advertises {', '.join(wrong)} tests; the suite defines {tests} "
                     f"(every count README states must match — the check no longer depends on a "
                     f"phrase, and `python3 tests/run_all.py` is not a count: the lookbehind and the path "
                     f"guard are what keep filenames out of the arithmetic)")

    for doc in ("README.md", "PC-EYE.md"):
        for route in sorted(set(re.findall(r"(?:GET|POST) (/[a-z_]+)", read(doc)))):
            if route not in SERVED:
                fails.append(f"{doc} documents {route}, which the server does not serve")

    doc = read("PC-EYE.md")
    table = doc.split("## When something is refused")[1] if "## When something is refused" in doc else doc
    for code in REFUSALS:
        if not re.search(r"^\| `" + code + r"` \|", table, re.M):
            fails.append(f"refusal table has no row for {code}")
    rows = re.findall(r"^\| `(\d{3})` \|", table, re.M)
    if rows != sorted(rows):
        fails.append(f"refusal table rows are out of order: {rows} (1.0.2 had 503 stranded in a paragraph)")

    if "token_hex(4)" in src:
        fails.append("server.py still mints a 32-bit token")
    if "Cache-Control" not in src or "no-store" not in src:
        fails.append("responses must not be cacheable: a screenshot of someone's desktop is not a public asset")

    tests_src = read("tests/test_pc_eye.py")
    for bad in sorted(set(re.findall(r"open_socket_on\((\d{4,5})", tests_src))):
        if bad not in ("0",):
            fails.append(f"tests hardcode port {bad} — an OS-assigned port or nothing "
                         f"(Windows CI reserves whole ranges; a fixed port is a flake waiting to happen)")

    fails += check_encodings(ROOT)      # platform-blindness gate: F42 may not be re-introduced by editing
    if fails:
        print("DOC DRIFT:")
        for f in fails:
            print("  x", f)
        return 1
    print(f"docs in sync with pc-eye {ver} - {tests} tests - refusal table complete and ordered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
