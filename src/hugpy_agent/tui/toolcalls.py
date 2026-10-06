"""Tool-call presentation rules, ported from the shared Serve console
(pure: no curses, no I/O; transcript.py draws, state.py selects).

Serve rules this mirrors (server.py `_fmt_tool` + webui `Or`/`AcCalls`):

* a call is ONE muted line ``⚒ <name> · <summary>`` in the conversation flow at
  the position of its tool_use; ``▸``/``▾`` marks it expandable/expanded;
  expanding shows the full input (pretty JSON) and the result text.
* the summary is the first matching argument: file_path/path/notebook_path,
  command (160), pattern (120, `  (path)` appended), url, description/prompt
  (120), else compact JSON (160).
* results are ``✓``/``✗``; errors are drawn in the error colour. Here the
  result attaches to its call (by tool_use id when the serve sends one, else
  first-open-first-served) and the call line gains status + duration.
* consecutive calls (tool/thinking) collapse into a chip
  ``▸ ⚙ N calls · <last call>`` (110 chars of the last one); while the turn
  is live the most recent call stays visible under the chip.
* Agent/Task calls own the calls whose ``parent`` is their id; those render
  nested (indented) only when the Agent card is expanded.
"""
from __future__ import annotations

import json

SUMMARY_MAX = 160
PATTERN_MAX = 120
CHIP_LAST_MAX = 110
AGENT_TOOLS = ("Agent", "Task")
GROUP_KINDS = ("tool", "thinking")


def _clip(text, n):
    text = " ".join(str(text or "").strip().split("\n")).replace("\t", " ")
    return text if len(text) <= n else text[:n - 1] + "…"


def parse_input(detail):
    """Tool input as a dict when it is JSON (serve sends a JSON string)."""
    if isinstance(detail, dict):
        return detail
    if not detail or not isinstance(detail, str):
        return None
    s = detail.strip()
    if not s.startswith("{"):
        return None
    try:
        value = json.loads(s)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


# -- per-tool summarisers: input dict -> str ("" = no opinion, use fallback) --

def _path(a):
    return str(a.get("file_path") or a.get("notebook_path") or a.get("path") or "")


def _bash(a):
    lines = str(a.get("command") or "").strip().splitlines()
    if not lines:
        return str(a.get("description") or "")
    return lines[0] + (" …" if len(lines) > 1 else "")


def _search(a):
    pat = _clip(a.get("pattern") or "", PATTERN_MAX)
    return pat + ("  (%s)" % a["path"] if pat and a.get("path") else "")


def _agent(a):
    what = str(a.get("description") or a.get("prompt") or "")
    kind = a.get("subagent_type")
    return ("%s: %s" % (kind, what)) if kind and what else what


def _todo(a):
    todos = a.get("todos")
    return "%d todos" % len(todos) if isinstance(todos, list) else ""


SUMMARISERS = {
    "Bash": _bash,
    "Read": _path, "Edit": _path, "MultiEdit": _path, "Write": _path, "NotebookEdit": _path,
    "Grep": _search, "Glob": _search,
    "WebFetch": lambda a: str(a.get("url") or ""),
    "WebSearch": lambda a: str(a.get("query") or ""),
    "Agent": _agent, "Task": _agent,
    "TodoWrite": _todo,
}


def generic_summary(a):
    """serve `_fmt_tool` order for any tool without its own entry."""
    for k in ("file_path", "path", "notebook_path"):
        if a.get(k):
            return str(a[k])
    if a.get("command"):
        return _bash(a)
    if a.get("pattern"):
        return _search(a)
    if a.get("url"):
        return str(a["url"])
    for k in ("description", "prompt", "query"):
        if a.get(k):
            return _clip(a[k], PATTERN_MAX)
    if not a:
        return ""
    try:
        return json.dumps(a, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return str(a)


def summarise(name, args, fallback=""):
    """One-line argument summary for a call; `fallback` (the serve's own
    summary) is used when the input is not a JSON object."""
    a = parse_input(args)
    if a is None:
        return _clip(fallback or (args if isinstance(args, str) else ""), SUMMARY_MAX)
    fn = SUMMARISERS.get(name)
    text = (fn(a) if fn else "") or generic_summary(a) or fallback
    return _clip(text, SUMMARY_MAX)


def display_name(name):
    """`mcp__toolserver__fs_read_file` -> `toolserver:fs_read_file`."""
    name = name or "tool"
    if name.startswith("mcp__"):
        parts = name[5:].split("__", 1)
        if len(parts) == 2:
            return "%s:%s" % tuple(parts)
    return name


def is_agent(block):
    return block.kind == "tool" and block.name in AGENT_TOOLS


def status_mark(block):
    if block.ok is True:
        return "✓"
    if block.ok is False:
        return "✗"
    return "–" if block.meta.get("orphan") else "…"


def fmt_duration(seconds):
    if seconds is None or seconds < 0:
        return ""
    if seconds < 1:
        return "%dms" % round(seconds * 1000)
    if seconds < 60:
        return "%.1fs" % seconds
    return "%dm%02ds" % divmod(int(seconds), 60)


def head(block, expanded=False, children=0):
    """The collapsed one-liner for a tool card."""
    arrow = "▾ " if expanded else "▸ "
    summary = summarise(block.name, block.detail, block.text)
    text = arrow + "⚒ " + display_name(block.name) + (" · " + summary if summary else "")
    tail = status_mark(block)
    dur = fmt_duration(block.meta.get("dur"))
    if dur:
        tail += " " + dur
    if children:
        tail += " · %d call%s" % (children, "" if children == 1 else "s")
    return text + "  " + tail


def pretty_input(detail):
    a = parse_input(detail)
    if a is None:
        return detail or ""
    return json.dumps(a, indent=2, ensure_ascii=False, default=str)


# -- grouping / layout --------------------------------------------------------

def group_target(start):
    """Selection target for a group chip (block targets are >= 0, -1 = none)."""
    return -(start + 2)


def group_start(target):
    return -target - 2 if target <= -2 else None


def children_of(blocks):
    """{agent block index: [child indices]} from meta parent == agent tool_id."""
    ids = {b.meta.get("tool_id"): i for i, b in enumerate(blocks) if is_agent(b) and b.meta.get("tool_id")}
    out = {}
    for i, b in enumerate(blocks):
        p = b.meta.get("parent")
        if p and p in ids and ids[p] != i:
            out.setdefault(ids[p], []).append(i)
    return out


def top_level(blocks):
    nested = {i for kids in children_of(blocks).values() for i in kids}
    return [i for i in range(len(blocks)) if i not in nested]


def runs(blocks):
    """Maximal runs of consecutive top-level group-kind blocks -> [(start, [idx])]."""
    out, cur = [], []
    for i in top_level(blocks):
        b = blocks[i]
        if b.kind in GROUP_KINDS:
            cur.append(i)
            continue
        if b.kind == "assistant" and not b.text.strip():
            continue                       # an empty streaming reply does not split a run
        if cur:
            out.append((cur[0], cur))
            cur = []
    if cur:
        out.append((cur[0], cur))
    return out


def layout(blocks, groups_open, expanded, live=False):
    """Ordered render items: ("block", index, depth) and
    ("chip", start, hidden_indices, is_open). Pure; transcript.py draws it and
    state.py walks its targets for selection."""
    kids = children_of(blocks)
    run_of = {}
    for run in runs(blocks):
        for i in run[1]:
            run_of[i] = run
    last_top = top_level(blocks)
    tail = [i for i in last_top if not (blocks[i].kind == "assistant" and not blocks[i].text.strip())]
    tail_run = run_of.get(tail[-1]) if tail else None     # the run the live turn is still growing
    items = []

    def block_items(i, depth):
        items.append(("block", i, depth))
        if i in expanded and i in kids:
            for c in kids[i]:
                block_items(c, depth + 1)

    done = set()
    for i in last_top:
        if i in done:
            continue
        run = run_of.get(i)
        if not run or len(run[1]) < 2:          # a lone call is already one line
            block_items(i, 0)
            continue
        start, members = run
        done.update(members)
        is_open = start in groups_open
        is_live = live and run is tail_run
        if is_open:
            items.append(("chip", start, list(members), True))
            for j in members:
                block_items(j, 0)
            continue
        hidden = members[:-1] if is_live else members
        items.append(("chip", start, list(hidden), False))
        if is_live:
            block_items(members[-1], 0)
    return items


def targets(items):
    return [group_target(it[1]) if it[0] == "chip" else it[1] for it in items]


def chip_text(blocks, hidden, is_open):
    n = len(hidden)
    text = ("▾ " if is_open else "▸ ") + "⚙ %d call%s" % (n, "" if n == 1 else "s")
    if not is_open and hidden:
        last = blocks[hidden[-1]]
        if last.kind == "tool":
            label = "⚒ %s · %s" % (display_name(last.name), summarise(last.name, last.detail, last.text))
        else:
            label = "💭 " + (last.text or "")
        text += " · " + _clip(label, CHIP_LAST_MAX)
    errs = sum(1 for i in hidden if blocks[i].ok is False)
    if errs and not is_open:
        text += "  ✗%d" % errs
    return text


def match_result(blocks, tool_id="", parent=""):
    """Index of the open tool card a tool_result belongs to, or None.
    By id when known; otherwise the OLDEST open non-Agent card (Serve preserves
    result order, and an Agent's own result only lands after its
    subagent's calls finished), falling back to the oldest open Agent card."""
    if tool_id:
        for i, b in enumerate(blocks):
            if b.kind == "tool" and b.meta.get("tool_id") == tool_id:
                return i
    open_ = [i for i, b in enumerate(blocks)
             if b.kind == "tool" and b.ok is None and not b.meta.get("orphan")
             and not (tool_id and b.meta.get("tool_id"))]
    if parent:
        scoped = [i for i in open_ if blocks[i].meta.get("parent") == parent]
        open_ = scoped or open_
    plain = [i for i in open_ if not is_agent(blocks[i])]
    pool = plain or open_
    return pool[0] if pool else None
