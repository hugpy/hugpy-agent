# Phase 2 — Deterministic context engine

Implements the design §22 Phase-2 build: exact prompt retention, recent-turn
selection, source catalog, line/byte/JSON/AST excerpting, token estimation and
packing, provenance graphs, and exact deduplication — **with no local model**.

## Exit condition (met)

> *All selected context can be explained and reconstructed without a local model.*

Proven by `tools/phase2_demo.py` and the test suite:

```console
PYTHONPATH=src python3 tools/phase2_demo.py
pytest                       # 55 tests
```

- **Explained** — `ContextBuilder.build` returns a trace: for every candidate,
  the score, its component breakdown (`A R D F C Z U`), and an include/omit
  reason (`selected` / `budget-exhausted` / `low-score` / `exact-duplicate` /
  `required`). Surfaced on `TurnResult.context_trace` and answers §18.2's "why was
  this fragment included or omitted?".
- **Reconstructed** — every derived fact carries source-object pointers;
  `Compaction.provenance_graph` walks them back to originals (invariant 8), and
  the excerpt selectors reproduce any bounded slice deterministically.

## New modules (design §21)

| Module | Role |
|---|---|
| `excerpt.py` | Pure selectors: `lines` / `bytes` / `json` / `symbol` (AST) / `match` (regex+ctx) |
| `retrieval.py` | Deterministic candidate gathering: recent turns, facts, sources, lexical/structured/dependency search |
| `compaction.py` | Derived facts with provenance, regex extraction, dedup, conflict retention, provenance graph |
| `context_builder.py` | §11.2 scoring formula, budget-aware packing, exact dedup, explanation trace |

Extended: `ledger.py` (facts table + object listing), `objects.py` (delegates to
`excerpt`), `session.py` (`set_policy`, ContextBuilder-backed manifest, post-turn
memory extraction).

## The scoring formula (§11.2), deterministically

```
S_i = w_a·A_i + w_r·R_i + w_d·D_i + w_f·F_i + w_c·C_i − w_z·Z_i − w_u·U_i
```

| Term | Signal (Phase 2) |
|---|---|
| A — authority | role priority (governing_instruction=1.0, decision=0.85, …) |
| R — relevance | **lexical** term-overlap with the operator turn (embeddings are Phase 3) |
| D — dependency | is the candidate a source of an already-required fragment? |
| F — freshness | recency rank within the candidate pool |
| C — continuity | active-task affinity (recent turns high, one-off facts lower) |
| Z — size cost | token estimate / pack budget |
| U — redundancy | max Jaccard overlap with already-selected fragments |

Required items (policy, operator turn, explicit fragments) bypass scoring and are
packed first; the rest are packed greedily under `maximum_input_tokens −
reserved_output_tokens − pull_tokens_remaining`.

## Deliberately deferred

- **Semantic retrieval** — R_i is lexical only; the embedding index (`rag.py`
  reuse) lands in Phase 3.
- **Model-drafted summaries / nuanced extraction** — Phase 2 extraction is
  regex, one marker per line (`DECISION:` / `PREFER:` / `TODO:`). Model-assisted
  decision/preference/entity extraction is Phase 3, on the same fact schema and
  provenance discipline.
- **Near-duplicate clustering** — only *exact* (digest) dedup here; semantic
  near-dup clustering needs the model (§11.5 rule 2) and is non-destructive.

## Next: Phase 3

Local-model assistance (design §22 Phase 3): semantic retrieval, task-state /
decision / preference extraction, episodic summaries, redundancy ranking,
conflict detection. Exit: model output improves ranking but **cannot authorize,
delete originals, or become untraceable** — it feeds the deterministic packer
built here; it never replaces it.
