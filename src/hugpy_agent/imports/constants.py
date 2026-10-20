"""Static defaults, env-var names, and path constants for hugpy_agent.

Dependency-light: this module imports nothing but the standard library, so the
shared primitive layer never pulls a backend or a vendor SDK in. Runtime
configuration (resolving an effective ``Config``) stays with ``config.py``;
only the fixed values and the env-key tables live here, in one place.
"""

# ── Agent brain (model) defaults ───────────────────────────────────────────
# Operator-chosen agent brain — BRAIN SWITCH 2026-07-17. Tilde form = the fleet
# catalog key workers serve under. Overridable by HUGPY_AGENT_BRAIN (canonical)
# or HUGPY_MODEL (legacy generic name).
DEFAULT_AGENT_BRAIN = "Qwen~Qwen3-Coder-Next-GGUF"
# Legacy alias — import-site back-compat only. New code uses DEFAULT_AGENT_BRAIN.
DEFAULT_MODEL = DEFAULT_AGENT_BRAIN
# Second-in-line agent brain (HUGPY_AGENT_BRAIN_2). Empty = feature off.
DEFAULT_AGENT_BRAIN_2 = ""
# Ordered brain LADDER (HUGPY_AGENT_BRAINS, csv, best-first). Empty = derive
# the ladder from model/model_2 (full back-compat).
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

# ── Config env-var tables ───────────────────────────────────────────────────
# env-var name -> Config attribute. Dict order is precedence within a layer
# (later wins): HUGPY_AGENT_BRAIN beats HUGPY_MODEL when both are set in the
# same source; either alone works.
_ENV_KEYS = {
    # HUGPY_URL is accepted as an alias of HUGPY_BASE (the harness honours both);
    # HUGPY_BASE comes AFTER it so HUGPY_BASE wins when both are set (later keys
    # overwrite earlier ones in the env/.env application passes).
    "HUGPY_URL": "base",
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
    # Toolserver bridge (2026-09-25).
    "HUGPY_AGENT_TOOLSERVER": "toolserver",
    "HUGPY_AGENT_TOOLSERVER_URL": "toolserver_url",
    "HUGPY_AGENT_TOOLSERVER_TOKEN": "toolserver_token",
    "HUGPY_AGENT_LOCUS": "toolserver_locus",
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

# ── Runtime home + path constants (see functions.hugpy_home / _relocate) ─────
ENV_HUGPY_HOME = "HUGPY_HOME"
ENV_HUGPY_WORKSPACE = "HUGPY_WORKSPACE"
HUGPY_HOME_DIRNAME = ".hugpy"            # default under $HOME
LEGACY_MCT_DIR = ".mct"                  # migrated into HUGPY_HOME/mct
LEGACY_CONSOLE_DIR = ".hugpy_agent/console"  # migrated into HUGPY_HOME/agent/console

# ── Standalone agent console service (shipped by the hugpy-agent .deb) ───────
# The packaged arm (UI at /fleet/ + toolset API under /api/agent/*) runs as the
# hugpy-agent console server. Host/port and the optional env file are read at
# serve time; nothing here opens a socket. Env names carry the HUGPY_AGENT_CONSOLE
# / HUGPY_AGENT_{LINKS,ARTIFACTS}_DIR prefixes so they never clash with the
# agent's model/node config keys in _ENV_KEYS above. The arm identity is "agent";
# the UI path stays /fleet/ (the legacy public route, not the legacy deb name).
ENV_CONSOLE_HOST = "HUGPY_AGENT_CONSOLE_HOST"
ENV_CONSOLE_PORT = "HUGPY_AGENT_CONSOLE_PORT"
ENV_CONSOLE_ARTIFACTS_DIR = "HUGPY_AGENT_ARTIFACTS_DIR"
ENV_CONSOLE_LINKS_DIR = "HUGPY_AGENT_LINKS_DIR"
DEFAULT_CONSOLE_HOST = "127.0.0.1"
DEFAULT_CONSOLE_PORT = 7342
AGENT_ENV_FILE = "/etc/hugpy/agent.env"
ARM_NAME = "agent"
UI_BASE = "/fleet/"
API_MOUNT = "/api"
