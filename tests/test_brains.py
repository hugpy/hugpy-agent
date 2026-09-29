"""k96 brain ladder (HUGPY_AGENT_BRAINS): ladder parsing + back-compat,
warm-first run-start selection against mocked /llm/workers payloads, the
forward-only mid-run walk-down, the reduced-depth answer note, and the
no_makeroom key on every brain chat payload. All offline — the workers probe
is faked at the gateway seam (same harness as test_brain2)."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import json
import os
import tempfile
import unittest

from hugpy_agent.adapter import Adapter
from hugpy_agent.config import Config, load_config
from hugpy_agent.gateway import (ChatResult, Gateway, is_capacity_error,
                                 pick_ladder_brain, resolve_brain_ladder,
                                 warm_model_keys)
from hugpy_agent.journal import Journal
from hugpy_agent.loop import AgentLoop
from hugpy_agent.memory import Memory
from hugpy_agent.tools import build_registry

from helpers import FakeGateway, tc

BEST = "Qwen~Qwen3-Coder-Next-GGUF"
MID = "Qwen~Qwen3-4B-Instruct-GGUF"
PILOT = "Qwen~Qwen2.5-3B-Instruct-GGUF"
LADDER = [BEST, MID, PILOT]

CAPACITY_ERR = ("HTTP 507 from fake:///v1/chat/completions: "
                '{"error": "loadrefusal: model won\'t fit on any worker"}')
# The 2026-08-06 observed abort: central's load-verdict cache answering
# without a re-attempt. MUST walk the ladder (k96).
PERMANENT_ERR = ("HTTP 502 from fake:///v1/chat/completions: 'Qwen~Qwen3-"
                 "Coder-Next-GGUF' on 'ae' failed to load moments ago and "
                 "the failure is permanent (retrying cannot fix it): ...")
# The fleet's polite refusal wording (no_makeroom fail-fast) — also walks.
POLITE_ERR = ("HTTP 507 from fake:///v1/chat/completions: won't fit on GPU: "
              "model is not resident and free headroom is short — no_makeroom "
              "forbids evicting residents, refusing without evicting")


def workers_payload(*warm, unhealthy=(), ram=()):
    """One worker row shaped like the live /llm/workers response."""
    allocs = [{"model_key": k, "kind": "slot", "healthy": True} for k in warm]
    allocs += [{"model_key": k, "kind": "slot", "healthy": False}
               for k in unhealthy]
    allocs += [{"model_key": k, "kind": "ram"} for k in ram]
    return [{"id": "w1", "name": "worker-1", "allocations": allocs}]


class LadderFakeGateway(FakeGateway):
    def __init__(self, replies=None, warm=None, **kw):
        super().__init__(replies, **kw)
        self.warm = warm
        self.probe_calls = 0

    def warm_models(self, timeout=None):
        self.probe_calls += 1
        return self.warm


class ConfigTests(unittest.TestCase):
    def test_default_empty(self):
        with tempfile.TemporaryDirectory() as ws:
            cfg = load_config(environ={"HUGPY_WORKSPACE": ws})
            self.assertEqual(cfg.brains, [])

    def test_env_csv(self):
        with tempfile.TemporaryDirectory() as ws:
            cfg = load_config(environ={
                "HUGPY_WORKSPACE": ws,
                "HUGPY_AGENT_BRAINS": " %s , %s ,%s " % (BEST, MID, PILOT)})
            self.assertEqual(cfg.brains, LADDER)

    def test_toml_list(self):
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "agent.toml"), "w") as fh:
                fh.write('brains = "%s,%s"\n' % (BEST, PILOT))
            cfg = load_config(environ={"HUGPY_WORKSPACE": ws})
            self.assertEqual(cfg.brains, [BEST, PILOT])


class LadderResolutionTests(unittest.TestCase):
    def test_explicit_ladder_wins(self):
        cfg = Config(model="ignored", model_2="also-ignored", brains=LADDER)
        ladder, explicit = resolve_brain_ladder(cfg)
        self.assertEqual(ladder, LADDER)
        self.assertTrue(explicit)

    def test_backcompat_pair(self):
        cfg = Config(model=BEST, model_2=MID)
        ladder, explicit = resolve_brain_ladder(cfg)
        self.assertEqual(ladder, [BEST, MID])
        self.assertFalse(explicit)

    def test_backcompat_single(self):
        cfg = Config(model=BEST, model_2="")
        self.assertEqual(resolve_brain_ladder(cfg), ([BEST], False))

    def test_dedupe_keeps_best_position(self):
        cfg = Config(brains=[BEST, MID, BEST, "", PILOT, MID])
        ladder, _ = resolve_brain_ladder(cfg)
        self.assertEqual(ladder, LADDER)


class WarmParsingTests(unittest.TestCase):
    def test_healthy_slot_and_ram_are_warm(self):
        keys = warm_model_keys(workers_payload(BEST, ram=(PILOT,)))
        self.assertEqual(sorted(keys), sorted([BEST, PILOT]))

    def test_unhealthy_slot_is_not_warm(self):
        self.assertEqual(warm_model_keys(workers_payload(unhealthy=(BEST,))),
                         [])

    def test_healthy_absent_counts_warm(self):
        keys = warm_model_keys([{"allocations":
                                 [{"model_key": MID, "kind": "slot"}]}])
        self.assertEqual(keys, [MID])

    def test_garbage_is_none(self):
        self.assertIsNone(warm_model_keys({"error": "nope"}))
        self.assertIsNone(warm_model_keys("html"))
        self.assertIsNone(warm_model_keys(None))

    def test_malformed_rows_skipped(self):
        keys = warm_model_keys(["x", {"allocations": ["y", {"kind": "slot"}]},
                                workers_payload(BEST)[0]])
        self.assertEqual(keys, [BEST])


class SelectionTableTests(unittest.TestCase):
    def test_warm_mid_entry_beats_cold_first(self):
        model, pos, why = pick_ladder_brain([MID], LADDER, True)
        self.assertEqual((model, pos), (MID, 1))
        self.assertIn("warm", why)

    def test_first_warm_wins(self):
        model, pos, _ = pick_ladder_brain([MID, BEST], LADDER, True)
        self.assertEqual((model, pos), (BEST, 0))

    def test_none_warm_explicit_goes_pilot(self):
        model, pos, why = pick_ladder_brain([], LADDER, True)
        self.assertEqual((model, pos), (PILOT, 2))
        self.assertIn("pilot light", why)

    def test_none_warm_backcompat_stays_primary(self):
        model, pos, _ = pick_ladder_brain([], [BEST, MID], False)
        self.assertEqual((model, pos), (BEST, 0))

    def test_probe_failed_defaults_first(self):
        model, pos, _ = pick_ladder_brain(None, LADDER, True)
        self.assertEqual((model, pos), (BEST, 0))

    def test_bare_tail_matches(self):
        model, pos, _ = pick_ladder_brain(["Qwen2.5-3B-Instruct-GGUF"],
                                          LADDER, True)
        self.assertEqual((model, pos), (PILOT, 2))


class WalkdownMarkerTests(unittest.TestCase):
    def test_permanent_verdict_is_capacity_class(self):
        self.assertTrue(is_capacity_error(PERMANENT_ERR))

    def test_polite_refusal_is_capacity_class(self):
        self.assertTrue(is_capacity_error(POLITE_ERR))

    def test_transport_errors_still_are_not(self):
        for s in (None, "", "connect to x failed: timeout",
                  "HTTP 503 from x: upstream down"):
            self.assertFalse(is_capacity_error(s), repr(s))


class PayloadTests(unittest.TestCase):
    def test_no_makeroom_on_every_chat_payload(self):
        gw = Gateway("https://x/api", model="m")
        p = gw.build_payload([{"role": "user", "content": "hi"}])
        self.assertIs(p["no_makeroom"], True)
        # invariant holds alongside the older platform gotcha
        self.assertEqual(p["max_chunks"], 1)


class LoopHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
        self.cfg = Config(workspace=self.ws, tools_mode="prompted",
                          max_steps=10, model="unused-when-brains-set",
                          brains=list(LADDER), policy_mode="auto")
        self.db = os.path.join(self.ws, ".hugpy_agent", "journal.db")
        self.events = []

    def tearDown(self):
        self.tmp.cleanup()

    def make_loop(self, replies, warm=None):
        gw = LadderFakeGateway(replies, warm=warm)
        journal = Journal(self.db)
        reg = build_registry(self.ws, gw, Memory(self.ws))
        loop = AgentLoop(self.cfg, gateway=gw, registry=reg, journal=journal,
                         adapter=Adapter("prompted"), memory=Memory(self.ws),
                         on_event=lambda kind, *a: self.events.append((kind, a)))
        return loop, gw, journal

    def chat_models(self, gw):
        return [kw.get("model") for _, kw in gw.calls]


class RunStartTests(LoopHarness):
    def test_warm_mid_entry_selected_and_journaled(self):
        loop, gw, journal = self.make_loop(
            [tc("final_answer", answer="ok")], warm=[MID])
        report = loop.run("t")
        self.assertEqual(self.chat_models(gw), [MID])
        self.assertEqual(gw.probe_calls, 1)
        choice = json.loads(journal.kv_get("brain|%s" % report["run_id"]))
        self.assertEqual(choice["model"], MID)
        self.assertEqual(choice["position"], 1)
        self.assertEqual(choice["ladder"], LADDER)
        self.assertIn("warm", choice["reason"])

    def test_none_warm_starts_on_pilot_light(self):
        loop, gw, _ = self.make_loop(
            [tc("final_answer", answer="ok")], warm=["some-other-model"])
        report = loop.run("t")
        self.assertEqual(self.chat_models(gw), [PILOT])
        self.assertEqual(report["model"], PILOT)

    def test_probe_error_silent_first_entry(self):
        loop, gw, _ = self.make_loop(
            [tc("final_answer", answer="ok")], warm=None)
        loop.run("t")
        self.assertEqual(self.chat_models(gw), [BEST])

    def test_single_brain_no_probe(self):
        self.cfg.brains = [BEST]
        loop, gw, _ = self.make_loop([tc("final_answer", answer="ok")],
                                     warm=[BEST])
        loop.run("t")
        self.assertEqual(gw.probe_calls, 0)


class WalkdownTests(LoopHarness):
    def test_capacity_then_permanent_walks_two_rungs(self):
        loop, gw, _ = self.make_loop(
            [ChatResult(ok=False, error=CAPACITY_ERR),    # BEST refused
             ChatResult(ok=False, error=PERMANENT_ERR),   # MID verdict-cached
             tc("final_answer", answer="ok")],            # PILOT answers
            warm=[BEST])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.chat_models(gw), [BEST, MID, PILOT])
        falls = [a for kind, a in self.events if kind == "brain_fallback"]
        self.assertEqual([f[0] for f in falls], [MID, PILOT])
        self.assertEqual(report["model"], PILOT)

    def test_forward_only_and_bounded_on_last_rung(self):
        """On the pilot light a further capacity error rides the normal
        failure ladder — never back up, never loop."""
        loop, gw, _ = self.make_loop(
            [ChatResult(ok=False, error=CAPACITY_ERR)] * 5
            + [tc("final_answer", answer="ok")],
            warm=["nothing-warm"])   # starts on PILOT (last)
        report = loop.run("t")
        # 3 consecutive failures on the pilot light abort the run: no switch
        # was possible (already on the last rung), so no ping-pong either.
        self.assertEqual(report["outcome"], "aborted")
        self.assertEqual(set(self.chat_models(gw)), {PILOT})
        self.assertEqual([k for k, _ in self.events
                          if k == "brain_fallback"], [])

    def test_non_capacity_error_never_walks(self):
        loop, gw, _ = self.make_loop(
            [ChatResult(ok=False, error="connect to x failed: timeout"),
             tc("final_answer", answer="ok")], warm=[BEST])
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.chat_models(gw), [BEST, BEST])

    def test_reduced_depth_note_on_answer_and_report(self):
        loop, gw, _ = self.make_loop(
            [ChatResult(ok=False, error=CAPACITY_ERR),
             tc("final_answer", answer="the findings")], warm=[BEST])
        report = loop.run("t")
        self.assertEqual(report["model"], MID)
        self.assertEqual(report["brain_note"],
                         "answered by %s (ladder position 2 of 3)" % MID)
        self.assertTrue(report["answer"].startswith("the findings"))
        self.assertIn("[answered by %s (ladder position 2 of 3)]" % MID,
                      report["answer"])

    def test_no_note_on_first_rung(self):
        loop, gw, _ = self.make_loop(
            [tc("final_answer", answer="ok")], warm=[BEST])
        report = loop.run("t")
        self.assertNotIn("brain_note", report)
        self.assertEqual(report["answer"], "ok")


if __name__ == "__main__":
    unittest.main()
