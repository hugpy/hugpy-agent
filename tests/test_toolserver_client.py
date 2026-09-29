"""Shared toolserver client + per-harness wiring, against an in-thread fake
toolserver (stdlib http.server) that speaks the real routes: POST /mcp
(initialize / tools/list flat), /ts/categories, /ts/list, /ts/call, with the
X-Operator-Token gate (401 {"error":"unauthorized"})."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from hugpy_agent import toolserver_client as tsc
from hugpy_agent.toolserver_client import (ToolserverAuthError, ToolserverClient,
                                           ToolserverError)

from helpers import FakeGateway


def tc(name, **arguments):
    """Prompted-tier tool-call block; `name`/`arguments` are the meta-tool's own
    params here, so this shadows helpers.tc with an explicit-args form."""
    return "<tool_call>\n%s\n</tool_call>" % json.dumps({"name": name, "arguments": arguments})


def ts(name, arguments=None):
    """A ts_call round-trip block targeting toolserver tool `name`."""
    return "<tool_call>\n%s\n</tool_call>" % json.dumps(
        {"name": "ts_call", "arguments": {"name": name, "arguments": arguments or {}}})

TOKEN = "secret-token"
TOOLS = [
    {"name": "fs_glob", "description": "Glob files under a path by pattern.",
     "inputSchema": {"type": "object", "properties": {"path": {}, "pattern": {}},
                     "required": ["path", "pattern"]}},
    {"name": "todo_list", "description": "List todos.",
     "inputSchema": {"type": "object", "properties": {"locus": {"type": "string"}}}},
    {"name": "todo_add", "description": "Add a todo.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "vm_stop", "description": "Stop a VM.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
]


class FakeToolserver:
    """Records every request as (path, body, headers). `token` None = open."""

    def __init__(self, token=TOKEN, mcp=True):
        self.token, self.mcp = token, mcp
        self.calls = []
        self.results = {"fs_glob": ["/srv/vm_mgr/docs/a.md", "/srv/vm_mgr/docs/b.md"],
                        "todo_list": [{"id": 1, "text": "x"}], "todo_add": {"ok": True, "id": 2},
                        "vm_stop": {"stopped": True}}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, doc):
                body = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send(405, {"error": "POST only"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
                path = self.path.split("?")[0]
                fake.calls.append((self.path, body, dict(self.headers)))
                if fake.token:
                    sent = self.headers.get("X-Operator-Token", "")
                    if not sent:
                        auth = self.headers.get("Authorization", "")
                        sent = auth[7:] if auth.startswith("Bearer ") else ""
                    if sent != fake.token:
                        return self._send(401, {"error": "unauthorized"})
                if path == "/mcp":
                    if not fake.mcp:
                        return self._send(404, {"error": "not found"})
                    flat = "mode=flat" in self.path
                    m, rid = body.get("method"), body.get("id")
                    if m == "initialize":
                        return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {
                            "protocolVersion": "2025-06-18",
                            "serverInfo": {"name": "abstract-toolserver", "version": "9.9.9"},
                            "capabilities": {"tools": {}}}})
                    if m == "tools/list":
                        tools = TOOLS if flat else [{"name": "ts_call", "description": "",
                                                     "inputSchema": {"type": "object", "properties": {}}}]
                        return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}})
                    return self._send(200, {"jsonrpc": "2.0", "id": rid,
                                            "error": {"code": -32601, "message": "method not found"}})
                if path == "/ts/categories":
                    return self._send(200, {"result": [{"category": "fs", "tools": 1, "summary": ""},
                                                       {"category": "todo", "tools": 2, "summary": ""},
                                                       {"category": "vm", "tools": 1, "summary": ""}]})
                if path == "/ts/list":
                    cat = body.get("category")
                    tools = [{"name": t["name"], "description": t["description"],
                              "parameters": t["inputSchema"]} for t in TOOLS
                             if t["name"].startswith(cat + "_")]
                    return self._send(200, {"result": {"category": cat, "count": len(tools), "tools": tools}})
                if path == "/ts/call":
                    name = body.get("name")
                    if name not in fake.results:
                        return self._send(200, {"result": {"error": "unknown tool: %s" % name}})
                    return self._send(200, {"result": fake.results[name]})
                self._send(404, {"error": "no route"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._server.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def called(self, path):
        return [c for c in self.calls if c[0].split("?")[0] == path]


def client_for(fake, token=TOKEN, **kw):
    return ToolserverClient(fake.url, token, environ={}, **kw)


# ── client ───────────────────────────────────────────────────────────────────
class ClientTests(unittest.TestCase):
    def test_list_tools_shape_and_cache(self):
        with FakeToolserver() as fake:
            c = client_for(fake)
            tools = c.list_tools()
            self.assertEqual([t["name"] for t in tools], ["fs_glob", "todo_list", "todo_add", "vm_stop"])
            self.assertEqual(set(tools[0]), {"name", "description", "input_schema"})
            self.assertEqual(tools[0]["input_schema"]["required"], ["path", "pattern"])
            c.list_tools(); c.list_tools()
            self.assertEqual(len(fake.called("/mcp")), 1)          # cached
            self.assertIn("mode=flat", fake.calls[0][0])
            self.assertEqual(fake.calls[0][2].get("X-Operator-Token"), TOKEN)
            self.assertEqual(len(c.list_tools(refresh=True)), 4)
            self.assertEqual(len(fake.called("/mcp")), 2)

    def test_list_tools_falls_back_to_ts_routes_on_old_server(self):
        with FakeToolserver(mcp=False) as fake:
            c = client_for(fake)
            self.assertEqual([t["name"] for t in c.list_tools()],
                             ["fs_glob", "todo_list", "todo_add", "vm_stop"])
            self.assertTrue(fake.called("/ts/categories") and fake.called("/ts/list"))

    def test_call_round_trip_unwraps_result(self):
        with FakeToolserver() as fake:
            c = client_for(fake)
            out = c.call("fs_glob", {"path": "/srv/vm_mgr/docs", "pattern": "*.md"})
            self.assertEqual(out, ["/srv/vm_mgr/docs/a.md", "/srv/vm_mgr/docs/b.md"])
            path, body, headers = fake.called("/ts/call")[0]
            self.assertEqual(body, {"name": "fs_glob",
                                    "arguments": {"path": "/srv/vm_mgr/docs", "pattern": "*.md"}})
            self.assertEqual(headers.get("Authorization"), "Bearer " + TOKEN)
            # tool-level error is data, not an exception
            self.assertEqual(c.call("nope")["error"], "unknown tool: nope")
            # call_json is always a JSON string
            self.assertEqual(json.loads(c.call_json("todo_list")), [{"id": 1, "text": "x"}])

    def test_call_coerces_json_looking_strings(self):
        with FakeToolserver() as fake:
            client_for(fake).call("todo_add", {"text": "hi", "tags": "[\"a\"]", "done": "false"})
            self.assertEqual(fake.called("/ts/call")[0][1]["arguments"],
                             {"text": "hi", "tags": ["a"], "done": False})

    def test_auth_rejected(self):
        with FakeToolserver() as fake:
            c = client_for(fake, token="wrong")
            with self.assertRaises(ToolserverAuthError) as cm:
                c.list_tools()
            self.assertEqual(cm.exception.status, 401)
            h = c.health()
            self.assertFalse(h["ok"])
            self.assertEqual(h["auth"], "rejected")
            self.assertEqual(c.status()["auth"], "rejected")

    def test_missing_token_names_the_env_var(self):
        with FakeToolserver() as fake:
            with mock.patch.object(tsc, "resolve_token", return_value=("", "none")):
                c = ToolserverClient(fake.url, environ={})
            self.assertEqual(c.token, "")
            with self.assertRaises(ToolserverAuthError) as cm:
                c.call("todo_list")
            self.assertIn("TOOLSERVER_OPERATOR_TOKEN", str(cm.exception))
            h = c.health()
            self.assertEqual(h["auth"], "missing")
            self.assertIn("TOOLSERVER_OPERATOR_TOKEN", h["error"])
            self.assertEqual(c.call_json("todo_list")[:9], '{"error":')

    def test_open_server_without_token(self):
        with FakeToolserver(token=None) as fake:
            with mock.patch.object(tsc, "resolve_token", return_value=("", "none")):
                c = ToolserverClient(fake.url, environ={})
            h = c.health()
            self.assertTrue(h["ok"])
            self.assertEqual(h["auth"], "open")

    def test_health_and_status(self):
        with FakeToolserver() as fake:
            c = client_for(fake)
            h = c.health()
            self.assertEqual({k: h[k] for k in ("url", "ok", "tool_count", "auth", "version")},
                             {"url": fake.url, "ok": True, "tool_count": 4, "auth": "ok",
                              "version": "9.9.9"})
            self.assertIsInstance(h["latency_ms"], int)
            n = len(fake.calls)
            s = c.status()
            self.assertEqual(s["tool_count"], 4)
            self.assertEqual(len(fake.calls), n)                    # cached verdict
            self.assertEqual(tsc.status_line(s), "toolserver ok (4 tools)")

    def test_unreachable(self):
        c = ToolserverClient("http://127.0.0.1:1", TOKEN, environ={}, probe_timeout=2)
        with self.assertRaises(ToolserverError) as cm:
            c.list_tools()
        self.assertEqual(cm.exception.kind, "unreachable")
        h = c.health()
        self.assertFalse(h["ok"])
        self.assertIn("unreachable", h["error"])
        self.assertEqual(tsc.status_line(h), "toolserver unreachable")

    def test_model_api_adapters(self):
        with FakeToolserver() as fake:
            c = client_for(fake)
            oa = c.as_openai_tools()
            self.assertEqual([t["function"]["name"] for t in oa], ["fs_glob", "todo_list", "todo_add"])
            self.assertEqual(oa[0]["type"], "function")
            self.assertEqual(oa[0]["function"]["parameters"]["required"], ["path", "pattern"])
            an = c.as_anthropic_tools(names=["fs_glob", "vm_stop"])
            self.assertEqual([t["name"] for t in an], ["fs_glob"])      # vm_stop privileged
            self.assertIn("input_schema", an[0])
            self.assertEqual([t["name"] for t in c.as_anthropic_tools(allowed_only=False)][-1], "vm_stop")

    def test_config_resolution(self):
        env = {"TOOLSERVER_URL": "http://10.0.0.5:7004/", "TOOLSERVER_OPERATOR_TOKEN": "t1",
               "HUGPY_AGENT_TOOLSERVER_ALLOW": "vm_list, sys_*", "HUGPY_AGENT_TOOLSERVER_DENY": "todo_add"}
        c = ToolserverClient(environ=env)
        self.assertEqual((c.url, c.token, c.token_source), ("http://10.0.0.5:7004", "t1", "env"))
        self.assertTrue(c.allowed("vm_list") and c.allowed("sys_run_cmd"))
        self.assertFalse(c.allowed("todo_add") or c.allowed("vm_stop"))
        self.assertIn("HUGPY_AGENT_TOOLSERVER_DENY", c.denial("todo_add"))
        self.assertIn("HUGPY_AGENT_TOOLSERVER_ALLOW=vm_stop", c.denial("vm_stop"))
        with mock.patch.object(tsc, "env_file_values", return_value=[]):   # no host env files
            alias = ToolserverClient(environ={"TOOLSERVER_TOKEN": "t2"})
        self.assertEqual((alias.url, alias.token), (tsc.DEFAULT_URL, "t2"))
        self.assertFalse(tsc.enabled({"HUGPY_AGENT_TOOLSERVER": "0"}))
        self.assertTrue(tsc.enabled({}))

    def test_hugpy_home_env_file_is_read(self):
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, "toolserver.env"), "w") as fh:
                fh.write("TOOLSERVER_URL=http://127.0.0.1:7004\nexport TOOLSERVER_OPERATOR_TOKEN='from-file'\n")
            c = ToolserverClient(environ={"HUGPY_HOME": home})
            self.assertEqual((c.token, c.token_source), ("from-file", "file"))

    def test_classification_defaults(self):
        cases = {"fs_glob": "readonly", "fs_read_file": "readonly", "comms_inbox": "readonly",
                 "todo_add": "mutating", "ledger_put": "mutating", "comms_ping": "mutating",
                 "fs_write_file": "privileged", "sys_run_cmd": "privileged", "vm_stop": "privileged",
                 "vm_list": "privileged", "vmpool_status": "privileged", "browser_go": "privileged",
                 "claude_oauth_set": "privileged", "claude_oauth_status": "readonly",
                 "ui_click_verify": "privileged", "ui_capture": "readonly"}
        for name, want in cases.items():
            self.assertEqual(tsc.classify(name), want, name)
        self.assertEqual(tsc.classify("db_query", {"sql": " SELECT 1"}), "readonly")
        self.assertEqual(tsc.classify("db_query", {"sql": "DELETE FROM x"}), "privileged")
        self.assertTrue(tsc.is_allowed("todo_add"))
        self.assertFalse(tsc.is_allowed("vm_stop"))
        self.assertTrue(tsc.is_allowed("vm_stop", allow=["*"]))
        self.assertFalse(tsc.is_allowed("vm_stop", allow=["*"], deny=["vm_*"]))


# ── CLI: hugpy-agent tools list|call|health ───────────────────────────────────
class CliTests(unittest.TestCase):
    def run_cli(self, argv):
        from hugpy_agent import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_health_list_call(self):
        with FakeToolserver() as fake:
            base = ["tools", "--url", fake.url, "--token", TOKEN]
            code, out, _ = self.run_cli(base[:1] + ["health"] + base[1:])
            self.assertEqual(code, 0)
            self.assertIn("OK", out)
            self.assertIn("tools=4", out)
            code, out, err = self.run_cli(base[:1] + ["list"] + base[1:])
            self.assertEqual(code, 0)
            self.assertIn("fs_glob", out)
            self.assertNotIn("vm_stop", out)          # privileged hidden by default
            self.assertIn("+1 privileged", err)
            code, out, _ = self.run_cli(base[:1] + ["list", "--all", "--json-out"] + base[1:])
            rows = json.loads(out)
            self.assertEqual([r["name"] for r in rows][-1], "vm_stop")
            self.assertFalse(rows[-1]["allowed"])
            code, out, _ = self.run_cli(base[:1] + ["call", "fs_glob", "--json",
                                                    '{"path":"/srv/vm_mgr/docs","pattern":"*.md"}'] + base[1:])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out), ["/srv/vm_mgr/docs/a.md", "/srv/vm_mgr/docs/b.md"])
            code, out, _ = self.run_cli(base[:1] + ["call", "vm_stop"] + base[1:])
            self.assertEqual(code, 1)
            self.assertIn("privileged", json.loads(out)["error"])
            self.assertEqual(len(fake.called("/ts/call")), 1)      # the denied call never left
            code, out, _ = self.run_cli(base[:1] + ["call", "vm_stop", "--allow-privileged"] + base[1:])
            self.assertEqual(code, 0)

    def test_auth_exit_codes(self):
        with FakeToolserver() as fake:
            code, _, err = self.run_cli(["tools", "health", "--url", fake.url, "--token", "bad"])
            self.assertEqual(code, 2)
            with mock.patch.object(tsc, "resolve_token", return_value=("", "none")):
                code, _, err = self.run_cli(["tools", "call", "todo_list", "--url", fake.url])
            self.assertEqual(code, 2)
            self.assertIn("TOOLSERVER_OPERATOR_TOKEN", err)

    def test_no_toolserver_flag_reaches_config(self):
        from hugpy_agent import cli
        args = mock.Mock(spec=[])
        for k in ("base", "model", "workspace", "max_steps", "tools_mode", "no_think", "policy_mode",
                  "audit_verbose", "task_source", "task_queue", "poll_interval", "agent_node",
                  "agent_central"):
            setattr(args, k, None)
        args.toolserver = False
        self.assertFalse(cli._cfg(args).toolserver)
        args.toolserver = None
        self.assertTrue(cli._cfg(args).toolserver)


# ── chat harness (AgentLoop registry) ────────────────────────────────────────
class ChatHarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_loop(self, fake, replies, **cfg_kw):
        from hugpy_agent.config import Config
        from hugpy_agent.loop import AgentLoop
        cfg = Config(workspace=self.ws, tools_mode="prompted", max_steps=6, model="fake-model",
                     policy_mode="auto", rag_enabled=False, audit_log="",
                     toolserver_url=fake.url, toolserver_token=TOKEN, **cfg_kw)
        gw = FakeGateway(replies)
        events = []
        loop = AgentLoop(cfg, gateway=gw, on_event=lambda k, *a: events.append((k, a)))
        return loop, gw, events

    def test_tool_defs_reach_model_and_result_round_trips(self):
        with FakeToolserver() as fake:
            loop, gw, events = self.make_loop(fake, [
                ts("fs_glob", {"path": "/srv/vm_mgr/docs", "pattern": "*.md"}),
                tc("final_answer", answer="ok")])
            self.assertIn("ts_call", loop.registry.names())
            ready = [a for k, a in events if k == "toolserver"]
            self.assertEqual(ready[0][0], "ready", ready)
            report = loop.run("glob the docs")
            self.assertEqual(report["outcome"], "done", report)
            # definitions reached the model: prompted tier puts the schema in the system prompt
            system = gw.calls[0][0][0]["content"]
            self.assertIn('"ts_call"', system)
            self.assertIn("ts_categories", system)
            # the call left with the token, and its result came back to the model
            path, body, headers = fake.called("/ts/call")[0]
            self.assertEqual(body["name"], "fs_glob")
            self.assertEqual(headers.get("X-Operator-Token"), TOKEN)
            second_turn = json.dumps(gw.calls[1][0])
            self.assertIn("/srv/vm_mgr/docs/a.md", second_turn)

    def test_privileged_tool_is_refused_as_data(self):
        with FakeToolserver() as fake:
            loop, gw, _ = self.make_loop(fake, [
                ts("vm_stop", {"name": "x"}),
                ts("todo_list", {}),            # a successful call so final_answer is admitted
                tc("final_answer", answer="ok")])
            self.assertEqual(loop.run("stop it")["outcome"], "done")
            self.assertEqual([c[1]["name"] for c in fake.called("/ts/call")], ["todo_list"])
            self.assertIn("HUGPY_AGENT_TOOLSERVER_ALLOW=vm_stop", gw.calls[1][0][-1]["content"])

    def test_explicit_allow_opens_privileged_tool(self):
        with FakeToolserver() as fake:
            loop, gw, _ = self.make_loop(fake, [
                ts("vm_stop", {"name": "x"}),
                tc("final_answer", answer="ok")], toolserver_allow=["vm_stop"])
            loop.run("stop it")
            self.assertEqual(fake.called("/ts/call")[0][1]["name"], "vm_stop")

    def test_policy_gate_sees_target_risk(self):
        from hugpy_agent.policy import decide, effective_risk
        from hugpy_agent.tools import RISK_DESTRUCTIVE, RISK_NETWORK, RISK_READONLY
        with FakeToolserver() as fake:
            loop, _, _ = self.make_loop(fake, [])
            spec = loop.registry.get("ts_call")
            self.assertEqual(effective_risk(spec, {"name": "fs_glob"}), RISK_READONLY)
            self.assertEqual(effective_risk(spec, {"name": "todo_add"}), RISK_NETWORK)
            self.assertEqual(effective_risk(spec, {"name": "vm_stop"}), RISK_DESTRUCTIVE)
            self.assertEqual(decide("readonly", spec, {"name": "fs_glob"}), "allow")
            self.assertEqual(decide("readonly", spec, {"name": "todo_add"}), "deny")
            self.assertEqual(decide("ask", spec, {"name": "todo_add"}), "ask")

    def test_disabled_and_unreachable_register_nothing(self):
        with FakeToolserver() as fake:
            loop, _, events = self.make_loop(fake, [], toolserver=False)
            self.assertNotIn("ts_call", loop.registry.names())
            self.assertEqual(fake.calls, [])                      # no probe when switched off
            from hugpy_agent.tools import toolserver as bridge
            self.assertEqual(bridge.specs(loop.cfg, on_event=lambda k, *a: events.append((k, a))), [])
            self.assertEqual([a[0] for k, a in events if k == "toolserver"], ["disabled"])
            # env switch (HUGPY_AGENT_TOOLSERVER=0) has the same effect as the flag
            events2 = []
            self.assertEqual(bridge.specs(self.make_loop(fake, [])[0].cfg,
                                          on_event=lambda k, *a: events2.append((k, a)),
                                          environ={"HUGPY_AGENT_TOOLSERVER": "0"}), [])
            self.assertEqual(events2[0][1][0], "disabled")
        from hugpy_agent.config import Config
        from hugpy_agent.loop import AgentLoop
        cfg = Config(workspace=self.ws, tools_mode="prompted", model="fake-model", rag_enabled=False,
                     audit_log="", toolserver_url="http://127.0.0.1:1", toolserver_token=TOKEN)
        events = []
        loop = AgentLoop(cfg, gateway=FakeGateway([]), on_event=lambda k, *a: events.append((k, a)))
        self.assertNotIn("ts_call", loop.registry.names())
        self.assertEqual([a[0] for k, a in events if k == "toolserver"], ["unavailable"])

    def test_flat_mode_registers_allowed_tools_without_shadowing_local(self):
        with FakeToolserver() as fake:
            loop, gw, events = self.make_loop(fake, [
                tc("todo_list", locus="keeper"), tc("final_answer", answer="ok")],
                toolserver_tools="flat")
            names = loop.registry.names()
            self.assertIn("todo_list", names)
            self.assertIn("todo_add", names)
            self.assertNotIn("vm_stop", names)                 # privileged: not registered
            self.assertEqual(loop.registry.get("fs_glob").risk_class, "readonly")
            # local fs_glob kept: its handler is the jailed one (workspace arg), not a POST
            loop.run("list")
            self.assertEqual(fake.called("/ts/call")[0][1], {"name": "todo_list",
                                                              "arguments": {"locus": "keeper"}})
            resp = gw.calls[1][0][-1]["content"]          # the tool_response the model saw
            self.assertIn("<tool_response>", resp)
            self.assertIn('\\"text\\": \\"x\\"', resp)
            self.assertIn("flat", [a[0] for k, a in events if k == "toolserver"])


# ── serve harness (service/) ─────────────────────────────────────────────────
class ServeHarnessTests(unittest.TestCase):
    def runtime(self, fake, tmp):
        from pathlib import Path
        from hugpy_agent.config import Config
        from hugpy_agent.service.runtime import Runtime
        tmp = Path(tmp)
        profiles = tmp / "profiles.json"
        profiles.write_text(json.dumps({"discover_clients": False, "profiles": {
            "local": {"base_url": "http://localhost:8000/v1", "model": "test", "context_length": 32768}}}))
        cfg = Config(workspace=str(tmp), audit_log="", max_steps=5, rag_enabled=False,
                     toolserver_url=fake.url, toolserver_token=TOKEN)
        return Runtime(cfg, tmp / "state", profiles)

    def test_state_tools_and_round_trip(self):
        with FakeToolserver() as fake, tempfile.TemporaryDirectory() as tmp:
            rt = self.runtime(fake, tmp)
            st = rt.state()["toolserver"]
            self.assertEqual((st["ok"], st["tool_count"], st["auth"], st["url"]), (True, 4, "ok", fake.url))
            cat = rt.tools()
            self.assertEqual(cat["meta_tools"], ["ts_categories", "ts_list", "ts_call"])
            by = {t["name"]: t for t in cat["tools"]}
            self.assertEqual((by["fs_glob"]["allowed"], by["fs_glob"]["risk"]), (True, "readonly"))
            self.assertEqual((by["vm_stop"]["allowed"], by["vm_stop"]["risk"]), (False, "destructive"))
            gw = FakeGateway([ts("todo_list", {}),
                              tc("final_answer", answer="listed")])
            with mock.patch("hugpy_agent.service.runtime.gateway", return_value=gw):
                sid = rt.create()["id"]
                rt.start(sid, "list todos")
                import time
                until = time.monotonic() + 10
                while time.monotonic() < until and rt.view(sid)["status"] not in ("done", "aborted"):
                    time.sleep(0.02)
            view = rt.view(sid)
            self.assertEqual(view["status"], "done", view)
            self.assertEqual(fake.called("/ts/call")[0][1]["name"], "todo_list")
            resp = gw.calls[1][0][-1]["content"]
            self.assertIn("<tool_response>", resp)
            self.assertIn('\\"text\\": \\"x\\"', resp)
            rt.shutdown()

    def test_api_tools_route(self):
        from urllib.request import Request, urlopen
        from hugpy_agent.service.http import handler
        with FakeToolserver() as fake, tempfile.TemporaryDirectory() as tmp:
            rt = self.runtime(fake, tmp)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler(rt, "svc-token"))
            th = threading.Thread(target=server.serve_forever, daemon=True)
            th.start()
            try:
                req = Request("http://127.0.0.1:%d/api/tools" % server.server_port,
                              headers={"Authorization": "Bearer svc-token"})
                with urlopen(req) as r:
                    doc = json.load(r)
                self.assertTrue(doc["toolserver"]["ok"])
                self.assertEqual(len(doc["tools"]), 4)
            finally:
                server.shutdown(); server.server_close(); th.join(); rt.shutdown()


# ── mct harness (A = claude -p): the MCP grant ───────────────────────────────
class MctGrantTests(unittest.TestCase):
    def test_grant_shape(self):
        from hugpy_agent.mct.claude_adapter import toolserver_grant
        with FakeToolserver() as fake:
            g = toolserver_grant(environ={}, client=client_for(fake))
            self.assertEqual(g["server"]["type"], "http")
            self.assertEqual(g["server"]["url"], fake.url + "/mcp?mode=flat")
            self.assertEqual(g["server"]["headers"]["X-Operator-Token"], TOKEN)
            self.assertEqual(g["allowed"], ["mcp__toolserver__fs_glob", "mcp__toolserver__todo_list",
                                            "mcp__toolserver__todo_add"])
            self.assertEqual(g["disallowed"], ["mcp__toolserver__vm_stop"])
            self.assertEqual(g["count"], 3)

    def test_grant_absent_when_off_or_unreachable(self):
        from hugpy_agent.mct.claude_adapter import toolserver_grant
        with FakeToolserver() as fake:
            self.assertIsNone(toolserver_grant(environ={"HUGPY_AGENT_TOOLSERVER": "0"},
                                               client=client_for(fake)))
            self.assertIsNone(toolserver_grant(environ={}, client=client_for(fake, token="bad")))
        self.assertIsNone(toolserver_grant(
            environ={}, client=ToolserverClient("http://127.0.0.1:1", TOKEN, environ={}, probe_timeout=1)))


# ── fleet TUI status line ────────────────────────────────────────────────────
class FleetTuiTests(unittest.TestCase):
    def test_status_bar_shows_toolserver_health(self):
        from hugpy_agent import fleet_console as fc
        from hugpy_agent import fleet_tui as tui

        class Screen:
            def __init__(self):
                self.text = []
            def getmaxyx(self): return (24, 160)
            def addnstr(self, y, x, value, limit, attr): self.text.append(value[:limit])
            def erase(self): pass
            def refresh(self): pass

        ui = tui.Console(Screen(), fc.Client("http://localhost:7002", "key", "operator"))
        ui.state["workers"] = [{"id": "w", "name": "gpu", "status": "online", "loaded_models": []}]
        ui.draw()
        self.assertTrue(any("toolserver: probing" in t for t in ui.screen.text))
        ui.events.put(("toolserver", {"ok": True, "tool_count": 198, "auth": "ok"}))
        ui.drain()
        ui.draw()
        self.assertTrue(any("toolserver ok (198 tools)" in t for t in ui.screen.text))
        ui.toolserver = {"ok": False, "tool_count": 0, "auth": "rejected"}
        ui.draw()
        self.assertTrue(any("toolserver auth rejected" in t for t in ui.screen.text))


if __name__ == "__main__":
    unittest.main()
