"""Multi-line composer with a 50-entry history ring (§3.1).

Newline is `\\`+Enter (mct_repl convention, decision 5) or Alt+Enter; a plain
Enter submits. The App owns key decoding; this class only edits the buffer.
"""
from __future__ import annotations

from collections import deque

from .panels import put
from .text import clean, cut, width

HISTORY = 50


class Composer:
    def __init__(self):
        self.buffer = ""
        self.cursor = 0
        self.history = deque(maxlen=HISTORY)
        self.hist_index = None     # None = editing a fresh line
        self.stash = ""

    # -- editing -----------------------------------------------------------
    def insert(self, text):
        text = clean(text) if "\n" not in text else text
        self.buffer = self.buffer[:self.cursor] + text + self.buffer[self.cursor:]
        self.cursor += len(text)
        self.hist_index = None

    def newline(self):
        self.buffer = self.buffer[:self.cursor] + "\n" + self.buffer[self.cursor:]
        self.cursor += 1

    def backspace(self):
        if self.cursor:
            self.buffer = self.buffer[:self.cursor - 1] + self.buffer[self.cursor:]
            self.cursor -= 1

    def delete(self):
        self.buffer = self.buffer[:self.cursor] + self.buffer[self.cursor + 1:]

    def left(self):
        self.cursor = max(0, self.cursor - 1)

    def right(self):
        self.cursor = min(len(self.buffer), self.cursor + 1)

    def home(self):
        self.cursor = self.buffer.rfind("\n", 0, self.cursor) + 1

    def end(self):
        nxt = self.buffer.find("\n", self.cursor)
        self.cursor = len(self.buffer) if nxt < 0 else nxt

    def clear(self):
        self.buffer, self.cursor, self.hist_index = "", 0, None

    def kill_line(self):
        self.home()
        end = self.buffer.find("\n", self.cursor)
        end = len(self.buffer) if end < 0 else end
        self.buffer = self.buffer[:self.cursor] + self.buffer[end:]

    @property
    def multiline(self):
        return "\n" in self.buffer

    # -- submit / history ----------------------------------------------------
    def submit(self):
        """Enter: a trailing backslash before the cursor means newline; else
        return the text (None when nothing to send) and push it to history."""
        if self.buffer[:self.cursor].endswith("\\"):
            self.backspace()
            self.newline()
            return None
        text = self.buffer.strip()
        self.clear()
        if not text:
            return None
        if not self.history or self.history[-1] != text:
            self.history.append(text)
        return text

    def recall(self, delta):
        """Up (-1) / Down (+1) through history; True when the buffer changed."""
        if not self.history:
            return False
        if self.hist_index is None:
            if delta > 0:
                return False
            self.stash, self.hist_index = self.buffer, len(self.history)
        index = self.hist_index + delta
        if index < 0:
            return False
        if index >= len(self.history):
            self.buffer, self.cursor, self.hist_index = self.stash, len(self.stash), None
            return True
        self.hist_index = index
        self.buffer = self.history[index]
        self.cursor = len(self.buffer)
        return True

    # -- geometry --------------------------------------------------------------
    def lines(self, cols):
        """Visual rows (soft-wrapped at `cols`) and the cursor's (row, col)."""
        cols = max(1, cols)
        rows, cursor_rc, pos = [], (0, 0), 0
        for para in self.buffer.split("\n"):
            chunks, cur, used = [], "", 0
            for ch in para:
                w = width(ch)
                if used + w > cols:
                    chunks.append(cur)
                    cur, used = "", 0
                cur, used = cur + ch, used + w
            chunks.append(cur)
            for chunk in chunks:
                if pos <= self.cursor <= pos + len(chunk):
                    cursor_rc = (len(rows), width(chunk[:self.cursor - pos]))
                rows.append(chunk)
                pos += len(chunk)
            pos += 1          # the newline
        return rows or [""], cursor_rc


def draw_composer(scr, comp, rect, theme, prompt="> "):
    """Draw and return the (y, x) the hardware cursor should sit at."""
    cols = max(1, rect.w - width(prompt))
    rows, (cr, cc) = comp.lines(cols)
    first = max(0, cr - rect.h + 1)
    for i, row in enumerate(rows[first:first + rect.h]):
        put(scr, rect.y + i, rect.x, (prompt if i + first == 0 else " " * width(prompt)) + row, 0, rect.w)
    if len(rows) > rect.h:
        put(scr, rect.y, rect.x + rect.w - 6, "+%d" % (len(rows) - rect.h), theme.MUTED)
    return rect.y + (cr - first), min(rect.x + rect.w - 1, rect.x + width(prompt) + cc)
