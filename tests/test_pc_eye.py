#!/usr/bin/env python3
"""Protocol tests for pc-eye, runnable on a headless box.

What these CAN prove: the routes, the token, the action contract, the refusals, the log — everything an agent
will hit. What they CANNOT prove: that a click lands on the right pixel on Windows, or that capture returns a
live screen there. Those are proved by the Director's own machine, and `launcher.py` says out loud which of
them failed. A test that pretends otherwise is the failure mode this project keeps paying for.
"""
from __future__ import annotations

import base64
import json
import os
import platform
import socket
import sys
import tempfile
import threading
import pathlib
import re
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import server as pceye  # noqa: E402

PNG_1x1 = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")


class FakeScreen(pceye.Screen):
    """Stands in for the real screen so the protocol can be tested without a display."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = []

    def size(self):
        return (1920, 1080)

    def grab(self, region=None, max_width=None, fmt="jpeg", quality=None, optimize=None):
        region = pceye.parse_region(region)      # same parser as the real Screen — a fake that skips
        self.calls.append({"region": region, "max_width": max_width, "fmt": fmt,     # it is a mirror
                           "quality": quality, "optimize": optimize})
        if region and region[0] + region[2] > 1920 or (region and region[1] + region[3] > 1080):
            raise ValueError(f"region {region} is outside the screen (1920x1080 at 0,0) — returning it "
                             f"would send you a black rectangle that looks like an empty desktop.")
        meta = {"full": [1920, 1080], "scaled": True, "bytes": len(PNG_1x1), "at": "now",
                "format": fmt, "hash": pceye.content_hash(PNG_1x1), "quality": 72, "optimize": False}
        if region:
            return PNG_1x1, dict(meta, sent=[1, 1], mime="image/png", format="png")
        return PNG_1x1, dict(meta, sent=[1440, 810], mime="image/jpeg")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, base, token):
        self.base, self.token = base, token

    def get(self, path, token=True, raw=False):
        url = self.base + path + (("&" if "?" in path else "?") + "t=" + self.token if token else "")
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                body = r.read()
                return r.status, (body if raw else json.loads(body or b"{}")), dict(r.headers)
        except urllib.error.HTTPError as e:
            body = e.read()
            return e.code, (body if raw else json.loads(body or b"{}")), dict(e.headers)

    def post(self, path, obj, token=True):
        url = self.base + path + (("?t=" + self.token) if token else "")
        req = urllib.request.Request(url, data=json.dumps(obj).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})   # /act demands it: see
                                                                                     # test_a_browser_shaped                                                                                     #_post_is_refused
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


class Protocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="pceye-")
        cls.port = free_port()
        cls.token = "testtoken123"
        srv = pceye.build(cls.port, cls.token, "127.0.0.1", 1440, os.path.join(cls.tmp.name, "t.log"),
                          dry_run=True)
        srv.RequestHandlerClass.screen = FakeScreen()
        cls.srv = srv
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        cls.c = Client(f"http://127.0.0.1:{cls.port}", cls.token)
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()
        getattr(pceye.Handler, '_close_log', lambda: None)()          # the held log handle is class state; tests must not leak it
        cls.tmp.cleanup()

    # ---- auth ----
    def test_no_token_is_401_and_says_how_to_supply_one(self):
        code, body, _ = self.c.get("/state", token=False)
        self.assertEqual(code, 401)
        self.assertFalse(body["ok"])
        self.assertIn("token", body["hint"].lower())

    def test_a_wrong_token_is_401(self):
        url = f"http://127.0.0.1:{self.port}/state?t=wrong"
        try:
            urllib.request.urlopen(url, timeout=5)
            self.fail("a wrong token was accepted")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_the_landing_page_needs_no_token_and_names_the_off_switch(self):
        code, body, _ = self.c.get("/", token=False, raw=True)
        self.assertEqual(code, 200)
        self.assertIn(b"close this window", body)

    # ---- reads ----
    def test_state_reports_screen_cursor_and_last_action(self):
        code, body, _ = self.c.get("/state")
        self.assertEqual(code, 200)
        self.assertEqual(body["screen"], [1920, 1080])
        self.assertIn("last", body)

    def test_shot_returns_real_image_bytes_with_an_image_content_type(self):
        code, body, headers = self.c.get("/shot", raw=True)
        self.assertEqual(code, 200)
        self.assertTrue(headers.get("Content-Type", "").startswith("image/"))
        self.assertGreater(len(body), 50)
        self.assertEqual(body[:8], PNG_1x1[:8], "the bytes are the image, not a JSON error wearing one")

    def test_shot_carries_the_conversion_numbers_the_agent_needs(self):
        """Without these two headers, an agent aiming from a downscaled image clicks the wrong pixel."""
        code, _, headers = self.c.get("/shot", raw=True)
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("X-Screen-Full"), "1920x1080")
        self.assertEqual(headers.get("X-Screen-Sent"), "1440x810")

    def test_shot_accepts_a_region_and_an_explicit_max(self):
        code, _, _ = self.c.get("/shot?region=100,200,300,120&max=640&fmt=png", raw=True)
        self.assertEqual(code, 200)
        last = self.srv.RequestHandlerClass.screen.calls[-1]
        self.assertEqual(last["region"], (100, 200, 300, 120))
        self.assertEqual(last["max_width"], 640)
        self.assertEqual(last["fmt"], "png")

    # ---- the action contract ----
    def test_click_reports_what_it_did_and_moves_the_record(self):
        code, body = self.c.post("/act", {"action": "click", "x": 640, "y": 480})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["did"]["x"], 640)
        code, state, _ = self.c.get("/state")
        self.assertEqual(state["last"]["action"], "click")

    def test_every_documented_action_dispatches(self):
        for body in ({"action": "move", "x": 10, "y": 10},
                     {"action": "type", "text": "hello"},
                     {"action": "key", "key": "enter"},
                     {"action": "scroll", "clicks": -3},
                     {"action": "drag", "x": 1, "y": 2, "x2": 300, "y2": 400}):
            code, out = self.c.post("/act", body)
            self.assertEqual(code, 200, f"{body['action']}: {out}")
            self.assertTrue(out["ok"], out)

    def test_unknown_action_is_refused_with_the_list(self):
        code, body = self.c.post("/act", {"action": "rm-rf"})
        self.assertEqual(code, 400)
        self.assertIn("click", body["actions"])

    def test_missing_field_is_named(self):
        code, body = self.c.post("/act", {"action": "click", "x": 5})
        self.assertEqual(code, 400)
        self.assertIn("y", body["error"])

    def test_a_typo_field_does_not_ride_along(self):
        code, body = self.c.post("/act", {"action": "click", "x": 5, "y": 5, "windwo": "x"})
        self.assertEqual(code, 400)
        self.assertIn("windwo", body["error"])

    def test_ctrl_alt_del_is_refused_and_explained(self):
        code, body = self.c.post("/act", {"action": "key", "key": "ctrl+alt+del"})
        self.assertEqual(code, 409)
        self.assertIn("reserves it", body["error"])
        self.assertIn("never be an automation verb", body["error"])

    def test_a_bad_region_is_refused_in_words_and_never_becomes_a_full_screen_shot(self):
        code, body, _ = self.c.get("/shot?region=abc")
        self.assertEqual(code, 400, "a mistyped region must not silently succeed as a whole-screen capture")
        self.assertIn("x,y,w,h", body["error"])

    def test_region_refusals_name_the_problem(self):
        for bad, want in (("1,2,3", "four numbers"), ("1,2,0,3", "positive"), ("a,b,c,d", "integers")):
            with self.assertRaises(ValueError) as e:
                pceye.parse_region(bad)
            self.assertIn(want, str(e.exception), bad)
        self.assertIsNone(pceye.parse_region(None))
        self.assertEqual(pceye.parse_region(" 5, 6, 7, 8 "), (5, 6, 7, 8))

    def test_text_is_never_echoed_back_in_full(self):
        code, body = self.c.post("/act", {"action": "type", "text": "super-secret-value"})
        self.assertEqual(code, 200)
        self.assertNotIn("super-secret-value", json.dumps(body), "the reply must not carry the payload back")
        self.assertIn("chars", json.dumps(body))

    def test_selftest_is_honest_about_this_machine(self):
        code, body, _ = self.c.get("/selftest")
        self.assertEqual(code, 200)
        names = [c["name"] for c in body["checks"]]
        self.assertIn("capture", names)
        platform_check = [c for c in body["checks"] if c["name"] == "platform"][0]
        self.assertFalse(platform_check["ok"],
                         "on a non-Windows box this check must go red — a green 'platform' on Linux would be "
                         "the exact lie that makes a green run worthless")

    def test_every_action_lands_in_the_log(self):
        """Self-contained on purpose: a test that passes only because another test ran first is not a test."""
        log = Path(self.tmp.name) / "t.log"
        code, _ = self.c.post("/act", {"action": "click", "x": 7, "y": 8})
        self.assertEqual(code, 200)
        self.c.get("/shot", raw=True)
        self.assertTrue(log.exists(), "no log file: nothing would be reviewable afterwards")
        text = log.read_text()
        self.assertIn("act click", text)
        self.assertIn("shot", text)


class CoordinateSpace(unittest.TestCase):
    """The bug found on the real machine, kept as a test so it cannot come back.

    On CarloPC the screenshot was 1920x1080 while SetCursorPos/GetCursorPos lived in 1536x864 (125% DPI
    scaling), clamping silently: asked (1800,933), landed (1535,863). The agent reasons in image pixels, so
    every click would have gone to the wrong place while reporting ok.
    """

    def test_the_carlopc_case_is_caught(self):
        r = pceye.coord_space_report([1920, 1080], [1536, 864])
        self.assertFalse(r["consistent"])
        self.assertAlmostEqual(r["scale"], 1.25, places=2)
        self.assertIn("DPI", r["warning"])
        self.assertIn("1536x864", r["warning"])

    def test_agreeing_spaces_are_quiet(self):
        r = pceye.coord_space_report([1920, 1080], [1920, 1080])
        self.assertTrue(r["consistent"])
        self.assertIsNone(r["warning"])

    def test_unknown_input_space_is_not_claimed_either_way(self):
        r = pceye.coord_space_report([1920, 1080], None)
        self.assertIsNone(r["consistent"], "must not report a verdict it cannot support")

    def test_non_uniform_scaling_is_reported_per_axis(self):
        r = pceye.coord_space_report([1920, 1080], [1280, 720])
        self.assertFalse(r["consistent"])
        self.assertAlmostEqual(r["scale"], 1.5, places=2)


class WindowsBehaviour(unittest.TestCase):
    """Routes whose behaviour differs on Windows, exercised by faking only the two OS seams."""

    def setUp(self):
        self._space, self._cursor = pceye.input_space, pceye.cursor_pos
        self._rect = pceye.display_rect
        pceye._display_cache.update(rect=None, at=0.0)        # no stale geometry between tests
        cls = pceye.Handler
        self._cls_seams = (cls.input_space, cls.display_rect)
        cls.input_space = staticmethod(lambda: pceye.input_space())
        cls.display_rect = staticmethod(lambda force=False: pceye.display_rect(force))
        self.tmp = tempfile.TemporaryDirectory(prefix="pceye-w-")
        self.port, self.token = free_port(), "wintoken"
        srv = pceye.build(self.port, self.token, "127.0.0.1", 1440,
                          os.path.join(self.tmp.name, "w.log"), dry_run=True)
        srv.RequestHandlerClass.screen = FakeScreen()
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.srv = srv
        self.c = Client(f"http://127.0.0.1:{self.port}", self.token)
        time.sleep(0.2)

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()
        getattr(pceye.Handler, '_close_log', lambda: None)()
        pceye.Handler.input_space, pceye.Handler.display_rect = self._cls_seams
        self.tmp.cleanup()
        pceye.input_space, pceye.cursor_pos = self._space, self._cursor
        pceye.display_rect = self._rect
        pceye._display_cache.update(rect=None, at=0.0)

    def test_a_coordinate_the_hand_cannot_reach_is_refused_not_clamped(self):
        pceye.input_space = lambda: [1536, 864]          # the CarloPC situation
        pceye.display_rect = lambda force=False: None    # bounds rule only, so the gate stays out of it
        code, body = self.c.post("/act", {"action": "click", "x": 1800, "y": 933})
        self.assertEqual(code, 409, "a clamped click must never be reported as ok")
        self.assertIn("1536x864", body["error"])
        self.assertIn("coord_space", body["error"])

    def test_agreeing_spaces_allow_a_coordinate_inside_the_space(self):
        """Behaviour change pinned (1.0.3): when the eyes and the hand are in the SAME space, the click
        goes through; when they disagree, EVERY pointing action is refused — not just the ones whose
        numbers fall outside. The 1.0.2 code let (900,400) through while the screenshot was 1920x1080
        and the hand lived in 1536x864, i.e. it clicked what was at (1125,500) and said ok."""
        pceye.input_space = lambda: [1536, 864]
        pceye.display_rect = lambda force=False: [0, 0, 1536, 864]      # one space: no scale
        code, body = self.c.post("/act", {"action": "move", "x": 900, "y": 400})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])

    def test_a_split_coordinate_space_refuses_even_an_in_bounds_click(self):
        pceye.input_space = lambda: [1536, 864]
        pceye.display_rect = lambda force=False: [0, 0, 1920, 1080]      # the CarloPC situation
        code, body = self.c.post("/act", {"action": "move", "x": 900, "y": 400})
        self.assertEqual(code, 409, "a click that would land at (1125,500) must not be reported as ok")
        self.assertIn("different spaces", body["error"])
        self.assertIn("do not work around", body["error"].lower())
        # ...and a verb that does not aim the pointer still works, because it cannot land wrong
        code, body = self.c.post("/act", {"action": "type", "text": "ok"})
        self.assertEqual(code, 200)

    def test_a_click_that_lands_elsewhere_says_so_instead_of_ok(self):
        pceye.input_space = lambda: [1920, 1080]
        pceye.display_rect = lambda force=False: [0, 0, 1920, 1080]
        pceye.cursor_pos = lambda: [1535, 863]           # the pointer did not go where it was told
        code, body = self.c.post("/act", {"action": "click", "x": 1800, "y": 933})
        self.assertEqual(code, 200)
        self.assertFalse(body["exact"])
        self.assertEqual(body["landed"], [1535, 863])
        self.assertIn("NOT having done", body["warning"])

    def test_a_click_that_lands_where_asked_is_reported_exact(self):
        pceye.input_space = lambda: [1920, 1080]
        pceye.display_rect = lambda force=False: [0, 0, 1920, 1080]
        pceye.cursor_pos = lambda: [1800, 933]
        code, body = self.c.post("/act", {"action": "click", "x": 1800, "y": 933})
        self.assertTrue(body["exact"])
        self.assertNotIn("warning", body)


class InputRules(unittest.TestCase):
    """The input layer's own refusals, without a socket in the way."""

    def test_unknown_key_names_the_problem(self):
        inp = pceye.Input(dry_run=True)
        with self.assertRaises(RuntimeError) as e:
            inp.key("hyperdrive")
        self.assertIn("unknown key", str(e.exception))

    def test_known_keys_are_all_real_windows_virtual_keys(self):
        for name, code in pceye.Input.KEYS.items():
            self.assertIsInstance(code, int, name)
            self.assertTrue(0x01 <= code <= 0xFE, f"{name} -> {hex(code)}")

    def test_letter_chords_resolve_and_do_not_raise(self):
        """'ctrl+t' was refused on the first live run: only named keys were in the table. A dict.get default
        is evaluated even when unused, so the naive fix would still have crashed on every chord."""
        inp = pceye.Input(dry_run=True)
        for chord in ("ctrl+t", "ctrl+shift+n", "alt+f4", "ctrl+s", "win+r"):
            inp.key(chord)                     # must not raise
        self.assertIn(("key", "ctrl+t", 1), inp.did)

    def test_a_chord_with_an_unknown_word_is_refused_and_named(self):
        inp = pceye.Input(dry_run=True)
        with self.assertRaises(RuntimeError) as e:
            inp.key("ctrl+hyper")
        self.assertIn("hyper", str(e.exception))

    def test_a_single_letter_is_a_key(self):
        inp = pceye.Input(dry_run=True)
        inp.key("a")
        self.assertIn(("key", "a", 1), inp.did)

    def test_dry_run_records_instead_of_acting(self):
        inp = pceye.Input(dry_run=True)
        inp.click(1, 2)
        inp.type_text("x")
        self.assertEqual(inp.did, [("click", 1, 2, "left"), ("type", "x")])




class RegressionV103(unittest.TestCase):
    """One named test per flaw the 1.0.3 audit found. A fix without a test is a rumour.

    Every one of these was first demonstrated against the shipped 1.0.2 code on a headless box, i.e.
    none of them needed a Windows machine to find — which is the argument for the CI leg, not against it.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="pceye-r-")
        cls.port, cls.token = free_port(), "regressiontoken"
        cls.srv = pceye.build(cls.port, cls.token, "127.0.0.1", 1440,
                              os.path.join(cls.tmp.name, "r.log"), dry_run=True)
        cls.srv.RequestHandlerClass.screen = FakeScreen()
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.c = Client(f"http://127.0.0.1:{cls.port}", cls.token)
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close(); getattr(pceye.Handler, '_close_log', lambda: None)(); cls.tmp.cleanup()

    def raw_post(self, payload, ctype="application/json", headers=None, token=True):
        url = f"http://127.0.0.1:{self.port}/act" + (f"?t={self.token}" if token else "")
        h = {"Content-Type": ctype}
        h.update(headers or {})
        req = urllib.request.Request(url, data=payload, method="POST", headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    # ---- F1: /shot argument validation ----
    def test_a_non_numeric_max_is_a_400_that_names_the_field(self):
        """Was: `int("abc")` inside the route -> handler died -> the agent saw 'connection closed without
        response', which is indistinguishable from a PC that is off."""
        for bad in ("max=abc", "max=-5", "quality=999", "quality=x", "opt=maybe", "fmt=tiff"):
            code, body, _ = self.c.get(f"/shot?{bad}")
            self.assertEqual(code, 400, bad)
            self.assertIn("must be", body["error"], bad)

    def test_quality_and_opt_are_actually_honoured(self):
        """/shot used to have no quality parameter at all, so 'quality=20' silently returned q72 — a smaller
        number in the URL and the same bytes. Silent acceptance of an ignored knob is how trust dies."""
        self.c.get("/shot?quality=20&opt=1", raw=True)
        call = self.srv.RequestHandlerClass.screen.calls[-1]
        self.assertEqual(call["quality"], 20)
        self.assertTrue(call["optimize"])
        self.c.get("/shot?quality=90&opt=0", raw=True)
        call = self.srv.RequestHandlerClass.screen.calls[-1]
        self.assertEqual(call["quality"], 90)
        self.assertFalse(call["optimize"])

    # ---- F2: body must be an object, and must not be able to kill the handler ----
    def test_a_json_list_or_string_body_is_a_400_not_a_dead_connection(self):
        for payload in (b'[{"action":"click"}]', b'"click"', b'5', b'null', b'true'):
            code, body = self.raw_post(payload)
            self.assertEqual(code, 400, payload)
            self.assertIn("object", body["error"], payload)

    def test_a_non_numeric_coordinate_is_named_not_crashed(self):
        code, body = self.c.post("/act", {"action": "click", "x": "banana", "y": 3})
        self.assertEqual(code, 400)
        self.assertIn("x", body["error"])

    # ---- F3: a browser on the human's own machine must not be able to drive the PC ----
    def test_a_cross_origin_post_from_a_web_page_is_refused(self):
        """A token in a URL outlives the session: browser history, proxy logs, the phone that opened it.
        A text/plain or form POST is a CORS 'simple request': a page can send it with no preflight, and
        1.0.2 never looked at Content-Type — so any web page could have clicked for you."""
        payload = json.dumps({"action": "click", "x": 9, "y": 9}).encode()
        code, body = self.raw_post(payload, ctype="text/plain",
                                     headers={"Origin": "https://evil.example"})
        self.assertIn(code, (403, 415), "a browser-shaped POST must never reach the OS")
        self.assertIn("error", body)
        code, body = self.raw_post(payload, ctype="application/x-www-form-urlencoded")
        self.assertEqual(code, 415, "no Content-Type a <form> can set is accepted")
        code, body = self.raw_post(payload, ctype="application/json",
                                    headers={"Origin": "https://evil.example"})
        self.assertEqual(code, 403, "even a preflighted cross-origin request is refused outright")
        self.assertIn("cross-origin", body["error"])
        code, body = self.raw_post(payload)                    # a real client says what it sends
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])

    def test_a_non_windows_box_still_answers_instead_of_dropping_the_connection(self):
        """input_space()/cursor_pos() touched ctypes.windll with no guard, so the documented
        'run it on Linux to test the protocol' path raised AttributeError inside the route. That is not a
        500 with a message: the socket closes, and a closed socket is what a dead PC looks like."""
        orig = pceye.SYSTEM
        pceye.SYSTEM = "Windows"                  # the worst case: pretend to be Windows on a Linux box
        try:
            self.assertIsNone(pceye.input_space())
            self.assertIsNone(pceye.cursor_pos())
            code, body = self.c.post("/act", {"action": "paste", "text": "hello"})
            self.assertEqual(code, 409, f"must be a JSON refusal, got {code}")
            self.assertIn("NOT pasted", body["error"])
        finally:
            pceye.SYSTEM = orig

    def test_a_local_page_on_the_same_host_is_still_allowed(self):
        code, body = self.raw_post(json.dumps({"action": "move", "x": 4, "y": 4}).encode(),
                                   headers={"Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(code, 200)

    # ---- F4: dry-run must never be able to look like a real machine ----
    def test_dry_run_is_visible_in_state_act_and_selftest(self):
        """This build can be started --dry-run by a manager to demo the protocol. An agent that was not
        told read 'ok' for every action and wrote a report about a PC that was never touched."""
        _, state, _ = self.c.get("/state")
        self.assertTrue(state["dry_run"])
        _, out = self.c.post("/act", {"action": "click", "x": 1, "y": 1})
        self.assertTrue(out["dry_run"])
        self.assertIn("NOT delivered", out["note"])
        _, st, _ = self.c.get("/selftest")
        self.assertTrue(st["dry_run"])
        self.assertIn("TEST-MODE", [c["name"] for c in st["checks"]])
        self.assertIn("NOTHING is sent", json.dumps(st["checks"]))

    # ---- F5: the log must survive, and must be verified by writing ----
    def test_selftest_proves_the_log_by_writing_it(self):
        """os.access() on a path whose parent directory does not exist returned False, and the old check
        OR-ed in 'not os.path.exists(path)' — so a log that could never be created reported OK."""
        with self.assertRaises(OSError):
            open(os.path.join(self.tmp.name, "nope", "deep", "x.log"), "a")
        bad = os.path.join(self.tmp.name, "nope", "deep", "pceye.log")
        old, pceye.Handler.log_path = pceye.Handler.log_path, bad
        getattr(pceye.Handler, '_close_log', lambda: None)()
        try:
            _, st, _ = self.c.get("/selftest")
            chk = [c for c in st["checks"] if c["name"] == "log-writable"][0]
            self.assertFalse(chk["ok"], "an unwritable log must go red")
        finally:
            pceye.Handler.log_path = old
            getattr(pceye.Handler, '_close_log', lambda: None)()
        pceye.Handler.log_path = os.path.join(self.tmp.name, "r.log")
        _, st, _ = self.c.get("/selftest")
        self.assertTrue([c for c in st["checks"] if c["name"] == "log-writable"][0]["ok"])

    # ---- F6: regions that are not on any screen ----
    def test_an_out_of_screen_region_is_refused_instead_of_arriving_black(self):
        """ImageGrab happily returns black for a box off the edge. An agent reads a black square and
        concludes 'the desktop is empty' — the most expensive kind of wrong."""
        pceye._display_cache.update(rect=[0, 0, 1920, 1080], at=time.time())
        self.addCleanup(pceye._display_cache.update, {"rect": None, "at": 0.0})
        real = pceye.Screen(1440)
        with self.assertRaises(ValueError) as e:
            real.grab(region="5000,5000,200,200")
        self.assertIn("outside the screen", str(e.exception))
        self.assertIn("black", str(e.exception))

    def test_a_second_monitor_at_negative_coordinates_is_not_refused(self):
        """The extended desktop starts at a NEGATIVE origin when a monitor sits left/above the main one.
        A naive 0<=x check would refuse to look at the human's second screen."""
        pceye._display_cache.update(rect=[-1920, 0, 3840, 1080], at=time.time())
        self.addCleanup(pceye._display_cache.update, {"rect": None, "at": 0.0})
        self.assertEqual(pceye.parse_region("-1800,10,100,100"), (-1800, 10, 100, 100))
        real = pceye.Screen(1440)
        try:
            real.grab(region="-1800,10,100,100")     # bounds pass; capture itself will fail headless
        except ValueError as e:
            self.fail(f"a valid extended-desktop region was refused: {e}")
        except RuntimeError:
            pass                                      # no display here — that is the honest failure

    # ---- F7: fields that were accepted and ignored ----
    def test_move_seconds_is_recorded_and_capped(self):
        """PC-EYE.md promised 'ease to the point'; Input.move took `seconds` and teleported. A promised
        behaviour that is not implemented is worse than a missing one, because the agent stops checking."""
        inp = pceye.Input(dry_run=True)
        inp.move(10, 20, seconds=0.6)
        self.assertEqual(inp.did[-1][:3], ("move", 10, 20))
        self.assertEqual(inp.did[-1][3], 0.6, "the ease time must be part of what was done")
        inp.move(1, 1, seconds=3600)
        self.assertEqual(inp.did[-1][3], pceye.MAX_EASE_S, "a sleep must be capped")
        with self.assertRaises(ValueError):
            inp.move(1, 1, seconds=-2)

    def test_drag_releases_on_the_exact_endpoint(self):
        """18 steps with int() truncation used to let go up to a pixel short of x2,y2 — the drag itself
        was the reason /act had an off-by complaint in the field."""
        src = pathlib.Path(pceye.__file__).read_text()
        body = src.split("def drag(")[1]
        self.assertIn("for i in range(1, steps):", body, "the interpolated path must stop short of the end")
        self.assertIn("SetCursorPos(int(x2), int(y2))", body, "and then land exactly before the button-up")

    # ---- F8: unbounded arguments that hold the human's keyboard ----
    def test_type_is_bounded_and_says_why(self):
        code, body = self.c.post("/act", {"action": "type", "text": "x" * (pceye.MAX_TYPE_CHARS + 1)})
        self.assertEqual(code, 409)
        self.assertIn("paste", body["error"])

    def test_key_times_is_bounded(self):
        code, body = self.c.post("/act", {"action": "key", "key": "enter", "times": 100000})
        self.assertEqual(code, 409)
        self.assertIn("times", body["error"])
        with self.assertRaises(ValueError):
            pceye.Input(dry_run=True).key("enter", times="lots")

    def test_paste_is_bounded(self):
        code, body = self.c.post("/act", {"action": "paste", "text": "x" * (pceye.MAX_PASTE_CHARS + 1)})
        self.assertEqual(code, 409)
        self.assertIn("paste of", body["error"])

    # ---- F9: the ok-shaped lie for a paste that did not paste ----
    def test_a_paste_the_clipboard_refused_is_an_error_not_a_200(self):
        """paste() returned False, the route wrapped it as {"pasted": false} and answered 200 {"ok": true}.
        The agent read ok, typed nothing into the form and reported success."""
        inp = pceye.Input(dry_run=False)
        orig = pceye.SYSTEM
        pceye.SYSTEM = "Linux"                       # so paste() raises rather than being skipped
        try:
            with self.assertRaises(RuntimeError):
                inp.paste("text")
        finally:
            pceye.SYSTEM = orig
        orig_sys, orig_clip = pceye.SYSTEM, pceye.Input._clipboard_set_windows
        pceye.SYSTEM = "Windows"
        pceye.Input._clipboard_set_windows = lambda self, text: False   # the clipboard is busy
        try:
            code, body = self.c.post("/act", {"action": "paste", "text": "hello"})
            self.assertEqual(code, 409, "a refused clipboard must not answer 200")
            self.assertFalse(body["ok"])
            self.assertIn("NOT pasted", body["error"])
        finally:
            pceye.SYSTEM, pceye.Input._clipboard_set_windows = orig_sys, orig_clip

    def test_a_failed_paste_never_reports_ok(self):
        src = pathlib.Path(pceye.__file__).read_text()
        route = src.split("if action == \"paste\":")[1].split("except RuntimeError")[0]
        self.assertIn("raise RuntimeError", route, "the route must turn a False paste into an error")
        self.assertNotIn('"pasted": self.inp.paste', route)

    # ---- F10: a rate limiter that blunts a runaway loop without touching a person ----
    def test_a_spinning_agent_is_stopped_and_told_why(self):
        lim = pceye.RateBucket(capacity=3, refill_per_s=0.001)
        self.assertTrue(all([lim.take() for _ in range(3)]))
        self.assertFalse(lim.take(), "the burst must be finite")
        time.sleep(0.05)
        self.assertTrue(pceye.RateBucket(1, 1000).take())
        self.assertTrue(pceye.RateBucket(1, -1).take.__name__ == "take")   # never raises at the caller

    # ---- F11: token strength is the whole defence ----
    def test_the_default_token_has_128_bits(self):
        """`secrets.token_hex(4)` = 32 bits, on an internet-reachable endpoint, with no lockout, for a
        tool that types into the human's windows. The URL is handed out in a chat; 32 bits is a afternoon."""
        src = pathlib.Path(pceye.__file__).read_text()
        self.assertIn("secrets.token_hex(16)", src)
        self.assertNotIn("token_hex(4)", src)
        self.assertIn("if len(token) < 16", src)     # and it warns when a human insists on a weak one

    def test_selftest_grades_the_token_length(self):
        _, st, _ = self.c.get("/selftest")
        tok = [c for c in st["checks"] if c["name"] == "token"][0]
        self.assertEqual(tok["detail"], f"{len(self.token)} chars")
        self.assertTrue(tok["ok"] or len(self.token) < 16)

    # ---- F12: the state route must not capture the screen ----
    def test_the_real_screen_class_asks_the_os_before_it_grabs(self):
        """Patched on the REAL Screen and on the REAL `from PIL import ImageGrab` lookup: a fake that
        already avoided the cost would let this test pass on the broken code — which is how 1.0.2 shipped."""
        import PIL
        calls = {"rect": 0, "grab": 0}

        class CountingImage:
            width, height = 1920, 1080

        class CountingGrab:
            @staticmethod
            def grab(*a, **k):
                calls["grab"] += 1
                return CountingImage()

        def fake_rect(force=False):
            calls["rect"] += 1
            return [0, 0, 1920, 1080]

        real_grab, real_pil = getattr(PIL, "ImageGrab", None), getattr(pceye, "ImageGrab", None)
        PIL.ImageGrab = CountingGrab
        orig_rect, pceye.display_rect = pceye.display_rect, fake_rect
        pceye._display_cache.update(rect=None, at=0.0)
        try:
            self.assertEqual(pceye.Screen(1440).size(), (1920, 1080))
            self.assertEqual(calls["grab"], 0, "geometry came from the OS, not from a capture")
            self.assertGreaterEqual(calls["rect"], 1)
            pceye.display_rect = lambda force=False: None      # nothing to ask: the old path is the fallback
            self.assertEqual(pceye.Screen(1440).size(), (1920, 1080))
            self.assertEqual(calls["grab"], 1, "and it still answers when the OS cannot")
        finally:
            pceye.display_rect = orig_rect
            pceye._display_cache.update(rect=None, at=0.0)
            if real_grab is not None:
                PIL.ImageGrab = real_grab
            else:
                del PIL.ImageGrab

    def test_state_answers_without_touching_the_screen(self):
        """With geometry known, /state must do zero pixel work. On the shipped code /state called
        ImageGrab.grab(all_screens=True) — a full lossless capture — to read a WIDTH."""
        seen = []
        real = pceye.Screen.grab

        def spy(self, *a, **k):
            seen.append(1)
            return real(self, *a, **k)

        orig_screen, pceye.Screen.grab = pceye.Handler.screen, spy
        pceye.Handler.screen = pceye.Screen(1440)
        orig_rect, pceye.Handler.display_rect = pceye.Handler.display_rect, staticmethod(lambda force=False: [0, 0, 1920, 1080])
        try:
            code, body, _ = self.c.get("/state")
            self.assertEqual(code, 200)
            self.assertEqual(body["screen"], [1920, 1080])
            self.assertEqual(len(seen), 0, f"/state performed {len(seen)} screen capture(s)")
        finally:
            pceye.Handler.screen, pceye.Handler.display_rect = orig_screen, orig_rect
            pceye.Screen.grab = real

    def test_display_rect_is_cached_and_expires(self):
        pceye._display_cache.update(rect=[0, 0, 1234, 567], at=time.time())
        self.assertEqual(pceye.display_rect(), [0, 0, 1234, 567], "the cache must be used")
        pceye._display_cache.update(rect=[0, 0, 1234, 567], at=time.time() - pceye.DISPLAY_TTL - 1)
        self.assertNotEqual(pceye.display_rect(), [0, 0, 1234, 567], "a stale cache must be re-asked")
        pceye._display_cache.update(rect=None, at=0.0)

    # ---- F13: watching a long task without re-fetching the screen ----
    def test_a_304_when_nothing_changed_saves_the_whole_frame(self):
        """The loop is look-decide-act-look again. Waiting for a build used to cost a full JPEG per look;
        the hash header lets an agent poll for the price of a request."""
        self.c.get("/shot", raw=True)
        h = self.srv.RequestHandlerClass.screen.calls and pceye.content_hash(PNG_1x1)
        code, body, hdr = self.c.get(f"/shot?changed={h}", raw=True)
        self.assertEqual(code, 304, "same hash => no body, no bytes")
        self.assertEqual(hdr.get("X-Screen-Hash"), h)
        code, body, _ = self.c.get("/shot?changed=deadbeefdeadbeef", raw=True)
        self.assertEqual(code, 200, "a changed screen must still be delivered")

    # ---- F14: the docs and the code must not drift apart ----
    def test_version_in_the_code_matches_the_readme_and_changelog(self):
        readme = pathlib.Path(HERE, "README.md").read_text()
        self.assertIn(pceye.VERSION, readme, "README carried 1.0.2 while server.py said 1.0.0")
        self.assertTrue(os.path.exists(os.path.join(HERE, "CHANGELOG.md")))
        self.assertIn(pceye.VERSION, pathlib.Path(HERE, "CHANGELOG.md").read_text())

    def test_the_method_and_action_tables_still_match(self):
        self.assertEqual(set(pceye.METHOD), set(pceye.ACTIONS), "an action in the schema with no "
                                                                "implementation is the bug the comment "
                                                                "claims a test prevents")
        self.assertEqual(set(pceye.POINTING_ACTIONS) - set(pceye.ACTIONS), set())

    def test_the_docs_describe_only_routes_that_exist(self):
        """PC-EYE.md is the agent's whole instruction set: a route named there that the server does not
        serve sends the agent off to debug its own syntax."""
        doc = pathlib.Path(HERE, "PC-EYE.md").read_text() + pathlib.Path(HERE, "README.md").read_text()
        documented = set(re.findall(r"(?:GET|POST) (/[a-z_]+)", doc))
        served = {"/state", "/shot", "/act", "/selftest", "/favicon.ico", "/"}
        self.assertEqual(documented - served, set(), f"documented but not served: {documented - served}")

    # ---- F15: 304 must not try to send a body (HTTP forbids it) ----
    def test_empty_responses_carry_no_body_bytes(self):
        code, body, _ = self.c.get("/shot?changed=" + pceye.content_hash(PNG_1x1), raw=True)
        self.assertEqual(code, 304)
        self.assertEqual(body, b"")



if __name__ == "__main__":
    unittest.main(verbosity=2)


class Launcher(unittest.TestCase):
    """The launcher is what a human double-clicks, so its failures must be sentences, not tracebacks.

    These are the two failure modes the console could not explain: a port already taken (pc-eye started
    twice, or something else on 8790) and a tunnel binary that arrived as an error page.
    """

    def test_port_collision_moves_and_reports_it(self):
        from launcher import open_socket_on
        a, pa, _ = open_socket_on(43790, tries=1)
        try:
            b, pb, moved = open_socket_on(43790, tries=4)
            try:
                self.assertEqual((pb, moved), (43791, 1), "it must move one port and say so")
            finally:
                b.close()
        finally:
            a.close()

    def test_port_exhaustion_is_an_explanation(self):
        from launcher import open_socket_on
        held = [open_socket_on(43800 + i, tries=1) for i in range(3)]
        try:
            with self.assertRaises(OSError) as e:
                open_socket_on(43800, tries=3)
            self.assertIn("another pc-eye", str(e.exception))
        finally:
            for s_, _, _ in held:
                s_.close()

    def test_a_bogus_tunnel_download_is_refused_before_it_is_executed(self):
        """The download is HTTPS from GitHub with no signature check, so the least we can do is refuse to
        exec an HTML error page. Both failure shapes must be caught: short file, and right-size nonsense."""
        from launcher import validate_binary
        with tempfile.TemporaryDirectory() as td:
            tiny = Path(td) / "cloudflared.exe"
            tiny.write_bytes(b"<html><body>404 Not Found</body></html>")
            ok, why = validate_binary(tiny)
            self.assertFalse(ok, "a 43-byte error page must not be run as an executable")
            self.assertIn("not cloudflared", why)

            wrong_header = Path(td) / "cloudflared-script"
            wrong_header.write_bytes(b"#!/bin/sh\n" + b"\0" * 6_000_000)
            ok2, why2 = validate_binary(wrong_header)
            self.assertFalse(ok2, "a shell script of the right size must not pass as a binary")
            self.assertIn("header", why2)

            good = Path(td) / "cloudflared-ok"
            good.write_bytes((b"MZ" if platform.system() == "Windows" else b"\x7fELF") + b"\0" * 6_000_000)
            ok3, why3 = validate_binary(good)
            self.assertTrue(ok3, f"a plausible binary for this OS must be accepted: {why3}")

    def test_the_build_accepts_a_pre_bound_socket(self):
        """The launcher must not have a window between 'is 8790 free?' and 'I want 8790'."""
        import urllib.request
        from launcher import open_socket_on
        sock, port, _ = open_socket_on(43810, tries=1)
        srv = pceye.build(port, "preboundtoken123", "127.0.0.1", 1440,
                          os.path.join(tempfile.gettempdir(), "pceye-prebound.log"), True, sock=sock)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            time.sleep(0.2)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/state?t=preboundtoken123", timeout=5) as r:
                d = json.loads(r.read())
            self.assertTrue(d["ok"])
            self.assertTrue(d["dry_run"])
        finally:
            srv.shutdown(); srv.server_close(); pceye.Handler._close_log()

    def test_the_launcher_uses_a_128_bit_token_by_default(self):
        src = pathlib.Path(HERE, "launcher.py").read_text()
        self.assertIn("secrets.token_hex(16)", src)
        self.assertNotIn("token_hex(4)", src)
        self.assertIn("tunnel.terminate()", src), "closing the window must close the tunnel too"
