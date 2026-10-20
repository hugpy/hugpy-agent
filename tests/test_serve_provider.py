"""`hugpy-agent serve` is the harness session server with the agent's loop and
gateway swapped in; the shared abstract-serve-core console is no longer opened."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import argparse
import json
import unittest
from unittest.mock import patch

from hugpy_agent import cli


def _args(**overrides):
    base = dict(profiles=None, state=None, host="127.0.0.1", port=None, console=False, console_port=None,
                sessions=False, daemon=False, no_browser=True, task_source=None, task_queue=None,
                agent_node=None, agent_central=None, max_cycles=None, workspace=None, base=None,
                model=None, policy=None, quiet=True, poll_interval=None)
    base.update(overrides)
    return argparse.Namespace(**base)


class ServeIsTheHarnessSessionServer(unittest.TestCase):
    def test_console_flag_is_retired_with_a_pointer_to_the_tui(self):
        with patch("sys.stderr") as err:
            self.assertEqual(cli.cmd_serve(_args(console=True)), 2)
        text = "".join(str(c.args[0]) for c in err.write.call_args_list)
        self.assertIn("hugpy-agent tui --serve", text)

    def test_default_serve_builds_the_harness_server_with_the_agent_client(self):
        from hugpy_agent.serve_client.wrapper import WrapperClient
        from hugpy_agent.runtime.loop import AgentLoop
        from hugpy_agent.gateway import Gateway
        built = {}

        class FakeServer:
            def __init__(self, client, host, port, token):
                built.update(client=client, host=host, port=port, token=token)
                self.url = "http://%s:%s" % (host, port)
                self.server = self

            def serve_forever(self):
                built["served"] = True

        with patch("hugpy_harness.serve.SessionServer", FakeServer), \
                patch("hugpy_harness.cli.chat.harness_endpoint", return_value=("http://worker:1234", "wrapper")), \
                patch("hugpy_harness.cli.chat.harness_key", return_value=""), \
                patch("hugpy_harness.gateway.Gateway.models", return_value=[]), \
                patch("hugpy_agent.cli._ensure_toolserver_async"), \
                patch("hugpy_agent.cli._session_server_is_live", return_value=False):
            self.assertEqual(cli.cmd_serve(_args(workspace=".", port=0)), 0)
        self.assertIsInstance(built["client"], WrapperClient)
        self.assertIs(built["client"].loop_class, AgentLoop)
        self.assertIs(built["client"].gateway_class, Gateway)
        self.assertEqual((built["host"], built["port"]), ("127.0.0.1", 0))
        self.assertTrue(built["served"])

    def test_profiles_flag_still_runs_the_provider_neutral_service(self):
        with patch("hugpy_agent.service.http.main", return_value=0) as legacy, \
                patch("hugpy_agent.cli._ensure_toolserver_async"), \
                patch("hugpy_agent.cli._session_server_is_live", return_value=False), \
                patch("pathlib.Path.is_file", return_value=True):
            self.assertEqual(cli.cmd_serve(_args(profiles="/tmp/profiles.json", workspace=".")), 0)
        argv = legacy.call_args.args[0]
        self.assertIn("--profiles", argv)
        self.assertEqual(argv[argv.index("--port") + 1], "9126")

    def test_a_live_service_is_joined_not_rebound(self):
        with patch("hugpy_agent.cli._ensure_toolserver_async"), \
                patch("hugpy_agent.cli._session_server_is_live", return_value=True), \
                patch("builtins.print") as out:
            self.assertEqual(cli.cmd_serve(_args()), 0)
        self.assertTrue(any("9126" in str(c.args[0]) for c in out.call_args_list))

    def test_discovery_recognises_both_brands(self):
        from hugpy_agent.tui import identify
        self.assertEqual(identify({"service": "hugpy-harness", "protocol_version": 1}), "hugpy")
        self.assertEqual(identify({"service": "hugpy-agent", "protocol_version": 1}), "hugpy")
        self.assertIsNone(identify({"service": "other", "protocol_version": 1}))


if __name__ == "__main__":
    unittest.main()
