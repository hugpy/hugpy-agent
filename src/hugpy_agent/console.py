"""Interactive console — OpenCode as the fleet's terminal face.

OpenCode (the open-source TUI agent CLI, `npm i -g opencode-ai`) is an
OPTIONAL PEER of this package, never a dependency: hugpy-agent works fully
without it, and this module never installs it. What we own is the seam —
detect the binary, generate a correct opencode.json pointed at the fleet's
OpenAI-compatible /v1 mount, and exec into it. Three doctrines shape the
code:

  1. STDLIB ONLY, still. The whole console path uses urllib/json/os/shutil —
     the zero-runtime-deps promise is not negotiable, and "launch a Node TUI"
     must not smuggle in a Python client library to do it.

  2. THE KEY NEVER LANDS IN THE FILE. OpenCode config supports `{env:NAME}`
     references resolved at ITS runtime; we always write the reference, never
     the literal key. opencode.json lives in a workspace dir a user may back
     up, share, or commit — a secret at rest there is a leak waiting to
     happen. The literal key is used exactly once, in-memory, to authenticate
     the model-map fetch, and is exported into the child's environment at
     exec time (a process env entry, not a file).

  3. THE MODEL MAP IS GENERATED, NOT CURATED. OpenCode custom providers
     require a STATIC models map (verified against opencode.ai/docs/providers
     2026-07-22 — no /v1/models auto-discovery for custom providers), but the
     fleet is live: models come, go, and get operator-BLOCKED. So every
     launch refreshes the map from the fleet's own /v1/models, filtered to
     chat-drivable tasks (an ASR or diffusion model in a coding agent's
     picker is a foot-gun) and honoring `blocked` (the operator's model BLOCK
     outranks listing — same rule the fleet itself enforces). `--offline`
     opts out and reuses the existing file when the fleet is unreachable.

Config resolution REUSES config.load_config — base URL and HUGPY_API_KEY
come from the exact env > .env > agent.toml chain every other subcommand
uses; there is deliberately no second config path here.

Writes are atomic (tmp + os.replace, same discipline as trace.py) with the
previous config kept as `.bak`, so a crash mid-refresh never leaves OpenCode
staring at a torn JSON file and a bad sync is one `mv` from undone.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request

from .gateway import normalize_base, origin

# The env var name OpenCode resolves at ITS runtime via the `{env:NAME}`
# reference we write into opencode.json. Distinct from HUGPY_API_KEY on
# purpose: the reference is visible in a shareable file, and a dedicated
# name scopes what a leaked config can even ask the environment for.
KEY_ENV_NAME = "HUGPY_OPEN_CODE_HARNESS_API"

# Chat-drivable tasks (mirrors the sync-models reference script): OpenCode
# drives /v1/chat/completions, so only text-generation and vision-chat
# models belong in its picker.
TEXT_TASKS = frozenset({"text-generation", "image-text-to-text"})

# Default-model preference: the operator-chosen agent brain (config.py's
# DEFAULT_AGENT_BRAIN) when the fleet serves it, else first available.
PREFERRED_DEFAULT = "Qwen~Qwen3-Coder-Next-GGUF"

DEFAULT_WORKSPACE = os.path.join("~", ".hugpy_agent", "console")

INSTALL_HINT = """\
opencode not found. OpenCode is an optional peer — install it once with npm:

    npm config set prefix ~/.npm-global
    npm install -g opencode-ai

then make sure ~/.npm-global/bin is on your PATH, e.g. add to ~/.bashrc:

    export PATH="$HOME/.npm-global/bin:$PATH"

hugpy-agent never auto-installs it; everything else here works without it."""


class ConsoleError(Exception):
    """A console-path failure with a message fit for the terminal. Raised
    (not sys.exit'd) so cmd_console owns the exit code and tests can assert
    on the message without capturing stdio."""


def resolve_opencode() -> str | None:
    """Absolute path of the `opencode` binary, or None.

    PATH first (shutil.which — respects however the user installed it),
    then the one well-known npm-prefix location our install hint sets up,
    which a fresh shell may not have on PATH yet. None means "not
    installed", never an error: absence is the documented, supported state.
    """
    found = shutil.which("opencode")
    if found:
        return found
    candidate = os.path.expanduser(os.path.join("~", ".npm-global", "bin",
                                                "opencode"))
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None


def models_url(central: str) -> str:
    """The /v1/models URL for a configured central base. Follows gateway.py's
    normalization rules (bare host -> https, `/api` -> `/api/v1`) without the
    live probe — one deterministic URL is enough here, and the fetch error
    message names it so a wrong base is self-diagnosing."""
    base = normalize_base(central)
    if base.endswith("/v1"):
        return base + "/models"
    if base.endswith("/api"):
        return base + "/v1/models"
    return origin(base) + "/api/v1/models"


def fetch_model_map(central: str, key: str,
                    tasks: frozenset[str] = TEXT_TASKS,
                    timeout: int = 30) -> tuple[dict, str]:
    """(models map, chosen default id) from the fleet's live /v1/models.

    Ported from the sync-models reference script. Filtering doctrine:
      * tasks must intersect `tasks` — chat-drivable only;
      * `blocked: true` records are skipped — the operator's model BLOCK
        outranks listing, here as everywhere;
      * context_length rides into the display name ("(32k ctx)") and
        vision capability is tagged "[vision]" so the picker shows fit.

    Default preference: PREFERRED_DEFAULT (the agent brain) when present,
    else the first map entry. Network/HTTP failures raise ConsoleError with
    the URL and the --offline escape hatch spelled out — a dead fleet must
    read as "here is what to do", not a urllib traceback.
    """
    url = models_url(central)
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode(errors="replace"))
    except Exception as exc:
        raise ConsoleError(
            "could not fetch the fleet model list from %s: %s\n"
            "(use --offline to skip the sync and launch with the existing "
            "opencode.json, if one was written before)" % (url, exc)) from exc

    records = data.get("data") or data.get("models") or []
    models: dict = {}
    for m in sorted((r for r in records if isinstance(r, dict) and r.get("id")),
                    key=lambda r: str(r["id"]).lower()):
        if m.get("blocked"):
            continue
        m_tasks = set(m.get("tasks") or ([m["task"]] if m.get("task") else []))
        if not (m_tasks & tasks):
            continue
        ctx = m.get("context_length") or 0
        label = str(m["id"])
        if ctx:
            label += " (%dk ctx)" % (int(ctx) // 1024)
        if "image-text-to-text" in m_tasks:
            label += " [vision]"
        models[str(m["id"])] = {"name": label}

    if not models:
        raise ConsoleError(
            "the fleet at %s listed no chat-drivable models (tasks %s) — "
            "nothing for OpenCode to serve" % (url, sorted(tasks)))
    default = (PREFERRED_DEFAULT if PREFERRED_DEFAULT in models
               else next(iter(models)))
    return models, default


def build_config(central: str, key_env_name: str, models: dict,
                 default_model: str) -> dict:
    """The opencode.json structure for the hugpy provider.

    The apiKey is ALWAYS the `{env:NAME}` reference — OpenCode resolves it
    from its own process environment at runtime; the literal key never
    enters this dict and therefore never lands on disk.

    Permission posture is read from ``HUGPY_CONSOLE_PERMISSION`` (edit/bash/
    webfetch), defaulting to ``ask`` — the same fail-toward-the-operator posture
    as this package's own policy engine, so a fresh console still must not
    silently mutate anything. Wiring this env is what lets an UNATTENDED keeper
    run: ``console`` re-syncs by default (rewriting opencode.json every launch),
    so a hand-edited "allow" never stuck — the value has to come from the
    environment the fleet already sets (HUGPY_CONSOLE_PERMISSION=allow in
    hugpy-keeper.env). Anything other than ask/allow/deny falls back to ask.
    """
    perm = os.environ.get("HUGPY_CONSOLE_PERMISSION", "ask")
    if perm not in ("ask", "allow", "deny"):
        perm = "ask"
    base = normalize_base(central)
    if not base.endswith("/v1"):
        base = models_url(central)[: -len("/models")]
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "hugpy": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "hugpy fleet",
                "options": {
                    "baseURL": base,
                    "apiKey": "{env:%s}" % key_env_name,
                },
                "models": models,
            },
        },
        "model": "hugpy/" + default_model,
        "permission": {
            "edit": perm,
            "bash": perm,
            "webfetch": perm,
        },
    }


def config_path(workspace_dir: str) -> str:
    return os.path.join(os.path.realpath(os.path.expanduser(workspace_dir)),
                        "opencode.json")


def materialize(workspace_dir: str, config: dict) -> str:
    """Write/refresh <workspace>/opencode.json atomically; returns the path.

    tmp-in-same-dir + os.replace (trace.py's discipline): a reader — OpenCode
    itself, mid-launch — sees the old file or the whole new one, never a torn
    middle. An existing config is first copied to `.bak`, so a sync that
    picks up a broken fleet listing is one `mv opencode.json.bak
    opencode.json` from undone (and --offline will happily launch on it).
    """
    path = config_path(workspace_dir)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".opencode-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def launch(workspace_dir: str, key: str,
           binary: str | None = None) -> "None":
    """chdir into the workspace and exec OpenCode in place (os.execvp — the
    Python process becomes the TUI; no wrapper process lingers to garble
    terminal ownership). The literal API key is exported into the child's
    ENVIRONMENT here — the one place it travels — matching the `{env:NAME}`
    reference the config carries. A missing binary raises ConsoleError with
    the install hint; auto-installing an npm package is explicitly out of
    scope (OpenCode is a peer, not a dependency).

    Only returns by raising; on success the exec replaces the process.
    """
    binary = binary or resolve_opencode()
    if not binary:
        raise ConsoleError(INSTALL_HINT)
    ws = os.path.realpath(os.path.expanduser(workspace_dir))
    if key:
        os.environ[KEY_ENV_NAME] = key
    os.chdir(ws)
    _rebind_stdin_to_tty()
    os.execvp(binary, [binary])


def _rebind_stdin_to_tty() -> None:
    """If stdin is not a TTY but a controlling terminal exists, re-bind fd 0
    to /dev/tty before the exec.

    Why: the one-liner install path is ``curl … | bash`` — bash's stdin IS
    the curl pipe, and every child inherits it. The OpenCode TUI then enables
    mouse tracking on the terminal while its input loop reads the exhausted
    pipe, so the terminal's mouse reports (``^[[<35;101;1M`` …) land in the
    shell as literal garbage (operator report, 2026-07-23). Re-binding fd 0
    to the controlling TTY gives the TUI the input stream its escape-mode
    setup assumes. No controlling TTY (cron, CI) -> leave stdin alone; the
    TUI's own non-interactive handling applies. Windows has no /dev/tty and
    the pipe-install path there is PowerShell's, so this is a POSIX-only
    concern by construction."""
    try:
        if os.isatty(0):
            return
        fd = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return  # no controlling terminal — headless is a legitimate caller
    try:
        os.dup2(fd, 0)
    finally:
        if fd != 0:
            os.close(fd)


def run_console(cfg, workspace: str | None = None, sync: bool = True,
                offline: bool = False, model: str | None = None,
                print_config: bool = False) -> int:
    """The `hugpy-agent console` flow, factored out of cli.py for testing.

    Order: resolve workspace -> (sync? fetch+write config) -> print-config
    short-circuit -> launch. `--offline` implies no sync and additionally
    requires an existing opencode.json to be present (launching OpenCode
    with no provider config would drop the user into a stranger's setup —
    fail with instructions instead). Returns an exit code; only the launch
    step never returns (exec).
    """
    ws = os.path.expanduser(workspace or DEFAULT_WORKSPACE)
    path = config_path(ws)

    do_sync = sync and not offline
    if do_sync:
        models, default = fetch_model_map(cfg.base, cfg.api_key)
        chosen = model or default
        if chosen not in models:
            # An explicit --model that the fleet doesn't serve is honored
            # anyway (the operator may know a load is in flight), but say so.
            print("[console] note: model %r is not in the fleet's "
                  "chat-drivable list right now" % chosen, file=sys.stderr)
        config = build_config(cfg.base, KEY_ENV_NAME, models, chosen)
        if print_config:
            print(json.dumps(config, indent=2))
            return 0
        materialize(ws, config)
        print("[console] wrote %s (%d models, default hugpy/%s)"
              % (path, len(models), chosen), file=sys.stderr)
    else:
        if print_config:
            if not os.path.exists(path):
                raise ConsoleError(
                    "no existing config at %s and sync is off — run once "
                    "without --offline/--no-sync first" % path)
            with open(path, encoding="utf-8") as fh:
                print(fh.read().rstrip())
            return 0
        if not os.path.exists(path):
            raise ConsoleError(
                "offline/no-sync launch needs an existing config at %s — "
                "run `hugpy-agent console` online once to generate it" % path)
        if model:
            # Repoint the default in the existing file (still atomic+.bak);
            # the models map is whatever the last sync captured.
            with open(path, encoding="utf-8") as fh:
                config = json.load(fh)
            config["model"] = "hugpy/" + model
            materialize(ws, config)
        print("[console] reusing %s (no sync)" % path, file=sys.stderr)

    launch(ws, cfg.api_key)
    return 0  # unreachable on success (exec); keeps the signature honest
