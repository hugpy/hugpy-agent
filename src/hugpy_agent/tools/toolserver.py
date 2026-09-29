"""Native (stdlib-only) bridge from the running abstract_toolserver into the
hugpy-agent toolset — the CATEGORICAL contract, default ON.

Category nesting is a TOOLSERVER feature (separate workstream): the server
serves the whole ~250-tool surface behind THREE endpoints, so hugpy-agent never
scrapes /endpoints or ?help and never categorizes anything itself:

  POST /ts/categories {}                          -> [{category, tools, summary}]
  POST /ts/list       {"category": str}           -> {category, count, tools:[{name, description, parameters}]}
  POST /ts/call       {"name": str, "arguments": {}} -> exactly what that tool returns
  (any of these may be wrapped {"result": ...} like other toolserver endpoints.)

hugpy-agent's part is THIN: three static ToolSpecs whose handlers just POST to
/ts/*. The only standing prompt cost is those three schemas. This module imports
**no** ``abstract_*`` package and no vendor SDK, only urllib — ``import
hugpy_agent`` works with nothing else installed.

Default ON: enabled whenever a base url resolves (the default always does) and a
bounded probe (POST /ts/categories) succeeds; when the toolserver is unreachable
or unauthorized the agent runs normally and states why ONCE (errors-as-data,
never a crash, bounded timeouts so startup never hangs). Opt out with
HUGPY_AGENT_TOOLSERVER=0.

Token + base resolution and the disk-token safety rule are ported faithfully
from the reference bridge (abstract_claude/src/abstract_claude/mcp.py).
"""
from __future__ import annotations

import json
import os
import socket
import urllib.parse
import urllib.request

from . import RISK_NETWORK, RISK_READONLY, ToolSpec

# ── token / base resolution (ported from abstract_claude.mcp) ────────────────
_TOKEN_KEYS = ("TOOLSERVER_TOKEN", "HUGPY_OPERATOR_TOKEN",
               "STATION_CONSOLE_TOOLSERVER_TOKEN", "TOOLSERVER_OPERATOR_TOKEN")
_ENV_FILES = ("~/.config/hugpy-station/toolserver.env",
              "~/.config/hugpy/operator.env",
              "/etc/hugpy-station/toolserver.env",
              "/etc/hugpy/operator.env")
_DEFAULT_BASE = "https://toolserver.hugpy.ai"
_LOOPBACK_HOSTS = {"localhost", "::1"}

PROBE_TIMEOUT = 8.0          # bounded: startup must never hang
CALL_TIMEOUT_CAP = None      # ts_call inherits cfg.timeout (cold GPU tools slow)

# result governor caps (a huge ts_call result must not blow the context)
GOV_MAX_LINES = int(os.environ.get("HUGPY_AGENT_TOOLSERVER_MAX_LINES", "200") or 200)
GOV_MAX_BYTES = int(os.environ.get("HUGPY_AGENT_TOOLSERVER_MAX_BYTES", "16384") or 16384)
GOV_ENABLED = os.environ.get("HUGPY_AGENT_TOOLSERVER_GOVERNOR", "1").strip().lower() \
    not in ("0", "false", "no", "off")

# process-wide memo of a successful probe: {(base, token): ToolserverClient}.
# Keeps every top-level run from re-probing; a failed probe is never memoized
# (so a toolserver that comes up later is picked up on the next run).
_CLIENT_MEMO: dict = {}


def _parse_env_file(path: str) -> dict:
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


def _env_file_values() -> list:
    return [_parse_env_file(p) for p in _ENV_FILES]


def _from_env(environ, keys):
    for k in keys:
        v = (environ.get(k) or "").strip()
        if v:
            return v
    return ""


def _from_files(file_values, keys):
    """(value, that file's {KEY: VALUE}) from the first env file carrying any
    of `keys`; ("", {}) when none."""
    for values in file_values:
        for k in keys:
            if values.get(k):
                return values[k], values
    return "", {}


def _file_token_ok(base: str, file_values: dict) -> bool:
    """May a token read off DISK travel to `base`? Yes for https, loopback, the
    host the token's own env file names (STATION_CONSOLE_TOOLSERVER) and the
    default toolserver host. Anything else is a plaintext host chosen by nothing
    but process env — withhold, so a lone URL never forwards the operator's file
    token to an arbitrary host."""
    u = urllib.parse.urlparse(base)
    host = (u.hostname or "").lower()
    if not host:
        return False
    if u.scheme == "https" or host in _LOOPBACK_HOSTS or host.startswith("127."):
        return True
    trusted = {urllib.parse.urlparse(_DEFAULT_BASE).hostname}
    own = file_values.get("STATION_CONSOLE_TOOLSERVER", "")
    if own:
        trusted.add((urllib.parse.urlparse(own).hostname or "").lower())
    return host in trusted


def resolve_base(cfg, environ=None, file_values=None) -> str:
    """The toolserver base url. cfg.toolserver_url wins; else env TOOLSERVER_URL
    / STATION_CONSOLE_TOOLSERVER, then a STATION_CONSOLE_TOOLSERVER named in an
    env file, then the default host."""
    environ = os.environ if environ is None else environ
    file_values = _env_file_values() if file_values is None else file_values
    explicit = (getattr(cfg, "toolserver_url", "") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    return (_from_env(environ, ("TOOLSERVER_URL", "STATION_CONSOLE_TOOLSERVER"))
            or _from_files(file_values, ("STATION_CONSOLE_TOOLSERVER",))[0]
            or _DEFAULT_BASE).rstrip("/")


def resolve_token(cfg, base: str, environ=None, file_values=None):
    """(token, source). cfg.toolserver_token wins (sent unconditionally). Else a
    process-env token is sent unconditionally; a file token only when
    _file_token_ok(base). ("", "withheld"/"none") otherwise."""
    environ = os.environ if environ is None else environ
    file_values = _env_file_values() if file_values is None else file_values
    explicit = (getattr(cfg, "toolserver_token", "") or "").strip()
    if explicit:
        return explicit, "config"
    tok_env = _from_env(environ, _TOKEN_KEYS)
    if tok_env:
        return tok_env, "env"
    tok_file, tok_src = _from_files(file_values, _TOKEN_KEYS)
    if tok_file and _file_token_ok(base, tok_src):
        return tok_file, "file"
    return "", ("withheld" if tok_file else "none")


def resolve_locus(cfg, environ=None) -> str:
    """The agent's stable comms locus: cfg.toolserver_locus, else
    HUGPY_AGENT_LOCUS, else cfg.agent_name, else the short hostname — lowercased
    (messages.py lowercases loci)."""
    environ = os.environ if environ is None else environ
    for v in ((getattr(cfg, "toolserver_locus", "") or "").strip(),
              (environ.get("HUGPY_AGENT_LOCUS") or "").strip(),
              (getattr(cfg, "agent_name", "") or "").strip()):
        if v:
            return v.lower()
    try:
        return socket.gethostname().split(".")[0].lower() or "hugpy-agent"
    except Exception:
        return "hugpy-agent"


# ── HTTP ─────────────────────────────────────────────────────────────────────
def _http(base, token, path, body, timeout):
    """POST JSON to base+path, return the parsed reply. Raises on transport /
    HTTP error (callers convert to data)."""
    url = base + path
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Operator-Token", token)
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
    try:
        return json.loads(raw)
    except ValueError:
        return {"result": raw}


def _unwrap(resp):
    """Toolserver endpoints may wrap payloads as {"result": ...}; unwrap once."""
    if isinstance(resp, dict) and "result" in resp and "error" not in resp:
        return resp["result"]
    return resp


# ── arg coercion / autofill (ported + comms locus) ───────────────────────────
def _coerce(args):
    """JSON-looking string args (arrays/objects/bools/null) -> real values, so
    params typed as string still reach the tool. Plain strings and bare numbers
    are left alone."""
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


# comms tools whose caller identity should default to the agent's locus.
_LOCUS_FROM = {"comms_send", "comms_reply", "comms_ping"}   # -> from_
_LOCUS_SELF = {"comms_poll": "locus", "comms_inbox": "to"}  # -> the named arg


def _autofill(name, args, locus):
    """Fill the agent's stable locus into comms calls when the model omitted it,
    so messages to/from the agent route. Never overrides an explicit value."""
    if not locus:
        return args
    if name in _LOCUS_FROM and not str(args.get("from_") or "").strip():
        args["from_"] = locus
    key = _LOCUS_SELF.get(name)
    if key and not str(args.get(key) or "").strip():
        args[key] = locus
    return args


# ── target-tool risk classification (for the policy gate) ────────────────────
# Deterministic from the tool NAME (recorded fact). WRITE tokens win: a name
# carrying any mutating verb is side-effecting (RISK_NETWORK); else a name
# carrying a read verb is RISK_READONLY; an unrecognized name is conservatively
# side-effecting (fail closed — the posture the agent's own tools already take).
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
""".split())


def classify_target(name: str) -> str:
    toks = set(str(name or "").split("_"))
    if toks & _WRITE_TOKENS:
        return RISK_NETWORK
    if toks & _READ_TOKENS:
        return RISK_READONLY
    return RISK_NETWORK


# ── result governor ──────────────────────────────────────────────────────────
def truncate_result(text, max_lines=None, max_bytes=None, head_fraction=0.75):
    """Pure: (kept_text, stats) where stats is None when `text` fits both caps,
    else a {lines/bytes total/kept/dropped} dict. Keeps head_fraction of the cap
    as head and the rest as tail, first by lines then by bytes."""
    max_lines = GOV_MAX_LINES if max_lines is None else int(max_lines)
    max_bytes = GOV_MAX_BYTES if max_bytes is None else int(max_bytes)
    lines = text.split("\n")
    bytes_total, lines_total = len(text.encode("utf-8")), len(lines)
    if lines_total <= max_lines and bytes_total <= max_bytes:
        return text, None
    kept = text
    if lines_total > max_lines:
        head_n = max(1, int(max_lines * head_fraction))
        tail_n = max(0, max_lines - head_n)
        kept = "\n".join(lines[:head_n] + (lines[-tail_n:] if tail_n else []))
    kept_b = len(kept.encode("utf-8"))
    if kept_b > max_bytes:
        ratio = max_bytes / float(kept_b)
        head_c = max(1, int(len(kept) * ratio * head_fraction))
        tail_c = max(0, int(len(kept) * ratio) - head_c)
        kept = kept[:head_c] + ("\n" + kept[-tail_c:] if tail_c else "")
    kept_lines = kept.count("\n") + 1
    kept_b = len(kept.encode("utf-8"))
    return kept, {"lines_total": lines_total, "bytes_total": bytes_total,
                  "lines_kept": kept_lines, "bytes_kept": kept_b,
                  "lines_dropped": max(0, lines_total - kept_lines),
                  "bytes_dropped": max(0, bytes_total - kept_b)}


def govern_result(text, tool=""):
    """Cap one serialized tool result and append a truncation marker. Never
    raises: on any internal error the original text is returned unchanged."""
    try:
        if not GOV_ENABLED:
            return text
        kept, stats = truncate_result(text)
        if stats is None:
            return text
        marker = ("[truncated: %d more lines / %d more bytes not shown; narrow "
                  "the ts_call arguments to fetch less]"
                  % (stats["lines_dropped"], stats["bytes_dropped"]))
        return kept.rstrip("\n") + "\n" + marker
    except Exception:
        return text


# ── the client (thin /ts/* caller) + meta-tool specs ─────────────────────────
class ToolserverClient:
    """Thin client for the toolserver's three /ts/* endpoints. Holds base, token
    and the agent's locus; every method returns a JSON STRING (errors as data)."""

    def __init__(self, base, token, locus, call_timeout=120.0):
        self.base = base
        self.token = token
        self.locus = locus
        self.call_timeout = call_timeout

    def _post(self, path, body, timeout):
        try:
            return _unwrap(_http(self.base, self.token, path, body, timeout)), None
        except Exception as exc:   # transport / HTTP / decode: errors-as-data
            return None, "%s: %s" % (type(exc).__name__, exc)

    def probe(self):
        """(ok, detail): POST /ts/categories bounded, so default-on can decide."""
        out, err = self._post("/ts/categories", {}, PROBE_TIMEOUT)
        if err is not None:
            return False, err
        if isinstance(out, dict) and out.get("error"):
            return False, str(out["error"])
        return True, out

    # -- ts_categories --
    def categories(self) -> str:
        out, err = self._post("/ts/categories", {}, PROBE_TIMEOUT)
        if err is not None:
            return json.dumps({"error": "toolserver /ts/categories failed: %s" % err})
        return govern_result(json.dumps(out, default=str), "ts_categories")

    # -- ts_list --
    def list_category(self, category: str) -> str:
        out, err = self._post("/ts/list", {"category": str(category or "")},
                              PROBE_TIMEOUT)
        if err is not None:
            return json.dumps({"error": "toolserver /ts/list failed: %s" % err})
        return govern_result(json.dumps(out, default=str), "ts_list")

    # -- ts_call --
    def call(self, name: str, arguments=None) -> str:
        target = str(name or "").strip()
        if not target:
            return json.dumps({"error": "ts_call requires a tool name (use "
                               "ts_categories then ts_list to find one)"})
        call_args = _autofill(target, _coerce(arguments or {}), self.locus)
        out, err = self._post("/ts/call", {"name": target, "arguments": call_args},
                              self.call_timeout)
        if err is not None:
            return json.dumps({"error": "%s call failed: %s" % (target, err)})
        return govern_result(json.dumps(out, default=str), target)

    def risk_of_call(self, args) -> str:
        return classify_target((args or {}).get("name"))


# Exact descriptions (operator-specified — do not paraphrase).
_META_DESCRIPTIONS = {
    "ts_categories": "List toolserver tool categories with counts. Call first, "
                     "then ts_list.",
    "ts_list": "List the tools in one category with their parameters. Args: "
               "category.",
    "ts_call": "Invoke a toolserver tool by full name. Args: name, arguments "
               "(object, from ts_list).",
}


def build_specs(client: ToolserverClient) -> list:
    """The three static meta-tool ToolSpecs bound to `client`."""
    return [
        ToolSpec(name="ts_categories",
                 description=_META_DESCRIPTIONS["ts_categories"],
                 parameters={"type": "object", "properties": {}},
                 handler=lambda: client.categories(),
                 risk_class=RISK_READONLY),
        ToolSpec(name="ts_list",
                 description=_META_DESCRIPTIONS["ts_list"],
                 parameters={"type": "object",
                             "properties": {"category": {"type": "string"}},
                             "required": ["category"]},
                 handler=lambda category: client.list_category(category),
                 risk_class=RISK_READONLY),
        ToolSpec(name="ts_call",
                 description=_META_DESCRIPTIONS["ts_call"],
                 parameters={"type": "object",
                             "properties": {
                                 "name": {"type": "string"},
                                 "arguments": {"type": "object"}},
                             "required": ["name"]},
                 handler=lambda name, arguments=None: client.call(name, arguments),
                 # static class is fail-closed side-effecting; the real per-call
                 # risk is the TARGET tool's, resolved via dynamic_risk.
                 risk_class=RISK_NETWORK,
                 dynamic_risk=client.risk_of_call),
    ]


def specs(cfg, on_event=None, environ=None, client=None) -> list:
    """Build the toolserver ToolSpecs for a registry.

    Default ON: probes POST /ts/categories (bounded); on success returns the
    three meta-tools, on any failure emits ONE event stating why and returns []
    so the agent runs normally. `client` is injectable for tests (its .probe()
    is still honored, so an injected transport exercises the same path)."""
    emit = on_event or (lambda *a, **k: None)
    if not getattr(cfg, "toolserver", True):
        emit("toolserver", "disabled", "HUGPY_AGENT_TOOLSERVER is off")
        return []
    environ = os.environ if environ is None else environ

    if client is None:
        file_values = _env_file_values()
        base = resolve_base(cfg, environ, file_values)
        token, tok_src = resolve_token(cfg, base, environ, file_values)
        memo = _CLIENT_MEMO.get((base, token))
        if memo is not None:
            return build_specs(memo)
        locus = resolve_locus(cfg, environ)
        try:
            call_timeout = float(getattr(cfg, "timeout", 120) or 120)
        except (TypeError, ValueError):
            call_timeout = 120.0
        client = ToolserverClient(base, token, locus, call_timeout=call_timeout)
        ok, detail = client.probe()
        if not ok:
            emit("toolserver", "unavailable",
                 "%s unreachable (token %s): %s; agent runs without toolserver "
                 "tools" % (base, tok_src, detail))
            return []
        _CLIENT_MEMO[(base, token)] = client
        emit("toolserver", "ready",
             "%s (token %s); ts_categories/ts_list/ts_call available"
             % (base, tok_src))

    return build_specs(client)
