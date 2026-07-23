"""Subagent (`spawn`) behavior against a scripted model (P2.5): delegation
runs a linked child to completion, authority never widens (strict tool
subset, inherited policy), depth and budget caps hold, and a parent resumed
over a pending spawn RE-ATTACHES to its child instead of re-spawning.

The parent and child share one FakeGateway script: the child runs
synchronously inside the parent's spawn call, so replies pop in strict
chronological order across both loops."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import json
import os
import tempfile
import unittest

from hugpy_agent.adapter import Adapter
from hugpy_agent.config import Config, load_config
from hugpy_agent.journal import Journal, idem_key
from hugpy_agent.loop import AgentLoop
from hugpy_agent.memory import Memory
from hugpy_agent.subagent import generations_used, make_spawn_spec
from hugpy_agent.tools import RISK_READONLY, ToolSpec

from helpers import FakeGateway, tc


class SubagentHarness(unittest.TestCase):
    """Temp workspace + parent loops built through the PRODUCTION registry
    path (registry=None), so spawn registration is the real code path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
        # policy_mode=auto except where a test targets inheritance; the
        # policy gate itself is covered in tests/test_policy.py.
        self.cfg = Config(workspace=self.ws, tools_mode="prompted",
                          max_steps=10, model="fake-model",
                          policy_mode="auto")
        self.db = os.path.join(self.ws, ".hugpy_agent", "journal.db")

    def tearDown(self):
        self.tmp.cleanup()

    def make_loop(self, replies, journal=None):
        gw = FakeGateway(replies)
        journal = journal or Journal(self.db)
        loop = AgentLoop(self.cfg, gateway=gw, journal=journal,
                         adapter=Adapter("prompted"), memory=Memory(self.ws))
        return loop, gw, journal

    def child_runs(self, journal, parent_rid):
        return [r for r in journal.list_runs()
                if r.get("parent_run_id") == parent_rid]

    def run_text(self, journal, rid):
        """Everything journaled for one run, as one searchable string."""
        return json.dumps(journal.raw_messages(rid))


class SpawnBasicsTests(SubagentHarness):
    def test_spawn_runs_child_and_returns_answer(self):
        """The core contract: spawn runs a linked child run to completion
        and hands its final answer back to the parent as data."""
        with open(os.path.join(self.ws, "a.txt"), "w") as fh:
            fh.write("alpha facts")
        loop, gw, journal = self.make_loop([
            tc("spawn", brief="read a.txt and report its contents"),
            tc("fs_read", path="a.txt"),                    # child
            tc("final_answer", answer="a.txt says: alpha facts"),  # child
            tc("final_answer", answer="delegated: alpha facts"),   # parent
        ])
        report = loop.run("delegate the reading of a.txt")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(report["answer"], "delegated: alpha facts")
        # exactly one child run, linked to the parent
        children = self.child_runs(journal, report["run_id"])
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["status"], "done")
        self.assertEqual(children[0]["task"],
                         "read a.txt and report its contents")
        # the child's answer reached the PARENT model as the tool result
        parent_wire = json.dumps(gw.calls[3][0])
        self.assertIn("a.txt says: alpha facts", parent_wire)
        self.assertIn(children[0]["run_id"], parent_wire)

    def test_default_child_toolset_excludes_spawn(self):
        """Without an explicit `tools` grant the child gets the parent's
        toolset MINUS spawn — depth stays bounded by default."""
        loop, _, journal = self.make_loop([
            tc("spawn", brief="small job"),
            tc("final_answer", answer="child done"),        # child
            tc("final_answer", answer="done"),              # parent
        ])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        crid = self.child_runs(journal, report["run_id"])[0]["run_id"]
        child_sys = journal.raw_messages(crid)[0]["content"]
        self.assertNotIn("spawn", child_sys)                # tool not offered
        self.assertIn("fs_read", child_sys)                 # the rest is there

    def test_child_max_steps_capped_at_sub_max_steps(self):
        """A spawn may ask for FEWER steps than the deployment cap, never
        more — the journaled call state records the effective cap."""
        self.cfg.sub_max_steps = 4
        loop, _, journal = self.make_loop([
            tc("spawn", brief="b", max_steps=999),
            tc("final_answer", answer="child done"),        # child
            tc("final_answer", answer="done"),              # parent
        ])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        (state,) = journal.list_call_states(report["run_id"]).values()
        self.assertEqual(state["kind"], "subagent")
        self.assertEqual(state["max_steps"], 4)


class AuthorityTests(SubagentHarness):
    def test_requesting_tool_parent_lacks_is_refused(self):
        """The strict-subset gate: a child cannot be granted a tool the
        parent does not hold. Refused as data; no child run is created."""
        loop, gw, journal = self.make_loop([
            tc("spawn", brief="x", tools=["fs_read", "teleport"]),
            tc("final_answer", answer="could not delegate"),
        ])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.child_runs(journal, report["run_id"]), [])
        self.assertEqual(len(journal.list_runs()), 1)       # parent only
        refusal = json.dumps(gw.calls[1][0])
        self.assertIn("spawn refused", refusal)
        self.assertIn("teleport", refusal)

    def test_readonly_parent_yields_readonly_child(self):
        """Policy inheritance: under a readonly parent the child's write
        call is denied by the CHILD's own policy gate — delegation cannot
        exceed the parent's authority."""
        self.cfg.policy_mode = "readonly"
        loop, _, journal = self.make_loop([
            tc("spawn", brief="write x.txt"),               # spawn is readonly
            tc("fs_write", path="x.txt", content="boom"),   # child: denied
            tc("final_answer", answer="could not write"),   # child
            tc("final_answer", answer="done"),              # parent
        ])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertFalse(os.path.exists(os.path.join(self.ws, "x.txt")))
        crid = self.child_runs(journal, report["run_id"])[0]["run_id"]
        self.assertIn("policy denied", self.run_text(journal, crid))

    def test_depth_cap_removes_spawn_at_floor(self):
        """A child AT max_depth gets no spawn tool even when it was
        explicitly requested — the recursion bound is structural."""
        self.cfg.max_depth = 1
        loop, _, journal = self.make_loop([
            tc("spawn", brief="try recursion",
               tools=["fs_glob", "spawn", "final_answer"]),
            tc("spawn", brief="grandchild"),                # child: unknown tool
            tc("final_answer", answer="no spawn down here"),  # child
            tc("final_answer", answer="done"),              # parent
        ])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(len(journal.list_runs()), 2)       # no grandchild
        crid = self.child_runs(journal, report["run_id"])[0]["run_id"]
        self.assertIn("unknown tool 'spawn'", self.run_text(journal, crid))

    def test_max_depth_zero_removes_spawn_from_root(self):
        """make_spawn_spec gates on the loop's own depth: at (or past) the
        floor it returns None and build_registry registers nothing."""
        loop, _, _ = self.make_loop([])
        self.assertIsNotNone(loop.registry.get("spawn"))    # depth 0 < default 2
        self.assertIsNotNone(make_spawn_spec(loop))
        self.cfg.max_depth = 0
        loop0, _, _ = self.make_loop([])
        self.assertIsNone(loop0.registry.get("spawn"))
        self.assertIsNone(make_spawn_spec(loop0))

    def test_config_knobs_from_env(self):
        cfg = load_config(workspace=self.ws, environ={
            "HUGPY_SUB_MAX_STEPS": "5", "HUGPY_MAX_DEPTH": "1"})
        self.assertEqual(cfg.sub_max_steps, 5)
        self.assertEqual(cfg.max_depth, 1)


class BudgetTests(SubagentHarness):
    """Children draw on the SAME max_generations pool as the parent,
    measured from journaled run state across the whole delegation tree."""

    def _register_budget_tools(self, loop):
        loop.registry.register(ToolSpec(
            name="fake_gen", description="enqueue-shaped test tool",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda _context=None: (
                _context.set_state({"kind": "generation", "job_id": "J"})
                or "enqueued"),
            risk_class=RISK_READONLY, needs_context=True))
        loop.registry.register(ToolSpec(
            name="gencap", description="reports the effective generation cap",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda _context=None: "cap=%d" % _context.max_generations,
            risk_class=RISK_READONLY, needs_context=True))

    def test_children_share_the_generation_pool(self):
        self.cfg.max_generations = 3
        loop, _, journal = self.make_loop([
            tc("fake_gen"),                                 # parent spends 1
            tc("spawn", brief="child A work"),
            tc("gencap"),                                   # child A sees 3-1=2
            tc("fake_gen"),                                 # child A spends 1
            tc("final_answer", answer="A done"),            # child A
            tc("spawn", brief="child B work"),
            tc("gencap"),                                   # child B sees 3-2=1
            tc("final_answer", answer="B done"),            # child B
            tc("final_answer", answer="done"),              # parent
        ])
        self._register_budget_tools(loop)
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        children = {r["task"]: r["run_id"]
                    for r in self.child_runs(journal, report["run_id"])}
        self.assertIn("cap=2", self.run_text(journal, children["child A work"]))
        self.assertIn("cap=1", self.run_text(journal, children["child B work"]))
        # and the recursive accounting matches: 1 parent + 1 child enqueue
        self.assertEqual(generations_used(journal, report["run_id"]), 2)


class MigrationTests(SubagentHarness):
    def test_old_journal_gains_parent_column_on_open(self):
        """A journal created before P2.5 has no parent_run_id column; the
        idempotent ALTER pass must retrofit it without touching old rows."""
        import sqlite3
        os.makedirs(os.path.dirname(self.db), exist_ok=True)
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, task TEXT NOT NULL,"
            " model TEXT NOT NULL, status TEXT NOT NULL, outcome TEXT,"
            " created_at REAL NOT NULL, updated_at REAL NOT NULL)")
        conn.execute(
            "INSERT INTO runs VALUES ('old1','legacy task','m','done',"
            " NULL, 1.0, 1.0)")
        conn.commit()
        conn.close()

        journal = Journal(self.db)                          # migrates on open
        self.assertIsNone(journal.get_run("old1")["parent_run_id"])
        rid = journal.create_run("new", "m", parent_run_id="old1")
        self.assertEqual(journal.get_run(rid)["parent_run_id"], "old1")
        journal.close()
        journal2 = Journal(self.db)                         # re-open: no-op
        self.assertEqual(journal2.get_run(rid)["parent_run_id"], "old1")
        journal2.close()


class ResumeTests(SubagentHarness):
    """A pending spawn on resume must RE-ATTACH to the journaled child run
    (same pattern as async generation jobs) — never spawn a duplicate. The
    'kill' is simulated by seeding the exact journal state a dead process
    leaves behind, then resuming with a fresh loop."""

    def _seed_pending_spawn(self, child_status=None, child_outcome=None):
        j1 = Journal(self.db)
        rid = j1.create_run("parent task", "fake-model")
        j1.append_message(rid, "system", "sys")
        j1.append_message(rid, "user", "TASK:\nparent task")
        a_seq = j1.append_message(rid, "assistant",
                                  tc("spawn", brief="sub task"))
        key = idem_key(rid, a_seq, "spawn", {"brief": "sub task"})
        j1.record_call_start(key, rid, a_seq, "spawn", {"brief": "sub task"})
        crid = j1.create_run("sub task", "fake-model", parent_run_id=rid)
        j1.append_message(crid, "system", "sys")
        j1.append_message(crid, "user", "TASK:\nsub task")
        j1.set_call_state(rid, key, {
            "kind": "subagent", "child_run_id": crid,
            "tools": ["fs_glob", "final_answer"],
            "max_steps": 5, "max_generations": 2})
        if child_status:
            j1.set_run_status(crid, child_status, child_outcome)
        j1.close()   # <- process dies here
        return rid, crid

    def test_resume_reattaches_to_inflight_child(self):
        rid, crid = self._seed_pending_spawn()
        loop, gw, journal = self.make_loop([
            tc("final_answer", answer="child-part-done"),   # child continues
            tc("final_answer", answer="parent-done"),       # parent
        ])
        report = loop.resume(rid)
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(report["answer"], "parent-done")
        self.assertEqual(len(journal.list_runs()), 2)       # NO re-spawn
        self.assertEqual(journal.get_run(crid)["status"], "done")
        # the re-attached child's answer reached the parent model
        self.assertIn("child-part-done", json.dumps(gw.calls[1][0]))

    def test_resume_collects_completed_child_without_rerun(self):
        """Child finished before the parent died: re-attach returns the
        journaled outcome — zero child model calls."""
        rid, crid = self._seed_pending_spawn(
            child_status="done",
            child_outcome={"run_id": "x", "outcome": "done",
                           "answer": "forty-two", "steps": 1,
                           "tool_calls": 0})
        loop, gw, journal = self.make_loop([
            tc("final_answer", answer="ok"),                # parent only
        ])
        report = loop.resume(rid)
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(len(gw.calls), 1)                  # child never called
        self.assertEqual(len(journal.list_runs()), 2)
        self.assertIn("forty-two", json.dumps(gw.calls[0][0]))

    def test_operator_stop_chains_into_child_and_resume_reattaches(self):
        """SIGINT mid-child: the child sees the parent's stop signal, ends
        interrupted, the spawn call stays pending WITH its child_run_id,
        and a fresh process resumes the SAME child to completion."""
        def stopper_spec(handler):
            return ToolSpec(
                name="stopper", description="test stop trigger",
                parameters={"type": "object", "properties": {},
                            "required": []},
                handler=handler, risk_class=RISK_READONLY)

        loop, _, journal = self.make_loop([
            tc("spawn", brief="interruptible work"),
            tc("stopper"),                                  # child; sets stop
        ])
        loop.registry.register(stopper_spec(
            lambda: setattr(loop, "stop_requested", True) or "stopped"))
        report = loop.run("t")
        self.assertEqual(report["outcome"], "interrupted")
        rid = report["run_id"]
        (crid,) = [r["run_id"] for r in self.child_runs(journal, rid)]
        self.assertEqual(journal.get_run(crid)["status"], "interrupted")
        # the spawn call is still pending, with the child journaled:
        states = journal.list_call_states(rid)
        (key, state), = states.items()
        self.assertEqual(state["child_run_id"], crid)
        self.assertEqual(journal.lookup_call(key)["status"], "pending")
        journal.close()

        loop2, _, journal2 = self.make_loop([
            tc("final_answer", answer="child recovered"),   # SAME child resumes
            tc("final_answer", answer="parent recovered"),  # parent
        ])
        loop2.registry.register(stopper_spec(lambda: "noop"))
        report2 = loop2.resume(rid)
        self.assertEqual(report2["outcome"], "done")
        self.assertEqual(report2["answer"], "parent recovered")
        self.assertEqual(len(journal2.list_runs()), 2)      # still no re-spawn
        self.assertEqual(journal2.get_run(crid)["status"], "done")


if __name__ == "__main__":
    unittest.main()
