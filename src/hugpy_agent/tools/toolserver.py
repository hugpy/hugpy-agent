"""Bridge from the running abstract_toolserver into the hugpy-agent toolset —
the CATEGORICAL contract, default ON.

Transport, token/url resolution, the catalog cache and the allowlist all live
in the SHARED client (``hugpy_agent.toolserver_client``); this module is the
registry-facing skin the agent loop (chat / run / serve / subagents) uses:

  meta mode (default)   three static ToolSpecs — ts_categories / ts_list /
                        ts_call — whose handlers POST to /ts/*. The standing
                        prompt cost is those three schemas, whatever the
                        server's tool count.
  flat mode (opt-in)    HUGPY_AGENT_TOOLSERVER_TOOLS=flat registers every
                        ALLOWED toolserver tool as its own ToolSpec (native
                        tool-calling models). A name already taken by a local
                        tool (fs_glob…) keeps the local, jailed one.

Default ON: enabled whenever the shared client resolves a url (the default
always does) and a bounded probe succeeds; when the toolserver is unreachable
or unauthorized the agent runs normally and states why ONCE (errors-as-data,
never a crash, bounded timeouts so startup never hangs). Opt out with
HUGPY_AGENT_TOOLSERVER=0 or `--no-toolserver`.

Allowlist (see toolserver_client.classify): readonly + mutating tools are
callable (mutating ones still pass the loop's policy gate as RISK_NETWORK);
PRIVILEGED tools (vm_*, vmpool_*, sys_*, browser_*, fs_write_file, db_query
writes, oauth/session control) are refused as data unless named in
HUGPY_AGENT_TOOLSERVER_ALLOW (or '*'). HUGPY_AGENT_TOOLSERVER_DENY wins.
"""
from __future__ import annotations

import json
import os
import socket

from . import RISK_DESTRUCTIVE, RISK_NETWORK, RISK_READONLY, ToolSpec
from .. import toolserver_client as tsc
from ..toolserver_client import (ToolserverClient as SharedClient,   # noqa: F401
                                 ToolserverError, parse_env_file as _parse_env_file,
                                 file_token_ok as _file_token_ok)

# ── back-compat names (earlier callers/tests import these from here) ─────────
_TOKEN_KEYS = tsc.TOKEN_ENV_KEYS
_ENV_FILES = tsc.ENV_FILES
_DEFAULT_BASE = tsc.DEFAULT_URL
PROBE_TIMEOUT = tsc.PROBE_TIMEOUT
CALL_TIMEOUT_CAP = None      # ts_call inherits cfg.timeout (cold GPU tools slow)

# result governor caps (a huge ts_call result must not blow the context)
GOV_MAX_LINES = int(os.environ.get("HUGPY_AGENT_TOOLSERVER_MAX_LINES", "200") or 200)
GOV_MAX_BYTES = int(os.environ.get("HUGPY_AGENT_TOOLSERVER_MAX_BYTES", "16384") or 16384)
GOV_ENABLED = os.environ.get("HUGPY_AGENT_TOOLSERVER_GOVERNOR", "1").strip().lower() \
    not in ("0", "false", "no", "off")

# process-wide memo of a successful probe: {(base, token): ToolserverClient}.
# A failed probe is never memoized (a toolserver that comes up later is picked
# up on the next run).
_CLIENT_MEMO: dict = {}


def _env_file_values() -> list:
    return tsc.env_file_values()


def resolve_base(cfg, environ=None, file_values=None) -> str:
    """The toolserver base url: cfg.toolserver_url, else HUGPY_TOOLSERVER_URL /
    TOOLSERVER_URL / STATION_CONSOLE_TOOLSERVER (env, then env files), else the
    toolserver advertised on this host (abstract_toolserver.discovery)."""
    return tsc.resolve_url((getattr(cfg, "toolserver_url", "") or ""), environ, file_values)


def resolve_token(cfg, base: str, environ=None, file_values=None):
    """(token, source). cfg.toolserver_token wins; else process env; else an
    env-file token when the disk-token safety rule admits `base`."""
    return tsc.resolve_token((getattr(cfg, "toolserver_token", "") or ""), base,
                             environ, file_values)


def resolve_locus(cfg, environ=None) -> str:
    """The agent's stable comms locus: cfg.toolserver_locus, else
    HUGPY_AGENT_LOCUS, else cfg.agent_name, else the short hostname —
    lowercased (messages.py lowercases loci)."""
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


# ── arg coercion / autofill ──────────────────────────────────────────────────
_coerce = tsc.coerce_args

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
_RISK_OF = {tsc.READONLY: RISK_READONLY, tsc.MUTATING: RISK_NETWORK,
            tsc.PRIVILEGED: RISK_DESTRUCTIVE}


def classify_target(name: str, args: dict | None = None) -> str:
    """Registry risk class for a toolserver tool: readonly -> RISK_READONLY,
    mutating -> RISK_NETWORK, privileged -> RISK_DESTRUCTIVE."""
    return _RISK_OF.get(tsc.classify(name, args), RISK_NETWORK)


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
                  "the %s arguments to fetch less]"
                  % (stats["lines_dropped"], stats["bytes_dropped"], tool or "ts_call"))
        return kept.rstrip("\n") + "\n" + marker
    except Exception:
        return text


# ── the registry-facing client (meta-tool handlers over the shared client) ───
class ToolserverClient:
    """Meta-tool handlers bound to one shared client + the agent's locus.
    Every method returns a JSON STRING (errors as data)."""

    def __init__(self, base, token, locus, call_timeout=120.0, allow=None, deny=None,
                 shared: SharedClient | None = None):
        self.base = base
        self.token = token
        self.locus = locus
        self.call_timeout = call_timeout
        self.shared = shared or SharedClient(base, token, timeout=call_timeout,
                                             allow=allow, deny=deny)

    def _post(self, path, body, timeout):
        try:
            return tsc._unwrap(self.shared._post(path, body, timeout)), None
        except ToolserverError as exc:
            return None, str(exc)
        except Exception as exc:   # noqa: BLE001 — errors-as-data
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
        if isinstance(out, dict) and isinstance(out.get("tools"), list):
            for t in out["tools"]:
                if isinstance(t, dict) and t.get("name"):
                    t["allowed"] = self.shared.allowed(t["name"])
        return govern_result(json.dumps(out, default=str), "ts_list")

    # -- ts_call --
    def call(self, name: str, arguments=None) -> str:
        target = str(name or "").strip()
        if not target:
            return json.dumps({"error": "ts_call requires a tool name (use "
                               "ts_categories then ts_list to find one)"})
        call_args = _autofill(target, _coerce(arguments or {}), self.locus)
        text = self.shared.call_json(target, call_args, self.call_timeout)
        return govern_result(text, target)

    def risk_of_call(self, args) -> str:
        args = args or {}
        return classify_target(args.get("name"), args.get("arguments") or {})


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


def flat_specs(client: ToolserverClient, taken=()) -> list:
    """One ToolSpec per ALLOWED toolserver tool (flat mode). Names in `taken`
    (already-registered local tools) are skipped so the jailed local tool
    keeps its name."""
    out = []
    try:
        tools = client.shared.list_tools()
    except ToolserverError:
        return out
    taken = set(taken or ())
    for t in tools:
        name = t["name"]
        if name in taken or not client.shared.allowed(name):
            continue
        out.append(ToolSpec(
            name=name, description=t["description"] or name,
            parameters=t["input_schema"],
            handler=(lambda _n: (lambda **kw: client.call(_n, kw)))(name),
            risk_class=classify_target(name),
            dynamic_risk=(lambda _n: (lambda a: classify_target(_n, a)))(name)))
    return out


def make_client(cfg, environ=None) -> ToolserverClient:
    """Build (without probing) the registry-facing client for `cfg`."""
    environ = os.environ if environ is None else environ
    file_values = _env_file_values()
    base = resolve_base(cfg, environ, file_values)
    token, _src = resolve_token(cfg, base, environ, file_values)
    try:
        call_timeout = float(getattr(cfg, "timeout", 120) or 120)
    except (TypeError, ValueError):
        call_timeout = 120.0
    return ToolserverClient(base, token, resolve_locus(cfg, environ),
                            call_timeout=call_timeout,
                            allow=getattr(cfg, "toolserver_allow", None),
                            deny=getattr(cfg, "toolserver_deny", None))


def specs(cfg, on_event=None, environ=None, client=None, taken=()) -> list:
    """Build the toolserver ToolSpecs for a registry.

    Default ON: probes POST /ts/categories (bounded); on success returns the
    meta-tools (plus every allowed tool in flat mode), on any failure emits ONE
    event stating why and returns [] so the agent runs normally. `client` is
    injectable for tests (its .probe() is still honored)."""
    emit = on_event or (lambda *a, **k: None)
    environ = os.environ if environ is None else environ
    if not getattr(cfg, "toolserver", True) or not tsc.enabled(environ):
        emit("toolserver", "disabled", "HUGPY_AGENT_TOOLSERVER is off")
        return []

    if client is None:
        file_values = _env_file_values()
        base = resolve_base(cfg, environ, file_values)
        token, tok_src = resolve_token(cfg, base, environ, file_values)
        # memo key carries the allowlist too: a run with a different
        # allow/deny must not inherit another run's verdicts.
        key = (base, token, tuple(getattr(cfg, "toolserver_allow", None) or ()),
               tuple(getattr(cfg, "toolserver_deny", None) or ()))
        memo = _CLIENT_MEMO.get(key)
        if memo is not None:
            client = memo
        else:
            client = make_client(cfg, environ)
            ok, detail = client.probe()
            if not ok:
                emit("toolserver", "unavailable",
                     "%s unreachable (token %s): %s; agent runs without toolserver "
                     "tools" % (base, tok_src, detail))
                return []
            _CLIENT_MEMO[key] = client
            emit("toolserver", "ready",
                 "%s (token %s); ts_categories/ts_list/ts_call available"
                 % (base, tok_src))

    out = build_specs(client)
    mode = (getattr(cfg, "toolserver_tools", "") or environ.get("HUGPY_AGENT_TOOLSERVER_TOOLS")
            or "meta").strip().lower()
    if mode == "flat":
        flat = flat_specs(client, taken)
        emit("toolserver", "flat", "%d toolserver tools registered directly" % len(flat))
        out.extend(flat)
    return out
