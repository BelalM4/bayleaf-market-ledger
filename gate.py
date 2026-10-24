"""
The approval gate: where the agent stops and a person decides.

The agent prepares; the market manager approves. The gate enforces:
  - Segregation of duties: whoever requested an action can never approve it.
    The agent requests everything, so the agent can never approve anything.
  - Permissions: only a user whose role has "decide_approvals" can decide. Callers
    must have signed that user in first (the web app uses server sessions; the CLI asks
    for the PIN).
  - Each request can be decided once. Some choices (overrides, "it was fine")
    require a written note.
  - Books first: no decision is accepted while the books don't tie out, and a decision
    that would break them is rolled back before anything is saved.
  - One payment, many blockers: each held payment lists everything blocking it (a bank
    change, a recount, an ineligible-token question...). Money is released only when no
    blocker remains, so the same dollars can never be released twice.
  - Frozen runs: each agent run is tied to fingerprints of the files it read. If those
    files change, the old run and its approvals are archived and marked superseded, and
    a fresh run starts. Old approvals never apply to new findings.
  - Money sent stays sent: every payment is recorded against what it paid for (a market
    day's tokens from one vendor), not against the batch's name or payday. A run that
    supersedes another starts from what was already paid, so nothing is paid twice, even
    if payday moves.
  - Current screens only: a decision must name the run it was made for. A decision from
    an out-of-date screen is refused.
  - Tamper-evident audit trail: every event is appended to state/audit.jsonl, each line
    chained to the one before by a hash, and every save records a fingerprint of the saved
    state in that chain.
  - One transaction per save: the new state is written to a temp file first, then its
    audit lines are appended (the commit point), then the file is swapped in. If anything
    fails before the commit, memory and the audit trail are rolled back. If it fails after,
    the next start finishes the swap. A missing, shortened or edited trail, or an edited
    state file, stops the gate from starting instead of silently starting over.

    python3 gate.py status
    python3 gate.py decide REQ-01 approve --as jordan.lee --pin 2468
    python3 gate.py decide REQ-05 pay_all_override --as jordan.lee --pin 2468 --note "Recounted, all real"
    python3 gate.py reset          # archive everything to state/history/ and start a fresh run
"""
import argparse
import copy
import getpass
import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

import agent as agent_mod
import books
from auth import check_pin
from ledger import Ledger, cr, dr

ROOT = Path(__file__).parent

# finding type -> [(option key, what the manager sees, note required?)]
OPTIONS = {
    "payout_batch": [("approve", "Approve and send these payments", False),
                     ("send_back", "Send back to the agent without paying", True)],
    "match_over_cap": [("write_off", "Don't bill the grant for the extra; the market covers it", False),
                       ("ask_funder", "Ask the funder to cover it anyway", True)],
    "redemption_while_absent": [("attendance_was_wrong", "The vendor was there; the attendance log is wrong", True),
                                ("recover_from_vendor", "The vendor wasn't there: don't pay, or recover the money", False)],
    "ineligible_vendor_snap": [("refuse", "Refuse: don't pay for tokens it may not accept", False),
                               ("pay_once_and_warn", "Pay this once and warn the vendor", True)],
    "match_float_negative": [("pay_verified_only", "Pay only the tokens that can exist", False),
                             ("pay_all_override", "Pay the full count anyway (override)", True)],
    "bank_detail_change": [("verified_by_phone", "Vendor confirmed on the phone number on file: change account", True),
                           ("reject_request", "Vendor says they didn't send it: keep old account", True)],
}
OPTIONS["ineligible_vendor_match"] = OPTIONS["ineligible_vendor_snap"]
OPTIONS["snap_float_negative"] = OPTIONS["match_float_negative"]

# Blockers that stop the whole payment (we don't trust where it goes, or whether the
# vendor was even there) versus ones that only cover some of its tokens.
WHOLE_LINE = {"bank_detail_change", "redemption_while_absent"}


class NotAllowed(Exception):
    pass


class IntegrityError(Exception):
    pass


def now():
    return datetime.now().isoformat(timespec="seconds")


class AuditLog:
    """Append-only, hash-chained event log. Editing, deleting or reordering a line breaks the chain."""

    GENESIS = "0" * 64

    def __init__(self, path):
        self.path = Path(path)
        self.entries = []      # written to disk
        self.pending = []      # staged in a transaction, not written yet

    @staticmethod
    def _hash(prev, entry):
        body = {k: entry[k] for k in ("seq", "at", "actor", "step", "detail")}
        return hashlib.sha256((prev + json.dumps(body, sort_keys=True)).encode()).hexdigest()

    @property
    def head(self):
        last = (self.entries + self.pending)[-1:]
        return last[0]["hash"] if last else self.GENESIS

    def stage(self, actor, step, detail):
        e = {"seq": len(self.entries) + len(self.pending) + 1, "at": now(), "actor": actor, "step": step,
             "detail": detail, "prev": self.head}
        e["hash"] = self._hash(e["prev"], e)
        self.pending.append(e)
        return e

    def flush(self):
        """Write every staged line in one write. This is the commit point of a transaction."""
        if not self.pending:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write("".join(json.dumps(e) + "\n" for e in self.pending))
            f.flush()
            os.fsync(f.fileno())
        self.entries.extend(self.pending)
        self.pending = []

    def discard(self):
        self.pending = []

    def append(self, actor, step, detail):
        e = self.stage(actor, step, detail)
        self.flush()
        return e

    @classmethod
    def load(cls, path):
        log = cls(path)
        prev = cls.GENESIS
        lines = Path(path).read_text().splitlines()
        for n, line in enumerate(lines, 1):
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                if n == len(lines):           # a write cut off mid-line by a crash: it never committed, so drop it
                    Path(path).write_text("".join(l + "\n" for l in lines[:-1]))
                    log.repaired = True
                    break
                raise IntegrityError(f"Audit trail line {n} is not valid JSON")
            if e.get("seq") != n or e.get("prev") != prev or e.get("hash") != cls._hash(prev, e):
                raise IntegrityError(f"Audit trail was altered at line {n}: its hash chain doesn't match")
            log.entries.append(e)
            prev = e["hash"]
        return log


class Gate:
    def __init__(self, state_path=ROOT / "state" / "gate.json", data_dir=ROOT / "data", reset=False):
        self.path = Path(state_path)
        self.audit_path = self.path.with_name("audit.jsonl")
        self.history = self.path.parent / "history"
        self.a = agent_mod.Agent(data_dir)
        fresh = self.a.run()
        self.config = self.a.config
        fingerprint = [[s["file"], s["sha256"]] for s in fresh["sources_read"]]

        if reset:
            self._archive_all("reset")
            self.log = AuditLog(self.audit_path)
            self._start(fresh, fingerprint, "reset: started a fresh run")
            return

        have_state, have_log = self.path.exists(), self.audit_path.exists()
        if not have_state and not have_log:
            self.log = AuditLog(self.audit_path)
            self._start(fresh, fingerprint, "first run")
            return
        if have_log and not have_state:
            n = len(self.audit_path.read_text().splitlines())
            raise IntegrityError(f"The saved state is missing, but the audit trail has {n} events. Refusing to start "
                                 "over silently. Run `python3 gate.py reset` to archive everything and start fresh.")
        if have_state and not have_log:
            raise IntegrityError("The audit trail is missing. Refusing to load state without its history. "
                                 "Run `python3 gate.py reset` to archive and start fresh.")

        self.log = AuditLog.load(self.audit_path)
        s = json.loads(self.path.read_text())
        checkpoints = [e for e in self.log.entries if e["step"] == "checkpoint"]
        tmp = self.path.with_name(self.path.name + ".tmp")
        if checkpoints and tmp.exists():
            staged = json.loads(tmp.read_text())
            if checkpoints[-1]["detail"] == f"state sha256 {self._state_hash(staged)}":
                os.replace(tmp, self.path)          # the audit trail committed this save; finish swapping it in
                s = staged
                self.log.append("agent", "recovered", "Finished a save that was interrupted after it committed")
            else:
                tmp.unlink()                        # a save that never committed
        if not checkpoints or checkpoints[-1]["detail"] != f"state sha256 {self._state_hash(s)}":
            raise IntegrityError("The saved state was changed outside the app: its fingerprint doesn't match the last "
                                 "checkpoint in the audit trail. Run `python3 gate.py reset` to archive and start fresh.")
        head = s["audit_head"]
        if head["count"] > len(self.log.entries) or self.log.entries[head["count"] - 1]["hash"] != head["hash"]:
            raise IntegrityError("The audit trail doesn't match the saved state (events were removed). "
                                 "Run `python3 gate.py reset` to archive and start fresh.")
        if s["fingerprint"] != fingerprint:
            changed = sorted({f for f, h in fingerprint} ^ {f for f, h in s["fingerprint"]}
                             | {f for (f, h) in fingerprint if [f, h] not in s["fingerprint"]})
            archived = self._archive_state(f"{s['run_id']}-superseded")
            decided = sum(1 for r in s["requests"] if r["status"] == "decided")
            paid = sum(l["paid"] for l in s["batch"]["lines"])
            self.log.append("agent", "superseded",
                            f"Source records changed ({', '.join(changed)}). Run {s['run_id']} and its {decided} decisions "
                            f"were superseded and archived to {archived}."
                            + (f" ${paid:,} of payments were already sent under it; check them against the next bank "
                               "statement." if paid else ""))
            self._start(fresh, fingerprint, f"sources changed; replaces {s['run_id']}", carry=s)
            return
        self.run_id, self.run, self.fingerprint = s["run_id"], s["run"], s["fingerprint"]
        self.ledger = Ledger.from_dict(s["ledger"])
        self.batch, self.requests, self.sent = s["batch"], s["requests"], s["sent"]

    @property
    def audit(self):
        return self.log.entries

    # ---------- runs and archives ----------

    def _archive_state(self, label):
        dest = self.history / f"{datetime.now():%Y%m%d-%H%M%S}-{label}"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.path, dest / "gate.json")
        return dest.relative_to(self.path.parent).as_posix()

    def _archive_all(self, label):
        """Move state and audit trail into history/ (never delete), and remember where the old chain ended."""
        self._previous = None
        if not self.path.exists() and not self.audit_path.exists():
            return
        dest = self.history / f"{datetime.now():%Y%m%d-%H%M%S}-{label}"
        dest.mkdir(parents=True, exist_ok=True)
        for p in (self.path, self.audit_path):
            if p.exists():
                shutil.move(str(p), str(dest / p.name))
        lines = (dest / "audit.jsonl").read_text().splitlines() if (dest / "audit.jsonl").exists() else []
        last = json.loads(lines[-1])["hash"] if lines else "none"
        self._previous = f"{dest.relative_to(self.path.parent).as_posix()} ({len(lines)} events, last hash {last[:12]})"

    def _start(self, run, fingerprint, why, carry=None):
        self.run, self.fingerprint = run, fingerprint
        self.run_id = f"RUN-{datetime.now():%m%d}-{uuid.uuid4().hex[:12]}"
        self.ledger = books.build(self.a)
        self.sent, self.requests = [], []
        self.batch = copy.deepcopy(run["payout_batch"])
        findings = {f["id"]: f for f in run["findings"]}
        for l in self.batch["lines"]:
            l.update(paid=0, forfeited=0, blocks=[])
            for fid in l["hold_reasons"]:
                f = findings[fid]
                l["blocks"].append({"finding": fid, "kind": f["type"], "amount": f["hold"]["amount"],
                                    "scope": "line" if f["type"] in WHOLE_LINE else "tokens"})
            self._recompute(l)
        if getattr(self, "_previous", None):
            self.log.append("agent", "reset", f"Previous state and audit trail archived to {self._previous}")
        self.log.append("agent", "run_started",
                        f"{self.run_id} ({why}). Read {len(fingerprint)} files: "
                        + ", ".join(f"{f} {h}" for f, h in fingerprint))
        for step in run["audit_log"]:
            self.log.append("agent", step["step"], step["detail"])
        if carry:
            self._carry_payments(carry)
        self.log.append("agent", "books", f"Posted {len(self.ledger.entries)} journal entries; closed periods through "
                                          f"{self.ledger.locked_through}")
        self._request("payout_batch", None, f"Approve payment batch {self.batch['batch_id']}", self.batch["total_pay_now"])
        for f in run["findings"]:
            if f["action"] == "hold_for_approval":
                self._request(f["type"], f, f["title"], f["amount"])
        self.save()

    def _request(self, kind, finding, title, amount):
        r = {"id": f"REQ-{len(self.requests) + 1:02d}", "kind": kind, "finding": finding and finding["id"],
             "title": title, "amount": amount, "vendor_id": finding and finding.get("vendor_id"),
             "requested_by": "agent", "requested_at": now(), "status": "pending", "decision": None,
             "options": [{"key": k, "label": lbl, "note_required": n} for k, lbl, n in OPTIONS[kind]]}
        self.requests.append(r)
        self.log.append("agent", "request_approval", f"{r['id']}: {title}")

    @staticmethod
    def _state_hash(state):
        body = {k: v for k, v in state.items() if k != "audit_head"}
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    def save(self):
        """Record the state's fingerprint in the audit chain, then write the state atomically."""
        state = {"run_id": self.run_id, "fingerprint": self.fingerprint, "run": self.run,
                 "ledger": self.ledger.to_dict(), "batch": self.batch, "requests": self.requests, "sent": self.sent}
        state = json.loads(json.dumps(state))          # the same plain-JSON form that load() will read back
        self.log.stage("agent", "checkpoint", f"state sha256 {self._state_hash(state)}")
        state["audit_head"] = {"count": len(self.log.entries) + len(self.log.pending), "hash": self.log.head}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:                                             # 1. stage the new state on disk
            with open(tmp, "w") as f:
                f.write(json.dumps(state, indent=1))
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            self.log.discard()
            raise
        try:                                             # 2. commit: append the audit lines
            self.log.flush()
        except Exception:
            self.log.discard()
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self.path)                       # 3. swap in (if this fails, the next start finishes it)

    @property
    def vendor_accounts(self):
        """Where each vendor is paid, besides the account on file. Only a phone-verified decision can change it,
        so it's worked out from the decisions every time and never stored on its own."""
        out = {}
        for r in self.requests:
            if r["kind"] == "bank_detail_change" and r["decision"] and r["decision"]["option"] == "verified_by_phone":
                req = next(b for b in self.a.bank_changes if b["vendor_id"] == r["vendor_id"])
                out[r["vendor_id"]] = req["new_bank_last4"]
        return out

    def _carry_payments(self, old):
        """A superseding run starts from the money the old run already sent."""
        # match on what was paid for (market day + vendor), never on the batch name, which includes payday
        carried = [p for p in old.get("sent", []) if p["market_date"] == self.batch["for_market_date"]]
        for p in carried:
            l = next((l for l in self.batch["lines"] if l["vendor_id"] == p["vendor_id"]), None)
            if l is None:
                continue
            self.ledger.post(p["date"], f"Payment already sent to {l['vendor']} under {old['run_id']} "
                                        f"(account ending {p['account']}); carried over, not paid again",
                             [dr("2100", p["amount"], p["vendor_id"]), cr("1000", p["amount"])],
                             {"kind": "payment", "carried_from": old["run_id"], "request": p["request"]},
                             posted_by="agent", approved_by=p["approved_by"])
            l["paid"] += p["amount"]
            l["pay_to_account"] = p["account"]
            self.sent.append({**p, "carried_from": old["run_id"]})
        if carried:
            total = sum(p["amount"] for p in carried)
            self.log.append("agent", "carried_payments",
                            f"{len(carried)} payments (${total:,}) already sent under {old['run_id']} were carried into "
                            f"{self.run_id}. Approving again only pays what's still due.")

    # ---------- signing in ----------

    def authenticate(self, user, pin):
        return check_pin(self.config["users"], user, pin)

    # ---------- deciding ----------

    def request(self, req_id):
        r = next((r for r in self.requests if r["id"] == req_id), None)
        if r is None:
            raise NotAllowed(f"No request {req_id}")
        return r

    def _refuse(self, actor, req_id, option, why):
        self.log.append(actor, "refused", f"{req_id} {option}: {why}")
        self.save()
        raise NotAllowed(why)

    def decide(self, req_id, option, actor, note="", run_id=None):
        """`actor` must already be signed in by the caller (web session or CLI PIN).
        `run_id` is the run the person was looking at; the web app always sends it."""
        if run_id is not None and run_id != self.run_id:
            self._refuse(actor, req_id, option, f"This screen is out of date: it shows {run_id}, but the current run is "
                         f"{self.run_id} (the records changed). Reload and decide again.")
        r = self.request(req_id)
        user = self.config["users"].get(actor)
        if user is None:
            self._refuse(actor, req_id, option, f"Unknown user '{actor}'")
        if actor == r["requested_by"]:
            self._refuse(actor, req_id, option, f"Segregation of duties: {user['name']} requested {req_id}, so they can't approve it.")
        if "decide_approvals" not in self.config["permissions"][user["role"]]:
            self._refuse(actor, req_id, option, f"{user['name']} is a {user['role'].replace('_', ' ')} and can't decide approvals.")
        if r["status"] != "pending":
            self._refuse(actor, req_id, option, f"{req_id} was already decided by {r['decision']['by']}.")
        opt = next((o for o in r["options"] if o["key"] == option), None)
        if opt is None:
            self._refuse(actor, req_id, option, f"'{option}' is not an option for {req_id}: {[o['key'] for o in r['options']]}")
        if opt["note_required"] and not note.strip():
            self._refuse(actor, req_id, option, f"'{opt['label']}' needs a note explaining why.")
        broken = [c["name"] for c in self.tie_outs() if not c["ok"]]
        if broken:
            self._refuse(actor, req_id, option, "The books don't tie out right now (" + "; ".join(broken) + "), so no "
                         "decisions can be approved until that's fixed. See the open findings.")

        before = (len(self.ledger.entries), copy.deepcopy(self.batch), copy.deepcopy(self.sent),
                  copy.deepcopy(self.requests))

        def roll_back():
            del self.ledger.entries[before[0]:]
            self.batch, self.sent, self.requests = before[1], before[2], before[3]

        try:
            # the decision is recorded first, because some effects (like a confirmed bank account) depend on it
            r["status"] = "decided"
            r["decision"] = {"option": option, "label": opt["label"], "by": actor, "at": now(), "note": note, "entries": []}
            entries = self._apply(r, option, actor, note)
        except Exception as e:
            roll_back()
            self._refuse(actor, req_id, option, f"The decision failed while posting ({type(e).__name__}: {e}), so it was "
                                                f"rolled back completely and nothing was saved.")
        broken = [c["name"] for c in self.tie_outs() if not c["ok"]]
        if broken:
            roll_back()
            self._refuse(actor, req_id, option, "That decision would have broken the books (" + "; ".join(broken)
                         + "), so it was rolled back and nothing was posted.")
        r["decision"]["entries"] = [e["id"] for e in entries]
        self.log.stage(actor, "decide", f"{req_id} -> {opt['label']}" + (f' ("{note}")' if note else "")
                       + (f"; posted {', '.join(e['id'] for e in entries)}" if entries else ""))
        try:
            self.save()
        except OSError as e:
            if not self.log.pending and self.path.with_name(self.path.name + ".tmp").exists():
                raise NotAllowed(f"The decision was recorded, but the final file swap failed ({e}). "
                                 "It will be finished automatically when the app restarts.")
            self.log.discard()
            roll_back()
            raise NotAllowed(f"Couldn't save the decision ({e}), so it was rolled back completely: nothing was "
                             "paid, posted or written to the audit trail.")
        return r

    # ---------- holds ----------

    @staticmethod
    def _recompute(l):
        """Held = everything a blocker covers. A whole-line blocker holds all of it; token blockers add up."""
        owed = l["gross"] - l["forfeited"]
        if any(b["scope"] == "line" for b in l["blocks"]):
            l["held"] = owed
        else:
            l["held"] = min(owed, sum(b["amount"] for b in l["blocks"]))
        l["pay_now"] = owed - l["held"]

    def _totals(self):
        lines = self.batch["lines"]
        self.batch["total_pay_now"] = sum(l["pay_now"] for l in lines)
        self.batch["total_held"] = sum(l["held"] for l in lines)

    def _apply(self, r, option, actor, note):
        today = self.config["today"]
        posted = []
        f = next((x for x in self.run["findings"] if x["id"] == r["finding"]), None)

        def post(memo, lines, kind, date=today):
            e = self.ledger.post(date, memo, lines, {"kind": kind, "request": r["id"], "finding": r["finding"], "note": note},
                                 posted_by="agent", approved_by=actor)
            posted.append(e)

        def line(vid):
            return next(l for l in self.batch["lines"] if l["vendor_id"] == vid)

        def pay_whats_due(l, why):
            due = l["pay_now"] - l["paid"]
            if due <= 0 or self.batch["status"] != "approved":
                return
            acct = self.vendor_accounts.get(l["vendor_id"], l["pay_to_account"])
            post(f"Pay {l['vendor']} {why} to account ending {acct}",
                 [dr("2100", due, l["vendor_id"]), cr("1000", due)], "payment", self.config["next_payout_date"])
            l["paid"] += due
            l["pay_to_account"] = acct
            self.sent.append({"batch_id": self.batch["batch_id"], "market_date": self.batch["for_market_date"],
                              "vendor_id": l["vendor_id"], "amount": due, "account": acct,
                              "date": self.config["next_payout_date"], "request": r["id"], "approved_by": actor,
                              "run_id": self.run_id, "at": now()})

        def resolve(vid, fid, forfeits=(), why=""):
            """Remove one blocker. `forfeits` = [(amount, credit account)] that won't be paid at all.
            Whatever is no longer blocked by anything moves from on-hold to payable, exactly once."""
            l = line(vid)
            held_before = l["held"]
            l["blocks"] = [b for b in l["blocks"] if b["finding"] != fid]
            for amount, account in forfeits:
                if amount:
                    post(f"{why}: {l['vendor']} won't be paid for ${amount} ({fid})",
                         [dr("2110", amount, vid), cr(account, amount)], "forfeit" if account == "4100" else
                         "recover_tokens" if account in ("2000", "2010") else "resolve_suspense")
                    l["forfeited"] += amount
            self._recompute(l)
            released = held_before - sum(a for a, _ in forfeits) - l["held"]
            assert released >= 0, "release can never be negative"
            if released:
                post(f"{why}: release ${released} for {l['vendor']} ({fid})",
                     [dr("2110", released, vid), cr("2100", released, vid)], "release_hold")
            pay_whats_due(l, "(released hold)")

        k = r["kind"]
        pending = f is not None and f.get("hold") is not None
        if k == "payout_batch":
            if option == "approve":
                self.batch.update(status="approved", approved_by=actor)
                for l in self.batch["lines"]:
                    pay_whats_due(l, f"for {self.batch['for_market_date']} tokens")
            else:
                self.batch["status"] = "sent_back"
        elif k == "match_over_cap" and option == "write_off":
            post(f"Match over the cap on {f['market_date']}: not billable to the grant ({f['id']})",
                 [dr("5000", f["amount"]), cr("1210", f["amount"])], "write_off")
        elif k == "redemption_while_absent":
            if pending and option == "attendance_was_wrong":
                resolve(f["vendor_id"], f["id"], why="Vendor confirmed present")
            elif pending:                    # not paid yet: don't pay, and put the tokens back as unspent
                l = line(f["vendor_id"])
                rest = l["gross"] - l["forfeited"]
                snap = min(l["snap_tokens"], rest)
                resolve(f["vendor_id"], f["id"], [(snap, "2000"), (rest - snap, "2010")], why="Vendor wasn't there")
            elif option == "recover_from_vendor":   # already paid: the vendor owes it back
                post(f"Reverse {f['market_date']} turn-in by an absent vendor; the books for that month are closed, "
                     f"so the correction is dated {today} ({f['id']})",
                     [dr("1410", f["amount"], f["vendor_id"]), cr("2000", f["amount"])], "recover_tokens")
        elif k in ("ineligible_vendor_snap", "ineligible_vendor_match"):
            forfeits = [(f["amount"], "4100")] if option == "refuse" else []
            resolve(f["vendor_id"], f["id"], forfeits, why="Refuse ineligible tokens" if forfeits else "Pay once by exception")
        elif k in ("match_float_negative", "snap_float_negative"):
            if option == "pay_verified_only":
                resolve(f["vendor_id"], f["id"], [(f["shortfall"], "1900")], why="Pay only tokens that can exist")
            else:
                resolve(f["vendor_id"], f["id"], why="Override: pay the full count")
                if f["shortfall"]:
                    post(f"{f['shortfall']} tokens paid that could not exist: program loss ({f['id']})",
                         [dr("5010", f["shortfall"]), cr("1900", f["shortfall"])], "resolve_suspense")
        elif k == "bank_detail_change":
            vid = f["vendor_id"]
            if option == "verified_by_phone":
                line(vid)["pay_to_account"] = self.vendor_accounts[vid]
            if pending:
                resolve(vid, f["id"], why=f"Bank change {option.replace('_', ' ')}")
        self._totals()
        return posted

    # ---------- reading ----------

    def tie_outs(self):
        checks = books.tie_outs(self.a, self.ledger, self.batch)
        on_file = {vid: v["bank_last4"] for vid, v in self.a.vendors.items()}
        allowed = {**on_file, **self.vendor_accounts}
        wrong = [l["vendor"] for l in self.batch["lines"] if l["pay_to_account"] != allowed[l["vendor_id"]]]
        wrong += [f"{p['vendor_id']} payment" for p in self.sent if p["account"] != allowed[p["vendor_id"]]]
        checks.append({"name": "Every payment goes to the account on file, or one confirmed by phone",
                       "ledger": len(wrong), "outside": 0, "source": "vendors.csv and approved bank changes",
                       "ok": not wrong, "explains": ", ".join(wrong)})
        posted = sum(l["debit"] for e in self.ledger.entries if e["source"].get("kind") == "payment"
                     for l in e["lines"] if l["account"] == "2100") / 100
        recorded = sum(p["amount"] for p in self.sent)
        checks.append({"name": "Payments in the books = payments recorded as sent", "ledger": posted, "outside": recorded,
                       "source": "payment record", "ok": round(posted, 2) == round(recorded, 2), "explains": ""})
        return checks

    def status(self):
        checks = self.tie_outs()
        return {"run_id": self.run_id, "requests": self.requests, "batch": self.batch, "checks": checks,
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
    d.add_argument("--pin", help="your sign-in PIN (asked for if left out)")
    d.add_argument("--note", default="")
    args = p.parse_args(argv)

    try:
        g = Gate(reset=args.cmd == "reset")
    except IntegrityError as e:
        print(f"STOPPED: {e}")
        return
    if args.cmd == "decide":
        pin = args.pin if args.pin is not None else getpass.getpass(f"PIN for {args.actor}: ")
        if not g.authenticate(args.actor, pin):
            print(f"REFUSED: couldn't sign in as {args.actor}.")
        else:
            try:
                r = g.decide(args.request, args.option, args.actor, args.note)
                print(f"{r['id']} decided: {r['decision']['label']}. Posted: {', '.join(r['decision']['entries']) or 'nothing'}")
            except NotAllowed as e:
                print(f"REFUSED: {e}")
    s = g.status()
    print(f"\n{s['run_id']}: {s['entries']} journal entries. Tie-outs: {'all OK' if s['all_tie_outs_ok'] else 'PROBLEM'}")
    for c in s["checks"]:
        if not c["ok"]:
            print(f"  FAIL {c['name']}: ledger {c['ledger']} vs {c['outside']}")
    b = s["batch"]
    print(f"Batch {b['batch_id']}: {b['status']}")
    for l in b["lines"]:
        blockers = ", ".join(x["finding"] for x in l["blocks"])
        print(f"  {l['vendor']:24} owed ${l['gross']:>4}  pay ${l['pay_now']:>4}  held ${l['held']:>4}  "
              f"sent ${l['paid']:>4}  refused ${l['forfeited']:>3}  -> acct {l['pay_to_account']}"
              + (f"  blocked by {blockers}" if blockers else ""))
    print("Requests:")
    for r in s["requests"]:
        d = r["decision"]
        print(f"  {r['id']} [{r['status']:8}] {r['title']}" + (f"  => {d['label']} (by {d['by']})" if d else ""))
        if r["status"] == "pending":
            print("            options: " + ", ".join(o["key"] + ("*" if o["note_required"] else "") for o in r["options"])
                  + "   (* needs --note)")


if __name__ == "__main__":
    main()
