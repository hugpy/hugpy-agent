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


def estimate_tokens(text: str) -> int:
    """Client-side token estimate (~4 chars/token). Isolated here because the
    /v1 seam returns null usage today (design §2.2); when real usage lands,
    this is the only function to replace."""
    return max(1, len(text or "") // 4)


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
        }
        if tools:
            # Native passthrough tier — ignored by /v1 today (design §2.1),
            # kept so the tier activates the day the seam supports it.
            payload["tools"] = tools
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
