"""The generated toolserver MCP entry is emitted ONLY when it will work, and
in the argv shape OpenCode 1.18 actually runs (`command` is the whole argv;
there is no `args` key — 0.1.79's split form launched a bare python that hung
until OpenCode's 30 s "Operation timed out"). Unusable -> no `mcp` block plus a
one-line launch notice. No live network: the probe is injected."""
import _bootstrap  # noqa: F401
import io
import json
import os
import subprocess
import sys
import unittest
import urllib.error
from unittest import mock

from hugpy_agent import console
from hugpy_agent.config import Config

TOKEN = "ts_secret_token_value_1234"
NO_FILES = mock.patch("hugpy_agent.tools.toolserver._env_file_values",
                      return_value=[])


def _cfg(**kw):
    return Config(base="https://dev.hugpy.ai/api", api_key="k", **kw)


def _ok(base, token):
    return None


class ToolserverMcpGate(unittest.TestCase):
    def test_no_token_omits_entry(self):
        with NO_FILES:
            entry, env, why = console.toolserver_mcp(_cfg(), environ={},
                                                     bridge_ok=True, probe=_ok)
        self.assertIsNone(entry)
        self.assertEqual(env, {})
        self.assertIn("no toolserver token", why)
        cfg = console.build_config("https://x/api", "K", {"m": {}}, "m",
                                   toolserver=entry)
        self.assertNotIn("mcp", cfg)

    def test_bridge_missing_omits_entry(self):
        with NO_FILES:
            entry, _, why = console.toolserver_mcp(
                _cfg(), environ={"TOOLSERVER_TOKEN": TOKEN}, bridge_ok=False,
                probe=_ok)
        self.assertIsNone(entry)
        self.assertIn("not installed", why)

    def test_opt_out(self):
        entry, _, why = console.toolserver_mcp(
            _cfg(toolserver=False), environ={"TOOLSERVER_TOKEN": TOKEN},
            bridge_ok=True, probe=_ok)
        self.assertIsNone(entry)
        self.assertIn("disabled", why)

    def test_rejected_token_fails_fast_with_reason(self):
        def probe(base, token):
            raise urllib.error.HTTPError(base, 401, "unauthorized", {}, None)
        with NO_FILES:
            entry, _, why = console.toolserver_mcp(
                _cfg(), environ={"TOOLSERVER_TOKEN": TOKEN}, bridge_ok=True,
                probe=probe)
        self.assertIsNone(entry)
        self.assertIn("HTTP 401", why)

    def test_unreachable(self):
        def probe(base, token):
            raise OSError("connection refused")
        with NO_FILES:
            entry, _, why = console.toolserver_mcp(
                _cfg(), environ={"TOOLSERVER_TOKEN": TOKEN}, bridge_ok=True,
                probe=probe)
        self.assertIsNone(entry)
        self.assertIn("unreachable", why)

    def test_usable_entry_shape_and_no_secret_on_disk(self):
        with NO_FILES:
            entry, env, why = console.toolserver_mcp(
                _cfg(toolserver_url="https://ts.example/"),
                environ={"TOOLSERVER_TOKEN": TOKEN}, bridge_ok=True, probe=_ok)
        self.assertEqual(entry["command"],
                         [sys.executable, "-m", "abstract_toolserver.mcp"])
        self.assertNotIn("args", entry)          # OpenCode ignores it
        self.assertTrue(entry["enabled"])
        self.assertIsInstance(entry["timeout"], int)
        self.assertEqual(env, {"TOOLSERVER_URL": "https://ts.example",
                               "TOOLSERVER_TOKEN": TOKEN})
        self.assertTrue(why.startswith("on (https://ts.example"))
        cfg = console.build_config("https://x/api", "K", {"m": {}}, "m",
                                   toolserver=entry)
        self.assertIs(cfg["mcp"]["toolserver"], entry)
        self.assertNotIn(TOKEN, json.dumps(cfg))  # {env:...} refs only

    def test_config_token_wins(self):
        with NO_FILES:
            entry, env, why = console.toolserver_mcp(
                _cfg(toolserver_token="cfgtok"), environ={}, bridge_ok=True,
                probe=_ok)
        self.assertIsNotNone(entry)
        self.assertEqual(env["TOOLSERVER_TOKEN"], "cfgtok")
        self.assertIn("token from config", why)


class RunConsoleNotice(unittest.TestCase):
    def test_unconfigured_prints_one_line_and_writes_no_mcp(self):
        import tempfile
        with tempfile.TemporaryDirectory() as ws, \
             mock.patch.object(console, "fetch_model_map",
                               return_value=({"m": {"name": "m"}}, "m")), \
             mock.patch.object(console, "toolserver_mcp",
                               return_value=(None, {}, "no toolserver token for X (none)")), \
             mock.patch.object(console, "launch"), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            console.run_console(_cfg(), workspace=ws)
            with open(console.config_path(ws)) as fh:
                written = json.load(fh)
        self.assertNotIn("mcp", written)
        lines = [l for l in err.getvalue().splitlines() if "toolserver tools" in l]
        self.assertEqual(len(lines), 1)
        self.assertIn("not configured", lines[0])
        self.assertIn("TOOLSERVER_TOKEN", lines[0])


@unittest.skipUnless(console._bridge_importable(), "abstract_serve_core not installed")
class EmittedCommandSpeaksMcp(unittest.TestCase):
    def test_command_verbatim_answers_initialize(self):
        with NO_FILES:
            entry, _, _ = console.toolserver_mcp(
                _cfg(), environ={"TOOLSERVER_TOKEN": TOKEN}, bridge_ok=True,
                probe=_ok)
        req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2024-11-05"}}) + "\n"
        out = subprocess.run(entry["command"], input=req, capture_output=True,
                             text=True, timeout=15,
                             env={**os.environ, "TOOLSERVER_TOKEN": TOKEN})
        reply = json.loads(out.stdout.splitlines()[0])
        self.assertEqual(reply["id"], 1)
        self.assertIn("serverInfo", reply["result"])


if __name__ == "__main__":
    unittest.main()
