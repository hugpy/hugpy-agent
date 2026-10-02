"""Configuration resolution for hugpy_agent.

Precedence (highest wins): process environment > `.env` in the workspace >
`agent.toml` in the workspace > built-in defaults.

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

from abstract_toolserver import hugpy_home

# The fleet pointer is resolved centrally (abstract_toolserver.hugpy_home.base:
# env → ~/.hugpy/.env → http://127.0.0.1:7002); no hugpy arm keeps its own.
DEFAULT_BASE = hugpy_home.DEFAULT_BASE
# Operator-chosen agent brain — BRAIN SWITCH 2026-07-17 (operator call on the
# P3.4 scorecard): Qwen3-Coder-Next replaces flux2-klein. Reliability over
# speed — coder passed 4/4 eval tasks vs klein's 3/4 (klein looped on
# glob_count); ~26s/task accepted. Tilde form = the fleet catalog key the
# workers serve under. Overridable by env/flag (HUGPY_AGENT_BRAIN, canonical;
# HUGPY_MODEL accepted as the legacy generic name).
#
# NAMED FOR WHAT IT IS (operator, 2026-07-17): the bare name DEFAULT_MODEL
# exists with OTHER meanings in other hugpy packages (central's chat default,
# the todo-keeper's model, the bot's DEFAULT_MODEL_KEY). Same-named keys
# resolve odd the moment a user gets specific in a shared .env — so the
# agent's constant says AGENT_BRAIN, unambiguously, everywhere.
DEFAULT_AGENT_BRAIN = "Qwen~Qwen3-Coder-Next-GGUF"
# Legacy alias — import site back-compat only (external clones may import the
# old name). New code must use DEFAULT_AGENT_BRAIN.
DEFAULT_MODEL = DEFAULT_AGENT_BRAIN
# Second-in-line agent brain (HUGPY_AGENT_BRAIN_2). Default EMPTY = feature
# off. When set, the loop checks at RUN START (never per step) which of the
# two brains a fleet worker actually has seated (/llm/workers) and starts on
# the resident one; a capacity-class refusal mid-run falls back to it once
# and sticks. Same knob family as the BRAIN switch above — an operator names
# a specific standby, nothing is ever auto-discovered.
DEFAULT_AGENT_BRAIN_2 = ""
# Ordered brain LADDER (HUGPY_AGENT_BRAINS, csv, best-first) — k96, operator
# ruling 2026-08-06 ("a priority brain list would be ideal, taking the path of
# least resistance"). Default EMPTY = ladder is derived from model/model_2
# exactly as before (full back-compat). When set, the run starts on the FIRST
# entry that is warm on a fleet worker, and walks DOWN one entry per
# capacity-class/permanent-verdict refusal mid-run (forward-only, never back).
# CONVENTION (documented, not enforced): the LAST entry is the PILOT LIGHT —
# a model small enough that a cold load is cheap, so a fleet with nothing warm
# still answers at reduced depth instead of aborting.
DEFAULT_AGENT_BRAINS: list = []
DEFAULT_MAX_STEPS = 25
DEFAULT_TIMEOUT = 300          # per-read socket timeout; cold model loads are slow by design
DEFAULT_MAX_TOKENS = 1024      # per agent step
DEFAULT_CTX_FALLBACK = 8192    # when /v1/models reports no context_length
DEFAULT_MAX_GENERATIONS = 2    # async GPU generations per run (remote_compute cap)
DEFAULT_ASK_TIMEOUT = 300      # seconds an escalation waits for the operator
DEFAULT_LOOP_GUARD_N = 3       # identical calls before the loop-guard nudge
DEFAULT_SUB_MAX_STEPS = 12     # step cap for a spawned subagent (P2.5)
DEFAULT_MAX_DEPTH = 2          # deepest subagent nesting level (root = 0)
DEFAULT_RAG_K = 5              # facts returned by recall / run-start auto-recall
DEFAULT_POLL_INTERVAL = 10     # seconds between serve task-source polls (P2.7)

# env-var name -> Config attribute
#
# NB `model` has TWO env names. HUGPY_MODEL is the original, generic-sounding
# knob; HUGPY_AGENT_BRAIN is the DEDICATED agent-brain variable (operator ask,
# 2026-07-17): other hugpy components have their own model knobs (the Discord
# bot's DEFAULT_MODEL_KEY predates this package), so the agent deserves a name
# that can't be confused or cross-configured on a box running several of them.
# Dict order is precedence within a layer (later wins): BRAIN beats MODEL when
# both are set in the same source; either alone works.
_ENV_KEYS = {
    "HUGPY_BASE": "base",
    "HUGPY_API_KEY": "api_key",
    "HUGPY_MODEL": "model",
    "HUGPY_AGENT_BRAIN": "model",
    "HUGPY_AGENT_BRAIN_2": "model_2",
    "HUGPY_AGENT_BRAINS": "brains",
    "HUGPY_MAX_STEPS": "max_steps",
    "HUGPY_TIMEOUT": "timeout",
    "HUGPY_MAX_TOKENS": "max_tokens",
    "HUGPY_TOOLS_MODE": "tools_mode",
    "HUGPY_MAX_GENERATIONS": "max_generations",
    "HUGPY_NO_THINK": "no_think",
    "HUGPY_POLICY": "policy_mode",
    "HUGPY_TOOL_ALLOW": "tool_allow",
    "HUGPY_TOOL_DENY": "tool_deny",
    "HUGPY_AUDIT_LOG": "audit_log",
    "HUGPY_AUDIT_VERBOSE": "audit_verbose",
    "HUGPY_DISCORD_SESSION": "discord_session",
    "HUGPY_DISCORD_MINT": "discord_mint",
    "HUGPY_DISCORD_CHANNEL": "discord_channel",
    "HUGPY_ASK_TIMEOUT": "ask_timeout",
    "HUGPY_LOOP_GUARD_N": "loop_guard_n",
    "HUGPY_OBS_CAP_CHARS": "observation_cap_chars",
    "HUGPY_SUB_MAX_STEPS": "sub_max_steps",
    "HUGPY_MAX_DEPTH": "max_depth",
    "HUGPY_RAG": "rag_enabled",
    "HUGPY_RAG_K": "rag_k",
    "HUGPY_TASK_SOURCE": "task_source",
    "HUGPY_TASK_QUEUE": "task_queue",
    "HUGPY_POLL_INTERVAL": "poll_interval",
    "HUGPY_AGENT_CENTRAL": "agent_central",
    "HUGPY_AGENT_NODE": "agent_node",
    "HUGPY_AGENT_NAME": "agent_name",
    "HUGPY_AGENT_CAPABILITIES": "agent_capabilities",
    "HUGPY_AGENT_STATE": "agent_state",
    # Toolserver bridge (2026-09-25): the running abstract_toolserver's whole
    # tool surface behind three category meta-tools, default ON.
    "HUGPY_AGENT_TOOLSERVER": "toolserver",
    "HUGPY_AGENT_TOOLSERVER_URL": "toolserver_url",
    "HUGPY_AGENT_TOOLSERVER_TOKEN": "toolserver_token",
    "HUGPY_AGENT_LOCUS": "toolserver_locus",
    # Allowlist + registration mode for the shared toolserver client
    # (hugpy_agent.toolserver_client): comma lists of tool names ('*' and
    # 'vm_*' globs accepted); tools = meta (three ts_* tools) | flat.
    "HUGPY_AGENT_TOOLSERVER_ALLOW": "toolserver_allow",
    "HUGPY_AGENT_TOOLSERVER_DENY": "toolserver_deny",
    "HUGPY_AGENT_TOOLSERVER_TOOLS": "toolserver_tools",
}
_INT_FIELDS = {"max_steps", "timeout", "max_tokens", "max_generations",
               "ask_timeout", "loop_guard_n", "observation_cap_chars",
               "sub_max_steps", "max_depth",
               "rag_k", "poll_interval"}
_BOOL_FIELDS = {"no_think", "audit_verbose", "discord_mint", "rag_enabled",
                "agent_node", "toolserver"}
_LIST_FIELDS = {"tool_allow", "tool_deny",   # comma-separated in env/.env
                "agent_capabilities", "brains",
                "toolserver_allow", "toolserver_deny"}
# Attributes where an EXPLICIT empty value is meaningful (it disables the
# feature) rather than "unset". Everywhere else an empty value is skipped.
_EMPTY_DISABLES = {"audit_log"}
# All settable attributes, in precedence-application order (shared by the
# agent.toml and CLI-override passes so a new knob is wired in one place).
_SETTABLE = ("base", "api_key", "model", "model_2", "brains",
             "max_steps", "timeout",
             "max_tokens", "tools_mode", "max_generations", "no_think",
             "policy_mode", "tool_allow", "tool_deny",
             "audit_log", "audit_verbose",
             "discord_session", "discord_mint", "discord_channel",
             "ask_timeout", "loop_guard_n", "observation_cap_chars",
               "sub_max_steps", "max_depth",
             "rag_enabled", "rag_k",
             "task_source", "task_queue", "poll_interval",
             "agent_central", "agent_node", "agent_name",
             "agent_capabilities", "agent_state",
             "toolserver", "toolserver_url", "toolserver_token",
             "toolserver_locus", "toolserver_allow", "toolserver_deny",
             "toolserver_tools")


def _as_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return None


@dataclass
class Config:
    base: str = field(default_factory=hugpy_home.base)
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
    # (ts_categories/ts_list/ts_call) that reach the running abstract_toolserver
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
    # meta (default; ts_categories/ts_list/ts_call) | flat (every allowed tool
    # registered as its own ToolSpec — native tool-calling models).
    toolserver_allow: list = field(default_factory=list)
    toolserver_deny: list = field(default_factory=list)
    toolserver_tools: str = "meta"
    ctx_fallback: int = DEFAULT_CTX_FALLBACK
    sources: dict = field(default_factory=dict)  # attr -> where it came from (audit aid)


def _parse_env_file(path: str) -> dict:
    """Minimal .env parser: KEY=VALUE lines, '#' comments, optional quotes.
    Deliberately no interpolation/escapes — predictability over features."""
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k:
            out[k] = v
    return out


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

    # 3. .env in the workspace (HUGPY_* names, same as the environment).
    # An empty value normally means "unset" (skip); for _EMPTY_DISABLES
    # attrs an explicit empty is an off-switch and must be applied.
    env_file = _parse_env_file(os.path.join(cfg.workspace, ".env"))
    for env_key, attr in _ENV_KEYS.items():
        val = env_file.get(env_key)
        if val or (val == "" and attr in _EMPTY_DISABLES):
            _set(cfg, attr, val, ".env")

    # 4. process environment.
    for env_key, attr in _ENV_KEYS.items():
        val = env.get(env_key)
        if val or (val == "" and attr in _EMPTY_DISABLES):
            _set(cfg, attr, val, "env")

    # 5. CLI overrides.
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
