"""Getting text OUT of the terminal harness: clipboard (OSC 52) and Markdown
transcript export. Pure helpers; app.py does the writing.

OSC 52 is the confirmed copy path on this fleet (Station terminal, tmux with
the default `set-clipboard external`, most SSH terminals): the escape sets the
clipboard of the terminal the operator is looking at, local or remote.
"""
from __future__ import annotations

import base64
import os
import re
import time

from .state import block_text
from . import toolcalls as tc

OSC52_MAX = 100_000          # many terminals drop larger OSC 52 payloads


def osc52(text):
    """The escape that puts `text` on the terminal clipboard ('' if empty)."""
    if not text:
        return ""
    data = base64.b64encode(text[:OSC52_MAX].encode("utf-8")).decode("ascii")
    return "\x1b]52;c;%s\x07" % data


def copy_text(block):
    """What `y` copies for one block: the reply/prompt text, or a tool call's
    input and result."""
    if block.kind == "tool":
        parts = [tc.display_name(block.name)]
        if block.detail:
            parts.append(tc.pretty_input(block.detail))
        if block.output:
            parts.append(block.output)
        return "\n\n".join(parts)
    return block_text(block)


def _fence(text, lang=""):
    ticks = "```"
    while ticks in (text or ""):
        ticks += "`"
    return "%s%s\n%s\n%s" % (ticks, lang, (text or "").rstrip("\n"), ticks)


def _stamp(block):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(block.ts)) if block.ts else ""


def to_markdown(blocks, title, meta=None):
    """A session transcript as Markdown (audit/export)."""
    out = ["# %s" % title, ""]
    for key, value in (meta or {}).items():
        if value:
            out.append("- **%s:** %s" % (key, value))
    out += ["", "---", ""]
    for b in blocks:
        when = _stamp(b)
        head = " · ".join(p for p in (when,) if p)
        if b.kind == "user":
            out += ["### ▶ operator%s" % (" · " + head if head else ""), "", b.text or "", ""]
        elif b.kind == "assistant":
            if (b.text or "").strip():
                out += ["### assistant%s" % (" · " + head if head else ""), "", b.text, ""]
        elif b.kind == "tool":
            mark = tc.status_mark(b)
            out += ["**⚒ %s** %s%s" % (tc.display_name(b.name), mark, (" · " + head) if head else ""), ""]
            if b.detail:
                out += [_fence(tc.pretty_input(b.detail), "json"), ""]
            if b.output:
                out += [_fence(b.output), ""]
        elif b.kind == "thinking":
            out += ["> 💭 " + (b.text or "").replace("\n", "\n> "), ""]
        elif b.kind in ("approval", "question"):
            out += ["**? %s** → %s" % (b.text, b.decision or "pending"), ""]
            if b.detail:
                out += [_fence(b.detail), ""]
        elif b.kind == "note":
            out += ["> %s%s" % ("**error:** " if b.ok is False else "", b.text), ""]
        else:
            out += ["*· %s*" % b.text, ""]
    return "\n".join(out).rstrip() + "\n"


def export_path(locus, sid, now=None, root=None):
    root = os.path.expanduser(root or os.environ.get("HUGPY_TUI_EXPORTS") or "~/.hugpy/exports")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", "%s-%s" % (locus or "serve", (sid or "session")[:13]))
    return os.path.join(root, "%s-%s.md" % (safe, time.strftime("%Y%m%d-%H%M%S", time.localtime(now))))


def write_private(path, text):
    """Write a file only the operator can read (transcripts carry secrets)."""
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path
