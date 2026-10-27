import pathlib
p = pathlib.Path("/home/user/pc-eye/server.py")
s = p.read_text()

old = '''    def _json(self, code, obj):
        body = json.dumps(obj, sort_keys=True, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self._write(body)'''
new = '''    def _json(self, code, obj):
        body = json.dumps(obj, sort_keys=True, default=str).encode()
        return self._respond(code, [("Content-Type", "application/json"),
                                    ("Content-Length", len(body)),
                                    ("Cache-Control", "no-store"),
                                    ("X-Content-Type-Options", "nosniff")], body)'''

assert old in s
s = s.replace(old, new, 1)

# _bytes → same funnel
old_b = '''    def _bytes(self, code, data, mime, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        if data:
            self._write(data)'''
new_b = '''    def _bytes(self, code, data, mime, extra=None):
        hdrs = [("Content-Type", mime), ("Content-Length", len(data)),
                ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")]
        hdrs += [(k, v) for k, v in (extra or {}).items()]
        return self._respond(code, hdrs, data)'''
assert old_b in s
s = s.replace(old_b, new_b, 1)

# the funnel
old_w = '''    def _write(self, data) -> bool:
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
            return False'''
new_w = '''    # Every exception that can legitimately mean "the other end is gone" (F41). A peer aborting is not a
    # bug; the whole point of listing them is so that ANY other traceback still means one.
    PEER_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                 TimeoutError, ssl.SSLError, OSError)

    def _respond(self, code, headers, body) -> bool:
        """Write a whole response, tolerating a peer that vanished. Returns False if it did.

        My first attempt at this guarded only `wfile.write(body)`. The windows-latest soak then printed 33
        tracebacks pointing at the `_json` call on the cross-origin refusal — because the flush happens in
        `end_headers()`/`send_header()`, not in the body write, and on Windows a peer that aborted while we
        were buffering raises ConnectionAbortedError (WSAECONNABORTED, 10053) where Linux simply gives you
        EPIPE on the next write. Same class of bug as every other finding in this audit — an unanswered
        request looks exactly like a dead PC — and it was only visible on the platform I could not test on.

        So the funnel is the whole response, and the swallow list is "socket said the peer is gone", not
        "OSError", which would also hide a genuine bug: ssl.SSLError is imported so that HTTPS-in-front-of-a-
        proxy experiments stay honest too.
        """
        try:
            self.send_response(code)
            for k, v in headers:
                self.send_header(k, str(v))
            self.end_headers()               # ← the flush; the header block is where Windows notices first
            if body:
                self.wfile.write(body)
            return True
        except self.PEER_GONE:
            self.close_connection = True
            try:
                if self.wfile is not None:
                    self.wfile.close()       # do not let GC flush a corpse and print a second traceback
            except Exception:
                pass
            return False

    def _write(self, data) -> bool:
        """Kept for the 304/204 paths and direct callers: a body write on its own, peer-abort tolerant."""
        try:
            self.wfile.write(data)
            return True
        except self.PEER_GONE:
            self.close_connection = True
            return False'''
assert old_w in s
s = s.replace(old_w, new_w, 1)
s = s.replace("import platform\n", "import platform\nimport ssl\n", 1)
p.write_text(s)
print("response funnel + Windows peer-abort semantics")
