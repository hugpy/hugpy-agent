"""Shared test doubles."""
from __future__ import annotations

import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)

from hugpy_agent.gateway import ChatResult, estimate_tokens


class FakeGateway:
    """Scripted model: each chat() pops the next canned reply. Fails the test
    loudly (empty error result) if the script runs dry, so an accidental
    extra round-trip is visible instead of hanging."""

    def __init__(self, replies=None, ctx=8192):
        self.replies = list(replies or [])
        self.ctx = ctx
        self.calls = []          # (messages, kwargs) per chat() for assertions
        self.base = "fake://"
        self.model = "fake-model"

    def chat(self, messages, **kw):
        self.calls.append((messages, kw))
        if not self.replies:
            return ChatResult(ok=False, error="FakeGateway script exhausted")
        item = self.replies.pop(0)
        if isinstance(item, ChatResult):
            return item
        return ChatResult(ok=True, text=item, est_tokens=estimate_tokens(item))

    def context_length(self, model=None, fallback=8192):
        return self.ctx

    def models(self, refresh=False):
        return [{"id": "fake-model", "context_length": self.ctx}]

    def resolve(self):
        return ("fake:///v1/chat/completions", "fake:///v1/models")


def tc(name, **arguments):
    """Render a prompted-tier tool call block."""
    import json
    return "<tool_call>\n%s\n</tool_call>" % json.dumps(
        {"name": name, "arguments": arguments})


FIXTURES = __import__("os").path.join(__import__("os").path.dirname(__file__), "fixtures", "serve_audit")


def fixture(name):
    """Load a recorded serve reply (h27 audit, GET-only) by file stem."""
    import json
    import os
    with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


class FakeServe:
    """In-thread stdlib HTTP server replaying fixtures for the serve_client tests.

    routes: {(method, path): doc | callable(query, body) -> doc | (status, doc)
             | ("sse", [frames])}. Unknown routes answer 404 {"error"}. Every
    GET/POST is recorded in `calls` as (method, path, query, body) so tests can
    assert the exact route/query/body a Client method produced. `prefix` lets
    a test mount everything under AC_UI_BASE ("/ac") to prove that direct
    loopback calls with the prefix are tolerated by the client."""

    def __init__(self, routes=None, prefix=""):
        import threading
        self.routes = dict(routes or {})
        self.prefix = prefix
        self.calls = []
        self._server = None
        self._thread = None
        self._lock = threading.Lock()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def start(self):
        import http.server
        import json
        import threading
        from urllib.parse import parse_qs, urlparse
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self, method):
                parsed = urlparse(self.path)
                path = parsed.path
                if fake.prefix and path.startswith(fake.prefix):
                    path = path[len(fake.prefix):] or "/"
                query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                body = None
                if method == "POST":
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}")
                with fake._lock:
                    fake.calls.append((method, path, query, body))
                route = fake.routes.get((method, path))
                if route is None:
                    return self._reply(404, {"error": "not found: %s %s" % (method, path)})
                if isinstance(route, tuple) and route and route[0] == "sse":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for frame in route[1]:
                        self.wfile.write(("data: %s\n\n" % json.dumps(frame)).encode())
                    self.wfile.flush()
                    return
                if callable(route):
                    route = route(query, body)
                status, doc = route if isinstance(route, tuple) else (200, route)
                self._reply(status, doc)

            def _reply(self, status, doc):
                raw = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return "http://127.0.0.1:%d%s" % (self._server.server_address[1], self.prefix)

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def posts(self, path=None):
        return [(p, b) for m, p, q, b in self.calls if m == "POST" and (path is None or p == path)]

    def gets(self, path=None):
        return [(p, q) for m, p, q, b in self.calls if m == "GET" and (path is None or p == path)]
