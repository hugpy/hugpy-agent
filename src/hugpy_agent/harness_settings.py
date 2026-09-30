"""Uniform harness settings — one table, each harness's NATIVE mechanism.

Two operator settings apply to every terminal harness hugpy-agent launches
(`frontends.REGISTRY`), behind the same hugpy-agent options:

* **allow_all** (`--allow-all` / `--yolo`; env default off) — start the harness
  with every permission/approval prompt bypassed, for THAT launch only. Shared
  generated configs (opencode.json, the Hermes profile) keep their prompting
  posture; the bypass rides argv/env.
* **small_model** (`--small-model`; default ``hugpy/Qwen2.5-Coder-1.5B-Instruct-
  GGUF``) — the model a harness uses for session titles / lightweight side
  calls, so those never spend the session's agent brain.

Resolution (per harness ``h`` with env prefix ``P``, e.g. HUGPY_OPENCODE):

* allow_all:   ``P_ALLOW_ALL`` if set, else ``HUGPY_HARNESS_ALLOW_ALL``; default 0.
* small_model: ``P_SMALL_MODEL`` if set, else ``HUGPY_HARNESS_SMALL_MODEL``,
  else DEFAULT_SMALL_MODEL. ``off``/``none``/empty disables it (the harness's
  own fall-back-to-main-model behaviour). A ``hugpy/`` prefix is accepted.

The CLI flags export the generic ``HUGPY_HARNESS_*`` vars so they survive the
``console`` re-exec that the OpenCode / Claude Code / Qwen Code adapters use.

``harness_env`` is the single env-assembly hook for the harness child
environment (see ENV_HOOKS) — other layers (e.g. session identity) register
there rather than editing each adapter.
"""
from __future__ import annotations

import json
import os

ALLOW_ALL_ENV = "HUGPY_HARNESS_ALLOW_ALL"
SMALL_MODEL_ENV = "HUGPY_HARNESS_SMALL_MODEL"
DEFAULT_SMALL_MODEL = "Qwen2.5-Coder-1.5B-Instruct-GGUF"
BANNER = "%s: all permissions ALLOWED (--allow-all)"

_TRUE = ("1", "true", "yes", "on")
_OFF = ("", "off", "none", "0", "false")

# OpenCode permission keys (opencode 1.18.33 bundled docs: "Known permission
# keys"); OPENCODE_PERMISSION all-allow names every one of them.
OPENCODE_PERMISSION_KEYS = (
    "read", "edit", "glob", "grep", "list", "bash", "task",
    "external_directory", "todowrite", "question", "webfetch", "websearch",
    "lsp", "doom_loop", "skill")

# THE table: harness -> how allow_all and small_model are expressed natively.
# `verified` names the installed version whose --help/source was checked.
HARNESSES = {
    "opencode": {
        "env_prefix": "HUGPY_OPENCODE",
        "allow_all_argv": ["--auto"],
        "allow_all_env": {"OPENCODE_PERMISSION": json.dumps(
            {k: "allow" for k in OPENCODE_PERMISSION_KEYS})},
        "small_model": "opencode.json top-level small_model = hugpy/<id> "
                       "(title agent: agent.title.model ?? small_model ?? main)",
        "verified": "opencode 1.18.33",
    },
    "claude-code": {
        "env_prefix": "HUGPY_CLAUDE",
        "allow_all_argv": ["--dangerously-skip-permissions"],
        "allow_all_env": {},
        # Only ever applied by launch_claude_code, which always points Claude
        # Code at the hugpy /v1/messages shim — never at Anthropic directly.
        "small_model_env": ("ANTHROPIC_SMALL_FAST_MODEL",
                            "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
        "small_model": "env ANTHROPIC_SMALL_FAST_MODEL + "
                       "ANTHROPIC_DEFAULT_HAIKU_MODEL (hugpy shim only)",
        "verified": "claude 2.1.285",
    },
    "qwen-code": {
        "env_prefix": "HUGPY_QWEN",
        "allow_all_argv": ["--yolo"],
        "allow_all_env": {},
        "small_model": "settings fastModel (drives session titles) via a "
                       "derived QWEN_CODE_SYSTEM_SETTINGS_PATH file",
        "verified": "qwen 0.22.2",
    },
    "hermes": {
        "env_prefix": "HUGPY_HERMES",
        "allow_all_argv": ["--yolo"],
        "allow_all_env": {},
        "small_model": "profile config.yaml auxiliary.title_generation "
                       "{provider: custom:hugpy, model}",
        "verified": "hermes 0.21.5",
    },
    "aider": {
        "env_prefix": "HUGPY_AIDER",
        "allow_all_argv": ["--yes-always"],
        "allow_all_env": {},
        "small_model": "argv --weak-model openai/<id> (commit messages, "
                       "history summaries)",
        "verified": "aider 0.86.2",
    },
}


def _env(environ):
    return os.environ if environ is None else environ


def _prefix(harness: str) -> str:
    return HARNESSES[harness]["env_prefix"]


def allow_all(harness: str, environ=None) -> bool:
    env = _env(environ)
    own = env.get(_prefix(harness) + "_ALLOW_ALL")
    raw = own if own is not None else env.get(ALLOW_ALL_ENV, "0")
    return raw.strip().lower() in _TRUE


def small_model(harness: str, environ=None) -> str | None:
    """Bare fleet id of the harness's small/title model, or None (disabled)."""
    env = _env(environ)
    own = env.get(_prefix(harness) + "_SMALL_MODEL")
    raw = own if own is not None else env.get(SMALL_MODEL_ENV, DEFAULT_SMALL_MODEL)
    raw = raw.strip()
    if raw.lower() in _OFF:
        return None
    return raw[len("hugpy/"):] if raw.startswith("hugpy/") else raw


def allow_all_spec(harness: str, environ=None) -> tuple[list, dict]:
    """(argv additions, env additions) for allow_all, or ([], {}) when off."""
    if not allow_all(harness, environ):
        return [], {}
    row = HARNESSES[harness]
    return list(row["allow_all_argv"]), dict(row["allow_all_env"])


def banner(harness: str) -> str:
    return BANNER % harness


def scrub(env) -> None:
    """Drop the hugpy control vars from a FINAL harness env, so a hugpy-agent
    launched from inside the harness does not silently inherit --allow-all."""
    env.pop(ALLOW_ALL_ENV, None)
    for row in HARNESSES.values():
        env.pop(row["env_prefix"] + "_ALLOW_ALL", None)


# Hook: callables (harness_id, env) -> None, run on every harness child env
# assembled by frontends.prepare. Register here; do not patch each adapter.
ENV_HOOKS: list = []


def harness_env(harness: str, env: dict, extra: dict | None = None) -> dict:
    """Single extension point for the harness child environment."""
    if extra:
        env.update(extra)
    for hook in list(ENV_HOOKS):
        hook(harness, env)
    return env
