"""Policy engine (P2.1): the pure decide() matrix, list precedence, the
fail-closed defaults, config wiring, and the loop-level gate — a denied call
comes back as error-as-data and the loop keeps driving."""
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
from hugpy_agent.policy import ALLOW, ASK, DENY, decide
from hugpy_agent.tools import (RISK_DESTRUCTIVE, RISK_NETWORK, RISK_READONLY,
                               RISK_REMOTE_COMPUTE, RISK_WRITE, ToolSpec,
                               build_registry)

from helpers import FakeGateway, tc

ALL_RISKS = (RISK_READONLY, RISK_WRITE, RISK_DESTRUCTIVE, RISK_NETWORK,
             RISK_REMOTE_COMPUTE)
UNSAFE_RISKS = tuple(r for r in ALL_RISKS if r != RISK_READONLY)


def _spec(risk, name="t"):
    return ToolSpec(name=name, description="test tool",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda: "ok", risk_class=risk)


class DecideMatrixTests(unittest.TestCase):
    """The full mode x risk-class matrix, one assertion per cell."""

    def test_readonly_mode(self):
        self.assertEqual(decide("readonly", _spec(RISK_READONLY), {}), ALLOW)
        for risk in UNSAFE_RISKS:
            self.assertEqual(decide("readonly", _spec(risk), {}), DENY, risk)

    def test_ask_mode(self):
        self.assertEqual(decide("ask", _spec(RISK_READONLY), {}), ALLOW)
        for risk in UNSAFE_RISKS:
            self.assertEqual(decide("ask", _spec(risk), {}), ASK, risk)

    def test_auto_mode(self):
        for risk in ALL_RISKS:
            self.assertEqual(decide("auto", _spec(risk), {}), ALLOW, risk)

    def test_unknown_mode_fail_closes_to_ask(self):
        for mode in ("yolo", "", None, "READ-ONLY?"):
            self.assertEqual(decide(mode, _spec(RISK_READONLY), {}), ALLOW, mode)
            self.assertEqual(decide(mode, _spec(RISK_WRITE), {}), ASK, mode)

    def test_mode_string_is_normalized(self):
        self.assertEqual(decide(" READONLY ", _spec(RISK_WRITE), {}), DENY)
        self.assertEqual(decide("Auto", _spec(RISK_DESTRUCTIVE), {}), ALLOW)

    def test_unknown_risk_class_treated_as_unsafe(self):
        """A risk class nobody classified must get the unsafe default, never
        a free pass (fail closed on ambiguity)."""
        weird = _spec("experimental")
        self.assertEqual(decide("readonly", weird, {}), DENY)
        self.assertEqual(decide("ask", weird, {}), ASK)
        self.assertEqual(decide("auto", weird, {}), ALLOW)


class ListPrecedenceTests(unittest.TestCase):
    """explicit deny > explicit allow > mode default — in every mode."""

    def test_allow_list_overrides_mode(self):
        spec = _spec(RISK_WRITE, name="fs_write")
        for mode in ("readonly", "ask"):
            self.assertEqual(
                decide(mode, spec, {}, allow=["fs_write"]), ALLOW, mode)

    def test_deny_list_overrides_mode(self):
        spec = _spec(RISK_READONLY, name="fs_read")
        for mode in ("readonly", "ask", "auto"):
            self.assertEqual(
                decide(mode, spec, {}, deny=["fs_read"]), DENY, mode)

    def test_deny_beats_allow(self):
        spec = _spec(RISK_WRITE, name="shell")
        self.assertEqual(
            decide("auto", spec, {}, allow=["shell"], deny=["shell"]), DENY)

    def test_lists_only_match_the_named_tool(self):
        spec = _spec(RISK_WRITE, name="fs_write")
        self.assertEqual(
            decide("ask", spec, {}, allow=["shell"], deny=["http_get"]), ASK)


class ConfigWiringTests(unittest.TestCase):
    def test_defaults(self):
        cfg = Config()
        self.assertEqual(cfg.policy_mode, "ask")
        self.assertEqual(cfg.tool_allow, [])
        self.assertEqual(cfg.tool_deny, [])

    def test_env_and_comma_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(environ={
                "HUGPY_WORKSPACE": tmp,
                "HUGPY_POLICY": "readonly",
                "HUGPY_TOOL_ALLOW": "fs_read, fs_glob",
                "HUGPY_TOOL_DENY": "shell",
            })
        self.assertEqual(cfg.policy_mode, "readonly")
        self.assertEqual(cfg.tool_allow, ["fs_read", "fs_glob"])
        self.assertEqual(cfg.tool_deny, ["shell"])
        self.assertEqual(cfg.sources["policy_mode"], "env")

    def test_cli_override_beats_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(overrides={"policy_mode": "auto"},
                              environ={"HUGPY_WORKSPACE": tmp,
                                       "HUGPY_POLICY": "readonly"})
        self.assertEqual(cfg.policy_mode, "auto")
        self.assertEqual(cfg.sources["policy_mode"], "cli")


class LoopGateTests(unittest.TestCase):
    """The gate in _execute: denials are error-as-data, journaled, and the
    loop CONTINUES to the model's next step — never a raise, never a hang."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
        self.db = os.path.join(self.ws, ".hugpy_agent", "journal.db")
        self.effect_runs = []

    def tearDown(self):
        self.tmp.cleanup()

    def make_loop(self, replies, policy_mode, allow=None, deny=None):
        cfg = Config(workspace=self.ws, tools_mode="prompted", max_steps=10,
                     model="fake-model", policy_mode=policy_mode,
                     tool_allow=allow or [], tool_deny=deny or [])
        gw = FakeGateway(replies)
        journal = Journal(self.db)
        reg = build_registry(self.ws, gw, Memory(self.ws))
        reg.register(ToolSpec(
            name="effect", description="side-effecting test tool",
            parameters={"type": "object",
                        "properties": {"tag": {"type": "string"}},
                        "required": ["tag"]},
            handler=lambda tag: self.effect_runs.append(tag) or "effect-ran:%s" % tag,
            risk_class=RISK_WRITE))
        loop = AgentLoop(cfg, gateway=gw, registry=reg, journal=journal,
                         adapter=Adapter("prompted"), memory=Memory(self.ws))
        return loop, gw, journal

    def test_readonly_mode_denies_write_and_loop_continues(self):
        loop, gw, journal = self.make_loop([
            tc("effect", tag="boom"),
            tc("fs_glob", pattern="*"),   # one SUCCESSFUL call: the final_answer guard needs it
            tc("final_answer", answer="could not write; reported"),
        ], policy_mode="readonly")
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")            # loop continued
        self.assertEqual(self.effect_runs, [])                 # never executed
        sent = json.dumps(gw.calls[1][0])                      # model saw data
        self.assertIn("policy denied", sent)
        # ...and the denial is journaled like any other call result:
        rows = journal.raw_messages(report["run_id"])
        self.assertIn("policy denied", json.dumps([r["content"] for r in rows]))
        self.assertEqual(report["tool_calls"], 2)              # denied effect + fs_glob both recorded

    def test_readonly_mode_still_allows_reads(self):
        with open(os.path.join(self.ws, "f.txt"), "w") as fh:
            fh.write("payload")
        loop, gw, _ = self.make_loop([
            tc("fs_read", path="f.txt"),
            tc("final_answer", answer="read it"),
        ], policy_mode="readonly")
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertIn("payload", json.dumps(gw.calls[1][0]))

    def test_default_ask_fails_closed_with_clear_message(self):
        """With no operator channel configured (P2.3), ask fails closed to
        deny and SAYS WHY, so the operator knows what to configure."""
        loop, gw, _ = self.make_loop([
            tc("effect", tag="boom"),
            tc("fs_glob", pattern="*"),   # one SUCCESSFUL call: the final_answer guard needs it
            tc("final_answer", answer="blocked"),
        ], policy_mode="ask")
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.effect_runs, [])
        sent = json.dumps(gw.calls[1][0])
        self.assertIn("operator approval", sent)
        self.assertIn("no operator channel configured", sent)
        self.assertIn("HUGPY_DISCORD_SESSION", sent)

    def test_allow_list_lets_a_write_through_readonly(self):
        loop, _, _ = self.make_loop([
            tc("effect", tag="ok"),
            tc("final_answer", answer="done"),
        ], policy_mode="readonly", allow=["effect"])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.effect_runs, ["ok"])

    def test_deny_list_blocks_even_in_auto(self):
        loop, gw, _ = self.make_loop([
            tc("effect", tag="no"),
            tc("fs_glob", pattern="*"),   # one SUCCESSFUL call: the final_answer guard needs it
            tc("final_answer", answer="done"),
        ], policy_mode="auto", deny=["effect"])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.effect_runs, [])
        self.assertIn("policy denied", json.dumps(gw.calls[1][0]))


if __name__ == "__main__":
    unittest.main()
