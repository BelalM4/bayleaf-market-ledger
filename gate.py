"""
The approval gate: where the agent stops and a person decides.

The agent prepares; the market manager approves. The gate enforces:
  - Segregation of duties: whoever requested an action can never approve it.
    The agent requests everything, so the agent can never approve anything.
  - Permissions: only a user whose role has "decide_approvals" can decide.
  - Each request can be decided once. Some choices (overrides, "it was fine")
    require a written note.
  - A decision posts its journal entries through the same ledger rules as
    everything else, marked with who approved it, and is written to the audit trail.

    python3 gate.py status
    python3 gate.py decide REQ-01 approve --as jordan.lee
    python3 gate.py decide REQ-06 pay_all_override --as jordan.lee --note "Recounted, all real"
    python3 gate.py reset          # start over from the agent's fresh run
"""
import argparse
import copy
import json
from datetime import datetime
from pathlib import Path

import agent as agent_mod
import books
from ledger import Ledger, cr, dr

ROOT = Path(__file__).parent

# finding type -> [(option key, what the manager sees, note required?)]
OPTIONS = {
    "payout_batch": [("approve", "Approve and send these payments", False),
                     ("send_back", "Send back to the agent without paying", True)],
    "match_over_cap": [("write_off", "Don't bill the grant for the extra; the market covers it", False),
                       ("ask_funder", "Ask the funder to cover it anyway", True)],
    "redemption_while_absent": [("attendance_was_wrong", "The vendor was there; the attendance log is wrong", True),
                                ("recover_from_vendor", "The vendor wasn't there; recover the money", False)],
    "ineligible_vendor_snap": [("refuse", "Refuse: don't pay for tokens it may not accept", False),
                               ("pay_once_and_warn", "Pay this once and warn the vendor", True)],
    "match_float_negative": [("pay_verified_only", "Pay only the tokens that can exist", False),
                             ("pay_all_override", "Pay the full count anyway (override)", True)],
    "bank_detail_change": [("verified_by_phone", "Vendor confirmed on the phone number on file: change account", True),
                           ("reject_request", "Vendor says they didn't send it: keep old account", True)],
}
OPTIONS["ineligible_vendor_match"] = OPTIONS["ineligible_vendor_snap"]
OPTIONS["snap_float_negative"] = OPTIONS["match_float_negative"]


class NotAllowed(Exception):
    pass


def now():
    return datetime.now().isoformat(timespec="seconds")


class Gate:
    def __init__(self, state_path=ROOT / "state" / "gate.json", data_dir=ROOT / "data", reset=False):
        self.path = Path(state_path)
        self.a = agent_mod.Agent(data_dir)
        self.run = self.a.run()
        self.config = self.a.config
        if self.path.exists() and not reset:
            s = json.loads(self.path.read_text())
            self.ledger = Ledger.from_dict(s["ledger"])
            self.batch, self.requests, self.audit = s["batch"], s["requests"], s["audit"]
            self.vendor_accounts = s["vendor_accounts"]
        else:
            self._start_fresh()

    def _start_fresh(self):
        self.ledger = books.build(self.a)
        self.batch = copy.deepcopy(self.run["payout_batch"])
        for l in self.batch["lines"]:
            l.update(paid=0, forfeited=0)
        self.vendor_accounts = {}
        self.audit = list(self.run["audit_log"])
        self.audit.append({"at": now(), "actor": "agent", "step": "books",
                           "detail": f"Posted {len(self.ledger.entries)} journal entries; closed periods through "
                                     f"{self.ledger.locked_through}"})
        self.requests = []
        self._request("payout_batch", None, f"Approve payment batch {self.batch['batch_id']}",
                      self.batch["total_pay_now"])
        for f in self.run["findings"]:
            if f["action"] == "hold_for_approval":
                self._request(f["type"], f, f["title"], f["amount"])
        self.save()

    def _request(self, kind, finding, title, amount):
        r = {"id": f"REQ-{len(self.requests) + 1:02d}", "kind": kind, "finding": finding and finding["id"],
             "title": title, "amount": amount, "vendor_id": finding and finding.get("vendor_id"),
             "requested_by": "agent", "requested_at": now(), "status": "pending", "decision": None,
             "options": [{"key": k, "label": lbl, "note_required": n} for k, lbl, n in OPTIONS[kind]]}
        self.requests.append(r)
        self.audit.append({"at": r["requested_at"], "actor": "agent", "step": "request_approval",
                           "detail": f"{r['id']}: {title}"})

    def save(self):
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps({"ledger": self.ledger.to_dict(), "batch": self.batch, "requests": self.requests,
                                         "audit": self.audit, "vendor_accounts": self.vendor_accounts}, indent=1))

    # ---------- deciding ----------

    def request(self, req_id):
        r = next((r for r in self.requests if r["id"] == req_id), None)
        if r is None:
            raise NotAllowed(f"No request {req_id}")
        return r

    def decide(self, req_id, option, actor, note=""):
        r = self.request(req_id)
        user = self.config["users"].get(actor)
        try:
            if user is None:
                raise NotAllowed(f"Unknown user '{actor}'")
            if actor == r["requested_by"]:
                raise NotAllowed(f"Segregation of duties: {user['name']} requested {req_id}, so they can't approve it.")
            if "decide_approvals" not in self.config["permissions"][user["role"]]:
                raise NotAllowed(f"{user['name']} is a {user['role'].replace('_', ' ')} and can't decide approvals.")
            if r["status"] != "pending":
                raise NotAllowed(f"{req_id} was already decided by {r['decision']['by']}.")
            opt = next((o for o in r["options"] if o["key"] == option), None)
            if opt is None:
                raise NotAllowed(f"'{option}' is not an option for {req_id}: {[o['key'] for o in r['options']]}")
            if opt["note_required"] and not note.strip():
                raise NotAllowed(f"'{opt['label']}' needs a note explaining why.")
        except NotAllowed as e:
            self.audit.append({"at": now(), "actor": actor, "step": "refused", "detail": f"{req_id} {option}: {e}"})
            self.save()
            raise
        entries = self._apply(r, option, actor, note)
        r["status"] = "decided"
        r["decision"] = {"option": option, "label": opt["label"], "by": actor, "at": now(), "note": note,
                         "entries": [e["id"] for e in entries]}
        self.audit.append({"at": r["decision"]["at"], "actor": actor, "step": "decide",
                           "detail": f"{req_id} -> {opt['label']}" + (f' ("{note}")' if note else "")
                                     + (f"; posted {', '.join(e['id'] for e in entries)}" if entries else "")})
        self.save()
        return r

    def _apply(self, r, option, actor, note):
        today = self.config["today"]
        posted = []
        f = next((f for f in self.run["findings"] if f["id"] == r["finding"]), None)

        def post(memo, lines, kind, date=today):
            e = self.ledger.post(date, memo, lines, {"kind": kind, "request": r["id"], "finding": r["finding"], "note": note},
                                 posted_by="agent", approved_by=actor)
            posted.append(e)
            return e

        def line(vid):
            return next(l for l in self.batch["lines"] if l["vendor_id"] == vid)

        def pay(l, amount, why):
            if amount <= 0:
                return
            acct = self.vendor_accounts.get(l["vendor_id"], l["pay_to_account"])
            post(f"Pay {l['vendor']} {why} to account ending {acct}",
                 [dr("2100", amount, l["vendor_id"]), cr("1000", amount)], "payment", self.config["next_payout_date"])
            l["paid"] += amount
            l["pay_to_account"] = acct

        def release(vid, amount, memo):
            l = line(vid)
            post(memo, [dr("2110", amount, vid), cr("2100", amount, vid)], "release_hold")
            l["held"] -= amount
            l["pay_now"] += amount
            if self.batch["status"] == "approved":
                pay(l, amount, "(released hold)")

        k = r["kind"]
        if k == "payout_batch":
            if option == "approve":
                self.batch.update(status="approved", approved_by=actor)
                for l in self.batch["lines"]:
                    pay(l, l["pay_now"] - l["paid"], f"for {self.batch['for_market_date']} tokens")
            else:
                self.batch["status"] = "sent_back"
        elif k == "match_over_cap" and option == "write_off":
            post(f"Match over the cap on {f['market_date']}: not billable to the grant ({f['id']})",
                 [dr("5000", f["amount"]), cr("1210", f["amount"])], "write_off")
        elif k == "redemption_while_absent" and option == "recover_from_vendor":
            post(f"Reverse {f['market_date']} turn-in by an absent vendor; the books for that month are closed, "
                 f"so the correction is dated {today} ({f['id']})",
                 [dr("1410", f["amount"], f["vendor_id"]), cr("2000", f["amount"])], "recover_tokens")
        elif k in ("ineligible_vendor_snap", "ineligible_vendor_match"):
            if option == "refuse":
                l = line(f["vendor_id"])
                post(f"Refuse tokens {l['vendor']} may not accept ({f['id']})",
                     [dr("2110", f["amount"], f["vendor_id"]), cr("4100", f["amount"])], "forfeit")
                l["held"] -= f["amount"]
                l["forfeited"] += f["amount"]
            else:
                release(f["vendor_id"], f["amount"], f"Pay held tokens once, by exception ({f['id']})")
        elif k in ("match_float_negative", "snap_float_negative"):
            vid, n, short = f["vendor_id"], f["amount"], f["shortfall"]
            l = line(vid)
            if option == "pay_verified_only":
                post(f"Pay {n - short} of {n} held tokens; {short} could not exist ({f['id']})",
                     [dr("2110", n, vid), cr("2100", n - short, vid), cr("1900", short)], "resolve_suspense")
                l["held"] -= n
                l["pay_now"] += n - short
                l["forfeited"] += short
                if self.batch["status"] == "approved":
                    pay(l, n - short, "(verified tokens)")
            else:
                release(vid, n, f"Override: pay all {n} held tokens ({f['id']})")
                post(f"{short} tokens paid that could not exist: program loss ({f['id']})",
                     [dr("5010", short), cr("1900", short)], "resolve_suspense")
        elif k == "bank_detail_change":
            vid = f["vendor_id"]
            if option == "verified_by_phone":
                req = next(b for b in self.a.bank_changes if b["vendor_id"] == vid)
                self.vendor_accounts[vid] = req["new_bank_last4"]
                line(vid)["pay_to_account"] = req["new_bank_last4"]
            if f["hold"]:
                release(vid, f["hold"]["amount"], f"Release bank-change hold: {option.replace('_', ' ')} ({f['id']})")
        return posted

    # ---------- reading ----------

    def tie_outs(self):
        return books.tie_outs(self.a, self.ledger, self.batch)

    def status(self):
        checks = self.tie_outs()
        return {"requests": self.requests, "batch": self.batch, "checks": checks,
                "all_tie_outs_ok": all(c["ok"] for c in checks), "entries": len(self.ledger.entries)}


def main(argv=None):
    p = argparse.ArgumentParser(description="Bayleaf approval gate")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("reset")
    d = sub.add_parser("decide")
    d.add_argument("request")
    d.add_argument("option")
    d.add_argument("--as", dest="actor", required=True)
    d.add_argument("--note", default="")
    args = p.parse_args(argv)

    g = Gate(reset=args.cmd == "reset")
    if args.cmd == "decide":
        try:
            r = g.decide(args.request, args.option, args.actor, args.note)
            print(f"{r['id']} decided: {r['decision']['label']}. Posted: {', '.join(r['decision']['entries']) or 'nothing'}")
        except NotAllowed as e:
            print(f"REFUSED: {e}")
    s = g.status()
    print(f"\n{s['entries']} journal entries. Tie-outs: {'all OK' if s['all_tie_outs_ok'] else 'PROBLEM'}")
    for c in s["checks"]:
        if not c["ok"]:
            print(f"  FAIL {c['name']}: ledger {c['ledger']} vs {c['outside']}")
    b = s["batch"]
    print(f"Batch {b['batch_id']}: {b['status']}")
    for l in b["lines"]:
        print(f"  {l['vendor']:24} gross ${l['gross']:>4}  pay ${l['pay_now']:>4}  held ${l['held']:>4}  "
              f"sent ${l['paid']:>4}  refused ${l['forfeited']:>3}  -> acct {l['pay_to_account']}")
    print("Requests:")
    for r in s["requests"]:
        d = r["decision"]
        print(f"  {r['id']} [{r['status']:8}] {r['title']}" + (f"  => {d['label']} (by {d['by']})" if d else ""))
        if r["status"] == "pending":
            print("            options: " + ", ".join(o["key"] + ("*" if o["note_required"] else "") for o in r["options"])
                  + "   (* needs --note)")


if __name__ == "__main__":
    main()
