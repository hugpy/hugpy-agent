"""Serve-style tool calls in the terminal UI: summariser table, result
attachment, chips (consecutive calls), subagent nesting, toggle keys."""
import _bootstrap  # noqa: F401
import json
import unittest

from hugpy_agent.serve_client import Event, EventPage, Roster, Session
from hugpy_agent.serve_client import abstract_claude as ac
from hugpy_agent.tui import state as st
from hugpy_agent.tui import toolcalls as tc
from hugpy_agent.tui.views import transcript

CS = "cs-toolcalls000000000000000000000"


def ev(kind, seq, **kw):
    fields = dict(seq=seq, ts=100.0 + seq, session_id=CS, kind=kind)
    fields.update(kw)
    return Event(**fields)


def call(seq, name, args, tool_id="", parent="", **kw):
    meta = {}
    if tool_id:
        meta["tool_id"] = tool_id
    if parent:
        meta["parent"] = parent
    return ev("tool", seq, name=name, text="", detail=json.dumps(args), meta=meta, **kw)


def result(seq, text, ok=True, tool_id="", **kw):
    return ev("tool_result", seq, text=text, detail=text, ok=ok,
              meta={"tool_id": tool_id} if tool_id else {}, **kw)


def model():
    roster = Roster(roles=[Session(id=CS, role="keeper", label="Keeper", backend="claude", model="m")])
    return st.reduce(st.Model(kind="abstract-claude", base="http://x"), {"type": "roster", "roster": roster})


def feed(m, events, busy=False):
    page = EventPage(events, busy, None, str(events[-1].seq if events else 0), "console", False)
    return st.reduce(m, {"type": "events", "sid": CS, "page": page, "now": 1.0})


def texts(m, width=100):
    return [ln.text for ln in transcript.render_lines(m, width)]


class SummariserTests(unittest.TestCase):
    CASES = [
        ("Bash", {"command": "ls -la /srv\necho done", "description": "list"}, "ls -la /srv …"),
        ("Bash", {"command": "git status"}, "git status"),
        ("Read", {"file_path": "/etc/hosts", "limit": 5}, "/etc/hosts"),
        ("Edit", {"file_path": "/a/b.py", "old_string": "x", "new_string": "y"}, "/a/b.py"),
        ("Write", {"file_path": "/tmp/out.txt", "content": "hello"}, "/tmp/out.txt"),
        ("Grep", {"pattern": "def main", "path": "src/"}, "def main  (src/)"),
        ("Grep", {"pattern": "TODO"}, "TODO"),
        ("Glob", {"pattern": "**/*.py", "path": "/srv"}, "**/*.py  (/srv)"),
        ("WebFetch", {"url": "https://example.com/x", "prompt": "summarise"}, "https://example.com/x"),
        ("WebSearch", {"query": "textual pilot"}, "textual pilot"),
        ("Agent", {"description": "find callers", "prompt": "long…", "subagent_type": "Explore"},
         "Explore: find callers"),
        ("Task", {"description": "audit", "prompt": "go"}, "audit"),
        ("mcp__toolserver__fs_read_file", {"path": "/srv/x.json"}, "/srv/x.json"),
        ("mcp__toolserver__todo_list", {"status": "open"}, '{"status":"open"}'),
        ("SomethingNew", {"url": "http://u"}, "http://u"),
        ("SomethingNew", {}, ""),
    ]

    def test_table(self):
        for name, args, want in self.CASES:
            with self.subTest(name=name, args=args):
                self.assertEqual(tc.summarise(name, json.dumps(args)), want)
                self.assertEqual(tc.summarise(name, args), want)      # dict input too

    def test_fallbacks_and_truncation(self):
        self.assertEqual(tc.summarise("Read", "not json", fallback="Read /x"), "Read /x")
        self.assertEqual(tc.summarise("Bash", "{}", fallback="ls"), "ls")
        long = tc.summarise("Bash", {"command": "x" * 500})
        self.assertEqual(len(long), tc.SUMMARY_MAX)
        self.assertTrue(long.endswith("…"))
        self.assertEqual(tc.display_name("mcp__toolserver__fs_read_file"), "toolserver:fs_read_file")
        self.assertEqual(tc.display_name("Bash"), "Bash")
        self.assertEqual(tc.fmt_duration(0.25), "250ms")
        self.assertEqual(tc.fmt_duration(3.21), "3.2s")
        self.assertEqual(tc.fmt_duration(125), "2m05s")
        self.assertIs(set(tc.SUMMARISERS) >= {"Bash", "Read", "Edit", "Write", "Grep", "Glob", "WebFetch",
                                               "Agent", "Task"}, True)


class AttachTests(unittest.TestCase):
    def test_result_attaches_to_its_call_by_id(self):
        m = feed(model(), [ev("user", 1, text="go"),
                           call(2, "Read", {"file_path": "/a"}, "t1"),
                           call(3, "Read", {"file_path": "/b"}, "t2"),
                           result(4, "B body", tool_id="t2"),
                           result(5, "A body", tool_id="t1")])
        tools = [b for b in m.blocks if b.kind == "tool"]
        self.assertEqual(len(m.blocks), 3)                    # results never become messages
        self.assertEqual([t.output for t in tools], ["A body", "B body"])
        self.assertAlmostEqual(tools[0].meta["dur"], 3.0)
        self.assertAlmostEqual(tools[1].meta["dur"], 1.0)

    def test_parallel_calls_keep_order_without_ids(self):
        m = feed(model(), [call(1, "Grep", {"pattern": "a"}), call(2, "Glob", {"pattern": "*.py"}),
                           call(3, "Read", {"file_path": "/c"}),
                           result(4, "ra"), result(5, "rb", ok=False), result(6, "rc")])
        self.assertEqual([(b.name, b.output, b.ok) for b in m.blocks],
                         [("Grep", "ra", True), ("Glob", "rb", False), ("Read", "rc", True)])

    def test_unanswered_calls_are_orphaned_at_done(self):
        m = feed(model(), [call(1, "Bash", {"command": "sleep 9"}), ev("done", 2, meta={"interrupted": True}),
                           ev("user", 3, text="again"), call(4, "Read", {"file_path": "/x"}), result(5, "x")])
        self.assertTrue(m.blocks[0].meta.get("orphan"))
        self.assertIsNone(m.blocks[0].ok)
        self.assertEqual(m.blocks[-1].output, "x")            # not swallowed by the stale call
        self.assertIn("▸ ⚒ Bash · sleep 9  –", texts(m))

    def test_adapter_carries_ids(self):
        use = ac.normalize_event({"type": "tool", "name": "Read", "summary": "/a", "id": "toolu_1",
                                  "input": '{"file_path": "/a"}', "parent_tool_use_id": "toolu_0", "seq": 1})
        self.assertEqual(use.meta, {"tool_id": "toolu_1", "parent": "toolu_0"})
        res = ac.normalize_event({"type": "tool_result", "is_error": True, "text": "no", "tool_use_id": "toolu_1"})
        self.assertEqual((res.meta["tool_id"], res.ok), ("toolu_1", False))
        nat_use = ac.normalize_native_event({"kind": "tool_use", "tool": "Bash", "tool_id": "toolu_9",
                                             "excerpt": "ls", "ts": "2026-09-29T10:00:00.000Z"}, 1)
        nat_res = ac.normalize_native_event({"kind": "tool_result", "tool": "toolu_9", "excerpt": "ok",
                                             "ts": "2026-09-29T10:00:02.500Z"}, 2)
        self.assertEqual((nat_use.name, nat_use.meta["tool_id"]), ("Bash", "toolu_9"))
        self.assertEqual((nat_res.name, nat_res.meta["tool_id"]), ("", "toolu_9"))
        m = feed(model(), [nat_use, nat_res])
        self.assertEqual(len(m.blocks), 1)
        self.assertIn("▸ ⚒ Bash · ls  ✓ 2.5s", texts(m))


class RenderTests(unittest.TestCase):
    def turn(self, busy=False):
        return feed(model(), [ev("user", 1, text="look around"),
                              call(2, "Bash", {"command": "ls /srv"}, "a"),
                              call(3, "Read", {"file_path": "/srv/README.md"}, "b"),
                              result(4, "hugpy\nvm_mgr", tool_id="a"),
                              result(5, "no such file", ok=False, tool_id="b"),
                              ev("assistant", 6, text="Done.", meta={"final": True})], busy=busy)

    def test_collapsed_by_default_as_one_chip(self):
        m = self.turn()
        out = texts(m)
        chip = [t for t in out if "⚙" in t]
        self.assertEqual(chip, ["▸ ⚙ 2 calls · ⚒ Read · /srv/README.md  ✗1"])
        self.assertFalse(any("⚒ Bash" in t for t in out))
        self.assertEqual(out.index(chip[0]), 2)                   # inline, between user and reply
        self.assertEqual(out[3], "Done.")

    def test_toggle_chip_then_call(self):
        m = self.turn()
        target = tc.group_target(1)
        self.assertIn(target, st.visible_targets(m))
        m = st.reduce(m, {"type": "expand", "index": target})
        out = texts(m)
        self.assertEqual(out[2:5], ["▾ ⚙ 2 calls", "▸ ⚒ Bash · ls /srv  ✓ 2.0s", "▸ ⚒ Read · /srv/README.md  ✗ 2.0s"])
        lines = transcript.render_lines(m, 100)
        self.assertEqual(lines[4].attr, "TOOL_ERR")               # error styled distinctly
        self.assertEqual(lines[3].attr, "MUTED")
        m = st.reduce(m, {"type": "expand", "index": 2})
        out = texts(m)
        self.assertIn("▾ ⚒ Read · /srv/README.md  ✗ 2.0s", out)
        self.assertIn("  ┌ input", out)
        self.assertIn('  │   "file_path": "/srv/README.md"', out)
        self.assertIn("  ┌ error", out)
        self.assertIn("  │ no such file", out)
        m = st.reduce(m, {"type": "expand", "index": 2})
        self.assertNotIn("  ┌ error", texts(m))
        m = st.reduce(m, {"type": "expand", "index": target})
        self.assertEqual(texts(m)[2], "▸ ⚙ 2 calls · ⚒ Read · /srv/README.md  ✗1")

    def test_live_turn_keeps_latest_call_visible(self):
        m = feed(model(), [ev("user", 1, text="go"), call(2, "Bash", {"command": "ls"}),
                           result(3, "x"), call(4, "Grep", {"pattern": "foo"})], busy=True)
        out = texts(m)
        self.assertEqual(out[2:4], ["▸ ⚙ 1 call · ⚒ Bash · ls", "▸ ⚒ Grep · foo  …"])
        self.assertEqual(transcript.render_lines(m, 100)[3].attr, "TOOL")   # running
        m = feed(m, [result(5, "hit"), ev("done", 6, text="ok")])
        m = st.replace(m, busy=False)
        self.assertEqual(texts(m)[2], "▸ ⚙ 2 calls · ⚒ Grep · foo")

    def test_toggle_all_and_navigation(self):
        m = self.turn()
        m = st.reduce(m, {"type": "expand_all"})
        out = texts(m)
        self.assertIn("▾ ⚙ 2 calls", out)
        self.assertIn("▾ ⚒ Bash · ls /srv  ✓ 2.0s", out)
        self.assertIn("  │ vm_mgr", out)
        m = st.reduce(m, {"type": "expand_all"})
        self.assertEqual(m.lane().expanded, set())
        self.assertEqual(m.lane().groups_open, set())
        m = st.reduce(m, {"type": "focus", "which": "transcript"})
        self.assertEqual(st.visible_targets(m), [0, tc.group_target(1), 3])
        m = st.reduce(m, {"type": "move", "delta": -1})
        self.assertEqual(m.selected, tc.group_target(1))
        m = st.reduce(m, {"type": "expand"})                       # Enter on the chip
        self.assertEqual(m.lane().groups_open, {1})
        m = st.reduce(m, {"type": "move", "delta": 1})
        self.assertEqual(m.selected, 1)                            # into the opened chip
        m = st.reduce(m, {"type": "expand", "index": 3, "select": True})   # click on the reply: no-op
        self.assertEqual((m.selected, m.lane().expanded), (3, set()))

    def test_single_call_is_one_line_no_chip(self):
        m = feed(model(), [ev("user", 1, text="x"), call(2, "WebFetch", {"url": "https://a.b"}), result(3, "page"),
                           ev("assistant", 4, text="ok", meta={"final": True})])
        self.assertEqual(texts(m)[2], "▸ ⚒ WebFetch · https://a.b  ✓ 1.0s")

    def test_subagent_calls_nest_under_agent(self):
        m = feed(model(), [ev("user", 1, text="x"),
                           call(2, "Agent", {"description": "scan repo", "subagent_type": "Explore"}, "ag"),
                           call(3, "Grep", {"pattern": "main"}, "g1", parent="ag"),
                           result(4, "3 hits", tool_id="g1"),
                           call(5, "Read", {"file_path": "/m.py"}, "r1", parent="ag"),
                           result(6, "body", tool_id="r1"),
                           result(7, "report", tool_id="ag"),
                           ev("assistant", 8, text="ok", meta={"final": True})])
        out = texts(m)
        self.assertEqual(out[2], "▸ ⚒ Agent · Explore: scan repo  ✓ 5.0s · 2 calls")
        self.assertFalse(any("Grep" in t for t in out))            # nested calls collapsed with the agent
        m = st.reduce(m, {"type": "expand", "index": 1})
        out = texts(m)
        self.assertIn("  ▸ ⚒ Grep · main  ✓ 1.0s", out)
        self.assertIn("  ▸ ⚒ Read · /m.py  ✓ 1.0s", out)
        self.assertEqual(st.visible_targets(st.reduce(m, {"type": "focus", "which": "transcript"})), [0, 1, 2, 3, 4])
        m = st.reduce(m, {"type": "expand", "index": 2})
        self.assertIn("    ┌ result", texts(m))

    def test_result_without_id_skips_open_agent(self):
        m = feed(model(), [call(1, "Agent", {"description": "d"}), call(2, "Bash", {"command": "ls"}),
                           result(3, "files"), result(4, "agent report")])
        self.assertEqual([b.output for b in m.blocks], ["agent report", "files"])


if __name__ == "__main__":
    unittest.main()
