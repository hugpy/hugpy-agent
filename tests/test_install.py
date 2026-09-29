"""Enrollment installer (P2.7): unit rendering (key directives, %h
portability, no secrets), the 0600 env file, idempotent re-install, the
injectable runner (system-touching commands are NEVER executed here), and
the CLI entry point. All offline, all inside a tempdir 'home'."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import contextlib
import io
import os
import stat
import tempfile
import unittest

from hugpy_agent import install as install_mod
from hugpy_agent.install import (UNIT_NAME, install, main, render_env,
                                 render_unit)

KEY = "sk-SECRET-test-key"
SESSION = "https://central.test/api/discord/session/SECRETTOKEN"


class RecordingRunner:
    """The injectable system-command seam: records argv, executes nothing.
    `fail_prefixes` makes chosen commands report a non-zero exit."""

    def __init__(self, fail_prefixes=()):
        self.calls = []
        self.fail_prefixes = tuple(fail_prefixes)

    def __call__(self, argv):
        self.calls.append(list(argv))
        return 1 if any(argv[0] == p for p in self.fail_prefixes) else 0


def values(home, **extra):
    v = {"HUGPY_BASE": "https://central.test/api",
         "HUGPY_API_KEY": KEY,
         "HUGPY_WORKSPACE": os.path.join(home, "hugpy-agent", "workspace")}
    v.update(extra)
    return v


class RenderUnitTests(unittest.TestCase):
    def test_key_directives(self):
        unit = render_unit()
        for needle in ("[Unit]", "[Service]", "[Install]",
                       "Restart=on-failure",
                       "RestartSec=5",
                       "After=network-online.target",
                       "WantedBy=default.target",
                       "EnvironmentFile=%h/.config/hugpy-agent/agent.env"):
            self.assertIn(needle, unit)
        # ExecStart runs the daemon, not a one-shot:
        exec_line = [l for l in unit.splitlines()
                     if l.startswith("ExecStart=")][0]
        self.assertTrue(exec_line.endswith(" serve"))
        self.assertIn("%h/hugpy-agent/venv/bin/hugpy-agent", exec_line)

    def test_portable_no_expanded_home_and_no_secrets(self):
        unit = render_unit()
        self.assertNotIn(os.path.expanduser("~"), unit)
        self.assertNotIn("/home/", unit)
        # nothing secret-shaped belongs in the unit — it isn't 0600:
        self.assertNotIn("HUGPY_API_KEY", unit)
        self.assertNotIn(KEY, unit)

    def test_deterministic(self):
        self.assertEqual(render_unit(), render_unit())


class RenderEnvTests(unittest.TestCase):
    def test_key_value_lines(self):
        text = render_env({"HUGPY_BASE": "https://x/api",
                           "HUGPY_API_KEY": KEY})
        self.assertIn("HUGPY_BASE=https://x/api\n", text)
        self.assertIn("HUGPY_API_KEY=%s\n" % KEY, text)

    def test_rejects_unwritable_input(self):
        # A value systemd would mis-parse must fail loudly, not write:
        with self.assertRaises(ValueError):
            render_env({"HUGPY_BASE": "https://x\nHUGPY_EVIL=1"})
        with self.assertRaises(ValueError):
            render_env({"not a key": "v"})
        with self.assertRaises(ValueError):
            render_env({"": "v"})

    def test_none_becomes_empty(self):
        self.assertIn("HUGPY_TASK_QUEUE=\n",
                      render_env({"HUGPY_TASK_QUEUE": None}))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def paths(self):
        return (os.path.join(self.home, ".config", "systemd", "user",
                             UNIT_NAME),
                os.path.join(self.home, ".config", "hugpy-agent",
                             "agent.env"))

    def test_writes_unit_env_0600_and_runs_commands(self):
        runner = RecordingRunner()
        report = install(values(self.home), home=self.home, runner=runner)
        unit_path, env_path = self.paths()
        self.assertEqual(report["unit_path"], unit_path)
        self.assertEqual(report["env_path"], env_path)
        self.assertTrue(report["unit_changed"] and report["env_changed"])
        self.assertEqual(report["warnings"], [])
        # env file: 0600, carries the secret; unit: no secret anywhere.
        mode = stat.S_IMODE(os.stat(env_path).st_mode)
        self.assertEqual(mode, 0o600)
        with open(env_path) as fh:
            env_text = fh.read()
        with open(unit_path) as fh:
            unit_text = fh.read()
        self.assertIn(KEY, env_text)
        self.assertNotIn(KEY, unit_text)
        # the workspace (the daemon's jail root) exists:
        self.assertTrue(os.path.isdir(
            os.path.join(self.home, "hugpy-agent", "workspace")))
        # system-touching calls went through the runner ONLY:
        cmds = [" ".join(c) for c in runner.calls]
        self.assertTrue(any(c.startswith("loginctl enable-linger")
                            for c in cmds))
        self.assertIn("systemctl --user daemon-reload", cmds)
        self.assertIn("systemctl --user enable --now " + UNIT_NAME, cmds)
        # ... and the secret never rode an argv:
        for c in cmds:
            self.assertNotIn(KEY, c)

    def test_reinstall_is_idempotent(self):
        runner = RecordingRunner()
        install(values(self.home), home=self.home, runner=runner)
        unit_path, env_path = self.paths()
        with open(unit_path) as fh:
            unit_before = fh.read()
        with open(env_path) as fh:
            env_before = fh.read()
        report2 = install(values(self.home), home=self.home, runner=runner)
        self.assertFalse(report2["unit_changed"])
        self.assertFalse(report2["env_changed"])
        with open(unit_path) as fh:
            self.assertEqual(fh.read(), unit_before)   # byte-identical
        with open(env_path) as fh:
            self.assertEqual(fh.read(), env_before)
        self.assertEqual(stat.S_IMODE(os.stat(env_path).st_mode), 0o600)
        # no duplicated files appeared next to the real ones:
        unit_dir = os.path.dirname(unit_path)
        self.assertEqual(os.listdir(unit_dir), [UNIT_NAME])

    def test_changed_values_rewrite_in_place(self):
        runner = RecordingRunner()
        install(values(self.home), home=self.home, runner=runner)
        report = install(values(self.home, HUGPY_TASK_SOURCE="queue"),
                         home=self.home, runner=runner)
        self.assertFalse(report["unit_changed"])       # unit is config-free
        self.assertTrue(report["env_changed"])
        with open(report["env_path"]) as fh:
            self.assertIn("HUGPY_TASK_SOURCE=queue", fh.read())
        self.assertEqual(
            stat.S_IMODE(os.stat(report["env_path"]).st_mode), 0o600)

    def test_loose_env_perms_are_retightened(self):
        """A pre-existing env file with sloppy perms is fixed even when its
        content is already current (the whole point of the 0600 doctrine)."""
        runner = RecordingRunner()
        report = install(values(self.home), home=self.home, runner=runner)
        os.chmod(report["env_path"], 0o644)
        install(values(self.home), home=self.home, runner=runner)
        self.assertEqual(
            stat.S_IMODE(os.stat(report["env_path"]).st_mode), 0o600)

    def test_no_enable_skips_all_system_commands(self):
        runner = RecordingRunner()
        report = install(values(self.home), home=self.home, runner=runner,
                         enable=False)
        self.assertEqual(runner.calls, [])
        self.assertEqual(report["commands"], [])
        self.assertFalse(report["enabled"])
        self.assertTrue(os.path.exists(report["unit_path"]))

    def test_failed_linger_is_a_warning_not_a_crash(self):
        runner = RecordingRunner(fail_prefixes=("loginctl",))
        report = install(values(self.home), home=self.home, runner=runner)
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("loginctl", report["warnings"][0])
        # the later systemctl commands still ran:
        self.assertEqual(len(runner.calls), 3)

    def test_custom_venv_under_home_stays_portable(self):
        runner = RecordingRunner()
        report = install(values(self.home), home=self.home, runner=runner,
                         venv=os.path.join(self.home, "opt", "venv"))
        with open(report["unit_path"]) as fh:
            unit = fh.read()
        self.assertIn("ExecStart=%h/opt/venv/bin/hugpy-agent serve", unit)
        self.assertNotIn(self.home, unit)


class MainTests(unittest.TestCase):
    """The CLI wrapper: env-first secrets, required args, no key in stdout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self.tmp.name)
        # secrets ride the environment (the bootstrap.sh contract); save and
        # restore whatever the real environment held.
        self._saved = {k: os.environ.get(k)
                       for k in ("HUGPY_API_KEY", "HUGPY_DISCORD_SESSION")}
        os.environ["HUGPY_API_KEY"] = KEY
        os.environ["HUGPY_DISCORD_SESSION"] = SESSION

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def run_main(self, argv, runner=None):
        runner = runner or RecordingRunner()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = main(argv, home=self.home, runner=runner)
        return rc, out.getvalue(), runner

    def test_env_secrets_land_only_in_the_env_file(self):
        rc, out, runner = self.run_main(
            ["--central", "https://central.test/api",
             "--task-source", "discord-inbox"])
        self.assertEqual(rc, 0)
        env_path = os.path.join(self.home, ".config", "hugpy-agent",
                                "agent.env")
        with open(env_path) as fh:
            env_text = fh.read()
        self.assertIn("HUGPY_API_KEY=%s" % KEY, env_text)
        self.assertIn("HUGPY_DISCORD_SESSION=%s" % SESSION, env_text)
        self.assertIn("HUGPY_TASK_SOURCE=discord-inbox", env_text)
        # never printed, never on a command argv:
        self.assertNotIn(KEY, out)
        self.assertNotIn("SECRETTOKEN", out)
        for c in runner.calls:
            self.assertNotIn(KEY, " ".join(c))

    def test_missing_central_or_key_fail_fast(self):
        os.environ.pop("HUGPY_API_KEY", None)
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                main(["--central", "https://x/api"], home=self.home,
                     runner=RecordingRunner())
        os.environ["HUGPY_API_KEY"] = KEY
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                main([], home=self.home, runner=RecordingRunner())

    def test_no_enable_flag(self):
        rc, out, runner = self.run_main(
            ["--central", "https://central.test/api", "--no-enable"])
        self.assertEqual(rc, 0)
        self.assertEqual(runner.calls, [])
        self.assertIn("NOT enabled", out)

    def test_warning_exits_nonzero(self):
        runner = RecordingRunner(fail_prefixes=("systemctl",))
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = main(["--central", "https://central.test/api"],
                      home=self.home, runner=runner)
        self.assertEqual(rc, 1)
        self.assertIn("WARNING", err.getvalue())

    def test_module_default_runner_is_subprocess(self):
        """Belt-and-suspenders: the production default really is the
        subprocess runner (tests above never reach it)."""
        self.assertEqual(install_mod._default_runner.__module__,
                         install_mod.__name__)


if __name__ == "__main__":
    unittest.main()
