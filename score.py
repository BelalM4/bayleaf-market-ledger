"""
Grades the agent against the answer key. The agent never sees the key; only this script does.

    python3 score.py        # runs the agent on data/, then prints the scorecard

A planted mistake counts as caught when a finding has the same type and points at
the same market day / vendor / shopper. It also has to choose the right action.
Any finding that matches nothing planted is a false alarm.
"""
import json
import sys
from pathlib import Path

import agent

ROOT = Path(__file__).parent
MATCH_ON = ("market_date", "vendor_id", "shopper_card")


def matches(finding, planted):
    if finding["type"] != planted["type"]:
        return False
    return all(finding.get(k) == planted["where"][k] for k in MATCH_ON if k in planted["where"])


def score(run, key):
    findings = run["findings"]
    used = set()
    rows = []
    for p in key["planted"]:
        f = next((f for f in findings if f["id"] not in used and matches(f, p)), None)
        if f:
            used.add(f["id"])
        rows.append({"id": p["id"], "type": p["type"], "caught": bool(f), "finding": f and f["id"],
                     "action_ok": bool(f) and f["action"] == p["expected_action"],
                     "expected_action": p["expected_action"], "got_action": f and f["action"]})
    false_alarms = [f for f in findings if f["id"] not in used]

    expected = {l["vendor_id"]: l for l in key["expected_oct_20_batch"]}
    got = {l["vendor_id"]: l for l in run["payout_batch"]["lines"]}
    batch_errors = []
    for vid in sorted(set(expected) | set(got)):
        e, g = expected.get(vid), got.get(vid)
        if not e or not g or (e["gross"], e["held"], e["pay_now"]) != (g["gross"], g["held"], g["pay_now"]):
            batch_errors.append({"vendor_id": vid, "expected": e and {k: e[k] for k in ("gross", "held", "pay_now")},
                                 "got": g and {k: g[k] for k in ("gross", "held", "pay_now")}})
    warnings_ok = {w["type"] for w in key["warnings"]} <= {w["type"] for w in run["warnings"]}
    return {
        "caught": sum(r["caught"] for r in rows), "planted": len(rows),
        "right_action": sum(r["action_ok"] for r in rows),
        "false_alarms": [{"id": f["id"], "type": f["type"], "title": f["title"]} for f in false_alarms],
        "batch_exact": not batch_errors, "batch_errors": batch_errors,
        "batch_awaiting_approval": run["payout_batch"]["status"] == "awaiting_approval",
        "warnings_ok": warnings_ok, "rows": rows,
    }


def main(data_dir=ROOT / "data"):
    run = agent.Agent(data_dir).run()
    key = json.loads((Path(data_dir) / "answer_key.json").read_text())
    return score(run, key)


if __name__ == "__main__":
    s = main(*sys.argv[1:2])
    for r in s["rows"]:
        mark = "CAUGHT" if r["caught"] else "MISSED"
        act = "right action" if r["action_ok"] else f"wrong action (expected {r['expected_action']}, got {r['got_action']})"
        print(f"  {r['id']}  {mark:6}  {r['type']:26} {act if r['caught'] else ''}")
    print(f"\nCaught {s['caught']}/{s['planted']} planted mistakes, right action on {s['right_action']}/{s['planted']}")
    print(f"False alarms: {len(s['false_alarms'])}" + "".join(f"\n    {f['id']} {f['title']}" for f in s["false_alarms"]))
    print(f"Tuesday's payment batch exactly right: {'yes' if s['batch_exact'] else 'NO ' + str(s['batch_errors'])}")
    print(f"Batch waits for the manager's approval: {'yes' if s['batch_awaiting_approval'] else 'NO'}")
    print(f"Grant warning raised: {'yes' if s['warnings_ok'] else 'NO'}")
