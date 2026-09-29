# Phase 0 — Freeze the contract

This directory is the complete Phase 0 deliverable for the Mediated Context
Terminal (MCT), per design §22. Phase 0 produces **no runtime code** — it freezes
the protocol, the security model, and the decisions everything else composes onto.

## Deliverables (design §22 Phase 0)

| Deliverable | Where | Status |
|---|---|---|
| Protocol schemas | [`../../src/hugpy_agent/mct/schemas/`](../../src/hugpy_agent/mct/schemas/) — 6 JSON Schemas | ✅ frozen + validated |
| Invariant test list | [`03-invariant-tests.md`](03-invariant-tests.md) | ✅ |
| Threat model | [`01-threat-model.md`](01-threat-model.md) | ✅ |
| Service-account & mount plan | [`02-service-account-and-mounts.md`](02-service-account-and-mounts.md) | ✅ |
| Explicit B-only-answers decision (default disabled) | [`04-decisions.md` §1](04-decisions.md) | ✅ disabled |
| **Exit condition:** every privileged op has a named deterministic enforcement point | [`05-enforcement-point-registry.md`](05-enforcement-point-registry.md) | ✅ 18 rows |

## Exit condition

> *Every privileged operation has a named deterministic enforcement point.*

Satisfied by the [enforcement-point registry](05-enforcement-point-registry.md):
18 privileged operations, each mapped to a deterministic code/OS enforcement
point (never model judgement, per invariant 9), the invariant it upholds, its
fail-closed behavior, and the phase that implements it. The registry's coverage
section cross-checks it against every "must not do" in the roles table (§2) and
every adversarial case (§22.1).

## What is executable today

The schemas are not just prose — they are validated:

```console
$ python3 tools/validate_schemas.py
  ...
PASS — 17 fixtures checked across 6 schemas. Contract frozen.
```

The harness checks each schema against the JSON Schema 2020-12 metaschema, then
validates the design's canonical §8/§14 example payloads (positive fixtures) and
a set of invariant-violating payloads that must be rejected (negative fixtures).
This is the seed of the `tests/protocol/` suite.

## The six frozen schemas

| Schema | Design ref | Object kind |
|---|---|---|
| `envelope-v1.json` | §8.1–8.2 | control-plane message |
| `context-v1.json` | §8.3 | `context_manifest` |
| `pull-request-v1.json` | §8.4 | `pull_request` |
| `pull-result-v1.json` | §8.5 | `pull_result` |
| `response-v1.json` | §8.6 | `response_body` manifest |
| `event-v1.json` | §14.1 | ledger event |

## Reading order

1. [`04-decisions.md`](04-decisions.md) — what's settled and why.
2. [`01-threat-model.md`](01-threat-model.md) — who we defend against.
3. [`05-enforcement-point-registry.md`](05-enforcement-point-registry.md) — the exit condition.
4. [`02-service-account-and-mounts.md`](02-service-account-and-mounts.md) — the OS ceiling.
5. [`03-invariant-tests.md`](03-invariant-tests.md) — how each invariant gets proven.

## Next: Phase 1

Design §22 Phase 1 — the non-LLM transport core (object store, pointer resolver,
SQLite event ledger, session/turn/sequence/idempotency/epoch logic, inbox/outbox,
complete-response rendering). Exit: *a scripted fake A completes, pulls, retries,
crashes, resumes, and never bypasses B.* Not started — awaiting go-ahead.
