"""Deterministic local model behavior (design §11.2, §11.4, §20.6)."""
from hugpy_agent.mct.local_model import DeterministicLocalModel, cosine


def test_embeddings_are_deterministic_and_normalized():
    m = DeterministicLocalModel()
    a1 = m.embed(["gpu eviction preempt"])[0]
    a2 = m.embed(["gpu eviction preempt"])[0]
    assert a1 == a2                                   # reproducible, no randomness
    assert abs(sum(x * x for x in a1) ** 0.5 - 1.0) < 1e-9  # L2-normalized


def test_synonyms_raise_similarity_above_raw_overlap():
    m = DeterministicLocalModel()
    # "removed" and "eviction" share no exact token but stem to the same concept
    ev, rm = m.embed(["worker was removed"]), m.embed(["worker eviction happened"])
    assert cosine(ev[0], rm[0]) > 0.3


def test_extract_catches_unprefixed_decision():
    m = DeterministicLocalModel()
    items = m.extract("The outage was caused by a preempt on alloc A17.")
    assert any(i["kind"] == "decision" for i in items)   # no 'DECISION:' prefix needed


def test_conflict_detection_on_negation():
    m = DeterministicLocalModel()
    assert m.are_conflicting("max-gpu keeps the worker resident",
                             "max-gpu bypassed, worker evicted")
    assert not m.are_conflicting("apples are red", "oranges are round")


def test_summarize_shortens():
    m = DeterministicLocalModel()
    long = "First sentence here. Second one. Third. Fourth. Fifth."
    assert m.summarize(long, max_sentences=2).count(".") <= 2
