# pc-eye

**An eye and a hand on your PC, for an agent that lives in a chat.**

An agent in a chat can make HTTP requests but cannot hold a socket to your home machine. `pc-eye` is the
smallest honest thing that closes that gap: a local server on 127.0.0.1 that lets the agent *look* at your
screen and *act* on it, through a tunnel you start yourself. No API keys, nothing running in the background,
and the off switch is the console window you can close.

```
GET  /state      screen size, cursor, uptime, last action, coordinate-space check  (no pixel capture)
GET  /shot       the screen, as an image the agent looks at (+ X-Screen-Hash; ?changed=HASH -> 304)
POST /act        one action: click / move / type / key / drag / scroll / paste
GET  /selftest   what this machine can and cannot do, reported honestly
```

## Run it

Double-click **`start.bat`** (install Pillow once if asked). It prints two lines:

```
URL:   https://<something>.trycloudflare.com
TOKEN: 32 hex characters        <- 128 bits; the token is the ONLY lock on the door
```

The token is a bearer secret that rides in a query string (`?t=`), so it lands in browser history and in
whatever proxy is between you and the tunnel. **Paste it to the agent in the chat that owns the session,
never to a wire, a group channel, a screenshot or a commit.** `--token` can still set a shorter one;
`/selftest` will grade that red and the server will say so at startup.

Give those to the agent, and give it **[`PC-EYE.md`](PC-EYE.md)** — that file is the instruction set it
follows. Everything else is in there: the look–decide–act–look-again loop, the coordinate arithmetic between
the downscaled screenshot and the real screen, and what every refusal means.

Yours, meanwhile: `URL/shot?t=TOKEN` in a phone browser is the same screen the agent is looking at, and
`URL/?t=TOKEN` is the health page. The token never leaves your PC, your console, your `READY.txt` and your chat.

## What is borrowed, and from where

The parts that touch the machine are **Argus's**, taken from the qcaptcha Screen Solver
(`apps/qcaptcha/qcaptcha/`, superocr, branch `agent/argus/t-20261014-cc4a`) — code that had already run on
this machine:

* capture: `PIL.ImageGrab.grab(bbox=..., all_screens=True)`, with the old-Pillow fallback
* click: `SetCursorPos` + `mouse_event` (LEFTDOWN/LEFTUP)
* typing: `keybd_event(0, ord(ch), KEYEVENTF_UNICODE, 0)` + key-up
* paste: clipboard + Ctrl+V

What is new here is only the HTTP surface, the token, the refusals, the launcher and the instruction file.
qcaptcha's own overlay app is not touched, replaced or subsumed by this.

## Two bugs the first live run found (they are the reason to read this section)

Nothing here had ever executed on a Windows machine until it was driven end-to-end on 2026-10-16.

1. **The eyes and the hand were in different coordinate spaces.** `ImageGrab` returns physical pixels while
   `SetCursorPos` / `GetCursorPos`, in a DPI-unaware process, live in the scaled space — 1920x1080 against
   1536x864 at 125% scaling — and *silently clamp*. Asked for (1800,933), landed (1535,863), reported `ok`.
   An agent aiming from a screenshot would have clicked the wrong thing and been told it worked. Fixed: the
   process asks Windows for physical pixels before it captures or clicks, `/act` refuses a coordinate the
   input calls cannot reach instead of clamping it, and every pointer action reports `landed` / `off_by` /
   `exact`.
2. **Clipboard paste crashed the request handler.** `GlobalAlloc` returns a 64-bit HANDLE; ctypes defaults a
   foreign call's return to a 32-bit int, so the handle arrived truncated, `GlobalLock` read a bogus pointer
   and the write faulted at `0x...20`. Fixed by declaring every argument and return type, and freeing the
   block only when the clipboard did not take ownership.

Both failures were invisible from Linux and obvious within minutes on the real machine. **That asymmetry
is now a CI leg** (`.github/workflows/ci.yml` runs `windows-latest`, where `ctypes.windll`, `ImageGrab`
and the clipboard actually execute) — because "we found it by hand at 23:00 through a chat window" is not
a test strategy. See `AUDIT.md` F16.

## Speed, measured (1.0.3)

The loop is look → decide → act → look again, so the cost that matters is per-request, in milliseconds,
on the human's upload. `evidence/bench.py` + `evidence/CAPTURE_COUNT.txt` hold the raw numbers; the
before/after ran the **same harness on both trees**.

| | 1.0.2 | 1.0.3 |
|---|---|---|
| pixel captures per `/state` (geometry known) | 1 full-screen grab | **0** (asks the OS, cached 20 s) |
| captures per look+check cycle | 2 | **1** |
| JPEG encode, 1080p→1440 q72 | 42.62 ms / 481 KB | **31.81 ms** / 509 KB (`optimize` default off, `?opt=1` to restore) |
| aim mode `?max=0` | 8.83 ms | **5.76 ms** |
| watching a build (poll) | one frame per look | **304, no body** (`?changed=HASH`) |
| cost of the new safety checks | — | **~0.1 ms/request** |

Known trap, kept honest rather than hidden: `?fmt=png` at full resolution costs ~294 ms and ~6 MB on the
test box — a save-to-disk mode, not a browsing mode.

## Tests

```
python tests/run_all.py                       # the suite, and what CI runs
python -m unittest discover -s tests          # 82 tests, headless-safe
python evidence/bench.py --no-limit           # timings (needs a display for capture-side numbers)
python evidence/capture_count.py              # pixel work per request, old tree vs new
python evidence/negative_check.py             # proves the regression tests fail on 1.0.2
```

They cover the routes, the token, the action contract, the refusals, the log, the coordinate-space verdict
(the 1920x1080 / 1536x864 case is a regression test), the chord resolution and the dry-run behaviour. They
cannot prove that a click lands on the right pixel on *your* Windows box — `/selftest` and the `exact` field
on every action are what tell you that, on the machine itself.

## Limits a person should know before trusting an agent with it (1.0.3)

* **Every action is bounded**: 20,000 characters per `type`, 100 `times` per key, 1 MB per request,
  120 actions and 180 shots per minute. A runaway agent loop is stopped with a 429 that says what to do
  instead — and the limiter fails *open*, so it can never lock you out of your own machine.
* **`/act` refuses a browser that is not on this host** (403/415). A page on your own browser cannot drive
  the PC just because it saw the URL.
* **A split coordinate space is a refusal, not a warning.** If DPI awareness ever fails to take, `/act`
  answers 409 on anything that aims the pointer, instead of clicking confidently on the wrong thing.
* **Dry-run says so.** `--dry-run` replies carry `dry_run: true` and a `TEST-MODE` selftest line.

## Known limits, stated rather than hidden

* **Ctrl+Alt+Del cannot be pressed** by any user-mode program; Windows reserves it.
* **Elevated windows (UAC / admin apps) will not take the input**, and their pixels may come back black.
* One machine, one screen. Multi-monitor is captured as one wide surface.
* If the PC sleeps or you close the console window, requests fail. That is your switch, and it is deliberate.

## Editing this repo: three mechanical rules (AUDIT.md F39, F42)

Commit messages go through `git commit -F - <<'MSG'` (quoted delimiter). In a double-quoted `git commit -m`
the shell runs backticks first: mine deleted its own evidence, created a stray file, and aborted the
`git checkout -b` that preceded it — which is how a commit landed on `main`. And a `git merge` inside an `&&`
chain can print "MERGED" while main does not move, so merges are one call and the proof
(`rev-parse main`, `merge-base --is-ancestor`) is the next. Third: every `open`/`read_text`/`write_text`
names `encoding="utf-8"`, because the CI legs include Windows and its default codec cannot read this repo.
All three are checked by a machine (`tools/check_docs.py`, plus the pre-commit hook), not by memory.

## How the protocol was tested, and why there is a fuzzer in `evidence/`

`tests/test_pc_eye.py` (82 tests) is the contract. `evidence/fuzz_protocol.py` is the generator that found
six more bugs the contract did not describe — four of them in code written during the 1.0.3 audit itself,
including `/shot?fmt=png&quality=50` raising `UnboundLocalError` inside the handler. Two properties it
checks that a hand-written list never would: **the server always answers** (a bad argument is a 400 with a
sentence, never a closed socket — an agent cannot tell a dead PC from a typo), and **the server prints no
traceback** (a human's console full of fake bugs is how a real one gets ignored).

    python3 evidence/fuzz_protocol.py --requests 4000        # ~3s, no display, no mouse, --dry-run

Every log line is UTF-8-forced with `backslashreplace`, on purpose: a Windows console opened by
`start.bat` is cp1252/cp437, and one em-dash in a `print()` inside a handler is a dead thread, i.e. a dead
eye, for a formatting reason. Do not "simplify" that back to `print(..., flush=True)`.

`python3 tests/run_all.py --all` runs the suite, `tools/check_docs.py` (docs may not drift from the code),
the capture counter, and the negative control — which hosts these very tests against the pre-fix `server.py`
and reports how many go red **without** the fix (38 run, 35 red). A suite that only passes on the new code
proves nothing; that ratio is the proof. A green gate is still not proof that a click landed.
