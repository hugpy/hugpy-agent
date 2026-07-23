"""Workspace memory: one fact per file + index, no silent overwrites."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import json
import os
import tempfile
import unittest

from hugpy_agent.memory import Memory


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = Memory(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_index(self):
        self.assertEqual(self.mem.load_index(), "")

    def test_remember_creates_file_and_index(self):
        out = json.loads(self.mem.remember("The build needs make >= 4.3",
                                           title="build prereq"))
        self.assertEqual(out["title"], "build prereq")
        path = os.path.join(self.tmp.name, "memory", out["remembered"])
        with open(path) as fh:
            self.assertIn("make >= 4.3", fh.read())
        self.assertIn("build prereq", self.mem.load_index())

    def test_same_title_never_overwrites(self):
        a = json.loads(self.mem.remember("fact one", title="dup"))
        b = json.loads(self.mem.remember("fact two", title="dup"))
        self.assertNotEqual(a["remembered"], b["remembered"])
        index = self.mem.load_index()
        entries = [ln for ln in index.splitlines() if ln.startswith("- [dup]")]
        self.assertEqual(len(entries), 2)


if __name__ == "__main__":
    unittest.main()
