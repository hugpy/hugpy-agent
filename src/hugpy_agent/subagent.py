"""Subagents — the `spawn` tool and its child-loop factory (P2.5).

The agent delegates a scoped sub-task to a CHILD AgentLoop: fresh run_id
linked to the parent (`runs.parent_run_id`), a filtered tool subset, its own
(smaller) step budget, and the same journal — the keeper-style
assess→assign→verify pattern, expressible inside the product.

Authority doctrine (risk register: "policy inheritance leaks in subagents"):
delegation NEVER widens authority.
  * The child's registry is built by FILTERING the parent's registry — the
    child tool set is a subset of the parent's by construction, enforced
    again by an explicit check (fail closed, refuse the spawn as data).
  * policy_mode, tool_allow/tool_deny, the comms channel, and the workspace
    are inherited verbatim: a readonly parent yields a readonly child, and
    the child's own calls pass through the child's own policy gate with
    their real risk classes (spawn itself is risk `readonly` — starting a
    child mutates nothing; what the child DOES is gated per call).
  * Budget is shared, not minted: the child's max_generations is the
    parent's REMAINING pool, measured from journaled run state (one state
    entry per real enqueue, so replays/resumes never double-count).
  * Depth is capped (cfg.max_depth): a loop at the floor gets no `spawn`
    tool at all (make_spawn_spec returns None), so recursion is bounded by
    construction, not by convention.

Resume semantics mirror async generation jobs: the spawn handler journals
{child_run_id, ...} via ToolContext.set_state THE MOMENT the child run is
created, before the child takes a step. A parent resumed over a pending
spawn re-executes this handler (spawn is readonly), which finds the state
and RE-ATTACHES — resuming an in-flight child or collecting a completed
child's journaled outcome — never re-spawning a duplicate.

Structured delegation (P2.6, trace.py): a spawn may carry a `packet` — a
dispatch contract (objective/scope/constraints/verification/handoff) that
is validated BEFORE the child runs; a malformed packet refuses the spawn as
data (fail closed, same posture as the toolset gate). Every COMPLETED
delegation — packeted or not — leaves a trace artifact under
`<workspace>/.hugpy_agent/traces/`; artifact writing is fail-open (a trace
failure degrades to an on_event line, never crashes the spawn path), and
re-attach never duplicates an existing artifact.
"""
from __future__ import annotations

import json
import os
from dataclasses import replace

from .adapter import Adapter
from .loop import AgentLoop
from .tools import (RISK_READONLY, Registry, ToolContext, ToolInterrupted,
                    ToolSpec)
from .trace import synthesize_packet, trace_path, validate_packet, write_trace


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


class _ChildLoop(AgentLoop):
    """AgentLoop whose stop signal chains to its parent's: an operator
    SIGINT must halt the whole delegation tree, not just the loop that holds
    the terminal. The child then finishes `interrupted`, the spawn handler
    raises ToolInterrupted, and the parent's spawn call stays pending WITH
    its child_run_id journaled — resume re-attaches (same shape as an
    interrupted generation poll)."""

    def __init__(self, *args, parent_stop=None, **kwargs):
        self._own_stop = False
        self._parent_stop = parent_stop or (lambda: False)
        super().__init__(*args, **kwargs)

    @property
    def stop_requested(self) -> bool:
        return self._own_stop or self._parent_stop()

    @stop_requested.setter
    def stop_requested(self, value: bool) -> None:
        self._own_stop = bool(value)


def generations_used(journal, run_id: str, _seen: set | None = None) -> int:
    """Generation jobs charged to `run_id` INCLUDING its descendants — the
    whole delegation tree draws on one max_generations pool. Counts journaled
    enqueue states (kind == "generation"; exactly one per real enqueue, the
    same source _generation_guard uses) and recurses through journaled
    spawn states (kind == "subagent") into child runs. `_seen` guards
    against a corrupt state cycle — better a conservative count than a
    recursion crash."""
    seen = _seen if _seen is not None else set()
    if run_id in seen:
        return 0
    seen.add(run_id)
    used = 0
    for state in journal.list_call_states(run_id).values():
        kind = state.get("kind")
        if kind == "generation":
            used += 1
        elif kind == "subagent" and state.get("child_run_id"):
            used += generations_used(journal, state["child_run_id"], seen)
    return used


def _resolve_toolset(parent: AgentLoop, requested) -> tuple[list | None, str]:
    """(tool_names, error). The strict-subset gate: every requested name
    must already be in the PARENT's registry — a child cannot be granted a
    tool its parent does not hold (refused as data, fail closed). Default
    grant: the parent's whole toolset minus `spawn` (depth stays bounded
    unless recursion is explicitly requested AND the depth cap allows it).
    `final_answer` is always included — a child that cannot terminate is a
    step-cap burn, never a feature."""
    parent_names = set(parent.registry.names())
    if requested:
        if not isinstance(requested, (list, tuple)):
            return None, ("spawn refused: `tools` must be a list of tool "
                          "names from your own toolset")
        names = []
        for t in requested:
            t = str(t).strip()
            if t and t not in names:
                names.append(t)
        missing = sorted(n for n in names if n not in parent_names)
        if missing:
            return None, ("spawn refused: tool(s) %s are not in your own "
                          "toolset — a subagent can only receive a subset "
                          "of the tools you hold. Your tools: %s"
                          % (", ".join(missing),
                             ", ".join(sorted(parent_names))))
    else:
        names = [n for n in parent.registry.names() if n != "spawn"]
    if "final_answer" not in names:
        names.append("final_answer")
    # Belt-and-suspenders assertion of the no-widening invariant. Cannot
    # fire given the checks above; if it ever does, refuse rather than run.
    if not set(names) <= parent_names:
        return None, "spawn refused: child toolset exceeds the parent's"
    return names, ""


def _build_child(parent: AgentLoop, names: list, child_steps: int,
                 child_generations: int) -> AgentLoop:
    """The child-loop factory. Everything authority-shaped is INHERITED
    (policy mode, allow/deny lists, comms, workspace); everything budget-
    shaped is CAPPED (steps, generations); the registry is a filtered view
    of the parent's registry objects. Same journal — child runs live in the
    same ledger, linked by parent_run_id."""
    cfg = replace(parent.cfg,
                  max_steps=child_steps,
                  max_generations=child_generations,
                  # lists/dicts are copied so a child can never mutate the
                  # parent's policy config through a shared reference
                  tool_allow=list(parent.cfg.tool_allow),
                  tool_deny=list(parent.cfg.tool_deny),
                  sources=dict(parent.cfg.sources))
    reg = Registry()
    for name in names:
        if name == "spawn":
            continue    # never share the PARENT's spawn binding (wrong loop)
        spec = parent.registry.get(name)
        if spec is not None:
            reg.register(spec)
    child = _ChildLoop(cfg, gateway=parent.gateway, registry=reg,
                       journal=parent.journal,
                       # the parent's adapter mode is already resolved (any
                       # native probe happened at the root) — inherit it,
                       # emit zero probe traffic
                       adapter=Adapter(parent.adapter.mode),
                       memory=parent.memory, on_event=parent.on_event,
                       comms=parent.comms, depth=parent.depth + 1,
                       parent_stop=lambda: parent.stop_requested)
    if "spawn" in names:
        # Recursion was explicitly requested: bind a FRESH spawn to the
        # child. At the depth floor this is None and the child simply does
        # not get the tool (the cap is structural, not advisory).
        child_spawn = make_spawn_spec(child)
        if child_spawn is not None:
            reg.register(child_spawn)
    return child


def _record_trace(parent: AgentLoop, ctx: ToolContext, child_run_id: str,
                  packet: dict | None, task_text: str, report: dict) -> None:
    """Write the delegation's trace artifact — fail-OPEN (audit.py posture:
    losing evidence of finished work is bad; killing the spawn result over
    it is worse). Idempotent across re-attach: an existing artifact for
    this (parent, child) pair is left untouched, so resume never duplicates
    a trace or its INDEX line."""
    try:
        path = trace_path(parent.cfg.workspace, ctx.run_id, child_run_id)
        if os.path.exists(path):
            return                       # re-attach over a completed child
        pkt = dict(packet) if packet else synthesize_packet(task_text)
        outcome = {"status": report.get("outcome") or "unknown",
                   "summary": (report.get("answer")
                               or report.get("error") or ""),
                   "evidence": "journal run %s: steps=%s tool_calls=%s"
                               % (child_run_id, report.get("steps", 0),
                                  report.get("tool_calls", 0))}
        write_trace(parent.cfg.workspace, ctx.run_id, child_run_id,
                    pkt, outcome)
    except Exception as exc:  # noqa: BLE001 — trace must never break spawn
        ctx.on_event("trace_error", "%s: %s" % (type(exc).__name__, exc))


def _spawn_handler(parent: AgentLoop, brief: str, tools=None, max_steps=None,
                   packet=None, _context: ToolContext | None = None) -> str:
    """The spawn tool body. Two paths, split on journaled call state:
    fresh spawn (create + link + run) vs. re-attach (resume the journaled
    child). Every failure is data; the only exception allowed out is
    ToolInterrupted (operator stop), which keeps the call pending so the
    NEXT resume re-attaches.

    Structured dispatch: an optional `packet` is validated fail-closed
    BEFORE any child exists; its objective is appended to the child's task
    text so the contract the parent stated is the contract the child sees.
    The packet is journaled in the call state so a re-attach after a crash
    still writes the trace with the ORIGINAL contract, not a reconstruction."""
    ctx = _context or ToolContext()
    state = ctx.get_state() or {}
    if state.get("kind") == "subagent" and state.get("child_run_id"):
        # ── re-attach: this exact spawn already created its child ────────
        child_run_id = state["child_run_id"]
        task_text = state.get("task") or str(brief or "")
        packet = state.get("packet")     # journaled contract wins over args
        names, err = _resolve_toolset(parent, state.get("tools"))
        if err:
            return _err(err)   # e.g. the toolset shrank since; fail closed
        child = _build_child(
            parent, names,
            max(1, int(state.get("max_steps") or parent.cfg.sub_max_steps)),
            max(0, int(state.get("max_generations") or 0)))
        ctx.on_event("spawn_reattach", child_run_id)
        report = child.resume(child_run_id)
    else:
        # ── fresh spawn ──────────────────────────────────────────────────
        if not str(brief or "").strip():
            return _err("spawn needs a non-empty `brief` — a complete, "
                        "self-contained task statement for the subagent")
        if packet is not None:
            # Dispatch contract gate — fail closed, all errors in one reply
            # so the model repairs the packet in a single round-trip.
            errors = validate_packet(packet)
            if errors:
                return _err("spawn refused: invalid dispatch packet: "
                            + "; ".join(errors))
        names, err = _resolve_toolset(parent, tools)
        if err:
            return _err(err)
        cap = max(1, int(parent.cfg.sub_max_steps))
        try:
            child_steps = int(max_steps) if max_steps is not None else cap
        except (TypeError, ValueError):
            child_steps = cap
        child_steps = max(1, min(child_steps, cap))   # ask for less, never more
        # Shared pool: the child receives only what the parent's tree has
        # not already spent (measured from journaled state, not memory).
        remaining = max(0, int(ctx.max_generations)
                        - generations_used(parent.journal, ctx.run_id))
        task_text = str(brief)
        if packet:
            # The stated contract rides INTO the child's task so the child
            # is briefed with the same fields the artifact will audit.
            task_text += ("\n\nDISPATCH CONTRACT\n"
                          "Objective: %s\nScope: %s\nConstraints: %s\n"
                          "Verification: %s\nHandoff expected: %s"
                          % (packet["objective"], packet["scope"],
                             "; ".join(packet.get("constraints") or [])
                             or "(none)",
                             packet["verification"],
                             packet["handoff_expectations"]))
        child = _build_child(parent, names, child_steps, remaining)
        child_run_id = child.prepare_run(task_text, parent_run_id=ctx.run_id)
        # CRITICAL ordering (same as async enqueue): the child_run_id is
        # journaled before the child takes a single step, so a crash
        # anywhere in the child leaves a pending spawn WITH state — resume
        # lands back here and re-attaches, never re-spawns.
        ctx.set_state({"kind": "subagent", "child_run_id": child_run_id,
                       "tools": names, "max_steps": child_steps,
                       "max_generations": remaining,
                       "task": task_text,
                       "packet": dict(packet) if packet else None})
        ctx.on_event("spawn", child_run_id, str(brief)[:120])
        report = child.resume(child_run_id)

    outcome = report.get("outcome")
    if outcome == "interrupted" and ctx.stop():
        # Operator stop rippled into the child: keep this call pending so
        # the parent's resume re-attaches to the same child run. No trace
        # yet — the delegation is not over; the eventual completion writes.
        raise ToolInterrupted("stop requested during subagent run %s"
                              % child_run_id)
    _record_trace(parent, ctx, child_run_id, packet, task_text, report)
    if outcome == "done":
        return json.dumps({"child_run_id": child_run_id,
                           "answer": report.get("answer") or "",
                           "steps": report.get("steps", 0),
                           "tool_calls": report.get("tool_calls", 0)})
    return json.dumps({"error": "subagent run %s ended %r: %s"
                                % (child_run_id, outcome,
                                   report.get("error") or "no answer produced"),
                       "child_run_id": child_run_id})


def make_spawn_spec(loop: AgentLoop) -> ToolSpec | None:
    """The spawn ToolSpec bound to `loop`, or None when `loop` sits at (or
    past) the depth floor — a child at max depth gets NO spawn tool, so the
    recursion bound cannot be talked around. Risk `readonly`: creating a
    child mutates nothing; the child's own calls carry their real risk
    classes through the child's own (inherited) policy gate."""
    if loop.depth >= max(0, int(loop.cfg.max_depth)):
        return None
    return ToolSpec(
        name="spawn",
        description=("Delegate a scoped sub-task to a subagent (a fresh "
                     "agent run with its own step budget). The subagent "
                     "sees NOTHING of this conversation — `brief` must be a "
                     "complete, self-contained task statement. Returns the "
                     "subagent's final answer. `tools` optionally narrows "
                     "which of your tools it may use (default: all of them "
                     "except spawn); it can never get a tool you lack."),
        parameters={"type": "object",
                    "properties": {
                        "brief": {"type": "string",
                                  "description": "complete, self-contained "
                                                 "task brief for the subagent"},
                        "tools": {"type": "array",
                                  "items": {"type": "string"},
                                  "description": "optional tool names to "
                                                 "grant (subset of your own)"},
                        "max_steps": {"type": "integer",
                                      "description": "optional step cap for "
                                                     "the subagent (bounded "
                                                     "by the deployment cap)"},
                        "packet": {"type": "object",
                                   "description":
                                       "optional dispatch contract: "
                                       "{objective, scope, constraints[], "
                                       "verification, handoff_expectations, "
                                       "context_refs[]?}. Validated before "
                                       "the subagent runs; the delegation "
                                       "leaves an auditable trace artifact."}},
                    "required": ["brief"]},
        handler=lambda brief, tools=None, max_steps=None, packet=None,
                       _context=None:
            _spawn_handler(loop, brief, tools, max_steps, packet, _context),
        risk_class=RISK_READONLY,
        needs_context=True)
