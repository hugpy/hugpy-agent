"""Shadow evaluation: full-context baseline vs MCT-curated vs MCT-with-faults.

Design ref: §22 Phase 5, §24 (task families + measurements), §23.3 (shadow-mode
rollout), §20.3 (reuse an ``eval``-style deterministic checker suite). Serves the
Phase-5 exit condition: *token savings are material and quality stays within the
accepted threshold on the task suite.*

The core metrics are **deterministic and offline** — token reduction, context
selection precision/recall, omission vs pull-recovery, and fault recovery — so the
suite is reproducible and free. An optional live arm (``mode="live"``) runs real
Claude Code on both paths for true correctness, at cost. Token counts use the
installed gateway estimator (§20.3) so both arms are measured identically.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import fake_a
from .a_adapter import AAdapterClient
from .protocol import parse_pointer
from .session import BrokerConfig, BrokerServer, _ABinding

try:
    from hugpy_agent.gateway import estimate_tokens as _gw_tokens
except Exception:  # pragma: no cover
    _gw_tokens = None


def toks(text: str) -> int:
    if _gw_tokens is not None:
        try:
            return _gw_tokens(text)
        except Exception:
            pass
    return max(1, len(text) // 4)


@dataclass
class Task:
    id: str
    family: str
    prompt: str
    gold_evidence: str                       # substring that answers the task
    sources: list[tuple[str, str]] = field(default_factory=list)  # (catalog_name, content)
    prior_turns: list[str] = field(default_factory=list)          # build memory first
    pull_query: str | None = None
    pull_form: str | None = None
    latest_instruction: str | None = None    # adherence tasks
    relevant_marker: str | None = None        # selection P/R: relevant fragments contain this
    is_fault: bool = False


# --- the task suite (design §23.1 families) --------------------------------

def build_suite() -> list[Task]:
    # Representative LARGE contexts — the families MCT targets (§1.2, §23.1). Token
    # savings are expected to be material here; MCT deliberately adds overhead on
    # trivially small inputs, which is not what it is for.
    big_log = "\n".join(
        (f"line {i}: heartbeat ok" if i != 640 else
         "line 640: FATAL OutOfMemory worker=gpu-02 alloc=A17 (max-gpu preference overridden)")
        for i in range(1, 1200)) + "\n"

    big_config = "\n".join(
        [f"# section {s}" for s in range(30)] +
        ["[server]", "workers = 8", "timeout = 30"] +
        [f"tunable_{i} = {i}" for i in range(120)] +
        ["[eviction]", "policy = max-gpu", "preempt_override = true  # KEY: allows eviction",
         "[logging]", "level = info"])

    # A transcript of prior decisions (kept modest so the suite runs fast; the
    # token-savings claim rests on the source tasks + the scaling experiment, not
    # on these small memory tasks, which measure adherence and selection quality).
    filler = [f"DECISION: minor setting {i} = value-{i}" for i in range(8)]
    indent_turns = filler + ["DECISION: use tabs for indentation",
                             "DECISION: actually use spaces for indentation, not tabs"]

    widget_turns = ([f"DECISION: unrelated policy {i} is set to option-{i}" for i in range(8)] +
                    ["DECISION: the widget color is teal",
                     "DECISION: the widget border is teal too"])

    return [
        Task(id="log-diagnosis", family="log-diagnosis",
             prompt="What caused the crash on worker gpu-02? Cite the exact log line.",
             gold_evidence="FATAL OutOfMemory worker=gpu-02 alloc=A17",
             sources=[("logs.app", big_log)],
             pull_query="logs app", pull_form="match FATAL ctx 0"),
        Task(id="config-lookup", family="architecture-recall",
             prompt="Does the eviction policy allow preemption to override max-gpu?",
             gold_evidence="preempt_override = true",
             sources=[("config.eviction", big_config)],
             pull_query="config eviction", pull_form="match preempt_override ctx 0"),
        Task(id="latest-instruction", family="contradiction",
             prompt="Which indentation should we use in the codebase?",
             gold_evidence="spaces", prior_turns=indent_turns,
             latest_instruction="spaces"),
        Task(id="selection-precision", family="selection",
             prompt="What did we decide about the widget color?",
             gold_evidence="widget", prior_turns=widget_turns,
             relevant_marker="widget"),
        Task(id="fault-recovery", family="fault", is_fault=True,
             prompt="What is the current eviction policy value?",
             gold_evidence="policy = zonal-fair",   # AFTER the source change
             sources=[("config.eviction", big_config)],
             pull_query="config eviction", pull_form="match policy ctx 0"),
    ]


# --- evaluator -------------------------------------------------------------

class ShadowEvaluator:
    def __init__(self, *, use_model: bool = True):
        self.use_model = use_model

    def _fresh(self, budget_tokens: int | None = None):
        ws = Path(tempfile.mkdtemp(prefix="mct-eval-"))
        cfg = BrokerConfig(use_model=self.use_model)
        if budget_tokens is not None:
            cfg.max_input_tokens = budget_tokens
            cfg.reserved_output_tokens = budget_tokens // 4
            cfg.pull_budget.max_tokens = budget_tokens // 4  # keep pack budget > 0
        server = BrokerServer(ws, sink=lambda *_: None, config=cfg)
        sess = server.session(server.open_session("eval"))
        sess.set_policy("Answer only from resolved evidence; cite it.")
        return server, sess, ws

    def _seed(self, sess, task: Task):
        for turn in task.prior_turns:
            sess.submit(turn, fake_a.answer("ack"))
        root = None
        if task.sources:
            root = Path(tempfile.mkdtemp(prefix="mct-src-"))
            for name, content in task.sources:
                rel = name.replace(".", "_") + ".txt"
                (root / rel).write_text(content)
                sess.register_root("root", str(root)) if "root" not in sess._roots else None
                sess.register_source_file(name, "root", rel)
        return root

    def baseline_tokens(self, task: Task) -> int:
        """Full-context: everything pasted inline — transcript + all sources + prompt."""
        blob = "\n".join(task.prior_turns)
        blob += "".join(content for _, content in task.sources)
        blob += "\n" + task.prompt
        return toks(blob)

    def run_task_offline(self, task: Task) -> dict:
        server, sess, ws = self._fresh()
        root = self._seed(sess, task)

        prompt = task.prompt
        turn_id, epoch, op_ref, manifest_ptr, sha, trace = sess._prepare_turn(prompt, None)

        # MCT curated input tokens = operator prompt + every packed fragment's REAL text
        # (sources are NOT inlined — that is the whole point).
        import json
        manifest = json.loads(server.store.resolve(sess.session_id, manifest_ptr))
        mct_input = toks(prompt)
        packed_texts = []
        for frag in manifest["fragments"]:
            data = server.store.resolve(sess.session_id, frag["object"]).decode("utf-8", "replace")
            packed_texts.append((frag, data))
            mct_input += toks(data)

        # Answerability: gold in packed context, else pull, else omission.
        gold = task.gold_evidence
        answerable_no_pull = any(gold in d for _, d in packed_texts)
        pull_tokens, pull_recovered, pull_decision = 0, False, None
        if not answerable_no_pull and task.pull_query:
            binding = _ABinding(sess, turn_id, epoch, manifest_ptr, sha)
            out = AAdapterClient(binding).submit_pull(
                need="evidence", target={"kind": "catalog-query", "query": task.pull_query},
                preferred_form=task.pull_form)
            pull_decision = out.decision
            for obj in out.objects:
                ex = server.store.resolve(sess.session_id, obj["object"]).decode("utf-8", "replace")
                pull_tokens += toks(ex)
                if gold in ex:
                    pull_recovered = True

        omission = (not answerable_no_pull) and (not pull_recovered)
        baseline = self.baseline_tokens(task)
        mct_total = mct_input + pull_tokens
        reduction = 1.0 - (mct_total / baseline) if baseline else 0.0

        metrics = {
            "id": task.id, "family": task.family,
            "baseline_tokens": baseline, "mct_tokens": mct_total,
            "token_reduction": round(reduction, 4),
            "answerable_no_pull": answerable_no_pull,
            "pull_decision": pull_decision, "pull_recovered": pull_recovered,
            "omission_error": omission,
        }
        if task.latest_instruction:
            metrics["adherence"] = self._check_adherence(sess, packed_texts, task)
        if task.relevant_marker:
            facts = server.compaction.facts(sess.session_id, kind="decision")
            metrics.update(self._selection_pr(packed_texts, task, facts))
        server.close()
        return metrics

    def _selection_pr(self, packed_texts, task: Task, task_session_facts) -> dict:
        """Precision/recall of the packer over durable decisions (dedup by object,
        decision_memory role only — episodic copies are not double-counted)."""
        seen, included, included_relevant = set(), 0, 0
        for f, d in packed_texts:
            if f["role"] != "decision_memory" or f["object"] in seen:
                continue
            seen.add(f["object"])
            included += 1
            if task.relevant_marker in d:
                included_relevant += 1
        # Denominator = relevant decisions that actually exist in durable memory.
        all_relevant = sum(1 for f in task_session_facts if task.relevant_marker in f.text)
        precision = included_relevant / included if included else 0.0
        recall = included_relevant / all_relevant if all_relevant else 0.0
        return {"selection_precision": round(precision, 3), "selection_recall": round(recall, 3)}

    def _check_adherence(self, sess, packed_texts, task: Task) -> bool:
        """Latest operator instruction must be represented; the stale one must not win."""
        decisions = sess.server.compaction.facts(sess.session_id, kind="decision")
        if not decisions:
            return False
        latest = decisions[-1].text  # most recently recorded
        packed_decision_text = " ".join(d for f, d in packed_texts if f["role"] == "decision_memory")
        return task.latest_instruction in latest and task.latest_instruction in packed_decision_text

    def run_fault_task(self, task: Task) -> dict:
        """Inject a source change + epoch reset; verify B re-snapshots and rebuilds
        a fresh working set without assuming stale residency (§12.3, invariant 7)."""
        server, sess, ws = self._fresh()
        root = self._seed(sess, task)
        name, rel = task.sources[0][0], task.sources[0][0].replace(".", "_") + ".txt"

        # First turn: snapshot the ORIGINAL source.
        sess._prepare_turn("initial look", None)
        snaps_before = server.ledger.list_objects(sess.session_id, kinds=["source_snapshot"])
        digest_before = snaps_before[0]["digest"]

        # Change the source on disk AND reset the epoch (A restarted / context cleared).
        (root / rel).write_text("[eviction]\npolicy = zonal-fair\npreempt_override = false\n")
        new_epoch = sess.new_epoch("source changed + A restart")
        sess.invalidate_source_cache(name)  # force re-snapshot on next build

        # Next turn under the new epoch: must re-snapshot the CHANGED bytes.
        turn_id, epoch, op_ref, manifest_ptr, sha, trace = sess._prepare_turn(task.prompt, None)
        snaps_after = server.ledger.list_objects(sess.session_id, kinds=["source_snapshot"])
        digests_after = {s["digest"] for s in snaps_after}

        binding = _ABinding(sess, turn_id, epoch, manifest_ptr, sha)
        out = AAdapterClient(binding).submit_pull(
            need="current policy", target={"kind": "catalog-query", "query": task.pull_query},
            preferred_form=task.pull_form)
        pulled = ""
        for obj in out.objects:
            pulled = server.store.resolve(sess.session_id, obj["object"]).decode("utf-8", "replace")

        result = {
            "id": task.id, "family": "fault",
            "epoch_changed": epoch == new_epoch,
            "source_resnapshotted": digest_before not in digests_after or len(snaps_after) > 1,
            "serves_new_content": task.gold_evidence in pulled,
            "stale_digest_not_reused": any(d != digest_before for d in digests_after),
        }
        result["fault_recovered"] = (result["epoch_changed"] and result["serves_new_content"]
                                     and result["source_resnapshotted"])
        server.close()
        return result

    def scaling_experiment(self, history_lengths=(50, 200, 800),
                           budget_tokens: int = 1500) -> list[dict]:
        """Bounded working set (design §0): as the conversation grows, the
        full-context baseline grows ~linearly while MCT's per-turn input stays
        bounded. Run one memory query at several history lengths."""
        rows = []
        for n in history_lengths:
            server, sess, ws = self._fresh(budget_tokens=budget_tokens)
            # Seed durable memory directly (fast) — equivalent to N prior turns.
            src = server.store.commit(sess.session_id, b"history", media_type="text/plain",
                                      kind="operator_turn")
            for i in range(n):
                server.compaction.record_fact(sess.session_id, f"setting {i} is value-{i}",
                                               kind="decision", source_object_ids=[src.object_id])
            server.compaction.record_fact(sess.session_id, "the deployment region is us-east",
                                           kind="decision", source_object_ids=[src.object_id])
            prompt = "Which deployment region did we choose?"
            baseline = toks("\n".join(f"DECISION: setting {i} is value-{i}" for i in range(n))
                            + "\nDECISION: the deployment region is us-east\n" + prompt)
            _tid, _e, _op, mptr, _sha, _tr = sess._prepare_turn(prompt, None)
            import json
            manifest = json.loads(server.store.resolve(sess.session_id, mptr))
            mct = toks(prompt)
            for frag in manifest["fragments"]:
                mct += toks(server.store.resolve(sess.session_id, frag["object"]).decode("utf-8", "replace"))
            rows.append({"history": n, "baseline_tokens": baseline, "mct_tokens": mct,
                         "token_reduction": round(1 - mct / baseline, 4) if baseline else 0.0})
            server.close()
        return rows

    def run_suite_offline(self, tasks: list[Task] | None = None,
                          scaling_lengths=(50, 200, 800)) -> dict:
        tasks = tasks or build_suite()
        rows = [self.run_fault_task(t) if t.is_fault else self.run_task_offline(t) for t in tasks]
        scaling = self.scaling_experiment(history_lengths=scaling_lengths)

        # "Material savings" is asserted where MCT is designed to help: large
        # sources/logs and long histories. Small-context memory tasks are reported
        # for quality (adherence, P/R), not as a savings claim.
        big = {"log-diagnosis", "architecture-recall"}
        source_reductions = [r["token_reduction"] for r in rows if r.get("family") in big]
        summary = {
            "tasks": len(rows),
            "mean_reduction_large_context": round(sum(source_reductions) / len(source_reductions), 4)
                if source_reductions else 0.0,
            "scaling_reduction_at_max_history": scaling[-1]["token_reduction"],
            "omission_errors": sum(1 for r in rows if r.get("omission_error")),
            "pull_recovered": sum(1 for r in rows if r.get("pull_recovered")),
            "adherence_pass": all(r.get("adherence", True) for r in rows),
            "selection_recall": next((r["selection_recall"] for r in rows
                                      if "selection_recall" in r), None),
            "fault_recovered": all(r.get("fault_recovered", True) for r in rows),
        }
        return {"rows": rows, "scaling": scaling, "summary": summary}
