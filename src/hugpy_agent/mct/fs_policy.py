"""Frontier filesystem policy — the persistent, operator-facing control for
"which directories may the frontier model (A) reach, and is that reach on?"

The MCT enforcement (session._fs_broker_resolve, gated by
``allow_frontier_fs_requests``) has always existed, but its only controls were
a start-up flag and an in-REPL ``/fsreq`` toggle — nothing the fleet console's
"directory accessibility" button could drive, and nothing that survived a
session. This module makes the policy a small JSON file in the workspace that
IS the single source of truth:

    <workspace>/.hugpy_agent/mct/fs_policy.json
    {
      "allow_frontier_fs_requests": false,       # the on/off (allow/disallow)
      "granted_roots": [ {"name": "...", "path": "/abs/dir"}, ... ]
    }

The console button writes it via ``hugpy-agent mct-fs`` (symmetric with the
``mct-usage`` it already polls); the broker reads it at session open AND live
before each brokered resolution, so a toggle takes effect on the next A turn
without restarting the session. ``/fsreq`` and ``--allow-fs-requests`` write
through here too, so every path shares one truth.

Confinement is unchanged: granting a root only makes it *eligible*; B still
validates, confines, snapshots, and serves — A never receives a host path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_DEFAULT: dict[str, Any] = {
    "allow_frontier_fs_requests": False,
    "granted_roots": [],
}


def policy_path(workspace: str | Path) -> Path:
    return Path(workspace) / ".hugpy_agent" / "mct" / "fs_policy.json"


def load_policy(workspace: str | Path) -> dict[str, Any]:
    """The current policy, always a well-formed dict (defaults when absent or
    unreadable — a broken file must never break a session, only default-closed)."""
    p = policy_path(workspace)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(_DEFAULT, granted_roots=[])
    out = dict(_DEFAULT)
    out["allow_frontier_fs_requests"] = bool(raw.get("allow_frontier_fs_requests", False))
    roots = []
    seen = set()
    for r in raw.get("granted_roots") or []:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "").strip()
        path = str(r.get("path") or "").strip()
        if not name or not path or name in seen:
            continue
        seen.add(name)
        roots.append({"name": name, "path": path})
    out["granted_roots"] = roots
    return out


def save_policy(workspace: str | Path, policy: dict[str, Any]) -> dict[str, Any]:
    """Atomically persist a normalized policy; returns what was written."""
    p = policy_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    norm = load_policy_from_obj(policy)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(norm, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return norm


def load_policy_from_obj(obj: dict[str, Any]) -> dict[str, Any]:
    """Normalize an in-memory policy dict the same way load_policy normalizes a
    file (dedupe roots by name, coerce the flag, drop malformed entries)."""
    out = dict(_DEFAULT)
    out["allow_frontier_fs_requests"] = bool(obj.get("allow_frontier_fs_requests", False))
    roots, seen = [], set()
    for r in obj.get("granted_roots") or []:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "").strip()
        path = str(r.get("path") or "").strip()
        if not name or not path or name in seen:
            continue
        seen.add(name)
        roots.append({"name": name, "path": path})
    out["granted_roots"] = roots
    return out


# --- mutators used by the CLI / REPL / (indirectly) the console button -------
def set_allow(workspace: str | Path, allow: bool) -> dict[str, Any]:
    pol = load_policy(workspace)
    pol["allow_frontier_fs_requests"] = bool(allow)
    return save_policy(workspace, pol)


def add_root(workspace: str | Path, name: str, path: str) -> dict[str, Any]:
    name, path = str(name).strip(), os.path.abspath(os.path.expanduser(str(path).strip()))
    if not name or not path:
        raise ValueError("both a name and a path are required")
    pol = load_policy(workspace)
    pol["granted_roots"] = [r for r in pol["granted_roots"] if r["name"] != name]
    pol["granted_roots"].append({"name": name, "path": path})
    return save_policy(workspace, pol)


def remove_root(workspace: str | Path, name: str) -> dict[str, Any]:
    name = str(name).strip()
    pol = load_policy(workspace)
    pol["granted_roots"] = [r for r in pol["granted_roots"] if r["name"] != name]
    return save_policy(workspace, pol)
