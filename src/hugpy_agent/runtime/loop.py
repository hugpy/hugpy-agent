"""hugpy_agent agent loop — the hugpy_terminal agent loop core with the fleet
extras wired through the harness's hooks:

  * RAG memory      — rag_factory builds a RagIndex when cfg.rag_enabled;
  * operator channel — the Discord Comms (comms_factory);
  * fleet toolset    — the agent's catalog + http/fleet/lean/toolserver/
    remember/recall/spawn providers;
  * session signals  — turn_ctx spans each drive as one turn to central;
  * agent gateway    — session_signals ON by default.

The chat-mode fixes (empty/failed-reply abort with no re-injection, replay
dropping legacy injections, <tool_response> gating + markup stripping, the
per-user-message step budget, answer-less final_answer repair) come free from
the harness core. Everything else (escalation labels, resume, compaction,
loop-guard, brain ladder) is inherited unchanged.
"""
from __future__ import annotations

import contextlib

from hugpy_terminal.loop import (  # noqa: F401  (re-exported surface)
    APPROVE,
    APPROVE_ALL_FMT,
    DENY_LABEL,
    AgentLoop as _HarnessLoop,
    _DeltaGate,
    default_journal_path,
)

from ..comms import Comms
from ..gateway import Gateway
from ..rag import RagIndex, default_vectors_path, fleet_embedder
from ..tools import AGENT_CATALOG_PROVIDER, AGENT_TOOL_PROVIDERS
from . import session_signals


def _rag_factory(cfg, gateway):
    """Embed-RAG memory (P2.6): a semantic INDEX over the markdown fact store,
    vectors in <workspace>/.hugpy_agent/memory_vectors.db, embeddings from the
    fleet's /api/ml/embed. cfg.rag_enabled=False -> None (no recall tool, no
    indexing, no run-start recall)."""
    if not getattr(cfg, "rag_enabled", False):
        return None
    return RagIndex(default_vectors_path(cfg.workspace),
                    fleet_embedder(gateway, cfg.workspace))


class AgentLoop(_HarnessLoop):
    """The harness agent loop + hugpy fleet extras, injected through hooks."""

    def __init__(self, cfg, gateway=None, registry=None, journal=None,
                 adapter=None, memory=None, on_event=None, comms=None, depth=0,
                 object_store=None, session_id=None, conversation=False):
        super().__init__(
            cfg,
            gateway=gateway if gateway is not None else Gateway.from_config(cfg),
            registry=registry, journal=journal, adapter=adapter, memory=memory,
            on_event=on_event, comms=comms, depth=depth,
            object_store=object_store, session_id=session_id,
            conversation=conversation,
            rag_factory=_rag_factory,
            comms_factory=Comms.from_config,
            catalog_provider=AGENT_CATALOG_PROVIDER,
            extra_tool_providers=AGENT_TOOL_PROVIDERS)

    def turn_ctx(self, run_id: str):
        """One turn to central: every model call of this drive shares a turn
        id, the lease is held until the drive returns, and turn_done is sent
        then — so a tool step between calls never reads as a stale session."""
        sig = session_signals.signals()
        if sig is None:
            return contextlib.nullcontext()
        return sig.turn(task="run:%s" % run_id)
