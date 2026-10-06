"""Adapter over `hugpy-agent serve` (:9126) — keeps what the retired tui.py did.

Routes: GET /api/state, POST /api/sessions, GET /api/sessions/<sid>?after=N,
POST /api/sessions/<sid>/{messages,profile,answer,stop}. Events are
`{id, kind, data}`; the cursor is the max event id. There is no roster on this
serve, so `roster()` builds one "Local" role from `state()["sessions"]`.

The event vocabulary is whatever service/runtime.py and loop.py `emit`: the
operator must see every tool call, every model/client failure and how each
turn ended, so each runtime kind maps to a transcript Event (h26 §2.3) and
only pure noise (`final`, which `reply` repeats) is dropped.
"""
from __future__ import annotations

import json

from .base import (Client, Event, EventPage, ProviderOption, Receipt, Roster,
                   ServeError, Session)
from .http import Http

BUSY_STATES = ("running", "waiting", "stopping")
PAGE_SIZE = 500            # runtime Store.events LIMIT
FIRST_SIGHT_PAGES = 200    # first load of a long session: deltas are most rows


def _text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _args(data):
    return list(data) if isinstance(data, (list, tuple)) else [data]


def tool_ok(result):
    """Registry tools return errors as data: `{"error": ...}` (tools/__init__)
    or a leading `error:`. Anything else is a successful observation."""
    s = _text(result).lstrip()
    if s[:1] == "{":
        try:
            doc = json.loads(s)
        except ValueError:
            doc = None
        if isinstance(doc, dict) and doc.get("error") and len(doc) <= 2:
            return False
    return not s[:6].lower() == "error:"


def _client_item(item):
    """Native client (codex/claude-code CLI) item -> one tool card."""
    item = item if isinstance(item, dict) else {"item": item}
    name = str(item.get("type") or "client")
    output = item.get("aggregated_output") or item.get("output") or item.get("result") or ""
    detail = {k: v for k, v in item.items() if k not in ("aggregated_output", "output", "result")}
    status = str(item.get("status") or "")
    ok = False if (status == "failed" or item.get("exit_code") not in (None, 0)) else True
    return name, _text(detail), _text(output), ok


def normalize_event(raw, seq=None):
    """`{id, kind, data}` -> Event (h26 §2.3). Unknown kinds -> None."""
    if not isinstance(raw, dict):
        return None
    kind, data = raw.get("kind"), raw.get("data")
    b = dict(seq=int(raw.get("id", seq or 0) or 0), ts=float(raw.get("ts", 0) or 0),
             session_id=str(raw.get("session") or raw.get("session_id") or ""))
    if kind == "user":
        return Event(kind="user", text=_text(data), **b)
    if kind == "reply":
        return Event(kind="assistant", text=_text(data), meta={"final": True}, **b)
    if kind == "delta":
        return Event(kind="assistant", text=_text(data), meta={"delta": True}, **b)
    if kind == "assistant":
        # prose beside a tool call, <tool_call> stripped; replaces the gated stream
        text = _text(data)
        return Event(kind="assistant", text=text, meta={"final": True}, **b) if text.strip() else None
    if kind == "final":
        return None                                   # `reply` carries the same answer
    if kind == "question":
        data = data if isinstance(data, dict) else {"question": _text(data)}
        return Event(kind="question", request_id=str(data.get("id", "")), text=data.get("question", ""),
                     options=list(data.get("options") or []), **b)
    if kind in ("answer", "ask"):
        # an answered question (any client): closes the modal; `ask` also names
        # the tool, so an orphan (no card on screen) still leaves an audit line
        if kind == "answer":
            return Event(kind="resolved", text=_text(data), **b)
        a = _args(data)
        tool, choice = (a + ["", ""])[:2]
        return Event(kind="resolved", text=_text(choice),
                     meta={"line": "⚑ approval · %s · %s" % (tool or "tool", _text(choice) or "no answer")}, **b)
    if kind == "tool":
        a = _args(data)
        name, args, result, replayed = (a + ["", {}, "", False])[:4]
        meta = {"result": _text(result), "result_ok": tool_ok(result)}
        if replayed:
            meta["replayed"] = True
        return Event(kind="tool", name=str(name or "tool"), detail=_text(args), meta=meta, **b)
    if kind == "client":
        name, detail, output, ok = _client_item(data)
        return Event(kind="tool", name=name, detail=detail, meta={"result": output, "result_ok": ok}, **b)
    if kind == "policy":
        a = _args(data)
        tool, decision = (a + ["", ""])[:2]
        if str(decision).lower() in ("deny", "denied", "block"):
            return Event(kind="note", text="policy denied %s" % (tool or "tool"), ok=False, **b)
        return None                                    # allow/ask: the question card says it
    if kind == "done":
        report = data if isinstance(data, dict) else {"outcome": _text(data)}
        outcome = str(report.get("outcome") or "")
        meta = {"report": report}
        if outcome == "interrupted":
            meta["interrupted"] = True
        elif outcome not in ("done", "") and (report.get("error") or outcome):
            where = ", ".join(p for p in (report.get("model") or "",
                                          "%s steps" % report["steps"] if report.get("steps") else "") if p)
            meta["error"] = "%s: %s%s" % (outcome, report.get("error") or "no answer",
                                          " (%s)" % where if where else "")
        return Event(kind="done", ok=outcome == "done", meta=meta, **b)
    if kind in ("chat_error", "client_error", "rag_error", "audit_error"):
        label = {"chat_error": "model call failed", "client_error": "client error",
                 "rag_error": "memory recall failed", "audit_error": "audit failed"}[kind]
        body = " · ".join(_text(x) for x in _args(data) if _text(x))
        return Event(kind="system", text="⚠ %s: %s" % (label, body[:200]), detail=body,
                     meta={"warn": True}, **b)
    if kind in ("error", "aborted"):
        return Event(kind="note", text=_text(data) or kind, ok=False, **b)
    if kind == "loop_guard":
        a = _args(data)
        tool, action, count = (a + ["", "", ""])[:3]
        if action == "abort":
            return Event(kind="note", text="loop guard stopped %s after %s repeats" % (tool, count), ok=False, **b)
        return Event(kind="system", text="↻ loop guard: %s repeated %s× — nudged" % (tool, count), **b)
    if kind in ("nudge", "repair"):
        body = "; ".join(_text(x) for x in _args(data) if _text(x))
        label = "nudge" if kind == "nudge" else "repair"
        return Event(kind="system", text="↻ %s%s" % (label, ": " + body[:160] if body else " (no tool call)"),
                     detail=body, **b)
    if kind == "toolserver":
        a = _args(data)
        return Event(kind="system", text="toolserver %s" % _text(a[0]), detail=" · ".join(_text(x) for x in a[1:]),
                     **b)
    if kind == "model":
        return Event(kind="system", text="→ model %s" % _text(data), meta={"refresh_roster": True}, **b)
    if kind in ("brain", "brain_fallback", "mode", "compaction", "resume"):
        a = [_text(x) for x in _args(data) if _text(x)]
        label = {"brain": "→ brain", "brain_fallback": "→ fallback", "mode": "mode",
                 "compaction": "context compacted", "resume": "↻ resumed run"}[kind]
        return Event(kind="system", text=" ".join([label] + a[:1]), detail="\n".join(a[1:]), **b)
    if kind == "note":
        return Event(kind="note", text=_text(data), **b)
    if kind == "clear":
        return Event(kind="note", text="context cleared — earlier turns hidden from the model", **b)
    if kind == "run_start":
        return Event(kind="status", text="running", **b)
    return None


def _session(row):
    return Session(id=row.get("id", ""), backend="hugpy", model=row.get("profile", ""),
                   busy=row.get("status") in BUSY_STATES, paused=row.get("status") == "interrupted",
                   updated=float(row.get("updated", 0) or 0))


class HugpyServeClient(Client):
    kind = "hugpy"

    def __init__(self, base, token=None, timeout=5):
        super().__init__(base)
        self.http = Http(self.base, token, timeout)
        self._asked = {}          # sid -> id of the latest question (answer/ask rows carry none)

    def probe(self):
        return self.http.get("/api/state")

    state = probe

    def roster(self):
        doc = self.state()
        if not isinstance(doc, dict):
            raise ServeError("serve state is not an object")
        sessions = [_session(r) for r in doc.get("sessions") or [] if isinstance(r, dict)]
        options = [ProviderOption("hugpy", p.get("id", ""), p.get("label") or p.get("id", ""))
                   for p in doc.get("profiles") or [] if isinstance(p, dict) and p.get("available", True)]
        head = sessions[0] if sessions else Session(id="", backend="hugpy",
                                                    model=(doc.get("config") or {}).get("default_profile", ""))
        local = Session(id=head.id, role="local", label="Local", backend="hugpy", model=head.model,
                        busy=head.busy, paused=head.paused, updated=head.updated)
        return Roster(roles=[local], sessions=sessions, provider_options=options,
                      cwd=doc.get("workspace", ""), defaults=doc.get("config") or {})

    def create(self, profile=None):
        return self.http.post("/api/sessions", {"profile": profile} if profile else {})

    def events(self, sid, since, on_page=None, max_pages=FIRST_SIGHT_PAGES):
        """Every event after `since`, paged at the runtime's 500-row LIMIT (a
        long session is mostly `delta` rows; one page per poll left the first
        load minutes behind)."""
        cursor = int(since or 0)
        raw_all, view = [], {}
        for _ in range(max(1, max_pages)):
            view = self.http.get("/api/sessions/%s" % sid, after=cursor)
            if not isinstance(view, dict):
                raise ServeError("session view is not an object")
            raw = [r for r in view.get("events") or [] if isinstance(r, dict)]
            raw_all += raw
            if raw:
                cursor = max([cursor] + [int(r.get("id", 0) or 0) for r in raw])
            if on_page:
                on_page(len(raw_all))
            if len(raw) < PAGE_SIZE:
                break
        events = []
        for r in raw_all:
            ev = normalize_event(r)
            if r.get("kind") == "question" and ev is not None:
                self._asked[sid] = ev.request_id
            if ev is None:
                continue
            if ev.kind == "resolved" and not ev.request_id:
                ev.request_id = self._asked.get(sid, "")
            events.append(ev)
        pending = view.get("pending")
        if isinstance(pending, dict) and pending.get("id"):
            # Re-raise the open approval on every poll; the reducer dedupes by
            # request_id. Its own key: seq == cursor is the last real row's key.
            events.append(Event(seq=cursor, ts=0.0, session_id=sid, kind="question",
                                request_id=str(pending["id"]), text=pending.get("question", ""),
                                options=list(pending.get("options") or []),
                                meta={"pending": True, "key": "pending:%s" % pending["id"]}))
        busy = view.get("status") in BUSY_STATES
        return EventPage(events, busy, None, str(cursor), "hugpy", False)

    def send(self, sid, text, on_event=None):
        doc = self.http.post("/api/sessions/%s/messages" % sid, {"text": text})
        return Receipt(session_id=doc.get("id", sid), cursor="", queued=doc.get("status") in BUSY_STATES)

    def interrupt(self, sid):
        return self.http.post("/api/sessions/%s/stop" % sid, {})

    def clear(self, sid):
        return self.http.post("/api/sessions/%s/clear" % sid, {})

    def emergency_preflight(self):
        return self.http.get("/api/emergency/preflight")

    def launch_emergency(self, model_path=None, gpu_layers="auto"):
        return self.http.post("/api/sessions/emergency",
                              {"model_path": model_path or "", "gpu_layers": gpu_layers})

    def queue(self, sid):
        return None

    def queue_action(self, sid, action, **kw):
        raise ServeError("hugpy-agent serve has no queue (%s)" % action)

    def answer(self, sid, request_id, decision):
        return self.http.post("/api/sessions/%s/answer" % sid, {"id": request_id, "choice": decision})

    def models(self):
        doc = self.http.get("/api/console/models")
        return [ProviderOption(o.get("backend", "hugpy"), o.get("model", ""), o.get("label", ""))
                for o in doc.get("models") or [] if isinstance(o, dict)]

    def set_model(self, role, model):
        """`role` is the session id here — this serve has profiles per session."""
        return self.http.post("/api/sessions/%s/profile" % role, {"profile": model})

    def set_provider(self, role, backend, model):
        return self.set_model(role, model)

    def select_model(self, row, option):
        """Set the selected session's profile; this API has no role roster."""
        if not row or not row.id:
            raise ServeError("no session to set a model on — send a prompt first")
        self.set_model(row.id, option.model)
        return "model: " + (option.model or "default")
