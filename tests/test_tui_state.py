"""Pure reducer tests: one per h26 §2.1 row, plus held/net/stale/re-baseline."""
import _bootstrap  # noqa: F401
import unittest

from helpers import fixture

from hugpy_agent.serve_client import Event, EventPage, QueueView, Receipt, Roster, Session
from hugpy_agent.serve_client import abstract_claude as ac
from hugpy_agent.serve_client import hugpy_serve as hs
from hugpy_agent.tui import state as st

CS = "cs-synthetic00000000000000000000"


def ev(kind, seq=1, **kw):
    fields = dict(seq=seq, ts=float(seq), session_id=CS, kind=kind)
    fields.update(kw)
    return Event(**fields)


def page(events, busy=False, queue=None, cursor=None, source="console", truncated=False):
    cursor = str(events[-1].seq if events else 0) if cursor is None else cursor
    return EventPage(events, busy, queue, cursor, source, truncated)


def model(sid=CS):
    roster = Roster(roles=[Session(id=sid, role="keeper", label="Keeper", backend="hugpy", model="m")])
    return st.reduce(st.Model(kind="abstract-serve", base="http://x"), {"type": "roster", "roster": roster})


def feed(m, events, **kw):
    return st.reduce(m, {"type": "events", "sid": m.active_sid, "page": page(events, **kw), "now": 100.0})


class TableTests(unittest.TestCase):
    """The synthetic fixture holds every one of the 17 console types."""

    def setUp(self):
        raw = fixture("console_events_synthetic")
        self.events = [ac.normalize_event(r) for r in raw["events"]]
        self.m0 = model()

    def test_reduce_is_pure(self):
        m1 = feed(self.m0, self.events[:5])
        self.assertEqual(self.m0.blocks, [])
        self.assertEqual(len(m1.blocks), 3)      # user, system, assistant(coalesced)
        m2 = feed(m1, self.events[5:6])
        self.assertEqual(len(m1.blocks), 3)
        self.assertEqual(len(m2.blocks), 4)

    def test_user_status_system_text(self):
        m = feed(self.m0, self.events[:5])
        kinds = [b.kind for b in m.blocks]
        self.assertEqual(kinds, ["user", "system", "assistant"])
        self.assertEqual(m.notice, "compiling…")           # status -> notice only (open turn)
        self.assertEqual(feed(self.m0, self.events[:5] + [self.events[21]]).notice, "")   # closed turn: silent
        self.assertEqual(m.blocks[1].text, "claude-opus-4-8 · 3 tools")
        self.assertEqual(m.blocks[1].detail, "Bash\nRead\nEdit")
        self.assertEqual(m.blocks[2].text, "Sure, reading the file.")
        self.assertTrue(m.blocks[2].streaming)
        self.assertEqual(m.lane().cursor, "5")

    def test_thinking_closes_stream_and_collapses(self):
        m = feed(self.m0, self.events[:6])
        self.assertFalse(m.blocks[2].streaming)
        self.assertEqual(m.blocks[3].kind, "thinking")
        self.assertNotIn(3, m.lane().expanded)

    def test_claude_tool_card_and_result(self):
        m = feed(self.m0, self.events[:8])
        card = m.blocks[-1]
        self.assertEqual((card.kind, card.name, card.text), ("tool", "Read", "Read /tmp/x"))
        self.assertEqual(card.detail, '{"file_path": "/tmp/x"}')
        self.assertEqual(card.output, "line 1\nline 2")
        self.assertIs(card.ok, True)

    def test_gpt_tool_started_delta_completed(self):
        m = feed(self.m0, self.events[:11])
        card = m.blocks[-1]
        self.assertEqual(card.name, "commandExecution")
        self.assertEqual(card.output, "total 0\n")           # delta appended
        self.assertIs(card.ok, True)                          # completed marks the same card
        self.assertIn('"exit": 0', card.detail)

    def test_hugpy_tool_use_and_full_result(self):
        m = feed(self.m0, self.events[:13])
        card = m.blocks[-1]
        self.assertEqual((card.name, card.text), ("shell", "shell uptime"))
        self.assertEqual(card.output, "up 3 days, load 0.1")
        self.assertIs(card.ok, True)

    def test_approval_question_resolved(self):
        m = feed(self.m0, self.events[:15])
        self.assertEqual([a.kind for a in m.approvals], ["approval", "question"])
        self.assertTrue(m.approval_open)
        self.assertEqual(m.approvals[0].title, "Run command")
        self.assertEqual(m.approvals[0].params["command"], "rm -rf build")
        self.assertEqual(m.approvals[1].options[1], "Approve all shell this run")
        self.assertEqual([b.kind for b in m.blocks[-2:]], ["approval", "question"])
        m = feed(m, self.events[15:16])                     # approval_resolved r1
        self.assertEqual([a.request_id for a in m.approvals], ["q1"])
        self.assertEqual(m.blocks[-2].decision, "resolved")
        self.assertTrue(m.approval_open)
        m = st.reduce(m, {"type": "approval_answered", "request_id": "q1", "decision": "Approve"})
        self.assertEqual(m.approvals, [])
        self.assertFalse(m.approval_open)
        self.assertEqual(m.blocks[-1].decision, "Approve")
        # Re-raised hugpy pending question (same request_id) is not duplicated.
        m = feed(m, [ev("question", seq=99, request_id="q1", text="again", options=["Approve"])])
        self.assertEqual(len([b for b in m.blocks if b.kind == "question"]), 1)

    def test_historical_approvals_never_open_the_modal(self):
        # First load of a session with an old, unanswered hugpy question that a
        # later `done` already closed (seen live on the keeper row, h26 smoke).
        m = feed(self.m0, [self.events[14], self.events[21]])          # question q1, then done
        self.assertEqual(m.approvals, [])
        self.assertFalse(m.approval_open)
        self.assertEqual((m.blocks[-2].kind, m.blocks[-2].decision), ("question", "expired"))
        # Same question with nothing after it but older than the 300 s timeout.
        old = st.reduce(self.m0, {"type": "events", "sid": CS, "page": page([self.events[14]]),
                                  "now": 1.0, "wall": self.events[14].ts + 301})
        self.assertEqual(old.approvals, [])
        fresh = st.reduce(self.m0, {"type": "events", "sid": CS, "page": page([self.events[14]]),
                                    "now": 1.0, "wall": self.events[14].ts + 10})
        self.assertEqual(len(fresh.approvals), 1)
        # hugpy serve re-raises `pending` on every poll: always live.
        pend = ev("question", seq=7, request_id="p1", text="?", options=["Approve"], meta={"pending": True})
        live = st.reduce(self.m0, {"type": "events", "sid": CS, "page": page([pend]), "now": 1.0, "wall": 9e9})
        self.assertEqual(len(live.approvals), 1)

    def test_usage_note_session_backend_rollover(self):
        m = feed(self.m0, self.events[:21])
        self.assertEqual((m.usage.in_tokens, m.usage.out_tokens, m.usage.context_window, m.usage.source),
                         (1200, 300, 200000, "gpt"))
        note = next(b for b in m.blocks if b.kind == "note")
        self.assertEqual(note.text, "Quota fallback engaged")
        self.assertEqual(m.notice, "quota fallback → claude-sonnet-5")
        self.assertEqual(m.roster.roles[0].native_id, "1fe77174-9abd-4f3a-ac69-cbd3545b2e63")   # session: no block
        self.assertEqual(m.roster.roles[0].model, "claude-opus-4-8")
        self.assertEqual([b.text for b in m.blocks if b.kind == "system"][1:],
                         ["→ hugpy", "rolled over · 2 carried"])
        self.assertTrue(m.roster_stale)
        self.assertEqual(m.lane().cursor, "21")               # cursor unchanged by rollover

    def test_done_result_without_streamed_text_and_held(self):
        # A turn whose only output is done.result renders as an assistant block.
        user = ev("user", 1, text="q", meta={"message_ids": ["m1"]})
        m = feed(self.m0, [user, ev("done", 2, text="answer", ok=True, meta={"message_ids": ["m1"]})])
        self.assertEqual([b.kind for b in m.blocks], ["user", "assistant"])
        self.assertEqual(m.blocks[-1].text, "answer")
        # Streamed text + done.result -> no duplicate.
        m = feed(self.m0, [user, ev("assistant", 2, text="answer"), ev("done", 3, text="answer", ok=True)])
        self.assertEqual(len(m.blocks), 2)
        self.assertFalse(m.blocks[-1].streaming)
        # held / error / interrupted
        m = feed(m, [self.events[22]])
        self.assertEqual(m.held, (True, "Held after a serve restart; retry to resume."))
        m = feed(m, [ev("done", 30, ok=False, meta={"error": "boom"})])
        self.assertEqual((m.blocks[-1].kind, m.blocks[-1].ok), ("note", False))
        m = feed(m, [ev("done", 31, ok=False, meta={"interrupted": True})])
        self.assertEqual(m.notice, "interrupted")

    def test_full_fixture_replays_and_dedupes(self):
        m = feed(self.m0, self.events)
        n = len(m.blocks)
        m = feed(m, self.events)                              # same seqs again
        self.assertEqual(len(m.blocks), n)
        live = [ac.normalize_event(r) for r in fixture("console_events_cs02c944d4")["events"]]
        m = feed(model("cs-02c944d4527643b69a439d1bd9f41ee0"), live)
        self.assertEqual(m.lane().cursor, "11156")
        self.assertTrue(any(b.kind == "user" for b in m.blocks))


class SseAndHugpyTests(unittest.TestCase):
    def test_sse_done_queued_and_exchange(self):
        m = model("547bcd67-4925-4397-8bf7-26d3eb9b1721")
        done = ac.normalize_sse_frame({"type": "done", "queued": "m1", "result": "Queued"}, 1)
        m = st.reduce(m, {"type": "sse_event", "sid": m.active_sid, "event": done})
        self.assertEqual(m.notice, "Queued")
        done = ac.normalize_sse_frame({"type": "done", "result": "x", "rc": 0, "exchange": {"in": 7, "out": 3},
                                       "cost": {"usd": 0.02}}, 2)
        m = st.reduce(m, {"type": "sse_event", "sid": m.active_sid, "event": done})
        self.assertEqual((m.usage.in_tokens, m.usage.out_tokens, m.usage.cost_usd, m.usage.source), (7, 3, 0.02, "sse"))
        text = ac.normalize_sse_frame({"type": "text", "text": "hi"}, 3)
        m = st.reduce(m, {"type": "sse_event", "sid": m.active_sid, "event": text})
        self.assertTrue(any(b.meta.get("sse") for b in m.blocks))
        m = st.reduce(m, {"type": "sse_done", "sid": m.active_sid})
        self.assertFalse(any(b.meta.get("sse") for b in m.blocks))

    def test_hugpy_delta_coalescing_then_reply(self):
        rows = [{"id": 1, "kind": "user", "data": "hi"}, {"id": 2, "kind": "run_start", "data": ["r", "hi"]},
                {"id": 3, "kind": "delta", "data": "wor"}, {"id": 4, "kind": "delta", "data": "ld"},
                {"id": 5, "kind": "reply", "data": "world!"}]
        m = feed(model("s1"), [hs.normalize_event(r) for r in rows[:4]], busy=True, source="hugpy")
        self.assertEqual([b.kind for b in m.blocks], ["user", "assistant"])
        self.assertEqual(m.blocks[-1].text, "world")
        self.assertTrue(m.blocks[-1].streaming)
        self.assertTrue(m.busy)
        m = feed(m, [hs.normalize_event(rows[4])], source="hugpy")
        self.assertEqual(m.blocks[-1].text, "world!")
        self.assertFalse(m.blocks[-1].streaming)
        self.assertFalse(m.busy)


class LifecycleTests(unittest.TestCase):
    def test_held_from_queue_and_unheld_by_send(self):
        m = model()
        m = st.reduce(m, {"type": "queue", "queue": QueueView(auto=False, paused=True, items=[])})
        self.assertEqual(m.held, (True, "paused"))
        m = st.reduce(m, {"type": "sent", "receipt": Receipt(CS, ["m1"]), "now": 1.0})
        self.assertFalse(m.held[0])
        self.assertEqual(len(m.pending_receipts), 1)
        m = st.reduce(m, {"type": "queue", "queue": QueueView(auto=True, paused=False, items=[])})
        self.assertEqual(m.held, (False, ""))

    def test_net_backoff_states(self):
        m = model()
        self.assertEqual(m.net, "live")
        m = st.reduce(m, {"type": "net", "ok": False, "now": 10.0})
        self.assertEqual((m.net, m.net_retry_at), ("degraded", 11.0))
        m = st.reduce(m, {"type": "net", "ok": False, "now": 11.0})
        self.assertEqual((m.net, m.net_retry_at), ("degraded", 13.0))
        m = st.reduce(m, {"type": "net", "ok": False, "now": 13.0})
        self.assertEqual((m.net, m.net_retry_at), ("down", 17.0))
        for i in range(3):
            m = st.reduce(m, {"type": "net", "ok": False, "now": 20.0})
        self.assertEqual(m.net_retry_at, 30.0)               # capped at 10 s
        cursor = m.lane().cursor
        m = st.reduce(m, {"type": "net", "ok": True, "now": 40.0})
        self.assertEqual((m.net, m.net_failures, m.lane().cursor), ("live", 0, cursor))

    def test_rebaseline_on_source_change(self):
        sid = "547bcd67-4925-4397-8bf7-26d3eb9b1721"
        m = model(sid)
        m = feed(m, [ev("user", 1, text="a", meta={"key": "k1"})], cursor="805197:1", source="transcript")
        self.assertEqual(len(m.blocks), 1)
        m = feed(m, [ev("user", 2, text="b", meta={"key": "k2"})], cursor="0:2", source="exchanges")
        self.assertEqual(m.blocks, [])
        self.assertTrue(m.lane().rebaseline)
        self.assertEqual((m.lane().cursor, m.lane().source), ("0", "exchanges"))
        m = feed(m, [ev("user", 2, text="b", meta={"key": "k2"})], cursor="0:2", source="exchanges")
        self.assertEqual(len(m.blocks), 1)
        self.assertFalse(m.lane().rebaseline)

    def test_stale_receipt_flagged_after_30s_when_idle(self):
        m = st.reduce(model(), {"type": "sent", "receipt": Receipt(CS, ["m1"]), "now": 0.0})
        m = st.reduce(m, {"type": "tick", "now": 20.0})
        self.assertFalse(m.pending_receipts[0]["unacked"])
        busy = feed(m, [], busy=True)
        self.assertFalse(st.reduce(busy, {"type": "tick", "now": 31.0}).pending_receipts[0]["unacked"])
        m = st.reduce(m, {"type": "tick", "now": 31.0})
        self.assertTrue(m.pending_receipts[0]["unacked"])
        m = feed(m, [ev("user", 5, text="x", meta={"message_ids": ["m1"]})])
        self.assertEqual(m.pending_receipts, [])
        m = st.reduce(st.reduce(model(), {"type": "sent", "receipt": Receipt(CS, ["m2"]), "now": 0.0}),
                      {"type": "receipt_lost", "receipt": None})
        self.assertIn("prompt lost", m.notice)

    def test_truncated_first_page_prepends_note(self):
        m = feed(model(), [ev("user", 1, text="x")], truncated=True)
        self.assertEqual((m.blocks[0].kind, m.blocks[0].text), ("note", "older history truncated"))

    def test_select_keeps_other_cursors(self):
        roster = Roster(roles=[Session(id="cs-a", role="keeper", label="Keeper"), Session(id="cs-b", role="worker", label="Worker")])
        m = st.reduce(st.Model(), {"type": "roster", "roster": roster, "prefer": "worker"})
        self.assertEqual(m.active_sid, "cs-b")
        m = feed(m, [ev("user", 7, text="x")])
        m = st.reduce(m, {"type": "select", "sid": "cs-a"})
        self.assertEqual(m.blocks, [])
        self.assertEqual(m.lane("cs-b").cursor, "7")
        self.assertEqual(st.reduce(st.Model(), {"type": "roster", "roster": roster, "prefer": "cs-b"}).active_sid, "cs-b")
        self.assertEqual(st.reduce(st.Model(), {"type": "roster", "roster": roster}).active_sid, "cs-a")

    def test_resize_scroll_expand_focus(self):
        m = feed(model(), [ev("user", 1, text="a"), ev("tool", 2, name="Bash", text="ls", detail="{}"),
                           ev("tool_result", 3, text="out", ok=True), ev("assistant", 4, text="done")])
        m = st.reduce(m, {"type": "scroll", "to": -5, "current": 20})
        self.assertEqual(m.lane().scroll, 15)
        m = st.reduce(m, {"type": "resize", "h": 30, "w": 100})
        self.assertEqual((m.size, m.lane().scroll), ((30, 100), 15))
        m = st.reduce(m, {"type": "scroll", "to": "end"})
        self.assertEqual(m.lane().scroll, -1)
        m = st.reduce(m, {"type": "expand"})                  # Ctrl-T: latest tool card
        self.assertEqual(m.lane().expanded, {1})
        m = st.reduce(m, {"type": "expand"})
        self.assertEqual(m.lane().expanded, set())
        m = st.reduce(m, {"type": "focus"})
        self.assertEqual((m.focus, m.selected), ("transcript", 2))
        m = st.reduce(m, {"type": "move", "delta": -1})
        m = st.reduce(m, {"type": "expand"})
        self.assertEqual(m.lane().expanded, {1})
        m = st.reduce(m, {"type": "expand", "index": 0})      # user blocks never expand
        self.assertEqual(m.lane().expanded, {1})
        m = st.reduce(m, {"type": "tools", "text": "3 ✓"})
        self.assertEqual(m.tools, "3 ✓")


if __name__ == "__main__":
    unittest.main()
