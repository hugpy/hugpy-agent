"""Header, sidebar, status bar, splash and banners (h26 §3.2 / §3.4)."""
from __future__ import annotations

import curses
import os
import time

from ...branding import logo_lines
from ..layout import Rect
from .text import clean, cut, width as vw

ROLE_ORDER = ("keeper", "chat", "worker", "local")
SHELL_ROW = "__shell__"       # sidebar hit id of the standing shell row


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


def draw_header(scr, m, rect, theme, folded=False, loci=None, active_locus="", locus_hits=None):
    host = m.base.replace("http://", "").replace("https://", "")
    text = "hugpy-agent %s" % host
    if folded:
        # Narrow: the sidebar folds into the header as `[K] C W L`.
        marks = []
        for r in role_rows(m):
            mark = (r.label or r.role or "?")[:1].upper()
            marks.append("[%s]" % mark if r.id == m.active_sid else mark)
        port = host.rsplit(":", 1)[-1] if ":" in host else host
        text = "hugpy :%s  %s" % (port, " ".join(marks))
        if active_locus:
            text += "  @" + active_locus
        put(scr, rect.y, rect.x, text, theme.ACCENT, rect.w)
        return
    put(scr, rect.y, rect.x, text, theme.ACCENT, rect.w)
    if not loci:
        return
    # Locus tabs, right of the title: `LOCUS  hugpy │ keeper │ hs-fresh`
    # (current one highlighted; each tab's x-span lands in locus_hits for clicks).
    x = rect.x + len(text) + 3
    put(scr, rect.y, x, "LOCUS", theme.MUTED, max(0, rect.w - x))
    x += 6
    for i, entry in enumerate(loci):
        name = entry["locus"]
        if i:
            put(scr, rect.y, x, " │ ", theme.MUTED, max(0, rect.w - x))
            x += 3
        if x + len(name) > rect.w:
            break
        current = name == active_locus
        put(scr, rect.y, x, ("[%s]" % name) if current else name,
            theme.SELECT if current else 0, max(0, rect.w - x))
        span = len(name) + (2 if current else 0)
        if locus_hits is not None:
            locus_hits.append((x, x + span - 1, name))
        x += span


def draw_sidebar(scr, m, rect, theme, hits=None):
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
        if hits is not None:
            hits[y] = r.id
        y += 1
    # Standing shell, below Local/B (operator 2026-10-04): a plain login shell on
    # this host; the TUI suspends while it runs and `exit` comes back here.
    if m.roster and y < rect.bottom:
        put(scr, y, rect.x, "○ %-7s %s" % ("shell", os.path.basename(os.environ.get("SHELL") or "bash")) if rect.w >= 28
            else "○ shell", 0, rect.w)
        if hits is not None:
            hits[y] = SHELL_ROW
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
            if hits is not None:
                hits[y] = s.id
            y += 1
    # Separator column (transcript starts at rect.w + 1).
    for row in range(rect.y, rect.bottom):
        put(scr, row, rect.x + rect.w, "│", theme.MUTED, 1)


SERVE_LABELS = {"abstract-serve": "abstract-serve", "abstract-claude": "abstract-serve", "hugpy": "hugpy-agent"}


def serve_label(m):
    """Provider-neutral serve field (branding ruling 2026-10-06): the API the
    TUI speaks + its port, never a provider name."""
    port = m.base.rsplit(":", 1)[-1].strip("/") if ":" in m.base.split("//", 1)[-1] else ""
    name = SERVE_LABELS.get(m.kind, m.kind or "serve")
    return "[%s %s]" % (name, port) if port else "[%s]" % name


def token_field(m):
    """ONE token field: provider usage totals when the serve has them, else
    the per-call counters (context of the last call + output so far)."""
    u = m.usage
    if u is not None and ((u.in_tokens or 0) + (u.out_tokens or 0) or u.context_window or u.cost_usd):
        total = (u.in_tokens or 0) + (u.out_tokens or 0)
        text = "tok %s%s" % (_k(total), "/%s" % _k(u.context_window) if u.context_window else "")
        if float(u.cost_usd or 0):
            text += " $%.2f" % u.cost_usd
        return text
    lane = m.lane() if hasattr(m, "lane") else None
    if lane is not None and lane.ctx_tokens:
        text = "ctx %s · out %s" % (_k(lane.ctx_tokens), _k(lane.tok_out))
    elif lane is not None and (lane.tok_in or lane.tok_out):
        text = "tok %s in/%s out" % (_k(lane.tok_in), _k(lane.tok_out))
    else:
        row = m.session
        return "tok n/a" if row is not None and (row.id or "").startswith("cs-") else ""
    if lane.cost:
        text += " $%.2f" % lane.cost
    return text


# Drop order when the bar is narrow: lowest priority first (rightmost among
# equals). What the operator must always see — which serve, which session,
# busy/held, unseen errors, connection — outranks counters.
_KEEP, _HIGH, _MID, _LOW = 4, 3, 2, 1


def status_items(m, now=None):
    """[(text, priority)] left -> right (§3.4)."""
    now = time.monotonic() if now is None else now
    items = [(serve_label(m), _KEEP)]
    row = m.session
    if row is not None:
        items.append(("%s %s" % ((row.label or row.role or "session").lower(), short_id(row.id)), _HIGH))
        provider = "%s/%s" % (row.backend, (row.model or "").split(":")[-1]) if row.model else (row.backend or "")
        if row.pending_model:
            provider += " → %s staged" % row.pending_model
        items.append((provider, _MID))
    elif m.active_sid:
        items.append((short_id(m.active_sid), _HIGH))
    if m.held[0]:
        items.append(("HELD", _KEEP))
    elif m.busy:
        items.append(("BUSY %ds" % max(0, int(now - m.busy_since)) if m.busy_since else "BUSY", _HIGH))
    else:
        items.append(("idle", _HIGH))
    if getattr(m, "alerts", 0):
        items.append(("⚠ %d /log" % m.alerts, _HIGH))
    if m.queue is not None:
        depth = len(m.queue.items)
        items.append(("q:%d%s" % (depth, "" if m.queue.auto else " auto off"), _MID))
    tok = token_field(m)
    if tok:
        items.append((tok, _LOW))
    items.append(("tools: %s" % (m.tools or "off"), _LOW))
    net = {"live": "● live", "degraded": "◌ retrying", "down": "✕ down", "connecting": "… connecting"}[m.net]
    if m.net in ("degraded", "down") and m.net_retry_at:
        net += " %ds" % max(0, int(m.net_retry_at - now))
    items.append((net, _HIGH if m.net == "live" else _KEEP))
    return items


def status_fields(m, now=None):
    """Left -> right field texts (§3.4)."""
    return [text for text, _ in status_items(m, now)]


def _k(n):
    n = int(n or 0)
    return "%.1fk" % (n / 1000.0) if n >= 1000 else str(n)


def status_text(m, width, now=None, reserve=0):
    """Fields joined to fit `width` minus the help tail and `reserve` cells
    (the notice); dropped from the right until they fit."""
    items = status_items(m, now)
    tail = "F1 help"
    room = width - vw(tail) - 1 - reserve
    while len(items) > 1 and vw(" · ".join(t for t, _ in items)) > room:
        low = min(p for _, p in items)
        drop = max(i for i, (_, p) in enumerate(items) if p == low)
        items.pop(drop)
    return " · ".join(t for t, _ in items), tail


def draw_status(scr, m, rect, theme, now=None):
    # errors get up to half the bar (they carry the fix); info a third
    share = 2 if getattr(m, "notice_level", "info") == "error" else 3
    notice = cut(m.notice or "", max(0, rect.w // share))
    text, tail = status_text(m, rect.w, now, reserve=(vw(notice) + 2) if notice else 0)
    put(scr, rect.y, rect.x, text, theme.MUTED, rect.w)
    if notice:
        attr = theme.TOOL_ERR if getattr(m, "notice_level", "info") == "error" else theme.ACCENT
        put(scr, rect.y, min(rect.x + vw(text) + 2, max(rect.x, rect.x + rect.w - vw(tail) - vw(notice) - 2)),
            notice, attr)
    put(scr, rect.y, max(rect.x, rect.x + rect.w - vw(tail) - 1), tail, theme.MUTED)
    return text


def draw_splash(scr, base, theme, version=""):
    """Branded home treatment until the first roster+events land (no timer)."""
    h, w = scr.getmaxyx()
    lines = logo_lines() if h >= 20 and w >= 60 else []
    top = max(1, (h - len(lines) - 5) // 2)
    for offset, line in enumerate(lines):
        put(scr, top + offset, max(0, (w - vw(line)) // 2), line, curses.A_BOLD)
    title = "hugpy-agent" + (" " + version if version else "")
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
