"""
Turns the market's records into journal entries, then proves the books are right.

Every event becomes one balanced entry that points back to the record behind it:

  What happened                     Debit                         Credit
  --------------------------------  ----------------------------  ------------------------------
  Booth hands out SNAP tokens       1100 EBT Receivable           2000 SNAP Tokens Outstanding
  Booth hands out MATCH tokens      1210 Grant Unbilled Match     2010 Match Tokens Outstanding
  Vendor turns tokens in            2000 / 2010 Tokens            2100 Vendor Payable (per vendor)
    ...but the payment is on hold                                 2110 Vendor Payable - On Hold
    ...tokens that can't exist      1900 Suspense                 (instead of 2010)
  EBT processor deposits            1000 Cash                     1100 EBT Receivable
  Market pays a vendor              2100 Vendor Payable           1000 Cash
  Bank pays a vendor twice          1400 Due from Bank            1000 Cash
  Market bills the grant            1200 Grant Billed             1210 Grant Unbilled Match
  Grant pays                        1000 Cash                     1200 Grant Billed

After posting, `tie_outs` checks every important balance against an outside
source (the bank, the token counts, the payment batch). It's a reconciliation:
two records of the same thing must agree, or the difference must be explained.
"""
from collections import defaultdict

from ledger import Ledger, cr, dr


def build(a):
    """`a` is an Agent that has already run. Returns the ledger with all routine entries posted."""
    led = Ledger(a.opening["accounts"])
    post = lambda date, memo, lines, source: led.post(date, memo, lines, source, posted_by="agent")

    post(a.opening["as_of"], "Opening balances",
         [dr(x["code"], x["balance"]) if x["type"] in ("asset", "expense") else cr(x["code"], x["balance"])
          for x in a.opening["accounts"]],
         {"file": "opening_balances.json"})

    holds = defaultdict(int)          # (market_date, vendor) -> held dollars
    for f in a.findings:
        if f["hold"]:
            holds[(f["hold"]["market_date"], f["hold"]["vendor_id"])] += f["hold"]["amount"]
    unverified = defaultdict(int)     # (market_date, "snap"/"match") -> tokens that can't exist
    for f in a.findings:
        if f["type"].endswith("_float_negative"):
            unverified[(f["market_date"], f["type"].split("_")[0])] += f["shortfall"]
    dup_payment = {f["market_date"] + f["vendor_id"]: f["id"] for f in a.findings if f["type"] == "duplicate_ach_payment"}
    register = {p["ach_ref"]: p for p in a.register}

    events = []                       # (date, order, callable) so entries post in date order
    for day in a.days:
        swipes = [s for s in a.swipes if s["market_date"] == day]
        snap = sum(int(s["snap_amount"]) for s in swipes)
        match = sum(int(s["match_tokens_given"]) for s in swipes)
        events.append((day, 0, lambda day=day, snap=snap, match=match, n=len(swipes): post(
            day, f"Tokens handed out at the booth on {day} ({n} swipes)",
            [dr("1100", snap), cr("2000", snap), dr("1210", match), cr("2010", match)],
            {"file": "booth_terminal_log.csv", "market_date": day, "swipes": n})))

        turned_in = [r for r in a.redemptions if r["market_date"] == day]
        fake_snap, fake_match = unverified[(day, "snap")], unverified[(day, "match")]
        lines = [dr("2000", sum(int(r["snap_tokens"]) for r in turned_in) - fake_snap),
                 dr("2010", sum(int(r["match_tokens"]) for r in turned_in) - fake_match),
                 dr("1900", fake_snap + fake_match)]
        for r in turned_in:
            vid = r["vendor_id"]
            gross = int(r["snap_tokens"]) + int(r["match_tokens"])
            held = min(gross, holds[(day, vid)])
            lines += [cr("2100", gross - held, vid), cr("2110", held, vid)]
        events.append((day, 1, lambda day=day, lines=lines, n=len(turned_in): post(
            day, f"Tokens turned in by {n} vendors on {day}", lines,
            {"file": "token_redemptions.csv", "market_date": day})))

    seen_refs = set()
    for b in a.bank:
        amt, ref = round(float(b["amount"])), b["reference"]
        src = {"file": "bank_statement.csv", "line": b["_line"], "reference": ref}
        if ref.startswith("EBT-"):
            lines = [dr("1000", amt), cr("1100", amt)]
        elif ref.startswith("GC-"):
            lines = [dr("1000", amt), cr("1200", amt)]
        elif ref.startswith("ACH") and ref in register:
            p = register[ref]
            if ref in seen_refs:
                src["finding"] = dup_payment.get(p["market_date"] + p["vendor_id"])
                lines = [dr("1400", -amt, p["vendor_id"]), cr("1000", -amt)]
            else:
                lines = [dr("2100", -amt, p["vendor_id"]), cr("1000", -amt)]
            seen_refs.add(ref)
        else:                         # money we can't explain waits in suspense, never in a real account
            lines = [dr("1000", amt), cr("1900", amt)] if amt > 0 else [dr("1900", -amt), cr("1000", -amt)]
        events.append((b["date"], 2, lambda b=b, lines=lines, src=src: post(b["date"], b["description"], lines, src)))

    for c in a.claims:
        amt = round(float(c["amount_claimed"]))
        events.append((c["submitted"], 3, lambda c=c, amt=amt: post(
            c["submitted"], f"Grant claim {c['claim_id']} for {c['month']} match",
            [dr("1200", amt), cr("1210", amt)], {"file": "grant_claims.csv", "line": c["_line"]})))

    for _, _, do in sorted(events, key=lambda e: (e[0], e[1])):
        do()
    led.lock(a.config["books_closed_through"])
    return led


def tie_outs(a, led, batch=None):
    """Each check compares a ledger balance with an independent source. All must pass.

    `batch` is the live payment batch (the approval gate updates it as holds are decided).
    Entries the manager approved are counted by their `source.kind`, so a decision
    that legitimately changes a balance is explained, not flagged.
    """
    batch = batch or a.batch
    checks = []

    def check(name, ledger_value, outside_value, outside_source, explains=""):
        checks.append({"name": name, "ledger": ledger_value, "outside": outside_value, "source": outside_source,
                       "ok": round(ledger_value, 2) == round(outside_value, 2), "explains": explains})

    def approved(kind, account, side):
        return sum(l[side] for e in led.entries if e["source"].get("kind") == kind
                   for l in e["lines"] if l["account"] == account) / 100

    tb = led.trial_balance()
    check("Trial balance: total debits = total credits", sum(r["debit"] for r in tb), sum(r["credit"] for r in tb),
          "the ledger itself")

    bank_end = round(float(a.bank[-1]["balance"]))
    sent = approved("payment", "1000", "credit")
    check("Cash = bank statement balance, minus payments sent but not yet on the statement",
          led.balance("1000"), bank_end - sent, f"bank_statement.csv line {a.bank[-1]['_line']}",
          f"${sent:,.0f} approved and sent, not on the statement yet" if sent else "")

    swiped = sum(int(s["snap_amount"]) for s in a.swipes)
    deposited = sum(round(float(b["amount"])) for b in a.bank if b["reference"].startswith("EBT-"))
    missing = [f for f in a.findings if f["type"] == "missing_settlement"]
    check("EBT receivable = swipes not yet deposited", led.balance("1100"), swiped - deposited,
          "booth_terminal_log.csv vs bank_statement.csv",
          "; ".join(f"{f['id']}: {f['title']}" for f in missing))

    last = a.float_by_day[-1]
    fake_snap = sum(f["shortfall"] for f in a.findings if f["type"] == "snap_float_negative")
    fake = sum(f["shortfall"] for f in a.findings if f["type"] == "match_float_negative")
    recovered = approved("recover_tokens", "2000", "credit")
    check("SNAP tokens outstanding = tokens shoppers still hold", led.balance("2000"),
          last["snap"]["held_after"] + fake_snap + recovered, "token roll-forward (terminal vs turn-ins)",
          "; ".join(x for x in (f"{recovered:.0f} tokens put back after a turn-in was reversed" if recovered else "",
                                f"{fake_snap} tokens that can't exist went to suspense" if fake_snap else "") if x))
    check("Match tokens outstanding = tokens shoppers still hold", led.balance("2010"), last["match"]["held_after"] + fake,
          "token roll-forward, not counting tokens that can't exist",
          f"{fake} tokens that can't exist went to suspense" if fake else "")
    resolved = approved("resolve_suspense", "1900", "credit")
    check("Suspense = tokens still under review", led.balance("1900"), fake + fake_snap - resolved, "token roll-forward",
          f"{resolved:.0f} resolved by the manager" if resolved else "")

    lines = batch["lines"]
    check("Vendor payable = batch amounts approved to pay but not yet sent", led.balance("2100"),
          sum(l["pay_now"] - l.get("paid", 0) for l in lines), "payout batch")
    check("Vendor payable on hold = held lines in the batch", led.balance("2110"), sum(l["held"] for l in lines), "payout batch")
    for l in lines:
        check(f"Subledger: {l['vendor']} owed + held", led.balance("2100", l["vendor_id"]) + led.balance("2110", l["vendor_id"]),
              l["pay_now"] - l.get("paid", 0) + l["held"], "payout batch line")

    billed_months = {c["month"] for c in a.claims}
    unbilled_redeemed = sum(int(r["match_tokens"]) for r in a.redemptions if r["market_date"][:7] not in billed_months) - fake
    written_off = approved("write_off", "1210", "credit")
    check("Grant unbilled = match tokens still out + match turned in but not yet billed",
          led.balance("1210"), led.balance("2010") + unbilled_redeemed - written_off, "token counts and grant_claims.csv",
          f"${written_off:,.0f} written off as not billable" if written_off else "")

    dups = sum(f["amount"] for f in a.findings if f["type"] == "duplicate_ach_payment")
    check("Due from bank = duplicate payments to recover", led.balance("1400"), dups, "bank vs payout register")
    for code, acct in led.accounts.items():
        if acct["type"] in ("asset", "liability") and led.balance(code) < 0:
            check(f"{code} {acct['name']} is not negative", led.balance(code), 0, "common sense")
    return checks
