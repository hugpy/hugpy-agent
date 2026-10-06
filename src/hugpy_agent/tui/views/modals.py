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
# Key -> decision synonyms across the serve's two approval vocabularies:
# provider approvals (accept/acceptForSession/decline/cancel) and serve
# permission requests (allow_once/allow_session/deny).
KEY_DECISIONS = {"y": ("accept", "allow_once"), "a": ("acceptForSession", "allow_session"),
                 "n": ("decline", "deny"), "c": ("cancel",)}
DECISION_LABELS = {"accept": "accept", "acceptForSession": "accept for this session",
                   "decline": "decline", "cancel": "cancel", "allow_once": "allow once",
                   "allow_session": "allow for this session", "deny": "deny"}


def key_decision(key, options):
    """The option a y/a/n/c key means for THIS approval, or None."""
    for wanted in KEY_DECISIONS.get(key, ()):
        if wanted in options:
            return wanted
    return None
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
    """Up/Down + Enter -> index into `options`; Esc/q -> None. Long lists
    page. `/` filters (case-insensitive substring; Esc clears the filter)."""
    flt, filtering = "", False
    while True:
        shown = [i for i, label in enumerate(options) if flt.lower() in str(label).lower()]
        if selected not in shown:
            selected = shown[0] if shown else -1
        pos = shown.index(selected) if selected in shown else 0
        if filtering or flt:
            footer = "filter: %s%s · %d/%d · Enter choose · Esc clear" % (flt, "_" if filtering else "",
                                                                          len(shown), len(options))
        else:
            footer = "Up/Down select · Enter choose · / filter · Esc back"
        h, w = _frame(scr, title, theme, footer)
        count = max(1, h - 5)
        start = pos // count * count
        for row, i in enumerate(shown[start:start + count]):
            put(scr, 2 + row, 2, options[i], theme.SELECT if i == selected else 0, w - 2)
        if len(shown) > count:
            put(scr, 1, max(0, w - 12), "%d-%d/%d" % (start + 1, min(len(shown), start + count), len(shown)),
                theme.MUTED)
        if not shown:
            put(scr, 2, 2, "(no match)", theme.MUTED, w - 2)
        scr.refresh()
        key = _tick(scr, drain)
        if key == ESC:
            if filtering or flt:
                flt, filtering = "", False
                continue
            return None
        if filtering and key not in ENTER and key not in (curses.KEY_UP, curses.KEY_DOWN,
                                                          curses.KEY_NPAGE, curses.KEY_PPAGE):
            if key in (curses.KEY_BACKSPACE, 127, 8):
                flt = flt[:-1]
            elif 32 <= key < 0x110000 and key != curses.KEY_RESIZE:
                try:
                    flt += chr(key)
                except ValueError:
                    pass
            continue
        if key == ord("/"):
            filtering = True
        elif key == ord("q"):
            return None
        elif key in (curses.KEY_UP, ord("k")) and shown:
            selected = shown[max(0, pos - 1)]
        elif key in (curses.KEY_DOWN, ord("j")) and shown:
            selected = shown[min(len(shown) - 1, pos + 1)]
        elif key in (curses.KEY_NPAGE,) and shown:
            selected = shown[min(len(shown) - 1, pos + count)]
        elif key in (curses.KEY_PPAGE,) and shown:
            selected = shown[max(0, pos - count)]
        elif key in ENTER and shown:
            return selected


def approval_body(approval):
    params = approval.params
    if isinstance(params, dict):
        lines = []
        first = ("command", "cwd", "paths", "description", "reason", "risk")
        for key in first + tuple(k for k in params if k not in first):
            if key not in params:
                continue
            value = params[key]
            if isinstance(value, (dict, list)):          # nested: readable JSON under its key
                lines.append("%s: %s" % (key, json.dumps(value, indent=1, ensure_ascii=False)[:2000]))
            else:
                lines.append("%s: %s" % (key, value))
        return "\n".join(lines) or "(no parameters)"
    return str(params or "")


def approval_modal(scr, approval, theme, drain=None, now=None, alive=None):
    """Returns the decision string, or None when hidden with Esc (stays
    pending) or when `alive()` turns False (answered elsewhere / expired)."""
    selected = 0
    options = list(approval.options) or ["accept", "decline"]
    while True:
        if alive is not None and not alive():
            return None
        now_s = time.time() if now is None else now
        title = ("? %s" % approval.title) if approval.kind == "approval" else "? question"
        if approval.kind == "approval":
            keys = " · ".join("%s %s" % (k, DECISION_LABELS.get(key_decision(k, options), ""))
                              for k in "yanc" if key_decision(k, options))
        else:
            keys = "1-9 choose"
        h, w = _frame(scr, title, theme, keys + " · Up/Down+Enter · Esc hide (Ctrl-A reopens)")
        body = approval.title if approval.kind == "question" else approval_body(approval)
        y = 2
        for line in wrap(body, w - 4)[: max(1, h - 8 - len(options))]:
            put(scr, y, 2, line, 0, w - 2)
            y += 1
        y += 1
        for i, label in enumerate(options):
            prefix = "%d " % (i + 1) if approval.kind == "question" else ""
            shown = DECISION_LABELS.get(label, label) if approval.kind == "approval" else label
            put(scr, y + i, 2, prefix + shown, theme.SELECT if i == selected else 0, w - 2)
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
        elif approval.kind == "approval" and 0 <= key < 256 and key_decision(chr(key), options):
            return key_decision(chr(key), options)
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


def text_modal(scr, title, lines, theme, drain=None, at_end=False):
    """Scrollable read-only viewer (help overlay, /tools list, /log, queue
    edit view). `at_end` opens at the bottom (newest log rows)."""
    first = 10 ** 9 if at_end else 0
    while True:
        h, w = _frame(scr, title, theme, "Up/Down/PgUp/PgDn/Home/End scroll · Esc/q close")
        rows = []
        for line in lines:
            rows.extend(wrap(line, w - 2))
        height = max(1, h - 4)
        first = max(0, min(first, len(rows) - height))
        if len(rows) > height:
            put(scr, 0, max(0, w - 16), "%d-%d/%d" % (first + 1, min(len(rows), first + height), len(rows)),
                theme.MUTED)
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
        elif key in (curses.KEY_HOME, ord("g")):
            first = 0
        elif key in (curses.KEY_END, ord("G")):
            first = 10 ** 9
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
