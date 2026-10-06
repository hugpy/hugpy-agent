"""0.1.111 TUI hardening: protocol coverage the TUI used to drop, the App's
failure handling (poller, crash guard, locus generations, waits) and the
operator tools (/find, copy, export, /log). No curses, no real serve."""
import _bootstrap  # noqa: F401
import base64
import os
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from helpers import FakeServe

from hugpy_agent.serve_client import (Approval, Event, EventPage, QueueView, Receipt, Roster, ServeError,
                                      Session, connect)
from hugpy_agent.serve_client import abstract_serve as ac
from hugpy_agent.serve_client import hugpy_serve as hs
from hugpy_agent.tui import app as app_mod
from hugpy_agent.tui import output
from hugpy_agent.tui import state as st
from hugpy_agent.tui.diag import Diag, log_path
from hugpy_agent.tui.views import modals, panels, theme, transcript

CS = "cs-hardening000000000000000000000"
CTRL_Q, ESC = 17, 27
T = theme.plain()


def ev(kind, seq=1, **kw):
    fields = dict(seq=seq, ts=float(seq), session_id=CS, kind=kind)
    fields.update(kw)
    return Event(**fields)


def model(sid=CS):
    roster = Roster(roles=[Session(id=sid, role="keeper", label="Keeper", backend="hugpy", model="m")])
    return st.reduce(st.Model(kind="abstract-serve", base="http://127.0.0.1:9124", net="live"),
                     {"type": "roster", "roster": roster})


def feed(m, events, wall=None):
    page = EventPage(events, False, None, str(events[-1].seq if events else 0), "console", False)
    return st.reduce(m, {"type": "events", "sid": m.active_sid, "page": page, "now": 100.0,
                         **({"wall": wall} if wall else {})})


# -- hugpy-agent serve (:9126) vocabulary --------------------------------------

class HugpyVocabularyTests(unittest.TestCase):
    def norm(self, kind, data, i=1):
        return hs.normalize_event({"id": i, "kind": kind, "data": data, "ts": 10.0})

    def test_tool_rows_become_cards_with_results(self):
        ok = self.norm("tool", ["ts_categories", {}, '[{"category": "assess"}]', False])
        self.assertEqual((ok.kind, ok.name, ok.meta["result_ok"]), ("tool", "ts_categories", True))
        self.assertIn("assess", ok.meta["result"])
        bad = self.norm("tool", ["shell", {"cmd": "x"}, '{"error": "shell failed: boom"}', True], 2)
        self.assertIs(bad.meta["result_ok"], False)
        self.assertTrue(bad.meta["replayed"])
        self.assertIs(hs.tool_ok("error: give path or text"), False)
        self.assertIs(hs.tool_ok('{"error": null, "rows": [1], "count": 1}'), True)   # a result, not an error
        m = feed(model(), [ok, bad])
        self.assertEqual([(b.kind, b.ok) for b in m.blocks], [("tool", True), ("tool", False)])
        self.assertIn("shell failed", m.blocks[1].output)

    def test_turn_outcomes_and_failures_are_visible(self):
        done = self.norm("done", {"outcome": "aborted", "model": "Qwen3-Coder-Next-GGUF", "steps": 3,
                                  "error": "model unreachable: connect to http://127.0.0.1:7002 failed"}, 2)
        self.assertEqual(done.kind, "done")
        self.assertIs(done.ok, False)
        self.assertIn("model unreachable", done.meta["error"])
        self.assertIn("3 steps", done.meta["error"])
        m = feed(model(), [self.norm("user", "hi", 1), done])
        self.assertEqual(m.blocks[-1].kind, "note")
        self.assertIs(m.blocks[-1].ok, False)
        stopped = self.norm("done", {"outcome": "interrupted"})
        self.assertTrue(stopped.meta["interrupted"])
        self.assertIsNone(self.norm("done", {"outcome": "done", "answer": "x"}).meta.get("error"))
        chat = self.norm("chat_error", "connect to http://127.0.0.1:7002/v1 failed")
        self.assertEqual((chat.kind, chat.meta["warn"]), ("system", True))
        self.assertIn("model call failed", chat.text)
        self.assertEqual(self.norm("policy", ["shell", "deny"]).kind, "note")
        self.assertIsNone(self.norm("policy", ["ts_call", "ask"]))
        self.assertEqual(self.norm("loop_guard", ["shell", "abort", 5]).ok, False)
        self.assertIn("nudge", self.norm("nudge", "").text)
        self.assertIn("repair", self.norm("repair", ["bad json"]).text)
        self.assertTrue(self.norm("model", "hugpy-fleet:Qwen3").meta["refresh_roster"])
        self.assertIn("toolserver ready", self.norm("toolserver", ["ready", "https://ts"]).text)
        self.assertIsNone(self.norm("final", "answer"))                    # reply repeats it
        self.assertIsNone(self.norm("assistant", ""))
        self.assertTrue(self.norm("assistant", "prose").meta["final"])
        card = self.norm("client", {"type": "command_execution", "command": "ls", "status": "failed",
                                    "aggregated_output": "denied", "exit_code": 1})
        self.assertEqual((card.kind, card.name, card.meta["result_ok"]), ("tool", "command_execution", False))

    def test_events_page_through_and_answers_resolve_their_question(self):
        rows = [{"id": i, "kind": "delta", "data": "x", "ts": 1.0} for i in range(1, 501)]
        rows += [{"id": 501, "kind": "question", "data": {"id": "q9", "question": "ok?", "options": ["Approve", "Deny"]}},
                 {"id": 502, "kind": "answer", "data": "Approve"},
                 {"id": 503, "kind": "ask", "data": ["shell", "Approve"]}]

        def view(query, body):
            after = int(query.get("after", "0"))
            return {"id": "s1", "status": "running", "pending": None,
                    "events": [r for r in rows if r["id"] > after][:500]}
        fake = FakeServe({("GET", "/api/sessions/s1"): view})
        try:
            client = connect(fake.start(), "hugpy", timeout=3)
            page = client.events("s1", "0")
        finally:
            fake.stop()
        self.assertEqual(page.cursor, "503")                               # both pages in one poll
        self.assertEqual(len(fake.gets("/api/sessions/s1")), 2)
        resolved = [e for e in page.events if e.kind == "resolved"]
        self.assertEqual([e.request_id for e in resolved], ["q9", "q9"])
        self.assertIn("⚑ approval · shell · Approve", resolved[1].meta["line"])

    def test_pending_reraise_is_not_deduped_against_the_last_row(self):
        fake = FakeServe({("GET", "/api/sessions/s2"): {
            "id": "s2", "status": "waiting", "events": [{"id": 7, "kind": "user", "data": "go"}],
            "pending": {"id": "q1", "question": "run?", "options": ["Approve", "Deny"]}}})
        try:
            page = connect(fake.start(), "hugpy", timeout=3).events("s2", "0")
        finally:
            fake.stop()
        m = st.reduce(st.Model(active_sid="s2"), {"type": "events", "sid": "s2", "page": page, "now": 0.0})
        self.assertEqual([a.request_id for a in m.approvals], ["q1"])


# -- shared serve (:9124/:9125) additions --------------------------------------

class SharedServeTests(unittest.TestCase):
    def test_permission_cards_and_resolution(self):
        raw = {"type": "permission", "request_id": "perm-1", "tool": "Bash",
               "input": {"command": "rm -rf build", "description": "clean"}, "summary": "rm -rf build",
               "decisions": ["allow_once", "allow_session", "deny"], "session_id": CS, "seq": 5, "ts": 1000.0}
        card = ac.normalize_event(raw)
        self.assertEqual((card.kind, card.request_id, card.text), ("approval", "perm-1", "Allow Bash"))
        self.assertEqual(card.options, ["allow_once", "allow_session", "deny"])
        self.assertEqual(card.meta["params"]["command"], "rm -rf build")
        self.assertIsNone(ac.normalize_event(dict(raw, auto="bypassPermissions")))   # mode decided it
        # 10 minutes old but permissions wait 30: still answerable
        m = feed(model(), [card], wall=1600.0)
        self.assertEqual([a.request_id for a in m.approvals], ["perm-1"])
        done = ac.normalize_event({"type": "permission_resolved", "request_id": "perm-1", "tool": "Bash",
                                   "decision": "deny", "reason": "not now", "by": "operator", "seq": 6, "ts": 1001.0})
        m = feed(m, [done])
        self.assertEqual(m.approvals, [])
        self.assertEqual(m.blocks[-1].decision, "denied")
        # auto-allow by mode: no card, no line; an auto-DENY leaves an audit line
        quiet = ac.normalize_event({"type": "permission_resolved", "request_id": "p2", "tool": "Read",
                                    "decision": "allow_once", "by": "mode:bypassPermissions", "seq": 7})
        loud = ac.normalize_event({"type": "permission_resolved", "request_id": "p3", "tool": "Bash",
                                   "decision": "deny", "by": "mode:plan", "seq": 8})
        m = feed(m, [quiet, loud])
        self.assertEqual(m.blocks[-1].kind, "system")
        self.assertIn("⚑ permission · Bash · denied (plan)", m.blocks[-1].text)
        self.assertTrue(m.blocks[-1].meta["warn"])
        self.assertEqual(sum(1 for b in m.blocks if "Read" in b.text), 0)

    def test_call_rows_feed_the_context_counter(self):
        calls = [ac.normalize_event({"type": "call", "usage": {"in": 2, "cr": 36105, "cw": 3191, "out": 3},
                                     "seq": 1, "ts": 1.0}),
                 ac.normalize_event({"type": "call", "usage": {"in": 5, "cr": 39296, "cw": 583, "out": 40},
                                     "seq": 2, "ts": 2.0})]
        self.assertTrue(all(c.meta["silent"] for c in calls))
        m = feed(model(), calls)
        self.assertEqual(m.blocks, [])                                  # no transcript noise
        self.assertEqual((m.lane().ctx_tokens, m.lane().tok_out), (5 + 39296 + 583, 43))
        self.assertIn("ctx 39.9k · out 43", panels.status_fields(m, now=0))
        self.assertEqual(sum(1 for f in panels.status_fields(m, now=0) if f.startswith(("tok", "ctx"))), 1)
        mode = ac.normalize_event({"type": "permission_mode", "mode": "plan", "text": "Permission mode: plan"})
        self.assertEqual((mode.kind, mode.text), ("system", "Permission mode: plan"))


class ModalTests(unittest.TestCase):
    class Screen:
        def __init__(self, keys=(), size=(24, 80)):
            self.keys, self.size, self.text = iter(keys), size, []

        def getmaxyx(self): return self.size
        def getch(self): return next(self.keys, ESC)
        def addnstr(self, y, x, value, limit, attr=0): self.text.append(value[:limit])
        def erase(self): pass
        def refresh(self): pass

    def test_permission_keys_map_to_serve_decisions(self):
        perm = Approval("p1", "approval", "Allow Bash", ["allow_once", "allow_session", "deny"], {"command": "ls"})
        self.assertEqual(modals.approval_modal(self.Screen([ord("y")]), perm, T), "allow_once")
        self.assertEqual(modals.approval_modal(self.Screen([ord("a")]), perm, T), "allow_session")
        self.assertEqual(modals.approval_modal(self.Screen([ord("n")]), perm, T), "deny")
        self.assertIsNone(modals.approval_modal(self.Screen([ord("c")]), perm, T))   # no cancel offered
        scr = self.Screen([ESC])
        modals.approval_modal(scr, perm, T)
        self.assertTrue(any("allow for this session" in t for t in scr.text))
        gone = iter([True, False])
        self.assertIsNone(modals.approval_modal(self.Screen([-1, -1]), perm, T, alive=lambda: next(gone)))

    def test_picker_filter(self):
        opts = ["Qwen3-32B", "Kimi-K3", "qwen3-coder", "GLM-4.7"]
        keys = [ord("/")] + [ord(c) for c in "qwen"] + [258, 10]           # filter, Down, Enter
        self.assertEqual(modals.choose(self.Screen(keys), "MODEL", opts, T), 2)
        self.assertIsNone(modals.choose(self.Screen([ord("/"), ord("z"), ESC, ESC]), "MODEL", opts, T))
        self.assertEqual(modals.choose(self.Screen([ord("q")]), "MODEL", opts, T), None)   # q still closes


# -- reducer: find / resolution ------------------------------------------------

class FindTests(unittest.TestCase):
    def test_find_wraps_and_opens_the_chip_hiding_a_match(self):
        blocks = [ev("user", 1, text="deploy the thing"),
                  ev("tool", 2, name="Bash", detail='{"command": "ls"}', meta={"result": "a", "result_ok": True}),
                  ev("tool", 3, name="Read", detail='{"file_path": "/etc/needle.conf"}',
                     meta={"result": "x", "result_ok": True}),
                  ev("assistant", 4, text="the needle is here", meta={"final": True})]
        m = feed(model(), blocks)
        m = st.reduce(m, {"type": "find", "query": "needle", "current": 0})
        self.assertEqual((m.focus, m.selected), ("transcript", 2))
        self.assertIn(1, m.lane().groups_open)                             # chip of calls 1-2 opened
        self.assertIn("1/2", m.notice)
        m = st.reduce(m, {"type": "find"})                                 # F3: next
        self.assertEqual(m.selected, 3)
        m = st.reduce(m, {"type": "find"})                                 # wraps
        self.assertEqual(m.selected, 2)
        m = st.reduce(m, {"type": "find", "query": "absent"})
        self.assertIn("no match", m.notice)

    def test_alerts_counter(self):
        m = st.reduce(model(), {"type": "alerts", "add": 1})
        m = st.reduce(m, {"type": "alerts", "add": 1})
        self.assertIn("⚠ 2 /log", panels.status_fields(m, now=0))
        self.assertEqual(st.reduce(m, {"type": "alerts", "clear": True}).alerts, 0)


# -- App ------------------------------------------------------------------------

class Screen:
    def __init__(self, keys=(), size=(24, 80)):
        self.keys = iter(keys)
        self.size = size
        self.text = []

    def getmaxyx(self): return self.size
    def getch(self): return next(self.keys, CTRL_Q)
    def addnstr(self, y, x, value, limit, attr=0): self.text.append(value[:limit])
    def erase(self): pass
    def clear(self): pass
    def refresh(self): pass
    def timeout(self, value): pass
    def keypad(self, value): pass
    def nodelay(self, value): pass
    def move(self, y, x): pass


class Client:
    kind = "abstract-serve"
    base = "http://127.0.0.1:9124"

    def __init__(self):
        self.roster_calls = 0
        self.fail_roster = 0
        self.slow = 0.0
        self.queue_calls = 0

    def roster(self):
        self.roster_calls += 1
        if self.fail_roster:
            self.fail_roster -= 1
            return [].get("roles")                                         # AttributeError, like a list reply
        return Roster(roles=[Session(id=CS, role="keeper", label="Keeper", backend="hugpy", model="m")])

    def events(self, sid, since, on_page=None):
        return EventPage([Event(1, 0.0, sid, "user", "hello"),
                          Event(2, 0.0, sid, "assistant", "the answer", meta={"final": True})],
                         False, None, "2", "console")

    def state(self): return {}
    def usage(self, sid): return None
    def rollover(self): return {}
    def send(self, sid, text, on_event=None): return Receipt(sid, ["m1"], "1")
    def interrupt(self, sid): pass

    def queue(self, sid):
        self.queue_calls += 1
        time.sleep(self.slow)
        return QueueView(items=[])

    def queue_action(self, sid, action, **kw): pass
    def answer(self, sid, rid, decision): pass

    def models(self):
        time.sleep(self.slow)
        return []

    def set_provider(self, *a): pass
    def set_model(self, *a): return {}


def make(keys=(), client=None):
    client = client or Client()
    ui = app_mod.App(Screen(keys), client, theme=theme.plain())
    ui.send("action", {"type": "roster", "roster": client.roster(), "prefer": None})
    ui.send("action", {"type": "events", "sid": CS, "page": client.events(CS, "0"), "now": 0.0})
    return ui, client


def run_app(ui):
    with patch.object(ui, "start_poller"), patch.object(app_mod.curses, "curs_set"):
        return ui.run()


class AppHardeningTests(unittest.TestCase):
    def test_poller_survives_a_malformed_reply(self):
        ui, client = make()
        client.fail_roster = 1
        ui.drain()
        thread = threading.Thread(target=ui._poll_loop, daemon=True)
        thread.start()
        deadline = time.monotonic() + 4
        while client.roster_calls < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        ui.closed.set()
        thread.join(2)
        self.assertGreaterEqual(client.roster_calls, 3)                    # kept polling after the fault
        self.assertTrue(any(level == "ERROR" and "poll failed" in text for _, level, text in ui.diag.rows()))
        self.assertTrue(any(item[0] == "error" and "AttributeError" in item[1] for item in list(ui.events.queue)))

    def test_messages_from_a_left_locus_are_dropped(self):
        ui, client = make()
        ui.drain()
        old = ui.gen
        ui.gen += 1                                                        # a locus switch happened
        other = Roster(roles=[Session(id="cs-other", role="keeper")])
        ui.send("action", {"type": "roster", "roster": other}, gen=old)
        ui.send("notice", "stale", gen=old)
        ui.drain()
        self.assertEqual(ui.m.roster.roles[0].id, CS)
        self.assertNotEqual(ui.m.notice, "stale")

    def test_crash_guard_keeps_running_then_stops_a_spin(self):
        ui, client = make([ord("x"), CTRL_Q])
        real = ui.handle_key
        calls = {"n": 0}

        def flaky(key):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyError("boom")
            return real(key)
        ui.handle_key = flaky
        self.assertEqual(run_app(ui), 0)                                   # survived, then quit normally
        self.assertTrue(any("main loop fault" in t for _, _, t in ui.diag.rows()))

        ui2, _ = make([ord("x")] * 50)
        ui2.handle_key = lambda key: (_ for _ in ()).throw(ValueError("always"))
        self.assertEqual(run_app(ui2), 1)
        self.assertIn("internal errors", ui2.exit_message)

    def test_wait_replays_typeahead_and_esc_cancels(self):
        ui, client = make([ord("a"), ord("b")])
        client.slow = 0.4
        ui.drain()
        with patch.object(app_mod.curses, "curs_set"):
            self.assertEqual(ui.wait("loading queue", lambda: client.queue(CS)).items, [])
        self.assertEqual(list(ui.typeahead)[:2], [ord("a"), ord("b")])     # typed during the wait, kept
        ui3, c3 = make([ESC])
        c3.slow = 1.0
        ui3.drain()
        with patch.object(app_mod.curses, "curs_set"):
            with self.assertRaises(app_mod.Cancelled):
                ui3.wait("loading models", c3.models)

    def test_stale_receipt_check_does_not_block_the_main_loop(self):
        ui, client = make()
        client.slow = 1.5
        ui.drain()
        receipt = Receipt(CS, ["m9"], "1")
        ui.dispatch({"type": "sent", "receipt": receipt, "now": 0.0})
        ui.m = st.replace(ui.m, pending_receipts=[dict(ui.m.pending_receipts[0], unacked=True)])
        start = time.monotonic()
        ui.drain()
        self.assertLess(time.monotonic() - start, 0.5)
        ui.join_workers(3)
        ui.drain()
        self.assertEqual(ui.m.pending_receipts, [])
        self.assertEqual(client.queue_calls, 1)

    def test_copy_export_and_log(self):
        ui, client = make()
        ui.drain()
        written = []
        ui.tty_write = written.append
        ui.copy()                                                          # no selection: last reply
        self.assertTrue(written and written[0].startswith("\x1b]52;c;"))
        payload = written[0][len("\x1b]52;c;"):-1]
        self.assertEqual(base64.b64decode(payload).decode(), "the answer")
        self.assertIn("copied 10 chars", ui.m.notice)
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "sub", "t.md")
            ui.export(target)
            self.assertTrue(os.path.exists(target))
            self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
            text = open(target, encoding="utf-8").read()
            self.assertIn("### ▶ operator", text)
            self.assertIn("the answer", text)
            self.assertIn(CS, text)
        ui.say("serve: boom", "error")
        self.assertEqual(ui.m.alerts, 1)
        with patch.object(app_mod.modals, "text_modal") as viewer:
            ui.show_log()
        lines = viewer.call_args[0][2]
        self.assertTrue(any(l.startswith("serve   http://127.0.0.1:9124") for l in lines))
        self.assertTrue(any("serve: boom" in l for l in lines))
        self.assertEqual(ui.m.alerts, 0)

    def test_approval_answered_elsewhere_closes_the_modal(self):
        ui, client = make([-1, -1, CTRL_Q])
        card = Event(3, time.time(), CS, "approval", "Allow Bash", name="Bash", request_id="p1",
                     options=["allow_once", "deny"], meta={"params": {"command": "ls"}})
        ui.send("action", {"type": "events", "sid": CS, "page": EventPage([card], False, None, "3", "console"),
                           "now": 0.0, "wall": time.time()})
        gone = Event(4, time.time(), CS, "resolved", "allowed once", request_id="p1")
        ticks = {"n": 0}
        real_drain = ui.drain

        def drain():
            real_drain()
            ticks["n"] += 1
            if ticks["n"] == 2:
                ui.send("action", {"type": "events", "sid": CS,
                                   "page": EventPage([gone], False, None, "4", "console"), "now": 0.0})
        ui.drain = drain
        run_app(ui)
        self.assertEqual(ui.m.approvals, [])
        self.assertEqual(ui.m.blocks[-1].decision, "allowed once")


class DiagTests(unittest.TestCase):
    def test_log_file_rotation_target_and_off_switch(self):
        self.assertEqual(log_path({"HUGPY_TUI_LOG": "off"}), "")
        self.assertTrue(log_path({}).endswith(os.path.join(".hugpy", "logs", "tui.log")))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "logs", "tui.log")
            d = Diag(path=path, file=True)
            d.error("kaboom", ValueError("bad"))
            d.close()
            text = open(path, encoding="utf-8").read()
            self.assertIn("kaboom", text)
            self.assertIn("ValueError: bad", text)
            self.assertEqual(d.counts(), (1, 0))

    def test_hooks_capture_thread_deaths_and_stderr(self):
        import sys
        d = Diag()
        undo = d.install_hooks()
        try:
            t = threading.Thread(target=lambda: 1 / 0, name="doomed")
            t.start()
            t.join()
            print("stray warning", file=sys.stderr)
        finally:
            undo()
        rows = d.rows()
        self.assertTrue(any("thread doomed died" in text and "ZeroDivisionError" in text for _, _, text in rows))
        self.assertTrue(any("stderr: stray warning" in text for _, _, text in rows))


class OutputTests(unittest.TestCase):
    def test_markdown_fences_survive_backticks(self):
        blocks = [st.Block("tool", "", name="Bash", detail='{"command": "echo ```"}', output="```\nx\n```", ok=True)]
        md = output.to_markdown(blocks, "t")
        self.assertIn("````", md)                                          # fence longer than the payload's
        self.assertIn("**⚒ Bash** ✓", md)
        self.assertEqual(output.osc52(""), "")


class EmptyStateTests(unittest.TestCase):
    def test_loaded_empty_session_says_so(self):
        m = feed(model(), [])
        scr = ModalTests.Screen()
        from hugpy_agent.tui import layout
        transcript.draw_transcript(scr, m, layout.compute(24, 80).transcript, T)
        self.assertTrue(any("no messages in this session yet" in t for t in scr.text))


if __name__ == "__main__":
    unittest.main()


class ScreenshotFindingsTests(unittest.TestCase):
    """Bugs the README screenshot run exposed (0.1.112)."""

    def test_expired_approval_closes_the_streaming_reply(self):
        reply = ev("assistant", 1, text="restarting needs your approval")          # streams (not final)
        card = ev("approval", 2, text="Allow Bash", request_id="p9", options=["allow_once", "deny"])
        m = feed(model(), [reply, card], wall=2 + 7200)                            # card long expired
        self.assertFalse(m.blocks[0].streaming)
        self.assertEqual(m.blocks[1].decision, "expired")

    def test_approval_body_lists_plain_fields(self):
        a = Approval("p1", "approval", "Allow Bash", ["allow_once", "deny"],
                     {"command": "reboot", "description": "reboot box", "risk": "destructive", "env": {"A": 1}})
        body = modals.approval_body(a).splitlines()
        self.assertEqual(body[:3], ["command: reboot", "description: reboot box", "risk: destructive"])
        self.assertTrue(body[3].startswith("env: {"))

    def test_version_is_the_running_trees(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as fh:
            want = [l.split('"')[1] for l in fh if l.startswith("version")][0]
        if os.path.abspath(app_mod.__file__).startswith(os.path.join(root, "src")):
            self.assertEqual(app_mod.version(), want)
