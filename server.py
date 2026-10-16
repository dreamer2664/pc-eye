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
import base64
import datetime as dt
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

VERSION = "1.0.0"
SYSTEM = platform.system()
DEFAULT_PORT = 8790
# action name -> the method on Input that performs it. Kept beside the contract so a new action cannot
# exist in the schema and be missing from the implementation (a test asserts the two match).
METHOD = {"click": "click", "move": "move", "type": "type_text", "key": "key",
          "drag": "drag", "scroll": "scroll", "paste": "paste"}
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
    """The rectangle the input calls actually live in, in this process's own coordinates."""
    if SYSTEM != "Windows":
        return None
    import ctypes
    u32 = ctypes.windll.user32
    return [u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)]


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
        x, y, w, h = (int(p) for p in parts)
    except (TypeError, ValueError):
        raise ValueError(f"region must be four integers x,y,w,h — got {spec!r}")
    if w <= 0 or h <= 0:
        raise ValueError(f"region width and height must be positive — got {w}x{h}")
    return (x, y, w, h)


def z() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ screen ---
class Screen:
    """Capture, downscaled to something a chat agent can actually read and a network can carry.

    A full 2560x1440 PNG is ~2-4 MB; a 1440-wide JPEG at q=72 is ~150-300 KB and every detail an agent needs
    for "where do I click" survives it. Full resolution is available on request with `?max=0`.
    """

    def __init__(self, max_width: int = 1440, quality: int = 72):
        self.max_width, self.quality = max_width, quality
        self.available = True
        self.why = ""

    def size(self):
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab(all_screens=True)
            return img.width, img.height
        except Exception as e:
            self.available, self.why = False, f"{type(e).__name__}: {e}"
            return None

    def grab(self, region=None, max_width=None, fmt="jpeg", quality=None):
        """Returns (bytes, dict) or raises RuntimeError with a sentence a human can act on."""
        try:
            from PIL import ImageGrab
        except Exception as e:
            raise RuntimeError(f"Pillow is not installed ({e}) — run: py -3 -m pip install pillow")
        box = None
        r = parse_region(region)            # ValueError -> the route turns it into a 400 the agent can read
        if r:
            box = (r[0], r[1], r[0] + r[2], r[1] + r[3])
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
        if fmt == "png":
            img.save(buf, "PNG", optimize=True)
            mime = "image/png"
        else:
            q = self.quality if quality is None else int(quality)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.save(buf, "JPEG", quality=q, optimize=True)
            mime = "image/jpeg"
        data = buf.getvalue()
        meta = {"full": list(full), "sent": [img.width, img.height], "scaled": scaled,
                "bytes": len(data), "mime": mime, "format": fmt, "at": z()}
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
        if self.dry_run:
            self.did.append(("move", x, y)); return
        if SYSTEM == "Windows":
            self._move_windows(x, y)

    def type_text(self, text, interval=0.004):
        if self.dry_run:
            self.did.append(("type", text)); return
        if SYSTEM == "Windows":
            self._type_windows(text, interval)

    def paste(self, text):
        """Clipboard + Ctrl+V — the fast, drop-free path (qcaptcha's default delivery mode)."""
        if self.dry_run:
            self.did.append(("paste", text)); return False
        if SYSTEM != "Windows":
            raise RuntimeError("paste is Windows-only in this build")
        ok = self._clipboard_set_windows(text)
        if not ok:
            return False
        u32 = self._u32()
        VK_CONTROL, VK_V, KEYEVENTF_KEYUP = 0x11, 0x56, 0x0002
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
            parts = [p.strip() for p in name.split("+") if p.strip()]
            bad = [p for p in parts if p not in self.KEYS and not (len(p) == 1 and p.isalnum())]
            if bad:
                raise RuntimeError(f"unknown key in chord {name!r}: {bad} (known: {sorted(self.KEYS)}, or a "
                                   f"single letter/digit, e.g. ctrl+t, ctrl+shift+n)")
            codes = [self.KEYS[p] if p in self.KEYS else ord(p.upper()) for p in parts]
        elif name in self.KEYS:
            codes = [self.KEYS[name]]
        elif len(name) == 1 and name.isalnum():
            codes = [ord(name.upper())]        # VK codes for A-Z/0-9 are their ASCII values
        elif len(name) != 1:
            raise RuntimeError(f"unknown key {key!r} — use a single character, one of {sorted(self.KEYS)}, "
                               f"or a chord like ctrl+s")
        if self.dry_run:
            self.did.append(("key", name, times)); return
        if SYSTEM != "Windows":
            raise RuntimeError("key is Windows-only in this build")
        u32 = self._u32()
        if len(name) == 1 and codes is None:
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
        for i in range(1, steps + 1):
            u32.SetCursorPos(int(x + (x2 - x) * i / steps), int(y + (y2 - y) * i / steps))
            time.sleep(max(0.01, seconds / steps))
        u32.mouse_event(up, 0, 0, 0, 0)

    def scroll(self, clicks=-3, x=None, y=None):
        if self.dry_run:
            self.did.append(("scroll", clicks)); return
        if SYSTEM != "Windows":
            raise RuntimeError("scroll is Windows-only in this build")
        u32 = self._u32()
        if x is not None and y is not None:
            u32.SetCursorPos(int(x), int(y)); time.sleep(0.05)
        u32.mouse_event(0x0800, 0, 0, int(clicks) * 120, 0)      # MOUSEEVENTF_WHEEL


# ------------------------------------------------------------------- state ---
def cursor_pos():
    if SYSTEM != "Windows":
        return None
    import ctypes
    class P(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    p = P()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return [p.x, p.y]


# ------------------------------------------------------------------ server ---
class Handler(BaseHTTPRequestHandler):
    server_version = f"pc-eye/{VERSION}"
    screen: Screen = None
    inp: Input = None
    token: str = ""
    log_path: str = "pceye.log"

    # -- helpers --
    def _json(self, code, obj):
        body = json.dumps(obj, sort_keys=True, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, code, data, mime, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(data)

    def _token_ok(self, q) -> bool:
        given = self.headers.get("X-Token") or (q.get("t") or [""])[0] or \
                (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        return bool(given) and hmac.compare_digest(str(given), self.token)

    def _log(self, line):
        """Console always; file best-effort but LOUD — a run whose log silently vanished is not reviewable."""
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"{z()} {line}\n")
        except Exception as e:
            if not getattr(Handler, "_log_warned", False):
                Handler._log_warned = True
                print(f"  !! cannot write {self.log_path} ({type(e).__name__}: {e}) — actions will only be "
                      f"printed here, not recorded", flush=True)
        print(f"  {z()} {line}", flush=True)

    def log_message(self, fmt, *a):          # keep the console readable: one line per request
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode() or "{}")
        except Exception:
            raise ValueError("body must be JSON")

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
            sz = self.screen.size()
            space = coord_space_report(sz, input_space())
            return self._json(200, {"ok": True, "version": VERSION, "host": platform.node(), "os": SYSTEM,
                                    "screen": list(sz) if sz else None, "capture_ok": self.screen.available,
                                    "capture_why": self.screen.why or None, "cursor": cursor_pos(),
                                    "uptime_s": int(time.time() - START), "last": LAST,
                                    "dpi": getattr(Handler, "dpi", {}),
                                    "coord_space": space, "at": z()})
        if u.path == "/shot":
            region = (q.get("region") or [None])[0]
            fmt = (q.get("fmt") or ["jpeg"])[0]
            maxw = q.get("max") or [None]
            maxw = int(maxw[0]) if maxw and maxw[0] not in (None, "") else None
            try:
                data, meta = self.screen.grab(region=region, max_width=maxw, fmt=fmt)
            except ValueError as e:                 # a bad argument: the agent's mistake, not the PC's
                return self._json(400, {"ok": False, "error": str(e)})
            except RuntimeError as e:               # the machine could not do it: a different problem
                return self._json(503, {"ok": False, "error": str(e)})
            self._log(f"shot {meta['sent']} {meta['bytes']}B scaled={meta['scaled']}")
            # The agent needs both sizes to convert what it sees into where it clicks. Sent as headers so
            # the arithmetic never depends on a second request landing between the shot and the click.
            return self._bytes(200, data, meta["mime"], {
                "X-Screen-Full": f"{meta['full'][0]}x{meta['full'][1]}",
                "X-Screen-Sent": f"{meta['sent'][0]}x{meta['sent'][1]}"})
        if u.path == "/selftest":
            out = {"ok": True, "version": VERSION, "checks": []}
            def chk(name, ok, detail=""):
                out["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:200]})
                out["ok"] = out["ok"] and bool(ok)
            try:
                data, meta = self.screen.grab()
                chk("capture", len(data) > 1000, f"{meta['sent']} {meta['bytes']}B")
            except RuntimeError as e:
                chk("capture", False, str(e))
            chk("token", bool(self.token), "a token was generated" if self.token else "NO TOKEN")
            chk("platform", SYSTEM == "Windows", f"os={SYSTEM}" +
                ("" if SYSTEM == "Windows" else " — clicks and keys are Windows-only in this build"))
            try:
                sz = self.screen.size()
                space = coord_space_report(sz, input_space())
                chk("coordinate-space", space["consistent"] is not False,
                    space["warning"] or f"eyes and hand agree ({sz[0]}x{sz[1]})")
                chk("dpi-aware", bool(getattr(Handler, "dpi", {}).get("dpi_aware")),
                    getattr(Handler, "dpi", {}).get("method") or
                    getattr(Handler, "dpi", {}).get("error") or "not reported")
            except Exception as e:
                chk("coordinate-space", False, f"could not determine: {type(e).__name__}: {e}")
            chk("log-writable", os.access(self.log_path, os.W_OK) or not os.path.exists(self.log_path),
                self.log_path)
            return self._json(200, out)
        return self._json(404, {"ok": False, "error": f"no route {u.path}",
                                "routes": ["/state", "/shot", "/act", "/selftest"]})

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._token_ok(q):
            return self._json(401, {"ok": False, "error": "bad or missing token",
                                    "hint": "add ?t=TOKEN, or the X-Token header"})
        if u.path != "/act":
            return self._json(404, {"ok": False, "error": f"no route {u.path}"})
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
        # A coordinate outside the space the INPUT calls live in would be silently clamped to the edge and
        # land on something else. That is the failure that looks like success, so it is refused in words.
        space = input_space()
        if space:
            for kx, ky in (("x", "y"), ("x2", "y2")):
                if body.get(kx) is None or body.get(ky) is None:
                    continue
                px, py = int(body[kx]), int(body[ky])
                if not (0 <= px < space[0] and 0 <= py < space[1]):
                    return self._json(409, {"ok": False, "action": action,
                                            "error": f"({px},{py}) is outside the coordinate space the input "
                                                     f"calls live in ({space[0]}x{space[1]}) — a clamped click "
                                                     f"would silently land somewhere else. This is what "
                                                     f"/state -> coord_space reports on.",
                                            "input_space": space})
        args = {k: v for k, v in body.items() if k != "action"}
        try:
            with LOCK:
                result = getattr(self.inp, METHOD[action])(**args) if action != "paste" else \
                         {"pasted": self.inp.paste(args["text"])}
                LAST.update(action=action, at=z(), args={k: (v if k != "text" else f"{len(str(v))} chars")
                                                          for k, v in args.items()})
        except RuntimeError as e:
            self._log(f"REFUSED {action}: {e}")
            return self._json(409, {"ok": False, "action": action, "error": str(e)})
        except TypeError as e:
            return self._json(400, {"ok": False, "action": action, "error": f"bad arguments: {e}"})
        except Exception as e:
            self._log(f"ERROR {action}: {type(e).__name__}: {e}")
            return self._json(500, {"ok": False, "action": action, "error": f"{type(e).__name__}: {e}"})
        args_show = {k: (f"{len(str(v))} chars" if k == "text" else v) for k, v in args.items()}
        out = {"ok": True, "action": action, "did": args_show, "cursor": cursor_pos(), "at": z()}
        if action in ("click", "move", "drag") and body.get("x") is not None and body.get("y") is not None:
            landed = out["cursor"]
            if landed:                      # None in dry-run / non-Windows: then we simply do not claim it
                off = (landed[0] - int(body["x"]), landed[1] - int(body["y"]))
                out["landed"], out["off_by"] = landed, list(off)
                out["exact"] = abs(off[0]) <= 2 and abs(off[1]) <= 2
                if not out["exact"]:
                    out["warning"] = (f"pointer is at {landed}, not {(body['x'], body['y'])} — the click went to "
                                      f"where the pointer actually is. Treat this action as NOT having done "
                                      f"what you asked, and check /state -> coord_space.")
                    self._log(f"act {action} MISMATCH asked=({body['x']},{body['y']}) landed={landed}")
        self._log(f"act {action} {json.dumps(args_show, sort_keys=True)} -> ok"
                  f"{'' if out.get('exact', True) else ' (mismatch)'}")
        return self._json(200, out)


def build(port: int, token: str, host: str = "127.0.0.1", max_width: int = 1440,
          log_path: str = "pceye.log", dry_run: bool = False):
    Handler.dpi = set_dpi_awareness()          # before anything captures or clicks
    Handler.screen = Screen(max_width=max_width)
    Handler.inp = Input(dry_run=dry_run)
    Handler.token = token
    Handler.log_path = log_path
    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="server.py", description="pc-eye: HTTP view/control of this PC")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--token", default=None)
    ap.add_argument("--max-width", type=int, default=1440)
    ap.add_argument("--log", default="pceye.log")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="never touch the mouse or keyboard (tests)")
    a = ap.parse_args(argv)
    token = a.token or secrets.token_hex(4)
    srv = build(a.port, token, a.host, a.max_width, a.log, a.dry_run)
    if a.selftest:
        s = Handler.screen
        try:
            data, meta = s.grab()
            print(json.dumps({"ok": True, "capture": meta, "token_set": bool(token)}, indent=2))
        except RuntimeError as e:
            print(json.dumps({"ok": False, "capture_error": str(e), "token_set": bool(token)}, indent=2))
            return 1
        return 0
    print(f"pc-eye {VERSION} on {platform.node()} ({SYSTEM})", flush=True)
    print(f"  local:  http://{a.host}:{a.port}/  (token: {token})", flush=True)
    print(f"  log:    {os.path.abspath(a.log)}", flush=True)
    print(f"  stop:   Ctrl+C here, or close this window", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("stopped by the human at the keyboard", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
