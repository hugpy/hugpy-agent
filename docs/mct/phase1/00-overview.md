# Phase 1 — Non-LLM transport core

Implements the design §22 Phase-1 build: immutable object store, pointer
resolver, SQLite event ledger, session/turn/sequence/idempotency/epoch logic,
inbox/outbox (brokered object API), and complete-response rendering — with **no
LLM anywhere**.

## Exit condition (met)

> *A scripted fake A completes, pulls, retries, crashes, resumes, and never
> bypasses B.*

Proven by `tools/phase1_demo.py` (six scenarios, all asserting) and the `tests/`
suite (31 tests). Run:

```console
PYTHONPATH=src python3 tools/phase1_demo.py
pytest
```

## Modules (design §21)

| Module | Role | Key enforcement (registry) |
|---|---|---|
| `errors.py` | Fail-closed exception hierarchy | — |
| `ids.py` | ULID / counter identifiers matching schema patterns | — |
| `protocol.py` | Envelope validation, framing, pointer parse | row 1 |
| `objects.py` | Immutable store, atomic commit, digest-verified resolve | rows 2–4 |
| `ledger.py` | Hash-chained events, idempotency, epochs, turn state | row 5 |
| `cache_epochs.py` | Epochs + adapter read receipts | rows 6–7 |
| `capabilities.py` | Session-scope authorization (Phase-1 subset) | rows 11–12 |
| `pull_broker.py` | Pull arbitration: exact/reduced/not_found/denied/budget | rows 11–13 |
| `response.py` | Complete-body response validation | row 7 |
| `renderer.py` | Idempotent terminal rendering | rows 7–8 |
| `a_adapter.py` | The restricted client A talks to (the "no bypass" boundary) | rows 3, 16 |
| `session.py` | `MctSessionLoop` / `BrokerServer` — B orchestration | rows 5–8, 16 |
| `recovery.py` | Startup reconciliation, orphan GC, chain verify | — |
| `fake_a.py` | Scripted A programs for the acceptance harness | — |

## What Phase 1 deliberately does NOT do

Kept for later phases so the transport core stays small and provable:

- **No filesystem sources.** Pulls resolve against objects already committed in
  the session; `confined_io.openat2` snapshotting is Phase 4. The pull broker's
  "resolve the target" step is the seam where it slots in.
- **No local model.** Context manifests are built deterministically (operator
  turn + explicitly supplied fragments). Ranking/summaries are Phase 2–3.
- **No streaming.** Complete sealed body only (decision §26); streaming is Phase 6.
- **No real A / network transport.** In-process synchronous broker; the control
  plane is still pointer-only and every envelope is schema-validated and framed.

## How "A never bypasses B" is structural

A holds exactly one object: an `AAdapterClient` bound to one `(session, turn,
epoch)`. Its entire public surface is `resolve`, `open_manifest`,
`read_operator_turn`, `submit_pull`, `respond`. It has no reference to the object
store, the ledger, the filesystem, or any other session. Every read is
digest-verified and receipt-recorded by B; every pointer is an opaque handle, not
a path; cross-session pointers raise `IsolationError`. See
`tests/security/test_boundary.py`.

## Next: Phase 2

Deterministic context engine (design §22 Phase 2): exact prompt retention,
recent-turn selection, source catalog, line/byte/JSON/AST excerpting, token
packing, provenance graphs, exact deduplication. Exit: all selected context can
be explained and reconstructed without a local model.
