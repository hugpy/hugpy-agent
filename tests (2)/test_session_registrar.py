"""hugpy-agent registers the box's harness sessions with central (runtime/session_registrar.py)."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import unittest

from hugpy_agent.runtime.session_registrar import SessionRegistrar, advertised_url


class FakeService:
    def __init__(self, rows):
        self.rows = rows

    def sessions(self):
        return [dict(r) for r in self.rows]


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.fail_beats_with = None
        self.raise_once = False

    def __call__(self, url, method, payload, timeout=30, headers=None):
        if self.raise_once:
            self.raise_once = False
            raise OSError("central unreachable")
        self.calls.append((url, method, payload, headers))
        route = url.rsplit("/", 1)[1]
        if route == "beat" and self.fail_beats_with:
            return self.fail_beats_with, {"error": "x"}
        return 200, {"ok": True}


def session(sid, **over):
    row = {"id": sid, "name": "Desk " + sid, "route": "session:" + sid, "model": "small", "status": "idle",
           "settings": {"base": "http://worker:1234", "mode": "wrapper"}}
    row.update(over)
    return row


class RegistrarTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeService([session("a"), session("b", model="dense-chat")])
        self.fc = FakeTransport()
        self.reg = SessionRegistrar(self.service, "http://central:7020/", api_key="hp_key",
                                    serve_url="http://192.168.1.50:9126", serve_token="tok", locus="box-1",
                                    transport=self.fc)

    def test_first_sync_registers_then_beats(self):
        report = self.reg.sync_once()
        self.assertEqual(report["registered"], ["a", "b"]) and self.assertEqual(report["errors"], [])
        url, method, payload, headers = self.fc.calls[0]
        self.assertEqual(url, "http://central:7020/api/sessions/register")
        self.assertEqual(headers, {"Authorization": "Bearer hp_key"})
        self.assertEqual(payload["serve_url"], "http://192.168.1.50:9126")
        self.assertEqual(payload["serve_token"], "tok") and self.assertFalse(payload["local_only"])
        self.assertEqual((payload["locus"], payload["model"], payload["base"], payload["mode"]),
                         ("box-1", "small", "http://worker:1234", "wrapper"))
        import socket
        self.assertEqual(payload["host"], socket.gethostname())
        self.fc.calls.clear()
        report = self.reg.sync_once()
        self.assertEqual(report["beat"], ["a", "b"]) and self.assertEqual(report["registered"], [])
        self.assertTrue(all(c[0].endswith("/beat") for c in self.fc.calls))
        self.assertEqual(self.fc.calls[1][2]["model"], "dense-chat")

    def test_central_that_forgot_a_session_gets_it_re_registered(self):
        self.reg.sync_once()
        self.fc.calls.clear()
        self.fc.fail_beats_with = 404
        report = self.reg.sync_once()
        self.assertEqual(report["registered"], ["a", "b"]) and self.assertEqual(report["errors"], [])
        self.assertEqual([c[0].rsplit("/", 1)[1] for c in self.fc.calls], ["beat", "register", "beat", "register"])

    def test_other_beat_failures_are_errors_as_data(self):
        self.reg.sync_once()
        self.fc.fail_beats_with = 503
        report = self.reg.sync_once()
        self.assertEqual([e["status"] for e in report["errors"]], [503, 503])
        self.assertEqual(self.reg.failures, 1)

    def test_vanished_sessions_are_withdrawn(self):
        self.reg.sync_once()
        self.service.rows.pop(0)
        self.fc.calls.clear()
        report = self.reg.sync_once()
        self.assertEqual(report["removed"], ["a"]) and self.assertEqual(report["beat"], ["b"])
        self.assertIn(("http://central:7020/api/sessions/remove", "POST", {"session_id": "a"},
                       {"Authorization": "Bearer hp_key"}), self.fc.calls)
        self.reg.stop()
        self.assertEqual(self.fc.calls[-1][2], {"session_id": "b"})       # stop() withdraws the rest
        self.assertEqual(self.reg.known, set())

    def test_transport_exception_never_raises(self):
        self.fc.raise_once = True
        report = self.reg.sync_once()
        self.assertEqual(report["errors"][0]["status"], 0)
        self.assertEqual(report["registered"], ["b"])                      # the second call went through

    def test_loopback_or_tokenless_serve_is_local_only(self):
        for url, token in (("http://127.0.0.1:9126", "tok"), ("http://192.168.1.50:9126", "")):
            reg = SessionRegistrar(self.service, "http://central:7020", serve_url=url, serve_token=token,
                                   transport=self.fc)
            self.assertTrue(reg.local_only, (url, token))
            reg.sync_once()
            self.assertTrue(self.fc.calls[-1][2]["local_only"])
        self.assertFalse(self.reg.local_only)
        self.assertIsNone(SessionRegistrar(self.service, "http://c", transport=self.fc)._headers())

    def test_advertised_url(self):
        self.assertEqual(advertised_url("192.168.1.50", 9126, environ={}), "http://192.168.1.50:9126")
        self.assertEqual(advertised_url("0.0.0.0", 1, environ={"HUGPY_SERVE_URL": "https://box.example/"}),
                         "https://box.example")
        wildcard = advertised_url("0.0.0.0", 9126, environ={})
        self.assertTrue(wildcard.startswith("http://") and wildcard.endswith(":9126"))
        self.assertNotIn("0.0.0.0", wildcard)


if __name__ == "__main__":
    unittest.main()
