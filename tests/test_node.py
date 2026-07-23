"""Agent node mode (P3.2): the register/heartbeat/pull client, its fail-closed
state machine (401/403/410 + once-only token), the serve source that drives it
(~30s heartbeat cadence, backoff, at-least-once cursor), and the make_source
composition (`--node` alone / alongside a local task source).

Fully OFFLINE. Most tests drive NodeClient through `FakeCentral` — an in-process
model of the P3.1 store+routes (agent_nodes.py / agent_routes.py) used directly
as the client's transport, so no socket is touched. ONE class stands the SAME
model up behind a stdlib http.server and exercises the REAL urllib transport end
to end (register -> heartbeat -> dispatch -> pull -> re-enroll on 410) — the
stand-in for the post-deploy live acceptance while /agent/* is undeployed."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import json
import os
import re
import secrets
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hugpy_agent.config import Config, load_config
from hugpy_agent.node import (AgentNodeSource, BACKOFF_START, HEARTBEAT_INTERVAL,
                              MultiSource, NodeClient, default_state_path,
                              task_text)
from hugpy_agent.serve import Daemon, make_source


# ── a faithful in-process model of the P3.1 /agent/* store + routes ─────────
class FakeCentral:
    """Models comms.agent_nodes.AgentNodeStore + routes/agent_routes.py closely
    enough to test the client against: once-only token at register, node-token
    auth with 401/403/410 semantics, a monotonic dispatch cursor. Callable as a
    NodeClient transport: (url, method, payload, timeout, headers) -> (status,
    body)."""

    def __init__(self, require_api_key=False):
        self.nodes = {}       # id -> row dict (incl. the plaintext token)
        self.tasks = {}       # id -> [ {seq, task, status} ]
        self.seq = 0
        self.require_api_key = require_api_key
        self.calls = []       # (method, path) — the token is never recorded

    # -- operator-side helpers the test driver uses --------------------------
    def dispatch(self, node_id, task):
        if node_id not in self.nodes or self.nodes[node_id]["revoked"]:
            return None
        self.seq += 1
        row = {"seq": self.seq, "task": task, "status": "queued"}
        self.tasks.setdefault(node_id, []).append(row)
        return row

    def revoke(self, node_id):
        if node_id in self.nodes:
            self.nodes[node_id]["revoked"] = True

    def wipe(self):
        """Simulate a central db reset — every known node now 410s."""
        self.nodes.clear()
        self.tasks.clear()

    # -- the handler ---------------------------------------------------------
    def _auth(self, node_id, headers):
        node = self.nodes.get(node_id)
        if node is None:
            return None, 410
        if node["revoked"]:
            return None, 403
        tok = (headers or {}).get("Authorization", "")
        tok = tok[7:] if tok.lower().startswith("bearer ") else ""
        if not tok or tok != node["token"]:
            return None, 401
        return node, 200

    @staticmethod
    def _public(node_id, node):
        return {"id": node_id, "name": node["name"], "host": node["host"],
                "capabilities": node["capabilities"], "status": node["status"],
                "current_task": node["current_task"], "version": node["version"],
                "revoked": node["revoked"]}

    def handle(self, method, path, query, payload, headers):
        self.calls.append((method, path))
        if path.endswith("/agent/register") and method == "POST":
            if self.require_api_key:
                auth = (headers or {}).get("Authorization", "")
                if not auth.lower().startswith("bearer "):
                    return 401, {"error": "registration requires an API key"}
            payload = payload or {}
            name = (payload.get("name") or "").strip()
            if not name:
                return 400, {"error": "name required"}
            nid = "agn_%s" % secrets.token_hex(6)
            tok = "agt_%s" % secrets.token_hex(24)   # minted ONCE, here only
            self.nodes[nid] = {"token": tok, "revoked": False, "name": name,
                               "host": payload.get("host") or "",
                               "capabilities": payload.get("capabilities") or [],
                               "status": "enrolled", "current_task": None,
                               "version": None}
            body = self._public(nid, self.nodes[nid])
            body["token"] = tok
            return 201, body
        m = re.search(r"/agent/([^/]+)/heartbeat$", path)
        if m and method == "POST":
            node, code = self._auth(m.group(1), headers)
            if code != 200:
                return code, {"error": "auth %s" % code}
            for k in ("status", "current_task", "version"):
                if (payload or {}).get(k) is not None:
                    node[k] = payload[k]
            return 200, self._public(m.group(1), node)
        m = re.search(r"/agent/([^/]+)/tasks$", path)
        if m and method == "GET":
            node, code = self._auth(m.group(1), headers)
            if code != 200:
                return code, {"error": "auth %s" % code}
            since = 0
            try:
                since = int((query.get("since") or ["0"])[0])
            except (TypeError, ValueError):
                since = 0
            items = [t for t in self.tasks.get(m.group(1), [])
                     if t["seq"] > since]
            cursor = items[-1]["seq"] if items else since
            return 200, {"node_id": m.group(1), "since": since,
                         "cursor": cursor, "tasks": items}
        m = re.search(r"/agent/([^/]+)/tasks/([^/]+)/result$", path)
        if m and method == "POST":
            node, code = self._auth(m.group(1), headers)
            if code != 200:                          # 410/403/401, same as pull
                return code, {"error": "auth %s" % code}
            p = payload or {}
            status = (p.get("status") or "").strip().lower()
            if status not in ("done", "error"):
                return 400, {"error": "status must be done/error"}
            row = None                               # task scoped to this node
            for t in self.tasks.get(m.group(1), []):
                if str(t["seq"]) == m.group(2):
                    row = t
                    break
            if row is None:                          # unknown / other node's seq
                return 404, {"error": "unknown task for this node"}
            if row.get("status") in ("done", "error"):
                # first-report-wins: NOT overwritten, returned with the marker.
                return 409, dict(row, already_finalized=True)
            result = p.get("result")
            if result is not None and not isinstance(result, str):
                result = json.dumps(result)          # structured -> text
            if isinstance(result, str):              # 64 KiB cap: TRUNCATE, never reject
                raw = result.encode("utf-8")
                if len(raw) > 65536:
                    result = (raw[:65536].decode("utf-8", "ignore")
                              + "…[truncated]")
            row["status"] = status
            row["result"] = result
            row["finished_at"] = 1.0
            return 200, row
        m = re.search(r"/agent/([^/]+)/dispatch$", path)
        if m and method == "POST":
            row = self.dispatch(m.group(1), (payload or {}).get("task"))
            if row is None:
                return 404, {"error": "unknown/revoked node"}
            return 201, row
        return 404, {"error": "no route %s %s" % (method, path)}

    # -- transport interface -------------------------------------------------
    def __call__(self, url, method="GET", payload=None, timeout=30,
                 headers=None):
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query)
        return self.handle(method, parts.path, query, payload, headers)


class FakeClock:
    """monotonic + sleep pair (sleeping advances time), matching test_serve."""

    def __init__(self):
        self.t = 0.0
        self.slept = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def _cfg(central="http://central.test/api", state=None, name="node-a",
         api_key="", workspace=".", agent_node=False, caps=None):
    return Config(base=central, agent_central=central, api_key=api_key,
                  agent_name=name, workspace=workspace, agent_state=state or "",
                  agent_node=agent_node,
                  agent_capabilities=list(caps or []))


# ── NodeClient: the three verbs + the fail-closed state machine ─────────────
class NodeClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "node_state.json")
        self.fc = FakeCentral()
        self.client = NodeClient(_cfg(state=self.state), transport=self.fc)

    def test_register_persists_id_and_token_once(self):
        res = self.client.register()
        self.assertTrue(res["ok"])
        self.assertTrue(self.client.node_id.startswith("agn_"))
        self.assertTrue(self.client.token.startswith("agt_"))
        # the token is minted ONCE, from register — persisted, then reused:
        with open(self.state) as fh:
            st = json.load(fh)
        self.assertEqual(st["id"], self.client.node_id)
        self.assertEqual(st["token"], self.client.token)
        self.assertEqual(st["cursor"], 0)

    def test_state_file_is_0600(self):
        self.client.register()
        mode = os.stat(self.state).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_registered_node_reused_from_state_no_re_register(self):
        self.client.register()
        nid = self.client.node_id
        again = NodeClient(_cfg(state=self.state), transport=self.fc)
        self.assertTrue(again.enrolled())
        self.assertEqual(again.node_id, nid)
        # ensure_enrolled must NOT hit register again:
        before = len([c for c in self.fc.calls if c[1].endswith("/register")])
        again.ensure_enrolled()
        after = len([c for c in self.fc.calls if c[1].endswith("/register")])
        self.assertEqual(before, after)

    def test_config_drift_ignores_stale_creds(self):
        self.client.register()
        # same state file, but a different central -> do not present a token
        # central never minted; a fresh client re-enrolls.
        other = NodeClient(_cfg(central="http://other.test/api",
                                state=self.state), transport=self.fc)
        self.assertFalse(other.enrolled())

    def test_heartbeat_round_trip(self):
        self.client.register()
        res = self.client.heartbeat(status="idle")
        self.assertTrue(res["ok"])
        self.assertEqual(res["node"]["status"], "idle")
        self.assertEqual(res["node"]["version"], self.client.version)
        self.assertNotIn("token", res["node"])       # never echoed back

    def test_heartbeat_410_reenrolls(self):
        self.client.register()
        old = self.client.node_id
        self.fc.wipe()                               # central forgot everyone
        res = self.client.heartbeat(status="idle")
        self.assertTrue(res["ok"])
        self.assertTrue(res.get("reenrolled"))
        self.assertNotEqual(self.client.node_id, old)  # fresh identity minted
        self.assertTrue(self.client.node_id in self.fc.nodes)

    def test_heartbeat_403_revoked_is_data_and_forgets(self):
        self.client.register()
        self.fc.revoke(self.client.node_id)
        res = self.client.heartbeat(status="idle")
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], 403)
        self.assertFalse(self.client.enrolled())     # dropped the dead token

    def test_bad_token_is_401_data_not_raise(self):
        self.client.register()
        self.client.token = "agt_wrong"              # tampered credential
        res = self.client.heartbeat(status="idle")
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], 401)

    def test_register_api_key_gate_401(self):
        fc = FakeCentral(require_api_key=True)
        # no api_key configured -> register 401 as data
        client = NodeClient(_cfg(state=self.state), transport=fc)
        res = client.register()
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], 401)
        # with a console key, register succeeds (the key rides as Bearer)
        keyed = NodeClient(_cfg(state=self.state + "2", api_key="hp_test"),
                           transport=fc)
        self.assertTrue(keyed.register()["ok"])

    def test_pull_returns_tasks_and_cursor(self):
        self.client.register()
        self.fc.dispatch(self.client.node_id, {"kind": "chat", "prompt": "hi"})
        self.fc.dispatch(self.client.node_id, {"prompt": "again"})
        res = self.client.pull()
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["tasks"]), 2)
        self.assertEqual(res["cursor"], 2)
        # idempotent: re-pull from the cursor yields nothing
        self.client.set_cursor(res["cursor"])
        self.assertEqual(self.client.pull()["tasks"], [])

    def test_pull_410_reenrolls_resets_cursor(self):
        self.client.register()
        self.fc.dispatch(self.client.node_id, {"prompt": "x"})
        self.client.set_cursor(self.client.pull()["cursor"])
        self.fc.wipe()
        res = self.client.pull()
        self.assertTrue(res.get("reenrolled"))
        self.assertEqual(self.client.cursor, 0)

    def test_network_error_is_data_never_raises(self):
        def boom(*a, **k):
            raise OSError("central down")
        client = NodeClient(_cfg(state=self.state), transport=boom)
        self.assertFalse(client.register()["ok"])
        self.assertFalse(client.heartbeat()["ok"])
        pull = client.pull()
        self.assertFalse(pull["ok"])
        self.assertEqual(pull["tasks"], [])
        rr = client.report_result(1, status="done", result="a")
        self.assertFalse(rr["ok"])
        self.assertFalse(rr["recorded"])             # never raises, never lies

    # ── report_result: the P3.1b completion route ──────────────────────────
    def test_report_result_records_and_stores_full_body(self):
        self.client.register()
        self.fc.dispatch(self.client.node_id, {"prompt": "do X"})
        res = self.client.report_result(1, status="done", result="the answer")
        self.assertTrue(res["recorded"])
        self.assertEqual(res["status"], 200)
        row = self.fc.tasks[self.client.node_id][0]
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["result"], "the answer")

    def test_report_result_error_status(self):
        self.client.register()
        self.fc.dispatch(self.client.node_id, {"prompt": "do X"})
        res = self.client.report_result(1, status="error", result="it broke")
        self.assertTrue(res["recorded"])
        self.assertEqual(self.fc.tasks[self.client.node_id][0]["status"],
                         "error")

    def test_report_result_second_post_409_recorded_first_wins(self):
        self.client.register()
        self.fc.dispatch(self.client.node_id, {"prompt": "do X"})
        self.assertTrue(
            self.client.report_result(1, "done", "first")["recorded"])
        res = self.client.report_result(1, "error", "second")
        self.assertTrue(res["recorded"])             # 409 still counts recorded
        self.assertEqual(res["status"], 409)
        row = self.fc.tasks[self.client.node_id][0]
        self.assertEqual(row["status"], "done")      # first report wins
        self.assertEqual(row["result"], "first")     # NOT overwritten

    def test_report_result_oversize_body_is_truncated_not_rejected(self):
        self.client.register()
        self.fc.dispatch(self.client.node_id, {"prompt": "x"})
        res = self.client.report_result(1, "done", "y" * 70000)
        self.assertTrue(res["recorded"])             # truncated, NOT 4xx-rejected
        self.assertEqual(res["status"], 200)
        stored = self.fc.tasks[self.client.node_id][0]["result"]
        self.assertLessEqual(len(stored.encode("utf-8")), 65536 + 32)

    def test_report_result_410_reenrolls(self):
        self.client.register()
        self.fc.dispatch(self.client.node_id, {"prompt": "x"})
        old = self.client.node_id
        self.fc.wipe()                               # central forgot everyone
        res = self.client.report_result(1, "done", "a")
        self.assertFalse(res["recorded"])
        self.assertTrue(res.get("reenrolled"))
        self.assertNotEqual(self.client.node_id, old)

    def test_report_result_403_revoked_is_data_and_forgets(self):
        self.client.register()
        self.fc.dispatch(self.client.node_id, {"prompt": "x"})
        self.fc.revoke(self.client.node_id)
        res = self.client.report_result(1, "done", "a")
        self.assertFalse(res["recorded"])
        self.assertEqual(res["status"], 403)
        self.assertFalse(self.client.enrolled())     # dropped the dead token

    def test_report_result_404_unknown_task_is_data(self):
        self.client.register()
        res = self.client.report_result(999, "done", "a")  # never dispatched
        self.assertFalse(res["recorded"])
        self.assertEqual(res["status"], 404)

    def test_report_result_401_bad_token_is_data(self):
        self.client.register()
        self.fc.dispatch(self.client.node_id, {"prompt": "x"})
        self.client.token = "agt_wrong"              # tampered credential
        res = self.client.report_result(1, "done", "a")
        self.assertFalse(res["recorded"])
        self.assertEqual(res["status"], 401)


class TaskTextTests(unittest.TestCase):
    def test_common_shapes_map_to_a_string(self):
        self.assertEqual(task_text("bare"), "bare")
        self.assertEqual(task_text({"kind": "chat", "prompt": "do X"}), "do X")
        self.assertEqual(task_text({"task": "do Y"}), "do Y")
        self.assertEqual(task_text({"text": "do Z"}), "do Z")

    def test_unknown_shape_round_trips_as_json_never_dropped(self):
        out = task_text({"weird": [1, 2]})
        self.assertEqual(json.loads(out), {"weird": [1, 2]})


# ── AgentNodeSource: cadence, backoff, at-least-once cursor ──────────────────
class AgentNodeSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "node_state.json")
        self.fc = FakeCentral()
        self.clock = FakeClock()
        self.client = NodeClient(_cfg(state=self.state), transport=self.fc)
        self.src = AgentNodeSource(self.client, monotonic=self.clock.monotonic)

    def test_first_poll_enrolls_and_idle_beats(self):
        self.assertEqual(self.src.poll(), [])
        self.assertTrue(self.client.enrolled())
        beats = [c for c in self.fc.calls if c[1].endswith("/heartbeat")]
        self.assertEqual(len(beats), 1)              # one idle beat, no tasks

    def test_dispatched_task_pulled_and_busy_then_idle(self):
        self.src.poll()                              # enroll + idle beat
        self.fc.dispatch(self.client.node_id, {"prompt": "summarize A"})
        tasks = self.src.poll()
        self.assertEqual(tasks, ["summarize A"])
        # picking it up flips the node busy on central:
        self.assertEqual(self.fc.nodes[self.client.node_id]["status"], "busy")
        self.assertEqual(self.fc.nodes[self.client.node_id]["current_task"],
                         "1")
        # reporting completion beats it back to idle and advances the cursor:
        self.src.report("summarize A", {"outcome": "done", "run_id": "r1"})
        self.assertEqual(self.fc.nodes[self.client.node_id]["status"], "idle")
        # cleared via empty string (null would keep the prior value; see poll):
        self.assertEqual(self.fc.nodes[self.client.node_id]["current_task"], "")
        self.assertEqual(self.client.cursor, 1)

    def test_cursor_persists_only_after_report_at_least_once(self):
        self.src.poll()
        self.fc.dispatch(self.client.node_id, {"prompt": "t1"})
        self.src.poll()
        # pulled but NOT yet reported: the PERSISTED cursor is still 0, so a
        # crash-restart re-pulls the un-reported task.
        with open(self.state) as fh:
            self.assertEqual(json.load(fh)["cursor"], 0)
        self.src.report("t1", {"outcome": "done"})
        with open(self.state) as fh:
            self.assertEqual(json.load(fh)["cursor"], 1)

    def test_report_posts_outcome_to_central_result_route(self):
        self.src.poll()
        self.fc.dispatch(self.client.node_id, {"prompt": "summarize A"})
        self.src.poll()
        self.src.report("summarize A",
                        {"outcome": "done", "answer": "A is about X"})
        # the outcome landed on central's task row, not just local bookkeeping:
        row = self.fc.tasks[self.client.node_id][0]
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["result"], "A is about X")
        self.assertEqual(self.client.cursor, 1)      # recorded -> advanced

    def test_report_sends_full_answer_not_clipped_to_discord_limit(self):
        self.src.poll()
        self.fc.dispatch(self.client.node_id, {"prompt": "big"})
        self.src.poll()
        big = "x" * 5000                             # >> the 1900-char reply cap
        self.src.report("big", {"outcome": "done", "answer": big})
        # central got the FULL answer (format_reply would have clipped it):
        self.assertEqual(self.fc.tasks[self.client.node_id][0]["result"], big)

    def test_report_error_outcome_sends_error_body(self):
        self.src.poll()
        self.fc.dispatch(self.client.node_id, {"prompt": "boom"})
        self.src.poll()
        self.src.report("boom", {"outcome": "error", "error": "it blew up"})
        row = self.fc.tasks[self.client.node_id][0]
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["result"], "it blew up")
        self.assertEqual(self.client.cursor, 1)

    def test_report_404_advances_cursor_dropping_futile_task(self):
        self.src.poll()
        self.fc.dispatch(self.client.node_id, {"prompt": "gone"})
        self.src.poll()
        self.fc.tasks[self.client.node_id].clear()   # task vanished on central
        self.src.report("gone", {"outcome": "done", "answer": "a"})
        self.assertEqual(self.client.cursor, 1)      # 404 -> still advance/drop

    def test_report_transient_failure_leaves_cursor_unadvanced(self):
        real = self.fc

        def flaky(url, method="GET", payload=None, timeout=30, headers=None):
            if method == "POST" and url.endswith("/result"):
                return 500, {"error": "central hiccup"}   # not recorded, not 404
            return real(url, method=method, payload=payload, timeout=timeout,
                        headers=headers)
        client = NodeClient(_cfg(state=self.state + "f"), transport=flaky)
        src = AgentNodeSource(client, monotonic=self.clock.monotonic)
        src.poll()
        real.dispatch(client.node_id, {"prompt": "t"})
        src.poll()
        src.report("t", {"outcome": "done", "answer": "a"})
        # not recorded (and not 404) -> the PERSISTED cursor stays put so a
        # crash-restart re-pulls + re-reports (at-least-once):
        self.assertEqual(client.cursor, 0)
        with open(self.state + "f") as fh:
            self.assertEqual(json.load(fh)["cursor"], 0)

    def test_heartbeat_cadence_is_about_thirty_seconds(self):
        self.src.poll()                              # beat at t=0
        self.clock.t = HEARTBEAT_INTERVAL - 1        # not due yet
        self.src.poll()
        beats = [c for c in self.fc.calls if c[1].endswith("/heartbeat")]
        self.assertEqual(len(beats), 1)
        self.clock.t = HEARTBEAT_INTERVAL + 0.5      # now due
        self.src.poll()
        beats = [c for c in self.fc.calls if c[1].endswith("/heartbeat")]
        self.assertEqual(len(beats), 2)

    def test_unreachable_central_backs_off_and_never_raises(self):
        def boom(*a, **k):
            raise OSError("central down")
        client = NodeClient(_cfg(state=self.state + "b"), transport=boom)
        src = AgentNodeSource(client, monotonic=self.clock.monotonic)
        self.assertEqual(src.poll(), [])             # fails as data
        self.assertGreaterEqual(src._backoff, BACKOFF_START)
        na = src._next_attempt
        self.assertGreater(na, self.clock.t)
        # inside the window: no central I/O at all
        src.poll()
        # after the window elapses it retries (still down -> backoff grows)
        self.clock.t = na + 0.1
        src.poll()
        self.assertGreater(src._backoff, BACKOFF_START)

    def test_recovers_when_central_returns(self):
        calls = {"n": 0}
        real = self.fc

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] <= 1:
                raise OSError("blip")
            return real(*a, **k)
        client = NodeClient(_cfg(state=self.state + "c"), transport=flaky)
        src = AgentNodeSource(client, monotonic=self.clock.monotonic)
        src.poll()                                   # blip -> backoff
        self.clock.t = src._next_attempt + 0.1
        src.poll()                                   # recovers, enrolls
        self.assertTrue(client.enrolled())
        self.assertEqual(src._backoff, 0.0)


# ── MultiSource: --node alongside a local source ────────────────────────────
class _RecordingSource:
    def __init__(self, name, tasks):
        self.name = name
        self._tasks = tasks
        self.reported = []

    def poll(self):
        out, self._tasks = self._tasks, []
        return out

    def report(self, task, report):
        self.reported.append(task)


class MultiSourceTests(unittest.TestCase):
    def test_concatenates_and_routes_reports_to_the_right_source(self):
        a = _RecordingSource("a", ["a1", "a2"])
        b = _RecordingSource("b", ["b1"])
        multi = MultiSource([a, b])
        self.assertEqual(multi.poll(), ["a1", "a2", "b1"])
        multi.report("b1", {"outcome": "done"})
        multi.report("a1", {"outcome": "done"})
        self.assertEqual(a.reported, ["a1"])
        self.assertEqual(b.reported, ["b1"])


# ── make_source composition (the --node flag semantics) ─────────────────────
class MakeSourceCompositionTests(unittest.TestCase):
    def test_node_off_is_unchanged(self):
        src, note = make_source(Config(workspace="."))
        self.assertIsNone(src)               # no task source, node off -> idle

    def test_node_only_when_no_local_source(self):
        src, note = make_source(_cfg(agent_node=True), transport=FakeCentral())
        self.assertIsInstance(src, AgentNodeSource)
        self.assertIn("agent-node", note)

    def test_node_alongside_a_queue_source_is_multi(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(agent_node=True, workspace=tmp)
            cfg.task_source = "queue"
            src, note = make_source(cfg, transport=FakeCentral())
            self.assertIsInstance(src, MultiSource)
            self.assertIn("+", note)

    def test_node_without_central_falls_back_to_base(self):
        # agent_central empty but base set -> node still usable (base has the
        # /api dual-mount).
        cfg = Config(workspace=".", base="http://c.test/api", agent_node=True)
        src, note = make_source(cfg, transport=FakeCentral())
        self.assertIsInstance(src, AgentNodeSource)


class ConfigWiringTests(unittest.TestCase):
    def test_env_wiring(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(environ={
                "HUGPY_WORKSPACE": tmp,
                "HUGPY_AGENT_CENTRAL": "http://central.test/api",
                "HUGPY_AGENT_NODE": "true",
                "HUGPY_AGENT_NAME": "blackbird-1",
                "HUGPY_AGENT_CAPABILITIES": "chat, tools, shell",
            })
        self.assertEqual(cfg.agent_central, "http://central.test/api")
        self.assertTrue(cfg.agent_node)
        self.assertEqual(cfg.agent_name, "blackbird-1")
        self.assertEqual(cfg.agent_capabilities, ["chat", "tools", "shell"])

    def test_defaults(self):
        cfg = Config()
        self.assertEqual(cfg.agent_central, "")
        self.assertFalse(cfg.agent_node)
        self.assertEqual(cfg.agent_capabilities, [])

    def test_default_state_path_under_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                default_state_path(tmp),
                os.path.join(os.path.realpath(tmp), ".hugpy_agent",
                             "node_state.json"))


# ── end-to-end over a REAL socket (the live-acceptance stand-in) ────────────
class _Handler(BaseHTTPRequestHandler):
    central = None  # set per-test

    def log_message(self, *a):        # keep the test output quiet
        return

    def _run(self, method):
        parts = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parts.query)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        payload = json.loads(raw) if raw.strip() else None
        headers = {"Authorization": self.headers.get("Authorization", "")}
        status, body = self.central.handle(method, parts.path, query,
                                           payload, headers)
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._run("GET")

    def do_POST(self):
        self._run("POST")


class LiveHttpServerTests(unittest.TestCase):
    """The real urllib transport against a socket-backed P3.1 model — the
    faithful stand-in for the post-deploy acceptance while /agent/* is
    undeployed on central."""

    def setUp(self):
        self.fc = FakeCentral()
        _Handler.central = self.fc
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "node_state.json")

    def _client(self):
        base = "http://127.0.0.1:%d/api" % self.port
        return NodeClient(_cfg(central=base, state=self.state))

    def test_full_flow_register_heartbeat_dispatch_pull(self):
        client = self._client()
        self.assertTrue(client.register()["ok"])     # real POST /agent/register
        self.assertTrue(client.heartbeat(status="idle")["ok"])
        # operator dispatches, node pulls it over the wire:
        self.fc.dispatch(client.node_id, {"kind": "chat", "prompt": "do it"})
        res = client.pull()
        self.assertTrue(res["ok"])
        self.assertEqual(task_text(res["tasks"][0]["task"]), "do it")
        client.set_cursor(res["cursor"])
        self.assertEqual(client.pull()["tasks"], [])  # idempotent re-pull

    def test_result_route_over_the_wire(self):
        client = self._client()
        client.register()
        self.fc.dispatch(client.node_id, {"prompt": "run it"})
        res = client.report_result(1, status="done", result="the real answer")
        self.assertTrue(res["recorded"])              # real POST over urllib
        self.assertEqual(res["status"], 200)
        # a second post -> 409 over the wire; still recorded, first result wins:
        again = client.report_result(1, status="error", result="oops")
        self.assertTrue(again["recorded"])
        self.assertEqual(again["status"], 409)
        self.assertEqual(self.fc.tasks[client.node_id][0]["result"],
                         "the real answer")

    def test_410_self_heals_over_the_wire(self):
        client = self._client()
        client.register()
        old = client.node_id
        self.fc.wipe()                                # central db reset
        res = client.heartbeat(status="idle")
        self.assertTrue(res["ok"])
        self.assertNotEqual(client.node_id, old)

    def test_bad_token_401_over_the_wire(self):
        client = self._client()
        client.register()
        client.token = "agt_bogus"
        res = client.heartbeat(status="idle")
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], 401)

    def test_daemon_runs_a_dispatched_task_end_to_end(self):
        """The whole P3.2 loop: Daemon(--node) enrolls, pulls a dispatched
        task, runs it through a stub loop, and reports — offline, real wire."""
        cfg = _cfg(central="http://127.0.0.1:%d/api" % self.port,
                   state=self.state, agent_node=True)
        clock = FakeClock()
        log = []

        class StubLoop:
            def run(self, task):
                log.append(task)
                return {"run_id": "r1", "outcome": "done", "steps": 1,
                        "answer": "done it"}
        daemon = Daemon(cfg, loop_factory=lambda: StubLoop(),
                        sleep=clock.sleep, monotonic=clock.monotonic)
        # pre-enroll so we can dispatch to a known id, then run one cycle that
        # picks the task up:
        daemon.run(max_cycles=1)                      # cycle 1: enroll + idle
        node_id = daemon.source.client.node_id
        self.fc.dispatch(node_id, {"prompt": "the dispatched task"})
        daemon.run(max_cycles=1)                      # cycle 2: pull + run
        self.assertEqual(log, ["the dispatched task"])
        self.assertEqual(self.fc.nodes[node_id]["status"], "idle")  # done


if __name__ == "__main__":
    unittest.main()
