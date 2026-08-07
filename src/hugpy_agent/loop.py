"""Agent core — the assess->act->observe loop (design §3.2).

Shape of one step:
  1. load wire messages from the journal (compaction-aware), fit to budget
  2. call the model (gateway), scrub known platform leaks, journal the reply
  3. extract a tool call (adapter); on parse/validation failure spend the ONE
     repair round-trip; on repeated failure abort with a structured report
  4. execute via the journal's idempotency protocol (record-before-execute),
     journal the tool response, go to 1

Termination is a TOOL (`final_answer`) — a schema'd signal beats inferring
"done" from prose (fail-closed on ambiguity). `resume(run_id)` re-enters the
same loop: because every message and call is journaled, resume is literally
"load and continue", with completed side-effect calls replayed from the
ledger instead of re-executed.

SIGINT safety: the CLI sets `stop_requested`; the loop only checks it at
step boundaries, and every journal write is individually committed, so an
interrupt can never leave a half-written ledger.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from . import adapter as adapter_mod
from .adapter import Adapter, cached_probe_native
from .audit import AuditLog, default_audit_path, sha256_of
from .comms import Comms
from .config import Config
from .gateway import (Gateway, estimate_tokens, is_capacity_error,
                      pick_ladder_brain, resolve_brain_ladder)
from .journal import Journal, idem_key
from .memory import Memory
from .policy import decide
from .rag import RagIndex, default_vectors_path, fleet_embedder
from .tools import Registry, ToolContext, ToolInterrupted, UNSAFE_TO_RERUN

# Escalation button labels (P2.3). The operator's click comes back as the
# label text, so these strings ARE the protocol; compare exactly.
APPROVE = "Approve"
DENY_LABEL = "Deny"
APPROVE_ALL_FMT = "Approve all %s this run"

# Consecutive model-output failures (unparseable / no tool call / chat error)
# tolerated before a structured abort. 3 = the initial miss, the repair
# round-trip, and one last chance — beyond that a small model is looping.
MAX_CONSECUTIVE_FAILURES = 3
COMPACT_AT = 0.7        # fraction of the context budget that triggers compaction
KEEP_TAIL = 8           # recent messages never compacted (working set)

# Loop-guard nudge (P2.4), injected ONCE per identical-call streak when it
# reaches cfg.loop_guard_n. If the streak then reaches 2N the run aborts
# with outcome "looping" — a weak model spinning on the same call must fail
# fast, not burn the step cap (the agent-level analogue of base_runner's
# text-level repetition guard).
LOOP_GUARD_NUDGE = ("You have called %s with the same arguments %d times; "
                    "do something different or call final_answer.")

_SYSTEM_TEMPLATE = """You are hugpy-agent, an autonomous assistant working \
inside a workspace directory on this machine.

Workspace: {workspace}
All file paths you use are relative to the workspace. You cannot access files \
outside it.

Rules:
- Work step by step: inspect before you act, verify after you act.
- Make EXACTLY ONE tool call per reply, then stop and wait for its result.
- Cite concrete file paths in your reasoning and answers.
- If a tool returns an error, adapt — try a different approach rather than \
repeating the same call.
- `final_answer` only REPORTS — it does not save anything to disk. If the \
task asks you to write or create a file, you MUST create it with `fs_write` \
BEFORE calling `final_answer`, and name the written file in your answer.
- When the task is complete (or clearly impossible), call the `final_answer` \
tool with your full report. That is the only way to finish.
{memory_block}
{tools_block}"""


class AgentLoop:
    def __init__(self, cfg: Config, gateway: Gateway | None = None,
                 registry: Registry | None = None, journal: Journal | None = None,
                 adapter: Adapter | None = None, memory: Memory | None = None,
                 on_event=None, comms: Comms | None = None, depth: int = 0):
        self.cfg = cfg
        # Subagent nesting level (P2.5): 0 for an operator-started run;
        # children get parent.depth + 1. Gates whether this loop's registry
        # may carry a `spawn` tool at all (see subagent.make_spawn_spec).
        self.depth = depth
        self.gateway = gateway or Gateway.from_config(cfg)
        self.memory = memory or Memory(cfg.workspace)
        # Operator channel (P2.3): shared by the ask_operator tool and the
        # escalation gate. Unconfigured comms is a valid state — every ask
        # then fails closed to deny.
        self.comms = comms or Comms.from_config(cfg)
        # Embed-RAG memory (P2.6): a semantic INDEX over the markdown fact
        # store — vectors in <workspace>/.hugpy_agent/memory_vectors.db,
        # embeddings from the fleet's /api/ml/embed. cfg.rag_enabled=False
        # leaves it None: no recall tool, no indexing, no run-start recall.
        self.rag = (RagIndex(default_vectors_path(cfg.workspace),
                             fleet_embedder(self.gateway, cfg.workspace))
                    if cfg.rag_enabled else None)
        if registry is None:
            from .tools import build_registry
            registry = build_registry(cfg.workspace, self.gateway, self.memory,
                                      comms=self.comms, agent_loop=self,
                                      rag=self.rag)
        self.registry = registry
        self.journal = journal or Journal(default_journal_path(cfg.workspace))
        self.adapter = adapter or Adapter(self._pick_mode())
        self.on_event = on_event or (lambda *a, **k: None)
        # Audit trail (P2.2). cfg.audit_log None => the workspace default;
        # "" => disabled. The lambda keeps the event hook live even if a
        # caller swaps loop.on_event after construction.
        audit_path = (cfg.audit_log if cfg.audit_log is not None
                      else default_audit_path(cfg.workspace))
        self.audit = AuditLog(audit_path, verbose=cfg.audit_verbose,
                              on_event=lambda *a, **k: self.on_event(*a, **k))
        self.stop_requested = False
        # Brain ladder (k96): the model this run actually talks to. Set by
        # _select_brain() at run start (never per step); the capacity walk-down
        # in _drive may advance it FORWARD along the ladder, never back; every
        # chat/journal/audit site reads active_model, never cfg.model, so the
        # whole loop agrees on the brain.
        self.active_model = cfg.model
        self._ladder = [cfg.model]        # resolved for real by _select_brain
        self._ladder_pos = 0              # index of active_model in _ladder
        self._brain_reason = "not selected yet"

    # ── brain ladder (run-start selection, k96) ──────────────────────────
    def _select_brain(self) -> None:
        """Resolve the ladder and choose the starting brain ONCE, at run
        start (never per step).

        Warm-first: asks /llm/workers which models are warm and starts on
        the FIRST warm ladder entry; with an explicit HUGPY_AGENT_BRAINS
        ladder and nothing warm, starts on the LAST entry — the pilot light,
        whose cold load is cheap by design. A single-entry ladder makes NO
        probe traffic, and any probe trouble is a SILENT ladder[0]: this
        feature is an optimization and must never be able to break or delay
        a run beyond the probe's few-second timeout."""
        self._ladder, explicit = resolve_brain_ladder(self.cfg)
        if len(self._ladder) == 1:
            self.active_model = self._ladder[0]
            self._ladder_pos = 0
            self._brain_reason = "single brain configured"
            return
        try:
            warm = self.gateway.warm_models()
        except Exception:   # belt-and-suspenders; the probe never raises
            warm = None
        model, pos, why = pick_ladder_brain(warm, self._ladder, explicit)
        self.active_model = model
        self._ladder_pos = pos
        self._brain_reason = why
        if pos != 0:
            self.on_event("brain", model, why)

    def _record_brain_choice(self, run_id: str) -> None:
        """Journal the run-start ladder decision (choice + reason) so a case
        report's provenance survives the process — kv, keyed by run_id."""
        try:
            self.journal.kv_set("brain|%s" % run_id, json.dumps({
                "model": self.active_model, "position": self._ladder_pos,
                "ladder": self._ladder, "reason": self._brain_reason}))
        except Exception:  # noqa: BLE001 — bookkeeping must never break a run
            pass

    # ── mode selection ───────────────────────────────────────────────────
    def _pick_mode(self) -> str:
        """Initial adapter mode from config. `prompted` (the default) and
        `constrained` are honored as-is with ZERO probe traffic — the
        2026-07-14 dev incident was this loop probing when nobody asked.
        `auto` and `native` start prompted and may be upgraded by the ONE
        cached probe in _maybe_probe_native() at run start."""
        mode = (self.cfg.tools_mode or "prompted").lower()
        if mode in (adapter_mod.MODE_PROMPTED, adapter_mod.MODE_CONSTRAINED):
            return mode
        return adapter_mod.MODE_PROMPTED  # auto/native: upgrade after probe

    def _maybe_probe_native(self) -> None:
        """Upgrade to the native tier — ONLY when explicitly requested.

        Fires solely for tools_mode auto|native, and then only through the
        per-box file cache (adapter.cached_probe_native: one live probe per
        (base, model) per TTL across ALL workspaces — the incident's other
        root cause was a per-workspace journal cache re-probing dev from
        every fresh workspace/test). Under the default `prompted` config no
        code path reaches a probe. Explicit `native` still verifies via the
        same cached probe: sending `tools` to a seam that drops them costs
        ~a minute of GPU per step today, so we fail closed to prompted and
        say so rather than honor a harmful override blindly."""
        mode = (self.cfg.tools_mode or "prompted").lower()
        if mode not in ("auto", adapter_mod.MODE_NATIVE):
            return
        ok = cached_probe_native(self.gateway, self.active_model)
        if ok:
            self.adapter.mode = adapter_mod.MODE_NATIVE
            self.on_event("mode", "native tool-calling detected and enabled")
        elif mode == adapter_mod.MODE_NATIVE:
            self.on_event("mode", "native requested but the seam does not "
                                  "support tools; using prompted")

    # ── prompt assembly ──────────────────────────────────────────────────
    def _system_prompt(self) -> str:
        mem = self.memory.load_index()
        memory_block = ("\nWorkspace memory index (fetch entries with fs_read "
                        "if relevant):\n%s\n" % mem) if mem else ""
        tools_block = self.adapter.system_prompt_block(self.registry.specs())
        return _SYSTEM_TEMPLATE.format(workspace=self.cfg.workspace,
                                       memory_block=memory_block,
                                       tools_block=tools_block)

    def _auto_recall_block(self, task: str) -> str:
        """Run-start auto-recall (P2.6, design §3.4): the rag_k remembered
        facts nearest the task text, rendered for pinning into the SYSTEM
        message — seq 0, which compaction never touches, so the recalled
        facts survive the whole run. Degrades to a silent-with-event no-op:
        an unavailable embed endpoint emits one `rag_error` event and
        returns "" — recall is an accelerant, never a run dependency (the
        embed attempt itself is bounded by rag.EMBED_TIMEOUT)."""
        if self.rag is None or not str(task or "").strip():
            return ""
        try:
            matches, err = self.rag.recall(task, k=self.cfg.rag_k)
        except Exception as exc:  # noqa: BLE001 — belt-and-suspenders;
            matches, err = None, str(exc)  # recall() itself never raises
        if matches is None:
            self.on_event("rag_error", "auto_recall", err)
            return ""
        if not matches:
            return ""
        lines = "\n".join("- %s" % m["text"][:300].replace("\n", " ")
                          for m in matches)
        return ("\nWorkspace memory recalled for this task (semantic match; "
                "verify against the memory/ fact files before relying on "
                "it):\n%s\n" % lines)

    # ── public API ───────────────────────────────────────────────────────
    def run(self, task: str) -> dict:
        self._select_brain()
        self._maybe_probe_native()
        return self._drive(self.prepare_run(task))

    def prepare_run(self, task: str, parent_run_id: str | None = None) -> str:
        """Create + seed a run WITHOUT driving it. The subagent seam (P2.5):
        spawn needs the child's run_id journaled into the parent's call state
        BEFORE the child takes a step, so a crash mid-child re-attaches on
        resume instead of re-spawning. Deliberately does NOT probe for
        native tools — a child inherits the parent's already-resolved
        adapter mode (zero extra probe traffic, post-incident doctrine)."""
        run_id = self.journal.create_run(task, self.active_model,
                                         parent_run_id=parent_run_id)
        self._record_brain_choice(run_id)
        self.journal.append_message(run_id, "system",
                                    self._system_prompt()
                                    + self._auto_recall_block(task))
        self.journal.append_message(run_id, "user", "TASK:\n" + task)
        self.on_event("run_start", run_id, task)
        return run_id

    def resume(self, run_id: str) -> dict:
        run = self.journal.get_run(run_id)
        if run is None:
            return {"run_id": run_id, "outcome": "aborted",
                    "error": "unknown run_id %r" % run_id,
                    "steps": 0, "tool_calls": 0, "est_tokens": 0}
        if run["status"] == "done":
            return json.loads(run["outcome"]) if run["outcome"] else \
                {"run_id": run_id, "outcome": "done", "steps": 0,
                 "tool_calls": 0, "est_tokens": 0}
        # Resume is a run start from this process's point of view: re-check
        # which brain is seated NOW (the fleet may have reshuffled since the
        # original process died) — still never per step.
        self._select_brain()
        self._record_brain_choice(run_id)
        self.journal.set_run_status(run_id, "running")
        self.on_event("resume", run_id, run["task"])
        return self._drive(run_id)

    def send_user(self, run_id: str, text: str) -> None:
        """Chat REPL support: append a user turn to an existing run."""
        self.journal.append_message(run_id, "user", text)

    def start_chat(self) -> str:
        """Create a run for the interactive REPL (no fixed task brief)."""
        self._select_brain()
        self._maybe_probe_native()
        run_id = self.journal.create_run("(interactive chat)", self.active_model)
        self._record_brain_choice(run_id)
        self.journal.append_message(run_id, "system", self._system_prompt())
        self.journal.append_message(
            run_id, "user",
            "This is an interactive session. Answer the user's messages; use "
            "tools when needed; call final_answer to deliver each answer.")
        return run_id

    # ── the loop ─────────────────────────────────────────────────────────
    def _drive(self, run_id: str) -> dict:
        failures = 0
        est_total = 0
        # Loop-guard (P2.4): rolling signature of the current streak of
        # validated calls. The signature is (tool_name, args_sha256) using
        # the same hash the audit line needs anyway — computed once per call,
        # nothing extra on the happy path. Turns that produce no validated
        # call (garbage, unknown tool, bad args) leave the streak untouched:
        # they are not progress either, and the failure counter above already
        # bounds them.
        guard_sig = None       # (tool, args_sha256) of the current streak
        guard_count = 0        # consecutive identical validated calls
        guard_nudged = False   # this streak's one nudge has been sent
        while True:
            if self.stop_requested:
                return self._finish(run_id, "interrupted")
            steps = self.journal.assistant_step_count(run_id)
            if steps >= self.cfg.max_steps:
                return self._finish(run_id, "max_steps")

            # Resume seam: if the last journaled message is an assistant turn,
            # a previous process died before handling it — process it instead
            # of calling the model again (the idempotency key is derived from
            # that message's seq, so replay lines up exactly).
            last = self.journal.last_message(run_id)
            if last and last["role"] == "assistant":
                text = last["content"] if isinstance(last["content"], str) else ""
                a_seq = last["seq"]
                native_calls = []
            else:
                self._compact_if_needed(run_id)
                wire = self.journal.wire_messages(run_id)
                res = self.gateway.chat(
                    wire, model=self.active_model,
                    max_tokens=self.cfg.max_tokens,
                    tools=self.adapter.wire_tools(self.registry.specs()),
                    stream=(self.adapter.mode != adapter_mod.MODE_NATIVE),
                    on_delta=lambda p: self.on_event("delta", p))
                est_total += res.est_tokens + sum(
                    estimate_tokens(json.dumps(m.get("content"))) for m in wire)
                if not res.ok and not res.text:
                    # Reactive ladder walk-down (k96): a CAPACITY-class or
                    # permanent-verdict refusal (the fleet cannot seat/serve
                    # this brain right now) is not a transient the retry/abort
                    # ladder can fix — advance to the NEXT ladder entry and
                    # retry this step on it. FORWARD-ONLY and index-bounded:
                    # at most len(ladder)-1 switches per run, never back up,
                    # so no ping-pong is possible by construction. On the last
                    # entry (the pilot light) a refusal rides the normal
                    # failure ladder below.
                    if (self._ladder_pos < len(self._ladder) - 1
                            and is_capacity_error(res.error)):
                        self._ladder_pos += 1
                        self.active_model = self._ladder[self._ladder_pos]
                        self.on_event("brain_fallback", self.active_model,
                                      res.error)
                        continue    # the one retry, not a counted failure
                    failures += 1
                    self.on_event("chat_error", res.error)
                    if failures >= MAX_CONSECUTIVE_FAILURES:
                        return self._finish(run_id, "aborted",
                                            error="model unreachable: %s" % res.error)
                    time.sleep(1)
                    continue
                text = adapter_mod.scrub(res.text)
                a_seq = self.journal.append_message(run_id, "assistant", text)
                self.on_event("assistant", text)
                native_calls = res.native_tool_calls

            outcome = self.adapter.extract(text, native_calls)

            if not outcome.calls:
                failures += 1
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    return self._finish(
                        run_id, "aborted",
                        error="model failed to produce a valid tool call after "
                              "%d attempts; last errors: %s"
                              % (failures, outcome.errors or ["no tool call in reply"]))
                if outcome.errors:
                    # The ONE repair round-trip for malformed JSON.
                    self.journal.append_message(
                        run_id, **_as_row(self.adapter.repair_message(outcome.errors)))
                    self.on_event("repair", outcome.errors)
                else:
                    self.journal.append_message(
                        run_id, "user",
                        "You must respond with a tool call. Use `final_answer` "
                        "if the task is complete.")
                    self.on_event("nudge", text[:200])
                continue

            call = outcome.calls[0]
            extra_note = ""
            if len(outcome.calls) > 1:
                # One call at a time is a stated rule; executing several from
                # one turn would let the model act on results it never saw.
                extra_note = (" NOTE: you sent %d tool calls; only the first "
                              "was executed. Send one call per reply."
                              % len(outcome.calls))

            if call.name == "final_answer":
                errors, args = self.adapter.validate(
                    self.registry.get("final_answer").parameters, call.arguments)
                answer = args.get("answer") if not errors else None
                if answer is None:
                    answer = json.dumps(call.arguments)[:4000]
                # Belt-and-suspenders: a thinking model may nest a <think>
                # span inside the answer value itself.
                answer = adapter_mod.strip_think(answer)
                report = self._finish(run_id, "done", answer=answer,
                                      est_tokens=est_total)
                self.on_event("final", answer)
                return report

            spec = self.registry.get(call.name)
            if spec is None:
                result = json.dumps({"error": "unknown tool %r; available tools: %s"
                                     % (call.name, ", ".join(self.registry.names()))})
                self.journal.append_message(
                    run_id, **_as_row(self.adapter.tool_response_message(call.name, result)))
                continue

            errors, args = self.adapter.validate(spec.parameters, call.arguments)
            if errors:
                failures += 1
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    return self._finish(
                        run_id, "aborted",
                        error="repeated invalid arguments for %s: %s"
                              % (call.name, errors))
                self.journal.append_message(
                    run_id, **_as_row(self.adapter.repair_message(errors)))
                self.on_event("repair", errors)
                continue

            # Only a successfully validated + dispatched call resets the
            # failure streak — resetting on mere parse success would let a
            # model alternate parse-ok/args-bad forever without tripping the
            # structured abort.
            failures = 0

            # Loop-guard bookkeeping (P2.4). sha256_of canonicalizes the
            # args (sorted keys), so logically-identical calls hash the same
            # regardless of key order; the value is handed down to the audit
            # writer so it is computed exactly once per call.
            args_sha = sha256_of(args)
            if self.cfg.loop_guard_n > 0:
                sig = (call.name, args_sha)
                if sig == guard_sig:
                    guard_count += 1
                else:
                    # Genuine progress — a distinct tool or distinct args —
                    # starts a fresh streak and re-arms the nudge.
                    guard_sig, guard_count, guard_nudged = sig, 1, False
                if guard_nudged and guard_count >= 2 * self.cfg.loop_guard_n:
                    # Nudged and still spinning: abort NOW (before executing
                    # yet another copy) rather than burn the step cap.
                    self.on_event("loop_guard", call.name, "abort",
                                  guard_count)
                    return self._finish(
                        run_id, "looping",
                        error="loop-guard: %s called with identical "
                              "arguments %d times (nudged after %d); "
                              "aborting the run"
                              % (call.name, guard_count,
                                 self.cfg.loop_guard_n))

            result, replayed = self._execute(run_id, a_seq, spec, args,
                                             args_sha)
            if result is None:
                # Tool was interrupted mid-execution (operator stop): the
                # call stays pending in the journal; the loop-top stop check
                # finishes the run as interrupted, and resume re-executes the
                # handler, which re-attaches via its journaled state.
                continue
            if len(result) > self.cfg.observation_cap_chars > 0:
                # A single oversized observation (an 84KB /llm/jobs dump on a
                # 32k-ctx brain) squeezes every later completion to a few
                # tokens and the run dies mid-tool-call. Clamp BELOW the
                # per-tool caps (http_fetch's 64KB FETCH_CAP) — journal keeps
                # the full result; only the conversation copy is clipped.
                result = (result[:self.cfg.observation_cap_chars]
                          + '\n[observation clipped at %d of %d chars — '
                            're-query with a narrower filter for the rest]'
                          % (self.cfg.observation_cap_chars, len(result)))
            self.on_event("tool", call.name, args, result, replayed)
            self.journal.append_message(
                run_id,
                **_as_row(self.adapter.tool_response_message(call.name,
                                                             result + extra_note)))
            if (self.cfg.loop_guard_n > 0 and not guard_nudged
                    and guard_count >= self.cfg.loop_guard_n):
                # The ONE strong nudge for this streak, placed after the
                # tool response so the model sees its (identical) result
                # first. Repeating past this point hits the 2N abort above.
                guard_nudged = True
                self.journal.append_message(
                    run_id, "user",
                    LOOP_GUARD_NUDGE % (call.name, guard_count))
                self.on_event("loop_guard", call.name, "nudge", guard_count)

    # ── execution with the idempotency protocol ──────────────────────────
    def _execute(self, run_id: str, assistant_seq: int, spec, args,
                 args_sha: str | None = None):
        """Returns (result_str, replayed); (None, False) = interrupted, call
        left pending. `args_sha` is the caller's precomputed sha256_of(args)
        (the loop-guard signature), reused for the audit line so the hash is
        computed once per call. Decides policy, dispatches, then writes ONE
        audit line per resolved call (P2.2) — allowed, denied, and replayed alike
        (denials are the point of an audit trail). An interrupted call is
        left pending and NOT audited: its resume re-executes the handler and
        audits the resolved outcome. The audit writer never raises; a write
        failure surfaces as on_event("audit_error", ...) and the run goes on.
        """
        # Policy gate (P2.1) — BEFORE the idempotency lookup, so a stricter
        # policy on resume re-blocks a call rather than replaying it. A
        # denial is journaled as a normal error result (resume-consistent)
        # and returned as data: the model observes the refusal and adapts;
        # the loop never crashes on policy.
        decision = decide(self.cfg.policy_mode, spec, args,
                          self.cfg.tool_allow, self.cfg.tool_deny)
        started = time.monotonic()
        result, replayed = self._dispatch(run_id, assistant_seq, spec, args,
                                          decision)
        if result is not None:
            # `decision` is the POLICY verdict: an escalated call is audited
            # as "ask" whether the operator approved or denied it — the
            # outcome rides in error_bool + the result hash.
            self.audit.record(
                datetime.now(timezone.utc),
                run_id=run_id,
                step=self.journal.assistant_step_count(run_id),
                tool=spec.name, risk=spec.risk_class, decision=decision,
                model=self.active_model,
                args=args, result=result, args_sha256=args_sha,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=result.startswith('{"error"'))
        return result, replayed

    def _dispatch(self, run_id: str, assistant_seq: int, spec, args,
                  decision: str):
        """The gated execute. Record-before-execute: the pending row hits
        disk before the handler runs, so a crash mid-handler is detectable on
        resume as 'pending'. Pending resolution:
          * read-only            -> safe, re-execute
          * unsafe + call STATE  -> re-execute; the handler re-attaches to
            its journaled remote handle (async job_id) instead of redoing
            the side effect — this is what makes resume re-POLL a
            generation job rather than enqueue a duplicate (§6 Ph1.5)
          * unsafe, no state     -> outcome unknown; reported as data
        """
        key = idem_key(run_id, assistant_seq, spec.name, args)
        if decision == "ask":
            # Escalation gate (P2.3): block on the operator's button click.
            # None => approved, fall through to the normal execute path
            # (which also handles replay); a string is the denial-as-data.
            self.on_event("policy", spec.name, decision)
            denial = self._escalate(run_id, key, spec, args)
            if denial is not None:
                self.journal.record_call_start(key, run_id, assistant_seq,
                                               spec.name, args)
                self.journal.record_call_result(key, "error", denial)
                return denial, False
        elif decision != "allow":
            self.on_event("policy", spec.name, decision)
            result = json.dumps({"error": (
                "policy denied: tool %s (risk %s) is not permitted under "
                "policy mode %r. Continue without this action or finish "
                "and report what could not be done."
            ) % (spec.name, spec.risk_class, self.cfg.policy_mode)})
            self.journal.record_call_start(key, run_id, assistant_seq,
                                           spec.name, args)
            self.journal.record_call_result(key, "error", result)
            return result, False

        existing = self.journal.lookup_call(key)
        if existing:
            if existing["status"] in ("done", "error"):
                return existing["result"] or "", True
            # pending = crashed/interrupted mid-execution
            if (spec.risk_class in UNSAFE_TO_RERUN
                    and self.journal.get_call_state(run_id, key) is None):
                result = json.dumps({
                    "error": ("a previous process was interrupted while executing "
                              "this %s call; its outcome is unknown. Verify the "
                              "state with read-only tools before retrying.")
                              % spec.name})
                self.journal.record_call_result(key, "error", result)
                return result, True
            # read-only, or resumable state exists: re-execute (falls through)
        else:
            self.journal.record_call_start(key, run_id, assistant_seq,
                                           spec.name, args)
        context = ToolContext(journal=self.journal, run_id=run_id,
                              idem_key=key,
                              max_generations=self.cfg.max_generations,
                              stop=lambda: self.stop_requested,
                              on_event=self.on_event)
        try:
            result = self.registry.execute(spec, args, context)
        except ToolInterrupted:
            return None, False             # stays pending; resume re-attaches
        status = "error" if result.startswith('{"error"') else "done"
        self.journal.record_call_result(key, status, result)
        return result, False

    # ── escalation gate (P2.3) ───────────────────────────────────────────
    def _escalate(self, run_id: str, key: str, spec, args) -> str | None:
        """Resolve an `ask` policy decision with the operator. Returns None
        when the call may proceed, else the denial as an error-as-data
        string. Fail closed: only an explicit Approve click opens the gate —
        Deny, timeout, an unconfigured channel, and any comms failure all
        deny.

        Resolution order:
          1. a call already resolved in the ledger was approved when it ran —
             resume replays it instead of re-asking the operator;
          2. a journaled per-run "approve all <tool>" grant (the operator's
             third button) skips the ask — and, living in the journal's kv,
             it survives resume but is scoped to this run_id;
          3. otherwise ask, blocking (bounded by cfg.ask_timeout).
        """
        existing = self.journal.lookup_call(key)
        if existing and existing["status"] in ("done", "error"):
            return None                     # normal path replays the result
        cache_key = "approval|%s|%s" % (run_id, spec.name)
        if self.journal.kv_get(cache_key) == "all":
            return None
        approve_all = (APPROVE_ALL_FMT % spec.name)[:80]  # Discord label cap
        preview = json.dumps(args, sort_keys=True)
        if len(preview) > 300:
            preview = preview[:300] + "…"
        question = ("hugpy-agent requests approval (run %s)\n"
                    "tool: %s  (risk: %s)\nargs: %s"
                    % (run_id, spec.name, spec.risk_class, preview))
        reply = self.comms.ask(question, [APPROVE, approve_all, DENY_LABEL],
                               stop=lambda: self.stop_requested)
        choice = reply.get("choice") if reply.get("answered") else None
        self.on_event("ask", spec.name,
                      choice or ("timeout" if reply.get("timed_out")
                                 else reply.get("error") or "no answer"))
        if choice == APPROVE:
            return None
        if choice == approve_all:
            self.journal.kv_set(cache_key, "all")
            return None
        if reply.get("timed_out"):
            return json.dumps({"error": (
                "policy denied: the operator did not answer the approval "
                "request for %s within %ss. Continue without this action or "
                "finish and report what could not be done."
            ) % (spec.name, self.cfg.ask_timeout)})
        if choice == DENY_LABEL:
            return json.dumps({"error": (
                "policy denied: the operator denied %s (risk %s). Continue "
                "without this action or finish and report what could not "
                "be done."
            ) % (spec.name, spec.risk_class)})
        return json.dumps({"error": (
            "policy denied: %s (risk %s) requires operator approval and "
            "the operator channel is unavailable (%s). Continue without "
            "this action or finish and report what could not be done."
        ) % (spec.name, spec.risk_class,
             reply.get("error") or "no usable answer")})

    # ── context management ───────────────────────────────────────────────
    def _compact_if_needed(self, run_id: str) -> None:
        """Summarize the oldest non-pinned turns via the model itself when the
        estimated wire size crosses COMPACT_AT of the context budget. The
        system prompt and task brief (seq 0..1) are never compacted — losing
        the contract or the goal is unrecoverable; losing old observations is
        just lossy."""
        wire = self.journal.wire_messages(run_id)
        budget = self.gateway.context_length(self.active_model,
                                             self.cfg.ctx_fallback)
        est = sum(estimate_tokens(json.dumps(m.get("content"))) for m in wire) \
            + self.cfg.max_tokens
        if est <= budget * COMPACT_AT:
            return
        rows = [r for r in self.journal.raw_messages(run_id)
                if r["role"] != "summary"]
        # candidates: everything after the pinned prefix, sparing the tail.
        middle = [r for r in rows if r["seq"] > 1][:-KEEP_TAIL]
        if not middle:
            return
        cut = middle[-1]["seq"]
        transcript = "\n\n".join(
            "%s: %s" % (r["role"], (r["content"] if isinstance(r["content"], str)
                                    else json.dumps(r["content"])))[:2000]
            for r in middle)
        res = self.gateway.chat(
            [{"role": "user", "content":
              "Summarize this agent-session transcript into a dense progress "
              "note: what was tried, what was learned (with file paths), what "
              "remains. Max ~300 words.\n\n%s\n\n/no_think" % transcript}],
            model=self.active_model, max_tokens=500, stream=False)
        if res.ok and res.text.strip():
            summary = res.text.strip()
        else:
            # Compaction must degrade, not fail: fall back to naming what was
            # dropped so the model at least knows information is missing.
            summary = ("(%d earlier messages dropped to fit the context "
                       "window; re-read files if needed)" % len(middle))
        self.journal.append_message(run_id, "summary", summary,
                                    meta={"replaces_upto": cut})
        self.on_event("compaction", cut, len(middle))

    # ── reports ──────────────────────────────────────────────────────────
    def _finish(self, run_id: str, outcome: str, answer: str | None = None,
                error: str | None = None, est_tokens: int = 0) -> dict:
        # k96: a run answered off ladder[0] must SAY so — a pilot-light case
        # report is visibly reduced-depth, never silently passed off as the
        # primary brain's work. One line, appended to the answer (the surface
        # sentinel case reports actually publish) and carried in the report.
        brain_note = None
        if self._ladder_pos > 0 and len(self._ladder) > 1:
            brain_note = ("answered by %s (ladder position %d of %d)"
                          % (self.active_model, self._ladder_pos + 1,
                             len(self._ladder)))
            if answer is not None:
                answer = "%s\n\n[%s]" % (answer, brain_note)
        report = {
            "run_id": run_id,
            "outcome": outcome,
            # The brain that actually drove (the tail of) this run — differs
            # from the runs.model column when the capacity walk-down fired.
            "model": self.active_model,
            "steps": self.journal.assistant_step_count(run_id),
            "tool_calls": self.journal.call_count(run_id),
            "est_tokens": est_tokens,
        }
        if brain_note is not None:
            report["brain_note"] = brain_note
        if answer is not None:
            report["answer"] = answer
        if error is not None:
            report["error"] = error
        self.journal.set_run_status(run_id, outcome, report)
        return report


def _as_row(msg: dict) -> dict:
    """Adapter messages are wire-shaped ({role, content, ...}); the journal
    wants keyword args (role=, content=). Extra native-tier keys (e.g. the
    tool `name`) are intentionally dropped: the /v1 seam has no real tool
    role today and the wire loader emits role+content only."""
    return {"role": msg["role"], "content": msg["content"]}


def default_journal_path(workspace: str) -> str:
    import os
    return os.path.join(os.path.realpath(workspace), ".hugpy_agent",
                        "journal.db")
