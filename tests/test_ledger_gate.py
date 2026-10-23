"""
Tests for the double-entry ledger, the books built from the records, and the approval gate.
"""
import tempfile
import unittest
from pathlib import Path

import agent
import books
import generate_data
from gate import Gate, NotAllowed
from ledger import BadLine, Ledger, PeriodLocked, Unbalanced, UnknownAccount, cr, dr

DATA = Path(__file__).resolve().parent.parent / "data"
ACCOUNTS = [{"code": "1000", "name": "Cash", "type": "asset"},
            {"code": "2100", "name": "Vendor Payable", "type": "liability"}]


class TestLedgerRules(unittest.TestCase):
    def setUp(self):
        self.led = Ledger(ACCOUNTS, locked_through="2026-09-30")

    def test_balanced_entry_posts(self):
        e = self.led.post("2026-10-19", "pay vendor", [dr("2100", 50), cr("1000", 50)], {}, "agent")
        self.assertEqual(e["id"], "JE-0001")
        self.assertEqual(self.led.balance("1000"), -50)
        self.assertTrue(self.led.is_balanced())

    def test_unbalanced_entry_refused(self):
        with self.assertRaises(Unbalanced):
            self.led.post("2026-10-19", "oops", [dr("2100", 50), cr("1000", 49)], {}, "agent")
        self.assertEqual(self.led.entries, [])

    def test_unknown_account_refused(self):
        with self.assertRaises(UnknownAccount):
            self.led.post("2026-10-19", "oops", [dr("9999", 5), cr("1000", 5)], {}, "agent")

    def test_bad_lines_refused(self):
        with self.assertRaises(BadLine):
            self.led.post("2026-10-19", "one line", [dr("1000", 5)], {}, "agent")
        with self.assertRaises(BadLine):
            self.led.post("2026-10-19", "negative", [dr("1000", -5), cr("2100", -5)], {}, "agent")
        both = {"account": "1000", "debit": 500, "credit": 500, "vendor_id": None}
        with self.assertRaises(BadLine):
            self.led.post("2026-10-19", "both sides", [both, dr("2100", 5), cr("1000", 5)], {}, "agent")

    def test_closed_period_refused(self):
        with self.assertRaises(PeriodLocked):
            self.led.post("2026-09-05", "backdated", [dr("2100", 5), cr("1000", 5)], {}, "agent")

    def test_save_and_reload_replays_every_entry(self):
        self.led.post("2026-10-19", "pay", [dr("2100", 7), cr("1000", 7)], {}, "agent")
        again = Ledger.from_dict(self.led.to_dict())
        self.assertEqual(again.balance("1000"), -7)
        tampered = self.led.to_dict()
        tampered["entries"][0]["lines"][0]["debit"] = 800
        with self.assertRaises(Unbalanced):
            Ledger.from_dict(tampered)


class TestBooks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        generate_data.main(DATA)
        cls.a = agent.Agent(DATA)
        cls.a.run()
        cls.led = books.build(cls.a)

    def test_books_balance(self):
        self.assertTrue(self.led.is_balanced())

    def test_every_tie_out_passes(self):
        failed = [c for c in books.tie_outs(self.a, self.led) if not c["ok"]]
        self.assertEqual(failed, [])

    def test_open_items_show_up_in_balances(self):
        self.assertEqual(self.led.balance("1100"), 25)      # P3: the $25 swipe the bank never sent
        self.assertEqual(self.led.balance("1400"), 202)     # P8: the duplicate payment to recover
        self.assertEqual(self.led.balance("1900"), 22)      # P7: tokens that can't exist
        self.assertEqual(self.led.balance("2110"), 207)     # held payments

    def test_every_entry_points_to_its_source(self):
        for e in self.led.entries:
            self.assertTrue(e["source"].get("file"), e["memo"])

    def test_september_is_closed(self):
        with self.assertRaises(PeriodLocked):
            self.led.post("2026-09-30", "late fix", [dr("1000", 1), cr("2100", 1)], {}, "agent")


class TestApprovalGate(unittest.TestCase):
    def setUp(self):
        generate_data.main(DATA)
        self.tmp = tempfile.TemporaryDirectory()
        self.gate = Gate(Path(self.tmp.name) / "gate.json", DATA, reset=True)

    def tearDown(self):
        self.tmp.cleanup()

    def req(self, kind):
        return next(r for r in self.gate.requests if r["kind"] == kind)["id"]

    def assert_books_ok(self):
        self.assertEqual([c["name"] for c in self.gate.tie_outs() if not c["ok"]], [])

    def test_agent_cannot_approve_its_own_request(self):
        with self.assertRaisesRegex(NotAllowed, "Segregation of duties"):
            self.gate.decide(self.req("payout_batch"), "approve", "agent")
        self.assertEqual(self.gate.batch["status"], "awaiting_approval")

    def test_volunteer_cannot_approve(self):
        with self.assertRaisesRegex(NotAllowed, "volunteer"):
            self.gate.decide(self.req("payout_batch"), "approve", "priya.shah")

    def test_unknown_user_cannot_approve(self):
        with self.assertRaises(NotAllowed):
            self.gate.decide(self.req("payout_batch"), "approve", "sam.ortiz")

    def test_refusals_are_written_to_the_audit_trail(self):
        with self.assertRaises(NotAllowed):
            self.gate.decide(self.req("payout_batch"), "approve", "agent")
        self.assertEqual(self.gate.audit[-1]["step"], "refused")

    def test_manager_approves_batch_and_payments_post(self):
        self.gate.decide(self.req("payout_batch"), "approve", "jordan.lee")
        self.assertEqual(self.gate.ledger.balance("2100"), 0)
        self.assertEqual(sum(l["paid"] for l in self.gate.batch["lines"]), 470)
        self.assertTrue(all(e["approved_by"] == "jordan.lee" for e in self.gate.ledger.entries[-5:]))
        self.assert_books_ok()

    def test_cannot_decide_twice(self):
        rid = self.req("payout_batch")
        self.gate.decide(rid, "approve", "jordan.lee")
        with self.assertRaisesRegex(NotAllowed, "already decided"):
            self.gate.decide(rid, "approve", "jordan.lee")

    def test_override_needs_a_note(self):
        rid = self.req("match_float_negative")
        with self.assertRaisesRegex(NotAllowed, "needs a note"):
            self.gate.decide(rid, "pay_all_override", "jordan.lee")
        self.gate.decide(rid, "pay_all_override", "jordan.lee", note="Recounted with Priya; tokens look real")
        self.assertEqual(self.gate.ledger.balance("5010"), 22)       # the market eats the loss, visibly
        self.assert_books_ok()

    def test_pay_verified_only_pays_what_can_exist(self):
        self.gate.decide(self.req("match_float_negative"), "pay_verified_only", "jordan.lee")
        green = next(l for l in self.gate.batch["lines"] if l["vendor_id"] == "V01")
        self.assertEqual((green["held"], green["forfeited"]), (0, 22))
        self.assertEqual(self.gate.ledger.balance("1900"), 0)
        self.assert_books_ok()

    def test_hold_released_after_batch_approval_is_paid_separately(self):
        self.gate.decide(self.req("payout_batch"), "approve", "jordan.lee")
        self.gate.decide(self.req("bank_detail_change"), "reject_request", "jordan.lee", note="Owner says they didn't send it")
        honey = next(l for l in self.gate.batch["lines"] if l["vendor_id"] == "V05")
        self.assertEqual((honey["paid"], honey["pay_to_account"]), (27, "2290"))
        self.assert_books_ok()

    def test_verified_bank_change_pays_new_account(self):
        self.gate.decide(self.req("bank_detail_change"), "verified_by_phone", "jordan.lee", note="Called number on file")
        self.gate.decide(self.req("payout_batch"), "approve", "jordan.lee")
        honey = next(l for l in self.gate.batch["lines"] if l["vendor_id"] == "V05")
        self.assertEqual((honey["paid"], honey["pay_to_account"]), (27, "0067"))

    def test_correction_for_closed_month_is_dated_today(self):
        self.gate.decide(self.req("redemption_while_absent"), "recover_from_vendor", "jordan.lee")
        e = self.gate.ledger.entries[-1]
        self.assertEqual(e["date"], self.gate.config["today"])
        self.assertIn("closed", e["memo"])
        self.assert_books_ok()

    def test_every_decision_keeps_the_books_right(self):
        choices = {"match_over_cap": ("write_off", ""), "redemption_while_absent": ("recover_from_vendor", ""),
                   "ineligible_vendor_snap": ("refuse", ""), "match_float_negative": ("pay_verified_only", ""),
                   "bank_detail_change": ("verified_by_phone", "Called"), "payout_batch": ("approve", "")}
        for r in list(self.gate.requests):
            option, note = choices[r["kind"]]
            self.gate.decide(r["id"], option, "jordan.lee", note)
            self.assert_books_ok()
        self.assertTrue(self.gate.ledger.is_balanced())

    def test_decisions_survive_a_restart(self):
        self.gate.decide(self.req("payout_batch"), "approve", "jordan.lee")
        again = Gate(self.gate.path, DATA)
        self.assertEqual(again.batch["status"], "approved")
        self.assertEqual(len(again.ledger.entries), len(self.gate.ledger.entries))


if __name__ == "__main__":
    unittest.main()
