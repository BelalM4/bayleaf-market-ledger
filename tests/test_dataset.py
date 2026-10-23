"""
Proves the answer key tells the truth.

These tests re-read the raw CSV files (not the generator's internals) and check
that every planted mistake really is in the data, that every trap really is
harmless, and that the rest of the data is clean. Run:

    python3 -m unittest -v
"""
import csv
import json
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

import generate_data

DATA = Path(__file__).resolve().parent.parent / "data"


def rows(name):
    with open(DATA / name) as f:
        return list(csv.DictReader(f))


def dollars(s):
    return round(float(s))


class TestDataset(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        generate_data.main(DATA)
        cls.key = json.loads((DATA / "answer_key.json").read_text())
        cls.planted = {p["id"]: p for p in cls.key["planted"]}
        cls.vendors = {v["vendor_id"]: v for v in rows("vendors.csv")}
        cls.terminal_raw = rows("booth_terminal_log.csv")
        # the unique swipes (P1 aside, txn_id identifies a swipe)
        seen = {}
        for t in cls.terminal_raw:
            seen.setdefault(t["txn_id"], t)
        cls.terminal = list(seen.values())
        cls.redemptions = rows("token_redemptions.csv")
        cls.attendance = {(a["market_date"], a["vendor_id"]): a["present"] == "True" for a in rows("attendance.csv")}
        cls.bank = rows("bank_statement.csv")
        cls.config = json.loads((DATA / "market_config.json").read_text())

    def test_same_seed_same_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            generate_data.main(tmp)
            for f in DATA.iterdir():
                self.assertEqual(f.read_text(), (Path(tmp) / f.name).read_text(), f.name)

    # ---------- planted mistakes ----------

    def test_p1_exactly_one_duplicated_terminal_row(self):
        counts = defaultdict(int)
        for t in self.terminal_raw:
            counts[t["txn_id"]] += 1
        dupes = [txn for txn, n in counts.items() if n > 1]
        self.assertEqual(dupes, [self.planted["P1"]["where"]["txn_id"]])

    def test_settlements_match_except_p2_and_p3(self):
        by_day = defaultdict(int)
        for t in self.terminal:
            by_day[t["market_date"]] += int(t["snap_amount"])
        deposits = {b["reference"]: dollars(b["amount"]) for b in self.bank if b["reference"].startswith("EBT-")}
        late, missing = self.planted["P2"], self.planted["P3"]
        for day, expected in by_day.items():
            got = deposits[f"EBT-{day[5:7]}{day[8:10]}"]
            if day == "2026-09-19":
                self.assertEqual(expected - got, late["amount"])
            elif day == "2026-10-10":
                self.assertEqual(expected - got, missing["amount"])
            else:
                self.assertEqual(expected, got, day)
        self.assertEqual(deposits["EBT-0919-L1"], late["amount"])

    def test_p4_is_the_only_match_cap_violation_and_t3_is_under_cap(self):
        per_shopper_day = defaultdict(int)
        for t in self.terminal:
            per_shopper_day[(t["shopper_card"], t["market_date"])] += int(t["match_tokens_given"])
        cap = self.config["match_cap_per_shopper_per_day"]
        over = {k: v - cap for k, v in per_shopper_day.items() if v > cap}
        p4 = self.planted["P4"]["where"]
        self.assertEqual(over, {(p4["shopper_card"], p4["market_date"]): 5})
        t3 = next(t for t in self.key["traps"] if t["id"] == "T3")["where"]
        self.assertEqual(per_shopper_day[(t3["shopper_card"], t3["market_date"])], 9)

    def test_p5_only_ineligible_redemption(self):
        bad = [(r["market_date"], r["vendor_id"], "snap") for r in self.redemptions
               if int(r["snap_tokens"]) and self.vendors[r["vendor_id"]]["snap_eligible"] != "True"]
        bad += [(r["market_date"], r["vendor_id"], "match") for r in self.redemptions
                if int(r["match_tokens"]) and self.vendors[r["vendor_id"]]["match_eligible"] != "True"]
        self.assertEqual(bad, [("2026-10-17", "V07", "snap")])

    def test_p6_only_redemption_while_absent(self):
        absent = [(r["market_date"], r["vendor_id"]) for r in self.redemptions
                  if not self.attendance[(r["market_date"], r["vendor_id"])]]
        self.assertEqual(absent, [("2026-09-05", "V06")])

    def test_p7_match_float_goes_negative_only_on_oct_17_and_t1_is_fine(self):
        opening = json.loads((DATA / "opening_balances.json").read_text())["tokens_held_by_shoppers"]
        snap, match = opening["snap"], opening["match"]
        for day in self.config["market_days"]:
            issued = [t for t in self.terminal if t["market_date"] == day]
            turned_in = [r for r in self.redemptions if r["market_date"] == day]
            snap_issued = sum(int(t["snap_amount"]) for t in issued)
            snap_in = sum(int(r["snap_tokens"]) for r in turned_in)
            snap += snap_issued - snap_in
            match += sum(int(t["match_tokens_given"]) for t in issued) - sum(int(r["match_tokens"]) for r in turned_in)
            self.assertGreaterEqual(snap, 0, day)
            if day == "2026-09-12":   # T1: more turned in than issued that day, still fine overall
                self.assertGreater(snap_in, snap_issued)
            if day == "2026-10-17":
                self.assertEqual(match, -self.planted["P7"]["shortfall"])
            else:
                self.assertGreaterEqual(match, 0, day)

    def test_p8_only_duplicated_ach(self):
        refs = defaultdict(int)
        for b in self.bank:
            if b["reference"].startswith("ACH"):
                refs[b["reference"]] += 1
        self.assertEqual([r for r, n in refs.items() if n > 1], [self.planted["P8"]["where"]["reference"]])

    def test_p9_bank_change_request_differs_from_file(self):
        req = rows("bank_change_requests.csv")[0]
        self.assertEqual(req["vendor_id"], "V05")
        self.assertNotEqual(req["new_bank_last4"], self.vendors["V05"]["bank_last4"])

    # ---------- the rest of the world is consistent ----------

    def test_bank_running_balance(self):
        balance = json.loads((DATA / "opening_balances.json").read_text())["accounts"][0]["balance"]
        for b in self.bank:
            balance += dollars(b["amount"])
            self.assertEqual(balance, dollars(b["balance"]), b)

    def test_payout_register_matches_turn_ins(self):
        paid = {(p["market_date"], p["vendor_id"]): dollars(p["amount"]) for p in rows("payout_register.csv")}
        for r in self.redemptions:
            if r["market_date"] != "2026-10-17":
                self.assertEqual(paid[(r["market_date"], r["vendor_id"])], int(r["snap_tokens"]) + int(r["match_tokens"]))

    def test_opening_balances_balance(self):
        accts = json.loads((DATA / "opening_balances.json").read_text())["accounts"]
        debits = sum(a["balance"] for a in accts if a["type"] in ("asset", "expense"))
        credits = sum(a["balance"] for a in accts if a["type"] in ("liability", "equity"))
        self.assertEqual(debits, credits)

    def test_expected_batch_matches_turn_ins(self):
        oct17 = {r["vendor_id"]: r for r in self.redemptions if r["market_date"] == "2026-10-17"}
        for line in self.key["expected_oct_20_batch"]:
            r = oct17[line["vendor_id"]]
            self.assertEqual(line["gross"], int(r["snap_tokens"]) + int(r["match_tokens"]))
            self.assertEqual(line["pay_now"] + line["held"], line["gross"])


if __name__ == "__main__":
    unittest.main()
