#!/usr/bin/env python3
"""pc-eye benchmark harness — measures what an agent actually pays per round trip.

Runnable headless: encode-path numbers are measured with a synthetic screen buffer
(the same resize/encode code /shot runs), and route numbers are measured against a
live server with the Windows seams faked. Prints one line per metric, machine-readable.
"""
import hashlib
import io, json, os, socket, statistics, sys, threading, time, urllib.request, urllib.error
from pathlib import Path
import concurrent.futures as cf
from PIL import Image

HERE = Path(__file__).resolve().parent.parent
for _i, _a in enumerate(sys.argv):                  # --repo <path>: benchmark an older tree, same harness
    if _a == "--repo":
        HERE = Path(sys.argv[_i + 1]).resolve()
        sys.argv.pop(_i + 1); sys.argv.pop(_i)
        break
sys.path.insert(0, str(HERE))
import server as pceye

def ms(seq):  # percentile-ish summary
    seq = sorted(seq)
    p = lambda q: seq[min(len(seq) - 1, int(q * len(seq)))]
    return {"n": len(seq), "median_ms": round(p(.5), 2), "p95_ms": round(p(.95), 2),
            "mean_ms": round(statistics.fmean(seq), 2)}

def noisy_image(w, h):
    """A screen with real content: text-on-white plus noise, so JPEG actually has to work.

    A black rectangle compresses to nothing and makes every encode number a lie, which is how a
    benchmark can 'prove' a 25% saving that does not exist. This one also gives the resizer something
    to do, so the filter cost is visible.
    """
    import random
    rnd = random.Random(1234)
    base = bytes(bytearray(rnd.randrange(240, 256) for _ in range(w * h * 3)))   # near-white desk
    px = bytearray(base)
    for y in range(20, h - 20, 26):                                               # rows of "text"
        for x in range(40, w - 40):
            if (x // 7 + y) % 3:
                o = (y * w + x) * 3
                run = 12 if (x // 7) % 4 else 0                                   # word gaps
                if x % 7 < 5 and run:
                    for c in range(3):
                        px[o + c] = rnd.randrange(20, 90)
    for _ in range(w * 4):                                                        # a few photo-ish blocks
        x, y = rnd.randrange(w - 80), rnd.randrange(h - 80)
        for dy in range(80):
            for dx in range(80):
                o = ((y + dy) * w + x + dx) * 3
                v = rnd.randrange(256)
                px[o] = v; px[o + 1] = (v * 3) % 256; px[o + 2] = (v * 7) % 256
    return Image.frombytes("RGB", (w, h), bytes(px))


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

# ---------- 1. pure encode path (what /shot costs after the grab) ----------
def encode_path(n=12):
    out = {}
    for (w, h) in ((1920, 1080), (2560, 1440)):
        img = Image.new("RGB", (w, h))
        # give it real entropy or JPEG cost is unrealistically low
        px = bytearray(os.urandom(w * h * 3 // 8))
        img.putdata(list(Image.frombytes("RGB", (w // 4, h // 4), bytes(px)).getdata()) * 16)
        for mw, q, opt, fmt in ((1440, 72, True, "jpeg"), (1440, 72, False, "jpeg"),
                                (1440, 85, True, "jpeg"), (0, 0, False, "png"), (0, 0, False, "jpeg")):
            t = []
            for _ in range(n):
                im = img.copy()
                t0 = time.perf_counter()
                if mw and im.width > mw:
                    im = im.resize((mw, max(1, int(im.height * mw / im.width))))
                buf = io.BytesIO()
                if fmt == "png":
                    im.save(buf, "PNG")
                else:
                    im.save(buf, "JPEG", quality=q or 72, optimize=opt)
                t.append((time.perf_counter() - t0) * 1000)
            out[f"{w}x{h}->{mw or 'full'} {fmt} q{q} opt{int(opt)}"] = {**ms(t), "kb": round(len(buf.getvalue()) / 1024, 1)}
    return out

# ---------- 2. route latency against a live server ----------
class FlatScreen(pceye.Screen):
    POLICY = None            # None = whatever the build defaults to; True/False = pinned for a fair A/B
    """Real Screen.grab code path over a synthetic source: isolates HTTP+encode from the display."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k); self._img = Image.new("RGB", (1920, 1080))
    def size(self):
        return (1920, 1080)
    def grab(self, region=None, max_width=None, fmt="jpeg", quality=None, optimize=None):
        """Speaks the CURRENT Screen contract, including `optimize` — a fake that lags the real
        signature is how a benchmark quietly keeps measuring the old code."""
        img = self._img
        mw = self.max_width if max_width is None else int(max_width)
        if mw and img.width > mw:
            img = img.resize((mw, max(1, int(img.height * mw / img.width))))
        buf = io.BytesIO()
        if FlatScreen.POLICY is not None:
            opt = FlatScreen.POLICY          # pinned: both trees encode identically
        else:
            opt = getattr(self, "optimize", True) if optimize is None else bool(optimize)
        if fmt == "png":
            img.save(buf, "PNG", optimize=opt); mime = "image/png"
        else:
            img.save(buf, "JPEG", quality=self.quality if quality is None else int(quality), optimize=opt)
            mime = "image/jpeg"
        data = buf.getvalue()
        return data, {"full": [1920, 1080], "sent": [img.width, img.height], "scaled": img.width != 1920,
                      "bytes": len(data), "mime": mime, "format": fmt, "at": "now",
                      "hash": hashlib.sha256(data).hexdigest()[:16], "optimize": bool(opt),
                      "quality": self.quality if quality is None else int(quality)}

def routes(port, token, n=25):
    base = f"http://127.0.0.1:{port}"
    def get(path, raw=False):
        t = []
        for _ in range(n):
            t0 = time.perf_counter()
            with urllib.request.urlopen(base + path + ("&" if "?" in path else "?") + "t=" + token, timeout=10) as r:
                b = r.read()
            t.append((time.perf_counter() - t0) * 1000)
        return {**ms(t), "kb": round(len(b) / 1024, 1)}
    out = {"/state": get("/state"), "/shot": get("/shot", raw=True),
           "/shot?max=0": get("/shot?max=0", raw=True), "/shot?quality=55": get("/shot?quality=55", raw=True)}
    def post(body):
        t = []
        for _ in range(n):
            req = urllib.request.Request(base + "/act?t=" + token, data=json.dumps(body).encode(),
                                         method="POST", headers={"Content-Type": "application/json"})
            t0 = time.perf_counter()
            try:
                urllib.request.urlopen(req, timeout=20).read()
            except urllib.error.HTTPError as e:
                e.read()
            t.append((time.perf_counter() - t0) * 1000)
        return ms(t)
    out["/act click"] = post({"action": "click", "x": 100, "y": 100})
    out["/act type 200 chars"] = post({"action": "type", "text": "x" * 200})
    return out

def contention(port, token):
    """Does a slow /act starve reads? 6 concurrent /state while one 200-char type runs."""
    base = f"http://127.0.0.1:{port}"
    def state():
        t0 = time.perf_counter()
        urllib.request.urlopen(base + "/state?t=" + token, timeout=30).read()
        return (time.perf_counter() - t0) * 1000
    def typing():
        urllib.request.urlopen(urllib.request.Request(base + "/act?t=" + token,
            data=json.dumps({"action": "type", "text": "y" * 200}).encode(), method="POST", headers={"Content-Type": "application/json"}), timeout=30).read()
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        th = ex.submit(typing); time.sleep(0.02)
        lat = list(ex.map(lambda _: state(), range(6)))
    th.result()
    return {"reads_while_typing_ms": ms(lat)}

def state_cost(port, token, n=40):
    """What the agent pays for the request that only asks 'are we in the same coordinate space?'"""
    base = f"http://127.0.0.1:{port}"
    t = []
    for _ in range(n):
        t0 = time.perf_counter()
        urllib.request.urlopen(base + f"/state?t={token}", timeout=10).read()
        t.append((time.perf_counter() - t0) * 1000)
    return ms(t)


def bytes_per_loop(port, token, n=15):
    """The unit of the whole product: look (/shot) + check (/state). Bytes moved per iteration."""
    base = f"http://127.0.0.1:{port}"
    tot, t = 0, []
    for _ in range(n):
        t0 = time.perf_counter()
        with urllib.request.urlopen(base + f"/shot?t={token}", timeout=10) as r:
            tot += len(r.read())
            urllib.request.urlopen(base + f"/state?t={token}", timeout=10).read()
        t.append((time.perf_counter() - t0) * 1000)
    return {"median_loop_ms": ms(t)["median_ms"], "kb_per_loop": round(tot / n / 1024, 1)}


if __name__ == "__main__":
    for i, a in enumerate(sys.argv):
        if a == "--policy":
            FlatScreen.POLICY = sys.argv[i + 1] == "optimize"
            sys.argv[i + 1] = ""; sys.argv[i] = ""
    sys.argv = [a for a in sys.argv if a]
    nolit = "--no-limit" in sys.argv
    sys.argv = [a for a in sys.argv if a != "--no-limit"]
    port, token = free_port(), "benchtoken123"
    srv = pceye.build(port, token, "127.0.0.1", 1440, "/tmp/bench.log", True)
    if nolit or not hasattr(pceye.Handler, "limits") or pceye.Handler.limits is None:
        pceye.Handler.limits = None      # a benchmark of the encode path must not be a benchmark of the limiter
    srv.RequestHandlerClass.screen = FlatScreen(1440)
    threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
    res = {"version": pceye.VERSION, "policy": ("pinned-optimize" if FlatScreen.POLICY is True else
                                                "pinned-fast" if FlatScreen.POLICY is False else "build default"), "encode_path": encode_path(), "routes": routes(port, token),
           "contention": contention(port, token), "state_cost_ms": state_cost(port, token),
           "bytes_per_loop": bytes_per_loop(port, token)}
    srv.shutdown()
    out = None
    for i, a in enumerate(sys.argv):
        if a == "--out":
            out = sys.argv[i + 1]
    txt = json.dumps(res, indent=2)
    if out:
        with open(out, "w") as f:
            f.write(txt + "\n")
    print(txt)
