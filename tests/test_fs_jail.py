"""Workspace jail: every escape route must be a structured error, never a read."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import json
import os
import tempfile
import unittest

from hugpy_agent.tools import Registry
from hugpy_agent.tools.fs import _confine, specs


class JailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
        self.outside = tempfile.TemporaryDirectory()
        with open(os.path.join(self.outside.name, "secret.txt"), "w") as fh:
            fh.write("SECRET")
        with open(os.path.join(self.ws, "inside.txt"), "w") as fh:
            fh.write("inside")
        self.reg = Registry()
        for s in specs(self.ws):
            self.reg.register(s)

    def tearDown(self):
        self.tmp.cleanup()
        self.outside.cleanup()

    def _run(self, name, **args):
        return self.reg.execute(self.reg.get(name), args)

    def test_read_inside_ok(self):
        self.assertEqual(self._run("fs_read", path="inside.txt"), "inside")

    def test_dotdot_escape_rejected(self):
        out = self._run("fs_read", path="../" * 8 + "etc/hostname")
        self.assertIn("escapes the workspace", out)

    def test_absolute_path_outside_rejected(self):
        out = self._run("fs_read",
                        path=os.path.join(self.outside.name, "secret.txt"))
        self.assertIn("escapes the workspace", out)
        self.assertNotIn("SECRET", out)

    def test_symlink_escape_rejected(self):
        link = os.path.join(self.ws, "sneaky")
        os.symlink(self.outside.name, link)
        out = self._run("fs_read", path="sneaky/secret.txt")
        self.assertIn("escapes the workspace", out)

    def test_write_escape_rejected(self):
        out = self._run("fs_write", path="../evil.txt", content="x")
        self.assertIn("escapes the workspace", out)
        self.assertFalse(os.path.exists(
            os.path.join(os.path.dirname(self.ws), "evil.txt")))

    def test_write_through_symlinked_dir_rejected(self):
        link = os.path.join(self.ws, "outdir")
        os.symlink(self.outside.name, link)
        out = self._run("fs_write", path="outdir/evil.txt", content="x")
        self.assertIn("escapes the workspace", out)
        self.assertFalse(os.path.exists(
            os.path.join(self.outside.name, "evil.txt")))

    def test_glob_does_not_leak_through_symlink(self):
        os.symlink(self.outside.name, os.path.join(self.ws, "sneaky"))
        out = json.loads(self._run("fs_glob", pattern="sneaky/*"))
        self.assertEqual(out["matches"], [])

    def test_write_creates_nested_dirs_inside(self):
        out = json.loads(self._run("fs_write", path="a/b/c.txt", content="hi"))
        self.assertEqual(out["written"], "a/b/c.txt")
        with open(os.path.join(self.ws, "a/b/c.txt")) as fh:
            self.assertEqual(fh.read(), "hi")

    def test_confine_workspace_root_itself_ok(self):
        self.assertEqual(_confine(self.ws, "."), self.ws)

    def test_sibling_prefix_dir_rejected(self):
        """/ws-evil must not pass a naive startswith('/ws') check."""
        sibling = self.ws + "-evil"
        os.makedirs(sibling, exist_ok=True)
        try:
            with self.assertRaises(ValueError):
                _confine(self.ws, sibling)
        finally:
            os.rmdir(sibling)


if __name__ == "__main__":
    unittest.main()
