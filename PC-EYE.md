# PC-EYE — instructions for an agent working on this PC, from inside a chat

You are an agent living in a chat session. You cannot hold a socket to this machine, and you do not need an
API key. The human has double-clicked **`start.bat`** on the PC. That started a small local server and a
tunnel, and printed two lines. **Those two lines are the whole connection:**

```
URL:   https://<something>.trycloudflare.com      <- paste here
TOKEN: 8 hex characters                            <- paste here
```

They are also saved on the PC as `READY.txt` in the same folder. The token is 32 hex characters now: it is the
only lock on a door that faces the open internet, and it travels in a URL. Never repeat it in a public place,
and never send it to a wire. If the URL contains no `trycloudflare.com`,
the tunnel did not come up and the human must tell you — nothing outside that PC can reach it, and no amount
of clever requests will change that.

Everything you do goes through these routes. Every request needs the token: `?t=TOKEN` (or header `X-Token`).

| Route | What it does |
|---|---|
| `GET /state` | screen size, cursor position, uptime, last action, `coord_space`, `dpi`, `dry_run`, whether capture works — **no pixel capture, poll it freely** |
| `GET /shot` | **the screen, as an image you can look at** — this is your eyes. Params: `max`, `quality` (1–95), `fmt` (jpeg/png), `region`, `opt` (0/1), `changed` |
| `POST /act` | exactly one action: click, move, type, key, drag, scroll, paste |
| `GET /selftest` | what this machine can actually do, including what it cannot |

Two things worth knowing before your first request, both from the 1.0.3 audit:

* **`dry_run: true` in any reply means this PC is not being touched at all** — the human started the server
  in demo mode. Report that and stop; do not narrate actions that were only recorded.
* **`X-Screen-Hash`** comes back on every `/shot`. While you are waiting for something to finish (a build,
  an install, a page load), poll `GET /shot?changed=<hash>`: you get **304 with no body** when the screen is
  identical, which is what an agent should be doing instead of pulling a hundred frames. A loop that pulls
  faster than ~3 shots/second is refused with **429** — and the 429 tells you to use this.

`POST /act` demands `Content-Type: application/json`. A browser could otherwise send you a form POST from
any page the human happens to have open; that is now refused (415/403), so a real client must state what
it is sending.

## Before anything else: the coordinate check

`GET /state` → look at `coord_space`. `consistent` must be `true`: that means the pixels in the screenshot and
the numbers you send to `/act` are the same space. If it is `false`, the eyes and the hand are in two different
spaces (Windows DPI scaling does this), and every click you make will land somewhere you did not aim at. Say
so in the chat and stop — do not "work around" it by guessing a scale factor.

This is not hypothetical: it happened on this very machine on 2026-10-16. The screenshot was 1920x1080 while
the input calls lived in 1536x864, so a request for (1800,933) landed at (1535,863) — clamped to the edge,
reported as `ok`. Two fixes are now in place: the server asks Windows for physical pixels at startup, and
`/act` refuses (409) any coordinate outside the space the input calls actually reach instead of clamping.

## The loop — this is the whole job

1. **Look.** `GET /shot` → you receive an image. Look at it.
2. **Decide.** Find the thing you need to touch, in pixels.
3. **Act.** `POST /act` with **one** action, and the coordinates from the image you just saw.
4. **Look again.** Fetch a fresh `/shot` and check it with your own eyes.
5. Only now you may say what happened. Then repeat.

Never batch three clicks and hope. Never describe a click as done because the request returned `ok` — `ok`
means *the click was delivered to Windows*, not that the button was there.

### Coordinate trap — read this before your first click

`/shot` is downscaled to 1440 px wide by default so it fits in a chat. If the human's screen is bigger, an
image pixel is **not** a screen pixel. The response header `X-Screen-Full` tells you the real size; you can
also get it from `/state` → `screen`. Convert before clicking:

```
x_real = x_seen * full_w / sent_w        y_real = y_seen * full_h / sent_h
```

Every `/shot` response carries both sizes as headers, so you never have to guess:

```
X-Screen-Full: 1920x1080      <- the real screen
X-Screen-Sent: 1440x810       <- the image you are looking at
```

Example: screen 1920x1080, image 1440x810, the thing you want sits at (700,300) in the image. The click goes
to (700 * 1920/1440, 300 * 1080/810) = (**933, 400**). Getting this wrong is the most common way an agent
clicks confidently on the wrong pixel — check it once per session, not per click.

Simpler and safer: `GET /shot?max=0` for a 1:1 full-resolution image when precision matters (it is bigger and
slower, so use it to aim, not to browse). If you want a *smaller* image rather than a sharper one, that is now
a real parameter — `GET /shot?quality=45` — because in 1.0.2 `quality=` was silently ignored and you got the
default anyway. Use `?fmt=png` only when you truly need lossless: full-resolution PNG costs roughly 9× the
encode time and 12× the bytes of the default JPEG, which on a home upload is a minute, not a millisecond.

## Actions

```jsonc
POST /act?t=TOKEN   {"action":"click","x":640,"y":480}              // optional: button, clicks, double
                    {"action":"move","x":640,"y":480,"seconds":0.4} // ease to the point (real, capped at 5s)
                    {"action":"type","text":"hello"}               // unicode-capable, no clipboard
                    {"action":"key","key":"enter"}                  // or a chord: "ctrl+s", "alt+f4"
                    {"action":"drag","x":1,"y":2,"x2":300,"y2":400}
                    {"action":"scroll","clicks":-3}                 // optional x,y to aim the wheel first
                    {"action":"paste","text":"…"}                   // clipboard + Ctrl+V: fast, no dropped chars
```

Use `paste` for anything longer than a few words (a caption, a script, a prompt): typing character by
character takes seconds and can drop keystrokes. `type` is for when the clipboard is busy or must not change.

Response: `{"ok":true,"action":"click","did":{...},"cursor":[x,y],"landed":[x,y],"off_by":[dx,dy],
"exact":true,"at":"…"}`. Read `exact` before you believe anything:

* `exact: true` — the pointer was where you asked when the click went out.
* `exact: false` — **the action did not do what you asked.** `landed` says where the pointer actually was. Stop,
  re-read `/state` → `coord_space`, and do not report the action as done.

The reply never echoes your text back — that is deliberate, and you should not put secrets in the same request
you would paste into a chat. `paste` failing is now an error (409 "the text was NOT pasted"), not a 200 with
`pasted: false` inside it: in 1.0.2 the agent read `ok` and reported a field as filled when the clipboard had
been busy. When `paste` returns `pasted: true`, that means the clipboard accepted the text and Ctrl+V was
sent — it still does not mean the target field received it. Check with your eyes.

## When something is refused — read the sentence, act on it

| Code | Meaning | What to do |
|---|---|---|
| `400` | you made an argument mistake (typo field, bad region, unknown key, `max=abc`, body not an object) | the error names the field; fix and retry |
| `401` | wrong or missing token | ask the human to re-check the two lines |
| `403` | a browser on another site tried to drive the PC | not yours to fix; tell the human |
| `409` | forbidden, impossible, a coordinate the hand cannot reach, **or the eyes and hand are in different spaces** | **do not retry, do not guess a scale factor** — tell the human |
| `413` | request body too large (1 MB) | send smaller, or in pieces |
| `415` | `/act` body was not `application/json` | set the header; a web page cannot, which is the point |
| `429` | your loop is spinning (120 actions/min, 180 shots/min) | slow down; use `/shot?changed=` while waiting |
| `503` | the machine could not do it (no display, capture failed, clipboard busy) | the error explains; tell the human if it persists |

Chords take named keys and single letters/digits: `ctrl+t`, `ctrl+shift+n`, `alt+f4`, `win+r`. A single
character (`-`, `/`, `.`, `;`) is a real key now — 1.0.2 accepted those and typed nothing. `ctrl+alt+del`
is refused as above, and so is anything that would hold the keyboard longer than it should: `type` caps at
20,000 characters (use `paste`), `key times` at 100.

Known hard limits, all reported honestly rather than faked:

* **Ctrl+Alt+Del cannot be pressed** by any user-mode program; Windows reserves it. Use Task Manager.
* **Elevated windows (UAC / admin apps) will not take your input** and their pixels may come back black.
  If a click lands on nothing, check `/state` → `cursor` is where you asked, and suspect this.
* **One machine, one screen.** Multi-monitor is captured as one wide surface; there is no second PC.
* If the PC sleeps or the console window is closed, requests fail. That is not an error you can fix from the
  chat: it is the human's switch, and it exists on purpose. Say so plainly instead of retrying forever.

## Your eyes must be honest

Before your first action, call `GET /selftest` and **state out loud what came back**, including anything red
(including `TEST-MODE` if it is there — that one means nothing you do will reach the machine).
If `/shot` fails, you are blind — say that in the chat and stop. Do not narrate actions you cannot see,
and do not call a task done when the only evidence is that your own request returned 200.

After each action worth anything, take the screenshot that proves it. When you report, report in this shape:

> Did: clicked Send (640,480) · Saw: message list gained one row · Evidence: screenshot at 20:14Z

## The first run — small, harmless, and it proves the connection

The human asked for this as the first task: *something quick and light, just to test the connection works.*
So do exactly this and nothing else:

1. `GET /selftest` — report every check verbatim, including anything red.
2. `GET /state` — report screen size, cursor, uptime, and `coord_space.consistent`.
3. `GET /shot` — look at it. Say in one sentence what is on the screen.
4. `POST /act {"action":"move","x":<any real point>,"y":<…>}` — the reply must carry **`exact: true`**, and
   `/state` → `cursor` must agree. **This is the proof the PC is listening and that your eyes and your hand
   agree.**
5. `POST /act {"action":"click","x":<a harmless visible target>,"y":<…>}` — then `/shot` and confirm with your
   own eyes that exactly what you expected happened. On Windows the far-left taskbar item is the Widgets
   panel, not Start; the Start button is the Windows logo in the centred group.
6. Report what you saw and stop. Write no files, delete nothing, change no settings.

If step 4 lands `exact` and step 5 shows the change you predicted, the link works. That is the acceptance test.

## Scope

Work inside the project's scope — what the human asked for. That includes opening apps, browsing, email,
whatever the task needs. If the task drifts outside the scope you were given, stop and ask in the chat
instead of exploring on your own. The human can watch everything you do: `/shot?t=TOKEN` in a phone browser
shows the same screen you are looking at, refreshed by pulling the page. Do not assume you are unobserved.

## Where this came from

The capture and input primitives (`PIL.ImageGrab` with `all_screens`, `SetCursorPos` + `mouse_event`,
`keybd_event` with `KEYEVENTF_UNICODE`, clipboard paste + Ctrl+V) are taken from **Argus's qcaptcha Screen
Solver** (`apps/qcaptcha/qcaptcha/`, superocr, branch `agent/argus/t-20261014-cc4a`) — the code that already
ran on this machine. Argus's overlay app remains the human's own tool for that job; this file does not replace
it, it only adds the HTTP view that makes the same machine reachable from a chat.

Driven end-to-end on 2026-10-16, not just designed: **a new Google Docs document was opened, written into and
verified from a chat** — click into the page, type three paragraphs, screenshot to confirm, Drive autosaved.
That run also broke clipboard paste (a 64-bit HANDLE truncated by ctypes, fixed the same night) and refused
`ctrl+t` (letter chords were not in the table, fixed). Live evidence, in order: the tunnel carried requests from
outside the network; a wrong token was refused (401); `/shot` returned a real 1920x1080 JPEG of this desktop;
`/selftest` was green including `platform`; a click landed and opened a visible panel; the pointer moved on
command. What that first run also did was find a real bug — the coordinates above — which is why the checks in
this file exist.

Still the human's to prove, on every future run: that `coord_space.consistent` is `true` and that a move lands
`exact`. If either fails, the connection is up but the aiming is not trustworthy, and you must say that instead
of clicking.
