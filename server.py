#!/usr/bin/env python3
"""pc-eye — a window into this PC, for an agent that lives in a chat session.

WHY THIS EXISTS
---------------
The Director's requirement, in his words: *"i'll give it an md file containing all the instructions to connect
to the pc and work, and it'll do that. in the chat."* An agent inside a chat can make HTTP requests but cannot
hold a socket to somebody's home machine. So the missing piece is not a smarter agent and not an API key — it
is **the smallest honest HTTP surface over a screen and an input device**:

    GET  /state      -> JSON: screen size, cursor, uptime, last action
    GET  /shot       -> the screen, as an image the agent can open and look at
    POST /act        -> exactly one action: click / move / type / key / drag / scroll / paste
    GET  /selftest   -> exercises capture and reports what it actually found

The agent's loop is then: `/shot` -> look at it -> `/act` -> `/shot` -> ... which is what "constantly receiving
screenshots and clicking around" means when the agent is in a chat.

WHAT IS NOT INVENTED HERE
-------------------------
The primitives are lifted from `apps/qcaptcha/qcaptcha/` (Argus, branch `agent/argus/t-20261014-cc4a`,
superocr) — the code that has already run on this machine:

  * capture ......... `PIL.ImageGrab.grab(bbox=..., all_screens=True)` (with the old-Pillow fallback)
  * click ........... `SetCursorPos` + `mouse_event(0x0002 / 0x0004)` (LEFTDOWN/LEFTUP)
  * typing .......... `keybd_event(0, ord(ch), KEYEVENTF_UNICODE, 0)` + key-up
  * paste ........... clipboard via ctypes, then Ctrl+V

Only the HTTP wrapper, the token and the honest reporting are new. Where something cannot be verified on this
machine, this file says so instead of pretending (`/selftest`, and the `capture` field in `/state`).

SAFETY, STATED PLAINLY
----------------------
* Binds 127.0.0.1 by default. It is reachable from outside **only** through the tunnel the launcher starts,
  and every request must carry the token printed at startup.
* The human's off switch is the console window: closing it, or Ctrl+C, stops everything. There is no remote
  way to make this process outlive that, on purpose.
* Every action is appended to `pceye.log` and to the console, so there is a record of what was done and when.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import io
import json
import os
import platform
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

VERSION = "1.0.3"
SYSTEM = platform.system()
DEFAULT_PORT = 8790
# The whole product is "look, decide, act, look again" — so the limits below exist to keep a
# chatty agent from hanging the machine it is driving. A 200k-character `type` holds the input
# lock for 13 minutes and the human gets their keyboard back only when it finishes.
MAX_TYPE_CHARS = 20000        # longer than any prompt/caption worth typing; use paste for more
MAX_KEY_TIMES = 100           # times=1000000 was accepted and slept for ~83 minutes
DISPLAY_TTL = 20.0            # seconds: how often we re-ask the OS for the display geometry
REQ_BODY_MAX = 1_000_000      # 1 MB of JSON is generous; anything bigger is not an action
ACT_RATE_PER_MIN = 120        # sustained input actions per minute (see RateBucket)
SHOT_RATE_PER_MIN = 180       # a look-decide-act loop is ~1-2 req/s; this is 3/s, generous on purpose
BURST_ACT, BURST_SHOT = 30, 60
MAX_EASE_S = 5.0              # `move seconds=` is honoured now, and capped
MAX_SCROLL_CLICKS = 100       # one click is 120 units of wheel; 100 is a screenful, not a stack overflow
MAX_INTERVAL_S = 0.5          # a per-character delay is a tuning knob, not a sleep budget
MAX_PASTE_CHARS = 200000      # clipboard text is cheap; still not unbounded
# action name -> the method on Input that performs it. Kept beside the contract so a new action cannot
# exist in the schema and be missing from the implementation (a test asserts the two match).
METHOD = {"click": "click", "move": "move", "type": "type_text", "key": "key",
          "drag": "drag", "scroll": "scroll", "paste": "paste"}
# actions that place the pointer — the ones a wrong coordinate space silently ruins
POINTING_ACTIONS = ("click", "move", "drag", "scroll")
COORD_TOL = 2               # px of tolerance before a landing counts as "not where you asked"
ACTIONS = {
    # action    required fields            optional fields
    "click":   (("x", "y"),               ("button", "double", "clicks")),
    "move":    (("x", "y"),               ("seconds",)),
    "type":    (("text",),                ("interval",)),
    "key":     (("key",),                 ("times",)),
    "drag":    (("x", "y", "x2", "y2"),   ("seconds", "button")),
    "scroll":  ((),                       ("clicks", "x", "y")),
    "paste":   (("text",),                ()),
}
START = time.time()
LOCK = threading.Lock()
LAST = {"action": None, "at": None, "args": None}


def set_dpi_awareness() -> dict:
    """Make this process see PHYSICAL pixels, so the eyes and the hand share one coordinate space.

    The bug this exists to kill (found on CarloPC, 2026-10-16): Pillow's ImageGrab raises the thread's DPI
    awareness for the capture, so the screenshot came back 1920x1080 — while SetCursorPos/GetCursorPos, in a
    DPI-unaware process, lived in the 1536x864 (125% scaling) space and *silently clamped* to it. The agent
    then reasons in image pixels and clicks somewhere else entirely. Two spaces in one process is the whole
    bug; a click that lands in the wrong place must never be able to look like success.

    Called once, before anything captures or clicks. Falls back through the three Windows APIs in order of
    preference and reports which one answered, so /state can say what actually happened.
    """
    info = {"dpi_aware": False, "method": None, "error": None}
    if SYSTEM != "Windows":
        info["error"] = f"n/a on {SYSTEM}"
        return info
    import ctypes
    tries = (
        ("SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)",
         lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))),
        ("SetProcessDpiAwareness(PER_MONITOR)",
         lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2)),
        ("SetProcessDPIAware()",
         lambda: ctypes.windll.user32.SetProcessDPIAware()),
    )
    for name, call in tries:
        try:
            if call():
                info.update(dpi_aware=True, method=name)
                return info
        except Exception as e:
            info["error"] = f"{name}: {type(e).__name__}: {e}"
    return info


def input_space():
    """The rectangle the input calls actually live in, in this process's own coordinates.

    Wrapped in a guard for a reason: this runs on EVERY /act, and `ctypes.windll` does not exist on
    Linux/macOS. The `SYSTEM != "Windows"` check above was the only thing between a non-Windows box and
    an AttributeError raised inside the route handler — which does not answer 500, it drops the
    connection, and "connection closed" is what a dead PC looks like to an agent.
    """
    if SYSTEM != "Windows":
        return None
    try:
        import ctypes
        u32 = ctypes.windll.user32
        u32.GetSystemMetrics.restype = ctypes.c_int
        w, h = u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)
        return [int(w), int(h)] if w and h else None
    except Exception:
        return None                                      # no verdict rather than a dead connection


_display_cache = {"rect": None, "at": 0.0}


def display_rect(force=False):
    """Physical pixels of the whole virtual desktop — WITHOUT grabbing a single pixel.

    /state used to learn the screen size by running `ImageGrab.grab(all_screens=True)` and
    reading `.width/.height` off the result: a full lossless screen capture (tens of MB, and
    on Windows DPI-awareness work on top of it) just to answer "how big is the monitor", and it
    ran on *every* /shot too, since /state and /shot share the route. GetSystemMetrics says the
    same thing in about a microsecond; Xlib says it here for the Linux test box.

    Cached for DISPLAY_TTL seconds (geometry only changes if the human plugs something in), and
    it reports the VIRTUAL desktop origin, so a monitor left of / above the main one — which
    gives negative coordinates — is described correctly instead of being refused.
    """
    now = time.time()
    if not force and _display_cache["rect"] and (now - _display_cache["at"]) < DISPLAY_TTL:
        return _display_cache["rect"]
    rect = None
    try:
        if SYSTEM == "Windows":
            import ctypes
            u32 = ctypes.windll.user32
            SM_XVIRTUAL, SM_YVIRTUAL, SM_CXVIRTUAL, SM_CYVIRTUAL = 76, 77, 78, 79
            x, y = u32.GetSystemMetrics(SM_XVIRTUAL), u32.GetSystemMetrics(SM_YVIRTUAL)
            w, h = u32.GetSystemMetrics(SM_CXVIRTUAL), u32.GetSystemMetrics(SM_CYVIRTUAL)
            if w > 0 and h > 0:
                rect = [int(x), int(y), int(w), int(h)]
        elif SYSTEM == "Linux":
            import ctypes
            x11 = ctypes.CDLL("libX11.so.6")
            x11.XOpenDisplay.restype = ctypes.c_void_p
            d = x11.XOpenDisplay(None)
            if d:
                s = x11.XDefaultScreenOfDisplay(d)      # c_void_p by default: fine for these calls
                x11.XWidthOfScreen.restype = ctypes.c_int
                x11.XHeightOfScreen.restype = ctypes.c_int
                x11.XCloseDisplay.restype = ctypes.c_int
                w, h = x11.XWidthOfScreen(s), x11.XHeightOfScreen(s)
                x11.XCloseDisplay(d)
                if w > 0 and h > 0:
                    rect = [0, 0, int(w), int(h)]
    except Exception:
        rect = None                                      # no verdict rather than a wrong one
    if rect:
        _display_cache.update(rect=rect, at=now)
    return rect


def coord_gate(action, body, capture_full, in_space):
    """May this action go to the OS? Pure, so every refusal is a test and not a Windows session.

    Returns None to allow, or (status, payload) to refuse. Two spaces in one process is the
    failure that LOOKS LIKE SUCCESS — the agent aims in image pixels, the pointer lives somewhere
    else, and the reply says ok — so a mismatch is a hard stop, exactly as PC-EYE.md promises.
    """
    if action not in POINTING_ACTIONS:
        return None
    if not capture_full or not in_space:
        return None                     # cannot judge; the bounds check below still guards the clamp
    cw, ch = capture_full
    iw, ih = in_space
    if abs(cw / iw - 1.0) >= 0.01 or abs(ch / ih - 1.0) >= 0.01:
        return 409, {"ok": False, "action": action,
                     "error": f"eyes and hand are in different spaces: /shot returns {cw}x{ch} but the input "
                              f"calls live in {iw}x{ih}. Every click would land somewhere other than where you "
                              f"aimed, so /act refuses to guess a scale factor. Fix it on the PC: DPI awareness "
                              f"did not take (see /state -> dpi.method). Do not work around this.",
                     "coord_space": {"capture": [cw, ch], "input_space": [iw, ih], "consistent": False}}
    return None


def strict_int(name, v, lo=None, hi=None, whole=True):
    """For the optional knobs (`clicks`, `seconds`, `times`, `interval`), which reached ctypes raw in 1.0.2.

    num() only ever saw x/y/x2/y2. So `scroll clicks:"three"` raised ValueError inside the Windows branch
    (a dead socket and no message), `clicks:true` scrolled one notch because True IS an int in Python, and
    `type interval:3600` slept an hour per character. One funnel, one phrasing, everywhere.
    """
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise ValueError(f"{name} must be a number, not true/false — got {v!r}")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number — got {v!r}")
    except OverflowError:
        raise ValueError(f"{name} is far outside anything sane — got {v!r}")
    if not -1e308 < f < 1e308:      # covers inf/-inf; ordering matters, see the comment below
        raise ValueError(f"{name} is not a usable number — got {v!r}")
    # Order: finiteness FIRST. `f != int(f)` is the obvious whole-number test and it raises
    # OverflowError on infinity — which the fuzzer turned into the third dead-socket instance of the same
    # root cause. Guard, then compare, never the reverse.
    if whole and f != int(f):
        raise ValueError(f"{name} must be a whole number — got {v!r}")
    out = int(f) if whole else f
    if lo is not None and out < lo:
        raise ValueError(f"{name} must be >= {lo} — got {out}")
    if hi is not None and out > hi:
        raise ValueError(f"{name} must be <= {hi} — got {out}")
    return out


def num(name, v, lo=None, hi=None, integer=True):
    """One place that turns a query/body value into a number or raises a sentence an agent can act on.

    Before this, `/shot?max=abc` ran `int("abc")` inside the route — the handler died, the tunnel
    returned "connection closed without response", and the agent could not tell a typo from a dead PC.
    """
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        # `{"x": true}` used to type-check as 1 and click a real pixel. A boolean is not a coordinate,
        # in Python's eyes or anyone else's, and silently coercing it is the ok-shaped lie again.
        raise ValueError(f"{name} must be {'an integer' if integer else 'a number'}, not true/false — "
                         f"got {v!r}")
    try:
        out = int(v) if integer else float(v)
    except (TypeError, ValueError):
        want = "an integer" if integer else "a number"
        raise ValueError(f"{name} must be {want} — got {v!r}")
    except OverflowError:
        # `{"x": 1e999}` is legal JSON (Infinity) and int(Infinity) raises OverflowError, which 1.0.2
        # never caught either: it escaped the handler and the connection just died. Note the float branch
        # does NOT raise — float("1e999") is simply inf — so the finiteness test below is what covers it.
        raise ValueError(f"{name} is far outside any screen — got {v!r}")
    # Compare integers as integers. My first finiteness guard here said `float(out) < 1e308`, which is
    # precisely the bug it was written to fix: `{"x": 10**400}` is legal JSON, int() accepts it, and
    # float(huge_int) raises OverflowError — an escaped exception, i.e. a dead socket again. So the
    # magnitude test is an integer comparison and float() only runs on something already known to fit.
    if not -1e308 < out < 1e308:
        raise ValueError(f"{name} is far outside any screen — got {v!r}")
    if integer:
        # float(v), not float(out): `out` is int(v) — already truncated — so comparing it with itself
        # would accept 3.5. That is how the second version of this guard broke its own test. A number that
        # does not fit in a float is refused above, so this conversion cannot raise.
        if float(v) != out:
            raise ValueError(f"{name} must be a whole number of pixels — got {v!r} (rounding a coordinate "
                             f"silently is how an agent ends up one pixel off a scrollbar)")
    if lo is not None and out < lo:
        raise ValueError(f"{name} must be >= {lo} — got {out}")
    if hi is not None and out > hi:
        raise ValueError(f"{name} must be <= {hi} — got {out}")
    return out


class RateBucket:
    """A tiny token bucket: burst, then a sustained rate. Fail-open by design.

    This endpoint can click and type on a human's desktop, over a public tunnel, for an agent that
    can loop by accident. A hard limiter that misfires would break real work, so on any weirdness
    we allow the request; the point is to blunt a runaway loop, not to police a person.
    """

    def __init__(self, capacity, refill_per_s):
        self.cap, self.rate, self.level, self.seen = capacity, refill_per_s, capacity, time.time()

    def take(self, n=1.0) -> bool:
        try:
            now = time.time()
            self.level = min(self.cap, self.level + (now - self.seen) * self.rate)
            self.seen = now
            if self.level >= n:
                self.level -= n
                return True
            return False
        except Exception:
            return True


def coord_space_report(capture_size, in_space) -> dict:
    """Are the eyes and the hand in the same space? Pure function, so the CarloPC case is a unit test.

    capture_size: [w, h] of what /shot returns.  in_space: [w, h] from input_space().
    """
    out = {"consistent": None, "capture": list(capture_size) if capture_size else None,
           "input_space": list(in_space) if in_space else None, "scale": None, "warning": None}
    if not capture_size or not in_space:
        return out
    cw, ch = capture_size
    iw, ih = in_space
    sx, sy = cw / iw, ch / ih
    out["scale"] = round(sx, 4)
    if abs(sx - 1.0) < 0.01 and abs(sy - 1.0) < 0.01:
        out["consistent"] = True
        return out
    out["consistent"] = False
    out["warning"] = (f"image is {cw}x{ch} but input coordinates live in {iw}x{ih} "
                      f"(scale {sx:.2f}x{'' if abs(sx-sy) < 0.01 else f' / {sy:.2f}'}) — DPI virtualisation is "
                      f"splitting the eyes from the hand; clicks would be silently wrong. /act refuses "
                      f"coordinates outside {iw}x{ih} instead of clamping them.")
    return out


def parse_region(spec):
    """'x,y,w,h' -> (x, y, w, h), or None. Raises ValueError with a sentence an agent can act on.

    Shared by the /shot route and by Screen.grab so that a mistyped region is refused once, in one place,
    instead of being silently reinterpreted as a full-screen shot (which looks like success and wastes a turn).
    """
    if spec is None or spec == "":
        return None
    if isinstance(spec, (list, tuple)):
        parts = list(spec)
    else:
        parts = [p.strip() for p in str(spec).split(",")]
    if len(parts) != 4:
        raise ValueError(f"region must be x,y,w,h (four numbers) — got {spec!r}")
    try:
        # A tuple, not a generator: unpacking a generator runs int() INSIDE the generator, so the
        # OverflowError from a huge float (`region=1e999,...`) escapes this except clause entirely and
        # kills the thread. Same class as every other bug here: the crash is one line away from the parse.
        x, y, w, h = tuple(int(p) for p in parts)
    except (TypeError, ValueError):
        raise ValueError(f"region must be four integers x,y,w,h — got {spec!r}")
    except OverflowError:
        raise ValueError(f"region must be four integers x,y,w,h — got {spec!r} (a float that large is not "
                         f"a coordinate)")
    if w <= 0 or h <= 0:
        raise ValueError(f"region width and height must be positive — got {w}x{h}")
    # A region is a crop, and Pillow raises OverflowError (not ValueError) when a box is astronomically
    # large — which reached the handler as a traceback. /shot already refuses an off-screen region with a
    # sentence, so an absurd one is refused in the same voice, with the real numbers in it.
    if max(abs(x), abs(y), w, h) > 100_000:
        raise ValueError(f"region {x},{y},{w},{h} is not a plausible part of a screen (limits are ±100000 "
                         f"for offsets and 100000 for size). If you want the whole desktop, omit region.")
    return (x, y, w, h)


def content_hash(data: bytes) -> str:
    """Short hash of the encoded frame. It is what makes "tell me if anything changed" possible at
    all: an agent watching a build, a download, a long compile, can poll for the cost of a header
    instead of a few hundred kilobytes per look."""
    return hashlib.sha256(data).hexdigest()[:16]


def z() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ screen ---
class Screen:
    """Capture, downscaled to something a chat agent can actually read and a network can carry.

    A full 2560x1440 PNG is ~2-4 MB; a 1440-wide JPEG at q=72 is ~150-300 KB and every detail an agent needs
    for "where do I click" survives it. Full resolution is available on request with `?max=0`.
    """

    def __init__(self, max_width: int = 1440, quality: int = 72, optimize: bool = False):
        self.max_width, self.quality, self.optimize = max_width, quality, optimize
        self.available = True
        self.why = ""

    def size(self):
        """The honest way to ask how big the screen is: the OS, not a screen capture.

        Kept as a method (and still Pillow-backed as the final fallback) because a test fakes it
        and because `display_rect()` can legitimately know nothing on a box with no display.
        """
        rect = display_rect()
        if rect:
            return rect[2], rect[3]
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab(all_screens=True)
            return img.width, img.height
        except Exception as e:
            self.available, self.why = False, f"{type(e).__name__}: {e}"
            return None

    def grab(self, region=None, max_width=None, fmt="jpeg", quality=None, optimize=None):
        """Returns (bytes, dict) or raises ValueError (the caller's mistake) / RuntimeError (the PC's).

        `optimize=None` means "use self.optimize": JPEG Huffman-table optimisation costs ~25% of the
        encode for ~5% of the bytes, and in the look-decide-act loop those milliseconds are what the
        agent pays 200 times a session. Full-resolution PNG is the opposite trade and still available.
        """
        try:
            from PIL import ImageGrab
        except Exception as e:
            raise RuntimeError(f"Pillow is not installed ({e}) — run: py -3 -m pip install pillow")
        box = None
        r = parse_region(region)            # ValueError -> the route turns it into a 400 the agent can read
        if r:
            box = (r[0], r[1], r[0] + r[2], r[1] + r[3])
            rect = display_rect()
            if rect:                        # an out-of-bounds region must not read as a black screen
                rx, ry, rw, rh = rect
                if r[0] < rx or r[1] < ry or r[2] <= 0 or r[3] <= 0 or \
                        r[0] + r[2] > rx + rw or r[1] + r[3] > ry + rh:
                    raise ValueError(
                        f"region {r} is outside the screen ({rw}x{rh} at {rx},{ry}) — returning it would "
                        f"send you a black rectangle that looks like an empty desktop. "
                        f"{'If the thing you want is off-screen, scroll first or take a wider /shot.' if (r[0] < rx or r[1] < ry) else 'Check the numbers against X-Screen-Full.'}")
        try:
            try:
                img = ImageGrab.grab(bbox=box, all_screens=True)
            except TypeError:          # Pillow < 9.2 has no all_screens
                img = ImageGrab.grab(bbox=box)
        except Exception as e:
            self.available, self.why = False, f"{type(e).__name__}: {e}"
            raise RuntimeError(f"screen capture failed: {type(e).__name__}: {e}")
        self.available, self.why = True, ""
        full = (img.width, img.height)
        mw = self.max_width if max_width is None else int(max_width)
        scaled = False
        if mw and img.width > mw:
            img = img.resize((mw, max(1, int(img.height * mw / img.width))))
            scaled = True
        buf = io.BytesIO()
        # Resolved once, for both formats. `q` used to exist only in the JPEG arm while the meta dict below
        # referenced it unconditionally, so `/shot?fmt=png&quality=50` raised UnboundLocalError inside the
        # handler — an agent got a dead socket on a request whose only crime was asking for a lossless frame
        # with a knob the docs list for that route. The fuzzer found it; my hand-written tests missed it
        # because each of the two parameters had been tested on its own. PNG ignores quality, but says so.
        q = self.quality if quality is None else int(quality)
        opt = self.optimize if optimize is None else bool(optimize)
        if fmt == "png":
            img.save(buf, "PNG", optimize=opt)
            mime = "image/png"
        else:
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.save(buf, "JPEG", quality=q, optimize=opt)
            mime = "image/jpeg"
        data = buf.getvalue()
        meta = {"full": list(full), "sent": [img.width, img.height], "scaled": scaled,
                "bytes": len(data), "mime": mime, "format": fmt, "quality": q,
                "optimize": opt,
                "at": z(), "hash": content_hash(data)}
        return data, meta


# ------------------------------------------------------------------- input ---
class Input:
    """Clicks, keys and text, using the calls that are already proven on this machine."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.did = []

    # -- windows primitives (from apps/qcaptcha/qcaptcha/injection.py, unchanged in substance) --
    @staticmethod
    def _u32():
        import ctypes
        return ctypes.windll.user32

    def _click_windows(self, x, y, button="left", clicks=1, double=False):
        u32 = self._u32()
        u32.SetCursorPos(int(x), int(y))
        time.sleep(0.05)
        down = {"left": 0x0002, "right": 0x0008, "middle": 0x0020}.get(button, 0x0002)
        up = {"left": 0x0004, "right": 0x0010, "middle": 0x0040}.get(button, 0x0004)
        n = 2 if double else max(1, int(clicks))
        for i in range(n):
            u32.mouse_event(down, 0, 0, 0, 0)
            u32.mouse_event(up, 0, 0, 0, 0)
            if i + 1 < n:
                time.sleep(0.12)

    def _move_windows(self, x, y):
        self._u32().SetCursorPos(int(x), int(y))

    def _type_windows(self, text, interval=0.004):
        u32 = self._u32()
        KEYEVENTF_UNICODE, KEYEVENTF_KEYUP = 0x0004, 0x0002
        for ch in text:
            u32.keybd_event(0, ord(ch), KEYEVENTF_UNICODE, 0)
            u32.keybd_event(0, ord(ch), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0)
            time.sleep(interval)

    def _clipboard_set_windows(self, text) -> bool:
        """CF_UNICODETEXT via ctypes. Returns False if the clipboard would not open (another app holds it).

        Every argument and return type is declared. Without that, ctypes assumes a 32-bit int for a foreign
        function's return value, and GlobalAlloc's 64-bit HANDLE comes back truncated — GlobalLock then reads
        a bogus pointer and the write faults. That is not theory: it crashed on the first live run with
        "access violation writing 0x0000000000000020" (2026-10-16, CarloPC).
        """
        import ctypes
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
        k32.GlobalAlloc.restype, k32.GlobalAlloc.argtypes = ctypes.c_void_p, [ctypes.c_uint, ctypes.c_size_t]
        k32.GlobalLock.restype, k32.GlobalLock.argtypes = ctypes.c_void_p, [ctypes.c_void_p]
        k32.GlobalUnlock.argtypes, k32.GlobalFree.argtypes = [ctypes.c_void_p], [ctypes.c_void_p]
        u32.OpenClipboard.restype, u32.OpenClipboard.argtypes = ctypes.c_bool, [ctypes.c_void_p]
        u32.EmptyClipboard.restype = ctypes.c_bool
        u32.SetClipboardData.restype, u32.SetClipboardData.argtypes = ctypes.c_void_p, [ctypes.c_uint,
                                                                                         ctypes.c_void_p]
        u32.CloseClipboard.restype = ctypes.c_bool
        for _ in range(5):
            if u32.OpenClipboard(None):
                break
            time.sleep(0.05)
        else:
            return False
        try:
            u32.EmptyClipboard()
            buf = ctypes.create_unicode_buffer(text)
            size = ctypes.sizeof(buf)
            h = k32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not h:
                return False
            ptr = k32.GlobalLock(h)
            if not ptr:
                k32.GlobalFree(h)               # never leak the block when the lock fails
                return False
            ctypes.memmove(ptr, buf, size)
            k32.GlobalUnlock(h)
            if u32.SetClipboardData(CF_UNICODETEXT, h):
                return True                     # ownership passed to the clipboard; do NOT free it
            k32.GlobalFree(h)
            return False
        finally:
            u32.CloseClipboard()

    # -- dispatch --
    def click(self, x, y, button="left", clicks=1, double=False):
        if self.dry_run:
            self.did.append(("click", x, y, button))
            return
        if SYSTEM == "Windows":
            self._click_windows(x, y, button, clicks, double)
        else:                                  # Linux/macOS: only for local testing of the protocol
            raise RuntimeError(f"click is implemented for Windows in this build (running on {SYSTEM}) — "
                               f"this server is meant to run on the PC")

    def move(self, x, y, seconds=0.0):
        """`seconds` used to be accepted and ignored: the docs promised an eased path, the code teleported.

        A slow ease is genuinely useful (some apps only track a moving pointer, and a drag that starts
        with a teleport can miss the handle), so it is now real — and capped, because `seconds=3600` was
        also accepted and would have held the input lock for an hour.
        """
        secs = float(seconds or 0)
        if secs < 0:
            raise ValueError("seconds must be >= 0")
        secs = min(secs, MAX_EASE_S)
        if self.dry_run:
            self.did.append(("move", x, y, round(secs, 3))); return
        if SYSTEM == "Windows":
            if secs <= 0:
                self._move_windows(x, y); return
            sx, sy = cursor_pos() or [0, 0]
            steps = max(2, int(secs / 0.008))
            for i in range(1, steps + 1):
                self._move_windows(sx + (x - sx) * i / steps, sy + (y - sy) * i / steps)
                time.sleep(secs / steps)
            self._move_windows(x, y)          # land exactly, not one step short

    def type_text(self, text, interval=0.004):
        try:
            interval = float(interval)
        except (TypeError, ValueError):
            raise ValueError(f"type interval must be a number of seconds — got {interval!r}")
        if not (0.0 <= interval <= MAX_INTERVAL_S):
            raise ValueError(f"type interval must be within 0..{MAX_INTERVAL_S}s — got {interval}. "
                             f"A bigger delay is a hang, not a setting.")
        n = len(str(text))
        if n > MAX_TYPE_CHARS:
            raise RuntimeError(f"{n} characters is more than one action should be ({MAX_TYPE_CHARS} max) — "
                               f"each keystroke is injected and verified, so this would hold the keyboard for "
                               f"{n * 0.004:.0f}s. Use `paste`, or send it in pieces.")
        if self.dry_run:
            self.did.append(("type", text)); return
        if SYSTEM == "Windows":
            self._type_windows(text, interval)

    def paste(self, text):
        """Clipboard + Ctrl+V — the fast, drop-free path (qcaptcha's default delivery mode).

        Returns True/False for "did the clipboard take it"; the ROUTE turns False into a 503, because
        a 200 with `pasted: false` inside it is precisely the ok-shaped lie this whole file exists to kill.
        """
        n = len(str(text))
        if n > MAX_PASTE_CHARS:
            raise RuntimeError(f"paste of {n} chars exceeds {MAX_PASTE_CHARS} — send it in pieces")
        if self.dry_run:
            self.did.append(("paste", text)); return False
        if SYSTEM != "Windows":
            raise RuntimeError("paste is Windows-only in this build")
        ok = self._clipboard_set_windows(text)
        if not ok:
            return False
        u32 = self._u32()
        VK_CONTROL, VK_V, KEYEVENTF_KEYUP = 0x11, 0x56, 0x0002
        time.sleep(0.03)                      # let the clipboard settle before the keystroke arrives
        u32.keybd_event(VK_CONTROL, 0, 0, 0)
        u32.keybd_event(VK_V, 0, 0, 0)
        u32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
        u32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        return True

    KEYS = {"enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "backspace": 0x08,
            "delete": 0x2E, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
            "pageup": 0x21, "pagedown": 0x22, "ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
            "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
            "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B}

    def key(self, key, times=1):
        name = str(key).lower().strip()
        codes = None
        # ---- validation first: a refusal or a typo is a property of the contract, so it must be raised
        # ---- whether or not this process is allowed to touch the keyboard.
        if name in ("ctrl+alt+del", "cad"):
            raise RuntimeError("ctrl+alt+del cannot be synthesized by user code — Windows reserves it "
                               "(and it must never be an automation verb); use Task Manager instead")
        if "+" in name:
            raw = [p.strip() for p in name.split("+")]
            if any(not p for p in raw):
                # "ctrl++" silently dropped the empty piece and pressed ctrl; "ctrl+" pressed ctrl. A
                # half-typed chord that does SOMETHING ELSE is worse than one that is refused.
                raise RuntimeError(f"chord {key!r} has an empty key between '+' — write it as "
                                   f"ctrl+shift+n, with no blank parts")
            parts = raw
            bad = [p for p in parts if p not in self.KEYS and not (len(p) == 1 and p.isalnum())]
            if bad:
                raise RuntimeError(f"unknown key in chord {name!r}: {bad} (known: {sorted(self.KEYS)}, or a "
                                   f"single letter/digit, e.g. ctrl+t, ctrl+shift+n)")
            codes = [self.KEYS[p] if p in self.KEYS else ord(p.upper()) for p in parts]
        elif name in self.KEYS:
            codes = [self.KEYS[name]]
        elif len(name) == 1:
            # A single character goes through the Unicode key path, NOT a raw VK code: `keybd_event(VK_OEM…)`
            # without shift produces nothing, or the wrong thing, for everything outside A-Z/0-9 — and the
            # old code accepted `-`, `/`, `.` and replied ok. KEYEVENTF_UNICODE is what `type` already uses.
            codes = None
        else:
            raise RuntimeError(f"unknown key {key!r} — use a single character, one of {sorted(self.KEYS)}, "
                               f"or a chord like ctrl+s")
        try:
            times = int(times if times is not None else 1)
        except (TypeError, ValueError):
            raise ValueError(f"key times must be an integer — got {times!r}")
        except OverflowError:
            raise ValueError(f"key times is not a number of presses: {times!r}")
        if times != float(times if times is not None else 1):
            raise ValueError(f"key times must be a whole number of presses — got {times!r}")
        if times > MAX_KEY_TIMES:
            raise RuntimeError(f"key times={times} exceeds {MAX_KEY_TIMES} — that would hold the "
                               f"keyboard for {times * 0.03:.0f}s of nothing useful")
        if self.dry_run:
            self.did.append(("key", name, times)); return
        if SYSTEM != "Windows":
            raise RuntimeError("key is Windows-only in this build")
        u32 = self._u32()
        if codes is None:                     # single character: unicode path, shift-free and exact
            self._type_windows(name)
            return
        for _ in range(max(1, int(times))):
            for c in codes:
                u32.keybd_event(c, 0, 0, 0)
            for c in reversed(codes):
                u32.keybd_event(c, 0, 0x0002, 0)
            time.sleep(0.03)

    def drag(self, x, y, x2, y2, seconds=0.4, button="left"):
        if self.dry_run:
            self.did.append(("drag", x, y, x2, y2)); return
        if SYSTEM != "Windows":
            raise RuntimeError("drag is Windows-only in this build")
        u32 = self._u32()
        down = 0x0002 if button == "left" else 0x0008
        up = 0x0004 if button == "left" else 0x0010
        u32.SetCursorPos(int(x), int(y)); time.sleep(0.08)
        u32.mouse_event(down, 0, 0, 0, 0)
        steps = 18
        for i in range(1, steps):            # up to, not including: the last step is an exact landing
            u32.SetCursorPos(int(x + (x2 - x) * i / steps), int(y + (y2 - y) * i / steps))
            time.sleep(max(0.01, seconds / steps))
        u32.SetCursorPos(int(x2), int(y2))   # with 18 steps and int() truncation the old loop let go ~1px short
        time.sleep(0.02)
        u32.mouse_event(up, 0, 0, 0, 0)

    def scroll(self, clicks=-3, x=None, y=None):
        try:
            clicks = int(clicks)
        except (TypeError, ValueError):
            raise ValueError(f"scroll clicks must be an integer — got {clicks!r}")
        except OverflowError:
            raise ValueError(f"scroll clicks is not a number of clicks: {clicks!r}")
        if abs(clicks) > MAX_SCROLL_CLICKS:
            raise ValueError(f"scroll clicks must be within ±{MAX_SCROLL_CLICKS} — got {clicks}. "
                             f"The wheel delta goes to Windows as clicks*120; ask for a page at a time.")
        if self.dry_run:
            self.did.append(("scroll", clicks)); return
        if SYSTEM != "Windows":
            raise RuntimeError("scroll is Windows-only in this build")
        u32 = self._u32()
        if x is not None and y is not None:
            u32.SetCursorPos(int(x), int(y)); time.sleep(0.05)
        u32.mouse_event(0x0800, 0, 0, clicks * 120, 0)            # MOUSEEVENTF_WHEEL


# ------------------------------------------------------------------- state ---
def cursor_pos():
    if SYSTEM != "Windows":
        return None
    try:
        import ctypes

        class P(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        p = P()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
        return [p.x, p.y]
    except Exception:
        return None                                      # cursor_pos() is read on every reply: never throw


# ------------------------------------------------------------------ server ---
class Handler(BaseHTTPRequestHandler):
    server_version = f"pc-eye/{VERSION}"
    screen: Screen = None
    inp: Input = None
    token: str = ""
    log_path: str = "pceye.log"
    dry_run: bool = False                 # visible in every reply: a fake must never look real
    # seams, not shortcuts: tests patch these two and exercise the real routes on Linux
    input_space = staticmethod(input_space)
    display_rect = staticmethod(display_rect)
    limits: list = None                   # [RateBucket for /act, RateBucket for /shot]
    _log_lock = threading.Lock()
    _log_fh = None
    _log_at = 0.0

    # -- helpers --
    def _guard(self, cost=1.0):
        """Refuse to read a body we have no intention of parsing, and reject a cross-origin form POST."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._json(400, {"ok": False, "error": "Content-Length must be a number"})
            return False
        if n > REQ_BODY_MAX:
            self._json(413, {"ok": False, "error": f"body of {n} bytes exceeds {REQ_BODY_MAX} — "
                                                   f"one action cannot need that much"})
            return False
        return True

    def _same_origin(self) -> bool:
        """Two things must be true for an /act to be accepted from a browser-ish client.

        A POST with a CORS-"simple" shape (no custom headers, Content-Type of form/text/plain) is sent by
        a web page without any preflight. That is the one way a page on the human's own browser can aim the
        mouse just because the tunnel URL was pasted somewhere — and a token in a URL outlives the session:
        history, proxy logs, the phone that opened it. So:

          * an explicit Origin that is not this host is refused outright (403, in words);
          * a body that is not `application/json` is refused — which a browser cannot forge without
            triggering a preflight, and which no real agent has to care about;
          * a request with no Origin header at all (curl, python, an HTTP client in a chat) is allowed,
            because the token is what authenticates it.
        """
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._json(415, {"ok": False, "error": f"Content-Type must be application/json — got "
                                                    f"{ctype or 'nothing'!r}. A web page can send anything "
                                                    f"else without asking permission first, so /act does not "
                                                    f"accept it. Real clients set the header."})
            return False
        origin = self.headers.get("Origin")
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if site == "same-origin" and not origin:
            return True
        if origin in (None, "") and not site:
            return True                                  # not a browser: a script, curl, an agent
        if site == "same-origin":
            return True
        host = (self.headers.get("Host") or "").split(":")[0]
        try:
            o = urlparse(origin).netloc.split(":")[0] if origin else ""
        except Exception:
            o = ""
        return bool(o) and o == host

    def _json(self, code, obj):
        body = json.dumps(obj, sort_keys=True, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self._write(body)

    def _write(self, data) -> bool:
        """Send a body, tolerating a peer that vanished mid-write.

        A phone pulling to refresh, a curl `--max-time`, a proxy that gave up: they all close the socket
        while we are still writing, and BaseHTTPRequestHandler lets the BrokenPipeError escape — killing the
        handler thread and printing a traceback into the human's console for something that happens hundreds
        of times a day and means nothing. Swallow exactly that, so a traceback that DOES appear still means a
        bug. (The multi-MB screenshot is where it happens most, which is why this is under _bytes too.)
        """
        try:
            self.wfile.write(data)
            return True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            return False

    def _bytes(self, code, data, mime, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        if data:
            self._write(data)

    def _token_ok(self, q) -> bool:
        given = self.headers.get("X-Token") or (q.get("t") or [""])[0] or \
                (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        return bool(given) and hmac.compare_digest(str(given), self.token)

    def _log(self, line):
        """Console always; file best-effort but LOUD — a run whose log silently vanished is not reviewable.

        The handle is held open (a Windows FILE_APPEND_DATA per request was pure overhead) and reopened
        if the path goes away, so `del pceye.log` mid-run does not quietly end the record.
        """
        print(f"  {z()} {line}", flush=True)
        for _ in range(2):
            try:
                with self._log_lock:
                    if Handler._log_fh is None or (time.time() - Handler._log_at) > 5.0:
                        if Handler._log_fh:
                            try:
                                Handler._log_fh.close()
                            except Exception:
                                pass
                        Handler._log_fh = open(self.log_path, "a", encoding="utf-8", buffering=1)
                        Handler._log_at = time.time()
                    Handler._log_fh.write(f"{z()} {line}\n")
                return
            except Exception as e:
                Handler._log_fh = None
                if not getattr(Handler, "_log_warned", False):
                    Handler._log_warned = True
                    print(f"  !! cannot write {self.log_path} ({type(e).__name__}: {e}) — actions will only be "
                          f"printed here, not recorded", flush=True)
                return

    def log_message(self, fmt, *a):          # keep the console readable: one line per request
        pass

    def finish(self):
        """A phone pulling-to-refresh, a curl timeout, a proxy giving up: all of them close the socket
        mid-write. BaseHTTPRequestHandler lets the BrokenPipeError escape, which kills the handler thread
        and prints a traceback into the human's console for a non-event that will happen hundreds of times.
        """
        try:
            super().finish()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            try:
                self.connection.close()
            except Exception:
                pass
        except OSError:
            pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
        except Exception:
            raise ValueError("body must be a JSON object, e.g. {\"action\":\"click\",\"x\":10,\"y\":10}")
        if not isinstance(body, dict):
            # a bare list/string/number used to reach `body.get` and kill the handler: the agent saw
            # "connection closed", which reads exactly like "the PC is down".
            raise ValueError(f"body must be a JSON object, not {type(body).__name__}")
        return body

    # -- routes --
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._bytes(200, (f"pc-eye {VERSION} is running on {platform.node()} ({SYSTEM}).\n"
                                     f"Agent endpoints need the token: /state /shot /act /selftest\n"
                                     f"Uptime {int(time.time() - START)}s. Stop: close this window.\n").encode(),
                               "text/plain; charset=utf-8")
        if u.path == "/favicon.ico":
            return self._bytes(204, b"", "image/x-icon")
        if not self._token_ok(q):
            return self._json(401, {"ok": False, "error": "bad or missing token",
                                    "hint": "add ?t=TOKEN, or the X-Token header"})
        if u.path == "/state":
            # The coordinate-space verdict used to cost a whole screen capture (Screen.size() grabbed
            # pixels to read .width). display_rect() asks the OS instead, and /state has never been
            # the route an agent can afford to poll — so now it does no capture at all.
            rect = self.display_rect()
            sz = (rect[2], rect[3]) if rect else self.screen.size()
            space = coord_space_report(sz, self.input_space())
            return self._json(200, {"ok": True, "version": VERSION, "host": platform.node(), "os": SYSTEM,
                                    "screen": list(sz) if sz else None, "capture_ok": self.screen.available,
                                    "capture_why": self.screen.why or None, "cursor": cursor_pos(),
                                    "uptime_s": int(time.time() - START), "last": LAST,
                                    "dpi": getattr(Handler, "dpi", {}),
                                    "dry_run": bool(self.dry_run),
                                    "coord_space": space, "at": z()})
        if u.path == "/shot":
            if not self._bucket(1):
                self.send_header("Retry-After", "2") if False else None
                return self._json(429, {"ok": False,
                                        "error": f"more than {SHOT_RATE_PER_MIN} shots/min — you are polling "
                                                 f"faster than a human can act. Watching something load? Use "
                                                 f"/shot?changed=HASH and pay a header, not a frame.",
                                        "retry_after_s": 2})
            try:
                region = (q.get("region") or [None])[0]
                fmt = (q.get("fmt") or ["jpeg"])[0]
                if fmt not in ("jpeg", "png"):
                    raise ValueError(f"fmt must be jpeg or png — got {fmt!r}")
                maxw = num("max", (q.get("max") or [None])[0], lo=0, hi=7680)
                qual = num("quality", (q.get("quality") or [None])[0], lo=1, hi=95)
                opt = {"1": True, "0": False, "yes": True, "no": False,
                       "": None}.get((q.get("opt") or [""])[0].lower(), None)
                if opt is None and (q.get("opt") or [None])[0] not in (None, ""):
                    raise ValueError(f"opt must be 1 or 0 — got {(q.get('opt') or [''])[0]!r}")
                want = (q.get("changed") or [None])[0]
            except ValueError as e:                 # a bad argument: the agent's mistake, not the PC's
                return self._json(400, {"ok": False, "error": str(e)})
            try:
                data, meta = self.screen.grab(region=region, max_width=maxw, fmt=fmt, quality=qual,
                                              optimize=opt)
            except ValueError as e:                 # e.g. a region off the edge of the screen
                return self._json(400, {"ok": False, "error": str(e)})
            except RuntimeError as e:               # the machine could not do it: a different problem
                return self._json(503, {"ok": False, "error": str(e)})
            except OSError as e:                    # Pillow rejects a bad save() argument this way
                return self._json(400, {"ok": False, "error": f"bad image settings: {type(e).__name__}: {e}"})
            # The agent needs both sizes to convert what it sees into where it clicks. Sent as headers so
            # the arithmetic never depends on a second request landing between the shot and the click.
            hdr = {"X-Screen-Full": f"{meta['full'][0]}x{meta['full'][1]}",
                   "X-Screen-Sent": f"{meta['sent'][0]}x{meta['sent'][1]}",
                   "X-Screen-Hash": meta.get("hash") or content_hash(data)}
            if want:                                # watching a build: pay a header, not a frame
                if hmac.compare_digest(str(want), hdr["X-Screen-Hash"]):
                    self._log(f"shot unchanged ({hdr['X-Screen-Hash'][:8]})")
                    return self._bytes(304, b"", "text/plain", hdr)
            self._log(f"shot {meta['sent']} {meta['bytes']}B scaled={meta['scaled']} "
                      f"opt={int(meta.get('optimize', False))}")
            return self._bytes(200, data, meta["mime"], hdr)
        if u.path == "/selftest":
            out = {"ok": True, "version": VERSION, "dry_run": bool(self.dry_run), "checks": []}
            def chk(name, ok, detail=""):
                out["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:200]})
                out["ok"] = out["ok"] and bool(ok)
            if self.dry_run:
                out["checks"].append({"name": "TEST-MODE", "ok": True,
                                      "detail": "dry-run: NOTHING is sent to the mouse or keyboard. "
                                                "Every ok in this build is a protocol pass, not a PC."})
            try:
                data, meta = self.screen.grab()
                chk("capture", len(data) > 1000, f"{meta['sent']} {meta['bytes']}B")
            except (RuntimeError, ValueError) as e:
                chk("capture", False, str(e))
            chk("token", len(self.token or "") >= 16,
                f"{len(self.token or '')} chars" if self.token else "NO TOKEN")
            chk("platform", SYSTEM == "Windows", f"os={SYSTEM}" +
                ("" if SYSTEM == "Windows" else " — clicks and keys are Windows-only in this build"))
            try:
                rect = self.display_rect()
                sz = (rect[2], rect[3]) if rect else self.screen.size()
                space = coord_space_report(sz, self.input_space())
                chk("coordinate-space", space["consistent"] is not False,
                    space["warning"] or f"eyes and hand agree ({sz[0]}x{sz[1]})")
                chk("dpi-aware", bool(getattr(Handler, "dpi", {}).get("dpi_aware")),
                    getattr(Handler, "dpi", {}).get("method") or
                    getattr(Handler, "dpi", {}).get("error") or "not reported")
            except Exception as e:
                chk("coordinate-space", False, f"could not determine: {type(e).__name__}: {e}")
            # the old check asked os.access() about a path whose parent did not exist and called it
            # writable. The only honest test of a log is the write.
            try:
                self._log("selftest log-write probe")
                chk("log-writable", os.path.exists(self.log_path), self.log_path)
            except Exception as e:
                chk("log-writable", False, f"{self.log_path}: {type(e).__name__}: {e}")
            return self._json(200, out)
        return self._json(404, {"ok": False, "error": f"no route {u.path}",
                                "routes": ["/state", "/shot", "/act", "/selftest"]})

    @classmethod
    def _close_log(cls):
        """Release the held log handle. Tests and shutdown must be able to prove they leave nothing open."""
        with cls._log_lock:
            if cls._log_fh:
                try:
                    cls._log_fh.close()
                except Exception:
                    pass
            cls._log_fh = None

    def _bucket(self, idx, cost=1.0):
        try:
            if not self.limits:
                return True
            return self.limits[idx].take(cost)
        except Exception:
            return True                             # a limiter that breaks must not break the PC's owner

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path != "/act":
            return self._json(404, {"ok": False, "error": f"no route {u.path}"})
        if not self._same_origin():
            return self._json(403, {"ok": False, "error": "cross-origin POST is refused — a browser on this "
                                                           "PC must not be able to drive it from a web page"})
        if not self._token_ok(q):
            return self._json(401, {"ok": False, "error": "bad or missing token",
                                    "hint": "add ?t=TOKEN, or the X-Token header"})
        if not self._guard():
            return
        if not self._bucket(0):
            return self._json(429, {"ok": False,
                                    "error": f"more than {ACT_RATE_PER_MIN} actions/min — a loop is spinning "
                                             f"on the human's keyboard. Stop, take one /shot, and think "
                                             f"before the next action.",
                                    "retry_after_s": 5})
        try:
            body = self._body()
        except ValueError as e:
            return self._json(400, {"ok": False, "error": str(e)})
        action = str(body.get("action") or "")
        if action not in ACTIONS:
            return self._json(400, {"ok": False, "error": f"unknown action {action!r}",
                                    "actions": sorted(ACTIONS)})
        req, opt = ACTIONS[action]
        missing = [k for k in req if body.get(k) in (None, "")]
        extra = [k for k in body if k not in ("action",) + req + opt]
        if missing:
            return self._json(400, {"ok": False, "error": f"{action} needs {missing}"})
        if extra:
            return self._json(400, {"ok": False, "error": f"{action} has no field(s) {extra} — a typo must "
                                                          f"not ride along to the OS"})
        # The optional numbers are parsed at the door, with the same strictness as the coordinates. In 1.0.2
        # `clicks`, `times`, `seconds` and `interval` were forwarded untouched, which is a traceback in a
        # thread instead of an answer — and an unanswered request is what a dead PC looks like.
        try:
            if "clicks" in body:
                v = strict_int("clicks", body["clicks"], lo=-MAX_SCROLL_CLICKS, hi=MAX_SCROLL_CLICKS)
                if v == 0 and action == "scroll":
                    # 1e-999 (a legal JSON float) underflows to 0, and a scroll of zero notches answers
                    # ok:true having moved nothing. That is the ok-shaped lie in a new costume.
                    raise ValueError("scroll clicks must be at least one notch either way — got 0. "
                                     "Omit the field for the default of -3.")
                body["clicks"] = v
            if "times" in body:
                body["times"] = strict_int("times", body["times"], lo=1, hi=MAX_KEY_TIMES)
            if "seconds" in body:
                body["seconds"] = strict_int("seconds", body["seconds"], lo=0, hi=MAX_EASE_S, whole=False)
            if "interval" in body:
                body["interval"] = strict_int("interval", body["interval"], lo=0, hi=MAX_INTERVAL_S,
                                              whole=False)
        except ValueError as e:
            return self._json(400, {"ok": False, "action": action, "error": str(e)})
        # Numbers are parsed HERE, once, so a `x: "banana"` is a 400 that names the field instead of an
        # AttributeError inside ctypes that the agent reads as "the PC is broken".
        try:
            coords = {k: num(k, body.get(k), integer=True) for k in ("x", "y", "x2", "y2") if k in body}
        except ValueError as e:
            return self._json(400, {"ok": False, "action": action, "error": str(e) + " — an integer pixel value"})
        for k, v in list(coords.items()):
            if v is None:
                return self._json(400, {"ok": False, "action": action,
                                        "error": f"{k} must be an integer pixel value — got {body[k]!r}"})
            coords[k] = v
        if coords:
            body.update(coords)
        # The clamp bug and the DPI bug are the SAME bug seen twice: a coordinate the hand cannot reach
        # lands somewhere else, and a split coordinate space means every coordinate is that way. Both are
        # refused in words before anything touches the OS. input_space() is the primary monitor (that is
        # what SetCursorPos clamps against); display_rect() is the whole virtual desktop, which is what
        # /shot returns — so a second monitor's negative coordinates are described, not refused.
        space = self.input_space()
        rect = self.display_rect()
        cap = (rect[2], rect[3]) if rect else self.screen.size()
        if space:
            for kx, ky in (("x", "y"), ("x2", "y2")):
                if body.get(kx) is None or body.get(ky) is None:
                    continue
                px, py = coords.get(kx, body[kx]), coords.get(ky, body[ky])
                if not (0 <= px < space[0] and 0 <= py < space[1]):
                    return self._json(409, {"ok": False, "action": action,
                                            "error": f"({px},{py}) is outside the coordinate space the input "
                                                     f"calls live in ({space[0]}x{space[1]}) — a clamped click "
                                                     f"would silently land somewhere else. This is what "
                                                     f"/state -> coord_space reports on.",
                                            "input_space": space})
        gate = coord_gate(action, body, cap, space)
        if gate:
            code, payload = gate
            self._log(f"REFUSED {action}: {payload['error'][:120]}")
            return self._json(code, payload)
        args = {k: v for k, v in body.items() if k != "action"}
        try:
            with LOCK:
                if action == "paste":
                    if not self.inp.paste(args["text"]):
                        raise RuntimeError("the clipboard would not open (another app is holding it) — "
                                          "the text was NOT pasted")
                    result = {"pasted": True}
                else:
                    result = getattr(self.inp, METHOD[action])(**args)
                LAST.update(action=action, at=z(), dry_run=bool(self.dry_run),
                            args={k: (v if k != "text" else f"{len(str(v))} chars") for k, v in args.items()})
        except RuntimeError as e:
            self._log(f"REFUSED {action}: {e}")
            return self._json(409, {"ok": False, "action": action, "error": str(e), "dry_run": bool(self.dry_run)})
        except ValueError as e:                      # a bad argument that the method itself caught
            return self._json(400, {"ok": False, "action": action, "error": str(e)})
        except TypeError as e:
            return self._json(400, {"ok": False, "action": action, "error": f"bad arguments: {e}"})
        except Exception as e:
            self._log(f"ERROR {action}: {type(e).__name__}: {e}")
            return self._json(500, {"ok": False, "action": action, "error": f"{type(e).__name__}: {e}"})
        args_show = {k: (f"{len(str(v))} chars" if k == "text" else v) for k, v in args.items()}
        out = {"ok": True, "action": action, "did": args_show, "cursor": cursor_pos(), "at": z(),
               "dry_run": bool(self.dry_run)}
        if isinstance(result, dict):
            out.update(result)
        if self.dry_run:
            out["note"] = "dry-run: this action was recorded, NOT delivered to Windows"
        if action in POINTING_ACTIONS and body.get("x") is not None and body.get("y") is not None:
            landed = out["cursor"]
            if landed:                      # None in dry-run / non-Windows: then we simply do not claim it
                off = (landed[0] - int(body["x"]), landed[1] - int(body["y"]))
                out["landed"], out["off_by"] = landed, list(off)
                out["exact"] = abs(off[0]) <= COORD_TOL and abs(off[1]) <= COORD_TOL
                if not out["exact"]:
                    out["warning"] = (f"pointer is at {landed}, not {(body['x'], body['y'])} — the click went to "
                                      f"where the pointer actually is. Treat this action as NOT having done "
                                      f"what you asked, and check /state -> coord_space.")
                    self._log(f"act {action} MISMATCH asked=({body['x']},{body['y']}) landed={landed}")
        self._log(f"act {action} {json.dumps(args_show, sort_keys=True)} -> "
                  f"{'DRY-RUN' if self.dry_run else 'ok'}{'' if out.get('exact', True) else ' (mismatch)'}")
        return self._json(200, out)


def configure(token, max_width=1440, log_path="pceye.log", dry_run=False, optimize=False):
    """Install the run's configuration on the handler. Separate from the socket so the launcher can bind
    a port FIRST (and say which one is busy) without a race between 'is it free' and 'I want it'."""
    Handler.dpi = set_dpi_awareness()          # before anything captures or clicks
    Handler.screen = Screen(max_width=max_width, optimize=optimize)
    Handler.inp = Input(dry_run=dry_run)
    Handler.dry_run = bool(dry_run)
    Handler.limits = [RateBucket(BURST_ACT, ACT_RATE_PER_MIN / 60.0),
                      RateBucket(BURST_SHOT, SHOT_RATE_PER_MIN / 60.0)]
    Handler.token = token
    Handler.log_path = log_path
    return Handler


def build(port: int, token: str, host: str = "127.0.0.1", max_width: int = 1440,
          log_path: str = "pceye.log", dry_run: bool = False, optimize: bool = False, sock=None):
    cls = configure(token, max_width, log_path, dry_run, optimize)
    if sock is not None:                       # pre-bound: ThreadingHTTPServer will not bind again
        srv = ThreadingHTTPServer((host, port), cls, bind_and_activate=False)
        srv.socket = sock
        srv.server_activate()
        return srv
    return ThreadingHTTPServer((host, port), cls)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="server.py", description="pc-eye: HTTP view/control of this PC")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--token", default=None)
    ap.add_argument("--max-width", type=int, default=1440)
    ap.add_argument("--log", default="pceye.log")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="never touch the mouse or keyboard (tests)")
    ap.add_argument("--optimize", action="store_true",
                    help="smaller JPEGs (~5%%) for a slower encode (~25%%); off by default — the agent "
                         "pays the milliseconds 200 times a session, nobody reads the bytes")
    ap.add_argument("--version", action="version", version=f"pc-eye {VERSION}")
    a = ap.parse_args(argv)
    token = a.token or secrets.token_hex(16)
    if len(token) < 16 and not a.dry_run:
        print(f"  !! token is {len(token)} chars. This machine is reachable from the internet through the "
              f"tunnel and the token is the ONLY thing between a stranger and your mouse. "
              f"Drop --token and let it generate 32.", flush=True)
    if a.selftest:
        # Deliberately BEFORE the socket exists: a self-test is a question about this machine, not a server
        # to be reached. Binding first meant `python server.py --selftest` could die with "address already
        # in use" — on a healthy PC, and on a CI runner whose port range Hyper-V has reserved — which reads
        # to an agent as "the eye is broken" when nothing is.
        configure(token, a.max_width, a.log, a.dry_run, a.optimize)   # the screen/dpi objects live here
        s = Handler.screen
        try:
            data, meta = s.grab()
            print(json.dumps({"ok": True, "capture": meta, "token_set": bool(token),
                              "dry_run": bool(a.dry_run)}, indent=2))
        except (RuntimeError, ValueError, OSError) as e:
            # Any of those is "this machine could not do it", and the answer must still be one JSON object:
            # a traceback from --selftest is the least useful output this tool can produce.
            print(json.dumps({"ok": False, "capture_error": f"{type(e).__name__}: {e}",
                              "token_set": bool(token), "dry_run": bool(a.dry_run)}, indent=2))
            return 1
        return 0
    srv = build(a.port, token, a.host, a.max_width, a.log, a.dry_run, optimize=a.optimize)
    print(f"pc-eye {VERSION} on {platform.node()} ({SYSTEM})", flush=True)
    if a.dry_run:
        print("  *** DRY-RUN: nothing reaches the mouse or keyboard. The agent must be told. ***", flush=True)
    print(f"  local:  http://{a.host}:{a.port}/  (token: {token})", flush=True)
    print(f"  token:  {len(token)} chars · give the URL+token to the agent only, never to a wire", flush=True)
    print(f"  log:    {os.path.abspath(a.log)}", flush=True)
    print(f"  stop:   Ctrl+C here, or close this window", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("stopped by the human at the keyboard", flush=True)
    finally:
        Handler._close_log()
    return 0


if __name__ == "__main__":
    sys.exit(main())
