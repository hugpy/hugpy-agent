"""Multi-session isolation under concurrency (design §7.4, §22 Load, exit cond.).

One broker serves many concurrent sessions (the realistic single-host model,
§14.2). We assert per-session isolation and hash-chain integrity under load, and
that a fault in one session never corrupts the others.
"""
import threading

import pytest

from hugpy_agent.mct import fake_a, recovery
from hugpy_agent.mct.errors import IsolationError
from hugpy_agent.mct.protocol import make_pointer
from hugpy_agent.mct.session import BrokerServer


def test_concurrent_sessions_isolated_and_intact(tmp_path):
    printed_counts: dict[int, int] = {}
    sids: dict[int, str] = {}
    server = BrokerServer(tmp_path, sink=lambda *_: None)

    def worker(idx, turns=3):
        sid = server.open_session(f"worker-{idx}")
        sids[idx] = sid
        for t in range(turns):
            server.session(sid).submit(f"worker {idx} turn {t}", fake_a.answer(f"ack {idx}"))
        printed_counts[idx] = turns

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for th in threads: th.start()
    for th in threads: th.join()

    assert len(sids) == 8
    for idx, sid in sids.items():
        assert server.ledger.verify_chain(sid)                 # per-session chain intact
        turns = server.ledger._db.execute(
            "SELECT COUNT(*) n FROM turns WHERE session_id=? AND state='Committed'",
            (sid,)).fetchone()["n"]
        assert turns == 3

    # cross-session isolation holds under concurrency
    a_objs = server.ledger.list_objects(sids[0], kinds=["operator_turn"])
    ptr = make_pointer(sids[0], a_objs[0]["object_id"])
    with pytest.raises(IsolationError):
        server.store.resolve(sids[1], ptr)
    server.close()


def test_fault_during_concurrency(tmp_path):
    """A fault in one session while others run: survivors commit, the crashed turn
    is resumable, every chain stays intact (exit condition)."""
    server = BrokerServer(tmp_path, sink=lambda *_: None)
    sids: dict[int, str] = {}

    def worker(idx):
        sid = server.open_session(f"ok-{idx}")
        sids[idx] = sid
        for t in range(3):
            server.session(sid).submit(f"turn {t}", fake_a.answer("ack"))

    def crasher():
        sid = server.open_session("crasher")
        sids[99] = sid

        def boom(client):
            client.read_operator_turn()
            raise RuntimeError("injected crash")
        try:
            server.session(sid).submit("will crash", boom)
        except RuntimeError:
            pass

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)] + \
              [threading.Thread(target=crasher)]
    for th in threads: th.start()
    for th in threads: th.join()

    report = recovery.reconcile(server)
    assert report.chains_ok                                    # nothing corrupted
    for i in range(4):
        assert server.ledger.verify_chain(sids[i])
    assert any(t["session_id"] == sids[99] for t in report.unfinished_turns)
    server.close()
