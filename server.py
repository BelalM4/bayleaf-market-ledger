"""
The web app. Nothing to install.

    python3 server.py            # then open http://localhost:8000
    python3 server.py 8080       # different port

The page is static/index.html. It talks to this server through a small JSON API:
    GET  /api/state       everything the page shows
    POST /api/decide      {request, option, actor, note}   -> 403 with the reason if not allowed
    POST /api/reset       start over from the agent's fresh run
    POST /api/scenario    {name, params}  "what if...?" on a scratch copy of the data
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import scenarios
import score
from gate import Gate, NotAllowed

ROOT = Path(__file__).parent
INDEX = ROOT / "static" / "index.html"


class App:
    def __init__(self, state_path=ROOT / "state" / "gate.json", data_dir=ROOT / "data"):
        self.state_path, self.data_dir = Path(state_path), Path(data_dir)
        self.lock = threading.Lock()
        self.gate = Gate(self.state_path, self.data_dir)

    def state(self):
        g = self.gate
        s = g.status()
        findings = {f["id"]: f for f in g.run["findings"]}
        key = json.loads((self.data_dir / "answer_key.json").read_text())   # the server grades; the agent never sees it
        return {
            "as_of": g.config["today"], "market": g.config["market"], "payout_date": g.config["next_payout_date"],
            "users": g.config["users"], "permissions": g.config["permissions"],
            "summary": g.run["summary"], "findings": g.run["findings"], "warnings": g.run["warnings"],
            "cleared": g.run["cleared"], "sources_read": g.run["sources_read"], "token_float": g.run["token_float"],
            "requests": [{**r, "finding_detail": findings.get(r["finding"])} for r in g.requests],
            "batch": g.batch, "checks": s["checks"], "all_tie_outs_ok": s["all_tie_outs_ok"],
            "trial_balance": g.ledger.trial_balance(), "entries": g.ledger.entries,
            "locked_through": g.ledger.locked_through, "audit": g.audit,
            "score": score.score(g.run, key),
            "scenarios": [{"name": n, "title": t, "description": d} for n, (t, d, _) in scenarios.SCENARIOS.items()],
            "vendors": {vid: v["name"] for vid, v in g.a.vendors.items()},
            "market_days": g.config["market_days"],
        }

    def handle(self, method, path, body):
        """Returns (status, payload). Kept apart from HTTP so tests can call it directly."""
        with self.lock:
            if method == "GET" and path == "/api/state":
                return 200, self.state()
            if method == "POST" and path == "/api/decide":
                try:
                    r = self.gate.decide(body.get("request", ""), body.get("option", ""), body.get("actor", ""),
                                         body.get("note", ""))
                    return 200, {"ok": True, "request": r, "state": self.state()}
                except NotAllowed as e:
                    return 403, {"ok": False, "error": str(e), "state": self.state()}
            if method == "POST" and path == "/api/reset":
                self.gate = Gate(self.state_path, self.data_dir, reset=True)
                return 200, {"ok": True, "state": self.state()}
            if method == "POST" and path == "/api/scenario":
                if body.get("name") not in scenarios.SCENARIOS:
                    return 400, {"ok": False, "error": "Unknown scenario"}
                return 200, {"ok": True, "result": scenarios.run(body["name"], body.get("params"), self.data_dir)}
        return 404, {"ok": False, "error": "Not found"}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, payload, content_type="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            self._send(*app.handle("GET", self.path, {}))

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"ok": False, "error": "Body must be JSON"})
            self._send(*app.handle("POST", self.path, body))

        def log_message(self, fmt, *args):
            sys.stderr.write(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}\n")

    return Handler


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    app = App()
    print(f"Bayleaf Market Ledger running at http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), make_handler(app)).serve_forever()
