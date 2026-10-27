#!/usr/bin/env python3
"""check_docs.py — the docs may not drift from the code.

Why this exists: 1.0.2 shipped with `VERSION = "1.0.0"` in a repo whose README and title said 1.0.2, and
`PC-EYE.md`'s refusal table had a row stranded inside a paragraph, so an agent reading the one file it is
told to trust got a table that did not answer its error code. A doc an agent trusts is part of the product
(AUDIT.md F16). Exit 1, with the exact mismatch named.

Usage: python3 tools/check_docs.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVED = {"/state", "/shot", "/act", "/selftest", "/favicon.ico", "/"}
REFUSALS = ("400", "401", "403", "409", "413", "415", "429", "503")


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


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
    tests = len(re.findall(r"def test_", read("tests/test_pc_eye.py")))
    declared = re.findall(r"(\d+) tests, headless-safe", readme)
    if declared and int(declared[0]) != tests:
        fails.append(f"README advertises {declared[0]} tests; the suite defines {tests}")

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

    if fails:
        print("DOC DRIFT:")
        for f in fails:
            print("  x", f)
        return 1
    print(f"docs in sync with pc-eye {ver} - {tests} tests - refusal table complete and ordered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
