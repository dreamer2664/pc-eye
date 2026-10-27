#!/usr/bin/env python3
"""How much PIXEL WORK does each request cost? Counted, not argued.

`Screen.size()` used to answer "how wide is the monitor" by grabbing the whole screen
losslessly. That is the hot path: /shot did it too (it needs the size for the conversion
headers), so the loop an agent runs hundreds of times an hour paid TWO full captures per look.
This harness counts captures per request, on both trees, with the OS geometry deliberately made
unavailable so the fallback path is the one under test — the same situation as a box where
GetSystemMetrics is not reachable.

Usage:  python3 evidence/capture_count.py [--old /tmp/oldtree/server.py] [--new server.py]
It does not need a display, a mouse or a keyboard. It only counts.
"""
import argparse
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROUTES = ("/state", "/shot", "/shot?max=0", "/selftest")
COUNT = {"n": 0}


class FakePILImage:
    """Just enough of a PIL image for both versions' encode paths to run to completion."""
    width, height, mode = 1920, 1080, "RGB"

    def resize(self, *a, **k):
        return self

    def convert(self, *a, **k):
        return self

    def crop(self, *a, **k):
        return self

    def save(self, buf, fmt="JPEG", **kw):
        buf.write(b"\xff\xd8" + os.urandom(1200) + b"\xff\xd9")


class GrabCounter:
    @staticmethod
    def grab(*a, **k):
        COUNT["n"] += 1
        return FakePILImage()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def measure(path, tag, geometry):
    """geometry: 'known' = the OS can answer (a real desktop), 'unknown' = it cannot (worst case)."""
    here = os.path.dirname(os.path.abspath(path))
    sys.path.insert(0, here)
    spec = importlib.util.spec_from_file_location("srv_" + tag.replace(".", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    import PIL
    real = getattr(PIL, "ImageGrab", None)
    PIL.ImageGrab = GrabCounter()
    if hasattr(mod, "_display_cache"):
        if geometry == "known":
            mod._display_cache.update(rect=[0, 0, 1920, 1080], at=time.time() + 999)
        else:
            mod._display_cache.update(rect=None, at=0.0)
    tmp = tempfile.mkdtemp(prefix="pceye-count-")
    tok = "tok1234567890abcd"
    port = free_port()
    srv = mod.build(port, tok, "127.0.0.1", 1440, os.path.join(tmp, "c.log"), True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    per, err = {}, {}
    base = f"http://127.0.0.1:{port}"
    for route in ROUTES:
        COUNT["n"] = 0
        try:
            urllib.request.urlopen(base + route + ("&" if "?" in route else "?") + "t=" + tok,
                                   timeout=8).read()
        except urllib.error.HTTPError as e:
            e.read()
            err[route] = f"HTTP {e.code}"
        except Exception as e:
            err[route] = f"{type(e).__name__}: {e}"
        per[route] = COUNT["n"]
    srv.shutdown()
    srv.server_close()
    if hasattr(mod.Handler, "_close_log"):
        mod.Handler._close_log()
    if real is not None:
        PIL.ImageGrab = real
    sys.path.remove(here)
    return {"version": getattr(mod, "VERSION", "?"), "captures": per, "errors": err}


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import oldtree
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--new", default=os.path.join(here, "server.py"))
    base = oldtree.ensure()
    ap.add_argument("--old", default=str(base / "server.py") if base else "/dev/null")
    a = ap.parse_args()
    out = {}
    for geom in ("known", "unknown"):
        ok = True
        for tag, path in (("old", a.old), ("new", a.new)):
            if not os.path.exists(path) or not os.path.getsize(path):
                oldtree.skip() if tag == "old" else print(f"  (missing {path})")
                ok = False
                continue
            out.setdefault(geom, {})[tag] = measure(path, tag + "-" + geom, geom)
        if not ok:
            continue
        o = out[geom]["old"]["captures"]
        n = out[geom]["new"]["captures"]
        print(f"\n== OS geometry {geom.upper():8s} : pixel captures per request "
              f"(old {out[geom]['old']['version']} vs new {out[geom]['new']['version']}) ==")
        print("%-14s %6s %6s  effect" % ("route", "old", "new"))
        for r in ROUTES:
            d = "same" if o.get(r) == n.get(r) else ("FASTER" if n.get(r, 0) < o.get(r, 0) else "WORSE")
            print("%-14s %6d %6d  %s" % (r, o.get(r, -1), n.get(r, -1), d))
        for r in ROUTES:
            if o.get(r, 0) and n.get(r, 9) > o[r]:
                print(f"  !! regression on {r}")
        oc = o.get("/shot", 0) + o.get("/state", 0)
        nc = n.get("/shot", 0) + n.get("/state", 0)
        print(f"  one look+check cycle: {oc} -> {nc} capture(s)"
              + (f"   ({oc // max(nc,1)}x less pixel work)" if nc and oc > nc else ""))
    print("\n" + json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
