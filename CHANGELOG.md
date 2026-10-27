# CHANGELOG — pc-eye

Append-only. Correcting an old entry means adding a new one, not editing history.

## 1.0.3 — 2026-10-27 (vesper)

**Whole-process audit + hardening. `AUDIT.md` is the record; this is the summary.**
26 findings (F1–F26): 6 CRIT, 8 HIGH. Every one is either fixed with a named regression test or reported
with a recommendation. `evidence/negative_check.py` proves **35 of the 38 new tests fail against 1.0.2**.

### Second pass — a fuzzer, seven more findings, four of them in this release's own code

`evidence/fuzz_protocol.py` generates malformed requests and asserts two absolute properties: the server
**always answers** (a bad argument is a 400 with a sentence, never a closed socket) and it **prints no
traceback**. 200,000 requests over five seeds: zero of either. It found F27–F33, recorded in `AUDIT.md`:

* **F27** `/shot?fmt=png&quality=50` raised `UnboundLocalError` inside the handler — `q` existed only in the
  JPEG arm while the meta dict read it unconditionally. Shipped earlier the same morning, invisible to every
  hand-written test because each parameter passed alone. Fixed: quality resolves once for both formats.
* **F28** `{"x": true}` clicked pixel 1 (`isinstance(True, int)` is true in Python). A bool is now refused
  as a bool, everywhere.
* **F29** `{"x": 3.5}` was silently truncated to 3. Fractional coordinates are refused; `3.0` still works.
* **F30** JSON `1e999` (Infinity) reached `int()` → `OverflowError` escaped on four routes. Finiteness is
  checked before any conversion, and the ordering is commented because `f != int(f)` on infinity *is* the crash.
* **F31** the optional knobs (`clicks`, `times`, `seconds`, `interval`) were never parsed at all:
  `clicks:"three"` raised inside the Windows branch, `interval:3600` slept an hour per character,
  `clicks:1e309` went to the OS as a wheel delta. `strict_int()` parses them at the door.
* **F32** `ctrl++` dropped the empty part and pressed `ctrl`. A half-typed chord is now refused, with the
  correct spelling in the message — something-else is worse than nothing.
* **F33** a peer that hangs up mid-write (pull-to-refresh, `curl --max-time`, a proxy giving up) killed the
  handler thread and printed a `BrokenPipeError` for a non-event. `_write()` swallows exactly those two
  exceptions and nothing else, so a traceback that *does* appear still means a bug.

`scroll clicks:0` used to answer `ok:true` having moved nothing (a legal JSON `1e-999` underflows to 0).
Refused now — the same `ok`-shaped lie as F2, arriving from a completely different direction.

Two of the fixes introduced their own bug and were caught by the same machinery (documented in `AUDIT.md`):
a finiteness guard that called `float()` on a huge int, and a whole-number test that compared a value with
its own truncation. Suite 67 → 77; the negative control now runs 38 tests against 1.0.2 and 35 go red.

### F42 — textual I/O in this repo must name its encoding (`tools/check_docs.py` enforces it)

Windows' default codec is cp1252, and `server.py` contains em-dashes by design. Tests that read the product's
own source, and `evidence/negative_check.py` writing it into a tmp file, were therefore green on Linux and
`UnicodeDecodeError`/`UnicodeEncodeError` on the windows-latest leg. Fixed the eight call sites; added an
`ast`-based gate in `tools/check_docs.py` that fails the build on any `read_text`/`write_text`/`open` of a text
file with no `encoding=`, shipped with a negative control (2/2 caught, 4/4 deliberately skipped) because a
check that cries wolf gets ignored — the first draft, written as a character-scanning regex, did exactly that.

### F41 — the response guard guarded the wrong write (Windows-only, found by the CI leg)

`_write()` protected the body write, which is the only place Linux reports a vanished peer; on Windows the
abort arrives from the header flush as `ConnectionAbortedError`/winsock 10053, and a refusal like the 403
cross-origin has no body to guard. All responses now go through one funnel, `_respond()`, whose swallow test
is `Handler._peer_vanished(e)` — named connection errors ∪ an errno list that includes the winsock codes, and
nothing else (a full disk must stay loud). The fix's own two bugs are recorded in `AUDIT.md` so they read as
reasons rather than superstition: `except (self.PEER_GONE, OSError)` is a runtime error, not a syntax one, and
closing `wfile` inside the guard produced a fresh `ValueError` on the stdlib's own final flush. Suite 77 → 79;
the fuzz oracle was widened at the same time (it had been blind to bare `TypeError`/`NameError` lines).

### Two process fixes, because the process is part of the product (F39–F40)

`AUDIT.md` now records that three separate losses this shift came from *quoting*, not logic — a YAML colon
that failed CI at parse time with no job and no log; a `git merge` inside an `&&` chain that reported success
while main did not move; and backticks in a double-quoted `git commit -m`, which the shell executed, ate my
own evidence, created a stray file and aborted the `git checkout -b` in front of it. The rules are written
down (quoted heredocs for messages; one git command per call with the proof in the next call) and the branch
rule is now a hook — `tools/install_hooks.sh`, `tools/git-hooks/pre-commit` refuses commits on `main` —
because a rule an agent promises to remember is a rule that breaks under load. I proved that twice in an hour.

### The windows-latest leg earned its keep (F35–F38)

First run red with 5 failures while Linux was green. Three were my *tests* being wrong about Windows
(asserting `input_space() is None`, assuming ephemeral ports come in adjacent runs, decoding a healthy
child's em-dashes as cp1252), and one was a real defect: `build(port, …, sock=)` orphaned a socket, because
`ThreadingHTTPServer(bind_and_activate=False)` still runs `server_bind()` and replaces `self.socket` —
reported by `-X dev` as a ResourceWarning, and by CI as `PYTHONWARNINGS=error::ResourceWarning`. Now the
pre-bound socket is installed before activation, and `server.py`/the harnesses force UTF-8 with
`backslashreplace`, because a `print()` that raises `UnicodeEncodeError` inside a handler is indistinguishable
from a crashed eye on the one platform where the console codepage is not UTF-8.

### Housekeeping a merge should not have to mention

`start.sh` had lost its executable bit in a snapshot/restore cycle (it is 100755 upstream, which `git ls-tree`
confirmed). Restored, plus a `.gitattributes` pinning `*.sh text eol=lf` and `*.bat text eol=crlf`: a shell
script with CRLF endings fails with `/bin/bash^M: bad interpreter`, and a Windows clone is exactly where that
would happen.

### A confident lie became a refusal
* **F1** `/act` now **stops** when the eyes and the hand are in different coordinate spaces (the prose
  promised this since 1.0.1; the code only bounds-checked). `coord_gate()` is pure → unit-testable.
* **F2** a `paste` the clipboard refused returned `200 {"ok":true}`; it is now a 409 that says NOT pasted.
* **F3** `--dry-run` was invisible: `dry_run` now appears in `/state`, `/act`, `/selftest` (+ a `TEST-MODE`
  check), in the log, and on the console banner.
* **F4** `key` could not type punctuation and did not validate `times`; single characters now always go
  through `KEYEVENTF_UNICODE` (shift-free, exact).
* **F5** `/selftest` reported an impossible log path as writable; it now proves the log **by writing it**.
* **F6** an off-screen `region` returned black (reads as "empty desktop"); refused as 400 — and a second
  monitor at **negative** coordinates is now described instead of refused.

### Doors closed
* **F7** token: 8 hex (32 bits) → **32 hex (128 bits)**; a short `--token` is warned about and graded red
  by `/selftest`. **No token may ever be posted to a wire, a chat log or a screenshot.**
* **F8** `/act` requires `Content-Type: application/json` (a browser cannot fake that without a preflight)
  and refuses a foreign `Origin` with 403. 1.0.2 accepted a cross-origin `text/plain` POST = CSRF.
* **F9** bounds on everything: type 20k chars, key `times` 100, paste 200k, body 1 MB (413);
  `/act` 120/min and `/shot` 180/min (429 **with a remedy**), failing **open**.
* **F10** four crash paths that looked exactly like a dead PC (`?max=abc`, a list body, a string
  coordinate, `ctypes.windll` on Linux) are 400s that name the field.

### Faster, measured (`evidence/BENCH_*.json`, `evidence/CAPTURE_COUNT.txt`)
* **F12** `/state` grabbed the whole screen to read a width → asks the OS, cached 20 s.
  On a real PC: **2 pixel captures per look+check cycle → 1**; `/selftest` 2 → 1.
* **F13** `optimize=True` on every shot cost 25 % of the encode for 5 % of the bytes → default off,
  `?opt=1` back. **42.62 ms → 31.81 ms** on an identical 1080p frame.
* **F14** `X-Screen-Hash` + `GET /shot?changed=HASH` → **304 with no body**: watching a build now costs a
  header instead of a frame.
* **F15** `?max=0` (aim mode) **−21 %** at pinned policy, **−35 %** at each build's own default.
* Cost of all the new safety work: **~0.1 ms per request** (`/shot` +0.7 %, inside noise).

### The launch path
* **F16** added `tests/run_all.py` and `.github/workflows/ci.yml` with a **windows-latest** leg — the leg
  that actually executes the ctypes/ImageGrab/clipboard paths the two historical bugs hid in.
* **F17** `start_tunnel` could block 45 s silently and left cloudflared running after Ctrl+C; it now
  reports progress and terminates the tunnel on timeout and on exit.
* **F18** the downloaded cloudflared is validated (size + `MZ`/`ELF` header) before being executed.
* **F19** a busy port said `[Errno 10048]`; now it binds deliberately, moves up and says so
  (`build(..., sock=)`), or names pc-eye as the likely occupant.
* **F20** `move seconds=` was accepted and ignored (now real, capped); `drag` released ~1 px short of
  `x2,y2` (now lands exactly before the button-up).
* `--version` added; `VERSION` matches README/CHANGELOG (a test pins it); unused `base64` import removed.

### Tests
32 → **67**, still headless-safe. `Handler.input_space` / `Handler.display_rect` are now named seams, so
the Windows behaviour is exercised through the real routes instead of around them.

### Not changed, on purpose
the token-as-only-lock threat model (pairing/HMAC would break "paste two lines and work"); the proven
`SetCursorPos`/`mouse_event`/`keybd_event` primitives; and no Windows-only path was "fixed" blind — what
cannot run headless is pinned by a source-shape test or handed to the Windows CI leg.

## 1.0.2 — 2026-10-16

First live run on CarloPC. Found and fixed the DPI coordinate split (capture 1920x1080 vs input
1536x864, silently clamped) and the truncated `GlobalAlloc` HANDLE that crashed clipboard paste.
32 tests.

## Unreleased — 2026-10-27 (vesper): the suite was order-dependent (F43)

- **Fixed**: `WindowsBehaviour` restored `Handler.input_space` / `Handler.display_rect` by reading them off the
  class, which unwraps a `staticmethod` into a plain function — re-installing it as an *instance method* and
  making every later `self.input_space()` call raise `TypeError`. 8 tests were red in file order and green in CI,
  because `unittest discover` sorts classes alphabetically and pytest does not. New `save_seams` / `restore_seams`
  helpers preserve the descriptor; three patch sites now use them.
- **Fixed**: the fuzz harness left its 1920x1080 geometry stubs welded onto `Handler` forever, so anything after
  it in the same process tested against invented geometry and passed anyway.
- **Added** `tests/run_ordered.py`: runs the module in source order (classes read out of the AST — pytest is not
  a dependency here), and `tests/run_all.py` now runs it *and* `unittest discover`. A gate that samples one order
  of a suite with shared state is a coin flip. `ZSeamIntegrity` asserts the seam shape in either order; with the
  old bare restore put back, that class turns both runners red (verified: source order 1 failure + 9 errors,
  alphabetical 2 failures), so the guard has teeth and does not merely document.
- **Docs**: README's test count regenerated (79 → 82) by `tools/check_docs.py`, which is the check that flagged
  it; SHIP_GATE now says to run both orders.
- Not fixed, logged: `free_port()`'s bind-then-close TOCTOU (two classes can pick the same port).

## Unreleased — 2026-10-27 (vesper): a test that was green because of the leak (F44)

- `Protocol` asserted `/state`'s `screen` was 1920×1080 while only faking the *screen*, not the *geometry* the
  handler prefers. True headless, false on a real desktop; on `windows-latest` it had been passing solely
  because `FuzzFindings` leaked a `display_rect` stub that ran first in alphabetical order. Now the class pins
  `display_rect` (and restores it), so the assertion is platform-independent.
- The first attempt pinned only `display_rect`; the `windows-latest` legs then failed with `/act` 409
  (capture 1920x1080 vs input space 1024x768), so `input_space` is pinned as well — the class now asserts inside
  one coherent space instead of half-faking one axis of a comparison.
- `tests/run_ordered.py --isolated`: each class alone in its own process, added to `tests/run_all.py`. It fails a
  test that is green only in company, and it prints the import error instead of eight fake reds when the module
  cannot load (verified both ways, including a deliberately broken import).
- Gate: `python3 tests/run_all.py` is 4 steps — `unittest discover` (alphabetical, as CI), source order, each
  class alone, docs check.
