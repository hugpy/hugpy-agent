"""Operator comms + escalation gate (P2.3): the send payload shape, the
poll/click round-trip, the strictly-bounded timeout, every fail-closed path,
the _execute gate (Approve / Deny / timeout / approve-all cache), and the
ask_operator tool. All offline — the transport is a scripted stub."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import json
import os
import tempfile
import unittest

from hugpy_agent.adapter import Adapter
from hugpy_agent.comms import Comms, MAX_OPTIONS
from hugpy_agent.config import Config, load_config
from hugpy_agent.journal import Journal
from hugpy_agent.loop import AgentLoop, APPROVE, APPROVE_ALL_FMT, DENY_LABEL
from hugpy_agent.memory import Memory
from hugpy_agent.tools import RISK_WRITE, ToolSpec, build_registry

from helpers import FakeGateway, tc

SESSION = "https://central.test/api/discord/session/TESTTOKEN"


class FakeClock:
    """Deterministic monotonic + sleep pair: sleeping advances time, so the
    poll's deadline math runs instantly and exactly."""

    def __init__(self):
        self.t = 0.0
        self.slept = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


class StubTransport:
    """Scripted HTTP double mirroring the live central shapes (verified
    against discord_routes.py): send echoes the outbound message (with its
    ts watermark); each poll pops the next scripted messages page."""

    def __init__(self, polls=None, send_ts=100.0, mint=None):
        self.calls = []          # dicts: url/method/payload/headers
        self.polls = list(polls or [])
        self.send_response = {"ok": True,
                              "message": {"id": "m1", "direction": "out",
                                          "source": "session", "ts": send_ts}}
        self.mint_response = mint
        self.fail_sends = 0      # raise on the next N sends
        self.fail_polls = 0      # raise on the next N polls

    def __call__(self, url, method="GET", payload=None, timeout=30,
                 headers=None):
        self.calls.append({"url": url, "method": method, "payload": payload,
                           "headers": headers})
        if url.endswith("/discord/sessions"):
            return self.mint_response or {}
        if url.endswith("/send"):
            if self.fail_sends:
                self.fail_sends -= 1
                raise OSError("send boom")
            return self.send_response
        if "/messages?" in url:
            if self.fail_polls:
                self.fail_polls -= 1
                raise OSError("poll boom")
            return self.polls.pop(0) if self.polls else {"messages": []}
        raise AssertionError("unexpected url %s" % url)

    def sends(self):
        return [c for c in self.calls if c["url"].endswith("/send")]

    def poll_urls(self):
        return [c["url"] for c in self.calls if "/messages?" in c["url"]]


def _in(content, ts):
    return {"direction": "in", "source": "discord", "content": content,
            "ts": ts}


def make_comms(transport, clock=None, **kw):
    clock = clock or FakeClock()
    kw.setdefault("session_url", SESSION)
    return Comms(transport=transport, monotonic=clock.monotonic,
                 sleep=clock.sleep, **kw)


class AskTests(unittest.TestCase):
    def test_send_payload_shape_and_clicked_label(self):
        tr = StubTransport(polls=[{"messages": [_in("Approve", 101.0)]}])
        res = make_comms(tr).ask("deploy?", ["Approve", "Deny"])
        self.assertEqual(res, {"answered": True, "choice": "Approve",
                               "timed_out": False})
        (send,) = tr.sends()
        self.assertEqual(send["url"], SESSION + "/send")
        self.assertEqual(send["method"], "POST")
        self.assertEqual(send["payload"],
                         {"content": "deploy?", "options": ["Approve", "Deny"]})
        # the poll watermark is the echoed outbound's own ts (skew-proof):
        self.assertIn("/messages?since=100.0", tr.poll_urls()[0])

    def test_reply_matching_skips_echo_and_chatter_case_insensitive(self):
        """Only an inbound message matching an offered label counts: our own
        outbound echo and unrelated chatter are ignored; a case-different
        click still resolves to the CANONICAL label."""
        tr = StubTransport(polls=[
            {"messages": [
                {"direction": "out", "source": "session",
                 "content": "Deny", "ts": 100.5},          # our own echo
                _in("what's this about?", 101.0),           # chatter
            ]},
            {"messages": [_in("deny", 102.0)]},
        ])
        res = make_comms(tr).ask("q?", ["Approve", "Deny"])
        self.assertEqual(res["choice"], "Deny")             # canonical label
        self.assertTrue(res["answered"])

    def test_timeout_is_strictly_bounded(self):
        clock = FakeClock()
        tr = StubTransport()                                # never answers
        comms = make_comms(tr, clock=clock, timeout=10)
        res = comms.ask("q?", ["Approve", "Deny"])
        self.assertEqual(res, {"answered": False, "choice": None,
                               "timed_out": True})
        # 10s deadline / 3s interval => polls at t=0,3,6,9 plus one final
        # look exactly at the deadline (a click during the last sleep still
        # counts), then a strict stop.
        self.assertEqual(len(tr.poll_urls()), 5)
        self.assertLessEqual(clock.t, 10.0)                 # never over-waits
        # per-call override beats the configured timeout:
        tr2 = StubTransport()
        res2 = make_comms(tr2, timeout=600).ask("q?", ["A"], timeout=0)
        self.assertTrue(res2["timed_out"])
        self.assertEqual(len(tr2.poll_urls()), 1)           # one look, no wait

    def test_option_count_validated_before_any_network(self):
        tr = StubTransport()
        comms = make_comms(tr)
        for bad in ([], ["", "  "], ["x"] * (MAX_OPTIONS + 1)):
            res = comms.ask("q?", bad)
            self.assertFalse(res["answered"])
            self.assertFalse(res["timed_out"])
            self.assertIn("options", res["error"])
        self.assertEqual(tr.calls, [])                      # nothing sent

    def test_unconfigured_fails_closed_without_network(self):
        tr = StubTransport()
        res = make_comms(tr, session_url="").ask("q?", ["A"])
        self.assertEqual((res["answered"], res["choice"], res["timed_out"]),
                         (False, None, False))
        self.assertIn("no operator channel configured", res["error"])
        self.assertEqual(tr.calls, [])

    def test_send_failure_is_data(self):
        tr = StubTransport()
        tr.fail_sends = 1
        res = make_comms(tr).ask("q?", ["A"])
        self.assertFalse(res["answered"])
        self.assertFalse(res["timed_out"])                  # nothing to poll
        self.assertIn("send failed", res["error"])

    def test_transient_poll_error_tolerated_until_reply(self):
        tr = StubTransport(polls=[{"messages": [_in("A", 101.0)]}])
        tr.fail_polls = 2                                   # then recovers
        res = make_comms(tr, timeout=30).ask("q?", ["A", "B"])
        self.assertEqual(res["choice"], "A")

    def test_stop_callback_aborts_the_wait(self):
        tr = StubTransport()
        res = make_comms(tr, timeout=300).ask("q?", ["A"], stop=lambda: True)
        self.assertFalse(res["answered"])
        self.assertFalse(res["timed_out"])
        self.assertIn("interrupted", res["error"])

    def test_long_inputs_clipped_to_wire_limits(self):
        tr = StubTransport(polls=[{"messages": [_in("Y" * 80, 101.0)]}])
        res = make_comms(tr).ask("q" * 5000, ["Y" * 200])
        (send,) = tr.sends()
        self.assertLessEqual(len(send["payload"]["content"]), 1900)
        self.assertEqual(send["payload"]["options"], ["Y" * 80])
        self.assertEqual(res["choice"], "Y" * 80)


class MintTests(unittest.TestCase):
    def test_mint_then_send_uses_in_run_token(self):
        tr = StubTransport(polls=[{"messages": [_in("Go", 101.0)]}],
                           mint={"token": "MINTED",
                                 "endpoint": "/discord/session/MINTED"})
        comms = make_comms(tr, session_url="", mint=True,
                           base="https://central.test/api",
                           channel_id="42", api_key="sk-op")
        res = comms.ask("q?", ["Go", "No"])
        self.assertEqual(res["choice"], "Go")
        mint_call = tr.calls[0]
        self.assertEqual(mint_call["url"],
                         "https://central.test/api/discord/sessions")
        self.assertEqual(mint_call["payload"]["channel_id"], "42")
        # the mint route is operator-gated: the Bearer rides on THAT call...
        self.assertEqual(mint_call["headers"],
                         {"Authorization": "Bearer sk-op"})
        # ...and the minted token is used in-process only:
        self.assertEqual(
            tr.sends()[0]["url"],
            "https://central.test/api/discord/session/MINTED/send")
        # a second ask reuses the session — no second mint:
        tr.polls = [{"messages": [_in("Go", 102.0)]}]
        comms.ask("again?", ["Go"])
        mints = [c for c in tr.calls if c["url"].endswith("/discord/sessions")]
        self.assertEqual(len(mints), 1)

    def test_mint_without_channel_fails_closed(self):
        tr = StubTransport()
        comms = make_comms(tr, session_url="", mint=True,
                           base="https://central.test/api", channel_id="")
        res = comms.ask("q?", ["A"])
        self.assertFalse(res["answered"])
        self.assertIn("no operator channel configured", res["error"])
        self.assertEqual(tr.calls, [])

    def test_mint_failure_fails_closed(self):
        tr = StubTransport(mint={})                         # no token back
        comms = make_comms(tr, session_url="", mint=True,
                           base="https://central.test/api", channel_id="42")
        res = comms.ask("q?", ["A"])
        self.assertFalse(res["answered"])
        self.assertIn("no operator channel", res["error"])
        self.assertEqual(len(tr.calls), 1)                  # only the mint try


class ConfigWiringTests(unittest.TestCase):
    def test_defaults_are_unconfigured_and_300s(self):
        cfg = Config()
        self.assertEqual(cfg.discord_session, "")
        self.assertFalse(cfg.discord_mint)
        self.assertEqual(cfg.discord_channel, "")
        self.assertEqual(cfg.ask_timeout, 300)
        self.assertFalse(Comms.from_config(cfg).configured())

    def test_env_wires_session_and_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(environ={
                "HUGPY_WORKSPACE": tmp,
                "HUGPY_DISCORD_SESSION": SESSION,
                "HUGPY_ASK_TIMEOUT": "120",
            })
        self.assertEqual(cfg.discord_session, SESSION)
        self.assertEqual(cfg.ask_timeout, 120)
        comms = Comms.from_config(cfg)
        self.assertTrue(comms.configured())
        self.assertEqual(comms.timeout, 120)

    def test_env_wires_mint_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(environ={
                "HUGPY_WORKSPACE": tmp,
                "HUGPY_DISCORD_MINT": "1",
                "HUGPY_DISCORD_CHANNEL": "42",
            })
        self.assertTrue(cfg.discord_mint)
        self.assertEqual(cfg.discord_channel, "42")
        self.assertTrue(Comms.from_config(cfg).configured())


class StubComms:
    """Scripted operator: each ask() pops the next canned reply; records
    every question/options pair so tests can assert what was asked."""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.asks = []           # (question, options)

    @staticmethod
    def click(label):
        return {"answered": True, "choice": label, "timed_out": False}

    @staticmethod
    def timeout():
        return {"answered": False, "choice": None, "timed_out": True}

    def ask(self, question, options, timeout=None, stop=None):
        self.asks.append((question, list(options)))
        return self.replies.pop(0) if self.replies else self.timeout()


APPROVE_ALL_EFFECT = APPROVE_ALL_FMT % "effect"


class GateTests(unittest.TestCase):
    """The _execute escalation gate: Approve proceeds, Deny/timeout deny as
    data, approve-all caches per run, unconfigured fails closed — and the
    audit line always carries decision 'ask' with the outcome in error_bool
    (P2.2 behavior, unchanged)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
        self.audit_path = os.path.join(self.ws, "audit.jsonl")
        self.effect_runs = []

    def tearDown(self):
        self.tmp.cleanup()

    def make_loop(self, replies, comms):
        cfg = Config(workspace=self.ws, tools_mode="prompted", max_steps=10,
                     model="fake-model", policy_mode="ask",
                     audit_log=self.audit_path, ask_timeout=7)
        gw = FakeGateway(replies)
        journal = Journal(os.path.join(self.ws, ".hugpy_agent", "journal.db"))
        reg = build_registry(self.ws, gw, Memory(self.ws), comms=comms)
        reg.register(ToolSpec(
            name="effect", description="side-effecting test tool",
            parameters={"type": "object",
                        "properties": {"tag": {"type": "string"}},
                        "required": ["tag"]},
            handler=lambda tag: self.effect_runs.append(tag) or "ran:%s" % tag,
            risk_class=RISK_WRITE))
        return AgentLoop(cfg, gateway=gw, registry=reg, journal=journal,
                         adapter=Adapter("prompted"), memory=Memory(self.ws),
                         comms=comms), gw

    def audit_rows(self):
        with open(self.audit_path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_approve_proceeds_and_audits_ask(self):
        comms = StubComms([StubComms.click(APPROVE)])
        loop, gw = self.make_loop([tc("effect", tag="x"),
                                   tc("final_answer", answer="ok")], comms)
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.effect_runs, ["x"])
        self.assertIn("ran:x", json.dumps(gw.calls[1][0]))  # model saw result
        # the question names the tool, risk and args; buttons are the trio:
        question, options = comms.asks[0]
        self.assertIn("effect", question)
        self.assertIn("write", question)
        self.assertIn('"tag": "x"', question)               # arg preview
        self.assertEqual(options, [APPROVE, APPROVE_ALL_EFFECT, DENY_LABEL])
        (row,) = self.audit_rows()
        self.assertEqual((row["tool"], row["decision"], row["error_bool"]),
                         ("effect", "ask", False))

    def test_deny_click_denies_as_data(self):
        comms = StubComms([StubComms.click(DENY_LABEL)])
        loop, gw = self.make_loop([tc("effect", tag="x"),
                                   tc("final_answer", answer="blocked")], comms)
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")         # loop continued
        self.assertEqual(self.effect_runs, [])              # never executed
        sent = json.dumps(gw.calls[1][0])
        self.assertIn("policy denied", sent)
        self.assertIn("operator denied", sent)
        (row,) = self.audit_rows()
        self.assertEqual((row["decision"], row["error_bool"]), ("ask", True))

    def test_timeout_denies_as_data(self):
        comms = StubComms([StubComms.timeout()])
        loop, gw = self.make_loop([tc("effect", tag="x"),
                                   tc("final_answer", answer="blocked")], comms)
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.effect_runs, [])
        sent = json.dumps(gw.calls[1][0])
        self.assertIn("did not answer", sent)
        self.assertIn("7s", sent)                           # cfg.ask_timeout
        (row,) = self.audit_rows()
        self.assertEqual((row["decision"], row["error_bool"]), ("ask", True))

    def test_approve_all_caches_for_the_run(self):
        comms = StubComms([StubComms.click(APPROVE_ALL_EFFECT)])
        loop, _ = self.make_loop([tc("effect", tag="one"),
                                  tc("effect", tag="two"),
                                  tc("final_answer", answer="ok")], comms)
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.effect_runs, ["one", "two"])
        self.assertEqual(len(comms.asks), 1)                # asked exactly once
        rows = self.audit_rows()                            # both still audited
        self.assertEqual([r["decision"] for r in rows], ["ask", "ask"])
        self.assertEqual([r["error_bool"] for r in rows], [False, False])

    def test_plain_approve_is_single_shot(self):
        """A bare Approve opens the gate for THAT call only — the next call
        of the same tool asks again (approve-all is the explicit grant)."""
        comms = StubComms([StubComms.click(APPROVE),
                           StubComms.click(DENY_LABEL)])
        loop, _ = self.make_loop([tc("effect", tag="one"),
                                  tc("effect", tag="two"),
                                  tc("final_answer", answer="ok")], comms)
        loop.run("t")
        self.assertEqual(self.effect_runs, ["one"])         # second was denied
        self.assertEqual(len(comms.asks), 2)

    def test_unconfigured_comms_fails_closed(self):
        comms = Comms(session_url="")                       # the real thing
        loop, gw = self.make_loop([tc("effect", tag="x"),
                                   tc("final_answer", answer="blocked")], comms)
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.effect_runs, [])
        self.assertIn("no operator channel configured",
                      json.dumps(gw.calls[1][0]))


class AskOperatorToolTests(unittest.TestCase):
    """The model-facing tool: registered by build_registry (risk readonly, so
    the ask-mode policy auto-allows it) and returns the click as data."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_loop(self, replies, comms):
        cfg = Config(workspace=self.ws, tools_mode="prompted", max_steps=10,
                     model="fake-model", policy_mode="ask", audit_log="")
        gw = FakeGateway(replies)
        journal = Journal(os.path.join(self.ws, ".hugpy_agent", "journal.db"))
        reg = build_registry(self.ws, gw, Memory(self.ws), comms=comms)
        return AgentLoop(cfg, gateway=gw, registry=reg, journal=journal,
                         adapter=Adapter("prompted"), memory=Memory(self.ws),
                         comms=comms), gw

    def test_registered_as_readonly(self):
        reg = build_registry(self.ws, FakeGateway(), comms=None)
        spec = reg.get("ask_operator")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.risk_class, "readonly")
        self.assertEqual(spec.parameters["required"], ["question", "options"])

    def test_returns_the_clicked_label_as_data(self):
        comms = StubComms([StubComms.click("Blue")])
        loop, gw = self.make_loop([
            tc("ask_operator", question="Which theme?",
               options=["Blue", "Green"]),
            tc("final_answer", answer="Blue it is"),
        ], comms)
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(comms.asks, [("Which theme?", ["Blue", "Green"])])
        # unwrap the tool_response the model saw and check the exact data:
        wrapped = gw.calls[1][0][-1]["content"]
        inner = json.loads(wrapped.replace("<tool_response>", "")
                                  .replace("</tool_response>", "").strip())
        self.assertEqual(inner["name"], "ask_operator")
        self.assertEqual(json.loads(inner["result"]),
                         {"answered": True, "choice": "Blue"})

    def test_timeout_and_unconfigured_are_error_data_not_crashes(self):
        # timeout:
        comms = StubComms([StubComms.timeout()])
        loop, gw = self.make_loop([
            tc("ask_operator", question="q?", options=["A"]),
            tc("final_answer", answer="moving on"),
        ], comms)
        self.assertEqual(loop.run("t")["outcome"], "done")
        self.assertIn("did not answer", json.dumps(gw.calls[1][0]))
        # unconfigured (fail closed, still data):
        comms2 = Comms(session_url="")
        loop2, gw2 = self.make_loop([
            tc("ask_operator", question="q?", options=["A"]),
            tc("final_answer", answer="moving on"),
        ], comms2)
        self.assertEqual(loop2.run("t")["outcome"], "done")
        self.assertIn("no operator channel", json.dumps(gw2.calls[1][0]))


class ResumeTests(unittest.TestCase):
    """Escalation vs the resume protocol: an approve-all grant is journaled
    per-run, so a resumed run does not re-pester the operator."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
        self.effect_runs = []

    def tearDown(self):
        self.tmp.cleanup()

    def _loop(self, replies, comms, max_steps=10):
        cfg = Config(workspace=self.ws, tools_mode="prompted",
                     max_steps=max_steps, model="fake-model",
                     policy_mode="ask", audit_log="")
        gw = FakeGateway(replies)
        journal = Journal(os.path.join(self.ws, ".hugpy_agent", "journal.db"))
        reg = build_registry(self.ws, gw, Memory(self.ws), comms=comms)
        reg.register(ToolSpec(
            name="effect", description="side-effecting test tool",
            parameters={"type": "object",
                        "properties": {"tag": {"type": "string"}},
                        "required": ["tag"]},
            handler=lambda tag: self.effect_runs.append(tag) or "ran:%s" % tag,
            risk_class=RISK_WRITE))
        return AgentLoop(cfg, gateway=gw, registry=reg, journal=journal,
                         adapter=Adapter("prompted"), memory=Memory(self.ws),
                         comms=comms)

    def test_approve_all_grant_survives_resume(self):
        comms = StubComms([StubComms.click(APPROVE_ALL_EFFECT)])
        loop = self._loop([tc("effect", tag="one")], comms, max_steps=1)
        loop.run("t")                                  # stops at the step cap
        run_id = loop.journal.list_runs()[0]["run_id"]
        # resume with a FRESH loop (same journal db) and a comms stub that
        # would deny if consulted: the journaled grant must carry.
        comms2 = StubComms([StubComms.click(DENY_LABEL)])
        loop2 = self._loop([tc("effect", tag="two"),
                            tc("final_answer", answer="ok")], comms2)
        report = loop2.resume(run_id)
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(self.effect_runs, ["one", "two"])
        self.assertEqual(comms2.asks, [])              # never re-asked


if __name__ == "__main__":
    unittest.main()
