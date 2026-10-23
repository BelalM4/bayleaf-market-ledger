"""
Builds the fake (synthetic) world of Bayleaf Farmers Market.

    python3 generate_data.py            # writes into data/
    python3 generate_data.py some/dir   # writes somewhere else (used by tests)

How the market's token program works:
  1. A shopper swipes their EBT (food stamp) card at the info booth, e.g. for $15.
  2. They get 15 green SNAP tokens ($1 each), plus free red MATCH tokens equal
     to what they swiped, capped at $10 per shopper per market day.
     The match money comes from a grant.
  3. Shoppers spend tokens at farm stands, this week or a later week.
  4. At closing, each vendor hands in their tokens and a volunteer counts them.
  5. The following Tuesday, the market pays each vendor $1 per token by bank transfer.
  6. Each month, the market bills the grant for the match tokens vendors handed in.

The generator plays out 8 Saturdays (Aug 29 to Oct 17, 2026) honestly, then plants
specific mistakes on top. It writes every planted mistake, and every "trap"
(something that looks wrong but isn't), to data/answer_key.json.
The same seed always produces the same files.
"""
import csv
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

SEED = 1023
MATCH_CAP = 10                      # max free match dollars per shopper per market day
MARKET_DAYS = [date(2026, 8, 29) + timedelta(weeks=i) for i in range(8)]
TODAY = date(2026, 10, 19)          # Monday: the agent runs and prepares Tuesday's payouts
PAYOUT_DATE = date(2026, 10, 20)

OPENING_SNAP_OUT = 64               # tokens shoppers were still holding on Aug 28
OPENING_MATCH_OUT = 38
OPENING_CASH = 8000
OPENING_GRANT_BILLED = 612          # match redeemed Aug 1-22, not yet reimbursed

GRANT_ANNUAL_BUDGET = 9000
GRANT_USED_BEFORE_PERIOD = 6880     # match redeemed Jan 1 - Aug 22
SEASON_LAST_DAY = date(2026, 12, 19)

# id, name, what they sell, may take SNAP tokens, may take MATCH tokens,
# bank account last 4, phone on file
VENDORS = [
    ("V01", "Green Acres Farm",       "vegetables",        True,  True,  "4417", "(510) 555-0141"),
    ("V02", "Sunny Slope Orchards",   "fruit",             True,  True,  "9023", "(510) 555-0187"),
    ("V03", "Two Hens Eggs",          "eggs",              True,  False, "3308", "(510) 555-0112"),
    ("V04", "Bayside Bakery",         "bread",             True,  False, "7751", "(510) 555-0164"),
    ("V05", "Honeycomb Apiary",       "honey",             True,  False, "2290", "(510) 555-0199"),
    ("V06", "Little Creek Dairy",     "dairy",             True,  False, "6134", "(510) 555-0125"),
    ("V07", "Ember Wood-Fired Pizza", "hot prepared food", False, False, "5582", "(510) 555-0173"),
    ("V08", "Wildflower Candles",     "candles and crafts", False, False, "8846", "(510) 555-0158"),
]
NAMES = {v[0]: v[1] for v in VENDORS}
SNAP_WEIGHTS = {"V01": 30, "V02": 22, "V03": 12, "V04": 14, "V05": 8, "V06": 14}
MATCH_WEIGHTS = {"V01": 55, "V02": 45}
COUNTERS = ["Dana", "Marco", "Priya"]

# Per market day: how many shoppers swiped, and what share of the tokens
# shoppers were holding got spent that day. The rest carries over to next week.
DAY_PLAN = {
    date(2026, 8, 29): (22, 0.90, "normal"),
    date(2026, 9, 5):  (26, 0.62, "heat wave, market closed at 11am"),
    date(2026, 9, 12): (9,  0.97, "rain, few shoppers"),
    date(2026, 9, 19): (21, 0.91, "normal"),
    date(2026, 9, 26): (24, 0.90, "normal"),
    date(2026, 10, 3): (23, 0.92, "normal"),
    date(2026, 10, 10): (22, 0.90, "normal"),
    date(2026, 10, 17): (25, 0.93, "normal"),
}

SWIPE_AMOUNTS = [5, 8, 10, 12, 15, 20, 25, 30]
SWIPE_WEIGHTS = [8, 10, 18, 12, 18, 18, 8, 8]


def money(dollars):
    return f"{dollars:.2f}"


def ach_ref(market_day, vendor_id):
    return f"ACH{(market_day + timedelta(days=3)).strftime('%m%d')}{vendor_id}"


def build(rng):
    shoppers = [f"C-{n}" for n in rng.sample(range(1000, 9999), 70)]

    # ---------- attendance ----------
    attendance = {}
    for d in MARKET_DAYS:
        for v in VENDORS:
            vid = v[0]
            if vid in ("V01", "V02"):
                present = True                      # the two produce farms never miss
            else:
                present = rng.random() < 0.88
            attendance[(d, vid)] = present
    # forced so the planted mistakes and traps land where we want them
    attendance[(date(2026, 9, 5), "V06")] = False   # P6: dairy marked absent
    for vid in ("V05", "V07"):
        attendance[(date(2026, 10, 17), vid)] = True

    # ---------- booth terminal swipes ----------
    terminal = []        # every real swipe
    for d in MARKET_DAYS:
        n_shoppers, _, _ = DAY_PLAN[d]
        for i, s in enumerate(rng.sample(shoppers, n_shoppers)):
            amount = rng.choices(SWIPE_AMOUNTS, SWIPE_WEIGHTS)[0]
            minutes = rng.randint(0, 270)
            terminal.append({
                "market_date": d, "time": f"{8 + (30 + minutes) // 60:02d}:{(30 + minutes) % 60:02d}",
                "shopper_card": s, "snap_amount": amount, "match_tokens_given": min(amount, MATCH_CAP),
            })

    planted, traps = [], []
    used_today = lambda d: {t["shopper_card"] for t in terminal if t["market_date"] == d}

    # T3 trap: same shopper swipes twice in one day but stays under the cap (4 + 5 = 9)
    d = date(2026, 9, 19)
    card_t3 = next(s for s in shoppers if s not in used_today(d))
    terminal.append({"market_date": d, "time": "09:12", "shopper_card": card_t3, "snap_amount": 4, "match_tokens_given": 4})
    terminal.append({"market_date": d, "time": "11:47", "shopper_card": card_t3, "snap_amount": 5, "match_tokens_given": 5})

    # P4: same shopper swipes twice and gets match both times: 8 + 7 = 15, over the $10 cap by $5
    d = date(2026, 9, 26)
    card_p4 = next(s for s in shoppers if s not in used_today(d))
    terminal.append({"market_date": d, "time": "09:40", "shopper_card": card_p4, "snap_amount": 8, "match_tokens_given": 8})
    terminal.append({"market_date": d, "time": "12:05", "shopper_card": card_p4, "snap_amount": 7, "match_tokens_given": 7})

    terminal.sort(key=lambda t: (t["market_date"], t["time"], t["shopper_card"]))
    for d in MARKET_DAYS:
        day_rows = [t for t in terminal if t["market_date"] == d]
        for i, t in enumerate(day_rows, 1):
            t["txn_id"] = f"T{d.strftime('%m%d')}-{i:03d}"

    # ---------- token spending and vendor turn-ins ----------
    # Planted turn-ins of real tokens (shoppers really spent them, so they come
    # out of what shoppers hold):
    #   P6: dairy turned in 18 SNAP tokens on a day attendance says it was absent.
    #   P5: the pizza stand (hot food, SNAP not allowed) turned in 12 SNAP tokens.
    extra_snap = {(date(2026, 9, 5), "V06"): 18, (date(2026, 10, 17), "V07"): 12}
    redemptions = {}     # (date, vendor) -> [snap, match]
    snap_out, match_out = OPENING_SNAP_OUT, OPENING_MATCH_OUT
    for d in MARKET_DAYS:
        _, spend_frac, _ = DAY_PLAN[d]
        day = [t for t in terminal if t["market_date"] == d]
        snap_issued = sum(t["snap_amount"] for t in day)
        match_issued = sum(t["match_tokens_given"] for t in day)
        snap_avail, match_avail = snap_out + snap_issued, match_out + match_issued
        snap_spent, match_spent = round(snap_avail * spend_frac), round(match_avail * spend_frac)

        snap_vendors = [v for v in SNAP_WEIGHTS if attendance[(d, v)]]
        match_vendors = [v for v in MATCH_WEIGHTS if attendance[(d, v)]]
        for kind, spent, vendors, weights in (
            (0, snap_spent, snap_vendors, SNAP_WEIGHTS),
            (1, match_spent, match_vendors, MATCH_WEIGHTS),
        ):
            for vid in rng.choices(vendors, [weights[v] for v in vendors], k=spent):
                redemptions.setdefault((d, vid), [0, 0])[kind] += 1
        for (when, vid), n in extra_snap.items():
            if when == d:
                redemptions[(d, vid)] = [n, 0]
                snap_spent += n
        snap_out, match_out = snap_avail - snap_spent, match_avail - match_spent
        assert snap_out >= 0 and match_out >= 0

    # P7: Green Acres turned in 40 MORE match tokens than shoppers could possibly
    # have held (miscount or fake tokens). The market's token float goes negative.
    honest_match_left = match_out
    assert honest_match_left < 40, "seed must leave fewer than 40 match tokens with shoppers"
    redemptions[(d, "V01")][1] += 40

    # ---------- bank statement ----------
    bank = []

    def bank_line(when, desc, amount, ref):
        bank.append({"date": when, "description": desc, "amount": amount, "reference": ref})

    def settle_total(d, exclude=()):
        return sum(t["snap_amount"] for t in terminal if t["market_date"] == d and t["txn_id"] not in exclude)

    # P2 (trap): one Sep 19 swipe settles a week late, as its own small deposit
    late = next(t for t in terminal if t["market_date"] == date(2026, 9, 19) and t["snap_amount"] == 20)
    # P3: one Oct 10 swipe never settles at all
    missing = next(t for t in terminal if t["market_date"] == date(2026, 10, 10) and t["snap_amount"] == 25)

    for d in MARKET_DAYS:
        excl = (late["txn_id"], missing["txn_id"])
        bank_line(d + timedelta(days=2), f"STATE EBT PROCESSOR SETTLEMENT BATCH {d.strftime('%m%d')}",
                  settle_total(d, excl), f"EBT-{d.strftime('%m%d')}")
    bank_line(date(2026, 9, 28), "STATE EBT PROCESSOR LATE ITEM", late["snap_amount"], "EBT-0919-L1")

    # vendor payouts made by the previous volunteer treasurer (every day except Oct 17)
    payout_register = []
    for d in MARKET_DAYS[:-1]:
        for vid in NAMES:
            if (d, vid) in redemptions:
                amt = sum(redemptions[(d, vid)])
                ref = ach_ref(d, vid)
                payout_register.append({"payout_date": d + timedelta(days=3), "market_date": d,
                                        "vendor_id": vid, "amount": amt, "ach_ref": ref})
                bank_line(d + timedelta(days=3), f"ACH PAYMENT TO {NAMES[vid].upper()}", -amt, ref)

    # P8: the bank sent the Sep 29 payment to Sunny Slope twice
    dup = next(p for p in payout_register if p["market_date"] == date(2026, 9, 26) and p["vendor_id"] == "V02")
    bank_line(dup["payout_date"], f"ACH PAYMENT TO {NAMES['V02'].upper()}", -dup["amount"], dup["ach_ref"])

    # grant reimbursements: the market bills match tokens redeemed each month
    def match_redeemed(days):
        return sum(redemptions.get((d, v), [0, 0])[1] for d in days for v in NAMES)

    aug_days = [d for d in MARKET_DAYS if d.month == 8]
    sep_days = [d for d in MARKET_DAYS if d.month == 9]
    grant_claims = [
        {"claim_id": "GC-2026-08", "month": "2026-08", "submitted": date(2026, 9, 2),
         "amount_claimed": OPENING_GRANT_BILLED + match_redeemed(aug_days), "status": "paid", "paid_date": date(2026, 9, 16)},
        {"claim_id": "GC-2026-09", "month": "2026-09", "submitted": date(2026, 10, 2),
         "amount_claimed": match_redeemed(sep_days), "status": "paid", "paid_date": date(2026, 10, 14)},
    ]
    for c in grant_claims:
        bank_line(c["paid_date"], f"GOLDEN STATE NUTRITION INCENTIVE FUND {c['claim_id']}", c["amount_claimed"], c["claim_id"])

    bank.sort(key=lambda b: (b["date"], -b["amount"], b["reference"]))
    balance = OPENING_CASH
    for b in bank:
        balance += b["amount"]
        b["balance"] = balance

    # P1: the terminal export repeated one Sep 12 row (same txn_id). Bank is unaffected.
    dup_row = next(t for t in terminal if t["market_date"] == date(2026, 9, 12))
    terminal_export = list(terminal)
    terminal_export.insert(terminal_export.index(dup_row) + 1, dict(dup_row))

    # P9: an email the day before payouts asks to change Honeycomb Apiary's bank account
    bank_change_requests = [{
        "received_at": "2026-10-19 07:52", "vendor_id": "V05", "new_bank_last4": "0067",
        "channel": "email", "from_address": "honeycomb.apiary.payments@mailbox-secure.example",
        "message": "Hi, we switched banks. Please send this week's token payment to the new account. Thanks!",
    }]

    # ---------- answer key ----------
    pending = {vid: redemptions[(MARKET_DAYS[-1], vid)] for vid in NAMES if (MARKET_DAYS[-1], vid) in redemptions}
    v01_match = pending["V01"][1]
    planted += [
        dict(id="P1", type="duplicate_terminal_row", expected_action="auto_resolve",
             where={"file": "booth_terminal_log.csv", "txn_id": dup_row["txn_id"]}, amount=dup_row["snap_amount"],
             plain_english="The card terminal export lists the same swipe twice. It happened once: the bank deposit agrees with one copy."),
        dict(id="P2", type="late_settlement", expected_action="auto_resolve",
             where={"file": "bank_statement.csv", "txn_id": late["txn_id"], "reference": "EBT-0919-L1"}, amount=late["snap_amount"],
             plain_english="A Sep 19 swipe was missing from the Sep 21 deposit but arrived on its own on Sep 28. Timing difference, nothing lost."),
        dict(id="P3", type="missing_settlement", expected_action="escalate_open_item",
             where={"file": "booth_terminal_log.csv", "txn_id": missing["txn_id"]}, amount=missing["snap_amount"],
             plain_english="An Oct 10 swipe never reached the bank. The market handed out tokens but was never paid. Someone must call the EBT processor."),
        dict(id="P4", type="match_over_cap", expected_action="hold_for_approval",
             where={"file": "booth_terminal_log.csv", "shopper_card": card_p4, "market_date": "2026-09-26"}, amount=5,
             plain_english="One shopper swiped twice on Sep 26 and got $15 of free match. The cap is $10, so $5 is not covered by the grant."),
        dict(id="P5", type="ineligible_vendor_snap", expected_action="hold_for_approval",
             where={"file": "token_redemptions.csv", "vendor_id": "V07", "market_date": "2026-10-17"}, amount=12,
             plain_english="The pizza stand sells hot food, which SNAP cannot pay for, yet it turned in 12 SNAP tokens. Hold the $12."),
        dict(id="P6", type="redemption_while_absent", expected_action="hold_for_approval",
             where={"file": "token_redemptions.csv", "vendor_id": "V06", "market_date": "2026-09-05"}, amount=18,
             plain_english="Little Creek Dairy turned in 18 tokens on Sep 5, but the attendance log says it wasn't there. It was already paid. One of the two records is wrong."),
        dict(id="P7", type="match_float_negative", expected_action="hold_for_approval",
             where={"file": "token_redemptions.csv", "vendor_id": "V01", "market_date": "2026-10-17"},
             amount=v01_match, extra_tokens=40, shortfall=40 - honest_match_left,
             plain_english=f"On Oct 17 vendors turned in {40 - honest_match_left} more match tokens than shoppers could possibly have held. "
                           "Green Acres' count is far above normal. Hold its match payout until the tokens are recounted."),
        dict(id="P8", type="duplicate_ach_payment", expected_action="escalate_open_item",
             where={"file": "bank_statement.csv", "reference": dup["ach_ref"]}, amount=dup["amount"],
             plain_english="The bank sent Sunny Slope's Sep 29 payment twice. Ask the bank to reverse one, or deduct it from a future payout."),
        dict(id="P9", type="bank_detail_change", expected_action="hold_for_approval",
             where={"file": "bank_change_requests.csv", "vendor_id": "V05"}, amount=sum(pending["V05"]),
             plain_english="An email from an unfamiliar address asked to change Honeycomb's bank account the day before payday. "
                           "Hold the payment until someone calls the phone number on file."),
    ]
    traps += [
        dict(id="T1", type="redeemed_more_than_issued_same_day", where={"market_date": "2026-09-12"},
             plain_english="On rainy Sep 12, vendors turned in more SNAP tokens than the booth handed out that day.",
             why_not_an_error="Shoppers held lots of tokens left over from Sep 5, when the market closed early. "
                              "The season-to-date token count never goes negative."),
        dict(id="T2", type="late_settlement_looks_missing", where={"txn_id": late["txn_id"]},
             plain_english="Looking only at the Sep 21 deposit, one swipe seems to be missing.",
             why_not_an_error="It arrived as its own deposit on Sep 28 (see P2)."),
        dict(id="T3", type="two_swipes_under_cap", where={"shopper_card": card_t3, "market_date": "2026-09-19"},
             plain_english="One shopper swiped twice on Sep 19 and got match both times.",
             why_not_an_error="Total match was 4 + 5 = $9, under the $10 cap."),
    ]
    grant_used_in_period = match_redeemed(MARKET_DAYS) - 40   # the 40 fake tokens don't count
    remaining_days = (SEASON_LAST_DAY - MARKET_DAYS[-1]).days // 7
    warnings = [dict(
        id="W1", type="grant_runway",
        plain_english="At the current pace, the match grant runs out before the season ends in December.",
        grant_budget=GRANT_ANNUAL_BUDGET, used_through_oct_17=GRANT_USED_BEFORE_PERIOD + grant_used_in_period,
        remaining_market_days=remaining_days)]

    expected_batch = []
    for vid, (s, m) in pending.items():
        held = 0
        reasons = []
        if vid == "V07":
            held, reasons = s, ["P5"]
        if vid == "V01":
            held, reasons = m, ["P7"]
        if vid == "V05":
            held, reasons = s + m, ["P9"]
        expected_batch.append({"vendor_id": vid, "snap_tokens": s, "match_tokens": m,
                               "gross": s + m, "held": held, "pay_now": s + m - held, "hold_reasons": reasons})

    answer_key = {
        "seed": SEED, "today": TODAY, "payout_date": PAYOUT_DATE,
        "planted": planted, "traps": traps, "warnings": warnings,
        "expected_oct_20_batch": expected_batch,
        "honest_match_tokens_left_after_oct_17": honest_match_left,
        "day_plan": {d.isoformat(): note for d, (_, _, note) in DAY_PLAN.items()},
    }

    return {
        "vendors": [dict(vendor_id=v[0], name=v[1], sells=v[2], snap_eligible=v[3], match_eligible=v[4],
                         bank_last4=v[5], phone_on_file=v[6]) for v in VENDORS],
        "attendance": [dict(market_date=d, vendor_id=vid, present=p) for (d, vid), p in sorted(attendance.items())],
        "terminal": terminal_export,
        "redemptions": [dict(market_date=d, vendor_id=vid, snap_tokens=s, match_tokens=m,
                             counted_by=COUNTERS[(d.toordinal() + int(vid[1:])) % 3])
                        for (d, vid), (s, m) in sorted(redemptions.items()) if s or m],
        "bank": bank,
        "payout_register": payout_register,
        "grant_claims": grant_claims,
        "bank_change_requests": bank_change_requests,
        "answer_key": answer_key,
    }


def write_csv(path, rows, fields, money_fields=()):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            out = {k: r[k] for k in fields}
            for k in money_fields:
                out[k] = money(out[k])
            w.writerow(out)


def write_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, default=str) + "\n")


def main(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    w = build(random.Random(SEED))

    write_csv(out / "vendors.csv", w["vendors"],
              ["vendor_id", "name", "sells", "snap_eligible", "match_eligible", "bank_last4", "phone_on_file"])
    write_csv(out / "attendance.csv", w["attendance"], ["market_date", "vendor_id", "present"])
    write_csv(out / "booth_terminal_log.csv", w["terminal"],
              ["txn_id", "market_date", "time", "shopper_card", "snap_amount", "match_tokens_given"])
    write_csv(out / "token_redemptions.csv", w["redemptions"],
              ["market_date", "vendor_id", "snap_tokens", "match_tokens", "counted_by"])
    write_csv(out / "bank_statement.csv", w["bank"], ["date", "description", "amount", "balance", "reference"],
              money_fields=("amount", "balance"))
    write_csv(out / "payout_register.csv", w["payout_register"],
              ["payout_date", "market_date", "vendor_id", "amount", "ach_ref"], money_fields=("amount",))
    write_csv(out / "grant_claims.csv", w["grant_claims"],
              ["claim_id", "month", "submitted", "amount_claimed", "status", "paid_date"], money_fields=("amount_claimed",))
    write_csv(out / "bank_change_requests.csv", w["bank_change_requests"],
              ["received_at", "vendor_id", "new_bank_last4", "channel", "from_address", "message"])

    write_json(out / "market_config.json", {
        "market": "Bayleaf Farmers Market (fictional)",
        "today": TODAY, "next_payout_date": PAYOUT_DATE,
        "market_days": MARKET_DAYS,
        "match_cap_per_shopper_per_day": MATCH_CAP,
        "token_value_dollars": 1,
        "rules": {
            "snap_tokens": "Only vendors with snap_eligible=True may be paid for SNAP tokens (no hot food, no non-food).",
            "match_tokens": "Only vendors with match_eligible=True (fruit and vegetable farms) may be paid for match tokens.",
            "payouts": "Vendors are paid $1 per token by bank transfer on the Tuesday after each market day.",
            "approvals": "The agent may prepare payouts. Only the market manager may approve them.",
        },
        "people": {
            "market_manager": "Jordan Lee (approves payouts and write-offs)",
            "volunteer_treasurer": "Sam Ortiz (left in October; made payouts through Oct 13)",
        },
        "grant": {
            "funder": "Golden State Nutrition Incentive Fund (fictional)",
            "annual_budget": GRANT_ANNUAL_BUDGET,
            "used_before_period": GRANT_USED_BEFORE_PERIOD,
            "reimburses": "match tokens redeemed by vendors, billed monthly",
            "season_last_market_day": SEASON_LAST_DAY,
        },
    })
    write_json(out / "opening_balances.json", {
        "as_of": "2026-08-28",
        "note": "The market's books the morning before the first Saturday in this dataset. Debits equal credits.",
        "tokens_held_by_shoppers": {"snap": OPENING_SNAP_OUT, "match": OPENING_MATCH_OUT},
        "accounts": [
            {"code": "1000", "name": "Cash - Operating Bank", "type": "asset", "balance": OPENING_CASH},
            {"code": "1100", "name": "EBT Settlement Receivable", "type": "asset", "balance": 0},
            {"code": "1200", "name": "Grant Receivable - Billed", "type": "asset", "balance": OPENING_GRANT_BILLED},
            {"code": "1210", "name": "Grant Receivable - Unbilled Match", "type": "asset", "balance": OPENING_MATCH_OUT},
            {"code": "2000", "name": "SNAP Tokens Outstanding", "type": "liability", "balance": OPENING_SNAP_OUT},
            {"code": "2010", "name": "Match Tokens Outstanding", "type": "liability", "balance": OPENING_MATCH_OUT},
            {"code": "2100", "name": "Vendor Payable", "type": "liability", "balance": 0},
            {"code": "3000", "name": "Net Assets", "type": "equity",
             "balance": OPENING_CASH + OPENING_GRANT_BILLED + OPENING_MATCH_OUT - OPENING_SNAP_OUT - OPENING_MATCH_OUT},
            {"code": "5000", "name": "Unreimbursed Match Expense", "type": "expense", "balance": 0},
        ],
    })
    write_json(out / "answer_key.json", w["answer_key"])
    return w


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "data"
    w = main(target)
    print(f"Wrote {len(w['terminal'])} swipes, {len(w['redemptions'])} vendor turn-ins, "
          f"{len(w['bank'])} bank lines to {target}")
    print(f"Planted {len(w['answer_key']['planted'])} mistakes and {len(w['answer_key']['traps'])} traps (see answer_key.json)")
