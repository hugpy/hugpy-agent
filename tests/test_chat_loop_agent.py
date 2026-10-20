"""The agent loop inherits the harness chat-mode fixes after the swap onto the
hugpy_harness core (conversation=True):

  1. an empty or failed reply ABORTS with a clear error — never the old
     "You must respond with a tool call" re-injection — and replay drops a
     legacy injection already on the record (_chat_wire);
  2. <tool_response>/markup is gated out and stripped from the final answer;
  3. the step budget resets per user message (_budget_steps);
  4. an answer-less final_answer is repaired, not dumped as JSON args.

Offline: model behaviour is scripted through FakeGateway.
"""
import _bootstrap  # noqa: F401  (src-path shim)
import json
import os
import tempfile
import unittest

from hugpy_agent.config import Config
from hugpy_agent.gateway import ChatResult
from hugpy_agent.loop import AgentLoop
from hugpy_agent.memory import Memory

from helpers import FakeGateway, tc

LEGACY = ("You must respond with a tool call. Investigate with a real tool "
          "first; use `final_answer` only once you have.")


def _cfg(ws):
    return Config(workspace=ws, model="fake-model", policy_mode="auto",
                  tools_mode="prompted", rag_enabled=False, toolserver=False,
                  max_steps=5)


def _chat_loop(ws, replies):
    gw = FakeGateway(replies)
    return AgentLoop(_cfg(ws), gateway=gw, memory=Memory(ws), conversation=True)


def _texts(loop, run_id):
    return [r.get("content") if isinstance(r.get("content"), str) else ""
            for r in loop.journal.raw_messages(run_id)]


class ChatFixesInheritedTests(unittest.TestCase):
    def test_empty_reply_aborts_no_reinjection(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = os.path.realpath(tmp)
            loop = _chat_loop(ws, [ChatResult(ok=True, text="")])
            try:
                rid = loop.start_chat()
                loop.send_user(rid, "hello")
                report = loop._drive(rid)
                self.assertEqual(report["outcome"], "aborted", report)
                self.assertIn("empty reply", report["error"])
                self.assertTrue(all(LEGACY not in t for t in _texts(loop, rid)))
                self.assertEqual(len(loop.gateway.calls), 1)
            finally:
                loop.journal.close()

    def test_failed_reply_aborts_with_engine_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = os.path.realpath(tmp)
            loop = _chat_loop(ws, [ChatResult(ok=False, error="engine exploded: 500")])
            try:
                rid = loop.start_chat()
                loop.send_user(rid, "hello")
                report = loop._drive(rid)
                self.assertEqual(report["outcome"], "aborted", report)
                self.assertIn("engine exploded: 500", report["error"])
            finally:
                loop.journal.close()

    def test_chat_wire_drops_legacy_injection_on_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = os.path.realpath(tmp)
            loop = _chat_loop(ws, [])
            try:
                rid = loop.start_chat()
                loop.journal.append_message(rid, "user", "do a thing")
                loop.journal.append_message(rid, "assistant", "")
                loop.journal.append_message(rid, "user", LEGACY)
                wire = loop._chat_wire(rid)
                texts = [m.get("content") for m in wire]
                self.assertNotIn(LEGACY, texts)
                self.assertTrue(all(not (m.get("role") == "assistant"
                                         and not m.get("content")) for m in wire))
            finally:
                loop.journal.close()

    def test_tool_response_markup_stripped(self):
        reply = ("Result ready.\n<tool_response>{\"name\": \"fs_read\", "
                 "\"result\": \"secret\"}</tool_response>\nAll done.")
        with tempfile.TemporaryDirectory() as tmp:
            ws = os.path.realpath(tmp)
            loop = _chat_loop(ws, [reply])
            try:
                rid = loop.start_chat()
                loop.send_user(rid, "do it")
                report = loop._drive(rid)
                self.assertEqual(report["outcome"], "done", report)
                self.assertNotIn("<tool_response>", report["answer"])
                self.assertNotIn("secret", report["answer"])
                self.assertIn("Result ready.", report["answer"])
                self.assertIn("All done.", report["answer"])
            finally:
                loop.journal.close()

    def test_step_budget_resets_per_user_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = os.path.realpath(tmp)
            loop = _chat_loop(ws, [])
            try:
                rid = loop.start_chat()
                for _ in range(3):
                    loop.journal.append_message(rid, "assistant", "step")
                self.assertEqual(loop._budget_steps(rid), 3)
                loop.send_user(rid, "a fresh user turn")
                self.assertEqual(loop._budget_steps(rid), 0)
                self.assertEqual(loop.journal.assistant_step_count(rid), 3)
            finally:
                loop.journal.close()

    def test_answerless_final_answer_repaired_then_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = os.path.realpath(tmp)
            loop = _chat_loop(ws, [tc("final_answer"),
                                   tc("final_answer", answer="the real answer")])
            try:
                rid = loop.start_chat()
                loop.send_user(rid, "answer me")
                report = loop._drive(rid)
                self.assertEqual(report["outcome"], "done", report)
                self.assertEqual(report["answer"], "the real answer")
                self.assertEqual(len(loop.gateway.calls), 2)
                self.assertTrue(all(t != "{}" for t in _texts(loop, rid)))
            finally:
                loop.journal.close()


if __name__ == "__main__":
    unittest.main()
