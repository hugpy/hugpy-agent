# Phase 5 — Shadow evaluation

Runs the same tasks three ways — full-context baseline, MCT-curated, and MCT with
injected faults — and measures token savings and quality (design §22 Phase 5,
§24). Deterministic and offline; token counts use the installed
`gateway.estimate_tokens` so both arms are measured identically (§20.3).

## Exit condition (met)

> *Token savings are material and quality stays within the accepted threshold on
> the task suite.*

```console
PYTHONPATH=src python3 tools/phase5_eval.py      # full report
pytest tests/evaluation/test_phase5.py            # 7 threshold checks
```

Representative results:

| Task | family | baseline | MCT | reduction |
|---|---|---:|---:|---:|
| log-diagnosis | log diagnosis (1200-line log) | 6948 | 48 | **99.3%** |
| config-lookup | architecture recall (large config) | 628 | 38 | **94.0%** |
| latest-instruction | contradiction / correction | 219 | 385 | −75.8% |
| selection-precision | selection quality | 249 | 455 | −82.7% |

**Bounded working set** (a memory query as history grows) — MCT plateaus while the
baseline grows unbounded (design §0):

| history | baseline tokens | MCT tokens | reduction |
|---:|---:|---:|---:|
| 50 | 427 | 279 | 34.7% |
| 200 | 1715 | 749 | 56.3% |
| 800 | 6965 | 749 | **89.2%** |

Quality: **0 omission errors**, latest-instruction **adherence holds**, selection
**recall = 1.0** (no relevant fact lost), both source tasks **recover via pull**,
and **fault recovery** works (epoch reset + source change → re-snapshot, serve new
content, no stale residency).

## What the numbers honestly say

- **Where MCT is designed to help — large sources/logs and long histories — savings
  are large (94–99%, and 89% at 800-turn history).** This is the whole thesis
  (§1.2): make big files available through bounded excerpts + pulls instead of bulk
  ingestion, and keep the per-turn working set bounded as history grows.
- **On trivially small inputs MCT adds overhead** (the two negative rows): curation
  metadata (policy, extracted facts, episodics) costs more than pasting a
  ten-line transcript. This is expected and reported, not hidden — MCT is not for
  tiny contexts. Quality on those tasks still holds (adherence, recall).
- **Selection precision is low (~0.1) while recall is 1.0.** The packer keeps every
  relevant fact (no omission) but, under a loose budget, also packs low-relevance
  decisions. That is a *curation-tuning* opportunity (relevance-gate the packer /
  strengthen the redundancy penalty), not a correctness problem — nothing needed
  is lost. Tracked for Phase 6 tuning.

## Method

`evaluation.py`:
- **baseline** = tokens of the full transcript + all sources inlined + prompt.
- **MCT** = tokens of the operator prompt + each packed fragment's real text +
  any pulled excerpt (sources are never inlined — they are pulled as bounded
  excerpts through `confined_io`).
- **omission / pull-recovery** = is the gold evidence in the curated context, else
  reachable via one pull, else an omission error?
- **selection P/R** = packer's included decisions vs the relevant ones in memory.
- **fault** = build context, change the source on disk, reset the epoch,
  invalidate the snapshot cache, rebuild — verify the new bytes are re-snapshotted
  and served under the new epoch (invariant 7, §12.3).

This is the design's **shadow mode** (§23.3): B builds the curated manifest and we
record what it *would* send versus the full-context baseline, before the mediated
path is ever made authoritative.

## Fix made this phase

Model extraction now **complements** regex extraction (runs only when regex found
nothing) instead of duplicating it — removing near-duplicate facts that were
bloating memory and inflating token counts. (`session._extract_memory`.)

## Next: Phase 6 — Hardening and fleet packaging

Streaming, service-account sandboxing, remote authenticated transport, quotas +
GC, multi-session isolation, monitoring/runbooks, and the packer relevance-gating
noted above (design §22 Phase 6).
