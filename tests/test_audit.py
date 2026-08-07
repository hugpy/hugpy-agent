"""Audit trail (P2.2): well-formed JSONL per tool call, stable hashing,
the verbose opt-in, the never-raise doctrine on write failure, the empty-
path off switch, and the config wiring. All offline."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from hugpy_agent.adapter import Adapter
from hugpy_agent.audit import (VERBOSE_MAX, AuditLog, default_audit_path,
                               sha256_of)
from hugpy_agent.config import Config, load_config
from hugpy_agent.journal import Journal
from hugpy_agent.loop import AgentLoop
from hugpy_agent.memory import Memory
from hugpy_agent.tools import RISK_WRITE, ToolSpec, build_registry

from helpers import FakeGateway, tc

TS = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
SCHEMA_KEYS = {"ts_iso", "run_id", "step", "tool", "risk", "decision",
               "model", "args_sha256", "result_sha256", "result_len",
               "duration_ms", "error_bool"}


def _record(log, args=None, result="ok", **over):
    """One record() call with sane defaults, overridable per test."""
    kw = dict(run_id="r1", step=1, tool="fs_read", risk="readonly",
              decision="allow", args=args if args is not None else {"p": "x"},
              result=result, duration_ms=7, error=False)
    kw.update(over)
    log.record(TS, **kw)


def _lines(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "audit.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_well_formed_line_with_injected_clock(self):
        _record(AuditLog(self.path), args={"path": "f.txt"},
                result='{"n": 1}')
        (row,) = _lines(self.path)
        self.assertEqual(set(row), SCHEMA_KEYS)   # exact schema, no extras
        self.assertEqual(row["ts_iso"], "2026-07-15T12:00:00+00:00")
        self.assertEqual(row["run_id"], "r1")
        self.assertEqual(row["tool"], "fs_read")
        self.assertEqual(row["decision"], "allow")
        self.assertEqual(row["result_len"], len('{"n": 1}'))
        self.assertIs(row["error_bool"], False)

    def test_append_only_accumulates(self):
        log = AuditLog(self.path)
        for step in (1, 2, 3):
            _record(log, step=step)
        # a fresh writer instance appends rather than truncating:
        _record(AuditLog(self.path), step=4)
        self.assertEqual([r["step"] for r in _lines(self.path)],
                         [1, 2, 3, 4])

    def test_hashing_stable_across_runs_and_key_order(self):
        """Same logical args => same hash, regardless of process, writer
        instance, or dict insertion order (canonicalized JSON)."""
        self.assertEqual(sha256_of({"a": 1, "b": 2}),
                         sha256_of({"b": 2, "a": 1}))
        _record(AuditLog(self.path), args={"a": 1, "b": 2})
        _record(AuditLog(self.path), args={"b": 2, "a": 1})
        one, two = _lines(self.path)
        self.assertEqual(one["args_sha256"], two["args_sha256"])
        self.assertEqual(one["result_sha256"], sha256_of("ok"))

    def test_default_mode_hashes_only_no_secret_plaintext(self):
        """The core doctrine: a secret riding through args/result appears in
        the log ONLY as a hash."""
        _record(AuditLog(self.path), args={"key": "sk-SECRET-hunter2"},
                result="token=sk-SECRET-hunter2")
        raw = open(self.path, encoding="utf-8").read()
        self.assertNotIn("hunter2", raw)
        (row,) = _lines(self.path)
        self.assertNotIn("args_text", row)
        self.assertNotIn("result_text", row)

    def test_verbose_mode_stores_truncated_plaintext(self):
        long_result = "R" * (VERBOSE_MAX * 3)
        _record(AuditLog(self.path, verbose=True), args={"path": "f.txt"},
                result=long_result)
        (row,) = _lines(self.path)
        self.assertIn('"path":"f.txt"', row["args_text"])
        self.assertEqual(row["result_text"], "R" * VERBOSE_MAX)  # truncated
        # hashes remain (over the FULL result) so lines stay correlatable:
        self.assertEqual(row["result_sha256"], sha256_of(long_result))
        self.assertEqual(row["result_len"], len(long_result))

    def test_disabled_empty_path_writes_nothing(self):
        for path in ("", None):
            log = AuditLog(path)
            self.assertFalse(log.enabled)
            _record(log)                        # must be a silent no-op
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_write_failure_never_raises_and_emits_audit_error(self):
        """Point the writer at a directory: open('a') fails. The doctrine is
        that this must not raise into the run — only an event."""
        events = []
        log = AuditLog(self.tmp.name,                 # a dir, not a file
                       on_event=lambda *a: events.append(a))
        _record(log)                                   # must not raise
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "audit_error")
        self.assertTrue(events[0][1])                  # says what went wrong

    def test_creates_parent_directory(self):
        nested = os.path.join(self.tmp.name, "a", "b", "audit.jsonl")
        _record(AuditLog(nested))
        self.assertEqual(len(_lines(nested)), 1)


class ConfigWiringTests(unittest.TestCase):
    def test_defaults(self):
        cfg = Config()
        self.assertIsNone(cfg.audit_log)     # None => workspace default path
        self.assertFalse(cfg.audit_verbose)

    def test_env_sets_path_and_verbose(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(environ={
                "HUGPY_WORKSPACE": tmp,
                "HUGPY_AUDIT_LOG": "/var/log/agent-audit.jsonl",
                "HUGPY_AUDIT_VERBOSE": "1",
            })
        self.assertEqual(cfg.audit_log, "/var/log/agent-audit.jsonl")
        self.assertTrue(cfg.audit_verbose)
        self.assertEqual(cfg.sources["audit_log"], "env")

    def test_explicit_empty_env_disables(self):
        """HUGPY_AUDIT_LOG= (empty) is an off switch, not 'unset'."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(environ={"HUGPY_WORKSPACE": tmp,
                                       "HUGPY_AUDIT_LOG": ""})
        self.assertEqual(cfg.audit_log, "")

    def test_explicit_empty_dotenv_disables(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".env"), "w") as fh:
                fh.write("HUGPY_AUDIT_LOG=\n")
            cfg = load_config(environ={"HUGPY_WORKSPACE": tmp})
        self.assertEqual(cfg.audit_log, "")
        self.assertEqual(cfg.sources["audit_log"], ".env")


class LoopAuditTests(unittest.TestCase):
    """The seam in _execute: one line per resolved tool call, denials
    included, real run_ids/decisions, and the default-path resolution."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
        self.audit_path = os.path.join(self.ws, "audit.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def make_loop(self, replies, policy_mode="auto", audit_log=..., **cfg_kw):
        cfg = Config(workspace=self.ws, tools_mode="prompted", max_steps=10,
                     model="fake-model", policy_mode=policy_mode,
                     audit_log=(self.audit_path if audit_log is ...
                                else audit_log), **cfg_kw)
        gw = FakeGateway(replies)
        journal = Journal(os.path.join(self.ws, ".hugpy_agent", "journal.db"))
        reg = build_registry(self.ws, gw, Memory(self.ws))
        reg.register(ToolSpec(
            name="effect", description="side-effecting test tool",
            parameters={"type": "object",
                        "properties": {"tag": {"type": "string"}},
                        "required": ["tag"]},
            handler=lambda tag: "effect-ran:%s" % tag,
            risk_class=RISK_WRITE))
        return AgentLoop(cfg, gateway=gw, registry=reg, journal=journal,
                         adapter=Adapter("prompted"), memory=Memory(self.ws))

    def test_run_produces_one_line_per_tool_call(self):
        with open(os.path.join(self.ws, "f.txt"), "w") as fh:
            fh.write("payload")
        loop = self.make_loop([
            tc("fs_read", path="f.txt"),
            tc("fs_glob", pattern="*"),
            tc("final_answer", answer="done"),   # terminal: not a dispatch
        ])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        rows = _lines(self.audit_path)
        self.assertEqual([r["tool"] for r in rows], ["fs_read", "fs_glob"])
        for r in rows:
            self.assertEqual(set(r), SCHEMA_KEYS)
            self.assertEqual(r["run_id"], report["run_id"])
            self.assertEqual(r["decision"], "allow")
            self.assertIs(r["error_bool"], False)
            self.assertGreaterEqual(r["duration_ms"], 0)
        self.assertEqual([r["step"] for r in rows], [1, 2])

    def test_denied_and_asked_calls_audit_too(self):
        """Denials are the point of an audit trail: readonly-mode deny and
        default-mode ask (degraded to deny until P2.3) both land as lines
        carrying the POLICY decision."""
        loop = self.make_loop([tc("effect", tag="x"),
                               tc("final_answer", answer="blocked")],
                              policy_mode="readonly")
        loop.run("t")
        loop = self.make_loop([tc("effect", tag="x"),
                               tc("final_answer", answer="blocked")],
                              policy_mode="ask")
        loop.run("t")
        deny_row, ask_row = _lines(self.audit_path)
        self.assertEqual(deny_row["decision"], "deny")
        self.assertEqual(ask_row["decision"], "ask")
        for r in (deny_row, ask_row):
            self.assertEqual(r["tool"], "effect")
            self.assertEqual(r["risk"], "write")
            self.assertIs(r["error_bool"], True)    # the call did not run
            # the denial text itself is hashed, not stored:
            self.assertNotIn("args_text", r)

    def test_default_path_is_workspace_dot_dir(self):
        with open(os.path.join(self.ws, "f.txt"), "w") as fh:
            fh.write("x")
        loop = self.make_loop([tc("fs_read", path="f.txt"),
                               tc("final_answer", answer="ok")],
                              audit_log=None)     # cfg default => resolve
        loop.run("t")
        expected = default_audit_path(self.ws)
        self.assertEqual(
            expected, os.path.join(self.ws, ".hugpy_agent", "audit.jsonl"))
        self.assertEqual(len(_lines(expected)), 1)

    def test_disabled_via_empty_string_writes_nothing(self):
        loop = self.make_loop([tc("fs_glob", pattern="*"),
                               tc("final_answer", answer="ok")],
                              audit_log="")
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")   # run unaffected
        self.assertFalse(
            os.path.exists(os.path.join(self.ws, ".hugpy_agent",
                                        "audit.jsonl")))
        self.assertFalse(os.path.exists(self.audit_path))

    def test_write_failure_mid_run_emits_event_and_run_completes(self):
        events = []
        loop = self.make_loop([tc("fs_glob", pattern="*"),
                               tc("final_answer", answer="ok")],
                              audit_log=self.ws)     # a dir: open('a') fails
        loop.on_event = lambda kind, *a: events.append(kind)
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")  # audit never breaks a run
        self.assertIn("audit_error", events)

    def test_verbose_knob_flows_from_config(self):
        loop = self.make_loop([tc("fs_glob", pattern="*"),
                               tc("final_answer", answer="ok")],
                              audit_verbose=True)
        loop.run("t")
        (row,) = _lines(self.audit_path)
        self.assertIn("fs_glob", json.dumps(row))
        self.assertIn("result_text", row)


if __name__ == "__main__":
    unittest.main()
