"""C over HTTP: MCT behind an OpenAI-compatible endpoint.

One completion = one MCT turn, with the A↔B exchange relayed live before the
answer. This is what makes C's frontend swappable — a real TUI can be the
operator terminal without a line of frontend code here, and without A or B
knowing which frontend is attached.
"""
import json
import threading
import urllib.error
import urllib.request

import pytest

from hugpy_agent.console import build_mct_config
from hugpy_agent.mct.fs_policy import add_root, set_allow
from hugpy_agent.mct.openai_shim import MctChatService, serve

_PORT = 8793


@pytest.fixture
def shim(tmp_path):
    """A served MCT with a scripted A (no claude subprocess: hermetic)."""
    d = tmp_path / "ws"
    d.mkdir()
    (d / "nginx.conf").write_text("listen 443 ssl;\n")
    set_allow(tmp_path, True)
    add_root(tmp_path, "proj", str(d))

    httpd, svc = serve(str(tmp_path), port=_PORT)
    svc.server.gateway = lambda: None

    def a_program(client):
        client.read_operator_turn()
        out = client.submit_pull(need="the conf",
                                 target={"kind": "catalog-query", "query": "listen ssl"})
        if out.objects:
            client.resolve(out.objects[0]["object"])
        client.respond("It listens on 443.")

    svc.session.submit_via_claude = lambda prompt, model=None, **kw: svc.session.submit(
        prompt, a_program)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{_PORT}", svc
    httpd.shutdown()
    svc.close()


def _post(base, payload, timeout=180):
    req = urllib.request.Request(f"{base}/v1/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def _stream(base, payload):
    out = []
    for raw in _post(base, payload):
        line = raw.decode().strip()
        if not line.startswith("data: "):
            continue
        if line == "data: [DONE]":
            break
        c = json.loads(line[6:])["choices"][0]["delta"].get("content")
        if c:
            out.append(c)
    return "".join(out)


def test_models_advertises_mct(shim):
    base, _ = shim
    d = json.load(urllib.request.urlopen(f"{base}/v1/models", timeout=10))
    assert [m["id"] for m in d["data"]] == ["mct"]


def test_a_completion_is_one_mct_turn(shim):
    base, _ = shim
    body = json.load(_post(base, {"model": "mct", "messages": [
        {"role": "user", "content": "which port?"}]}))
    assert body["object"] == "chat.completion"
    assert "listens on 443" in body["choices"][0]["message"]["content"]
    assert body["usage"]["total_tokens"] >= 0


def test_streaming_relays_the_exchange_then_the_answer(shim):
    base, _ = shim
    text = _stream(base, {"model": "mct", "stream": True,
                          "messages": [{"role": "user", "content": "which port?"}]})
    assert "A->B pull" in text and "B    read" in text   # the work, live
    assert text.index("A->B pull") < text.index("listens on 443")  # before the answer
    assert "listens on 443" in text


def test_relay_carries_absolute_paths_for_linkification(shim, tmp_path):
    """A frontend can only linkify what it is given — relative paths are dead
    text in a TUI that has no idea what the root is."""
    base, _ = shim
    text = _stream(base, {"model": "mct", "stream": True,
                          "messages": [{"role": "user", "content": "which port?"}]})
    assert str(tmp_path / "ws" / "nginx.conf") in text


def test_only_the_last_user_message_is_submitted(shim):
    """The frontend resends its whole history every turn; replaying it would
    duplicate the context B has already curated — the exact cost MCT exists to
    avoid."""
    _, svc = shim
    seen = {}
    svc.session.submit_via_claude = lambda prompt, model=None, **kw: seen.setdefault("p", prompt)
    svc.turn(MctChatService._last_user([
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"}]))
    assert seen["p"] == "second"


def test_content_parts_are_flattened():
    assert MctChatService._last_user(
        [{"role": "user", "content": [{"type": "text", "text": "a"},
                                      {"type": "text", "text": "b"}]}]) == "ab"


def test_empty_prompt_is_a_400(shim):
    base, _ = shim
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(base, {"model": "mct", "messages": [{"role": "assistant", "content": "x"}]})
    assert e.value.code == 400


def test_unknown_route_is_a_404(shim):
    base, _ = shim
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"{base}/v1/embeddings", timeout=10)
    assert e.value.code == 404


def test_a_failure_is_reported_not_papered_over(shim):
    """B must never answer in A's place (§5.2) — a failed turn says so."""
    base, svc = shim

    class _Bad:
        state, body, error, tokens = "Failed", None, "A unavailable", None

    svc.session.submit_via_claude = lambda prompt, model=None, **kw: _Bad()
    body = json.load(_post(base, {"model": "mct", "messages": [
        {"role": "user", "content": "hi"}]}))
    content = body["choices"][0]["message"]["content"]
    assert "did not answer" in content and "A unavailable" in content


def test_opencode_config_denies_its_own_tools():
    """OpenCode here is C — a prompt and a display. The actor that edits files
    is A, through B's audited act channel. A second unaudited actor on the same
    machine would make 'who touched this file' unanswerable."""
    cfg = build_mct_config("http://127.0.0.1:8770/v1")
    assert cfg["model"] == "mct/mct"
    assert cfg["provider"]["mct"]["options"]["baseURL"] == "http://127.0.0.1:8770/v1"
    assert "apiKey" not in cfg["provider"]["mct"]["options"]   # no secret implied
    assert set(cfg["permission"].values()) == {"deny"}


def test_native_tool_postures_are_graded():
    """The question is not 'safe vs dangerous' — B is unrestricted and A can
    already reach the host via submit_act. It is whether the mediation still
    means anything after the grant."""
    from hugpy_agent.mct.claude_adapter import tool_policy

    none_a, none_d = tool_policy("none")
    off_a, off_d = tool_policy("off_host")
    all_a, all_d = tool_policy("all")

    mct = {"mcp__mct__resolve", "mcp__mct__submit_pull", "mcp__mct__submit_act",
           "mcp__mct__submit_ask", "mcp__mct__respond", "mcp__mct__todo"}
    assert set(none_a) == mct
    # off_host bypasses nothing: no host reach is granted
    assert {"WebSearch", "WebFetch", "TodoWrite"} <= set(off_a)
    assert not ({"Read", "Grep", "Bash", "Edit", "Write"} & set(off_a))
    # all grants host reach
    assert {"Read", "Grep", "Bash", "Edit", "Write"} <= set(all_a)
    # subagents are never granted: a child would inherit tools B cannot see
    for d in (none_d, off_d, all_d):
        assert {"Task", "Agent"} <= set(d)
    # every posture keeps the MCT tools
    for a in (none_a, off_a, all_a):
        assert mct <= set(a)


def test_granting_host_tools_is_recorded_not_silent(tmp_path):
    """The access log only sees brokered work. If A can read around it, the log
    would under-report — and a log that quietly under-reports is worse than no
    log. So the grant itself is an event."""
    from hugpy_agent.mct.claude_adapter import ClaudeCodeAdapter
    from hugpy_agent.mct.session import BrokerServer

    server = BrokerServer(tmp_path, sink=lambda *_: None)
    sess = server.session(server.open_session("t"))
    try:
        ad = ClaudeCodeAdapter(server)
        ad.available = lambda: False          # stop before spawning claude
        ad.run_turn(sess, "t_000001", "e1", "mct://x", native_tools="all")
        # available() short-circuits, so drive the record path directly
        server.access.record("A", "unmediated", "native host tools granted",
                             session=sess.session_id, turn="t_000001")
        rows = server.access.entries(sess.session_id)
        assert any(r["verb"] == "unmediated" for r in rows)
    finally:
        server.close()


def test_the_system_prompt_still_steers_to_the_cheap_path():
    from hugpy_agent.mct import claude_adapter as ca

    assert "EXPENSIVE path" in ca._SYSTEM_NATIVE
    assert "submit_pull" in ca._SYSTEM_NATIVE     # the cheap path stays default


# --- structured search: B as A's search agent -------------------------------

def _search_session(tmp_path, files):
    from hugpy_agent.mct.fs_policy import add_root, set_allow
    from hugpy_agent.mct.session import BrokerServer
    d = tmp_path / "src"
    d.mkdir()
    for name, body in files.items():
        (d / name).write_text(body)
    server = BrokerServer(tmp_path, sink=lambda *_: None)
    server.gateway = lambda: None
    set_allow(tmp_path, True)
    add_root(tmp_path, "proj", str(d))
    sess = server.session(server.open_session("t"))
    server.apply_fs_policy(sess, force=True)
    sess._active_turn = ("t_000001", "e1")
    return server, sess


def test_all_terms_is_an_intersection(tmp_path):
    server, sess = _search_session(tmp_path, {
        "both.md": "nginx and ssl together\n",
        "one.md": "nginx only\n"})
    try:
        got = [h["name"] for h in sess._fs_structured_search({"all": ["nginx", "ssl"]})]
        assert got == ["fs:proj:both.md"]
    finally:
        server.close()


def test_any_terms_is_a_union(tmp_path):
    server, sess = _search_session(tmp_path, {
        "a.md": "nginx here\n", "b.md": "ssl here\n", "c.md": "neither\n"})
    try:
        got = {h["name"] for h in sess._fs_structured_search({"any": ["nginx", "ssl"]})}
        assert got == {"fs:proj:a.md", "fs:proj:b.md"}
    finally:
        server.close()


def test_none_is_a_veto_not_a_penalty(tmp_path):
    """'Filter out X' has to mean gone. A ranked-down result A still has to read
    and reject is not a filter — and A cannot reason over a set it cannot trust."""
    server, sess = _search_session(tmp_path, {
        "keep.md": "nginx config\n", "drop.md": "nginx config DEPRECATED\n"})
    try:
        got = [h["name"] for h in sess._fs_structured_search(
            {"all": ["nginx"], "none": ["DEPRECATED"]})]
        assert got == ["fs:proj:keep.md"]
    finally:
        server.close()


def test_extension_and_path_filters_narrow_the_corpus(tmp_path):
    server, sess = _search_session(tmp_path, {
        "a.ts": "route handler\n", "b.md": "route handler\n"})
    try:
        got = [h["name"] for h in sess._fs_structured_search(
            {"any": ["route"], "ext": [".ts"]})]
        assert got == ["fs:proj:a.ts"]
    finally:
        server.close()


def test_date_window_bounds_the_result(tmp_path):
    import os
    import time
    server, sess = _search_session(tmp_path, {"old.md": "nginx\n", "new.md": "nginx\n"})
    try:
        old = tmp_path / "src" / "old.md"
        stamp = time.time() - 400 * 24 * 3600          # ~13 months ago
        os.utime(old, (stamp, stamp))
        recent = sess._fs_structured_search(
            {"any": ["nginx"], "modified_after": "2026-01-01"})
        assert [h["name"] for h in recent] == ["fs:proj:new.md"]
        assert not sess._fs_structured_search(
            {"any": ["nginx"], "modified_before": "1990-01-01"})
    finally:
        server.close()


def test_a_malformed_date_widens_rather_than_silently_empties(tmp_path):
    """A typo in a filter must not look like 'nothing matched'."""
    from hugpy_agent.mct.session import _parse_when
    assert _parse_when("not-a-date") is None
    assert _parse_when("") is None
    assert _parse_when("2026-08-01") > 0

    server, sess = _search_session(tmp_path, {"a.md": "nginx\n"})
    try:
        assert sess._fs_structured_search(
            {"any": ["nginx"], "modified_after": "garbage"})
    finally:
        server.close()


def test_a_directive_with_no_terms_returns_nothing(tmp_path):
    server, sess = _search_session(tmp_path, {"a.md": "nginx\n"})
    try:
        assert sess._fs_structured_search({"ext": [".md"]}) == []
    finally:
        server.close()


def test_search_is_a_valid_pull_target():
    from hugpy_agent.mct.protocol import validate_against
    validate_against("pull-request-v1", {
        "schema": "mct.pull-request/1", "session_id": "s_A", "turn_id": "t_000001",
        "epoch": "e_A", "request_id": "pr_0001", "need": "find it",
        "target": {"kind": "search", "spec": {
            "all": ["foryou"], "none": ["test"], "ext": [".ts"],
            "modified_after": "2026-07-01", "limit": 5}}})
