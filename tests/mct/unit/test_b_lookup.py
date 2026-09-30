"""b_lookup — B answers lookups by string search, never by inference.

Operator ask 2026-09-29: with an EMPTY state block every question to B was
still a Coder-Next call. These tests pin the contract: classification is
deterministic, an empty state never reaches the model, searches return
pointers, only synthesis over non-empty state calls the (mocked) model, and
every reply ends with the (timestamp · tokens · duration · model) line.
"""
from __future__ import annotations

import io
import json
import re
from unittest import mock

import pytest

from hugpy_agent.mct import b_lookup as bl
from hugpy_agent.mct.b_lookup import classify, respond

META_RE = re.compile(r"\(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ · tokens (\d+) · \d+\.\d{3}s · ([^)]+)\)$")


class NeverChat:
    """A model client that fails the test the moment it is called."""
    calls = 0

    def __call__(self, messages):
        NeverChat.calls += 1
        raise AssertionError("model client was called for a lookup")


class FakeResult:
    def __init__(self, text, ok=True, est_tokens=42, error=None):
        self.text, self.ok, self.est_tokens, self.error = text, ok, est_tokens, error


def _meta(reply: str):
    m = META_RE.search(reply.strip().splitlines()[-1])
    assert m, f"no metadata line on: {reply!r}"
    return int(m.group(1)), m.group(2)


def _populate(session, broker):
    """A non-empty state: policy, two catalog entries, one fact, one ledger
    object, and a rolling-log line."""
    session.set_policy("keep answers terse; never show raw tokens")
    session.register_source("notes.md", "gpu worker ae evicted klein at 03:12\nsecond line\n")
    session.register_source("plan.txt", "phase 4 wires the fleet embedder\n")
    src = broker.ledger.list_objects(session.session_id)[0]["object_id"]
    broker.compaction.record_fact(session.session_id, "we chose coder-next over klein",
                                  kind="decision", source_object_ids=[src])
    broker.store.commit(session.session_id, b"operator asked about the wg1 tunnel",
                        media_type="text/plain", kind="operator_message", provenance={})
    p = broker.ledger.event_log_path
    with open(p, "a") as fh:
        fh.write("event: pull granted for notes.md\n")


# ── classifier ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", ["ok", "thanks", "Thank you!", "got it", "ty", "noted."])
def test_classify_ack(text):
    assert classify(text).kind == "ack"


@pytest.mark.parametrize("text", ["help", "/help", "what can you do?", "commands"])
def test_classify_help(text):
    assert classify(text).kind == "help"


@pytest.mark.parametrize("text", [
    "does this take Coder-Next's inference? isn't it a standard string search?",
    "what model are you using",
    "are you calling the model for this?",
])
def test_classify_meta(text):
    assert classify(text).kind == "meta"


@pytest.mark.parametrize("text,fields", [
    ("what is in the catalog?", ["catalog"]),
    ("show me the policy", ["policy"]),
    ("what do you remember? any facts?", ["memory"]),
    ("how many tokens have we spent", ["tokens"]),
    ("what's in the ledger", ["log"]),
    ("status", ["all"]),
    ("what do you have", ["all"]),
    ("list the sources and the policy", ["policy", "catalog"]),
])
def test_classify_state(text, fields):
    it = classify(text)
    assert it.kind == "state"
    assert set(fields) <= set(it.fields)


@pytest.mark.parametrize("text,query", [
    ("where is the klein eviction?", "the klein eviction"),
    ("find evicted", "evicted"),
    ("which entries mention wg1", "wg1"),
    ("search for 'phase 4'", "phase 4"),
    ("do you have anything about the tunnel", "the tunnel"),
    ("grep coder-next", "coder-next"),
])
def test_classify_search(text, query):
    it = classify(text)
    assert it.kind == "search"
    assert it.query == query


@pytest.mark.parametrize("text", [
    "summarize what you know about the eviction and judge whether it was right",
    "rewrite the policy so it is friendlier",
    "why did the worker evict klein, in your opinion?",
])
def test_classify_synth(text):
    assert classify(text).kind == "synth"


# ── empty state → no network, ever ───────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "what is in the catalog?", "status", "where is klein", "find anything",
    "summarize everything you know", "why is the sky blue",
])
def test_empty_state_never_calls_model(session, broker, text):
    NeverChat.calls = 0
    out = respond(text, session, broker, {"last": None}, chat=NeverChat(),
                  model_name="never", base="http://x", busy=lambda: False)
    assert NeverChat.calls == 0
    assert out["mode"] in ("empty",)
    assert out["model"] == "lookup" and out["tokens"] == 0
    assert "state is empty" in out["reply"]
    assert "/policy" in out["reply"]           # says what would populate it
    tokens, model = _meta(out["reply"])
    assert (tokens, model) == (0, "lookup")


def test_ack_help_meta_are_lookups_even_when_empty(session, broker):
    for text, mode in (("ok", "ack"), ("help", "help"), ("what model do you use", "meta")):
        out = respond(text, session, broker, chat=NeverChat(), model_name="qwen-x",
                      base="http://central", busy=lambda: False)
        assert out["mode"] == mode and out["model"] == "lookup" and out["tokens"] == 0
    assert "qwen-x" in out["reply"] and "no inference" in out["reply"]


# ── state + search over a fixture ────────────────────────────────────────────
def test_state_lookup_lists_fields_without_model(session, broker):
    _populate(session, broker)
    out = respond("what is in the catalog and the policy?", session, broker,
                  chat=NeverChat(), busy=lambda: False)
    assert out["mode"] == "state"
    assert "notes.md" in out["reply"] and "plan.txt" in out["reply"]
    assert "mct://" in out["reply"]            # pointers, not just names
    assert "keep answers terse" in out["reply"]
    assert _meta(out["reply"]) == (0, "lookup")


def test_search_returns_pointers_from_every_store(session, broker):
    _populate(session, broker)
    out = respond("which entries mention klein", session, broker,
                  chat=NeverChat(), busy=lambda: False)
    assert out["mode"] == "search"
    r = out["reply"]
    assert re.search(r"catalog:notes\.md \(mct://[^)]+\) line 1: gpu worker ae evicted klein", r)
    assert "memory:[decision]" in r and "coder-next over klein" in r
    out2 = respond("where is the wg1 tunnel?", session, broker, chat=NeverChat(),
                   busy=lambda: False)
    assert "ledger:operator_message" in out2["reply"]
    out3 = respond("find 'pull granted'", session, broker, chat=NeverChat(),
                   busy=lambda: False)
    assert re.search(r"log:.*mct\.log:\d+: event: pull granted", out3["reply"])
    out4 = respond("find terse", session, broker, chat=NeverChat(), busy=lambda: False)
    assert out4["reply"].startswith("1 match") and "policy:" in out4["reply"]


def test_search_phrase_then_terms_and_no_match(session, broker):
    _populate(session, broker)
    out = respond("find fleet embedder wiring", session, broker, chat=NeverChat(),
                  busy=lambda: False)
    assert "catalog:plan.txt" in out["reply"]      # phrase missed, term hit
    out = respond("find zzz-nothing", session, broker, chat=NeverChat(), busy=lambda: False)
    assert out["reply"].startswith("No match for 'zzz-nothing'")
    assert "2 catalog entries" in out["reply"]


# ── synthesis over non-empty state reaches the (mocked) model ────────────────
def test_synth_calls_model_once_with_grounded_prompt(session, broker):
    _populate(session, broker)
    chat = mock.Mock(return_value=FakeResult("B says: klein was evicted for capacity."))
    out = respond("summarize the eviction and judge whether it was right", session,
                  broker, history=[{"role": "user", "content": "earlier"},
                                   {"role": "assistant", "content": "reply"}],
                  chat=chat, model_name="Qwen~Qwen3-Coder-Next-GGUF", busy=lambda: False)
    assert chat.call_count == 1
    msgs = chat.call_args.args[0]
    assert msgs[0]["role"] == "system" and msgs[0]["content"].startswith(bl.B_SYSMSG)
    assert "catalog (2): notes.md, plan.txt" in msgs[0]["content"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert out["mode"] == "synth" and out["model"] == "Qwen~Qwen3-Coder-Next-GGUF"
    assert out["tokens"] == 42
    assert _meta(out["reply"]) == (42, "Qwen~Qwen3-Coder-Next-GGUF")
    assert out["reply"].startswith("B says:")


def test_synth_gateway_failure_degrades_to_readout(session, broker):
    _populate(session, broker)
    chat = mock.Mock(return_value=FakeResult("", ok=False, error="HTTP 503"))
    out = respond("judge the plan", session, broker, chat=chat, model_name="m",
                  busy=lambda: False)
    assert out["mode"] == "offline" and out["offline"] is True
    assert "HTTP 503" in out["reply"] and "catalog (2)" in out["reply"]
    assert _meta(out["reply"]) == (0, "lookup")


def test_synth_skipped_when_queue_busy(session, broker):
    _populate(session, broker)
    chat = mock.Mock()
    out = respond("judge the plan", session, broker, chat=chat, model_name="m",
                  busy=lambda: True)
    assert chat.call_count == 0 and out["mode"] == "busy"
    assert "queue is backed up" in out["reply"]


def test_synth_one_in_flight_per_workspace(session, broker):
    _populate(session, broker)
    st = bl.collect_state(session, broker)
    holder = bl._Inflight(st.state_dir)
    assert holder.acquire()
    try:
        chat = mock.Mock()
        out = respond("judge the plan", session, broker, chat=chat, model_name="m",
                      busy=lambda: False)
        assert chat.call_count == 0 and out["mode"] == "busy"
        assert "already answering" in out["reply"]
    finally:
        holder.release()
    chat = mock.Mock(return_value=FakeResult("ok"))
    out = respond("judge the plan", session, broker, chat=chat, model_name="m",
                  busy=lambda: False)
    assert chat.call_count == 1 and out["mode"] == "synth"


def test_queue_busy_probe_reads_counts(monkeypatch):
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(bl.urllib.request, "urlopen",
                        lambda req, timeout: R(json.dumps({"active": [], "counts": {"waiting": 2}}).encode()))
    assert bl.queue_busy("https://dev.hugpy.ai/api") is True
    monkeypatch.setattr(bl.urllib.request, "urlopen",
                        lambda req, timeout: R(json.dumps({"counts": {"waiting": 0}}).encode()))
    assert bl.queue_busy("https://dev.hugpy.ai/api") is False

    def boom(req, timeout):
        raise OSError("no central")
    monkeypatch.setattr(bl.urllib.request, "urlopen", boom)
    assert bl.queue_busy("https://dev.hugpy.ai/api") is False   # probe failure = not busy


# ── the one-shot stdin/stdout contract the station execs ─────────────────────
def test_b_answer_one_shot_lookup_never_touches_gateway(tmp_path, monkeypatch):
    from hugpy_agent import gateway
    from hugpy_agent.mct import b_answer

    def no_chat(self, *a, **k):
        raise AssertionError("Gateway.chat called on a lookup")
    monkeypatch.setattr(gateway.Gateway, "chat", no_chat)
    payload = json.dumps({"workspace": str(tmp_path), "text": "what do you have?"})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    assert b_answer.main() == 0
    out = json.loads(buf.getvalue())
    assert out["mode"] == "empty" and out["model"] == "lookup" and out["tokens"] == 0
    assert out["offline"] is False and "state is empty" in out["reply"]
    assert out["meta"].endswith("· lookup)")


def test_repl_b_prompt_uses_lookup(session, broker):
    from hugpy_agent.mct.repl import _b_prompt, _b_state_text
    _populate(session, broker)
    with mock.patch.object(bl, "default_chat", side_effect=AssertionError("model built")):
        body = _b_prompt("which entries mention klein", session, broker, {"last": None})
    assert "catalog:notes.md" in body and body.rstrip().endswith("· lookup)")
    assert "policy: keep answers terse" in _b_state_text(session, broker, {"last": None})


# ── station log findings (findings.json next to mct.log) ─────────────────────
FINDINGS_DOC = {
    "schema": "station.findings.v1", "updated": 1790730000, "detector": "log_findings",
    "findings": [
        {"key": "a1b2c3d4e5f6", "kind": "crash_loop", "severity": "high",
         "source": "hugpy-station-web.service", "locus": "ae-hugpy", "count": 120,
         "first_seen": 1790720000, "last_seen": 1790729900,
         "signature": "unit failed repeatedly",
         "sample_lines": ["2026-09-29T18:42:23-05:00 ae systemd[1]: hugpy-station-web.service: "
                          "Scheduled restart job, restart counter is at 120."],
         "suggested_action": ""},
        {"key": "0f9e8d7c6b5a", "kind": "rate_limit_429", "severity": "high", "source": "hugpy",
         "locus": "ae-hugpy", "count": 7, "first_seen": 1790729000, "last_seen": 1790729800,
         "signature": "ERROR upstream N Too Many Requests", "sample_lines": ["... 429 ..."],
         "suggested_action": "wait for the window or switch the seat's model"},
    ],
    "emitted": [],
}


def _write_findings(broker, doc=FINDINGS_DOC):
    from pathlib import Path
    p = Path(broker.ledger.event_log_path).parent / bl.FINDINGS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc))
    return p


@pytest.mark.parametrize("text", [
    "what's broken?", "what is failing", "any errors?", "are there crash loops",
    "any 429s", "show me the findings", "anything wrong on ae?",
])
def test_classify_findings(text):
    assert classify(text).kind == "findings"


def test_findings_answered_by_lookup_even_when_state_empty(session, broker):
    path = _write_findings(broker)
    NeverChat.calls = 0
    out = respond("what's broken?", session, broker, chat=NeverChat(), busy=lambda: False)
    assert NeverChat.calls == 0
    assert out["mode"] == "findings" and out["model"] == "lookup" and out["tokens"] == 0
    assert "2 finding(s)" in out["reply"]
    assert "crash_loop · ae-hugpy · hugpy-station-web.service ×120" in out["reply"]
    assert "action: wait for the window" in out["reply"]
    assert _meta(out["reply"]) == (0, "lookup")
    assert str(path) == bl.collect_state(session, broker).findings_path


def test_findings_absent_or_empty_say_so(session, broker):
    out = respond("any errors?", session, broker, chat=NeverChat(), busy=lambda: False)
    assert out["mode"] == "findings" and "no log findings" in out["reply"]
    _write_findings(broker, dict(FINDINGS_DOC, findings=[]))
    out = respond("any errors?", session, broker, chat=NeverChat(), busy=lambda: False)
    assert "No running problems" in out["reply"]


def test_search_covers_findings(session, broker):
    _write_findings(broker)
    out = respond("find hugpy-station-web", session, broker, chat=NeverChat(), busy=lambda: False)
    assert out["mode"] == "search"
    assert "finding:a1b2c3d4e5f6 crash_loop hugpy-station-web.service ×120" in out["reply"]
    assert _meta(out["reply"]) == (0, "lookup")
