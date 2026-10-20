"""Agent toolset — the hugpy_harness tool registry core (byte-identical
primitives: Registry/ToolSpec/ToolContext/RISK_*/ToolInterrupted) plus the
agent's fleet-coupled tools layered on through the harness's provider hooks.

build_registry delegates to hugpy_harness.tools.build_registry, replacing the
default portable catalog with the curated toolset_tools catalog and adding the
http / fleet / lean / toolserver / remember / recall / spawn tools as
providers. ask_operator and final_answer are harness built-ins.
"""
from __future__ import annotations

import json

from hugpy_harness.tools import (  # noqa: F401  (primitives re-exported)
    RISK_DESTRUCTIVE,
    RISK_NETWORK,
    RISK_READONLY,
    RISK_REMOTE_COMPUTE,
    RISK_WRITE,
    Registry,
    ToolContext,
    ToolInterrupted,
    ToolProviderContext,
    ToolSpec,
    UNSAFE_TO_RERUN,
    build_registry as _harness_build_registry,
)


# ── agent tool-handlers (kept agent-side; the harness owns ask_operator) ─────
def _remember_handler(memory, rag, fact: str, title: str = "",
                      _context: ToolContext | None = None) -> str:
    """remember body when a RAG index is attached (P2.6). Ordering is the
    contract: the markdown fact is written FIRST and unconditionally — the
    file store is the source of truth, vectors are only an index. An
    embed/index failure is a WARN-shaped event (`rag_error`), never a raise
    and never a lost fact; the tool result stays the normal remember
    receipt so the model is not derailed by a degraded index."""
    out = memory.remember(fact, title=title)
    err = rag.index(fact)
    if err:
        (_context or ToolContext()).on_event("rag_error", "index", err)
    return out


def _recall_handler(rag, query: str, k=5) -> str:
    """recall body: embed the query, cosine top-k over the vector store.
    Matches come back as data ({text, score} per fact, best first); an
    unavailable embed endpoint is the structured 'RAG disabled: ...'
    error-as-data, pointing the model at the always-available fallback
    (the memory index + fs_read)."""
    try:
        k = max(1, min(int(k), 20))
    except (TypeError, ValueError):
        k = 5
    matches, err = rag.recall(query, k)
    if matches is None:
        return json.dumps({"error": "RAG disabled: %s. The markdown memory "
                                    "index in the system prompt still works "
                                    "— use fs_read on memory/ files instead."
                                    % err})
    return json.dumps({"matches": [{"text": m["text"], "score": m["score"]}
                                   for m in matches],
                       "count": len(matches)})


# ── providers: provider(ctx) -> [ToolSpec] ───────────────────────────────────
def _toolset_tools_catalog(ctx: ToolProviderContext):
    """The agent's curated hugpy_tools toolset (replaces the harness's default
    portable catalog)."""
    try:
        from . import toolset_tools
    except ImportError:
        return []
    return list(toolset_tools.specs(ctx.workspace))


def _http_provider(ctx: ToolProviderContext):
    from . import http
    return [http.spec(object_store=ctx.object_store,
                      default_session_id=ctx.session_id)]


def _fleet_provider(ctx: ToolProviderContext):
    from . import fleet
    return list(fleet.specs(ctx.gateway, ctx.workspace))


def _lean_provider(ctx: ToolProviderContext):
    from . import lean
    return list(lean.specs(ctx.gateway, ctx.workspace))


def _toolserver_provider(ctx: ToolProviderContext):
    # Default ON; only attempted when a Config is supplied (loop/serve/node).
    # Errors-as-data: an unreachable toolserver registers nothing. `taken`
    # keeps a toolserver tool from shadowing a jailed local one in flat mode.
    if ctx.cfg is None or not getattr(ctx.cfg, "toolserver", True):
        return []
    try:
        from . import toolserver as _ts
        return list(_ts.specs(ctx.cfg, on_event=ctx.on_event, taken=ctx.taken))
    except Exception:  # noqa: BLE001 — the toolset must never fail to build
        return []


def _memory_provider(ctx: ToolProviderContext):
    """remember (always, when a memory store exists) + recall (only with a RAG
    index). With RAG the remember handler also embeds+indexes the fact — the
    markdown write ALWAYS happens first and an index failure degrades to an
    event."""
    specs = []
    if ctx.memory is not None:
        if ctx.rag is not None:
            mem_handler = (lambda fact, title="", _context=None:
                           _remember_handler(ctx.memory, ctx.rag, fact, title, _context))
        else:
            mem_handler = ctx.memory.remember
        specs.append(ToolSpec(
            name="remember",
            description=("Save a durable fact to workspace memory (one markdown "
                         "file per fact, indexed in memory/MEMORY.md). Use for "
                         "things future sessions must know."),
            parameters={"type": "object",
                        "properties": {
                            "fact": {"type": "string",
                                     "description": "the fact, one or two sentences"},
                            "title": {"type": "string",
                                      "description": "short title for the index"}},
                        "required": ["fact"]},
            handler=mem_handler,
            risk_class=RISK_WRITE,
            needs_context=(ctx.rag is not None)))
    if ctx.rag is not None:
        specs.append(ToolSpec(
            name="recall",
            description=("Semantic search over workspace memory: returns the "
                         "k remembered facts most similar to `query` (cosine "
                         "over fleet embeddings), best match first. Use it "
                         "when past sessions may have recorded something "
                         "relevant that the memory index does not obviously "
                         "surface."),
            parameters={"type": "object",
                        "properties": {
                            "query": {"type": "string",
                                      "description": "what to search memory for"},
                            "k": {"type": "integer",
                                  "description": "how many matches to return "
                                                 "(default 5)"}},
                        "required": ["query"]},
            handler=lambda query, k=5: _recall_handler(ctx.rag, query, k),
            risk_class=RISK_READONLY))
    return specs


def _spawn_provider(ctx: ToolProviderContext):
    if ctx.agent_loop is None:
        return []
    from ..subagent import make_spawn_spec
    spec = make_spawn_spec(ctx.agent_loop)
    return [spec] if spec is not None else []


# The agent's catalog + extra tool providers, in registration order.
AGENT_CATALOG_PROVIDER = _toolset_tools_catalog
AGENT_TOOL_PROVIDERS = [
    _http_provider,
    _fleet_provider,
    _lean_provider,
    _toolserver_provider,
    _memory_provider,
    _spawn_provider,
]


def build_registry(workspace: str, gateway, memory=None, comms=None,
                   agent_loop=None, rag=None, object_store=None,
                   session_id: str | None = None, cfg=None,
                   on_event=None) -> Registry:
    """The agent's built-in toolset: the harness core (fs/shell + ask_operator
    + final_answer) with the agent's curated catalog and fleet/toolserver/
    subagent/memory tools layered on via the harness provider hooks."""
    return _harness_build_registry(
        workspace, gateway=gateway, memory=memory, comms=comms,
        agent_loop=agent_loop, rag=rag, object_store=object_store,
        session_id=session_id, cfg=cfg, on_event=on_event,
        catalog_provider=AGENT_CATALOG_PROVIDER,
        extra_tool_providers=AGENT_TOOL_PROVIDERS)
