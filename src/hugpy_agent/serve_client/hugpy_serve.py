"""Adapter over `hugpy-agent serve` (:9126) — keeps what the retired tui.py did.

Routes: GET /api/state, POST /api/sessions, GET /api/sessions/<sid>?after=N,
POST /api/sessions/<sid>/{messages,profile,answer,stop}. Events are
`{id, kind, data}`; the cursor is the max event id. There is no roster on this
serve, so `roster()` builds one "Local" role from `state()["sessions"]`.
"""
from __future__ import annotations

from .base import (Client, Event, EventPage, ProviderOption, Receipt, Roster,
                   ServeError, Session)
from .http import Http

BUSY_STATES = ("running", "waiting", "stopping")


def normalize_event(raw, seq=None):
    """`{id, kind, data}` -> Event (h26 §2.3). Unknown kinds -> None."""
    if not isinstance(raw, dict):
        return None
    kind, data = raw.get("kind"), raw.get("data")
    b = dict(seq=int(raw.get("id", seq or 0) or 0), ts=float(raw.get("ts", 0) or 0),
             session_id=str(raw.get("session") or raw.get("session_id") or ""))
    if kind == "user":
        return Event(kind="user", text=str(data or ""), **b)
    if kind == "reply":
        return Event(kind="assistant", text=str(data or ""), meta={"final": True}, **b)
    if kind == "delta":
        return Event(kind="assistant", text=str(data or ""), meta={"delta": True}, **b)
    if kind == "question":
        data = data if isinstance(data, dict) else {"question": str(data)}
        return Event(kind="question", request_id=str(data.get("id", "")), text=data.get("question", ""),
                     options=list(data.get("options") or []), **b)
    if kind in ("error", "aborted"):
        return Event(kind="note", text=str(data or kind), ok=False, **b)
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

    def probe(self):
        return self.http.get("/api/state")

    state = probe

    def roster(self):
        doc = self.state()
        sessions = [_session(r) for r in doc.get("sessions") or [] if isinstance(r, dict)]
        options = [ProviderOption("hugpy", p.get("id", ""), p.get("label") or p.get("id", ""))
                   for p in doc.get("profiles") or [] if p.get("available", True)]
        head = sessions[0] if sessions else Session(id="", backend="hugpy",
                                                    model=(doc.get("config") or {}).get("default_profile", ""))
        local = Session(id=head.id, role="local", label="Local", backend="hugpy", model=head.model,
                        busy=head.busy, paused=head.paused, updated=head.updated)
        return Roster(roles=[local], sessions=sessions, provider_options=options,
                      cwd=doc.get("workspace", ""), defaults=doc.get("config") or {})

    def create(self, profile=None):
        return self.http.post("/api/sessions", {"profile": profile} if profile else {})

    def events(self, sid, since, on_page=None):
        view = self.http.get("/api/sessions/%s" % sid, after=int(since or 0))
        raw = view.get("events") or []
        events = [e for e in (normalize_event(r) for r in raw) if e]
        cursor = max([int(since or 0)] + [int(r.get("id", 0) or 0) for r in raw])
        pending = view.get("pending")
        if isinstance(pending, dict) and pending.get("id"):
            # Re-raise the open approval on every poll; the reducer dedupes by request_id.
            events.append(Event(seq=cursor, ts=0.0, session_id=sid, kind="question",
                                request_id=str(pending["id"]), text=pending.get("question", ""),
                                options=list(pending.get("options") or []), meta={"pending": True}))
        busy = view.get("status") in BUSY_STATES
        return EventPage(events, busy, None, str(cursor), "hugpy", False)

    def send(self, sid, text, on_event=None):
        doc = self.http.post("/api/sessions/%s/messages" % sid, {"text": text})
        return Receipt(session_id=doc.get("id", sid), cursor="", queued=doc.get("status") in BUSY_STATES)

    def interrupt(self, sid):
        return self.http.post("/api/sessions/%s/stop" % sid, {})

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
