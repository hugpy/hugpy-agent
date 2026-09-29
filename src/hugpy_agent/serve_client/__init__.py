"""Serve clients for the terminal harness: `connect(base, kind)` picks the
adapter; the dataclasses in base.py are the whole contract the TUI sees."""
from __future__ import annotations

from .base import (Approval, Client, Event, EventPage, ProviderOption, QueueItem,  # noqa: F401
                   QueueView, Receipt, Roster, ServeError, Session, Usage, KINDS)


def connect(base, kind, token=None, timeout=5):
    if kind == "abstract-claude":
        from .abstract_claude import AbstractClaudeClient
        return AbstractClaudeClient(base, token, timeout)
    if kind == "hugpy":
        from .hugpy_serve import HugpyServeClient
        return HugpyServeClient(base, token, timeout)
    raise ServeError("unknown serve kind: %r" % (kind,))
