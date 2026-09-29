"""Shared toolserver client — the ONE way every hugpy-agent harness and client
reaches the running abstract_toolserver (:7004).

Operator decision (2026-09-29): "all hugpy-agent harnesses and clients should
have toolserver integrated." This module is the shared seam; per-harness
wiring lives next to each harness (tools/toolserver.py for the agent loop,
mct/claude_adapter.py for MCT's A, service/ for serve, fleet_tui.py, cli.py).

Wire contract (what the toolserver actually serves — see
abstract_toolserver.app._install_operator_gate and catalog.handle_mcp):

  auth      X-Operator-Token: <token>   (Authorization: Bearer <token> also
            honoured; both are sent). 401 {"error":"unauthorized"} otherwise.
            The server reads the token from TOOLSERVER_OPERATOR_TOKEN.
  list      POST /mcp?mode=flat  {"jsonrpc":"2.0","id":1,"method":"tools/list"}
            -> {"result":{"tools":[{name, description, inputSchema}]}}
            Fallback for older servers: POST /ts/categories + POST /ts/list.
  call      POST /ts/call {"name","arguments"} -> the tool's own value, usually
            wrapped once as {"result": ...}; tool errors are DATA
            ({"error": ...}) with HTTP 200, never a transport failure.
  health    POST /mcp initialize -> serverInfo.version (cheap, authed).
  streaming none (GET /mcp is 405; /events/stream is unrelated).

Stdlib only (urllib). Nothing here imports a vendor SDK or abstract_* package.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# ── configuration surface ────────────────────────────────────────────────────
DEFAULT_URL = "http://127.0.0.1:7004"
URL_ENV = "TOOLSERVER_URL"
URL_ENV_KEYS = (URL_ENV, "STATION_CONSOLE_TOOLSERVER")
# The toolserver itself reads TOOLSERVER_OPERATOR_TOKEN (app.py). The station
# seat env block and the Claude MCP bridge spell the same secret as
# TOOLSERVER_TOKEN / HUGPY_OPERATOR_TOKEN; all are accepted, first hit wins.
TOKEN_ENV = "TOOLSERVER_OPERATOR_TOKEN"
TOKEN_ENV_KEYS = (TOKEN_ENV, "TOOLSERVER_TOKEN", "HUGPY_OPERATOR_TOKEN",
                  "STATION_CONSOLE_TOOLSERVER_TOKEN")
# KEY=VALUE files consulted when the process env carries nothing. The first
# entry is the package's own ~/.hugpy convention (HUGPY_HOME); the rest are the
# station/operator files the reference MCP bridge already reads.
ENV_FILES = ("~/.hugpy/toolserver.env",
             "~/.config/hugpy-station/toolserver.env",
             "~/.config/hugpy/operator.env",
             "/etc/hugpy-station/toolserver.env",
             "/etc/hugpy/operator.env")
HEADER = "X-Operator-Token"
_LEGACY_HOST = "toolserver.hugpy.ai"
_LOOPBACK_HOSTS = {"localhost", "::1"}

PROBE_TIMEOUT = 8.0
CALL_TIMEOUT = 120.0
LIST_TTL = 300.0      # seconds a tools/list answer is reused
STATUS_TTL = 30.0     # seconds a health() verdict is reused by status()

# ── allowlist policy (defaults) ──────────────────────────────────────────────
# Three classes, decided from the tool NAME (deterministic, no I/O):
#   readonly   — on by default everywhere.
#   mutating   — side-effecting but scoped (todo_add, ledger_put, comms_ping…):
#                on by default, subject to the harness policy gate (ask/auto).
#   privileged — controls machines or writes arbitrary files/SQL/commands:
#                OFF unless named in the allow list (or allow == ["*"]).
PRIVILEGED_PREFIXES = ("vm_", "vmpool_", "sys_", "browser_", "handoff_",
                       "claude_oauth", "gpt_oauth", "gpt_login")
PRIVILEGED_TOOLS = frozenset("""
fs_write_file db_query claude_reset claude_restore claude_set_model
claude_save_template gpt_restore gpt_set_model gpt_save_template
session_spin session_release ui_click_verify
""".split())
# Pure reads that live under a privileged prefix (a status probe leaks nothing).
READONLY_TOOLS = frozenset("""
claude_oauth_status claude_oauth_probe gpt_oauth_status
""".split())
# db_query is privileged only when it writes; a SELECT is a read.
_DB_READ_PREFIXES = ("select", "with", "explain", "pragma", "show", "describe")
_WRITE_TOKENS = frozenset("""
send reply ack ping add put done update remove batch open close write start
stop run exec set spin release claim pick beat submit record ingest dedup
archive crop resize save restore reset solve register request clear edit
delete create kill pause promote spawn type click key scroll go upload oauth
""".split())
_READ_TOKENS = frozenset("""
list get read state status find search info poll inbox schema tables columns
query fetch see text links attributes versions jobs job symbols configs
history summary models dirs windows monitors pointers pointer identity sessions
session report probe focus pending duplicates trace keywords calls loads count
detect analyze locate ocr tokens language template templates extract imports
glob span prescreen assess board loci metrics screenshot shot ip console usage
capture""".split())

READONLY, MUTATING, PRIVILEGED = "readonly", "mutating", "privileged"


def classify(name: str, args: dict | None = None) -> str:
    """readonly | mutating | privileged for a toolserver tool name (+ args for
    the few arg-sensitive cases). Unknown shapes are conservatively mutating."""
    name = str(name or "").strip()
    if name == "db_query":
        sql = str((args or {}).get("sql") or (args or {}).get("query") or "").lstrip().lower()
        return READONLY if sql.startswith(_DB_READ_PREFIXES) else PRIVILEGED
    if name in READONLY_TOOLS:
        return READONLY
    if name in PRIVILEGED_TOOLS or name.startswith(PRIVILEGED_PREFIXES):
        return PRIVILEGED
    toks = set(name.split("_"))
    if toks & _WRITE_TOKENS:
        return MUTATING
    if toks & _READ_TOKENS:
        return READONLY
    return MUTATING


def _match(name: str, patterns) -> bool:
    for p in patterns or ():
        p = str(p).strip()
        if not p:
            continue
        if p == "*" or p == name:
            return True
        if p.endswith("*") and name.startswith(p[:-1]):
            return True
    return False


def is_allowed(name: str, allow=None, deny=None, args: dict | None = None) -> bool:
    """The allowlist verdict. deny > allow > class default (privileged OFF)."""
    if _match(name, deny):
        return False
    if _match(name, allow):
        return True
    return classify(name, args) != PRIVILEGED


def denial_reason(name: str, allow=None, deny=None, args: dict | None = None) -> str:
    if _match(name, deny):
        return "%s is denied by HUGPY_AGENT_TOOLSERVER_DENY" % name
    return ("%s is a privileged toolserver tool (%s); it is off by default — "
            "set HUGPY_AGENT_TOOLSERVER_ALLOW=%s (or '*') to enable it"
            % (name, classify(name, args), name))


# ── env-file + resolution helpers (shared with tools/toolserver.py) ──────────
def parse_env_file(path: str) -> dict:
    """{KEY: VALUE} from a KEY=VALUE file (comments, blanks, a leading
    ``export`` and surrounding quotes tolerated); {} when unreadable."""
    out: dict[str, str] = {}
    try:
        with open(os.path.expanduser(path), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k.startswith("export "):
                    k = k[7:].strip()
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                if k and v and k not in out:
                    out[k] = v
    except OSError:
        pass
    return out


def env_file_values(environ=None) -> list:
    environ = os.environ if environ is None else environ
    files = list(ENV_FILES)
    home = (environ.get("HUGPY_HOME") or "").strip()
    if home:
        files[0] = os.path.join(home, "toolserver.env")
    return [parse_env_file(p) for p in files]


def _from_env(environ, keys):
    for k in keys:
        v = (environ.get(k) or "").strip()
        if v:
            return v
    return ""


def _from_files(file_values, keys):
    for values in file_values:
        for k in keys:
            if values.get(k):
                return values[k], values
    return "", {}


def file_token_ok(base: str, file_values: dict) -> bool:
    """May a token read off DISK travel to `base`? https, loopback, the host
    the token's own env file names, and the legacy default host: yes. Any
    other plaintext host chosen by nothing but a URL: no."""
    u = urllib.parse.urlparse(base)
    host = (u.hostname or "").lower()
    if not host:
        return False
    if u.scheme == "https" or host in _LOOPBACK_HOSTS or host.startswith("127."):
        return True
    trusted = {_LEGACY_HOST}
    own = (file_values or {}).get("STATION_CONSOLE_TOOLSERVER", "")
    if own:
        trusted.add((urllib.parse.urlparse(own).hostname or "").lower())
    return host in trusted


def resolve_url(explicit: str = "", environ=None, file_values=None) -> str:
    environ = os.environ if environ is None else environ
    if (explicit or "").strip():
        return explicit.strip().rstrip("/")
    file_values = env_file_values(environ) if file_values is None else file_values
    return (_from_env(environ, URL_ENV_KEYS)
            or _from_files(file_values, ("STATION_CONSOLE_TOOLSERVER",))[0]
            or DEFAULT_URL).rstrip("/")


def resolve_token(explicit: str = "", url: str = "", environ=None, file_values=None):
    """(token, source) — source in config|env|file|withheld|none."""
    environ = os.environ if environ is None else environ
    if (explicit or "").strip():
        return explicit.strip(), "config"
    tok = _from_env(environ, TOKEN_ENV_KEYS)
    if tok:
        return tok, "env"
    file_values = env_file_values(environ) if file_values is None else file_values
    tok, src = _from_files(file_values, TOKEN_ENV_KEYS)
    if tok and file_token_ok(url or DEFAULT_URL, src):
        return tok, "file"
    return "", ("withheld" if tok else "none")


def enabled(environ=None) -> bool:
    """HUGPY_AGENT_TOOLSERVER=0|false|no|off opts a process out entirely."""
    environ = os.environ if environ is None else environ
    v = (environ.get("HUGPY_AGENT_TOOLSERVER") or "").strip().lower()
    return v not in ("0", "false", "no", "off")


def missing_token_message(url: str = DEFAULT_URL) -> str:
    return ("toolserver at %s rejected the request (401) and no token is set: "
            "export %s=<operator token> (the value the toolserver's own "
            "toolserver.env carries; %s is also accepted)"
            % (url, TOKEN_ENV, " / ".join(TOKEN_ENV_KEYS[1:])))


# ── errors ───────────────────────────────────────────────────────────────────
class ToolserverError(Exception):
    """Transport/HTTP failure. `kind`: unreachable | auth | http | protocol."""

    def __init__(self, message, kind="http", status=None):
        super().__init__(message)
        self.kind = kind
        self.status = status


class ToolserverAuthError(ToolserverError):
    def __init__(self, message, status=401):
        super().__init__(message, kind="auth", status=status)


# ── the client ───────────────────────────────────────────────────────────────
class ToolserverClient:
    """One authenticated client per (url, token).

    list_tools()          -> [{name, description, input_schema}] (cached)
    call(name, args, t)   -> the tool's result (tool errors come back as the
                             data the server sent; transport/auth raise)
    call_json(name, args) -> JSON string, errors-as-data (for tool handlers)
    health()              -> {url, ok, auth, tool_count, version, latency_ms,
                              error, token_source}
    status()              -> the last health() (re-probed after STATUS_TTL)
    as_openai_tools()     -> [{"type":"function","function":{...}}]
    as_anthropic_tools()  -> [{"name","description","input_schema"}]
    allowed(name, args)   -> allowlist verdict; classify(name) -> class
    """

    def __init__(self, url: str | None = None, token: str | None = None, *,
                 timeout: float = CALL_TIMEOUT, probe_timeout: float = PROBE_TIMEOUT,
                 allow=None, deny=None, environ=None, opener=None):
        environ = os.environ if environ is None else environ
        file_values = env_file_values(environ)
        self.url = resolve_url(url or "", environ, file_values)
        if token is not None and token != "":
            self.token, self.token_source = token, "config"
        else:
            self.token, self.token_source = resolve_token("", self.url, environ, file_values)
        self.timeout = float(timeout or CALL_TIMEOUT)
        self.probe_timeout = float(probe_timeout or PROBE_TIMEOUT)
        self.allow = list(allow or _list_env(environ, "HUGPY_AGENT_TOOLSERVER_ALLOW"))
        self.deny = list(deny or _list_env(environ, "HUGPY_AGENT_TOOLSERVER_DENY"))
        self._opener = opener or urllib.request.urlopen
        self._lock = threading.Lock()
        self._tools: list | None = None
        self._tools_at = 0.0
        self._status: dict | None = None
        self._status_at = 0.0
        self._rpc_id = 0

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

    # -- transport -------------------------------------------------------------
    def _headers(self) -> dict:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            h[HEADER] = self.token
            h["Authorization"] = "Bearer " + self.token
        return h

    def _post(self, path: str, body, timeout: float | None = None):
        url = self.url + path
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST", headers=self._headers())
        try:
            with self._opener(req, timeout=timeout or self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if exc.code in (401, 403):
                msg = (missing_token_message(self.url) if not self.token
                       else "toolserver at %s rejected the token (%d): %s"
                            % (self.url, exc.code, detail or "unauthorized"))
                raise ToolserverAuthError(msg, status=exc.code) from None
            raise ToolserverError("toolserver %s -> HTTP %d: %s" % (path, exc.code, detail),
                                  kind="http", status=exc.code) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None) or exc
            raise ToolserverError("toolserver at %s unreachable: %s" % (self.url, reason),
                                  kind="unreachable") from None
        try:
            return json.loads(raw)
        except ValueError:
            return {"result": raw}

    def rpc(self, method: str, params: dict | None = None, timeout: float | None = None,
            flat: bool = True):
        """One JSON-RPC 2.0 call on POST /mcp (flat = the full tool surface)."""
        with self._lock:
            self._rpc_id += 1
            rid = self._rpc_id
        body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        doc = self._post("/mcp?mode=flat" if flat else "/mcp", body, timeout)
        if not isinstance(doc, dict) or doc.get("jsonrpc") != "2.0":
            raise ToolserverError("toolserver /mcp returned a non JSON-RPC body", kind="protocol")
        if doc.get("error"):
            err = doc["error"]
            raise ToolserverError("toolserver /mcp %s: %s" % (method, err.get("message", err)),
                                  kind="protocol", status=err.get("code"))
        return doc.get("result")

    # -- catalog ---------------------------------------------------------------
    @staticmethod
    def _norm(t: dict) -> dict:
        schema = t.get("input_schema") or t.get("inputSchema") or t.get("parameters") \
            or {"type": "object", "properties": {}}
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        return {"name": str(t.get("name") or ""), "description": str(t.get("description") or ""),
                "input_schema": schema}

    def _fetch_tools(self) -> list:
        try:
            res = self.rpc("tools/list", timeout=self.probe_timeout)
            tools = [self._norm(t) for t in (res or {}).get("tools", []) if t.get("name")]
            if tools and not all(t["name"].startswith("ts_") for t in tools):
                return tools
        except ToolserverError as exc:
            if exc.kind in ("auth", "unreachable"):
                raise
        # older server (no /mcp, or meta-only): walk /ts/categories + /ts/list
        cats = _unwrap(self._post("/ts/categories", {}, self.probe_timeout))
        out = []
        for c in cats if isinstance(cats, list) else []:
            listing = _unwrap(self._post("/ts/list", {"category": c.get("category")},
                                         self.probe_timeout))
            for t in (listing or {}).get("tools", []) if isinstance(listing, dict) else []:
                out.append(self._norm(t))
        return out

    def list_tools(self, refresh: bool = False) -> list:
        """Cached [{name, description, input_schema}] (LIST_TTL seconds).
        Raises ToolserverError when the server cannot be listed."""
        with self._lock:
            fresh = self._tools is not None and (time.monotonic() - self._tools_at) < LIST_TTL
            if fresh and not refresh:
                return list(self._tools)
        tools = self._fetch_tools()
        with self._lock:
            self._tools, self._tools_at = tools, time.monotonic()
        return list(tools)

    def tool(self, name: str) -> dict | None:
        for t in self.list_tools():
            if t["name"] == name:
                return t
        return None

    def names(self, allowed_only: bool = False) -> list:
        return [t["name"] for t in self.list_tools()
                if not allowed_only or self.allowed(t["name"])]

    # -- allowlist --------------------------------------------------------------
    def classify(self, name: str, args: dict | None = None) -> str:
        return classify(name, args)

    def allowed(self, name: str, args: dict | None = None) -> bool:
        return is_allowed(name, self.allow, self.deny, args)

    def denial(self, name: str, args: dict | None = None) -> str:
        return denial_reason(name, self.allow, self.deny, args)

    # -- invocation -------------------------------------------------------------
    def call(self, name: str, args: dict | None = None, timeout: float | None = None,
             enforce_allowlist: bool = True):
        """POST /ts/call. Returns the tool's value (a tool-level error is the
        {"error": ...} dict the server sent). Raises ToolserverError on
        transport/auth failure, and — with enforce_allowlist — returns an
        {"error"} dict for a tool the allowlist keeps off."""
        name = str(name or "").strip()
        args = coerce_args(args or {})
        if not name:
            return {"error": "tool name is required"}
        if enforce_allowlist and not self.allowed(name, args):
            return {"error": self.denial(name, args), "denied": True}
        doc = self._post("/ts/call", {"name": name, "arguments": args},
                         timeout or self.timeout)
        return _unwrap(doc)

    def call_json(self, name: str, args: dict | None = None, timeout: float | None = None) -> str:
        """Errors-as-data flavour for tool handlers: always a JSON string."""
        try:
            out = self.call(name, args, timeout)
        except ToolserverError as exc:
            return json.dumps({"error": "%s call failed: %s" % (name, exc), "kind": exc.kind})
        try:
            return json.dumps(out, default=str)
        except (TypeError, ValueError):
            return json.dumps({"result": str(out)})

    # -- health -----------------------------------------------------------------
    def health(self, timeout: float | None = None) -> dict:
        """Probe now. ok = server answered AND tools could be listed."""
        t0 = time.monotonic()
        out = {"url": self.url, "ok": False, "auth": "ok" if self.token else "missing",
               "tool_count": 0, "version": None, "latency_ms": None, "error": None,
               "token_source": self.token_source}
        try:
            info = self.rpc("initialize", {"protocolVersion": "2025-06-18",
                                           "capabilities": {},
                                           "clientInfo": {"name": "hugpy-agent", "version": "0"}},
                            timeout=timeout or self.probe_timeout, flat=False)
            out["version"] = ((info or {}).get("serverInfo") or {}).get("version")
        except ToolserverAuthError as exc:
            out["auth"] = "missing" if not self.token else "rejected"
            out["error"] = str(exc)
        except ToolserverError as exc:
            if exc.kind == "unreachable" or exc.status not in (404, 405):
                out["error"] = str(exc)
        if out["error"] is None:
            try:
                out["tool_count"] = len(self.list_tools())
                out["ok"] = True
                if not self.token:
                    out["auth"] = "open"      # server answered without a token
            except ToolserverAuthError as exc:
                out["auth"] = "missing" if not self.token else "rejected"
                out["error"] = str(exc)
            except ToolserverError as exc:
                out["error"] = str(exc)
        out["latency_ms"] = int((time.monotonic() - t0) * 1000)
        with self._lock:
            self._status, self._status_at = out, time.monotonic()
        return out

    def status(self, max_age: float = STATUS_TTL) -> dict:
        """{url, ok, tool_count, auth, ...} — cached health, re-probed after
        max_age seconds. Safe to poll from a UI."""
        with self._lock:
            cached = self._status
            fresh = cached is not None and (time.monotonic() - self._status_at) < max_age
        if fresh:
            return dict(cached)
        return self.health()

    # -- model-API adapters -----------------------------------------------------
    def as_openai_tools(self, names=None, allowed_only: bool = True) -> list:
        return [{"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t["input_schema"]}}
                for t in self._select(names, allowed_only)]

    def as_anthropic_tools(self, names=None, allowed_only: bool = True) -> list:
        return [{"name": t["name"], "description": t["description"],
                 "input_schema": t["input_schema"]}
                for t in self._select(names, allowed_only)]

    def _select(self, names, allowed_only):
        wanted = set(names) if names else None
        return [t for t in self.list_tools()
                if (wanted is None or t["name"] in wanted)
                and (not allowed_only or self.allowed(t["name"]))]

    def __repr__(self):
        return "ToolserverClient(%s, token=%s)" % (self.url, self.token_source)


# ── helpers ──────────────────────────────────────────────────────────────────
def _list_env(environ, key) -> list:
    return [v.strip() for v in (environ.get(key) or "").split(",") if v.strip()]


def _unwrap(doc):
    """Toolserver endpoints wrap payloads as {"result": ...}; unwrap once."""
    if isinstance(doc, dict) and "result" in doc and "error" not in doc:
        return doc["result"]
    return doc


def coerce_args(args: dict) -> dict:
    """JSON-looking string args (arrays/objects/bools/null) -> real values, so
    params a model typed as strings still reach the tool."""
    out = {}
    for k, v in (args or {}).items():
        if isinstance(v, str):
            s = v.strip()
            if s[:1] in "[{" or s in ("true", "false", "null"):
                try:
                    v = json.loads(s)
                except ValueError:
                    pass
        out[k] = v
    return out


_DEFAULT: dict = {}
_DEFAULT_LOCK = threading.Lock()


def default_client(environ=None, refresh: bool = False) -> ToolserverClient:
    """Process-wide client for the env-resolved (url, token); one per pair."""
    environ = os.environ if environ is None else environ
    probe = ToolserverClient(environ=environ)
    key = (probe.url, probe.token)
    with _DEFAULT_LOCK:
        if refresh or key not in _DEFAULT:
            _DEFAULT[key] = probe
        return _DEFAULT[key]


def status_line(status: dict) -> str:
    """One-line human summary for status bars: 'toolserver ok (198 tools)'."""
    if not status:
        return "toolserver: unknown"
    if status.get("ok"):
        return "toolserver ok (%d tools)" % int(status.get("tool_count") or 0)
    auth = status.get("auth")
    if auth in ("missing", "rejected"):
        return "toolserver auth %s" % auth
    return "toolserver unreachable"
