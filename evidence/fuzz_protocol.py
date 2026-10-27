#!/usr/bin/env python3
"""fuzz_protocol.py — throw everything at the surface that an agent's typo could produce.

Why a fuzzer and not more hand-written cases: the audit found four separate paths where a bad argument
raised INSIDE the handler, which does not answer 500 — it closes the socket, and a closed socket is what a
dead PC looks like to an agent. That class of bug is exactly what a generator finds and a checklist misses.
The oracle is therefore simple and absolute: **the server must always answer, and always in JSON** (except
304, which must have no body at all). Anything else is a finding.

  python3 evidence/fuzz_protocol.py --requests 4000          # ~20s
  python3 evidence/fuzz_protocol.py --seed 7 --requests 20000

Needs no display, no mouse and no keyboard: everything runs --dry-run with the OS seams faked.
"""
import argparse
import io
import json
import os
import random
import socket
import sys
import re
import tempfile
import threading
import time
import http.client
import urllib.error
import urllib.request
from http.client import RemoteDisconnected
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import server as pceye                                    # noqa: E402
from PIL import Image                                       # noqa: E402


class FakeImage:
    width, height, mode = 1920, 1080, "RGB"

    def resize(self, *a, **k):
        return self

    def convert(self, *a, **k):
        return self

    def crop(self, *a, **k):
        return self

    def save(self, buf, fmt="JPEG", **kw):
        buf.write(b"\xff\xd8" + os.urandom(900) + b"\xff\xd9")


class FakeGrab:
    @staticmethod
    def grab(*a, **k):
        return FakeImage()


FREE_INTS = [-10, -1, 0, 1, 7, 900, 1535, 1536, 1919, 1920, 2000, 10**9]
BAD_VALUES = [None, "", "banana", [], {}, True, False, 3.5, "1e999", "\x00", "\r\n", "9" * 400]
ACTION_FIELDS = {
    "click": (["x", "y"], ["button", "double", "clicks"]),
    "move": (["x", "y"], ["seconds"]),
    "type": (["text"], ["interval"]),
    "key": (["key"], ["times"]),
    "drag": (["x", "y", "x2", "y2"], ["seconds", "button"]),
    "scroll": ([], ["clicks", "x", "y"]),
    "paste": (["text"], []),
}
KEYS = ["enter", "a", "-", "/", "ctrl+s", "ctrl+alt+del", "hyperdrive", "win+r", "", " ", "1",
        "ctrl+", "+ctrl", "ctrl+alt+del+shift", "f13", "caps_lock", "shift+a", "CTRL+S"]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def gen_body(rnd):
    pick = rnd.random()
    if pick < 0.08:
        return rnd.choice([b"", b"not json", b"[1,2]", b'"str"', b"null", b"true", b"{", b"[]",
                           b'{"action":}', b'{"action":null}', b'{"action":42}', b"\xff\xfe"])
    if pick < 0.14:
        return rnd.choice([b'{"action":"rm-rf"}', b'{"action":"click"}', b'{"x":1}', b"{}",
                           b'{"action":"type","text":123}', b'{"action":"key","key":[]}',
                           b'{"action":"click","x":1,"y":2,"z":3}',
                           b'{"action":"paste"}', b'{"action":"scroll","clicks":"x"}'])
    action = rnd.choice(list(ACTION_FIELDS))
    req, opt = ACTION_FIELDS[action]
    body = {"action": action}
    for f in req:
        if rnd.random() < 0.94:
            body[f] = rnd.choice(FREE_INTS if f.startswith("x") or f.startswith("y") else
                                 (KEYS if f == "key" else ["", "x" * rnd.randrange(1, 60)]))
    for f in opt:
        if rnd.random() < 0.5:
            body[f] = rnd.choice(BAD_VALUES + FREE_INTS + [0.004, -1, "0.5"])
    if rnd.random() < 0.15:
        body[rnd.choice(["windwo", "buton", "x2 ", "Double", "extra"])] = rnd.choice(BAD_VALUES)
    if rnd.random() < 0.05:
        return (json.dumps(body).encode() + b" " * rnd.randrange(1, 300))[:4096]
    return json.dumps(body).encode()


def gen_query(rnd):
    parts = []
    for k in ("region", "max", "quality", "fmt", "opt", "changed", "t"):
        if rnd.random() < 0.45:
            v = rnd.choice(["", "abc", "0", "-5", "99999", "1,2,3", "10,10,10,10", "5000,5000,200,200",
                            "jpeg", "png", "tiff", "1", "0", "x" * 50, "1e5", "0x10", "-0"])
            parts.append(f"{k}={v}")
    if rnd.random() < 0.1:
        parts.append("region=" + rnd.choice(["-1,-1,10,10", "0,0,1920,1080", "1919,1079,2,2"]))
    if rnd.random() < 0.08:
        parts.append("&".join(rnd.choice(["a=1", "region", "max=", "=%20", "t="]) for _ in range(2)))
    return "&".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=int(time.time()))
    ap.add_argument("--trace", action="store_true", help="print the first server tracebacks in full")
    a = ap.parse_args()
    rnd = random.Random(a.seed)

    import PIL
    PIL.ImageGrab = FakeGrab()
    tmp = tempfile.mkdtemp(prefix="pceye-fuzz-")
    port, tok = free_port(), "fuzztoken0123456789"
    srv = pceye.build(port, tok, "127.0.0.1", 1440, os.path.join(tmp, "f.log"), dry_run=True)
    # HTTP/1.0 for the harness only: no connection reuse, so a "handler died" line can never be a
    # keep-alive race. The real server keeps HTTP/1.1 (tested elsewhere for Content-Length honesty).
    pceye.Handler.protocol_version = "HTTP/1.0"
    pceye.Handler.limits = None                   # the limiter has its own tests; here we hunt for crashes
    pceye.Handler.display_rect = staticmethod(lambda force=False: [0, 0, 1920, 1080])
    pceye.Handler.input_space = staticmethod(lambda: [1920, 1080])
    pceye.cursor_pos = lambda: [640, 480]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    base = f"http://127.0.0.1:{port}"
    codes, hard, soft = {}, [], 0

    def still_alive():
        """The control question. Every 'the connection died' is only a finding if the server is also gone."""
        try:
            with urllib.request.urlopen(base + f"/state?t={tok}", timeout=4) as r:
                r.read()
                return r.status == 200
        except Exception:
            return False

    # The handler prints tracebacks to stderr; that is the ground truth of a crash, so capture it whole.
    real_err, sys.stderr = sys.stderr, io.StringIO()
    t0 = time.time()
    for i in range(a.requests):
        try:
            if rnd.random() < 0.5:
                q = gen_query(rnd)
                url = base + "/shot?" + (q + ("&" if q else "") + "t=" + tok)
                try:
                    with urllib.request.urlopen(url, timeout=8) as r:
                        codes[r.status] = codes.get(r.status, 0) + 1
                        r.read()
                except urllib.error.HTTPError as e:
                    codes[e.code] = codes.get(e.code, 0) + 1
                    e.read()
            else:
                hdrs = {"Content-Type": rnd.choice(["application/json"] * 12 + ["text/plain",
                        "application/x-www-form-urlencoded", "", "application/json; charset=utf-8"])}
                if rnd.random() < 0.06:
                    hdrs["Origin"] = rnd.choice(["https://evil.example", "http://127.0.0.1", "null"])
                if rnd.random() < 0.05:
                    hdrs["X-Token"] = rnd.choice([tok, "", "wrong"])
                q = "" if rnd.random() < 0.5 else "?t=" + rnd.choice([tok, "", "wrong"])
                req = urllib.request.Request(base + "/act" + q, data=gen_body(rnd), method="POST",
                                             headers=hdrs)
                try:
                    with urllib.request.urlopen(req, timeout=8) as r:
                        codes[r.status] = codes.get(r.status, 0) + 1
                        body = r.read()
                        if r.status != 204 and body and r.headers.get("Content-Type", "").startswith(
                                "application/json"):
                            json.loads(body)
                except urllib.error.HTTPError as e:
                    codes[e.code] = codes.get(e.code, 0) + 1
                    body = e.read()
                    try:
                        json.loads(body)
                    except Exception:
                        hard.append(f"HTTP {e.code} with a non-JSON body: {body[:120]!r}")
        except (RemoteDisconnected, ConnectionResetError, http.client.BadStatusLine) as e:
            # A closed socket LOOKS like a dead PC to an agent, which is exactly the bug class we hunt. But
            # it is also what a keep-alive race looks like to a test client. Tell them apart the only honest
            # way: ask the server, right now, whether it is still answering.
            if still_alive():
                soft += 1
            else:
                hard.append(f"request {i}: {type(e).__name__} AND the server stopped answering — crash, "
                            f"not a refusal")
        except Exception as e:
            hard.append(f"request {i}: client-side {type(e).__name__}: {e}")
    dt = time.time() - t0
    captured, sys.stderr = sys.stderr, real_err
    alive = True
    try:
        urllib.request.urlopen(base + f"/state?t={tok}", timeout=8).read()
    except Exception:
        alive = False
    srv.shutdown(); srv.server_close()
    pceye.Handler._close_log()
    print(f"fuzz {a.requests} requests in {dt:.1f}s (seed {a.seed}) · server alive after: {alive}")
    print("status codes:", dict(sorted(codes.items())))
    trace = [l.strip() for l in captured.getvalue().splitlines()
             if re.match(r"^[A-Za-z_.]*(Error|Exception|Interrupt):", l.strip())
             and "peer vanished" not in l]
    if trace:
        for line in sorted(set(trace)):
            hard.append("server printed an exception (a client hang-up would be silent): " + line)
    print("keep-alive races (server verified alive after each):", soft)
    print("server-side tracebacks:", len(trace))
    if trace and a.trace:
        for l in captured.getvalue().splitlines():
            if "server.py" in l or re.match(r"^\S*(Error|Exception):", l.strip()):
                print("   |", l.strip()[:150])
    print("hard failures:", len(hard))
    for h in hard[:12]:
        print("  !", h)
    if not alive:
        print("VERDICT: the server stopped answering — that is a crash, not a refusal")
    return 0 if (alive and not hard) else 1


if __name__ == "__main__":
    sys.exit(main())
