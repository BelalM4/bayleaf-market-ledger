"""
The web app. Nothing to install.

    python3 server.py            # then open http://localhost:8000
    python3 server.py 8080       # different port

The page is static/index.html. It talks to this server through a small JSON API:
    GET  /api/state       everything the page shows
    POST /api/login       {user, pin} -> sets an HttpOnly session cookie
    POST /api/logout
    POST /api/decide      {request, option, note, run_id} as the signed-in user -> 401/403 with the reason if not allowed
    POST /api/agent_attempt {request}  the agent tries to approve its own request (always refused)
    POST /api/reset       archive everything and start a fresh run (signed-in manager only)
    POST /api/scenario    {name, params}  "what if...?" on a scratch copy of the data
    POST /api/ask         {finding, question, actor}  "ask why" (Claude if connected, else the recorded reasoning)
"""
import importlib.util
import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import explain
import scenarios
import score
from gate import Gate, IntegrityError, NotAllowed

ROOT = Path(__file__).parent
INDEX = ROOT / "static" / "index.html"


class App:
    def __init__(self, state_path=ROOT / "state" / "gate.json", data_dir=ROOT / "data"):
        self.state_path, self.data_dir = Path(state_path), Path(data_dir)
        self.lock = threading.Lock()
        self.gate = Gate(self.state_path, self.data_dir)
        self.sessions = {}          # random token -> user id; lives only in this server's memory

    def state(self, me=None):
        g = self.gate
        s = g.status()
        findings = {f["id"]: f for f in g.run["findings"]}
        key = json.loads((self.data_dir / "answer_key.json").read_text())   # the server grades; the agent never sees it
        users = {uid: {"name": u["name"], "role": u["role"], "can_sign_in": bool(u.get("pin_hash"))}
                 for uid, u in g.config["users"].items()}
        return {
            "as_of": g.config["today"], "market": g.config["market"], "payout_date": g.config["next_payout_date"],
            "me": me and {"id": me, **users[me]}, "run_id": g.run_id,
            "users": users, "permissions": g.config["permissions"],
            "summary": g.run["summary"], "findings": g.run["findings"], "warnings": g.run["warnings"],
            "cleared": g.run["cleared"], "sources_read": g.run["sources_read"], "token_float": g.run["token_float"],
            "requests": [{**r, "finding_detail": findings.get(r["finding"])} for r in g.requests],
            "batch": g.batch, "checks": s["checks"], "all_tie_outs_ok": s["all_tie_outs_ok"],
            "trial_balance": g.ledger.trial_balance(), "entries": g.ledger.entries,
            "locked_through": g.ledger.locked_through, "audit": g.audit, "audit_head": g.log.head,
            "score": score.score(g.run, key),
            "scenarios": [{"name": n, "title": t, "description": d} for n, (t, d, _) in scenarios.SCENARIOS.items()],
            "vendors": {vid: v["name"] for vid, v in g.a.vendors.items()},
            "market_days": g.config["market_days"],
            "claude_connected": claude_connected(),
        }

    def handle(self, method, path, body, client=None, token=None):
        """Returns (status, payload, headers). Kept apart from HTTP so tests can call it directly.

        Who you are comes only from the session `token` (an HttpOnly cookie in the browser),
        never from the request body.
        """
        me = self.sessions.get(token)
        if method == "POST" and path == "/api/ask":
            # outside the lock: a Claude answer can take a few seconds and shouldn't freeze the page
            result = explain.ask(self.gate, body.get("finding", ""), body.get("question", ""), client=client)
            with self.lock:
                self.gate.log.stage(me or "viewer", "asked_why", f"{result['finding_id']}: \"{result.get('question', '')}\" "
                                                                 f"(answered by {result['source']})")
                self.gate.save()
            return 200, {"ok": True, "result": result}, {}
        with self.lock:
            if method == "GET" and path == "/api/state":
                return 200, self.state(me), {}
            if method == "POST" and path == "/api/login":
                user = body.get("user", "")
                if not self.gate.authenticate(user, str(body.get("pin", ""))):
                    self.gate.log.stage(user or "unknown", "sign_in_failed", "Wrong PIN, or a user that can't sign in")
                    self.gate.save()
                    return 401, {"ok": False, "error": "Wrong PIN, or that user can't sign in."}, {}
                new = secrets.token_urlsafe(24)
                self.sessions[new] = user
                self.gate.log.stage(user, "signed_in", f"{self.gate.config['users'][user]['name']} signed in")
                self.gate.save()
                return 200, {"ok": True, "state": self.state(user)}, \
                    {"Set-Cookie": f"session={new}; HttpOnly; SameSite=Strict; Path=/"}
            if method == "POST" and path == "/api/logout":
                self.sessions.pop(token, None)
                return 200, {"ok": True, "state": self.state()}, {"Set-Cookie": "session=; Max-Age=0; Path=/"}
            if method == "POST" and path == "/api/decide":
                if not me:
                    return 401, {"ok": False, "error": "Sign in first. Approvals need a signed-in market manager."}, {}
                if body.get("run_id") != self.gate.run_id:
                    return 409, {"ok": False, "error": "This screen is out of date (the records changed since it loaded). "
                                                     "It has been refreshed; check the request and decide again.",
                                 "state": self.state(me)}, {}
                try:
                    r = self.gate.decide(body.get("request", ""), body.get("option", ""), me, body.get("note", ""),
                                         run_id=body["run_id"])
                    return 200, {"ok": True, "request": r, "state": self.state(me)}, {}
                except NotAllowed as e:
                    return 403, {"ok": False, "error": str(e), "state": self.state(me)}, {}
            if method == "POST" and path == "/api/agent_attempt":
                # The agent is this server's own identity. Let it try to approve its own request, to show the refusal.
                r = self.gate.request(body.get("request", "")) if body.get("request") else None
                try:
                    self.gate.decide(r["id"], r["options"][0]["key"], "agent", "")
                    return 500, {"ok": False, "error": "The agent was allowed to approve. This should never happen."}, {}
                except (NotAllowed, TypeError) as e:
                    return 403, {"ok": False, "error": str(e), "state": self.state(me)}, {}
            if method == "POST" and path == "/api/reset":
                if not me or self.gate.config["users"][me]["role"] != "market_manager":
                    return 401, {"ok": False, "error": "Only a signed-in market manager can reset the demo."}, {}
                self.gate = Gate(self.state_path, self.data_dir, reset=True)
                self.gate.log.stage(me, "reset_by", f"Demo reset by {self.gate.config['users'][me]['name']}")
                self.gate.save()
                return 200, {"ok": True, "state": self.state(me)}, {}
            if method == "POST" and path == "/api/scenario":
                if body.get("name") not in scenarios.SCENARIOS:
                    return 400, {"ok": False, "error": "Unknown scenario"}, {}
                return 200, {"ok": True, "result": scenarios.run(body["name"], body.get("params"), self.data_dir)}, {}
        return 404, {"ok": False, "error": "Not found"}, {}


def claude_connected():
    has_sdk = importlib.util.find_spec("anthropic") is not None
    return has_sdk and bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, payload, headers=None, content_type="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(200, INDEX.read_bytes(), content_type="text/html; charset=utf-8")
            self._send(*app.handle("GET", self.path, {}, token=self._token()))

        def _token(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "session":
                    return v
            return None

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"ok": False, "error": "Body must be JSON"})
            if self.path != "/api/ask" and self.headers.get("Origin") not in (None, f"http://{self.headers.get('Host')}"):
                return self._send(403, {"ok": False, "error": "Cross-site request refused"})
            self._send(*app.handle("POST", self.path, body, token=self._token()))

        def log_message(self, fmt, *args):
            sys.stderr.write(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}\n")

    return Handler


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    try:
        app = App()
    except IntegrityError as e:
        sys.exit(f"STOPPED: {e}")
    print(f"Bayleaf Market Ledger running at http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), make_handler(app)).serve_forever()
