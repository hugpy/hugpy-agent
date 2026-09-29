"""Embed-RAG memory (P2.6): pure-python cosine store + top-k, the recall
tool contract, remember's index hook with graceful degradation, and
run-start auto-recall pinning. Offline — deterministic stub embedders only,
no network."""
import _bootstrap  # noqa: F401  (src-path shim; no-op when installed)
import json
import os
import tempfile
import unittest

from hugpy_agent.adapter import Adapter
from hugpy_agent.config import Config
from hugpy_agent.journal import Journal
from hugpy_agent.loop import AgentLoop
from hugpy_agent.memory import Memory
from hugpy_agent.rag import (RagIndex, VectorStore, cosine,
                             default_vectors_path, pack_vector, unpack_vector)
from hugpy_agent.tools import RISK_READONLY, ToolContext, build_registry

from helpers import FakeGateway, tc

# Deterministic stub embedder: one axis per keyword. Facts/queries in these
# tests always contain exactly one keyword, so nearest-by-cosine is exact.
_AXES = ("solar", "backup", "discord")

FACTS = ["the solar eval runs at 02:00 UTC before the backup window",
         "nightly backup fires at 03:30 UTC",
         "the discord session token lives in .env"]


def kw_embed(text: str) -> list:
    t = text.lower()
    return [1.0 if w in t else 0.0 for w in _AXES]


def dead_embed(text: str):
    raise RuntimeError("embed endpoint down: HTTP 503")


class CountingEmbedder:
    def __init__(self, inner=kw_embed):
        self.calls = 0
        self.inner = inner

    def __call__(self, text):
        self.calls += 1
        return self.inner(text)


class StoreTests(unittest.TestCase):
    """Pure store + math: known geometry in, known ranking out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = default_vectors_path(self.tmp.name)
        self.store = VectorStore(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_pack_roundtrip(self):
        vec = [0.5, -1.25, 3.0]
        out = unpack_vector(pack_vector(vec))
        for a, b in zip(vec, out):
            self.assertAlmostEqual(a, b, places=5)

    def test_cosine_geometry_and_edges(self):
        self.assertAlmostEqual(cosine([1, 0], [2, 0]), 1.0, places=6)
        self.assertAlmostEqual(cosine([1, 0], [0, 1]), 0.0, places=6)
        # zero vector and dimension mismatch must never fake similarity
        self.assertEqual(cosine([0, 0], [1, 0]), 0.0)
        self.assertEqual(cosine([1, 0], [1, 0, 0]), 0.0)
        self.assertEqual(cosine([], []), 0.0)

    def test_top_k_nearest_by_cosine(self):
        # cos with [1,0]: exact=1.0, near=0.8, orthogonal=0.0
        self.store.add("exact", [1.0, 0.0])
        self.store.add("near", [0.8, 0.6])
        self.store.add("orthogonal", [0.0, 1.0])
        top = self.store.top_k([1.0, 0.0], k=2)
        self.assertEqual([m["text"] for m in top], ["exact", "near"])
        self.assertAlmostEqual(top[0]["score"], 1.0, places=5)
        self.assertAlmostEqual(top[1]["score"], 0.8, places=5)
        self.assertEqual(len(self.store.top_k([1.0, 0.0], k=10)), 3)

    def test_rows_survive_reopen(self):
        rid = self.store.add("durable", [1.0, 0.0])
        self.store.close()
        again = VectorStore(self.path)
        self.assertEqual(again.count(), 1)
        top = again.top_k([1.0, 0.0], k=1)
        self.assertEqual(top[0]["id"], rid)
        self.assertEqual(top[0]["text"], "durable")
        again.close()


class RagIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = default_vectors_path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_recall_ranks_related_fact_first(self):
        rag = RagIndex(self.path, kw_embed)
        for fact in FACTS:
            self.assertEqual(rag.index(fact), "")
        matches, err = rag.recall("when does the solar eval run", k=2)
        self.assertEqual(err, "")
        self.assertIn("solar eval", matches[0]["text"])
        self.assertEqual(len(matches), 2)

    def test_empty_store_short_circuits_before_embedding(self):
        emb = CountingEmbedder()
        rag = RagIndex(self.path, emb)
        matches, err = rag.recall("anything", k=5)
        self.assertEqual((matches, err), ([], ""))
        self.assertEqual(emb.calls, 0)   # zero embed traffic when empty

    def test_dead_embedder_disables_recall_as_data(self):
        RagIndex(self.path, kw_embed).index(FACTS[0])   # non-empty store
        rag = RagIndex(self.path, dead_embed)
        matches, err = rag.recall("solar", k=5)
        self.assertIsNone(matches)
        self.assertIn("embed endpoint down", err)

    def test_index_failure_returns_reason_writes_nothing(self):
        rag = RagIndex(self.path, dead_embed)
        err = rag.index("a fact")
        self.assertIn("embed failed", err)
        self.assertIn("embed endpoint down", err)
        self.assertEqual(rag.store.count(), 0)


class RecallToolTests(unittest.TestCase):
    """The registered tool surface: result shape, risk class, and the
    remember hook (markdown first, vectors best-effort)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
        self.rag = RagIndex(default_vectors_path(self.ws), kw_embed)
        self.memory = Memory(self.ws)
        self.reg = build_registry(self.ws, FakeGateway([]), self.memory,
                                  rag=self.rag)
        self.events = []
        self.ctx = ToolContext(
            on_event=lambda *a, **k: self.events.append(a))

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, tool, **args):
        spec = self.reg.get(tool)
        return json.loads(self.reg.execute(spec, args, self.ctx))

    def test_recall_registered_readonly(self):
        spec = self.reg.get("recall")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.risk_class, RISK_READONLY)

    def test_no_rag_no_recall_tool(self):
        reg = build_registry(self.ws, FakeGateway([]), self.memory)
        self.assertIsNone(reg.get("recall"))

    def test_recall_result_shape_best_first(self):
        for fact in FACTS:
            self.rag.index(fact)
        out = self._run("recall", query="the backup schedule", k=2)
        self.assertEqual(out["count"], 2)
        self.assertEqual(sorted(out["matches"][0]), ["score", "text"])
        self.assertIn("backup", out["matches"][0]["text"])
        self.assertGreaterEqual(out["matches"][0]["score"],
                                out["matches"][1]["score"])

    def test_recall_disabled_is_structured_data(self):
        self.rag.index(FACTS[0])          # non-empty: forces an embed attempt
        self.rag.embedder = dead_embed    # then the endpoint dies
        out = self._run("recall", query="solar")
        self.assertTrue(out["error"].startswith("RAG disabled:"))
        self.assertIn("embed endpoint down", out["error"])

    def test_remember_writes_markdown_and_indexes(self):
        out = self._run("remember", fact=FACTS[0], title="solar eval time")
        path = os.path.join(self.ws, "memory", out["remembered"])
        self.assertTrue(os.path.exists(path))
        self.assertEqual(self.rag.store.count(), 1)
        matches, err = self.rag.recall("solar", k=1)
        self.assertEqual(err, "")
        self.assertIn("solar eval", matches[0]["text"])

    def test_remember_survives_dead_embedder(self):
        """Vectors are an index, files are truth: the markdown write must
        land and the tool result stay a normal receipt when embedding
        fails — the failure surfaces only as a rag_error event."""
        self.rag.embedder = dead_embed
        out = self._run("remember", fact=FACTS[1], title="backup time")
        self.assertNotIn("error", out)
        path = os.path.join(self.ws, "memory", out["remembered"])
        self.assertTrue(os.path.exists(path))
        self.assertEqual(self.rag.store.count(), 0)
        rag_events = [e for e in self.events if e[0] == "rag_error"]
        self.assertEqual(len(rag_events), 1)
        self.assertEqual(rag_events[0][1], "index")


class AutoRecallTests(unittest.TestCase):
    """Run-start pinning (design §3.4) and its degrade paths."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
        self.cfg = Config(workspace=self.ws, tools_mode="prompted",
                          model="fake-model", policy_mode="auto")
        self.events = []

    def tearDown(self):
        self.tmp.cleanup()

    def _loop(self, replies, embedder=kw_embed):
        gw = FakeGateway(replies)
        journal = Journal(os.path.join(self.ws, ".hugpy_agent", "journal.db"))
        loop = AgentLoop(self.cfg, gateway=gw, journal=journal,
                         adapter=Adapter("prompted"), memory=Memory(self.ws),
                         on_event=lambda *a, **k: self.events.append(a))
        # swap the live fleet embedder for the deterministic stub
        loop.rag = RagIndex(default_vectors_path(self.ws), embedder)
        return loop, journal

    def test_run_start_pins_recall_into_system_context(self):
        loop, journal = self._loop([])
        for fact in FACTS:
            loop.rag.index(fact)
        rid = loop.prepare_run("verify the solar eval results")
        system = journal.wire_messages(rid)[0]
        self.assertEqual(system["role"], "system")
        self.assertIn("Workspace memory recalled", system["content"])
        self.assertIn("solar eval runs at 02:00", system["content"])

    def test_empty_store_pins_nothing(self):
        loop, journal = self._loop([])
        rid = loop.prepare_run("some task")
        self.assertNotIn("Workspace memory recalled",
                         journal.wire_messages(rid)[0]["content"])

    def test_dead_embed_noops_with_event_and_run_completes(self):
        """A dead embed endpoint must never delay or break a run: no pin,
        one rag_error event, and the run itself finishes normally."""
        loop, journal = self._loop([tc("final_answer", answer="done")],
                                   embedder=dead_embed)
        # non-empty store, so auto-recall genuinely attempts the embed
        RagIndex(default_vectors_path(self.ws), kw_embed).index(FACTS[0])
        report = loop.run("check the solar eval")
        self.assertEqual(report["outcome"], "done")
        system = journal.wire_messages(report["run_id"])[0]
        self.assertNotIn("Workspace memory recalled", system["content"])
        rag_events = [e for e in self.events if e[0] == "rag_error"]
        self.assertEqual(len(rag_events), 1)
        self.assertEqual(rag_events[0][1], "auto_recall")

    def test_rag_disabled_by_config(self):
        cfg = Config(workspace=self.ws, tools_mode="prompted",
                     model="fake-model", rag_enabled=False)
        loop = AgentLoop(cfg, gateway=FakeGateway([]),
                         adapter=Adapter("prompted"))
        self.assertIsNone(loop.rag)
        self.assertNotIn("recall", loop.registry.names())

    def test_rag_default_on_registers_recall(self):
        loop = AgentLoop(self.cfg, gateway=FakeGateway([]),
                         adapter=Adapter("prompted"))
        self.assertIsNotNone(loop.rag)
        self.assertIn("recall", loop.registry.names())


if __name__ == "__main__":
    unittest.main()
