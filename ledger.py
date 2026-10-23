"""
A small double-entry general ledger.

Double entry means every journal entry moves money between at least two accounts,
and the debits always equal the credits. So the books as a whole always balance.

    Example: a shopper swipes $15 at the booth.
      Debit  1100 EBT Settlement Receivable  $15   (the processor owes us $15)
      Credit 2000 SNAP Tokens Outstanding    $15   (we owe $15 to whoever turns the tokens in)

The ledger refuses, with an error, any entry that:
  - doesn't balance (debits != credits)
  - uses an account that isn't in the chart of accounts
  - has a line with no amount, a negative amount, or both a debit and a credit
  - is dated inside a closed (locked) period; corrections go in the open period

Amounts are whole cents (integers), so there is never any rounding.
"""


class LedgerError(Exception):
    pass


class Unbalanced(LedgerError):
    pass


class UnknownAccount(LedgerError):
    pass


class BadLine(LedgerError):
    pass


class PeriodLocked(LedgerError):
    pass


DEBIT_NORMAL = ("asset", "expense")     # these grow with debits; the rest grow with credits


def cents(dollars):
    return round(dollars * 100)


def dr(account, dollars, vendor_id=None):
    return {"account": account, "debit": cents(dollars), "credit": 0, "vendor_id": vendor_id}


def cr(account, dollars, vendor_id=None):
    return {"account": account, "debit": 0, "credit": cents(dollars), "vendor_id": vendor_id}


class Ledger:
    def __init__(self, accounts, locked_through=None):
        self.accounts = {a["code"]: {"code": a["code"], "name": a["name"], "type": a["type"]} for a in accounts}
        self.entries = []
        self.locked_through = locked_through

    def post(self, date, memo, lines, source, posted_by, approved_by=None):
        lines = [l for l in lines if l["debit"] or l["credit"]]     # drop zero lines, e.g. "match $0"
        if len(lines) < 2:
            raise BadLine(f"An entry needs at least two lines: {memo}")
        for l in lines:
            if l["account"] not in self.accounts:
                raise UnknownAccount(f"No account {l['account']} in the chart of accounts")
            if l["debit"] < 0 or l["credit"] < 0 or (l["debit"] and l["credit"]):
                raise BadLine(f"Each line is a positive debit or a positive credit, not both: {l}")
            if not isinstance(l["debit"], int) or not isinstance(l["credit"], int):
                raise BadLine(f"Amounts must be whole cents: {l}")
        debits, credits = sum(l["debit"] for l in lines), sum(l["credit"] for l in lines)
        if debits != credits:
            raise Unbalanced(f"Debits {debits / 100:.2f} != credits {credits / 100:.2f}: {memo}")
        if self.locked_through and date <= self.locked_through:
            raise PeriodLocked(f"Books are closed through {self.locked_through}; can't post on {date}. "
                               "Post a correcting entry in the open period instead.")
        entry = {"id": f"JE-{len(self.entries) + 1:04d}", "date": date, "memo": memo, "lines": lines,
                 "source": source, "posted_by": posted_by, "approved_by": approved_by}
        self.entries.append(entry)
        return entry

    def lock(self, through):
        self.locked_through = through

    def balance(self, code, vendor_id=None, as_of=None):
        """Balance in dollars, positive in the account's normal direction."""
        total = 0
        for e in self.entries:
            if as_of and e["date"] > as_of:
                continue
            for l in e["lines"]:
                if l["account"] == code and (vendor_id is None or l.get("vendor_id") == vendor_id):
                    total += l["debit"] - l["credit"]
        if self.accounts[code]["type"] not in DEBIT_NORMAL:
            total = -total
        return total / 100

    def trial_balance(self):
        rows = []
        for code, a in sorted(self.accounts.items()):
            d = sum(l["debit"] for e in self.entries for l in e["lines"] if l["account"] == code)
            c = sum(l["credit"] for e in self.entries for l in e["lines"] if l["account"] == code)
            net = d - c
            rows.append({"code": code, "name": a["name"], "type": a["type"],
                         "debit": max(net, 0) / 100, "credit": max(-net, 0) / 100})
        return rows

    def is_balanced(self):
        tb = self.trial_balance()
        return round(sum(r["debit"] for r in tb), 2) == round(sum(r["credit"] for r in tb), 2)

    def to_dict(self):
        return {"accounts": list(self.accounts.values()), "locked_through": self.locked_through, "entries": self.entries}

    @classmethod
    def from_dict(cls, d):
        """Rebuild by re-posting every saved entry, so a tampered save file fails the same checks."""
        ledger = cls(d["accounts"])
        for e in d["entries"]:
            ledger.post(e["date"], e["memo"], e["lines"], e["source"], e["posted_by"], e.get("approved_by"))
        ledger.lock(d["locked_through"])
        return ledger
