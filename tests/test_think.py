"""Think management for Qwen3-family brains (2026-07-14 slice):
  * /no_think appended to the WIRE copy of the latest user turn when the knob
    is on, absent when off, on the text part of a multimodal turn, and NEVER
    in stored journal history;
  * <think>…</think> stripped from assistant output before tool_call parsing
    and before a final answer (leading think, think-then-call, multiple
    blocks, unclosed tolerated, outside text preserved);
  * default model resolves to the Qwen3-Coder-Next brain (2026-07-17 switch).
All offline, no network, no probes.
"""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import json
import os
import tempfile
import unittest

from hugpy_agent.adapter import Adapter, strip_think
from hugpy_agent.config import (DEFAULT_AGENT_BRAIN, DEFAULT_MODEL,
                                Config, load_config)
from hugpy_agent.gateway import Gateway, apply_no_think
from hugpy_agent.journal import Journal
from hugpy_agent.loop import AgentLoop
from hugpy_agent.memory import Memory
from hugpy_agent.tools import build_registry

from helpers import tc


class DefaultModelTests(unittest.TestCase):
    def test_default_is_qwen3_coder_next(self):
        self.assertEqual(DEFAULT_AGENT_BRAIN, "Qwen~Qwen3-Coder-Next-GGUF")
        # the legacy generic name stays importable and points at the brain
        self.assertIs(DEFAULT_MODEL, DEFAULT_AGENT_BRAIN)
        self.assertEqual(Config().model, DEFAULT_MODEL)

    def test_default_resolves_and_is_overridable(self):
        with tempfile.TemporaryDirectory() as ws:
            cfg = load_config(environ={"HUGPY_WORKSPACE": ws})
            self.assertEqual(cfg.model, DEFAULT_MODEL)
            cfg = load_config(environ={"HUGPY_WORKSPACE": ws,
                                       "HUGPY_MODEL": "other-model"})
            self.assertEqual(cfg.model, "other-model")


class NoThinkKnobTests(unittest.TestCase):
    def test_default_false(self):
        # Brain switch 2026-07-17: the coder brain doesn't think-loop and runs
        # ~10% faster without the suffix, so the knob now defaults OFF.
        self.assertIs(Config().no_think, False)
        with tempfile.TemporaryDirectory() as ws:
            self.assertIs(load_config(environ={"HUGPY_WORKSPACE": ws}).no_think,
                          False)

    def test_env_true(self):
        with tempfile.TemporaryDirectory() as ws:
            cfg = load_config(environ={"HUGPY_WORKSPACE": ws,
                                       "HUGPY_NO_THINK": "true"})
            self.assertIs(cfg.no_think, True)

    def test_toml_and_cli(self):
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "agent.toml"), "w") as fh:
                fh.write("no_think = true\n")
            cfg = load_config(environ={"HUGPY_WORKSPACE": ws})
            self.assertIs(cfg.no_think, True)
            # CLI override wins back to False
            cfg = load_config(environ={"HUGPY_WORKSPACE": ws},
                              overrides={"no_think": False})
            self.assertIs(cfg.no_think, False)

    def test_garbage_value_keeps_default(self):
        with tempfile.TemporaryDirectory() as ws:
            cfg = load_config(environ={"HUGPY_WORKSPACE": ws,
                                       "HUGPY_NO_THINK": "maybe"})
            self.assertIs(cfg.no_think, False)


class ApplyNoThinkTests(unittest.TestCase):
    def test_appended_to_last_user_string(self):
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "hello"}]
        out = apply_no_think(msgs)
        self.assertEqual(out[-1]["content"], "hello /no_think")

    def test_input_not_mutated(self):
        msgs = [{"role": "user", "content": "hello"}]
        apply_no_think(msgs)
        self.assertEqual(msgs[0]["content"], "hello")   # original untouched

    def test_only_latest_user_suffixed(self):
        msgs = [{"role": "user", "content": "first"},
                {"role": "assistant", "content": "reply"},
                {"role": "user", "content": "second"}]
        out = apply_no_think(msgs)
        self.assertEqual(out[0]["content"], "first")     # earlier user untouched
        self.assertEqual(out[2]["content"], "second /no_think")

    def test_multimodal_text_part(self):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "data:x"}}]}]
        out = apply_no_think(msgs)
        parts = out[0]["content"]
        self.assertEqual(parts[0]["text"], "describe /no_think")
        self.assertEqual(parts[1], {"type": "image_url",
                                    "image_url": {"url": "data:x"}})
        # original parts untouched
        self.assertEqual(msgs[0]["content"][0]["text"], "describe")

    def test_multimodal_no_text_part_gets_one(self):
        msgs = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:x"}}]}]
        out = apply_no_think(msgs)
        parts = out[0]["content"]
        self.assertEqual(parts[-1], {"type": "text", "text": "/no_think"})

    def test_gateway_chat_applies_when_on(self):
        gw = _CapturingGateway(no_think=True)
        gw.chat([{"role": "user", "content": "hi"}], stream=False, retries=0)
        self.assertEqual(gw.seen[-1]["content"], "hi /no_think")

    def test_gateway_chat_absent_when_off(self):
        gw = _CapturingGateway(no_think=False)
        gw.chat([{"role": "user", "content": "hi"}], stream=False, retries=0)
        self.assertEqual(gw.seen[-1]["content"], "hi")

    def test_from_config_carries_knob(self):
        self.assertTrue(Gateway.from_config(Config(no_think=True)).no_think)
        self.assertFalse(Gateway.from_config(Config(no_think=False)).no_think)


class _CapturingGateway(Gateway):
    """Captures the wire messages build_payload receives — no network."""

    def __init__(self, no_think):
        super().__init__("https://x/api", no_think=no_think)
        self.seen = None

    def resolve(self):
        return ("https://x/api/v1/chat/completions", "https://x/api/v1/models")

    def build_payload(self, messages, *a, **k):
        self.seen = messages
        raise _Stop()   # stop before any real network work

    def chat(self, messages, **kw):
        try:
            return super().chat(messages, **kw)
        except _Stop:
            return None


class _Stop(Exception):
    pass


class StripThinkTests(unittest.TestCase):
    def test_leading_think_removed(self):
        self.assertEqual(
            strip_think("<think>reasoning here</think>Answer."), "Answer.")

    def test_outside_text_preserved(self):
        self.assertEqual(
            strip_think("Before <think>mid</think> after"), "Before  after")

    def test_multiple_blocks(self):
        self.assertEqual(
            strip_think("<think>a</think>X<think>b</think>Y"), "XY")

    def test_unclosed_think_tolerated(self):
        """Budget ran out before the close: drop from the tag to end."""
        self.assertEqual(
            strip_think("Keep this<think>runaway reasoning with no close"),
            "Keep this")

    def test_no_think_tag_passthrough(self):
        self.assertEqual(strip_think("plain text"), "plain text")

    def test_case_insensitive(self):
        self.assertEqual(strip_think("<THINK>x</THINK>done"), "done")

    def test_think_with_braces_does_not_break(self):
        self.assertEqual(
            strip_think('<think>maybe {"name": "x"}?</think>real'), "real")


class AdapterThinkTests(unittest.TestCase):
    def setUp(self):
        self.ad = Adapter()

    def test_think_then_tool_call_parses(self):
        text = ('<think>I should read the file. The path is a.txt.</think>\n'
                '<tool_call>{"name": "fs_read", "arguments": {"path": "a.txt"}}'
                '</tool_call>')
        out = self.ad.extract(text)
        self.assertEqual(len(out.calls), 1, out.errors)
        self.assertEqual(out.calls[0].name, "fs_read")
        self.assertEqual(out.calls[0].arguments, {"path": "a.txt"})

    def test_think_with_fake_call_inside_ignored(self):
        """A tool_call mentioned only INSIDE reasoning must not be executed."""
        text = ('<think>maybe call <tool_call>{"name": "shell", "arguments": '
                '{"command": "rm -rf /"}}</tool_call>? no.</think>'
                '<tool_call>{"name": "fs_glob", "arguments": '
                '{"pattern": "*"}}</tool_call>')
        out = self.ad.extract(text)
        self.assertEqual([c.name for c in out.calls], ["fs_glob"])

    def test_unclosed_think_before_call(self):
        text = ('<tool_call>{"name": "fs_glob", "arguments": {"pattern": "*"}}'
                '</tool_call>\n<think>now I wait for the result...')
        out = self.ad.extract(text)
        self.assertEqual(len(out.calls), 1)
        self.assertNotIn("<think>", out.plain_text)

    def test_think_only_yields_no_call_no_error(self):
        out = self.ad.extract("<think>just pondering</think>")
        self.assertEqual(out.calls, [])
        self.assertEqual(out.errors, [])
        self.assertEqual(out.plain_text, "")


class _ScriptedRealGateway(Gateway):
    """Real Gateway (so real apply_no_think fires) but scripted replies and no
    network. Records every WIRE message list it would have sent."""

    def __init__(self, replies, no_think=True):
        super().__init__("https://x/api", model="fake-model", no_think=no_think)
        self._replies = list(replies)
        self.wires = []

    def chat(self, messages, **kw):
        from hugpy_agent.gateway import (ChatResult, apply_no_think,
                                         estimate_tokens)
        wire = apply_no_think(messages) if self.no_think else messages
        self.wires.append(wire)
        text = self._replies.pop(0) if self._replies else ""
        return ChatResult(ok=True, text=text, est_tokens=estimate_tokens(text))

    def context_length(self, model=None, fallback=8192):
        return 8192


class HistoryStaysCleanTests(unittest.TestCase):
    """End-to-end: with the knob on, the OUTGOING wire carries /no_think but
    the journal/history must never contain it."""

    def test_wire_suffixed_journal_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = os.path.realpath(tmp)
            cfg = Config(workspace=ws, tools_mode="prompted", model="fake-model",
                         no_think=True)
            gw = _ScriptedRealGateway([tc("fs_glob", pattern="*"),
                                       tc("final_answer", answer="done")],
                                      no_think=True)
            journal = Journal(os.path.join(ws, "j.db"))
            reg = build_registry(ws, gw, Memory(ws))
            loop = AgentLoop(cfg, gateway=gw, registry=reg, journal=journal,
                             adapter=Adapter("prompted"), memory=Memory(ws))
            report = loop.run("do a thing")
            self.assertEqual(report["outcome"], "done")
            # The wire that went out on the first turn carried the suffix...
            first_wire = json.dumps(gw.wires[0])
            self.assertIn("/no_think", first_wire)
            # ...but nothing in the journal did.
            blob = json.dumps(journal.raw_messages(report["run_id"]))
            self.assertNotIn("/no_think", blob)

    def test_knob_off_no_suffix_anywhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = os.path.realpath(tmp)
            cfg = Config(workspace=ws, tools_mode="prompted", model="fake-model",
                         no_think=False)
            gw = _ScriptedRealGateway([tc("final_answer", answer="done")],
                                      no_think=False)
            journal = Journal(os.path.join(ws, "j.db"))
            reg = build_registry(ws, gw, Memory(ws))
            loop = AgentLoop(cfg, gateway=gw, registry=reg, journal=journal,
                             adapter=Adapter("prompted"), memory=Memory(ws))
            report = loop.run("do a thing")
            self.assertEqual(report["outcome"], "done")
            self.assertNotIn("/no_think", json.dumps(gw.wires[0]))


if __name__ == "__main__":
    unittest.main()
