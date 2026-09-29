#!/usr/bin/env python3
"""Phase 4 acceptance demo — REAL Claude Code as A (design §22 Phase 4, §25).

Launches a headless, confined `claude` as the reasoning model A. A is restricted
by --strict-mcp-config + --allowedTools to B's resolve/submit_pull/respond tools
only: no filesystem, shell, or network. A must read the manifest, pull the
eviction evidence from a confined file snapshot, and answer.

This makes a real (billed) Claude Code call and is non-deterministic. No API key —
Claude Code uses its own auth.

Usage:  PYTHONPATH=src python3 tools/phase4_demo.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hugpy_agent.mct.claude_adapter import ClaudeCodeAdapter
from hugpy_agent.mct.session import BrokerConfig, BrokerServer


def banner(m): print(f"\n\033[1m=== {m} ===\033[0m")


def build_log(ws: Path) -> Path:
    d = ws / "workspace"
    d.mkdir(parents=True)
    lines = []
    for i in range(1, 900):
        if i == 512:
            lines.append(f"line {i}: DECISION evict worker gpu-02 reason=preempt alloc=A17 "
                         f"(max-gpu preference overridden under preempt)")
        else:
            lines.append(f"line {i}: heartbeat worker gpu-0{i % 3} ok")
    (d / "eviction.log").write_text("\n".join(lines) + "\n")
    return d


def main() -> int:
    ok = True
    def check(label, cond):
        nonlocal ok; ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")

    ws = Path(tempfile.mkdtemp(prefix="mct-phase4-"))
    printed = []
    server = BrokerServer(ws, sink=printed.append, config=BrokerConfig(use_model=True))

    if not ClaudeCodeAdapter(server).available():
        print("claude CLI not found — cannot run the live Phase 4 demo."); return 1

    logdir = build_log(ws)
    sess = server.session(server.open_session("demo"))
    sess.set_policy("Answer only from resolved evidence. Cite the log line you used.")
    sess.register_root("ws", str(logdir))
    sess.register_source_file("logs.eviction", "ws", "eviction.log")

    banner("Real Claude Code (A) traces the eviction through B")
    print("  launching confined `claude` ... (real call, ~15-60s)")
    r = sess.submit_via_claude(
        "Trace why worker gpu-02 was evicted even though the max-gpu preference "
        "should have kept it resident. Cite the exact log line.",
        model="sonnet", timeout=240)

    answer = r.body or (printed[0] if printed else "")
    print("\n  --- rendered answer (B -> operator) ---")
    print("  " + (answer or "<none>").replace("\n", "\n  "))
    print("  ---------------------------------------")

    check("turn committed", r.state == "Committed")
    check("B rendered A's answer exactly once", r.rendered and len(printed) == 1)
    check("answer identifies gpu-02 eviction", "gpu-02" in answer.lower())

    # Receipts: what A actually opened, recorded by B's transport (§12.2, invariant 6)
    receipts = server.ledger.receipts_for_turn(sess.session_id, r.turn_id)
    kinds_opened = {server.ledger.get_object(rr["object_id"])["kind"] for rr in receipts}
    print(f"\n  A opened {len(receipts)} objects; kinds={sorted(kinds_opened)}")
    check("A read the exact operator turn", "operator_turn" in kinds_opened)

    # A pulled the evidence through B (a pull was arbitrated + a snapshot exists)
    pulls = server.ledger._db.execute(
        "SELECT decision FROM pulls WHERE session_id=? AND turn_id=?",
        (sess.session_id, r.turn_id)).fetchall()
    snaps = server.ledger.list_objects(sess.session_id, kinds=["source_snapshot"])
    print(f"  pulls this turn: {[p['decision'] for p in pulls]}; source snapshots: {len(snaps)}")
    check("the eviction log was snapshotted via confined_io", len(snaps) == 1)

    server.close()
    print("\n" + ("\033[1mPHASE-4 ACCEPTANCE: PASSED\033[0m" if ok
                  else "\033[1mPHASE-4 ACCEPTANCE: SOME CHECKS FAILED\033[0m"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
