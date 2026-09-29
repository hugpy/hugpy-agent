#!/usr/bin/env python3
"""Phase 1 acceptance demo — proves the design §22 Phase-1 exit condition:

    a scripted fake A completes, pulls, retries, crashes, resumes,
    and never bypasses B.

Runs entirely LLM-free over the real transport core. Each scenario prints what it
proved. Exit code 0 iff every assertion holds.

Usage:  PYTHONPATH=src python3 tools/phase1_demo.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hugpy_agent.mct import fake_a, recovery
from hugpy_agent.mct.a_adapter import AAdapterClient
from hugpy_agent.mct.session import BrokerServer

# A big-ish "eviction log" source, echoing the worked example (design §25).
LOG = "".join(
    f"line {i}: heartbeat worker gpu-0{i % 3} ok\n" if i not in (42, 43) else
    (f"line {i}: DECISION evict worker gpu-02 reason=preempt alloc=A17\n" if i == 42 else
     f"line {i}: NOTE max-gpu preference bypassed for gpu-02 under preempt\n")
    for i in range(1, 200)
)


def banner(msg: str) -> None:
    print(f"\n\033[1m=== {msg} ===\033[0m")


def crashing_program(text: str):
    """A responds (B renders), then the process 'crashes' before B commits the turn."""
    def program(client: AAdapterClient) -> None:
        client.read_operator_turn()
        render = client.respond(text)
        program.pending = (client.turn_id, client.epoch,
                           client.manifest_pointer, render)  # adapter still holds the pointer
        # capture the response manifest pointer the adapter would resend
        program.resend = (client.turn_id, client.epoch, client._b.response_manifest,
                          f"{client.session_id}:{client.turn_id}:response:1")
        raise RuntimeError("simulated B crash after render, before commit")
    return program


def main() -> int:
    printed: list[str] = []
    sink = lambda body: printed.append(body)  # noqa: E731 — capture "terminal" output
    workspace = tempfile.mkdtemp(prefix="mct-phase1-")
    ok = True

    def check(label: str, cond: bool) -> None:
        nonlocal ok
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")

    # ---- 1. completes -----------------------------------------------------
    banner("1. A completes a normal turn")
    server = BrokerServer(workspace, sink=sink)
    session_id = server.open_session("demo")
    sess = server.session(session_id)
    r1 = sess.submit("hello, are you there?", fake_a.answer("Yes — mediated and present."))
    check("turn committed", r1.state == "Committed")
    check("rendered exactly once", r1.rendered and len(printed) == 1)
    check("receipt sealed", r1.receipt is not None)

    # ---- 2. pulls ---------------------------------------------------------
    banner("2. A pulls missing context through B")
    sess.register_source("logs.worker-gpu-02.2026-08-01", LOG)
    before = len(printed)
    r2 = sess.submit(
        "Trace why gpu-02 was evicted despite the max-gpu preference.",
        fake_a.answer_after_pull(
            need="first eviction decision for gpu-02",
            query="logs gpu-02",
            preferred_form="lines 40-44",
        ),
    )
    check("turn committed", r2.state == "Committed")
    check("rendered once", len(printed) == before + 1)
    check("answer cites reduced evidence", "reduced" in printed[-1] and "evict worker gpu-02" in printed[-1])

    # ---- 3. retries (idempotent display) ----------------------------------
    banner("3. A retries the same response.ready (duplicate)")
    before = len(printed)
    prog = fake_a.double_respond("Idempotent answer.")
    r3 = sess.submit("say it once", prog)
    first, second = prog.renders
    check("first render wrote output", first.rendered and not first.already_rendered)
    check("second render suppressed", (not second.rendered) and second.already_rendered)
    check("only one line printed", len(printed) == before + 1)

    # ---- 4. never bypasses B ---------------------------------------------
    banner("4. A tries to bypass B — all fail closed")
    prog = fake_a.try_bypass()
    sess.submit("attempt bypass", prog)
    v = prog.violations
    check("host path rejected (mct.protocol)", v[0] == "mct.protocol")
    check("cross-session pointer rejected (mct.isolation)", v[1] == "mct.isolation")
    check("cross-session pull denied", v[2] == "denied")

    # ---- 5. B never answers for A ----------------------------------------
    banner("5. A stays silent — B does NOT substitute (invariant 10)")
    before = len(printed)
    r5 = sess.submit("say nothing", fake_a.silent())
    check("turn failed, no answer invented", r5.state == "Failed" and not r5.rendered)
    check("nothing printed", len(printed) == before)

    # ---- 6. crashes & resumes --------------------------------------------
    banner("6. B crashes after render, restarts, resumes idempotently")
    before = len(printed)
    prog = crashing_program("Answer emitted just before the crash.")
    crashed = False
    try:
        sess.submit("answer then crash", prog)
    except RuntimeError:
        crashed = True
    check("simulated crash raised", crashed)
    check("output rendered once before crash", len(printed) == before + 1)
    turn_id, epoch, manifest_ptr, key = prog.resend

    server.close()  # drop all in-memory state
    server2 = BrokerServer(workspace, sink=sink)  # restart over same durable state
    report = recovery.reconcile(server2)
    check("hash chain intact after restart", report.chains_ok)
    check("interrupted turn detected", any(t["turn_id"] == turn_id for t in report.unfinished_turns))

    sess2 = server2.session(session_id)
    resumed = sess2.on_response_ready(turn_id, epoch, manifest_ptr, key)  # adapter resends pointer
    check("resend did NOT re-render", resumed["already_rendered"] and not resumed["rendered"])
    check("no duplicate output after resume", len(printed) == before + 1)
    check("turn now committed", server2.ledger.get_turn(session_id, turn_id)["state"] == "Committed")
    server2.close()

    print("\n" + ("\033[1mALL PHASE-1 ACCEPTANCE CHECKS PASSED\033[0m" if ok
                  else "\033[1mSOME CHECKS FAILED\033[0m"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
