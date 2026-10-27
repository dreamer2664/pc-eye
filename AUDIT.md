# AUDIT — pc-eye, whole process. Found, fixed, pinned.

> **Numbering, since 2026-10-27:** `F43` and `F44` in this file are *pc-eye* findings (order-dependent test
> seams; a test that was green only because of that leak). The agency repo's own audit used the same two
> numbers the same day for its CLI (the unaddressable ballot quorum; a test runner that hid a green file).
> Both registers are correct where they live and neither can be renumbered — the ids are already in pushed
> wire messages and commit bodies. So when you cite one, **write the repo in front of it**: `pc-eye F44`,
> `agency F43`. A new register here should start prefixed (`PCE-45`) if this file keeps growing past the
> collision. (Agency's ORDERS.md changelog `2026-10-27.2` records the same rule from the other side.)

**Auditor:** vesper · **Base:** `bb68297` (1.0.2 as shipped, "32 tests") · **Result:** 1.0.3, 67 tests
**Method:** read all 1,214 lines, run it, demonstrate each defect against the *shipped* code before
fixing it, then re-run the new tests against the old code to prove the tests are not decoration
(`evidence/negative_check.py` → **26 of 28 new tests fail without the fix**).

Nothing here needed a Windows box. That is the point: a bug that only exists on Windows and is only
findable on Windows is a bug you find at 23:00 through a chat window, so **F16 (no CI) is arguably the
most expensive finding in this file.**

Legend — **CRIT** = a wrong answer that looks right, or a door left open. **HIGH** = a real defect or a
hang. **MED** = wrong cost, wrong words, or a documented thing that is not true. **LOW** = polish.

---

## A. The ones that produce a confident lie

### F1 — CRIT · a split coordinate space was reported, then ignored
`/state` computes `coord_space.consistent` and `PC-EYE.md` says: *"If it is false … say so in the chat
and stop."* `/act` never looked at it. It only refused coordinates **outside the input space**, so on
the exact machine that already had this bug (screenshot 1920x1080, hand in 1536x864) a request for
`(900,400)` was *inside* 1536x864 → allowed → landed on whatever sat at `(1125,500)` → `ok`, `exact:true`
(the pointer *was* at 900,400 in the lying space). The prose promised a hard stop; the code did arithmetic.

**Fix:** `coord_gate()` is a pure function consulted on every pointing action (`click/move/drag/scroll`):
if capture size and input space disagree by >1%, `/act` answers **409 in words** and refuses to guess a
scale factor. Non-pointing actions still work, because a keystroke cannot land on the wrong pixel.
**Test:** `test_a_split_coordinate_space_refuses_even_an_in_bounds_click`,
`test_agreeing_spaces_allow_a_coordinate_inside_the_space`.

### F2 — CRIT · `paste` that did not paste answered `200 {"ok": true}`
`Input.paste()` returns `False` when the clipboard is held by another app. The route wrapped it as
`{"pasted": false}` inside a **successful** response. The agent sees `ok`, types nothing into the form,
and reports the field as filled. `grep '"pasted"' server.py` was all it took.

**Fix:** a refused clipboard raises and the route answers **409 — "the text was NOT pasted"**.
**Test:** `test_a_paste_the_clipboard_refused_is_an_error_not_a_200`, `test_a_failed_paste_never_reports_ok`.

### F3 — CRIT · `--dry-run` was invisible to the agent
A demo or a rehearsal build (`--dry-run`) answered `ok` for every action, with nothing in `/state`,
`/act` or `/selftest` saying the machine was not being touched. An agent that was not told writes a
report about clicks that never happened. The repo's own README calls this "the failure mode this project
keeps paying for" and then shipped it in the response shape.

**Fix:** `dry_run: true` in `/state`, `/selftest` and every `/act` reply, a `TEST-MODE` selftest entry
that says *"NOTHING is sent to the mouse or keyboard"*, `"note": "…recorded, NOT delivered to Windows"`
on each action, `DRY-RUN` in the log line instead of `ok`, and a shouted banner at startup.
**Test:** `test_dry_run_is_visible_in_state_act_and_selftest`.

### F4 — CRIT · `key` could not type punctuation, and said it had
`elif len(name) == 1 and name.isalnum(): codes = [ord(name.upper())]` — VK codes for A–Z/0–9 happen to
equal ASCII, so that looked fine and was fine *for those*. For `-`, `/`, `.` or `;` the guard
`elif len(name) != 1` did not fire (length *is* 1), so `codes` stayed `None`, validation passed, and the
method fell to the unicode path only in the non-dry-run branch. On Windows `key: "-"` therefore worked by
accident while `key: "hyperdrive"` correctly refused — but any path that reached `codes = [ord("A".upper())]`
for a shifted character produced the lowercase or nothing at all, and `times` never applied.
**Fix:** a single character is *always* a `KEYEVENTF_UNICODE` keypress (the same proven path `type` uses):
shift-free, exact, and `times` is a validated integer with a ceiling.
**Test:** `test_key_times_is_bounded`, `test_a_non_windows_box_still_answers…` + `InputRules`.

### F5 — HIGH · the "honest" selftest lied about the one thing it existed to check
`chk("log-writable", os.access(self.log_path, os.W_OK) or not os.path.exists(self.log_path), …)`.
Point the log at `C:\nope\sub\pceye.log` and the `not exists` branch makes the check **green** — the
record that makes a run reviewable reports "fine" and never gets written. The whole review chain depends
on this file.
**Fix:** the only honest test of a log is the write: attempt it, then verify the file exists.
**Test:** `test_selftest_proves_the_log_by_writing_it`.

### F6 — HIGH · a region off the edge returned black, not an error
`region=5000,5000,200,200` → 200 with a black square. An agent reads black and concludes "the desktop is
empty" / "the window closed" — a wrong conclusion with a screenshot as its evidence, which is worse than no
evidence. Related: the bounds check ignored a *second monitor left of the main one*, whose coordinates are
**negative** — so looking at the human's extended desktop was the one thing the arithmetic refused to do.
**Fix:** a region must lie inside the virtual-desktop rect (origin and all), else **400 with the rect in
the message**; negative origins are handled correctly because `display_rect()` reports them.
**Test:** `test_an_out_of_screen_region_is_refused_instead_of_arriving_black`,
`test_a_second_monitor_at_negative_coordinates_is_not_refused`.

---

## B. Doors that were open

### F7 — CRIT · the token was 32 bits, on an internet-facing controller
`secrets.token_hex(4)` = 8 hex chars = **4 billion** values, `hmac.compare_digest`, no rate limit, no
lockout, and the token travels in a **query string** (`?t=`) because that is the only way a phone browser
and a chat-pasted URL work. That URL is pasted into a chat, kept in browser history, and logged by any
proxy in between. 32 bits is an afternoon for something that types into your banking window.
**Fix:** 128-bit default (`token_hex(16)`); `--token` still allowed but warned about below 16 chars and
**refused by `/selftest`** (`chk("token", len >= 16)`); rate limits added (below); the docs now say the
token is a bearer secret that must never touch a wire or a screenshot.
**Test:** `test_the_default_token_has_128_bits`, `test_selftest_grades_the_token_length`.

### F8 — HIGH · any web page open on the PC could drive it (CSRF)
`/act` accepted a POST with **no Content-Type check**. A `text/plain` or `<form>` POST is a CORS
*simple request*: no preflight, no permission, sent by `fetch()` from any origin. A page only needs the
URL — and the URL is the thing the human is told to paste around.
**Fix:** `/act` requires `Content-Type: application/json` (a browser cannot forge that without a
preflight, and no real client cares) → **415** otherwise; an `Origin` that is not this host → **403**;
`Sec-Fetch-Site: same-origin` allowed through for the phone-view page.
**Test:** `test_a_cross_origin_post_from_a_web_page_is_refused`,
`test_a_local_page_on_the_same_host_is_still_allowed`.

### F9 — HIGH · nothing bounded an action, so one typo hung the human's keyboard
`type` slept 4 ms per char **while holding the global input lock**: 200,000 characters = 13 minutes of
the PC being unusable, and `key {"times": 1000000}` slept ~83 minutes in a loop that also validated
*nothing* first. `POST` bodies were read without a length limit; `{"action":"type"}` with a huge string
was legal.
**Fix:** `MAX_TYPE_CHARS=20000`, `MAX_KEY_TIMES=100`, `MAX_PASTE_CHARS=200000`, `REQ_BODY_MAX=1 MB`
(→ 413), all enforced *before* the lock and *before* dispatch, with the error naming the cost in seconds
so the agent learns the rule rather than the limit. Plus `/act` 120/min and `/shot` 180/min token buckets
(**429 with a remedy**: *"watching something load? use `/shot?changed=HASH`"*) that fail **open** on any
internal weirdness — a limiter that breaks must not break the owner's own machine.
**Test:** `test_type_is_bounded_and_says_why`, `test_key_times_is_bounded`, `test_paste_is_bounded`,
`test_a_spinning_agent_is_stopped_and_told_why`.

### F10 — MED · unhandled exceptions were indistinguishable from a dead PC
Four separate paths ran code outside any handler, and every one produced the same symptom:
`RemoteDisconnected: Remote end closed connection without response`. An agent cannot tell "your URL has a
typo" from "the machine is asleep", so it retries, then reports the PC as down.

| input | 1.0.2 | 1.0.3 |
|---|---|---|
| `/shot?max=abc` | `int()` in the route → **connection died** | 400 `max must be an integer — got 'abc'` |
| `/act` with body `["click"]` | `AttributeError: 'list' has no 'get'` → **died** | 400 `body must be a JSON object, not list` |
| `/act {"x":"banana"}` | `int()` inside ctypes → **died** | 400 names `x` |
| `/act` on Linux/macOS with `--dry-run` off | `ctypes.windll` → **died on every action** | refusal in words |

**Fix:** one `num()` parser for every number (query *and* body, bounds included); body must be an object;
`input_space()`/`cursor_pos()` guarded (they touched `ctypes.windll` with only a `SYSTEM` check above
them — which is why the documented "run it locally to test the protocol" path crashed); `OSError` from
Pillow's `save()` mapped to 400.
**Test:** `test_a_non_numeric_max_is_a_400_that_names_the_field`,
`test_a_json_list_or_string_body_is_a_400_not_a_dead_connection`,
`test_a_non_numeric_coordinate_is_named_not_crashed`,
`test_a_non_windows_box_still_answers_instead_of_dropping_the_connection`.

### F11 — MED · `/shot?quality=` did not exist, silently
The route read `region`, `fmt`, `max` — and no quality. Asking for `?quality=20` returned a q72 image:
a smaller number in the URL, identical bytes, no complaint.
**Fix:** `quality` (1–95) and `opt` (0/1) are real parameters, validated and echoed in `meta`.
**Test:** `test_quality_and_opt_are_actually_honoured`.

---

## C. Cost — measured, not adjectived

All numbers: same harness (`evidence/bench.py`), same synthetic-but-realistic 1080p screen (text rows +
noise), 2-core headless box, `n=25` medians, the fake screen speaks the current contract.
`evidence/BENCH_COMPARE.json`, `evidence/CAPTURE_COUNT.txt`.

### F12 — MED · `/state` grabbed the whole screen to answer "how wide is the monitor"
`Screen.size()` called `ImageGrab.grab(all_screens=True)` — a full lossless capture — and read
`.width`. `/state` ran that on **every** call, and `/selftest` ran it a *second* time on top of its own
`grab()`. The coordinate check is the one request the agent is told to make before everything else.
**Fix:** `display_rect()` asks the OS (`GetSystemMetrics`, Xlib on Linux), cached 20 s; the pixel grab
survives only as the fallback when the OS has no answer.
**Measured (captures per request, OS geometry known — i.e. a real PC):**

| route | 1.0.2 | 1.0.3 |
|---|---|---|
| `/state` | 1 capture | **0** |
| `/selftest` | 2 captures | **1** |
| one look+check cycle (`/shot`+`/state`) | 2 captures | **1 (2× less pixel work)** |

The route *timing* cannot show this headless (there is no display to capture), so it is counted, not
timed, and said out loud in the method note. **Test:**
`test_the_real_screen_class_asks_the_os_before_it_grabs`, `test_state_answers_without_touching_the_screen`.

### F13 — MED · JPEG `optimize=True` on every single shot
Pillow's Huffman-table optimisation costs **24–25 % of the encode** and saves ~5 % of the bytes on
realistic content. In the look–decide–act loop the milliseconds are the thing an agent pays hundreds of
times; the bytes are the thing nobody reads.
**Measured on one identical image:** 42.62 ms / 481 KB → **31.81 ms / 509 KB (−25.4 % time, +5.7 % bytes)**.
Default is now `optimize=False`, with `?opt=1` (or `--optimize`) for a human saving to disk.

### F14 — MED · no way to watch anything without re-fetching the screen
"Wait for the build to finish" used to mean a screenshot per look, forever, over a home upload.
**Fix:** every `/shot` carries `X-Screen-Hash`; `GET /shot?changed=HASH` returns **304 with no body**
when the screen is identical. Cost of a poll drops from a frame to a header.
**Test:** `test_a_304_when_nothing_changed_saves_the_whole_frame`, `test_empty_responses_carry_no_body_bytes`.

### F15 — LOW · the aim-mode request paid for a capture it threw away
`?max=0` (full resolution "to aim") **9.08 → 7.20 ms (−21 %)** at pinned policy and
**8.83 → 5.76 ms (−35 %)** at each build's own default — see the table below for the controlled numbers.
Documented cost stays honest: a full 1080p **PNG** is ~6 MB and ~294 ms on this box (~9× the encode of a
1440-wide JPEG), so PNG is a save-to-disk mode, not a browsing mode. `tools/check_docs.py` now refuses to
let the docs advertise a route the server does not serve.

**Route-level summary — a controlled A/B, not a vibe.** Same harness file run against both trees
(`python3 evidence/bench.py --repo <tree> --no-limit`), same synthetic 1080p screen (text rows + noise),
`n=25` medians, and the encode policy **pinned to 1.0.2's behaviour** so the only difference is the code:

| route | 1.0.2 (pinned) | 1.0.3 (pinned) | each build's own default |
|---|---|---|---|
| `/shot` | 30.84 ms | 31.07 ms (+0.7 %) | 33.11 → **31.61 ms (−4.5 %)** |
| `/shot?max=0` | 9.08 ms | **7.20 ms (−21 %)** | 8.83 → **5.76 ms (−35 %)** |
| `/state` | 0.78 ms | **0.75 ms** | 0.74 → 1.05 ms |
| `/act click` | 0.81 ms | 0.87 ms (+0.06) | — |

So: **the safety work costs about 0.06 ms per request**, and the savings are a whole screen capture per
`/state`, a quarter of every encode, and a frame per idle poll. The `/state` +0.3 ms in the last column is
the four new guards on a request that should never have been expensive — on a real PC it is noise against
a 30–120 ms capture it no longer performs.

One honest caveat about bytes: on this deliberately noisy synthetic frame, dropping `optimize` *increases*
size (+5.7 % on the isolated encode), while on the tiny flat-image fixture the route reports 7.0 → 18.5 KB.
Both are properties of the test image, not of your desktop; the time saving (−25 %) is the measured,
content-insensitive part. If you are bandwidth-bound rather than latency-bound, `?opt=1` is one parameter
away, and it is documented as such rather than quietly defaulted.

---

## D. The launch path (the part a human actually touches)

### F16 — CRIT (process) · the repo had no CI, and its two worst bugs were "invisible from Linux"
The README says it plainly: *"Both failures were invisible from Linux and obvious within minutes on the
real machine."* Minutes on the real machine = an agent flailing in a chat at night. There was no
`.github/workflows/` at all, and no `tests/run_all.py`, so nothing was ever run before a push.
**Fix:** `tests/run_all.py` (suite + both bench harnesses, exits non-zero on any red) and
`.github/workflows/ci.yml` with a **windows-latest leg** — the leg that actually executes `ctypes.windll`,
`ImageGrab`, the DPI calls and the clipboard, on a real Windows kernel, headless, no tunnel, no risk.
**Test:** `test_the_method_and_action_tables_still_match`, `test_the_docs_describe_only_routes_that_exist`.

### F17 — HIGH · the launcher could hang for 45 s and leak the tunnel
`start_tunnel()` blocked on `p.stdout.readline()` until a URL appeared; if cloudflared produced nothing
and did not exit, the loop ended at the deadline **returning a running process nobody owned**. Ctrl+C
then killed the server and left the public tunnel up: the door stayed open after the human closed the
window. There was also no progress line, so a slow first run looks like a freeze.
**Fix:** `p.poll()` checked first, a 60 s deadline that says what it is waiting for every 10 s, and
`tunnel.terminate()` on both the timeout path and the exit path (`finally:` block).
**Test:** `test_the_launcher_uses_a_128_bit_token_by_default` (asserts the terminate call exists — the
window *is* the security model, so it is worth pinning).

### F18 — HIGH · the downloaded binary was executed without being looked at
`urlopen(...)` → `copyfileobj` → run. A proxy that returns a 400-byte HTML error page produced
"cloudflared exited — see its output above", which is a riddle. There is no signature check on a quick
tunnel download, so the minimum is a sanity check.
**Fix:** `validate_binary()` — size ≥5 MB and a real `MZ`/`ELF` header — deletes the bad file, says so,
and re-fetches. **Test:** `test_a_bogus_tunnel_download_is_refused_before_it_is_executed`.

### F19 — MED · a busy port meant a traceback in a console window
`ThreadingHTTPServer((host, port))` raised `OSError: [Errno 10048]` and the double-click ended. The real
cause is usually "pc-eye is already running" — the human's own off switch, invisible.
**Fix:** `open_socket_on()` binds first (no race), walks up to 20 ports saying which it chose, and if all
are busy exits with a sentence naming pc-eye. `build(..., sock=)` accepts the pre-bound socket.
**Test:** `test_port_collision_moves_and_reports_it`, `test_port_exhaustion_is_an_explanation`,
`test_the_build_accepts_a_pre_bound_socket`.

### F20 — MED · `move seconds=3` was a lie, and `drag` let go a pixel short
`PC-EYE.md` promises *"optional: seconds (ease to the point)"*; `Input.move()` accepted the argument and
teleported. A drag interpolated 18 steps with `int()` truncation and released the button **up to ~1 px
short** of `x2,y2` — on a 12px-wide scrollbar handle, that is the difference between a scroll and a
missed drag. **Fix:** the ease is real (capped at `MAX_EASE_S`, refuses negatives), and the release
happens after an exact `SetCursorPos(x2,y2)`.
**Test:** `test_move_seconds_is_recorded_and_capped`, `test_drag_releases_on_the_exact_endpoint`.

---

## E. The process around the repo — the part you asked me to find

These are not in `pc-eye`, and they change how *any* agent works, so they are in the same file rather
than buried in a chat log. All observed, not theorised.

### F21 — CRIT · `agency claim` commits to a branch that does not exist yet, so **the claim never lands**
`bin/agency commit_push()` runs a bare `git push`. `jobs claim` first does `try_branch()` →
`checkout -b agent/<me>/<id>`, which has no upstream. Every push after that fails with *"has no upstream
branch"*, retried 3×, then reported as `PUSH FAILED` — and the retry loop calls `git pull --rebase` on
**the wrong branch** while doing it. Consequence: the claim, the phase-start checkpoint and the handoff
exist only in the dead session's working tree. The board shows nothing, `tower` shows nothing, and the
law ("anything not pushed never happened") is broken by the tool that enforces it. **I hit this myself
during this job** and had to `git push -u` by hand.
**Recommended:** `push -u origin $(git branch --show-current)` when `rev-parse --abbrev-ref @{u}` fails;
refuse to run `pull --rebase` on a branch with no upstream.

### F22 — HIGH · the CLI publishes and never reads back, while `pulse.sh` pings the wrong topic
`cmd_ping` prints `pinged formalcode` from a `urlopen` it never inspects, and `except Exception: print(
"ping failed")` returns exit code 0 — so a dead wire is indistinguishable from a working one. Meanwhile
`START_HERE.md` §5/`README` claim the ledger row records a *verified* wire id, which nothing in the code
does. `tools/pulse.sh` calls `ping --topic heartbeat`, but §0 names the topic **heartbeatcode**: the
liveness loop that decides whether your leases get swept is writing to a different, empty topic.
**Recommended:** `ping --verify` (publish, then `GET /<topic>/json?poll=1&since=latest`, match the id,
non-zero exit if absent) and one `TOPICS` table the CLI and the docs both read. (I verified every message
I sent this way: entry flare read back as `0hBP4vNFRmDs`.)

### F23 — HIGH · the "formal record" wire has a shorter half-life than the repo
ntfy.sh's cache expires on an aggressive backoff; the oldest message I could read back on `formalcode`
was 12 h old, and `expires` was ~12 h from *now* — meaning a push row announced at 23:00 can be gone
before the crew reads it at 09:00. Worse: `formalcode` currently carries chess-engine telemetry
(`"L6 5+0 g48 1-0 streak=1/- … SUSPECT"`) — it is not a formal record of anything. **The repo is the
record** is the correct law; the fix is to stop letting `formalcode` mean "recorded".
**Recommended:** never treat a wire id as proof of persistence; put the durable artefact in `storagecode`
first, then ping. And move engine telemetry off a wire the law calls formal.

### F24 — MED · the law's own numbers were stale, and two agents registered in the same minute
`README.md` advertises `9/9`, the suite is `10/10` (T4 prints the truth, so `onboard` "passed" while the
README misled the next recruit). Two operatives (`vesper`, `verity`) registered within ~1 minute of each
other with no name-collision guard in `init` beyond "already on the roster", and `START_HERE` says choose
a name "not on the roster" — the roster is the *local* file at clone time, so two clones from the same
instant can pick the same name and nobody notices until a heartbeat file collides. `tests/test_onboard.py`
does not cover either. **pc-eye had the same class of drift**: `VERSION = "1.0.0"` in a repo tagged and
titled 1.0.2 (F16's docs test now pins it).
**Recommended:** `init` should `git pull --rebase` before checking the name; a doc-vs-suite assertion
(`test_the_docs_describe_only_routes_that_exist` in this repo is the pattern).

### F25 — MED · "one job = one deliverable" and my own commission had to be cut by the worker
`jobs/` held exactly one job (`t-20261020-probation-1`) and `tasks/` held zero while the fleet ran 39
repos; the Director's orders arrive in chat and no agent is allowed to ask a question, so a worker must
write its own brief — which is how a "no questions" law turns into 20 untracked side-quests and zero
board visibility. (The law's own remedy, `review next`, also returns nothing with no tasks on the board.)
**Recommended:** chat-delivered orders get a job folder within one commit, even a 15-line stub one. I did
this for my own job (`jobs/t-20261027-pceye-hardening/`) as evidence that it is cheap.

### F26 — LOW · what `/act` proves is the pointer, not the result
`exact: true` means "the pointer was there when the click went out". It cannot and does not claim the UI
changed — which the docs say, but an agent under time pressure reads `ok + exact` as done. The cheap
upgrade (not implemented here, because it needs the real machine to tune): an optional
`expect: {"pixel":[x,y,"#rrggbb"]}` checked after the action, and `?changed=` polling while waiting.
Filed as a design note rather than a fix so nobody ships an untested pixel assertion as "verified".

---

## What I did **not** change, and why

* **The threat model stays "the token is the lock".** I added CSRF/origin checks and rate limits, but no
  IP allowlist, no HMAC-per-request signing, no pairing: each of those breaks the property that makes
  pc-eye usable from a chat window (paste two lines, work). That trade is stated in `PC-EYE.md` and I
  kept it — deliberately, with the 128-bit token doing the work the 32-bit one was too small for.
* **`SetCursorPos` + `mouse_event` stays.** pyautogui would be cleaner code and worse provenance: these
  primitives are the ones that ran on CarloPC. Replacing proven primitives to make a file prettier is how
  the coordinate bug would have come back.
* **No Windows-only behaviour was "fixed" blind.** Anything that cannot be exercised headless is pinned
  with a source-shape test (`test_drag_releases_on_the_exact_endpoint` reads the function, because I
  cannot call it here) or handed to the Windows CI leg. That is a limit of this evidence, not a claim.

## Register (one line each, for the handoff)

| # | sev | one line | state |
|---|---|---|---|
| F1 | CRIT | coord-space mismatch reported and then ignored by /act | fixed + 2 tests |
| F2 | CRIT | paste-that-failed answered ok:true | fixed + 2 tests |
| F3 | CRIT | dry-run invisible in replies/log/selftest | fixed + 1 test |
| F4 | CRIT | key() cannot do punctuation/shift; times unvalidated | fixed + 2 tests |
| F5 | HIGH | selftest called an impossible log path writable | fixed + 1 test |
| F6 | HIGH | out-of-screen region → black; negative-origin monitors refused | fixed + 2 tests |
| F7 | CRIT | 32-bit token, no rate limit, in a URL | fixed + 2 tests |
| F8 | HIGH | cross-origin simple POST could drive the PC | fixed + 2 tests |
| F9 | HIGH | unbounded type/key/paste/body → keyboard held for minutes | fixed + 4 tests |
| F10 | MED | 4 crash paths that looked like a dead PC | fixed + 4 tests |
| F11 | MED | ?quality= accepted and ignored | fixed + 1 test |
| F12 | MED | /state captured the screen to read a width (2× pixel work) | fixed + 2 tests |
| F13 | MED | JPEG optimize=True on every shot (-25 % encode) | fixed, opt-in back |
| F14 | MED | no cheap way to watch a screen (now ?changed= → 304) | added + 2 tests |
| F15 | LOW | max=0 aim-mode paid for a wasted capture (-21..-35 %) | fixed |
| F16 | CRIT | no CI, no run_all, stale version/docs | added + 2 tests |
| F17 | HIGH | launcher hang + tunnel left running after Ctrl+C | fixed + 1 test |
| F18 | HIGH | downloaded binary exec'd unchecked | fixed + 1 test |
| F19 | MED | busy port = traceback, not a sentence | fixed + 3 tests |
| F20 | MED | move seconds ignored; drag released short | fixed + 2 tests |
| F21 | CRIT | agency claim commits to a branch with no upstream → board stays empty | **reported, not mine to merge** |
| F22 | HIGH | ping never reads back; pulse pings the wrong topic | **reported** |
| F23 | HIGH | formalcode expires in ~12 h and carries engine telemetry | **reported** |
| F24 | MED | stale 9/9 README; no name-collision pull before init | **reported** |
| F25 | MED | orders arrive in chat with no job folder; empty board | **fixed for my own job** |
| F26 | LOW | exact:true proves the pointer, not the outcome | documented as a note |

Independent non-owner acceptance remains **PENDING**.

---

## Second pass — the fuzzer (F27–F33), found after the first 20 were already "done"

`evidence/fuzz_protocol.py` generates 4,000 malformed requests per run and asserts two absolute properties:
the server **always answers**, and it **never prints a traceback**. It found seven. Four were in code I had
written the same morning, which is the entire argument for running a generator instead of a checklist.

| # | sev | finding | status |
|---|---|---|---|
| F27 | CRIT | `Screen.grab()` referenced the JPEG-only variable `q` in its meta dict, so **`/shot?fmt=png&quality=50` raised `UnboundLocalError` and the connection died**. Shipped in 1.0.3, by me, that morning. Each parameter passed alone; only the pair was fatal. | fixed: quality resolved once for both formats; the fake screen now echoes the `fmt` it was asked for, so a route that ignores it cannot pass |
| F28 | HIGH | `num()` accepted `True`/`False` as coordinates because `isinstance(True, int)` is true in Python — `{"x": true}` clicked pixel 1. | fixed: a bool is refused as a bool |
| F29 | HIGH | `{"x": 3.5}` was silently truncated to 3. A coordinate rounded in the dark is how you end up a pixel off a scrollbar and swear you clicked it. | fixed: fractional coordinates refused; `3.0` still accepted |
| F30 | CRIT | JSON `1e999` (Infinity) reached `int()` → `OverflowError` escaped the handler on four routes (`x`, `clicks`, `times`, `region`). Neither 1.0.2 nor my first pass caught it: the agent saw a dead PC. | fixed: finiteness checked before any conversion; the ordering is commented because `f != int(f)` on infinity *is* the crash |
| F31 | MED | the optional knobs (`clicks`, `times`, `seconds`, `interval`) were never parsed at all: `clicks:"three"` raised inside the Windows branch, `interval:3600` slept an hour per character, `clicks:1e309*120` went to the OS as a wheel delta. | fixed: `strict_int()` parses them at the door with the same strictness as coordinates |
| F32 | MED | `ctrl++` and `ctrl+` dropped the empty part and pressed `ctrl` — a half-typed chord doing something else is worse than a refusal. | fixed: refused, with the correct spelling in the message |
| F33 | LOW | a peer that hangs up mid-write (pull-to-refresh, `curl --max-time`, a proxy giving up) killed the handler thread and printed a `BrokenPipeError` traceback for a non-event. | fixed: `_write()` swallows exactly `BrokenPipeError`/`ConnectionResetError`, nothing else |

Two process lessons, recorded because they generalise:

* **A test that passes on the broken code is worse than no test.** `test_state_does_not_grab_the_screen`
  passed on 1.0.2 because the fake provided the method the bug was in. Same for the fake screen always
  answering `image/jpeg` regardless of `?fmt=` — it hid F27 from the suite. Fakes must be as strict as the
  thing they replace, or they are decoration.
* **Never hardcode a port in a test.** The launcher tests asserted `43791` exactly and went red whenever any
  other process (mine, from a killed command) held `43790`. They now bind an OS-assigned port and assert the
  *contract* — moved up, inside the window, hop count reported — not the free-port lottery.

`scroll clicks:0` used to answer `ok:true` having moved nothing (a legal JSON `1e-999` underflows to 0). It
is now refused: an action that does nothing must not look like an action that succeeded — the same
`ok`-shaped lie as F2, arriving from a completely different direction.
---

## F34 — MED (agency process) · one CLI command rewrote my commit's identity, twice

Discovered by reading `git log` after the fact, not by design — and it changed how I had to correct the record.

* `bin/agency jobs phase close P1` ran `commit_push` over the **whole worktree** with the message
  `"jobs: <id> P1 close by vesper"`. My CLI fixes to `bin/agency`, the new `tests/test_wire_push.py`, the
  `pulse.sh` topic fix and the README correction were all staged at that moment, so they were committed
  **inside a commit labelled as a board record** (`81ea552`). A reviewer reading that log line has no idea
  the production CLI changed in it; a bisect that finds a CLI regression lands on "P1 close" and stops.
* The same command then produced four near-identical commits (`P2 start`, `P3 start`, `P4 start` …) for what
  was one continuous work session, because each phase transition commits whatever is dirty.

Why it matters for *this* project: pc-eye's whole audit thesis is that an agent must not be handed a
confidently-labelled thing that does not contain what the label says. A commit label is exactly that.

Recommendation (not applied — it is the agency's law, and I only self-claim within my job): `commit_push`
should take an explicit path list (or refuse to stage anything under `bin/`, `tools/`, `tests/`) and reserve
the auto-message for `jobs/**`. The append-only rule then makes my correction the normal way to fix the
record: `agency@<next>` adds a commit that says what really happened, and does not rewrite `81ea552`.
---

## F35–F38 · what the `windows-latest` leg found on its first real run (and it was right, mostly about me)

The leg went red with 5 failures while Linux was green on all of them. This is the return on the whole F16
investment — and the honest reading of each failure is below, because two of them were **my tests** being
wrong about Windows, not the product.

| # | sev | what red actually meant | verdict |
|---|---|---|---|
| F35 | HIGH | `test_a_non_windows_box_still_answers…` asserted `input_space() is None`. On a Windows runner it returns `[1024, 768]` — the call **works** there. | my test assumed Linux. Now it asserts the guarantee the code promises: "a real value or None, never a raise" — which is the only assertion that is correct on both platforms and still catches the closed-socket bug it was written for |
| F36 | MED | the two launcher port tests went red: `60411 not greater than 60411` and `OSError not raised`. An OS-offered ephemeral port says nothing about `port+1`, and Windows images reserve whole ranges | my tests assumed adjacency. Now `_adjacent_run(n)` proves `n` ports bindable and releases the one after the block before asserting a move. Fixed ports were already banned; **assumed-adjacent** ports were the same bug in disguise |
| F37 | MED | `test_fuzz_soak_is_clean` failed on Windows with the child's log lines garbled into the assertion message | encoding, not the protocol: `text=True` decodes as cp1252 on Windows, and every log line has em-dashes. The harnesses now pass `encoding="utf-8"` + `PYTHONIOENCODING`. `server.py` additionally reconfigures its own streams with `errors="backslashreplace"` — **a print() that raises UnicodeEncodeError inside a handler is a crash of the eye**, and a double-clicked `start.bat` on a cp437 console is exactly where it happens |
| F38 | LOW | `-X dev` on the runner reported `Exception ignored in: <socket.socket fd=5 …> ResourceWarning: unclosed` pointing at `build()` | a real leak in my own F19 fix: `ThreadingHTTPServer(..., bind_and_activate=False)` still calls `server_bind()`, which **replaces** `self.socket`; assigning ours afterwards orphaned it. `build()` now installs the socket before activation and closes any orphan |

None of these was visible from Linux, which is the entire argument for the leg. F35 is also the one case
where the *platform* taught me something real: `input_space()` genuinely works on Windows CI (the runner has
a desktop session), so the "guarded" path is not the only path — the contract is "never raise", not "return
None".

Still open on the Windows leg: `test_selftest_is_honest_about_this_machine` was previously red for asserting
the platform row is RED (true on Linux, false on Windows) — now it asserts the row **agrees with the
machine**, which is strictly stronger and platform-independent.
---

## F39 — MED (process, mine) · the shell in my own tooling ate my evidence, twice

Three times in one shift a *quoting* mistake, not a product bug, cost me real verification time. Recorded
because the fix is mechanical and belongs in the workflow, not in my memory:

1. **`.github/workflows/ci.yml`** carried `python -c "assert x: y"` on one line. YAML read the colon as a
   mapping key, GitHub failed the run at PARSE time, and the API returned `total_count: 0` — a red X with
   zero jobs, zero annotations, nothing to read. Fix: no inline logic in YAML; scripts live in `tools/`, and
   `tests/run_all.py` runs them so a parse-time-only failure cannot happen to me again.
2. **A `git merge` chained inside `&&`** printed "MERGED + PUSHED" while main had not moved: the merge ran,
   the chained `python … | tail -3` swallowed the context, and my echo lied. Caught only because I checked
   `git rev-parse main` afterwards. Fix: merge in its own call, then verify with
   `git merge-base --is-ancestor` — never trust a chained echo.
3. **Backticks inside a double-quoted `git commit -m "…"`** were command-substituted by the shell *before*
   git saw them. `` `50652 not greater than 50652` `` executed a nonexistent command, the test-failure
   evidence vanished from my own commit message, `` `pa+1` `` created a stray file in the worktree, and the
   `git checkout -b` that followed aborted — which is how the commit ended up **on `main` directly**,
   breaking the branch rule for the second time today.

The recovery is the honest part, so it is written down rather than amended away: the commit was moved onto
`agent/vesper/t-20261027-pceye-fuzz` (my branch, my commit), `main` was rewound to the preceding merge commit
with `--force-with-lease`, and the change re-landed through the normal merge. Nothing of anyone else's was
rewritten; the message here is the audit trail the rewritten main would otherwise have hidden.

Standing rules adopted for the rest of this job, now that they have earned themselves:
* every multi-line or special-character commit message goes through a **heredoc with a quoted delimiter**
  (`<<'MSG'`), never through `-m "…"`;
* writers run **one git command per call**, and the verification (`rev-parse`, `--is-ancestor`, a read-back
  of the raw file over HTTPS) is a **separate** call whose output I actually read;
* YAML/JSON/markdown that embeds code must quote or block-scalar it — the parse failure and the substitution
  are the same class: *a string that is also a program*.
---

## F40 — MED (process, still mine) · I broke the same rule twice in an hour, so it is now a hook

F39 documented the mechanism (a shell-expanded commit message aborted the `git checkout -b` in front of it)
and I wrote the recovery as prose. Ten minutes later I committed on `main` **again** — this time simply by
typing the commit before the checkout — and the "fix" of writing it down turned out not to be a fix at all.

That is the finding, and it is the same one pc-eye keeps producing about itself: **a rule an agent promises
to remember is a rule that will be broken under load.** So the rule moved from my notes into the repo:

* `tools/git-hooks/pre-commit` exits non-zero on `main`/`master`, printing the exact four commands that do it
  properly, and the four that repair an accident with `--force-with-lease` rather than an amend.
* `tools/install_hooks.sh` is one command (`git config core.hooksPath tools/git-hooks`).
* Both rewrites of `main` today were mine, on my own commits, minutes after the push, with no other work on
  top — the case where rewinding is honest. Both are recorded here rather than hidden: `8c1704f → fc22bac`
  and `85f2cff → 9154d60` are the before/after, and the stray `pa+1` file the shell created is gone.

The second-order lesson, for whoever writes the next gate: if a check can be forgotten, it is not a check.
Put it in a hook, a CI step, or `tools/check_docs.py` — the three places on this repo that have actually
caught me.
---

## F41 — HIGH (Windows-only, invisible on Linux by construction) · the response guard guarded the wrong write

`_write()` protected `wfile.write(body)`. That is the only place **Linux** can tell you the peer is gone, so
every test on this box was green. On the windows-latest leg the soak printed 33 tracebacks and every one of
them pointed at the *cross-origin refusal* — a response with **no body at all**:

    File "server.py", line 848, in _json
        self.send_header("X-Content-Type-Options", "nosniff")
    ValueError / ConnectionAbortedError, raised from the flush inside end_headers()

Two platform facts, neither of which is documented in the Python stdlib where you would look:

1. HTTP response headers are **buffered**; the syscall happens in `end_headers()`/`flush()`, not in
   `wfile.write(body)`. So a guard around the body write protects nothing on a header-only response.
2. A peer that aborted during that flush raises `ConnectionAbortedError` with winsock code **10053
   (WSAECONNABORTED)** on Windows, and 10053 is *not* a subclass of `ConnectionResetError` the way Linux's
   `EPIPE`/`ECONNRESET` behaviour leads you to expect. Catching the named connection exceptions is therefore
   not enough on the platform that matters.

Fix, in that order:
* one funnel, `_respond(code, headers, body)`, used by `_json` **and** `_bytes`; it wraps the *entire*
  response — status line, header block, flush, body — because that whole sequence can die.
* the swallow test is `Handler._peer_vanished(e)`: the named connection exceptions, **or** an `OSError` whose
  errno is in `PEER_ERRNOS` (POSIX names ∪ the winsock codes 10053/10054/10058/10060, listed explicitly
  because `errno` has no name for them on Linux). Everything else re-raises: a full disk (`ENOSPC`), a bad
  fd, or a bug in a custom stream must stay loud. `except OSError: pass` would have "fixed" the red and
  destroyed the operator's only signal of an outage.
* do **not** close `self.wfile` when the peer vanishes. My first attempt did, and `handle_one_request()` ends
  with `self.wfile.flush()` → `ValueError: I/O operation on closed file` — a new traceback, printed for the
  non-event I was silencing. The stream is replaced with an inert suppressor instead.

Two meta-findings, because they are the useful part:

* **A second-order bug in the fix itself.** `except (self.PEER_GONE, OSError)` looks like a union of a tuple
  and a class; Python rejects it only at *runtime*, on the first request that trips it
  (`TypeError: catching classes that do not inherit from BaseException`). A syntax check, an import, and the
  whole suite on Linux all stayed green until a real aborted client arrived. Now pinned by a test that aborts
  a socket mid-headers on three routes, and by a direct unit test of `_peer_vanished` that asserts ENOSPC is
  **not** peer-gone.
* **The fuzzer's own oracle had a hole** — its exception regex missed bare `TypeError`/`NameError` lines, so
  it reported `tracebacks: 33` while showing me nothing. The matcher now enumerates the suffixes
  (`Error|Exception|Interrupt|Exit|Warning|Failure`), which is what immediately surfaced
  `NameError: name 'self' is not defined` on the next run. An oracle that cannot see a class of failure is
  worse than no oracle: it makes the silence look audited.
---

## F42 — MED · the repo read its own source with the platform's default codec (windows-latest leg)

After F41 the only two Windows reds left were *my* tests, both the same line, and it was not the protocol:

    tests/test_pc_eye.py:596: src = pathlib.Path(pceye.__file__).read_text()
    encodings/cp1252.py, line 23, in decode → UnicodeDecodeError

`server.py` legitimately contains em-dashes and arrows (a refusal must read like a sentence an operator
recognises, not a stack frame), and Windows' default text encoding is cp1252. Any code in this repo that
reads or writes its own files without naming an encoding is therefore **correct on Linux and broken on
Windows** — including the ship-gate evidence script: `evidence/negative_check.py` did
`write_text(...)` of that same source into a tmp file, which on Windows is `UnicodeEncodeError` at the exact
moment a human is trying to prove the suite catches real bugs.

Fixed all six reads and two writes, then made it unrepeatable: `tools/check_docs.py` now walks every `.py` in
the repo with `ast` and fails if `read_text`/`write_text`/`open` touches a text file without `encoding=`.

Two lessons from building the gate, both about my own tooling rather than the product:

* **My first version was a regex over characters** that scanned for the closing paren of each call. It had an
  off-by-one and reported `(e` as the argument list — so it flagged the *already-fixed* lines and, worse, in
  another shape would have flagged nothing at all. A gate whose failure list is nonsense is worse than no
  gate: it gets switched off (agency F15/F27). Rewritten as an `ast` walk; it cannot misparse what it parses.
* **The gate found a bug I had not looked for** (`negative_check.py`'s write) on its first honest run, and it
  found a false alarm in itself on that same run (`open(os.path.join(ROOT, name)` with `encoding=` on the
  next line). Both are why the rule is "the check ships with its negative control": the tests asserting
  *2/2 caught, 4/4 skipped* are part of this finding — a "flag everything" check would have gone green.

Cross-platform rule, written down because the repo keeps relearning it: on this project, `open()`/`read_text`
without `encoding=` is a bug even when the code is Linux-only, because the CI leg is not.

### F43 · MED · the test suite poisoned a shared class seam, and CI's runner order hid it for a full day

*Found:* 2026-10-27 14:50Z (vesper), by running `python3 -m pytest tests/ -q` after the soak, on a tree CI had
declared green. *Status:* fixed, `tests/test_pc_eye.py` + `tests/run_ordered.py`.

`WindowsBehaviour.tearDown` restored the two handler seams it had patched with
`pceye.Handler.input_space, pceye.Handler.display_rect = self._cls_seams`, where the save side had read
`cls.input_space` **off the class**. Reading a `staticmethod` off a class hands you the bare function; writing
that back installs an *instance method*. So from that moment in a process, every `self.input_space()` in
`server.py` raised `TypeError: input_space() takes 0 positional arguments but 1 was given` — 8 red tests in
`RegressionV103`, the class that happens to sit after it in the file.

Two things make it worth a number rather than a quiet fix:

1. **CI could not have seen it.** `tools/ship.sh`/CI run `python tests/run_all.py --all`, whose suite step is
   `unittest discover` — which loads classes **alphabetically**: `RegressionV103` before `WindowsBehaviour`, so
   the victim ran before the poison and everything passed. The same code, run in pytest's **source order**, is
   red. Both runners were honest; the gate sampled one of the two orders. Any shared-mutable-state defect is
   only as visible as the order you happen to run in — so the gate now runs *both* (`tests/run_ordered.py`,
   classes in source order taken from the AST, because pytest is not a dependency and is absent from the Windows
   images CI uses), and `ZSeamIntegrity` asserts in either order that the seams are back to being `staticmethod`
   objects whose `__func__` is the module function.
2. **My own hardening made it fatal and I shipped it anyway.** The `self.input_space()` call that crashes is
   inside the /act bounds gate I added in `76bf8d4`, the first commit of this audit; the leak predates it. At
   `76bf8d4` the same command was `10 failed, 57 passed` and I ran per-class commands that were green, so I
   recorded "tests pass" without ever running the file in one process. *Every* "N passed" I have quoted today is
   suspect for the same reason, which is why the fix is in the gate and not in my notes.

Also found in the pass and fixed: the fuzz class patched `Handler.input_space`/`display_rect` to a fixed
1920x1080 and **never restored them** — a leak that does not crash, so every class after it silently tested
against invented geometry and could pass scaling assertions it had not earned. The `ZSeamIntegrity` identity
check (`__func__ is pceye.input_space`) is what catches that shape, and it is the more dangerous of the two: a
leak that crashes is a gift, a leak that lies is a debt.

Observed and *not* fixed, deliberately: `free_port()` binds port 0, closes it and returns the number, so two
classes can pick the same port — a TOCTOU race that the ~1,900 TIME-WAIT sockets from my 200k-request soak would
have made likelier, and which is a candidate for the next unexplained red here. Closing it means `build()`
accepting a bound socket instead of a port number, i.e. an API change to the thing under audit, in a turn that
had other work to do. Recorded in `PC-EYE.md`/here so the next person does not re-derive it.

### F44 · MED · `Protocol` asserted a screen size it never asked for, and was green on Windows only by inheriting another class's stub

*Found:* 2026-10-27 14:58Z (vesper), as the second half of F43 — it is what the leak had been covering for.
*Status:* fixed, `tests/test_pc_eye.py` (`Protocol` pins `display_rect`) + `tests/run_ordered.py --isolated`.

`/state` prefers the OS over the injected fake: `rect = self.display_rect()`, and `sz = (…) if rect else
self.screen.size()`. `Protocol` installs `FakeScreen` (1920×1080) and asserts `body["screen"] == [1920, 1080]`
— but on a machine with a real display, `display_rect()` answers and the fake is never consulted. Headless Linux
returns nothing, so the fallback fires and the assertion is true; `windows-latest` has a desktop at 1024×768 and
the assertion is false. It had been passing on the Windows leg for exactly one reason: `FuzzFindings` left a
1920×1080 `display_rect` stub welded onto the shared `Handler`, and under `unittest discover`'s alphabetical
order the fuzzer ran *before* `Protocol`. Fixing F43 removed the crutch and the test went red on Windows in the
same push — which is the correct outcome and the real finding: **the suite was not testing the same thing on
both platforms, and the bug report that revealed it came from the leak fix, not from a Windows user.**

Fixed by making `Protocol` install the geometry it asserts about (save/restore via `save_seams`) — **both
seams, not the one I first noticed**: pinning `display_rect` alone made the Windows legs go red a second time,
with `/act` answering 409 "eyes and hand are in different spaces: /shot returns 1920x1080 but the input calls
live in 1024x768", because the class then claimed a 1080p desktop to the coordinate gate while `input_space`
still reported the runner's real 1024x768. Half a fake is worse than none: it makes the two ends of a
comparison disagree on purpose. `input_space` is pinned too, so the class asserts inside one coherent space. Verified by A/B rather than by reading: a `display_rect` that answers
like a desktop, with and without the pin — without it `Protocol` fails, with it `Protocol` passes. (Two other
tests failed under that same simulation; both were the simulation's fault, not the product's — they patch
`pceye.display_rect` at module level and call it directly, and my override bypassed the cache they assert on.
The real Windows legs in CI are the arbiter for those, and they are the reason the legs exist.)

Two general rules come out of this, both now enforced rather than advised:

1. **A test that asserts a fake must install the fake.** An assertion about geometry, a clock, or a screen size
   that relies on "nobody else in this process touched it" is a cross-platform lie waiting for a runner that
   changes the order. `--isolated` runs every class alone in its own process and fails anything that cannot stand
   by itself: the green that needs company is the interesting one.
2. **Module-level patches do not reach the handler.** `Handler.display_rect = staticmethod(display_rect)` binds
   the *function object* at import; rebinding `pceye.display_rect` afterwards changes the module's name, not the
   class's attribute. Every test that wants to steer handler behaviour must patch the class (which is why these
   sites all go through `save_seams`/`restore_seams` now) — and this is the trap the F41 `Launcher` test nearly
   fell into too, from the other side.

Sequence, because it is the part worth keeping: `0d50fd1` (F43+F44 first half) → both `windows-latest` legs red
with the 409 above → `c33a8f3` added the `display_rect` pin, still red (that commit is where the 409 text comes
from) → the pin extended to `input_space` here. Verified locally by A/B on a simulated 1024x768 desktop, patched
onto `pceye.Handler` before `Protocol.setUpClass` runs (patching `pceye.display_rect` at module scope would prove
nothing, because `Handler.display_rect` is bound to the function object at import — the same trap as rule 2
above): without the pin 1 of 18 tests fails on the /state assertion, with it all 18 pass. That A/B does *not*
reproduce the 409 pair — the /act path reads `/shot`'s capture size, which needs the real screen — so the
Windows legs in CI are the arbiter for those, and the claim here is only that the pin makes the class
self-consistent, not that a Linux box can emulate a desktop.
