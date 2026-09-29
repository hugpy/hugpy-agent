"""serve_client.abstract_claude against a FakeServe replaying the h27 fixtures."""
import _bootstrap  # noqa: F401
import io
import os
import unittest

from helpers import FakeServe, fixture

from hugpy_agent.serve_client import Event, ServeError, connect
from hugpy_agent.serve_client import abstract_claude as ac
from hugpy_agent.serve_client.http import Http

CS = "cs-02c944d4527643b69a439d1bd9f41ee0"
NATIVE = "547bcd67-4925-4397-8bf7-26d3eb9b1721"


def routes():
    events = fixture("console_events_cs02c944d4")
    synthetic = fixture("console_events_synthetic")

    def console_events(query, body):
        since = int(query.get("since", "0"))
        doc = synthetic if query.get("id", "").startswith("cs-synthetic") else events
        return dict(doc, events=[e for e in doc["events"] if e["seq"] > since])

    return {
        ("GET", "/api/state"): fixture("state_9124"),
        ("GET", "/api/session/roster"): fixture("roster"),
        ("GET", "/api/console/sessions"): fixture("console_sessions"),
        ("GET", "/api/console/models"): fixture("console_models"),
        ("GET", "/api/console/events"): console_events,
        ("GET", "/api/session/events"): fixture("session_events_native"),
        ("GET", "/api/session/history"): fixture("history_cs"),
        ("GET", "/api/session/rollover"): fixture("rollover"),
        ("GET", "/api/console/queue"): lambda q, b: {"session_id": q["id"], "auto": 0, "busy": False, "paused": True,
                                                     "items": [{"id": "m9", "text": "later", "ts": 1.0}]},
        ("GET", "/api/usage/session"): {"session_id": NATIVE, "keepalive": False, "runs": [],
                                        "totals": {"claude-opus-4-8": {"in": 1000, "out": 200, "usd": 0.5}}},
        ("POST", "/api/console/chat"): lambda q, b: {"session_id": b["session_id"], "message_ids": ["mid1"],
                                                     "cursor": 11156, "queued": False},
        ("POST", "/api/console/interrupt"): {"ok": True},
        ("POST", "/api/console/queue"): {"ok": True},
        ("POST", "/api/console/approval"): lambda q, b: (400, {"error": "No pending approval for this session"})
        if b["request_id"] == "gone" else {"ok": True},
        ("POST", "/api/session/roster"): lambda q, b: {"roles": [{"role": b["role"], "model": "claude-opus-4-8",
                                                                  "pending_model": b.get("model") if b["action"] == "set_model" else None}]},
        ("POST", "/api/session/chat"): ("sse", [
            {"type": "system", "model": "claude-opus-4-8", "tools": 2, "tool_names": ["Bash", "Read"]},
            {"type": "text", "text": "hi "},
            {"type": "text", "text": "there"},
            {"type": "error", "text": "transient"},
            {"type": "done", "result": "hi there", "rc": 0, "message_ids": ["n1"],
             "exchange": {"in": 10, "out": 5}, "cost": {"usd": 0.01}},
        ]),
    }


class HttpTests(unittest.TestCase):
    def test_http_error_mapping(self):
        with FakeServe({("GET", "/boom"): (400, {"error": "No active turn"}),
                        ("GET", "/plain"): (500, {"nope": 1})}) as base:
            http = Http(base, token="tok", timeout=2)
            with self.assertRaises(ServeError) as ctx:
                http.get("/boom")
            self.assertEqual(str(ctx.exception), "No active turn")
            self.assertEqual(ctx.exception.code, 400)
            with self.assertRaises(ServeError) as ctx:
                http.get("/plain")
            self.assertEqual(str(ctx.exception), "HTTP 500")
        with self.assertRaises(ServeError) as ctx:
            Http("http://127.0.0.1:1", timeout=1).get("/api/state")
        self.assertIn("serve unavailable", str(ctx.exception))

    def test_query_encoding_and_token_header(self):
        seen = {}

        def echo(query, body):
            return {"q": query}
        with FakeServe({("GET", "/api/x"): echo}) as base:
            http = Http(base, token="t")
            self.assertEqual(http.get("/api/x", id="cs-1", since=0, skip=None)["q"], {"id": "cs-1", "since": "0"})
            self.assertEqual(http.url("/api/x?a=1", {"b": 2}), base + "/api/x?a=1&b=2")


class FakeServeTests(unittest.TestCase):
    def test_fake_serve_serves_fixtures(self):
        with FakeServe(routes()) as base:
            http = Http(base)
            state = http.get("/api/state")
            self.assertIn("busy", state)
            self.assertTrue(state["version"][0].isdigit())
            page = http.get("/api/console/events", id=CS, since=0)
            self.assertEqual(len(page["events"]), 89)
            self.assertEqual(len(http.get("/api/console/events", id=CS, since=11100)["events"]),
                             len([e for e in page["events"] if e["seq"] > 11100]))
            with self.assertRaises(ServeError):
                http.get("/nowhere")


class NormalizeTests(unittest.TestCase):
    def test_normalize_event_table(self):
        got = [ac.normalize_event(r) for r in fixture("console_events_synthetic")["events"]]
        kinds = [e.kind for e in got]
        self.assertEqual(kinds, ["user", "status", "system", "assistant", "assistant", "thinking", "tool",
                                 "tool_result", "tool", "tool_result", "tool", "tool", "tool_result",
                                 "approval", "question", "resolved", "usage", "note", "system", "system",
                                 "system", "done", "done"])
        by_seq = {e.seq: e for e in got}
        self.assertEqual(by_seq[1].meta, {"via": "deterministic", "message_ids": ["m1"]})
        self.assertEqual(by_seq[2].text, "compiling")
        self.assertEqual(by_seq[3].text, "claude-opus-4-8 · 3 tools")
        self.assertEqual(by_seq[3].detail, "Bash\nRead\nEdit")
        self.assertEqual(by_seq[7].detail, '{"file_path": "/tmp/x"}')
        self.assertIs(by_seq[8].ok, True)
        self.assertEqual(by_seq[9].meta["status"], "started")
        self.assertTrue(by_seq[10].meta.get("delta"))
        self.assertEqual(by_seq[11].meta["status"], "completed")
        self.assertIn('"cmd": "uptime"', by_seq[12].detail)
        self.assertEqual(by_seq[13].detail, "up 3 days, load 0.1")
        self.assertEqual(by_seq[14].text, "Run command")
        self.assertEqual(by_seq[14].options, ["accept", "acceptForSession", "decline", "cancel"])
        self.assertEqual(by_seq[15].options[2], "Deny")
        self.assertEqual(by_seq[16].request_id, "r1")
        self.assertEqual(by_seq[17].meta["usage"]["modelContextWindow"], 200000)
        self.assertEqual(by_seq[18].meta["quota_fallback"], "claude-sonnet-5")
        self.assertTrue(by_seq[19].meta["silent"])
        self.assertEqual(by_seq[20].text, "→ hugpy")
        self.assertEqual(by_seq[21].text, "rolled over · 2 carried")
        self.assertIs(by_seq[22].ok, True)
        self.assertIs(by_seq[23].ok, False)
        self.assertTrue(by_seq[23].meta["held"])
        self.assertIsNone(ac.normalize_event({"type": "future-thing"}))
        self.assertIsNone(ac.normalize_event("junk"))

    def test_live_recorded_page_normalises_without_drops(self):
        raw = fixture("console_events_cs02c944d4")["events"]
        got = [ac.normalize_event(r) for r in raw]
        self.assertTrue(all(isinstance(e, Event) for e in got))
        self.assertEqual(len(got), 89)

    def test_sse_frame_parser_and_mapping(self):
        body = b'data: {"type":"text","text":"a"}\n\n: keepalive\n\ndata: {"type":"done","queued":"m1","result":"Queued"}\n\ndata: [DONE]\n\ndata: {"type":"text","text":"never"}\n'
        frames = list(ac.iter_sse(io.BytesIO(body)))
        self.assertEqual([f["type"] for f in frames], ["text", "done"])
        done = ac.normalize_sse_frame(frames[1], 2)
        self.assertEqual((done.kind, done.meta["queued"]), ("done", "m1"))
        err = ac.normalize_sse_frame({"type": "error", "text": "boom"})
        self.assertEqual((err.kind, err.ok), ("note", False))

    def test_native_transcript_events(self):
        raw = fixture("session_events_native")["events"]
        got = [ac.normalize_native_event(r, i + 1) for i, r in enumerate(raw)]
        self.assertEqual(got[0].kind, "user")
        self.assertEqual(got[1].kind, "assistant")
        self.assertTrue(got[1].meta["final"])
        tool = next(e for e in got if e.kind == "tool")
        self.assertEqual(tool.name, "Bash")
        self.assertIn("usage", tool.meta)
        self.assertTrue(all(e.meta["key"] for e in got))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeServe(routes())
        self.base = self.fake.start()
        self.client = connect(self.base, "abstract-claude", timeout=3)

    def tearDown(self):
        self.fake.stop()

    def test_probe_and_roster(self):
        self.assertIn("busy", self.client.probe())
        roster = self.client.roster()
        self.assertEqual([r.role for r in roster.roles], ["keeper", "chat", "worker", "local"])
        keeper = roster.roles[0]
        self.assertTrue(keeper.id.startswith("cs-"))
        self.assertTrue(keeper.paused)               # held row from the console sessions join
        self.assertEqual(roster.roles[1].id, NATIVE)
        self.assertEqual(roster.roles[1].pending_model, "claude-opus-5")
        self.assertEqual(len(roster.sessions), 6)
        self.assertTrue(roster.provider_options)
        self.assertEqual(roster.find(NATIVE).role, "chat")

    def test_events_cs_propagates_since_and_cursor(self):
        page = self.client.events(CS, 0)
        self.assertEqual(len(page.events), 89)
        self.assertEqual(page.cursor, "11156")
        self.assertEqual(page.source, "console")
        self.assertFalse(page.busy)
        self.assertEqual(page.queue.items, [])
        self.assertEqual(self.fake.gets("/api/console/events")[0][1], {"id": CS, "since": "0"})
        page = self.client.events(CS, 11150)
        self.assertTrue(all(e.seq > 11150 for e in page.events))
        self.assertEqual(self.fake.gets("/api/console/events")[-1][1]["since"], "11150")
        synthetic = self.client.events("cs-synthetic00000000000000000000", 0)
        self.assertTrue(synthetic.busy)
        self.assertEqual(synthetic.queue.items[0].text, "queued follow-up")
        self.assertEqual(synthetic.cursor, "23")

    def test_events_pages_at_500_and_caps(self):
        big = [{"type": "text", "text": "x", "seq": i, "ts": 0.0, "session_id": "cs-big"} for i in range(1, 1501)]

        def paged(query, body):
            since = int(query["since"])
            return {"events": [e for e in big if e["seq"] > since][:500], "busy": False,
                    "queue": {"auto": 1, "paused": 0, "items": []}}
        self.fake.routes[("GET", "/api/console/events")] = paged
        page = self.client.events("cs-big", 0)
        self.assertEqual(len(page.events), 1500)
        self.assertEqual(page.cursor, "1500")
        self.assertFalse(page.truncated)
        self.assertEqual([q["since"] for p, q in self.fake.gets("/api/console/events")], ["0", "500", "1000", "1500"])
        # Endless stream -> cap at max_pages, keep the last window, flag it.
        self.fake.routes[("GET", "/api/console/events")] = lambda q, b: {
            "events": [dict(e, seq=int(q["since"]) + i + 1) for i, e in enumerate(big[:500])], "busy": False}
        page = self.client.events("cs-big", 0, max_pages=3)
        self.assertTrue(page.truncated)
        self.assertEqual(page.cursor, "1500")
        self.assertLessEqual(len(page.events), 2000)

    def test_events_native_uses_session_events_cursor(self):
        page = self.client.events(NATIVE, "0")
        self.assertEqual(page.source, "transcript")
        self.assertEqual(page.cursor, "805197:127")
        self.assertEqual(self.fake.gets("/api/session/events")[0][1], {"id": NATIVE, "since": "0"})
        self.assertEqual(page.events[0].kind, "user")

    def test_send_cs_posts_console_chat(self):
        receipt = self.client.send(CS, "hello")
        self.assertEqual(self.fake.posts("/api/console/chat")[0][1], {"session_id": CS, "prompt": "hello"})
        self.assertEqual((receipt.message_ids, receipt.cursor, receipt.queued), (["mid1"], "11156", False))

    def test_send_native_consumes_sse(self):
        seen = []
        receipt = self.client.send(NATIVE, "hi", on_event=seen.append)
        self.assertEqual(self.fake.posts("/api/session/chat")[0][1], {"session_id": NATIVE, "prompt": "hi"})
        self.assertEqual([e.kind for e in seen], ["system", "assistant", "assistant", "note", "done"])
        self.assertEqual(seen[-1].meta["exchange"], {"in": 10, "out": 5})
        self.assertEqual(receipt.message_ids, ["n1"])
        self.assertFalse(receipt.queued)

    def test_interrupt_queue_and_actions(self):
        self.client.interrupt(CS)
        self.assertEqual(self.fake.posts("/api/console/interrupt")[0][1], {"session_id": CS})
        queue = self.client.queue(CS)
        self.assertTrue(queue.paused)
        self.assertFalse(queue.auto)
        self.assertEqual(queue.items[0].id, "m9")
        self.client.queue_action(CS, "retry")
        self.client.queue_action(CS, "update", id="m9", text="edited")
        bodies = [b for p, b in self.fake.posts("/api/console/queue")]
        self.assertEqual(bodies[0], {"session_id": CS, "action": "retry"})
        self.assertEqual(bodies[1], {"session_id": CS, "action": "update", "id": "m9", "text": "edited"})

    def test_approval_answer_and_400(self):
        self.client.answer(CS, "r1", "accept")
        self.assertEqual(self.fake.posts("/api/console/approval")[0][1],
                         {"session_id": CS, "request_id": "r1", "decision": "accept"})
        with self.assertRaises(ServeError) as ctx:
            self.client.answer(CS, "gone", "accept")
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("No pending approval", str(ctx.exception))

    def test_models_set_provider_set_model_staged(self):
        options = self.client.models()
        self.assertEqual(sorted({o.backend for o in options}), ["claude", "gpt"])
        self.assertEqual(options[1].model, "claude-fable-5-1")
        self.client.set_provider("worker", "hugpy", "hugpy-fleet:Qwen3-32B")
        doc = self.client.set_model("chat", "claude-opus-5")
        bodies = [b for p, b in self.fake.posts("/api/session/roster")]
        self.assertEqual(bodies[0]["action"], "set_provider")
        self.assertEqual(bodies[1], {"action": "set_model", "role": "chat", "model": "claude-opus-5",
                                     "by": "hugpy-agent tui"})
        self.assertEqual(ac.staged_model(doc, "chat"), "claude-opus-5")
        self.assertIsNone(ac.staged_model(doc, "keeper"))

    def test_usage_native_only(self):
        self.assertIsNone(self.client.usage(CS))
        usage = self.client.usage(NATIVE)
        self.assertEqual((usage.in_tokens, usage.out_tokens, usage.cost_usd), (1000, 200, 0.5))
        self.assertIn("archived", self.client.rollover())

    def test_ui_base_prefix_tolerated(self):
        with FakeServe(routes(), prefix="/ac") as base:
            client = connect(base, "abstract-claude", timeout=3)
            self.assertEqual(len(client.roster().roles), 4)
            self.assertEqual(len(client.events(CS, 0).events), 89)


@unittest.skipUnless(os.environ.get("HUGPY_TUI_LIVE"), "HUGPY_TUI_LIVE=1 enables the GET-only live smoke")
class LiveSmoke(unittest.TestCase):
    """GET-only against the keeper serve. NEVER posts."""
    BASE = "http://127.0.0.1:9124"

    def test_live_reads(self):
        from hugpy_agent.tui.discovery import identify
        client = connect(self.BASE, "abstract-claude", timeout=5)
        self.assertEqual(identify(client.probe()), "abstract-claude")
        roster = client.roster()
        keeper = next(r for r in roster.roles if r.role == "keeper")
        page = client.events(keeper.id, 0, max_pages=1)
        self.assertTrue(page.cursor)
        self.assertTrue(all(e.kind in ac.KINDS for e in page.events) if hasattr(ac, "KINDS") else True)


if __name__ == "__main__":
    unittest.main()
