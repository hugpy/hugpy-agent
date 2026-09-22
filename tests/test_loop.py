"""Agent-loop behavior against a scripted model: multi-step task, repair
round-trip, structured abort, and the write->kill->resume protocol."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import json
import os
import tempfile
import unittest

from hugpy_agent.adapter import Adapter
from hugpy_agent.config import Config
from hugpy_agent.gateway import CONTINUATION_LEAK
from hugpy_agent.journal import Journal, idem_key
from hugpy_agent.loop import AgentLoop
from hugpy_agent.memory import Memory
from hugpy_agent.tools import (RISK_READONLY, RISK_REMOTE_COMPUTE, RISK_WRITE,
                               ToolInterrupted, ToolSpec, build_registry)

from helpers import FakeGateway, tc


class LoopHarness(unittest.TestCase):
    """Shared scaffolding: temp workspace, fake gateway, counting tools."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
        # policy_mode=auto: these tests target loop mechanics, not the P2.1
        # gate (the default `ask` fails closed and would deny every write
        # tool). The gate itself is covered in tests/test_policy.py.
        self.cfg = Config(workspace=self.ws, tools_mode="prompted",
                          max_steps=10, model="fake-model",
                          policy_mode="auto")
        self.db = os.path.join(self.ws, ".hugpy_agent", "journal.db")
        self.effect_runs = []

    def tearDown(self):
        self.tmp.cleanup()

    def make_loop(self, replies, journal=None):
        gw = FakeGateway(replies)
        journal = journal or Journal(self.db)
        reg = build_registry(self.ws, gw, Memory(self.ws))
        reg.register(ToolSpec(
            name="effect", description="side-effecting test tool",
            parameters={"type": "object",
                        "properties": {"tag": {"type": "string"}},
                        "required": ["tag"]},
            handler=lambda tag: self.effect_runs.append(tag) or "effect-ran:%s" % tag,
            risk_class=RISK_WRITE))
        loop = AgentLoop(self.cfg, gateway=gw, registry=reg, journal=journal,
                         adapter=Adapter("prompted"), memory=Memory(self.ws))
        return loop, gw, journal


class MultiStepTests(LoopHarness):
    def test_read_reason_write_flow(self):
        """The acceptance-shaped flow: glob -> read -> write report -> finish."""
        with open(os.path.join(self.ws, "notes.txt"), "w") as fh:
            fh.write("this project boils oceans")
        loop, gw, _ = self.make_loop([
            tc("fs_glob", pattern="*.txt"),
            tc("fs_read", path="notes.txt"),
            tc("fs_write", path="report.md", content="# Report\nboils oceans"),
            tc("final_answer", answer="Wrote report.md: the project boils oceans."),
        ])
        report = loop.run("describe the project and write report.md")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(report["steps"], 4)
        self.assertEqual(report["tool_calls"], 3)
        self.assertIn("report.md", report["answer"])
        with open(os.path.join(self.ws, "report.md")) as fh:
            self.assertIn("boils oceans", fh.read())
        # tool results actually reached the model on the next call
        sent = json.dumps(gw.calls[1][0])
        self.assertIn("notes.txt", sent)

    def test_leaked_continuation_string_survives(self):
        """The known platform leak in a reply must not derail parsing."""
        loop, _, _ = self.make_loop([
            CONTINUATION_LEAK + tc("fs_write", path="x.txt", content="ok"),
            tc("final_answer", answer="done"),
        ])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertTrue(os.path.exists(os.path.join(self.ws, "x.txt")))

    def test_repair_round_trip_recovers(self):
        loop, gw, journal = self.make_loop([
            '<tool_call>{"name": "fs_write", "arguments": {broken</tool_call>',
            tc("fs_write", path="ok.txt", content="fixed"),
            tc("final_answer", answer="done"),
        ])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        # The repair prompt was actually sent back to the model.
        repair_sent = json.dumps(gw.calls[1][0])
        self.assertIn("invalid tool call", repair_sent)
        self.assertTrue(os.path.exists(os.path.join(self.ws, "ok.txt")))

    def test_repeated_garbage_aborts_structured(self):
        """A sloppy model that never recovers gets a clear abort, not a hang."""
        garbage = '<tool_call>{nope}</tool_call>'
        loop, gw, _ = self.make_loop([garbage] * 10)
        report = loop.run("t")
        self.assertEqual(report["outcome"], "aborted")
        self.assertIn("error", report)
        self.assertEqual(len(gw.calls), 3)   # initial + repair + last chance

    def test_invalid_args_repaired_then_ok(self):
        loop, _, _ = self.make_loop([
            tc("fs_read"),                          # missing required 'path'
            tc("fs_read", path="none.txt"),         # valid args, file missing
            tc("final_answer", answer="file absent"),
        ])
        with open(os.path.join(self.ws, "none.txt"), "w") as fh:
            fh.write("x")
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")

    def test_tool_error_is_data_not_crash(self):
        loop, gw, _ = self.make_loop([
            tc("fs_read", path="does-not-exist.txt"),
            tc("fs_glob", pattern="*"),   # one SUCCESSFUL call: the final_answer guard needs it
            tc("final_answer", answer="the file is missing"),
        ])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        sent = json.dumps(gw.calls[1][0])
        self.assertIn("failed", sent)   # structured error reached the model

    def test_unknown_tool_reported_to_model(self):
        loop, gw, _ = self.make_loop([
            tc("teleport", destination="mars"),
            tc("fs_glob", pattern="*"),   # one SUCCESSFUL call: the final_answer guard needs it
            tc("final_answer", answer="no teleporter available"),
        ])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertIn("unknown tool", json.dumps(gw.calls[1][0]))

    def test_max_steps_cap(self):
        self.cfg.max_steps = 3
        loop, _, _ = self.make_loop([tc("fs_glob", pattern="*")] * 10)
        report = loop.run("t")
        self.assertEqual(report["outcome"], "max_steps")
        self.assertEqual(report["steps"], 3)

    def test_only_first_of_multiple_calls_executed(self):
        loop, gw, _ = self.make_loop([
            tc("effect", tag="one") + tc("effect", tag="two"),
            tc("final_answer", answer="done"),
        ])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.effect_runs, ["one"])
        self.assertIn("only the first", json.dumps(gw.calls[1][0]))


class ResumeTests(LoopHarness):
    """write -> kill -> resume. The 'kill' is simulated by building the exact
    journal state a dead process leaves behind, then resuming with a FRESH
    loop/journal (new process). README documents the manual procedure."""

    def _seed_run(self, journal):
        rid = journal.create_run("do the effect then finish", "fake-model")
        journal.append_message(rid, "system", "sys-prompt")
        journal.append_message(rid, "user", "TASK:\ndo the effect then finish")
        a_seq = journal.append_message(rid, "assistant", tc("effect", tag="boom"))
        return rid, a_seq

    def test_completed_side_effect_not_reexecuted(self):
        """Killed after the tool ran and its result was journaled, but before
        the tool_response message was appended — the classic crash window."""
        j1 = Journal(self.db)
        rid, a_seq = self._seed_run(j1)
        key = idem_key(rid, a_seq, "effect", {"tag": "boom"})
        j1.record_call_start(key, rid, a_seq, "effect", {"tag": "boom"})
        j1.record_call_result(key, "done", "effect-ran:boom")
        j1.close()   # <- process dies here

        loop, gw, _ = self.make_loop([tc("final_answer", answer="all done")])
        report = loop.resume(rid)
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.effect_runs, [])          # NOT re-executed
        # ...but its journaled result still reached the model:
        self.assertIn("effect-ran:boom", json.dumps(gw.calls[0][0]))

    def test_pending_side_effect_reported_unknown(self):
        """Killed DURING execution: outcome unknowable -> fail closed, tell
        the model to verify state instead of blindly re-running."""
        j1 = Journal(self.db)
        rid, a_seq = self._seed_run(j1)
        key = idem_key(rid, a_seq, "effect", {"tag": "boom"})
        j1.record_call_start(key, rid, a_seq, "effect", {"tag": "boom"})
        j1.close()   # <- process dies mid-handler

        loop, gw, _ = self.make_loop([tc("fs_glob", pattern="*"),   # guard needs 1 success
                                      tc("final_answer", answer="verified")])
        report = loop.resume(rid)
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.effect_runs, [])
        self.assertIn("outcome is unknown", json.dumps(gw.calls[0][0]))

    def test_pending_readonly_reexecuted(self):
        """Read-only tools are safe to re-run; resume should just do it."""
        j1 = Journal(self.db)
        rid = j1.create_run("t", "fake-model")
        j1.append_message(rid, "system", "sys")
        j1.append_message(rid, "user", "TASK:\nt")
        with open(os.path.join(self.ws, "f.txt"), "w") as fh:
            fh.write("payload")
        a_seq = j1.append_message(rid, "assistant", tc("fs_read", path="f.txt"))
        key = idem_key(rid, a_seq, "fs_read", {"path": "f.txt"})
        j1.record_call_start(key, rid, a_seq, "fs_read", {"path": "f.txt"})
        j1.close()

        loop, gw, _ = self.make_loop([tc("final_answer", answer="ok")])
        report = loop.resume(rid)
        self.assertEqual(report["outcome"], "done")
        self.assertIn("payload", json.dumps(gw.calls[0][0]))

    def test_interrupted_run_is_resumable(self):
        """stop_requested (SIGINT path) marks the run interrupted; a fresh
        loop resumes it to completion."""
        loop, _, journal = self.make_loop([tc("fs_glob", pattern="*")] * 3)
        loop.stop_requested = True
        report = loop.run("t")
        self.assertEqual(report["outcome"], "interrupted")
        rid = report["run_id"]
        journal.close()

        loop2, _, _ = self.make_loop([tc("fs_glob", pattern="*"),   # guard needs 1 success
                                      tc("final_answer", answer="finished")])
        report2 = loop2.resume(rid)
        self.assertEqual(report2["outcome"], "done")
        self.assertEqual(report2["answer"], "finished")

    def test_resume_unknown_run(self):
        loop, _, _ = self.make_loop([])
        report = loop.resume("nope")
        self.assertEqual(report["outcome"], "aborted")
        self.assertIn("unknown run_id", report["error"])


class RemoteComputeResumeTests(LoopHarness):
    """Loop-level semantics for tools that journal durable state (async
    generation): pending + state -> handler re-executed (re-attaches);
    pending + no state -> outcome unknown (existing rule); ToolInterrupted
    leaves the call pending and the run resumable."""

    def _register_genlike(self, loop):
        """A generation-shaped tool: journals a job_id via context, then
        'polls'. self.gen_behavior controls each execution."""
        def handler(tag, _context=None):
            self.gen_log.append(tag)
            state = _context.get_state()
            if state and state.get("job_id"):
                return json.dumps({"artifact": "artifacts/%s.png" % state["job_id"],
                                   "job_id": state["job_id"], "reattached": True})
            _context.set_state({"kind": "generation", "job_id": "J-" + tag})
            if self.gen_behavior == "interrupt":
                loop.stop_requested = True
                raise ToolInterrupted("stop during poll")
            return json.dumps({"artifact": "artifacts/J-%s.png" % tag,
                               "job_id": "J-" + tag})
        loop.registry.register(ToolSpec(
            name="genlike", description="test generation tool",
            parameters={"type": "object",
                        "properties": {"tag": {"type": "string"}},
                        "required": ["tag"]},
            handler=handler, risk_class=RISK_REMOTE_COMPUTE,
            needs_context=True))

    def setUp(self):
        super().setUp()
        self.gen_log = []
        self.gen_behavior = "ok"

    def test_pending_with_state_reattaches_on_resume(self):
        """Killed after enqueue (job_id journaled): resume must re-execute
        the handler, which re-polls — not report outcome-unknown, and the
        handler must see its own journaled job_id."""
        j1 = Journal(self.db)
        rid = j1.create_run("t", "fake-model")
        j1.append_message(rid, "system", "sys")
        j1.append_message(rid, "user", "TASK:\nt")
        a_seq = j1.append_message(rid, "assistant", tc("genlike", tag="x"))
        key = idem_key(rid, a_seq, "genlike", {"tag": "x"})
        j1.record_call_start(key, rid, a_seq, "genlike", {"tag": "x"})
        j1.set_call_state(rid, key, {"kind": "generation", "job_id": "J-old"})
        j1.close()   # <- killed while polling

        loop, gw, _ = self.make_loop([tc("final_answer", answer="ok")])
        self._register_genlike(loop)
        report = loop.resume(rid)
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.gen_log, ["x"])            # re-executed once
        sent = json.dumps(gw.calls[0][0])
        self.assertIn("J-old", sent)                      # re-attached, not new
        self.assertIn("reattached", sent)

    def test_interrupt_mid_tool_then_resume(self):
        """SIGINT during the poll: the call stays pending WITH state; the run
        ends interrupted; a fresh process resumes and re-attaches."""
        self.gen_behavior = "interrupt"
        loop, _, journal = self.make_loop([tc("genlike", tag="v")])
        self._register_genlike(loop)
        report = loop.run("make a picture")
        self.assertEqual(report["outcome"], "interrupted")
        rid = report["run_id"]
        # the call is pending, with its job_id journaled:
        states = journal.list_call_states(rid)
        self.assertEqual(len(states), 1)
        (key, state), = states.items()
        self.assertEqual(state["job_id"], "J-v")
        self.assertEqual(journal.lookup_call(key)["status"], "pending")
        journal.close()

        self.gen_behavior = "ok"
        loop2, gw2, _ = self.make_loop([tc("final_answer", answer="ok")])
        self._register_genlike(loop2)
        report2 = loop2.resume(rid)
        self.assertEqual(report2["outcome"], "done")
        self.assertIn("J-v", json.dumps(gw2.calls[0][0]))  # same job resumed

    def test_pending_without_state_stays_unknown(self):
        """A remote_compute call that died BEFORE the remote accepted work
        (no state) follows the fail-closed rule."""
        j1 = Journal(self.db)
        rid = j1.create_run("t", "fake-model")
        j1.append_message(rid, "system", "sys")
        j1.append_message(rid, "user", "TASK:\nt")
        a_seq = j1.append_message(rid, "assistant", tc("genlike", tag="z"))
        key = idem_key(rid, a_seq, "genlike", {"tag": "z"})
        j1.record_call_start(key, rid, a_seq, "genlike", {"tag": "z"})
        j1.close()   # <- killed inside enqueue, nothing journaled

        loop, gw, _ = self.make_loop([tc("fs_glob", pattern="*"),   # guard needs 1 success
                                      tc("final_answer", answer="ok")])
        self._register_genlike(loop)
        report = loop.resume(rid)
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.gen_log, [])               # NOT re-executed
        self.assertIn("outcome is unknown", json.dumps(gw.calls[0][0]))


class CompactionTests(LoopHarness):
    def test_over_budget_triggers_summary(self):
        gw = FakeGateway([], ctx=256)  # tiny budget forces compaction
        journal = Journal(self.db)
        reg = build_registry(self.ws, gw, Memory(self.ws))
        loop = AgentLoop(self.cfg, gateway=gw, registry=reg, journal=journal,
                         adapter=Adapter("prompted"), memory=Memory(self.ws))
        rid = journal.create_run("t", "fake-model")
        journal.append_message(rid, "system", "sys")
        journal.append_message(rid, "user", "TASK:\nt")
        for i in range(12):
            journal.append_message(rid, "assistant", "filler %d " % i + "x" * 400)
            journal.append_message(rid, "user", "resp %d " % i + "y" * 400)
        gw.replies = ["a compact summary of earlier work"]
        loop._compact_if_needed(rid)
        wire = journal.wire_messages(rid)
        joined = json.dumps(wire)
        self.assertIn("a compact summary of earlier work", joined)
        self.assertIn("sys", wire[0]["content"])          # pinned survives
        self.assertIn("TASK", wire[1]["content"])
        self.assertLess(len(wire), 26)

    def test_summary_failure_degrades_not_fails(self):
        from hugpy_agent.gateway import ChatResult
        gw = FakeGateway([ChatResult(ok=False, error="down")], ctx=256)
        journal = Journal(self.db)
        reg = build_registry(self.ws, gw, Memory(self.ws))
        loop = AgentLoop(self.cfg, gateway=gw, registry=reg, journal=journal,
                         adapter=Adapter("prompted"), memory=Memory(self.ws))
        rid = journal.create_run("t", "fake-model")
        journal.append_message(rid, "system", "sys")
        journal.append_message(rid, "user", "TASK:\nt")
        for i in range(12):
            journal.append_message(rid, "assistant", "z" * 400)
        loop._compact_if_needed(rid)
        self.assertIn("dropped to fit", json.dumps(journal.wire_messages(rid)))


if __name__ == "__main__":
    unittest.main()
