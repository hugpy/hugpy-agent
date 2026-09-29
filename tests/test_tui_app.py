"""App with a fake client + scripted Screen: send / interrupt / quit / slash
commands without curses (curs_set patched, poller not started)."""
import _bootstrap  # noqa: F401
import curses
import time
import unittest
from unittest.mock import patch

from hugpy_agent.serve_client import Event, EventPage, QueueView, Receipt, Roster, ServeError, Session
from hugpy_agent.tui import app as app_mod
from hugpy_agent.tui.views import theme

KEEPER = "cs-cd165265d096407f92fcc4d54f2c2bd9"
WORKER = "cs-02c944d4527643b69a439d1bd9f41ee0"
CTRL_Q, CTRL_X, CTRL_C, TAB, ENTER = 17, 24, 3, 9, 10


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


class FakeClient:
    kind = "abstract-claude"
    base = "http://127.0.0.1:9124"

    def __init__(self):
        self.sent, self.interrupts, self.actions, self.answers = [], [], [], []
        self.fail_send = False

    def roster(self):
        return Roster(roles=[Session(id=KEEPER, role="keeper", label="Keeper", backend="hugpy", model="m"),
                             Session(id=WORKER, role="worker", label="Worker", backend="hugpy", model="w")])

    def events(self, sid, since, on_page=None):
        return EventPage([Event(1, 0.0, sid, "user", "hello")], False, QueueView(), "1", "console")

    def send(self, sid, text, on_event=None):
        if self.fail_send:
            raise ServeError("boom", 500)
        self.sent.append((sid, text))
        return Receipt(sid, ["m1"], "1")

    def interrupt(self, sid): self.interrupts.append(sid)
    def queue(self, sid): return QueueView(items=[])
    def queue_action(self, sid, action, **kw): self.actions.append((sid, action, kw))
    def answer(self, sid, rid, decision): self.answers.append((sid, rid, decision))
    def models(self): return []
    def set_provider(self, *a): pass
    def set_model(self, *a): return {}
    def state(self): return {}
    def rollover(self): return {}


def make(keys, client=None, **kw):
    client = client or FakeClient()
    ui = app_mod.App(Screen(keys), client, theme=theme.plain(), **kw)
    ui.send("action", {"type": "roster", "roster": client.roster(), "prefer": kw.get("session")})
    ui.send("action", {"type": "events", "sid": WORKER if kw.get("session") == "worker" else KEEPER,
                       "page": client.events(KEEPER, "0"), "now": 0.0})
    return ui, client


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class AppTests(unittest.TestCase):
    def run_app(self, ui):
        with patch.object(ui, "start_poller"), patch.object(app_mod.curses, "curs_set"):
            return ui.run()

    def test_tab_type_enter_interrupt_quit(self):
        ui, client = make([TAB, ord("h"), ord("i"), ENTER, CTRL_X, CTRL_Q])
        self.assertEqual(self.run_app(ui), 0)
        self.assertTrue(ui.closed.is_set())
        self.assertEqual(ui.m.active_sid, WORKER)               # Tab cycled keeper -> worker
        self.assertTrue(wait_for(lambda: client.sent == [(WORKER, "hi")]))
        self.assertEqual(client.interrupts, [WORKER])
        self.assertEqual(ui.composer.history[-1], "hi")

    def test_ctrl_c_clears_then_quits_and_prefer_session(self):
        ui, client = make([ord("x"), CTRL_C, CTRL_C], session="worker")
        self.assertEqual(self.run_app(ui), 0)
        self.assertEqual(ui.composer.buffer, "")
        self.assertEqual(client.sent, [])
        self.assertEqual(ui.m.active_sid, WORKER)

    def test_ctrl_c_interrupts_when_busy_and_quit_confirms(self):
        ui, client = make([CTRL_C, CTRL_Q, curses.KEY_DOWN, ENTER])
        ui.send("action", {"type": "events", "sid": KEEPER, "page": EventPage([], True, None, "1", "console"), "now": 0.0})
        self.assertEqual(self.run_app(ui), 0)
        self.assertEqual(client.interrupts, [KEEPER])            # Ctrl-C on an idle composer while busy
        self.assertTrue(any("still running" in t for t in ui.screen.text))

    def test_backslash_enter_newline_and_send_failure_notice(self):
        ui, client = make([ord("a"), ord("\\"), ENTER, ord("b"), ENTER, CTRL_Q])
        client.fail_send = True
        self.run_app(ui)
        self.assertTrue(wait_for(lambda: "send failed: boom" in ui.m.notice or any(
            k == "notice" for k, v in list(ui.events.queue))))
        self.assertEqual(ui.composer.history[-1], "a\nb")

    def test_slash_commands_status_retry_tools(self):
        class Tools:
            def status(self): return {"url": "http://t", "ok": True, "tool_count": 3, "auth": "ok"}
            def list_tools(self): return [{"name": "fs_read"}, "sys_run"]
        keys = [ord(c) for c in "/status"] + [ENTER] + [ord(c) for c in "/retry"] + [ENTER] + \
               [ord(c) for c in "/tools"] + [ENTER, ord("q"), CTRL_Q]
        ui, client = make(keys, toolserver_status=Tools())
        self.run_app(ui)
        self.assertEqual(ui.m.blocks[-1].kind, "note")
        self.assertTrue(ui.m.blocks[-1].text.startswith("status: [ac 9124]"))
        self.assertEqual(client.actions, [(KEEPER, "retry", {})])
        self.assertTrue(any("fs_read" in t for t in ui.screen.text))
        self.assertTrue(any("sys_run" in t for t in ui.screen.text))
        self.assertEqual(app_mod.tools_field(Tools().status()), "3 ✓")
        self.assertEqual(app_mod.tools_field({"ok": False, "auth": "missing"}), "auth missing")
        self.assertEqual(app_mod.tools_field({"ok": False, "auth": "ok"}), "off")
        self.assertEqual(app_mod.tools_field(None), "off")
        ui2 = app_mod.App(Screen(), FakeClient(), theme=theme.plain())
        self.assertEqual(ui2.m.tools, "off")                    # no client module -> "off", still runs

    def test_approval_modal_answers_and_transcript_keys(self):
        ui, client = make([ord("y"), curses.KEY_F2, curses.KEY_UP, ENTER, ord("r"), CTRL_Q])
        approval = Event(5, 0.0, KEEPER, "approval", "Run command", name="item/commandExecution/requestApproval",
                         request_id="r1", options=["accept", "acceptForSession", "decline", "cancel"],
                         meta={"params": {"command": "ls"}})
        tool = Event(4, 0.0, KEEPER, "tool", "ls", name="Bash", detail="{}")
        ui.send("action", {"type": "events", "sid": KEEPER,
                           "page": EventPage([tool, approval], False, None, "5", "console"), "now": 0.0})
        self.run_app(ui)
        self.assertEqual(client.answers, [(KEEPER, "r1", "accept")])
        self.assertEqual(ui.m.approvals, [])
        self.assertEqual(ui.m.blocks[-1].decision, "accept")
        self.assertEqual(ui.m.focus, "transcript")
        self.assertIn(1, ui.m.lane().expanded)                    # Up selected the tool card, Enter expanded it
        self.assertEqual(client.actions[-1][1], "retry")           # r in transcript focus

    def test_resize_and_draw_never_raise_on_small_screens(self):
        ui, client = make([curses.KEY_RESIZE, CTRL_Q])
        ui.screen.size = (15, 40)
        self.run_app(ui)
        self.assertEqual(ui.m.size, (15, 40))
        self.assertTrue(any("HUGPY" in t for t in ui.screen.text))


if __name__ == "__main__":
    unittest.main()
