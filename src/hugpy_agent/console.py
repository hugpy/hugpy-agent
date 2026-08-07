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


# The env var Claude Code reads for its bearer credential and its API origin.
# Claude Code APPENDS `/v1/messages` itself, so ANTHROPIC_BASE_URL must be the
# ORIGIN (e.g. https://dev.hugpy.ai), never the /v1 mount.
CLAUDE_AUTH_ENV = "ANTHROPIC_AUTH_TOKEN"
CLAUDE_BASE_ENV = "ANTHROPIC_BASE_URL"

CLAUDE_INSTALL_HINT = """\
claude (Claude Code) not found. It is an optional peer — install it once:

    npm config set prefix ~/.npm-global
    npm install -g @anthropic-ai/claude-code

then make sure ~/.npm-global/bin is on your PATH, e.g. add to ~/.bashrc:

    export PATH="$HOME/.npm-global/bin:$PATH"

hugpy-agent never auto-installs it; opencode remains the default frontend."""


def resolve_claude() -> str | None:
    """Absolute path of the `claude` (Claude Code) binary, or None.

    Same discipline as resolve_opencode: PATH first, then the well-known
    npm-prefix location the install hint sets up. None means "not installed",
    never an error."""
    found = shutil.which("claude")
    if found:
        return found
    candidate = os.path.expanduser(os.path.join("~", ".npm-global", "bin",
                                                "claude"))
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None


def launch_claude_code(central: str, key: str,
                       binary: str | None = None,
                       init_prompt: str | None = None) -> "None":
    """exec Claude Code pointed at the fleet's Anthropic Messages shim.

    Init prompting on call: `init_prompt` (or $HUGPY_INIT_PROMPT when unset) is
    passed to Claude Code as `--append-system-prompt`, so a caller — e.g. the
    console handing down the Steward's reach statement — can seed the frontend's
    system prompt at launch. Empty/unset adds nothing (the bare exec is
    unchanged), keeping it sparing.

    Sets ANTHROPIC_BASE_URL to the fleet ORIGIN (Claude Code appends
    `/v1/messages` itself — so NOT the /v1 mount) and ANTHROPIC_AUTH_TOKEN to
    the same key opencode uses (HUGPY_API_KEY, resolved by cfg). The key is only
    ever a process-env entry, never written to a file — the same discipline as
    the opencode `{env:NAME}` reference. Never installs anything: a missing
    binary raises ConsoleError with the install hint.

    Only returns by raising; on success the exec replaces the process.
    """
    binary = binary or resolve_claude()
    if not binary:
        raise ConsoleError(CLAUDE_INSTALL_HINT)
    # ANTHROPIC_BASE_URL must be the API root that Claude Code appends
    # `/v1/messages` to — WITHOUT a trailing /v1 (the operator's constraint),
    # but WITH the fleet's /api routing prefix where the topology uses it. The
    # shim is dual-mounted at /v1/messages AND /api/v1/messages, and the public
    # dev front only proxies /api/* to the API. Deriving the base by stripping
    # `/v1/models` off the same models_url opencode uses gets exactly the right
    # root for every base form: bare host -> https://host/api, an /api base ->
    # .../api, an explicit /v1 base -> its origin. Appending /v1/messages then
    # lands on the shim on every topology.
    api_base = models_url(central)[: -len("/v1/models")]
    os.environ[CLAUDE_BASE_ENV] = api_base
    # ALWAYS export a token, even on a keyless fleet: Claude Code treats a
    # missing ANTHROPIC_AUTH_TOKEN as "no credential" and falls back to its
    # own Anthropic login flow — a hugpy console must never demand a Claude
    # account. The placeholder is fine on an open fleet (the shim only
    # verifies keys when require_key is on); a gated fleet still needs the
    # real key configured, and gets a clean 401 from the shim instead of an
    # Anthropic login screen.
    os.environ[CLAUDE_AUTH_ENV] = key or "hugpy-open-fleet"
    _rebind_stdin_to_tty()
    init = (init_prompt if init_prompt is not None
            else os.environ.get("HUGPY_INIT_PROMPT", "")).strip()
    argv = [binary] + (["--append-system-prompt", init] if init else [])
    os.execvp(binary, argv)


# The env vars qwen-code (Qwen Code) reads: an OpenAI-compatible base that it
# appends `/chat/completions` to (so the /v1 mount itself), a bearer key, and
# a model id. "default" rides hugpy's resolve() fall-through to the served
# brain, so no model discovery is needed at launch.
QWEN_AUTH_ENV = "OPENAI_API_KEY"
QWEN_BASE_ENV = "OPENAI_BASE_URL"
QWEN_MODEL_ENV = "OPENAI_MODEL"

QWEN_INSTALL_HINT = """\
qwen (Qwen Code) not found. It is an optional peer — install it once:

    npm config set prefix ~/.npm-global
    npm install -g @qwen-code/qwen-code

then make sure ~/.npm-global/bin is on your PATH, e.g. add to ~/.bashrc:

    export PATH="$HOME/.npm-global/bin:$PATH"

hugpy-agent never auto-installs it; opencode remains the default frontend."""


def resolve_qwen() -> str | None:
    """Absolute path of the `qwen` (Qwen Code) binary, or None.

    Same discipline as resolve_opencode: PATH first, then the well-known
    npm-prefix location the install hint sets up. None means "not installed",
    never an error."""
    found = shutil.which("qwen")
    if found:
        return found
    candidate = os.path.expanduser(os.path.join("~", ".npm-global", "bin",
                                                "qwen"))
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None


def ensure_qwen_openai_auth(settings_path: str | None = None) -> None:
    """Pre-select qwen-code's "openai" auth type in ~/.qwen/settings.json.

    Without `security.auth.selectedType` qwen-code ignores the OPENAI_* env
    and blocks on its auth-selection dialog — which in `-p` mode is a silent
    hang. Only this one nested flag is merged in; every other setting is
    preserved and THE KEY NEVER LANDS IN THE FILE (same discipline as the
    other frontends). An existing file that fails to parse is left untouched
    — qwen then asks interactively rather than us clobbering user config.
    """
    path = settings_path or os.path.expanduser(os.path.join(
        "~", ".qwen", "settings.json"))
    settings: dict = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                settings = json.load(fh)
            if not isinstance(settings, dict):
                return
        except (OSError, ValueError):
            return
    sec = settings.setdefault("security", {})
    auth = sec.setdefault("auth", {}) if isinstance(sec, dict) else None
    if not isinstance(auth, dict) or auth.get("selectedType") == "openai":
        return
    auth["selectedType"] = "openai"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)


def launch_qwen_code(central: str, key: str, model: str | None = None,
                     binary: str | None = None) -> "None":
    """exec Qwen Code pointed at the fleet's OpenAI-compatible /v1.

    The fully Claude-free Claude-Code-style frontend: qwen-code's TUI speaks
    plain `/v1/chat/completions`, so it drives hugpy models with no Anthropic
    account, binary, or shim involved. Base = the /v1 mount (qwen appends
    `/chat/completions` itself); key is exported into the child env only
    (placeholder on an open fleet, same as the claude-code path); model is
    cfg.model — the AGENT BRAIN, named explicitly. Never "default": the
    server's no-preference fall-through is DEFAULT_CHAT_MODEL (a small fast
    chat model), and an agent-sized prompt can exceed its slot ctx. A
    pre-set OPENAI_MODEL is respected.

    Only returns by raising; on success the exec replaces the process.
    """
    binary = binary or resolve_qwen()
    if not binary:
        raise ConsoleError(QWEN_INSTALL_HINT)
    os.environ[QWEN_BASE_ENV] = qwen_base(central)
    os.environ[QWEN_AUTH_ENV] = key or "hugpy-open-fleet"
    os.environ.setdefault(QWEN_MODEL_ENV, model or "default")
    ensure_qwen_openai_auth()
    _rebind_stdin_to_tty()
    os.execvp(binary, [binary])


def qwen_base(central: str) -> str:
    """The OpenAI-compatible base for qwen-code: the /v1 mount itself.

    Derived by stripping `/models` off the same models_url every frontend
    uses, so every configured base form (bare host, /api, explicit /v1)
    lands on the right mount."""
    return models_url(central)[: -len("/models")]


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
                    tasks: "frozenset[str] | None" = TEXT_TASKS,
                    timeout: int = 30) -> tuple[dict, str]:
    """(models map, chosen default id) from the fleet's live /v1/models.

    Ported from the sync-models reference script. Filtering doctrine:
      * tasks must intersect `tasks` — chat-drivable only. ``tasks=None`` (the
        ``--all-models`` opt-in) DROPS the task filter and lists every
        non-blocked model — the operator's escape hatch when they want the
        whole fleet in the picker, accepting that a non-chat model (ASR,
        diffusion, embedding) selected in a coding agent will simply fail;
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
        if tasks and not (m_tasks & tasks):   # tasks=None -> no filter (all models)
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
            "the fleet at %s listed no %s models — nothing for OpenCode to serve"
            % (url, ("non-blocked" if not tasks
                     else "chat-drivable (tasks %s)" % sorted(tasks))))
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


def build_mct_config(base: str, model_label: str = "mct") -> dict:
    """opencode.json pointing at a local MCT shim instead of the fleet.

    Same provider mechanism as :func:`build_config` — OpenCode talks
    OpenAI-compatible to whatever ``baseURL`` names — so MCT drops into the seam
    that already exists rather than needing a frontend of its own.

    Two deliberate differences from the fleet config:

    * **No apiKey reference.** The shim binds loopback and authenticates
      nothing; writing an ``{env:...}`` pointer would imply a secret that does
      not exist.
    * **Permissions are ``deny``, not ``ask``.** Here OpenCode is C — a prompt
      and a display. The agent that edits files is A, working through B's act
      channel where every change is brokered, snapshotted and ledgered. If
      OpenCode also held its own edit/bash tools it would be a second, unaudited
      actor on the same machine, and the operator could not tell which one
      touched a file.
    """
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "mct": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "MCT (mediated context terminal)",
                "options": {"baseURL": base.rstrip("/")},
                "models": {model_label: {"name": "MCT — A via B"}},
            },
        },
        "model": "mct/" + model_label,
        "permission": {"edit": "deny", "bash": "deny", "webfetch": "deny"},
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
                print_config: bool = False,
                frontend: str = "opencode",
                all_models: bool = False) -> int:
    """The `hugpy-agent console` flow, factored out of cli.py for testing.

    `frontend` selects the terminal face. Default "opencode" keeps the existing
    behavior (generate opencode.json from /v1/models, exec OpenCode). The opt-in
    "claude-code" resolves the `claude` binary and execs it against the fleet's
    Anthropic Messages shim — NO opencode.json, no model-map sync (Claude Code
    discovers models itself); the key is exported into the child env only, never
    written to a file.

    opencode order: resolve workspace -> (sync? fetch+write config) ->
    print-config short-circuit -> launch. `--offline` implies no sync and
    additionally requires an existing opencode.json to be present. Returns an
    exit code; only the launch step never returns (exec).
    """
    if frontend == "claude-code":
        # Claude Code carries no per-workspace config we own; the whole seam is
        # the two env vars set in launch_claude_code. A missing binary raises
        # ConsoleError (cmd_console maps it to a non-zero exit + hint).
        launch_claude_code(cfg.base, cfg.api_key)
        return 0  # unreachable on success (exec)

    if frontend == "qwen-code":
        # Same no-config seam as claude-code, but fully Claude-free: qwen-code
        # speaks OpenAI /v1 directly. Env-only key, "default" model.
        launch_qwen_code(cfg.base, cfg.api_key, cfg.model)
        return 0  # unreachable on success (exec)

    ws = os.path.expanduser(workspace or DEFAULT_WORKSPACE)
    path = config_path(ws)

    do_sync = sync and not offline
    if do_sync:
        # --all-models (or HUGPY_CONSOLE_ALL_MODELS=1): drop the chat-drivable
        # task filter so the picker lists every non-blocked fleet model.
        env_all = os.environ.get("HUGPY_CONSOLE_ALL_MODELS", "").strip().lower() \
            in ("1", "true", "yes", "on")
        tasks = None if (all_models or env_all) else TEXT_TASKS
        models, default = fetch_model_map(cfg.base, cfg.api_key, tasks=tasks)
        chosen = model or default
        if chosen not in models:
            # An explicit --model that the fleet doesn't serve is honored
            # anyway (the operator may know a load is in flight), but say so.
            print("[console] note: model %r is not in the fleet's "
                  "%s list right now"
                  % (chosen, "model" if tasks is None else "chat-drivable"),
                  file=sys.stderr)
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
