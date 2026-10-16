#!/usr/bin/env python3
"""pc-eye launcher — one double-click on the PC, then the agent can see and drive it.

What it does, in order, and it says which step failed instead of looking like it worked:
  1. a real selftest: can this machine capture the screen? (a failure here is the whole thing)
  2. starts the local server on 127.0.0.1
  3. opens a public tunnel (cloudflared, downloaded once if missing) so an agent in a chat — which has
     internet but no route into your home network — can reach that server
  4. prints ONE block: URL + token, ready to paste into the chat

If step 3 fails you still get the local URL, and the block says plainly that no agent can reach it from
outside. That distinction is the difference between "it's running" and "the agent can use it".
"""
from __future__ import annotations

import argparse
import ctypes
import os
import platform
import re
import shutil
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import server as pceye  # noqa: E402

CLOUDFLARED_URL = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
                   "cloudflared-windows-amd64.exe")
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def say(msg=""):
    print(msg, flush=True)


def keep_awake():
    """Best effort: stop Windows sleeping while the agent works. Harmless if the call is unavailable."""
    try:
        if platform.system() == "Windows":
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000002)
    except Exception:
        pass


def ensure_cloudflared(quiet=False) -> Path | None:
    exe = HERE / ("cloudflared.exe" if platform.system() == "Windows" else "cloudflared")
    if exe.exists():
        return exe
    found = shutil.which("cloudflared")
    if found:
        return Path(found)
    if not quiet:
        say(f"  tunnel: cloudflared not found, downloading once (~40 MB) …")
    try:
        tmp = exe.with_suffix(".part")
        with urllib.request.urlopen(CLOUDFLARED_URL, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        tmp.replace(exe)
        if platform.system() != "Windows":
            exe.chmod(0o755)
        if not quiet:
            say(f"  tunnel: saved {exe.name} ({exe.stat().st_size / 1e6:.1f} MB)")
        return exe
    except Exception as e:
        say(f"  tunnel: could NOT get cloudflared ({type(e).__name__}: {e})")
        return None


def start_tunnel(port: int, exe: Path | None):
    """Returns (url, process) — url is None when the tunnel could not be established, with the reason printed."""
    if exe is None:
        return None, None
    cmd = [str(exe), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", bufsize=1)
    except Exception as e:
        say(f"  tunnel: failed to start ({type(e).__name__}: {e})")
        return None, None
    url, deadline = None, time.time() + 45
    while time.time() < deadline:
        line = p.stdout.readline() if p.stdout else ""
        if not line:
            if p.poll() is not None:
                say("  tunnel: cloudflared exited — see its output above")
                return None, None
            time.sleep(0.1)
            continue
        m = TUNNEL_RE.search(line)
        if m:
            url = m.group(0)
            break
    threading.Thread(target=lambda: [None for _ in p.stdout], daemon=True).start()   # drain the rest
    return url, p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="launcher.py")
    ap.add_argument("--port", type=int, default=pceye.DEFAULT_PORT)
    ap.add_argument("--token", default=None)
    ap.add_argument("--max-width", type=int, default=1440)
    ap.add_argument("--no-tunnel", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    token = a.token or secrets.token_hex(4)
    say()
    say("  pc-eye — a window into this PC for an agent working in a chat")
    say("  " + "-" * 68)

    # 1. capture, verified rather than assumed
    scr = pceye.Screen(max_width=a.max_width)
    try:
        data, meta = scr.grab()
        say(f"  capture: ok — screen {meta['full'][0]}x{meta['full'][1]}, "
            f"sending {meta['sent'][0]}x{meta['sent'][1]} {meta['format'].upper()} {meta['bytes']//1024} KB")
    except RuntimeError as e:
        say(f"  capture: FAILED — {e}")
        say("  Fix that first: py -3 -m pip install pillow   (then run this again)")
        return 2

    # 2. the server
    srv = pceye.build(a.port, token, "127.0.0.1", a.max_width, str(HERE / "pceye.log"), a.dry_run)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    say(f"  server:  http://127.0.0.1:{a.port}/  (bound to this machine only)")
    keep_awake()

    # 3. the tunnel
    url = None
    if not a.no_tunnel:
        exe = ensure_cloudflared()
        url, _ = start_tunnel(a.port, exe)
        say(f"  tunnel:  {url}" if url else "  tunnel:  NOT established — the agent cannot reach this PC "
                                            "from the internet (local use only)")
    say("  " + "-" * 68)
    say()
    if url:
        ready = HERE / "READY.txt"
        try:
            ready.write_text(f"URL: {url}\nTOKEN: {token}\n", encoding="utf-8")
            where = str(ready)
        except Exception as e:
            where = f"(could not write READY.txt: {e})"
        say("  ================  PC-EYE READY — give these two lines to the agent  ================")
        say(f"  URL:   {url}")
        say(f"  TOKEN: {token}")
        say("  ==================================================================================")
        say()
        say(f"  Also saved to: {where}")
        say(f"  YOUR OWN PHONE VIEW (no agent needed): open this in the phone browser:")
        say(f"    {url}/shot?t={token}      <- the screen, refreshed by pulling the page")
        say(f"    {url}/?t={token}          <- health, screen size, uptime")
        say("  The token stays on this PC and in your chat. It is NEVER posted to ntfy or anywhere public.")
    else:
        say("  Local only. The agent in a chat has no route into your network, so it cannot drive this PC")
        say(f"  until a tunnel exists. Local URL for your own testing: http://127.0.0.1:{a.port}/?t={token}")
    say()
    say("  STOP SWITCH: close this window, or press Ctrl+C. Nothing here outlives that, on purpose.")
    say("  Every action the agent takes is printed below and appended to pceye.log.")
    say()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        say("\n  stopped by the human at the keyboard")
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
