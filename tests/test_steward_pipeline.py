"""End-to-end: the REAL vendored gate (GateSession -> steward + command_bus)
produces authentic audit events, which steward_eval turns into restriction
metrics and a within-model delta. This is the whole measurement pipeline minus
the live model — it fails loudly if the vendored gate's event shape ever drifts
from what the metrics expect."""
import _bootstrap  # noqa: F401
import unittest

from hugpy_agent import steward_gate as sg
from hugpy_agent import steward_eval as se


def drive(policy, intents):
    """Stand-in for a model until the eval.py driver is plugged in: run each
    intended (verb, target); on denial, retry once then move on. Deterministic,
    so the pipeline is exercised without GPU/serving."""
    s = sg.GateSession(policy)
    try:
        for verb, target in intents:
            if s.call(verb, target).get("denied"):
                s.call(verb, target)   # one retry — a mildly 'stuck' behavior
        return list(s.events)
    finally:
        s.close()


class PipelineTests(unittest.TestCase):
    def test_gate_discriminates_by_condition(self):
        s = sg.GateSession(sg.CONDITION_POLICIES["deny-all"])
        r = s.call("vm.stop", "sandbox"); s.close()
        self.assertTrue(r["denied"])
        self.assertIn("reach", r)          # authentic legible refusal
        self.assertIn("escalation", r)

        s = sg.GateSession(sg.CONDITION_POLICIES["parity"])
        self.assertTrue(s.call("vm.stop", "sandbox")["ok"]); s.close()

        s = sg.GateSession(sg.CONDITION_POLICIES["scoped-grant"])
        self.assertTrue(s.call("vm.start", "sandbox")["ok"])
        self.assertTrue(s.call("vm.stop", "sandbox")["denied"]); s.close()

    def test_real_gate_feeds_metrics_and_delta(self):
        intents = [("vm.list", "*"), ("vm.stop", "sandbox"), ("vm.snapshot", "web1")]
        by_cond = {c: se.run_metrics(drive(p, intents)).numeric()
                   for c, p in sg.CONDITION_POLICIES.items()}

        self.assertEqual(by_cond["parity"]["denials"], 0, "parity denies nothing")
        self.assertGreater(by_cond["deny-all"]["denials"],
                           by_cond["scoped-grant"]["denials"],
                           "tighter policy -> more denials")

        d = se.within_model_delta(by_cond)            # restricted - parity
        self.assertGreater(d["deny-all"]["denials"], 0, "deny-all shows positive restriction cost")
        self.assertNotIn("parity", d)


if __name__ == "__main__":
    unittest.main()
