"""Model Gateway — the OpenAI-compatible client for the hugpy fleet.

Lifted and adapted (headless, PyQt stripped) from field-tested clients in
`abstract_ide`: `servicesTab/src/main.py` (`llm_stream`, `resolve_routes`,
`_origin`, keepalive + non-streaming fallback handling) and
`hugpyTab/src/main.py` (`_req`, `_list_from` base normalization) — those are
the one proven implementation of the wire contract (design §2b: "reuse, don't
rewrite").

Platform gotchas this module owns (design §2, live-probed 2026-07-14):
  * The public front only proxies `/api/*` (prefix-stripped); bare `/v1/*` is
    shadowed by the marketing SPA. A configured base may end in `/api`, `/v1`,
    `/api/v1`, or be a bare origin — `candidate_routes()` normalizes and
    `resolve()` probes candidates once, cached.
  * Every chat payload carries `"max_chunks": 1` — kills the known
    continuation-prompt leak at the chat_runner chunking seam.
  * `usage` comes back null-filled — `estimate_tokens()` is the single place
    client-side token accounting lives, so swapping in real usage later is a
    one-line change.
  * Errors and timeouts are DATA: `chat()` returns a ChatResult with
    `ok=False` and a structured error string instead of raising, because a
    failed model call is an observation the agent loop must reason about,
    not a crash.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .config import Config, DEFAULT_CTX_FALLBACK

# The continuation-prompt string the chat_runner chunking seam is known to
# leak into reply bodies (design §2.3). max_chunks:1 should prevent it, but
# we scrub defensively too — corrupted structured output is the worst failure
# mode for a tool-calling agent.
CONTINUATION_LEAK = "Continue exactly where I left off. Do not repeat any previous text."


NO_THINK_SUFFIX = " /no_think"


def apply_no_think(messages):
    """Return a COPY of `messages` with ` /no_think` appended to the latest
    user turn — mirroring how the reference hugpyTab keeps a clean history and
    only suffixes the outgoing wire (design §2b). The input list and its dicts
    are never mutated, so a caller's stored history stays pristine.

    A plain-string content is suffixed directly; a multimodal parts-list turn
    (content is a list of {type:text|image_url,...}) gets the suffix on its
    last text part (or a new text part if it somehow has none), so the image
    parts are untouched.
    """
    out = [dict(m) if isinstance(m, dict) else m for m in messages]
    for m in reversed(out):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            m["content"] = content + NO_THINK_SUFFIX
        elif isinstance(content, list):
            parts = [dict(p) if isinstance(p, dict) else p for p in content]
            text_idx = next(
                (i for i in range(len(parts) - 1, -1, -1)
                 if isinstance(parts[i], dict) and parts[i].get("type") == "text"),
                None)
            if text_idx is None:
                parts.append({"type": "text", "text": NO_THINK_SUFFIX.strip()})
            else:
                parts[text_idx]["text"] = (parts[text_idx].get("text") or "") \
                    + NO_THINK_SUFFIX
            m["content"] = parts
        # a non-string/non-list content is left as-is (nothing to suffix)
        break
    return out


# ── second-in-line brain support ─────────────────────────────────────────
# The workers probe is a run-start amenity: a few seconds, best-effort, and
# NEVER load-bearing — every failure mode below resolves to "use the primary".

# Timeout for the run-start GET /llm/workers probe. Deliberately short: the
# probe is an optimization (start on the brain that is already seated), so a
# slow control plane must cost seconds, not a run.
WORKERS_PROBE_TIMEOUT = 5

# Substrings that mark a chat failure as CAPACITY-class — the worker refused
# to seat/serve the model (fleet loadrefusal/budgetrefusal wording plus the
# raw llama.cpp/CUDA phrasings that ride inside HTTP error bodies). Matched
# case-insensitively against the structured error string.
#
# k96 additions (deliberate — each marker is a live central/worker phrasing):
#   * "failure is permanent" — central's load-verdict cache answering without a
#     re-attempt ("<model> on <worker> failed to load moments ago and the
#     failure is permanent"; resolvers/remote._verdict_message). Retrying the
#     SAME brain cannot fix it, which is precisely when the ladder must walk.
#   * "refusing without evicting" / "no_makeroom" — the fleet's polite-load
#     refusal wording: with no_makeroom riding every brain chat, a cold brain
#     that would need an eviction is refused fast, and that refusal must read
#     as capacity so the ladder walks instead of burning the retry budget.
CAPACITY_MARKERS = ("won't fit", "wont fit", "out of memory",
                    "loadrefusal", "budgetrefusal",
                    "failure is permanent",
                    "refusing without evicting", "no_makeroom")


def is_capacity_error(error: str | None) -> bool:
    """True when a ChatResult error string reads as a capacity-class refusal
    (the model could not be seated), as opposed to a transport or model
    failure. Curly apostrophes are folded so a prettified "won’t fit"
    still matches."""
    s = (error or "").lower().replace("’", "'")
    return any(marker in s for marker in CAPACITY_MARKERS)


def brain_matches_key(model: str, key: str) -> bool:
    """Does a configured brain name match a worker allocation's model_key?

    Exact match, or equal bare tails after '~' — the catalog serves keys in
    'Org~Name' form (e.g. 'Qwen~Qwen3-Coder-Next-GGUF') while worker rows may
    report either the full key or just the bare name, and operators type both.
    """
    m, k = (model or "").strip(), (key or "").strip()
    if not m or not k:
        return False
    return m == k or m.split("~")[-1] == k.split("~")[-1]


def slot_model_keys(payload) -> list[str] | None:
    """model_keys actively SEATED (allocation kind == 'slot') in a
    /llm/workers payload; None when the payload isn't worker rows (an error
    body, HTML, etc.) so callers can tell 'could not read the fleet' from
    'nothing is seated'. Accepts the bare list form and a {"workers": [...]}
    wrapper. Pure — unit-testable offline."""
    workers = payload if isinstance(payload, list) else \
        payload.get("workers") if isinstance(payload, dict) else None
    if not isinstance(workers, list):
        return None
    keys: list[str] = []
    for w in workers:
        if not isinstance(w, dict):
            continue
        for a in (w.get("allocations") or []):
            if isinstance(a, dict) and a.get("kind") == "slot" \
                    and a.get("model_key"):
                keys.append(str(a["model_key"]))
    return keys


def warm_model_keys(payload) -> list[str] | None:
    """model_keys WARM (ready to answer now) in a /llm/workers payload, or
    None when the payload isn't worker rows — the ladder's warm source (k96).

    Broader than slot_model_keys deliberately: a 'slot' allocation counts
    unless it reports healthy=False (a seat mid-load/wedged is not warm), and
    a 'ram' (in-process resident) allocation counts too — an in-process
    resident answers without any load. Parsed defensively; malformed rows and
    allocation entries are skipped, never fatal. Pure — unit-testable
    offline."""
    workers = payload if isinstance(payload, list) else \
        payload.get("workers") if isinstance(payload, dict) else None
    if not isinstance(workers, list):
        return None
    keys: list[str] = []
    for w in workers:
        if not isinstance(w, dict):
            continue
        for a in (w.get("allocations") or []):
            if not isinstance(a, dict) or not a.get("model_key"):
                continue
            if a.get("healthy") is False:      # tri-state: absent = warm
                continue
            keys.append(str(a["model_key"]))
    return keys


def resolve_brain_ladder(cfg) -> tuple[list[str], bool]:
    """The ordered brain ladder for a run: (ladder, explicit) — k96.

    HUGPY_AGENT_BRAINS (cfg.brains, csv, best-first) wins when set:
    explicit=True, and by convention the LAST entry is the operator's PILOT
    LIGHT — a model small enough that a cold load is cheap (documented, not
    enforced). Unset falls back to the pre-ladder pair: [model] + [model_2 if
    set], explicit=False — byte-compatible with the second-in-line feature.
    Duplicates are dropped keeping the first (best) position; empty entries
    are skipped. The ladder is never empty: it always ends at [cfg.model]."""
    raw = list(getattr(cfg, "brains", None) or [])
    explicit = bool(raw)
    if not raw:
        raw = [cfg.model]
        model_2 = getattr(cfg, "model_2", "")
        if model_2:
            raw.append(model_2)
    ladder: list[str] = []
    for b in raw:
        b = str(b or "").strip()
        if b and b not in ladder:
            ladder.append(b)
    return (ladder or [cfg.model]), explicit


def pick_ladder_brain(warm: list[str] | None, ladder: list[str],
                      explicit: bool) -> tuple[str, int, str]:
    """Warm-first run-start choice over the ladder: (model, position, why).

    First ladder entry that is warm wins. None-warm splits by provenance:
    an EXPLICIT ladder (HUGPY_AGENT_BRAINS) starts on its LAST entry — the
    designated pilot light, whose cold load is cheap by design — while the
    back-compat pair keeps the historical answer (the primary), because the
    operator never designated a pilot light there. A failed probe (warm is
    None) always answers ladder[0]: this probe is an optimization and must
    never redirect a run on no evidence. Pure — probe I/O lives in
    Gateway.warm_models."""
    if len(ladder) == 1:
        return ladder[0], 0, "single brain configured"
    if warm is None:
        return ladder[0], 0, "workers probe failed; defaulting to ladder[0]"
    for i, model in enumerate(ladder):
        if any(brain_matches_key(model, k) for k in warm):
            if i == 0:
                return model, 0, "ladder[0] %r is warm" % model
            return model, i, ("earlier ladder entries (%s) are not seated on "
                              "any worker; %r is warm (ladder position %d)"
                              % (", ".join(ladder[:i]), model, i + 1))
    if explicit:
        last = len(ladder) - 1
        return ladder[last], last, ("no ladder entry is warm; starting on the "
                                    "pilot light %r (last entry — cold load "
                                    "is cheap by design)" % ladder[last])
    return ladder[0], 0, "neither brain is warm; defaulting to the primary"


def pick_resident_brain(seated: list[str] | None, primary: str,
                        secondary: str) -> tuple[str, str]:
    """Run-start choice between the primary and second-in-line brain, given
    the seated model_keys (or None = probe failed). Returns (model, why).

    LEGACY (pre-k96) import surface: the loop now drives resolve_brain_ladder
    + pick_ladder_brain, which subsume this decision table; kept because
    external clones import it and its contract is still true.

    Primary seated -> primary; else secondary seated -> secondary; else (and
    on any probe failure) primary — the primary is the operator's stated
    preference, so only positive evidence that it is absent AND the standby
    is present ever redirects a run. Pure — the probe I/O lives in
    Gateway.seated_model_keys."""
    if not secondary or secondary == primary:
        return primary, "no second-in-line brain configured"
    if seated is None:
        return primary, "workers probe failed; defaulting to primary"
    if any(brain_matches_key(primary, k) for k in seated):
        return primary, "primary brain is seated on a worker"
    if any(brain_matches_key(secondary, k) for k in seated):
        return secondary, ("primary brain %r is not seated on any worker; "
                           "second-in-line %r is" % (primary, secondary))
    return primary, "neither brain is seated; defaulting to primary"


def estimate_tokens(text: str) -> int:
    """Client-side token estimate (~4 chars/token). Isolated here because the
    /v1 seam returns null usage today (design §2.2); when real usage lands,
    this is the only function to replace. //3 not //4: agent transcripts are
    JSON-escaped tool dumps (~2.5-3 chars/token), and //4 undercounting made
    compaction fire only after the wire prompt already overflowed a 32k slot
    (sentinel case runs died at step ~14, 2026-08-06)."""
    return max(1, len(text or "") // 3)


def origin(url: str) -> str:
    """scheme://host of a URL; passthrough for a pathless base."""
    p = urllib.parse.urlsplit(url)
    if p.scheme and p.netloc:
        return "%s://%s" % (p.scheme, p.netloc)
    return url.rstrip("/")


def normalize_base(base: str) -> str:
    """A hand-typed bare host (no scheme) makes urllib raise 'unknown url
    type' — default it to https:// (fleet hosts are TLS-fronted)."""
    base = (base or "").strip().rstrip("/")
    if base and "://" not in base:
        base = "https://" + base
    return base


def candidate_routes(base: str) -> list[tuple[str, str]]:
    """Ordered (chat_url, models_url) candidates for a configured base.

    Pure function (unit-testable offline). The first candidate follows the
    base's own suffix (`/v1` -> OpenAI convention, `/api` -> hugpy mount);
    then the hugpy `/api/v1` mount and bare `/v1` on the origin as fallbacks,
    mirroring servicesTab.resolve_routes' probe order.
    """
    base = normalize_base(base)
    o = origin(base)
    cands: list[tuple[str, str]] = []

    def add(prefix: str) -> None:
        pair = (prefix + "/chat/completions", prefix + "/models")
        if pair not in cands:
            cands.append(pair)

    if base.endswith("/v1"):
        add(base)
    elif base.endswith("/api"):
        add(base + "/v1")
    else:
        add(base + "/v1")
    add(o + "/api/v1")
    add(o + "/v1")
    return cands


@dataclass
class ChatResult:
    ok: bool
    text: str = ""
    error: str | None = None
    native_tool_calls: list = field(default_factory=list)
    request_id: str | None = None
    est_tokens: int = 0


class Gateway:
    """One client instance per (base, key). Route resolution is probed lazily
    and cached; every other method is a plain request with errors-as-data."""

    def __init__(self, base: str, api_key: str = "", model: str = "",
                 timeout: int = 300, no_think: bool = False):
        self.base = normalize_base(base)
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        # Think suppression for Qwen3-family brains: append ` /no_think` to the
        # WIRE copy of the latest user turn on every chat call. Owned here (the
        # single chat chokepoint) so the main loop, compaction, native probe,
        # and live_smoke all get it uniformly — and applied to a COPY so stored
        # journal/history is never mutated.
        self.no_think = no_think
        self._routes: tuple[str, str] | None = None   # (chat_url, models_url)
        self._models_cache: list | None = None

    @classmethod
    def from_config(cls, cfg: Config) -> "Gateway":
        return cls(cfg.base, cfg.api_key, cfg.model, cfg.timeout,
                   no_think=getattr(cfg, "no_think", False))

    # ── plumbing ─────────────────────────────────────────────────────────
    def _headers(self, ctype: str | None = None) -> dict:
        h = {"Accept": "application/json"}
        if ctype:
            h["Content-Type"] = ctype
        # Auth is currently open on dev, but we send the Bearer whenever a key
        # is configured so key enforcement can flip on without client changes.
        if self.api_key:
            h["Authorization"] = "Bearer %s" % self.api_key
        return h

    def api_json(self, path: str, method: str = "GET", payload=None,
                 timeout: int | None = None):
        """JSON request against an absolute path on the base's ORIGIN (e.g.
        '/api/ml/vision'). Raises urllib errors — callers that need
        errors-as-data (the tools) wrap this."""
        url = origin(self.base) + path
        data = None
        ctype = None
        if payload is not None:
            data = json.dumps(payload).encode()
            ctype = "application/json"
        req = urllib.request.Request(url, data=data, headers=self._headers(ctype),
                                     method=method)
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            raw = resp.read().decode(errors="replace")
        return json.loads(raw) if raw.strip() else {}

    def api_multipart(self, path: str, filepath: str, fields: dict | None = None,
                      timeout: int | None = None):
        """POST one file as multipart/form-data (no requests dependency).
        Lifted from hugpyTab/src/generate.py::post_multipart — the shape the
        /api/ml/* media amenities accept (`file` + form fields)."""
        import base64 as _b64
        import os as _os
        boundary = "----hugpyagent%s" % _b64.b16encode(_os.urandom(8)).decode()
        with open(filepath, "rb") as fh:
            content = fh.read()
        lines = []
        for k, v in (fields or {}).items():
            lines.append(b"--" + boundary.encode())
            lines.append(('Content-Disposition: form-data; name="%s"' % k).encode())
            lines.append(b"")
            lines.append(str(v).encode())
        lines.append(b"--" + boundary.encode())
        lines.append(('Content-Disposition: form-data; name="file"; filename="%s"'
                      % _os.path.basename(filepath)).encode())
        lines.append(b"Content-Type: application/octet-stream")
        lines.append(b"")
        lines.append(content)
        lines.append(b"--" + boundary.encode() + b"--")
        lines.append(b"")
        body = b"\r\n".join(lines)
        headers = self._headers("multipart/form-data; boundary=%s" % boundary)
        req = urllib.request.Request(origin(self.base) + path, data=body,
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            raw = resp.read().decode(errors="replace")
        return json.loads(raw) if raw.strip() else {}

    def api_bytes(self, path: str, timeout: int | None = None,
                  max_bytes: int = 64 * 1024 * 1024) -> bytes:
        """GET raw bytes from an absolute path on the base's origin (media
        fetches: /api/video/media?handle=...). Capped at 64MB — an artifact
        bigger than that has no business in an agent workspace."""
        req = urllib.request.Request(origin(self.base) + path,
                                     headers=self._headers())
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            return resp.read(max_bytes)

    def seated_model_keys(self,
                          timeout: int = WORKERS_PROBE_TIMEOUT) -> list[str] | None:
        """model_keys with a live 'slot' allocation on any fleet worker, via
        GET {base}/llm/workers. Returns None on ANY failure (network, non-JSON,
        unexpected shape) — the second-in-line selection treats that as
        'could not tell' and silently keeps the primary; this probe must
        never be able to break a run."""
        url = normalize_base(self.base) + "/llm/workers"
        try:
            req = urllib.request.Request(url, headers=self._headers())
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode(errors="replace"))
        except Exception:
            return None
        return slot_model_keys(data)

    def warm_models(self,
                    timeout: int = WORKERS_PROBE_TIMEOUT) -> list[str] | None:
        """model_keys WARM on any fleet worker (k96 ladder source), via GET
        {base}/llm/workers — slot allocations not reporting healthy=False plus
        in-process 'ram' residents (see warm_model_keys). Returns None on ANY
        failure, which the ladder selection treats as 'could not tell' and
        answers ladder[0] — this probe must never be able to break a run."""
        url = normalize_base(self.base) + "/llm/workers"
        try:
            req = urllib.request.Request(url, headers=self._headers())
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode(errors="replace"))
        except Exception:
            return None
        return warm_model_keys(data)

    # ── route resolution ─────────────────────────────────────────────────
    def resolve(self) -> tuple[str, str]:
        """(chat_url, models_url), probing candidates once and caching.

        Probe = the models URL answers with an OpenAI-style list. If nothing
        answers we still return the first candidate rather than fail here:
        the eventual chat call will surface a *specific* error the loop can
        report, which beats an opaque startup failure.
        """
        if self._routes:
            return self._routes
        cands = candidate_routes(self.base)
        for chat_url, models_url in cands:
            try:
                req = urllib.request.Request(models_url, headers=self._headers())
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode(errors="replace"))
                if isinstance(data, dict) and (data.get("data") or data.get("models")):
                    self._routes = (chat_url, models_url)
                    return self._routes
            except Exception:
                continue
        self._routes = cands[0]
        return self._routes

    # ── models ───────────────────────────────────────────────────────────
    def models(self, refresh: bool = False) -> list[dict]:
        """Model entries from /v1/models (id, and context_length when the
        server reports it). Cached — the loop asks repeatedly for budgets."""
        if self._models_cache is not None and not refresh:
            return self._models_cache
        _, models_url = self.resolve()
        req = urllib.request.Request(models_url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode(errors="replace"))
        entries = data.get("data") or data.get("models") or []
        self._models_cache = [e for e in entries if isinstance(e, dict)]
        return self._models_cache

    def context_length(self, model: str | None = None,
                       fallback: int = DEFAULT_CTX_FALLBACK) -> int:
        """Measured context window for a model id, from /v1/models metadata.
        Falls back to a conservative default — over-budgeting causes silent
        truncation at the server, which is worse than early compaction."""
        mid = model or self.model
        try:
            for e in self.models():
                if (e.get("id") or e.get("name")) == mid:
                    for k in ("context_length", "ctx_size", "ctx", "n_ctx",
                              "max_context_length"):
                        v = e.get(k)
                        if isinstance(v, (int, float)) and v > 0:
                            return int(v)
                    meta = e.get("meta") or {}
                    if isinstance(meta, dict):
                        for k in ("context_length", "n_ctx"):
                            v = meta.get(k)
                            if isinstance(v, (int, float)) and v > 0:
                                return int(v)
        except Exception:
            pass
        return fallback

    # ── chat ─────────────────────────────────────────────────────────────
    def build_payload(self, messages, model=None, temperature=0.2,
                      max_tokens=1024, stream=True, tools=None) -> dict:
        """Assemble the chat payload. Split out (and pure) so tests can assert
        the platform-gotcha invariants — max_chunks:1 on EVERY request —
        without any network."""
        payload = {
            "model": model or self.model or "default",
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": bool(stream),
            # Kills the continuation-prompt leak class (design §2.3); the
            # per-request knob exists since hugpy 0.1.171.
            "max_chunks": 1,
            # k96 no-evict guarantee: a brain call must NEVER cost the fleet a
            # resident model. Central's dispatch honors this by running any
            # cold load POLITELY (free headroom only) and failing fast with a
            # capacity-class refusal otherwise — which the brain ladder walks.
            # A warm brain serves exactly as before; an older central simply
            # drops the unknown key (additive and safe against any backend).
            "no_makeroom": True,
        }
        if tools:
            # Native passthrough tier — ignored by /v1 today (design §2.1),
            # kept so the tier activates the day the seam supports it.
            payload["tools"] = tools
        if self.no_think:
            # HARD no-think (hugpy 0.1.229+): the ` /no_think` suffix on the
            # messages is the SOFT directive a model may ignore (Wasserstein
            # does). This asks central to pre-close the think block at the chat
            # template (enable_thinking=false), which central version-gates and
            # forwards to the worker's llama-server. An older central/worker
            # simply drops the unknown key and the soft directive still rides
            # the messages — so this is additive and safe against any backend.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return payload

    def chat(self, messages, model=None, temperature=0.2, max_tokens=1024,
             stream=True, tools=None, on_delta=None, retries=2,
             timeout=None) -> ChatResult:
        """One chat completion; SSE streaming with keepalive handling and a
        transparent plain-JSON fallback (seed: servicesTab.llm_stream).

        Retries with backoff cover the CONNECT phase only (URLError/timeout
        before any byte arrives, and 502/503/504). A mid-stream failure is not
        retried: the model already consumed tokens and a blind retry could
        duplicate side-effect-inducing text — we return the partial text plus
        the error and let the loop decide.
        """
        chat_url, _ = self.resolve()
        # Suffix ` /no_think` on a wire-only copy — never the caller's history.
        wire = apply_no_think(messages) if self.no_think else messages
        payload = self.build_payload(wire, model, temperature, max_tokens,
                                     stream, tools)
        body = json.dumps(payload).encode()
        last_err = ""
        timeout = timeout or self.timeout
        for attempt in range(retries + 1):
            req = urllib.request.Request(chat_url, data=body,
                                         headers=self._headers("application/json"))
            try:
                resp = urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode(errors="replace")[:500]
                except Exception:
                    pass
                last_err = "HTTP %s from %s: %s" % (exc.code, chat_url, detail)
                if exc.code in (502, 503, 504) and attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                return ChatResult(ok=False, error=last_err)
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
                last_err = "connect to %s failed: %s" % (chat_url, exc)
                if attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                return ChatResult(ok=False, error=last_err)
            try:
                return self._read_response(resp, on_delta)
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
        return ChatResult(ok=False, error=last_err or "exhausted retries")

    def _read_response(self, resp, on_delta) -> ChatResult:
        """Parse either an SSE stream or a plain JSON completion body."""
        parts: list[str] = []
        request_id = None
        native_calls: list = []
        try:
            # Skip blank lines and SSE comments (the hub emits ": keepalive")
            # before deciding stream vs plain JSON (seed: llm_analyze).
            first = resp.readline().decode(errors="replace")
            while first and (not first.strip() or first.lstrip().startswith(":")):
                first = resp.readline().decode(errors="replace")
            if not first.lstrip().startswith("data:"):
                data = json.loads(first + resp.read().decode(errors="replace"))
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                text = msg.get("content") or ""
                calls = msg.get("tool_calls") or []
                return ChatResult(ok=True, text=text, native_tool_calls=calls,
                                  request_id=data.get("id"),
                                  est_tokens=estimate_tokens(text))
            line = first
            while line:
                s = line.strip()
                if s.startswith("data:"):
                    chunk = s[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        data = json.loads(chunk)
                        if request_id is None:
                            request_id = data.get("id")
                        delta = (data.get("choices") or [{}])[0].get("delta") or {}
                    except (json.JSONDecodeError, KeyError, IndexError):
                        delta = {}
                    if delta.get("tool_calls"):
                        native_calls.extend(delta["tool_calls"])
                    piece = delta.get("content") or ""
                    if piece:
                        parts.append(piece)
                        if on_delta:
                            on_delta(piece)
                line = resp.readline().decode(errors="replace")
        except (socket.timeout, TimeoutError, OSError, urllib.error.URLError) as exc:
            text = "".join(parts)
            return ChatResult(ok=False, text=text, request_id=request_id,
                              error="stream interrupted: %s" % exc,
                              est_tokens=estimate_tokens(text))
        text = "".join(parts)
        return ChatResult(ok=True, text=text, native_tool_calls=native_calls,
                          request_id=request_id, est_tokens=estimate_tokens(text))

    def cancel(self, request_id: str) -> dict:
        """Best-effort cancel of an in-flight chat (route from the live survey:
        POST /api/llm/chat/cancel/<request_id>). Errors as data."""
        if not request_id:
            return {"ok": False, "error": "no request_id"}
        try:
            return self.api_json("/api/llm/chat/cancel/%s"
                                 % urllib.parse.quote(str(request_id), safe=""),
                                 method="POST", payload={}, timeout=15)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
