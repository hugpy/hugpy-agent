"""Find and identify a serve (h26 §1.3).

Order: --serve URL -> $HUGPY_AGENT_SERVE -> :9124 -> :9125 -> :9126, each
probed with GET /api/state (0.8 s). The API kind is detected from the reply,
not the port: current servers advertise `service` + `protocol_version`.
Legacy shared Serve replies are still recognized by their old state shape.
"""
from __future__ import annotations

import json
import os
import urllib.request

from ..serve_client.base import ServeError

DEFAULTS = ("http://127.0.0.1:9124", "http://127.0.0.1:9125", "http://127.0.0.1:9126")
KINDS = ("auto", "abstract-serve", "abstract-claude", "hugpy")


def identify(doc):
    if not isinstance(doc, dict):
        return None
    if doc.get("service") == "abstract-serve" and doc.get("protocol_version") == 1:
        return "abstract-serve"
    if doc.get("service") == "hugpy-agent" and "protocol_version" in doc:
        return "hugpy"
    # Compatibility with shared Serve releases before the protocol marker.
    version = str(doc.get("version") or "")
    if version[:1].isdigit() and any(k in doc for k in ("busy", "root", "oauth")):
        return "abstract-serve"
    return None


def probe(base, opener=None, timeout=0.8):
    """GET /api/state -> parsed doc, or None when unreachable/not a serve."""
    opener = opener or urllib.request.urlopen
    try:
        with opener(base.rstrip("/") + "/api/state", timeout=timeout) as response:
            if getattr(response, "status", 200) != 200:
                return None
            return json.loads(response.read() or b"{}")
    except (OSError, ValueError):
        return None


def candidates(explicit=None, env=None):
    env = os.environ if env is None else env
    seen, out = set(), []
    for value in (explicit, env.get("HUGPY_AGENT_SERVE")) + DEFAULTS:
        if value and value.rstrip("/") not in seen:
            seen.add(value.rstrip("/"))
            out.append(value.rstrip("/"))
    return out


def discover(explicit=None, kind="auto", env=None, opener=None):
    """Return (base, API kind). `kind` other than auto rejects mismatches."""
    if kind not in KINDS:
        raise ServeError("unknown --kind %r (auto|abstract-serve|hugpy)" % (kind,))
    # Keep the old spelling working for scripts and existing operator configs.
    expected = "abstract-serve" if kind == "abstract-claude" else kind
    tried = []
    for base in candidates(explicit, env):
        doc = probe(base, opener)
        found = identify(doc)
        if found is None:
            tried.append(base)
            continue
        if expected != "auto" and found != expected:
            if explicit and base == explicit.rstrip("/"):
                raise ServeError("%s is %s, expected %s" % (base, found, expected))
            tried.append(base)
            continue
        return base, found
    raise ServeError("no serve found (tried %s)" % ", ".join(tried))
