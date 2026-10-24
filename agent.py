"""
The Bayleaf bookkeeping agent.

    python3 agent.py                  # reads data/, writes output/
    python3 agent.py some/data/dir    # run on a different (e.g. judge-edited) copy

What it does, in order:
  1. Reads the market's records. It may only open the files in ALLOWED_SOURCES;
     the answer key is off limits, like a test it isn't allowed to peek at.
  2. Runs every check below. Each problem becomes a "finding" with the evidence
     (file and line), the rule it applied, and one of three actions:
        auto_resolve        - safe to fix alone; explained in the record
        escalate_open_item  - needs someone to chase a bank or processor
        hold_for_approval   - money question; only the market manager decides
     Things it looked at and decided were fine go in "cleared", with the reason.
  3. Prepares Tuesday's vendor payments, holding anything a finding touches.
     It cannot send them: the batch waits for the manager's approval.
"""
import csv
import hashlib
import json
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
ALLOWED_SOURCES = {
    "vendors.csv", "attendance.csv", "booth_terminal_log.csv", "token_redemptions.csv",
    "bank_statement.csv", "payout_register.csv", "grant_claims.csv", "bank_change_requests.csv",
    "market_config.json", "opening_balances.json",
}


class AccessDenied(Exception):
    pass


class Sources:
    """The only way the agent can read files. Records a fingerprint of everything it read."""

    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self.read_log = []

    def _open(self, name):
        if name not in ALLOWED_SOURCES:
            raise AccessDenied(f"The agent is not allowed to read {name}")
        raw = (self.dir / name).read_bytes()
        self.read_log.append({"file": name, "sha256": hashlib.sha256(raw).hexdigest()[:16]})
        return raw.decode()

    def csv(self, name):
        rows = list(csv.DictReader(self._open(name).splitlines()))
        for i, r in enumerate(rows):
            r["_line"] = i + 2      # line number in the file, counting the header as line 1
        return rows

    def json(self, name):
        return json.loads(self._open(name))


def ev(file, row, *fields):
    """One piece of evidence: which file, which line, and the values that matter."""
    return {"file": file, "line": row["_line"], **{f: row[f] for f in fields}}


def by_txn_first(rows):
    """The first copy of each txn_id."""
    seen = {}
    for r in rows:
        seen.setdefault(r["txn_id"], r)
    return list(seen.values())


def possessive(name):
    return name + ("'" if name.endswith("s") else "'s")


def usd(n):
    return f"${n:,.0f}"


def nice(day):
    d = date.fromisoformat(day)
    return f"{d:%b} {d.day}"


class Agent:
    def __init__(self, data_dir):
        self.src = Sources(data_dir)
        s = self.src
        self.config = s.json("market_config.json")
        self.opening = s.json("opening_balances.json")
        self.vendors = {v["vendor_id"]: v for v in s.csv("vendors.csv")}
        self.attendance = {(a["market_date"], a["vendor_id"]): a for a in s.csv("attendance.csv")}
        self.terminal_raw = s.csv("booth_terminal_log.csv")
        self.redemptions = s.csv("token_redemptions.csv")
        self.bank = s.csv("bank_statement.csv")
        self.register = s.csv("payout_register.csv")
        self.claims = s.csv("grant_claims.csv")
        self.bank_changes = s.csv("bank_change_requests.csv")
        self.today = self.config["today"]
        self.days = self.config["market_days"]
        self.paid_days = {p["market_date"] for p in self.register}
        self.findings, self.cleared, self.warnings, self.log = [], [], [], []

    # ---------- helpers ----------

    def name(self, vid):
        return self.vendors[vid]["name"]

    def note(self, step, detail):
        self.log.append({"at": datetime.now().isoformat(timespec="seconds"), "actor": "agent",
                         "step": step, "detail": detail})

    def add(self, type, action, title, explanation, rule, amount, evidence, hold=None, **where):
        f = {"id": f"F{len(self.findings) + 1:02d}", "type": type, "action": action, "title": title,
             "amount": amount, "explanation": explanation, "rule": rule, "evidence": evidence,
             "hold": hold, **where}
        self.findings.append(f)
        self.note("finding", f"{f['id']} {type} ({action}): {title}")

    def clear(self, check, title, reason, evidence=()):
        self.cleared.append({"check": check, "title": title, "reason": reason, "evidence": list(evidence)})

    def payment_status(self, day, vid):
        if day in self.paid_days:
            p = next((p for p in self.register if p["market_date"] == day and p["vendor_id"] == vid), None)
            return f"It was already paid on {nice(p['payout_date'])} ({p['ach_ref']})." if p else "It was never paid."
        return f"It is in the {nice(self.config['next_payout_date'])} payment batch and has not been paid yet."

    # ---------- checks ----------

    def check_terminal_duplicates(self):
        """The terminal export should list each swipe (txn_id) once."""
        by_txn = defaultdict(list)
        for r in self.terminal_raw:
            by_txn[r["txn_id"]].append(r)
        self.swipes = []
        fields = ("market_date", "time", "shopper_card", "snap_amount", "match_tokens_given")
        for txn, copies in by_txn.items():
            self.swipes.append(copies[0])
            if len(copies) == 1:
                continue
            identical = all(all(c[f] == copies[0][f] for f in fields) for c in copies)
            lines = ", ".join(str(c["_line"]) for c in copies)
            day, amount = copies[0]["market_date"], int(copies[0]["snap_amount"])
            once = sum(int(r["snap_amount"]) for r in by_txn_first(self.terminal_raw) if r["market_date"] == day)
            deposit = next((b for b in self.bank if b["reference"] == f"EBT-{day[5:7]}{day[8:10]}"), None)
            bank_agrees = deposit is not None and round(float(deposit["amount"])) == once
            proof = (f"The {nice(deposit['date'])} bank deposit is {usd(once)}, which matches counting it once, not twice "
                     f"({usd(once + amount * (len(copies) - 1))})." if bank_agrees else
                     "The bank deposit doesn't confirm either version, so a person should look.")
            self.add(
                "duplicate_terminal_row", "auto_resolve" if identical and bank_agrees else "escalate_open_item",
                f"Swipe {txn} appears {len(copies)} times in the terminal export",
                f"Lines {lines} of the terminal export are the same swipe: same ID, time, card and amount. {proof} "
                "Counted once.",
                "A transaction ID identifies exactly one swipe. The bank deposit is the system of record for cash received.",
                amount, [ev("booth_terminal_log.csv", c, "txn_id", *fields) for c in copies]
                + ([ev("bank_statement.csv", deposit, "date", "amount", "reference")] if deposit else []),
                market_date=day, txn_id=txn)

    def check_settlements(self):
        """Each market day's swipes should arrive in the bank as one EBT deposit two days later."""
        batches, late_items = {}, defaultdict(list)
        for b in self.bank:
            ref = b["reference"]
            if not ref.startswith("EBT-"):
                continue
            mmdd = ref[4:8]
            day = next((d for d in self.days if d[5:7] + d[8:10] == mmdd), None)
            (late_items[day].append(b) if "LATE" in b["description"] else batches.__setitem__(day, b))
        matched = []
        for day in self.days:
            swipes = [s for s in self.swipes if s["market_date"] == day]
            expected = sum(int(s["snap_amount"]) for s in swipes)
            batch = batches.get(day)
            got = round(float(batch["amount"])) if batch else 0
            late = late_items.get(day, [])
            late_total = sum(round(float(b["amount"])) for b in late)
            due = (date.fromisoformat(day) + timedelta(days=2)).isoformat()
            if batch is None and due >= self.today:
                continue
            evidence = ([ev("bank_statement.csv", batch, "date", "amount", "reference")] if batch else [])
            if got == expected and not late:
                matched.append(day)
                continue
            if got < expected and late_total:
                self.add(
                    "late_settlement", "auto_resolve",
                    f"{nice(day)} deposit was {usd(late_total)} short, then the rest arrived late",
                    f"The {nice(day)} swipes total {usd(expected)}, but the {nice(batch['date'])} deposit was {usd(got)}. "
                    f"A separate {usd(late_total)} 'late item' deposit for the same batch arrived on {nice(late[0]['date'])}. "
                    "Together they match to the dollar. This was a timing difference, and no money is missing.",
                    "Bank deposits are the system of record for cash received. A late deposit tagged to the "
                    "same batch closes the gap.",
                    late_total, evidence + [ev("bank_statement.csv", b, "date", "amount", "reference") for b in late],
                    market_date=day)
                got += late_total
            if got < expected:
                short = expected - got
                candidates = [s for s in swipes if int(s["snap_amount"]) == short]
                age = (date.fromisoformat(self.today) - date.fromisoformat(due)).days
                who = (f"Swipe {candidates[0]['txn_id']} ({usd(short)}, card {candidates[0]['shopper_card']}) "
                       "is the only swipe of exactly that amount." if len(candidates) == 1 else
                       f"{len(candidates)} swipes that day are exactly {usd(short)}; the processor's batch detail will say which.")
                self.add(
                    "missing_settlement", "escalate_open_item",
                    f"{usd(short)} from {nice(day)} never reached the bank",
                    f"The booth handed out tokens for {usd(expected)} of swipes on {nice(day)}, but the bank received "
                    f"{usd(got)}. The deposit was due {age} days ago and nothing more has arrived. {who} "
                    "Someone needs to ask the EBT processor for the missing payment. "
                    "It stays open as a reconciling item until then.",
                    "Tokens handed out must be paid for by an EBT deposit. Gaps older than 5 days are chased with the processor.",
                    short, evidence + [ev("booth_terminal_log.csv", c, "txn_id", "snap_amount", "shopper_card") for c in candidates],
                    market_date=day, **({"txn_id": candidates[0]["txn_id"]} if len(candidates) == 1 else {}))
            elif got > expected:
                self.add(
                    "unexpected_deposit", "escalate_open_item", f"{nice(day)} deposit is {usd(got - expected)} more than the swipes",
                    "The bank received more than the booth recorded. A swipe may be missing from the terminal export.",
                    "Every deposit must trace back to recorded swipes.", got - expected, evidence, market_date=day)
        self.clear("settlements", f"{len(matched)} of {len(self.days)} EBT deposits matched the swipes to the dollar",
                   "Swipe totals per day equal the bank deposit for that batch.")

    def check_match_cap(self):
        """No shopper gets more than the cap in free match tokens on one market day."""
        cap = self.config["match_cap_per_shopper_per_day"]
        per = defaultdict(list)
        for s in self.swipes:
            per[(s["shopper_card"], s["market_date"])].append(s)
        for (card, day), swipes in sorted(per.items(), key=lambda kv: kv[0][1]):
            total = sum(int(s["match_tokens_given"]) for s in swipes)
            if len(swipes) < 2 and total <= cap:
                continue
            parts = " + ".join(f"${s['match_tokens_given']}" for s in swipes)
            evidence = [ev("booth_terminal_log.csv", s, "txn_id", "time", "shopper_card", "snap_amount", "match_tokens_given")
                        for s in swipes]
            if total > cap:
                self.add(
                    "match_over_cap", "hold_for_approval",
                    f"Shopper {card} got {usd(total)} of match on {nice(day)} (cap is {usd(cap)})",
                    f"Card {card} swiped {len(swipes)} times on {nice(day)} and got match each time: {parts} = {usd(total)}. "
                    f"The grant covers at most {usd(cap)} per shopper per day, so {usd(total - cap)} of match was given out "
                    "that the grant will not pay for. The tokens are already spent, so the market can't take them back. "
                    f"The manager should approve moving {usd(total - cap)} off the grant claim into unreimbursed match expense, "
                    "and remind booth volunteers to check for earlier swipes.",
                    f"Market config: match_cap_per_shopper_per_day = {cap}.", total - cap, evidence,
                    market_date=day, shopper_card=card)
            else:
                self.clear("match_cap", f"Card {card} swiped {len(swipes)} times on {nice(day)}",
                           f"Total match {parts} = {usd(total)}, within the {usd(cap)} cap.", evidence)

    def check_redemptions(self):
        """Vendors may only turn in tokens they're allowed to accept, on days they were at the market."""
        for r in self.redemptions:
            vid, day = r["vendor_id"], r["market_date"]
            v = self.vendors[vid]
            status = self.payment_status(day, vid)
            pending = day not in self.paid_days
            row_ev = ev("token_redemptions.csv", r, "market_date", "vendor_id", "snap_tokens", "match_tokens", "counted_by")
            for kind, flag in (("snap", "snap_eligible"), ("match", "match_eligible")):
                n = int(r[f"{kind}_tokens"])
                if n and v[flag] != "True":
                    self.add(
                        "ineligible_vendor_" + kind, "hold_for_approval",
                        f"{v['name']} turned in {n} {kind.upper()} tokens it may not accept",
                        f"{v['name']} sells {v['sells']}, which {kind.upper()} tokens can't pay for. On {nice(day)} it "
                        f"turned in {n} {kind.upper()} tokens (counted by {r['counted_by']}). {status} "
                        + (f"Holding {usd(n)}. " if pending else "")
                        + "The manager decides whether to pay it once, refuse it, or recover it, and should remind the vendor of the rule.",
                        f"Market rule ({kind}_tokens): {self.config['rules'][kind + '_tokens']}",
                        n, [row_ev, ev("vendors.csv", v, "vendor_id", "sells", flag)],
                        hold={"vendor_id": vid, "market_date": day, "amount": n} if pending else None,
                        market_date=day, vendor_id=vid)
            att = self.attendance.get((day, vid))
            if att is None or att["present"] != "True":
                n = int(r["snap_tokens"]) + int(r["match_tokens"])
                self.add(
                    "redemption_while_absent", "hold_for_approval",
                    f"{v['name']} turned in tokens on {nice(day)} but is marked absent",
                    f"The attendance log says {v['name']} was not at the market on {nice(day)}, but the token count sheet "
                    f"shows it turned in {n} tokens that day (counted by {r['counted_by']}). {status} "
                    "One of the two records is wrong, and the agent can't tell which. If the vendor was there, the attendance "
                    f"log needs fixing. If it wasn't, the {usd(n)} should be recovered. The manager should ask the vendor and {r['counted_by']}.",
                    "Tokens can only be earned at a stall that was open. Attendance and turn-ins must agree.",
                    n, [row_ev] + ([ev("attendance.csv", att, "market_date", "vendor_id", "present")] if att else []),
                    hold={"vendor_id": vid, "market_date": day, "amount": n} if pending else None,
                    market_date=day, vendor_id=vid)

    def check_token_float(self):
        """Tokens turned in can never exceed tokens that exist: what shoppers held before plus what the booth handed out."""
        held = dict(self.opening["tokens_held_by_shoppers"])
        history = defaultdict(list)          # vendor -> past match/snap counts, to spot unusual counts
        self.float_by_day = []
        for day in self.days:
            day_swipes = [s for s in self.swipes if s["market_date"] == day]
            day_in = [r for r in self.redemptions if r["market_date"] == day]
            row = {"market_date": day}
            for kind, issued_field in (("snap", "snap_amount"), ("match", "match_tokens_given")):
                start = held[kind]
                issued = sum(int(s[issued_field]) for s in day_swipes)
                turned_in = sum(int(r[f"{kind}_tokens"]) for r in day_in)
                held[kind] = start + issued - turned_in
                row[kind] = {"held_before": start, "handed_out": issued, "turned_in": turned_in, "held_after": held[kind]}
                if held[kind] < 0:
                    self._float_negative(kind, day, start, issued, turned_in, day_in, history)
                elif turned_in > issued:
                    self.clear("token_float", f"{nice(day)}: vendors turned in more {kind.upper()} tokens than were handed out that day",
                               f"{turned_in} turned in vs {issued} handed out, but shoppers were already holding {start} "
                               f"from earlier weeks, so up to {start + issued} could exist. {held[kind]} are still out there.")
            for r in day_in:
                for kind in ("snap", "match"):
                    if int(r[f"{kind}_tokens"]):
                        history[(r["vendor_id"], kind)].append(int(r[f"{kind}_tokens"]))
            self.float_by_day.append(row)

    def _float_negative(self, kind, day, start, issued, turned_in, day_in, history):
        """Tokens that can't exist were turned in. Work out whose counts to hold.

        Hold every vendor whose count is unusually high (at least 20 tokens and 40% above its
        median from earlier Saturdays), biggest jump first. If those jumps don't cover the
        tokens that can't exist, keep adding the next-biggest jumps until they do. The missing
        tokens are split in proportion to each vendor's jump, so each held vendor knows how many of its
        tokens are in doubt.
        """
        short = turned_in - start - issued
        jumps = []
        for r in day_in:
            n = int(r[f"{kind}_tokens"])
            past = history[(r["vendor_id"], kind)]
            if n and past:
                usual = statistics.median(past)
                jumps.append({"jump": n - usual, "n": n, "usual": usual, "row": r})
        jumps.sort(key=lambda j: -j["jump"])
        suspicious = [j for j in jumps if j["jump"] >= max(20, 0.4 * j["usual"])]
        for j in jumps:
            if sum(x["jump"] for x in suspicious) >= short:
                break
            if j not in suspicious:
                suspicious.append(j)
        total_jump = sum(max(j["jump"], 0) for j in suspicious) or 1
        for j in suspicious:
            j["share"] = int(short * max(j["jump"], 0) // total_jump)
        suspicious[0]["share"] += short - sum(j["share"] for j in suspicious)   # rounding leftovers go to the biggest jump

        pending = day not in self.paid_days
        normal = [j for j in jumps if j not in suspicious]
        others = ", ".join(f"{self.name(j['row']['vendor_id'])} {j['n']} (usual {j['usual']:.0f})" for j in normal) or "none"
        held_names = " and ".join(self.name(j["row"]["vendor_id"]) for j in suspicious)
        for j in suspicious:
            r, n, vid = j["row"], j["n"], j["row"]["vendor_id"]
            self.add(
                f"{kind}_float_negative", "hold_for_approval",
                f"{nice(day)}: {short} more {kind.upper()} tokens turned in than exist"
                + (f" ({self.name(vid)})" if len(suspicious) > 1 else ""),
                f"Before {nice(day)}, shoppers held at most {start} {kind.upper()} tokens, and the booth handed out {issued} more, "
                f"so no more than {start + issued} could be spent. Vendors turned in {turned_in}, which is {short} too many. "
                f"Those tokens can't be real: they were miscounted or counterfeit. "
                f"{self.name(vid)} turned in {n}, about {j['jump']:.0f} above its usual {j['usual']:.0f}"
                + (f". {held_names} are both far above normal, so both are held, and {j['share']} of the {short} missing "
                   f"tokens are attributed to {self.name(vid)}" if len(suspicious) > 1 else ", the biggest jump of any vendor")
                + f" (normal counts: {others}). Holding its {n} {kind.upper()} tokens ({usd(n)}) until they are recounted. "
                f"Its other tokens are not affected and get paid. Paying it now would mean billing the grant for tokens that never existed.",
                "Token roll-forward: tokens held + handed out - turned in can never go below zero. A count is unusual if it is "
                "at least 20 tokens and 40% above that vendor's median from earlier Saturdays.",
                n, [ev("token_redemptions.csv", x, "vendor_id", f"{kind}_tokens", "counted_by") for x in day_in],
                hold={"vendor_id": vid, "market_date": day, "amount": n} if pending else None,
                market_date=day, vendor_id=vid, shortfall=j["share"])

    def check_vendor_payments(self):
        """Every payment in the bank should be in the payout register exactly once, and the reverse."""
        bank_by_ref = defaultdict(list)
        for b in self.bank:
            if b["reference"].startswith("ACH"):
                bank_by_ref[b["reference"]].append(b)
        reg_by_ref = {p["ach_ref"]: p for p in self.register}
        for ref, lines in bank_by_ref.items():
            p = reg_by_ref.get(ref)
            if p is not None and round(-float(lines[0]["amount"]) * 100) != round(float(p["amount"]) * 100):
                sent, meant = -float(lines[0]["amount"]), float(p["amount"])
                self.add(
                    "payment_amount_mismatch", "escalate_open_item",
                    f"The bank sent {self.name(p['vendor_id'])} ${sent:,.2f}, not ${meant:,.2f}",
                    f"Payment {ref} for {nice(p['market_date'])} tokens is in the payout register as ${meant:,.2f}, but the bank "
                    f"statement shows ${sent:,.2f}, a difference of ${abs(sent - meant):,.2f}. Matching the reference alone "
                    "would have hidden this. The difference is parked in suspense until someone checks with the bank.",
                    "A payment matches only if the reference and the amount both agree.",
                    abs(sent - meant), [ev("bank_statement.csv", lines[0], "date", "amount", "reference"),
                                        ev("payout_register.csv", p, "payout_date", "amount", "ach_ref")],
                    market_date=p["market_date"], vendor_id=p["vendor_id"], difference=round(sent - meant, 2))
            if p is None:
                self.add("unrecorded_payment", "escalate_open_item", f"Bank payment {ref} is not in the payout register",
                         "Money left the bank with no record of why.", "Every payment needs a register entry.",
                         -round(float(lines[0]["amount"])), [ev("bank_statement.csv", b, "date", "amount", "reference") for b in lines])
            elif len(lines) > 1:
                amt = round(float(p["amount"]))
                self.add(
                    "duplicate_ach_payment", "escalate_open_item",
                    f"The bank paid {self.name(p['vendor_id'])} twice on {nice(p['payout_date'])}",
                    f"The payout register has one {usd(amt)} payment to {self.name(p['vendor_id'])} for {nice(p['market_date'])} "
                    f"tokens ({ref}), but the bank statement shows it {len(lines)} times. "
                    f"The market overpaid {usd(amt * (len(lines) - 1))}. Ask the bank to reverse the duplicate. If that fails, "
                    "the manager can approve deducting it from a future payment.",
                    "The payout register records what the market meant to pay. The bank shows what it actually paid. "
                    "Each reference should appear once in both.",
                    amt * (len(lines) - 1), [ev("bank_statement.csv", b, "date", "description", "amount", "reference") for b in lines]
                    + [ev("payout_register.csv", p, "payout_date", "vendor_id", "amount", "ach_ref")],
                    market_date=p["market_date"], vendor_id=p["vendor_id"])
        for ref, p in reg_by_ref.items():
            if ref not in bank_by_ref:
                self.add("payment_not_in_bank", "escalate_open_item", f"Register says {ref} was paid; the bank never sent it",
                         "The vendor may still be waiting for this money.", "Register and bank must agree.",
                         round(float(p["amount"])), [ev("payout_register.csv", p, "ach_ref", "amount")],
                         market_date=p["market_date"], vendor_id=p["vendor_id"])
        paid = defaultdict(int)
        for p in self.register:
            paid[(p["market_date"], p["vendor_id"])] += round(float(p["amount"]))
        wrong = [r for r in self.redemptions if r["market_date"] in self.paid_days
                 and paid[(r["market_date"], r["vendor_id"])] != int(r["snap_tokens"]) + int(r["match_tokens"])]
        for r in wrong:
            self.add("payout_amount_mismatch", "escalate_open_item",
                     f"{self.name(r['vendor_id'])} was paid a different amount than its tokens for {nice(r['market_date'])}",
                     "Payment should be $1 per token turned in.", self.config["rules"]["payouts"],
                     int(r["snap_tokens"]) + int(r["match_tokens"]), [ev("token_redemptions.csv", r, "snap_tokens", "match_tokens")],
                     market_date=r["market_date"], vendor_id=r["vendor_id"])
        if not wrong:
            self.clear("payouts", "Every past payment equals $1 per token turned in",
                       f"Checked {len(self.register)} payout register entries against the token count sheets.")

    def check_bank_balance(self):
        """The bank statement's running balance must equal the opening cash plus every line above it."""
        balance = next(a["balance"] for a in self.opening["accounts"] if a["code"] == "1000")
        for b in self.bank:
            balance = round(balance + float(b["amount"]), 2)
            stated = round(float(b["balance"]), 2)
            if balance != stated:
                self.add(
                    "bank_balance_mismatch", "escalate_open_item",
                    f"The bank statement doesn't add up at line {b['_line']}",
                    f"Adding up every line gives ${balance:,.2f} after {b['reference']} on {nice(b['date'])}, but the statement "
                    f"says ${stated:,.2f}. Either a line was changed or the export is broken. Get a fresh statement from the "
                    "bank. Until the cash ties out, no money decisions can be approved.",
                    "Each running balance equals the previous balance plus this line's amount.",
                    abs(balance - stated), [ev("bank_statement.csv", b, "date", "amount", "balance", "reference")],
                    market_date=b["date"])
                return
        self.clear("bank_balance", "The bank statement's running balance adds up",
                   f"Checked all {len(self.bank)} lines against the opening cash.")

    def check_bank_changes(self):
        """Bank details never change because of an email. Hold payment until a call to the number on file confirms it."""
        payout_day = self.config["next_payout_date"]
        for req in self.bank_changes:
            vid = req["vendor_id"]
            v = self.vendors[vid]
            pending_day = next((d for d in self.days if d not in self.paid_days), None)
            owed = sum(int(r["snap_tokens"]) + int(r["match_tokens"]) for r in self.redemptions
                       if r["vendor_id"] == vid and r["market_date"] == pending_day)
            self.add(
                "bank_detail_change", "hold_for_approval",
                f"Request to change {possessive(v['name'])} bank account the day before payday",
                f"On {req['received_at']}, an {req['channel']} from {req['from_address']} asked to send {v['name']}'s money to an "
                f"account ending {req['new_bank_last4']} instead of {v['bank_last4']}. It arrived the day before the "
                f"{nice(payout_day)} payment ({usd(owed)} is due). Changing bank details by email is a common way to "
                f"steal payments, so the agent did not change anything. It is holding {possessive(v['name'])} {usd(owed)} until someone "
                f"calls {v['phone_on_file']}, the number already on file, not one from the email. "
                "If the vendor confirms, the manager approves the change.",
                "Bank details change only after a call-back to the phone number on file, approved by the market manager.",
                owed, [ev("bank_change_requests.csv", req, "received_at", "channel", "from_address", "new_bank_last4"),
                       ev("vendors.csv", v, "vendor_id", "bank_last4", "phone_on_file")],
                hold={"vendor_id": vid, "market_date": pending_day, "amount": owed} if owed else None,
                vendor_id=vid)

    def check_grant(self):
        """Grant claims should equal match tokens turned in that month; and will the grant last the season?"""
        held_fake = sum(f.get("shortfall", 0) for f in self.findings if f["type"] == "match_float_negative")
        by_month = defaultdict(int)
        for r in self.redemptions:
            by_month[r["market_date"][:7]] += int(r["match_tokens"])
        first_month = self.days[0][:7]
        for c in self.claims:
            expected = by_month[c["month"]] + (self.opening["match_redeemed_not_yet_billed"] if c["month"] == first_month else 0)
            claimed = round(float(c["amount_claimed"]))
            receipt = next((b for b in self.bank if b["reference"] == c["claim_id"]), None)
            if claimed == expected and receipt and round(float(receipt["amount"])) == claimed:
                self.clear("grant_claims", f"Grant claim {c['claim_id']} matches", f"Claimed {usd(claimed)} = match tokens turned in, "
                           f"and the bank received {usd(claimed)} on {nice(receipt['date'])}.")
            else:
                self.add("grant_claim_mismatch", "escalate_open_item", f"Grant claim {c['claim_id']} doesn't match",
                         f"Expected {usd(expected)}, claimed {usd(claimed)}.", "Claims bill match tokens turned in.",
                         abs(claimed - expected), [ev("grant_claims.csv", c, "claim_id", "amount_claimed")])

        g = self.config["grant"]
        in_period = sum(by_month.values()) - held_fake
        used = g["used_before_period"] + in_period
        per_day = in_period / len(self.days)
        last = date.fromisoformat(self.days[-1])
        season_end = date.fromisoformat(g["season_last_market_day"])
        left_days = (season_end - last).days // 7
        left = g["annual_budget"] - used
        if per_day * left_days > left:
            days_funded = int(left // per_day)
            runs_out = last + timedelta(weeks=days_funded + 1)
            self.warnings.append({
                "id": "W1", "type": "grant_runway", "action": "escalate_open_item",
                "title": f"Match grant runs out around {runs_out:%b} {runs_out.day}",
                "explanation": f"{usd(used)} of the {usd(g['annual_budget'])} grant is used, leaving {usd(left)}. Match runs about "
                               f"{usd(per_day)} per Saturday, and {left_days} Saturdays are left in the season, which needs about "
                               f"{usd(per_day * left_days)}. The money runs out around {runs_out:%b} {runs_out.day}, "
                               f"about {left_days - days_funded - 1} Saturdays early. "
                               "The manager should ask the funder for more, or plan to lower the match cap.",
                "grant_left": left, "per_market_day": round(per_day), "remaining_market_days": left_days,
            })

    # ---------- the payment batch ----------

    def prepare_payout_batch(self):
        day = next((d for d in self.days if d not in self.paid_days), None)
        holds = defaultdict(list)
        for f in self.findings:
            if f["hold"] and f["hold"]["market_date"] == day:
                holds[f["hold"]["vendor_id"]].append(f)
        lines = []
        for r in self.redemptions:
            if r["market_date"] != day:
                continue
            vid = r["vendor_id"]
            gross = int(r["snap_tokens"]) + int(r["match_tokens"])
            held = min(gross, sum(f["hold"]["amount"] for f in holds[vid]))
            lines.append({
                "vendor_id": vid, "vendor": self.name(vid), "pay_to_account": self.vendors[vid]["bank_last4"],
                "snap_tokens": int(r["snap_tokens"]), "match_tokens": int(r["match_tokens"]),
                "gross": gross, "held": held, "pay_now": gross - held,
                "hold_reasons": [f["id"] for f in holds[vid]],
            })
        self.batch = {
            "batch_id": f"PAY-{self.config['next_payout_date']}", "for_market_date": day,
            "payout_date": self.config["next_payout_date"],
            "prepared_by": "agent", "status": "awaiting_approval",
            "requires_approval_by": "market_manager", "approved_by": None,
            "total_gross": sum(l["gross"] for l in lines), "total_held": sum(l["held"] for l in lines),
            "total_pay_now": sum(l["pay_now"] for l in lines), "lines": lines,
        }
        self.note("payout_batch", f"Prepared {self.batch['batch_id']}: {usd(self.batch['total_pay_now'])} to pay, "
                                  f"{usd(self.batch['total_held'])} held. Waiting for the market manager.")

    # ---------- run ----------

    def run(self):
        self.note("start", f"Read {len(self.src.read_log)} source files")
        for check in (self.check_terminal_duplicates, self.check_settlements, self.check_match_cap,
                      self.check_redemptions, self.check_token_float, self.check_vendor_payments,
                      self.check_bank_balance, self.check_bank_changes, self.check_grant):
            check()
            self.note("check", f"{check.__name__}: done")
        self.prepare_payout_batch()
        return {
            "run_at": datetime.now().isoformat(timespec="seconds"), "as_of": self.today,
            "sources_read": self.src.read_log,
            "summary": {a: sum(1 for f in self.findings if f["action"] == a)
                        for a in ("auto_resolve", "escalate_open_item", "hold_for_approval")},
            "findings": self.findings, "warnings": self.warnings, "cleared": self.cleared,
            "token_float": self.float_by_day, "payout_batch": self.batch, "audit_log": self.log,
        }


def main(data_dir=ROOT / "data", out_dir=ROOT / "output"):
    result = Agent(data_dir).run()
    out = Path(out_dir)
    out.mkdir(exist_ok=True)
    (out / "agent_run.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    r = main(*sys.argv[1:2])
    s = r["summary"]
    print(f"Findings: {len(r['findings'])}  (fixed alone {s['auto_resolve']}, open items {s['escalate_open_item']}, "
          f"need approval {s['hold_for_approval']})   Warnings: {len(r['warnings'])}   Checked and fine: {len(r['cleared'])}")
    for f in r["findings"] + r["warnings"]:
        print(f"  {f['id']:4} {f['action']:19} {f['title']}")
    b = r["payout_batch"]
    print(f"Payment batch {b['batch_id']}: pay {usd(b['total_pay_now'])}, hold {usd(b['total_held'])} -> {b['status']}")
    print("Full details: output/agent_run.json")
