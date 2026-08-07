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

    svc.session.submit_via_claude = lambda prompt, model=None: svc.session.submit(
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
    svc.session.submit_via_claude = lambda prompt, model=None: seen.setdefault("p", prompt)
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

    svc.session.submit_via_claude = lambda prompt, model=None: _Bad()
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
