# pc-eye

**An eye and a hand on your PC, for an agent that lives in a chat.**

An agent in a chat can make HTTP requests but cannot hold a socket to your home machine. `pc-eye` is the
smallest honest thing that closes that gap: a local server on 127.0.0.1 that lets the agent *look* at your
screen and *act* on it, through a tunnel you start yourself. No API keys, nothing running in the background,
and the off switch is the console window you can close.

```
GET  /state      screen size, cursor, uptime, last action, coordinate-space check
GET  /shot       the screen, as an image the agent looks at
POST /act        one action: click / move / type / key / drag / scroll / paste
GET  /selftest   what this machine can and cannot do, reported honestly
```

## Run it

Double-click **`start.bat`** (install Pillow once if asked). It prints two lines:

```
URL:   https://<something>.trycloudflare.com
TOKEN: 8 hex characters
```

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

Both failures were invisible from Linux and obvious within minutes on the real machine.

## Tests

```
python -m unittest discover -s tests        # 32 tests, headless-safe
```

They cover the routes, the token, the action contract, the refusals, the log, the coordinate-space verdict
(the 1920x1080 / 1536x864 case is a regression test), the chord resolution and the dry-run behaviour. They
cannot prove that a click lands on the right pixel on *your* Windows box — `/selftest` and the `exact` field
on every action are what tell you that, on the machine itself.

## Known limits, stated rather than hidden

* **Ctrl+Alt+Del cannot be pressed** by any user-mode program; Windows reserves it.
* **Elevated windows (UAC / admin apps) will not take the input**, and their pixels may come back black.
* One machine, one screen. Multi-monitor is captured as one wide surface.
* If the PC sleeps or you close the console window, requests fail. That is your switch, and it is deliberate.
