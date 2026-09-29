"""Eval harness (P3.4), offline: the fleet is a scripted FakeGateway, so no
test touches the network. Covers the deterministic checkers, the token-echo
readiness gate (including the false-200 loading decoy), per-task scoring
against the real loop, scorecard aggregation (incl. tool-accuracy), rendering,
and the CLI wiring."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import json
import os
import tempfile
import unittest

from hugpy_agent.config import Config
from hugpy_agent.gateway import ChatResult
from hugpy_agent import eval as evalmod
from hugpy_agent.eval import (
    CheckContext, DEFAULT_TASKS, EvalTask, Scorecard, TaskResult,
    answer_contains, file_contains, format_table, model_ready, run_task,
    score_model, task_by_name, READY_TOKEN,
)

from helpers import FakeGateway, tc


def _cfg(ws="."):
    # policy auto so eval write-tasks run; model overridden per test.
    return Config(workspace=ws, tools_mode="prompted", model="fake-model",
                  policy_mode="auto", rag_enabled=False)


class CheckerTests(unittest.TestCase):
    def test_file_contains_case_insensitive(self):
        ws = tempfile.mkdtemp()
        with open(os.path.join(ws, "answer.txt"), "w") as fh:
            fh.write("blackBIRD\n")
        ctx = CheckContext(workspace=ws, report={})
        self.assertTrue(file_contains("answer.txt", "BLACKBIRD")(ctx))
        self.assertFalse(file_contains("answer.txt", "eagle")(ctx))
        self.assertFalse(file_contains("missing.txt", "x")(ctx))

    def test_answer_contains_all_needles(self):
        ctx = CheckContext(workspace=".", report={}, answer="the code is 4471 ok")
        self.assertTrue(answer_contains("4471")(ctx))
        self.assertTrue(answer_contains("code", "4471")(ctx))
        self.assertFalse(answer_contains("9999")(ctx))

    def test_task_by_name(self):
        self.assertIsNotNone(task_by_name("write_artifact"))
        self.assertIsNone(task_by_name("nope"))


class ReadinessGateTests(unittest.TestCase):
    """The core doctrine: gate on a token echo, never on a 200 / serving flag."""

    def test_ready_when_token_echoed(self):
        gw = FakeGateway([READY_TOKEN + "\n"])
        ready, detail = model_ready(gw, tries=3, poll_interval=0, sleep=lambda s: None)
        self.assertTrue(ready)
        self.assertIn("ready", detail)

    def test_false_200_loading_body_is_not_ready(self):
        """A 200 whose body is a loading/error string must NOT pass — the
        exact trap the doctrine warns about."""
        decoy = ChatResult(ok=True, text="[error: worker 404 NOT FOUND, loading]")
        gw = FakeGateway([decoy, decoy])
        ready, detail = model_ready(gw, tries=2, poll_interval=0, sleep=lambda s: None)
        self.assertFalse(ready)
        self.assertIn("loading", detail.lower())

    def test_warmup_then_ready(self):
        """Polls politely through a cold load, then succeeds."""
        gw = FakeGateway([
            ChatResult(ok=True, text="[error: NOT FOUND]"),   # still loading
            ChatResult(ok=False, error="connect failed"),      # transient
            READY_TOKEN,                                        # up
        ])
        slept = []
        ready, _ = model_ready(gw, tries=5, poll_interval=7,
                               sleep=lambda s: slept.append(s))
        self.assertTrue(ready)
        self.assertEqual(slept, [7, 7])   # slept between the 3 attempts, not after

    def test_never_ready_reports_blocker(self):
        gw = FakeGateway([ChatResult(ok=False, error="down")] * 2)
        ready, detail = model_ready(gw, tries=2, poll_interval=0, sleep=lambda s: None)
        self.assertFalse(ready)
        self.assertIn("not ready after 2", detail)


class RunTaskTests(unittest.TestCase):
    def test_write_artifact_task_passes(self):
        gw = FakeGateway([
            tc("fs_write", path="answer.txt", content="BLACKBIRD"),
            tc("final_answer", answer="wrote answer.txt"),
        ])
        task = task_by_name("write_artifact")
        res = run_task(task, _cfg(), gateway=gw)
        self.assertTrue(res.passed)
        self.assertEqual(res.outcome, "done")
        self.assertEqual(res.tool_calls, 1)
        self.assertEqual(res.tool_ok, 1)
        self.assertEqual(res.tool_accuracy, 1.0)

    def test_wrong_artifact_fails_check(self):
        gw = FakeGateway([
            tc("fs_write", path="answer.txt", content="EAGLE"),
            tc("final_answer", answer="done"),
        ])
        res = run_task(task_by_name("write_artifact"), _cfg(), gateway=gw)
        self.assertFalse(res.passed)          # artifact present but wrong
        self.assertEqual(res.outcome, "done")

    def test_read_fact_task_passes(self):
        gw = FakeGateway([
            tc("fs_read", path="config.txt"),
            tc("final_answer", answer="the launch code is 4471"),
        ])
        res = run_task(task_by_name("read_fact"), _cfg(), gateway=gw)
        self.assertTrue(res.passed)

    def test_over_step_cap_fails_even_if_output_ok(self):
        """A task that only finishes by blowing the step cap must not pass."""
        task = EvalTask(name="tiny", prompt="finish",
                        check=answer_contains("x"), step_cap=1)
        gw = FakeGateway([
            tc("fs_glob", pattern="*"),                 # step 1 (cap reached)
            tc("final_answer", answer="x"),             # never reached
        ])
        res = run_task(task, _cfg(), gateway=gw)
        self.assertEqual(res.outcome, "max_steps")
        self.assertFalse(res.passed)

    def test_tool_error_lowers_accuracy(self):
        """A failed tool call (bad path) counts against tool-accuracy but the
        task can still pass if it recovers."""
        gw = FakeGateway([
            tc("fs_read", path="does-not-exist.txt"),   # error result
            tc("fs_read", path="config.txt"),            # ok
            tc("final_answer", answer="launch code 4471"),
        ])
        res = run_task(task_by_name("read_fact"), _cfg(), gateway=gw)
        self.assertTrue(res.passed)
        self.assertEqual(res.tool_calls, 2)
        self.assertEqual(res.tool_ok, 1)
        self.assertAlmostEqual(res.tool_accuracy, 0.5)

    def test_checker_bug_is_a_failed_task_not_a_crash(self):
        def boom(ctx):
            raise RuntimeError("checker bug")
        task = EvalTask(name="b", prompt="finish", check=boom, step_cap=2)
        gw = FakeGateway([tc("final_answer", answer="ok")])
        res = run_task(task, _cfg(), gateway=gw)
        self.assertFalse(res.passed)
        self.assertEqual(res.outcome, "done")


class ScoreModelTests(unittest.TestCase):
    def test_blocked_model_gets_a_row_no_tasks(self):
        def factory(model):
            return FakeGateway([ChatResult(ok=True, text="[error: NOT FOUND]")] * 3)
        card = score_model("m-down", _cfg(), [task_by_name("write_artifact")],
                           gateway_factory=factory, ready_tries=3, ready_poll=0,
                           sleep=lambda s: None)
        self.assertFalse(card.ready)
        self.assertEqual(card.total, 0)
        self.assertEqual(card.passed, 0)

    def test_ready_model_scored_across_suite(self):
        # A fresh scripted gateway per call: readiness ping, worker probe, then
        # each task's replies in order. gateway_factory is invoked once for the
        # readiness gate, once for the worker probe, then once per task.
        scripts = iter([
            [READY_TOKEN],                                   # readiness gate
            [],                                              # worker probe (api_json)
            [tc("fs_write", path="answer.txt", content="BLACKBIRD"),
             tc("final_answer", answer="wrote answer.txt")],
            [tc("fs_read", path="config.txt"),
             tc("final_answer", answer="code 4471")],
        ])

        def factory(model):
            return FakeGateway(next(scripts))

        card = score_model("m-up", _cfg(),
                           [task_by_name("write_artifact"),
                            task_by_name("read_fact")],
                           gateway_factory=factory, ready_tries=2, ready_poll=0,
                           sleep=lambda s: None)
        self.assertTrue(card.ready)
        self.assertEqual(card.passed, 2)
        self.assertEqual(card.total, 2)
        self.assertEqual(card.tool_accuracy, 1.0)
        d = card.to_dict()
        for k in ("model", "ready", "passed", "steps_avg", "tokens_avg",
                  "wall_avg", "tool_accuracy"):
            self.assertIn(k, d)


class ScorecardAggregationTests(unittest.TestCase):
    def _tr(self, passed, steps, tokens, calls, ok):
        return TaskResult(name="t", passed=passed, outcome="done", steps=steps,
                          est_tokens=tokens, wall_s=1.0, tool_calls=calls,
                          tool_ok=ok)

    def test_aggregate_tool_accuracy_over_all_calls(self):
        card = Scorecard(model="m", ready=True, tasks=[
            self._tr(True, 2, 100, 3, 3),      # 3/3
            self._tr(False, 4, 200, 1, 0),     # 0/1
        ])
        self.assertEqual(card.passed, 1)
        self.assertEqual(card.total, 2)
        self.assertEqual(card.steps_avg, 3.0)
        self.assertEqual(card.tokens_avg, 150.0)
        self.assertAlmostEqual(card.tool_accuracy, 3 / 4)   # 3 ok of 4 calls

    def test_no_calls_is_perfect_accuracy(self):
        card = Scorecard(model="m", ready=True,
                         tasks=[self._tr(True, 1, 10, 0, 0)])
        self.assertEqual(card.tool_accuracy, 1.0)


class RenderTests(unittest.TestCase):
    def test_table_has_a_row_per_model(self):
        cards = [Scorecard(model="alpha", ready=True,
                           tasks=[TaskResult("t", True, "done", 2, 100, 1.0, 1, 1)]),
                 Scorecard(model="beta", ready=False, ready_detail="blocked")]
        table = format_table(cards)
        self.assertIn("alpha", table)
        self.assertIn("beta", table)
        self.assertIn("NO", table)   # beta not ready

    def test_write_results_emits_json_and_table(self):
        cards = [Scorecard(model="alpha", ready=True,
                           tasks=[TaskResult("t", True, "done", 2, 100, 1.0, 1, 1)])]
        out = tempfile.mkdtemp()
        json_path, table_path = evalmod.write_results(cards, out)
        self.assertTrue(os.path.exists(json_path))
        self.assertTrue(os.path.exists(table_path))
        with open(json_path) as fh:
            data = json.load(fh)
        self.assertEqual(data["cards"][0]["model"], "alpha")
        self.assertEqual(data["cards"][0]["passed"], 1)


class CliWiringTests(unittest.TestCase):
    def test_eval_requires_a_model(self):
        import contextlib
        import io
        from hugpy_agent.cli import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["eval"])
        self.assertEqual(rc, 2)
        self.assertIn("--model", buf.getvalue())

    def test_default_suite_is_nonempty_and_named(self):
        names = {t.name for t in DEFAULT_TASKS}
        self.assertIn("write_artifact", names)
        self.assertIn("read_transform_write", names)
        for t in DEFAULT_TASKS:
            self.assertTrue(callable(t.check))
            self.assertGreaterEqual(t.step_cap, 1)


if __name__ == "__main__":
    unittest.main()
