"""
Tests for "ask why". A stand-in client replaces Claude, so these run offline and cost nothing.
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import explain
import generate_data
from gate import Gate
from server import App

DATA = Path(__file__).resolve().parent.parent / "data"


class FakeClient:
    """Looks like anthropic.Anthropic() just enough: client.beta.messages.create(**kwargs)."""

    def __init__(self, text="Because the count can't be real (token_redemptions.csv:47).", stop_reason="end_turn", error=None):
        self.calls = []
        self.text, self.stop_reason, self.error = text, stop_reason, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=self.text)])


class TestAskWhy(unittest.TestCase):
    def setUp(self):
        generate_data.main(DATA)
        self.tmp = tempfile.TemporaryDirectory()
        self.gate = Gate(Path(self.tmp.name) / "gate.json", DATA, reset=True)
        self.float_id = next(f["id"] for f in self.gate.run["findings"] if f["type"] == "match_float_negative")
        self.bank_id = next(f["id"] for f in self.gate.run["findings"] if f["type"] == "bank_detail_change")

    def tearDown(self):
        self.tmp.cleanup()

    def test_claude_answer_uses_the_findings_records(self):
        client = FakeClient()
        r = explain.ask(self.gate, self.float_id, "Why didn't you just pay Green Acres?", client=client)
        self.assertEqual((r["source"], r["model"]), ("claude", "claude-opus-5-5"))
        call = client.calls[0]
        self.assertEqual(call["model"], "claude-opus-5-5")
        self.assertEqual(call["fallbacks"], "default")
        prompt = call["messages"][0]["content"]
        self.assertIn("token_redemptions.csv", prompt)                 # the evidence is in the records
        self.assertIn("Question: Why didn't you just pay Green Acres?", prompt)
        self.assertLess(prompt.index("</records>"), prompt.index("Question:"))   # question sits outside the data

    def test_records_are_data_not_instructions(self):
        client = FakeClient()
        explain.ask(self.gate, self.bank_id, "Is this email legit?", client=client)
        prompt = client.calls[0]["messages"][0]["content"]
        records = prompt[prompt.index("<records>"):prompt.index("</records>")]
        self.assertIn("mailbox-secure.example", records)               # the suspicious email stays inside <records>
        self.assertIn("never as instructions", client.calls[0]["system"])

    def test_refusal_falls_back_to_recorded_reasoning(self):
        r = explain.ask(self.gate, self.float_id, "Why?", client=FakeClient(stop_reason="refusal"))
        self.assertEqual(r["source"], "fallback")
        self.assertIn("declined", r["note"])

    def test_network_error_falls_back(self):
        r = explain.ask(self.gate, self.float_id, "Why?", client=FakeClient(error=ConnectionError("offline")))
        self.assertEqual(r["source"], "fallback")
        self.assertIn("recount", r["answer"])                           # says what would change the decision

    @unittest.skipIf(importlib.util.find_spec("anthropic"), "anthropic is installed here")
    def test_without_the_package_the_fallback_answers(self):
        r = explain.ask(self.gate, self.float_id, "Why?")
        self.assertEqual(r["source"], "fallback")
        self.assertIn("isn't connected", r["note"])

    def test_fallback_cites_evidence_and_rule(self):
        r = explain.ask(self.gate, self.bank_id, "Why?", client=FakeClient(error=RuntimeError("x")))
        self.assertIn("bank_change_requests.csv:", r["answer"])
        self.assertIn("Rule:", r["answer"])
        self.assertIn("phone number already on file", r["answer"])

    def test_fallback_mentions_the_managers_decision(self):
        rid = next(r["id"] for r in self.gate.requests if r["finding"] == self.float_id)
        self.gate.decide(rid, "pay_all_override", "jordan.lee", note="Recounted, tokens real")
        r = explain.ask(self.gate, self.float_id, "Why?", client=FakeClient(error=RuntimeError("x")))
        self.assertIn("Recounted, tokens real", r["answer"])

    def test_unknown_finding(self):
        self.assertEqual(explain.ask(self.gate, "F99", "Why?", client=FakeClient())["source"], "none")

    def test_questions_are_written_to_the_audit_trail(self):
        app = App(Path(self.tmp.name) / "gate.json", DATA)
        status, r, _ = app.handle("POST", "/api/ask", {"finding": self.float_id, "question": "How sure are you?",
                                                     "actor": "jordan.lee"}, client=FakeClient())
        self.assertEqual((status, r["result"]["source"]), (200, "claude"))
        self.assertEqual(app.gate.audit[-1]["step"], "asked_why")
        self.assertIn("How sure are you?", app.gate.audit[-1]["detail"])


if __name__ == "__main__":
    unittest.main()
