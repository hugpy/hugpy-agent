#!/usr/bin/env python3
"""hugpy-agent bootstrap installer.

One entrypoint, three platforms (Windows / macOS / Linux). It is deliberately
"light python over api calls": every real action is a subprocess to pip / npm /
the hugpy-agent console. Nothing here is clever, which is the point -- you should
be able to read the whole thing later and not be betrayed by it.

Design notes (matches how the rest of the fleet is wired):
  * queue over callbacks     -> steps run out of a deque, one after another.
  * registry over globals     -> steps register themselves into STEP_REGISTRY;
                                 order of registration IS order of execution.
  * schema over ad-hoc dicts  -> InstallConfig / Context are typed dataclasses,
                                 validated once, then passed explicitly.
  * explicit env wiring       -> the API key + PATH are wired into os.environ
                                 on purpose, never inferred by a "smart default".

Usage:
    # the secure one-time install link (console -> API access -> install links):
    curl -fsSL https://dev.hugpy.ai/api/agent/install/<link_id>.sh | bash
    # (that download bakes a freshly-minted scoped key into EMBEDDED_API_KEY)

    python install_hugpy_agent.py --api-key hgp_xxx
    python install_hugpy_agent.py                 # resolve key from ./.env or ~/.env
    python install_hugpy_agent.py --no-launch      # set up but don't open the console
    python install_hugpy_agent.py --offline        # launch console without the model sync

Key resolution precedence (first hit wins):
    1. --api-key on the command line
    2. EMBEDDED_API_KEY baked into this file
       (this is the slot a secure hugpy one-time-download would fill per user)
    3. HUGPY_API_KEY already exported in the environment
    4. HUGPY_API_KEY= in ./.env   (the launch directory)
    5. HUGPY_API_KEY= in ~/.env   (the home fallback)
"""

import argparse
import collections
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# The prepackaged / one-time-download build overwrites this line with a freshly
# minted key for the user. Left blank, the installer falls back to .env lookup.
EMBEDDED_API_KEY = ""
# Central templates the public base URL here (same slot idiom as the key) so
# the launcher step can fetch the hugpy mark from ``<base>/agent/install/
# icon.png|.ico``. Left blank (direct runs), the icon fetch is skipped and the
# launcher is written iconless — today's behavior.
EMBEDDED_ICON_BASE = ""
# --------------------------------------------------------------------------- #

API_KEY_VAR = "HUGPY_API_KEY"
PYPI_NAME = "hugpy_agent"
OPENCODE_PKG = "opencode-ai"
CONSOLE_CMD = "hugpy-agent"

# Where we create a venv when the installer is run against a system python
# (the install-link one-liner curls into distro python3). This mirrors
# bootstrap.sh's ``~/hugpy-agent/venv`` so the two install paths CONVERGE on
# one location. Overridable via --venv or HUGPY_AGENT_VENV.
VENV_ENV_VAR = "HUGPY_AGENT_VENV"
DEFAULT_VENV = os.path.join(os.path.expanduser("~"), "hugpy-agent", "venv")
# The agent reads its .env from its WORKSPACE (config.py: HUGPY_WORKSPACE or
# cwd), NEVER from ~/.env. So the credential must land in a workspace the
# launched console will actually use. We make that deterministic: the venv's
# parent dir (~/hugpy-agent), and point every launcher's HUGPY_WORKSPACE /
# WorkingDirectory at it. This is what fixes the Windows "couldn't find the
# .env" failure — the installer previously wrote .env into its transient
# launch/temp dir (or the dead-drop ~/.env), which the console never reads.
def _workspace_for(venv_abs):
    return os.path.dirname(os.path.abspath(venv_abs))
# Set on the child process after we re-exec into the venv, so the child does
# NOT try to create/re-exec again (belt to the sys.prefix check).
_REEXEC_FLAG = "HUGPY_AGENT_VENV_REEXEC"


# --------------------------------------------------------------------------- #
# schemas
# --------------------------------------------------------------------------- #
@dataclass
class InstallConfig:
    """Everything the run needs, validated once up front."""

    api_key: str
    api_key_source: str
    package_spec: str = PYPI_NAME     # PyPI requirement string
    editable_hint: str = ""           # explicit local repo path (skips PyPI try)
    npm_prefix: str = ""              # computed if empty
    env_target: str = ""              # computed if empty
    venv: str = ""                    # venv to create/use when not already in one
    launch: bool = True
    offline: bool = False
    persist_path: bool = True
    install_opencode: bool = True

    def validate(self):
        if not self.api_key or not self.api_key.strip():
            raise SystemExit(
                f"no {API_KEY_VAR} resolved.\n"
                "  pass --api-key hgp_xxx, or put "
                f"{API_KEY_VAR}=... in ./.env or ~/.env"
            )
        self.api_key = self.api_key.strip()
        if not self.npm_prefix:
            self.npm_prefix = os.path.join(os.path.expanduser("~"), ".npm-global")
        if not self.venv:
            self.venv = os.environ.get(VENV_ENV_VAR, "") or DEFAULT_VENV
        self.venv = os.path.abspath(os.path.expanduser(self.venv))
        return self


@dataclass
class Context:
    """Mutable state threaded through the step queue -- no module globals."""

    cfg: InstallConfig
    npm_bin: str = ""
    # Interpreter used to CREATE the venv. Normally sys.executable; the version
    # gate replaces it when the running python is below the floor.
    base_python: str = ""
    notes: list = field(default_factory=list)

    def note(self, msg):
        self.notes.append(msg)


# --------------------------------------------------------------------------- #
# step registry + tiny shell helpers
# --------------------------------------------------------------------------- #
STEP_REGISTRY = []  # list[(name, fn)] -- registration order == run order


def step(name):
    def deco(fn):
        STEP_REGISTRY.append((name, fn))
        return fn
    return deco


def log(msg):
    print(f"[install] {msg}", flush=True)


def run(cmd, **kw):
    """Echo then run a subprocess, inheriting stdio. Returns the CompletedProcess."""
    printable = " ".join(cmd if isinstance(cmd, list) else [cmd])
    log(f"$ {printable}")
    return subprocess.run(cmd, **kw)


def read_env_file(path):
    """Parse a dotenv file into a plain dict. Missing file -> {}."""
    out = {}
    if not path or not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def npm_global_bin(prefix):
    # On Windows npm drops the .cmd shims directly in the prefix; on Unix in bin/.
    return prefix if os.name == "nt" else os.path.join(prefix, "bin")


# --------------------------------------------------------------------------- #
# key resolution (runs before the config schema is built)
# --------------------------------------------------------------------------- #
def resolve_api_key(cli_key):
    cwd_env = os.path.join(os.getcwd(), ".env")
    home_env = os.path.join(os.path.expanduser("~"), ".env")
    candidates = [
        ("--api-key", cli_key),
        ("embedded", EMBEDDED_API_KEY),
        ("environment", os.environ.get(API_KEY_VAR, "")),
        (f"./.env ({cwd_env})", read_env_file(cwd_env).get(API_KEY_VAR, "")),
        (f"~/.env ({home_env})", read_env_file(home_env).get(API_KEY_VAR, "")),
    ]
    for source, value in candidates:
        if value and value.strip():
            return value.strip(), source
    return "", "unresolved"


# --------------------------------------------------------------------------- #
# repo detection for the editable fallback
# --------------------------------------------------------------------------- #
def find_editable_root(hint):
    """Return a dir holding a python project marker, or '' if none found."""
    markers = ("pyproject.toml", "setup.py", "setup.cfg")
    here = os.getcwd()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    seen = []
    bases = [hint] if hint else []
    for base in (here, script_dir):
        bases.extend([base, os.path.join(base, PYPI_NAME), os.path.dirname(base)])
    for cand in bases:
        if not cand or cand in seen:
            continue
        seen.append(cand)
        if any(os.path.isfile(os.path.join(cand, m)) for m in markers):
            return os.path.abspath(cand)
    return ""


# --------------------------------------------------------------------------- #
# python version floor + interpreter discovery
# --------------------------------------------------------------------------- #
# Every hugpy-agent release requires >= 3.10. The install-link one-liner runs
# whatever `python3` the box has: on a stock Mac that is the Xcode CLT 3.9
# (/Library/Developer/CommandLineTools/usr/bin/python3), which pip reports only
# as a cryptic "No matching distribution found" (it silently ignores every
# release for Requires-Python). Field report 2026-07-24 (a real fresh MacBook).
# So we gate EARLY, try to FIND a good interpreter, and fail honestly if not.
MIN_PY = (3, 10)
# Newest first: we want the best available, not the oldest acceptable.
_PY_SERIES = ("3.14", "3.13", "3.12", "3.11", "3.10")
# Known install homes to probe beyond PATH.
_PY_HOME_DIRS = (
    "/opt/homebrew/bin",      # Apple-silicon Homebrew
    "/usr/local/bin",         # Intel Homebrew / python.org symlinks / generic
    "/usr/bin",               # distro python (linux)
)
# python.org framework installs (glob per version dir).
_PY_FRAMEWORK_GLOB = "/Library/Frameworks/Python.framework/Versions/*/bin"


def _version_of(exe):
    """(major, minor) of an interpreter, or None if it won't report one."""
    try:
        out = subprocess.run(
            [exe, "-c",
             "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return None
        parts = (out.stdout or "").strip().split(".")
        return (int(parts[0]), int(parts[1])) if len(parts) >= 2 else None
    except Exception:  # noqa: BLE001 — an unusable candidate is just skipped
        return None


def _python_candidates():
    """Ordered candidate interpreters: explicit series on PATH first, then the
    same series inside the known install homes, then python.org frameworks.
    Deduplicated, existence-checked; version is verified by the caller."""
    import glob
    seen, out = set(), []

    def add(p):
        if p and p not in seen:
            seen.add(p)
            out.append(p)

    for series in _PY_SERIES:                      # python3.13, python3.12, …
        add(shutil.which(f"python{series}"))
    for d in _PY_HOME_DIRS:                        # /opt/homebrew/bin/python3.12
        for series in _PY_SERIES:
            cand = os.path.join(d, f"python{series}")
            if os.path.isfile(cand):
                add(cand)
        cand = os.path.join(d, "python3")          # a home's generic python3
        if os.path.isfile(cand):
            add(cand)
    for bindir in sorted(glob.glob(_PY_FRAMEWORK_GLOB), reverse=True):
        for name in [f"python{s}" for s in _PY_SERIES] + ["python3"]:
            cand = os.path.join(bindir, name)
            if os.path.isfile(cand):
                add(cand)
    return out


def _discover_python(minimum=MIN_PY):
    """First candidate interpreter meeting the floor, as (path, (maj, min)),
    or (None, None). Never returns the running interpreter's path implicitly —
    the caller has already checked sys.version_info."""
    for cand in _python_candidates():
        ver = _version_of(cand)
        if ver and ver >= minimum:
            return cand, ver
    return None, None


def _version_floor_message(current):
    """The honest, actionable, platform-aware failure. Printed BEFORE anything
    is created, and never followed by pip's cryptic version-ignore error."""
    need = "%d.%d" % MIN_PY
    cur = "%d.%d" % current
    if sys.platform == "darwin":
        xcode = "/Library/Developer/CommandLineTools" in (sys.executable or "")
        which = " — the Xcode Command Line Tools python" if xcode else ""
        return (f"python {cur} is too old — hugpy-agent needs >= {need}.\n"
                f"  this interpreter: {sys.executable}{which}\n"
                "  Install a newer python, then re-run this SAME command:\n"
                "    brew install python          # Homebrew (recommended)\n"
                "    # or download an installer from https://python.org\n"
                "  No other change is needed — the installer finds it "
                "automatically.")
    head = (f"python {cur} is too old — hugpy-agent needs >= {need}.\n"
            f"  this interpreter: {sys.executable}\n")
    if os.name == "nt":
        return (head +
                "  Install python >= " + need + " from https://python.org "
                "(tick 'Add python.exe to PATH'),\n"
                "  then re-run this SAME command.")
    return (head +
            "  Install a newer python, then re-run this SAME command:\n"
            "    sudo apt install python3.12 python3.12-venv   # Debian/Ubuntu\n"
            "    sudo dnf install python3.12                   # Fedora/RHEL\n"
            "  No other change is needed — the installer finds it "
            "automatically.")


# --------------------------------------------------------------------------- #
# steps -- registered in execution order
# --------------------------------------------------------------------------- #
def _in_venv():
    """True when this interpreter is an isolated venv/virtualenv, not the
    distro/system python. We ONLY self-upgrade pip inside a venv — a
    system/PEP-668-managed python must never have its pip touched here."""
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _venv_python(venv_abs):
    """Absolute path to a venv's interpreter, per-platform."""
    if os.name == "nt":
        return os.path.join(venv_abs, "Scripts", "python.exe")
    return os.path.join(venv_abs, "bin", "python")


@step("check python version floor")
def step_python_floor(ctx):
    """FIRST step: enforce the >= 3.10 floor before ANYTHING is created.

    If the running python is too old we do not give up — we SEARCH for a
    suitable interpreter (PATH series, Homebrew/usr-local/usr-bin, python.org
    frameworks) and hand it to the venv step as the base interpreter. Only when
    nothing on the box qualifies do we exit, with a platform-aware actionable
    message and NOTHING created. This pre-empts pip's cryptic 'No matching
    distribution found' (it silently ignores every release whose
    Requires-Python we miss) — the 3.9-Xcode-CLT Mac field report."""
    if sys.version_info[:2] >= MIN_PY:
        ctx.base_python = sys.executable
        return
    # Already inside a venv built on an old python: re-execing won't help and
    # the venv itself is the problem — fail with the same honest message.
    current = sys.version_info[:2]
    log(f"this python is {current[0]}.{current[1]}; hugpy-agent needs "
        f">= {MIN_PY[0]}.{MIN_PY[1]} — searching for a newer interpreter")
    found, ver = _discover_python()
    if not found:
        # Exit BEFORE creating a venv / writing anything.
        raise SystemExit(_version_floor_message(current))
    log(f"system python is {current[0]}.{current[1]}; using {found} "
        f"({ver[0]}.{ver[1]})")
    ctx.base_python = found
    ctx.note(f"base interpreter switched to {found} ({ver[0]}.{ver[1]}) — "
             f"system python was {current[0]}.{current[1]}")


@step("create venv (system python only)")
def step_venv(ctx):
    """The install-link one-liner curls into the SYSTEM python (distro
    python3 / the Windows launcher). On modern Ubuntu that python is
    PEP-668-managed: ``pip install`` REFUSES (externally-managed-environment)
    — the default install promise is broken. Windows has no venv either, so
    the credential and console land in inconsistent, transient locations.

    Fix: if we are NOT already in a venv, create one at cfg.venv (default
    ~/hugpy-agent/venv — the SAME location bootstrap.sh uses, so the two
    install paths converge) and RE-EXEC this exact script with that venv's
    python. The re-executed process IS in the venv, so every subsequent step
    (pip upgrade, package install, launcher) targets one deterministic
    interpreter. Already in a venv → no-op (unchanged behavior)."""
    # No-op only when the venv we are ALREADY in meets the version floor. An
    # activated-but-too-old venv must not short-circuit us into installing
    # against it (that is the 3.9 failure wearing a venv).
    if (_in_venv() or os.environ.get(_REEXEC_FLAG)) \
            and sys.version_info[:2] >= MIN_PY:
        ctx.note(f"already in a venv ({sys.prefix}) — no venv created")
        return
    venv_abs = ctx.cfg.venv
    py = _venv_python(venv_abs)
    # Base interpreter: whatever the version gate approved (sys.executable
    # normally; a discovered newer python when the system one was too old).
    base = ctx.base_python or sys.executable
    # A venv left behind by an EARLIER failed run can itself be below the floor
    # (the 3.9-Mac field report leaves exactly that). Reusing it would re-exec
    # straight back into the too-old interpreter and fail identically, so a
    # stale venv is rebuilt from the approved base rather than reused.
    if os.path.isfile(py):
        existing = _version_of(py)
        if existing and existing < MIN_PY:
            log(f"existing venv at {venv_abs} is python "
                f"{existing[0]}.{existing[1]} (below the "
                f"{MIN_PY[0]}.{MIN_PY[1]} floor) — rebuilding it with {base}")
            shutil.rmtree(venv_abs, ignore_errors=True)
            ctx.note(f"rebuilt stale python {existing[0]}.{existing[1]} venv")
    if not os.path.isfile(py):
        log(f"creating venv at {venv_abs} with {base} (system python is not "
            "installable into directly on PEP-668 distros)")
        try:
            run([base, "-m", "venv", venv_abs], check=True)
        except subprocess.CalledProcessError as exc:
            raise SystemExit(
                f"failed to create a venv at {venv_abs}: {exc}\n"
                "  install the python venv module (e.g. 'sudo apt install "
                "python3-venv') and re-run.")
    else:
        log(f"venv already present at {venv_abs} — reusing it")
    # Re-exec into the venv. Pass a flag so the child never loops back here,
    # and carry the resolved key through the environment so the child resolves
    # it without re-reading a (possibly transient) .env.
    child_env = dict(os.environ)
    child_env[_REEXEC_FLAG] = "1"
    if ctx.cfg.api_key:
        child_env[API_KEY_VAR] = ctx.cfg.api_key
    log(f"re-exec into {py} to run the rest inside the venv")
    proc = subprocess.run([py, os.path.abspath(__file__)] + sys.argv[1:],
                          env=child_env)
    raise SystemExit(proc.returncode)


@step("upgrade pip (venv only)")
def step_pip_upgrade(ctx):
    """Distro pips from the 20.x era choke on modern wheels / PEP 517
    pyproject builds, making a first install look broken. Upgrade pip BEFORE
    installing the package. After step_venv we are ALWAYS in a venv, so this
    always applies now (that is the point — it fixes the PEP-668 refusal). The
    `-m pip` spelling is mandatory (Windows pip.exe cannot replace itself in
    place). A network / proxy failure is non-fatal: one visible warning, then
    continue with the existing (old-but-working) pip. Idempotent: newest is a
    no-op. The venv guard remains as a defensive belt: if a caller somehow
    reaches here on a system python, we must NOT touch its pip (PEP 668)."""
    if not _in_venv():
        # Defensive: step_venv guarantees a venv, so this should not be hit.
        # A system/PEP-668 python's pip must never be touched here.
        log("not in a venv -- leaving the system pip untouched (PEP 668). "
            "If the package install fails on an old pip, install into a venv.")
        ctx.note("pip upgrade skipped (system python, not a venv)")
        return
    before = _pip_version()
    log(f"pip before: {before}")
    proc = run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])
    if proc.returncode == 0:
        after = _pip_version()
        log(f"pip after:  {after}")
        ctx.note(f"pip upgraded in venv: {before} -> {after}")
    else:
        log("WARNING: pip self-upgrade failed (network/proxy?); continuing "
            f"with the existing pip {before}.")
        ctx.note(f"pip upgrade failed -- continuing with {before}")


def _pip_version():
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True, text=True)
        return (out.stdout or out.stderr or "").strip() or "unknown"
    except Exception:
        return "unknown"


def _looks_like_requires_python(text):
    """True when a pip failure is a PYTHON VERSION mismatch rather than a
    genuine 'package/repo not found'. pip words this several ways depending on
    version: an explicit 'Requires-Python' note per candidate, or just
    'No matching distribution found' after 'ignored the following versions'."""
    t = (text or "").lower()
    if "requires-python" in t:
        return True
    if "requires a different python" in t:
        return True
    # pip >= 21 prints "Ignored the following versions that require a different
    # python version" then "No matching distribution found for <pkg>".
    if "ignored the following versions" in t and "no matching distribution" in t:
        return True
    # Last resort: our own floor is unmet, and pip found nothing at all — on a
    # sub-floor interpreter that combination IS the version problem.
    if "no matching distribution" in t and sys.version_info[:2] < MIN_PY:
        return True
    return False


@step("pip install hugpy_agent")
def step_pip(ctx):
    cfg = ctx.cfg
    pip = [sys.executable, "-m", "pip", "install"]

    if cfg.editable_hint:
        root = find_editable_root(cfg.editable_hint)
        if not root:
            raise SystemExit(f"--editable path has no project marker: {cfg.editable_hint}")
        log(f"editable install from {root}")
        run(pip + ["-e", root], check=True)
        ctx.note(f"hugpy_agent installed editable from {root}")
        return

    # try PyPI first, fall back to a detected local repo. Capture the output so
    # we can classify the failure (and still show it — nothing is swallowed).
    proc = run(pip + [cfg.package_spec], capture_output=True, text=True)
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode == 0:
        ctx.note(f"hugpy_agent installed from PyPI ({cfg.package_spec})")
        return

    # Requires-Python mismatch: pip reports this as a bare "No matching
    # distribution found", and a local-repo/--editable suggestion would be
    # actively MISLEADING (a local install fails the same way). Say the real
    # thing instead. The early gate normally pre-empts this path entirely;
    # this is the belt for e.g. an already-activated too-old venv.
    blob = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    if _looks_like_requires_python(blob):
        raise SystemExit(_version_floor_message(sys.version_info[:2]))

    log("PyPI install failed -- looking for a local repo to install editable")
    root = find_editable_root("")
    if not root:
        raise SystemExit(
            "could not install from PyPI and found no local project "
            "(pyproject.toml / setup.py) near the cwd or this script.\n"
            "  re-run from the hugpy_agent repo, or pass --editable /path/to/repo"
        )
    run(pip + ["-e", root], check=True)
    ctx.note(f"hugpy_agent installed editable from {root} (PyPI fallback)")


@step("npm install -g opencode-ai")
def step_npm(ctx):
    cfg = ctx.cfg
    ctx.npm_bin = npm_global_bin(cfg.npm_prefix)
    if not cfg.install_opencode:
        ctx.note("skipped opencode install (--no-opencode)")
        return
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        ctx.note("npm not found -- opencode (optional peer) NOT installed")
        log("npm not on PATH; skipping opencode. hugpy-agent still works without it.")
        return
    run([npm, "config", "set", "prefix", cfg.npm_prefix], check=True)
    proc = run([npm, "install", "-g", OPENCODE_PKG])
    if proc.returncode != 0:
        ctx.note("opencode install failed (optional peer) -- continuing")
        log("opencode install failed; it is optional, so continuing.")
        return
    ctx.note(f"opencode-ai installed under {cfg.npm_prefix}")


@step("persist PATH for the npm global bin")
def step_path(ctx):
    cfg = ctx.cfg
    bin_dir = ctx.npm_bin or npm_global_bin(cfg.npm_prefix)

    # wire it into THIS process so the launch below can find opencode.
    cur = os.environ.get("PATH", "")
    if bin_dir not in cur.split(os.pathsep):
        os.environ["PATH"] = bin_dir + os.pathsep + cur

    if not cfg.persist_path:
        ctx.note("PATH not persisted (--no-path)")
        return

    if os.name == "nt":
        changed = _persist_path_windows(bin_dir)
        ctx.note(
            f"added {bin_dir} to your user PATH (open a NEW terminal to pick it up)"
            if changed else f"{bin_dir} already on your user PATH"
        )
    else:
        written = _persist_path_unix(bin_dir)
        if written:
            ctx.note("PATH export appended to: " + ", ".join(written))
        else:
            ctx.note(f"{bin_dir} already exported in your shell rc")


def _persist_path_windows(bin_dir):
    import winreg  # stdlib on Windows only

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0,
        winreg.KEY_READ | winreg.KEY_WRITE,
    ) as key:
        try:
            cur, _ = winreg.QueryValueEx(key, "PATH")
        except FileNotFoundError:
            cur = ""
        parts = [p for p in cur.split(os.pathsep) if p]
        if bin_dir in parts:
            return False
        parts.append(bin_dir)
        winreg.SetValueEx(key, "PATH", 0, winreg.REG_EXPAND_SZ, os.pathsep.join(parts))
        return True


def _persist_path_unix(bin_dir):
    home = os.path.expanduser("~")
    export_line = f'export PATH="{bin_dir}:$PATH"'
    block = f"\n# added by hugpy-agent installer\n{export_line}\n"
    written = []
    existing = [os.path.join(home, rc) for rc in (".bashrc", ".zshrc", ".profile")
                if os.path.isfile(os.path.join(home, rc))]
    for target in existing:
        with open(target, "r", encoding="utf-8") as fh:
            if bin_dir in fh.read():
                continue
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(block)
        written.append(target)
    if not written and not existing:
        target = os.path.join(home, ".profile")
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(block)
        written.append(target)
    return written


@step("write HUGPY_API_KEY to .env")
def step_env(ctx):
    cfg = ctx.cfg
    target = _resolve_env_target(cfg)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    _upsert_env(target, API_KEY_VAR, cfg.api_key)
    # wire into this process too, so the console launch is already authorized,
    # AND point the console at the workspace that .env lives in — the console
    # reads .env from its workspace (HUGPY_WORKSPACE or cwd), never from ~/.env.
    os.environ[API_KEY_VAR] = cfg.api_key
    os.environ.setdefault("HUGPY_WORKSPACE", os.path.dirname(target))
    ctx.note(f"{API_KEY_VAR} written to {target} (source: {cfg.api_key_source})")
    ctx.note(f"workspace = {os.path.dirname(target)} (the console reads .env here)")


def _resolve_env_target(cfg):
    """Where to WRITE the credential .env so the launched console READS it.

    The console resolves .env from its workspace only (config.py). Since the
    installer now always operates inside a venv it created (or was already in),
    the deterministic workspace is that venv's parent dir — the SAME location
    the launchers below set as HUGPY_WORKSPACE / WorkingDirectory. This is
    cross-platform: expanduser maps ~ to %USERPROFILE% on Windows. An explicit
    --env-target still wins (operator intent), and a pre-existing workspace
    .env is reused in place."""
    if cfg.env_target:
        return os.path.abspath(cfg.env_target)
    ws = _workspace_for(cfg.venv)
    return os.path.join(ws, ".env")


def _upsert_env(path, key, value):
    """Replace an existing KEY= line or append one; never duplicate."""
    lines, found = [], False
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip().startswith(key + "="):
                    lines.append(f"{key}={value}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={value}\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)


def _console_path(cfg):
    """Absolute path to the venv's hugpy-agent console entrypoint. Since the
    installer always operates inside a venv (created above when needed), the
    console is deterministically inside it — no PATH guessing for the launcher
    target. Falls back to shutil.which if, unexpectedly, it isn't there yet."""
    if os.name == "nt":
        cand = os.path.join(cfg.venv, "Scripts", CONSOLE_CMD + ".exe")
    else:
        cand = os.path.join(cfg.venv, "bin", CONSOLE_CMD)
    if os.path.isfile(cand):
        return cand
    return shutil.which(CONSOLE_CMD) or shutil.which(CONSOLE_CMD + ".exe") or cand


@step("install desktop / start-menu launcher")
def step_launcher(ctx):
    """Register a real application launcher that opens the TERMINAL console.
    Linux: a ~/.local/share/applications .desktop (Terminal=true) — skipped
    silently on a headless box. macOS: a ~/Applications/hugpy Agent.app bundle
    that opens the console in Terminal.app. Windows: a Start-Menu .lnk via
    WScript.Shell. All target the venv console via the hold-open launcher
    script and set the workspace as CWD so the launched agent finds its .env.
    Idempotent (overwrites the same files)."""
    cfg = ctx.cfg
    console = _console_path(cfg)
    workspace = _workspace_for(cfg.venv)
    if os.name == "nt":
        _install_launcher_windows(ctx, console, workspace)
    elif sys.platform == "darwin":
        _install_launcher_macos(ctx, console, workspace)
    else:
        _install_launcher_linux(ctx, console, workspace)


def _fetch_icon(ctx, workspace, filename, leaf):
    """Download <base>/agent/install/<leaf> into <workspace>/<filename> and
    return its absolute path, or None on ANY failure (no base templated, no
    network, bad response). Iconless launchers on failure are today's behavior
    — the icon is a nicety, never a gate. Idempotent overwrite."""
    base = (EMBEDDED_ICON_BASE or "").strip().rstrip("/")
    if not base:
        return None
    url = f"{base}/agent/install/{leaf}"
    dest = os.path.join(workspace, filename)
    try:
        import urllib.request
        os.makedirs(workspace, exist_ok=True)
        with urllib.request.urlopen(url, timeout=15) as r:
            if getattr(r, "status", 200) not in (200, None):
                raise OSError(f"HTTP {r.status}")
            data = r.read()
        if not data:
            raise OSError("empty icon response")
        with open(dest, "wb") as fh:
            fh.write(data)
        log(f"fetched launcher icon: {url} -> {dest}")
        return dest
    except Exception as exc:  # noqa: BLE001 — any failure degrades to iconless
        log(f"launcher icon fetch skipped ({url}): {exc}")
        return None


_DESKTOP_TEMPLATE = """[Desktop Entry]
Type=Application
Version=1.0
Name=hugpy Agent
Comment=hugpy fleet terminal console
Exec={exec_cmd}
Terminal=true
Categories=Development;Utility;
"""


# The Linux HOLD-OPEN launcher. Under Terminal=true a .desktop has no cmd /k
# equivalent — the terminal closes the instant the process exits, so a console
# that prints the "install OpenCode" hint and exits 1 just BLIPS (operator
# field report 2026-07-24). We interpose a script that ALWAYS holds: it runs
# the console, captures the exit code, then waits for Enter — so the final
# output (hint on failure, or a clean exit) stays readable, matching the
# Windows cmd /k behavior. Pointing .desktop Exec at an absolute script path
# also dodges .desktop's fragile Exec-quoting entirely.
_LAUNCH_SCRIPT = """#!/bin/sh
# hugpy-agent terminal console launcher (generated by the installer).
# Holds the terminal open after the console exits so its final output — e.g.
# the "install OpenCode" hint on a box without it — stays readable.
export HUGPY_WORKSPACE={workspace_q}
cd {workspace_q} || exit 1
{console_q} console
ec=$?
echo
echo "[hugpy-agent console exited with status $ec]"
printf "Press Enter to close..."
read -r _
exit "$ec"
"""


def _write_launch_script(workspace, console):
    """Write <workspace>/launch-console.sh (chmod 0755) and return its path."""
    os.makedirs(workspace, exist_ok=True)
    path = os.path.join(workspace, "launch-console.sh")
    body = _LAUNCH_SCRIPT.format(workspace_q=_sh_quote(workspace),
                                 console_q=_sh_quote(console))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass
    return path


def _install_launcher_linux(ctx, console, workspace):
    # HEADLESS GUARD: only install when a desktop session is plausible. A
    # headless worker box (no DISPLAY/WAYLAND/desktop env and no pre-existing
    # applications dir) must skip silently.
    apps_dir = os.path.join(
        os.environ.get("XDG_DATA_HOME")
        or os.path.join(os.path.expanduser("~"), ".local", "share"),
        "applications")
    desktop_plausible = any(os.environ.get(v) for v in (
        "XDG_DATA_HOME", "XDG_CURRENT_DESKTOP", "DISPLAY", "WAYLAND_DISPLAY"))
    if not desktop_plausible and not os.path.isdir(apps_dir):
        log("headless box (no desktop session) — skipping .desktop launcher")
        ctx.note("desktop launcher skipped (headless)")
        return
    os.makedirs(apps_dir, exist_ok=True)
    # Write the hold-open launcher script and point Exec at it (absolute path).
    # This is the Linux cmd /k: the terminal stays open on ANY exit so the
    # console's final output is readable, not a blip.
    script = _write_launch_script(workspace, console)
    exec_cmd = _sh_quote(script)
    body = _DESKTOP_TEMPLATE.format(exec_cmd=exec_cmd)
    # Icon: fetch the hugpy mark from central into the workspace and add an
    # absolute-path Icon= line. On ANY fetch failure the launcher is written
    # iconless (unchanged prior behavior).
    icon = _fetch_icon(ctx, workspace, "hugpy-icon.png", "icon.png")
    if icon:
        body += f"Icon={icon}\n"
    target = os.path.join(apps_dir, "hugpy-agent.desktop")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(body)
    try:
        os.chmod(target, 0o755)
    except OSError:
        pass
    upd = shutil.which("update-desktop-database")
    if upd:
        subprocess.run([upd, apps_dir], check=False)
    ctx.note(f"launcher script written: {script}")
    ctx.note(f"desktop launcher written: {target}")


def _sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _install_launcher_windows(ctx, console, workspace):
    # Start-Menu shortcut via the WScript.Shell COM object. The TUI needs a
    # real, persistent console window; a bare .exe target launched from
    # Explorer can flash-and-close, so we wrap it in `cmd /k` which keeps the
    # window open. WorkingDirectory = the workspace so the agent reads its
    # .env from there.
    appdata = os.environ.get("APPDATA")
    if not appdata:
        log("APPDATA not set — cannot place a Start-Menu shortcut; skipping")
        ctx.note("start-menu launcher skipped (no APPDATA)")
        return
    programs = os.path.join(appdata, "Microsoft", "Windows",
                            "Start Menu", "Programs")
    os.makedirs(programs, exist_ok=True)
    lnk = os.path.join(programs, "hugpy Agent.lnk")
    target = os.environ.get("COMSPEC") or "cmd.exe"
    # cmd /k runs the command then RETURNS TO THE PROMPT regardless of the
    # command's exit code — so the window stays open even when the console
    # exits nonzero (e.g. the "install OpenCode" hint + exit 1). This is the
    # Windows equivalent of the Linux hold-open launcher script; parity
    # confirmed (/k does not close on a nonzero inner exit). The inner command
    # cd's into the workspace and runs the venv console.
    arguments = f'/k "cd /d "{workspace}" && "{console}" console"'
    # Icon: fetch the .ico from central; on success add IconLocation=<ico>,0.
    # Any failure -> no IconLocation line (unchanged prior behavior).
    icon = _fetch_icon(ctx, workspace, "hugpy-icon.ico", "icon.ico")
    icon_line = f"$s.IconLocation = '{icon},0'; " if icon else ""
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{lnk}'); "
        f"$s.TargetPath = '{target}'; "
        f"$s.Arguments = '{arguments}'; "
        f"$s.WorkingDirectory = '{workspace}'; "
        "$s.Description = 'hugpy fleet terminal console'; "
        f"{icon_line}"
        "$s.Save()"
    )
    pwsh = shutil.which("powershell") or shutil.which("pwsh")
    if not pwsh:
        log("powershell not found — cannot create the Start-Menu shortcut")
        ctx.note("start-menu launcher skipped (no powershell)")
        return
    subprocess.run([pwsh, "-NoProfile", "-Command", ps], check=False)
    ctx.note(f"start-menu launcher written: {lnk}")


_INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>hugpy Agent</string>
    <key>CFBundleDisplayName</key>
    <string>hugpy Agent</string>
    <key>CFBundleIdentifier</key>
    <string>ai.hugpy.agent</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleExecutable</key>
    <string>hugpy-agent-launcher</string>
{icon_key}    <key>LSMinimumSystemVersion</key>
    <string>10.13</string>
</dict>
</plist>
"""

# The .app's executable: its ONLY job is to hand the hold-open launcher script
# to Terminal.app, which runs it and gives us the TUI with the same hold-open
# behavior as Linux/Windows. Reuses <workspace>/launch-console.sh verbatim.
_MACOS_APP_LAUNCHER = """#!/bin/sh
# hugpy Agent.app executable (generated by the installer).
# Opens the terminal console in Terminal.app via the shared hold-open script.
exec open -a Terminal {script_q}
"""


def _install_launcher_macos(ctx, console, workspace):
    """Build ~/Applications/hugpy Agent.app — a user-level bundle (no sudo, no
    /Applications) whose executable opens the console in Terminal.app via the
    shared hold-open launcher script. Icon: fetch icon.png from central, then
    convert to .icns on the Mac with the always-present `sips`; degrade to no
    icon on any sips failure. Idempotent (overwrites the bundle)."""
    # Simple, documented headless guard: a remote SSH session with no Aqua
    # window server (no active GUI login) — building a .app there is pointless.
    # Aqua presence is signalled by SECURITYSESSIONID in a real login session;
    # keep it to one condition and skip only the clearly-headless SSH case.
    if os.environ.get("SSH_CONNECTION") and not os.environ.get("SECURITYSESSIONID"):
        log("headless macOS (ssh, no Aqua session) — skipping .app launcher")
        ctx.note("app launcher skipped (headless macOS)")
        return
    # The hold-open launcher script is pure POSIX sh — write it on macOS too so
    # the .app and Linux converge on identical console-launch behavior.
    script = _write_launch_script(workspace, console)

    apps_dir = os.path.join(os.path.expanduser("~"), "Applications")
    os.makedirs(apps_dir, exist_ok=True)
    app = os.path.join(apps_dir, "hugpy Agent.app")
    macos_dir = os.path.join(app, "Contents", "MacOS")
    res_dir = os.path.join(app, "Contents", "Resources")
    os.makedirs(macos_dir, exist_ok=True)
    os.makedirs(res_dir, exist_ok=True)

    # Icon: fetch the PNG, then `sips` it to .icns. Any failure -> no icon.
    icon_key = ""
    png = _fetch_icon(ctx, workspace, "hugpy-icon.png", "icon.png")
    if png:
        icns = os.path.join(res_dir, "hugpy.icns")
        sips = shutil.which("sips")
        if sips:
            proc = subprocess.run(
                [sips, "-s", "format", "icns", png, "--out", icns],
                capture_output=True)
            if proc.returncode == 0 and os.path.isfile(icns):
                icon_key = "    <key>CFBundleIconFile</key>\n" \
                           "    <string>hugpy.icns</string>\n"
                ctx.note("app icon: hugpy.icns")
            else:
                log("sips could not convert the icon; app stays iconless")
        else:
            log("sips not found; app stays iconless")

    # The bundle executable: open the script in Terminal.app.
    exe = os.path.join(macos_dir, "hugpy-agent-launcher")
    with open(exe, "w", encoding="utf-8") as fh:
        fh.write(_MACOS_APP_LAUNCHER.format(script_q=_sh_quote(script)))
    try:
        os.chmod(exe, 0o755)
    except OSError:
        pass

    with open(os.path.join(app, "Contents", "Info.plist"),
              "w", encoding="utf-8") as fh:
        fh.write(_INFO_PLIST.format(icon_key=icon_key))

    ctx.note(f"launcher script written: {script}")
    ctx.note(f"app bundle written: {app}")


@step("launch hugpy-agent console")
def step_launch(ctx):
    cfg = ctx.cfg
    if not cfg.launch:
        ctx.note("console not launched (--no-launch)")
        return
    console = shutil.which(CONSOLE_CMD) or shutil.which(CONSOLE_CMD + ".exe")
    if not console:
        ctx.note(
            f"'{CONSOLE_CMD}' not on PATH yet -- open a new terminal and run "
            f"'{CONSOLE_CMD} console'"
        )
        log(f"{CONSOLE_CMD} not found on PATH; open a new shell and run it there.")
        return
    cmd = [console, "console"]
    if cfg.offline:
        cmd.append("--offline")
    log("handing over to the console...\n")
    # inherit stdio so the TUI owns the terminal; its exit code becomes ours.
    proc = subprocess.run(cmd, env=os.environ.copy())
    raise SystemExit(proc.returncode)


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def build_config(argv):
    p = argparse.ArgumentParser(
        prog="install_hugpy_agent.py",
        description="Cross-platform one-shot installer for hugpy-agent + opencode.",
    )
    p.add_argument("--api-key", default="", help="HUGPY_API_KEY (else resolved from .env)")
    p.add_argument("--package", default=PYPI_NAME, help="PyPI requirement string")
    p.add_argument("--editable", default="", help="local repo path -> pip install -e (skips PyPI)")
    p.add_argument("--npm-prefix", default="", help="npm global prefix (default ~/.npm-global)")
    p.add_argument("--env-target", default="", help="path of the .env to write (default <venv-parent>/.env)")
    p.add_argument("--venv", default="", help=f"venv to create/use when not already in one (default {DEFAULT_VENV}, or ${VENV_ENV_VAR})")
    p.add_argument("--offline", action="store_true", help="launch console without model sync")
    p.add_argument("--no-launch", action="store_true", help="set up but don't open the console")
    p.add_argument("--no-path", action="store_true", help="don't persist PATH to shell rc / registry")
    p.add_argument("--no-opencode", action="store_true", help="skip the opencode npm install")
    p.add_argument("--dry-run", action="store_true", help="resolve + print the plan, run nothing")
    args = p.parse_args(argv)

    key, source = resolve_api_key(args.api_key)
    cfg = InstallConfig(
        api_key=key,
        api_key_source=source,
        package_spec=args.package,
        editable_hint=args.editable,
        npm_prefix=args.npm_prefix,
        env_target=args.env_target,
        venv=args.venv,
        launch=not args.no_launch,
        offline=args.offline,
        persist_path=not args.no_path,
        install_opencode=not args.no_opencode,
    )
    return cfg, args.dry_run


def main(argv=None):
    cfg, dry_run = build_config(sys.argv[1:] if argv is None else argv)
    cfg.validate()

    queue = collections.deque(STEP_REGISTRY)
    log(f"platform={sys.platform}  python={sys.executable}")
    log(f"api key source: {cfg.api_key_source}")
    log(f"steps queued: {', '.join(name for name, _ in STEP_REGISTRY)}")

    if dry_run:
        log("dry-run: nothing executed.")
        return 0

    ctx = Context(cfg=cfg)
    while queue:
        name, fn = queue.popleft()
        log(f"--- {name} ---")
        fn(ctx)  # step_launch may raise SystemExit to hand over the terminal

    log("done. summary:")
    for n in ctx.notes:
        log(f"  - {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
