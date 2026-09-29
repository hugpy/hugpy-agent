"""`hugpy-agent console` (OpenCode co-install): the config carries the API
key as an `{env:NAME}` reference and NEVER the literal; the model map is
filtered (blocked skipped, non-chat tasks skipped, ctx/vision labels); the
default prefers the agent brain and falls back to first-available; writes
are atomic with a `.bak`; a missing binary exits with the install hint, not
an auto-install; --print-config never launches; offline reuses the existing
file. All network is mocked — no live calls."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from hugpy_agent import console
from hugpy_agent.config import Config

SECRET = "hp_super_secret_key_value_0000"

FLEET = [
    {"id": "Qwen~Qwen3-Coder-Next-GGUF", "tasks": ["text-generation"],
     "context_length": 32768},
    {"id": "openbmb~MiniCPM-V-4.6", "tasks": ["image-text-to-text"],
     "context_length": 262144},
    {"id": "blocked-model", "tasks": ["text-generation"], "blocked": True},
    {"id": "openai~whisper-large-v3", "tasks": ["automatic-speech-recognition"]},
    {"id": "legacy-task-only", "task": "text-generation"},   # old single-task shape
    {"id": "sd-turbo", "tasks": ["text-to-image"]},
]


def fake_urlopen(payload):
    """A context-manager response whose read() returns the JSON payload."""
    body = json.dumps(payload).encode()

    class _Resp:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return mock.Mock(return_value=_Resp())


class FetchModelMapTests(unittest.TestCase):
    def _fetch(self, records=FLEET):
        with mock.patch("urllib.request.urlopen",
                        fake_urlopen({"data": records})):
            return console.fetch_model_map("https://dev.hugpy.ai/api", SECRET)

    def test_filtering_blocked_and_non_text_tasks(self):
        models, _ = self._fetch()
        self.assertNotIn("blocked-model", models)          # BLOCK outranks listing
        self.assertNotIn("openai~whisper-large-v3", models)  # ASR filtered
        self.assertNotIn("sd-turbo", models)                 # diffusion filtered
        self.assertIn("Qwen~Qwen3-Coder-Next-GGUF", models)
        self.assertIn("legacy-task-only", models)            # single-task shape works

    def test_labels_ctx_and_vision(self):
        models, _ = self._fetch()
        self.assertEqual(models["Qwen~Qwen3-Coder-Next-GGUF"]["name"],
                         "Qwen~Qwen3-Coder-Next-GGUF (32k ctx)")
        self.assertEqual(models["openbmb~MiniCPM-V-4.6"]["name"],
                         "openbmb~MiniCPM-V-4.6 (256k ctx) [vision]")
        # no context_length -> no "(0k ctx)" noise
        self.assertEqual(models["legacy-task-only"]["name"], "legacy-task-only")

    def test_default_prefers_agent_brain(self):
        _, default = self._fetch()
        self.assertEqual(default, "Qwen~Qwen3-Coder-Next-GGUF")

    def test_default_falls_back_to_first_available(self):
        records = [r for r in FLEET if r["id"] != "Qwen~Qwen3-Coder-Next-GGUF"]
        models, default = self._fetch(records)
        self.assertEqual(default, next(iter(models)))

    def test_network_error_raises_console_error_with_offline_hint(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=OSError("connection refused")):
            with self.assertRaises(console.ConsoleError) as ctx:
                console.fetch_model_map("https://dev.hugpy.ai/api", SECRET)
        self.assertIn("--offline", str(ctx.exception))
        self.assertIn("/v1/models", str(ctx.exception))

    def test_empty_chat_drivable_list_refused(self):
        with self.assertRaises(console.ConsoleError):
            self._fetch([{"id": "only-asr",
                          "tasks": ["automatic-speech-recognition"]}])


class BuildConfigTests(unittest.TestCase):
    def test_key_is_env_reference_never_literal(self):
        models, default = {"m": {"name": "m"}}, "m"
        cfg = console.build_config("https://dev.hugpy.ai/api",
                                   console.KEY_ENV_NAME, models, default)
        text = json.dumps(cfg)
        self.assertIn("{env:%s}" % console.KEY_ENV_NAME, text)
        self.assertNotIn(SECRET, text)   # the literal never enters the dict
        self.assertEqual(cfg["provider"]["hugpy"]["options"]["apiKey"],
                         "{env:%s}" % console.KEY_ENV_NAME)

    def test_structure_matches_opencode_contract(self):
        cfg = console.build_config("https://dev.hugpy.ai/api",
                                   console.KEY_ENV_NAME,
                                   {"m": {"name": "m"}}, "m")
        hp = cfg["provider"]["hugpy"]
        self.assertEqual(hp["npm"], "@ai-sdk/openai-compatible")
        self.assertEqual(hp["options"]["baseURL"],
                         "https://dev.hugpy.ai/api/v1")
        self.assertEqual(cfg["model"], "hugpy/m")

    def test_permission_defaults_to_ask_when_env_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HUGPY_CONSOLE_PERMISSION", None)
            cfg = console.build_config("https://dev.hugpy.ai/api",
                                       console.KEY_ENV_NAME, {"m": {"name": "m"}}, "m")
        self.assertEqual(cfg["permission"],
                         {"edit": "ask", "bash": "ask", "webfetch": "ask"})

    def test_permission_honors_HUGPY_CONSOLE_PERMISSION(self):
        # The unattended keeper needs 'allow' to survive --sync (which rewrites
        # opencode.json every launch): the value must come from the env the
        # fleet sets, not a hand-edit that gets clobbered.
        for val in ("allow", "deny", "ask"):
            with mock.patch.dict(os.environ, {"HUGPY_CONSOLE_PERMISSION": val}):
                cfg = console.build_config("https://dev.hugpy.ai/api",
                                           console.KEY_ENV_NAME, {"m": {"name": "m"}}, "m")
            self.assertEqual(cfg["permission"],
                             {"edit": val, "bash": val, "webfetch": val})

    def test_permission_falls_back_to_ask_on_a_bad_value(self):
        with mock.patch.dict(os.environ, {"HUGPY_CONSOLE_PERMISSION": "yolo"}):
            cfg = console.build_config("https://dev.hugpy.ai/api",
                                       console.KEY_ENV_NAME, {"m": {"name": "m"}}, "m")
        self.assertEqual(cfg["permission"],
                         {"edit": "ask", "bash": "ask", "webfetch": "ask"})

    def test_base_normalization(self):
        for base, want in (
                ("dev.hugpy.ai", "https://dev.hugpy.ai/api/v1"),
                ("https://x.example/api", "https://x.example/api/v1"),
                ("https://x.example/api/v1", "https://x.example/api/v1")):
            cfg = console.build_config(base, "K", {"m": {"name": "m"}}, "m")
            self.assertEqual(cfg["provider"]["hugpy"]["options"]["baseURL"],
                             want, base)


class SmallModelTests(unittest.TestCase):
    """small_model (OpenCode's title/lightweight-task model) is emitted from
    config, defaults to the tiny coder model, and honours the env override."""

    def _build(self, models, env=None):
        with mock.patch.dict(os.environ, env or {}, clear=False):
            if env is None:
                os.environ.pop(console.SMALL_MODEL_ENV, None)
            return console.build_config("https://dev.hugpy.ai/api", "K",
                                        models, "m")

    def test_default_emitted_and_listed(self):
        models = {"m": {"name": "m"},
                  console.DEFAULT_SMALL_MODEL: {"name": "tiny"}}
        cfg = self._build(models)
        self.assertEqual(cfg["small_model"],
                         "hugpy/Qwen2.5-Coder-1.5B-Instruct-GGUF")
        self.assertEqual(cfg["model"], "hugpy/m")          # main model untouched
        self.assertIn(console.DEFAULT_SMALL_MODEL,
                      cfg["provider"]["hugpy"]["models"])

    def test_default_added_to_models_map_when_fleet_lacks_it(self):
        models = {"m": {"name": "m"}}
        cfg = self._build(models)
        self.assertIn(console.DEFAULT_SMALL_MODEL,
                      cfg["provider"]["hugpy"]["models"])
        self.assertEqual(models, {"m": {"name": "m"}})     # input not mutated

    def test_resolves_to_fleet_spelling_by_bare_tail(self):
        models = {"m": {"name": "m"},
                  "bartowski~Qwen2.5-Coder-1.5B-Instruct-GGUF": {"name": "x"}}
        cfg = self._build(models)
        self.assertEqual(cfg["small_model"],
                         "hugpy/bartowski~Qwen2.5-Coder-1.5B-Instruct-GGUF")
        self.assertEqual(len(cfg["provider"]["hugpy"]["models"]), 2)

    def test_env_override_honoured(self):
        models = {"m": {"name": "m"}, "Qwen3-0.6B-GGUF": {"name": "q"}}
        for val in ("Qwen3-0.6B-GGUF", "hugpy/Qwen3-0.6B-GGUF"):
            cfg = self._build(models, {console.SMALL_MODEL_ENV: val})
            self.assertEqual(cfg["small_model"], "hugpy/Qwen3-0.6B-GGUF")
            self.assertNotIn(console.DEFAULT_SMALL_MODEL,
                             cfg["provider"]["hugpy"]["models"])

    def test_env_off_omits_key(self):
        for val in ("off", "none", ""):
            cfg = self._build({"m": {"name": "m"}},
                              {console.SMALL_MODEL_ENV: val})
            self.assertNotIn("small_model", cfg)
            self.assertEqual(list(cfg["provider"]["hugpy"]["models"]), ["m"])


class MaterializeTests(unittest.TestCase):
    def test_atomic_write_and_bak_on_refresh(self):
        with tempfile.TemporaryDirectory() as ws:
            p1 = console.materialize(ws, {"v": 1})
            self.assertTrue(os.path.exists(p1))
            self.assertFalse(os.path.exists(p1 + ".bak"))  # first write: no bak
            console.materialize(ws, {"v": 2})
            with open(p1) as fh:
                self.assertEqual(json.load(fh), {"v": 2})
            with open(p1 + ".bak") as fh:
                self.assertEqual(json.load(fh), {"v": 1})  # rollback lever
            # no torn temp files left behind
            leftovers = [f for f in os.listdir(ws) if f.endswith(".tmp")]
            self.assertEqual(leftovers, [])


class LaunchTests(unittest.TestCase):
    def test_missing_binary_raises_hint_and_never_installs(self):
        with mock.patch.object(console, "resolve_opencode", return_value=None):
            with self.assertRaises(console.ConsoleError) as ctx:
                console.launch("/tmp", SECRET)
        msg = str(ctx.exception)
        self.assertIn("npm install -g opencode-ai", msg)
        self.assertIn("npm config set prefix", msg)

    def test_launch_execs_from_workspace_with_key_in_env(self):
        with tempfile.TemporaryDirectory() as ws:
            with mock.patch("os.execvp") as execvp, \
                 mock.patch("os.chdir") as chdir, \
                 mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(console.KEY_ENV_NAME, None)
                console.launch(ws, SECRET, binary="/fake/opencode")
                # the literal key travels ONLY via the process environment
                # (assert inside patch.dict — it restores env on exit)
                self.assertEqual(os.environ.get(console.KEY_ENV_NAME), SECRET)
            chdir.assert_called_once_with(os.path.realpath(ws))
            execvp.assert_called_once_with("/fake/opencode", ["/fake/opencode"])

    def test_resolve_opencode_falls_back_to_npm_global(self):
        with tempfile.TemporaryDirectory() as home:
            bindir = os.path.join(home, ".npm-global", "bin")
            os.makedirs(bindir)
            binpath = os.path.join(bindir, "opencode")
            with open(binpath, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(binpath, 0o755)
            with mock.patch("shutil.which", return_value=None), \
                 mock.patch.dict(os.environ, {"HOME": home}):
                self.assertEqual(console.resolve_opencode(), binpath)
            with mock.patch("shutil.which", return_value=None), \
                 mock.patch.dict(os.environ, {"HOME": os.path.join(home, "x")}):
                self.assertIsNone(console.resolve_opencode())


class AllowAllTests(unittest.TestCase):
    """--allow-all: per-launch all-allow via OPENCODE_PERMISSION + --auto;
    off by default; the shared opencode.json is never touched."""

    def test_off_means_no_override(self):
        argv, env = console.opencode_launch_spec("/b/opencode", False)
        self.assertEqual(argv, ["/b/opencode"])
        self.assertEqual(env, {})

    def test_on_allows_every_known_key_and_passes_auto(self):
        argv, env = console.opencode_launch_spec("/b/opencode", True)
        self.assertEqual(argv, ["/b/opencode", "--auto"])
        perm = json.loads(env["OPENCODE_PERMISSION"])
        self.assertEqual(set(perm), set(console.OPENCODE_PERMISSION_KEYS))
        for key in ("edit", "bash", "webfetch", "doom_loop",
                    "external_directory", "read"):
            self.assertEqual(perm[key], "allow", key)
        self.assertEqual(set(perm.values()), {"allow"})

    def test_flag_wins_env_is_default(self):
        self.assertFalse(console.allow_all_requested(None, {}))
        self.assertTrue(console.allow_all_requested(
            None, {console.ALLOW_ALL_ENV: "1"}))
        self.assertFalse(console.allow_all_requested(
            None, {console.ALLOW_ALL_ENV: "0"}))
        self.assertTrue(console.allow_all_requested(True, {}))
        self.assertFalse(console.allow_all_requested(
            False, {console.ALLOW_ALL_ENV: "1"}))

    def _launch(self, allow_all, env):
        with tempfile.TemporaryDirectory() as ws, \
             mock.patch("os.execvp") as execvp, mock.patch("os.chdir"), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err, \
             mock.patch.dict(os.environ, env, clear=False):
            if not env:
                os.environ.pop(console.ALLOW_ALL_ENV, None)
            os.environ.pop("OPENCODE_PERMISSION", None)
            console.launch(ws, "", binary="/fake/opencode", allow_all=allow_all)
            perm = os.environ.get("OPENCODE_PERMISSION")
        return execvp, err.getvalue(), perm

    def test_launch_default_prompts(self):
        execvp, err, perm = self._launch(None, {})
        execvp.assert_called_once_with("/fake/opencode", ["/fake/opencode"])
        self.assertIsNone(perm)
        self.assertNotIn(console.ALLOW_ALL_BANNER, err)

    def test_launch_allow_all_banner_env_and_auto(self):
        for allow_all, env in ((True, {}), (None, {console.ALLOW_ALL_ENV: "1"})):
            execvp, err, perm = self._launch(allow_all, env)
            execvp.assert_called_once_with("/fake/opencode",
                                           ["/fake/opencode", "--auto"])
            self.assertIn("opencode: all permissions ALLOWED (--allow-all)", err)
            self.assertEqual(set(json.loads(perm).values()), {"allow"})

    def test_run_console_leaves_shared_config_prompting(self):
        with tempfile.TemporaryDirectory() as ws, \
             mock.patch("urllib.request.urlopen", fake_urlopen({"data": FLEET})), \
             mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.object(console, "launch") as launch:
            os.environ.pop("HUGPY_CONSOLE_PERMISSION", None)
            console.run_console(Config(base="https://dev.hugpy.ai/api",
                                       api_key=SECRET),
                                workspace=ws, allow_all=True)
            with open(console.config_path(ws)) as fh:
                written = json.load(fh)
        self.assertEqual(written["permission"],
                         {"edit": "ask", "bash": "ask", "webfetch": "ask"})
        self.assertIs(launch.call_args.kwargs["allow_all"], True)


class RunConsoleTests(unittest.TestCase):
    def _cfg(self):
        return Config(base="https://dev.hugpy.ai/api", api_key=SECRET)

    def test_print_config_syncs_but_never_writes_or_launches(self):
        with tempfile.TemporaryDirectory() as ws, \
             mock.patch("urllib.request.urlopen", fake_urlopen({"data": FLEET})), \
             mock.patch.object(console, "launch") as launch, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = console.run_console(self._cfg(), workspace=ws,
                                     print_config=True)
        self.assertEqual(rc, 0)
        launch.assert_not_called()
        self.assertFalse(os.path.exists(console.config_path(ws)))  # no write
        printed = json.loads(out.getvalue())
        self.assertEqual(printed["model"], "hugpy/Qwen~Qwen3-Coder-Next-GGUF")
        self.assertNotIn(SECRET, out.getvalue())   # env reference, not literal

    def test_sync_writes_config_then_launches(self):
        with tempfile.TemporaryDirectory() as ws, \
             mock.patch("urllib.request.urlopen", fake_urlopen({"data": FLEET})), \
             mock.patch.object(console, "launch") as launch:
            console.run_console(self._cfg(), workspace=ws)
            path = console.config_path(ws)
            self.assertTrue(os.path.exists(path))
            with open(path) as fh:
                written = fh.read()
            self.assertNotIn(SECRET, written)  # THE assertion: no key at rest
            launch.assert_called_once()

    def test_model_override_wins_over_default(self):
        with tempfile.TemporaryDirectory() as ws, \
             mock.patch("urllib.request.urlopen", fake_urlopen({"data": FLEET})), \
             mock.patch.object(console, "launch"):
            console.run_console(self._cfg(), workspace=ws,
                                model="legacy-task-only")
            with open(console.config_path(ws)) as fh:
                self.assertEqual(json.load(fh)["model"],
                                 "hugpy/legacy-task-only")

    def test_offline_reuses_existing_config_no_network(self):
        with tempfile.TemporaryDirectory() as ws:
            console.materialize(ws, {"model": "hugpy/old", "provider": {}})
            with mock.patch("urllib.request.urlopen",
                            side_effect=AssertionError("network hit")), \
                 mock.patch.object(console, "launch") as launch:
                rc = console.run_console(self._cfg(), workspace=ws,
                                         offline=True)
            self.assertEqual(rc, 0)
            launch.assert_called_once()

    def test_offline_without_existing_config_refused_with_instructions(self):
        with tempfile.TemporaryDirectory() as ws:
            with self.assertRaises(console.ConsoleError) as ctx:
                console.run_console(self._cfg(), workspace=ws, offline=True)
            self.assertIn("hugpy-agent console", str(ctx.exception))

    def test_cli_missing_binary_exit_1_with_hint(self):
        """The full cmd_console path: missing binary -> exit 1 + install hint
        on stderr, config still written (the sync is real work worth keeping)."""
        from hugpy_agent import cli
        with tempfile.TemporaryDirectory() as ws, \
             mock.patch("urllib.request.urlopen", fake_urlopen({"data": FLEET})), \
             mock.patch.object(console, "resolve_opencode", return_value=None), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = cli.main(["console", "--opencode", "--workspace", ws])
        self.assertEqual(rc, 1)
        self.assertIn("npm install -g opencode-ai", err.getvalue())


class QwenCodeFrontendTests(unittest.TestCase):
    """The Claude-free qwen-code frontend: base = the /v1 mount, the auth
    selection is merged into settings.json without clobbering (and never the
    key), and a missing binary raises the install hint."""

    def test_qwen_base_forms(self):
        for base, want in [
            ("https://dev.hugpy.ai", "https://dev.hugpy.ai/api/v1"),
            ("https://dev.hugpy.ai/api", "https://dev.hugpy.ai/api/v1"),
            ("http://127.0.0.1:7002/v1", "http://127.0.0.1:7002/v1"),
        ]:
            self.assertEqual(console.qwen_base(base), want, base)

    def test_ensure_auth_creates_and_merges(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            console.ensure_qwen_openai_auth(path)
            with open(path) as fh:
                self.assertEqual(
                    json.load(fh)["security"]["auth"]["selectedType"], "openai")
            # Existing unrelated settings survive the merge.
            with open(path, "w") as fh:
                json.dump({"theme": "dark", "security": {"other": 1}}, fh)
            console.ensure_qwen_openai_auth(path)
            with open(path) as fh:
                got = json.load(fh)
            self.assertEqual(got["theme"], "dark")
            self.assertEqual(got["security"]["other"], 1)
            self.assertEqual(got["security"]["auth"]["selectedType"], "openai")

    def test_ensure_auth_leaves_corrupt_file_alone(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w") as fh:
                fh.write("{not json")
            console.ensure_qwen_openai_auth(path)
            with open(path) as fh:
                self.assertEqual(fh.read(), "{not json")

    def test_missing_binary_raises_install_hint(self):
        with mock.patch.object(console, "resolve_qwen", return_value=None):
            with self.assertRaises(console.ConsoleError) as ctx:
                console.launch_qwen_code("https://dev.hugpy.ai", SECRET)
        self.assertIn("@qwen-code/qwen-code", str(ctx.exception))

    def test_launch_env_key_placeholder_and_model_default(self):
        """Env-only seam: base/key/model land in the child env (placeholder
        key on an open fleet, pre-set OPENAI_MODEL respected); exec is
        reached — the key never lands in any file."""
        calls = {}

        def fake_exec(binary, argv):
            calls["binary"], calls["argv"] = binary, argv

        for key, want_key in [(SECRET, SECRET), ("", "hugpy-open-fleet")]:
            with mock.patch.object(console, "resolve_qwen",
                                   return_value="/bin/qwen"), \
                 mock.patch.object(console, "ensure_qwen_openai_auth"), \
                 mock.patch.object(console, "_rebind_stdin_to_tty"), \
                 mock.patch.object(console.os, "execvp", fake_exec), \
                 mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OPENAI_MODEL", None)
                console.launch_qwen_code("https://dev.hugpy.ai", key,
                                         "Qwen~Qwen3-Coder-Next-GGUF")
                self.assertEqual(os.environ["OPENAI_BASE_URL"],
                                 "https://dev.hugpy.ai/api/v1")
                self.assertEqual(os.environ["OPENAI_API_KEY"], want_key)
                # The brain is named EXPLICITLY — "default" would resolve to
                # the small chat default whose ctx agent prompts exceed.
                self.assertEqual(os.environ["OPENAI_MODEL"],
                                 "Qwen~Qwen3-Coder-Next-GGUF")
        self.assertEqual(calls["binary"], "/bin/qwen")


if __name__ == "__main__":
    unittest.main()
