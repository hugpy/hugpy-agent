"""Native-probe traffic discipline (2026-07-14 dev incident regression tests).

The incident: the probe fired by default and its result was cached
per-workspace, so every fresh workspace hammered dev's /v1 with the ping
probe (~58s of GPU each, because the seam drops max_chunks). These tests pin
the fixed contract:
  * default config emits ZERO probe traffic,
  * auto/native probe exactly once per box (file cache, XDG-aware),
  * the cache honors its TTL and tolerates corruption (one re-probe, no loop).
"""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import json
import os
import tempfile
import time
import unittest

from hugpy_agent import adapter as adapter_mod
from hugpy_agent.adapter import cached_probe_native, probe_cache_path
from hugpy_agent.config import Config, load_config
from hugpy_agent.gateway import ChatResult
from hugpy_agent.journal import Journal
from hugpy_agent.loop import AgentLoop
from hugpy_agent.memory import Memory
from hugpy_agent.tools import build_registry

from helpers import FakeGateway, tc

# A probe answer that proves native support (message-level tool_calls).
NATIVE_YES = ChatResult(ok=True, text="", native_tool_calls=[
    {"function": {"name": "ping", "arguments": "{}"}}])
FINAL = tc("final_answer", answer="done")


def probe_calls(gw: FakeGateway):
    """The subset of chat() calls that are probes (carry the ping tool)."""
    out = []
    for messages, kw in gw.calls:
        tools = kw.get("tools") or []
        if any((t.get("function") or {}).get("name") == "ping" for t in tools):
            out.append((messages, kw))
    return out


class ProbeTrafficTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
        # Redirect the per-box cache into the sandbox so tests never touch
        # (or depend on) the real ~/.cache.
        self._old_xdg = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = os.path.join(self.ws, "xdg")

    def tearDown(self):
        if self._old_xdg is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._old_xdg
        self.tmp.cleanup()

    def make_loop(self, replies, tools_mode=None):
        cfg = Config(workspace=self.ws, model="fake-model")
        if tools_mode is not None:
            cfg.tools_mode = tools_mode
        gw = FakeGateway(replies)
        db = os.path.join(self.ws, ".hugpy_agent", "journal.db")
        loop = AgentLoop(cfg, gateway=gw,
                         registry=build_registry(self.ws, gw, Memory(self.ws)),
                         journal=Journal(db), memory=Memory(self.ws))
        return loop, gw

    # ── the incident regression: silence by default ─────────────────────
    def test_default_mode_is_prompted(self):
        self.assertEqual(Config().tools_mode, "prompted")
        cfg = load_config(environ={"HUGPY_WORKSPACE": self.ws})
        self.assertEqual(cfg.tools_mode, "prompted")

    def test_default_config_emits_no_probe(self):
        """Under default config NO code path may emit the ping probe."""
        loop, gw = self.make_loop([FINAL])          # tools_mode = default
        report = loop.run("t")
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(probe_calls(gw), [])
        # and no chat call carried a tools payload at all (prompted tier)
        self.assertTrue(all(not kw.get("tools") for _, kw in gw.calls))
        self.assertFalse(os.path.exists(probe_cache_path()))

    def test_explicit_prompted_and_constrained_never_probe(self):
        for mode in ("prompted", "constrained"):
            loop, gw = self.make_loop(
                ['{"name": "final_answer", "arguments": {"answer": "d"}}'],
                tools_mode=mode)
            loop.run("t")
            self.assertEqual(probe_calls(gw), [], "mode %s probed" % mode)

    def test_chat_repl_default_emits_no_probe(self):
        loop, gw = self.make_loop([FINAL])
        loop.start_chat()
        self.assertEqual(probe_calls(gw), [])

    # ── auto/native: exactly one probe per box ───────────────────────────
    def test_auto_probes_once_then_uses_file_cache(self):
        loop, gw = self.make_loop([NATIVE_YES], tools_mode="auto")
        loop._maybe_probe_native()
        self.assertEqual(len(probe_calls(gw)), 1)
        self.assertEqual(loop.adapter.mode, "native")
        self.assertTrue(os.path.exists(probe_cache_path()))

        # A second loop in a DIFFERENT workspace on the same box: cache hit,
        # zero traffic (per-workspace journals no longer matter).
        ws2 = os.path.join(self.ws, "other-ws")
        os.makedirs(ws2)
        cfg2 = Config(workspace=ws2, model="fake-model", tools_mode="auto")
        gw2 = FakeGateway([])
        loop2 = AgentLoop(cfg2, gateway=gw2,
                          registry=build_registry(ws2, gw2, Memory(ws2)),
                          journal=Journal(os.path.join(ws2, "j.db")),
                          memory=Memory(ws2))
        loop2._maybe_probe_native()
        self.assertEqual(gw2.calls, [])              # no probe, no anything
        self.assertEqual(loop2.adapter.mode, "native")

    def test_auto_probe_no_support_stays_prompted(self):
        loop, gw = self.make_loop(["I cannot call functions."],
                                  tools_mode="auto")
        loop._maybe_probe_native()
        self.assertEqual(len(probe_calls(gw)), 1)
        self.assertEqual(loop.adapter.mode, "prompted")

    def test_native_mode_probes_and_fails_closed(self):
        """Explicit native against a seam that drops tools must fall back:
        honoring it blindly costs ~a minute of GPU per step today."""
        events = []
        loop, gw = self.make_loop(["no tools here"], tools_mode="native")
        loop.on_event = lambda kind, *a: events.append((kind, a))
        loop._maybe_probe_native()
        self.assertEqual(loop.adapter.mode, "prompted")
        self.assertTrue(any(k == "mode" and "prompted" in a[0]
                            for k, a in events))

    def test_probe_max_tokens_capped(self):
        """The probe must stay tiny: max_tokens <= 16 (each probe token is
        GPU time on a seam that ignores our max_chunks)."""
        loop, gw = self.make_loop([NATIVE_YES], tools_mode="auto")
        loop._maybe_probe_native()
        _, kw = probe_calls(gw)[0]
        self.assertLessEqual(kw.get("max_tokens", 999), 16)

    # ── cache file semantics ─────────────────────────────────────────────
    def test_ttl_expiry_reprobes(self):
        path = probe_cache_path()
        gw = FakeGateway([NATIVE_YES])
        gw.base = "fake://"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        stale = {"fake://|fake-model": {"native": False,
                                        "ts": time.time() - 8 * 24 * 3600}}
        with open(path, "w") as fh:
            json.dump(stale, fh)
        self.assertTrue(cached_probe_native(gw, "fake-model"))
        self.assertEqual(len(probe_calls(gw)), 1)    # stale -> re-probed
        with open(path) as fh:                        # and cache refreshed
            data = json.load(fh)
        self.assertTrue(data["fake://|fake-model"]["native"])

    def test_fresh_cache_hit_no_traffic(self):
        path = probe_cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"fake://|fake-model": {"native": True,
                                              "ts": time.time()}}, fh)
        gw = FakeGateway([])
        self.assertTrue(cached_probe_native(gw, "fake-model"))
        self.assertEqual(gw.calls, [])

    def test_corrupt_cache_tolerated_and_rewritten(self):
        path = probe_cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("{not json at all")
        gw = FakeGateway([NATIVE_YES])
        self.assertTrue(cached_probe_native(gw, "fake-model"))
        self.assertEqual(len(probe_calls(gw)), 1)    # exactly ONE re-probe
        with open(path) as fh:                        # file healed
            data = json.load(fh)
        self.assertIn("fake://|fake-model", data)
        # ...and a followup call hits the healed cache, no traffic:
        gw2 = FakeGateway([])
        self.assertTrue(cached_probe_native(gw2, "fake-model"))
        self.assertEqual(gw2.calls, [])

    def test_malformed_entry_shape_reprobes(self):
        path = probe_cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"fake://|fake-model": "yes???"}, fh)
        gw = FakeGateway([NATIVE_YES])
        self.assertTrue(cached_probe_native(gw, "fake-model"))
        self.assertEqual(len(probe_calls(gw)), 1)


if __name__ == "__main__":
    unittest.main()
