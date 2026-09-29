"""Structured delegation (P2.6): dispatch-packet validation is fail-closed,
trace artifacts are written atomically and round-trip through read_trace,
and the spawn wiring writes exactly one artifact per delegation — packeted
or synthesized — without ever letting a trace failure crash the spawn path.

End-to-end tests reuse the scripted-model harness from test_subagent.py:
parent and child share one FakeGateway reply script."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import json
import os
import tempfile
import unittest
from unittest import mock

from hugpy_agent.adapter import Adapter
from hugpy_agent.config import Config
from hugpy_agent.journal import Journal
from hugpy_agent.loop import AgentLoop
from hugpy_agent.memory import Memory
from hugpy_agent import trace as trace_mod
from hugpy_agent.trace import (read_trace, synthesize_packet, trace_path,
                               traces_dir, validate_packet, write_trace)

from helpers import FakeGateway, tc

GOOD_PACKET = {
    "objective": "read a.txt and report its contents",
    "scope": "workspace file a.txt only",
    "constraints": ["read-only", "no network"],
    "verification": "parent greps the answer for 'alpha facts'",
    "handoff_expectations": "the verbatim file contents",
}


class ValidatePacketTests(unittest.TestCase):
    def test_valid_packet_passes(self):
        self.assertEqual(validate_packet(GOOD_PACKET), [])

    def test_valid_with_optional_context_refs(self):
        pkt = dict(GOOD_PACKET, context_refs=["memory/foo.md"])
        self.assertEqual(validate_packet(pkt), [])

    def test_empty_constraints_allowed(self):
        pkt = dict(GOOD_PACKET, constraints=[])
        self.assertEqual(validate_packet(pkt), [])

    def test_non_dict_refused(self):
        errs = validate_packet("just a string")
        self.assertEqual(len(errs), 1)
        self.assertIn("must be an object", errs[0])

    def test_all_missing_fields_reported_at_once(self):
        """Every problem in ONE reply — the model repairs the packet in a
        single round-trip instead of one refusal per field."""
        errs = validate_packet({})
        names = " ".join(errs)
        for field in ("objective", "scope", "verification",
                      "handoff_expectations", "constraints"):
            self.assertIn("packet.%s" % field, names)

    def test_blank_and_wrong_type_strings_refused(self):
        errs = validate_packet(dict(GOOD_PACKET, objective="   ",
                                    verification=42))
        joined = " ".join(errs)
        self.assertIn("packet.objective", joined)
        self.assertIn("packet.verification", joined)

    def test_non_string_list_items_refused(self):
        errs = validate_packet(dict(GOOD_PACKET, constraints=["ok", 7]))
        self.assertIn("packet.constraints", " ".join(errs))

    def test_unknown_fields_refused(self):
        errs = validate_packet(dict(GOOD_PACKET, surprise=1))
        self.assertIn("unknown field", " ".join(errs))
        self.assertIn("surprise", " ".join(errs))


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_and_round_trip_read(self):
        outcome = {"status": "done", "summary": "reported alpha facts",
                   "evidence": "journal run c1: steps=2 tool_calls=1"}
        path = write_trace(self.ws, "p1", "c1", GOOD_PACKET, outcome,
                           created_at="2026-07-22T00:00:00+00:00")
        self.assertEqual(path, trace_path(self.ws, "p1", "c1"))
        self.assertTrue(path.endswith(
            os.path.join(".hugpy_agent", "traces", "p1", "c1.md")))
        got = read_trace(path)
        self.assertEqual(got["parent_run_id"], "p1")
        self.assertEqual(got["child_run_id"], "c1")
        self.assertEqual(got["status"], "done")
        self.assertEqual(got["created_at"], "2026-07-22T00:00:00+00:00")
        self.assertEqual(got["dispatch"]["objective"],
                         GOOD_PACKET["objective"])
        self.assertEqual(got["dispatch"]["constraints"],
                         GOOD_PACKET["constraints"])   # lists round-trip
        self.assertEqual(got["outcome"]["status"], "done")
        self.assertEqual(got["outcome"]["summary"], "reported alpha facts")
        self.assertEqual(got["handoff"]["expected"],
                         GOOD_PACKET["handoff_expectations"])

    def test_unstructured_marker_round_trips(self):
        pkt = synthesize_packet("just do the thing")
        path = write_trace(self.ws, "p1", "c2", pkt, {"status": "done"})
        got = read_trace(path)
        self.assertEqual(got["dispatch"]["unstructured"], True)
        self.assertEqual(got["dispatch"]["objective"], "just do the thing")
        self.assertEqual(got["dispatch"]["scope"], "(unstated)")

    def test_atomic_no_partial_file_on_failure(self):
        """A crash mid-write must leave neither the artifact nor a tmp
        turd — readers see the old state or the whole new file, never a
        torn middle."""
        target = trace_path(self.ws, "p1", "c3")
        with mock.patch.object(trace_mod.os, "replace",
                               side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                write_trace(self.ws, "p1", "c3", GOOD_PACKET,
                            {"status": "done"})
        self.assertFalse(os.path.exists(target))
        parent = os.path.dirname(target)
        leftovers = [f for f in os.listdir(parent)] if os.path.isdir(parent) else []
        self.assertEqual(leftovers, [])                 # tmp cleaned up

    def test_index_appends_one_line_per_artifact(self):
        write_trace(self.ws, "p1", "c1",
                    dict(GOOD_PACKET, objective="x" * 200),
                    {"status": "done"})
        write_trace(self.ws, "p1", "c2", GOOD_PACKET, {"status": "aborted"})
        index = os.path.join(traces_dir(self.ws), "INDEX.md")
        with open(index) as fh:
            text = fh.read()
        lines = [ln for ln in text.splitlines() if ln.startswith("- [")]
        self.assertEqual(len(lines), 2)
        self.assertIn("[c1](p1/c1.md)", lines[0])
        self.assertIn("x" * 80, lines[0])
        self.assertNotIn("x" * 81, lines[0])            # objective truncated
        self.assertIn("aborted", lines[1])

    def test_read_trace_rejects_non_artifact(self):
        path = os.path.join(self.ws, "nope.md")
        with open(path, "w") as fh:
            fh.write("# just a doc\n")
        with self.assertRaises(ValueError):
            read_trace(path)


class SpawnWiringTests(unittest.TestCase):
    """End-to-end through the real spawn tool (production registry path),
    same harness shape as test_subagent.py."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
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

    def test_spawn_with_valid_packet_briefs_child_and_writes_trace(self):
        with open(os.path.join(self.ws, "a.txt"), "w") as fh:
            fh.write("alpha facts")
        loop, _, journal = self.make_loop([
            tc("spawn", brief="delegate the read", packet=GOOD_PACKET),
            tc("fs_read", path="a.txt"),                      # child
            tc("final_answer", answer="a.txt says: alpha facts"),  # child
            tc("final_answer", answer="done"),                # parent
        ])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        (child,) = self.child_runs(journal, report["run_id"])
        # the dispatch contract rode into the child's task text
        self.assertIn("DISPATCH CONTRACT", child["task"])
        self.assertIn(GOOD_PACKET["verification"], child["task"])
        # exactly one artifact, carrying the packet and the outcome
        path = trace_path(self.ws, report["run_id"], child["run_id"])
        got = read_trace(path)
        self.assertEqual(got["status"], "done")
        self.assertEqual(got["dispatch"]["objective"],
                         GOOD_PACKET["objective"])
        self.assertNotIn("unstructured", got["dispatch"])
        self.assertIn("alpha facts", got["outcome"]["summary"])
        with open(os.path.join(traces_dir(self.ws), "INDEX.md")) as fh:
            self.assertIn(child["run_id"], fh.read())

    def test_spawn_with_invalid_packet_refused_as_data(self):
        """Fail-closed gate: no child run, no artifact, error string back
        to the model listing every packet problem."""
        loop, gw, journal = self.make_loop([
            tc("spawn", brief="x", packet={"objective": ""}),
            tc("final_answer", answer="could not delegate"),
        ])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.child_runs(journal, report["run_id"]), [])
        self.assertFalse(os.path.isdir(traces_dir(self.ws)))
        refusal = json.dumps(gw.calls[1][0])
        self.assertIn("invalid dispatch packet", refusal)
        self.assertIn("packet.objective", refusal)
        self.assertIn("packet.verification", refusal)

    def test_unpacketed_spawn_writes_unstructured_trace(self):
        loop, _, journal = self.make_loop([
            tc("spawn", brief="small job"),
            tc("final_answer", answer="child done"),          # child
            tc("final_answer", answer="done"),                # parent
        ])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        (child,) = self.child_runs(journal, report["run_id"])
        got = read_trace(trace_path(self.ws, report["run_id"],
                                    child["run_id"]))
        self.assertEqual(got["dispatch"]["unstructured"], True)
        self.assertEqual(got["dispatch"]["objective"], "small job")

    def test_failed_child_still_leaves_a_trace(self):
        """The artifact records failures too — an aborted delegation is
        evidence, not something to hide."""
        self.cfg.sub_max_steps = 1
        loop, _, journal = self.make_loop([
            tc("spawn", brief="doomed job"),
            tc("fs_glob", pattern="*"),        # child burns its only step
            tc("final_answer", answer="parent noted the failure"),
        ])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        (child,) = self.child_runs(journal, report["run_id"])
        got = read_trace(trace_path(self.ws, report["run_id"],
                                    child["run_id"]))
        self.assertNotEqual(got["status"], "done")

    def test_trace_write_failure_never_crashes_spawn(self):
        """Fail-open posture: an artifact write failure degrades to a
        trace_error event; the spawn result still reaches the parent."""
        events = []
        loop, _, _ = self.make_loop([
            tc("spawn", brief="job"),
            tc("final_answer", answer="child done"),          # child
            tc("final_answer", answer="done"),                # parent
        ])
        loop.on_event = lambda *a, **k: events.append(a)
        with mock.patch("hugpy_agent.subagent.write_trace",
                        side_effect=OSError("read-only fs")):
            report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(report["answer"], "done")
        self.assertIn("trace_error", [e[0] for e in events])

    def test_reattach_does_not_duplicate_artifact(self):
        """Kill-and-resume over a completed child: the artifact written by
        the first process survives untouched — no duplicate file, no
        duplicate INDEX line (same shape as ResumeTests in
        test_subagent.py, seeding the journal a dead process leaves)."""
        from hugpy_agent.journal import idem_key
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
            "max_steps": 5, "max_generations": 2,
            "task": "sub task", "packet": None})
        j1.set_run_status(crid, "done",
                          {"run_id": crid, "outcome": "done",
                           "answer": "forty-two", "steps": 1,
                           "tool_calls": 0})
        j1.close()
        # the FIRST process already wrote the artifact before dying:
        write_trace(self.ws, rid, crid, synthesize_packet("sub task"),
                    {"status": "done", "summary": "forty-two",
                     "evidence": "seeded"})
        before = read_trace(trace_path(self.ws, rid, crid))

        loop, _, journal = self.make_loop([
            tc("final_answer", answer="ok"),                  # parent only
        ])
        report = loop.resume(rid)
        self.assertEqual(report["outcome"], "done")
        after = read_trace(trace_path(self.ws, rid, crid))
        self.assertEqual(before, after)                       # untouched
        with open(os.path.join(traces_dir(self.ws), "INDEX.md")) as fh:
            index_lines = [ln for ln in fh.read().splitlines()
                           if crid in ln]
        self.assertEqual(len(index_lines), 1)                 # one line only

    def test_reattach_after_crash_writes_trace_with_original_packet(self):
        """Crash BEFORE the trace existed: resume re-attaches, collects the
        outcome, and writes the artifact from the JOURNALED packet — the
        original contract, not a synthesized one."""
        from hugpy_agent.journal import idem_key
        j1 = Journal(self.db)
        rid = j1.create_run("parent task", "fake-model")
        j1.append_message(rid, "system", "sys")
        j1.append_message(rid, "user", "TASK:\nparent task")
        args = {"brief": "sub task", "packet": GOOD_PACKET}
        a_seq = j1.append_message(rid, "assistant",
                                  tc("spawn", **args))
        key = idem_key(rid, a_seq, "spawn", args)
        j1.record_call_start(key, rid, a_seq, "spawn", args)
        crid = j1.create_run("sub task", "fake-model", parent_run_id=rid)
        j1.append_message(crid, "system", "sys")
        j1.append_message(crid, "user", "TASK:\nsub task")
        j1.set_call_state(rid, key, {
            "kind": "subagent", "child_run_id": crid,
            "tools": ["fs_glob", "final_answer"],
            "max_steps": 5, "max_generations": 2,
            "task": "sub task", "packet": dict(GOOD_PACKET)})
        j1.close()   # <- dies before the child finished; no artifact yet

        loop, _, _ = self.make_loop([
            tc("final_answer", answer="child recovered"),     # child resumes
            tc("final_answer", answer="parent recovered"),    # parent
        ])
        report = loop.resume(rid)
        self.assertEqual(report["outcome"], "done")
        got = read_trace(trace_path(self.ws, rid, crid))
        self.assertEqual(got["dispatch"]["objective"],
                         GOOD_PACKET["objective"])             # original
        self.assertNotIn("unstructured", got["dispatch"])
        self.assertEqual(got["status"], "done")


if __name__ == "__main__":
    unittest.main()
