"""Terminal agent adapters. Fleet connection overrides are child-process only.

Hermes: https://hermes-agent.nousresearch.com/docs/integrations/providers/
Aider: https://aider.chat/docs/llms/openai-compat.html
"""
from __future__ import annotations

import os
import json
import shutil
import subprocess
import sys
import tempfile

from . import console
from . import harness_settings as hs
from . import session_signals  # registers the identity ENV_HOOK
from .fleet_console import FleetError


REGISTRY = (
    {"id": "hermes", "name": "Hermes Agent", "binary": "hermes",
     "protocol": "OpenAI-compatible", "docs": "https://hermes-agent.nousresearch.com/docs/getting-started/installation/",
     "install": ["bash", "-c", "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"],
     "install_text": "official Hermes per-user installer"},
    {"id": "claude-code", "name": "Claude Code", "binary": "claude",
     "protocol": "Hugpy Anthropic shim", "docs": "https://code.claude.com/docs/en/setup",
     "install": ["npm", "install", "-g", "@anthropic-ai/claude-code"],
     "install_text": "npm install -g @anthropic-ai/claude-code"},
    {"id": "qwen-code", "name": "Qwen Code", "binary": "qwen",
     "protocol": "OpenAI-compatible", "docs": "https://qwenlm.github.io/qwen-code-docs/en/users/overview/",
     "install": ["npm", "install", "-g", "@qwen-code/qwen-code"],
     "install_text": "npm install -g @qwen-code/qwen-code"},
    {"id": "opencode", "name": "OpenCode", "binary": "opencode",
     "protocol": "OpenAI-compatible", "docs": "https://opencode.ai/docs/",
     "install": ["npm", "install", "-g", "opencode-ai"],
     "install_text": "npm install -g opencode-ai"},
    {"id": "aider", "name": "Aider", "binary": "aider",
     "protocol": "OpenAI-compatible", "docs": "https://aider.chat/docs/install.html",
     "install": ["bash", "-c", "curl -LsSf https://aider.chat/install.sh | sh"],
     "install_text": "official Aider per-user installer"},
)


def resolve(spec):
    resolvers = {"claude-code": console.resolve_claude, "qwen-code": console.resolve_qwen,
                 "opencode": console.resolve_opencode}
    if spec["id"] in resolvers:
        return resolvers[spec["id"]]()
    found = shutil.which(spec["binary"])
    if found:
        return found
    candidates = [os.path.expanduser("~/.local/bin/" + spec["binary"])]
    if spec["id"] == "hermes":
        candidates += [os.path.expanduser("~/.hermes/hermes-agent/venv/bin/hermes"),
                       os.path.expanduser("~/.hermes/venv/bin/hermes")]
    return next((p for p in candidates if os.path.isfile(p) and os.access(p, os.X_OK)), None)


def available():
    return [{**spec, "path": resolve(spec)} for spec in REGISTRY]


def install(spec):
    """Run the registry's fixed, official installer; never interpolate input."""
    argv = spec.get("install")
    if not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
        raise FleetError("No supported installer for " + spec["name"] + ". " + spec["docs"])
    try:
        return subprocess.call(argv)
    except OSError as exc:
        raise FleetError("Could not start %s installer: %s" % (spec["name"], exc)) from exc


def prepare(spec, client, model, environ=None):
    """Construct argv/env without launching, changing global env, or writing keys."""
    binary = resolve(spec)
    if not binary:
        raise FleetError(spec["name"] + " is not installed. " + spec["docs"])
    if not model or any(ord(c) < 32 for c in model):
        raise FleetError("Choose a valid fleet model")
    env = dict(os.environ if environ is None else environ)
    env.pop("HUGPY_OPERATOR_TOKEN", None)
    v1 = client.base + "/v1"
    env.update(HUGPY_API_KEY=client.key, OPENAI_API_KEY=client.key or "hugpy-open-fleet",
               OPENAI_BASE_URL=v1, OPENAI_API_BASE=v1, OPENAI_MODEL=model)
    if spec["id"] == "hermes":
        # openai-api uses Responses in current Hermes. A named custom
        # provider explicitly selects Chat Completions instead.
        env["HUGPY_HERMES_API_KEY"] = client.key or "hugpy-open-fleet"
        argv = [binary, "chat", "--provider", "custom:hugpy", "--model", model]
        argv += _final_allow_all("hermes", env)
    elif spec["id"] == "aider":
        chosen = "openai/" + model
        # Architect/editor stay on the chosen fleet model; the WEAK model
        # (commit messages, history summaries) is the harness small model.
        small = hs.small_model("aider", env)
        weak = "openai/" + small if small else chosen
        argv = [binary, "--model", chosen, "--weak-model", weak, "--editor-model", chosen]
        argv += _final_allow_all("aider", env)
    elif spec["id"] in ("claude-code", "qwen-code", "opencode"):
        # Reuse the existing protocol/config adapters. Explicit /v1 avoids
        # their public-site bare-origin normalization on direct central URLs.
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(__file__)) + os.pathsep + env.get("PYTHONPATH", "")
        # Ensure a resolver fallback outside PATH is available to the child.
        env["PATH"] = os.path.dirname(binary) + os.pathsep + env.get("PATH", "")
        if spec["id"] == "claude-code":
            headers = env.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines()
            env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(h for h in headers if h.partition(":")[0].strip().lower() != "x-hugpy-model")
        argv = [sys.executable, "-m", "hugpy_agent.cli", "console", "--base", v1,
                "--frontend", spec["id"], "--model", model]
    else:
        raise FleetError("No fleet adapter for " + spec["id"])
    hs.harness_env(spec["id"], env)
    return argv, env


def _final_allow_all(harness, env):
    """allow_all argv for a harness exec'd DIRECTLY from here (no console
    re-exec): resolve, announce, then scrub the control vars from the child."""
    argv_add, env_add = hs.allow_all_spec(harness, env)
    hs.scrub(env)
    if argv_add:
        print(hs.banner(harness), file=sys.stderr)
        env.update(env_add)
    return argv_add


def configure(spec, env, model, root=None):
    """Fresh, retained Hermes profile; never overwrite the user's own config.

    JSON is valid YAML. Only a key-env reference is written; session files
    survive exit under the console's profile directory.
    """
    if spec["id"] != "hermes":
        return None
    root = root or os.path.expanduser("~/.hugpy_agent/console/hermes")
    os.makedirs(root, mode=0o700, exist_ok=True)
    profile = tempfile.mkdtemp(prefix="session-", dir=root)
    config = {
        "providers": {"hugpy": {"name": "Hugpy fleet", "api": env["OPENAI_BASE_URL"],
                                  "key_env": "HUGPY_HERMES_API_KEY", "transport": "openai_chat",
                                  # per-launch profile: literal identity (not secret)
                                  "extra_headers": session_signals.harness_header_values(env)}},
        "model": {"provider": "custom:hugpy", "default": model, "api_mode": "chat_completions"},
        "auxiliary": {task: {"provider": "main"} for task in ("compression", "vision", "session_naming")},
    }
    small = hs.small_model("hermes", env)
    if small:
        # Hermes 0.21.5 names titles `auxiliary.title_generation`.
        config["auxiliary"]["title_generation"] = {"provider": "custom:hugpy",
                                                   "model": small}
    with open(os.path.join(profile, "config.yaml"), "x", encoding="utf-8") as output:
        json.dump(config, output, indent=2)
    env["HERMES_HOME"] = profile
    # Parent debugging flags must not silently discard the generated route.
    env.pop("HERMES_IGNORE_USER_CONFIG", None)
    return profile
