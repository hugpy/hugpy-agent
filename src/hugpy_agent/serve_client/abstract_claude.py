"""Adapter over abstract-claude serve (:9124 keeper / :9125 hugpy locus).

Two API generations coexist on that serve (h27 audit B.0): `/api/console/*`
keyed by stable `cs-<hex>` ids (SQLite events, `seq` cursor) and the legacy
`/api/session/*` keyed by native Claude uuids (transcript cursor
"<offset>:<seq>", SSE on chat). Roster rows can be EITHER (Chat is a native
uuid today), so every method branches on `is_cs(sid)`. cs-* rows never use SSE;
native rows send via the SSE stream and poll `/api/session/events`.
"""
from __future__ import annotations

import json

from .base import (Approval, Client, Event, EventPage, ProviderOption, QueueItem,
                   QueueView, Receipt, Roster, ServeError, Session, Usage)
from .http import Http

APPROVAL_DECISIONS = ["accept", "acceptForSession", "decline", "cancel"]
PAGE_SIZE = 500          # LIMIT on /api/console/events (audit B.2)
FIRST_SIGHT_PAGES = 40   # 20 000 events cap on first load (design §2.4)

_METHOD_TITLES = {
    "item/commandExecution/requestApproval": "Run command",
    "item/fileChange/requestApproval": "Change files",
    "item/permissions/requestApproval": "Grant permissions",
}


def is_cs(sid):
    return str(sid or "").startswith("cs-")


def approval_title(method):
    return _METHOD_TITLES.get(method, method or "Approval")


def _json(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=1, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _base(raw, seq=None):
    return dict(seq=int(raw.get("seq", seq or 0) or 0), ts=float(raw.get("ts", 0) or 0),
                session_id=str(raw.get("session_id", "") or ""))


def _call_ids(raw, *keys):
    """tool_use / parent ids when the serve forwards them (Claude stream-json
    carries `id` on tool_use, `tool_use_id` on tool_result and
    `parent_tool_use_id` on a subagent's events)."""
    meta = {}
    for k in keys:
        if raw.get(k):
            meta["tool_id"] = str(raw[k])
            break
    parent = raw.get("parent_tool_use_id") or raw.get("parent")
    if parent:
        meta["parent"] = str(parent)
    return meta


def _iso_ts(value):
    """ISO-8601 transcript timestamp -> epoch seconds (0.0 when absent)."""
    if not value or not isinstance(value, str):
        return 0.0
    from datetime import datetime
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def normalize_event(raw, seq=None):
    """Console event (`GET /api/console/events`) -> Event, per the h26 §2.1
    table. Unknown types return None so a newer serve never crashes the TUI."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("type")
    b = _base(raw, seq)
    if kind == "user":
        return Event(kind="user", text=raw.get("text", ""),
                     meta={"via": raw.get("via", ""), "message_ids": list(raw.get("message_ids") or [])}, **b)
    if kind == "status":
        return Event(kind="status", text=raw.get("state", ""), meta={"message_ids": list(raw.get("message_ids") or [])}, **b)
    if kind == "system":
        names = raw.get("tool_names") or []
        return Event(kind="system", text="%s · %s tools" % (raw.get("model") or "model", raw.get("tools", len(names))),
                     detail="\n".join(names), meta={"model": raw.get("model", "")}, **b)
    if kind == "text":
        return Event(kind="assistant", text=raw.get("text", ""), **b)
    if kind == "thinking":
        return Event(kind="thinking", text=raw.get("text", ""), **b)
    if kind == "tool":
        if "item" in raw or "status" in raw:              # gpt shape
            return Event(kind="tool", name=raw.get("name", ""), text=raw.get("summary", ""),
                         detail=_json(raw.get("item")), meta={"status": raw.get("status", "")}, **b)
        return Event(kind="tool", name=raw.get("name", ""), text=raw.get("summary", ""),
                     detail=_json(raw.get("input")), meta=_call_ids(raw, "id", "tool_id", "tool_use_id"), **b)
    if kind == "tool_use":
        return Event(kind="tool", name=raw.get("name", ""), text=raw.get("text") or raw.get("summary", ""),
                     detail=_json(raw.get("input")), meta=_call_ids(raw, "id", "tool_id", "tool_use_id"), **b)
    if kind == "tool_result":
        if "is_error" not in raw:                          # gpt output delta
            return Event(kind="tool_result", text=raw.get("text", ""), meta={"delta": True}, **b)
        return Event(kind="tool_result", name=raw.get("name", ""), text=raw.get("text", ""),
                     detail=_json(raw.get("full")) or raw.get("text", ""), ok=not raw.get("is_error"),
                     meta=_call_ids(raw, "tool_use_id", "tool_id"), **b)
    if kind == "approval":
        return Event(kind="approval", request_id=str(raw.get("request_id", "")), name=raw.get("method", ""),
                     text=approval_title(raw.get("method")), options=list(APPROVAL_DECISIONS),
                     detail=_json(raw.get("params")), meta={"params": raw.get("params")}, **b)
    if kind == "question":
        return Event(kind="question", request_id=str(raw.get("request_id", "")), text=raw.get("question", ""),
                     options=list(raw.get("options") or []), **b)
    if kind == "approval_resolved":
        return Event(kind="resolved", request_id=str(raw.get("request_id", "")), **b)
    if kind == "usage":
        return Event(kind="usage", meta={"usage": raw.get("usage") or {}}, **b)
    if kind == "note":
        meta = {k: raw[k] for k in ("quota_fallback", "from_model", "quota_answered") if k in raw}
        return Event(kind="note", text=raw.get("text", ""), meta=meta, **b)
    if kind == "session":
        return Event(kind="system", meta={"native_id": raw.get("native_id", ""), "model": raw.get("model", ""),
                                          "silent": True}, **b)
    if kind == "backend":
        return Event(kind="system", text="→ %s" % raw.get("backend", ""), detail=raw.get("text", ""),
                     meta={"backend": raw.get("backend", ""), "refresh_roster": True}, **b)
    if kind == "rollover":
        return Event(kind="system", text="rolled over · %s carried" % raw.get("carried", 0),
                     meta={"native_id": raw.get("native_id", ""), "old_native_id": raw.get("old_native_id", ""),
                           "refresh_roster": True}, **b)
    if kind == "done":
        rc = raw.get("rc")
        meta = {k: raw.get(k) for k in ("held", "interrupted", "error", "message_ids", "exchange", "cost",
                                        "model", "quota_fallback", "rollover") if raw.get(k) is not None}
        return Event(kind="done", text=raw.get("result") or "", ok=(rc == 0 if rc is not None else None), meta=meta, **b)
    return None


def normalize_sse_frame(frame, seq=0):
    """Legacy chat SSE frame (native uuid rows only) -> Event (§2.2)."""
    if not isinstance(frame, dict):
        return None
    kind = frame.get("type")
    if kind == "error":
        return Event(kind="note", text=frame.get("text") or frame.get("error", ""), ok=False, **_base(frame, seq))
    if kind == "done" and frame.get("queued"):
        return Event(kind="done", text=frame.get("result", ""), ok=True,
                     meta={"queued": frame.get("queued")}, **_base(frame, seq))
    return normalize_event(frame, seq)


def normalize_native_event(raw, seq=0):
    """`/api/session/events` transcript/exchanges row -> Event. Dedupe key is
    msg_id or cursor (meta.key); per-call usage/cost ride in meta."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind") or raw.get("role")
    b = dict(seq=int(raw.get("seq", seq) or seq), ts=_iso_ts(raw.get("ts")),
             session_id=str(raw.get("session_id", "") or ""))
    meta = {"key": raw.get("msg_id") or raw.get("cursor") or str(seq), "ts": raw.get("ts", "")}
    for k in ("usage", "cost", "model"):
        if raw.get(k):
            meta[k] = raw[k]
    text = raw.get("body") or raw.get("excerpt") or ""
    if kind == "user":
        return Event(kind="user", text=text, meta=meta, **b)
    if kind == "assistant":
        return Event(kind="assistant", text=text, meta=dict(meta, final=True), **b)
    if kind == "thinking":
        return Event(kind="thinking", text=text, meta=meta, **b)
    if kind == "tool_use":
        return Event(kind="tool", name=raw.get("tool", ""), text=raw.get("excerpt", "")[:120],
                     detail=text, meta=dict(meta, **_call_ids(raw, "tool_id")), **b)
    if kind == "tool_result":
        # `tool` on a transcript tool_result row is the tool_use_id, not a name.
        return Event(kind="tool_result", text=text, detail=text,
                     ok=not raw.get("is_error"), meta=dict(meta, **_call_ids(raw, "tool")), **b)
    if kind == "harness":
        return Event(kind="note", text=text, meta=dict(meta, harness=raw.get("harness_kind", "")), **b)
    return None


def iter_sse(response):
    """Yield decoded `data:` frames from an SSE response. Same shape as
    gateway._read_response's loop (copied — gateway carries OpenAI semantics)."""
    line = response.readline()
    while line:
        s = line.decode(errors="replace").strip()
        if s.startswith("data:"):
            chunk = s[5:].strip()
            if chunk == "[DONE]":
                return
            try:
                yield json.loads(chunk)
            except ValueError:
                pass
        line = response.readline()


def _queue(doc):
    if not isinstance(doc, dict):
        return None
    return QueueView(auto=bool(doc.get("auto", True)), busy=bool(doc.get("busy")), paused=bool(doc.get("paused")),
                     items=[QueueItem(str(i.get("id", "")), i.get("text", ""), float(i.get("ts", 0) or 0))
                            for i in doc.get("items") or [] if isinstance(i, dict)])


class AbstractClaudeClient(Client):
    kind = "abstract-claude"

    def __init__(self, base, token=None, timeout=5):
        super().__init__(base)
        self.http = Http(self.base, token, timeout)

    # -- reads -------------------------------------------------------------
    def probe(self):
        return self.http.get("/api/state")

    state = probe

    def roster(self):
        doc = self.http.get("/api/session/roster")
        rows = self.http.get("/api/console/sessions")
        live = {r.get("id"): r for r in rows.get("sessions") or [] if isinstance(r, dict)}
        roles = []
        for r in doc.get("roles") or []:
            sid = r.get("live_session_id") or r.get("session_id") or ""
            row = live.get(sid, {})
            roles.append(Session(id=sid, role=r.get("role", ""), label=r.get("label") or r.get("role", "").title(),
                                 backend=r.get("backend", ""), model=r.get("model", ""),
                                 pending_model=r.get("pending_model") or None, busy=bool(row.get("busy")),
                                 paused=bool(row.get("paused")), native_id=row.get("native_id") or "",
                                 updated=float(row.get("updated", 0) or 0)))
        sessions = [Session(id=r.get("id", ""), label=r.get("label") or "", backend=r.get("backend", ""),
                            model=r.get("model", ""), busy=bool(r.get("busy")), paused=bool(r.get("paused")),
                            native_id=r.get("native_id") or "", updated=float(r.get("updated", 0) or 0))
                    for r in live.values()]
        options = [ProviderOption(o.get("backend", ""), o.get("model", ""), o.get("label", ""))
                   for o in doc.get("provider_options") or [] if isinstance(o, dict)]
        return Roster(roles=roles, sessions=sessions, provider_options=options,
                      cwd=rows.get("cwd", ""), defaults=doc.get("defaults") or {})

    def events(self, sid, since, on_page=None, max_pages=FIRST_SIGHT_PAGES):
        if not is_cs(sid):
            doc = self.http.get("/api/session/events", id=sid, since=str(since or "0"))
            raw = doc.get("events") or []
            events = [e for e in (normalize_native_event(r, i + 1) for i, r in enumerate(raw)) if e]
            return EventPage(events, bool(doc.get("busy")), None, str(doc.get("cursor", since or "0")),
                             doc.get("source", "transcript"), False)
        cursor, events, busy, queue, truncated = int(since or 0), [], False, None, True
        for _ in range(max_pages):
            doc = self.http.get("/api/console/events", id=sid, since=cursor)
            raw = doc.get("events") or []
            events += [e for e in (normalize_event(r) for r in raw) if e]
            busy, queue = bool(doc.get("busy")), _queue(doc.get("queue"))
            if raw:
                cursor = max(cursor, max(int(r.get("seq", 0) or 0) for r in raw))
            if on_page:
                on_page(len(events))
            if len(raw) < PAGE_SIZE:
                truncated = False
                break
        if truncated:
            events = events[-PAGE_SIZE * 4:]        # keep the last window (§2.4)
        return EventPage(events, busy, queue, str(cursor), "console", truncated)

    def queue(self, sid):
        path = "/api/console/queue" if is_cs(sid) else "/api/session/queue"
        return _queue(self.http.get(path, id=sid))

    def models(self):
        doc = self.http.get("/api/console/models")
        return [ProviderOption(o.get("backend", ""), o.get("model", ""), o.get("label", ""))
                for o in doc.get("models") or [] if isinstance(o, dict)]

    def usage(self, sid):
        if is_cs(sid):
            return None                              # audit B.11: no usage rows for cs-* (decision 2)
        doc = self.http.get("/api/usage/session", id=sid)
        totals = doc.get("totals") or {}
        rows = list(totals.values()) if isinstance(totals, dict) else list(totals or [])
        usage = Usage(source="usage_db")
        for row in rows:
            if not isinstance(row, dict):
                continue
            usage.in_tokens += int(row.get("in") or row.get("input") or row.get("in_tokens") or 0)
            usage.out_tokens += int(row.get("out") or row.get("output") or row.get("out_tokens") or 0)
            usage.cost_usd += float(row.get("usd") or row.get("cost_usd") or row.get("cost") or 0)
        return usage

    def rollover(self):
        return self.http.get("/api/session/rollover")

    def roll(self, sid, by="tui"):
        """Trigger the serve's native rollover for a session (handoff ->
        fresh successor -> resume, session chain written by the serve)."""
        return self.http.post("/api/session/rollover",
                              {"action": "roll", "session_id": sid, "by": by})

    # -- writes ------------------------------------------------------------
    def send(self, sid, text, on_event=None):
        body = {"session_id": sid, "prompt": text}
        if is_cs(sid):
            doc = self.http.post("/api/console/chat", body)
            return Receipt(session_id=doc.get("session_id", sid), message_ids=list(doc.get("message_ids") or []),
                           cursor=str(doc.get("cursor", "")), queued=bool(doc.get("queued")))
        # Native uuid: the reply IS the SSE stream for this turn (audit B.3a).
        receipt = Receipt(session_id=sid)
        response = self.http.open("/api/session/chat", "POST", body, timeout=None)
        try:
            for n, frame in enumerate(iter_sse(response), 1):
                event = normalize_sse_frame(frame, n)
                if event and on_event:
                    on_event(event)
                if frame.get("type") == "done":
                    receipt.queued = bool(frame.get("queued"))
                    receipt.message_ids = list(frame.get("message_ids") or [])
        finally:
            response.close()
        return receipt

    def interrupt(self, sid):
        path = "/api/console/interrupt" if is_cs(sid) else "/api/session/interrupt"
        return self.http.post(path, {"session_id": sid})

    def queue_action(self, sid, action, **kw):
        path = "/api/console/queue" if is_cs(sid) else "/api/session/queue"
        return self.http.post(path, dict(kw, session_id=sid, action=action))

    def answer(self, sid, request_id, decision):
        return self.http.post("/api/console/approval",
                              {"session_id": sid, "request_id": request_id, "decision": decision})

    def set_provider(self, role, backend, model):
        return self.http.post("/api/session/roster", {"action": "set_provider", "role": role, "backend": backend,
                                                      "model": model, "by": "hugpy-agent tui"})

    def set_model(self, role, model):
        return self.http.post("/api/session/roster", {"action": "set_model", "role": role, "model": model,
                                                      "by": "hugpy-agent tui"})


def staged_model(roster_doc, role):
    """pending_model for `role` in a POST /api/session/roster reply, or None."""
    for r in (roster_doc or {}).get("roles") or []:
        if r.get("role") == role:
            return r.get("pending_model") or None
    return None
