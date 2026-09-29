import _bootstrap  # noqa: F401
import io
import os
import threading
import unittest
from unittest.mock import Mock, patch

from hugpy_agent import fleet_console as fc
from hugpy_agent import fleet_tui as tui
from hugpy_agent.config import Config


MODEL = {"model": "m", "task": "text-generation", "tasks": [], "blocked": False,
         "readiness": "ready now", "state": "hot", "worker": "gpu", "tok_s": 40, "eta_s": 0}
WORKER = {"id": "worker/id", "name": "gpu", "status": "online", "loaded_models": ["m"]}


class Screen:
    def __init__(self, keys=()):
        self.keys = iter(keys)
        self.text = []

    def getmaxyx(self): return (24, 100)
    def getch(self): return next(self.keys, ord("q"))
    def addnstr(self, y, x, value, limit, attr): self.text.append(value[:limit])
    def erase(self): pass
    def refresh(self): pass
    def timeout(self, value): pass
    def keypad(self, value): pass


class TuiTests(unittest.TestCase):
    def setUp(self):
        self.client = fc.Client("http://localhost:7002", "key", "operator")
        self.ui = tui.Console(Screen(), self.client)
        self.ui.models = [dict(MODEL)]
        self.ui.state["workers"] = [dict(WORKER)]

    def test_na_metric_placeholder_is_renderable(self):
        self.ui.tab = 4
        self.ui.results = [{"matrix": True, "ok": False, "model": "m",
                            "worker": "gpu", "tok_s": "N/A"}]
        self.ui.draw()
        self.assertTrue(any("N/A tok/s" in text for text in self.ui.screen.text))

    def test_default_terminal_launches_tui_without_repl(self):
        with patch("sys.stdin.isatty", return_value=True), patch.object(tui, "run", return_value=0) as run:
            self.assertEqual(fc.main([], cfg=Config()), 0)
        run.assert_called_once()

    def test_model_load_is_fully_menu_driven(self):
        # Select load, worker, existing allocation, confirm.
        with patch.object(self.ui, "choose", side_effect=[1, 0, 0, 1]), patch.object(self.ui, "operation") as operation:
            self.ui.open_row(dict(MODEL))
        with patch.object(self.client, "request", return_value={"ok": True}) as request:
            operation.call_args.args[1]()
        request.assert_called_once_with("/llm/workers/worker%2Fid/load", "POST", {"model_key": "m"})

    def test_back_from_confirmation_never_mutates(self):
        with patch.object(self.ui, "choose", side_effect=[0]), patch.object(self.ui, "operation") as operation:
            self.ui.control("unload", "m", WORKER)
        operation.assert_not_called()

    def test_worker_models_requires_no_typed_id(self):
        self.ui.tab = 1
        with patch.object(self.ui, "choose", return_value=0):
            self.ui.open_row(WORKER)
        self.assertEqual(self.ui.tab, 0)
        self.assertEqual(self.ui.items()[0]["model"], "m")

    def test_tabs_and_quit_restore_shutdown_state(self):
        screen = Screen([ord("2"), ord("q")])
        ui = tui.Console(screen, self.client)
        with patch.object(ui, "start_refresh"), patch.object(tui.curses, "curs_set"):
            self.assertEqual(ui.run(), 0)
        self.assertEqual(ui.tab, 1)
        self.assertTrue(ui.closed.is_set())
        self.assertTrue(ui.stop_tests.is_set())

    def test_first_refresh_shows_branded_intro(self):
        ui = tui.Console(Screen(), self.client)
        ui.refreshing = True
        ui.draw()
        self.assertTrue(any("HUGPY AGENT" in text for text in ui.screen.text))
        self.assertTrue(any("Your models. Your workers. One fleet." in text
                            for text in ui.screen.text))

    def test_modal_navigation_and_escape(self):
        self.ui.screen = Screen([tui.curses.KEY_DOWN, 10])
        self.assertEqual(self.ui.choose("Controls", ["Inspect", "Load"]), 1)
        self.ui.screen = Screen([27])
        self.assertIsNone(self.ui.choose("Controls", ["Inspect", "Load"]))

    def test_long_lists_scroll_without_losing_selected_row(self):
        self.ui.models = [dict(MODEL, model="model-%d" % i) for i in range(100)]
        self.ui.selected = 90
        self.ui.draw()
        self.assertTrue(any("model-90" in t for t in self.ui.screen.text))

    def test_busy_status_and_errors_are_visible(self):
        self.ui.busy = True
        self.ui.state["errors"] = {"workers": "HTTP 401"}
        self.ui.draw()
        self.assertTrue(any("operation running" in t for t in self.ui.screen.text))
        self.assertTrue(any("Data unavailable" in t for t in self.ui.screen.text))

    def test_batch_stops_after_uncertain_failure_without_retry(self):
        client = Mock()
        client.request.side_effect = fc.FleetError("timeout")
        report = Mock()
        tui.run_tests(client, [MODEL, dict(MODEL, model="next")], "hi", 64, False, threading.Event(), report)
        self.assertEqual(client.request.call_count, 1)
        self.assertFalse(report.call_args.args[1]["ok"])
        self.assertTrue(client.request.call_args.args[2]["no_makeroom"])

    def test_batch_stop_preserves_current_result(self):
        stop = threading.Event()
        client = Mock()
        def reply(*args):
            stop.set()
            return {"choices": []}
        client.request.side_effect = reply
        report = Mock()
        tui.run_tests(client, [MODEL, MODEL], "hi", 64, True, stop, report)
        self.assertEqual(client.request.call_count, 1)
        self.assertEqual(report.call_args.args[0], "result")
        self.assertTrue(report.call_args.args[1]["ok"])

    def test_matrix_records_worker_verified_speed_and_specs(self):
        client = Mock()
        client.request.side_effect = [
            {"ok": True, "fit": True, "vram_used": 123},
            {"choices": [{"message": {"content": "hello"}}]},
            {"entries": [{"ts": 101, "tok_per_s": 42.5}]},
        ]
        events = []
        with patch.object(tui.time, "time", return_value=100):
            tui.run_matrix_tests(client, [dict(MODEL, quant="Q4_K_M")],
                                 [dict(WORKER, admission="approved")],
                                 "hi", 64, threading.Event(),
                                 lambda kind, value: events.append((kind, value)))
        result = [value for kind, value in events if kind == "result"][0]
        self.assertTrue(result["ok"])
        self.assertEqual(result["worker_id"], "worker/id")
        self.assertEqual(result["tok_s"], 42.5)
        self.assertEqual(result["specs"]["quant"], "Q4_K_M")
        self.assertIn("worker%2Fid", client.request.call_args_list[0].args[0])

    def test_matrix_reports_nonlocal_pair_without_chat(self):
        client = Mock()
        client.request.return_value = {"ok": False, "fit": False,
                                       "error": "not local — probe does not download"}
        events = []
        tui.run_matrix_tests(client, [MODEL], [dict(WORKER, admission="approved")],
                             "hi", 64, threading.Event(),
                             lambda kind, value: events.append((kind, value)))
        result = [value for kind, value in events if kind == "result"][0]
        self.assertFalse(result["ok"])
        self.assertIn("not local", result["error"])
        self.assertEqual(client.request.call_count, 1)

    def test_matrix_does_not_misattribute_unverified_speed(self):
        client = Mock()
        client.request.side_effect = [
            {"ok": True, "fit": True},
            {"choices": [{"message": {"content": "hello"}}]},
            {"entries": []},
        ]
        events = []
        tui.run_matrix_tests(client, [MODEL], [dict(WORKER, admission="approved")],
                             "hi", 64, threading.Event(),
                             lambda kind, value: events.append((kind, value)))
        result = [value for kind, value in events if kind == "result"][0]
        self.assertFalse(result["ok"])
        self.assertIn("routed elsewhere", result["error"])

    def test_query_progress_relays_observed_cold_call_states(self):
        report = Mock()
        tracker = tui.QueryProgress(dict(MODEL, model="Model-Q4_K_M"), report,
                                    started_at=100)
        worker = {
            "id": "w", "name": "gpu-a",
            "disk": {"root": "/models"},
            "provisioning": ["Model-Q4_K_M"],
            "provision_progress": {"Model-Q4_K_M": {
                "downloaded_bytes": 2**30, "total_bytes": 2 * 2**30}},
            "loading": ["Model-Q4_K_M"],
            "planned_split": {"Model-Q4_K_M": {
                "gpu_bytes": 3 * 2**30, "ram_bytes": 4 * 2**30}},
            "loaded_models": ["Model-Q4_K_M"],
            "allocations": [{"model_key": "Model-Q4_K_M", "healthy": True,
                             "busy": True, "vram_bytes": 3 * 2**30,
                             "rss_anon_bytes": 4 * 2**30}],
            "vram_evictions": {"last": {"at": 101, "subject": "Model-Q4_K_M",
                                                "victim": "old-Q8", "vram_freed": 5 * 2**30}},
        }
        tracker.observe([worker], [{"model_key": "Model-Q4_K_M", "state": "waiting"}])
        events = [call.args[1] for call in report.call_args_list]
        self.assertEqual([e["stage"] for e in events],
                         ["queue", "evicting", "downloading", "loading", "loaded", "answering"])
        self.assertIn("Q4_K_M", events[2]["message"])
        self.assertIn("/models", events[2]["message"])
        self.assertIn("3.0 GiB to VRAM & 4.0 GiB to RAM", events[3]["message"])

    def test_query_progress_deduplicates_unchanged_telemetry(self):
        report = Mock()
        tracker = tui.QueryProgress(MODEL, report)
        worker = {"id": "w", "name": "gpu", "loaded_models": ["m"],
                  "allocations": [{"model_key": "m", "healthy": True,
                                   "busy": True, "vram_bytes": 1}]}
        tracker.observe([worker], [])
        tracker.observe([worker], [])
        self.assertEqual(report.call_count, 1)

    def test_terminal_control_characters_are_not_rendered(self):
        self.assertNotIn("\x1b", tui.clean("bad\x1b[2Jmodel"))

    def test_refresh_keeps_same_model_selected_after_reordering(self):
        self.ui.models = [dict(MODEL, model="z"), dict(MODEL, model="a")]
        self.ui.selected = 0
        state = dict(self.ui.state, catalog=[{"id": "a"}, {"id": "z"}])
        self.ui.send("snapshot", state)
        self.ui.drain()
        self.assertEqual(self.ui.items()[self.ui.selected]["model"], "z")

    def test_result_shows_answer_without_json_navigation(self):
        output = tui.describe({"model": "m", "elapsed_s": 1, "response": {
            "choices": [{"message": {"content": "Hello operator"}}]}})
        self.assertIn("Hello operator", output)
        self.assertNotIn('"choices"', output)

    @unittest.skipUnless(os.name == "posix", "PTY smoke test requires POSIX")
    def test_real_terminal_opens_tabs_and_quits(self):
        import pty
        import select
        import subprocess
        import sys
        import time
        master, slave = pty.openpty()
        code = """
from hugpy_agent import fleet_tui as t, fleet_console as f
def fake(self, path, *args):
    if path == '/llm/workers': return [{'id':'w','name':'smoke-worker','status':'online','admission':'approved','loaded_models':[]}]
    if path == '/v1/models': return {'data':[]}
    if path == '/llm/queue': return {'active':[]}
    return {'rows':[]}
f.Client.request = fake
t.run(f.Client('http://localhost:7002'))
"""
        env = dict(os.environ, TERM="xterm", PYTHONDONTWRITEBYTECODE="1")
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(tui.__file__))
        proc = subprocess.Popen([sys.executable, "-c", code], stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        output = b""
        try:
            deadline = time.monotonic() + 5
            switched = False
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    output += os.read(master, 65536)
                if b"HUGPY FLEET" in output and not switched:
                    os.write(master, b"2")
                    switched = True
                if b"smoke-worker" in output:
                    break
            self.assertIn(b"smoke-worker", output)
            os.write(master, b"q")
            self.assertEqual(proc.wait(timeout=5), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            os.close(master)


if __name__ == "__main__":
    unittest.main()
