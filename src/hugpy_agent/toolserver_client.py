"""hugpy-agent's toolserver client — a thin skin over the SHARED client in
``abstract_toolserver.client`` (the one client every consumer uses; see
abstract-toolserver's CENTRALIZATION.md).

Operator decision (2026-09-29): "all hugpy-agent harnesses and clients should
have toolserver integrated." Per-harness wiring lives next to each harness
(tools/toolserver.py for the agent loop, mct/claude_adapter.py for MCT's A,
service/ for serve, fleet_tui.py, cli.py); transport, endpoint discovery and
token resolution live in abstract_toolserver (2026-09-30: this module used to
carry its own ~580-line copy of them).

What stays hugpy-agent-specific here:
  * the allowlist is ENFORCED by default (privileged tools off unless named in
    HUGPY_AGENT_TOOLSERVER_ALLOW / HUGPY_TOOLSERVER_ALLOW; *_DENY wins);
  * ``from_config`` builds a client from a hugpy_agent.config.Config;
  * ``enabled()`` honours HUGPY_AGENT_TOOLSERVER=0;
  * ``ensure_toolserver()`` — the install / first-run / serve-startup hook over
    abstract_toolserver.discovery.ensure_endpoint().

Endpoint resolution (abstract_toolserver.discovery): cfg.toolserver_url →
$HUGPY_TOOLSERVER_URL / $TOOLSERVER_URL / $STATION_CONSOLE_TOOLSERVER → env
files (incl. $HUGPY_HOME/toolserver.env, default ~/.hugpy/toolserver.env) → the
toolserver ADVERTISED on this host → http://127.0.0.1:7004.
"""
from __future__ import annotations

import importlib.util
import os

from abstract_toolserver import client as _shared
from abstract_toolserver import discovery as _discovery
from abstract_toolserver.client import (  # noqa: F401  (re-exported API)
    CALL_TIMEOUT, DEFAULT_URL, ENV_FILES, HEADER, LIST_TTL, MUTATING, PRIVILEGED,
    PROBE_TIMEOUT, READONLY, STATUS_TTL, TOKEN_ENV, TOKEN_ENV_KEYS, URL_ENV,
    URL_ENV_KEYS, ToolserverAuthError, ToolserverError, _unwrap, classify,
    coerce_args, env_file_values, file_token_ok, is_allowed, parse_env_file,
    resolve_token, resolve_url, status_line)

ALLOW_ENV = ("HUGPY_AGENT_TOOLSERVER_ALLOW", "HUGPY_TOOLSERVER_ALLOW")
DENY_ENV = ("HUGPY_AGENT_TOOLSERVER_DENY", "HUGPY_TOOLSERVER_DENY")


def denial_reason(name: str, allow=None, deny=None, args: dict | None = None) -> str:
    return _shared.denial_reason(name, allow, deny, args,
                                 allow_env=ALLOW_ENV[0], deny_env=DENY_ENV[0])


def enabled(environ=None) -> bool:
    """HUGPY_AGENT_TOOLSERVER=0|false|no|off opts a process out entirely."""
    environ = os.environ if environ is None else environ
    v = (environ.get("HUGPY_AGENT_TOOLSERVER") or "").strip().lower()
    return v not in ("0", "false", "no", "off")


def missing_token_message(url: str = DEFAULT_URL) -> str:
    return _shared.missing_token_message(url)


class ToolserverClient(_shared.ToolserverClient):
    """The shared client with hugpy-agent's defaults (allowlist enforced)."""
    client_name = "hugpy-agent"
    allow_env = ALLOW_ENV
    deny_env = DENY_ENV
    enforce_allowlist_default = True

    def __init__(self, url: str | None = None, token: str | None = None, *,
                 environ=None, **kw):
        # Resolve through THIS module's names (resolve_token / env_file_values —
        # the shared implementations re-exported above) so a caller or test
        # patching them here steers the client. Order: explicit/env/env-file
        # url → the ADVERTISED local toolserver → DEFAULT_URL.
        environ = os.environ if environ is None else environ
        file_values = env_file_values(environ)
        configured = _discovery.configured_url(url or "", environ, file_values)
        advertised = None if configured else _discovery.find_endpoint(environ, probe=False)
        base = configured or (advertised or {}).get("url") or DEFAULT_URL
        if advertised:               # the advertised token_file is consulted first
            file_values = env_file_values(environ, advertised)
        if token:
            tok, src = token, "config"
        else:
            tok, src = resolve_token("", base, environ, file_values, advertised)
        super().__init__(base, tok or None, environ=environ, **kw)
        self.token, self.token_source = tok, src
        self.url_source = ("configured" if configured else
                           "advertised" if advertised else "default")

    @classmethod
    def from_config(cls, cfg, environ=None) -> "ToolserverClient":
        """Build from a hugpy_agent.config.Config (toolserver_url/_token/
        _allow/_deny + timeout); any attribute may be absent."""
        try:
            timeout = float(getattr(cfg, "timeout", CALL_TIMEOUT) or CALL_TIMEOUT)
        except (TypeError, ValueError):
            timeout = CALL_TIMEOUT
        return cls(getattr(cfg, "toolserver_url", "") or None,
                   getattr(cfg, "toolserver_token", "") or None,
                   timeout=timeout,
                   allow=getattr(cfg, "toolserver_allow", None),
                   deny=getattr(cfg, "toolserver_deny", None),
                   environ=environ)


def default_client(environ=None, refresh: bool = False) -> ToolserverClient:
    """Process-wide hugpy-agent client for the resolved (url, token)."""
    return _shared.default_client(environ, refresh, cls=ToolserverClient)


def _server_extra_installed() -> bool:
    """Starting a local toolserver needs abstract-toolserver[server]."""
    try:
        return all(importlib.util.find_spec(m) is not None
                   for m in ("flask", "abstract_flask"))
    except (ImportError, ValueError):
        return False


def ensure_toolserver(cfg=None, environ=None, wait: float = 10.0, starter=None) -> dict:
    """Install / first-run / serve-startup hook: find the ONE toolserver for
    this host (configured → advertised → local port) and — only when none is
    reachable AND the server extra is installed — start it. Never raises;
    bounded by `wait`. HUGPY_AGENT_TOOLSERVER=0 (or cfg.toolserver False) skips."""
    environ = os.environ if environ is None else environ
    if not enabled(environ) or (cfg is not None and not getattr(cfg, "toolserver", True)):
        return {"url": "", "source": "disabled"}
    configured = (getattr(cfg, "toolserver_url", "") or "").strip() if cfg is not None else ""
    if configured:
        return {"url": configured.rstrip("/"), "source": "configured"}
    try:
        return _discovery.ensure_endpoint(environ, start=_server_extra_installed(),
                                          starter=starter, wait=wait)
    except Exception as exc:  # noqa: BLE001 — a first-run hook must never break startup
        return {"url": "", "source": "none", "reason": "ensure_endpoint failed: %s" % exc}
