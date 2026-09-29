"""Views against a fake Screen (test_fleet_tui.Screen extended to record cells)."""
import _bootstrap  # noqa: F401
import curses
import unittest

from hugpy_agent.serve_client import Approval, QueueItem, QueueView, Roster, Session, Usage
from hugpy_agent.tui import layout
from hugpy_agent.tui import state as st
from hugpy_agent.tui.views import composer as cv
from hugpy_agent.tui.views import modals, panels, text, theme, transcript

CS = "cs-cd165265d096407f92fcc4d54f2c2bd9"


class Screen:
    """Records (y, x, text, attr); raises on any write outside the pane so
    layout bugs fail loudly instead of being swallowed by curses."""

    def __init__(self, keys=(), size=(24, 80)):
        self.keys = iter(keys)
        self.size = size
        self.cells = []
        self.cursor = None

    def getmaxyx(self):
        return self.size

    def getch(self):
        return next(self.keys, 27)

    def addnstr(self, y, x, value, limit, attr=0):
        h, w = self.size
        assert 0 <= y < h and 0 <= x < w, (y, x, value)
        shown = text.cut(value, limit)
        assert x + text.width(shown) <= w, (y, x, shown)
        self.cells.append((y, x, shown, attr))

    def erase(self): pass
    def clear(self): pass
    def refresh(self): pass
    def timeout(self, value): pass
    def keypad(self, value): pass
    def nodelay(self, value): pass
    def move(self, y, x): self.cursor = (y, x)

    @property
    def text(self):
        return [c[2] for c in self.cells]

    def has(self, needle):
        return any(needle in t for t in self.text)


def model(blocks=(), **kw):
    roster = Roster(roles=[Session(id=CS, role="keeper", label="Keeper", backend="hugpy",
                                   model="hugpy-fleet:Qwen3-Coder-Next-GGUF", paused=True),
                           Session(id="547bcd67-4925-4397-8bf7-26d3eb9b1721", role="chat", label="Chat",
                                   backend="claude", model="claude-opus-4-8", pending_model="claude-opus-5"),
                           Session(id="cs-02c944d4527643b69a439d1bd9f41ee0", role="worker", label="Worker",
                                   backend="hugpy", model="hugpy-fleet:Qwen3-32B"),
                           Session(id="local", role="local", label="Local", backend="b")],
                    sessions=[Session(id="cs-%032x" % i, backend="gpt", label="s%d" % i, updated=i) for i in range(12)])
    m = st.Model(kind="abstract-claude", base="http://127.0.0.1:9124", roster=roster, active_sid=CS, net="live")
    lane = st.Lane(blocks=list(blocks), loaded=True)
    m = st.replace(m, lanes={CS: lane}, **kw)
    return m


T = theme.plain()


class LayoutTests(unittest.TestCase):
    def test_layout_bounds(self):
        r = layout.compute(24, 80, 1)
        self.assertEqual(r.header, (0, 0, 1, 80))
        self.assertEqual(r.sidebar, (1, 0, 21, 20))
        self.assertEqual(r.transcript, (1, 21, 21, 59))
        self.assertEqual(r.composer, (22, 0, 1, 80))
        self.assertEqual(r.status, (23, 0, 1, 80))
        self.assertFalse(r.narrow or r.wide)
        r = layout.compute(24, 80, 9)
        self.assertEqual(r.composer.h, 6)              # capped
        self.assertEqual(r.transcript.h, 16)
        r = layout.compute(15, 40, 5)
        self.assertTrue(r.narrow)
        self.assertEqual(r.sidebar.h, 0)
        self.assertEqual(r.composer.h, 3)
        self.assertEqual(r.transcript, (1, 0, 10, 40))
        r = layout.compute(40, 130, 1)
        self.assertTrue(r.wide)
        self.assertEqual(r.sidebar.w, 28)
        for h, w in ((24, 80), (15, 40), (40, 130), (6, 20), (63, 235)):
            r = layout.compute(h, w, 2)
            for rect in (r.header, r.sidebar, r.transcript, r.composer, r.status):
                self.assertLessEqual(rect.y + rect.h, h)
                self.assertLessEqual(rect.x + rect.w, w)


class PanelTests(unittest.TestCase):
    def test_splash(self):
        scr = Screen()
        panels.draw_splash(scr, "http://127.0.0.1:9124", T)
        self.assertTrue(scr.has("HUGPY AGENT"))
        self.assertTrue(scr.has("Your models. Your workers. One fleet."))
        self.assertTrue(scr.has("Connecting to http://127.0.0.1:9124"))
        self.assertTrue(any("█" in t for t in scr.text))
        panels.draw_splash(Screen(size=(15, 40)), "http://x", T)   # never past bounds

    def test_sidebar_and_header_on_every_size(self):
        m = model()
        for size in ((24, 80), (15, 40), (40, 130)):
            scr = Screen(size=size)
            r = layout.compute(*size)
            panels.draw_header(scr, m, r.header, T, folded=r.narrow)
            panels.draw_sidebar(scr, m, r.sidebar, T)
            if r.narrow:
                self.assertTrue(scr.has("HUGPY · ac 9124  [K] C W L"))
                self.assertFalse(scr.has("ROLES"))
            else:
                self.assertTrue(scr.has("HUGPY AGENT · abstract-claude 127.0.0.1:9124"))
                self.assertTrue(scr.has("● keeper"))
                self.assertTrue(scr.has("○ chat"))
                self.assertTrue(scr.has("SESSIONS"))
                self.assertEqual(sum(1 for t in scr.text if t.startswith("○ cs-")), 8)
            if r.wide:
                self.assertTrue(scr.has("● keeper  Qwen3-Coder-Next"))   # model per row (cut at 28 cols)

    def test_status_fields_and_truncation_order(self):
        m = model(busy=True, busy_since=88.0, queue=QueueView(auto=False, items=[QueueItem("a", "x")] * 3),
                  usage=Usage(12300, 0, 240000, 0, "gpt"), tools="3 ✓")
        fields = panels.status_fields(m, now=100.0)
        self.assertEqual(fields, ["[ac 9124]", "keeper cs-cd16…", "hugpy/Qwen3-Coder-Next-GGUF", "BUSY 12s",
                                  "q:3 auto off", "tok 12.3k/240.0k", "tools: 3 ✓", "● live"])
        full, tail = panels.status_text(m, 140, now=100.0)
        self.assertIn("● live", full)
        self.assertEqual(tail, "? help")
        short, _ = panels.status_text(m, 40, now=100.0)
        self.assertTrue(short.startswith("[ac 9124] · keeper"))
        self.assertNotIn("tok", short)                     # dropped from the right first
        held = model(held=(True, "paused"), net="degraded", net_retry_at=104.0)
        self.assertIn("HELD", panels.status_fields(held, now=100.0))
        self.assertIn("◌ retrying 4s", panels.status_fields(held, now=100.0))
        chat = st.replace(model(), active_sid="547bcd67-4925-4397-8bf7-26d3eb9b1721")
        self.assertIn("claude/claude-opus-4-8 → claude-opus-5 staged", panels.status_fields(chat, now=0))
        claude_cs = model()
        claude_cs.roster.roles[0].backend = "claude"
        self.assertIn("tok n/a", panels.status_fields(claude_cs, now=0))
        scr = Screen()
        drawn = panels.draw_status(scr, st.replace(m, notice="compiling…"), layout.compute(24, 80).status, T, now=100.0)
        self.assertTrue(scr.has("? help"))
        self.assertTrue(scr.has("compiling…"))
        self.assertTrue(drawn.startswith("[ac 9124]"))


def blocks():
    return [st.Block("user", "hello there"),
            st.Block("tool", "Read /tmp/x", name="Read", detail='{"file_path": "/tmp/x"}',
                     output="\n".join("line %d" % i for i in range(60)), ok=True),
            st.Block("thinking", "first thought\nsecond thought"),
            st.Block("system", "claude-opus-4-8 · 3 tools", detail="Bash\nRead\nEdit"),
            st.Block("tool", "ls", name="Bash", detail="{}", output="boom", ok=False),
            st.Block("approval", "Run command", request_id="r1"),
            st.Block("assistant", "日本語のテキスト " * 12, streaming=True)]


class TranscriptTests(unittest.TestCase):
    def test_tool_card_collapsed(self):
        m = model(blocks())
        lines = transcript.render_lines(m, 59)
        texts = [ln.text for ln in lines]
        self.assertIn("⚒ Read · Read /tmp/x  [✓]", texts)
        self.assertIn("⚒ Bash · ls  [✗]", texts)
        self.assertIn("💭 first thought…", texts)
        self.assertIn("· claude-opus-4-8 · 3 tools", texts)
        self.assertIn("? Run command → pending", texts)
        self.assertEqual(lines[0].text, "▶ hello there")
        self.assertEqual(lines[0].attr, "USER")
        self.assertTrue(any(ln.text.endswith("▍") for ln in lines))
        self.assertTrue(all(text.width(ln.text) <= 59 for ln in lines))

    def test_tool_card_expanded(self):
        m = model(blocks())
        m = st.reduce(m, {"type": "expand", "index": 1})
        m = st.reduce(m, {"type": "expand", "index": 2})
        m = st.reduce(m, {"type": "expand", "index": 3})
        texts = [ln.text for ln in transcript.render_lines(m, 59)]
        self.assertIn("  ┌ input", texts)
        self.assertIn('  │ {"file_path": "/tmp/x"}', texts)
        self.assertIn("  ┌ output", texts)
        self.assertIn("  │ line 39", texts)
        self.assertNotIn("  │ line 40", texts)                  # capped at 40
        self.assertIn("  │ … 20 more lines", texts)
        self.assertTrue(any("second thought" in t for t in texts))
        self.assertTrue(any("Edit" in t for t in texts))
        wide = [ln.text for ln in transcript.render_lines(model(blocks()), 100, wide=True)]
        self.assertIn("  line 0", wide)                        # first output line when wide

    def test_wrap_wide_glyphs(self):
        rows = text.wrap("日本語のテキスト " * 3, 10)
        self.assertTrue(all(text.width(r) <= 10 for r in rows))
        self.assertEqual(text.wrap("abcdefghij" * 2, 8)[0], "abcdefgh")
        self.assertEqual(text.clean("a\x1b[2Jb\tc"), "ab  c")
        self.assertEqual(text.wrap("", 10), [""])

    def test_draw_transcript_scroll_and_selection(self):
        m = model(blocks())
        m = st.reduce(m, {"type": "expand", "index": 1})
        scr = Screen()
        rect = layout.compute(24, 80).transcript
        first, total = transcript.draw_transcript(scr, m, rect, T)
        self.assertGreater(total, rect.h)
        self.assertEqual(first, total - rect.h)                  # follows the tail
        self.assertTrue(all(c[0] < rect.bottom for c in scr.cells))
        m = st.reduce(m, {"type": "scroll", "to": "top"})
        scr = Screen()
        first, _ = transcript.draw_transcript(scr, m, rect, T)
        self.assertEqual(first, 0)
        self.assertTrue(scr.has("▶ hello there"))
        self.assertTrue(any(t.startswith("↓ ") for t in scr.text))
        m = st.reduce(m, {"type": "focus", "which": "transcript"})
        m = st.reduce(m, {"type": "move", "delta": -6})          # select block 0
        scr = Screen()
        transcript.draw_transcript(scr, m, rect, T)
        sel = [c for c in scr.cells if c[2] == "▶ hello there"]
        self.assertTrue(sel and sel[0][3] & curses.A_REVERSE)
        held = st.replace(m, held=(True, "Held after a serve restart"), net="down")
        scr = Screen()
        transcript.draw_transcript(scr, held, rect, T)
        self.assertTrue(scr.has("HELD: Held after a serve restart — r retry · Ctrl-K queue"))
        self.assertTrue(scr.has("serve unreachable"))
        transcript.draw_transcript(Screen(size=(15, 40)), held, layout.compute(15, 40).transcript, T)


class ComposerTests(unittest.TestCase):
    def test_composer_multiline(self):
        c = cv.Composer()
        for ch in "hello\\":
            c.insert(ch)
        self.assertIsNone(c.submit())                     # backslash+Enter -> newline
        self.assertEqual(c.buffer, "hello\n")
        c.insert("world 日本")
        rows, (cr, cc) = c.lines(6)
        self.assertEqual(rows, ["hello", "world ", "日本"])
        self.assertEqual((cr, cc), (2, 4))
        c.home()
        self.assertEqual(c.cursor, 6)
        c.end()
        self.assertEqual(c.cursor, len(c.buffer))
        self.assertEqual(c.submit(), "hello\nworld 日本")
        self.assertEqual(c.buffer, "")
        self.assertIsNone(c.submit())                     # empty Enter = no-op
        self.assertTrue(c.recall(-1))
        self.assertEqual(c.buffer, "hello\nworld 日本")
        self.assertFalse(c.recall(-1))
        self.assertTrue(c.recall(1))
        self.assertEqual(c.buffer, "")
        c.insert("abc")
        c.left(); c.backspace()
        self.assertEqual(c.buffer, "ac")
        c.delete()
        self.assertEqual(c.buffer, "a")
        scr = Screen()
        c.clear(); c.insert("x" * 100); c.newline(); c.insert("y")
        y, x = cv.draw_composer(scr, c, layout.compute(24, 80, 3).composer, T)
        self.assertTrue(scr.has("> " + "x" * 78))
        self.assertEqual((y, x), (20 + 2, 3))            # composer rows 20-22, cursor on row 3 of 3
        for _ in range(60):
            c.history.append("h")
        self.assertEqual(len(c.history), 50)


class ModalTests(unittest.TestCase):
    def test_picker_escape(self):
        self.assertEqual(modals.choose(Screen([curses.KEY_DOWN, 10]), "Pick", ["a", "b"], T), 1)
        self.assertIsNone(modals.choose(Screen([27]), "Pick", ["a", "b"], T))
        drained = []
        self.assertIsNone(modals.choose(Screen([-1, ord("q")]), "Pick", ["a"], T, drain=lambda: drained.append(1)))
        self.assertEqual(drained, [1])

    def test_approval_keys(self):
        gpt = Approval("r1", "approval", "Run command", ["accept", "acceptForSession", "decline", "cancel"],
                       {"command": "rm -rf build", "cwd": "/srv"}, ts=1000.0)
        self.assertEqual(modals.approval_modal(Screen([ord("y")]), gpt, T), "accept")
        self.assertEqual(modals.approval_modal(Screen([ord("a")]), gpt, T), "acceptForSession")
        self.assertEqual(modals.approval_modal(Screen([ord("n")]), gpt, T), "decline")
        self.assertEqual(modals.approval_modal(Screen([ord("c")]), gpt, T), "cancel")
        self.assertEqual(modals.approval_modal(Screen([curses.KEY_DOWN, curses.KEY_DOWN, 10]), gpt, T), "decline")
        self.assertIsNone(modals.approval_modal(Screen([27]), gpt, T))
        scr = Screen([27])
        modals.approval_modal(scr, gpt, T)
        self.assertTrue(scr.has("? Run command"))
        self.assertTrue(scr.has("command: rm -rf build"))
        q = Approval("q1", "question", "Run shell `uptime`?", ["Approve", "Approve all shell this run", "Deny"], ts=1000.0)
        self.assertEqual(modals.approval_modal(Screen([ord("3")]), q, T, now=1100.0), "Deny")
        self.assertEqual(modals.approval_modal(Screen([ord("y"), 10]), q, T, now=1100.0), "Approve")  # y ignored
        scr = Screen([27])
        modals.approval_modal(scr, q, T, now=1100.0)
        self.assertTrue(scr.has("expires in 200s"))
        self.assertTrue(scr.has("1 Approve"))

    def test_queue_and_text_modals(self):
        q = QueueView(auto=True, paused=True, items=[QueueItem("m1", "first"), QueueItem("m2", "second")])
        self.assertEqual(modals.queue_modal(Screen([curses.KEY_DOWN, ord("d")]), q, T), ("remove", q.items[1]))
        self.assertEqual(modals.queue_modal(Screen([ord("a")]), q, T), ("auto", False))
        self.assertEqual(modals.queue_modal(Screen([ord("r")]), q, T), ("retry", None))
        self.assertIsNone(modals.queue_modal(Screen([27]), QueueView(items=[]), T))
        scr = Screen([ord("q")])
        modals.text_modal(scr, "TOOLS", ["fs_read", "sys_run"], T)
        self.assertTrue(scr.has("fs_read"))
        self.assertEqual(modals.line_edit(Screen([ord("!"), 10]), "edit", "hi", T), "hi!")


if __name__ == "__main__":
    unittest.main()
