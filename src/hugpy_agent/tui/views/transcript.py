"""Wrapped transcript with collapsible tool / thinking / system cards (§3.2).

`render_lines` is pure and memoised on the things that can change what it
returns (lane identity, width, block count, tail text length, expanded set,
selection) so a 1 s poll that changed nothing costs no re-wrap.
"""
from __future__ import annotations

import curses
from typing import NamedTuple

from .panels import banners, put
from .. import toolcalls as tc
from .text import cut, wrap

CARD_LIMIT = 40          # lines of input / output shown when expanded


class Line(NamedTuple):
    text: str
    attr: str            # theme attribute NAME; resolved at draw time
    block_index: int     # selection target: block index, or a ⚙ chip target (< -1)


def _first_line(text):
    return (text or "").strip().split("\n", 1)[0]


def _mark(block):
    return tc.status_mark(block)


def _framed(label, body, width, attr, index, pad):
    lines = [Line(cut(pad + "┌ " + label, width), "MUTED", index)]
    rows = wrap(body, max(1, width - len(pad) - 2))
    for t in rows[:CARD_LIMIT]:
        lines.append(Line(cut(pad + "│ " + t, width), attr, index))
    if len(rows) > CARD_LIMIT:
        lines.append(Line(cut(pad + "│ … %d more lines" % (len(rows) - CARD_LIMIT), width), "MUTED", index))
    return lines


def tool_lines(block, index, width, expanded, wide=False, depth=0, children=0):
    """One call: collapsed = the serve one-liner (+ status/duration); expanded
    = pretty input + result framed under it (capped at CARD_LIMIT lines)."""
    pad = "  " * depth
    if block.ok is False:
        attr = "TOOL_ERR"
    elif block.ok is None and not block.meta.get("orphan"):
        attr = "TOOL"                                  # still running
    else:
        attr = "MUTED"
    lines = [Line(cut(pad + tc.head(block, expanded, children), width), attr, index)]
    if not expanded:
        if wide and block.output:
            lines.append(Line(cut(pad + "    ↳ " + _first_line(block.output), width), attr if attr == "TOOL_ERR" else "MUTED", index))
        return lines
    body_pad = pad + "  "
    if block.detail:
        lines += _framed("input", tc.pretty_input(block.detail), width, "MUTED", index, body_pad)
    if block.output:
        label = "error" if block.ok is False else "result"
        lines += _framed(label, block.output, width, "TOOL_ERR" if block.ok is False else "MUTED", index, body_pad)
    elif block.ok is None:
        lines.append(Line(cut(body_pad + ("└ no result (turn ended)" if block.meta.get("orphan") else "└ running…"), width),
                          "MUTED", index))
    return lines


def block_lines(block, index, width, expanded, wide=False, depth=0, children=0):
    """Lines for one block (attr names resolved by draw_transcript)."""
    kind = block.kind
    if kind == "user":
        return [Line(t, "USER", index) for t in wrap("▶ " + block.text, width)]
    if kind == "assistant":
        text = block.text + (" ▍" if block.streaming else "")
        return [Line(t, "ASSIST", index) for t in wrap(text, width)]
    if kind == "thinking":
        pad = "  " * depth
        if expanded:
            return [Line(t, "THINK", index) for t in wrap(pad + "💭 " + block.text, width)]
        more = "…" if "\n" in (block.text or "").strip() else ""
        return [Line(cut(pad + "💭 " + _first_line(block.text) + more, width), "THINK", index)]
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
        return tool_lines(block, index, width, expanded, wide, depth, children)
    return [Line(t, "MUTED", index) for t in wrap(block.text, width)]


_cache = {}


def render_lines(m, width, wide=False):
    lane = m.lane()
    blocks = lane.blocks
    tail = blocks[-1] if blocks else None
    cards = tuple((b.ok, len(b.output), bool(b.meta.get("orphan"))) for b in blocks if b.kind == "tool")
    key = (m.active_sid, width, wide, len(blocks), len(tail.text) + len(tail.output) if tail else 0,
           tail.ok if tail else None, tail.streaming if tail else None, tail.decision if tail else "",
           tuple(sorted(lane.expanded)), tuple(sorted(lane.groups_open)), bool(m.busy), cards)
    cached = _cache.get(m.active_sid)
    if cached and cached[0] == key:
        return cached[1]
    kids = tc.children_of(blocks)
    lines = []
    items = tc.layout(blocks, lane.groups_open, lane.expanded, m.busy)
    for n, item in enumerate(items):
        if item[0] == "chip":
            _, start, hidden, is_open = item
            errs = any(blocks[i].ok is False for i in hidden)
            lines.append(Line(cut(tc.chip_text(blocks, hidden, is_open), width),
                              "TOOL_ERR" if errs and not is_open else "MUTED", tc.group_target(start)))
            continue
        _, i, depth = item
        block = blocks[i]
        lines.extend(block_lines(block, i, width, i in lane.expanded, wide, depth, len(kids.get(i, ()))))
        if block.kind in ("user", "assistant") and i + 1 < len(blocks):
            lines.append(Line("", "MUTED", i))
    _cache.clear() if len(_cache) > 8 else None
    _cache[m.active_sid] = (key, lines)
    return lines


def draw_transcript(scr, m, rect, theme, wide=False, hits=None):
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
    if m.focus == "transcript" and m.selected != -1:
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
        if hits is not None:
            hits[y + row] = line.block_index
    if len(lines) > height and first < max_first:
        put(scr, rect.bottom - 1, rect.x + rect.w - 8, "↓ %d" % (max_first - first), theme.ACCENT)
    return first, len(lines)
