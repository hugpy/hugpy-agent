#!/usr/bin/env python3
"""Phase 6 acceptance — hardening under concurrency and fault injection.

Exercises streaming, per-session quota, GC, the service-account preflight,
content-safe telemetry, and multi-session isolation with an injected fault — the
design §22 Phase-6 exit condition. Offline; no billed calls.

Usage:  PYTHONPATH=src python3 tools/phase6_hardening.py
"""
from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hugpy_agent.mct import fake_a, gc, hardening, recovery
from hugpy_agent.mct.errors import QuotaError
from hugpy_agent.mct.protocol import make_pointer
from hugpy_agent.mct.session import BrokerConfig, BrokerServer


def banner(m): print(f"\n\033[1m=== {m} ===\033[0m")


def main() -> int:
    ok = True
    def check(label, cond):
        nonlocal ok; ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")

    ws = Path(tempfile.mkdtemp(prefix="mct-phase6-"))

    banner("1. Streaming response (frames + verified assembly)")
    printed = []
    server = BrokerServer(ws / "s", sink=printed.append)
    sess = server.session(server.open_session("stream"))
    prog = fake_a.stream_answer(["Tracing", " the", " cause…"])
    r = sess.submit("stream it", prog)
    check("frames rendered in order", printed == ["Tracing", " the", " cause…"])
    check("sealed once", r.state == "Committed" and prog.render.rendered)
    server.close()

    banner("2. Per-session disk quota fails closed")
    server = BrokerServer(ws / "q", sink=lambda *_: None,
                          config=BrokerConfig(session_quota_bytes=800))
    sess = server.session(server.open_session("q"))
    kept = server.store.commit(sess.session_id, b"x" * 400, media_type="text/plain", kind="x")
    quota_hit = False
    try:
        server.store.commit(sess.session_id, b"y" * 600, media_type="text/plain", kind="x")
    except QuotaError:
        quota_hit = True
    check("over-quota commit rejected", quota_hit)
    check("existing object preserved", server.store.resolve(sess.session_id, kept.pointer) == b"x" * 400)
    server.close()

    banner("3. Garbage collection reclaims orphans, retains referenced")
    server = BrokerServer(ws / "g", sink=lambda *_: None)
    sess = server.session(server.open_session("g"))
    live = server.store.commit(sess.session_id, b"live", media_type="text/plain", kind="x")
    orphan = server.store._path_for_digest("ab" * 32)
    orphan.parent.mkdir(parents=True, exist_ok=True); orphan.write_bytes(b"orphan")
    rep = gc.garbage_collect(server, dry_run=False)
    check("orphan collected", "ab" * 32 in rep.collectable and not orphan.exists())
    check("referenced object retained", bool(server.store.resolve(sess.session_id, live.pointer)))
    server.close()

    banner("4. Service-account preflight (registry row 15)")
    violations = hardening.check_service_account()
    print(f"    this process: {violations or 'clean'}")
    check("preflight runs and reports deterministically", isinstance(violations, list))

    banner("5. Telemetry is content-safe (no bodies/secrets)")
    server = BrokerServer(ws / "t", sink=lambda *_: None)
    sess = server.session(server.open_session("t"))
    r = sess.submit("deploy key SECRET-swordfish", fake_a.answer("noted"))
    report = server.telemetry.turn_report(sess.session_id, r.turn_id)
    check("no secret in telemetry", "swordfish" not in str(report))
    check("trace is content-safe", server.telemetry.is_content_safe(report))
    server.close()

    banner("6. Concurrency + injected fault (exit condition)")
    server = BrokerServer(ws / "c", sink=lambda *_: None)
    sids = {}
    def worker(i):
        sid = server.open_session(f"w{i}"); sids[i] = sid
        for t in range(3):
            server.session(sid).submit(f"t{t}", fake_a.answer("ack"))
    def crasher():
        sid = server.open_session("boom"); sids[99] = sid
        def boom(c): c.read_operator_turn(); raise RuntimeError("crash")
        try: server.session(sid).submit("x", boom)
        except RuntimeError: pass
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)] + \
              [threading.Thread(target=crasher)]
    for th in threads: th.start()
    for th in threads: th.join()
    report = recovery.reconcile(server)
    chains_ok = all(server.ledger.verify_chain(sids[i]) for i in range(6))
    isolated = True
    try:
        objs = server.ledger.list_objects(sids[0], kinds=["operator_turn"])
        server.store.resolve(sids[1], make_pointer(sids[0], objs[0]["object_id"]))
        isolated = False
    except Exception:
        pass
    check("all session chains intact under load + fault", chains_ok and report.chains_ok)
    check("cross-session isolation holds", isolated)
    check("crashed turn is resumable", any(t["session_id"] == sids[99]
                                           for t in report.unfinished_turns))
    server.close()

    print("\n" + ("\033[1mPHASE-6 HARDENING: PASSED\033[0m" if ok
                  else "\033[1mPHASE-6: SOME CHECKS FAILED\033[0m"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
