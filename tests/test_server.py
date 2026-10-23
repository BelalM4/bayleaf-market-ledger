"""
Tests for the web app's API (called directly, without opening a network port).
"""
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
        self.app.handle("POST", "/api/reset", {})

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_has_everything_the_page_shows(self):
        status, s = self.app.handle("GET", "/api/state", {})
        self.assertEqual(status, 200)
        for k in ("findings", "requests", "batch", "checks", "trial_balance", "entries", "audit", "score", "scenarios"):
            self.assertIn(k, s)
        self.assertTrue(s["all_tie_outs_ok"])
        self.assertEqual(s["score"]["caught"], 9)

    def test_agent_refused_with_403(self):
        status, r = self.app.handle("POST", "/api/decide", {"request": "REQ-01", "option": "approve", "actor": "agent"})
        self.assertEqual(status, 403)
        self.assertIn("Segregation of duties", r["error"])

    def test_manager_decision_updates_state(self):
        status, r = self.app.handle("POST", "/api/decide", {"request": "REQ-01", "option": "approve", "actor": "jordan.lee"})
        self.assertEqual(status, 200)
        self.assertEqual(r["state"]["batch"]["status"], "approved")
        self.assertTrue(r["state"]["all_tie_outs_ok"])

    def test_scenario_does_not_touch_real_books(self):
        before = len(self.app.handle("GET", "/api/state", {})[1]["entries"])
        status, r = self.app.handle("POST", "/api/scenario", {"name": "late_deposit_never_came"})
        self.assertEqual(status, 200)
        self.assertEqual(r["result"]["new_findings"][0]["type"], "missing_settlement")
        self.assertEqual(len(self.app.handle("GET", "/api/state", {})[1]["entries"]), before)

    def test_unknown_routes(self):
        self.assertEqual(self.app.handle("GET", "/api/nope", {})[0], 404)
        self.assertEqual(self.app.handle("POST", "/api/scenario", {"name": "nope"})[0], 400)


if __name__ == "__main__":
    unittest.main()
