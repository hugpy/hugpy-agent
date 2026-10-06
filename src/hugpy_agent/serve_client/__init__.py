"""Provider-neutral Serve clients for the terminal harness.

`connect(base, kind)` selects an API-protocol adapter; provider identity and
model choice are data returned by the Serve API, not client kinds.
"""
from __future__ import annotations

from .base import (Approval, Client, Event, EventPage, ProviderOption, QueueItem,  # noqa: F401
                   QueueView, Receipt, Roster, ServeError, Session, Usage, KINDS)


def connect(base, kind, token=None, timeout=5):
    if kind in ("abstract-serve", "abstract-claude"):
        from .abstract_serve import AbstractServeClient
        return AbstractServeClient(base, token, timeout)
    if kind == "hugpy":
        from .hugpy_serve import HugpyServeClient
        return HugpyServeClient(base, token, timeout)
    raise ServeError("unknown serve kind: %r" % (kind,))
