"""PURE reducer for the terminal harness (h26 §1.2). No I/O, no curses.

`reduce(model, action) -> Model` never mutates its input: the App keeps the
current Model, threads post actions through a queue, and views render whatever
Model they are handed. Every serve event is already an `Event` (serve_client
normalised it); the mapping table in §2.1 is implemented in `_apply_event`.
Per-session state (blocks, cursor, dedupe set, scroll) lives in a `Lane` so
switching sessions never discards another lane's cursor (§2.4).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..serve_client.base import Approval, Usage
from . import toolcalls as tc

STALE_RECEIPT_S = 30
APPROVAL_TTL_S = 300          # hugpy-agent times an unanswered approval out after 300 s
BACKOFF = (1, 2, 4, 8, 10)
CARD_KINDS = ("tool", "thinking", "system")      # collapsible one-liners


@dataclass
class Block:
    kind: str                     # user assistant thinking tool system note approval question
    text: str = ""
    name: str = ""
    detail: str = ""              # tool input / system tool list / approval params
    output: str = ""              # tool result text
    ts: float = 0.0
    seq: int = 0
    streaming: bool = False
    ok: bool | None = None
    request_id: str = ""
    decision: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class Lane:
    blocks: list = field(default_factory=list)
    cursor: str = "0"
    source: str = ""
    seen: set = field(default_factory=set)
    loaded: bool = False
    turn_text: bool = False       # assistant text streamed since the last user event
    scroll: int = -1              # -1 follows the tail; else first visible line
    expanded: set = field(default_factory=set)
    groups_open: set = field(default_factory=set)   # start index of opened ⚙ call chips
    rebaseline: bool = False      # App must reload from cursor "0"


@dataclass
class Model:
    kind: str = ""
    base: str = ""
    roster: object = None
    active_sid: str | None = None
    lanes: dict = field(default_factory=dict)
    busy: bool = False
    busy_since: float = 0.0
    queue: object = None
    approvals: list = field(default_factory=list)
    approval_open: bool = False
    held: tuple = (False, "")
    net: str = "connecting"       # connecting|live|degraded|down
    net_failures: int = 0
    net_retry_at: float = 0.0
    pending_receipts: list = field(default_factory=list)
    usage: Usage | None = None
    focus: str = "composer"       # composer|transcript
    selected: int = -1
    notice: str = ""
    size: tuple = (24, 80)
    roster_stale: bool = False
    tools: str = "off"            # toolserver status-bar field (operator addition)
    quit_confirm: bool = False

    # -- conveniences (read-only) ----------------------------------------
    def lane(self, sid=None):
        return self.lanes.get(sid or self.active_sid) or Lane()

    @property
    def blocks(self):
        return self.lane().blocks

    @property
    def session(self):
        return self.roster.find(self.active_sid) if self.roster and self.active_sid else None

    @property
    def open_approval(self):
        return self.approvals[0] if self.approvals else None


def reduce(m, action):
    kind = action["type"]
    fn = _HANDLERS.get(kind)
    if fn is None:
        return m
    return fn(m, action)


# -- helpers --------------------------------------------------------------

def _with_lane(m, sid, lane):
    lanes = dict(m.lanes)
    lanes[sid] = lane
    return replace(m, lanes=lanes)


def _copy_lane(m, sid):
    lane = m.lanes.get(sid) or Lane()
    return replace(lane, blocks=list(lane.blocks), seen=set(lane.seen), expanded=set(lane.expanded),
                   groups_open=set(lane.groups_open))


def _close_streaming(blocks):
    if blocks and blocks[-1].streaming:
        blocks[-1] = replace(blocks[-1], streaming=False)


def _last_open_card(blocks, name=None):
    for i in range(len(blocks) - 1, -1, -1):
        b = blocks[i]
        if b.kind == "tool" and b.ok is None and (name is None or b.name == name):
            return i
    return None


def _clear_receipts(m, message_ids):
    ids = set(message_ids or [])
    if not ids:
        return m
    keep = [r for r in m.pending_receipts if not ids & set(r["receipt"].message_ids)]
    return replace(m, pending_receipts=keep) if len(keep) != len(m.pending_receipts) else m


def _pick_active(roster, prefer):
    rows = list(roster.roles) + list(roster.sessions)
    if prefer:
        pl = prefer.lower()
        for row in rows:
            if row.id and (prefer == row.id or pl == (row.role or "").lower()
                           or pl == (row.label or "").lower()):
                return row.id
        for row in rows:
            if row.id.startswith(prefer):
                return row.id
    for row in rows:
        if row.id:
            return row.id
    return None


# -- event application (§2.1 / §2.2 / §2.3) --------------------------------

def _apply_event(m, lane, ev, now, stale=False):
    """Mutates the COPIED lane in place; returns the (possibly replaced) model.
    `stale` marks an approval/question from a finished or expired turn: it
    still renders as a block but must not open the modal."""
    blocks = lane.blocks
    key = ev.meta.get("key") or ("sse:%d" % ev.seq if ev.meta.get("sse") else ev.seq)
    if key in lane.seen:
        return m
    lane.seen.add(key)
    k = ev.kind
    if stale and k in ("approval", "question"):
        blocks.append(Block(k, ev.text, name=ev.name, detail=ev.detail, request_id=ev.request_id,
                            ts=ev.ts, seq=ev.seq, decision="expired"))
        return m
    if k == "user":
        _close_streaming(blocks)
        blocks.append(Block("user", ev.text, ts=ev.ts, seq=ev.seq, meta=dict(ev.meta)))
        lane.turn_text = False
        return _clear_receipts(m, ev.meta.get("message_ids"))
    if k == "status":
        if stale:
            return m                                    # a finished turn's "compiling" is not news
        return replace(m, notice=(ev.text + "…") if ev.text else m.notice)
    if k == "system":
        if ev.meta.get("silent"):
            return _touch_roster(m, ev.meta)
        blocks.append(Block("system", ev.text, detail=ev.detail, ts=ev.ts, seq=ev.seq, meta=dict(ev.meta)))
        return replace(m, roster_stale=True) if ev.meta.get("refresh_roster") else m
    if k == "assistant":
        final = ev.meta.get("final")
        if blocks and blocks[-1].kind == "assistant" and blocks[-1].streaming:
            text = ev.text if final else blocks[-1].text + ev.text
            blocks[-1] = replace(blocks[-1], text=text, streaming=not final)
        else:
            blocks.append(Block("assistant", ev.text, ts=ev.ts, seq=ev.seq, streaming=not final,
                                meta=dict(ev.meta)))
        lane.turn_text = True
        return m
    if k == "thinking":
        _close_streaming(blocks)
        blocks.append(Block("thinking", ev.text, ts=ev.ts, seq=ev.seq))
        return m
    if k == "tool":
        _close_streaming(blocks)
        if ev.meta.get("status") == "completed":
            i = _last_open_card(blocks, ev.name)
            if i is not None:
                blocks[i] = replace(blocks[i], ok=True, detail=ev.detail or blocks[i].detail)
                return m
        blocks.append(Block("tool", ev.text, name=ev.name, detail=ev.detail, ts=ev.ts, seq=ev.seq,
                            meta=dict(ev.meta)))
        return m
    if k == "tool_result":
        if ev.meta.get("delta"):
            i = _last_open_card(blocks)
            if i is not None:
                blocks[i] = replace(blocks[i], output=blocks[i].output + ev.text)
            return m
        # Attach to ITS call (tool_use id, else oldest open call) — never a
        # separate message; the card line gains status + duration in place.
        i = tc.match_result(blocks, ev.meta.get("tool_id", ""), ev.meta.get("parent", ""))
        if i is None:
            blocks.append(Block("tool", ev.name or "result", name=ev.name, output=ev.detail or ev.text,
                                ok=ev.ok, ts=ev.ts, seq=ev.seq, meta=dict(ev.meta)))
        else:
            card = blocks[i]
            meta = dict(card.meta)
            if card.ts and ev.ts and ev.ts >= card.ts:
                meta["dur"] = ev.ts - card.ts
            blocks[i] = replace(card, output=ev.detail or ev.text, ok=ev.ok, meta=meta)
        return m
    if k in ("approval", "question"):
        if any(a.request_id == ev.request_id for a in m.approvals):
            return m
        if not any(b.request_id == ev.request_id for b in blocks if b.kind in ("approval", "question")):
            _close_streaming(blocks)
            blocks.append(Block(k, ev.text, name=ev.name, detail=ev.detail, request_id=ev.request_id,
                                ts=ev.ts, seq=ev.seq))
        approval = Approval(ev.request_id, k, ev.text, list(ev.options), ev.meta.get("params") or ev.detail,
                            ev.ts or now, ev.session_id)
        return replace(m, approvals=m.approvals + [approval], approval_open=True)
    if k == "resolved":
        for i, b in enumerate(blocks):
            if b.request_id == ev.request_id and b.kind in ("approval", "question") and not b.decision:
                blocks[i] = replace(b, decision="resolved")
        approvals = [a for a in m.approvals if a.request_id != ev.request_id]
        return replace(m, approvals=approvals, approval_open=m.approval_open and bool(approvals))
    if k == "usage":
        u = ev.meta.get("usage") or {}
        total = u.get("total") or {}
        return replace(m, usage=Usage(int(total.get("input_tokens") or total.get("in") or 0),
                                      int(total.get("output_tokens") or total.get("out") or 0),
                                      int(u.get("modelContextWindow") or 0), 0.0, "gpt"))
    if k == "note":
        _close_streaming(blocks)
        blocks.append(Block("note", ev.text, ok=ev.ok, ts=ev.ts, seq=ev.seq, meta=dict(ev.meta)))
        if ev.meta.get("quota_fallback"):
            return replace(m, notice="quota fallback → %s" % ev.meta["quota_fallback"])
        return m
    if k == "done":
        _close_streaming(blocks)
        for i, b in enumerate(blocks):                  # calls the turn never answered
            if b.kind == "tool" and b.ok is None and not b.meta.get("orphan"):
                blocks[i] = replace(b, meta=dict(b.meta, orphan=True))
        meta = ev.meta
        if ev.text and not lane.turn_text:
            blocks.append(Block("assistant", ev.text, ts=ev.ts, seq=ev.seq, ok=ev.ok))
        lane.turn_text = False
        m = _clear_receipts(m, meta.get("message_ids"))
        if meta.get("held"):
            m = replace(m, held=(True, meta.get("error") or "held"))
        elif meta.get("error"):
            blocks.append(Block("note", str(meta["error"]), ok=False, ts=ev.ts, seq=ev.seq))
        if meta.get("interrupted"):
            m = replace(m, notice="interrupted")
        if meta.get("queued"):
            m = replace(m, notice="Queued")
        if meta.get("exchange"):
            ex = meta["exchange"]
            cost = (meta.get("cost") or {}).get("usd", 0) if isinstance(meta.get("cost"), dict) else 0
            m = replace(m, usage=Usage(int(ex.get("in") or ex.get("input_tokens") or 0),
                                       int(ex.get("out") or ex.get("output_tokens") or 0),
                                       0, float(cost or 0), "sse"))
        return m
    return m


def _touch_roster(m, meta):
    """`session` events repoint native_id / actual model on the roster row."""
    if not m.roster or not m.active_sid:
        return m
    roster = replace(m.roster, roles=list(m.roster.roles), sessions=list(m.roster.sessions))
    for rows in (roster.roles, roster.sessions):
        for i, row in enumerate(rows):
            if row.id == m.active_sid:
                rows[i] = replace(row, native_id=meta.get("native_id") or row.native_id,
                                  model=meta.get("model") or row.model)
    return replace(m, roster=roster)


# -- handlers ---------------------------------------------------------------

def _events(m, a):
    sid, page, now = a["sid"], a["page"], a.get("now", 0.0)
    lane = _copy_lane(m, sid)
    if lane.loaded and page.source and lane.source and page.source != lane.source:
        # Cursors are not comparable across sources (audit B.3): re-baseline.
        lane = Lane(cursor="0", source=page.source, rebaseline=True)
        return _with_lane(m, sid, lane)
    lane.source = page.source or lane.source
    lane.rebaseline = False
    if page.truncated and not lane.loaded:
        lane.blocks.append(Block("note", "older history truncated"))
    # Approvals only count when the turn is still open: anything before the
    # last `done` of this page, or older than the 300 s hugpy timeout, is
    # history (the hugpy adapter never emits approval_resolved for questions).
    last_done = max([i for i, e in enumerate(page.events) if e.kind == "done"] or [-1])
    wall = a.get("wall")
    for i, ev in enumerate(page.events):
        stale = not ev.meta.get("pending") and (
            i < last_done or bool(wall and ev.ts and wall - ev.ts > APPROVAL_TTL_S))
        m = _apply_event(m, lane, ev, now, stale)
    lane.cursor = str(page.cursor)
    lane.loaded = True
    m = _with_lane(m, sid, lane)
    if sid == m.active_sid:
        busy_since = m.busy_since if m.busy else now
        m = replace(m, busy=page.busy, busy_since=busy_since if page.busy else 0.0)
        if page.queue is not None:
            m = _queue(m, {"queue": page.queue})
    return m


def _sse_event(m, a):
    lane = _copy_lane(m, a["sid"])
    ev = a["event"]
    ev.meta["sse"] = True
    m = _apply_event(m, lane, ev, a.get("now", 0.0))
    return _with_lane(m, a["sid"], lane)


def _sse_done(m, a):
    """Drop the SSE preview blocks; the transcript poll re-paints the turn."""
    lane = _copy_lane(m, a["sid"])
    lane.blocks = [b for b in lane.blocks if not b.meta.get("sse")]
    lane.seen = {k for k in lane.seen if not str(k).startswith("sse:")}
    return _with_lane(m, a["sid"], lane)


def _roster(m, a):
    roster = a["roster"]
    active = m.active_sid or _pick_active(roster, a.get("prefer"))
    return replace(m, roster=roster, active_sid=active, roster_stale=False,
                   net="live" if m.net == "connecting" else m.net)


def _sent(m, a):
    receipt = a["receipt"]
    entry = {"receipt": receipt, "ts": a.get("now", 0.0), "unacked": False}
    notice = "Queued" if receipt.queued else m.notice
    # a first prompt sent with sid "new": adopt the serve-minted session id
    active = m.active_sid or (receipt.session_id
                              if receipt.session_id and receipt.session_id != "new" else "")
    # Sending un-holds (audit B.11); the next queue view confirms it.
    return replace(m, pending_receipts=m.pending_receipts + [entry], notice=notice,
                   active_sid=active,
                   held=(False, "") if m.held[0] and not receipt.queued else m.held)


def _queue(m, a):
    q = a["queue"]
    if q is None:
        return replace(m, queue=None)
    held = (True, m.held[1] or "paused") if q.paused else (False, "")
    return replace(m, queue=q, held=held)


def _net(m, a):
    now = a.get("now", 0.0)
    if a.get("ok"):
        return replace(m, net="live", net_failures=0, net_retry_at=0.0)
    failures = m.net_failures + 1
    return replace(m, net="degraded" if failures < 3 else "down", net_failures=failures,
                   net_retry_at=now + BACKOFF[min(failures, len(BACKOFF)) - 1])


def _tick(m, a):
    now = a["now"]
    if m.busy or not m.pending_receipts:
        return m
    changed = False
    receipts = []
    for r in m.pending_receipts:
        if not r["unacked"] and now - r["ts"] > STALE_RECEIPT_S:
            r, changed = dict(r, unacked=True), True
        receipts.append(r)
    return replace(m, pending_receipts=receipts) if changed else m


def _receipt_lost(m, a):
    keep = [r for r in m.pending_receipts if r["receipt"] is not a["receipt"]]
    return replace(m, pending_receipts=keep, notice=a.get("notice", "prompt lost — resend? (Up recalls it)"))


def _resize(m, a):
    return replace(m, size=(max(1, a["h"]), max(1, a["w"])))


def _select(m, a):
    sid = a["sid"]
    if sid == m.active_sid:
        return m
    lane = m.lanes.get(sid)
    return replace(m, active_sid=sid, selected=-1, busy=False, queue=None, held=(False, ""), usage=None,
                   lanes=m.lanes if lane else dict(m.lanes, **{sid: Lane()}))


def _scroll(m, a):
    lane = _copy_lane(m, m.active_sid)
    where = a["to"]
    if where == "end":
        lane.scroll = -1
    elif where == "top":
        lane.scroll = 0
    else:
        base = a.get("current", 0) if lane.scroll < 0 else lane.scroll
        lane.scroll = max(0, base + int(where))
        if a.get("max_scroll") is not None and lane.scroll >= a["max_scroll"]:
            lane.scroll = -1
    return _with_lane(m, m.active_sid, lane)


def visible_targets(m):
    """Selectable rows in draw order: block indices and ⚙ chip targets (< -1)."""
    lane = m.lane()
    return tc.targets(tc.layout(lane.blocks, lane.groups_open, lane.expanded, m.busy))


def _move(m, a):
    order = visible_targets(m)
    if not order:
        return m
    pos = order.index(m.selected) if m.selected in order else len(order) - 1
    return replace(m, selected=order[max(0, min(len(order) - 1, pos + a["delta"]))])


def _hidden_in_group(lane, index):
    """Start of the collapsed chip that hides block `index`, else None."""
    for start, members in tc.runs(lane.blocks):
        if index in members and len(members) > 1 and start not in lane.groups_open:
            return start
    return None


def _expand(m, a):
    lane = _copy_lane(m, m.active_sid)
    index = a.get("index")
    if index is None:
        index = m.selected if (m.focus == "transcript" and m.selected != -1) else _latest_card(lane.blocks)
    if index is None:
        return m
    if a.get("select"):
        m = replace(m, focus="transcript", selected=index)
    start = tc.group_start(index)
    if start is not None:                               # a ⚙ N calls chip
        (lane.groups_open.discard if start in lane.groups_open else lane.groups_open.add)(start)
        return _with_lane(m, m.active_sid, lane)
    if not (0 <= index < len(lane.blocks)) or lane.blocks[index].kind not in CARD_KINDS:
        return m
    if index in lane.expanded:
        lane.expanded.discard(index)
    else:
        lane.expanded.add(index)
        hidden = _hidden_in_group(lane, index)
        if hidden is not None:
            lane.groups_open.add(hidden)                # expanding a call behind a chip opens the chip
    return _with_lane(m, m.active_sid, lane)


def _expand_all(m, a):
    """Toggle every ⚙ chip and tool card of the lane at once."""
    lane = _copy_lane(m, m.active_sid)
    starts = {s for s, members in tc.runs(lane.blocks) if len(members) > 1}
    cards = {i for i, b in enumerate(lane.blocks) if b.kind == "tool"}
    if starts <= lane.groups_open and cards <= lane.expanded:
        lane.groups_open -= starts
        lane.expanded -= cards
    else:
        lane.groups_open |= starts
        lane.expanded |= cards
    return _with_lane(m, m.active_sid, lane)


def _latest_card(blocks):
    for i in range(len(blocks) - 1, -1, -1):
        if blocks[i].kind == "tool":
            return i
    return None


def _approval_shown(m, a):
    return replace(m, approval_open=bool(a.get("open")) and bool(m.approvals))


def _approval_answered(m, a):
    rid = a["request_id"]
    lane = _copy_lane(m, m.active_sid)
    for i, b in enumerate(lane.blocks):
        if b.request_id == rid and b.kind in ("approval", "question"):
            lane.blocks[i] = replace(b, decision=a.get("decision", ""))
    approvals = [x for x in m.approvals if x.request_id != rid]
    return replace(_with_lane(m, m.active_sid, lane), approvals=approvals, approval_open=bool(approvals))


def _notice(m, a):
    return replace(m, notice=a.get("text", ""))


def _focus(m, a):
    which = a.get("which") or ("transcript" if m.focus == "composer" else "composer")
    order = visible_targets(m) if which == "transcript" else []
    selected = m.selected if m.selected in order else (order[-1] if order else -1)
    return replace(m, focus=which, selected=selected)


def _usage(m, a):
    return replace(m, usage=a.get("usage"))


def _tools(m, a):
    return replace(m, tools=a.get("text") or "off")


def _quit_confirm(m, a):
    return replace(m, quit_confirm=bool(a.get("on")))


_HANDLERS = {
    "events": _events, "sse_event": _sse_event, "sse_done": _sse_done, "roster": _roster,
    "sent": _sent, "queue": _queue, "net": _net, "tick": _tick, "receipt_lost": _receipt_lost,
    "resize": _resize, "select": _select, "scroll": _scroll, "move": _move, "expand": _expand, "expand_all": _expand_all,
    "approval_shown": _approval_shown, "approval_answered": _approval_answered, "notice": _notice,
    "focus": _focus, "usage": _usage, "tools": _tools, "quit_confirm": _quit_confirm,
}
