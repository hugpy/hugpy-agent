"""TUI loci: the toolserver registry first, the JSON file as fallback."""
import _bootstrap  # noqa: F401
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from test_tui_hardening import Client, Screen

from hugpy_agent.tui import app as app_mod
from hugpy_agent.tui import loci
from hugpy_agent.tui.views import theme

HERE = lambda host: host == "192.168.1.100"          # noqa: E731 — "this machine" for the tests

KEEPER = {"locus": "keeper", "kind": "station", "status": "active", "endpoint": "vm_mgr@192.168.1.100",
          "pointer": {"serve_url": "http://127.0.0.1:9124",
                      "ssh": {"host": "192.168.1.100", "user": "vm_mgr", "port": 22, "key": ""}}}
REMOTE = {"locus": "hs-fresh", "kind": "station", "status": "active", "endpoint": "ubuntu@10.237.23.104:2222",
          "pointer": {"serve_url": "http://127.0.0.1:9125"}}


class RegistryLociTests(unittest.TestCase):
    def test_serve_on_this_host_is_used_as_is(self):
        self.assertEqual(loci.registry_loci([KEEPER], HERE),
                         [{"locus": "keeper", "serve": "http://127.0.0.1:9124", "source": "registry"}])

    def test_serve_on_another_host_is_its_port_behind_the_ssh_endpoint(self):
        self.assertEqual(loci.registry_loci([REMOTE], HERE),
                         [{"locus": "hs-fresh", "ssh": "ubuntu@10.237.23.104", "port": 9125,
                           "ssh_port": 2222, "source": "registry"}])

    def test_rows_that_publish_no_reachable_serve_are_left_out(self):
        rows = [{"locus": "no-pointer", "status": "active", "endpoint": "a@10.0.0.9", "pointer": None},
                {"locus": "no-serve", "status": "active", "endpoint": "a@10.0.0.9", "pointer": {"ssh": {}}},
                {"locus": "archived", "status": "archived", "endpoint": "a@192.168.1.100",
                 "pointer": {"serve_url": "http://127.0.0.1:9124"}},
                # a loopback address with no host to place it on is not guessed to be here
                {"locus": "nowhere", "status": "active", "endpoint": "ae",
                 "pointer": {"serve_url": "http://127.0.0.1:9124"}},
                "junk"]
        self.assertEqual(loci.registry_loci(rows, HERE), [])
        self.assertEqual(loci.registry_loci({"error": "denied"}, HERE), [])

    def test_a_routable_serve_url_needs_no_tunnel(self):
        row = {"locus": "lan", "status": "active", "endpoint": "a@10.0.0.9",
               "pointer": {"serve_url": "https://serve.example:9443/"}}
        self.assertEqual(loci.registry_loci([row], HERE),
                         [{"locus": "lan", "serve": "https://serve.example:9443", "source": "registry"}])

    def test_merge_keeps_file_loci_the_registry_does_not_name(self):
        registry = loci.registry_loci([KEEPER], HERE)
        local = [{"locus": "keeper", "serve": "http://127.0.0.1:1"}, {"locus": "hugpy", "serve": "http://127.0.0.1:9125"}]
        self.assertEqual([e["locus"] for e in loci.merge(registry, local)], ["keeper", "hugpy"])
        self.assertEqual(loci.merge(registry, local)[0]["serve"], "http://127.0.0.1:9124")

    def test_fetch_reads_loci_list_and_raises_on_a_refusal(self):
        class Tools:
            def __init__(self, reply): self.reply, self.calls = reply, []
            def call(self, name, args, timeout=None):
                self.calls.append((name, args))
                return self.reply
        tools = Tools([KEEPER])
        with patch.object(loci, "_is_local", HERE):
            self.assertEqual([e["locus"] for e in loci.fetch_registry(tools)], ["keeper"])
        self.assertEqual(tools.calls, [("loci_list", {"kind": "", "status": "active"})])
        with self.assertRaises(RuntimeError):
            loci.fetch_registry(Tools({"error": "denied", "denied": True}))

    def test_an_explicit_file_or_a_disabled_toolserver_turns_the_registry_off(self):
        self.assertTrue(loci.registry_enabled({}))
        self.assertFalse(loci.registry_enabled({"HUGPY_TUI_LOCI": "/tmp/x.json"}))
        self.assertFalse(loci.registry_enabled({"HUGPY_AGENT_TOOLSERVER": "0"}))


class TunnelTests(unittest.TestCase):
    def test_a_registry_ssh_port_reaches_the_ssh_command(self):
        class Proc:
            returncode = None
            def poll(self): return None
            def terminate(self): pass
        seen = []
        with patch.object(loci.subprocess, "Popen", lambda cmd, **kw: seen.append(cmd) or Proc()), \
                patch.object(loci, "probe", lambda base, timeout=0.5: {}):
            base = loci.Tunnels().base_for({"locus": "hs-fresh", "ssh": "ubuntu@10.237.23.104",
                                            "port": 9125, "ssh_port": 2222})
        self.assertTrue(base.startswith("http://127.0.0.1:"))
        self.assertEqual(seen[0][-3:], ["-p", "2222", "ubuntu@10.237.23.104"])
        self.assertTrue(seen[0][seen[0].index("-L") + 1].endswith(":127.0.0.1:9125"))


class AdoptTests(unittest.TestCase):
    def make(self, base="http://127.0.0.1:9124"):
        client = Client()
        client.base = base
        return app_mod.App(Screen(), client, theme=theme.plain())

    def test_the_serve_we_are_on_takes_its_registry_name(self):
        ui = self.make()
        self.assertEqual((ui.active_locus, [e["locus"] for e in ui.loci]), ("here", ["here"]))
        ui.adopt_loci(loci.registry_loci([KEEPER, REMOTE], HERE))
        self.assertEqual((ui.active_locus, [e["locus"] for e in ui.loci]), ("keeper", ["keeper", "hs-fresh"]))

    def test_a_serve_the_registry_does_not_name_stays_listed(self):
        ui = self.make("http://127.0.0.1:9126")
        ui.adopt_loci(loci.registry_loci([KEEPER], HERE))
        self.assertEqual((ui.active_locus, [e["locus"] for e in ui.loci]), ("here", ["here", "keeper"]))

    def test_file_loci_fill_in_behind_the_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "loci.json")
            with open(path, "w") as fh:
                json.dump([{"locus": "hugpy", "serve": "http://127.0.0.1:9125"}], fh)
            with patch.object(loci, "PATH", path):
                ui = self.make()
                ui.adopt_loci(loci.registry_loci([KEEPER], HERE))
        self.assertEqual([e["locus"] for e in ui.loci], ["keeper", "hugpy"])

    def test_refresh_does_nothing_when_the_registry_is_off(self):
        ui = self.make()
        with patch.object(loci, "fetch_registry", side_effect=AssertionError("must not be read")):
            ui.refresh_loci()                # HUGPY_TUI_LOCI is set by _bootstrap


if __name__ == "__main__":
    unittest.main()
