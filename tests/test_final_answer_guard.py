"""final_answer guard (successful_call_count): a model may not terminate
until at least one journaled tool call actually EXECUTED (status='done').

Regression for the B-fabrication bug (board t42/t45, 2026-09-02): policy-
denied, errored and invalid calls are journaled status='error' and must
NOT count as evidence, so a rejected fs_write cannot unlock final_answer.
"""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import os
import tempfile
import unittest

from hugpy_agent.adapter import Adapter
from hugpy_agent.config import Config
from hugpy_agent.journal import Journal
from hugpy_agent.loop import AgentLoop
from hugpy_agent.memory import Memory
from hugpy_agent.tools import build_registry

from helpers import FakeGateway, tc


class FinalAnswerGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
        with open(os.path.join(self.ws, "real.txt"), "w") as fh:
            fh.write("evidence\n")
        self.events = []

    def tearDown(self):
        self.tmp.cleanup()

    def make_loop(self, replies, policy_mode="readonly", max_steps=6):
        cfg = Config(workspace=self.ws, tools_mode="prompted",
                     max_steps=max_steps, model="fake-model",
                     policy_mode=policy_mode)
        gw = FakeGateway(replies)
        journal = Journal(os.path.join(self.ws, ".hugpy_agent", "journal.db"))
        reg = build_registry(self.ws, gw, Memory(self.ws))
        loop = AgentLoop(cfg, gateway=gw, registry=reg, journal=journal,
                         adapter=Adapter("prompted"), memory=Memory(self.ws))
        loop.on_event = lambda kind, *a: self.events.append((kind, a[0] if a else None))
        return loop, gw, journal

    def refusals(self):
        return [p for k, p in self.events
                if k == "nudge" and "final_answer refused" in str(p)]

    def test_immediate_final_answer_is_refused(self):
        loop, _, journal = self.make_loop([
            tc("final_answer", answer="made up"),
            tc("fs_read", path="real.txt"),
            tc("final_answer", answer="evidence"),
        ])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(len(self.refusals()), 1)
        self.assertEqual(journal.successful_call_count(report["run_id"]), 1)

    def test_policy_denied_write_does_not_unlock_final_answer(self):
        """readonly policy denies fs_write -> journaled status='error' ->
        final_answer still refused; a real fs_read then unlocks it."""
        loop, _, journal = self.make_loop([
            tc("fs_write", path="out.txt", content="x"),
            tc("final_answer", answer="wrote out.txt"),      # must be refused
            tc("fs_read", path="real.txt"),
            tc("final_answer", answer="evidence"),
        ])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertFalse(os.path.exists(os.path.join(self.ws, "out.txt")))
        rows = journal.tool_call_rows(report["run_id"])
        self.assertEqual([r["status"] for r in rows], ["error", "done"])
        self.assertEqual(len(self.refusals()), 1)

    def test_only_denied_calls_never_terminate(self):
        """A model that only ever emits denied writes + final_answer runs out
        of steps instead of being allowed to fabricate."""
        loop, _, journal = self.make_loop([
            tc("fs_write", path="a.txt", content="x"),
            tc("final_answer", answer="done"),
            tc("fs_write", path="b.txt", content="x"),
            tc("final_answer", answer="done"),
        ] * 3, max_steps=6)
        report = loop.run("t")
        self.assertNotEqual(report["outcome"], "done")
        self.assertEqual(journal.successful_call_count(report["run_id"]), 0)
        self.assertGreaterEqual(len(self.refusals()), 1)

    def test_errored_read_does_not_count(self):
        loop, _, journal = self.make_loop([
            tc("fs_read", path="missing.txt"),          # tool error
            tc("final_answer", answer="it is missing"),  # refused
            tc("fs_read", path="real.txt"),
            tc("final_answer", answer="evidence"),
        ])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(len(self.refusals()), 1)
        self.assertEqual(journal.successful_call_count(report["run_id"]), 1)


if __name__ == "__main__":
    unittest.main()
