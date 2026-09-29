"""Enrollment installer (P2.7): systemd user unit + 0600 env file + linger.

Mirrors the proven hugpy worker installer
(abstract_hugpy_dev/worker_agent/install.py — the pattern that survived the
fleet enrollments), simplified to this package's needs:

  ~/.config/systemd/user/hugpy-agent.service   the unit (NO secrets in it —
                                               user units are world-readable
                                               on many boxes)
  ~/.config/hugpy-agent/agent.env              EnvironmentFile, mode 0600 —
                                               the ONLY place the API key /
                                               session token land on disk
  loginctl enable-linger                       unit survives logout + reboot
  systemctl --user enable --now                daemon-reload, enable, start

Design rules (what the tests pin):
  * PURE rendering — render_unit()/render_env() are string→string, no I/O.
  * PATH-INJECTABLE — install() takes `home`; nothing hardcodes the real
    $HOME, so tests install into a tempdir.
  * SYSTEM-TOUCHING calls (systemctl/loginctl) go through an injectable
    `runner`; tests pass a recorder and NOTHING is ever executed.
  * IDEMPOTENT — rendering is deterministic; a re-run with the same inputs
    writes byte-identical files (reported as unchanged) and re-issues only
    idempotent commands. Changed inputs rewrite in place, never duplicate.
  * %h-PORTABLE — every path inside the UNIT uses systemd's %h specifier.
    The env FILE carries expanded absolute paths instead: systemd does NOT
    substitute specifiers inside EnvironmentFile values, and the env file is
    per-box anyway.
  * SECRETS — the key/session go only into the 0600 env file; never into the
    unit, argv of the runner commands, or anything printed.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

UNIT_NAME = "hugpy-agent.service"
# Paths as the UNIT sees them (%h = the service user's home).
UNIT_ENV_FILE = "%h/.config/hugpy-agent/agent.env"
UNIT_EXEC_DEFAULT = "%h/hugpy-agent/venv/bin/hugpy-agent serve"
# The same paths relative to an injectable home (for the actual writes).
REL_UNIT_PATH = os.path.join(".config", "systemd", "user", UNIT_NAME)
REL_ENV_PATH = os.path.join(".config", "hugpy-agent", "agent.env")
REL_VENV = os.path.join("hugpy-agent", "venv")
REL_WORKSPACE = os.path.join("hugpy-agent", "workspace")

# Secret-bearing env keys: these must never appear anywhere but the env file.
SECRET_KEYS = ("HUGPY_API_KEY", "HUGPY_DISCORD_SESSION")


# ── pure rendering ────────────────────────────────────────────────────────
def render_unit(exec_start: str = UNIT_EXEC_DEFAULT,
                env_file: str = UNIT_ENV_FILE,
                description: str = "hugpy-agent daemon (serve)") -> str:
    """The systemd user unit text. Deliberately contains ONLY structure —
    all configuration (and every secret) rides in the EnvironmentFile."""
    return (
        "[Unit]\n"
        "Description=%s\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "# All config + secrets live in the 0600 env file, NEVER here — the\n"
        "# unit file is not permission-protected.\n"
        "EnvironmentFile=%s\n"
        "ExecStart=%s\n"
        "# on-failure (NOT always): a graceful stop (SIGTERM finishes the\n"
        "# current task, exits 0) and a deliberate operator stop must STAY\n"
        "# stopped; only a crash restarts (the worker-unit doctrine).\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ) % (description, env_file, exec_start)


def render_env(values: dict) -> str:
    """KEY=VALUE lines for systemd's EnvironmentFile (also readable by
    config._parse_env_file). Fail closed on unwritable input: a key/value
    that cannot round-trip through the line format raises rather than
    writing a file systemd would mis-parse."""
    lines = ["# hugpy-agent daemon environment — written by hugpy_agent.install.",
             "# Contains secrets: keep mode 0600; never commit."]
    for key, value in values.items():
        key = str(key)
        value = "" if value is None else str(value)
        if not key or not all(c.isupper() or c.isdigit() or c == "_"
                              for c in key):
            raise ValueError("invalid env key %r (want UPPER_SNAKE_CASE)" % key)
        if "\n" in value or "\r" in value:
            raise ValueError("env value for %s contains a newline" % key)
        lines.append("%s=%s" % (key, value))
    return "\n".join(lines) + "\n"


# ── the installer ─────────────────────────────────────────────────────────
def _default_runner(argv: list) -> int:
    """Run one system-touching command. check=False: a failed enable/linger
    is reported as a warning, not a crash — the files are already correct
    and the operator can finish by hand."""
    return subprocess.run(argv, check=False).returncode


def _write_if_changed(path: str, content: str, mode: int | None = None):
    """Write `path` atomically-enough for config files; returns True when the
    content actually changed (the idempotency signal). `mode` (when given) is
    enforced on EVERY call — a pre-existing env file with loose perms gets
    tightened even if its content is already current."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existing = None
    try:
        with open(path, encoding="utf-8") as fh:
            existing = fh.read()
    except OSError:
        pass
    changed = existing != content
    if changed:
        if mode is not None:
            # Create with the tight mode from the first byte — no window
            # where the secret sits world-readable.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
        else:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
    if mode is not None:
        os.chmod(path, mode)
    return changed


def install(values: dict, home: str | None = None, venv: str | None = None,
            runner=None, enable: bool = True) -> dict:
    """Write the unit + env file and (via `runner`) enable the service.

    `values` is the full env-file dict (HUGPY_* keys; secrets included —
    this is their one destination). Returns a report dict; raises only on
    unrenderable input (render_env). System commands never raise: non-zero
    exits become warnings in the report.
    """
    home = os.path.realpath(home or os.path.expanduser("~"))
    runner = runner or _default_runner
    venv = venv or os.path.join(home, REL_VENV)

    # ExecStart/venv inside the unit: %h-portable when the venv lives under
    # this home (the normal case); an out-of-home venv is kept literal.
    if venv == os.path.join(home, REL_VENV):
        exec_start = UNIT_EXEC_DEFAULT
    elif venv.startswith(home + os.sep):
        exec_start = "%h" + venv[len(home):] + "/bin/hugpy-agent serve"
    else:
        exec_start = os.path.join(venv, "bin", "hugpy-agent") + " serve"

    unit_path = os.path.join(home, REL_UNIT_PATH)
    env_path = os.path.join(home, REL_ENV_PATH)
    unit_text = render_unit(exec_start=exec_start)
    env_text = render_env(values)

    unit_changed = _write_if_changed(unit_path, unit_text)
    env_changed = _write_if_changed(env_path, env_text, mode=0o600)

    # The daemon's workspace must exist before first start (the jail root).
    workspace = values.get("HUGPY_WORKSPACE")
    if workspace:
        os.makedirs(workspace, exist_ok=True)

    commands = []
    warnings = []
    if enable:
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        # Linger first: without it the unit dies at logout / never starts
        # after reboot — the classic "it stopped overnight" failure.
        for argv in (["loginctl", "enable-linger"] + ([user] if user else []),
                     ["systemctl", "--user", "daemon-reload"],
                     ["systemctl", "--user", "enable", "--now", UNIT_NAME]):
            commands.append(argv)
            try:
                rc = runner(argv)
            except Exception as exc:  # noqa: BLE001 — report, don't crash
                rc = -1
                warnings.append("%s failed: %s: %s"
                                % (" ".join(argv), type(exc).__name__, exc))
                continue
            if rc != 0:
                warnings.append("`%s` exited %d — run it by hand to finish "
                                "enrollment" % (" ".join(argv), rc))
    return {"unit_path": unit_path, "env_path": env_path,
            "unit_changed": unit_changed, "env_changed": env_changed,
            "commands": commands, "enabled": enable, "warnings": warnings}


# ── CLI entry point (`python -m hugpy_agent.install`) ─────────────────────
def main(argv=None, home: str | None = None, runner=None) -> int:
    """`home`/`runner` are injectable for tests; real invocations use the
    actual $HOME and subprocess."""
    p = argparse.ArgumentParser(
        prog="hugpy-agent-install",
        description="Install hugpy-agent as a systemd user service "
                    "(unit + 0600 env file + linger + enable).")
    p.add_argument("--central", default=os.environ.get("HUGPY_BASE"),
                   help="fleet base URL (HUGPY_BASE), e.g. "
                        "https://dev.hugpy.ai/api")
    # Secrets prefer the ENVIRONMENT over argv: bootstrap.sh exports them so
    # they never appear in `ps` / shell history on the target box.
    p.add_argument("--key", default=os.environ.get("HUGPY_API_KEY"),
                   help="API key (prefer exporting HUGPY_API_KEY instead)")
    p.add_argument("--session", default=os.environ.get("HUGPY_DISCORD_SESSION"),
                   help="operator Discord session endpoint URL (prefer "
                        "exporting HUGPY_DISCORD_SESSION)")
    p.add_argument("--workspace", default=os.environ.get("HUGPY_WORKSPACE"),
                   help="daemon workspace dir (default ~/hugpy-agent/workspace)")
    p.add_argument("--task-source", dest="task_source",
                   default=os.environ.get("HUGPY_TASK_SOURCE"),
                   choices=("discord-inbox", "queue"),
                   help="serve task source; omitted => the daemon idles "
                        "fail-closed with a heartbeat")
    p.add_argument("--model", default=os.environ.get("HUGPY_MODEL"))
    p.add_argument("--policy", default=os.environ.get("HUGPY_POLICY"))
    p.add_argument("--poll-interval", dest="poll_interval",
                   default=os.environ.get("HUGPY_POLL_INTERVAL"))
    p.add_argument("--venv", default=None,
                   help="venv the unit's ExecStart points at "
                        "(default ~/hugpy-agent/venv)")
    p.add_argument("--no-enable", dest="enable", action="store_false",
                   help="write the files but skip linger/systemctl")
    opts = p.parse_args(list(sys.argv[1:] if argv is None else argv))

    if not opts.central:
        p.error("--central is required (or set HUGPY_BASE)")
    if not opts.key:
        p.error("--key is required (or export HUGPY_API_KEY)")

    home_dir = os.path.realpath(home or os.path.expanduser("~"))
    values = {"HUGPY_BASE": opts.central,
              "HUGPY_API_KEY": opts.key,
              "HUGPY_WORKSPACE": opts.workspace
              or os.path.join(home_dir, REL_WORKSPACE)}
    # Optional knobs land in the env file only when actually configured —
    # the daemon's own defaults stay the single source of truth otherwise.
    for key, val in (("HUGPY_DISCORD_SESSION", opts.session),
                     ("HUGPY_TASK_SOURCE", opts.task_source),
                     ("HUGPY_MODEL", opts.model),
                     ("HUGPY_POLICY", opts.policy),
                     ("HUGPY_POLL_INTERVAL", opts.poll_interval)):
        if val:
            values[key] = val

    report = install(values, home=home_dir, venv=opts.venv, runner=runner,
                     enable=opts.enable)
    print("hugpy-agent installer")
    print("  unit : %s (%s)" % (report["unit_path"],
                                "written" if report["unit_changed"]
                                else "unchanged"))
    print("  env  : %s (0600, %s)" % (report["env_path"],
                                      "written" if report["env_changed"]
                                      else "unchanged"))
    if report["enabled"]:
        print("  service: enabled + started (%s)" % UNIT_NAME)
        print("  watch: journalctl --user -u hugpy-agent -f")
    else:
        print("  service: NOT enabled (--no-enable); to finish:")
        print("    loginctl enable-linger $USER && "
              "systemctl --user daemon-reload && "
              "systemctl --user enable --now %s" % UNIT_NAME)
    for w in report["warnings"]:
        print("WARNING: %s" % w, file=sys.stderr)
    return 1 if report["warnings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
