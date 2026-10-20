"""Configuration resolution for hugpy_agent.

Precedence (highest wins): process environment > `.env` in the workspace >
installed per-user credential file > `agent.toml` in the workspace > defaults.

WHY this order: env vars are the deployment seam (systemd units, CI, one-off
overrides); `.env` is per-workspace operator config that must never be
committed; `agent.toml` is committable, non-secret project config. The
workspace itself is resolved FIRST (env/CLI only, default cwd) because the
other two sources live inside it — a `.env` cannot relocate the workspace it
was found in.

No secrets are ever hardcoded here; `HUGPY_API_KEY` has no default.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

def _resolve_default_base() -> str:
    """Base for an UNCONFIGURED agent, resolved exactly like the harness
    (hugpy_harness.cli.chat.local_endpoint): a local wrapper endpoint
    (HUGPY_WRAPPER_URL / wrapper.endpoint.json), else the fleet endpoint
    (HUGPY_FLEET_URL / fleet.endpoint.json), else hugpy-core on this box
    (127.0.0.1:7020, where basic core listens — not the dead :7002). HUGPY_BASE
    and HUGPY_URL are applied by load_config's env layer, which outranks this
    default, so explicit config always wins."""
    try:
        from hugpy_harness.cli.chat import local_endpoint
        endpoint = local_endpoint()
        if endpoint:
            return endpoint
    except Exception:
        pass
    return "http://127.0.0.1:7020"


# Resolved at import so ``from hugpy_agent.config import DEFAULT_BASE`` keeps a
# concrete value; the Config field below re-resolves per construction so a
# wrapper/fleet endpoint written after import is still picked up.
DEFAULT_BASE = _resolve_default_base()

# Static defaults and the env-key tables now live in the shared imports layer
# (re-exported here so ``from hugpy_agent.config import DEFAULT_*`` keeps working).
from .imports.constants import (  # noqa: F401
    DEFAULT_AGENT_BRAIN,
    DEFAULT_MODEL,
    DEFAULT_AGENT_BRAIN_2,
    DEFAULT_AGENT_BRAINS,
    DEFAULT_MAX_STEPS,
    DEFAULT_TIMEOUT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_CTX_FALLBACK,
    DEFAULT_MAX_GENERATIONS,
    DEFAULT_ASK_TIMEOUT,
    DEFAULT_LOOP_GUARD_N,
    DEFAULT_SUB_MAX_STEPS,
    DEFAULT_MAX_DEPTH,
    DEFAULT_RAG_K,
    DEFAULT_POLL_INTERVAL,
    _ENV_KEYS,
    _INT_FIELDS,
    _BOOL_FIELDS,
    _LIST_FIELDS,
    _EMPTY_DISABLES,
    _SETTABLE,
)
from .imports.functions import as_bool as _as_bool  # noqa: F401
from .imports.functions import parse_env_file as _parse_env_file  # noqa: F401


@dataclass
class Config:
    base: str = field(default_factory=_resolve_default_base)
    api_key: str = ""
    model: str = DEFAULT_AGENT_BRAIN
    # Second-in-line brain (HUGPY_AGENT_BRAIN_2). Empty (the default) turns
    # the feature off entirely: no /llm/workers probe at run start, no
    # capacity fallback mid-run. See loop._select_brain for the semantics.
    model_2: str = DEFAULT_AGENT_BRAIN_2
    # Ordered brain ladder (HUGPY_AGENT_BRAINS, csv, best-first; last entry =
    # the pilot light by convention). Empty (the default) derives the ladder
    # from model/model_2 — see gateway.resolve_brain_ladder for the semantics.
    brains: list = field(default_factory=list)
    workspace: str = "."
    max_steps: int = DEFAULT_MAX_STEPS
    timeout: int = DEFAULT_TIMEOUT
    max_tokens: int = DEFAULT_MAX_TOKENS
    # prompted | constrained | auto | native. Default PROMPTED: it always
    # works and makes ZERO probe traffic. `auto`/`native` opt in to the
    # native-support probe (one tiny live call, file-cached per box). After
    # the 2026-07-14 dev incident, no probe is ever emitted unasked.
    tools_mode: str = "prompted"
    # Per-run ceiling on async generation calls (image/video jobs). GPU spend
    # is the fleet's scarcest resource — a looping model must hit a structured
    # refusal, not drain a worker (post-incident doctrine, design §6 Ph1.5).
    max_generations: int = DEFAULT_MAX_GENERATIONS
    # Suppress model "thinking": append ` /no_think` to the WIRE copy of the
    # latest user message on every chat call (never to stored history).
    # Default FALSE since the 2026-07-17 brain switch: the coder brain doesn't
    # think-loop, and omitting the suffix measured ~10% faster per task
    # (confirmed on the P3.4 eval suite). Set HUGPY_NO_THINK=true when pointing
    # at a THINKING-family brain (e.g. the old klein default), which otherwise
    # burns the whole budget inside <think>. Think spans are ALWAYS stripped
    # from output regardless (adapter.strip_think) as belt-and-suspenders.
    no_think: bool = False
    # Policy engine (P2.1): readonly | ask | auto. Default ASK — with no
    # escalation channel installed (P2.3) an `ask` decision fails closed to
    # deny, so a fresh deployment cannot mutate anything until the operator
    # explicitly chooses `auto` or wires up approvals. policy.decide() treats
    # any unrecognized value as `ask` (a typo must tighten, never open).
    policy_mode: str = "ask"
    # Per-tool-name overrides for policy.decide(); explicit deny beats
    # explicit allow beats the mode default. Comma-separated in env/.env.
    tool_allow: list = field(default_factory=list)
    tool_deny: list = field(default_factory=list)
    # Audit trail (P2.2): append-only JSONL, one line per tool call. None
    # means "use the default <workspace>/.hugpy_agent/audit.jsonl" (resolved
    # by the loop, since the workspace isn't known here); an explicit EMPTY
    # string (HUGPY_AUDIT_LOG=) disables auditing entirely. Args/results are
    # recorded as sha256 hashes; audit_verbose (HUGPY_AUDIT_VERBOSE /
    # --audit-verbose) opts into truncated plaintext for debugging.
    audit_log: str | None = None
    audit_verbose: bool = False
    # Operator comms (P2.3). `discord_session` is the FULL session endpoint
    # URL (…/api/discord/session/<token>) — the token inside it is a secret,
    # so it belongs in env/.env, never a committed file, and is never logged.
    # Alternative: discord_mint=1 + discord_channel mints a session on first
    # use (in-run token only; the mint route is operator-gated so the
    # api_key rides along). Nothing configured => `ask` decisions FAIL
    # CLOSED to deny (no operator channel).
    discord_session: str = ""
    discord_mint: bool = False
    discord_channel: str = ""
    # Seconds an escalation blocks waiting for the operator's click before
    # the poll gives up and the action is denied as data.
    ask_timeout: int = DEFAULT_ASK_TIMEOUT
    # Agent-level loop-guard (P2.4): after N consecutive identical tool
    # calls — same (tool_name, args_sha256), no distinct call in between —
    # the loop injects one strong nudge; at 2N it aborts the run with
    # outcome "looping" (fail fast, don't burn the step cap on a weak model
    # spinning). 0 disables the guard entirely.
    loop_guard_n: int = DEFAULT_LOOP_GUARD_N
    # Conversation copy of ONE tool result is clipped past this (journal keeps
    # the full result). 12k chars ~ 4k tokens: an 84KB observation on a
    # 32k-ctx brain squeezed later completions to nothing (sentinel case runs
    # died mid-tool-call, 2026-08-06). 0 disables.
    observation_cap_chars: int = 12_000
    # Subagents (P2.5). `sub_max_steps` is a hard ceiling on any spawned
    # child's step budget (a spawn may ask for less, never more). `max_depth`
    # bounds nesting: a loop at depth >= max_depth gets no `spawn` tool at
    # all (root run = depth 0, its children depth 1, ...). Both exist so a
    # delegating model cannot mint itself unbounded work — the same fail-
    # closed posture as max_generations, applied to steps and recursion.
    sub_max_steps: int = DEFAULT_SUB_MAX_STEPS
    max_depth: int = DEFAULT_MAX_DEPTH
    # Embed-RAG memory (P2.6). `rag_enabled` (HUGPY_RAG) gates the whole
    # index: off means no vector writes on remember, no recall tool, and no
    # run-start auto-recall — an operator switch for boxes whose embed
    # endpoint is known-dead (a dead endpoint already degrades cleanly, but
    # the off-switch also skips the bounded attempt). `rag_k` is the top-k
    # for both the recall tool default and the run-start auto-recall pin.
    rag_enabled: bool = True
    rag_k: int = DEFAULT_RAG_K
    # Daemon task source (P2.7, `hugpy-agent serve`): where the daemon polls
    # for work. "discord-inbox" watches the operator session's /messages for
    # `task: ...` messages (needs discord_session); "queue" consumes a local
    # file one task per line (task_queue, default
    # <workspace>/.hugpy_agent/tasks.queue). EMPTY (the default) means no
    # source is configured and serve FAILS CLOSED to an idle heartbeat loop —
    # a fresh unit must never execute work nobody configured it to receive.
    task_source: str = ""
    task_queue: str | None = None
    # Seconds between serve polls of the task source (and idle heartbeat
    # bookkeeping). Clamped to >= 1 by the daemon so a typo'd 0 cannot
    # busy-spin a box.
    poll_interval: int = DEFAULT_POLL_INTERVAL
    # Agent node mode (P3.2, `hugpy-agent serve --node`): enrol with central's
    # /agent/* registry, heartbeat, and pull operator-dispatched tasks. This is
    # the fleet counterpart of the daemon's local task sources — it runs
    # ALONGSIDE any `task_source` (both are polled) or on its own.
    #   `agent_central` (HUGPY_AGENT_CENTRAL) is the base URL of central's
    # /agent/* routes; EMPTY falls back to `base` (the /api dual-mount already
    # serves /agent there). `agent_node` (HUGPY_AGENT_NODE, or `serve --node`)
    # turns the mode on. `agent_name`/`agent_capabilities` are what the node
    # advertises at register (defaults: the box hostname / ["chat", "tools"]).
    # `agent_state` overrides where the persisted {id, token, cursor} live
    # (default <workspace>/.hugpy_agent/node_state.json, written 0600). The
    # enrol token is a SECRET — it is minted once by register, kept only in that
    # 0600 state file, and NEVER logged or committed (the file's dir is
    # gitignored). Nothing here is a secret at rest EXCEPT that state file.
    agent_central: str = ""
    agent_node: bool = False
    agent_name: str = ""
    agent_capabilities: list = field(default_factory=list)
    agent_state: str = ""
    # Toolserver bridge (2026-09-25). Default ON: when this runs and a base url
    # resolves (the default always does) and a bounded probe of POST
    # /ts/categories succeeds, the agent gains three category meta-tools
    # (toolserver_list_categories/toolserver_list_tools/toolserver_call_tool) that reach the running abstract_toolserver
    # — the messaging lane (comms_*, channel_*) plus everything else. An
    # unreachable/unauthorized toolserver is stated once and the agent runs
    # normally without them (errors-as-data). `toolserver` (HUGPY_AGENT_TOOLSERVER
    # =0) opts out. `toolserver_url`/`toolserver_token` override the resolution
    # chain (env TOOLSERVER_URL/…_TOKEN then the operator env files, with the
    # disk-token safety rule); empty means "resolve". `toolserver_locus`
    # (HUGPY_AGENT_LOCUS) is the agent's stable comms identity — empty derives it
    # from agent_name, else the short hostname — so comms messages to/from the
    # agent route. The token is a SECRET: env/.env only, never agent.toml.
    toolserver: bool = True
    toolserver_url: str = ""
    toolserver_token: str = ""
    toolserver_locus: str = ""
    # Allowlist (toolserver_client.classify): readonly + mutating tools are on
    # by default; PRIVILEGED ones (vm_*, vmpool_*, sys_*, browser_*,
    # fs_write_file, db_query writes, oauth/session control) need an explicit
    # entry here ('*' opens all). deny wins over allow. `toolserver_tools`:
    # meta (default; toolserver_list_categories/toolserver_list_tools/toolserver_call_tool) | flat (every allowed tool
    # registered as its own ToolSpec — native tool-calling models).
    toolserver_allow: list = field(default_factory=list)
    toolserver_deny: list = field(default_factory=list)
    toolserver_tools: str = "meta"
    ctx_fallback: int = DEFAULT_CTX_FALLBACK
    # Per-workspace state directory (journal, audit, RAG vectors), relative to
    # the workspace. hugpy-agent keeps its own ``.hugpy_agent`` (the portable
    # harness core defaults to ``.hugpy_wrapper/chat``); the harness loop reads
    # this to place default_journal_path / default_audit_path.
    state_dir: str = ".hugpy_agent"
    sources: dict = field(default_factory=dict)  # attr -> where it came from (audit aid)


def _parse_toml(path: str) -> dict:
    """Parse agent.toml. Uses stdlib tomllib on 3.11+; on 3.10 falls back to a
    minimal flat parser (key = "value" | int | true/false, top level or under
    [agent]) — enough for our config surface, keeping the >=3.10 promise."""
    try:
        import tomllib  # Python 3.11+
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except OSError:
            return {}
        except Exception:
            return {}  # malformed config must not crash startup; env still works
        if isinstance(data.get("agent"), dict):
            merged = dict(data)
            merged.pop("agent")
            merged.update(data["agent"])
            return merged
        return data
    except ImportError:
        pass
    out: dict = {}
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return out
    section = ""
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section not in ("", "agent") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            out[k] = v[1:-1]
        elif v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        else:
            try:
                out[k] = int(v)
            except ValueError:
                out[k] = v
    return out


def load_config(workspace: str | None = None, overrides: dict | None = None,
                environ: dict | None = None) -> Config:
    """Resolve the effective Config.

    `overrides` (from CLI flags) beat everything — an operator typing a flag is
    the most explicit intent there is. `workspace` is the next most explicit: a
    caller naming the directory outranks an ambient env var, which outranks cwd.
    `environ` is injectable for tests.
    """
    env = os.environ if environ is None else environ
    overrides = {k: v for k, v in (overrides or {}).items() if v not in (None, "")}

    cfg = Config()
    # 1. workspace first: CLI > argument > env > cwd (its files feed the rest).
    # The `workspace` argument was previously declared and then never read, so
    # every caller passing one silently got cwd instead — and with it whatever
    # agent.toml/.env happened to sit there. Honour it.
    ws = (overrides.get("workspace") or workspace
          or env.get("HUGPY_WORKSPACE") or os.getcwd())
    cfg.workspace = os.path.realpath(ws)
    cfg.sources["workspace"] = ("cli" if "workspace" in overrides
                                else "argument" if workspace
                                else "env" if env.get("HUGPY_WORKSPACE") else "default")

    # 2. lowest layer: agent.toml (keys are attr names, e.g. `model = "..."`).
    toml_vals = _parse_toml(os.path.join(cfg.workspace, "agent.toml"))
    for attr in _SETTABLE:
        if attr in toml_vals and (toml_vals[attr] != ""
                                  or attr in _EMPTY_DISABLES):
            _set(cfg, attr, toml_vals[attr], "agent.toml")

    # 3. A downloaded install link writes this private per-user file. Keep it
    # below workspace and process settings so explicit choices still win.
    installed = _parse_env_file(os.path.join(env.get("HOME") or os.path.expanduser("~"),
                                             ".config", "hugpy-agent", "agent.env"))
    for env_key, attr in _ENV_KEYS.items():
        val = installed.get(env_key)
        if val or (val == "" and attr in _EMPTY_DISABLES):
            _set(cfg, attr, val, "installed")

    # 4. .env in the workspace (HUGPY_* names, same as the environment).
    # An empty value normally means "unset" (skip); for _EMPTY_DISABLES
    # attrs an explicit empty is an off-switch and must be applied.
    env_file = _parse_env_file(os.path.join(cfg.workspace, ".env"))
    for env_key, attr in _ENV_KEYS.items():
        val = env_file.get(env_key)
        if val or (val == "" and attr in _EMPTY_DISABLES):
            _set(cfg, attr, val, ".env")

    # 5. process environment.
    for env_key, attr in _ENV_KEYS.items():
        val = env.get(env_key)
        if val or (val == "" and attr in _EMPTY_DISABLES):
            _set(cfg, attr, val, "env")

    # 6. CLI overrides.
    for attr in _SETTABLE:
        if attr in overrides:
            _set(cfg, attr, overrides[attr], "cli")
    return cfg


def _set(cfg: Config, attr: str, value, source: str) -> None:
    if attr in _INT_FIELDS:
        try:
            value = int(value)
        except (TypeError, ValueError):
            return  # a garbage number must not take down the agent; keep prior
    elif attr in _BOOL_FIELDS:
        parsed = _as_bool(value)
        if parsed is None:
            return  # unrecognized truthiness word: keep the prior value
        value = parsed
    elif attr in _LIST_FIELDS:
        # env/.env carry comma-separated names; agent.toml may carry a real
        # list. Either way the Config attribute is a clean list of names.
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        elif isinstance(value, (list, tuple)):
            value = [str(v).strip() for v in value if str(v).strip()]
        else:
            return  # unusable shape: keep the prior value
    setattr(cfg, attr, value)
    cfg.sources[attr] = source
