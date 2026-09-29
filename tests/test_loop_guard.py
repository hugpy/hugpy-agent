"""Agent-level loop-guard (P2.4): a model repeating the SAME tool call —
identical (tool_name, args_sha256) with nothing distinct in between — gets
one strong nudge at N repeats and a fail-fast abort (outcome "looping") at
2N. Varied or genuinely progressing sequences must never trip it."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import json
import os
import tempfile
import unittest

from hugpy_agent.adapter import Adapter
from hugpy_agent.config import Config, load_config
from hugpy_agent.journal import Journal
from hugpy_agent.loop import AgentLoop
from hugpy_agent.memory import Memory
from hugpy_agent.tools import build_registry

from helpers import FakeGateway, tc

# Stable core of LOOP_GUARD_NUDGE, used to find the injected nudge without
# coupling the tests to its exact wording.
NUDGE_MARK = "with the same arguments"

# The two signatures the tests loop on: same tool, different args — so a
# switch between them is "genuine progress" for the guard.
CALL_A = tc("fs_glob", pattern="*")
CALL_B = tc("fs_glob", pattern="*.txt")


class GuardHarness(unittest.TestCase):
    """Temp workspace + scripted model, mirroring tests/test_loop.py."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
        # policy_mode=auto: the guard is loop mechanics, not the P2.1 gate;
        # max_steps is kept well ABOVE 2N so a "looping" outcome can only
        # come from the guard, never from the step cap.
        self.cfg = Config(workspace=self.ws, tools_mode="prompted",
                          max_steps=20, model="fake-model",
                          policy_mode="auto")

    def tearDown(self):
        self.tmp.cleanup()

    def make_loop(self, replies):
        # Big fake ctx: since estimate_tokens counts //3 (the 2026-08-06
        # compaction fix), the default 8192 budget trips mid-test compaction —
        # extra model calls that are noise to the GUARD behavior under test.
        gw = FakeGateway(replies, ctx=256_000)
        journal = Journal(os.path.join(self.ws, ".hugpy_agent", "journal.db"))
        reg = build_registry(self.ws, gw, Memory(self.ws))
        loop = AgentLoop(self.cfg, gateway=gw, registry=reg, journal=journal,
                         adapter=Adapter("prompted"), memory=Memory(self.ws))
        return loop, gw, journal

    def nudges(self, journal, run_id):
        return [m for m in journal.raw_messages(run_id)
                if m["role"] == "user" and NUDGE_MARK in str(m["content"])]


class LoopGuardTests(GuardHarness):
    def test_identical_calls_nudge_at_n_abort_at_2n(self):
        """The contract: nudge after N=3 identical calls, abort at 2N=6 —
        the 6th copy is never executed (fail fast)."""
        loop, gw, journal = self.make_loop([CALL_A] * 12)
        report = loop.run("t")
        self.assertEqual(report["outcome"], "looping")
        self.assertIn("loop-guard", report["error"])
        self.assertIn("fs_glob", report["error"])
        self.assertEqual(report["steps"], 6)          # 2N, well under max_steps
        self.assertEqual(report["tool_calls"], 5)     # the 2N-th never ran
        self.assertEqual(len(gw.calls), 6)
        # exactly ONE nudge, and it actually reached the model (4th call):
        self.assertEqual(len(self.nudges(journal, report["run_id"])), 1)
        self.assertIn("same arguments 3 times", json.dumps(gw.calls[3][0]))

    def test_varied_sequence_never_trips(self):
        """Repeated-but-progressing work (streaks shorter than N) must run
        to completion with no nudge — the guard must never misfire."""
        loop, _, journal = self.make_loop(
            [CALL_A, CALL_A, CALL_B, CALL_B, CALL_A, CALL_A,
             tc("final_answer", answer="done")])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.nudges(journal, report["run_id"]), [])

    def test_progress_resets_counter_and_nudge(self):
        """A distinct call after a nudge starts a FRESH streak: the next
        identical run of N earns a second nudge, not an abort (without the
        reset, 3+3 identical calls would hit the 2N=6 abort)."""
        loop, _, journal = self.make_loop(
            [CALL_A, CALL_A, CALL_A,          # streak 1 -> nudge
             CALL_B,                          # genuine progress: reset
             CALL_A, CALL_A, CALL_A,          # streak 2 -> nudge again
             tc("final_answer", answer="done")])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(len(self.nudges(journal, report["run_id"])), 2)

    def test_custom_n_moves_both_thresholds(self):
        """loop_guard_n drives both rungs of the ladder: N=2 -> nudge at 2,
        abort at 4."""
        self.cfg.loop_guard_n = 2
        loop, gw, journal = self.make_loop([CALL_A] * 8)
        report = loop.run("t")
        self.assertEqual(report["outcome"], "looping")
        self.assertEqual(report["steps"], 4)
        self.assertEqual(len(self.nudges(journal, report["run_id"])), 1)
        self.assertIn("same arguments 2 times", json.dumps(gw.calls[2][0]))

    def test_zero_disables_guard(self):
        """HUGPY_LOOP_GUARD_N=0: no nudge, no looping abort — the run hits
        the ordinary step cap instead."""
        self.cfg.loop_guard_n = 0
        self.cfg.max_steps = 5
        loop, _, journal = self.make_loop([CALL_A] * 10)
        report = loop.run("t")
        self.assertEqual(report["outcome"], "max_steps")
        self.assertEqual(self.nudges(journal, report["run_id"]), [])


class LoopGuardConfigTests(unittest.TestCase):
    def test_env_knob_and_default(self):
        with tempfile.TemporaryDirectory() as ws:
            self.assertEqual(load_config(ws, environ={}).loop_guard_n, 3)
            self.assertEqual(
                load_config(ws, environ={"HUGPY_LOOP_GUARD_N": "5"}).loop_guard_n, 5)
            self.assertEqual(
                load_config(ws, environ={"HUGPY_LOOP_GUARD_N": "0"}).loop_guard_n, 0)
            # garbage keeps the prior value (a bad knob must not crash startup)
            self.assertEqual(
                load_config(ws, environ={"HUGPY_LOOP_GUARD_N": "many"}).loop_guard_n, 3)


if __name__ == "__main__":
    unittest.main()
