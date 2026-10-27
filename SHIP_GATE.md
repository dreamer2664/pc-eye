# SHIP_GATE.md — pc-eye's own gate (nothing reaches main without this green)

Same contract as the agency: `ship` runs every line starting with `$ `, in order, from the repo root.
Any failure = no ship. Merge authority follows the gate, not a manager — and the code that touches a
human's mouse is exactly the code that should not be merged on feel.

$ python3 tools/check_docs.py
$ python3 tests/run_all.py --all
$ python3 evidence/fuzz_protocol.py --requests 20000 --seed 7

## One command, so the branch rule is enforced by the machine

    sh tools/install_hooks.sh        # sets core.hooksPath -> tools/git-hooks

`tools/git-hooks/pre-commit` refuses any commit made on `main`. It is not part of the gate's `$` lines
(a gate cannot install itself), but it is the reason the rule below is now mechanical rather than moral.

## The three git/PC rules this file is run under (F39, F42 — learned the expensive way)

* A commit message with backticks or `$` in it is a **program**: `git commit -m "…"` lets the shell run it
  before git sees it. Every message here goes through `git commit -F - <<'MSG'` with a quoted delimiter.
* A `git merge` inside an `&&` chain can print success and change nothing. Merge in its own call, then prove
  it: `git rev-parse --short main` and `git merge-base --is-ancestor <branch> main`. Never trust an echo.
* Textual I/O names its encoding. `open(...)`/`read_text()`/`write_text()` without `encoding=` is a bug in
  this repo even when the code is Linux-only, because CI is not: Windows' cp1252 default cannot read
  `server.py`'s em-dashes (F42). `tools/check_docs.py` enforces it with an `ast` walk, so it needs no memory.

## What a green gate does NOT prove — say this out loud, do not paper over it

* it does **not** prove a click lands on the right pixel. Only `/selftest`, `coord_space.consistent` and
  `exact` on the real machine do that. That sentence is the whole reason this project exists.
* it does **not** run on Windows here. `.github/workflows/ci.yml` has a `windows-latest` leg that does,
  executing `ctypes.windll`, `ImageGrab`, `GetSystemMetrics` and the clipboard for real. A merge is
  "verified by CI", not "verified by Linux alone", once that leg is green.
* capture-side speed is **counted** (`evidence/capture_count.py`), not timed, on a headless box: a sandbox
  with no display cannot honestly measure a screen grab, and pretending otherwise is the failure mode
  this repo keeps paying for.
* `evidence/FUZZ.txt` is the 200,000-request record behind F27–F33. The generator's invariant is not "the
  right status code" but "**an answer, in JSON, always**": every bug it found was a closed socket, which is
  what a dead PC looks like to an agent. Add a route or a parameter and the generator covers it for free.
* `evidence/negative_check.py` is the check that the suite is not decoration: it runs the new tests
  against the pre-fix tree and reports how many fail. If that number is 0, the tests prove nothing.
