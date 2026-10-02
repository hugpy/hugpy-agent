"""curses loop + threads for `hugpy-agent tui` (h26 §1.2 app.py).

Pattern is fleet_tui.Console: worker threads never touch curses, they post
`(kind, value)` tuples to a queue.Queue and the main loop `drain()`s them into
the reducer before every draw. One Poller thread reads the serve (events 1 s,
roster 10 s, state 5 s, rollover 30 s, toolserver 15 s, backoff on error); each
send runs in its own short thread (native rows stream SSE from it).
"""
from __future__ import annotations

import curses
import os
import queue
import threading
import time

from ..serve_client import ServeError
from ..serve_client.abstract_claude import is_cs, staged_model
from . import layout
from .state import Model, reduce
from .views import composer as cv
from .views import modals, panels, theme as theme_mod, transcript

HELP = [
    "Enter send · \\+Enter / Alt+Enter newline · Up/Down history (single line)",
    "Ctrl-C clear composer / interrupt / quit · Ctrl-X interrupt · Ctrl-Q quit",
    "Tab / Shift-Tab next / previous role · Ctrl-G session picker · Ctrl-P model picker",
    "F2 focus transcript <-> composer · Up/Down select block · Enter/Space/click expand card",
    "Tool calls: ▸ ⚒ collapsed call (✓ ok ✗ error … running) · ▸ ⚙ N calls = consecutive calls",
    "  Enter/Space/click opens a chip or call · Ctrl-O (or a, transcript focus) toggle all",
    "PgUp/PgDn, Ctrl-U/Ctrl-D scroll · End follow tail · Ctrl-T expand latest tool card",
    "Ctrl-K queue · r (transcript focus) retry / un-hold · Ctrl-A reopen approval · Ctrl-L redraw",
    "Slash: /model /session <id> /queue /retry /expand [n] /status /tools /help /quit",
]
SLASH_MENU = [
    ("/handoff",  "store this session's state on its toolserver row (engine)"),
    ("/resume",   "load the state this session continues (engine)"),
    ("/rollover", "handoff -> clear -> resume, toolserver-managed (engine)"),
    ("/context",  "show the whole session as fed to the model + tokens"),
    ("/session",  "switch session (or Ctrl-G picker)"),
    ("/model",    "model picker"),
    ("/queue",    "queue view"),
    ("/retry",    "retry / un-hold"),
    ("/expand",   "expand card [n]"),
    ("/status",   "status line into transcript"),
    ("/tools",    "tool list"),
    ("/help",     "keys and commands"),
    ("/quit",     "leave the TUI"),
]

CTRL = {name: ord(ch) - 64 for name, ch in {"A": "A", "C": "C", "D": "D", "G": "G", "K": "K", "L": "L",
                                              "O": "O", "P": "P", "Q": "Q", "T": "T", "U": "U", "X": "X"}.items()}


def toolserver_probe(explicit=None):
    """Operator addition: every harness shows toolserver health. The client
    module is built by another stream; import lazily so the TUI runs without
    it. Returns (status_callable, tools_callable) or (None, None)."""
    if explicit is not None:
        status = explicit.status if hasattr(explicit, "status") else explicit
        tools = getattr(explicit, "list_tools", None)
        return status, tools
    try:
        from ..toolserver_client import ToolserverClient
    except Exception:
        return None, None
    try:
        client = ToolserverClient()
    except Exception:
        return None, None
    return getattr(client, "status", None), getattr(client, "list_tools", None)


def tools_field(status):
    if not isinstance(status, dict):
        return "off"
    auth = status.get("auth")
    if auth in ("missing", "rejected"):
        return "auth %s" % auth
    if status.get("ok"):
        return "%s ✓" % status.get("tool_count", "?")
    return "off"


class App:
    def __init__(self, screen, client, session=None, toolserver_status=None, theme=None):
        self.screen, self.client = screen, client
        self.m = Model(kind=client.kind, base=client.base)
        self.prefer = session
        self.events = queue.Queue()
        self.closed = threading.Event()
        self.composer = cv.Composer()
        self.slash_sel = 0
        self._slash_hits = []
        self.theme = theme
        self.sse_active = set()
        self.roster_now = threading.Event()
        self.tools_status, self.tools_list = toolserver_probe(toolserver_status)
        self.poll_error = ""
        self.last_lines = (0, 0)
        self.hits = {}                  # screen row -> transcript target (mouse clicks)
        self.now = time.monotonic

    # -- thread -> main loop ---------------------------------------------------
    def send(self, kind, value):
        if not self.closed.is_set():
            self.events.put((kind, value))

    def dispatch(self, action):
        self.m = reduce(self.m, action)

    def drain(self):
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "action":
                self.dispatch(value)
            elif kind == "notice":
                self.dispatch({"type": "notice", "text": value})
        self.dispatch({"type": "tick", "now": self.now()})
        for r in list(self.m.pending_receipts):
            if r["unacked"]:
                self._check_receipt(r)

    def _check_receipt(self, entry):
        """Stale receipt (§2.4): queued -> say so; absent -> prompt lost."""
        try:
            q = self.client.queue(self.m.active_sid)
        except ServeError:
            return
        self.dispatch({"type": "queue", "queue": q})
        if q and q.items:
            self.dispatch({"type": "receipt_lost", "receipt": entry["receipt"], "notice": "prompt queued — r retry"})
        else:
            self.dispatch({"type": "receipt_lost", "receipt": entry["receipt"]})

    # -- poller ------------------------------------------------------------------
    def start_poller(self):
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _poll_loop(self):
        due = {"roster": 0.0, "state": 0.0, "rollover": 0.0, "tools": 0.0, "usage": 0.0}
        while not self.closed.is_set():
            m = self.m
            now = self.now()
            if m.net_retry_at and now < m.net_retry_at:
                self._sleep(0.2)
                continue
            try:
                if now >= due["roster"] or m.roster_stale or self.roster_now.is_set():
                    self.roster_now.clear()
                    self.send("action", {"type": "roster", "roster": self.client.roster(), "prefer": self.prefer})
                    due["roster"] = now + 10
                    m = self.m if self.m.active_sid else m
                sid = m.active_sid
                if sid and sid not in self.sse_active:
                    lane = m.lane(sid)
                    since = "0" if (lane.rebaseline or not lane.loaded) else lane.cursor
                    page = self.client.events(sid, since)
                    self.send("action", {"type": "events", "sid": sid, "page": page, "now": now,
                                         "wall": time.time()})
                if now >= due["state"]:
                    self.client.state()
                    due["state"] = now + 5
                if sid and not is_cs(sid) and now >= due["usage"] and hasattr(self.client, "usage"):
                    usage = self.client.usage(sid)
                    if usage is not None:
                        self.send("action", {"type": "usage", "usage": usage})
                    due["usage"] = now + 15
                if sid and not is_cs(sid) and now >= due["rollover"]:
                    self._follow_rollover(sid)
                    due["rollover"] = now + 30
                self.send("action", {"type": "net", "ok": True, "now": now})
            except ServeError as exc:
                self.poll_error = str(exc)
                self.send("action", {"type": "net", "ok": False, "now": now})
                self.send("notice", "serve: " + str(exc))
            if self.tools_status and now >= due["tools"]:
                try:
                    self.send("action", {"type": "tools", "text": tools_field(self.tools_status())})
                except Exception as exc:
                    self.send("action", {"type": "tools", "text": "off"})
                due["tools"] = now + 15
            self._sleep(1.0)

    def _follow_rollover(self, sid):
        doc = self.client.rollover() or {}
        archived = (doc.get("archived") or {}).get(sid) or {}
        successor = archived.get("successor")
        if successor and successor != sid:
            self.send("action", {"type": "select", "sid": successor})
            self.send("notice", "session rolled over → %s" % successor[:8])
            self.roster_now.set()

    def _sleep(self, seconds):
        deadline = self.now() + seconds
        while not self.closed.is_set() and self.now() < deadline:
            time.sleep(0.05)

    # -- sending -------------------------------------------------------------
    def submit(self, text):
        if text.startswith("/"):
            return self.slash(text)
        sid = self.m.active_sid
        if not sid:
            # no session yet: the serve mints one for sid "new"; the receipt
            # carries the real id and _sent adopts it as active.
            sid = "new"
            self.dispatch({"type": "notice", "text": "starting a new session…"})
        threading.Thread(target=self._send, args=(sid, text), daemon=True).start()

    def _send(self, sid, text):
        native = self.client.kind == "abstract-claude" and not is_cs(sid)
        if native:
            self.sse_active.add(sid)
        try:
            had_sid = bool(self.m.active_sid)
            receipt = self.client.send(sid, text, on_event=(lambda ev: self.send(
                "action", {"type": "sse_event", "sid": sid, "event": ev, "now": self.now()})) if native else None)
            self.send("action", {"type": "sent", "receipt": receipt, "now": self.now()})
            if not had_sid:
                self.roster_now.set()      # the new session shows up in pickers at once
        except ServeError as exc:
            self.send("notice", "send failed: %s" % exc)
        except (TimeoutError, OSError) as exc:
            # a dropped stream must surface as a notice, never a thread
            # traceback sprayed over the curses screen
            self.send("notice", "stream lost (%s) — /retry or resend" % type(exc).__name__)
        finally:
            if native:
                self.sse_active.discard(sid)
                self.send("action", {"type": "sse_done", "sid": sid})

    def interrupt(self):
        sid = self.m.active_sid
        if not sid:
            return
        try:
            self.client.interrupt(sid)
            self.dispatch({"type": "notice", "text": "interrupt sent"})
        except ServeError as exc:
            self.dispatch({"type": "notice", "text": str(exc)})

    def retry(self):
        sid = self.m.active_sid
        if not sid:
            return
        try:
            self.client.queue_action(sid, "retry")
            self.dispatch({"type": "notice", "text": "retry sent"})
            self.dispatch({"type": "queue", "queue": self.client.queue(sid)})
        except ServeError as exc:
            self.dispatch({"type": "notice", "text": str(exc)})

    def answer(self, approval, decision):
        try:
            self.client.answer(approval.session_id or self.m.active_sid, approval.request_id, decision)
        except ServeError as exc:
            if exc.code == 400:
                self.dispatch({"type": "notice", "text": "approval already gone: %s" % exc})
            else:
                self.dispatch({"type": "notice", "text": str(exc)})
                return
        self.dispatch({"type": "approval_answered", "request_id": approval.request_id, "decision": decision})

    # -- slash commands ------------------------------------------------------
    def slash(self, text):
        parts = text.split()
        cmd, args = parts[0].lower(), parts[1:]
        if cmd in ("/quit", "/exit"):
            return "quit"
        if cmd == "/help":
            modals.text_modal(self.screen, "HELP", HELP, self.theme, self.drain)
        elif cmd == "/model":
            self.pick_model()
        elif cmd == "/session":
            self.pick_session(args[0] if args else None)
        elif cmd == "/queue":
            self.open_queue()
        elif cmd == "/retry":
            self.retry()
        elif cmd == "/expand":
            index = int(args[0]) if args and args[0].isdigit() else None
            self.dispatch({"type": "expand", "index": index})
        elif cmd == "/rollover":
            sid = self.m.active_sid
            if not sid:
                self.dispatch({"type": "notice", "text": "no session"})
            else:
                try:
                    res = self.client.roll(sid)
                    self.dispatch({"type": "notice",
                                   "text": "rollover: " + (res.get("note") or
                                           ("queued" if res.get("ok") else str(res.get("error"))))})
                except ServeError as exc:
                    self.dispatch({"type": "notice", "text": "rollover failed: %s" % exc})
        elif cmd == "/context":
            self.show_context()
        elif cmd == "/status":
            self.status_note()
        elif cmd == "/tools":
            self.show_tools()
        else:
            # not a TUI command: forward to the ENGINE (session-first commands
            # like /handoff /resume /rollover live there, not here)
            sid = self.m.active_sid or "new"
            threading.Thread(target=self._send, args=(sid, text), daemon=True).start()

    def status_note(self):
        from .state import Block
        lane = self.m.lane()
        fields = panels.status_fields(self.m, self.now())
        blocks = lane.blocks + [Block("note", "status: " + " · ".join(fields) + " · cursor %s · source %s" %
                                      (lane.cursor, lane.source or "-"))]
        self.m = self.m.__class__(**dict(self.m.__dict__, lanes=dict(self.m.lanes, **{
            self.m.active_sid: lane.__class__(**dict(lane.__dict__, blocks=blocks))})))

    def show_context(self):
        """The whole session as the model sees it on the next call, with tokens."""
        sid = self.m.active_sid
        if not sid:
            self.dispatch({"type": "notice", "text": "no session"})
            return
        lines = []
        try:
            events, _cursor = self.client.events(sid, "0")
        except Exception as exc:
            events = []
            lines.append("events unavailable: %s" % exc)
        for ev in events or []:
            k = getattr(ev, "kind", "")
            txt = (getattr(ev, "text", "") or "").rstrip()
            if k in ("user", "prompt"):
                lines.append("USER: " + txt)
            elif k in ("text", "assistant"):
                lines.append("ASSISTANT: " + txt)
            elif k in ("tool", "tool_call"):
                lines.append("TOOL: %s" % (getattr(ev, "meta", {}) or {}).get("name", txt[:80]))
            elif txt:
                lines.append("%s: %s" % (k.upper() or "EVENT", txt[:200]))
        u = getattr(self.m, "usage", None)
        if u:
            lines.append("")
            lines.append("tokens: %s in / %s out%s" % (
                "{:,}".format(getattr(u, "in_tokens", 0)),
                "{:,}".format(getattr(u, "out_tokens", 0)),
                "  $%.4f" % u.cost_usd if float(getattr(u, "cost_usd", 0) or 0) else ""))
        flat = []
        for ln in lines:
            flat.extend(ln.splitlines() or [""])
        modals.text_modal(self.screen, "CONTEXT %s" % sid[:13], flat or ["(empty)"],
                          self.theme, self.drain)

    def show_tools(self):
        lines = []
        try:
            if self.tools_list:
                tools = self.tools_list()
                for t in tools or []:
                    lines.append(t if isinstance(t, str) else (t.get("name") or str(t)))
            elif self.tools_status:
                lines.append(str(self.tools_status()))
        except Exception as exc:
            lines.append("toolserver error: %s" % exc)
        if not lines:
            lines = ["toolserver: off (no toolserver_client / TOOLSERVER_URL)"]
        modals.text_modal(self.screen, "TOOLS · %s" % (self.m.tools or "off"), lines, self.theme, self.drain)

    def pick_model(self):
        row = self.m.session
        try:
            options = self.client.models()
        except ServeError as exc:
            self.dispatch({"type": "notice", "text": str(exc)})
            return
        if not options:
            self.dispatch({"type": "notice", "text": "no models offered"})
            return
        labels = ["%s  (%s)" % (o.label or o.model, o.backend) for o in options]
        pick = modals.choose(self.screen, "MODEL · %s" % ((row.label or row.id) if row else (self.m.active_sid or "?")[:13]), labels, self.theme, self.drain)
        if pick is None:
            return
        if row is None and not self.m.active_sid:
            self.dispatch({"type": "notice", "text": "no session to set a model on — send a prompt first"})
            return
        chosen = options[pick]
        target = (row.role or row.id) if row else self.m.active_sid
        try:
            if self.client.kind == "hugpy":
                self.client.set_model((row.id if row else self.m.active_sid), chosen.model)
                note = "model: " + chosen.model
            elif chosen.backend and chosen.backend != row.backend:
                self.client.set_provider(target, chosen.backend, chosen.model)
                note = "provider → %s/%s" % (chosen.backend, chosen.model or "default")
            else:
                doc = self.client.set_model(target, chosen.model)
                note = "model: %s%s" % (chosen.model or "default", " (staged)" if staged_model(doc, target) else "")
            self.dispatch({"type": "notice", "text": note})
            self.roster_now.set()
        except ServeError as exc:
            self.dispatch({"type": "notice", "text": str(exc)})

    def pick_session(self, wanted=None):
        roster = self.m.roster
        if not roster or not (roster.roles or roster.sessions):
            self.dispatch({"type": "notice",
                           "text": "no sessions on this serve yet — type a prompt to start one"})
            return
        rows = [r for r in roster.roles if r.id] + [s for s in roster.sessions if s.id not in {r.id for r in roster.roles}]
        if wanted:
            for r in rows:
                if wanted in (r.id, r.role, (r.label or "").lower()) or r.id.startswith(wanted):
                    self.dispatch({"type": "select", "sid": r.id})
                    return
            self.dispatch({"type": "notice", "text": "no session %s" % wanted})
            return
        labels = ["%-8s %s · %s · %s%s" % ((r.label or r.role or "")[:8], panels.short_id(r.id), r.backend,
                                            (r.model or "").split(":")[-1] or "-", "  BUSY" if r.busy else "")
                  for r in rows]
        current = next((i for i, r in enumerate(rows) if r.id == self.m.active_sid), 0)
        pick = modals.choose(self.screen, "SESSIONS", labels, self.theme, self.drain, selected=current)
        if pick is not None:
            self.dispatch({"type": "select", "sid": rows[pick].id})

    def cycle_role(self, delta):
        roles = [r for r in panels.role_rows(self.m) if r.id]
        if not roles:
            return
        ids = [r.id for r in roles]
        index = ids.index(self.m.active_sid) if self.m.active_sid in ids else -1
        self.dispatch({"type": "select", "sid": ids[(index + delta) % len(ids)]})

    def open_queue(self):
        sid = self.m.active_sid
        try:
            q = self.client.queue(sid)
        except ServeError as exc:
            self.dispatch({"type": "notice", "text": str(exc)})
            return
        self.dispatch({"type": "queue", "queue": q})
        if q is None:
            self.dispatch({"type": "notice", "text": "this serve has no queue"})
            return
        result = modals.queue_modal(self.screen, q, self.theme, self.drain)
        if not result:
            return
        action, payload = result
        try:
            if action == "edit":
                text = modals.line_edit(self.screen, "EDIT %s" % payload.id[:8], payload.text, self.theme, self.drain)
                if text is not None:
                    self.client.queue_action(sid, "update", id=payload.id, text=text)
            elif action == "remove":
                self.client.queue_action(sid, "remove", id=payload.id)
            elif action == "auto":
                self.client.queue_action(sid, "auto", auto=bool(payload))
            else:
                self.client.queue_action(sid, action)
            self.dispatch({"type": "queue", "queue": self.client.queue(sid)})
            self.dispatch({"type": "notice", "text": "queue %s ok" % action})
        except ServeError as exc:
            self.dispatch({"type": "notice", "text": str(exc)})

    def open_approval(self):
        approval = self.m.open_approval
        if approval is None:
            return
        decision = modals.approval_modal(self.screen, approval, self.theme, self.drain)
        if decision is None:
            self.dispatch({"type": "approval_shown", "open": False})
            return
        self.answer(approval, decision)

    def confirm_quit(self):
        if not self.m.busy:
            return True
        pick = modals.choose(self.screen, "A turn is still running on the serve", ["Stay", "Quit (the turn keeps running)"],
                             self.theme, self.drain)
        return pick == 1

    # -- keys ------------------------------------------------------------------
    def getkey(self):
        get_wch = getattr(self.screen, "get_wch", None)
        if get_wch is None:
            return self.screen.getch()
        try:
            key = get_wch()
        except curses.error:
            return -1
        if isinstance(key, str):
            if len(key) == 1 and (ord(key) < 32 or ord(key) == 127):
                return ord(key)
            return key
        return key

    def handle_key(self, key):
        """Returns 'quit' to leave the loop."""
        m = self.m
        if key == -1:
            return None
        if key == curses.KEY_RESIZE:
            h, w = self.screen.getmaxyx()
            self.dispatch({"type": "resize", "h": h, "w": w})
            self.screen.clear()
            return None
        if isinstance(key, str):
            if m.focus == "composer":
                self.composer.insert(key)
            elif key == " ":
                self.dispatch({"type": "expand"})
            elif key == "a":
                self.dispatch({"type": "expand_all"})
            elif key == "r":
                self.retry()
            return None
        if key == 27:                                     # Esc / Alt-chord
            self.screen.nodelay(True)
            try:
                nxt = self.getkey()
            finally:
                self.screen.nodelay(False)
            if nxt in (10, 13):
                self.composer.newline()
            elif nxt == -1:
                self.dispatch({"type": "notice", "text": ""})
            return None
        menu_open = (m.focus == "composer" and self._slash_hits
                     and self.composer.buffer.startswith("/")
                     and " " not in self.composer.buffer)
        if menu_open and key == curses.KEY_UP:
            self.slash_sel = (self.slash_sel - 1) % len(self._slash_hits)
            return None
        if menu_open and key == curses.KEY_DOWN:
            self.slash_sel = (self.slash_sel + 1) % len(self._slash_hits)
            return None
        if menu_open and key == 9:
            pick = self._slash_hits[self.slash_sel]
            self.composer.clear()
            self.composer.insert(pick + (" " if pick in ("/session", "/expand") else ""))
            return None
        if menu_open and key in (10, 13, curses.KEY_ENTER):
            pick = self._slash_hits[self.slash_sel]
            if pick in ("/session", "/expand"):
                self.composer.clear()
                self.composer.insert(pick + " ")
                return None
            self.composer.clear()
            return self.submit(pick)
        if key in (10, 13, curses.KEY_ENTER):
            if m.focus == "transcript":
                self.dispatch({"type": "expand"})
                return None
            text = self.composer.submit()
            if text:
                return self.submit(text)
            return None
        if key == CTRL["C"]:
            if self.composer.buffer:
                self.composer.clear()
            elif m.busy:
                self.interrupt()
            elif self.confirm_quit():
                return "quit"
            return None
        if key == CTRL["Q"]:
            return "quit" if self.confirm_quit() else None
        if key == CTRL["X"]:
            self.interrupt()
        elif key == CTRL["K"]:
            self.open_queue()
        elif key == CTRL["P"]:
            self.pick_model()
        elif key == CTRL["G"]:
            self.pick_session()
        elif key == CTRL["A"]:
            self.dispatch({"type": "approval_shown", "open": True})
        elif key == CTRL["O"]:
            self.dispatch({"type": "expand_all"})
        elif key == getattr(curses, "KEY_MOUSE", None):
            self.click()
        elif key == CTRL["T"]:
            self.dispatch({"type": "expand", "index": None if m.focus == "composer" else m.selected})
        elif key == CTRL["L"]:
            self.screen.clear()
        elif key == CTRL["U"] or key == curses.KEY_PPAGE:
            self.scroll(-(self.page_rows() // (2 if key == CTRL["U"] else 1)))
        elif key == CTRL["D"] or key == curses.KEY_NPAGE:
            self.scroll(self.page_rows() // (2 if key == CTRL["D"] else 1))
        elif key == 9:
            self.cycle_role(1)
        elif key == curses.KEY_BTAB:
            self.cycle_role(-1)
        elif key == curses.KEY_F2:
            self.dispatch({"type": "focus"})
        elif key == curses.KEY_UP:
            if m.focus == "transcript":
                self.dispatch({"type": "move", "delta": -1})
            elif not self.composer.multiline:
                self.composer.recall(-1)
        elif key == curses.KEY_DOWN:
            if m.focus == "transcript":
                self.dispatch({"type": "move", "delta": 1})
            elif not self.composer.multiline:
                self.composer.recall(1)
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            self.composer.backspace()
        elif key == curses.KEY_DC:
            self.composer.delete()
        elif key == curses.KEY_LEFT:
            self.composer.left()
        elif key == curses.KEY_RIGHT:
            self.composer.right()
        elif key == curses.KEY_HOME:
            self.composer.home()
        elif key == curses.KEY_END:
            if m.focus == "transcript" or not self.composer.buffer:
                self.dispatch({"type": "scroll", "to": "end"})
            else:
                self.composer.end()
        elif key == 11:
            self.composer.kill_line()
        elif 32 <= key < 0x110000 and m.focus == "composer":
            try:
                self.composer.insert(chr(key))
            except ValueError:
                pass
        elif m.focus == "transcript" and key == ord(" "):
            self.dispatch({"type": "expand"})
        elif m.focus == "transcript" and key == ord("a"):
            self.dispatch({"type": "expand_all"})
        elif m.focus == "transcript" and key == ord("r"):
            self.retry()
        return None

    def click(self):
        """Left click on a transcript row toggles that call / chip (serve parity)."""
        try:
            _, _x, y, _, bstate = curses.getmouse()
        except curses.error:
            return
        target = self.hits.get(y)
        if target is None or not bstate & curses.BUTTON1_CLICKED:
            return
        self.dispatch({"type": "expand", "index": target, "select": True})

    def page_rows(self):
        h, w = self.screen.getmaxyx()
        return max(1, layout.compute(h, w, len(self.composer.lines(w)[0])).transcript.h)

    def scroll(self, delta):
        first, total = self.last_lines
        self.dispatch({"type": "scroll", "to": delta, "current": first,
                       "max_scroll": max(0, total - self.page_rows())})

    # -- draw ----------------------------------------------------------------
    def draw(self):
        scr, m = self.screen, self.m
        scr.erase()
        if m.roster is None or (m.active_sid and not m.lane().loaded and m.net != "down"):
            panels.draw_splash(scr, self.client.base, self.theme)
            if m.net in ("degraded", "down") and self.poll_error:
                h, w = scr.getmaxyx()
                panels.put(scr, h - 3, 0, "serve: " + self.poll_error, self.theme.TOOL_ERR)
            scr.refresh()
            return
        h, w = scr.getmaxyx()
        rows, _ = self.composer.lines(max(1, w - 2))
        rects = layout.compute(h, w, len(rows))
        panels.draw_header(scr, m, rects.header, self.theme, folded=rects.narrow)
        panels.draw_sidebar(scr, m, rects.sidebar, self.theme)
        self.hits = {}
        self.last_lines = transcript.draw_transcript(scr, m, rects.transcript, self.theme, wide=rects.wide,
                                                     hits=self.hits)
        if rects.rule.h:
            title = " prompt — / for commands · Enter send · \\+Enter newline "
            bar = "─" * max(0, rects.rule.w)
            panels.put(scr, rects.rule.y, 0, bar, self.theme.MUTED)
            panels.put(scr, rects.rule.y, 2, title, self.theme.MUTED)
        cy, cx = cv.draw_composer(scr, self.composer, rects.composer, self.theme)
        buf = self.composer.buffer
        if m.focus == "composer" and buf.startswith("/") and " " not in buf and "\n" not in buf:
            hits = [(c, d) for c, d in SLASH_MENU if c.startswith(buf)]
            self._slash_hits = [c for c, _ in hits]
            self.slash_sel = min(self.slash_sel, max(0, len(hits) - 1))
            top = max(1, rects.rule.y - len(hits))
            box_w = max(10, rects.transcript.w - 1)
            for i, (c, d) in enumerate(hits[: rects.rule.y - 1]):
                line = (" ▸ " if i == self.slash_sel else "   ") + "%-10s %s" % (c, d)
                panels.put(scr, top + i, rects.transcript.x,
                           line[:box_w].ljust(box_w),      # solid row: occlude the transcript
                           0 if i == self.slash_sel else self.theme.MUTED)
        else:
            self._slash_hits = []
            self.slash_sel = 0
        panels.draw_status(scr, m, rects.status, self.theme, self.now())
        try:
            scr.move(cy, cx)
        except curses.error:
            pass
        scr.refresh()

    def run(self):
        os.environ.setdefault("ESCDELAY", "25")
        if self.theme is None:
            self.theme = theme_mod.init()
        self.screen.keypad(True)
        self.screen.timeout(100)
        try:
            curses.curs_set(1)
            curses.raw()          # Ctrl-C / Ctrl-Q are keys (§3.1), not SIGINT / XOFF
        except curses.error:
            pass
        if os.environ.get("HUGPY_TUI_MOUSE", "1") != "0":
            try:                  # click toggles tool calls; Shift+drag still selects text
                curses.mousemask(curses.BUTTON1_CLICKED)
            except (curses.error, AttributeError):
                pass
        h, w = self.screen.getmaxyx()
        self.dispatch({"type": "resize", "h": h, "w": w})
        self.start_poller()
        try:
            while True:
                self.drain()
                if self.m.approval_open and self.m.approvals:
                    self.open_approval()
                    continue
                self.draw()
                if self.handle_key(self.getkey()) == "quit":
                    return 0
        except KeyboardInterrupt:
            return 0              # SIGINT still lands if raw() was unavailable
        finally:
            self.closed.set()
            try:
                curses.noraw()
                curses.curs_set(0)
            except curses.error:
                pass
