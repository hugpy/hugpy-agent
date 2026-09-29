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
