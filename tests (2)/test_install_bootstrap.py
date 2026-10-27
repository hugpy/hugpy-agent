"""The one-shot BOOTSTRAP installer (install/install_hugpy_agent.py) — the
file a secure one-time install link serves with a freshly minted key baked
into its EMBEDDED_API_KEY slot.

Distinct from test_install.py (which covers hugpy_agent.install — the
systemd-unit enrollment installer inside the package). This bootstrap script
is stdlib-only and deliberately NOT part of the package: it is what a bare
box curls BEFORE hugpy_agent exists there. It lives at
``install/install_hugpy_agent.py`` (central's ``/agent/install/<link_id>``
route serves that same single-source file, templated).

Covered here: the EMBEDDED_API_KEY template slot, key-resolution precedence
(--api-key > embedded > env > ./.env > ~/.env), find_editable_root, the .env
upsert (replace-not-duplicate), and validate()'s refusal without a key.
All offline; no subprocesses run (steps are never executed here).
"""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import importlib.util
import os
import tempfile
import unittest

_INSTALLER = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "install", "install_hugpy_agent.py"))


def _load():
    """Import the installer as a module from its file path (it is a script,
    not a package member — the same bytes central serves)."""
    spec = importlib.util.spec_from_file_location("install_hugpy_agent",
                                                  _INSTALLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TemplateSlotTests(unittest.TestCase):
    def test_slot_line_present_verbatim(self):
        # Central's templating replaces THIS EXACT line — if it drifts, the
        # one-time download route refuses to serve (fail-closed) and this test
        # points at why.
        with open(_INSTALLER, encoding="utf-8") as fh:
            self.assertIn('EMBEDDED_API_KEY = ""', fh.read())

    def test_slot_defaults_empty(self):
        self.assertEqual(_load().EMBEDDED_API_KEY, "")


class PipUpgradeStepTests(unittest.TestCase):
    """The venv-only pip self-upgrade: old distro pips choke on modern wheels /
    PEP 517 builds, so the installer refreshes pip BEFORE the package install —
    but only inside a venv, never a system/PEP-668 python."""

    def setUp(self):
        self.mod = _load()

    def test_upgrade_step_registered_before_install(self):
        names = [n for n, _ in self.mod.STEP_REGISTRY]
        self.assertIn("upgrade pip (venv only)", names)
        self.assertIn("pip install hugpy_agent", names)
        self.assertLess(names.index("upgrade pip (venv only)"),
                        names.index("pip install hugpy_agent"))

    def test_upgrade_uses_dash_m_pip_spelling(self):
        # `-m pip install --upgrade pip` (via sys.executable) is mandatory — a
        # bare `pip.exe` cannot self-replace on Windows.
        import inspect
        src = inspect.getsource(self.mod.step_pip_upgrade)
        self.assertIn('"-m", "pip", "install", "--upgrade", "pip"', src)

    def test_in_venv_detects_base_prefix(self):
        # sanity: the guard is the standard sys.prefix != sys.base_prefix check.
        import inspect
        src = inspect.getsource(self.mod._in_venv)
        self.assertIn("base_prefix", src)


class PythonFloorTests(unittest.TestCase):
    """The >= 3.10 floor with interpreter DISCOVERY. A stock Mac's python is the
    Xcode CLT 3.9, which pip reports only as a cryptic 'No matching
    distribution found' (field report 2026-07-24). We gate first, search for a
    good interpreter, and fail honestly + early when there is none."""

    def setUp(self):
        self.mod = _load()
        self._real_vi = self.mod.sys.version_info
        self._real_plat = self.mod.sys.platform
        self._real_exe = self.mod.sys.executable

    def tearDown(self):
        self.mod.sys.version_info = self._real_vi
        self.mod.sys.platform = self._real_plat
        self.mod.sys.executable = self._real_exe

    def _fake_39(self):
        import collections
        V = collections.namedtuple(
            "V", "major minor micro releaselevel serial")
        self.mod.sys.version_info = V(3, 9, 6, "final", 0)

    def _ctx(self):
        return self.mod.Context(cfg=self.mod.InstallConfig(
            api_key="hp_x", api_key_source="t").validate())

    def test_gate_registered_first_before_venv(self):
        names = [n for n, _ in self.mod.STEP_REGISTRY]
        self.assertEqual(names[0], "check python version floor")
        self.assertLess(names.index("check python version floor"),
                        names.index("create venv (system python only)"))

    def test_modern_python_is_a_noop(self):
        ctx = self._ctx()
        self.mod.step_python_floor(ctx)
        self.assertEqual(ctx.base_python, self.mod.sys.executable)
        self.assertEqual(ctx.notes, [])

    def test_old_python_discovers_newer_interpreter(self):
        self._fake_39()
        ctx = self._ctx()
        self.mod.step_python_floor(ctx)
        self.assertTrue(ctx.base_python)
        found = self.mod._version_of(ctx.base_python)
        self.assertIsNotNone(found)
        self.assertGreaterEqual(found, self.mod.MIN_PY)
        self.assertTrue(any("switched" in n for n in ctx.notes))

    def test_old_python_nothing_found_exits_early_and_creates_nothing(self):
        self._fake_39()
        self.mod._discover_python = lambda *a, **k: (None, None)
        tmp = tempfile.mkdtemp()
        venv = os.path.join(tmp, "hugpy-agent", "venv")
        ctx = self.mod.Context(cfg=self.mod.InstallConfig(
            api_key="hp_x", api_key_source="t", venv=venv).validate())
        with self.assertRaises(SystemExit) as cm:
            self.mod.step_python_floor(ctx)
        msg = str(cm.exception)
        self.assertIn("3.9 is too old", msg)
        self.assertIn(">= 3.10", msg)
        self.assertIn("re-run this SAME command", msg)
        # NOTHING was created before the exit:
        self.assertFalse(os.path.exists(venv))

    def test_macos_message_names_xcode_and_brew(self):
        self.mod.sys.platform = "darwin"
        self.mod.sys.executable = \
            "/Library/Developer/CommandLineTools/usr/bin/python3"
        msg = self.mod._version_floor_message((3, 9))
        self.assertIn("Xcode Command Line Tools python", msg)
        self.assertIn("brew install python", msg)
        self.assertIn("python.org", msg)
        self.assertIn("re-run this SAME command", msg)
        self.assertNotIn("apt install", msg)

    def test_linux_message_names_apt_dnf(self):
        self.mod.sys.platform = "linux"
        msg = self.mod._version_floor_message((3, 9))
        self.assertIn("apt install python3.12", msg)
        self.assertIn("dnf install python3.12", msg)
        self.assertNotIn("brew", msg)

    def test_discovery_order_prefers_newest_series(self):
        # The series list drives ordering: newest first, so a box with both
        # 3.10 and 3.13 gets 3.13.
        self.assertEqual(self.mod._PY_SERIES[0], "3.14")
        self.assertGreater(self.mod._PY_SERIES.index("3.10"),
                           self.mod._PY_SERIES.index("3.12"))
        # the documented install homes are probed:
        self.assertIn("/opt/homebrew/bin", self.mod._PY_HOME_DIRS)
        self.assertIn("/usr/local/bin", self.mod._PY_HOME_DIRS)
        self.assertIn("/usr/bin", self.mod._PY_HOME_DIRS)
        self.assertIn("Python.framework", self.mod._PY_FRAMEWORK_GLOB)

    def test_requires_python_classifier(self):
        f = self.mod._looks_like_requires_python
        self.assertTrue(f("ERROR: Ignored the following versions that require "
                          "a different python version: 0.1.0 Requires-Python "
                          ">=3.10\nERROR: No matching distribution found"))
        self.assertTrue(f("Requires-Python >=3.10"))
        self.assertTrue(f("package requires a different python"))
        # a genuine not-found must NOT be classified as a version problem
        # (so the --editable hint still shows for real local-install cases):
        self.assertFalse(f("ERROR: Could not find a version that satisfies "
                           "the requirement totally-bogus-package"))

    def test_stale_below_floor_venv_is_rebuilt_not_reused(self):
        """The 3.9 field report leaves a too-old venv behind; a re-run after
        installing brew python must REBUILD it, not re-exec back into 3.9."""
        import inspect
        src = inspect.getsource(self.mod.step_venv)
        self.assertIn("_version_of(py)", src)
        self.assertIn("MIN_PY", src)
        self.assertIn("rmtree", src)      # the stale venv is removed
        # and the no-op guard is floor-aware:
        self.assertIn("sys.version_info[:2] >= MIN_PY", src)

    def test_venv_uses_the_approved_base_interpreter(self):
        import inspect
        src = inspect.getsource(self.mod.step_venv)
        self.assertIn("ctx.base_python or sys.executable", src)
        self.assertIn('[base, "-m", "venv", venv_abs]', src)


class VenvCreateStepTests(unittest.TestCase):
    """When run against a SYSTEM python (the install-link one-liner), the
    installer creates a venv and re-execs into it BEFORE upgrading pip — so the
    PEP-668 refusal on modern Ubuntu is fixed and every later step targets one
    deterministic interpreter. Already-in-a-venv → the step no-ops."""

    def setUp(self):
        self.mod = _load()

    def test_venv_step_registered_before_pip_upgrade(self):
        names = [n for n, _ in self.mod.STEP_REGISTRY]
        self.assertIn("create venv (system python only)", names)
        self.assertLess(names.index("create venv (system python only)"),
                        names.index("upgrade pip (venv only)"))
        self.assertLess(names.index("upgrade pip (venv only)"),
                        names.index("pip install hugpy_agent"))

    def test_venv_step_noops_inside_a_venv(self):
        # This test process IS a venv (.venv) -> the step must not re-exec.
        cfg = self.mod.InstallConfig(api_key="hp_x", api_key_source="t").validate()
        ctx = self.mod.Context(cfg=cfg)
        self.mod.step_venv(ctx)   # must return, never SystemExit
        self.assertTrue(any("already in a venv" in n for n in ctx.notes))

    def test_venv_default_converges_with_bootstrap_location(self):
        # DEFAULT_VENV mirrors bootstrap.sh's ~/hugpy-agent/venv.
        self.assertTrue(self.mod.DEFAULT_VENV.endswith(
            os.path.join("hugpy-agent", "venv")))

    def test_venv_python_path_per_platform(self):
        import inspect
        src = inspect.getsource(self.mod._venv_python)
        self.assertIn("Scripts", src)   # Windows
        self.assertIn("bin", src)       # posix


class WorkspaceCredentialTests(unittest.TestCase):
    """The Windows 'couldn't find the .env' fix: the installer writes the
    credential into the venv-parent workspace, which is exactly where the
    console's config chain reads .env from. Cross-platform via expanduser."""

    def setUp(self):
        self.mod = _load()

    def test_env_target_is_the_venv_parent_workspace(self):
        cfg = self.mod.InstallConfig(
            api_key="hp_x", api_key_source="t",
            venv="/opt/box/hugpy-agent/venv").validate()
        target = self.mod._resolve_env_target(cfg)
        self.assertEqual(target,
                         os.path.join("/opt/box/hugpy-agent", ".env"))
        self.assertEqual(os.path.dirname(target),
                         self.mod._workspace_for(cfg.venv))

    def test_write_location_is_in_the_config_read_chain(self):
        """End-to-end: write via the installer, read via the REAL config
        module with HUGPY_WORKSPACE = the launcher's workspace."""
        import sys as _sys
        _sys.path.insert(0, os.path.abspath(os.path.join(
            os.path.dirname(_INSTALLER), "..", "src")))
        from hugpy_agent import config as cfgmod
        tmp = tempfile.mkdtemp()
        venv = os.path.join(tmp, "hugpy-agent", "venv")
        cfg = self.mod.InstallConfig(
            api_key="hp_secret", api_key_source="embedded", venv=venv).validate()
        target = self.mod._resolve_env_target(cfg)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        self.mod._upsert_env(target, self.mod.API_KEY_VAR, cfg.api_key)
        ws = self.mod._workspace_for(cfg.venv)
        loaded = cfgmod.load_config(environ={"HUGPY_WORKSPACE": ws})
        self.assertEqual(loaded.workspace, os.path.dirname(target))
        self.assertEqual(loaded.api_key, "hp_secret")

    def test_expanduser_maps_home_cross_platform(self):
        # ~ expansion is what makes this correct on %USERPROFILE% (Windows).
        cfg = self.mod.InstallConfig(
            api_key="hp_x", api_key_source="t", venv="~/hugpy-agent/venv")
        cfg.validate()
        self.assertNotIn("~", cfg.venv)
        self.assertTrue(os.path.isabs(cfg.venv))


class LauncherTests(unittest.TestCase):
    """The desktop / start-menu launcher opens the terminal console at the
    venv, with the workspace as CWD so the launched agent finds its .env."""

    def setUp(self):
        self.mod = _load()

    def test_linux_desktop_payload_and_headless_guard(self):
        import shutil as _sh
        tmp = tempfile.mkdtemp()
        old = dict(os.environ)
        try:
            # desktop plausible
            os.environ["XDG_DATA_HOME"] = tmp
            os.environ["DISPLAY"] = ":0"
            cfg = self.mod.InstallConfig(
                api_key="hp_x", api_key_source="t",
                venv=os.path.join(tmp, "hugpy-agent", "venv")).validate()
            ctx = self.mod.Context(cfg=cfg)
            self.mod._install_launcher_linux(
                ctx, os.path.join(cfg.venv, "bin", "hugpy-agent"),
                self.mod._workspace_for(cfg.venv))
            ws = self.mod._workspace_for(cfg.venv)
            desktop = os.path.join(tmp, "applications", "hugpy-agent.desktop")
            body = open(desktop).read()
            self.assertIn("Terminal=true", body)
            self.assertIn("Name=hugpy Agent", body)
            self.assertIn("Comment=hugpy fleet terminal console", body)
            self.assertIn("Categories=Development;Utility;", body)
            self.assertNotIn("Icon=", body)   # honest degrade: no asset ships
            # Exec now points at the HOLD-OPEN launcher script (absolute path),
            # NOT the binary directly — dodges .desktop Exec-quoting and holds.
            script = os.path.join(ws, "launch-console.sh")
            self.assertIn(script, body)
            self.assertTrue(os.path.isfile(script))
            self.assertTrue(os.access(script, os.X_OK))
            sbody = open(script).read()
            self.assertIn(f"cd '{ws}'", sbody)                      # cd to workspace
            self.assertIn(f"export HUGPY_WORKSPACE='{ws}'", sbody)  # workspace env
            self.assertIn(os.path.join(cfg.venv, "bin", "hugpy-agent"),
                          sbody)                                    # venv console
            self.assertIn("read -r", sbody)                        # the hold-open
            self.assertIn("Press Enter to close", sbody)
            self.assertIn("console exited with status", sbody)     # readable status
            self.assertIn('exit "$ec"', sbody)                     # exit code preserved
        finally:
            os.environ.clear(); os.environ.update(old)

    def test_launch_script_holds_open_and_preserves_exit_code(self):
        """The generated launcher runs the console, echoes its status, waits
        for Enter, and exits with the console's code — so an OpenCode-absent
        exit-1 shows a readable hint instead of a blip."""
        import subprocess
        ws = tempfile.mkdtemp()
        fake = os.path.join(ws, "fake-console")
        with open(fake, "w") as fh:
            fh.write('#!/bin/sh\necho "install: npm install -g opencode-ai"\n'
                     'exit 1\n')
        os.chmod(fake, 0o755)
        script = self.mod._write_launch_script(ws, fake)
        r = subprocess.run(["/bin/sh", script], input="\n",
                           capture_output=True, text=True)
        self.assertIn("install: npm install -g opencode-ai", r.stdout)
        self.assertIn("console exited with status 1", r.stdout)
        self.assertIn("Press Enter to close", r.stdout)
        self.assertEqual(r.returncode, 1)   # the console's code, preserved

    def test_linux_headless_skips_silently(self):
        tmp = tempfile.mkdtemp()
        old = dict(os.environ)
        try:
            for v in ("XDG_DATA_HOME", "XDG_CURRENT_DESKTOP",
                      "DISPLAY", "WAYLAND_DISPLAY"):
                os.environ.pop(v, None)
            os.environ["HOME"] = tmp   # no pre-existing applications dir
            cfg = self.mod.InstallConfig(
                api_key="hp_x", api_key_source="t",
                venv=os.path.join(tmp, "hugpy-agent", "venv")).validate()
            ctx = self.mod.Context(cfg=cfg)
            self.mod._install_launcher_linux(ctx, "/x/bin/hugpy-agent", "/x/ws")
            self.assertTrue(any("skipped" in n for n in ctx.notes))
            self.assertFalse(os.path.exists(os.path.join(
                tmp, ".local", "share", "applications", "hugpy-agent.desktop")))
        finally:
            os.environ.clear(); os.environ.update(old)

    def test_windows_shortcut_block_shape(self):
        # The Windows launcher must use WScript.Shell CreateShortcut, target a
        # console-persisting cmd /k wrapper, and set WorkingDirectory. We assert
        # the generated payload shape via the source (no COM on Linux CI).
        import inspect
        src = inspect.getsource(self.mod._install_launcher_windows)
        self.assertIn("WScript.Shell", src)
        self.assertIn("CreateShortcut", src)
        self.assertIn("/k", src)                    # keeps the console window open
        self.assertIn("WorkingDirectory", src)
        self.assertIn("hugpy Agent.lnk", src)
        # the console target resolves under the venv's Scripts\ on Windows:
        self.assertIn("Scripts", inspect.getsource(self.mod._console_path))
        # Windows icon: IconLocation = <ico>,0 is added only when the fetch
        # landed (icon_line guarded on the fetched path):
        self.assertIn("IconLocation", src)
        self.assertIn("icon.ico", src)


class IconLauncherTests(unittest.TestCase):
    """The launcher decorates itself with the hugpy mark, fetched from central
    (EMBEDDED_ICON_BASE) at install time. Any fetch failure -> iconless
    launcher (today's behavior)."""

    def setUp(self):
        self.mod = _load()
        self._old_base = self.mod.EMBEDDED_ICON_BASE

    def tearDown(self):
        self.mod.EMBEDDED_ICON_BASE = self._old_base

    def test_icon_base_slot_present_and_empty_by_default(self):
        # Central templates this slot the same way as EMBEDDED_API_KEY.
        with open(_INSTALLER, encoding="utf-8") as fh:
            self.assertIn('EMBEDDED_ICON_BASE = ""', fh.read())
        self.assertEqual(_load().EMBEDDED_ICON_BASE, "")

    def _serve_png(self):
        import functools
        import http.server
        import threading
        srv_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(srv_dir, "agent", "install"))
        with open(os.path.join(srv_dir, "agent", "install", "icon.png"),
                  "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nFAKEDATA")
        handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                    directory=srv_dir)
        httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def test_fetch_icon_success_writes_file(self):
        self.mod.EMBEDDED_ICON_BASE = self._serve_png()
        ws = tempfile.mkdtemp()
        ctx = self.mod.Context(cfg=self.mod.InstallConfig(
            api_key="hp_x", api_key_source="t").validate())
        path = self.mod._fetch_icon(ctx, ws, "hugpy-icon.png", "icon.png")
        self.assertIsNotNone(path)
        with open(path, "rb") as fh:
            self.assertTrue(fh.read().startswith(b"\x89PNG"))

    def test_fetch_icon_no_base_returns_none(self):
        self.mod.EMBEDDED_ICON_BASE = ""
        ws = tempfile.mkdtemp()
        ctx = self.mod.Context(cfg=self.mod.InstallConfig(
            api_key="hp_x", api_key_source="t").validate())
        self.assertIsNone(
            self.mod._fetch_icon(ctx, ws, "hugpy-icon.png", "icon.png"))
        self.assertFalse(os.path.exists(os.path.join(ws, "hugpy-icon.png")))

    def test_fetch_icon_bad_base_degrades(self):
        self.mod.EMBEDDED_ICON_BASE = "http://127.0.0.1:1"   # refused
        ws = tempfile.mkdtemp()
        ctx = self.mod.Context(cfg=self.mod.InstallConfig(
            api_key="hp_x", api_key_source="t").validate())
        self.assertIsNone(
            self.mod._fetch_icon(ctx, ws, "hugpy-icon.png", "icon.png"))

    def test_desktop_gains_icon_only_when_fetch_lands(self):
        old = dict(os.environ)
        try:
            # WITH icon
            base = self._serve_png()
            self.mod.EMBEDDED_ICON_BASE = base
            tmp = tempfile.mkdtemp()
            os.environ["XDG_DATA_HOME"] = tmp
            os.environ["DISPLAY"] = ":0"
            cfg = self.mod.InstallConfig(
                api_key="hp_x", api_key_source="t",
                venv=os.path.join(tmp, "hugpy-agent", "venv")).validate()
            self.mod._install_launcher_linux(
                self.mod.Context(cfg=cfg),
                os.path.join(cfg.venv, "bin", "hugpy-agent"),
                self.mod._workspace_for(cfg.venv))
            body = open(os.path.join(tmp, "applications",
                                     "hugpy-agent.desktop")).read()
            self.assertIn("Icon=", body)
            self.assertIn("hugpy-icon.png", body)

            # WITHOUT icon (no base)
            self.mod.EMBEDDED_ICON_BASE = ""
            tmp2 = tempfile.mkdtemp()
            os.environ["XDG_DATA_HOME"] = tmp2
            cfg2 = self.mod.InstallConfig(
                api_key="hp_x", api_key_source="t",
                venv=os.path.join(tmp2, "hugpy-agent", "venv")).validate()
            self.mod._install_launcher_linux(
                self.mod.Context(cfg=cfg2),
                os.path.join(cfg2.venv, "bin", "hugpy-agent"),
                self.mod._workspace_for(cfg2.venv))
            body2 = open(os.path.join(tmp2, "applications",
                                      "hugpy-agent.desktop")).read()
            self.assertNotIn("Icon=", body2)
        finally:
            os.environ.clear(); os.environ.update(old)


class MacosLauncherTests(unittest.TestCase):
    """The darwin branch builds ~/Applications/hugpy Agent.app whose executable
    opens the shared hold-open script in Terminal.app. Icon via sips->.icns,
    degrading to iconless. All asserted without a real Mac (mock the fetch/sips
    via env + a fake sips on PATH; the function itself is platform-independent).
    """

    def setUp(self):
        self.mod = _load()
        self._old = dict(os.environ)
        self._old_base = self.mod.EMBEDDED_ICON_BASE

    def tearDown(self):
        os.environ.clear(); os.environ.update(self._old)
        self.mod.EMBEDDED_ICON_BASE = self._old_base

    def _cfg(self, home):
        return self.mod.InstallConfig(
            api_key="hp_x", api_key_source="t",
            venv=os.path.join(home, "hugpy-agent", "venv")).validate()

    def test_dispatch_selects_macos_on_darwin(self):
        # step_launcher must branch to the macOS builder when sys.platform is
        # darwin (os.name is 'posix' there too, so the guard is sys.platform).
        import inspect
        src = inspect.getsource(self.mod.step_launcher)
        self.assertIn('sys.platform == "darwin"', src)
        self.assertIn("_install_launcher_macos", src)

    def test_bundle_structure_and_terminal_open(self):
        os.environ.pop("SSH_CONNECTION", None)
        self.mod.EMBEDDED_ICON_BASE = ""      # iconless: no fetch/sips needed
        home = tempfile.mkdtemp()
        os.environ["HOME"] = home
        cfg = self._cfg(home)
        self.mod._install_launcher_macos(
            self.mod.Context(cfg=cfg),
            os.path.join(cfg.venv, "bin", "hugpy-agent"),
            self.mod._workspace_for(cfg.venv))
        app = os.path.join(home, "Applications", "hugpy Agent.app")
        plist = open(os.path.join(app, "Contents", "Info.plist")).read()
        exe_path = os.path.join(app, "Contents", "MacOS", "hugpy-agent-launcher")
        exe = open(exe_path).read()
        # plist essentials
        self.assertIn("<string>hugpy Agent</string>", plist)
        self.assertIn("<string>ai.hugpy.agent</string>", plist)
        self.assertIn("<string>hugpy-agent-launcher</string>", plist)
        self.assertIn("LSMinimumSystemVersion", plist)
        self.assertNotIn("CFBundleIconFile", plist)     # iconless (no base)
        # executable opens the SHARED hold-open script in Terminal.app
        self.assertIn("open -a Terminal", exe)
        self.assertIn("launch-console.sh", exe)
        self.assertTrue(os.access(exe_path, os.X_OK))
        # the shared script really was written (converged with Linux behavior)
        ws = self.mod._workspace_for(cfg.venv)
        self.assertTrue(os.path.isfile(os.path.join(ws, "launch-console.sh")))

    def test_icon_lands_via_sips_and_wires_plist(self):
        # fake icon server + fake sips producing a real .icns
        import functools
        import http.server
        import threading
        srv = tempfile.mkdtemp()
        os.makedirs(os.path.join(srv, "agent", "install"))
        with open(os.path.join(srv, "agent", "install", "icon.png"), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nX")
        handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                    directory=srv)
        httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        self.mod.EMBEDDED_ICON_BASE = f"http://127.0.0.1:{httpd.server_address[1]}"

        bindir = tempfile.mkdtemp()
        sips = os.path.join(bindir, "sips")
        with open(sips, "w") as fh:
            fh.write('#!/bin/sh\nout=""\nwhile [ $# -gt 0 ]; do '
                     'if [ "$1" = "--out" ]; then out="$2"; fi; shift; done\n'
                     'printf icns > "$out"\n')
        os.chmod(sips, 0o755)
        os.environ.pop("SSH_CONNECTION", None)
        os.environ["PATH"] = bindir + os.pathsep + os.environ["PATH"]
        home = tempfile.mkdtemp()
        os.environ["HOME"] = home
        cfg = self._cfg(home)
        self.mod._install_launcher_macos(
            self.mod.Context(cfg=cfg),
            os.path.join(cfg.venv, "bin", "hugpy-agent"),
            self.mod._workspace_for(cfg.venv))
        app = os.path.join(home, "Applications", "hugpy Agent.app")
        self.assertTrue(os.path.isfile(
            os.path.join(app, "Contents", "Resources", "hugpy.icns")))
        plist = open(os.path.join(app, "Contents", "Info.plist")).read()
        self.assertIn("CFBundleIconFile", plist)
        self.assertIn("hugpy.icns", plist)

    def test_sips_command_construction(self):
        import inspect
        src = inspect.getsource(self.mod._install_launcher_macos)
        # the always-present system tool, exact conversion invocation:
        self.assertIn('"-s", "format", "icns"', src)
        self.assertIn('"--out"', src)
        self.assertIn('shutil.which("sips")', src)

    def test_headless_ssh_guard_skips(self):
        os.environ["SSH_CONNECTION"] = "1.2.3.4 5 6.7.8.9 22"
        os.environ.pop("SECURITYSESSIONID", None)
        home = tempfile.mkdtemp()
        os.environ["HOME"] = home
        ctx = self.mod.Context(cfg=self._cfg(home))
        self.mod._install_launcher_macos(ctx, "/x/bin/hugpy-agent", "/x/ws")
        self.assertTrue(any("skipped" in n for n in ctx.notes))
        self.assertFalse(os.path.exists(
            os.path.join(home, "Applications", "hugpy Agent.app")))


class BootstrapShTests(unittest.TestCase):
    """bootstrap.sh (the service-enrollment installer) upgrades pip inside the
    venv it creates, before installing the package."""

    _SH = os.path.abspath(os.path.join(
        os.path.dirname(_INSTALLER), "..", "bootstrap.sh"))

    def _text(self):
        with open(self._SH, encoding="utf-8") as fh:
            return fh.read()

    def test_bash_syntax_ok(self):
        import shutil
        import subprocess
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not available")
        r = subprocess.run([bash, "-n", self._SH],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_venv_pip_upgrade_present(self):
        text = self._text()
        # the -m pip spelling, run against the venv's python:
        self.assertIn('"$PY_BIN" -m pip install --upgrade pip', text)

    def test_pip_upgrade_before_package_install(self):
        text = self._text()
        up = text.index('"$PY_BIN" -m pip install --upgrade pip')
        inst = text.index('"$PIP_BIN" install --upgrade "$SPEC"')
        self.assertLess(up, inst, "pip upgrade must precede the package install")

    def test_desktop_launcher_heredoc_present(self):
        text = self._text()
        self.assertIn("hugpy-agent.desktop", text)
        self.assertIn("Terminal=true", text)
        self.assertIn("Name=hugpy Agent", text)
        self.assertIn("Comment=hugpy fleet terminal console", text)
        self.assertIn("Categories=Development;Utility;", text)
        # Exec points at the launcher SCRIPT, not the binary directly:
        self.assertIn("Exec='${LAUNCH_SCRIPT}'", text)
        self.assertIn("hugpy-agent.desktop", text)
        # the .desktop heredoc body has no Icon= key (no asset ships). There
        # are two heredocs (script, then .desktop) — grab the [Desktop Entry]
        # one and assert on it, not the whole file (a comment mentions "Icon=").
        de = text.index("[Desktop Entry]")
        desktop_body = text[de:text.index("\nEOF", de)]
        self.assertNotIn("Icon=", desktop_body)

    def test_launcher_script_heredoc_holds_open(self):
        text = self._text()
        # the hold-open launcher script the .desktop Exec points at:
        self.assertIn("launch-console.sh", text)
        self.assertIn("${VENV}/bin/hugpy-agent", text)   # venv console
        self.assertIn("cd '${LAUNCH_WS}'", text)          # cd to workspace
        self.assertIn("export HUGPY_WORKSPACE='${LAUNCH_WS}'", text)
        self.assertIn("Press Enter to close", text)       # the hold
        self.assertIn("read -r _", text)
        self.assertIn("console exited with status", text)
        # exit code preserved (escaped in the heredoc as \$ec):
        self.assertIn('exit "\\$ec"', text)

    def test_desktop_launcher_has_headless_guard(self):
        text = self._text()
        self.assertIn("XDG_CURRENT_DESKTOP", text)
        self.assertIn("WAYLAND_DISPLAY", text)
        self.assertIn("skipping .desktop launcher", text)
        # update-desktop-database best-effort:
        self.assertIn("update-desktop-database", text)

    def test_version_floor_gate_with_discovery(self):
        text = self._text()
        # discovery loop over explicit series + known homes:
        self.assertIn("python3.12", text)
        self.assertIn("/opt/homebrew/bin/python3", text)
        self.assertIn("/usr/local/bin/python3", text)
        # honest actionable message, not a bare die:
        self.assertIn("apt install python3.12", text)
        self.assertIn("dnf install python3.12", text)
        self.assertIn("re-run this SAME command", text)
        # the discovered interpreter builds the venv:
        self.assertIn('"$BASE_PY" -m venv "$VENV"', text)

    def test_stale_below_floor_venv_rebuilt(self):
        text = self._text()
        self.assertIn("below the 3.10 floor — rebuilding it", text)
        self.assertIn('rm -rf "$VENV"', text)

    def test_icon_fetch_and_wiring(self):
        text = self._text()
        # fetches the mark from central with curl -fsSL, into the workspace:
        self.assertIn("${CENTRAL}/agent/install/icon.png", text)
        self.assertIn("curl -fsSL", text)
        self.assertIn("hugpy-icon.png", text)
        # Icon= line only when the fetch landed (guarded via ICON_LINE):
        self.assertIn("ICON_LINE=", text)
        self.assertIn('Icon=${ICON_DST}', text)
        self.assertIn("${ICON_LINE}", text)   # interpolated into the .desktop


class KeyPrecedenceTests(unittest.TestCase):
    """resolve_api_key: --api-key > embedded > env > ./.env > ~/.env."""

    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old_cwd = os.getcwd()
        self._old_home = os.environ.get("HOME")
        self._old_env_key = os.environ.pop(self.mod.API_KEY_VAR, None)
        self.home = os.path.join(self.tmp.name, "home")
        self.cwd = os.path.join(self.tmp.name, "cwd")
        os.makedirs(self.home)
        os.makedirs(self.cwd)
        os.environ["HOME"] = self.home
        os.chdir(self.cwd)

    def tearDown(self):
        os.chdir(self._old_cwd)
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        if self._old_env_key is not None:
            os.environ[self.mod.API_KEY_VAR] = self._old_env_key
        else:
            os.environ.pop(self.mod.API_KEY_VAR, None)

    def _write(self, path, key):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"{self.mod.API_KEY_VAR}={key}\n")

    def test_cli_beats_everything(self):
        self.mod.EMBEDDED_API_KEY = "hp_embedded"
        os.environ[self.mod.API_KEY_VAR] = "hp_environ"
        self._write(os.path.join(self.cwd, ".env"), "hp_cwd")
        key, source = self.mod.resolve_api_key("hp_cli")
        self.assertEqual((key, source), ("hp_cli", "--api-key"))

    def test_embedded_beats_env_and_files(self):
        self.mod.EMBEDDED_API_KEY = "hp_embedded"
        os.environ[self.mod.API_KEY_VAR] = "hp_environ"
        self._write(os.path.join(self.cwd, ".env"), "hp_cwd")
        key, source = self.mod.resolve_api_key("")
        self.assertEqual((key, source), ("hp_embedded", "embedded"))

    def test_environ_beats_files(self):
        os.environ[self.mod.API_KEY_VAR] = "hp_environ"
        self._write(os.path.join(self.cwd, ".env"), "hp_cwd")
        key, source = self.mod.resolve_api_key("")
        self.assertEqual(key, "hp_environ")
        self.assertEqual(source, "environment")

    def test_cwd_env_beats_home_env(self):
        self._write(os.path.join(self.cwd, ".env"), "hp_cwd")
        self._write(os.path.join(self.home, ".env"), "hp_home")
        key, source = self.mod.resolve_api_key("")
        self.assertEqual(key, "hp_cwd")
        self.assertIn("./.env", source)

    def test_home_env_is_the_last_resort(self):
        self._write(os.path.join(self.home, ".env"), "hp_home")
        key, source = self.mod.resolve_api_key("")
        self.assertEqual(key, "hp_home")
        self.assertIn("~/.env", source)

    def test_nothing_resolves_unresolved(self):
        key, source = self.mod.resolve_api_key("")
        self.assertEqual((key, source), ("", "unresolved"))

    def test_validate_refuses_without_a_key(self):
        cfg = self.mod.InstallConfig(api_key="", api_key_source="unresolved")
        with self.assertRaises(SystemExit):
            cfg.validate()


class FindEditableRootTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old_cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self._old_cwd)

    def test_explicit_hint_with_marker(self):
        repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(repo)
        open(os.path.join(repo, "pyproject.toml"), "w").close()
        os.chdir(self.tmp.name)
        self.assertEqual(self.mod.find_editable_root(repo),
                         os.path.abspath(repo))

    def test_cwd_marker_found(self):
        os.chdir(self.tmp.name)
        open(os.path.join(self.tmp.name, "setup.py"), "w").close()
        self.assertEqual(self.mod.find_editable_root(""),
                         os.path.abspath(self.tmp.name))

    def test_nothing_found_is_empty(self):
        bare = os.path.join(self.tmp.name, "bare", "deep")
        os.makedirs(bare)
        os.chdir(bare)
        # NB: the walk also inspects the directory holding the installer
        # itself; the hugpy_agent repo root HAS pyproject.toml one level above
        # install/, which find_editable_root's script_dir candidates do not
        # reach (it looks at script_dir, script_dir/hugpy_agent, and the
        # PARENT of script_dir — the parent IS the repo root). So from a bare
        # cwd it legitimately finds the repo. Assert that honest behavior:
        root = self.mod.find_editable_root("")
        repo = os.path.abspath(os.path.join(os.path.dirname(_INSTALLER), ".."))
        self.assertIn(root, ("", repo))


class EnvUpsertTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = os.path.join(self.tmp.name, ".env")

    def _read(self):
        with open(self.env, encoding="utf-8") as fh:
            return fh.read()

    def test_creates_missing_file(self):
        self.mod._upsert_env(self.env, "HUGPY_API_KEY", "hp_a")
        self.assertEqual(self._read(), "HUGPY_API_KEY=hp_a\n")

    def test_replaces_existing_line_never_duplicates(self):
        with open(self.env, "w", encoding="utf-8") as fh:
            fh.write("OTHER=1\nHUGPY_API_KEY=hp_old\nMORE=2\n")
        self.mod._upsert_env(self.env, "HUGPY_API_KEY", "hp_new")
        text = self._read()
        self.assertIn("HUGPY_API_KEY=hp_new\n", text)
        self.assertNotIn("hp_old", text)
        self.assertEqual(text.count("HUGPY_API_KEY="), 1)
        self.assertIn("OTHER=1", text)
        self.assertIn("MORE=2", text)

    def test_appends_when_absent_preserving_content(self):
        with open(self.env, "w", encoding="utf-8") as fh:
            fh.write("OTHER=1")           # no trailing newline on purpose
        self.mod._upsert_env(self.env, "HUGPY_API_KEY", "hp_x")
        self.assertEqual(self._read(), "OTHER=1\nHUGPY_API_KEY=hp_x\n")


if __name__ == "__main__":
    unittest.main()
