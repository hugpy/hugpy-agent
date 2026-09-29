#!/usr/bin/env python3
"""Phase 3 acceptance demo — proves the design §22 Phase-3 exit condition:

    local-model output can IMPROVE RANKING but CANNOT authorize,
    CANNOT delete originals, and CANNOT become untraceable.

Uses the deterministic offline local model, so it runs with no fleet. A real
hugpy_agent Gateway/RAG embedder plugs into the same LocalModel interface later.

Usage:  PYTHONPATH=src python3 tools/phase3_demo.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hugpy_agent.mct import fake_a
from hugpy_agent.mct.protocol import make_pointer
from hugpy_agent.mct.session import BrokerConfig, BrokerServer


def banner(m): print(f"\n\033[1m=== {m} ===\033[0m")


def main() -> int:
    ok = True
    def check(label, cond):
        nonlocal ok; ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")

    ws = tempfile.mkdtemp(prefix="mct-phase3-")
    printed = []
    server = BrokerServer(ws, sink=printed.append, config=BrokerConfig(use_model=True))
    sess = server.session(server.open_session("demo"))
    print(f"local model: {server.model.name}")

    # ---- 1. semantic retrieval improves ranking --------------------------
    banner("1. Semantic ranking (synonym the lexical scorer would miss)")
    sess.submit("DECISION: gpu-02 eviction happened due to preempt on alloc A17",
                fake_a.answer("noted"))
    r = sess.submit("why was gpu-02 removed?", fake_a.answer("preempt"))  # 'removed' ≈ 'eviction'
    dm = [row for row in r.context_trace["included"] if row["role"] == "decision_memory"]
    for row in dm:
        c = row["components"]
        print(f"    decision fragment: R_lex={c.get('R_lex')} R_sem={c.get('R_sem')} R={c.get('R')}")
    check("prior decision retrieved for a synonym query", bool(dm))
    check("semantic component lifted relevance above lexical",
          any(row["components"].get("R_sem", 0) > row["components"].get("R_lex", 0) for row in dm))

    # ---- 2. model extraction beyond regex --------------------------------
    banner("2. Model extraction (no 'DECISION:' prefix)")
    sess.submit("The incident was caused by a preempt on alloc A17.", fake_a.answer("ack"))
    decisions = server.compaction.facts(sess.session_id, kind="decision")
    check("unprefixed decision extracted by the model",
          any("preempt" in f.text and "incident" in f.text.lower() for f in decisions))

    # ---- 3. episodic summary, drafted + traceable ------------------------
    banner("3. Model-drafted episodic summary is fully traceable")
    base = decisions[0]
    summ = server.compaction.summarize(sess.session_id, [base.object_id])
    graph = server.compaction.provenance_graph(sess.session_id, summ.object_id)
    prov = json.loads(server.ledger.get_object(summ.object_id)["provenance"])
    print(f"    summary provenance.model = {prov.get('model')}")
    check("summary is not an original", graph["is_original"] is False)
    check("summary traces back to source objects", bool(graph["sources"]))
    check("drafting model recorded in provenance", prov.get("model") == server.model.name)

    # ---- 4. conflict detection retains both ------------------------------
    banner("4. Conflict detection keeps both facts")
    src = server.store.commit(sess.session_id, b"x", media_type="text/plain", kind="operator_turn")
    server.compaction.record_fact(sess.session_id, "max-gpu keeps the worker resident",
                                  kind="decision", source_object_ids=[src.object_id])
    server.compaction.record_fact(sess.session_id, "max-gpu bypassed, worker evicted under preempt",
                                  kind="decision", source_object_ids=[src.object_id])
    before = len(server.ledger.facts(sess.session_id, kind="decision"))
    conflicts = server.compaction.detect_conflicts(sess.session_id, kind="decision")
    after = len(server.ledger.facts(sess.session_id, kind="decision"))
    check("a conflict was detected", bool(conflicts))
    check("no fact deleted by conflict detection", before == after)

    # ---- 5. near-duplicate clustering never deletes ----------------------
    banner("5. Near-duplicate clustering annotates, never deletes")
    a = server.compaction.record_fact(sess.session_id, "gpu-02 evicted by preempt alloc A17",
                                      kind="decision", source_object_ids=[src.object_id])
    b = server.compaction.record_fact(sess.session_id, "gpu-02 was evicted due to preempt, alloc A17",
                                      kind="decision", source_object_ids=[src.object_id])
    pairs = server.compaction.cluster_near_duplicates(sess.session_id, kind="decision", threshold=0.8)
    check("near-duplicates detected", bool(pairs))
    check("both near-duplicate originals still resolve",
          bool(server.store.resolve(sess.session_id, a.pointer))
          and bool(server.store.resolve(sess.session_id, b.pointer)))

    # ---- 6. the model CANNOT authorize -----------------------------------
    banner("6. The model cannot authorize (authorization stays deterministic)")
    def bypass(client):
        client.read_operator_turn()
        out = client.submit_pull(need="steal", target={"kind": "object",
                                 "object": make_pointer("s_OTHER", "o_X")})
        bypass.decision = out.decision
        client.respond("handled")
    sess.submit("try cross-session", bypass)
    check("cross-session pull denied regardless of the model", bypass.decision == "denied")

    server.close()
    print("\n" + ("\033[1mALL PHASE-3 ACCEPTANCE CHECKS PASSED\033[0m" if ok
                  else "\033[1mSOME CHECKS FAILED\033[0m"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
