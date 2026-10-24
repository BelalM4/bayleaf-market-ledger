"""
Regression tests for six weaknesses an outside review reproduced. Each test sets up the
reviewer's situation and checks the safe behavior.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import generate_data
import scenarios
from gate import Gate, IntegrityError, NotAllowed
from server import App

DATA = Path(__file__).resolve().parent.parent / "data"


class Base(unittest.TestCase):
    def setUp(self):
        generate_data.main(DATA)
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        shutil.copytree(DATA, self.data)
        self.state = self.tmp / "state" / "gate.json"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def gate(self, reset=False):
        return Gate(self.state, self.data, reset=reset)

    def req(self, g, kind, vendor_id=None):
        return next(r["id"] for r in g.requests if r["kind"] == kind and (vendor_id is None or r["vendor_id"] == vendor_id))

    def line(self, g, vid):
        return next(l for l in g.batch["lines"] if l["vendor_id"] == vid)

    def assert_books_ok(self, g):
        self.assertEqual([c["name"] for c in g.tie_outs() if not c["ok"]], [])


class TestOverlappingHolds(Base):
    """High: two holds on Green Acres let it be paid $441 against $273 owed."""

    def setUp(self):
        super().setUp()
        scenarios.edit_csv(self.data / "bank_change_requests.csv", lambda rows: rows.append(
            {**rows[0], "vendor_id": "V01", "new_bank_last4": "1212"}))

    def test_releasing_both_holds_never_pays_more_than_owed(self):
        g = self.gate(reset=True)
        green = self.line(g, "V01")
        self.assertEqual(green["held"], green["gross"])          # the bank-change hold covers the whole line
        g.decide(self.req(g, "payout_batch"), "approve", "jordan.lee")
        g.decide(self.req(g, "bank_detail_change", "V01"), "reject_request", "jordan.lee", note="Owner says not them")
        green = self.line(g, "V01")
        self.assertEqual(green["paid"], green["gross"] - 168)      # SNAP part paid; the 168 match tokens wait for the recount
        g.decide(self.req(g, "match_float_negative", "V01"), "pay_all_override", "jordan.lee", note="Recounted")
        green = self.line(g, "V01")
        self.assertEqual(green["paid"], green["gross"])           # 273, not 441
        self.assert_books_ok(g)

    def test_verified_only_pays_gross_minus_doubtful(self):
        g = self.gate(reset=True)
        g.decide(self.req(g, "match_float_negative", "V01"), "pay_verified_only", "jordan.lee")
        self.assertEqual(self.line(g, "V01")["pay_now"], 0)      # bank hold still blocks it
        g.decide(self.req(g, "bank_detail_change", "V01"), "reject_request", "jordan.lee", note="Not them")
        g.decide(self.req(g, "payout_batch"), "approve", "jordan.lee")
        green = self.line(g, "V01")
        self.assertEqual(green["paid"], green["gross"] - 22)
        self.assert_books_ok(g)


class TestSignIn(Base):
    """High: anyone could send actor=jordan.lee and approve without signing in."""

    def test_decide_without_a_session_is_refused(self):
        app = App(self.state, self.data)
        status, r, _ = app.handle("POST", "/api/decide", {"request": "REQ-01", "option": "approve", "actor": "jordan.lee"})
        self.assertEqual(status, 401)
        self.assertEqual(app.gate.batch["status"], "awaiting_approval")

    def test_wrong_pin_is_refused(self):
        app = App(self.state, self.data)
        status, _, headers = app.handle("POST", "/api/login", {"user": "jordan.lee", "pin": "0000"})
        self.assertEqual((status, headers), (401, {}))

    def test_identity_comes_from_the_session_not_the_body(self):
        app = App(self.state, self.data)
        _, _, headers = app.handle("POST", "/api/login", {"user": "priya.shah", "pin": "1357"})
        token = headers["Set-Cookie"].split("=", 1)[1].split(";")[0]
        status, r, _ = app.handle("POST", "/api/decide", {"request": "REQ-01", "option": "approve", "actor": "jordan.lee",
                                                          "run_id": app.gate.run_id}, token=token)
        self.assertEqual(status, 403)                              # Priya is signed in; claiming to be Jordan doesn't work
        self.assertIn("Priya", r["error"])

    def test_agent_cannot_sign_in(self):
        app = App(self.state, self.data)
        self.assertEqual(app.handle("POST", "/api/login", {"user": "agent", "pin": ""})[0], 401)


class TestSourcesChange(Base):
    """High: editing the records and restarting mixed new findings with old approvals."""

    def test_old_approvals_are_superseded_not_mixed(self):
        g = self.gate(reset=True)
        g.decide(self.req(g, "match_float_negative"), "pay_verified_only", "jordan.lee")
        scenarios.honest_green_acres(self.data)
        g2 = self.gate()
        self.assertNotEqual(g2.run_id, g.run_id)
        self.assertTrue(all(r["status"] == "pending" for r in g2.requests))
        self.assertTrue(all(l["held"] >= 0 for l in g2.batch["lines"]))
        self.assertTrue(any(e["step"] == "superseded" for e in g2.audit))
        self.assertTrue(any((self.state.parent / "history").iterdir()))
        self.assert_books_ok(g2)

    def test_same_sources_keep_the_same_run(self):
        g = self.gate(reset=True)
        g.decide(self.req(g, "payout_batch"), "approve", "jordan.lee")
        g2 = self.gate()
        self.assertEqual((g2.run_id, g2.batch["status"]), (g.run_id, "approved"))


class TestBooksMustTieOut(Base):
    """High: a $10 change to a payment amount wasn't flagged, and approvals still went through."""

    def setUp(self):
        super().setUp()

        def change(rows):
            for r in rows:
                if r["reference"] == "ACH1006V03":
                    r["amount"] = f"{float(r['amount']) - 10:.2f}"
        scenarios.edit_csv(self.data / "bank_statement.csv", change)

    def test_amount_mismatch_is_flagged(self):
        g = self.gate(reset=True)
        types = {f["type"] for f in g.run["findings"]}
        self.assertIn("payment_amount_mismatch", types)
        self.assertIn("bank_balance_mismatch", types)

    def test_approvals_blocked_while_books_dont_tie_out(self):
        g = self.gate(reset=True)
        with self.assertRaisesRegex(NotAllowed, "tie out"):
            g.decide(self.req(g, "payout_batch"), "approve", "jordan.lee")
        self.assertEqual(sum(l["paid"] for l in g.batch["lines"]), 0)


class TestAttendanceConfirmed(Base):
    """Medium: confirming an unpaid vendor's attendance left its money stuck on hold."""

    def test_confirming_attendance_releases_the_hold(self):
        scenarios.edit_csv(self.data / "attendance.csv", lambda rows: [
            {**r, "present": "False"} if (r["market_date"], r["vendor_id"]) == ("2026-10-17", "V02") else r for r in rows])
        g = self.gate(reset=True)
        sunny = self.line(g, "V02")
        self.assertEqual(sunny["held"], sunny["gross"])
        g.decide(self.req(g, "redemption_while_absent", "V02"), "attendance_was_wrong", "jordan.lee",
                 note="Saw them there; log was wrong")
        sunny = self.line(g, "V02")
        self.assertEqual((sunny["held"], sunny["pay_now"]), (0, sunny["gross"]))
        self.assert_books_ok(g)


class TestAuditIntegrity(Base):
    """Medium: deleting the saved audit trail was silently accepted on restart."""

    def test_deleted_state_is_refused_not_restarted(self):
        g = self.gate(reset=True)
        g.decide(self.req(g, "payout_batch"), "approve", "jordan.lee")
        self.state.unlink()
        with self.assertRaises(IntegrityError):
            self.gate()

    def test_deleted_audit_log_is_refused(self):
        g = self.gate(reset=True)
        g.audit_path.unlink()
        with self.assertRaises(IntegrityError):
            self.gate()

    def test_edited_audit_entry_is_detected(self):
        g = self.gate(reset=True)
        g.decide(self.req(g, "payout_batch"), "approve", "jordan.lee")
        lines = g.audit_path.read_text().splitlines()
        entry = json.loads(lines[-1])
        entry["actor"] = "priya.shah"
        lines[-1] = json.dumps(entry)
        g.audit_path.write_text("\n".join(lines) + "\n")
        with self.assertRaises(IntegrityError):
            self.gate()

    def test_reset_archives_instead_of_deleting(self):
        g = self.gate(reset=True)
        g.decide(self.req(g, "payout_batch"), "approve", "jordan.lee")
        self.gate(reset=True)
        archived = list((self.state.parent / "history").glob("*/audit.jsonl"))
        self.assertTrue(archived)
        self.assertIn("decide", archived[0].read_text())


if __name__ == "__main__":
    unittest.main()


# ---------- second review ----------

class TestPaymentsSurviveANewRun(Base):
    """High: approve $470, correct a source, restart, approve again -> $940 recorded paid."""

    def test_payments_carry_over_and_are_not_repeated(self):
        g = self.gate(reset=True)
        g.decide(self.req(g, "payout_batch"), "approve", "jordan.lee")
        scenarios.edit_csv(self.data / "vendors.csv", lambda rows: [
            {**r, "sells": "candles and soy crafts"} if r["vendor_id"] == "V08" else r for r in rows])
        g2 = self.gate()
        self.assertNotEqual(g2.run_id, g.run_id)
        self.assertEqual(sum(l["paid"] for l in g2.batch["lines"]), 470)       # remembered, not forgotten
        g2.decide(self.req(g2, "payout_batch"), "approve", "jordan.lee")
        self.assertEqual(sum(l["paid"] for l in g2.batch["lines"]), 470)       # not 940
        self.assertEqual(sum(p["amount"] for p in g2.sent), 470)
        self.assert_books_ok(g2)


class TestStaleScreens(Base):
    """High: a decision made on a screen for an older run was accepted."""

    def test_decision_for_another_run_is_refused(self):
        g = self.gate(reset=True)
        with self.assertRaisesRegex(NotAllowed, "out of date"):
            g.decide(self.req(g, "payout_batch"), "approve", "jordan.lee", run_id="RUN-old")
        self.assertEqual(g.batch["status"], "awaiting_approval")

    def test_server_requires_the_current_run_id(self):
        app = App(self.state, self.data)
        _, _, headers = app.handle("POST", "/api/login", {"user": "jordan.lee", "pin": "2468"})
        token = headers["Set-Cookie"].split("=", 1)[1].split(";")[0]
        body = {"request": "REQ-01", "option": "approve"}
        self.assertEqual(app.handle("POST", "/api/decide", body, token=token)[0], 409)
        self.assertEqual(app.handle("POST", "/api/decide", {**body, "run_id": "RUN-old"}, token=token)[0], 409)
        self.assertEqual(app.handle("POST", "/api/decide", {**body, "run_id": app.gate.run_id}, token=token)[0], 200)


class TestSavedStateTampering(Base):
    """Medium: editing the saved account override redirected a $191 payment unnoticed."""

    def test_edited_state_file_is_detected(self):
        g = self.gate(reset=True)
        s = json.loads(self.state.read_text())
        s["batch"]["lines"][1]["pay_to_account"] = "6666"
        self.state.write_text(json.dumps(s, indent=1))
        with self.assertRaisesRegex(IntegrityError, "saved state"):
            self.gate()

    def test_payment_destination_is_checked(self):
        g = self.gate(reset=True)
        self.line(g, "V02")["pay_to_account"] = "6666"           # changed in memory, bypassing any decision
        with self.assertRaisesRegex(NotAllowed, "tie out"):
            g.decide(self.req(g, "payout_batch"), "approve", "jordan.lee")
        self.assertEqual(sum(p["amount"] for p in g.sent), 0)


class TestRollback(Base):
    """Medium: a posting failure mid-decision left $296 partly recorded in memory."""

    def test_failure_while_posting_rolls_everything_back(self):
        g = self.gate(reset=True)
        before = (len(g.ledger.entries), json.dumps(g.batch, sort_keys=True), list(g.sent))
        real_post, calls = g.ledger.post, []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError("disk full")
            return real_post(*args, **kwargs)
        g.ledger.post = flaky
        with self.assertRaisesRegex(NotAllowed, "rolled back"):
            g.decide(self.req(g, "payout_batch"), "approve", "jordan.lee")
        self.assertEqual((len(g.ledger.entries), json.dumps(g.batch, sort_keys=True), list(g.sent)), before)
        self.assertEqual(self.gate().batch["status"], "awaiting_approval")
        g.ledger.post = real_post
        g.decide(self.req(g, "payout_batch"), "approve", "jordan.lee")    # and it works once the fault is gone
        self.assert_books_ok(g)
