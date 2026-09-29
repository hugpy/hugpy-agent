"""Full-screen modals: picker (ported from fleet_tui.Console.choose), approval,
queue, and a read-only text viewer for /help and /tools. Each modal owns the
getch loop while open and calls `drain()` every tick so poll results keep
flowing into the model (and a countdown can advance) underneath it.
"""
from __future__ import annotations

import curses
import json
import time

from .panels import put
from .text import wrap

ENTER = (10, 13, curses.KEY_ENTER)
ESC = 27
APPROVAL_KEYS = {"y": "accept", "a": "acceptForSession", "n": "decline", "c": "cancel"}
QUEUE_ACTIONS = (("e", "edit"), ("d", "remove"), ("a", "auto"), ("r", "retry"), ("x", "clear"))


def _frame(scr, title, theme, footer):
    scr.erase()
    h, w = scr.getmaxyx()
    put(scr, 0, 0, title, theme.ACCENT, w)
    put(scr, h - 2, 0, footer, theme.MUTED, w)
    return h, w


def _tick(scr, drain):
    key = scr.getch()
    if key == -1 and drain:
        drain()
    return key


def choose(scr, title, options, theme, drain=None, selected=0):
    """Up/Down + Enter -> index; Esc/q -> None. Long lists page."""
    while True:
        h, w = _frame(scr, title, theme, "Up/Down select · Enter choose · Esc back")
        count = max(1, h - 5)
        start = selected // count * count
        for i, label in enumerate(options[start:start + count], start):
            put(scr, 2 + i - start, 2, label, theme.SELECT if i == selected else 0, w - 2)
        scr.refresh()
        key = _tick(scr, drain)
        if key in (ESC, ord("q")):
            return None
        if key in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = min(len(options) - 1, selected + 1)
        elif key in (curses.KEY_NPAGE,):
            selected = min(len(options) - 1, selected + count)
        elif key in (curses.KEY_PPAGE,):
            selected = max(0, selected - count)
        elif key in ENTER and options:
            return selected


def approval_body(approval):
    params = approval.params
    if isinstance(params, dict):
        lines = []
        for key in ("command", "cwd", "paths", "reason"):
            if key in params:
                lines.append("%s: %s" % (key, params[key]))
        rest = {k: v for k, v in params.items() if k not in ("command", "cwd", "paths", "reason")}
        if rest:
            lines.append(json.dumps(rest, indent=1)[:2000])
        return "\n".join(lines) or "(no parameters)"
    return str(params or "")


def approval_modal(scr, approval, theme, drain=None, now=None):
    """Returns the decision string, or None when hidden with Esc (stays pending)."""
    selected = 0
    options = list(approval.options) or ["accept", "decline"]
    while True:
        now_s = time.time() if now is None else now
        title = ("? %s" % approval.title) if approval.kind == "approval" else "? question"
        keys = "y accept · a session · n decline · c cancel" if approval.kind == "approval" else "1-9 choose"
        h, w = _frame(scr, title, theme, keys + " · Up/Down+Enter · Esc hide (Ctrl-A reopens)")
        body = approval.title if approval.kind == "question" else approval_body(approval)
        y = 2
        for line in wrap(body, w - 4)[: max(1, h - 8 - len(options))]:
            put(scr, y, 2, line, 0, w - 2)
            y += 1
        y += 1
        for i, label in enumerate(options):
            prefix = "%d " % (i + 1) if approval.kind == "question" else ""
            put(scr, y + i, 2, prefix + label, theme.SELECT if i == selected else 0, w - 2)
        if approval.kind == "question" and approval.ts:
            left = int(approval.ts + 300 - now_s)
            put(scr, h - 3, 0, "expires in %ds" % max(0, left), theme.TOOL_ERR if left < 60 else theme.MUTED)
        scr.refresh()
        key = _tick(scr, drain)
        if key == ESC:
            return None
        if key in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = min(len(options) - 1, selected + 1)
        elif key in ENTER:
            return options[selected]
        elif approval.kind == "approval" and 0 <= key < 256 and chr(key) in APPROVAL_KEYS:
            return APPROVAL_KEYS[chr(key)]
        elif approval.kind == "question" and ord("1") <= key <= ord("9") and key - ord("1") < len(options):
            return options[key - ord("1")]


def queue_modal(scr, queue, theme, drain=None):
    """Returns (action, payload) or None. Payload: item for edit/remove,
    bool for auto, None for retry/clear."""
    selected = 0
    while True:
        items = list(queue.items) if queue else []
        head = "QUEUE · auto %s · %s" % ("on" if (queue and queue.auto) else "off",
                                       "paused" if (queue and queue.paused) else "running")
        h, w = _frame(scr, head, theme, "e edit · d remove · a auto toggle · r retry · x clear · Esc")
        if not items:
            put(scr, 2, 2, "(queue empty)", theme.MUTED, w - 2)
        for i, item in enumerate(items[: max(1, h - 5)]):
            put(scr, 2 + i, 2, "%s  %s" % (item.id[:8], item.text.replace("\n", " ")),
                theme.SELECT if i == selected else 0, w - 2)
        scr.refresh()
        key = _tick(scr, drain)
        if key in (ESC, ord("q")):
            return None
        if key in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = max(0, min(len(items) - 1, selected + 1))
        elif key == ord("a"):
            return "auto", not (queue.auto if queue else True)
        elif key == ord("r"):
            return "retry", None
        elif key == ord("x"):
            return "clear", None
        elif key in (ord("e"), ord("d")) and items:
            return ("edit" if key == ord("e") else "remove"), items[selected]


def text_modal(scr, title, lines, theme, drain=None):
    """Scrollable read-only viewer (help overlay, /tools list, queue edit view)."""
    first = 0
    while True:
        h, w = _frame(scr, title, theme, "Up/Down/PgUp/PgDn scroll · Esc/q close")
        rows = []
        for line in lines:
            rows.extend(wrap(line, w - 2))
        height = max(1, h - 4)
        first = max(0, min(first, len(rows) - height))
        for i, row in enumerate(rows[first:first + height]):
            put(scr, 2 + i, 1, row, 0, w - 1)
        scr.refresh()
        key = _tick(scr, drain)
        if key in (ESC, ord("q")):
            return None
        if key in (curses.KEY_UP, ord("k")):
            first -= 1
        elif key in (curses.KEY_DOWN, ord("j")):
            first += 1
        elif key == curses.KEY_PPAGE:
            first -= height
        elif key in (curses.KEY_NPAGE, ord(" ")):
            first += height
        elif key in ENTER:
            return None


def line_edit(scr, title, default, theme, drain=None):
    """Single-line editor for queue item edits; Esc cancels."""
    value = default
    while True:
        h, w = _frame(scr, title, theme, "Enter accept · Ctrl-U clear · Esc cancel")
        put(scr, 2, 0, value + "_", 0, w)
        scr.refresh()
        key = _tick(scr, drain)
        if key == ESC:
            return None
        if key in ENTER:
            return value
        if key in (curses.KEY_BACKSPACE, 127, 8):
            value = value[:-1]
        elif key == 21:
            value = ""
        elif 32 <= key < 0x110000 and key != curses.KEY_RESIZE:
            try:
                value += chr(key)
            except ValueError:
                pass
