"""Header, sidebar, status bar, splash and banners (h26 §3.2 / §3.4)."""
from __future__ import annotations

import curses
import time

from ...branding import logo_lines
from ..layout import Rect
from .text import clean, cut, width as vw

ROLE_ORDER = ("keeper", "chat", "worker", "local")


def put(scr, y, x, text, attr=0, limit=None):
    """Clipped write: never past the right edge, never off-screen, never a
    curses.error (bottom-right cell writes raise in real curses)."""
    h, w = scr.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    room = w - x - (1 if y == h - 1 else 0)
    if limit is not None:
        room = min(room, limit)
    if room <= 0:
        return
    text = cut(clean(text), room)
    if not text:
        return
    try:
        scr.addnstr(y, x, text, room, attr)
    except curses.error:
        pass


def short_id(sid):
    sid = sid or ""
    if sid.startswith("cs-"):
        return sid[:7] + "…" if len(sid) > 8 else sid
    return sid[:8] + "…" if len(sid) > 9 else sid


def role_rows(m):
    roles = list(m.roster.roles) if m.roster else []
    roles.sort(key=lambda r: ROLE_ORDER.index(r.role) if r.role in ROLE_ORDER else len(ROLE_ORDER))
    return roles


def other_sessions(m, limit=8):
    if not m.roster:
        return []
    taken = {r.id for r in m.roster.roles}
    rows = [s for s in m.roster.sessions if s.id not in taken]
    rows.sort(key=lambda s: -s.updated)
    return rows[:limit]


def draw_header(scr, m, rect, theme, folded=False):
    host = m.base.replace("http://", "").replace("https://", "")
    text = "HUGPY AGENT · %s %s" % (m.kind or "serve", host)
    if folded:
        # Narrow: the sidebar folds into the header as `[K] C W L`.
        marks = []
        for r in role_rows(m):
            mark = (r.label or r.role or "?")[:1].upper()
            marks.append("[%s]" % mark if r.id == m.active_sid else mark)
        port = host.rsplit(":", 1)[-1] if ":" in host else host
        text = "HUGPY · %s %s  %s" % ("ac" if m.kind == "abstract-claude" else (m.kind or "?"), port, " ".join(marks))
    put(scr, rect.y, rect.x, text, theme.ACCENT, rect.w)


def draw_sidebar(scr, m, rect, theme):
    if rect.h <= 0:
        return
    y = rect.y
    put(scr, y, rect.x, "ROLES", theme.ACCENT, rect.w)
    y += 1
    wide = rect.w >= 28
    for r in role_rows(m):
        if y >= rect.bottom:
            return
        mark = "●" if r.id == m.active_sid else "○"
        state = "busy" if r.busy else ("held" if r.paused else "")
        name = (r.label or r.role or short_id(r.id)).lower()
        if wide:
            line = "%s %-7s %s" % (mark, name[:7], (r.model or r.backend or "").split(":")[-1])
        else:
            line = "%s %-7s %s" % (mark, name[:7], state or (r.backend or "")[:8])
        attr = theme.SELECT if r.id == m.active_sid else (theme.HELD if r.paused else 0)
        put(scr, y, rect.x, line, attr, rect.w)
        y += 1
    others = other_sessions(m)
    if others and y + 1 < rect.bottom:
        y += 1
        put(scr, y, rect.x, "SESSIONS", theme.ACCENT, rect.w)
        y += 1
        for s in others:
            if y >= rect.bottom:
                return
            mark = "●" if s.id == m.active_sid else "○"
            line = "%s %s %s" % (mark, short_id(s.id), (s.label or s.backend)[: max(1, rect.w - 12)])
            put(scr, y, rect.x, line, theme.SELECT if s.id == m.active_sid else theme.MUTED, rect.w)
            y += 1
    # Separator column (transcript starts at rect.w + 1).
    for row in range(rect.y, rect.bottom):
        put(scr, row, rect.x + rect.w, "│", theme.MUTED, 1)


def status_fields(m, now=None):
    """Left -> right fields (§3.4); the drawer drops from the right when narrow."""
    now = time.monotonic() if now is None else now
    port = m.base.rsplit(":", 1)[-1].strip("/") if ":" in m.base else ""
    fields = ["[%s %s]" % ("ac" if m.kind == "abstract-claude" else (m.kind or "?"), port)]
    row = m.session
    if row is not None:
        fields.append("%s %s" % ((row.label or row.role or "session").lower(), short_id(row.id)))
        provider = "%s/%s" % (row.backend, (row.model or "").split(":")[-1]) if row.model else (row.backend or "")
        if row.pending_model:
            provider += " → %s staged" % row.pending_model
        fields.append(provider)
    elif m.active_sid:
        fields.append(short_id(m.active_sid))
    u = getattr(m, "usage", None)
    if u:
        toks = "tok %s in/%s out" % ("{:,}".format(getattr(u, "in_tokens", 0)),
                                       "{:,}".format(getattr(u, "out_tokens", 0)))
        cost = float(getattr(u, "cost_usd", 0) or 0)
        if cost:
            toks += " $%.2f" % cost
        fields.append(toks)
    if m.held[0]:
        fields.append("HELD")
    elif m.busy:
        fields.append("BUSY %ds" % max(0, int(now - m.busy_since)) if m.busy_since else "BUSY")
    else:
        fields.append("idle")
    if m.queue is not None:
        depth = len(m.queue.items)
        fields.append("q:%d%s" % (depth, "" if m.queue.auto else " auto off"))
    if m.usage is not None:
        total = m.usage.in_tokens + m.usage.out_tokens
        ctx = "/%s" % _k(m.usage.context_window) if m.usage.context_window else ""
        fields.append("tok %s%s" % (_k(total), ctx))
    else:
        lane = m.lane() if hasattr(m, "lane") else None
        if lane is not None and (lane.tok_in or lane.tok_out or lane.cost):
            tok = "tok %s in/%s out" % (_k(lane.tok_in), _k(lane.tok_out))
            if lane.cost:
                tok += " $%.2f" % lane.cost
            fields.append(tok)                         # cs-*: summed from per-row usage meta
        elif row is not None and row.backend == "claude" and (row.id or "").startswith("cs-"):
            fields.append("tok n/a")                   # no usage anywhere yet for this cs-* session
    fields.append("tools: %s" % (m.tools or "off"))
    net = {"live": "● live", "degraded": "◌ retrying", "down": "✕ down", "connecting": "… connecting"}[m.net]
    if m.net in ("degraded", "down") and m.net_retry_at:
        net += " %ds" % max(0, int(m.net_retry_at - now))
    fields.append(net)
    return fields


def _k(n):
    n = int(n or 0)
    return "%.1fk" % (n / 1000.0) if n >= 1000 else str(n)


def status_text(m, width, now=None, reserve=0):
    """Fields joined to fit `width` minus the help tail and `reserve` cells
    (the notice); dropped from the right until they fit."""
    fields = status_fields(m, now)
    tail = "? help"
    room = width - vw(tail) - 1 - reserve
    while len(fields) > 1 and vw(" · ".join(fields)) > room:
        fields.pop()
    return " · ".join(fields), tail


def draw_status(scr, m, rect, theme, now=None):
    notice = cut(m.notice or "", max(0, rect.w // 3))
    text, tail = status_text(m, rect.w, now, reserve=(vw(notice) + 2) if notice else 0)
    put(scr, rect.y, rect.x, text, theme.MUTED, rect.w)
    if notice:
        put(scr, rect.y, min(rect.x + vw(text) + 2, max(rect.x, rect.x + rect.w - vw(tail) - vw(notice) - 2)),
            notice, theme.ACCENT)
    put(scr, rect.y, max(rect.x, rect.x + rect.w - vw(tail) - 1), tail, theme.MUTED)
    return text


def draw_splash(scr, base, theme):
    """Branded home treatment until the first roster+events land (no timer)."""
    h, w = scr.getmaxyx()
    lines = logo_lines()
    top = max(1, (h - len(lines) - 5) // 2)
    for offset, line in enumerate(lines):
        put(scr, top + offset, max(0, (w - vw(line)) // 2), line, curses.A_BOLD)
    title = "HUGPY AGENT"
    subtitle = "Your models. Your workers. One fleet."
    status = "Connecting to " + base
    put(scr, top + len(lines) + 1, max(0, (w - len(title)) // 2), title, theme.ACCENT)
    put(scr, top + len(lines) + 2, max(0, (w - len(subtitle)) // 2), subtitle)
    put(scr, top + len(lines) + 4, max(0, (w - len(status)) // 2), status, theme.MUTED)
    put(scr, h - 2, 0, "Ctrl-C quit", theme.MUTED)


def banners(m):
    """Held / network banner lines shown at the top of the transcript pane."""
    out = []
    if m.held[0]:
        out.append(("HELD: %s — r retry · Ctrl-K queue" % (m.held[1] or "paused"), "held"))
    if m.net == "down":
        out.append(("serve unreachable — retrying, cursor kept", "down"))
    return out
