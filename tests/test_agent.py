"""
Tests for the agent.

The first group runs the agent on the standard data and grades it with the answer key.
The second group edits a copy of the data, the way a judge might ("what if...?"),
to show the agent reasons from the records instead of memorizing the answers.
"""
import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import agent
import generate_data
import score

DATA = Path(__file__).resolve().parent.parent / "data"


def edit_csv(path, change):
    """Read a CSV, let `change(rows)` modify the list, write it back."""
    with open(path) as f:
        reader = csv.DictReader(f)
        fields, rows = reader.fieldnames, list(reader)
    rows = change(rows) or rows
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


class TestAgentOnStandardData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        generate_data.main(DATA)
        cls.result = score.main(DATA)

    def test_catches_every_planted_mistake(self):
        self.assertEqual(self.result["caught"], self.result["planted"])

    def test_chooses_the_right_action_every_time(self):
        self.assertEqual(self.result["right_action"], self.result["planted"])

    def test_no_false_alarms_on_the_traps(self):
        self.assertEqual(self.result["false_alarms"], [])

    def test_payment_batch_exact_and_waiting_for_approval(self):
        self.assertTrue(self.result["batch_exact"], self.result["batch_errors"])
        self.assertTrue(self.result["batch_awaiting_approval"])

    def test_grant_warning(self):
        self.assertTrue(self.result["warnings_ok"])

    def test_every_finding_has_evidence_and_a_rule(self):
        run = agent.Agent(DATA).run()
        for f in run["findings"]:
            self.assertTrue(f["evidence"], f["id"])
            self.assertTrue(f["rule"], f["id"])
            for e in f["evidence"]:
                self.assertIn(e["file"], agent.ALLOWED_SOURCES)
                self.assertGreater(e["line"], 1)

    def test_agent_cannot_read_the_answer_key(self):
        with self.assertRaises(agent.AccessDenied):
            agent.Sources(DATA).json("answer_key.json")

    def test_held_vendors_paid_to_account_on_file_not_the_emailed_one(self):
        run = agent.Agent(DATA).run()
        honeycomb = next(l for l in run["payout_batch"]["lines"] if l["vendor_id"] == "V05")
        self.assertEqual(honeycomb["pay_to_account"], "2290")
        self.assertEqual(honeycomb["pay_now"], 0)


class TestAgentOnChangedData(unittest.TestCase):
    """Each test changes one thing in a copy of the data and checks the agent notices."""

    def setUp(self):
        generate_data.main(DATA)
        self.tmp = Path(tempfile.mkdtemp())
        for f in DATA.iterdir():
            shutil.copy(f, self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_agent(self):
        return agent.Agent(self.tmp).run()

    def types(self, run):
        return [(f["type"], f.get("market_date"), f.get("vendor_id")) for f in run["findings"]]

    def test_if_the_late_deposit_never_came_sep_19_becomes_missing(self):
        edit_csv(self.tmp / "bank_statement.csv", lambda rows: [r for r in rows if r["reference"] != "EBT-0919-L1"])
        t = self.types(self.run_agent())
        self.assertIn(("missing_settlement", "2026-09-19", None), t)
        self.assertNotIn(("late_settlement", "2026-09-19", None), t)

    def test_if_green_acres_count_is_honest_nothing_is_held(self):
        def fix(rows):
            for r in rows:
                if r["market_date"] == "2026-10-17" and r["vendor_id"] == "V01":
                    r["match_tokens"] = str(int(r["match_tokens"]) - 40)
        edit_csv(self.tmp / "token_redemptions.csv", fix)
        run = self.run_agent()
        self.assertFalse(any(f["type"] == "match_float_negative" for f in run["findings"]))
        green = next(l for l in run["payout_batch"]["lines"] if l["vendor_id"] == "V01")
        self.assertEqual(green["held"], 0)

    def test_a_second_bank_change_email_holds_that_vendor_too(self):
        def add(rows):
            rows.append({**rows[0], "vendor_id": "V02", "new_bank_last4": "1111"})
        edit_csv(self.tmp / "bank_change_requests.csv", add)
        run = self.run_agent()
        sunny = next(l for l in run["payout_batch"]["lines"] if l["vendor_id"] == "V02")
        self.assertEqual(sunny["pay_now"], 0)

    def test_a_new_over_cap_shopper_is_caught(self):
        def add(rows):
            base = next(r for r in rows if r["market_date"] == "2026-10-03")
            rows.append({**base, "txn_id": "T1003-901", "time": "12:59", "match_tokens_given": "10", "snap_amount": "10"})
        edit_csv(self.tmp / "booth_terminal_log.csv", add)
        run = self.run_agent()
        self.assertTrue(any(f["type"] == "match_over_cap" and f["market_date"] == "2026-10-03" for f in run["findings"]))

    def test_candles_turning_in_match_tokens_is_held(self):
        def add(rows):
            rows.append({"market_date": "2026-10-17", "vendor_id": "V08", "snap_tokens": "0",
                         "match_tokens": "6", "counted_by": "Dana"})
        edit_csv(self.tmp / "token_redemptions.csv", add)
        edit_csv(self.tmp / "attendance.csv", lambda rows: [
            {**r, "present": "True"} if (r["market_date"], r["vendor_id"]) == ("2026-10-17", "V08") else r for r in rows])
        run = self.run_agent()
        candles = next(l for l in run["payout_batch"]["lines"] if l["vendor_id"] == "V08")
        self.assertEqual(candles["held"], 6)
        self.assertIn(("ineligible_vendor_match", "2026-10-17", "V08"), self.types(run))

    def test_two_inflated_counts_both_held(self):
        import scenarios
        scenarios.change_count(self.tmp, vendor_id="V02", market_date="2026-10-17", kind="match", delta=70)
        run = self.run_agent()
        floats = {f["vendor_id"]: f for f in run["findings"] if f["type"] == "match_float_negative"}
        self.assertEqual(set(floats), {"V01", "V02"})
        self.assertEqual(sum(f["shortfall"] for f in floats.values()), 92)

    def test_inflated_snap_count_goes_to_suspense_and_books_tie_out(self):
        import books
        import scenarios
        scenarios.change_count(self.tmp, vendor_id="V04", market_date="2026-10-17", kind="snap", delta=60)
        a = agent.Agent(self.tmp)
        run = a.run()
        self.assertIn(("snap_float_negative", "2026-10-17", "V04"), self.types(run))
        led = books.build(a)
        self.assertEqual([c["name"] for c in books.tie_outs(a, led) if not c["ok"]], [])

    def test_payment_missing_from_bank_is_flagged(self):
        edit_csv(self.tmp / "bank_statement.csv", lambda rows: [r for r in rows if r["reference"] != "ACH1006V03"])
        self.assertIn(("payment_not_in_bank", "2026-10-03", "V03"), self.types(self.run_agent()))


if __name__ == "__main__":
    unittest.main()
