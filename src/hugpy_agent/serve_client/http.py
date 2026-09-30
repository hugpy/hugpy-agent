"""One urllib wrapper for both serves (lifted from the retired tui.ServeClient).

Bearer token comes from the argument or $HUGPY_SERVE_TOKEN; loopback callers on
the live keeper serve need none (audit B.8). HTTP error bodies `{error}` map to
ServeError(detail, code); transport failures to ServeError("serve unavailable…").
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .base import ServeError


class Http:
    def __init__(self, base, token=None, timeout=5):
        self.base = base.rstrip("/")
        self.token = token or os.environ.get("HUGPY_SERVE_TOKEN", "")
        self.timeout = timeout

    def url(self, path, query=None):
        path = "/" + path.lstrip("/")
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            if clean:
                path += ("&" if "?" in path else "?") + urllib.parse.urlencode(clean)
        return self.base + path

    def _request(self, path, method, body, query):
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        return urllib.request.Request(self.url(path, query), data=data,
                                      headers=headers, method=method)

    def open(self, path, method="GET", body=None, query=None, timeout=None):
        """Return the raw response (caller closes) — used for SSE streams."""
        req = self._request(path, method, body, query)
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            raise ServeError(_detail(exc), exc.code) from exc
        except (OSError, ValueError) as exc:
            raise ServeError("serve unavailable: " + type(exc).__name__) from exc

    def request(self, path, method="GET", body=None, query=None):
        with self.open(path, method, body, query) as response:
            raw = response.read() or b"{}"
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ServeError("serve returned non-JSON for " + path) from exc

    def get(self, path, **query):
        return self.request(path, "GET", None, query)

    def post(self, path, body):
        return self.request(path, "POST", body if body is not None else {})


def _detail(exc):
    try:
        doc = json.loads(exc.read() or b"{}")
        if isinstance(doc, dict) and doc.get("error"):
            return doc["error"]
    except (ValueError, OSError):
        pass
    return "HTTP %s" % exc.code
