"""The act channel (§6.2): B does the work, A drives and sees only the summary.

MCT bounds what enters A's context, not what A can accomplish. ``submit_act``
lets A instruct B to change files and run commands; B applies them on the host
and returns a compact result plus a pointer to the full output. Every action is
recorded in the ledger — an audit trail, not a permission gate.
"""
import json


def _events(broker, session_id, prefix="act."):
    rows = broker.ledger.list_events(session_id) if hasattr(broker.ledger, "list_events") else None
    if rows is None:  # fall back to the durable table
        import sqlite3
        con = sqlite3.connect(str(broker.root / "mct.db"))
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "select type, actor from events where session_id=? order by rowid_global",
            (session_id,))]
    return [r for r in rows if str(r["type"]).startswith(prefix)]


def test_write_creates_the_file_and_reports_size(tmp_path, broker):
    sess = broker.session(broker.open_session("t"))
    target = tmp_path / "handler.py"

    out = sess.broker_act("write", path=str(target), content="def feed(q):\n    return q\n")

    assert out["ok"] is True
    assert target.read_text() == "def feed(q):\n    return q\n"
    assert out["bytes"] == len("def feed(q):\n    return q\n")


def test_edit_replaces_and_snapshots_the_prior_bytes(tmp_path, broker):
    sess = broker.session(broker.open_session("t"))
    target = tmp_path / "handler.py"
    target.write_text("return FEED + search(q)\n")

    out = sess.broker_act("edit", path=str(target),
                          old="FEED + search(q)", new="search(q) if q else FEED")

    assert out["ok"] is True and out["replaced"] == 1
    assert target.read_text() == "return search(q) if q else FEED\n"
    # the pre-edit content survives as an immutable snapshot (invariant 8)
    prior = broker.store.resolve(sess.session_id, out["before"]).decode()
    assert prior == "return FEED + search(q)\n"


def test_ambiguous_edit_fails_closed_until_all_is_set(tmp_path, broker):
    sess = broker.session(broker.open_session("t"))
    target = tmp_path / "dup.txt"
    target.write_text("x\nx\n")

    out = sess.broker_act("edit", path=str(target), old="x", new="y")
    assert out["ok"] is False and "not unique" in out["error"]
    assert target.read_text() == "x\nx\n"  # nothing was written

    out = sess.broker_act("edit", path=str(target), old="x", new="y", all=True)
    assert out["ok"] is True and out["replaced"] == 2
    assert target.read_text() == "y\ny\n"


def test_missing_old_string_is_an_error_not_a_silent_noop(tmp_path, broker):
    sess = broker.session(broker.open_session("t"))
    target = tmp_path / "a.txt"
    target.write_text("hello\n")

    out = sess.broker_act("edit", path=str(target), old="nope", new="x")

    assert out["ok"] is False and "not found" in out["error"]
    assert target.read_text() == "hello\n"


def test_exec_returns_status_inline_and_spools_the_rest(broker):
    sess = broker.session(broker.open_session("t"))

    out = sess.broker_act("exec", command="echo hello-from-b")
    assert out["ok"] is True and out["returncode"] == 0
    assert "hello-from-b" in out["output"]

    # A long run must NOT import its transcript into A's window: the inline body
    # is capped and the full text is reachable by pointer.
    big = sess.broker_act("exec", command="seq 1 5000")
    assert big["truncated"] is True
    assert len(big["output"]) <= sess._ACT_INLINE
    full = broker.store.resolve(sess.session_id, big["full_output"]).decode()
    assert "5000" in full and len(full) > len(big["output"])


def test_failing_command_is_data_not_an_exception(broker):
    sess = broker.session(broker.open_session("t"))
    out = sess.broker_act("exec", command="exit 3")
    assert out["ok"] is False and out["returncode"] == 3


def test_every_action_is_recorded_in_the_ledger(tmp_path, broker):
    sess = broker.session(broker.open_session("t"))
    target = tmp_path / "f.txt"
    sess.broker_act("write", path=str(target), content="1")
    sess.broker_act("edit", path=str(target), old="ZZZ", new="2")   # fails

    kinds = [e["type"] for e in _events(broker, sess.session_id)]
    assert kinds.count("act.requested") == 2       # both attempts recorded
    assert "act.applied" in kinds and "act.failed" in kinds


def test_unknown_kind_is_refused_as_data(broker):
    sess = broker.session(broker.open_session("t"))
    out = sess.broker_act("launch_missiles", target="moon")
    assert out["ok"] is False and "unknown act kind" in out["error"]


def test_act_is_exposed_to_a_and_allowlisted(broker):
    """The capability is worthless if A is not told it exists, or if the adapter
    strips the call. Both wirings must agree on the tool name."""
    from hugpy_agent.mct import claude_adapter, mct_mcp_server

    names = [t["name"] for t in mct_mcp_server.TOOLS]
    assert "submit_act" in names
    assert "mcp__mct__submit_act" in claude_adapter._ALLOWED
    assert "submit_act" in claude_adapter._SYSTEM
