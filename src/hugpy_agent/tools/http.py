"""http_fetch — GET a URL with size cap and timeout.

GET-only on purpose: fetching is an observation; anything that mutates a
remote system should be an explicit, risk-classed tool, not a generic verb.
"""
from __future__ import annotations

import json
import urllib.request

from . import RISK_NETWORK, ToolSpec

FETCH_CAP = 64 * 1024
TIMEOUT = 30


def spec() -> ToolSpec:
    def http_fetch(url: str) -> str:
        if not url.startswith(("http://", "https://")):
            return json.dumps({"error": "only http(s) URLs are allowed, got %r" % url})
        req = urllib.request.Request(url, headers={
            "User-Agent": "hugpy-agent/0.1", "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = resp.read(FETCH_CAP + 1)
            status = resp.status
            ctype = resp.headers.get("Content-Type", "")
        truncated = len(data) > FETCH_CAP
        text = data[:FETCH_CAP].decode("utf-8", errors="replace")
        out = {"status": status, "content_type": ctype, "content": text}
        if truncated:
            out["note"] = "body truncated at %d bytes" % FETCH_CAP
        return json.dumps(out)

    return ToolSpec(
        name="http_fetch",
        description=("HTTP GET a URL; returns status, content_type and up to "
                     "64KB of the body as text."),
        parameters={"type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"]},
        handler=http_fetch, risk_class=RISK_NETWORK)
