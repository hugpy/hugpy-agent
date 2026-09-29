# Phase 6 — Hardening and fleet packaging

Adds the design §22 Phase-6 items: streaming, per-session quotas, garbage
collection, the service-account preflight, content-safe telemetry, multi-session
isolation under concurrency, and the operational runbook. Registry rows 13–15 and
the observability of §18.

## Exit condition (met)

> *The security and recovery suites pass under concurrency and fault injection.*

```console
PYTHONPATH=src python3 tools/phase6_hardening.py     # acceptance demo
pytest tests/security/test_concurrency.py tests/recovery/test_fault_injection.py
pytest                                               # 107 offline tests, stable across runs
```

- **Concurrency** — one broker serves many sessions across threads; every
  per-session hash chain verifies and cross-session isolation holds under load
  (`tests/security/test_concurrency.py`).
- **Fault injection** — crashes before respond, after context-built, and after
  render; each resumes idempotently with the chain intact and orphaned bytes
  reclaimed (`tests/recovery/test_fault_injection.py`).

## What was built

| Area | Module / change | Design |
|---|---|---|
| Streaming responses | `renderer.render_frame`/`seal_stream`, `a_adapter.respond_stream`, binding stream handlers | §16.2–16.3 |
| Per-session disk quota | `objects.ObjectStore(session_quota_bytes=…)`, `BrokerConfig` | §13.2, §17 |
| Garbage collection | `gc.py` — mark-and-sweep from durable roots, quarantine, legal hold | §17.2 |
| Service-account preflight | `hardening.py` — refuse to start under root-equivalent groups | §13.2, §20.4, row 15 |
| Observability | `telemetry.py` — per-turn metrics + content-safe traces; `metrics` table | §18 |
| Concurrency safety | thread-local ledger connections over one WAL file | §7.4, §14.2 |

### Streaming (§16.2)

A streams ordered, digest-verified frames; B renders complete frames in order,
then verifies the reassembled body against the sealed digest before committing.
Out-of-order frames and a body that doesn't match the streamed frames are both
rejected — B stops and shows nothing rather than guessing (§16.3). The complete
sealed body remains the default; streaming is opt-in via `respond_stream`.

### Concurrency model

`Ledger` uses **thread-local connections** to one WAL database, which is SQLite's
supported concurrency model — cursors never interleave across threads, writes
serialize via `busy_timeout`, reads don't block. One broker process safely serves
concurrent sessions (§14.2 notes Postgres as the fleet upgrade path, same schema).

### Telemetry / content privacy (§18.3)

Metrics (operator bytes, context tokens, pull count, latency) live in a `metrics`
table; traces are assembled from the append-only event log, which carries only
object IDs, digests, decisions, and timing. `Telemetry.is_content_safe` verifies
no body/secret fields leak — a prompt containing a secret string never appears in
any trace.

## Deployment-only items (see the runbook)

Sandboxing, remote transport, and staged rollout are configuration/operations,
not application code — they are specified in
[`runbook.md`](runbook.md): the systemd hardening unit, dedicated account
provisioning (the preflight enforces it at startup), mutually-authenticated
remote transport posture (§19.3), quotas/admission, GC scheduling, and the
shadow → authoritative rollout with rollback (§23.3).

## Known tuning item (carried from Phase 5)

The context packer keeps all relevant fragments (recall 1.0) but, under a loose
budget, also packs low-relevance ones (precision ~0.1). Relevance-gating the
packer / strengthening the redundancy penalty is a quality tuning task, not a
correctness gap — tracked here as the main open refinement.

## Project status

Phases 0–6 are complete. The prototype is a durable, capability-mediated
conversation runtime: pointer control plane, immutable authorized data plane,
deterministic context curation with model-assisted ranking, real Claude Code as a
confined A, measured token savings, and a suite that passes under concurrency and
fault injection.
