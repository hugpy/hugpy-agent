"""One test per row of harness_settings.HARNESSES: allow_all and the small
(title) model are expressed through each harness's native mechanism, off by
default, and never leak the hugpy control vars into the harness child."""
import _bootstrap  # noqa: F401
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from hugpy_agent import console, frontends as f
from hugpy_agent import harness_settings as hs
from hugpy_agent.fleet_console import Client

CONTROL = [hs.ALLOW_ALL_ENV, hs.SMALL_MODEL_ENV] + [
    r["env_prefix"] + s for r in hs.HARNESSES.values()
    for s in ("_ALLOW_ALL", "_SMALL_MODEL")]
SMALL = "Qwen2.5-Coder-1.5B-Instruct-GGUF"


class _Clean(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        for k in CONTROL + ["ANTHROPIC_SMALL_FAST_MODEL",
                            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                            console.QWEN_SYSTEM_SETTINGS_ENV,
                            "OPENCODE_PERMISSION"]:
            os.environ.pop(k, None)
        err = mock.patch("sys.stderr", new_callable=io.StringIO)
        self.err = err.start()
        self.addCleanup(err.stop)


class TableTests(_Clean):
    def test_every_registered_harness_has_a_row(self):
        self.assertEqual({s["id"] for s in f.REGISTRY}, set(hs.HARNESSES))
        for hid, row in hs.HARNESSES.items():
            self.assertTrue(row["allow_all_argv"] or row["allow_all_env"], hid)
            self.assertTrue(row["small_model"], hid)
            self.assertTrue(row["verified"], hid)

    def test_defaults_and_precedence(self):
        for hid, row in hs.HARNESSES.items():
            self.assertFalse(hs.allow_all(hid, {}), hid)
            self.assertEqual(hs.small_model(hid, {}), SMALL)
            self.assertTrue(hs.allow_all(hid, {hs.ALLOW_ALL_ENV: "1"}))
            self.assertFalse(hs.allow_all(hid, {hs.ALLOW_ALL_ENV: "1",
                                                row["env_prefix"] + "_ALLOW_ALL": "0"}))
            self.assertEqual(hs.small_model(hid, {hs.SMALL_MODEL_ENV: "hugpy/x"}), "x")
            self.assertEqual(hs.small_model(hid, {hs.SMALL_MODEL_ENV: "x",
                                                  row["env_prefix"] + "_SMALL_MODEL": "y"}), "y")
            self.assertIsNone(hs.small_model(hid, {hs.SMALL_MODEL_ENV: "off"}))

    def test_harness_env_hook(self):
        calls = []
        hs.ENV_HOOKS.append(lambda h, env: calls.append(h) or env.update(X_HOOK="1"))
        self.addCleanup(hs.ENV_HOOKS.pop)
        client = Client("http://localhost:7002", "k", "")
        with mock.patch.object(f, "resolve", return_value="/tools/aider"):
            _, env = f.prepare(next(s for s in f.REGISTRY if s["id"] == "aider"),
                               client, "m", {})
        self.assertEqual((calls, env["X_HOOK"]), (["aider"], "1"))


class OpenCodeRow(_Clean):
    def test_allow_all_and_small_model(self):
        self.assertEqual(console.opencode_launch_spec("oc")[0], ["oc"])
        os.environ[hs.ALLOW_ALL_ENV] = "1"
        argv, env = console.opencode_launch_spec("oc")
        self.assertEqual(argv, ["oc", "--auto"])
        self.assertEqual(set(json.loads(env["OPENCODE_PERMISSION"])),
                         set(hs.OPENCODE_PERMISSION_KEYS))
        cfg = console.build_config("https://x/api", "K", {"m": {}}, "m")
        self.assertEqual(cfg["small_model"], "hugpy/" + SMALL)


class ClaudeCodeRow(_Clean):
    def _launch(self):
        with mock.patch.object(console.os, "execvp") as ex, \
             mock.patch.object(console, "_rebind_stdin_to_tty"):
            console.launch_claude_code("https://x/api", "k", binary="claude",
                                       init_prompt="")
        return ex.call_args.args[1]

    def test_off_by_default_small_model_on_hugpy_shim(self):
        self.assertEqual(self._launch(), ["claude"])
        for name in ("ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
            self.assertEqual(os.environ[name], SMALL)

    def test_allow_all(self):
        os.environ[hs.ALLOW_ALL_ENV] = "1"
        self.assertEqual(self._launch(), ["claude", "--dangerously-skip-permissions"])
        self.assertIsNone(os.environ.get(hs.ALLOW_ALL_ENV))
        self.assertIn("claude-code: all permissions ALLOWED", self.err.getvalue())

    def test_small_model_off(self):
        os.environ["HUGPY_CLAUDE_SMALL_MODEL"] = "off"
        self._launch()
        self.assertNotIn("ANTHROPIC_SMALL_FAST_MODEL", os.environ)


class QwenCodeRow(_Clean):
    def _launch(self, ws):
        with mock.patch.object(console.os, "execvp") as ex, \
             mock.patch.object(console, "_rebind_stdin_to_tty"), \
             mock.patch.object(console, "ensure_qwen_openai_auth"), \
             mock.patch.object(console, "DEFAULT_WORKSPACE", ws):
            console.launch_qwen_code("https://x/api", "k", "m", binary="qwen")
        return ex.call_args.args[1]

    def test_allow_all_and_fast_model(self):
        with tempfile.TemporaryDirectory() as ws:
            self.assertEqual(self._launch(ws), ["qwen"])
            with open(os.environ[console.QWEN_SYSTEM_SETTINGS_ENV]) as fh:
                self.assertEqual(json.load(fh)["fastModel"], SMALL)
            self.assertTrue(os.environ[console.QWEN_SYSTEM_SETTINGS_ENV].startswith(ws))
            os.environ[hs.ALLOW_ALL_ENV] = "1"
            self.assertEqual(self._launch(ws), ["qwen", "--yolo"])

    def test_existing_system_settings_are_merged(self):
        with tempfile.TemporaryDirectory() as d:
            base = os.path.join(d, "sys.json")
            with open(base, "w") as fh:
                json.dump({"keep": 1}, fh)
            out = console.write_qwen_fast_model_settings(
                "tiny", os.path.join(d, "derived.json"),
                {console.QWEN_SYSTEM_SETTINGS_ENV: base})
            with open(out) as fh:
                self.assertEqual(json.load(fh), {"keep": 1, "fastModel": "tiny"})


class HermesRow(_Clean):
    def test_allow_all_and_title_generation(self):
        client = Client("http://localhost:7002", "k", "")
        spec = next(s for s in f.REGISTRY if s["id"] == "hermes")
        with mock.patch.object(f, "resolve", return_value="/tools/hermes"):
            argv, env = f.prepare(spec, client, "m", {})
            self.assertNotIn("--yolo", argv)
            argv, env = f.prepare(spec, client, "m", {hs.ALLOW_ALL_ENV: "1"})
        self.assertEqual(argv[-1], "--yolo")
        self.assertNotIn(hs.ALLOW_ALL_ENV, env)
        with tempfile.TemporaryDirectory() as root:
            profile = f.configure(spec, env, "m", root=root)
            with open(os.path.join(profile, "config.yaml")) as fh:
                aux = json.load(fh)["auxiliary"]
        self.assertEqual(aux["title_generation"],
                         {"provider": "custom:hugpy", "model": SMALL})


class AiderRow(_Clean):
    def test_allow_all_and_weak_model(self):
        client = Client("http://localhost:7002", "k", "")
        spec = next(s for s in f.REGISTRY if s["id"] == "aider")
        with mock.patch.object(f, "resolve", return_value="/tools/aider"):
            argv, _ = f.prepare(spec, client, "m", {})
            self.assertNotIn("--yes-always", argv)
            self.assertEqual(argv[argv.index("--weak-model") + 1], "openai/" + SMALL)
            argv, env = f.prepare(spec, client, "m", {hs.ALLOW_ALL_ENV: "1"})
        self.assertEqual(argv[-1], "--yes-always")
        self.assertNotIn(hs.ALLOW_ALL_ENV, env)


if __name__ == "__main__":
    unittest.main()
