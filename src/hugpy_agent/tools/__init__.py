"""Tool registry — declarative {name, description, json_schema, handler,
risk_class} (design §3.3).

Risk classes drive resume semantics today (journal replays vs. re-executes)
and Phase-2 policy gates later:
  readonly       — safe to re-execute any time
  write          — mutates the workspace; replay, never blind re-run
  destructive    — shell etc.; replay, never blind re-run
  network        — outbound side effects unknowable; treated like write
  remote_compute — spends fleet GPU (ML amenities, generation jobs); never
                   blindly re-run, capped per run for async generation, and
                   the Phase-2 policy layer hooks here (design §6 Ph1.5)

Handlers return a string (what the model sees). They may raise — the
registry converts every exception into a structured error STRING, because a
failed tool is an observation for the model, never a crash of the loop. The
single exception is ToolInterrupted (operator SIGINT mid-tool), which must
propagate so the journal keeps the call `pending` and resume can re-attach.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

RISK_READONLY = "readonly"
RISK_WRITE = "write"
RISK_DESTRUCTIVE = "destructive"
RISK_NETWORK = "network"
RISK_REMOTE_COMPUTE = "remote_compute"

# Risk classes whose side effects cannot be safely repeated: on resume a
# 'pending' journal row for these becomes an "outcome unknown" result —
# UNLESS the call left durable state (journal.get_call_state), in which case
# the handler is re-executed and re-attaches (e.g. re-polls its job_id).
UNSAFE_TO_RERUN = {RISK_WRITE, RISK_DESTRUCTIVE, RISK_NETWORK,
                   RISK_REMOTE_COMPUTE}


class ToolInterrupted(Exception):
    """Raised by a handler when the operator requested a stop mid-execution
    (long poll loops). Deliberately NOT converted to error-as-data: the call
    must stay `pending` in the journal so `resume` re-executes the handler,
    which re-attaches to its journaled remote state instead of restarting."""


@dataclass
class ToolContext:
    """Execution context the loop hands to handlers that ask for it
    (needs_context=True). This is how a handler journals durable state
    (async job ids), honors the per-run generation cap, and notices an
    operator interrupt — without importing the loop."""
    journal: object = None
    run_id: str = ""
    idem_key: str = ""
    max_generations: int = 2
    stop: Callable[[], bool] = lambda: False
    on_event: Callable = lambda *a, **k: None

    # convenience wrappers (None-journal tolerant so tools stay usable
    # standalone, e.g. from live_smoke)
    def get_state(self) -> dict | None:
        if self.journal is None:
            return None
        return self.journal.get_call_state(self.run_id, self.idem_key)

    def set_state(self, state: dict) -> None:
        if self.journal is not None:
            self.journal.set_call_state(self.run_id, self.idem_key, state)

    def run_states(self) -> dict:
        if self.journal is None:
            return {}
        return self.journal.list_call_states(self.run_id)


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict                       # JSON schema for the arguments object
    handler: Callable[..., str]
    risk_class: str = RISK_READONLY
    needs_context: bool = False            # handler takes a _context kwarg


class Registry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools)

    def execute(self, spec: ToolSpec, args: dict,
                context: ToolContext | None = None) -> str:
        """Dispatch with errors-as-data. The returned string is exactly what
        goes back to the model, so error text is written FOR the model:
        specific, actionable, no tracebacks."""
        # A model must never smuggle harness-internal kwargs: strip any
        # underscore-prefixed keys before dispatch (schemas don't declare
        # them, but validation tolerates unknown extras by design).
        args = {k: v for k, v in args.items() if not k.startswith("_")}
        if spec.needs_context:
            args["_context"] = context or ToolContext()
        try:
            result = spec.handler(**args)
        except ToolInterrupted:
            raise                          # see class docstring: stays pending
        except TypeError as exc:
            return json.dumps({"error": "bad arguments for %s: %s" % (spec.name, exc)})
        except Exception as exc:  # noqa: BLE001 — the loop must never die on a tool
            return json.dumps({"error": "%s failed: %s: %s"
                               % (spec.name, type(exc).__name__, exc)})
        if isinstance(result, str):
            return result
        try:
            return json.dumps(result)
        except (TypeError, ValueError):
            return str(result)


def _ask_operator_handler(comms, question: str, options: list) -> str:
    """ask_operator body: route a question to the operator channel and block
    (bounded) on the clicked reply. Everything is data: unconfigured comms,
    a timeout, and an answered click all come back as a JSON string the
    model can reason about (doctrine: fail closed, never crash)."""
    if comms is None:
        return json.dumps({"error": "no operator channel configured; "
                                    "proceed without operator input"})
    reply = comms.ask(question, options)
    if reply.get("answered"):
        return json.dumps({"answered": True, "choice": reply.get("choice")})
    if reply.get("timed_out"):
        return json.dumps({"error": "the operator did not answer within the "
                                    "configured timeout; proceed without "
                                    "operator input or finish and report"})
    return json.dumps({"error": "could not reach the operator: %s"
                       % (reply.get("error") or "unknown failure")})


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


def build_registry(workspace: str, gateway, memory=None, comms=None,
                   agent_loop=None, rag=None) -> Registry:
    """The Phase-1 built-in toolset. `final_answer` is registered as a real
    tool — a schema'd, validated termination signal beats parsing prose for
    'am I done?' (fail-closed on ambiguity).

    `agent_loop` (P2.5): the AgentLoop this registry will serve. When given,
    a `spawn` tool bound to that loop is registered — unless the loop sits
    at the configured max depth (subagent.make_spawn_spec returns None
    there; a floor-level child must not be able to recurse)."""
    from . import fs, http, shell, fleet, lean

    reg = Registry()
    for spec in fs.specs(workspace):
        reg.register(spec)
    reg.register(shell.spec(workspace))
    reg.register(http.spec())
    for spec in fleet.specs(gateway, workspace):
        reg.register(spec)
    # lean (2026-07-29): the token-efficiency kit — find-by-phrase, digest,
    # log digests, eviction windows, deliver-by-path. Registered so every
    # keeper driving this agent inherits the cheap paths by default.
    for spec in lean.specs(gateway, workspace):
        reg.register(spec)
    # ask_operator (P2.3): a direct line to the human. Risk READONLY — it
    # only sends a message and waits; it mutates nothing and is safe to
    # re-execute on resume (the operator just gets asked again).
    reg.register(ToolSpec(
        name="ask_operator",
        description=("Ask the human operator a clarifying question and wait "
                     "for their answer. Provide 1-5 short `options` labels — "
                     "they become clickable buttons; the reply is the chosen "
                     "label. Use when a decision genuinely needs a human "
                     "(ambiguous instructions, irreversible trade-offs)."),
        parameters={"type": "object",
                    "properties": {
                        "question": {"type": "string",
                                     "description": "the question to ask "
                                                    "(short; <=1900 chars)"},
                        "options": {"type": "array",
                                    "items": {"type": "string"},
                                    "description": "1-5 short answer labels "
                                                   "(<=80 chars each)"}},
                    "required": ["question", "options"]},
        handler=lambda question, options: _ask_operator_handler(
            comms, question, options),
        risk_class=RISK_READONLY))
    if memory is not None:
        # With a RAG index attached (P2.6) the handler also embeds+indexes
        # the fact — via _remember_handler so the markdown write ALWAYS
        # happens first and an index failure degrades to an event. Without
        # one, remember is the bare Phase-1 file write, unchanged.
        if rag is not None:
            mem_handler = (lambda fact, title="", _context=None:
                           _remember_handler(memory, rag, fact, title, _context))
        else:
            mem_handler = memory.remember
        reg.register(ToolSpec(
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
            needs_context=(rag is not None)))
    if rag is not None:
        # recall (P2.6): semantic search over remembered facts. Risk
        # READONLY per the slice contract — it reads the local index and
        # never mutates anything (the embed round-trip is a lookup cost,
        # not a side effect; a re-run on resume is harmless).
        reg.register(ToolSpec(
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
            handler=lambda query, k=5: _recall_handler(rag, query, k),
            risk_class=RISK_READONLY))
    reg.register(ToolSpec(
        name="final_answer",
        description=("Finish the task. Call this exactly once, when the task is "
                     "complete (or impossible), with your full final answer. "
                     "This does NOT write any file — use fs_write first if the "
                     "task requires an output file."),
        parameters={"type": "object",
                    "properties": {
                        "answer": {"type": "string",
                                   "description": "the complete final answer / report"}},
                    "required": ["answer"]},
        handler=lambda answer: answer,   # never dispatched; loop intercepts it
        risk_class=RISK_READONLY))
    if agent_loop is not None:
        # Lazy import: hugpy_agent.subagent imports the loop module, which
        # imports this package at module level — resolving spawn at call
        # time keeps the import graph acyclic.
        from ..subagent import make_spawn_spec
        spawn_spec = make_spawn_spec(agent_loop)
        if spawn_spec is not None:
            reg.register(spawn_spec)
    return reg
