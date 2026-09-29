"""Wrapped transcript with collapsible tool / thinking / system cards (§3.2).

`render_lines` is pure and memoised on the things that can change what it
returns (lane identity, width, block count, tail text length, expanded set,
selection) so a 1 s poll that changed nothing costs no re-wrap.
"""
from __future__ import annotations

import curses
from typing import NamedTuple

from .panels import banners, put
from .text import cut, wrap

CARD_LIMIT = 40          # lines of input / output shown when expanded


class Line(NamedTuple):
    text: str
    attr: str            # theme attribute NAME; resolved at draw time
    block_index: int


def _first_line(text):
    return (text or "").strip().split("\n", 1)[0]


def _mark(block):
    if block.ok is True:
        return "✓"
    if block.ok is False:
        return "✗"
    return "…"


def block_lines(block, index, width, expanded, wide=False):
    """Lines for one block (attr names resolved by draw_transcript)."""
    kind = block.kind
    if kind == "user":
        return [Line(t, "USER", index) for t in wrap("▶ " + block.text, width)]
    if kind == "assistant":
        text = block.text + (" ▍" if block.streaming else "")
        return [Line(t, "ASSIST", index) for t in wrap(text, width)]
    if kind == "thinking":
        if expanded:
            return [Line(t, "THINK", index) for t in wrap("💭 " + block.text, width)]
        return [Line(cut("💭 " + _first_line(block.text) + "…", width), "THINK", index)]
    if kind == "system":
        if expanded and block.detail:
            return [Line(t, "MUTED", index) for t in wrap("· " + block.text + "\n" + block.detail, width)]
        return [Line(cut("· " + block.text, width), "MUTED", index)]
    if kind == "note":
        return [Line(t, "TOOL_ERR" if block.ok is False else "MUTED", index) for t in wrap("» " + block.text, width)]
    if kind in ("approval", "question"):
        state = block.decision or "pending"
        return [Line(t, "TOOL", index) for t in wrap("? %s → %s" % (block.text, state), width)]
    if kind == "tool":
        attr = "TOOL_ERR" if block.ok is False else "TOOL"
        head = "⚒ %s · %s  [%s]" % (block.name or "tool", _first_line(block.text), _mark(block))
        if not expanded:
            lines = [Line(cut(head, width), attr, index)]
            if wide and block.output:
                lines.append(Line(cut("  " + _first_line(block.output), width), "MUTED", index))
            return lines
        lines = [Line(cut(head, width), attr, index)]
        for label, body in (("input", block.detail), ("output", block.output)):
            if not body:
                continue
            lines.append(Line(cut("  ┌ " + label, width), "MUTED", index))
            rows = wrap(body, max(1, width - 4))
            for t in rows[:CARD_LIMIT]:
                lines.append(Line("  │ " + t, "MUTED", index))
            if len(rows) > CARD_LIMIT:
                lines.append(Line("  │ … %d more lines" % (len(rows) - CARD_LIMIT), "MUTED", index))
        return lines
    return [Line(t, "MUTED", index) for t in wrap(block.text, width)]


_cache = {}


def render_lines(m, width, wide=False):
    lane = m.lane()
    tail = lane.blocks[-1] if lane.blocks else None
    key = (m.active_sid, width, wide, len(lane.blocks), len(tail.text) + len(tail.output) if tail else 0,
           tail.ok if tail else None, tail.streaming if tail else None, tail.decision if tail else "",
           tuple(sorted(lane.expanded)))
    cached = _cache.get(m.active_sid)
    if cached and cached[0] == key:
        return cached[1]
    lines = []
    for i, block in enumerate(lane.blocks):
        lines.extend(block_lines(block, i, width, i in lane.expanded, wide))
        if block.kind in ("user", "assistant") and i + 1 < len(lane.blocks):
            lines.append(Line("", "MUTED", i))
    _cache.clear() if len(_cache) > 8 else None
    _cache[m.active_sid] = (key, lines)
    return lines


def draw_transcript(scr, m, rect, theme, wide=False):
    """Returns (first_visible_line, total_lines) for the scroll reducer."""
    if rect.h <= 0 or rect.w <= 0:
        return 0, 0
    y = rect.y
    for text, kind in banners(m):
        if y >= rect.bottom:
            break
        put(scr, y, rect.x, cut(text, rect.w).ljust(rect.w), theme.HELD if kind == "held" else theme.TOOL_ERR, rect.w)
        y += 1
    height = rect.bottom - y
    lines = render_lines(m, rect.w, wide)
    scroll = m.lane().scroll
    if height <= 0:
        return 0, len(lines)
    max_first = max(0, len(lines) - height)
    if m.focus == "transcript" and 0 <= m.selected < len(m.blocks):
        # Keep the selected block in view.
        rows = [i for i, ln in enumerate(lines) if ln.block_index == m.selected]
        if rows and scroll >= 0 and not (scroll <= rows[0] < scroll + height):
            scroll = min(max_first, rows[0])
        elif rows and scroll < 0 and rows[0] < max_first:
            scroll = rows[0]
    first = max_first if scroll < 0 else min(scroll, max_first)
    for row, line in enumerate(lines[first:first + height]):
        attr = getattr(theme, line.attr, 0)
        if m.focus == "transcript" and line.block_index == m.selected:
            attr |= theme.SELECT
        put(scr, y + row, rect.x, line.text, attr, rect.w)
    if len(lines) > height and first < max_first:
        put(scr, rect.bottom - 1, rect.x + rect.w - 8, "↓ %d" % (max_first - first), theme.ACCENT)
    return first, len(lines)
