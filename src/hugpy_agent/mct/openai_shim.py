"""C over HTTP — MCT behind an OpenAI-compatible endpoint.

Design ref: §5 (C, the operator conversation), §20.8 (the terminal).

C is a prompt, a response display, and a live relay of the A↔B exchange.
Nothing about A or B constrains how C is rendered — which means C's frontend is
swappable, and a TUI worth using already exists (OpenCode). Rather than growing
a second terminal inside this package, expose C in the one dialect every such
frontend already speaks: ``POST /v1/chat/completions``.

One completion == one MCT turn. The frontend sends its whole message history;
we deliberately submit only the LAST user message, because B already owns
conversation state (ledger, catalog, derived memory). Replaying the frontend's
history would double the context B has curated — exactly the cost this system
exists to avoid.

**The relay is the point.** With ``stream: true`` the A↔B exchange is emitted as
it happens, before the answer: every file B scans, peeks, reads, writes, and
every pull A issues, each carrying an absolute path so the frontend can linkify
it. The operator watches the work rather than a spinner. ``MCT_STREAM_RELAY=0``
suppresses it for a frontend that wants only the answer.

A and B are untouched by any of this. The shim holds one session and calls the
same ``submit_via_claude`` the readline REPL calls; it reads ``access.jsonl``
for the relay exactly as :class:`~.repl.LiveFeed` does, because that file is the
only vantage point seeing both sides (A's MCP server is a separate process).

Stdlib only — the zero-runtime-deps promise is not negotiable, and "serve an
HTTP endpoint" must not smuggle in a web framework to do it.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL_ID = "mct"
_RELAY = os.environ.get("MCT_STREAM_RELAY", "1") not in ("0", "false", "no")


def _chunk(cid: str, created: int, content: str | None,
           finish: str | None = None) -> str:
    delta = {"content": content} if content is not None else {}
    body = {"id": cid, "object": "chat.completion.chunk", "created": created,
            "model": MODEL_ID,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(body)}\n\n"


def _relay_line(row: dict) -> str:
    """One A↔B record as a line the frontend can render and linkify.

    Absolute paths are emitted verbatim: this stream is C's, shown to the
    operator, and a path a TUI can turn into a link is worth more than a tidy
    relative one. A never sees this — it is assembled from B's own log."""
    actor = row.get("actor", "?")
    verb = row.get("verb", "")
    target = row.get("path") or row.get("target", "")
    bits = [f"{actor:<4}", f"{verb:<10}", str(target)]
    if row.get("bytes"):
        bits.append(f"({row['bytes']}B)")
    if row.get("detail"):
        bits.append(f"— {row['detail']}")
    return "  " + " ".join(bits)


class _Tail:
    """Follow access.jsonl from a mark, yielding rendered relay lines."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.offset = self.path.stat().st_size
        except OSError:
            self.offset = 0

    def drain(self) -> list[str]:
        out: list[str] = []
        try:
            with open(self.path, encoding="utf-8") as fh:
                fh.seek(self.offset)
                for line in fh:
                    if not line.endswith("\n"):   # partial write: next poll
                        break
                    self.offset += len(line.encode("utf-8"))
                    try:
                        out.append(_relay_line(json.loads(line)))
                    except ValueError:
                        continue
        except OSError:
            pass
        return out


class MctChatService:
    """One MCT session, driven by chat completions. Turns are serialized:
    B's session is not re-entrant, and two operators talking over each other
    would interleave into one ledger."""

    def __init__(self, workspace: str, model: str = "sonnet",
                 use_model: bool = True):
        from .session import BrokerConfig, BrokerServer
        self.model = model
        self.server = BrokerServer(workspace, sink=lambda *_: None,
                                   config=BrokerConfig(use_model=use_model))
        self.session = self.server.session(self.server.open_session("openai-shim"))
        self._lock = threading.Lock()

    def close(self) -> None:
        self.server.close()

    @staticmethod
    def _last_user(messages: list) -> str:
        for m in reversed(messages or []):
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):   # content parts
                    return "".join(p.get("text", "") for p in c
                                   if isinstance(p, dict))
        return ""

    def turn(self, prompt: str, model: str | None = None, on_relay=None):
        """Run one turn. ``on_relay`` receives relay lines as they land."""
        with self._lock:
            tail = _Tail(self.server.access.path)
            stop = threading.Event()
            result = {}

            def pump():
                while not stop.is_set():
                    for ln in tail.drain():
                        on_relay(ln)
                    stop.wait(0.1)
                for ln in tail.drain():       # never drop the tail
                    on_relay(ln)

            t = threading.Thread(target=pump, daemon=True) if on_relay else None
            if t:
                t.start()
            try:
                result = self.session.submit_via_claude(prompt,
                                                        model=model or self.model)
            finally:
                stop.set()
                if t:
                    t.join()
            return result


def _handler_for(service: MctChatService):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):            # quiet: C owns the terminal
            pass

        # --- helpers ---------------------------------------------------
        def _json(self, code: int, payload: dict):
            raw = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _sse_open(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

        def _sse(self, text: str):
            try:
                self.wfile.write(text.encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                raise

        # --- routes ----------------------------------------------------
        def do_GET(self):
            if self.path.rstrip("/").endswith("/models"):
                return self._json(200, {"object": "list", "data": [
                    {"id": MODEL_ID, "object": "model", "created": int(time.time()),
                     "owned_by": "hugpy-mct"}]})
            return self._json(404, {"error": {"message": "not found"}})

        def do_POST(self):
            if not self.path.rstrip("/").endswith("/chat/completions"):
                return self._json(404, {"error": {"message": "not found"}})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, OSError) as exc:
                return self._json(400, {"error": {"message": f"bad request: {exc}"}})

            prompt = service._last_user(req.get("messages"))
            if not prompt.strip():
                return self._json(400, {"error": {"message": "no user message"}})
            cid = "chatcmpl-" + uuid.uuid4().hex[:24]
            created = int(time.time())
            stream = bool(req.get("stream"))

            if not stream:
                r = service.turn(prompt)
                body = self._body_of(r)
                return self._json(200, {
                    "id": cid, "object": "chat.completion", "created": created,
                    "model": MODEL_ID,
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": body}}],
                    "usage": self._usage(r)})

            self._sse_open()
            broken = threading.Event()

            def relay(line: str):
                if not _RELAY or broken.is_set():
                    return
                try:
                    self._sse(_chunk(cid, created, line + "\n"))
                except Exception:
                    broken.set()   # frontend hung up: stop relaying, finish the turn

            try:
                if _RELAY:
                    self._sse(_chunk(cid, created, "```text\n"))
                r = service.turn(prompt, on_relay=relay)
                if _RELAY and not broken.is_set():
                    self._sse(_chunk(cid, created, "```\n\n"))
                if not broken.is_set():
                    self._sse(_chunk(cid, created, self._body_of(r)))
                    self._sse(_chunk(cid, created, None, "stop"))
                    self._sse("data: [DONE]\n\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

        # --- shaping ---------------------------------------------------
        @staticmethod
        def _body_of(r) -> str:
            if getattr(r, "state", "") == "Committed" and getattr(r, "body", None):
                return r.body
            # The illusion breaks explicitly (§5.2): B never answers for A.
            return (f"_[A did not answer — B does not answer in its place. "
                    f"reason: {getattr(r, 'error', None) or getattr(r, 'state', '?')}]_")

        @staticmethod
        def _usage(r) -> dict:
            t = getattr(r, "tokens", None) or {}
            return {"prompt_tokens": int(t.get("input") or 0),
                    "completion_tokens": int(t.get("output") or 0),
                    "total_tokens": int(t.get("input") or 0) + int(t.get("output") or 0)}

    return Handler


def serve(workspace: str, host: str = "127.0.0.1", port: int = 8770,
          model: str = "sonnet", use_model: bool = True):
    """Start the shim. Returns ``(httpd, service)``; caller runs/loops it.

    Binds loopback by default: this endpoint drives a confined Claude and can
    apply changes through B's act channel, so it is not something to expose on
    an interface by accident."""
    service = MctChatService(workspace, model=model, use_model=use_model)
    httpd = ThreadingHTTPServer((host, port), _handler_for(service))
    httpd.daemon_threads = True
    return httpd, service
