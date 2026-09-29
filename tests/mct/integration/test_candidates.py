"""Candidate selection to the frontier (§11.2): B ranks, A chooses.

An ambiguous catalog query returns decision 'candidates' with a committed
slate object; A resolves the slate, picks, and pulls the winner by pointer.
A dominant match still short-circuits to a direct result (no round-trip).
"""
import json

from hugpy_agent.mct.fs_policy import add_root, set_allow


def _no_finder(broker):
    broker.gateway = lambda: None  # hermetic: skip the central abstract-search


def test_ambiguous_query_returns_ranked_slate(tmp_path, broker):
    _no_finder(broker)
    d = tmp_path / "ws"
    d.mkdir()
    (d / "alpha.md").write_text("the scheduler retry policy is exponential")
    (d / "beta.md").write_text("retry policy: give up after 3 attempts")
    set_allow(broker.workspace_root, True)
    add_root(broker.workspace_root, "ws", str(d))
    sess = broker.session(broker.open_session("t"))

    got = {}

    def a(client):
        client.read_operator_turn()
        out = client.submit_pull(need="retry policy",
                                 target={"kind": "catalog-query", "query": "retry policy"})
        got["decision"] = out.decision
        slate = json.loads(client.resolve(out.objects[0]["object"]))
        got["slate"] = slate
        # A chooses: pull the winner by its pointer (kind=object)
        pick = slate["candidates"][0]
        out2 = client.submit_pull(need="the chosen source",
                                  target={"kind": "object", "object": pick["pointer"]})
        got["decision2"] = out2.decision
        got["body"] = client.resolve(out2.objects[0]["object"]).decode()
        client.respond("done")

    assert sess.submit("what is the retry policy?", a).state == "Committed"
    assert got["decision"] == "candidates"
    names = [c["name"] for c in got["slate"]["candidates"]]
    assert any("alpha.md" in n for n in names) and any("beta.md" in n for n in names)
    assert all(c["snippet"] for c in got["slate"]["candidates"])
    assert got["decision2"] == "exact"
    assert "retry" in got["body"]


def test_dominant_match_skips_the_slate(tmp_path, broker):
    _no_finder(broker)
    d = tmp_path / "ws"
    d.mkdir()
    (d / "backoff.md").write_text("retry backoff capped at 30 seconds")
    (d / "shopping.md").write_text("eggs, milk")
    set_allow(broker.workspace_root, True)
    add_root(broker.workspace_root, "ws", str(d))
    sess = broker.session(broker.open_session("t"))

    got = {}

    def a(client):
        client.read_operator_turn()
        out = client.submit_pull(need="backoff cap",
                                 target={"kind": "catalog-query", "query": "retry backoff"})
        got["decision"] = out.decision
        got["body"] = client.resolve(out.objects[0]["object"]).decode()
        client.respond("done")

    assert sess.submit("backoff?", a).state == "Committed"
    assert got["decision"] == "exact"       # one clear winner: no round-trip
    assert "30 seconds" in got["body"]


def test_exact_name_still_short_circuits(tmp_path, broker):
    _no_finder(broker)
    d = tmp_path / "ws"
    d.mkdir()
    (d / "gpu.log").write_text("line 42: DECISION evict worker gpu-02")
    sess = broker.session(broker.open_session("t"))
    sess.register_root("ws", str(d))
    sess.register_source_file("logs.gpu", "ws", "gpu.log")

    got = {}

    def a(client):
        client.read_operator_turn()
        out = client.submit_pull(need="the log",
                                 target={"kind": "catalog-query", "query": "logs gpu"})
        got["decision"] = out.decision
        client.respond("done")

    assert sess.submit("log?", a).state == "Committed"
    assert got["decision"] == "exact"


def test_catalog_only_slate_with_steward_off(tmp_path, broker):
    """Ambiguity among registered catalog sources yields a slate even with the
    frontier-fs gate closed (fail-closed default): no root walking involved."""
    _no_finder(broker)
    d = tmp_path / "ws"
    d.mkdir()
    (d / "a.txt").write_text("server timeout is 10 seconds")
    (d / "b.txt").write_text("client timeout is 5 seconds")
    sess = broker.session(broker.open_session("t"))
    sess.register_root("ws", str(d))
    sess.register_source_file("cat.a", "ws", "a.txt")
    sess.register_source_file("cat.b", "ws", "b.txt")

    got = {}

    def a(client):
        client.read_operator_turn()
        out = client.submit_pull(need="timeouts",
                                 target={"kind": "catalog-query", "query": "timeout"})
        got["decision"] = out.decision
        got["slate_raw"] = client.resolve(out.objects[0]["object"]).decode()
        client.respond("done")

    assert sess.submit("timeout?", a).state == "Committed"
    assert got["decision"] == "candidates"
    # invariant 5: no host path may leak into slate names or snippets
    assert str(tmp_path) not in got["slate_raw"]


def test_binary_file_is_never_a_candidate(tmp_path, broker):
    """A filename hit on a binary must not beat a real content answer."""
    _no_finder(broker)
    d = tmp_path / "ws"
    d.mkdir()
    (d / "rollback.bin").write_bytes(b"\x00\x01\x02" * 5000)
    (d / "runbook.md").write_text("to rollback: redeploy the previous tag")
    set_allow(broker.workspace_root, True)
    add_root(broker.workspace_root, "ws", str(d))
    sess = broker.session(broker.open_session("t"))

    got = {}

    def a(client):
        client.read_operator_turn()
        out = client.submit_pull(need="how to rollback",
                                 target={"kind": "catalog-query", "query": "rollback"})
        got["decision"] = out.decision
        got["body"] = client.resolve(out.objects[0]["object"]).decode()
        client.respond("done")

    assert sess.submit("rollback?", a).state == "Committed"
    assert got["decision"] == "exact"
    assert "previous tag" in got["body"]      # the runbook, not NUL soup


def test_large_file_content_is_still_searchable(tmp_path, broker):
    """Files above the peek cap are truncation-peeked, not skipped (read_prefix)."""
    _no_finder(broker)
    d = tmp_path / "ws"
    d.mkdir()
    (d / "bigdoc.md").write_text("the retry policy is exponential backoff\n"
                                 + ("filler line\n" * 30000))  # ~360 KB
    (d / "other.md").write_text("eggs, milk")
    set_allow(broker.workspace_root, True)
    add_root(broker.workspace_root, "ws", str(d))
    sess = broker.session(broker.open_session("t"))

    got = {}

    def a(client):
        client.read_operator_turn()
        out = client.submit_pull(need="retry policy",
                                 target={"kind": "catalog-query", "query": "retry policy"})
        got["decision"] = out.decision
        got["body"] = client.resolve(out.objects[0]["object"]).decode()
        client.respond("done")

    assert sess.submit("policy?", a).state == "Committed"
    assert got["decision"] == "exact"
    assert "exponential backoff" in got["body"]


def test_slate_is_a_committed_pointed_object(tmp_path, broker):
    _no_finder(broker)
    d = tmp_path / "ws"
    d.mkdir()
    (d / "one.txt").write_text("service timeout is 10s")
    (d / "two.txt").write_text("client timeout is 5s")
    set_allow(broker.workspace_root, True)
    add_root(broker.workspace_root, "ws", str(d))
    sess = broker.session(broker.open_session("t"))

    def a(client):
        client.read_operator_turn()
        out = client.submit_pull(need="timeouts",
                                 target={"kind": "catalog-query", "query": "timeout"})
        assert out.decision == "candidates"
        client.respond("done")

    assert sess.submit("timeouts?", a).state == "Committed"
    slates = broker.ledger.list_objects(sess.session_id, kinds=["candidate_list"])
    assert len(slates) == 1  # every outcome is a committed, pointed object
