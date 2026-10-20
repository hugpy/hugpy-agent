"""hugpy_agent.serve_client.wrapper — the harness WrapperClient with the agent's
loop and gateway swapped in.

The session store, named sessions, per-session settings, event bus and the
HTTP face (`hugpy_harness.serve`) are the harness's: one source of truth. What
the agent adds rides the harness's own hooks — its AgentLoop (RAG, Discord
comms, the fleet toolset, session-signal turns) and its Gateway (X-Hugpy-*
identity + the renewed lease on every fleet call). Nothing else differs, so a
session created through `hugpy-agent serve` is the same journal row
`hugpy-harness` shows.
"""
from __future__ import annotations

from hugpy_harness.serve_client.wrapper import WrapperClient as _HarnessWrapperClient, _model_size  # noqa: F401


class WrapperClient(_HarnessWrapperClient):
    @property
    def loop_class(self):
        from ..runtime.loop import AgentLoop
        return AgentLoop

    @property
    def gateway_class(self):
        from ..gateway import Gateway
        return Gateway
