"""GC, quota, and service-account preflight (design §17.2, §13.2, §17)."""
import pytest

from hugpy_agent.mct import gc, hardening
from hugpy_agent.mct.errors import HardeningError, QuotaError
from hugpy_agent.mct.session import BrokerConfig, BrokerServer


# --- garbage collection ----------------------------------------------------

def test_gc_collects_orphan_but_retains_referenced(broker):
    sess = broker.session(broker.open_session("t"))
    ref = broker.store.commit(sess.session_id, b"live object", media_type="text/plain", kind="x")
    # an orphan file with no ledger row (e.g. crash between rename and ledger commit)
    orphan = broker.store._path_for_digest("ab" * 32)
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")

    report = gc.garbage_collect(broker, dry_run=True)
    assert "ab" * 32 in report.collectable
    assert ref.sha256 not in report.collectable        # referenced object retained
    assert orphan.exists()                              # dry run deletes nothing

    gc.garbage_collect(broker, dry_run=False)
    assert not orphan.exists()                          # swept to quarantine
    assert broker.store.resolve(sess.session_id, ref.pointer) == b"live object"


def test_gc_respects_legal_hold(broker):
    sess = broker.session(broker.open_session("t"))
    held = broker.store._path_for_digest("cd" * 32)
    held.parent.mkdir(parents=True, exist_ok=True)
    held.write_bytes(b"held")
    report = gc.garbage_collect(broker, dry_run=True, legal_hold={"cd" * 32})
    assert "cd" * 32 not in report.collectable


# --- per-session disk quota ------------------------------------------------

def test_quota_fails_closed_and_preserves_existing(tmp_path):
    server = BrokerServer(tmp_path, sink=lambda *_: None,
                          config=BrokerConfig(session_quota_bytes=1000))
    sess = server.session(server.open_session("t"))
    ok = server.store.commit(sess.session_id, b"a" * 500, media_type="text/plain", kind="x")
    with pytest.raises(QuotaError):
        server.store.commit(sess.session_id, b"b" * 600, media_type="text/plain", kind="x")
    # the earlier object is untouched (§17: preserve existing on quota pressure)
    assert server.store.resolve(sess.session_id, ok.pointer) == b"a" * 500
    server.close()


# --- service-account preflight (registry row 15) ---------------------------

def test_preflight_flags_root_equivalent_groups(monkeypatch):
    monkeypatch.setattr(hardening, "_group_names", lambda: {"users", "docker"})
    monkeypatch.setattr(hardening.os, "getuid", lambda: 1000)
    violations = hardening.check_service_account()
    assert any("docker" in v for v in violations)
    with pytest.raises(HardeningError):
        hardening.assert_hardened(enforce=True)
    # dev mode surfaces but does not raise
    assert hardening.assert_hardened(enforce=False)


def test_preflight_clean_account_passes(monkeypatch):
    monkeypatch.setattr(hardening, "_group_names", lambda: {"mct-broker"})
    monkeypatch.setattr(hardening.os, "getuid", lambda: 1000)
    assert hardening.check_service_account() == []
    assert hardening.assert_hardened(enforce=True) == []
