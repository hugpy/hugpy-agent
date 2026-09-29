#!/usr/bin/env python3
"""Phase 2 acceptance demo — proves the design §22 Phase-2 exit condition:

    all selected context can be explained and reconstructed
    without a local model.

Deterministic end to end (no LLM). Shows: exact prompt retention, cross-turn
memory extraction/retrieval, the scored inclusion/omission trace (explanation),
provenance-graph reconstruction to originals, and the excerpt selectors.

Usage:  PYTHONPATH=src python3 tools/phase2_demo.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hugpy_agent.mct import fake_a
from hugpy_agent.mct.excerpt import apply_selector
from hugpy_agent.mct.session import BrokerServer

CODE = b"import os\n\n\ndef evict(worker):\n    return worker.preempted\n\n\nclass Scheduler:\n    max_gpu = True\n"


def banner(m): print(f"\n\033[1m=== {m} ===\033[0m")


def main() -> int:
    ok = True
    def check(label, cond):
        nonlocal ok; ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")

    workspace = tempfile.mkdtemp(prefix="mct-phase2-")
    printed = []
    server = BrokerServer(workspace, sink=printed.append)
    sess = server.session(server.open_session("demo"))
    sess.set_policy("Cite evidence. Prefer bounded excerpts over whole files.")

    # ---- Turn 1: operator records decisions; B extracts memory deterministically ----
    banner("1. Cross-turn memory (deterministic extraction)")
    sess.submit("DECISION: gpu-02 eviction is caused by preempt on alloc A17.\n"
                "PREFER: terse answers.", fake_a.answer("Understood."))
    decisions = server.compaction.facts(sess.session_id, kind="decision")
    prefs = server.compaction.facts(sess.session_id, kind="preference")
    check("decision extracted from turn 1", any("gpu-02" in f.text for f in decisions))
    check("preference extracted from turn 1", any("terse" in f.text for f in prefs))

    # ---- Register a source; it appears in the catalog (names only) ----
    sess.register_source("workspace.scheduler.py", CODE, media_type="text/x-python")

    # ---- Turn 2: related query; inspect the explanation trace ----
    banner("2. Explanation — why each fragment was included / omitted")
    r = sess.submit("why did gpu-02 get evicted, and how does the scheduler decide?",
                    fake_a.answer("Preempt on alloc A17."))
    trace = r.context_trace
    print(f"  pack_budget={trace['pack_budget']} tokens, selected={trace['selected_tokens']} tokens")
    for row in trace["included"]:
        comp = " ".join(f"{k}={v}" for k, v in row["components"].items()) or "(required)"
        print(f"    + {row['role']:<22} score={row['score']}  {row['reason']}  {comp}")
    for row in trace["omitted"][:3]:
        print(f"    - {row['role']:<22} score={row['score']}  {row['reason']}")
    roles = [row["role"] for row in trace["included"]]
    check("operator prompt retained verbatim (required)",
          any(row["reason"] == "required-operator-turn" for row in trace["included"]))
    check("governing instruction always present", "governing_instruction" in roles)
    check("prior decision retrieved into this turn", "decision_memory" in roles)
    check("every fragment carries a reason + components",
          all({"reason", "components", "score"} <= set(row) for row in trace["included"]))

    # ---- Reconstruction: walk a derived fragment back to its original bytes ----
    banner("3. Reconstruction — provenance graph back to originals")
    dm = [row for row in trace["included"] if row["role"] == "decision_memory"][0]
    graph = server.compaction.provenance_graph(sess.session_id, dm["object"])
    print("  " + json.dumps(graph, indent=2).replace("\n", "\n  "))
    check("derived fact is not an original", graph["is_original"] is False)
    check("fact traces to an original source object",
          bool(graph["sources"]) and graph["sources"][0]["is_original"])

    # ---- Excerpt selectors over the registered source ----
    banner("4. Bounded excerpts (lines / symbol / match)")
    src_ptr = sess._catalog["workspace.scheduler.py"]
    fn = sess.server.store.resolve(sess.session_id, src_ptr, selector="symbol evict").decode()
    hit = sess.server.store.resolve(sess.session_id, src_ptr, selector="match max_gpu").decode()
    print(f"    symbol evict -> {fn!r}")
    print(f"    match max_gpu -> {hit!r}")
    check("AST symbol excerpt", fn.startswith("def evict"))
    check("regex match excerpt", "max_gpu" in hit)

    # ---- No model was used anywhere ----
    banner("5. Determinism")
    check("no local model involved (context engine is pure code)",
          not hasattr(server, "gateway") and server.context_builder is not None)

    server.close()
    print("\n" + ("\033[1mALL PHASE-2 ACCEPTANCE CHECKS PASSED\033[0m" if ok
                  else "\033[1mSOME CHECKS FAILED\033[0m"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
