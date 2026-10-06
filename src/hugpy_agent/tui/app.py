"""curses loop + threads for `hugpy-agent tui` (h26 §1.2 app.py).

Pattern is fleet_tui.Console: worker threads never touch curses, they post
`(kind, value, gen)` tuples to a queue.Queue and the main loop `drain()`s them
into the reducer before every draw. One Poller thread reads the serve (events
1 s, roster 10 s, state 5 s, rollover 30 s, backoff on error), one probe
thread reads the toolserver (15 s); each send runs in its own short thread
(native rows stream SSE from it).

Operational rules (enterprise hardening, 0.1.111):

* The main thread never blocks on the network without telling the operator:
  data a modal needs is fetched by `wait()` (status-line spinner, Esc cancels,
  keys typed meanwhile are replayed in order); fire-and-forget calls go
  through `bg()`; a stale-receipt check is a background task.
* No thread dies silently: the poller survives any exception, the main loop
  has a crash guard, thread tracebacks and stray stderr go to the log
  (tui/diag.py, ~/.hugpy/logs/tui.log) — never over the screen.
* Every queued message carries the locus generation it was produced under;
  after a locus switch the old serve's late replies are dropped, not applied
  to the new serve's state.
"""
from __future__ import annotations

import collections
import curses
import os
import queue
import subprocess
import sys
import threading
import time

from ..serve_client import ServeError
from ..serve_client.abstract_serve import is_console_session
from . import layout
from . import loci as loci_mod
from . import output
from .diag import Diag
from .state import Model, reduce
from .views import composer as cv
from .views import modals, panels, theme as theme_mod, transcript

HELP = [
    "Enter send · \\+Enter / Alt+Enter newline · Up/Down history (single line) or caret (multi-line)",
    "Ctrl-C clear composer / interrupt / quit · Ctrl-X interrupt · Ctrl-Q quit",
    "Tab / Shift-Tab next / previous role · Ctrl-G session picker · Ctrl-P model picker",
    "F1 this help · F2 focus transcript <-> composer · F3 find next · Ctrl-L redraw",
    "",
    "Transcript focus (F2): Up/Down select · Enter/Space/click expand · a toggle all",
    "  y copy the selected block to the clipboard (OSC 52) · r retry / un-hold",
    "  any other key returns to the composer and types",
    "Tool calls: ▸ ⚒ collapsed call (✓ ok ✗ error … running – no result) · ▸ ⚙ N calls = consecutive calls",
    "  Ctrl-O toggle all cards · Ctrl-T expand the latest card",
    "PgUp/PgDn, Ctrl-U/Ctrl-D scroll · Home top · End follow tail · mouse wheel scrolls",
    "Ctrl-K queue · Ctrl-A reopen a hidden approval",
    "",
    "Pickers: Up/Down · Enter choose · / filter · Esc back",
    "Approvals: y allow/accept · a for this session · n deny/decline · c cancel · Esc hide",
    "Waiting on the serve (⋯ in the status bar): Esc cancels, keys typed meanwhile are kept",
    "",
    "Shift + / (? on an empty prompt) or /cli: open this role's own CLI (claude / codex), fresh,",
    "       resumed from the locus ledger — every native / option (/model /effort /login …); exit returns",
    "",
    "Slash: /handoff /resume /rollover /context /session <id> /clear /new /locus /model /cli",
    "       /emergency /shell /queue /retry /expand [n] /status /tools",
    "       /find <text> /copy /export [path] /log /help /quit",
    "",
    "Log: every notice and internal error is kept in /log and %s",
]
SLASH_MENU = [
    ("/handoff",  "store this session's state on its toolserver row (engine)"),
    ("/resume",   "load the state this session continues (engine)"),
    ("/rollover", "roll now · on/off/auto/manual/status = auto-roller switch"),
    ("/context",  "show the whole session as fed to the model + tokens"),
    ("/session",  "switch session (or Ctrl-G picker)"),
    ("/clear",    "wipe the model's context for this session (transcript kept)"),
    ("/new",      "start a fresh session"),
    ("/emergency", "break-glass: run a local GGUF as an agent (picker; [name]|auto)"),
    ("/locus",    "switch locus (named serve, ssh-tunnelled when remote)"),
    ("/model",    "model picker"),
    ("/cli",      "open this role's own CLI, resumed from the ledger — all its / options (Shift + /)"),
    ("/shell",    "open a login shell (exit returns here)"),
    ("/queue",    "queue view"),
    ("/retry",    "retry / un-hold"),
    ("/expand",   "expand card [n]"),
    ("/find",     "find text in the transcript (F3 next)"),
    ("/copy",     "copy the selected block / last reply (OSC 52)"),
    ("/export",   "write this transcript to a Markdown file [path]"),
    ("/status",   "status line into transcript"),
    ("/tools",    "tool list"),
    ("/log",      "diagnostics + notice/error log"),
    ("/help",     "keys and commands"),
    ("/quit",     "leave the TUI"),
]
ARG_COMMANDS = ("/session", "/expand", "/find")      # menu Enter inserts these with a space

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
    "11~": curses.KEY_F1, "13~": curses.KEY_F3,
}

NOTICE_S = 8.0              # an info notice fades after this long
NOTICE_ERROR_S = 20.0       # an error lingers (it usually names the fix)
FAULT_WINDOW_S = 5.0        # this many main-loop faults inside the window ...
FAULT_LIMIT = 8             # ... stop the TUI cleanly instead of spinning
JOIN_S = 1.0                # how long quit waits for in-flight background calls


class Cancelled(ServeError):
    """The operator pressed Esc while the TUI waited on the serve."""


def version():
    """The version of the code actually running. From a source tree
    (src/hugpy_agent next to pyproject.toml: checkouts, the rig, dev runs) that
    is the tree's pyproject version — the installed dist's metadata would name
    a different build."""
    import re
    src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    pyproject = os.path.join(os.path.dirname(src), "pyproject.toml")
    if os.path.basename(src) == "src" and os.path.isfile(pyproject):
        try:
            with open(pyproject, encoding="utf-8") as fh:
                found = re.search(r'^version\s*=\s*"([^"]+)"', fh.read(), re.M)
            if found:
                return found.group(1)
        except OSError:
            pass
    try:
        from importlib.metadata import version as _v
        return _v("hugpy-agent")
    except Exception:
        return "dev"


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


class _Keys:
    """The screen as modals see it: type-ahead captured during a wait() is
    replayed first, in order, as int key codes."""

    def __init__(self, app):
        self._app = app

    def getch(self):
        if self._app.typeahead:
            key = self._app.typeahead.popleft()
            return ord(key) if isinstance(key, str) and len(key) == 1 else key
        return self._app.screen.getch()

    def __getattr__(self, name):
        return getattr(self._app.screen, name)


class App:
    def __init__(self, screen, client, session=None, toolserver_status=None, theme=None, diag=None):
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
        self.diag = diag or Diag()
        self.version = version()
        self.poll_error = ""
        self.last_lines = (0, 0)
        self.hits = {}                  # screen row -> transcript target (mouse clicks)
        self.side_hits = {}             # screen row -> session id (sidebar clicks)
        self.side_w = 0                 # sidebar width at last draw (0 when folded)
        self._sel_seen = -1             # last drawn selection: follow it only when it CHANGES
        self.now = time.monotonic
        self.gen = 0                    # locus generation: tags every queued message
        self.typeahead = collections.deque()
        self.keys = _Keys(self)         # what modals read keys from
        self.workers = []               # background threads joined (briefly) at quit
        self.faults = collections.deque()
        self.exit_message = ""
        self._notice_seen = ("", 0.0)
        self._waiting = ""
        self.tty_write = self._tty_write
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
    def send(self, kind, value, gen=None):
        """Thread-safe: queue a message for the main loop. `gen` is the locus
        generation the producer started under (default: now)."""
        if not self.closed.is_set():
            self.events.put((kind, value, self.gen if gen is None else gen))

    def dispatch(self, action):
        self.m = reduce(self.m, action)

    def say(self, text, level="info"):
        """Main-thread notice + log entry. Errors draw red, linger, count in
        the status bar until /log is opened."""
        self.dispatch({"type": "notice", "text": text, "level": level})
        if level == "error":
            self.diag.warn(text)
            self.dispatch({"type": "alerts", "add": 1})
        elif text:
            self.diag.info(text)

    def drain(self):
        while True:
            try:
                kind, value, gen = self.events.get_nowait()
            except queue.Empty:
                break
            if gen != self.gen:
                continue                                    # produced for a locus we left
            if kind == "action":
                self.dispatch(value)
            elif kind == "notice":
                self.say(value)
            elif kind == "error":
                self.say(value, "error")
            elif kind == "call":
                value()
        now = self.now()
        self.dispatch({"type": "tick", "now": now})
        for r in list(self.m.pending_receipts):
            if r["unacked"] and not r.get("checking") and now >= r.get("retry_at", 0):
                self._check_receipt(r)
        self._fade_notice(now)

    def _fade_notice(self, now):
        text = self.m.notice
        if text != self._notice_seen[0]:
            self._notice_seen = (text, now)
        elif text and not self._waiting:
            ttl = NOTICE_ERROR_S if self.m.notice_level == "error" else NOTICE_S
            if now - self._notice_seen[1] > ttl:
                self.dispatch({"type": "notice", "text": ""})

    def _check_receipt(self, entry):
        """Stale receipt (§2.4): queued -> say so; absent -> prompt lost. A
        background task: with the serve down this used to block the main
        loop for the HTTP timeout on every tick."""
        entry["checking"] = True
        sid = self.m.active_sid

        def done(q):
            entry["checking"] = False
            self.dispatch({"type": "queue", "queue": q})
            if q and q.items:
                self.dispatch({"type": "receipt_lost", "receipt": entry["receipt"], "notice": "prompt queued — r retry"})
            else:
                self.dispatch({"type": "receipt_lost", "receipt": entry["receipt"]})

        def failed(_exc):
            entry["checking"] = False
            entry["retry_at"] = self.now() + 10

        self.bg(lambda: self.client.queue(sid), done, quiet=True, on_error=failed)

    # -- background work ---------------------------------------------------------
    def _track(self, thread):
        self.workers = [t for t in self.workers if t.is_alive()] + [thread]

    def bg(self, fn, then=None, label="", quiet=False, on_error=None):
        """Run `fn()` off the main thread; `then(result)` runs ON the main
        thread at the next drain (same locus only). Failures become an error
        notice (`label: why`) and a log row, never a traceback."""
        gen, client = self.gen, self.client

        def work():
            try:
                result = fn()
            except ServeError as exc:
                if on_error is not None:
                    self.send("call", lambda: on_error(exc), gen)
                if not quiet:
                    self.send("error", "%s%s" % (label + ": " if label else "", exc), gen)
                return
            except Exception as exc:                       # noqa: BLE001 — report, never die
                self.diag.error("background task failed (%s)" % (label or getattr(fn, "__name__", "?")), exc)
                if on_error is not None:
                    self.send("call", lambda: on_error(exc), gen)
                if not quiet:
                    self.send("error", "%s%s: %s (logged)" % (label + ": " if label else "", type(exc).__name__, exc),
                              gen)
                return
            if then is not None and client is self.client:
                self.send("call", lambda: then(result), gen)
        thread = threading.Thread(target=work, daemon=True, name="tui-bg")
        thread.start()
        self._track(thread)
        return thread

    def wait(self, label, fn):
        """Run `fn()` while the screen stays live; return its value or raise
        its exception. Esc / Ctrl-C cancel (raises Cancelled; the call itself
        finishes in the background). Keys typed meanwhile are replayed."""
        box = {}

        def work():
            try:
                box["value"] = fn()
            except BaseException as exc:                   # noqa: BLE001 — re-raised below
                box["error"] = exc
        thread = threading.Thread(target=work, daemon=True, name="tui-wait")
        thread.start()
        thread.join(0.05)
        if thread.is_alive():
            self._track(thread)
            note = "⋯ %s — Esc cancels" % label
            self.say(note)
            self._waiting = label
            try:
                while thread.is_alive():
                    self.drain()
                    self.draw()
                    key = self.getkey(raw=True)
                    if key in (27, CTRL["C"]):
                        self.say("%s: cancelled" % label)
                        raise Cancelled("cancelled")
                    if key not in (-1, None):
                        self.typeahead.append(key)
                    thread.join(0.0)
            finally:
                self._waiting = ""
                if self.m.notice == note:
                    self.dispatch({"type": "notice", "text": ""})
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def join_workers(self, timeout=JOIN_S):
        deadline = time.monotonic() + timeout
        for thread in list(self.workers):
            left = deadline - time.monotonic()
            if left <= 0:
                break
            thread.join(left)

    # -- poller ------------------------------------------------------------------
    def start_poller(self):
        threading.Thread(target=self._poll_loop, daemon=True, name="tui-poll").start()
        if self.tools_status:
            threading.Thread(target=self._tools_loop, daemon=True, name="tui-tools").start()
        self.refresh_loci()

    def refresh_loci(self):
        """Read the toolserver's loci registry off the main thread and adopt it.
        Unreachable or refused: the file's loci stay, with a log row."""
        if not loci_mod.registry_enabled():
            return

        def work():
            try:
                found = loci_mod.fetch_registry()
            except Exception as exc:                       # noqa: BLE001 — the file is the fallback
                self.diag.warn("loci registry: %s: %s" % (type(exc).__name__, exc))
                return
            self.send("call", lambda: self.adopt_loci(found))
        threading.Thread(target=work, daemon=True, name="tui-loci").start()

    def adopt_loci(self, registry):
        """Main thread: the registry's loci replace the list; the locus we are on
        and the parked ones keep their entries whatever the registry says."""
        merged = loci_mod.merge(registry, loci_mod.load_loci())
        base = self.client.base.rstrip("/")
        if self.active_locus == "here" and not self._locus_held:
            self.active_locus = next((e["locus"] for e in merged
                                      if (e.get("serve") or "").rstrip("/") == base), "here")
        named = {e["locus"] for e in merged}
        keep = [e for e in self.loci if e["locus"] not in named
                and (e["locus"] == self.active_locus or e["locus"] in self._locus_held)]
        self.loci = keep + merged

    def _poll_loop(self):
        due = {"roster": 0.0, "state": 0.0, "rollover": 0.0, "usage": 0.0}
        faults = 0
        while not self.closed.is_set():
            if self.pause_poll.is_set():
                self._sleep(0.1)
                continue
            gen, client, m = self.gen, self.client, self.m
            now = self.now()
            if m.net_retry_at and now < m.net_retry_at:
                self._sleep(0.2)
                continue
            try:
                if now >= due["roster"] or m.roster_stale or self.roster_now.is_set():
                    self.roster_now.clear()
                    self.send("action", {"type": "roster", "roster": client.roster(), "prefer": self.prefer}, gen)
                    due["roster"] = now + 10
                    m = self.m if self.m.active_sid else m
                sid = m.active_sid
                if sid and sid not in self.sse_active:
                    lane = m.lane(sid)
                    since = "0" if (lane.rebaseline or not lane.loaded) else lane.cursor
                    page = client.events(sid, since)
                    self.send("action", {"type": "events", "sid": sid, "page": page, "now": now,
                                         "wall": time.time()}, gen)
                if now >= due["state"]:
                    client.state()
                    due["state"] = now + 5
                if sid and not is_console_session(sid) and now >= due["usage"] and hasattr(client, "usage"):
                    usage = client.usage(sid)
                    if usage is not None:
                        self.send("action", {"type": "usage", "usage": usage}, gen)
                    due["usage"] = now + 15
                if sid and not is_console_session(sid) and now >= due["rollover"]:
                    self._follow_rollover(client, sid, gen)
                    due["rollover"] = now + 30
                self.poll_error = ""
                self.send("action", {"type": "net", "ok": True, "now": now}, gen)
                faults = 0
            except ServeError as exc:
                self.poll_error = str(exc)
                self.send("action", {"type": "net", "ok": False, "now": now}, gen)
                if m.net == "live" or m.net == "connecting":
                    self.send("error", "serve: " + str(exc), gen)
                self.diag.warn("poll %s: %s" % (client.base, exc))
            except Exception as exc:                       # noqa: BLE001 — the poller must not die
                faults += 1
                self.poll_error = "%s: %s" % (type(exc).__name__, exc)
                self.diag.error("poll failed (%s)" % client.base, exc)
                if faults == 1 or faults % 30 == 0:
                    self.send("error", "poll: %s — still polling (/log)" % self.poll_error, gen)
                self._sleep(min(10.0, float(faults)))
                continue
            self._sleep(1.0)

    def _tools_loop(self):
        """Toolserver health on its own thread: a slow toolserver must never
        stall the serve poll."""
        while not self.closed.is_set():
            try:
                text = tools_field(self.tools_status())
            except Exception as exc:                       # noqa: BLE001
                self.diag.warn("toolserver probe: %s: %s" % (type(exc).__name__, exc))
                text = "off"
            self.send("action", {"type": "tools", "text": text})
            self._sleep(15.0)

    def _follow_rollover(self, client, sid, gen):
        doc = client.rollover() or {}
        archived = (doc.get("archived") or {}).get(sid) or {} if isinstance(doc, dict) else {}
        successor = archived.get("successor") if isinstance(archived, dict) else None
        if successor and successor != sid:
            self.send("action", {"type": "select", "sid": successor}, gen)
            self.send("notice", "session rolled over → %s" % successor[:8], gen)
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
            self.say("starting a new session…")
        self.dispatch({"type": "local_user", "sid": sid, "text": text, "now": self.now()})
        self._track_send(sid, text)

    def _track_send(self, sid, text):
        thread = threading.Thread(target=self._send, args=(sid, text, self.gen, self.client),
                                  daemon=True, name="tui-send")
        thread.start()
        self._track(thread)

    def _send(self, sid, text, gen=None, client=None):
        gen = self.gen if gen is None else gen
        client = client or self.client
        native = client.kind == "abstract-serve" and not is_console_session(sid)
        if native:
            self.sse_active.add(sid)
        try:
            had_sid = bool(self.m.active_sid)
            receipt = client.send(sid, text, on_event=(lambda ev: self.send(
                "action", {"type": "sse_event", "sid": sid, "event": ev, "now": self.now()}, gen)) if native else None)
            self.send("action", {"type": "sent", "receipt": receipt, "now": self.now()}, gen)
            if not had_sid:
                self.roster_now.set()      # the new session shows up in pickers at once
        except ServeError as exc:
            self.send("error", "send failed: %s" % exc, gen)
        except (TimeoutError, OSError) as exc:
            # a dropped stream must surface as a notice, never a thread
            # traceback sprayed over the curses screen
            self.send("error", "stream lost (%s) — /retry or resend" % type(exc).__name__, gen)
        except Exception as exc:                           # noqa: BLE001
            self.diag.error("send thread failed", exc)
            self.send("error", "send failed: %s: %s (logged)" % (type(exc).__name__, exc), gen)
        finally:
            if native:
                self.sse_active.discard(sid)
                self.send("action", {"type": "sse_done", "sid": sid}, gen)

    def interrupt(self):
        sid = self.m.active_sid
        if not sid:
            return
        client = self.client
        self.bg(lambda: client.interrupt(sid), lambda _r: self.say("interrupt sent"), label="interrupt")

    def clear_context(self):
        """/clear — wipe the model's context for the active session. The
        transcript display is left intact; only what the model sees next turn
        is reset."""
        sid = self.m.active_sid
        if not sid:
            self.say("no session to clear")
            return
        client = self.client
        self.bg(lambda: client.clear(sid),
                lambda _r: self.say("context cleared — model starts fresh next turn (transcript kept)"),
                label="clear failed")

    def new_session(self, profile=None):
        """/new — mint a fresh session and switch to it. Defaults to the active
        session's model; serves that cannot mint one fall back to starting the
        session on the next prompt."""
        if not hasattr(self.client, "create"):
            self.dispatch({"type": "select", "sid": ""})
            self.say("new session — type a prompt to start it")
            return
        if profile is None:
            row = self.m.session
            profile = row.model if row else None
        client = self.client

        def adopt(view):
            sid = view.get("id") if isinstance(view, dict) else None
            if not sid:
                self.say("new session failed: no id returned", "error")
                return
            self.roster_now.set()
            self.dispatch({"type": "select", "sid": sid})
            self.say("new session " + sid[:13])
        self.bg(lambda: client.create(profile), adopt, label="new failed")

    def emergency(self, arg=None):
        """/emergency — break-glass: run a LOCAL GGUF as an agent, no hugpy
        wrapper/central/toolserver in the path. No arg opens a picker over the
        local models; an arg launches a name match, `auto` lets the serve pick."""
        try:
            pf = self.wait("emergency preflight", self.client.emergency_preflight)
        except ServeError as exc:
            self.say("emergency unavailable: %s" % exc, "error")
            return
        models = pf.get("models") or []
        if not models:
            lines = ["No local models available for emergency inference.", ""]
            lines += ["- " + e for e in (pf.get("errors") or [])]
            lines += ["", "Set HUGPY_EMERGENCY_MODEL_DIRS / HUGPY_EMERGENCY_LLAMA_BIN."]
            modals.text_modal(self.keys, "EMERGENCY INFERENCE", lines, self.theme, self.drain)
            return
        chosen_path = None
        if arg and arg.lower() != "auto":
            al = arg.lower()
            match = next((m for m in models if al in m["name"].lower()), None)
            if not match:
                self.say("no local model matching %r" % arg)
                return
            chosen_path = match["path"]
        elif arg is None:
            labels = ["%-40s %5d MB%s" % (m["name"][:40], (m["size_bytes"] or 0) >> 20,
                                          "  +vision" if m["mmproj"] else "")
                      for m in models]
            title = "EMERGENCY · %s · %d threads" % (
                os.path.basename(pf.get("llama_bin") or "?"), pf.get("threads") or 1)
            pick = modals.choose(self.keys, title, labels, self.theme, self.drain)
            if pick is None:
                return
            chosen_path = models[pick]["path"]
        # arg == "auto": chosen_path stays None, the serve auto-picks
        client = self.client

        def launched(view):
            sid = view.get("id") if isinstance(view, dict) else None
            if not sid:
                self.say("emergency launch returned no session", "error")
                return
            self.roster_now.set()
            self.dispatch({"type": "select", "sid": sid})
            self.say("emergency: launching — watch the transcript for 'ready'")
        self.bg(lambda: client.launch_emergency(chosen_path), launched, label="emergency failed")

    def retry(self):
        sid = self.m.active_sid
        if not sid:
            return
        client = self.client

        def work():
            client.queue_action(sid, "retry")
            return client.queue(sid)

        def done(q):
            self.say("retry sent")
            self.dispatch({"type": "queue", "queue": q})
        self.bg(work, done, label="retry")

    def answer(self, approval, decision):
        sid = approval.session_id or self.m.active_sid
        try:
            self.wait("sending %s" % decision, lambda: self.client.answer(sid, approval.request_id, decision))
        except Cancelled:
            return
        except ServeError as exc:
            if exc.code == 400:
                self.say("approval already gone: %s" % exc)
            else:
                self.say("approval: %s" % exc, "error")
                return
        self.dispatch({"type": "approval_answered", "request_id": approval.request_id, "decision": decision})

    # -- slash commands ------------------------------------------------------
    def slash(self, text):
        parts = text.split()
        cmd, args = parts[0].lower(), parts[1:]
        if cmd in ("/quit", "/exit"):
            return "quit"
        if cmd == "/help":
            self.show_help()
        elif cmd == "/model":
            self.pick_model()
        elif cmd == "/shell":
            self.open_shell()
        elif cmd == "/cli":
            self.open_cli()
        elif cmd == "/session":
            self.pick_session(args[0] if args else None)
        elif cmd == "/clear":
            self.clear_context()
        elif cmd == "/new":
            self.new_session(args[0] if args else None)
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
                self.say("no session")
            else:
                client = self.client
                self.bg(lambda: client.roll(sid), lambda res: self.say(
                    "rollover: " + (res.get("note") or ("queued" if res.get("ok") else str(res.get("error"))))),
                    label="rollover failed")
        elif cmd == "/context":
            self.show_context()
        elif cmd == "/status":
            self.status_note()
        elif cmd == "/tools":
            self.show_tools()
        elif cmd == "/emergency":
            self.emergency(args[0] if args else None)
        elif cmd == "/find":
            self.find(" ".join(args))
        elif cmd == "/copy":
            self.copy()
        elif cmd == "/export":
            self.export(" ".join(args) or None)
        elif cmd in ("/log", "/diag"):
            self.show_log()
        else:
            # not a TUI command: forward to the ENGINE (session-first commands
            # like /handoff /resume /rollover live there, not here)
            self._track_send(self.m.active_sid or "new", text)

    def roller_switch(self, mode):
        """/rollover on|off|auto|manual|status — the serve's auto-roller switch."""
        client = self.client
        if mode == "status":
            def status(doc):
                doc = doc or {}
                pol = doc.get("policy") or {}
                pend = doc.get("pending") or {}
                text = "roller: %s · %sk ctx · sweep %ss" % (
                    pol.get("rollover_mode", "?"),
                    int(pol.get("rollover_context_tokens", 0) or 0) // 1000,
                    pol.get("rollover_sweep_s", "?"))
                if pend:
                    text += " · PENDING %s" % str(pend.get("session_id", ""))[:8]
                self.say(text)
            self.bg(client.rollover, status, label="roller")
            return
        if not hasattr(client, "roll_mode"):
            self.say("this serve has no roller switch")
            return

        def switched(res):
            if res.get("ok"):
                text = "roller → %s" % res.get("mode")
                if res.get("warning"):
                    text += " ⚠ %s" % res["warning"]
                self.say(text)
            else:
                self.say("roller: %s" % res.get("error"), "error")
        self.bg(lambda: client.roll_mode(mode), switched, label="roller")

    def status_note(self):
        from .state import Block
        lane = self.m.lane()
        fields = panels.status_fields(self.m, self.now())
        blocks = lane.blocks + [Block("note", "status: " + " · ".join(fields) + " · cursor %s · source %s" %
                                      (lane.cursor, lane.source or "-"))]
        self.m = self.m.__class__(**dict(self.m.__dict__, lanes=dict(self.m.lanes, **{
            self.m.active_sid: lane.__class__(**dict(lane.__dict__, blocks=blocks))})))

    def show_help(self):
        lines = [line.replace("%s", self.diag.path or "the in-memory log (file logging off)")
                 for line in HELP]
        modals.text_modal(self.keys, "HELP · hugpy-agent %s" % self.version, lines, self.theme, self.drain)

    def show_context(self):
        """The whole session as the model sees it on the next call, with tokens."""
        sid = self.m.active_sid
        if not sid:
            self.say("no session")
            return
        lines = []
        try:
            events = self.wait("loading context", lambda: self.client.events(sid, "0")).events
        except Cancelled:
            return
        except Exception as exc:                           # noqa: BLE001 — shown in the modal
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
                lines.append("TOOL: %s" % (getattr(ev, "name", "") or txt[:80]))
            elif txt:
                lines.append("%s: %s" % (k.upper() or "EVENT", txt[:200]))
        u = getattr(self.m, "usage", None)
        if u:
            lines.append("")
            lines.append("tokens: %s in / %s out%s" % (
                "{:,}".format(getattr(u, "in_tokens", 0)),
                "{:,}".format(getattr(u, "out_tokens", 0)),
                "  $%.4f" % u.cost_usd if float(getattr(u, "cost_usd", 0) or 0) else ""))
        elif self.m.lane().ctx_tokens:
            lane = self.m.lane()
            lines += ["", "context of the last call: %s tokens · output so far: %s tokens" % (
                "{:,}".format(lane.ctx_tokens), "{:,}".format(lane.tok_out))]
        flat = []
        for ln in lines:
            flat.extend(ln.splitlines() or [""])
        modals.text_modal(self.keys, "CONTEXT %s" % sid[:13], flat or ["(empty)"], self.theme, self.drain)

    def show_tools(self):
        """Name + first description line, grouped by category prefix — a wall
        of bare mcp names is unscannable (operator 2026-10-02)."""
        lines = []
        try:
            if self.tools_list:
                by_cat = {}
                for t in self.wait("loading tools", self.tools_list) or []:
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
                lines.append(str(self.wait("toolserver status", self.tools_status)))
        except Cancelled:
            return
        except Exception as exc:                           # noqa: BLE001 — shown in the modal
            lines.append("toolserver error: %s" % exc)
        if not lines:
            lines = ["toolserver: off (no toolserver_client / TOOLSERVER_URL)"]
        modals.text_modal(self.keys, "TOOLS · %s" % (self.m.tools or "off"), lines, self.theme, self.drain)

    def show_log(self):
        """/log — what the operator needs to file a fault: build, serve,
        locus, session, terminal, log file, then every notice and error."""
        h, w = self.screen.getmaxyx()
        errors, warnings = self.diag.counts()
        head = [
            "hugpy-agent %s · python %s" % (self.version, sys.version.split()[0]),
            "code    %s" % os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "serve   %s (%s) · locus %s · net %s%s" % (self.client.base, self.client.kind, self.active_locus,
                                                       self.m.net, (" · last poll error: " + self.poll_error)
                                                       if self.poll_error else ""),
            "session %s" % (self.m.active_sid or "-"),
            "term    %s %dx%d · mouse %s" % (os.environ.get("TERM", "?"), w, h,
                                             "off" if os.environ.get("HUGPY_TUI_MOUSE") == "0" else "on"),
            "log     %s%s" % (self.diag.path or "memory only (HUGPY_TUI_LOG=off or not attached)",
                              " (%s)" % self.diag.file_error if self.diag.file_error else ""),
            "errors  %d · warnings %d" % (errors, warnings),
            "",
        ]
        self.dispatch({"type": "alerts", "clear": True})
        modals.text_modal(self.keys, "LOG", head + (self.diag.lines() or ["(nothing logged yet)"]),
                          self.theme, self.drain, at_end=True)

    def find(self, query=""):
        if not self.m.active_sid:
            self.say("no session")
            return
        self.dispatch({"type": "find", "query": query, "current": self.last_lines[0]})

    def _selected_block(self):
        """The block `y`/`/copy` act on: the transcript selection, else the
        last reply with text."""
        lane, m = self.m.lane(), self.m
        if m.focus == "transcript" and m.selected >= 0 and m.selected < len(lane.blocks):
            return lane.blocks[m.selected]
        if m.focus == "transcript" and m.selected <= -2:
            from . import toolcalls as tc
            start = tc.group_start(m.selected)
            members = next((mem for s, mem in tc.runs(lane.blocks) if s == start), [])
            if members:
                from .state import Block
                text = "\n\n".join(output.copy_text(lane.blocks[i]) for i in members)
                return Block("note", text)
        for b in reversed(lane.blocks):
            if b.kind == "assistant" and (b.text or "").strip():
                return b
        return None

    def copy(self):
        block = self._selected_block()
        if block is None:
            self.say("nothing to copy")
            return
        text = output.copy_text(block)
        seq = output.osc52(text)
        if not seq:
            self.say("nothing to copy (empty block)")
            return
        try:
            self.tty_write(seq)
        except OSError as exc:
            self.say("copy failed: %s" % exc, "error")
            return
        cut = len(text) > output.OSC52_MAX
        self.say("copied %s chars (OSC 52)%s" % ("{:,}".format(min(len(text), output.OSC52_MAX)),
                                                 " — truncated" if cut else ""))

    def export(self, path=None):
        lane, sid = self.m.lane(), self.m.active_sid
        if not sid or not lane.blocks:
            self.say("nothing to export")
            return
        row = self.m.session
        meta = {"serve": "%s (%s)" % (self.client.base, self.client.kind), "locus": self.active_locus,
                "session": sid, "model": (row.model if row else "") or "",
                "exported": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "by": "hugpy-agent tui %s" % self.version}
        text = output.to_markdown(lane.blocks, "Transcript · %s" % sid, meta)
        target = path or output.export_path(self.active_locus, sid)
        try:
            written = output.write_private(target, text)
        except OSError as exc:
            self.say("export failed: %s" % exc, "error")
            return
        self.say("exported %d blocks → %s" % (len(lane.blocks), written))

    @staticmethod
    def _tty_write(data):
        """Raw escape to the terminal (OSC 52); curses owns stdout but an OSC
        moves no cursor, so it is safe between refreshes."""
        stream = sys.__stdout__
        stream.write(data)
        stream.flush()

    def pick_model(self):
        row = self.m.session
        try:
            options = self.wait("loading models", self.client.models)
        except Cancelled:
            return
        except ServeError as exc:
            self.say("models: %s" % exc, "error")
            return
        if not options:
            self.say("no models offered")
            return
        labels = ["%s  (%s)" % (o.label or o.model, o.backend) for o in options]
        pick = modals.choose(self.keys, "MODEL · %s" % ((row.label or row.id) if row else (self.m.active_sid or "?")[:13]),
                             labels, self.theme, self.drain)
        if pick is None:
            return
        if row is None and not self.m.active_sid:
            self.say("no session to set a model on — send a prompt first")
            return
        chosen = options[pick]
        try:
            note = self.wait("setting model", lambda: self.client.select_model(row, chosen))
            self.say(note)
            self.roster_now.set()
        except Cancelled:
            return
        except ServeError as exc:
            self.say(str(exc), "error")

    def pick_locus(self, wanted=None):
        """/locus [name] — picker over the loci (toolserver registry, then
        tui-loci.json); switches the whole serve."""
        self.refresh_loci()             # the next open shows what was registered since
        if not self.loci:
            self.say("no loci (none publish a serve in the toolserver registry; ~/.hugpy/tui-loci.json adds more)")
            return
        if wanted:
            entry = next((e for e in self.loci if e["locus"].lower() == wanted.lower()
                          or e["locus"].lower().startswith(wanted.lower())), None)
            if entry is None:
                self.say("no locus %s" % wanted)
                return
            self.switch_locus(entry)
            return
        labels = ["%-10s %s%s" % (e["locus"], e.get("serve") or ("ssh " + e.get("ssh", "")),
                                  " · current" if e["locus"] == self.active_locus else "")
                  for e in self.loci]
        current = next((i for i, e in enumerate(self.loci) if e["locus"] == self.active_locus), 0)
        pick = modals.choose(self.keys, "LOCI", labels, self.theme, self.drain, selected=current)
        if pick is not None:
            self.switch_locus(self.loci[pick])

    def switch_locus(self, entry):
        """Park the current (client, model), connect to the entry's serve
        (opening its ssh tunnel when remote) and adopt or create its state.
        The connect runs under wait() (Esc cancels); the swap bumps the
        generation so the old serve's in-flight replies are dropped."""
        if entry["locus"] == self.active_locus:
            return
        from ..serve_client import connect
        from .discovery import identify, probe
        held = self._locus_held.pop(entry["locus"], None)
        if held is None:
            def reach():
                try:
                    base = self.tunnels.base_for(entry)
                except (RuntimeError, OSError) as exc:
                    raise ServeError(str(exc))
                kind = identify(probe(base, timeout=2.0))
                if kind is None:
                    raise ServeError("%s is not a serve" % base)
                return connect(base, kind), Model(kind=kind, base=base)
            try:
                held = self.wait("connecting to %s" % entry["locus"], reach)
            except Cancelled:
                return
            except ServeError as exc:
                self.say("locus %s: %s" % (entry["locus"], exc), "error")
                return
        self.pause_poll.set()           # the poller must not race the swap
        try:
            self._locus_held[self.active_locus] = (self.client, self.m)
            self.client, self.m = held
            self.gen += 1
            self.active_locus = entry["locus"]
            self.sse_active = set()
            self.roster_now.set()
            self.say("locus %s" % entry["locus"])
        finally:
            self.pause_poll.clear()

    def pick_session(self, wanted=None):
        roster = self.m.roster
        if not roster or not (roster.roles or roster.sessions):
            self.say("no sessions on this serve yet — type a prompt to start one")
            return
        rows = [r for r in roster.roles if r.id] + [s for s in roster.sessions if s.id not in {r.id for r in roster.roles}]
        if wanted:
            wl = wanted.lower()
            for r in rows:
                if wanted == r.id or wl == (r.role or "").lower() or wl == (r.label or "").lower() \
                        or r.id.startswith(wanted):
                    self.dispatch({"type": "select", "sid": r.id})
                    return
            self.say("no session %s" % wanted)
            return
        labels = ["%-8s %s · %s · %s%s" % ((r.label or r.role or "")[:8], panels.short_id(r.id), r.backend,
                                            (r.model or "").split(":")[-1] or "-", "  BUSY" if r.busy else "")
                  for r in rows]
        current = next((i for i, r in enumerate(rows) if r.id == self.m.active_sid), 0)
        pick = modals.choose(self.keys, "SESSIONS", labels, self.theme, self.drain, selected=current)
        if pick is not None:
            self.dispatch({"type": "select", "sid": rows[pick].id})

    def open_shell(self):
        """The standing shell row: hand the terminal to a login shell, then
        restore the TUI exactly as it was when the shell exits."""
        curses.def_prog_mode()
        curses.endwin()
        self._paste_mode(False)
        try:
            print("hugpy-agent tui: shell — type `exit` to return", flush=True)
            subprocess.call([os.environ.get("SHELL") or "/bin/bash", "-l"])
        except OSError as exc:
            print("shell failed: %s" % exc, flush=True)
        finally:
            self._paste_mode(True)
            curses.reset_prog_mode()
            self.screen.clear()
            self.screen.refresh()

    def open_cli(self):
        """The CLI opener (operator 2026-10-06: "there should be a cli opener, at
        least replicate the / options when in the client and holding down shift
        and pressing slash"; "no claude resume. resume the way we do here"):
        hand the terminal to a FRESH session of the active role's own CLI
        (claude / codex via the abstract launchers, the role's model, as the
        serve's user) that resumes from the locus's handoff ledger — so every
        native / option works there; restore the TUI when it exits. Refused
        while a turn runs on the role (two keepers on one ledger)."""
        import getpass
        row = self.m.session
        if row is None:
            self.say("no session — pick one first (Ctrl-G)", "error")
            return
        if row.busy:
            self.say("a turn is running on this session — wait or Esc it, then Shift + /", "error")
            return
        entry = next((e for e in self.loci if e.get("locus") == self.active_locus), {}) or {}
        login = entry.get("ssh") or entry.get("login") or ""
        locus = "" if self.active_locus in (None, "here") else self.active_locus
        mode = "ledger"
        if row.native_id:
            # both ways (operator 2026-10-06): the CLI's own session, or fresh from the ledger
            pick = modals.choose(self.keys, "OPEN %s CLI · %s" % ((row.backend or "?").upper(), row.label or row.role or row.id),
                                 ["fork this session — a throwaway copy, captured to the toolserver",
                                  "fresh session, resumed from the %s ledger (toolserver)" % (locus or "locus")],
                                 self.theme, self.drain)
            if pick is None:
                return
            mode = "native" if pick == 0 else "ledger"
        argv, why = loci_mod.cli_argv(row.backend, locus, row.model, row.cwd, login, getpass.getuser(),
                                      ssh_port=entry.get("ssh_port"), mode=mode, native_id=row.native_id)
        if argv is None:
            self.say(why, "error")
            return
        curses.def_prog_mode()
        curses.endwin()
        self._paste_mode(False)
        try:
            print("hugpy-agent tui: %s CLI for %s (%s) — exit it to return here" % (
                row.backend, row.label or row.role or row.id, " ".join(argv[:-1] + ["…"]) if argv[0] == "ssh" else argv[-1]),
                flush=True)
            subprocess.call(argv)
        except OSError as exc:
            print("CLI failed: %s" % exc, flush=True)
        finally:
            self._paste_mode(True)
            curses.reset_prog_mode()
            self.screen.clear()
            self.screen.refresh()
            self.roster_now.set()        # model / settings changed in the CLI show up at once

    def cycle_role(self, delta):
        roles = [r for r in panels.role_rows(self.m) if r.id]
        if not roles:
            return
        ids = [r.id for r in roles]
        index = ids.index(self.m.active_sid) if self.m.active_sid in ids else -1
        self.dispatch({"type": "select", "sid": ids[(index + delta) % len(ids)]})

    def open_queue(self):
        sid = self.m.active_sid
        client = self.client
        try:
            q = self.wait("loading queue", lambda: client.queue(sid))
        except Cancelled:
            return
        except ServeError as exc:
            self.say("queue: %s" % exc, "error")
            return
        self.dispatch({"type": "queue", "queue": q})
        if q is None:
            self.say("this serve has no queue")
            return
        result = modals.queue_modal(self.keys, q, self.theme, self.drain)
        if not result:
            return
        action, payload = result
        try:
            if action == "edit":
                text = modals.line_edit(self.keys, "EDIT %s" % payload.id[:8], payload.text, self.theme, self.drain)
                if text is not None:
                    self.wait("updating queue", lambda: client.queue_action(sid, "update", id=payload.id, text=text))
            elif action == "remove":
                self.wait("updating queue", lambda: client.queue_action(sid, "remove", id=payload.id))
            elif action == "auto":
                self.wait("updating queue", lambda: client.queue_action(sid, "auto", auto=bool(payload)))
            else:
                self.wait("updating queue", lambda: client.queue_action(sid, action))
            self.dispatch({"type": "queue", "queue": self.wait("loading queue", lambda: client.queue(sid))})
            self.say("queue %s ok" % action)
        except Cancelled:
            return
        except ServeError as exc:
            self.say("queue %s: %s" % (action, exc), "error")

    def open_approval(self):
        approval = self.m.open_approval
        if approval is None:
            return
        rid = approval.request_id
        decision = modals.approval_modal(self.keys, approval, self.theme, self.drain,
                                         alive=lambda: any(a.request_id == rid for a in self.m.approvals))
        if decision is None:
            if any(a.request_id == rid for a in self.m.approvals):
                self.dispatch({"type": "approval_shown", "open": False})
            else:
                self.say("approval answered elsewhere")
            return
        self.answer(approval, decision)

    def confirm_quit(self):
        if not self.m.busy:
            return True
        pick = modals.choose(self.keys, "A turn is still running on the serve", ["Stay", "Quit (the turn keeps running)"],
                             self.theme, self.drain)
        return pick == 1

    # -- keys ------------------------------------------------------------------
    def getkey(self, raw=False):
        if self.typeahead and not raw:
            return self.typeahead.popleft()
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

    def _transcript_key(self, ch):
        """Single-letter commands while the transcript has focus; False when
        the key is not one (it then types into the composer)."""
        if ch == " ":
            self.dispatch({"type": "expand"})
        elif ch == "a":
            self.dispatch({"type": "expand_all"})
        elif ch == "r":
            self.retry()
        elif ch == "y":
            self.copy()
        else:
            return False
        return True

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
            # Shift + / on an EMPTY prompt opens the role's own CLI ("?" mid-text still types)
            if key == "?" and m.focus == "composer" and not self.composer.buffer:
                self.open_cli()
                return None
            if m.focus == "composer":
                self.composer.insert(key)
            elif not self._transcript_key(key):
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
                if nxt in (ord("O"), "O"):                 # SS3: F1-F4 on some terminals
                    tail = self.getkey()
                    mapped = {"P": curses.KEY_F1, "R": curses.KEY_F3}.get(
                        tail if isinstance(tail, str) else (chr(tail) if isinstance(tail, int) and 0 < tail < 256 else ""))
                    if mapped is not None:
                        self.screen.timeout(100)
                        return self.handle_key(mapped)
                    if m.focus == "composer":
                        self.composer.insert("O")
                    return None
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
            self.composer.insert(pick + (" " if pick in ARG_COMMANDS else ""))
            return None
        if menu_open and key in (10, 13, curses.KEY_ENTER):
            pick = self._slash_hits[self.slash_sel]
            if pick in ARG_COMMANDS:
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
        elif key == curses.KEY_F1:
            self.show_help()
        elif key == curses.KEY_F2:
            self.dispatch({"type": "focus"})
        elif key == curses.KEY_F3:
            self.find()
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
        elif m.focus == "transcript" and 32 <= key < 256 and self._transcript_key(chr(key)):
            pass
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
            panels.draw_splash(scr, self.client.base, self.theme, self.version)
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
            marker = "[transcript ↑↓ select · y copy]" if m.focus == "transcript" else "[composer]"
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

    def _fault(self, exc):
        """Main-loop crash guard: log the traceback, tell the operator, keep
        running — unless faults repeat so fast the loop is spinning, then stop
        cleanly with the log path. True = stop."""
        now = time.monotonic()
        self.diag.error("main loop fault", exc)
        self.faults.append(now)
        while self.faults and now - self.faults[0] > FAULT_WINDOW_S:
            self.faults.popleft()
        if len(self.faults) >= FAULT_LIMIT:
            self.exit_message = ("hugpy-agent tui: stopped after %d internal errors in %ds — last: %s: %s%s"
                                 % (len(self.faults), FAULT_WINDOW_S, type(exc).__name__, exc,
                                    " (traceback in %s)" % self.diag.path if self.diag.path else ""))
            return True
        try:
            self.say("internal error: %s: %s — logged, still running (/log)" % (type(exc).__name__, exc), "error")
            self.screen.clear()
        except Exception:                                  # noqa: BLE001
            pass
        return False

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
        self.diag.info("tui %s start · serve %s (%s) · locus %s" % (self.version, self.client.base,
                                                                     self.client.kind, self.active_locus))
        self.start_poller()
        try:
            while True:
                try:
                    self.drain()
                    if self.m.approval_open and self.m.approvals:
                        self.open_approval()
                        continue
                    self.draw()
                    if self.handle_key(self.getkey()) == "quit":
                        return 0
                except KeyboardInterrupt:
                    return 0          # SIGINT still lands if raw() was unavailable
                except Exception as exc:                   # noqa: BLE001 — the crash guard
                    if self._fault(exc):
                        return 1
        finally:
            self.closed.set()
            self.join_workers()
            self.tunnels.close()
            self._paste_mode(False)
            self.diag.info("tui exit")
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
            sys.stdout.write("\x1b[?2004h" if on else "\x1b[?2004l")
            sys.stdout.flush()
        except Exception:
            pass
