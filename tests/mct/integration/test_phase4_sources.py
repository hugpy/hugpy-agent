"""Phase 4: filesystem-backed pulls via confined snapshots (design §13.4, §25)."""
import pytest

from hugpy_agent.mct import fake_a


def _write_log(tmp_path):
    d = tmp_path / "workspace"
    d.mkdir()
    (d / "gpu.log").write_text(
        "\n".join(f"line {i}: heartbeat ok" if i not in (42,) else
                  "line 42: DECISION evict worker gpu-02 reason=preempt alloc=A17"
                  for i in range(1, 100)) + "\n")
    return d


def test_pull_snapshots_a_real_file_and_reduces(tmp_path, broker):
    d = _write_log(tmp_path)
    sess = broker.session(broker.open_session("t"))
    sess.register_root("ws", str(d))
    sess.register_source_file("logs.gpu", "ws", "gpu.log")

    r = sess.submit("why evicted?",
                    fake_a.answer_after_pull(need="eviction line", query="logs gpu",
                                             preferred_form="match evict ctx 1"))
    assert r.state == "Committed"
    assert "evict worker gpu-02" in broker.printed[-1]
    assert "reduced" in broker.printed[-1]


def test_snapshot_is_an_immutable_source_object(tmp_path, broker):
    d = _write_log(tmp_path)
    sess = broker.session(broker.open_session("t"))
    sess.register_root("ws", str(d))
    sess.register_source_file("logs.gpu", "ws", "gpu.log")
    sess.submit("look", fake_a.answer("noted"))  # materializes on manifest build
    snaps = broker.ledger.list_objects(sess.session_id, kinds=["source_snapshot"])
    assert len(snaps) == 1  # the file was snapshotted into the store


def test_pull_for_unregistered_name_is_not_found(tmp_path, broker):
    d = _write_log(tmp_path)
    sess = broker.session(broker.open_session("t"))
    sess.register_root("ws", str(d))
    # NOTE: no register_source_file -> the file is not in the catalog

    def a(client):
        client.read_operator_turn()
        out = client.submit_pull(need="secret", target={"kind": "catalog-query",
                                                        "query": "etc shadow passwd"})
        assert out.decision == "not_found"       # A cannot name arbitrary host paths
        client.respond("nothing")

    assert sess.submit("probe", a).state == "Committed"


def test_source_byte_budget_exhaustion(tmp_path, broker):
    d = tmp_path / "ws"
    d.mkdir()
    (d / "a.txt").write_text("A" * 2000)
    broker.pull_broker.budget.max_source_bytes = 1000
    sess = broker.session(broker.open_session("t"))
    sess.register_root("ws", str(d))
    sess.register_source_file("cat.a", "ws", "a.txt")

    def a(client):
        client.read_operator_turn()
        out = client.submit_pull(need="a", target={"kind": "catalog-query", "query": "cat a"})
        assert out.decision == "budget_exhausted"
        client.respond(out.decision)

    sess.submit("hammer", a)
    assert "budget_exhausted" in broker.printed[-1]
