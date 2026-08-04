"""Object store: commit/resolve, integrity, dedup, selectors (design §7)."""
import pytest

from hugpy_agent.mct.errors import IntegrityError, IsolationError, NotFoundError
from hugpy_agent.mct.ledger import Ledger
from hugpy_agent.mct.objects import ObjectStore


@pytest.fixture
def store(tmp_path):
    ledger = Ledger(tmp_path / "mct.db")
    return ObjectStore(tmp_path, ledger), ledger


def test_commit_resolve_roundtrip(store):
    s, _ = store
    ref = s.commit("s_A", b"hello", media_type="text/plain", kind="x")
    assert s.resolve("s_A", ref.pointer) == b"hello"
    assert ref.sha256 == __import__("hashlib").sha256(b"hello").hexdigest()


def test_digest_verification_quarantines_tampered_object(store):
    s, _ = store
    ref = s.commit("s_A", b"trustworthy", media_type="text/plain", kind="x")
    # tamper the stored bytes directly on disk
    path = s._path_for_digest(ref.sha256)
    import os
    os.chmod(path, 0o640)
    path.write_bytes(b"tampered!!!!")
    with pytest.raises(IntegrityError):
        s.resolve("s_A", ref.pointer)  # invariant 4 — fail closed
    assert not path.exists()  # moved to quarantine


def test_cross_session_resolve_is_isolation_error(store):
    s, _ = store
    ref = s.commit("s_A", b"secret", media_type="text/plain", kind="x")
    with pytest.raises(IsolationError):
        s.resolve("s_B", ref.pointer)  # adversarial case 5


def test_physical_dedup_same_bytes_one_file(store):
    s, ledger = store
    r1 = s.commit("s_A", b"same", media_type="text/plain", kind="x")
    r2 = s.commit("s_A", b"same", media_type="text/plain", kind="x")
    assert r1.object_id != r2.object_id       # opaque IDs differ (§7.4)
    assert r1.sha256 == r2.sha256              # but bytes dedup by digest
    assert s._path_for_digest(r1.sha256).exists()


def test_line_selector(store):
    s, _ = store
    ref = s.commit("s_A", b"a\nb\nc\nd\n", media_type="text/plain", kind="x")
    assert s.resolve("s_A", ref.pointer, selector="lines 2-3") == b"b\nc\n"


def test_resolve_unknown_object(store):
    s, _ = store
    from hugpy_agent.mct.protocol import make_pointer
    with pytest.raises(NotFoundError):
        s.resolve("s_A", make_pointer("s_A", "o_DOESNOTEXIST"))
