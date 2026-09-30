"""Explicit model profiles and lazy discovery of installed native clients.

Profiles contain credential environment-variable names, never credential values.
Discovery uses filesystem lookups only: no subprocesses, downloads or installs.
"""
import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

PROTOCOLS = {"openai-chat", "openai-responses", "anthropic", "hugpy"}
CLIENTS = {"claude-code": "claude", "codex": "codex"}


def binary(name):
    found = shutil.which(CLIENTS[name])
    if found:
        return found
    path = Path.home() / ".local/bin" / CLIENTS[name]
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def load_profiles(path):
    doc = json.loads(Path(path).read_text())
    profiles = doc.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("profiles must be an object keyed by profile name")
    out = {}
    for name, raw in profiles.items():
        if not isinstance(raw, dict) or not name or len(name) > 100:
            raise ValueError("invalid profile")
        p = dict(raw)
        protocol = p.get("protocol", "openai-chat")
        if protocol not in PROTOCOLS | CLIENTS.keys():
            raise ValueError("unknown protocol for " + name)
        if "api_key" in p or "token" in p:
            raise ValueError("use api_key_env instead of credentials in profiles")
        if protocol in PROTOCOLS:
            u = urlsplit(p.get("base_url", ""))
            if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
                raise ValueError("base_url must be an HTTP(S) URL without credentials: " + name)
            if not isinstance(p.get("model"), str) or not p["model"]:
                raise ValueError("model is required: " + name)
        p["protocol"] = protocol
        p["label"] = p.get("label") or name
        p["context_length"] = int(p.get("context_length", 8192))
        p["max_tokens"] = int(p.get("max_tokens", 1024))
        if not 256 <= p["max_tokens"] < p["context_length"] <= 2000000:
            raise ValueError("invalid context_length/max_tokens: " + name)
        if not isinstance(p.get("parameters", {}), dict):
            raise ValueError("parameters must be an object: " + name)
        if p.get("token_parameter", "max_tokens") not in ("max_tokens", "max_completion_tokens"):
            raise ValueError("unsupported token_parameter: " + name)
        out[name] = p
    if doc.get("discover_clients", True):
        for name in CLIENTS:
            if name not in out and binary(name):
                out[name] = {"protocol": name, "label": {"codex": "Codex", "claude-code": "Claude Code"}[name],
                             "model": "", "context_length": 32768, "max_tokens": 4096}
    default = doc.get("default_profile") or next(iter(out), "")
    if default not in out:
        raise ValueError("default_profile must name a configured profile")
    return default, out


def public_profile(name, p):
    key_env = p.get("api_key_env", "")
    available = bool(binary(p["protocol"])) if p["protocol"] in CLIENTS else not key_env or bool(os.environ.get(key_env))
    return {"id": name, "label": p["label"], "protocol": p["protocol"],
            "model": p.get("model", ""), "available": available,
            "reason": "" if available else ("Client is not installed" if p["protocol"] in CLIENTS else "Credential is not configured"),
            "tools": "client configuration" if p["protocol"] in CLIENTS else "Hugpy Agent + Toolserver"}
