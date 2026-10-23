"""
"What if...?" scenarios for the live demo.

Each scenario copies the data to a scratch folder, changes one thing, and re-runs
the agent there. The real data and the approval queue are never touched.
"""
import csv
import shutil
import tempfile
from pathlib import Path

import agent

ROOT = Path(__file__).parent


def edit_csv(path, change):
    with open(path) as f:
        reader = csv.DictReader(f)
        fields, rows = reader.fieldnames, list(reader)
    rows = change(rows) or rows
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def late_deposit_never_came(d, **_):
    edit_csv(d / "bank_statement.csv", lambda rows: [r for r in rows if r["reference"] != "EBT-0919-L1"])


def honest_green_acres(d, **_):
    def fix(rows):
        for r in rows:
            if (r["market_date"], r["vendor_id"]) == ("2026-10-17", "V01"):
                r["match_tokens"] = str(int(r["match_tokens"]) - 40)
    edit_csv(d / "token_redemptions.csv", fix)


def second_bank_email(d, **_):
    edit_csv(d / "bank_change_requests.csv", lambda rows: rows.append(
        {**rows[0], "vendor_id": "V02", "new_bank_last4": "1111", "received_at": "2026-10-19 08:15",
         "from_address": "sunnyslope.billing@mailbox-secure.example"}))


def candles_take_match(d, **_):
    edit_csv(d / "token_redemptions.csv", lambda rows: rows.append(
        {"market_date": "2026-10-17", "vendor_id": "V08", "snap_tokens": "0", "match_tokens": "6", "counted_by": "Dana"}))
    edit_csv(d / "attendance.csv", lambda rows: [
        {**r, "present": "True"} if (r["market_date"], r["vendor_id"]) == ("2026-10-17", "V08") else r for r in rows])


def change_count(d, vendor_id="V02", market_date="2026-10-17", kind="match", delta=30, **_):
    """A judge picks a vendor, a day and how many tokens to add (or remove)."""
    delta = int(delta)
    field = f"{kind}_tokens"

    def fix(rows):
        row = next((r for r in rows if (r["market_date"], r["vendor_id"]) == (market_date, vendor_id)), None)
        if row is None:
            row = {"market_date": market_date, "vendor_id": vendor_id, "snap_tokens": "0", "match_tokens": "0",
                   "counted_by": "Dana"}
            rows.append(row)
        row[field] = str(max(0, int(row[field]) + delta))
    edit_csv(d / "token_redemptions.csv", fix)


SCENARIOS = {
    "late_deposit_never_came": ("The Sep 19 late deposit never arrives",
                                "Delete the $20 'late item' deposit from the bank statement.", late_deposit_never_came),
    "honest_green_acres": ("Green Acres' count was honest",
                           "Take the 40 extra match tokens out of Green Acres' Oct 17 count.", honest_green_acres),
    "second_bank_email": ("A second bank-change email arrives",
                          "Sunny Slope Orchards gets a suspicious bank-change email too.", second_bank_email),
    "candles_take_match": ("The candle stall turns in match tokens",
                           "Wildflower Candles (not food) turns in 6 match tokens on Oct 17.", candles_take_match),
    "change_count": ("Change any vendor's token count",
                     "Pick a vendor, a market day and how many tokens to add or remove.", change_count),
}


def key(f):
    return (f["type"], f.get("market_date"), f.get("vendor_id"))


def run(name, params=None, data_dir=ROOT / "data"):
    title, description, change = SCENARIOS[name]
    base = agent.Agent(data_dir).run()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for f in Path(data_dir).iterdir():
            if f.name in agent.ALLOWED_SOURCES:      # the scenario copy never includes the answer key
                shutil.copy(f, tmp)
        change(tmp, **(params or {}))
        result = agent.Agent(tmp).run()
    before, after = {key(f) for f in base["findings"]}, {key(f) for f in result["findings"]}
    return {
        "name": name, "title": title, "description": description, "params": params or {},
        "new_findings": [f for f in result["findings"] if key(f) not in before],
        "gone_findings": [f for f in base["findings"] if key(f) not in after],
        "changed_findings": [{"before": b, "after": a} for b in base["findings"] for a in result["findings"]
                             if key(a) == key(b) and (a["amount"], a["title"]) != (b["amount"], b["title"])],
        "batch_before": base["payout_batch"], "batch_after": result["payout_batch"],
        "findings": result["findings"],
    }
