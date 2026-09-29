"""Second-in-line brain (HUGPY_AGENT_BRAIN_2): run-start selection against
mocked /llm/workers payloads and the one-shot reactive capacity fallback.
All offline — the workers probe is faked at the gateway seam."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import os
import tempfile
import unittest

from hugpy_agent.adapter import Adapter
from hugpy_agent.config import Config, load_config
from hugpy_agent.gateway import (ChatResult, brain_matches_key,
                                 is_capacity_error, pick_resident_brain,
                                 slot_model_keys)
from hugpy_agent.journal import Journal
from hugpy_agent.loop import AgentLoop
from hugpy_agent.memory import Memory
from hugpy_agent.tools import build_registry

from helpers import FakeGateway, tc

PRIMARY = "Qwen~Qwen3-Coder-Next-GGUF"
SECONDARY = "Qwen~Qwen3-4B-Instruct-GGUF"

CAPACITY_ERR = ("HTTP 507 from fake:///v1/chat/completions: "
                '{"error": "loadrefusal: model won\'t fit on any worker"}')


def workers_payload(*slot_keys, ram_keys=()):
    """One worker row shaped like the live /llm/workers response: allocations
    carry model_key + kind, 'slot' = actively seated, 'ram' = cached only."""
    return [{"id": "w1", "name": "worker-1", "allocations":
             [{"model_key": k, "kind": "slot"} for k in slot_keys]
             + [{"model_key": k, "kind": "ram"} for k in ram_keys]}]


class BrainFakeGateway(FakeGateway):
    """FakeGateway plus the workers probe: `seated` is the probe's return
    value (a list of model_keys, or None = probe failed). The loop's run-start
    probe is warm_models (k96 ladder); seated_model_keys is kept for the
    legacy second-in-line surface — both count against probe_calls."""

    def __init__(self, replies=None, seated=None, **kw):
        super().__init__(replies, **kw)
        self.seated = seated
        self.probe_calls = 0

    def seated_model_keys(self, timeout=None):
        self.probe_calls += 1
        return self.seated

    def warm_models(self, timeout=None):
        self.probe_calls += 1
        return self.seated


class ConfigKnobTests(unittest.TestCase):
    def test_default_off(self):
        with tempfile.TemporaryDirectory() as ws:
            cfg = load_config(environ={"HUGPY_WORKSPACE": ws})
            self.assertEqual(cfg.model_2, "")

    def test_env_sets_model_2(self):
        with tempfile.TemporaryDirectory() as ws:
            cfg = load_config(environ={"HUGPY_WORKSPACE": ws,
                                       "HUGPY_AGENT_BRAIN_2": SECONDARY})
            self.assertEqual(cfg.model_2, SECONDARY)

    def test_toml_sets_model_2(self):
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "agent.toml"), "w") as fh:
                fh.write('model_2 = "toml-standby"\n')
            cfg = load_config(environ={"HUGPY_WORKSPACE": ws})
            self.assertEqual(cfg.model_2, "toml-standby")


class MatchingTests(unittest.TestCase):
    def test_exact(self):
        self.assertTrue(brain_matches_key(PRIMARY, PRIMARY))

    def test_bare_tail_both_directions(self):
        self.assertTrue(brain_matches_key(PRIMARY, "Qwen3-Coder-Next-GGUF"))
        self.assertTrue(brain_matches_key("Qwen3-Coder-Next-GGUF", PRIMARY))

    def test_no_match(self):
        self.assertFalse(brain_matches_key(PRIMARY, SECONDARY))
        self.assertFalse(brain_matches_key("", PRIMARY))
        self.assertFalse(brain_matches_key(PRIMARY, ""))


class WorkersPayloadTests(unittest.TestCase):
    def test_slot_keys_extracted(self):
        keys = slot_model_keys(workers_payload(PRIMARY, ram_keys=(SECONDARY,)))
        self.assertEqual(keys, [PRIMARY])   # 'ram' is cached, not seated

    def test_wrapper_dict_form(self):
        keys = slot_model_keys({"workers": workers_payload(SECONDARY)})
        self.assertEqual(keys, [SECONDARY])

    def test_garbage_payload_is_none(self):
        self.assertIsNone(slot_model_keys({"error": "nope"}))
        self.assertIsNone(slot_model_keys("html"))
        self.assertIsNone(slot_model_keys(None))

    def test_malformed_rows_skipped(self):
        keys = slot_model_keys(["x", {"allocations": ["y", {"kind": "slot"}]},
                                {"allocations":
                                 [{"model_key": PRIMARY, "kind": "slot"}]}])
        self.assertEqual(keys, [PRIMARY])


class SelectionTests(unittest.TestCase):
    """pick_resident_brain: the pure run-start decision table."""

    def test_primary_resident_wins(self):
        model, _ = pick_resident_brain([PRIMARY, SECONDARY], PRIMARY, SECONDARY)
        self.assertEqual(model, PRIMARY)

    def test_only_secondary_resident(self):
        model, why = pick_resident_brain([SECONDARY], PRIMARY, SECONDARY)
        self.assertEqual(model, SECONDARY)
        self.assertIn("not seated", why)

    def test_neither_resident_defaults_primary(self):
        model, _ = pick_resident_brain(["other-model"], PRIMARY, SECONDARY)
        self.assertEqual(model, PRIMARY)

    def test_probe_failure_defaults_primary(self):
        model, _ = pick_resident_brain(None, PRIMARY, SECONDARY)
        self.assertEqual(model, PRIMARY)

    def test_bare_tail_counts_as_resident(self):
        model, _ = pick_resident_brain(["Qwen3-4B-Instruct-GGUF"],
                                       PRIMARY, SECONDARY)
        self.assertEqual(model, SECONDARY)

    def test_no_secondary_configured(self):
        model, _ = pick_resident_brain([SECONDARY], PRIMARY, "")
        self.assertEqual(model, PRIMARY)
        model, _ = pick_resident_brain([], PRIMARY, PRIMARY)
        self.assertEqual(model, PRIMARY)


class CapacityErrorTests(unittest.TestCase):
    def test_markers(self):
        for s in ("model won't fit", "model wont fit", "CUDA out of memory",
                  "LoadRefusal: budget", "budgetrefusal", "model won’t fit"):
            self.assertTrue(is_capacity_error(s), s)

    def test_non_capacity(self):
        for s in (None, "", "connect to x failed: timeout",
                  "HTTP 503 from x: upstream down"):
            self.assertFalse(is_capacity_error(s), repr(s))


class LoopHarness(unittest.TestCase):
    """Loop-level scaffolding mirroring test_loop, with the probe faked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
        self.cfg = Config(workspace=self.ws, tools_mode="prompted",
                          max_steps=10, model=PRIMARY, model_2=SECONDARY,
                          policy_mode="auto")
        self.db = os.path.join(self.ws, ".hugpy_agent", "journal.db")
        self.events = []

    def tearDown(self):
        self.tmp.cleanup()

    def make_loop(self, replies, seated=None):
        gw = BrainFakeGateway(replies, seated=seated)
        journal = Journal(self.db)
        reg = build_registry(self.ws, gw, Memory(self.ws))
        loop = AgentLoop(self.cfg, gateway=gw, registry=reg, journal=journal,
                         adapter=Adapter("prompted"), memory=Memory(self.ws),
                         on_event=lambda kind, *a: self.events.append((kind, a)))
        return loop, gw, journal

    def chat_models(self, gw):
        return [kw.get("model") for _, kw in gw.calls]


class RunStartSelectionTests(LoopHarness):
    def test_primary_resident_runs_primary(self):
        loop, gw, journal = self.make_loop(
            [tc("final_answer", answer="ok")],
            seated=[PRIMARY, SECONDARY])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.chat_models(gw), [PRIMARY])
        self.assertEqual(journal.get_run(report["run_id"])["model"], PRIMARY)
        self.assertEqual(report["model"], PRIMARY)

    def test_only_secondary_resident_runs_secondary(self):
        loop, gw, journal = self.make_loop(
            [tc("final_answer", answer="ok")], seated=[SECONDARY])
        report = loop.run("t")
        self.assertEqual(self.chat_models(gw), [SECONDARY])
        # the choice is journaled and logged with the why:
        self.assertEqual(journal.get_run(report["run_id"])["model"], SECONDARY)
        brains = [a for kind, a in self.events if kind == "brain"]
        self.assertEqual(len(brains), 1)
        self.assertEqual(brains[0][0], SECONDARY)
        self.assertIn("not seated", brains[0][1])

    def test_neither_resident_runs_primary(self):
        loop, gw, _ = self.make_loop(
            [tc("final_answer", answer="ok")], seated=["some-other-model"])
        loop.run("t")
        self.assertEqual(self.chat_models(gw), [PRIMARY])

    def test_probe_error_silent_primary(self):
        loop, gw, _ = self.make_loop(
            [tc("final_answer", answer="ok")], seated=None)  # probe failed
        loop.run("t")
        self.assertEqual(self.chat_models(gw), [PRIMARY])
        self.assertEqual(gw.probe_calls, 1)
        self.assertEqual([k for k, _ in self.events if k == "brain"], [])

    def test_no_secondary_means_no_probe(self):
        self.cfg.model_2 = ""
        loop, gw, _ = self.make_loop(
            [tc("final_answer", answer="ok")], seated=[SECONDARY])
        loop.run("t")
        self.assertEqual(gw.probe_calls, 0)   # feature fully off
        self.assertEqual(self.chat_models(gw), [PRIMARY])

    def test_selection_once_per_run_not_per_step(self):
        loop, gw, _ = self.make_loop(
            [tc("fs_glob", pattern="*"), tc("fs_glob", pattern="*.txt"),
             tc("final_answer", answer="ok")], seated=[PRIMARY])
        loop.run("t")
        self.assertEqual(gw.probe_calls, 1)
        self.assertEqual(self.chat_models(gw), [PRIMARY] * 3)


class ReactiveFallbackTests(LoopHarness):
    def test_capacity_error_switches_exactly_once_and_sticks(self):
        loop, gw, _ = self.make_loop(
            [ChatResult(ok=False, error=CAPACITY_ERR),   # primary refused
             tc("fs_glob", pattern="*"),                 # retried on secondary
             tc("final_answer", answer="ok")],
            seated=[PRIMARY])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.chat_models(gw),
                         [PRIMARY, SECONDARY, SECONDARY])  # sticks for the run
        warnings = [a for kind, a in self.events if kind == "brain_fallback"]
        self.assertEqual(len(warnings), 1)                 # the ONE warning
        self.assertEqual(warnings[0][0], SECONDARY)
        self.assertEqual(report["model"], SECONDARY)       # surfaced in report

    def test_non_capacity_error_never_switches(self):
        loop, gw, _ = self.make_loop(
            [ChatResult(ok=False, error="connect to x failed: timeout"),
             tc("final_answer", answer="ok")],
            seated=[PRIMARY])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.chat_models(gw), [PRIMARY, PRIMARY])
        self.assertEqual([k for k, _ in self.events
                          if k == "brain_fallback"], [])

    def test_capacity_on_secondary_never_ping_pongs(self):
        """After the one switch, further capacity errors ride the normal
        failure ladder on the SECONDARY — no bounce back to the primary."""
        loop, gw, _ = self.make_loop(
            [ChatResult(ok=False, error=CAPACITY_ERR),   # primary refused
             ChatResult(ok=False, error=CAPACITY_ERR),   # secondary refused too
             tc("final_answer", answer="ok")],
            seated=[PRIMARY])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.chat_models(gw),
                         [PRIMARY, SECONDARY, SECONDARY])
        self.assertEqual(len([k for k, _ in self.events
                              if k == "brain_fallback"]), 1)

    def test_no_secondary_no_fallback(self):
        self.cfg.model_2 = ""
        loop, gw, _ = self.make_loop(
            [ChatResult(ok=False, error=CAPACITY_ERR),
             tc("final_answer", answer="ok")])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")     # normal retry path
        self.assertEqual(self.chat_models(gw), [PRIMARY, PRIMARY])

    def test_started_on_secondary_no_fallback_to_itself(self):
        """Run-start already chose the secondary: a capacity error there is
        an ordinary failure (model_2 does not differ from the active brain)."""
        loop, gw, _ = self.make_loop(
            [ChatResult(ok=False, error=CAPACITY_ERR),
             tc("final_answer", answer="ok")],
            seated=[SECONDARY])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.chat_models(gw), [SECONDARY, SECONDARY])
        self.assertEqual([k for k, _ in self.events
                          if k == "brain_fallback"], [])


if __name__ == "__main__":
    unittest.main()
