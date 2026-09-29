"""The serve daemon (P2.7): queue-file source (atomic consume), the
discord-inbox convention (`task:` parsing, watermark, no history replay,
completion reply), fail-closed idle with heartbeat, graceful stop, and
errors-as-data all the way around the loop. Offline: the loop is a stub, the
transport is scripted, sleep/monotonic are a fake clock."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import os
import tempfile
import unittest

from hugpy_agent.comms import MAX_CONTENT_CHARS
from hugpy_agent.config import Config, load_config
from hugpy_agent.serve import (Daemon, DiscordInboxSource, QueueFileSource,
                               TASK_PREFIX, default_queue_path, format_reply,
                               make_source)

SESSION = "https://central.test/api/discord/session/TESTTOKEN"


class FakeClock:
    """monotonic + sleep pair: sleeping advances time (see test_comms)."""

    def __init__(self):
        self.t = 0.0
        self.slept = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


class StubLoop:
    """Stands in for AgentLoop: records the task, returns a canned report
    (or raises). No gateway, no registry, no network."""

    def __init__(self, log, report=None, exc=None):
        self.log = log
        self.report = report or {"run_id": "r1", "outcome": "done",
                                 "steps": 3, "tool_calls": 2,
                                 "est_tokens": 10, "answer": "did it"}
        self.exc = exc

    def run(self, task):
        self.log.append(task)
        if self.exc:
            raise self.exc
        return dict(self.report, task=task)


class Events:
    def __init__(self):
        self.rows = []

    def __call__(self, kind, *a):
        self.rows.append((kind,) + a)

    def kinds(self):
        return [r[0] for r in self.rows]

    def of(self, kind):
        return [r for r in self.rows if r[0] == kind]


class StubTransport:
    """Scripted session endpoint: each poll pops the next messages page;
    sends are recorded."""

    def __init__(self, polls=None):
        self.polls = list(polls or [])
        self.calls = []
        self.fail_polls = 0

    def __call__(self, url, method="GET", payload=None, timeout=30,
                 headers=None):
        self.calls.append({"url": url, "method": method, "payload": payload})
        if "/messages?" in url:
            if self.fail_polls:
                self.fail_polls -= 1
                raise OSError("poll boom")
            return self.polls.pop(0) if self.polls else {"messages": []}
        if url.endswith("/send"):
            return {"ok": True, "message": {"id": "m", "ts": 999.0}}
        raise AssertionError("unexpected url %s" % url)

    def sends(self):
        return [c for c in self.calls if c["url"].endswith("/send")]


def _in(content, ts):
    return {"direction": "in", "source": "discord", "content": content,
            "ts": ts}


def make_daemon(cfg=None, source=None, note="test", log=None, events=None,
                clock=None, loop=None, factory=None):
    cfg = cfg or Config(workspace=".", poll_interval=10)
    clock = clock or FakeClock()
    events = events if events is not None else Events()
    log = log if log is not None else []
    factory = factory or (lambda: loop or StubLoop(log))
    d = Daemon(cfg, source=source, source_note=note, loop_factory=factory,
               on_event=events, sleep=clock.sleep, monotonic=clock.monotonic)
    return d, log, events, clock


class QueueSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "tasks.queue")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.read()

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(QueueFileSource(self.path).poll(), [])

    def test_consumes_one_task_atomically_preserving_the_rest(self):
        self.write("# queued work\nfirst task\n\nsecond task\n")
        src = QueueFileSource(self.path)
        self.assertEqual(src.poll(), ["first task"])
        # the comment, blank line and remaining task keep their places:
        self.assertEqual(self.read(), "# queued work\n\nsecond task\n")
        self.assertEqual(src.poll(), ["second task"])
        self.assertEqual(self.read(), "# queued work\n\n")
        self.assertEqual(src.poll(), [])
        self.assertFalse(os.path.exists(self.path + ".tmp"))  # replaced away

    def test_daemon_runs_queued_tasks_one_per_cycle(self):
        self.write("alpha\nbeta\n")
        d, log, events, clock = make_daemon(
            source=QueueFileSource(self.path))
        summary = d.run(max_cycles=3)
        self.assertEqual(log, ["alpha", "beta"])
        self.assertEqual(summary["tasks_run"], 2)
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(self.read(), "")
        # events narrate the work:
        self.assertEqual(len(events.of("task_start")), 2)
        self.assertEqual(len(events.of("task_done")), 2)
        done = events.of("task_done")[0][1]
        self.assertEqual(done["outcome"], "done")

    def test_default_queue_path_under_workspace(self):
        self.assertEqual(
            default_queue_path(self.tmp.name),
            os.path.join(os.path.realpath(self.tmp.name), ".hugpy_agent",
                         "tasks.queue"))


class DiscordInboxTests(unittest.TestCase):
    def test_first_poll_only_sets_the_watermark(self):
        """History must never replay: a `task:` message from before startup
        is watermarked past, not executed."""
        tr = StubTransport(polls=[
            {"messages": [_in("task: old dangerous thing", 50.0)]},
            {"messages": []},
        ])
        src = DiscordInboxSource(SESSION, transport=tr)
        self.assertEqual(src.poll(), [])            # baseline, no tasks
        self.assertTrue(src.primed)
        self.assertEqual(src.since, 50.0)
        self.assertEqual(src.poll(), [])            # and it never comes back
        self.assertIn("since=50.0", tr.calls[-1]["url"])

    def test_parses_task_prefix_case_insensitively_skips_chatter(self):
        tr = StubTransport(polls=[
            {"messages": []},                       # baseline
            {"messages": [
                {"direction": "out", "source": "session",
                 "content": "task: our own echo", "ts": 101.0},
                _in("how's it going?", 102.0),      # chatter
                _in("Task:  build the report ", 103.0),
                _in("task:", 104.0),                # empty task: ignored
            ]},
        ])
        src = DiscordInboxSource(SESSION, transport=tr)
        src.poll()
        self.assertEqual(src.poll(), ["build the report"])
        self.assertEqual(src.since, 104.0)          # advanced past everything

    def test_report_sends_the_formatted_reply(self):
        tr = StubTransport()
        src = DiscordInboxSource(SESSION, transport=tr)
        src.report("t", {"run_id": "abc123", "outcome": "done", "steps": 4,
                         "answer": "wrote report.md"})
        (send,) = tr.sends()
        self.assertEqual(send["url"], SESSION + "/send")
        self.assertEqual(send["method"], "POST")
        content = send["payload"]["content"]
        self.assertIn("run abc123", content)
        self.assertIn("outcome=done", content)
        self.assertIn("steps=4", content)
        self.assertIn("wrote report.md", content)

    def test_end_to_end_task_runs_once_and_replies(self):
        tr = StubTransport(polls=[
            {"messages": []},                       # cycle 1: baseline
            {"messages": [_in("task: do the thing", 101.0)]},
            {"messages": []},                       # cycle 3: nothing new
        ])
        d, log, events, clock = make_daemon(
            source=DiscordInboxSource(SESSION, transport=tr))
        summary = d.run(max_cycles=3)
        self.assertEqual(log, ["do the thing"])     # exactly once
        self.assertEqual(summary["tasks_run"], 1)
        (send,) = tr.sends()
        self.assertIn("outcome=done", send["payload"]["content"])

    def test_poll_error_is_serve_error_and_daemon_continues(self):
        tr = StubTransport(polls=[
            {"messages": []},
            {"messages": [_in("task: after the blip", 101.0)]},
        ])
        tr.fail_polls = 1                           # first poll blows up
        d, log, events, clock = make_daemon(
            source=DiscordInboxSource(SESSION, transport=tr))
        d.run(max_cycles=3)
        self.assertEqual(len(events.of("serve_error")), 1)
        self.assertEqual(log, ["after the blip"])   # recovered next cycles

    def test_reply_failure_does_not_fail_the_task(self):
        class Src(DiscordInboxSource):
            def report(self, task, report):
                raise OSError("send boom")
        tr = StubTransport(polls=[{"messages": []},
                                  {"messages": [_in("task: t1", 101.0)]}])
        d, log, events, clock = make_daemon(source=Src(SESSION, transport=tr))
        summary = d.run(max_cycles=2)
        self.assertEqual(log, ["t1"])
        self.assertEqual(summary["tasks_run"], 1)
        self.assertTrue(any("report failed" in r[1]
                            for r in events.of("serve_error")))


class FormatReplyTests(unittest.TestCase):
    def test_error_outcome_carries_the_error(self):
        text = format_reply({"run_id": "r", "outcome": "aborted",
                             "steps": 1, "error": "model unreachable"})
        self.assertIn("outcome=aborted", text)
        self.assertIn("model unreachable", text)

    def test_clipped_to_wire_limit(self):
        text = format_reply({"run_id": "r", "outcome": "done", "steps": 1,
                             "answer": "x" * 5000})
        self.assertLessEqual(len(text), MAX_CONTENT_CHARS)


class IdleTests(unittest.TestCase):
    def test_no_source_idles_with_heartbeat_never_crashes(self):
        booms = []
        d, log, events, clock = make_daemon(
            source=None, note="no task source configured; idling",
            factory=lambda: booms.append("built") or None)
        summary = d.run(max_cycles=30)              # bounded by the arg
        self.assertEqual(summary["cycles"], 30)
        self.assertEqual(summary["tasks_run"], 0)
        self.assertEqual(booms, [])                 # loop never constructed
        # 30 cycles * 10s sleep = 290s slept => heartbeats at 0s and then
        # every >=60s — periodic, not every cycle, not never:
        beats = events.of("heartbeat")
        self.assertGreaterEqual(len(beats), 4)
        self.assertLess(len(beats), 30)
        self.assertIn("idling", beats[0][1])

    def test_interval_clamped_to_at_least_one_second(self):
        cfg = Config(workspace=".", poll_interval=0)
        d, log, events, clock = make_daemon(cfg=cfg, source=None, note="idle")
        d.run(max_cycles=3)
        self.assertTrue(all(s >= 1 for s in clock.slept))

    def test_crashing_loop_is_data_and_the_daemon_survives(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        qpath = os.path.join(tmp.name, "q")
        with open(qpath, "w") as fh:
            fh.write("t1\nt2\n")
        log = []
        d, _, events, clock = make_daemon(
            source=QueueFileSource(qpath),
            factory=lambda: StubLoop(log, exc=RuntimeError("kaboom")))
        summary = d.run(max_cycles=3)
        self.assertEqual(log, ["t1", "t2"])         # kept going after t1 blew
        self.assertEqual(summary["errors"], 2)
        dones = events.of("task_done")
        self.assertEqual([r[1]["outcome"] for r in dones],
                         ["aborted", "aborted"])
        self.assertIn("kaboom", dones[0][1]["error"])


class StopTests(unittest.TestCase):
    def test_stop_finishes_the_current_task_then_exits(self):
        """SIGTERM semantics: the flag flips mid-task (the CLI handler does
        this); the in-flight task completes and is reported, queued siblings
        from the same poll are NOT started, run() returns."""
        tr = StubTransport(polls=[
            {"messages": []},
            {"messages": [_in("task: one", 101.0), _in("task: two", 102.0)]},
        ])
        log = []
        d = None

        def factory():
            def run(task):
                log.append(task)
                d.stop_requested = True             # signal lands mid-task
                return {"run_id": "r", "outcome": "done", "steps": 1,
                        "answer": "ok"}
            stub = StubLoop(log)
            stub.run = run
            return stub

        events = Events()
        clock = FakeClock()
        d = Daemon(Config(workspace="."),
                   source=DiscordInboxSource(SESSION, transport=tr),
                   source_note="test", loop_factory=factory,
                   on_event=events, sleep=clock.sleep,
                   monotonic=clock.monotonic)
        summary = d.run()                           # unbounded: stop ends it
        self.assertEqual(log, ["one"])              # `two` never started
        self.assertTrue(summary["stopped"])
        self.assertEqual(len(tr.sends()), 1)        # the finished task replied

    def test_stop_before_run_never_polls(self):
        tr = StubTransport()
        d, log, events, clock = make_daemon(
            source=DiscordInboxSource(SESSION, transport=tr))
        d.stop_requested = True
        summary = d.run()
        self.assertEqual(summary["cycles"], 0)
        self.assertEqual(tr.calls, [])


class MakeSourceTests(unittest.TestCase):
    def test_unconfigured_and_unknown_fail_closed_to_idle(self):
        src, note = make_source(Config(workspace="."))
        self.assertIsNone(src)
        self.assertIn("no task source configured", note)
        src, note = make_source(Config(workspace=".", task_source="webhook"))
        self.assertIsNone(src)
        self.assertIn("unknown task source", note)

    def test_queue_source_with_default_and_explicit_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, note = make_source(Config(workspace=tmp,
                                           task_source="queue"))
            self.assertIsInstance(src, QueueFileSource)
            self.assertEqual(src.path, default_queue_path(tmp))
            src, _ = make_source(Config(workspace=tmp, task_source="queue",
                                        task_queue="/x/q.txt"))
            self.assertEqual(src.path, "/x/q.txt")

    def test_discord_needs_a_session_else_idles(self):
        src, note = make_source(Config(workspace=".",
                                       task_source="discord-inbox"))
        self.assertIsNone(src)
        self.assertIn("HUGPY_DISCORD_SESSION", note)
        src, _ = make_source(Config(workspace=".",
                                    task_source="discord-inbox",
                                    discord_session=SESSION),
                             transport=StubTransport())
        self.assertIsInstance(src, DiscordInboxSource)
        self.assertEqual(src.session_url, SESSION)

    def test_config_env_wiring(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(environ={
                "HUGPY_WORKSPACE": tmp,
                "HUGPY_TASK_SOURCE": "queue",
                "HUGPY_TASK_QUEUE": os.path.join(tmp, "work.txt"),
                "HUGPY_POLL_INTERVAL": "30",
            })
        self.assertEqual(cfg.task_source, "queue")
        self.assertEqual(cfg.task_queue, os.path.join(tmp, "work.txt"))
        self.assertEqual(cfg.poll_interval, 30)
        # defaults: unconfigured source, 10s cadence
        cfg2 = Config()
        self.assertEqual(cfg2.task_source, "")
        self.assertIsNone(cfg2.task_queue)
        self.assertEqual(cfg2.poll_interval, 10)

    def test_task_prefix_is_the_documented_convention(self):
        self.assertEqual(TASK_PREFIX, "task:")


if __name__ == "__main__":
    unittest.main()
