"""Install-link ledger for the Fleet arm, backed by real Core API keys.

Every value a route returns is a RECORDED FACT: a real random token, a real
``created_at``/``expires_at`` computed from the request, a real on-disk count.
There are no placeholder links; an absent link is reported as absent (the list
is empty, a revoke of an unknown id says ``found: false``).

Storage: ``$HUGPY_AGENT_LINKS_DIR/install_links.json`` plus private, 0600
one-time key delivery files in ``key_secrets``. Core keeps only the key hash.
Ledger writes take a process lock and use ``os.replace``.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

from ..imports import constants as C
from ..imports.functions import home_dir

_LOCK = threading.RLock()

# Scopes the operator may record with a package download link.
_VALID_SCOPES = ("use",)
_DEFAULT_SCOPES = ["use"]
_DEFAULT_TTL_S = 86400
_BRIDGE_FILE = "/etc/hugpy/install-link-bridge.secret"


# ── storage ──────────────────────────────────────────────────────────────────
def _store_dir() -> str:
    d = os.environ.get(C.ENV_CONSOLE_LINKS_DIR) or os.path.join(home_dir(), "fleet")
    os.makedirs(d, exist_ok=True)
    return d


def _links_path() -> str:
    return os.path.join(_store_dir(), "install_links.json")


def _secret_path(link_id: str) -> str:
    directory = os.path.join(_store_dir(), "key_secrets")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    return os.path.join(directory, link_id)


def _load(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save(path: str, data) -> None:
    tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _read_links() -> dict:
    data = _load(_links_path(), {"links": {}, "audit": []})
    if not isinstance(data, dict):
        data = {"links": {}, "audit": []}
    data.setdefault("links", {})
    data.setdefault("audit", [])
    return data


# ── real Core API keys ────────────────────────────────────────────────────────
def _core_key_request(body: dict) -> dict:
    """Ask Core's active KeyAuth store through the private service bridge."""
    try:
        with open(_BRIDGE_FILE, encoding="ascii") as fh:
            bridge = fh.read().strip()
        if not bridge:
            raise OSError("empty bridge secret")
    except OSError as exc:
        raise RuntimeError("Core install-link key bridge unavailable") from exc
    base = os.environ.get("HUGPY_INSTALL_LINK_CORE_URL", "http://127.0.0.1:7020").rstrip("/")
    req = urllib.request.Request(
        base + "/api/keys/install-link", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Hugpy-Install-Bridge": bridge})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError("Core API key service is unavailable") from exc


def create_api_key(label, link_id, expires_at=None):
    result = _core_key_request({"action": "create", "label": label,
                                "link_id": link_id, "expires_at": expires_at})
    return result["id"], result["key"]


def revoke_api_key(key_id) -> bool:
    if not key_id:
        return False
    return bool(_core_key_request({"action": "revoke", "key_id": key_id}).get("revoked"))


def _save_secret(link_id: str, raw_key: str) -> None:
    fd = os.open(_secret_path(link_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(raw_key + "\n")


def _read_secret(link_id: str) -> str | None:
    try:
        with open(_secret_path(link_id), encoding="ascii") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _remove_secret(link_id: str) -> None:
    try:
        os.unlink(_secret_path(link_id))
    except OSError:
        pass


# ── link records ──────────────────────────────────────────────────────────────
def _status(rec: dict) -> str:
    now = time.time()
    if rec.get("revoked"):
        return "revoked"
    exp = rec.get("expires_at")
    if exp is not None and now > exp:
        return "expired"
    if (rec.get("uses_left") or 0) <= 0:
        return "exhausted"
    return "active"


def _public(rec: dict) -> dict:
    out = {k: v for k, v in rec.items() if k != "raw_key"}
    out["status"] = _status(rec)
    return out


def create_install_link(label, scopes=None, key_expires_at=None, link_ttl_s=None,
                        max_uses=None, owner=None, kind="agent") -> dict:
    """Create and persist one link (mints its own API key). Returns the record."""
    label = (label or "").strip()
    if not label:
        raise ValueError("label is required")
    from .console_artifacts import PACKAGE_KINDS, package_deb, _sha256
    if kind not in PACKAGE_KINDS:
        raise ValueError("unknown installer kind")
    artifact = package_deb(kind)
    if artifact is None:
        raise ValueError("no %s .deb is staged" % PACKAGE_KINDS[kind])
    artifact_sha256 = _sha256(os.path.join(*artifact))
    if not artifact_sha256:
        raise ValueError("could not checksum the staged .deb")
    if scopes is None:
        scopes = list(_DEFAULT_SCOPES)
    elif isinstance(scopes, str):
        scopes = [s.strip() for s in scopes.split(",") if s.strip()]
    elif isinstance(scopes, (list, tuple)):
        scopes = [str(s).strip() for s in scopes if str(s).strip()]
    else:
        raise ValueError("scopes must be a list")
    if not scopes or any(scope not in _VALID_SCOPES for scope in scopes):
        raise ValueError("choose at least one valid scope")
    ttl = _DEFAULT_TTL_S if link_ttl_s is None else int(link_ttl_s)
    if not 60 <= ttl <= 604800:
        raise ValueError("link lifetime must be between one minute and seven days")
    try:
        max_uses = 1 if max_uses is None else int(max_uses)
    except (TypeError, ValueError):
        raise ValueError("max_uses must be an integer") from None
    if not 1 <= max_uses <= 10:
        raise ValueError("max_uses must be between 1 and 10")
    now = time.time()
    if key_expires_at is not None:
        try:
            key_expires_at = float(key_expires_at)
        except (TypeError, ValueError):
            raise ValueError("key expiry must be a date in the future") from None
        if key_expires_at <= now:
            raise ValueError("key expiry must be in the future")
    link_id = secrets.token_urlsafe(24)
    key_id, raw_key = create_api_key(label, link_id, expires_at=key_expires_at)
    rec = {
        "link_id": link_id,
        "kind": kind,
        "artifact": artifact[1],
        "artifact_sha256": artifact_sha256,
        "key_id": key_id,
        "label": label,
        "owner": owner,
        "scopes": scopes,
        "created_at": now,
        "expires_at": (now + ttl) if ttl > 0 else None,
        "key_expires_at": key_expires_at,
        "max_uses": max_uses,
        "uses_left": max_uses,
        "downloads": [],
        "revoked": False,
    }
    try:
        _save_secret(link_id, raw_key)
        with _LOCK:
            data = _read_links()
            data["links"][link_id] = rec
            data["audit"].append({"ts": now, "action": "create", "link_id": link_id,
                                  "owner": owner})
            _save(_links_path(), data)
    except Exception:
        _remove_secret(link_id)
        revoke_api_key(key_id)
        raise
    return rec


def list_install_links(owner=None) -> list:
    """Public views, newest first. ``owner=None`` is the operator view (all)."""
    with _LOCK:
        data = _read_links()
    rows = list(data["links"].values())
    if owner is not None:
        rows = [r for r in rows if r.get("owner") == owner]
    rows.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return [_public(r) for r in rows]


def owner_of(link_id):
    with _LOCK:
        data = _read_links()
    rec = data["links"].get(link_id)
    return (rec is not None, rec.get("owner") if rec else None)


def _lookup(link_id):
    """The raw record for a link id, or None (status via _status)."""
    if not link_id:
        return None
    with _LOCK:
        data = _read_links()
    return data["links"].get(link_id)


def _record_download(link_id, name) -> bool:
    """Record one artifact download and consume one use; when the last use is
    spent the download URL closes; the minted key remains revocable separately."""
    now = time.time()
    with _LOCK:
        data = _read_links()
        rec = data["links"].get(link_id)
        if rec is None or _status(rec) != "active" or name != rec.get("artifact"):
            return False
        rec.setdefault("downloads", []).append({"ts": now, "name": name})
        left = rec.get("uses_left")
        if isinstance(left, int):
            rec["uses_left"] = max(0, left - 1)
            if rec["uses_left"] == 0:
                _remove_secret(link_id)
        data["audit"].append({"ts": now, "action": "download",
                              "link_id": link_id, "name": name})
        _save(_links_path(), data)
    return True


def revoke_install_link(link_id) -> bool:
    with _LOCK:
        data = _read_links()
        rec = data["links"].get(link_id)
        if not rec:
            return False
        rec["revoked"] = True
        revoke_api_key(rec.get("key_id"))
        _remove_secret(link_id)
        data["audit"].append({"ts": time.time(), "action": "revoke", "link_id": link_id})
        _save(_links_path(), data)
    return True


def delete_install_link(link_id) -> bool:
    with _LOCK:
        data = _read_links()
        rec = data["links"].pop(link_id, None)
        if rec is None:
            return False
        # revoke the key only if the link was still live (parity with legacy)
        if not rec.get("revoked"):
            revoke_api_key(rec.get("key_id"))
        _remove_secret(link_id)
        data["audit"].append({"ts": time.time(), "action": "delete", "link_id": link_id})
        _save(_links_path(), data)
    return True


def prune_install_links(owner=None) -> int:
    with _LOCK:
        data = _read_links()
        dead = [lid for lid, r in data["links"].items()
                if _status(r) != "active" and (owner is None or r.get("owner") == owner)]
        for lid in dead:
            data["links"].pop(lid, None)
        if dead:
            data["audit"].append({"ts": time.time(), "action": "prune", "count": len(dead)})
            _save(_links_path(), data)
    return len(dead)


# ── install url + per-OS commands (route-authored, never rebuilt client-side) ──
def _public_base() -> str:
    """The base a remote machine reaches core on (core proxies /api/agent/* to
    this arm). HUGPY_PUBLIC_BASE when set, else the agent's own resolved base
    (local wrapper → fleet → http://127.0.0.1:7020) — never the dead :7002."""
    base = os.environ.get("HUGPY_PUBLIC_BASE")
    if base:
        return base.rstrip("/")
    try:
        from ..config import _resolve_default_base
        return _resolve_default_base().rstrip("/")
    except Exception:
        return "http://127.0.0.1:7020"


def _script_url(link_id: str, platform: str = "") -> str:
    """The URL a one-liner fetches — the arm's install_script route, reached
    through core's arm proxy (/api/agent/* -> :7342)."""
    url = f"{_public_base()}/api/agent/install_script?id={link_id}"
    return url + ("&platform=" + platform if platform else "")


def _file_url(link_id: str, name: str) -> str:
    return f"{_public_base()}/api/agent/install_file?id={link_id}&name={quote(name, safe='')}"


def _install_commands(link_id: str) -> dict:
    sh = f"curl -fsSL '{_script_url(link_id)}' | bash"
    return {"linux": sh}


def _install_downloads(link_id: str, deb: str) -> dict:
    """The exact Debian artifact pinned when this link was minted."""
    return {"deb": _file_url(link_id, deb)} if deb else {}


# ── toolset route functions (JSON-serialisable; never return None) ─────────────
def list_links(owner=None) -> dict:
    """GET list of the caller's install links (operator view when owner unset)."""
    return {"links": list_install_links(owner)}


def mint(label=None, scopes=None, link_ttl_s=None, max_uses=None,
         key_expires_at=None, owner=None, kind="agent") -> dict:
    """Mint one scoped install link; returns its public view + url/commands/downloads."""
    rec = create_install_link(
        label, scopes=scopes, key_expires_at=key_expires_at,
        link_ttl_s=link_ttl_s, max_uses=max_uses, owner=owner, kind=kind)
    view = _public(rec)
    view["url"] = _script_url(rec["link_id"])
    view["commands"] = _install_commands(rec["link_id"])
    view["downloads"] = _install_downloads(rec["link_id"], rec["artifact"])
    return view


def revoke(link_id=None, owner=None) -> dict:
    """Revoke an active install link (its key stops working; the row stays)."""
    if not link_id:
        return {"ok": False, "error": "link_id is required"}
    found = revoke_install_link(link_id)
    return {"ok": found, "found": found, "purged": False, "link_id": link_id}


def remove(link_id=None, owner=None) -> dict:
    """Purge an install-link row (an already-installed machine keeps working)."""
    if not link_id:
        return {"ok": False, "error": "link_id is required"}
    found = delete_install_link(link_id)
    return {"ok": found, "found": found, "purged": True, "link_id": link_id}


def prune(owner=None) -> dict:
    """Sweep every dead (expired/exhausted/revoked) row for the caller."""
    return {"ok": True, "pruned": prune_install_links(owner)}


# ── served install script + artifact (the links above now resolve here) ────────
def _sh_script(base, link_id, rec, deb, raw_key) -> str:
    label = re.sub(r"[\x00-\x1f\x7f]", " ",
                   str(rec.get("label") or rec.get("kind") or "hugpy-agent"))
    exp = rec.get("expires_at")
    head = ("#!/bin/sh\n# hugpy-agent install — link %s%s\n# label: %s · base: %s\n"
            % (link_id, (" (expires %d)" % int(exp)) if exp else "", label, base))
    url = _file_url(link_id, deb)
    script = head + (
        "set -eu\n"
        "set +x\n"
        "umask 077\n"
        'tmp="$(mktemp -d)"\n'
        'trap \'rm -rf "$tmp"\' EXIT\n'
        'echo "downloading %s …"\n'
        "curl -fsSL '%s' -o \"$tmp/%s\"\n"
        'printf "%s  %%s\\n" "$tmp/%s" | sha256sum -c -\n'
        'sudo apt-get install -y "$tmp/%s"\n'
        'echo "installed %s"\n'
    ) % (deb, url, deb, rec.get("artifact_sha256", ""), deb, deb, deb)
    script += (
        'cfg="$HOME/.config/hugpy-agent/agent.env"\n'
        'mkdir -p "$HOME/.config/hugpy-agent"\n'
        'chmod 0700 "$HOME/.config/hugpy-agent"\n'
        'if [ -f "$cfg" ]; then grep -v -E "^HUGPY_(BASE|API_KEY)=" "$cfg" > "$tmp/agent.env" || true; fi\n'
        'cat >> "$tmp/agent.env" <<\'HUGPY_INSTALL_KEY\'\n'
        'HUGPY_BASE=%s\nHUGPY_API_KEY=%s\nHUGPY_INSTALL_KEY\n'
        'install -m 0600 "$tmp/agent.env" "$cfg"\n'
    ) % (base, raw_key)
    if rec.get("kind") in ("agent", "hugpy-agent"):
        script += (
            'printf "HUGPY_BASE=%%s\\nHUGPY_API_KEY=%%s\\n" \'%s\' \'%s\' '
            '| sudo tee -a /etc/hugpy/agent.env >/dev/null\n'
            'sudo chown root:hugpy-agent /etc/hugpy/agent.env\n'
            'sudo chmod 0640 /etc/hugpy/agent.env\n'
            'sudo systemctl restart hugpy-agent.service\n'
        ) % (base, raw_key)
    return script + 'echo "API credential installed in $cfg"\n'


def install_script(id=None, platform=None):
    """GET /api/agent/install_script?id=<link_id> — the Debian install
    script a one-liner fetches (``curl … | bash``).
    Returns a flask Response with the raw script, or a 404/410 Response when the
    link is unknown / expired / revoked / exhausted (the status is reported
    verbatim — no placeholder script is ever served)."""
    from flask import Response
    link_id = (id or "").strip()
    if not link_id:
        return Response("install_script: id is required\n", status=400,
                        mimetype="text/plain")
    rec = _lookup(link_id)
    if rec is None:
        return Response("install link not found\n", status=404,
                        mimetype="text/plain")
    status = _status(rec)
    if status != "active":
        return Response("install link %s\n" % status, status=410,
                        mimetype="text/plain")
    deb = rec.get("artifact")
    if not deb:
        return Response("install link has no pinned artifact\n", status=410,
                        mimetype="text/plain")
    base = _public_base()
    if str(platform or "").lower() in ("windows", "win", "ps1"):
        return Response("this installer is Debian-only\n", status=400,
                        mimetype="text/plain")
    raw_key = _read_secret(link_id)
    if not raw_key:
        return Response("install-link credential unavailable\n", status=410,
                        mimetype="text/plain")
    return Response(_sh_script(base, link_id, rec, deb, raw_key), mimetype="text/plain",
                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def install_file(id=None, name=None):
    """GET /api/agent/install_file?id=<link_id>&name=<artifact> — stream one
    staged artifact to an authorized install link, recording the download and
    consuming one use. Returns the file Response, or a 404/410 Response when the
    link is unknown / expired / revoked / exhausted (the shared console_file
    streamer owns the filename safety + its own 404)."""
    from flask import Response
    link_id = (id or "").strip()
    rec = _lookup(link_id)
    if rec is None:
        return Response("install link not found\n", status=404,
                        mimetype="text/plain")
    status = _status(rec)
    if status != "active":
        return Response("install link %s\n" % status, status=410,
                        mimetype="text/plain")
    if name != rec.get("artifact"):
        return Response("artifact is not in this link\n", status=404,
                        mimetype="text/plain")
    from .console_artifacts import package_file
    resp = package_file(rec.get("kind", "agent"), name)
    if getattr(resp, "status_code", 200) == 200 and not _record_download(link_id, name):
        return Response("install link exhausted\n", status=410,
                        mimetype="text/plain")
    return resp
