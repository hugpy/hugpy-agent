"""hugpy-agent resolves the toolserver via abstract_toolserver.discovery (the
advertised local endpoint) and calls through the shared client; the first-run
hook ensure_toolserver() uses an existing endpoint and never starts a second."""
import json
import os
import tempfile
import unittest
from unittest import mock

import _bootstrap  # noqa: F401

from abstract_toolserver import client as shared
from abstract_toolserver import discovery as D
from hugpy_agent import toolserver_client as tsc


class _Resp:
    def __init__(self, doc):
        self._b = json.dumps(doc).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class CentralTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = {"HOME": self.home}
        p = mock.patch.object(D, "SYSTEM_DIRS", ())
        p.start()
        self.addCleanup(p.stop)
        tf = os.path.join(self.home, "ts.env")
        with open(tf, "w") as fh:
            fh.write("TOOLSERVER_OPERATOR_TOKEN=advtok\n")
        D.write_advertisement(D.make_advertisement("http://127.0.0.1:7992", "127.0.0.1", 7992,
                                                   os.getpid(), token_file=tf), self.env)

    def test_client_is_the_shared_client_resolved_by_discovery(self):
        log = []

        def opener(req, timeout=None):
            log.append((req.full_url, json.loads(req.data.decode())))
            return _Resp({"result": {"ok": True}})

        c = tsc.ToolserverClient(environ=self.env, opener=opener)
        self.assertIsInstance(c, shared.ToolserverClient)
        self.assertEqual((c.url, c.url_source, c.token, c.token_source),
                         ("http://127.0.0.1:7992", "advertised", "advtok", "file"))
        self.assertEqual(c.call("todo_list", {"locus": "k"}), {"ok": True})
        self.assertEqual(log[-1], ("http://127.0.0.1:7992/ts/call",
                                   {"name": "todo_list", "arguments": {"locus": "k"}}))
        self.assertTrue(c.call("vm_stop")["denied"])       # hugpy-agent enforces the allowlist

    def test_ensure_uses_existing_never_starts(self):
        started = []
        res = tsc.ensure_toolserver(environ=self.env, starter=lambda **kw: started.append(kw))
        self.assertEqual((res["url"], res["source"]), ("http://127.0.0.1:7992", "advertised"))
        self.assertEqual(started, [])

    def test_ensure_opt_out_and_config(self):
        self.assertEqual(tsc.ensure_toolserver(environ={"HUGPY_AGENT_TOOLSERVER": "0"})["source"],
                         "disabled")
        cfg = mock.Mock(toolserver=True, toolserver_url="https://ext.example/ts/")
        self.assertEqual(tsc.ensure_toolserver(cfg, environ=self.env),
                         {"url": "https://ext.example/ts", "source": "configured"})


if __name__ == "__main__":
    unittest.main()
