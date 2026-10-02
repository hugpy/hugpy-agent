"""Stdlib stub of an abstract-claude serve, just enough for `hugpy-agent tui`.

Run: python3 stub_serve.py [port]   (prints "PORT <n>" then serves forever)

Shapes mirror serve_client/abstract_claude.py + tui/discovery.py:
  GET  /api/state            -> Claude Code `version` + busy/root/oauth (kind detect)
  GET  /api/session/roster   -> {roles, provider_options, defaults}
  GET  /api/console/sessions -> {sessions, cwd}
  GET  /api/session/events   -> {events, busy, cursor, source} (native transcript)
  GET  /api/console/models   -> {models}
  GET  /api/usage/session    -> {totals}
  GET  /api/session/rollover -> {archived}
  POST /api/session/chat     -> SSE stream (text frames + done)
  POST /api/session/rollover -> {ok, note}
  GET/POST /api/session/queue, /api/console/queue, /api/session/interrupt ...
"""
from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Two native-uuid role sessions (exercise /api/session/* + SSE) and one cs- row.
KEEPER = "11111111-aaaa-bbbb-cccc-000000000001"
CHAT = "22222222-aaaa-bbbb-cccc-000000000002"
WORKER = "cs-deadbeef01"

ROLES = [
    {"role": "keeper", "label": "Keeper", "live_session_id": KEEPER,
     "backend": "claude", "model": "claude-opus-4", "pending_model": None},
    {"role": "chat", "label": "Chat", "live_session_id": CHAT,
     "backend": "claude", "model": "claude-sonnet", "pending_model": None},
]
CONSOLE_SESSIONS = [
    {"id": KEEPER, "label": "Keeper", "backend": "claude", "model": "claude-opus-4",
     "busy": False, "paused": False, "native_id": KEEPER, "updated": 1000.0},
    {"id": CHAT, "label": "Chat", "backend": "claude", "model": "claude-sonnet",
     "busy": False, "paused": False, "native_id": CHAT, "updated": 900.0},
    {"id": WORKER, "label": "Worker", "backend": "hugpy", "model": "qwen",
     "busy": False, "paused": False, "native_id": "", "updated": 800.0},
]
PROVIDER_OPTIONS = [
    {"backend": "claude", "model": "claude-opus-4", "label": "Opus 4"},
    {"backend": "claude", "model": "claude-sonnet", "label": "Sonnet"},
    {"backend": "hugpy", "model": "qwen", "label": "Qwen (fleet)"},
]
MODELS = PROVIDER_OPTIONS

# Native transcript events (normalize_native_event: kind/role + body/excerpt + msg_id).
EVENTS = [
    {"kind": "user", "body": "Hello first prompt", "msg_id": "m1", "ts": "2026-10-01T00:00:00Z", "seq": 1},
    {"kind": "assistant", "body": "First assistant reply line one.", "msg_id": "m2",
     "ts": "2026-10-01T00:00:01Z", "seq": 2},
    {"kind": "tool_use", "tool": "Read", "excerpt": "/etc/hosts", "body": '{"file_path": "/etc/hosts"}',
     "tool_id": "t1", "msg_id": "m3", "ts": "2026-10-01T00:00:02Z", "seq": 3},
    {"kind": "tool_result", "tool": "t1", "body": "127.0.0.1 localhost", "is_error": False,
     "msg_id": "m4", "ts": "2026-10-01T00:00:03Z", "seq": 4},
    {"kind": "assistant", "body": "Second assistant reply, selectable block.", "msg_id": "m5",
     "ts": "2026-10-01T00:00:04Z", "seq": 5},
    {"kind": "user", "body": "Another user turn to scroll past.", "msg_id": "m6",
     "ts": "2026-10-01T00:00:05Z", "seq": 6},
    {"kind": "assistant", "body": "Reply six.", "msg_id": "m7", "ts": "2026-10-01T00:00:06Z", "seq": 7},
    {"kind": "user", "body": "Yet another prompt.", "msg_id": "m8", "ts": "2026-10-01T00:00:07Z", "seq": 8},
    {"kind": "assistant", "body": "Reply eight is here.", "msg_id": "m9",
     "ts": "2026-10-01T00:00:08Z", "seq": 9},
]
# Filler so the transcript is taller than a 45-row pane (scroll tests need it).
for _i in range(30):
    _s = 10 + _i
    EVENTS.append({"kind": "user", "body": "Filler user line %d marks a scroll position." % _i,
                   "msg_id": "f%du" % _i, "ts": "2026-10-01T00:10:%02dZ" % _i, "seq": _s})
EVENTS.append({"kind": "assistant", "body": "TAIL reply at the very end.", "msg_id": "mtail",
               "ts": "2026-10-01T00:20:00Z", "seq": 90})


# Replies the serve "persists" after a chat, so the transcript poll keeps showing
# them once the SSE preview is dropped (mirrors a real serve writing the turn).
EXTRA = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/state":
            return self._send({"version": "1.0.0", "busy": False, "root": "/tmp", "oauth": True})
        if path == "/api/session/roster":
            return self._send({"roles": ROLES, "provider_options": PROVIDER_OPTIONS, "defaults": {}})
        if path == "/api/console/sessions":
            return self._send({"sessions": CONSOLE_SESSIONS, "cwd": "/tmp/project"})
        if path == "/api/session/events":
            # busy=True so the status bar shows a per-second "BUSY Ns" counter —
            # the freeze test asserts it keeps advancing (main loop still drawing)
            # after an Esc press.
            return self._send({"events": EVENTS + EXTRA, "busy": True,
                               "cursor": "%d:%d" % (9 + len(EXTRA), 9 + len(EXTRA)), "source": "transcript"})
        if path == "/api/console/events":
            return self._send({"events": [], "busy": False, "cursor": "0", "queue": None})
        if path == "/api/console/models":
            return self._send({"models": MODELS})
        if path == "/api/usage/session":
            return self._send({"totals": {"claude": {"in": 1234, "out": 567, "usd": 0.0123}}})
        if path == "/api/session/rollover":
            return self._send({"archived": {}})
        if path in ("/api/session/queue", "/api/console/queue"):
            return self._send({"auto": True, "busy": False, "paused": False, "items": []})
        return self._send({})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        if path == "/api/session/chat":
            return self._sse(body)
        if path == "/api/session/rollover":
            return self._send({"ok": True, "note": "queued"})
        if path in ("/api/session/queue", "/api/console/queue"):
            return self._send({"auto": True, "busy": False, "paused": False, "items": []})
        if path in ("/api/session/interrupt", "/api/console/interrupt"):
            return self._send({"ok": True})
        if path == "/api/console/approval":
            return self._send({"ok": True})
        if path == "/api/session/roster":
            return self._send({"roles": ROLES})
        return self._send({"ok": True})

    def _sse(self, body):
        seq = 10 + len(EXTRA)
        EXTRA.append({"kind": "user", "body": body.get("prompt", ""), "msg_id": "u%d" % seq,
                      "ts": "2026-10-01T01:00:00Z", "seq": seq})
        EXTRA.append({"kind": "assistant", "body": "stub stream: got your prompt.",
                      "msg_id": "a%d" % (seq + 1), "ts": "2026-10-01T01:00:01Z", "seq": seq + 1})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        frames = [
            {"type": "text", "text": "stub stream: ", "seq": 1},
            {"type": "text", "text": "got your prompt.", "seq": 2},
            {"type": "done", "rc": 0, "result": "stub done", "message_ids": [], "seq": 3},
        ]
        for fr in frames:
            self.wfile.write(b"data: " + json.dumps(fr).encode() + b"\n\n")
            self.wfile.flush()
            time.sleep(0.05)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("PORT %d" % srv.server_address[1], flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
