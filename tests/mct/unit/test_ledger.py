"""Ledger: hash chain, idempotency, monotonic sequence, epochs (design §14, §15)."""
import pytest

from hugpy_agent.mct.ledger import Ledger


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "mct.db")


def test_hash_chain_verifies(ledger):
    sid, epoch = ledger.create_session()
    for i in range(5):
        ledger.append_event(sid, "t_000001", epoch, f"type.{i}", "B.test")
    assert ledger.verify_chain(sid) is True


def test_hash_chain_detects_tampering(ledger):
    sid, epoch = ledger.create_session()
    ledger.append_event(sid, "t_000001", epoch, "a", "B.test")
    ledger.append_event(sid, "t_000001", epoch, "b", "B.test")
    ledger._db.execute("UPDATE events SET type='forged' WHERE type='a'")
    ledger._db.commit()
    assert ledger.verify_chain(sid) is False


def test_sequence_is_monotonic(ledger):
    sid, _ = ledger.create_session()
    seqs = [ledger.next_sequence(sid) for _ in range(4)]
    assert seqs == sorted(seqs) and len(set(seqs)) == 4


def test_turn_and_request_ids_match_schema_patterns(ledger):
    import re
    sid, _ = ledger.create_session()
    t = ledger.next_turn_id(sid)
    assert re.match(r"^t_[0-9]{6,}$", t)
    assert re.match(r"^pr_[0-9]{4,}$", ledger.next_request_id(sid, t))


def test_idempotency_stores_once(ledger):
    sid, _ = ledger.create_session()
    ledger.idempotency_put("k1", sid, {"v": 1})
    ledger.idempotency_put("k1", sid, {"v": 2})  # ignored
    assert ledger.idempotency_get("k1") == {"v": 1}


def test_render_recorded_once(ledger):
    sid, _ = ledger.create_session()
    assert not ledger.is_rendered(sid, "t_000001")
    ledger.record_render(sid, "t_000001", "abc")
    assert ledger.is_rendered(sid, "t_000001")


def test_new_epoch_changes_active(ledger):
    sid, e0 = ledger.create_session()
    e1 = ledger.new_epoch(sid, "A restart")
    assert e0 != e1
    assert ledger.active_epoch(sid) == e1
