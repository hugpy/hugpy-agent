"""Gateway pure parts: base normalization, payload invariants, token math.
No network — resolve()/chat() are exercised live only by scripts/live_smoke.py."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import unittest

from hugpy_agent.gateway import (Gateway, candidate_routes, estimate_tokens,
                                 normalize_base, origin)


class RouteTests(unittest.TestCase):
    def test_api_suffix(self):
        chat, models = candidate_routes("https://dev.hugpy.ai/api")[0]
        self.assertEqual(chat, "https://dev.hugpy.ai/api/v1/chat/completions")
        self.assertEqual(models, "https://dev.hugpy.ai/api/v1/models")

    def test_v1_suffix(self):
        chat, models = candidate_routes("http://localhost:8081/v1")[0]
        self.assertEqual(chat, "http://localhost:8081/v1/chat/completions")
        self.assertEqual(models, "http://localhost:8081/v1/models")

    def test_api_v1_suffix(self):
        chat, _ = candidate_routes("https://dev.hugpy.ai/api/v1")[0]
        self.assertEqual(chat, "https://dev.hugpy.ai/api/v1/chat/completions")

    def test_bare_origin_gets_v1_and_api_fallback(self):
        cands = candidate_routes("https://dev.hugpy.ai")
        self.assertEqual(cands[0][0], "https://dev.hugpy.ai/v1/chat/completions")
        self.assertIn(("https://dev.hugpy.ai/api/v1/chat/completions",
                       "https://dev.hugpy.ai/api/v1/models"), cands)

    def test_bare_host_gets_scheme(self):
        self.assertEqual(normalize_base("dev.hugpy.ai"), "https://dev.hugpy.ai")

    def test_trailing_slash_stripped(self):
        self.assertEqual(normalize_base("https://x/api/"), "https://x/api")

    def test_origin(self):
        self.assertEqual(origin("https://dev.hugpy.ai/api/v1"),
                         "https://dev.hugpy.ai")


class PayloadTests(unittest.TestCase):
    def test_max_chunks_always_one(self):
        """The continuation-leak killer must ride EVERY chat payload."""
        gw = Gateway("https://dev.hugpy.ai/api", model="m")
        p = gw.build_payload([{"role": "user", "content": "hi"}])
        self.assertEqual(p["max_chunks"], 1)
        p = gw.build_payload([], stream=False, tools=[{"type": "function"}])
        self.assertEqual(p["max_chunks"], 1)
        self.assertIn("tools", p)

    def test_model_default(self):
        gw = Gateway("https://x/api", model="cfg-model")
        self.assertEqual(gw.build_payload([])["model"], "cfg-model")
        self.assertEqual(gw.build_payload([], model="override")["model"],
                         "override")

    def test_no_tools_key_when_absent(self):
        gw = Gateway("https://x/api")
        self.assertNotIn("tools", gw.build_payload([]))


class TokenTests(unittest.TestCase):
    def test_heuristic(self):
        # //3, not //4, since the 2026-08-06 compaction fix: agent transcripts
        # are JSON-escaped tool dumps (~2.5-3 chars/token) and //4 undercounted
        # until the wire prompt overflowed a 32k slot (see estimate_tokens).
        self.assertEqual(estimate_tokens("x" * 402), 134)
        self.assertEqual(estimate_tokens(""), 1)
        self.assertEqual(estimate_tokens(None), 1)


if __name__ == "__main__":
    unittest.main()
