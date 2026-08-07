"""Real-time file-access tracking (§18.2): who touched which file, live.

The ledger records *that* a read happened, in object ids. This records *which
file* and *which side* — B for real host access, A for the mediated view — and
mirrors it into the rolling log so one tail shows both in true order.
"""
import json

from hugpy_agent.mct.fs_policy import add_root, set_allow
from hugpy_agent.mct.session import _ABinding


def _mk(tmp_path, broker, files: dict):
    d = tmp_path / "ws"
    d.mkdir()
    for name, body in files.items():
        (d / name).write_text(body)
    set_allow(broker.workspace_root, True)
    add_root(broker.workspace_root, "ws", str(d))
    sess = broker.session(broker.open_session("t"))
    broker.apply_fs_policy(sess, force=True)
    sess._active_turn = ("t_000001", "e1")
    return sess


def test_b_host_reads_are_recorded_with_path_and_size(tmp_path, broker):
    broker.gateway = lambda: None
    sess = _mk(tmp_path, broker, {"notes.md": "the retry policy is exponential\n"})

    sess._fs_broker_search("retry policy", limit=3)

    rows = broker.access.entries(sess.session_id)
    reads = [r for r in rows if r["actor"] == "B" and r["verb"] == "read"]
    assert any(r["target"] == "ws:notes.md" for r in reads)
    assert all(r.get("bytes", 0) > 0 for r in reads)
    # the scan itself is recorded too, carrying the query
    scans = [r for r in rows if r["verb"] == "scan"]
    assert scans and "retry" in scans[0]["detail"]


def test_a_reads_are_attributed_back_to_the_real_file(tmp_path, broker):
    broker.gateway = lambda: None
    sess = _mk(tmp_path, broker, {"notes.md": "the retry policy is exponential\n"})
    hits = sess._fs_broker_search("retry policy", limit=1)
    assert hits, "precondition: B must find the file"

    b = _ABinding(sess, "t_000001", "e1", hits[0]["pointer"], "0" * 64)
    b.resolve(hits[0]["pointer"], None, "read")

    a_reads = [r for r in broker.access.entries(sess.session_id) if r["actor"] == "A"]
    assert a_reads and a_reads[-1]["target"] == "ws:notes.md"


def test_objects_with_no_file_behind_them_log_their_kind(tmp_path, broker):
    """A resolving a manifest or pull result must still be recorded — as the
    object kind, since attributing it to a path would be a lie."""
    sess = _mk(tmp_path, broker, {"a.txt": "hello"})
    ptr = broker.store.commit(sess.session_id, b"{}", media_type="text/plain",
                              kind="context_manifest", provenance={}).pointer
    b = _ABinding(sess, "t_000001", "e1", ptr, "0" * 64)
    b.resolve(ptr, None, "read")

    last = broker.access.entries(sess.session_id)[-1]
    assert last["actor"] == "A" and "context_manifest" in last["target"]
    assert ":" not in last["target"].split()[0]  # not dressed up as a path


def test_act_channel_writes_and_execs_are_tracked(tmp_path, broker):
    sess = _mk(tmp_path, broker, {"a.txt": "hello"})
    target = tmp_path / "out.txt"

    sess.broker_act("write", path=str(target), content="x")
    sess.broker_act("exec", command="echo hi")
    sess.broker_act("edit", path=str(target), old="ZZZ", new="y")  # fails

    rows = broker.access.entries(sess.session_id)
    verbs = {r["verb"] for r in rows}
    assert {"write", "exec", "edit"} <= verbs
    failed = [r for r in rows if r["verb"] == "edit"][-1]
    assert failed["detail"].startswith("FAILED")


def test_access_is_mirrored_into_the_rolling_log(tmp_path, broker):
    """One `tail -f mct.log` must show events and file access interleaved."""
    sess = _mk(tmp_path, broker, {"a.txt": "hello"})
    sess.broker_act("exec", command="echo interleaved")

    text = broker.ledger.event_log_path.read_text()
    assert "echo interleaved" in text          # the access line
    assert "act.applied" in text               # and the ledger event


def test_jsonl_is_parseable_and_survives_reopen(tmp_path, broker):
    sess = _mk(tmp_path, broker, {"a.txt": "hello"})
    sess.broker_act("exec", command="echo persisted")

    raw = broker.access.path.read_text().strip().splitlines()
    assert raw and all(json.loads(ln) for ln in raw)   # every line valid JSON
    assert "no file access recorded yet" not in broker.access.render(sess.session_id)


def test_the_full_a_b_exchange_is_followable(tmp_path, broker):
    """The point of the tracker: read the turn top-to-bottom and see A ask, B
    work, B answer, A read. The ledger has this only as object ids."""
    broker.gateway = lambda: None
    sess = _mk(tmp_path, broker, {"nginx.conf": "listen 443 ssl;\n"})

    def a_program(client):
        client.read_operator_turn()
        out = client.submit_pull(need="the nginx config",
                                 target={"kind": "catalog-query", "query": "listen ssl"})
        if out.objects:
            client.resolve(out.objects[0]["object"])
        client.respond("done")

    assert sess.submit("where is nginx configured?", a_program).state == "Committed"

    rows = broker.access.entries(sess.session_id)
    seq = [(r["actor"], r["verb"]) for r in rows]
    assert ("A->B", "pull") in seq          # A asked, in its own words
    assert ("B", "scan") in seq             # B went looking
    assert any(a == "B->A" for a, _ in seq)  # B answered with a decision
    assert ("A->B", "respond") in seq       # A closed the turn
    # ordering must be truthful: A cannot ask after B has already answered
    assert seq.index(("A->B", "pull")) < seq.index(("B", "scan"))
    assert seq.index(("B", "scan")) < seq.index(("A->B", "respond"))


def test_pull_records_carry_as_query_and_need(tmp_path, broker):
    broker.gateway = lambda: None
    sess = _mk(tmp_path, broker, {"n.conf": "listen 443\n"})

    def a_program(client):
        client.read_operator_turn()
        client.submit_pull(need="why the port", target={"kind": "catalog-query",
                                                        "query": "listen 443"})
        client.respond("ok")

    sess.submit("q?", a_program)
    pull = [r for r in broker.access.entries(sess.session_id)
            if (r["actor"], r["verb"]) == ("A->B", "pull")][0]
    assert "listen 443" in pull["target"] and "why the port" in pull["detail"]


def test_cache_hits_are_serve_not_read(tmp_path, broker):
    """'B read it again' and 'B reused what it had' are different facts."""
    broker.gateway = lambda: None
    sess = _mk(tmp_path, broker, {"notes.md": "retry policy here\n"})

    sess._fs_broker_search("retry policy", limit=1)     # first: disk read
    before = broker.access.entries(sess.session_id)
    sess._fs_broker_search("retry policy", limit=1)     # again: cached
    after = broker.access.entries(sess.session_id)[len(before):]

    assert any(r["verb"] == "read" for r in before)
    assert any(r["verb"] == "serve" for r in after)
    assert not any(r["verb"] == "read" for r in after)  # no second disk touch


def test_search_peeks_are_recorded_separately_from_snapshots(tmp_path, broker):
    """A file B opened while ranking, then lifted into the store, is two real
    reads — reporting one would understate what B actually touched."""
    broker.gateway = lambda: None
    sess = _mk(tmp_path, broker, {"notes.md": "retry policy here\n"})

    sess._fs_broker_search("retry policy", limit=1)

    rows = [r for r in broker.access.entries(sess.session_id)
            if r["target"] == "ws:notes.md"]
    assert {"peek", "read"} <= {r["verb"] for r in rows}


def test_records_carry_the_pointer_to_what_was_exchanged(tmp_path, broker):
    """A log line that cannot be opened is a claim, not evidence."""
    broker.gateway = lambda: None
    sess = _mk(tmp_path, broker, {"nginx.conf": "listen 443 ssl;\n"})

    def a_program(client):
        client.read_operator_turn()
        out = client.submit_pull(need="the conf",
                                 target={"kind": "catalog-query", "query": "listen ssl"})
        if out.objects:
            client.resolve(out.objects[0]["object"])
        client.respond("done")

    sess.submit("q?", a_program)
    rows = broker.access.entries(sess.session_id)
    for actor, verb in [("A->B", "pull"), ("A->B", "respond"), ("B", "read")]:
        match = [r for r in rows if (r["actor"], r["verb"]) == (actor, verb)]
        assert match, f"no {actor} {verb} record"
        assert match[0].get("object"), f"{actor} {verb} carries no pointer"


def test_cat_returns_the_real_bytes(tmp_path, broker):
    broker.gateway = lambda: None
    sess = _mk(tmp_path, broker, {"nginx.conf": "listen 443 ssl;\n"})
    sess._fs_broker_search("listen ssl", limit=1)

    rec = [r for r in broker.access.entries(sess.session_id) if r["verb"] == "read"][0]
    body = broker.access.cat(sess.session_id, rec["object"])
    assert "listen 443 ssl;" in body


def test_cat_is_bounded_and_says_how_much_it_withheld(tmp_path, broker):
    broker.gateway = lambda: None
    sess = _mk(tmp_path, broker, {"big.txt": "listen ssl\n" + ("x" * 5000)})
    sess._fs_broker_search("listen ssl", limit=1)

    rec = [r for r in broker.access.entries(sess.session_id) if r["verb"] == "read"][0]
    body = broker.access.cat(sess.session_id, rec["object"], max_bytes=100)
    assert len(body) < 400 and "more bytes]" in body   # truncation is stated


def test_render_cat_inlines_content_under_the_line(tmp_path, broker):
    broker.gateway = lambda: None
    sess = _mk(tmp_path, broker, {"nginx.conf": "listen 443 ssl;\n"})
    sess._fs_broker_search("listen ssl", limit=1)

    plain = broker.access.render(sess.session_id)
    catted = broker.access.render(sess.session_id, cat=True)
    assert "listen 443 ssl;" not in plain          # summary stays a summary
    assert "listen 443 ssl;" in catted             # …until asked for
    assert "┌─" in catted and "└─" in catted


def test_cat_degrades_when_bytes_are_gone(tmp_path, broker):
    """An unreadable object must report itself, never crash the operator view."""
    from hugpy_agent.mct.access_log import AccessLog
    log = AccessLog(tmp_path / "a.jsonl", resolver=None)
    assert log.cat("s", "mct://broker/session/s_x/object/o_x") == "(no content)"

    def boom(_s, _p):
        raise OSError("vanished")
    log2 = AccessLog(tmp_path / "b.jsonl", resolver=boom)
    assert "unreadable" in log2.cat("s", "mct://broker/session/s_x/object/o_x")


def test_file_records_carry_the_host_path(tmp_path, broker):
    """A frontend relaying this log must be able to linkify a path without
    owning a broker handle or understanding MCT pointers — so the record has to
    be self-describing."""
    broker.gateway = lambda: None
    sess = _mk(tmp_path, broker, {"nginx.conf": "listen 443 ssl;\n"})
    sess._fs_broker_search("listen ssl", limit=1)

    import os
    from hugpy_agent.mct.access_log import FILE_VERBS
    rows = [r for r in broker.access.entries(sess.session_id) if r["verb"] in FILE_VERBS]
    assert rows
    for r in rows:
        assert r.get("path"), f"{r['verb']} record has no host path"
        assert os.path.isabs(r["path"])
        assert os.path.exists(r["path"])


def test_act_writes_record_the_path_they_touched(tmp_path, broker):
    sess = _mk(tmp_path, broker, {"a.txt": "x"})
    target = tmp_path / "written.txt"
    sess.broker_act("write", path=str(target), content="hi")
    sess.broker_act("exec", command="echo hi")

    rows = broker.access.entries(sess.session_id)
    write = [r for r in rows if r["verb"] == "write"][-1]
    ex = [r for r in rows if r["verb"] == "exec"][-1]
    assert write["path"] == str(target)
    assert not ex.get("path")          # a command is not a file


def test_the_rendered_line_links_the_path_not_just_the_id(tmp_path, broker, monkeypatch):
    """The clickable thing is the path — that is what an operator wants to open."""
    from hugpy_agent.mct import repl as mct_repl

    broker.gateway = lambda: None
    sess = _mk(tmp_path, broker, {"nginx.conf": "listen 443 ssl;\n"})
    sess._fs_broker_search("listen ssl", limit=1)
    row = [r for r in broker.access.entries(sess.session_id) if r["verb"] == "read"][0]

    monkeypatch.setattr(mct_repl.sys.stdout, "isatty", lambda: True, raising=False)
    line = mct_repl.LiveFeed(broker.access.path)._render(row)
    assert f"file://{row['path']}" in line          # the path is a live link
    assert "\033]8;;" in line                        # emitted as OSC 8


def test_render_separates_a_and_b_totals(tmp_path, broker):
    broker.gateway = lambda: None
    sess = _mk(tmp_path, broker, {"notes.md": "retry policy here\n"})
    hits = sess._fs_broker_search("retry policy", limit=1)
    b = _ABinding(sess, "t_000001", "e1", hits[0]["pointer"], "0" * 64)
    b.resolve(hits[0]["pointer"], None, "read")

    out = broker.access.render(sess.session_id)
    assert "per-file totals" in out
    # the same file, reached from both sides, is reported as two distinct rows
    assert out.count("ws:notes.md") >= 2
