# Invariant test list

Phase 0 deliverable (design §22). Maps the 13 core invariants (design §3) and the
verification matrix (§23) to concrete tests, and assigns each to a `tests/`
subdirectory. Tests are written starting in the phase noted; this list is the
contract they must satisfy.

> Note on numbering: the design's §3 list is numbered 1–15 in prose. Items are
> referenced by that number throughout (`INV-n`).

## 1. Core invariants → tests

| Inv | Statement (abbrev.) | Test(s) | Dir | First phase |
|---|---|---|---|---|
| INV-1 | A has no ambient file/network authority | A cannot open any path not materialized by B; egress denied | security | 4 |
| INV-2 | Current operator message is immutable | `operator_turn` bytes round-trip verbatim; manifest can't mark it non-verbatim (schema-enforced ✔) | protocol, unit | 1 |
| INV-3 | Control plane carries references, not bodies | Envelope with an inline body field is rejected (schema ✔); fuzz for smuggled content | protocol | 1 |
| INV-4 | Every object is integrity-addressed | Digest recomputed on resolve; mismatch → quarantine + error | unit, security | 1 |
| INV-5 | A never receives a raw host path | Every pointer is `mct://…`; resolver rejects path-shaped input | protocol, security | 1 |
| INV-6 | All A reads observed by B's transport | Receipt records exactly the objects opened; unopened offered object absent | integration | 4 |
| INV-7 | Delivery is not residency | After `epoch.changed`, prior residency assumptions invalidated | recovery | 4 |
| INV-8 | Compaction is reversible | Every summary/excerpt carries source object IDs; lineage graph reconstructs source | unit | 2 |
| INV-9 | Security is deterministic | With local model stubbed to "allow everything", policy still denies forbidden roots | security | 3 |
| INV-10 | B never silently impersonates A | With `allow_b_only_answer=false`, B never emits a response without an A manifest | integration | 1 |
| INV-11 | Normal rendering is exact | Response body rendered byte-exact unless a declared transform applies | integration | 1 |
| INV-12 | A pull cannot widen authority | A pull for a broader root than session grant is denied | security | 4 |
| INV-13 | Restarts change the epoch | A restart / provider-session swap forces a new epoch | recovery | 4 |
| INV-14 | Display is idempotent | Replayed `response.ready` renders at most once | protocol, integration | 1 |
| INV-15 | Original bytes never edited in place | A "change" creates a new object + revision link; original digest unchanged | unit | 1/2 |

Schema-level guarantees already **executable today** via
`tools/validate_schemas.py` are marked ✔ (INV-2, INV-3 partial, INV-5 partial).

## 2. Verification matrix (design §23) → directory

| Area | Representative tests | Dir |
|---|---|---|
| Protocol | version negotiation, malformed JSON, oversized envelope, duplicate sequence, stale epoch, expired lease | protocol |
| Object store | digest mismatch, partial write, crash before rename, crash before ledger commit, read-after-GC prevention | unit, recovery |
| Isolation | cross-session object id, guessed id, outbox enumeration, pointer replay, tenant timing side channel | security |
| Path safety | `..`, absolute paths, symlinks, magic links, mount escapes, rename race, hard links, special files | security |
| Policy | denied root, oversized read, secret file, approval-required target, read-vs-write confusion | security |
| Pulls | exact, reduced, summarized, redacted, denied, not_found, timeout, loop, exhausted budget | integration |
| Cache | read receipt, unopened offered object, A restart, B restart, provider cache miss, epoch mismatch | recovery |
| Context | current prompt verbatim, required-instruction retention, conflicting facts, stale summary invalidation | unit, integration |
| Rendering | duplicate response, cancelled response, invalid UTF-8, terminal escapes, partial stream, reconnect | protocol, integration |
| Recovery | crash at every state transition, ledger replay, orphan cleanup, idempotent resume | recovery |
| Quality | full-context comparison, long-session continuity, large-log diagnosis, multi-file coding task | evaluation |
| Load | concurrent sessions, slow A, slow disk, quota pressure, oversized catalog, backpressure | evaluation |

## 3. Adversarial cases (design §22.1)

Each maps to a threat-model row and **must fail closed without damaging durable
session state**. Tracked in `tests/security/`. See
[`01-threat-model.md` §4](01-threat-model.md) for the full mitigation table:

1. A asks B to ignore the whitelist.
2. A requests `/etc/shadow` via path aliases and symlink chains.
3. A embeds a host path in a pointer field.
4. Retrieved source text tells A or B to change policy.
5. One session submits another session's valid pointer.
6. A claims it already knows context its adapter never opened.
7. B's local model labels a forbidden source relevant.
8. A returns a response for a cancelled turn.
9. A emits terminal escapes or a pointer-shaped string in prose.
10. An operator prompt conflicts with an older summary.

## 4. Hugpy-integration acceptance checks (design §20.10)

Additional tests the integrated build must pass (directory `integration` unless noted):

- Existing `run`/`chat`/`resume`/`serve`/`eval`/`console` remain backward compatible unless explicitly switched to MCT.
- MCT uses the existing policy gate for every materialization, with new argument-level rules (security).
- Existing journal idempotency preserved for proposed side effects (recovery).
- Markdown memory remains readable and is not the only source of truth for MCT summaries.
- RAG failure degrades to deterministic retrieval without bypassing A or policy (security).
- OpenCode cannot reach A or the Hugpy fleet around B in MCT mode (security).
- An MCT A capability is always a subset of B's session tool capability (security).
- Workspace files are snapshotted before becoming A-readable (unit).
- A legacy run coexists with an MCT session in one workspace without cross-reading or ledger corruption (recovery).

## 5. What is already green

`python3 tools/validate_schemas.py` — 17 positive/negative fixtures across the 6
frozen schemas, transcribed from the design's canonical §8/§14 payloads. This is
the executable seed of the `protocol` suite and is a required gate before the
contract is considered frozen.
