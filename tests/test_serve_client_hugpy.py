"""serve_client.hugpy_serve keeps the retired tui.py behaviour (:9126 shapes)."""
import _bootstrap  # noqa: F401
import unittest

from helpers import FakeServe, fixture

from hugpy_agent.serve_client import ServeError, connect
from hugpy_agent.serve_client import hugpy_serve as hs

SID = "e65ca08359ca4376b6e68ff21dc00a4d"


def routes():
    view = fixture("hugpy_session_view")

    def session_view(query, body):
        after = int(query.get("after", "0"))
        return dict(view, events=[e for e in view["events"] if e["id"] > after])

    return {
        ("GET", "/api/state"): fixture("state_9126"),
        ("GET", "/api/console/models"): {"models": [{"backend": "hugpy", "model": "hugpy-fleet", "label": "Hugpy · Fleet"}]},
        ("POST", "/api/sessions"): lambda q, b: (201, {"id": "s-new", "profile": b.get("profile") or "hugpy-fleet",
                                                       "status": "idle", "events": [], "pending": None}),
        ("GET", "/api/sessions/" + SID): session_view,
        ("GET", "/api/sessions/s-wait"): {"id": "s-wait", "profile": "hugpy-fleet", "status": "waiting", "events": [],
                                          "pending": {"id": "q7", "question": "Run `ls`?",
                                                      "options": ["Approve", "Approve all shell this run", "Deny"]}},
        ("POST", "/api/sessions/%s/messages" % SID): lambda q, b: (202, {"id": SID, "status": "running", "events": []}),
        ("POST", "/api/sessions/%s/profile" % SID): lambda q, b: {"id": SID, "profile": b["profile"], "status": "idle"},
        ("POST", "/api/sessions/%s/answer" % SID): {"ok": True},
        ("POST", "/api/sessions/%s/stop" % SID): {"ok": True, "note": "Stopping after the current operation"},
    }


class NormalizeTests(unittest.TestCase):
    def test_event_kinds(self):
        rows = [
            {"id": 1, "kind": "user", "data": "hello"},
            {"id": 2, "kind": "run_start", "data": ["rid", "hello"]},
            {"id": 3, "kind": "delta", "data": "wor"},
            {"id": 4, "kind": "delta", "data": "ld"},
            {"id": 5, "kind": "reply", "data": "world"},
            {"id": 6, "kind": "question", "data": {"id": "q1", "question": "ok?", "options": ["Approve", "Deny"]}},
            {"id": 7, "kind": "error", "data": "boom"},
            {"id": 8, "kind": "aborted", "data": "stopped"},
            {"id": 9, "kind": "mystery", "data": None},
        ]
        got = [hs.normalize_event(r) for r in rows]
        self.assertEqual([e.kind if e else None for e in got],
                         ["user", "status", "assistant", "assistant", "assistant", "question", "note", "note", None])
        self.assertTrue(got[2].meta["delta"])
        self.assertTrue(got[4].meta["final"])
        self.assertEqual((got[5].request_id, got[5].options), ("q1", ["Approve", "Deny"]))
        self.assertIs(got[6].ok, False)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeServe(routes())
        self.client = connect(self.fake.start(), "hugpy", timeout=3)

    def tearDown(self):
        self.fake.stop()

    def test_state_roster_single_local_role(self):
        self.assertEqual(self.client.probe()["service"], "hugpy-agent")
        roster = self.client.roster()
        self.assertEqual([r.role for r in roster.roles], ["local"])
        self.assertEqual(roster.roles[0].id, SID)
        self.assertEqual(roster.roles[0].backend, "hugpy")
        self.assertEqual(len(roster.sessions), 3)
        self.assertTrue(all(o.backend == "hugpy" for o in roster.provider_options))

    def test_create_poll_after_and_answer(self):
        created = self.client.create("hugpy-fleet")
        self.assertEqual(created["id"], "s-new")
        self.assertEqual(self.fake.posts("/api/sessions")[0][1], {"profile": "hugpy-fleet"})
        page = self.client.events(SID, 0)
        self.assertEqual(page.events[0].kind, "user")
        self.assertEqual(page.events[0].text, "hig")
        self.assertEqual(page.cursor, str(max(e["id"] for e in fixture("hugpy_session_view")["events"])))
        self.assertFalse(page.busy)
        self.assertIsNone(page.queue)
        self.assertEqual(self.fake.gets("/api/sessions/" + SID)[0][1], {"after": "0"})
        later = self.client.events(SID, int(page.cursor))
        self.assertEqual(later.events, [])
        self.assertEqual(self.fake.gets("/api/sessions/" + SID)[-1][1], {"after": page.cursor})
        waiting = self.client.events("s-wait", 0)
        self.assertTrue(waiting.busy)
        self.assertEqual(waiting.events[-1].kind, "question")
        self.assertEqual(waiting.events[-1].request_id, "q7")
        self.client.answer(SID, "q7", "Approve")
        self.assertEqual(self.fake.posts("/api/sessions/%s/answer" % SID)[0][1], {"id": "q7", "choice": "Approve"})

    def test_send_interrupt_profile(self):
        receipt = self.client.send(SID, "do it")
        self.assertEqual(self.fake.posts("/api/sessions/%s/messages" % SID)[0][1], {"text": "do it"})
        self.assertTrue(receipt.queued)
        self.client.interrupt(SID)
        self.assertTrue(self.fake.posts("/api/sessions/%s/stop" % SID))
        self.client.set_model(SID, "hugpy-fleet:Qwen3-32B")
        self.assertEqual(self.fake.posts("/api/sessions/%s/profile" % SID)[0][1], {"profile": "hugpy-fleet:Qwen3-32B"})
        self.assertEqual(self.client.models()[0].model, "hugpy-fleet")
        with self.assertRaises(ServeError):
            self.client.queue_action(SID, "retry")
        self.assertIsNone(self.client.queue(SID))


if __name__ == "__main__":
    unittest.main()
