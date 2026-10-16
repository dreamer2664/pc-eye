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
import socket
import sys
import tempfile
import threading
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

    def grab(self, region=None, max_width=None, fmt="jpeg", quality=None):
        region = pceye.parse_region(region)      # same parser as the real Screen — a fake that skips
        self.calls.append({"region": region, "max_width": max_width, "fmt": fmt})   # it is a mirror
        if region:
            return PNG_1x1, {"full": [1920, 1080], "sent": [1, 1], "scaled": True, "bytes": len(PNG_1x1),
                             "mime": "image/png", "format": "png", "at": "now"}
        return PNG_1x1, {"full": [1920, 1080], "sent": [1440, 810], "scaled": True, "bytes": len(PNG_1x1),
                         "mime": "image/jpeg", "format": fmt, "at": "now"}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


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
                                     headers={"Content-Type": "application/json"})
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
        cls.srv.shutdown()
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
        self.srv.shutdown(); self.tmp.cleanup()
        pceye.input_space, pceye.cursor_pos = self._space, self._cursor

    def test_a_coordinate_the_hand_cannot_reach_is_refused_not_clamped(self):
        pceye.input_space = lambda: [1536, 864]          # the CarloPC situation
        code, body = self.c.post("/act", {"action": "click", "x": 1800, "y": 933})
        self.assertEqual(code, 409, "a clamped click must never be reported as ok")
        self.assertIn("1536x864", body["error"])
        self.assertIn("coord_space", body["error"])

    def test_a_coordinate_inside_the_space_is_allowed(self):
        pceye.input_space = lambda: [1536, 864]
        code, body = self.c.post("/act", {"action": "move", "x": 900, "y": 400})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])

    def test_a_click_that_lands_elsewhere_says_so_instead_of_ok(self):
        pceye.input_space = lambda: [1920, 1080]
        pceye.cursor_pos = lambda: [1535, 863]           # the pointer did not go where it was told
        code, body = self.c.post("/act", {"action": "click", "x": 1800, "y": 933})
        self.assertEqual(code, 200)
        self.assertFalse(body["exact"])
        self.assertEqual(body["landed"], [1535, 863])
        self.assertIn("NOT having done", body["warning"])

    def test_a_click_that_lands_where_asked_is_reported_exact(self):
        pceye.input_space = lambda: [1920, 1080]
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
