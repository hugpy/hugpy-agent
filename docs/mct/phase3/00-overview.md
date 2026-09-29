# Phase 3 — Local-model assistance

Adds the design §22 Phase-3 capabilities: semantic retrieval, model-assisted
extraction (task-state / decisions / preferences), episodic summaries, redundancy
(near-duplicate) ranking, and conflict detection — all behind a pluggable local
model that is **advisory only**.

## Exit condition (met)

> *Local-model output can improve ranking but cannot authorize, delete originals,
> or become untraceable.*

Proven by `tools/phase3_demo.py` and `tests/integration/test_phase3_model.py`:

| Guarantee | How it holds | Check |
|---|---|---|
| **Improves ranking** | model contributes `R_sem`; `R_i = max(R_lex, R_sem)` — never lowers it | demo §1, `test_semantic_relevance_boosts_a_synonym_match` |
| **Cannot authorize** | authorization (`capabilities` / `pull_broker`) never consults the model; a cross-session pull is still denied | demo §6, `test_model_cannot_authorize_a_cross_session_pull` |
| **Cannot delete originals** | conflict + near-duplicate clustering only *annotate*; originals still resolve | demo §4–5, `test_near_duplicate_clustering_never_deletes` |
| **Cannot become untraceable** | every drafted fact/summary carries `source_objects` + `method` + `model`; `provenance_graph` walks it to originals | demo §3, `test_model_draft_is_traceable` |

Plus graceful degradation: a model that raises falls back to deterministic lexical
retrieval and never breaks a turn (design §17) — `test_model_failure_degrades_to_lexical`.

## The LocalModel interface (design §20.6)

`local_model.py` defines the shape B's brain must satisfy:

```python
class LocalModel(Protocol):
    name: str
    def embed(self, texts) -> list[list[float]]: ...     # semantic R_i
    def summarize(self, text, *, max_sentences=2) -> str: ...  # episodic summaries
    def extract(self, text) -> list[dict]: ...            # decisions/prefs/questions
    def are_conflicting(self, a, b) -> bool: ...          # conflict detection
```

- **Default:** `DeterministicLocalModel` — offline, reproducible, no network. Hashed
  bag-of-words embeddings with light synonym/stem normalization (so `removed ≈
  eviction`), extractive summaries, cue-based extraction, negation-based conflict.
- **Real model (Phase 4):** the installed `hugpy_agent` supplies it — `gateway.Gateway`
  for chat (summarize/extract) and the `rag` embedder (`fleet_embedder` + `cosine`)
  for `embed`. It plugs in behind this exact interface. That wiring is deferred to
  Phase 4 with the package-coexistence fix (our `src/hugpy_agent` currently shadows
  the installed one under `PYTHONPATH=src`).

## Wiring

`BrokerServer(..., local_model=…)` or `BrokerConfig(use_model=True)` turns it on.
With **no** model (the default), the engine is byte-for-byte the Phase-2
deterministic path — all 55 prior tests still pass unchanged. The model, when
present, only:

1. raises `R_i` in `context_builder` (ranking),
2. augments the always-on regex extraction with `extract_semantic` after a turn,
3. is available for `summarize` / `detect_conflicts` / `cluster_near_duplicates`.

It is threaded into `Retrieval`, `ContextBuilder`, and `Compaction` — never into
`Capabilities`, `PullBroker`, or `ResponseValidator` (invariant 9).

## Deliberately deferred

- **Real fleet model** — behind the interface, lands in Phase 4 (needs the
  coexistence fix). Everything here is proven with the deterministic model.
- **Automatic episodic summarization / conflict sweeps per turn** — the methods
  exist and are tested, but B calls them on demand rather than on every turn to
  keep turn latency predictable; scheduling them is a Phase 5/6 policy choice.

## Next: Phase 4

Real A adapter and pulls over filesystem sources (design §22 Phase 4): `openat2`
descriptor-confined snapshotting (`confined_io.py`), argument-level policy, the
real `hugpy_agent` `Gateway` behind `LocalModel`, automatic read receipts across a
real transport, and epoch reset/rehydration. This is where the deferred
coexistence fix and the security registry's rows 9–14 get implemented.
