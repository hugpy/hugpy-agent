"""tui.discovery: identify() by shape, probe order, --kind mismatch."""
import _bootstrap  # noqa: F401
import io
import json
import unittest

from helpers import fixture

from hugpy_agent.serve_client import ServeError
from hugpy_agent.tui import discovery


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def opener_for(table, log):
    def opener(url, timeout=None):
        log.append(url)
        for base, doc in table.items():
            if url.startswith(base):
                if doc is None:
                    raise OSError("refused")
                return Response(json.dumps(doc).encode())
        raise OSError("refused")
    return opener


class DiscoveryTests(unittest.TestCase):
    def test_identify_by_shape(self):
        self.assertEqual(discovery.identify({"service": "abstract-serve", "protocol_version": 1}),
                         "abstract-serve")
        # The old reply shape is retained for deployed servers not yet upgraded.
        self.assertEqual(discovery.identify(fixture("state_9124")), "abstract-serve")
        self.assertEqual(discovery.identify(fixture("state_9126")), "hugpy")
        self.assertIsNone(discovery.identify({"hello": 1}))
        self.assertIsNone(discovery.identify({"version": "x", "busy": {}}))
        self.assertIsNone(discovery.identify(None))

    def test_probe_order_and_env(self):
        log = []
        table = {"http://127.0.0.1:9124": None, "http://127.0.0.1:9125": None,
                 "http://127.0.0.1:9126": fixture("state_9126")}
        base, kind = discovery.discover(None, "auto", env={}, opener=opener_for(table, log))
        self.assertEqual((base, kind), ("http://127.0.0.1:9126", "hugpy"))
        self.assertEqual([u.split("/api")[0] for u in log],
                         ["http://127.0.0.1:9124", "http://127.0.0.1:9125", "http://127.0.0.1:9126"])
        log.clear()
        table["http://127.0.0.1:9124"] = fixture("state_9124")
        self.assertEqual(discovery.discover(None, "auto", env={}, opener=opener_for(table, log)),
                         ("http://127.0.0.1:9124", "abstract-serve"))
        self.assertEqual(len(log), 1)
        log.clear()
        table["http://10.0.0.5:9124"] = fixture("state_9124")
        env = {"HUGPY_AGENT_SERVE": "http://10.0.0.5:9124/"}
        self.assertEqual(discovery.discover(None, "auto", env=env, opener=opener_for(table, log))[0], "http://10.0.0.5:9124")
        self.assertTrue(log[0].startswith("http://10.0.0.5:9124/api/state"))
        self.assertEqual(discovery.discover("http://127.0.0.1:9126", "auto", env=env, opener=opener_for(table, log))[1], "hugpy")

    def test_kind_mismatch_and_nothing_found(self):
        table = {"http://127.0.0.1:9126": fixture("state_9126")}
        with self.assertRaises(ServeError) as ctx:
            discovery.discover("http://127.0.0.1:9126", "abstract-serve", env={}, opener=opener_for(table, []))
        self.assertIn("is hugpy, expected abstract-serve", str(ctx.exception))
        # Legacy command-line spelling remains accepted as an alias.
        self.assertEqual(discovery.discover("http://127.0.0.1:9124", "abstract-claude",
                                            env={}, opener=opener_for({"http://127.0.0.1:9124": fixture("state_9124")}, []))[1],
                         "abstract-serve")
        # Non-explicit candidates of the wrong kind are skipped, not fatal.
        table["http://127.0.0.1:9124"] = fixture("state_9124")
        self.assertEqual(discovery.discover(None, "hugpy", env={}, opener=opener_for(table, []))[0], "http://127.0.0.1:9126")
        with self.assertRaises(ServeError) as ctx:
            discovery.discover(None, "auto", env={}, opener=opener_for({}, []))
        self.assertIn("no serve found", str(ctx.exception))
        with self.assertRaises(ServeError):
            discovery.discover(None, "bogus", env={}, opener=opener_for({}, []))


if __name__ == "__main__":
    unittest.main()
