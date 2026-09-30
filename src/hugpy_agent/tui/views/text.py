"""Terminal text helpers: control-char scrub, east-asian-width aware measure,
cut and wrap. Kept separate so panels/transcript/composer share one ruler."""
from __future__ import annotations

import re
import unicodedata

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")


def clean(value, keep_newlines=False):
    """Drop control characters and whole ANSI escape sequences (terminal
    escapes must never reach the pane). `wrap` keeps "\\n" for paragraphs."""
    out = []
    for ch in _ANSI.sub("", str(value)):
        if ch == "\t":
            out.append("  ")
        elif ch == "\n" and keep_newlines:
            out.append(ch)
        elif ord(ch) < 32 or ord(ch) == 127:
            continue
        else:
            out.append(ch)
    return "".join(out)


def cw(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def width(text):
    return sum(cw(ch) for ch in text)


def cut(text, cols):
    """Prefix of `text` that fits in `cols` cells."""
    if cols <= 0:
        return ""
    used, out = 0, []
    for ch in text:
        w = cw(ch)
        if used + w > cols:
            break
        out.append(ch)
        used += w
    return "".join(out)


def wrap(text, cols):
    """Word-wrap one paragraph per input line; never returns an empty list."""
    cols = max(1, cols)
    lines = []
    for para in clean(text, keep_newlines=True).split("\n"):
        if not para:
            lines.append("")
            continue
        line, used = [], 0
        for word in para.split(" "):
            w = width(word)
            if used and used + 1 + w > cols:
                lines.append(" ".join(line))
                line, used = [], 0
            while w > cols:                       # a single token wider than the pane
                if line:
                    lines.append(" ".join(line))
                    line, used = [], 0
                head = cut(word, cols)
                lines.append(head)
                word, w = word[len(head):], w - width(head)
            line.append(word)
            used += w + (1 if used else 0)
        lines.append(" ".join(line))
    return lines or [""]
