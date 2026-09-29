"""Workspace memory — markdown facts, keeper-memory shaped (design §3.4).

One fact per file under `<workspace>/memory/` plus a MEMORY.md index, loaded
at session start. Markdown files (not a DB) on purpose: greppable,
human-editable, portable, and diffable in the workspace's own VCS.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re


class Memory:
    def __init__(self, workspace: str):
        self.dir = os.path.join(os.path.realpath(workspace), "memory")
        self.index_path = os.path.join(self.dir, "MEMORY.md")

    def load_index(self) -> str:
        """The index text, injected into the system prompt at session start.
        Only the index — individual fact files are fetched on demand via
        fs_read, keeping the pinned prompt small."""
        try:
            with open(self.index_path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def remember(self, fact: str, title: str = "") -> str:
        """`remember` tool handler: one markdown file per fact + index line."""
        title = (title or fact[:48]).strip()
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "fact"
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, slug + ".md")
        n = 2
        while os.path.exists(path):   # never silently overwrite a prior fact
            path = os.path.join(self.dir, "%s-%d.md" % (slug, n))
            n += 1
        stamp = _dt.date.today().isoformat()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# %s\n\n%s\n\n_(recorded %s)_\n" % (title, fact, stamp))
        fname = os.path.basename(path)
        line = "- [%s](%s) — %s\n" % (title, fname, stamp)
        if not os.path.exists(self.index_path):
            with open(self.index_path, "w", encoding="utf-8") as fh:
                fh.write("# Memory index\n\n" + line)
        else:
            with open(self.index_path, "a", encoding="utf-8") as fh:
                fh.write(line)
        return json.dumps({"remembered": fname, "title": title})
