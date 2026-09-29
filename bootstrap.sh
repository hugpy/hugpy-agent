#!/usr/bin/env bash
# hugpy-agent bootstrap — one command from a bare box to a running daemon.
# Mirrors the proven hugpy worker bootstrap (worker_agent/bootstrap.sh).
#
# Usage:
#   bootstrap.sh --central https://dev.hugpy.ai/api --key <api-key> \
#                [--session <discord-session-endpoint-url>] \
#                [--task-source discord-inbox|queue] \
#                [--workspace ~/hugpy-agent/workspace] \
#                [--venv ~/hugpy-agent/venv] \
#                [--src /path/to/hugpy_agent-checkout | --package hugpy-agent==X.Y.Z] \
#                [--no-check] [--no-start]
#
# What it does (idempotent — safe to re-run to upgrade):
#   1. checks python3 >= 3.10 with the venv module
#   2. creates ~/hugpy-agent/venv if missing
#   3. pip installs the package — from --src (a local checkout; the default
#      is the directory holding this script) until the PyPI release exists,
#      then --package takes over
#   4. sanity-checks that --central is reachable (informational only; see
#      the TLS note below) unless --no-check
#   5. runs `python -m hugpy_agent.install`, which writes the systemd user
#      unit + the 0600 env file, enables linger, and enables+starts
#      hugpy-agent.service (--no-start skips the enable/start)
#
# SECRETS: --key/--session are exported to the installer via the ENVIRONMENT
# (HUGPY_API_KEY / HUGPY_DISCORD_SESSION), never passed on its argv — they
# end up ONLY in the 0600 env file the installer writes. Prefer exporting
# them before running this script over passing them as flags at all.
#
# TLS DISCIPLINE (copied from the worker bootstrap): a strict-TLS failure is
# retried insecurely ONLY for the read-only reachability check in step 4,
# and only after a LOUD warning — and its result changes nothing that is
# installed. Nothing is ever downloaded over a downgraded connection, and
# the .env keeps the https URL: a broken trust store surfaces at runtime as
# a strict-TLS failure, never as a silent insecure fallback.
set -eu

CENTRAL=""
KEY="${HUGPY_API_KEY:-}"
SESSION="${HUGPY_DISCORD_SESSION:-}"
TASK_SOURCE=""
WORKSPACE=""
VENV="${HOME}/hugpy-agent/venv"
# Default source: the directory this script sits in (a checkout of the repo).
SRC="$(cd "$(dirname "$0")" && pwd)"
PACKAGE=""
CHECK=1
START=1

die() { printf 'bootstrap: %s\n' "$*" >&2; exit 1; }
say() { printf 'bootstrap: %s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --central)     CENTRAL="${2:-}"; shift 2 ;;
    --key)         KEY="${2:-}"; shift 2 ;;
    --session)     SESSION="${2:-}"; shift 2 ;;
    --task-source) TASK_SOURCE="${2:-}"; shift 2 ;;
    --workspace)   WORKSPACE="${2:-}"; shift 2 ;;
    --venv)        VENV="${2:-}"; shift 2 ;;
    --src)         SRC="${2:-}"; shift 2 ;;
    --package)     PACKAGE="${2:-}"; shift 2 ;;
    --no-check)    CHECK=0; shift ;;
    --no-start)    START=0; shift ;;
    -h|--help)     sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)             die "unknown argument: $1" ;;
  esac
done

[ -n "$CENTRAL" ] || die "--central is required (e.g. --central https://dev.hugpy.ai/api)"
[ -n "$KEY" ]     || die "--key is required (or export HUGPY_API_KEY first)"
CENTRAL="${CENTRAL%/}"   # strip a trailing slash so URL joins are clean

# 1. python3 >= 3.10 with the venv module ----------------------------------
command -v python3 >/dev/null 2>&1 || die "python3 not found (need >= 3.10)"
# Version FLOOR with interpreter DISCOVERY (parity with the .py installer): if
# python3 is below 3.10, look for a newer one (explicit series on PATH, then
# the known install homes) instead of failing on a cryptic pip error later.
BASE_PY="python3"
if ! "$BASE_PY" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)'; then
  FOUND=""
  for c in python3.14 python3.13 python3.12 python3.11 python3.10 \
           /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if command -v "$c" >/dev/null 2>&1 \
       && "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)' \
            2>/dev/null; then
      FOUND="$c"; break
    fi
  done
  if [ -z "$FOUND" ]; then
    die "python3 >= 3.10 required (found $(python3 -V 2>&1)); install one and
  re-run this SAME command:
    sudo apt install python3.12 python3.12-venv   # Debian/Ubuntu
    sudo dnf install python3.12                   # Fedora/RHEL
  No other change is needed — bootstrap finds it automatically."
  fi
  say "python3 is $(python3 -V 2>&1); using ${FOUND} instead"
  BASE_PY="$FOUND"
fi
"$BASE_PY" -c 'import venv' 2>/dev/null \
  || die "python3 venv module missing (install python3-venv)"

# 2. venv (idempotent) ------------------------------------------------------
# A venv left by an earlier failed run can itself be below the floor; rebuild
# it rather than reusing a too-old interpreter.
if [ -x "${VENV}/bin/python" ] \
   && ! "${VENV}/bin/python" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)' \
        2>/dev/null; then
  say "existing venv at ${VENV} is below the 3.10 floor — rebuilding it"
  rm -rf "$VENV"
fi
if [ ! -x "${VENV}/bin/python" ]; then
  say "creating venv at ${VENV} with ${BASE_PY}"
  "$BASE_PY" -m venv "$VENV"
fi
PY_BIN="${VENV}/bin/python"
PIP_BIN="${VENV}/bin/pip"

# 2b. upgrade pip INSIDE the venv (never system pip) ------------------------
# Distro pips from the 20.x era choke on modern wheels / PEP 517 pyproject
# builds, so a bare-box first install can look broken. Upgrade the venv's pip
# BEFORE the package install. The `-m pip` spelling is mandatory (a pip that
# is mid-self-replace can't overwrite its own launcher). This touches ONLY the
# venv we just made — never the system/apt pip (PEP 668). A network/proxy
# failure is non-fatal: one visible warning, then continue with the existing
# (old-but-working) pip, since that still beats a dead install. Idempotent:
# already-newest is a no-op.
say "pip (venv) before: $("$PY_BIN" -m pip --version 2>/dev/null || echo unknown)"
if "$PY_BIN" -m pip install --upgrade pip; then
  say "pip (venv) after:  $("$PY_BIN" -m pip --version 2>/dev/null || echo unknown)"
else
  say "WARNING: could not upgrade pip in the venv (network/proxy?); continuing"
  say "WARNING: with the existing pip $("$PY_BIN" -m pip --version 2>/dev/null || echo '(unknown)')."
fi

# 3. install / upgrade the package ------------------------------------------
if [ -n "$PACKAGE" ]; then
  SPEC="$PACKAGE"
else
  [ -f "${SRC}/pyproject.toml" ] \
    || die "--src ${SRC} is not a hugpy-agent checkout (no pyproject.toml); pass --src or --package"
  SPEC="$SRC"
fi
say "pip install --upgrade '${SPEC}'"
"$PIP_BIN" install --upgrade "$SPEC"
[ -x "${VENV}/bin/hugpy-agent" ] || die "install produced no ${VENV}/bin/hugpy-agent"

# 4. central reachability check (informational; never gates on insecure) ----
if [ "$CHECK" = "1" ] && command -v curl >/dev/null 2>&1; then
  if curl -fsS -o /dev/null --max-time 10 "$CENTRAL/models" 2>/dev/null \
     || curl -sS -o /dev/null --max-time 10 "$CENTRAL" 2>/dev/null; then
    say "central ${CENTRAL} is reachable (strict TLS)"
  elif curl -ksS -o /dev/null --max-time 10 "$CENTRAL" 2>/dev/null; then
    # Reachable, but ONLY with certificate checks off: warn LOUDLY and keep
    # the strict https config — the daemon will fail closed at runtime until
    # the trust store is fixed. We never write an insecure endpoint.
    say "WARNING: ${CENTRAL} answers only with TLS verification DISABLED."
    say "WARNING: the installed config stays strict-https and the agent will"
    say "WARNING: fail closed until this box trusts the certificate. Fix the"
    say "WARNING: trust store (or front a trusted cert) before expecting runs."
  else
    say "WARNING: ${CENTRAL} unreachable from here right now; installing"
    say "WARNING: anyway — the unit retries on its own schedule."
  fi
fi

# 4b. desktop launcher for the terminal console ----------------------------
# Register a .desktop that opens the terminal console (Terminal=true), pointed
# at a HOLD-OPEN launcher script. Under Terminal=true a .desktop has no cmd /k
# equivalent, so a console that prints the "install OpenCode" hint and exits 1
# just blips shut (operator field report 2026-07-24). The script runs the
# console, then waits for Enter so its final output stays readable. Pointing
# Exec at an absolute script path also dodges .desktop's fragile Exec-quoting.
# HEADLESS GUARD: a worker box with no desktop session skips silently.
# Idempotent (overwrites the same files). No Icon= line — no icon asset ships
# to the box (wanted follow-up; see the installer note).
CONSOLE_BIN="${VENV}/bin/hugpy-agent"
LAUNCH_WS="${WORKSPACE:-${HOME}/hugpy-agent/workspace}"
APPS_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"
if [ -n "${XDG_DATA_HOME:-}${XDG_CURRENT_DESKTOP:-}${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] \
   || [ -d "$APPS_DIR" ]; then
  mkdir -p "$APPS_DIR" "$LAUNCH_WS"
  LAUNCH_SCRIPT="${LAUNCH_WS}/launch-console.sh"
  cat > "$LAUNCH_SCRIPT" <<EOF
#!/bin/sh
# hugpy-agent terminal console launcher (generated by bootstrap.sh).
# Holds the terminal open after the console exits so its final output — e.g.
# the "install OpenCode" hint on a box without it — stays readable.
export HUGPY_WORKSPACE='${LAUNCH_WS}'
cd '${LAUNCH_WS}' || exit 1
'${CONSOLE_BIN}' console
ec=\$?
echo
echo "[hugpy-agent console exited with status \$ec]"
printf "Press Enter to close..."
read -r _
exit "\$ec"
EOF
  chmod 0755 "$LAUNCH_SCRIPT" 2>/dev/null || true
  # Fetch the hugpy mark from central into the workspace; add an Icon= line on
  # success, iconless on any failure (curl missing / offline / bad response).
  ICON_LINE=""
  ICON_DST="${LAUNCH_WS}/hugpy-icon.png"
  if command -v curl >/dev/null 2>&1 \
     && curl -fsSL "${CENTRAL}/agent/install/icon.png" -o "$ICON_DST" 2>/dev/null \
     && [ -s "$ICON_DST" ]; then
    ICON_LINE="Icon=${ICON_DST}"
    say "fetched launcher icon: ${ICON_DST}"
  else
    rm -f "$ICON_DST" 2>/dev/null || true
    say "launcher icon fetch skipped (offline / no curl) — iconless launcher"
  fi
  cat > "${APPS_DIR}/hugpy-agent.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=hugpy Agent
Comment=hugpy fleet terminal console
Exec='${LAUNCH_SCRIPT}'
Terminal=true
Categories=Development;Utility;
${ICON_LINE}
EOF
  chmod 0755 "${APPS_DIR}/hugpy-agent.desktop" 2>/dev/null || true
  command -v update-desktop-database >/dev/null 2>&1 \
    && update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
  say "launcher script written: ${LAUNCH_SCRIPT}"
  say "desktop launcher written: ${APPS_DIR}/hugpy-agent.desktop"
else
  say "headless box (no desktop session) — skipping .desktop launcher"
fi

# 5. write .env + unit, linger, enable + start ------------------------------
# Secrets ride the environment into the installer (never its argv, so they
# don't appear in `ps`); the installer's env file is their only disk home.
export HUGPY_API_KEY="$KEY"
if [ -n "$SESSION" ]; then export HUGPY_DISCORD_SESSION="$SESSION"; fi
set -- --central "$CENTRAL" --venv "$VENV"
if [ -n "$WORKSPACE" ];   then set -- "$@" --workspace "$WORKSPACE"; fi
if [ -n "$TASK_SOURCE" ]; then set -- "$@" --task-source "$TASK_SOURCE"; fi
if [ "$START" != "1" ];   then set -- "$@" --no-enable; fi
say "installing hugpy-agent.service (user unit)"
exec "$PY_BIN" -m hugpy_agent.install "$@"
