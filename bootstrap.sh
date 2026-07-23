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
python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)' \
  || die "python3 >= 3.10 required (found $(python3 -V 2>&1))"
python3 -c 'import venv' 2>/dev/null \
  || die "python3 venv module missing (install python3-venv)"

# 2. venv (idempotent) ------------------------------------------------------
if [ ! -x "${VENV}/bin/python" ]; then
  say "creating venv at ${VENV}"
  python3 -m venv "$VENV"
fi
PY_BIN="${VENV}/bin/python"
PIP_BIN="${VENV}/bin/pip"

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
