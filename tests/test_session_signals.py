"""Session signals (identity + lease + transitions) against a FAKE central.

No live calls: a loopback ThreadingHTTPServer plays central — /api/v1/models,
/api/v1/chat/completions (plain JSON or SSE), /api/llm/sessions/<sid>/lease,
/api/llm/jobs/<id>/cancel — and records every request it saw.
"""
import _bootstrap  # noqa: F401
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hugpy_agent import session_signals as ss
from hugpy_agent.gateway import Gateway


class FakeCentral:
    def __init__(self, lease_status=200, stream_chunks=None, chat_delay=0.0):
        self.seen = []            # (method, path, headers dict, body)
        self.lease_status = lease_status
        self.stream_chunks = stream_chunks
        self.chat_delay = chat_delay
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    return json.loads(raw) if raw else None
                except ValueError:
                    return raw

            def _send(self, code, doc):
                data = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                fake.seen.append(("GET", self.path, dict(self.headers), None))
                if self.path.endswith("/v1/models"):
                    return self._send(200, {"data": [{"id": "m"}]})
                self._send(404, {"error": "nope"})

            def do_POST(self):
                body = self._body()
                fake.seen.append(("POST", self.path, dict(self.headers), body))
                if "/llm/sessions/" in self.path:
                    return self._send(fake.lease_status, {"ok": fake.lease_status == 200})
                if "/llm/jobs/" in self.path and self.path.endswith("/cancel"):
                    return self._send(200, {"cancelled": True})
                if self.path.endswith("/chat/completions"):
                    if fake.chat_delay:
                        time.sleep(fake.chat_delay)
                    if fake.stream_chunks is None:
                        return self._send(200, {"id": "chatcmpl-1", "choices": [
                            {"message": {"content": "hello"}}]})
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for c in fake.stream_chunks:
                        self.wfile.write(("data: " + json.dumps(
                            {"id": "c", "choices": [{"delta": {"content": c}}]}) + "\n\n").encode())
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                self._send(404, {"error": "nope"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = "http://127.0.0.1:%d/api" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def posts(self, frag):
        return [s for s in self.seen if s[0] == "POST" and frag in s[1]]


class Base(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ.pop(ss.ENV_DISABLE, None)
        os.environ[ss.ENV_SESSION] = "ha-test-session"
        self.sig = ss.SessionSignals(interval=0.05, register_atexit=False)
        ss.reset_for_tests(self.sig)

    def tearDown(self):
        self.sig.closed = True
        ss.reset_for_tests(None)
        os.environ.clear()
        os.environ.update(self._env)

    def wait_for(self, pred, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.02)
        return pred()


class GatewayIdentityTests(Base):
    def test_every_chat_carries_central_header_names(self):
        fc = FakeCentral()
        try:
            res = Gateway(fc.base, model="m").chat([{"role": "user", "content": "hi"}],
                                                   stream=False, retries=0)
            self.assertTrue(res.ok, res.error)
            chat = fc.posts("/chat/completions")[0][2]
            lower = {k.lower(): v for k, v in chat.items()}
            self.assertEqual(lower["x-hugpy-client-session"], "ha-test-session")
            self.assertTrue(lower["x-hugpy-client-turn"].startswith("turn-"))
            self.assertTrue(lower["x-hugpy-client-request"].startswith("req-"))
            self.assertEqual(lower["x-hugpy-client-pid"], str(os.getpid()))
            self.assertIn("x-hugpy-client-process", lower)
            self.assertEqual(lower["x-hugpy-client"], "hugpy-agent")
        finally:
            fc.close()

    def test_implicit_turn_leases_then_sends_turn_done_idle(self):
        fc = FakeCentral(chat_delay=0.3)
        try:
            Gateway(fc.base, model="m").chat([{"role": "user", "content": "hi"}],
                                             stream=False, retries=0)
            self.wait_for(lambda: any(p[3]["event"] == "turn_done" for p in fc.posts("/lease")))
            leases = [p[3] for p in fc.posts("/llm/sessions/ha-test-session/lease")]
            waiting = [b for b in leases if b["event"] == "lease" and b["state"] == "waiting"]
            self.assertTrue(waiting, leases)
            self.assertEqual(len(waiting[0]["request_ids"]), 1)
            self.assertEqual(waiting[0]["ttl"], ss.LEASE_TTL)
            done = [b for b in leases if b["event"] == "turn_done"]
            self.assertEqual(len(done), 1)
            self.assertEqual(done[0]["state"], "idle")
            self.assertEqual(done[0]["request_ids"], [])
            chat_hdr = {k.lower(): v for k, v in fc.posts("/chat/completions")[0][2].items()}
            self.assertEqual(done[0]["turn_id"], chat_hdr["x-hugpy-client-turn"])
        finally:
            fc.close()

    def test_lease_renews_while_long_call_in_flight(self):
        """A long prefill: the lease keeps renewing, so central never
        abandons a call whose socket merely looks idle."""
        fc = FakeCentral(chat_delay=0.4)
        try:
            Gateway(fc.base, model="m").chat([{"role": "user", "content": "x"}],
                                             stream=False, retries=0)
            renewals = [p for p in fc.posts("/lease") if p[3]["event"] == "lease"]
            self.assertGreaterEqual(len(renewals), 3)
        finally:
            fc.close()

    def test_explicit_turn_shares_one_turn_id_across_calls(self):
        fc = FakeCentral()
        try:
            gw = Gateway(fc.base, model="m")
            with self.sig.turn(task="run:r1") as tid:
                gw.chat([{"role": "user", "content": "a"}], stream=False, retries=0)
                gw.chat([{"role": "user", "content": "b"}], stream=False, retries=0)
            turns = {({k.lower(): v for k, v in p[2].items()})["x-hugpy-client-turn"]
                     for p in fc.posts("/chat/completions")}
            reqs = {({k.lower(): v for k, v in p[2].items()})["x-hugpy-client-request"]
                    for p in fc.posts("/chat/completions")}
            self.assertEqual(turns, {tid})
            self.assertEqual(len(reqs), 2)
            self.wait_for(lambda: any(p[3]["event"] == "turn_done" for p in fc.posts("/lease")))
            done = [p[3] for p in fc.posts("/lease") if p[3]["event"] == "turn_done"]
            self.assertEqual([d["turn_id"] for d in done], [tid])
            hdr = {k.lower(): v for k, v in fc.posts("/chat/completions")[0][2].items()}
            self.assertEqual(hdr["x-hugpy-client-task"], "run:r1")
        finally:
            fc.close()

    def test_user_abort_cancels_the_request_on_central(self):
        fc = FakeCentral(stream_chunks=["a", "b", "c"])
        try:
            def boom(_piece):
                raise KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                Gateway(fc.base, model="m").chat([{"role": "user", "content": "x"}],
                                                 on_delta=boom, retries=0)
            hdr = {k.lower(): v for k, v in fc.posts("/chat/completions")[0][2].items()}
            cancels = fc.posts("/llm/jobs/")
            self.assertEqual(len(cancels), 1)
            self.assertEqual(cancels[0][1], "/api/llm/jobs/%s/cancel" % hdr["x-hugpy-client-request"])
            self.assertEqual(cancels[0][3]["session_id"], "ha-test-session")
        finally:
            fc.close()

    def test_old_central_404_degrades_silently(self):
        fc = FakeCentral(lease_status=404, chat_delay=0.3)
        try:
            gw = Gateway(fc.base, model="m")
            for _ in range(2):
                res = gw.chat([{"role": "user", "content": "x"}], stream=False, retries=0)
                self.assertTrue(res.ok, res.error)
            # one probe, then the endpoint is marked unsupported for the process
            time.sleep(0.2)
            self.assertEqual(len(fc.posts("/lease")), 1)
        finally:
            fc.close()

    def test_disabled_sends_no_identity_and_no_lease(self):
        os.environ[ss.ENV_DISABLE] = "0"
        fc = FakeCentral()
        try:
            Gateway(fc.base, model="m").chat([{"role": "user", "content": "x"}],
                                             stream=False, retries=0)
            hdr = {k.lower() for k in fc.posts("/chat/completions")[0][2]}
            self.assertFalse(any(h.startswith("x-hugpy-client") for h in hdr))
            self.assertEqual(fc.posts("/lease"), [])
        finally:
            fc.close()

    def test_third_party_provider_never_gets_hugpy_identity(self):
        from hugpy_agent.service.providers import ProviderGateway
        fc = FakeCentral()
        try:
            gw = ProviderGateway({"base_url": fc.base + "/v1", "model": "m",
                                  "protocol": "openai-chat", "context_length": 8192})
            gw.chat([{"role": "user", "content": "x"}], stream=False)
            hdr = {k.lower() for k in fc.posts("/chat/completions")[0][2]}
            self.assertFalse(any(h.startswith("x-hugpy") for h in hdr))
            self.assertEqual(fc.posts("/lease"), [])
        finally:
            fc.close()

    def test_close_sends_session_closed(self):
        fc = FakeCentral()
        try:
            Gateway(fc.base, model="m").chat([{"role": "user", "content": "x"}],
                                             stream=False, retries=0)
            self.wait_for(lambda: any(p[3]["event"] == "turn_done" for p in fc.posts("/lease")))
            self.sig.close()
            last = fc.posts("/lease")[-1][3]
            self.assertEqual((last["event"], last["state"]), ("session_closed", "closed"))
        finally:
            fc.close()


class PureTests(unittest.TestCase):
    def test_api_prefix(self):
        self.assertEqual(ss.api_prefix("https://h/api/v1/chat/completions"), "https://h/api")
        self.assertEqual(ss.api_prefix("http://127.0.0.1:1/v1/chat/completions"),
                         "http://127.0.0.1:1")
        self.assertEqual(ss.api_prefix("https://h/api"), "https://h/api")
        self.assertEqual(ss.api_prefix("https://h/api/v1"), "https://h/api")

    def test_merge_header_lines_keeps_others_replaces_ours(self):
        out = ss.merge_header_lines("X-Hugpy-Model: q\nX-Hugpy-Client-Session: old",
                                    {"X-Hugpy-Client-Session": "new"})
        self.assertEqual(out.splitlines(), ["X-Hugpy-Model: q", "X-Hugpy-Client-Session: new"])

    def test_harness_env_inherits_the_launch_session(self):
        add = ss.harness_env("opencode", {ss.ENV_SESSION: "ha-parent"}, pid=42)
        self.assertEqual(add[ss.ENV_SESSION], "ha-parent")
        self.assertEqual(add[ss.ENV_PID], "42")
        self.assertEqual(add[ss.ENV_NAME], "opencode")
        fresh = ss.harness_env("hermes", {})
        self.assertTrue(fresh[ss.ENV_SESSION].startswith("ha-"))

    def test_header_refs(self):
        oc = ss.harness_header_refs("opencode")
        self.assertEqual(oc["X-Hugpy-Client-Session"], "{env:HUGPY_CLIENT_SESSION}")
        self.assertEqual(ss.harness_header_refs("qwen")["X-Hugpy-Client-Pid"], "$HUGPY_CLIENT_PID")


class HarnessLeaseTests(unittest.TestCase):
    def test_leases_while_alive_then_closes(self):
        posts = []
        lives = iter([True, True, True, False])
        lease = ss.HarnessLease("http://c/api", "k", "ha-s", "hugpy-agent:opencode",
                                alive=lambda: next(lives), interval=0, pid=7,
                                harness="opencode",
                                poster=lambda url, body, key: posts.append((url, body)) or "ok",
                                sleep=lambda s: None)
        lease.run()
        self.assertEqual([b["event"] for _, b in posts], ["lease"] * 3 + ["session_closed"])
        self.assertEqual(posts[0][0], "http://c/api/llm/sessions/ha-s/lease")
        self.assertEqual(posts[0][1]["state"], "active")
        self.assertEqual(posts[-1][1]["state"], "closed")

    def test_old_central_stops_without_close(self):
        posts = []
        lease = ss.HarnessLease("http://c/api", "", "ha-s", "p", alive=lambda: True,
                                interval=0,
                                poster=lambda url, body, key: posts.append(body) or "unsupported",
                                sleep=lambda s: None)
        lease.run()
        self.assertEqual(len(posts), 1)

    def test_sidecar_skipped_when_owner_alive_spawned_otherwise(self):
        calls = []
        env = {ss.ENV_SESSION: "ha-s", ss.ENV_LEASE_OWNER: str(os.getpid())}
        self.assertFalse(ss.start_lease_sidecar("https://h/api", "k", "opencode", env,
                                                popen=lambda *a, **k: calls.append(a)))
        env = {ss.ENV_SESSION: "ha-s"}
        self.assertTrue(ss.start_lease_sidecar("https://h/api", "k", "opencode", env,
                                               pid=4242, popen=lambda a, **k: calls.append((a, k))))
        argv, kw = calls[0]
        self.assertIn("keep", argv)
        self.assertEqual(argv[argv.index("--pid") + 1], "4242")
        self.assertEqual(argv[argv.index("--session") + 1], "ha-s")
        self.assertEqual(kw["env"]["HUGPY_LEASE_KEY"], "k")
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(env[ss.ENV_LEASE_OWNER], "4242")

    def test_in_process_harness_lease_closes_on_exit(self):
        fc = FakeCentral()
        try:
            env = {ss.ENV_SESSION: "ha-tui"}
            with ss.harness_lease(fc.base, "", env, "hermes"):
                time.sleep(0.2)
            self.assertEqual(env[ss.ENV_LEASE_OWNER], str(os.getpid()))
            events = [p[3]["event"] for p in fc.posts("/llm/sessions/ha-tui/lease")]
            self.assertEqual(events[0], "lease")
            self.assertEqual(events[-1], "session_closed")
        finally:
            fc.close()


class HarnessConfigTests(unittest.TestCase):
    def test_opencode_provider_headers_are_env_references(self):
        from hugpy_agent import console
        cfg = console.build_config("https://h/api", "HUGPY_API_KEY", {"m": {}}, "m")
        hdrs = cfg["provider"]["hugpy"]["options"]["headers"]
        self.assertEqual(hdrs["X-Hugpy-Client-Session"], "{env:HUGPY_CLIENT_SESSION}")
        self.assertNotIn("ha-", json.dumps(cfg))   # no literal session in the shared file

    def test_hermes_profile_carries_literal_identity(self):
        from hugpy_agent import frontends
        env = {"OPENAI_BASE_URL": "https://h/api/v1", ss.ENV_SESSION: "ha-h",
               ss.ENV_NAME: "hermes", ss.ENV_PROCESS: "hugpy-agent:hermes"}
        with tempfile.TemporaryDirectory() as root:
            prof = frontends.configure({"id": "hermes"}, env, "m", root=root)
            with open(os.path.join(prof, "config.yaml")) as fh:
                cfg = json.load(fh)
        eh = cfg["providers"]["hugpy"]["extra_headers"]
        self.assertEqual(eh["X-Hugpy-Client-Session"], "ha-h")
        self.assertEqual(eh["X-Hugpy-Client"], "hermes")

    def test_qwen_system_settings_carry_identity_refs_and_keep_fast_model(self):
        from hugpy_agent import console
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "qwen-system-settings.json")
            env = {}
            env[console.QWEN_SYSTEM_SETTINGS_ENV] = console.write_qwen_fast_model_settings(
                "tiny", path=p, environ=env)
            out = console.write_qwen_identity_settings(path=p, environ=env)
            with open(out) as fh:
                s = json.load(fh)
        self.assertEqual(s["fastModel"], "tiny")
        h = s["model"]["generationConfig"]["customHeaders"]
        self.assertEqual(h["X-Hugpy-Client-Session"], "$HUGPY_CLIENT_SESSION")

    def test_env_hook_stamps_every_prepared_harness(self):
        from hugpy_agent import frontends, harness_settings
        self.assertIn(ss._identity_env_hook, harness_settings.ENV_HOOKS)
        env = {ss.ENV_SESSION: "ha-launch"}
        harness_settings.harness_env("aider", env)
        self.assertEqual(env[ss.ENV_SESSION], "ha-launch")
        self.assertEqual(env[ss.ENV_NAME], "aider")
        self.assertEqual(env[ss.ENV_PROCESS], "hugpy-agent:aider")

if __name__ == "__main__":
    unittest.main()
