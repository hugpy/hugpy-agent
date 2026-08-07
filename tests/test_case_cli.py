"""`hugpy-agent case` (k95): the sentinel's one-shot document-only run.

The profile must be pinned at the CLI-override layer (readonly + jailed
fs_write + http_fetch, mutation tools hard-denied) so that no workspace
.env/agent.toml or process environment can widen it.
"""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from hugpy_agent import cli
from hugpy_agent.policy import ALLOW, DENY, decide
from hugpy_agent.tools import (RISK_DESTRUCTIVE, RISK_NETWORK, RISK_WRITE,
                               ToolSpec)


def _spec(name, risk):
    return ToolSpec(name=name, description="t",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda: "ok", risk_class=risk)


class CaseProfileDecideTests(unittest.TestCase):
    """The pinned lists compose with decide() as the case contract needs."""

    def test_jailed_write_and_http_are_allowed(self):
        self.assertEqual(
            decide("readonly", _spec("fs_write", RISK_WRITE), {},
                   allow=cli.CASE_TOOL_ALLOW, deny=cli.CASE_TOOL_DENY), ALLOW)
        self.assertEqual(
            decide("readonly", _spec("http_fetch", RISK_NETWORK), {},
                   allow=cli.CASE_TOOL_ALLOW, deny=cli.CASE_TOOL_DENY), ALLOW)

    def test_shell_denied_even_if_environment_allows_it(self):
        # deny beats allow: an operator env carrying HUGPY_TOOL_ALLOW=shell
        # must not reopen mutation under a case run.
        self.assertEqual(
            decide("readonly", _spec("shell", RISK_DESTRUCTIVE), {},
                   allow=["shell"] + cli.CASE_TOOL_ALLOW,
                   deny=cli.CASE_TOOL_DENY), DENY)

    def test_remote_compute_stays_mode_denied(self):
        from hugpy_agent.tools import RISK_REMOTE_COMPUTE
        self.assertEqual(
            decide("readonly", _spec("summarize", RISK_REMOTE_COMPUTE), {},
                   allow=cli.CASE_TOOL_ALLOW, deny=cli.CASE_TOOL_DENY), DENY)


class CaseCliWiringTests(unittest.TestCase):
    """cmd_case pins the profile and jails the workspace to the case dir."""

    def _run(self, argv, report=None):
        captured = {}

        class StubLoop:
            def __init__(self, cfg, on_event=None):
                captured["cfg"] = cfg
                self.stop_requested = False

            def run(self, task):
                captured["task"] = task
                return report or {"outcome": "done", "answer": "ok"}

        with mock.patch.object(cli, "AgentLoop", StubLoop), \
                mock.patch.object(cli, "_install_sigint", lambda loop: None), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.main(argv)
        return rc, captured, out.getvalue()

    def test_profile_pinned_and_workspace_is_case_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = os.path.join(tmp, "case-001")
            os.makedirs(case_dir)
            # A hostile workspace .env must not widen the profile.
            with open(os.path.join(case_dir, ".env"), "w") as fh:
                fh.write("HUGPY_POLICY=auto\nHUGPY_TOOL_ALLOW=shell\n")
            brief = os.path.join(tmp, "brief.md")
            with open(brief, "w") as fh:
                fh.write("# CASE BRIEF\ndiagnose the thing")
            rc, captured, out = self._run(
                ["case", brief, "--case-dir", case_dir, "-q"])
            self.assertEqual(rc, 0)
            cfg = captured["cfg"]
            self.assertEqual(cfg.policy_mode, "readonly")
            self.assertEqual(cfg.tool_allow, cli.CASE_TOOL_ALLOW)
            self.assertEqual(cfg.tool_deny, cli.CASE_TOOL_DENY)
            self.assertEqual(cfg.workspace, os.path.realpath(case_dir))
            self.assertEqual(captured["task"],
                             "# CASE BRIEF\ndiagnose the thing")
            self.assertEqual(json.loads(out)["outcome"], "done")

    def test_non_done_outcome_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief = os.path.join(tmp, "brief.md")
            with open(brief, "w") as fh:
                fh.write("b")
            rc, _, _ = self._run(["case", brief, "--case-dir", tmp, "-q"],
                                 report={"outcome": "max_steps"})
            self.assertEqual(rc, 1)

    def test_empty_brief_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief = os.path.join(tmp, "brief.md")
            with open(brief, "w") as fh:
                fh.write("   \n")
            with mock.patch("sys.stderr", new=io.StringIO()):
                rc, _, _ = self._run(["case", brief, "--case-dir", tmp])
            self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
