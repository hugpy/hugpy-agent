"""Provider-neutral Serve clients for the terminal harness.

`connect(base, kind)` selects an API-protocol adapter; provider identity and
model choice are data returned by the Serve API, not client kinds.
"""
from __future__ import annotations

from .base import (Approval, Client, Event, EventPage, ProviderOption, QueueItem,  # noqa: F401
                   QueueView, Receipt, Roster, ServeError, Session, Usage, KINDS)


def connect(base, kind, token=None, timeout=5, *, workspace=".", model="",
            policy="ask", state_dir=None, optional_tools=False):
    if kind in ("abstract-serve", "abstract-claude"):
        from .abstract_serve import AbstractServeClient
        return AbstractServeClient(base, token, timeout)
    if kind == "hugpy":
        from .hugpy_serve import HugpyServeClient
        return HugpyServeClient(base, token, timeout)
    if kind in ("local", "wrapper"):
        # In-process harness WrapperClient — no serve: the agent talks to a
        # wrapper/fleet endpoint directly. state_dir lands its journal in the
        # agent's own state dir (.hugpy_agent), not the harness default.
        from .wrapper import WrapperClient
        return WrapperClient(base, workspace, model, token or "", policy,
                             optional_tools=optional_tools, state_dir=state_dir)
    raise ServeError("unknown serve kind: %r" % (kind,))
