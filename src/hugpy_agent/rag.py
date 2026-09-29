"""Embed-RAG memory — SQLite vector index + pure-python cosine (P2.6,
design §3.4).

The markdown fact store (memory.py) stays the SOURCE OF TRUTH; this module
is only an INDEX over it: on `remember` the fact is also embedded (fleet
`/api/ml/embed`) and stored as `(id, text, vector_blob)` in
`<workspace>/.hugpy_agent/memory_vectors.db`, and `recall(query, k)` ranks
stored facts by cosine similarity. Losing or corrupting the vector DB loses
nothing but speed — the facts themselves live in `memory/*.md`.

Doctrine:
  * Graceful degradation — every embed/store failure is returned as an
    error STRING (or raised as EmbedUnavailable inside the injected
    embedder and converted here); a dead embed endpoint disables recall,
    it never breaks `remember` or a run.
  * Stdlib only — the cosine and the float packing are pure python
    (struct + math, no numpy); memory-scale stores (hundreds of facts)
    make a full linear scan the right amount of machinery.
  * The embedder is INJECTED (callable text -> list[float]), so the store
    and ranking are fully unit-testable offline with a stub.
"""
from __future__ import annotations

import os
import sqlite3
import struct

# Bound on any single embed round-trip. Matches the gateway's metadata-call
# discipline (the /api/models catalog load uses the same 30s): run-start
# auto-recall must degrade within seconds when the endpoint is dead, never
# hang a run for the full 300s chat timeout.
EMBED_TIMEOUT = 30


class EmbedUnavailable(Exception):
    """The embed endpoint could not produce a vector (endpoint down, no
    embedding model in the catalog, malformed response). Raised by
    embedders; RagIndex converts it to an error string (errors-as-data)."""


# ── pure-python vector math ─────────────────────────────────────────────────
def pack_vector(vec: list[float]) -> bytes:
    """list[float] -> little-endian float32 blob (4 bytes/dim)."""
    return struct.pack("<%df" % len(vec), *vec)


def unpack_vector(blob: bytes) -> list[float]:
    return list(struct.unpack("<%df" % (len(blob) // 4), blob))


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity, pure python. Mismatched dimensions (e.g. the
    embedding model changed between sessions) or a zero vector score 0.0 —
    incomparable vectors must never fake similarity."""
    if not a or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


# ── the store ───────────────────────────────────────────────────────────────
class VectorStore:
    """SQLite-backed `(id, text, vector_blob)` rows + linear cosine top-k.
    No vector-DB dependency: at workspace-memory scale a full scan is
    microseconds, and SQLite gives us durability + concurrent readers (the
    journal already set that precedent)."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS vectors ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " text TEXT NOT NULL,"
            " vector_blob BLOB NOT NULL)")
        self._conn.commit()

    def add(self, text: str, vector: list[float]) -> int:
        cur = self._conn.execute(
            "INSERT INTO vectors (text, vector_blob) VALUES (?, ?)",
            (text, pack_vector([float(x) for x in vector])))
        self._conn.commit()
        return int(cur.lastrowid)

    def count(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM vectors").fetchone()[0])

    def top_k(self, vector: list[float], k: int = 5) -> list[dict]:
        """The k rows nearest `vector` by cosine, best first:
        [{id, text, score}, ...]."""
        query = [float(x) for x in vector]
        scored = []
        for rid, text, blob in self._conn.execute(
                "SELECT id, text, vector_blob FROM vectors"):
            scored.append((cosine(query, unpack_vector(blob)), rid, text))
        scored.sort(key=lambda t: (-t[0], t[1]))   # ties: oldest fact first
        return [{"id": rid, "text": text, "score": round(score, 6)}
                for score, rid, text in scored[:max(1, int(k))]]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


# ── store + injected embedder ───────────────────────────────────────────────
class RagIndex:
    """VectorStore + an injected embedder (callable text -> list[float]).
    Both public methods follow errors-as-data: `index` returns an error
    string ('' = success), `recall` returns (matches, error) where matches
    is None exactly when RAG is unavailable (and [] when it is healthy but
    has nothing stored)."""

    def __init__(self, path: str, embedder):
        self.store = VectorStore(path)
        self.embedder = embedder

    def index(self, text: str) -> str:
        """Embed + store one fact. Returns '' on success, else the reason —
        the caller decides whether that is a WARN (remember: the markdown
        was already written) or worth surfacing."""
        try:
            vec = self.embedder(text)
        except Exception as exc:  # noqa: BLE001 — degrade, never raise
            return "embed failed: %s" % exc
        if not vec:
            return "embed returned an empty vector"
        try:
            self.store.add(text, vec)
        except Exception as exc:
            return "vector store write failed: %s" % exc
        return ""

    def recall(self, query: str, k: int = 5):
        """(matches, error). An EMPTY store short-circuits BEFORE embedding:
        a fresh workspace must make zero embed traffic at run start (the
        same no-unasked-traffic doctrine as the native-tools probe)."""
        try:
            if self.store.count() == 0:
                return [], ""
        except Exception as exc:
            return None, "vector store unavailable: %s" % exc
        try:
            vec = self.embedder(query)
        except Exception as exc:  # noqa: BLE001
            return None, str(exc) or type(exc).__name__
        if not vec:
            return None, "embed returned an empty vector"
        try:
            return self.store.top_k(vec, k), ""
        except Exception as exc:
            return None, "vector store read failed: %s" % exc


# ── the live embedder (fleet transport) ─────────────────────────────────────
def fleet_embedder(gateway, workspace: str):
    """Embedder over the Phase-1.5 fleet transport: model_key resolved from
    the /api/models catalog by the `embed` task set (feature-extraction /
    sentence-similarity — all-minilm on the live fleet), vector via
    POST /api/ml/embed {text, model_key}. Every failure raises
    EmbedUnavailable with the fleet's own words; RagIndex turns that into
    errors-as-data. Bounded by EMBED_TIMEOUT, not the 300s chat timeout."""
    from .tools.fleet import FleetTools
    ft = FleetTools(gateway, workspace)   # own catalog cache, one GET per run

    def embed(text: str) -> list[float]:
        mk, err = ft.resolve_model_key("embed", "")
        if err:
            raise EmbedUnavailable(err)
        res, err = ft._ml_json("embed", {"text": text, "model_key": mk},
                               timeout=EMBED_TIMEOUT)
        if err:
            raise EmbedUnavailable(err)
        embs = res.get("embeddings") or []
        vec = embs[0] if embs and isinstance(embs[0], list) else embs
        if not isinstance(vec, list) or not vec:
            raise EmbedUnavailable("embed returned no vector")
        return [float(x) for x in vec]

    return embed


def default_vectors_path(workspace: str) -> str:
    return os.path.join(os.path.realpath(workspace), ".hugpy_agent",
                        "memory_vectors.db")
