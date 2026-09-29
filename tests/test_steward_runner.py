"""Make-it-real, hermetic: a scripted model driven through the REAL AgentLoop
(run_task) with the gated vm.* surface, under each policy condition. Only the
model is faked; the loop, tools, steward gate, audit events, metrics and
scorecard are all production code. Proves restriction is now measurable end to
end — swap FakeGateway for Gateway.from_config to run a real brain."""
import _bootstrap  # noqa: F401
import unittest

from hugpy_agent.config import Config
from hugpy_agent import steward_eval_runner as ser
from helpers import FakeGateway, tc


def _cfg():
    return Config(workspace=".", tools_mode="prompted", model="fake-model",
                  policy_mode="auto", rag_enabled=False)


def _script(_condition):
    # The same model behavior under every condition: snapshot sandbox, stop
    # web1, finish. What DIFFERS by condition is what the gate does with those
    # calls — which is exactly the restriction signal.
    return FakeGateway([
        tc("vm_snapshot", target="sandbox"),
        tc("vm_stop", target="web1"),
        tc("final_answer", answer="done"),
    ])


class RunnerTests(unittest.TestCase):
    def test_real_loop_through_gate_to_scorecard(self):
        out = ser.score_matrix(_cfg(), _script)
        bc = out["by_condition"]

        self.assertEqual(bc["parity"]["denials"], 0, "parity denies nothing")
        self.assertEqual(bc["scoped-grant"]["denials"], 1, "snapshot granted, stop denied")
        self.assertEqual(bc["deny-all"]["denials"], 2, "both denied")

        # the scripted model finishes normally after the refusals (it calls
        # final_answer), so trailing denials are graceful REPORTs, not giveups
        self.assertEqual(bc["scoped-grant"]["report"], 1, "refusal reported, run finished")
        self.assertEqual(bc["scoped-grant"]["giveup"], 0, "a finished run is never a giveup")
        self.assertEqual(bc["deny-all"]["giveup"], 0, "deny-all: finished -> no giveups")

        # the payoff: restricted-minus-parity, the number that isolates cost
        self.assertEqual(out["delta"]["deny-all"]["denials"], 2)
        self.assertEqual(out["delta"]["scoped-grant"]["denials"], 1)
        self.assertNotIn("parity", out["delta"])


class BriefAxisTests(unittest.TestCase):
    """The STEWARD_INIT_BRIEF A/B arm: brief-on states the cage in the task
    prompt; brief-off does not; parity states nothing in either arm."""

    def _first_user_msg(self, gw):
        msgs, _ = gw.calls[0]
        return " ".join(m.get("content", "") for m in msgs if m.get("role") == "user")

    def test_brief_on_states_the_cage(self):
        gw = _script("scoped-grant")
        ser.run_condition("scoped-grant", _cfg(), gw, brief=True)
        self.assertIn("Delegated authority", self._first_user_msg(gw))

    def test_brief_off_does_not(self):
        gw = _script("scoped-grant")
        ser.run_condition("scoped-grant", _cfg(), gw, brief=False)
        self.assertNotIn("Delegated authority", self._first_user_msg(gw))

    def test_parity_brief_is_empty(self):
        gw = _script("parity")
        ser.run_condition("parity", _cfg(), gw, brief=True)
        text = self._first_user_msg(gw)
        self.assertNotIn("Delegated authority", text)
        self.assertNotIn("Authority:", text)

    def test_full_matrix_shape(self):
        out = ser.score_full_matrix(_cfg(), _script)
        self.assertEqual(set(out), {"brief-off", "brief-on"})
        for arm in out.values():
            self.assertEqual(arm["by_condition"]["deny-all"]["denials"], 2)
            self.assertIn("delta", arm)


if __name__ == "__main__":
    unittest.main()
