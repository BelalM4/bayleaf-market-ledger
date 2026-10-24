"""
Tests for the web app's API (called directly, without opening a network port).
"""
import json
import tempfile
import unittest
from pathlib import Path

import generate_data
from server import App

DATA = Path(__file__).resolve().parent.parent / "data"


class TestServer(unittest.TestCase):
    def setUp(self):
        generate_data.main(DATA)
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(Path(self.tmp.name) / "gate.json", DATA)

    def login(self, user="jordan.lee", pin="2468"):
        status, _, headers = self.app.handle("POST", "/api/login", {"user": user, "pin": pin})
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split("=", 1)[1].split(";")[0]

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_has_everything_the_page_shows(self):
        status, s, _ = self.app.handle("GET", "/api/state", {})
        self.assertEqual(status, 200)
        for k in ("findings", "requests", "batch", "checks", "trial_balance", "entries", "audit", "score", "scenarios"):
            self.assertIn(k, s)
        self.assertTrue(s["all_tie_outs_ok"])
        self.assertEqual(s["score"]["caught"], 9)

    def test_agent_refused_with_403(self):
        status, r, _ = self.app.handle("POST", "/api/agent_attempt", {"request": "REQ-01"})
        self.assertEqual(status, 403)
        self.assertIn("Segregation of duties", r["error"])

    def test_manager_decision_updates_state(self):
        status, r, _ = self.app.handle("POST", "/api/decide", {"request": "REQ-01", "option": "approve",
                                                                 "run_id": self.app.gate.run_id}, token=self.login())
        self.assertEqual(status, 200)
        self.assertEqual(r["request"]["decision"]["by"], "jordan.lee")
        self.assertEqual(r["state"]["batch"]["status"], "approved")
        self.assertTrue(r["state"]["all_tie_outs_ok"])

    def test_scenario_does_not_touch_real_books(self):
        before = len(self.app.handle("GET", "/api/state", {})[1]["entries"])
        status, r, _ = self.app.handle("POST", "/api/scenario", {"name": "late_deposit_never_came"})
        self.assertEqual(status, 200)
        self.assertEqual(r["result"]["new_findings"][0]["type"], "missing_settlement")
        self.assertEqual(len(self.app.handle("GET", "/api/state", {})[1]["entries"]), before)

    def test_state_never_exposes_pin_hashes(self):
        self.assertNotIn("pin_hash", json.dumps(self.app.handle("GET", "/api/state", {})[1]))

    def test_reset_needs_a_signed_in_manager(self):
        self.assertEqual(self.app.handle("POST", "/api/reset", {})[0], 401)
        self.assertEqual(self.app.handle("POST", "/api/reset", {}, token=self.login("priya.shah", "1357"))[0], 401)
        self.assertEqual(self.app.handle("POST", "/api/reset", {}, token=self.login())[0], 200)

    def test_unknown_routes(self):
        self.assertEqual(self.app.handle("GET", "/api/nope", {})[0], 404)
        self.assertEqual(self.app.handle("POST", "/api/scenario", {"name": "nope"})[0], 400)


if __name__ == "__main__":
    unittest.main()
