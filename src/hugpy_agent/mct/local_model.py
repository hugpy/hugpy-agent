"""B's local model — the ranking/summarizing brain, never the security boundary.

Design ref: §11.2 (semantic relevance), §11.4 (model-drafted products), §20.6
(model separation inside B), invariant 9 ("the local model may rank and
summarize; code and OS controls authorize"). §21 (``retrieval``/``compaction``).

This module defines the :class:`LocalModel` interface B uses for semantic
retrieval, summary drafting, extraction, and conflict detection. The published
``hugpy_agent`` supplies the real one — its ``gateway.Gateway`` (chat) and
``rag`` embedder (``fleet_embedder`` / ``cosine``) — behind this exact shape
(design §20.3). That wiring lands in Phase 4 together with resolving the
package-coexistence issue; here the default is a fully deterministic, offline
model so the whole engine is testable and reproducible without a fleet.

Whatever backs it, three things stay true (the Phase-3 exit condition):
the model influences only ``R_i`` ranking and *drafts* derived products with
provenance — it cannot authorize, cannot delete originals, and cannot produce an
untraceable fact.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol, runtime_checkable

from .retrieval import tokenize

EMBED_DIM = 64

# Tiny synonym / stem normalization so the offline embedder captures a little
# meaning beyond exact tokens (evicted≈removed≈eviction). A real embedding model
# replaces this wholesale; the interface is unchanged.
_SYNONYMS = {
    "evicted": "evict", "eviction": "evict", "evicting": "evict", "removed": "evict",
    "remove": "evict", "kicked": "evict", "preempt": "preempt", "preempted": "preempt",
    "preemption": "preempt", "resident": "resident", "kept": "resident", "keep": "resident",
    "keeps": "resident", "stay": "resident", "gpus": "gpu", "worker": "worker",
    "workers": "worker", "decision": "decide", "decided": "decide", "chose": "decide",
    "chosen": "decide", "cause": "cause", "caused": "cause", "because": "cause",
    "reason": "cause",
}


def _stem(tok: str) -> str:
    if tok in _SYNONYMS:
        return _SYNONYMS[tok]
    for suf in ("ing", "ed", "es", "s"):
        if len(tok) > 4 and tok.endswith(suf):
            base = tok[: -len(suf)]
            return _SYNONYMS.get(base, base)
    return tok


@runtime_checkable
class LocalModel(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...
    def summarize(self, text: str, *, max_sentences: int = 2) -> str: ...
    def extract(self, text: str) -> list[dict]: ...            # [{"kind","text"}]
    def are_conflicting(self, a: str, b: str) -> bool: ...


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


_SENT = re.compile(r"(?<=[.!?])\s+")
_DECISION_CUES = ("decide", "chosen", "will use", "caused by", "because", "root cause",
                  "evict", "therefore", "we use", "must ")
_PREF_CUES = ("prefer", "terse", "always", "never show", "format", "should ", "please keep")
_QUESTION_CUES = ("unknown", "tbd", "todo", "how ", "why ", "what ", "unclear")


class DeterministicLocalModel:
    """Offline, reproducible stand-in — no network, no randomness.

    Good enough to exercise every Phase-3 integration path (semantic ranking,
    drafting, extraction, conflict detection) and to prove the exit condition.
    """

    name = "deterministic-local/1"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            vec = [0.0] * EMBED_DIM
            for tok in tokenize(t):
                bucket = int(hashlib.md5(_stem(tok).encode()).hexdigest(), 16) % EMBED_DIM
                vec[bucket] += 1.0
            norm = math.sqrt(sum(x * x for x in vec))
            out.append([x / norm for x in vec] if norm else vec)
        return out

    def summarize(self, text: str, *, max_sentences: int = 2) -> str:
        sents = [s.strip() for s in _SENT.split(text.strip()) if s.strip()]
        return " ".join(sents[:max_sentences])[:400]

    def extract(self, text: str) -> list[dict]:
        found = []
        for sent in _SENT.split(text.strip()):
            s = sent.strip()
            if not s:
                continue
            low = s.lower()
            if any(c in low for c in _DECISION_CUES):
                found.append({"kind": "decision", "text": s})
            elif any(c in low for c in _PREF_CUES):
                found.append({"kind": "preference", "text": s})
            elif "?" in s or any(c in low for c in _QUESTION_CUES):
                found.append({"kind": "open_question", "text": s})
        return found

    _NEG = {"not", "never", "no", "bypass", "bypassed", "without", "evict", "evicted",
            "removed", "failed", "cannot"}

    def are_conflicting(self, a: str, b: str) -> bool:
        ta, tb = set(map(_stem, tokenize(a))), set(map(_stem, tokenize(b)))
        shared = (ta & tb) - {"the", "a", "is", "of", "to", "and"}
        if not shared:
            return False
        a_neg = bool(set(tokenize(a)) & self._NEG)
        b_neg = bool(set(tokenize(b)) & self._NEG)
        return a_neg != b_neg
