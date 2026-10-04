"""curses loop + threads for `hugpy-agent tui` (h26 §1.2 app.py).

Pattern is fleet_tui.Console: worker threads never touch curses, they post
`(kind, value)` tuples to a queue.Queue and the main loop `drain()`s them into
the reducer before every draw. One Poller thread reads the serve (events 1 s,
roster 10 s, state 5 s, rollover 30 s, toolserver 15 s, backoff on error); each
send runs in its own short thread (native rows stream SSE from it).
"""
from __future__ import annotations

import curses
import subprocess
import os
import queue
import threading
import time

from ..serve_client import ServeError
from ..serve_client.abstract_claude import is_cs, staged_model
from . import layout
from . import loci as loci_mod
from .state import Model, reduce
from . import loci as loci_mod
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
    "Slash: /handoff /resume /rollover /context /session <id> /model /queue /retry",
    "       /expand [n] /status /tools /help /quit",
]
SLASH_MENU = [
    ("/handoff",  "store this session's state on its toolserver row (engine)"),
    ("/resume",   "load the state this session continues (engine)"),
    ("/rollover", "roll now · on/off/auto/manual/status = auto-roller switch"),
    ("/context",  "show the whole session as fed to the model + tokens"),
    ("/session",  "switch session (or Ctrl-G picker)"),
    ("/locus",    "switch locus (named serve, ssh-tunnelled when remote)"),
    ("/model",    "model picker"),
    ("/shell",    "open a login shell (exit returns here)"),
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

# Fallback decoding for CSI sequences terminfo did not fold into a KEY_* (seen
# with Home/End over tmux: they arrive as ESC[H / ESC[F, not khome/kend). Keyed
# by the bytes AFTER "ESC[".
_CSI_KEYS = {
    "H": curses.KEY_HOME, "1~": curses.KEY_HOME, "7~": curses.KEY_HOME,
    "F": curses.KEY_END, "4~": curses.KEY_END, "8~": curses.KEY_END,
    "A": curses.KEY_UP, "B": curses.KEY_DOWN, "C": curses.KEY_RIGHT, "D": curses.KEY_LEFT,
    "5~": curses.KEY_PPAGE, "6~": curses.KEY_NPAGE, "Z": curses.KEY_BTAB, "3~": curses.KEY_DC,
}


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
        self.side_hits = {}             # screen row -> session id (sidebar clicks)
        self.side_w = 0                 # sidebar width at last draw (0 when folded)
        self._sel_seen = -1             # last drawn selection: follow it only when it CHANGES
        self.now = time.monotonic
        # -- loci: named serves the operator can switch between (tui/loci.py)
        self.loci = loci_mod.load_loci()
        self.tunnels = loci_mod.Tunnels()
        self.active_locus = next((e["locus"] for e in self.loci
                                  if (e.get("serve") or "").rstrip("/") == client.base.rstrip("/")), "")
        if not self.active_locus:
            self.loci.insert(0, {"locus": "here", "serve": client.base})
            self.active_locus = "here"
        self._locus_held = {}           # locus -> (client, Model) parked by switch_locus
        self.locus_hits = []            # [(x0, x1, locus)] on the header row (mouse)
        self.pause_poll = threading.Event()

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
            if self.pause_poll.is_set():
                self._sleep(0.1)
                continue
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
        self.dispatch({"type": "scroll", "to": "end"})
        sid = self.m.active_sid
        if not sid:
            # no session yet: the serve mints one for sid "new"; the receipt
            # carries the real id and _sent adopts it as active.
            sid = "new"
            self.dispatch({"type": "notice", "text": "starting a new session…"})
        self.dispatch({"type": "local_user", "sid": sid, "text": text, "now": self.now()})
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
        elif cmd == "/shell":
            self.open_shell()
        elif cmd == "/session":
            self.pick_session(args[0] if args else None)
        elif cmd == "/locus":
            self.pick_locus(args[0] if args else None)
        elif cmd == "/queue":
            self.open_queue()
        elif cmd == "/retry":
            self.retry()
        elif cmd == "/expand":
            index = int(args[0]) if args and args[0].isdigit() else None
            self.dispatch({"type": "expand", "index": index})
        elif cmd == "/rollover":
            if args and args[0].lower() in ("on", "off", "auto", "manual", "status"):
                self.roller_switch(args[0].lower())
                return
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

    def roller_switch(self, mode):
        """/rollover on|off|auto|manual|status — the serve's auto-roller switch."""
        try:
            if mode == "status":
                doc = self.client.rollover() or {}
                pol = doc.get("policy") or {}
                pend = doc.get("pending") or {}
                text = "roller: %s · %sk ctx · sweep %ss" % (
                    pol.get("rollover_mode", "?"),
                    int(pol.get("rollover_context_tokens", 0) or 0) // 1000,
                    pol.get("rollover_sweep_s", "?"))
                if pend:
                    text += " · PENDING %s" % str(pend.get("session_id", ""))[:8]
                self.dispatch({"type": "notice", "text": text})
                return
            if not hasattr(self.client, "roll_mode"):
                self.dispatch({"type": "notice", "text": "this serve has no roller switch"})
                return
            res = self.client.roll_mode(mode)
            if res.get("ok"):
                text = "roller → %s" % res.get("mode")
                if res.get("warning"):
                    text += " ⚠ %s" % res["warning"]
            else:
                text = "roller: %s" % res.get("error")
            self.dispatch({"type": "notice", "text": text})
        except ServeError as exc:
            self.dispatch({"type": "notice", "text": "roller: %s" % exc})

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
            events = self.client.events(sid, "0").events   # EventPage(events, busy, queue, cursor, source, truncated)
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
        """Name + first description line, grouped by category prefix — a wall
        of bare mcp names is unscannable (operator 2026-10-02)."""
        lines = []
        try:
            if self.tools_list:
                by_cat = {}
                for t in self.tools_list() or []:
                    if isinstance(t, str):
                        name, desc = t, ""
                    else:
                        name = t.get("name") or str(t)
                        desc = (t.get("description") or "").strip().split("\n", 1)[0]
                        if len(desc) > 140:
                            desc = desc[:139] + "…"
                    by_cat.setdefault(name.split("_", 1)[0], []).append((name, desc))
                for cat in sorted(by_cat):
                    lines.append("── %s (%d) " % (cat, len(by_cat[cat])))
                    w = max(len(n) for n, _ in by_cat[cat])
                    for name, desc in sorted(by_cat[cat]):
                        lines.append("  %-*s  %s" % (w, name, desc) if desc else "  " + name)
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
        try:
            if self.client.kind == "hugpy":
                self.client.set_model((row.id if row else self.m.active_sid), chosen.model)
                note = "model: " + chosen.model
            else:
                # abstract-claude roster set_provider/set_model key on a STANDING
                # ROLE. A plain cs-* / native session has no role, so posting its
                # id as a role silently no-ops — refuse with a factual notice
                # rather than pretend it took (and avoid row.backend on None).
                if not (row and row.role):
                    self.dispatch({"type": "notice",
                                   "text": "model is set per role — switch to a role (Tab) to change its model"})
                    return
                if chosen.backend and chosen.backend != row.backend:
                    self.client.set_provider(row.role, chosen.backend, chosen.model)
                    note = "provider → %s/%s" % (chosen.backend, chosen.model or "default")
                else:
                    doc = self.client.set_model(row.role, chosen.model)
                    note = "model: %s%s" % (chosen.model or "default",
                                            " (staged)" if staged_model(doc, row.role) else "")
            self.dispatch({"type": "notice", "text": note})
            self.roster_now.set()
        except ServeError as exc:
            self.dispatch({"type": "notice", "text": str(exc)})

    def pick_locus(self, wanted=None):
        """/locus [name] — picker over tui-loci.json; switches the whole serve."""
        if not self.loci:
            self.dispatch({"type": "notice", "text": "no loci (write ~/.hugpy/tui-loci.json)"})
            return
        if wanted:
            entry = next((e for e in self.loci if e["locus"].lower() == wanted.lower()
                          or e["locus"].lower().startswith(wanted.lower())), None)
            if entry is None:
                self.dispatch({"type": "notice", "text": "no locus %s" % wanted})
                return
            self.switch_locus(entry)
            return
        labels = ["%-10s %s%s" % (e["locus"], e.get("serve") or ("ssh " + e.get("ssh", "")),
                                  " · current" if e["locus"] == self.active_locus else "")
                  for e in self.loci]
        current = next((i for i, e in enumerate(self.loci) if e["locus"] == self.active_locus), 0)
        pick = modals.choose(self.screen, "LOCI", labels, self.theme, self.drain, selected=current)
        if pick is not None:
            self.switch_locus(self.loci[pick])

    def switch_locus(self, entry):
        """Park the current (client, model), connect to the entry's serve
        (opening its ssh tunnel when remote) and adopt or create its state."""
        if entry["locus"] == self.active_locus:
            return
        from ..serve_client import connect
        from .discovery import identify, probe
        self.dispatch({"type": "notice", "text": "locus %s: connecting…" % entry["locus"]})
        self.draw()
        self.pause_poll.set()           # the poller must not race the swap
        try:
            held = self._locus_held.pop(entry["locus"], None)
            if held is None:
                base = self.tunnels.base_for(entry)
                kind = identify(probe(base, timeout=2.0))
                if kind is None:
                    raise RuntimeError("%s is not a serve" % base)
                held = (connect(base, kind), Model(kind=kind, base=base))
            self._locus_held[self.active_locus] = (self.client, self.m)
            self.client, self.m = held
            self.active_locus = entry["locus"]
            self.sse_active = set()
            self.roster_now.set()
            self.dispatch({"type": "notice", "text": "locus %s" % entry["locus"]})
        except (RuntimeError, ServeError, OSError) as exc:
            self.dispatch({"type": "notice", "text": "locus %s: %s" % (entry["locus"], exc)})
        finally:
            self.pause_poll.clear()

    def pick_session(self, wanted=None):
        roster = self.m.roster
        if not roster or not (roster.roles or roster.sessions):
            self.dispatch({"type": "notice",
                           "text": "no sessions on this serve yet — type a prompt to start one"})
            return
        rows = [r for r in roster.roles if r.id] + [s for s in roster.sessions if s.id not in {r.id for r in roster.roles}]
        if wanted:
            wl = wanted.lower()
            for r in rows:
                if wanted == r.id or wl == (r.role or "").lower() or wl == (r.label or "").lower() \
                        or r.id.startswith(wanted):
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

    def open_shell(self):
        """The standing shell row: hand the terminal to a login shell, then
        restore the TUI exactly as it was when the shell exits."""
        curses.def_prog_mode()
        curses.endwin()
        try:
            print("hugpy-agent tui: shell — type `exit` to return", flush=True)
            subprocess.call([os.environ.get("SHELL") or "/bin/bash", "-l"])
        except OSError as exc:
            print("shell failed: %s" % exc, flush=True)
        finally:
            curses.reset_prog_mode()
            self.screen.clear()
            self.screen.refresh()

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

    def _menu_open(self):
        """True while the slash command menu is on screen (composer focus, the
        buffer is a single `/token` and draw() found matches)."""
        return (self.m.focus == "composer" and bool(self._slash_hits)
                and self.composer.buffer.startswith("/")
                and " " not in self.composer.buffer)

    def _read_csi(self):
        """Read the tail of an ESC-[ control sequence (already consumed ESC and
        '['); returns e.g. '200~' / '201~' / 'A'. Reads nodelay — the terminal
        sends the whole sequence in one burst — tolerating a few empty reads."""
        out, misses = [], 0
        while misses < 3:
            ch = self.getkey()
            if ch == -1:
                misses += 1
                continue
            misses = 0
            c = ch if isinstance(ch, str) else (chr(ch) if 32 <= ch < 0x110000 else "")
            if not c:
                break
            out.append(c)
            if c.isalpha() or c == "~":
                break
        return "".join(out)

    def _paste(self):
        """Insert a bracketed-paste payload literally (CR -> newline) instead of
        letting each line's Enter submit. Ends at ESC[201~ or a 2 s deadline."""
        buf, deadline = [], self.now() + 2.0
        while self.now() < deadline:
            ch = self.getkey()
            if ch == -1:
                time.sleep(0.005)                          # paste may arrive chunked; wait for 201~
                continue
            if ch == 27:
                nxt = self.getkey()
                if nxt in (ord("["), "["):
                    if self._read_csi() == "201~":
                        break
                continue
            if isinstance(ch, str):
                buf.append(ch)
            elif ch in (10, 13):
                buf.append("\n")
            elif 32 <= ch < 0x110000:
                buf.append(chr(ch))
        text = "".join(buf)
        if text and self.m.focus == "composer":
            self.composer.insert(text)

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
            else:
                # typing always types: any other printable bounces focus back
                self.dispatch({"type": "focus", "which": "composer"})
                self.composer.insert(key)
            return None
        menu_open = self._menu_open()
        if key == 27:                                     # Esc / Alt-chord / CSI
            self.screen.nodelay(True)
            try:
                nxt = self.getkey()
                if nxt in (ord("["), "["):
                    seq = self._read_csi()
                    if seq == "200~":                      # bracketed paste start
                        self._paste()
                        return None
                    mapped = _CSI_KEYS.get(seq)            # Home/End/etc terminfo missed
                    if mapped is not None:
                        self.screen.timeout(100)           # restore before re-dispatch (finally also will)
                        return self.handle_key(mapped)
                    return None                            # other CSI: swallow quietly
            finally:
                self.screen.timeout(100)                   # NEVER nodelay(False): keep the poll tick alive
            if nxt in (10, 13):
                self.composer.newline()
            elif nxt == -1:                                # bare Esc
                if menu_open:
                    self.composer.clear()                  # Esc closes the slash menu
                else:
                    self.dispatch({"type": "notice", "text": ""})
            elif isinstance(nxt, str) and m.focus == "composer":
                self.composer.insert(nxt)                  # Alt+<letter>: don't swallow the letter
            return None
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
            # transcript focus with a real selection expands it; otherwise (incl.
            # selected == -1) fall through to _expand's latest-card default.
            index = m.selected if (m.focus == "transcript" and m.selected != -1) else None
            self.dispatch({"type": "expand", "index": index})
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
                if m.lane().scroll < 0:
                    self.dispatch({"type": "scroll", "to": 0, "current": self.last_lines[0]})
                self.dispatch({"type": "move", "delta": -1})
            elif self.composer.multiline:
                self.composer.up()               # caret up one line within the composer
            else:
                self.composer.recall(-1)
        elif key == curses.KEY_DOWN:
            if m.focus == "transcript":
                if m.lane().scroll < 0:
                    self.dispatch({"type": "scroll", "to": 0, "current": self.last_lines[0]})
                self.dispatch({"type": "move", "delta": 1})
            elif self.composer.multiline:
                self.composer.down()
            else:
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
            if m.focus == "transcript":
                # jump selection to the first block so the view follows to the top
                # (a bare scroll-to-top would be yanked back by selection-follow).
                self.dispatch({"type": "scroll", "to": "top"})
                self.dispatch({"type": "move", "delta": -(10 ** 6)})
            else:
                self.composer.home()
        elif key == curses.KEY_END:
            if m.focus == "transcript":
                self.dispatch({"type": "scroll", "to": "end"})
                self.dispatch({"type": "move", "delta": 10 ** 6})
            elif not self.composer.buffer:
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
        elif 32 <= key < 0x110000:
            # typing always types: printable in transcript focus returns to the composer
            self.dispatch({"type": "focus", "which": "composer"})
            try:
                self.composer.insert(chr(key))
            except ValueError:
                pass
        return None

    def click(self):
        """Left click on a transcript row toggles that call / chip (serve parity);
        the scroll wheel scrolls the transcript."""
        try:
            _, _x, y, _, bstate = curses.getmouse()
        except curses.error:
            return
        up = getattr(curses, "BUTTON4_PRESSED", 0)
        down = getattr(curses, "BUTTON5_PRESSED", 0)
        if up and bstate & up:
            self.scroll(-max(1, self.page_rows() // 3))
            return
        if down and bstate & down:
            self.scroll(max(1, self.page_rows() // 3))
            return
        if not bstate & curses.BUTTON1_CLICKED:
            return
        if y == 0 and self.locus_hits:                 # header: click a locus tab
            for x0, x1, name in self.locus_hits:
                if x0 <= _x <= x1:
                    self.pick_locus(name)
                    return
            return
        if self.side_w and _x <= self.side_w:          # sidebar: click a row to switch
            sid = self.side_hits.get(y)
            if sid == panels.SHELL_ROW:
                self.open_shell()
                return
            if sid is not None and sid != self.m.active_sid:
                self.dispatch({"type": "select", "sid": sid})
            return
        target = self.hits.get(y)
        if target is None:
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
        ever_loaded = any(l.loaded for l in m.lanes.values())
        if (m.roster is None or (m.active_sid and not m.lane().loaded)) and not ever_loaded and m.net != "down":
            panels.draw_splash(scr, self.client.base, self.theme)
            if m.net in ("degraded", "down") and self.poll_error:
                h, w = scr.getmaxyx()
                panels.put(scr, h - 3, 0, "serve: " + self.poll_error, self.theme.TOOL_ERR)
            scr.refresh()
            return
        h, w = scr.getmaxyx()
        rows, _ = self.composer.lines(max(1, w - 2))
        rects = layout.compute(h, w, len(rows))
        self.locus_hits = []
        panels.draw_header(scr, m, rects.header, self.theme, folded=rects.narrow,
                           loci=self.loci, active_locus=self.active_locus, locus_hits=self.locus_hits)
        self.side_hits = {}
        self.side_w = rects.sidebar.w
        panels.draw_sidebar(scr, m, rects.sidebar, self.theme, hits=self.side_hits)
        self.hits = {}
        self.last_lines = transcript.draw_transcript(scr, m, rects.transcript, self.theme, wide=rects.wide,
                                                     hits=self.hits, follow_sel=m.selected != self._sel_seen)
        self._sel_seen = m.selected
        if m.active_sid and not m.lane().loaded:
            panels.put(scr, rects.transcript.y, rects.transcript.x,
                       "loading session %s …" % m.active_sid[:13], self.theme.MUTED)
        if rects.rule.h:
            title = " prompt — / for commands · Enter send · \\+Enter newline "
            bar = "─" * max(0, rects.rule.w)
            panels.put(scr, rects.rule.y, 0, bar, self.theme.MUTED)
            panels.put(scr, rects.rule.y, 2, title, self.theme.MUTED)
            # focus cue: which pane keys drive, right-aligned on the rule row.
            marker = "[transcript ↑↓ select]" if m.focus == "transcript" else "[composer]"
            panels.put(scr, rects.rule.y, max(0, rects.rule.w - len(marker) - 2), marker,
                       self.theme.ACCENT if m.focus == "transcript" else self.theme.MUTED)
        cy, cx = cv.draw_composer(scr, self.composer, rects.composer, self.theme)
        if m.focus == "transcript" and not self.composer.buffer:
            # The composer is not receiving keys: say so where the operator is looking.
            panels.put(scr, rects.composer.y, rects.composer.x + 2,
                       "transcript selected — just type (or F2) to return to the prompt",
                       self.theme.ACCENT, rects.composer.w - 2)
        buf = self.composer.buffer
        if m.focus == "composer" and buf.startswith("/") and " " not in buf and "\n" not in buf:
            max_rows = max(1, rects.rule.y - 1)            # only as many rows as fit above the rule
            hits = [(c, d) for c, d in SLASH_MENU if c.startswith(buf)][:max_rows]
            self._slash_hits = [c for c, _ in hits]
            self.slash_sel = min(self.slash_sel, max(0, len(hits) - 1))
            top = max(1, rects.rule.y - len(hits))
            box_w = max(10, rects.transcript.w - 1)
            for i, (c, d) in enumerate(hits):
                line = (" ▸ " if i == self.slash_sel else "   ") + "%-10s %s" % (c, d)
                panels.put(scr, top + i, rects.transcript.x,
                           line[:box_w].ljust(box_w),      # solid row: occlude the transcript
                           0 if i == self.slash_sel else self.theme.MUTED)
        else:
            self._slash_hits = []
            self.slash_sel = 0
        panels.draw_status(scr, m, rects.status, self.theme, self.now())
        try:
            curses.curs_set(1 if m.focus == "composer" else 0)   # park the caret off the transcript
        except curses.error:
            pass
        if m.focus == "composer":
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
            try:                  # click toggles tool calls; wheel scrolls; Shift+drag still selects text
                mask = curses.BUTTON1_CLICKED | getattr(curses, "BUTTON4_PRESSED", 0) \
                    | getattr(curses, "BUTTON5_PRESSED", 0)
                curses.mousemask(mask)
            except (curses.error, AttributeError):
                pass
        self._paste_mode(True)    # bracketed paste: multi-line pastes insert, never auto-submit
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
            self.tunnels.close()
            self._paste_mode(False)
            try:
                curses.noraw()
                curses.curs_set(0)
            except curses.error:
                pass

    @staticmethod
    def _paste_mode(on):
        """Toggle the terminal's bracketed-paste mode (DECSET 2004). Written
        straight to the tty so pasted newlines arrive wrapped in ESC[200~/201~
        instead of as submitting Enters."""
        try:
            import sys
            sys.stdout.write("\x1b[?2004h" if on else "\x1b[?2004l")
            sys.stdout.flush()
        except Exception:
            pass
