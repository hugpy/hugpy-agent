import _bootstrap  # noqa: F401
import unittest
import tempfile
import json
import os
from unittest.mock import patch, Mock

from hugpy_agent import frontends as f
from hugpy_agent import cli
from hugpy_agent import fleet_tui as tui
from hugpy_agent.fleet_console import Client, FleetError


class FrontendTests(unittest.TestCase):
    def setUp(self):
        self.client = Client("http://localhost:7002", "fleet-secret", "operator-secret")

    def spec(self, key):
        return next(s for s in f.REGISTRY if s["id"] == key)

    def prepare(self, key, env=None):
        with patch.object(f, "resolve", return_value="/tools/" + key):
            return f.prepare(self.spec(key), self.client, "Org~Model", env or {})

    def test_all_requested_frontends_have_entries(self):
        self.assertEqual({s["id"] for s in f.REGISTRY}, {"hermes", "claude-code", "qwen-code", "opencode", "aider"})

    def test_hermes_explicit_provider_and_model_override_config(self):
        argv, env = self.prepare("hermes")
        self.assertEqual(argv, ["/tools/hermes", "chat", "--provider", "custom:hugpy", "--model", "Org~Model"])
        self.assertEqual(env["OPENAI_BASE_URL"], "http://localhost:7002/v1")

    def test_aider_all_model_roles_stay_on_fleet(self):
        argv, env = self.prepare("aider")
        for flag in ("--model", "--weak-model", "--editor-model"):
            self.assertEqual(argv[argv.index(flag) + 1], "openai/Org~Model")
        self.assertEqual(env["OPENAI_API_BASE"], "http://localhost:7002/v1")

    def test_all_adapters_keep_secrets_out_of_argv_and_operator_out_of_child(self):
        for spec in f.REGISTRY:
            with self.subTest(spec=spec["id"]):
                original = {"HUGPY_OPERATOR_TOKEN": "operator-secret", "OPENAI_MODEL": "old"}
                argv, env = self.prepare(spec["id"], original)
                self.assertNotIn("HUGPY_OPERATOR_TOKEN", env)
                self.assertNotIn("fleet-secret", str(argv))
                self.assertEqual(env["OPENAI_MODEL"], "Org~Model")
                self.assertEqual(original["OPENAI_MODEL"], "old")

    def test_existing_adapters_receive_explicit_v1_base(self):
        for key in ("opencode", "qwen-code", "claude-code"):
            argv, env = self.prepare(key)
            self.assertEqual(argv[argv.index("--base") + 1], "http://localhost:7002/v1")
            self.assertEqual(argv[argv.index("--model") + 1], "Org~Model")

    def test_claude_drops_stale_model_header_preserving_other_headers(self):
        _, env = self.prepare("claude-code", {"ANTHROPIC_CUSTOM_HEADERS": "X-Hugpy-Model: old\nOther: keep"})
        self.assertEqual(env["ANTHROPIC_CUSTOM_HEADERS"], "Other: keep")

    def test_missing_frontend_is_not_launched(self):
        with patch.object(f, "resolve", return_value=None):
            with self.assertRaisesRegex(FleetError, "not installed"):
                f.prepare(self.spec("hermes"), self.client, "m")

    def test_every_frontend_has_a_fixed_installer(self):
        for spec in f.REGISTRY:
            with self.subTest(spec=spec["id"]):
                self.assertIsInstance(spec.get("install"), list)
                self.assertTrue(spec["install"])
                self.assertTrue(spec.get("install_text"))

    def test_install_runs_registry_argv_without_shell_interpolation(self):
        spec = self.spec("claude-code")
        with patch.object(f.subprocess, "call", return_value=0) as call:
            self.assertEqual(f.install(spec), 0)
        call.assert_called_once_with(spec["install"])

    def test_root_harness_flags_map_to_every_registered_adapter(self):
        expected = {
            "--hermes": "hermes", "--claude-code": "claude-code",
            "--qwen-code": "qwen-code", "--opencode": "opencode",
            "--aider": "aider",
        }
        for flag, frontend in expected.items():
            with self.subTest(flag=flag):
                self.assertEqual(cli._direct_harness_argv(
                    [flag, "--model", "m"]),
                    ["_frontend", frontend, "--model", "m"])

    def test_console_local_harness_flag_is_not_rewritten(self):
        argv = ["console", "--opencode"]
        self.assertEqual(cli._direct_harness_argv(argv), argv)

    def test_direct_launcher_uses_shared_adapter_and_execs(self):
        fake_cfg = Mock(base="http://localhost:7002", api_key="k",
                        model="m", timeout=9)
        prepared = (["/tools/aider", "--model", "openai/m"], {"A": "B"})
        with patch.object(cli, "_cfg", return_value=fake_cfg), \
             patch.object(f, "prepare", return_value=prepared) as prepare, \
             patch.object(f, "configure") as configure, \
             patch.object(cli.os, "execvpe") as execute:
            rc = cli.main(["--aider"])
        self.assertEqual(rc, 0)
        self.assertEqual(prepare.call_args.args[0]["id"], "aider")
        configure.assert_called_once()
        execute.assert_called_once_with(prepared[0][0], *prepared)

    def test_harness_selects_opencode_console(self):
        args = Mock(base="https://fleet", model="m", workspace="/tmp/c")
        with patch.object(cli, "cmd_console", return_value=7) as console_cmd:
            self.assertEqual(cli.cmd_harness(args), 7)
        launched = console_cmd.call_args.args[0]
        self.assertEqual(launched.frontend, "opencode")
        self.assertTrue(launched.opencode)
        self.assertEqual(launched.console_workspace, "/tmp/c")

    def test_frontend_first_selects_fleet_model_then_returns(self):
        ui = tui.Console(Mock(), self.client)
        ui.models = [{"model": "m", "task": "text-generation", "readiness": "ready now", "tok_s": 10}]
        with patch.object(f, "resolve", return_value="/tools/hermes"), patch.object(f, "configure", return_value=None), patch.object(ui, "choose", return_value=0), \
             patch.object(tui.curses, "def_prog_mode"), patch.object(tui.curses, "endwin"), patch.object(tui.curses, "reset_prog_mode") as restore, \
             patch.object(tui.subprocess, "call", return_value=0) as launch:
            ui.launch(frontend=self.spec("hermes"))
        self.assertEqual(launch.call_args.args[0][-1], "m")
        self.assertIn("Back in fleet console", ui.notice)
        restore.assert_called_once()

    def test_missing_entry_shows_install_information_without_leaving_console(self):
        ui = tui.Console(Mock(), self.client)
        with patch.object(f, "resolve", return_value=None), \
             patch.object(ui, "choose", return_value=2), \
             patch.object(ui, "view") as view, \
             patch.object(tui.subprocess, "call") as launch:
            ui.launch(frontend=self.spec("hermes"))
        self.assertIn("installation", view.call_args.args[0])
        launch.assert_not_called()

    def test_missing_entry_can_install_then_launch(self):
        ui = tui.Console(Mock(), self.client)
        ui.models = [{"model": "m", "task": "text-generation",
                      "readiness": "ready now", "tok_s": 1}]
        missing = {**self.spec("aider"), "path": None}
        present = {**missing, "path": "/tools/aider"}
        with patch.object(f, "resolve", side_effect=[None, "/tools/aider", "/tools/aider"]), \
             patch.object(f, "available", return_value=[present]), \
             patch.object(f, "install", return_value=0) as install, \
             patch.object(f, "prepare", return_value=(["/tools/aider"], {})), \
             patch.object(f, "configure", return_value=None), \
             patch.object(ui, "choose", side_effect=[1, 1]), \
             patch.object(tui.curses, "def_prog_mode"), \
             patch.object(tui.curses, "endwin"), \
             patch.object(tui.curses, "reset_prog_mode"), \
             patch.object(tui.subprocess, "call", return_value=0) as launch:
            ui.launch(model="m", frontend=missing)
        install.assert_called_once_with(missing)
        launch.assert_called_once_with(["/tools/aider"], env={})

    def test_frontend_tab_survives_snapshot_update(self):
        ui = tui.Console(Mock(), self.client)
        ui.tab = 5
        ui.send("snapshot", dict(ui.state))
        ui.drain()
        self.assertEqual(len(ui.items()), 5)

    def test_hermes_profile_selects_chat_completions_and_never_writes_key(self):
        _, env = self.prepare("hermes")
        with tempfile.TemporaryDirectory() as root:
            profile = f.configure(self.spec("hermes"), env, "Org~Model", root=root)
            with open(os.path.join(profile, "config.yaml")) as source:
                raw = source.read()
            self.assertNotIn("fleet-secret", raw)
            config = json.loads(raw)
            self.assertEqual(config["providers"]["hugpy"]["transport"], "openai_chat")
            self.assertEqual(config["model"]["provider"], "custom:hugpy")
            self.assertEqual(config["providers"]["hugpy"]["key_env"], "HUGPY_HERMES_API_KEY")
            self.assertEqual(env["HERMES_HOME"], profile)
            second = f.configure(self.spec("hermes"), env, "other", root=root)
            self.assertNotEqual(profile, second)
            self.assertTrue(os.path.isfile(os.path.join(profile, "config.yaml")))


if __name__ == "__main__":
    unittest.main()
