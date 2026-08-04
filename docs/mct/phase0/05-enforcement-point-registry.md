# Enforcement-point registry (Phase 0 exit condition)

> **Phase 0 exit condition (design §22):** *every privileged operation has a named deterministic enforcement point.*

This registry is the checklist for that exit condition. Each row names a
privileged operation, the deterministic code location that will enforce it, the
mechanism, the invariant(s) it upholds, and the Phase where it lands.

"Deterministic" is load-bearing: the local B model may *rank, summarize, or
recommend*, but it is never the authority (invariant 9). Every row below is code
or an OS control, never model judgement.

Module paths refer to the planned layout in design §21 (`src/hugpy_agent/mct/…`).
None of this code exists yet — Phase 0 freezes *where* each control must live so
that no later phase can quietly skip one.

## Legend

- **Invariant** — the design §3 invariant number(s) the control upholds.
- **Fail mode** — required behavior on violation. Every privileged op fails **closed**.
- **Phase** — the prototype phase (design §22) that must implement it.

## Registry

| # | Privileged operation | Trigger | Enforcement point (planned) | Mechanism | Invariant | Fail mode | Phase |
|---|---|---|---|---|---|---|---|
| 1 | Accept a control envelope | B/A receives a message | `protocol.decode()` | Schema validation vs frozen `schemas/*.json`; version + size limits | 3 | reject, `error` | 1 |
| 2 | Expose object bytes to A | A resolves a pointer | `objects.resolve()` | Recompute SHA-256, compare to ledger digest before returning bytes | 4 | quarantine + integrity `error` | 1 |
| 3 | Map a pointer to A | any A read | `objects.resolve()` / `a_adapter` | Pointer is an opaque session-scoped handle table lookup — never `open(path)` | 5, and cross-session isolation | deny | 1 |
| 4 | Commit an object | object write | `objects.commit()` | temp-write → verify digest+size → fsync → atomic rename → dir fsync → ledger commit (§7.5) | 4, 15 | quarantine, no pointer issued | 1 |
| 5 | Record a state transition | any transition | `ledger.append()` | Append-only, hash-chained, monotonic sequence, idempotency key (§14.2) | 7, and audit | stop accepting turns (§17) | 1 |
| 6 | Assign turn/epoch identity | new turn / A restart | `cache_epochs` + `ledger` | Monotonic turn id; epoch bumped on any continuity break (§12.3) | 7, 13 | new epoch, rehydrate | 1 / 4 |
| 7 | Render A's response | `response.ready` | `response.validate()` → `renderer.render()` | Verify turn+epoch+lease+digest+UTF-8+escape policy; idempotent single render | 11, 14 | show explicit error, do not render | 1 / 6 |
| 8 | Reject stale/cancelled output | late pointer | `renderer` + turn state machine (§15) | Turn/epoch/sequence check; cancelled turns never render | 14 | record + discard | 1 |
| 9 | Resolve a filesystem target | pull hits L4 source | `confined_io.open_under_root()` | `openat2` under a pre-opened root fd, `RESOLVE_BENEATH`/`NO_SYMLINKS`; **not** `realpath()` (§13.3) | 1, 5 | deny (path error) | 4 |
| 10 | Snapshot a source | before A-readable | `confined_io.snapshot()` → `objects.commit()` | Copy selected bytes into immutable store, hash, issue snapshot pointer (§13.4) | 8, 15 | deny / unstable-source error | 4 |
| 11 | Authorize a pull | `pull.requested` | `capabilities.authorize()` in `pull_broker.arbitrate()` | `Cap_A = Reach_B,OS ∩ Grant_session ∩ Policy_tool ∩ Scope_request` (§13.1); pull may only narrow (invariant 12) | 1, 12 | pointed structured denial | 4 |
| 12 | Argument-level policy check | every materialization | `policy.decide(tool, args=…)` (extends `hugpy_agent.policy`) | Per-root / per-object / per-selector / size rules; deny precedence, fail-closed (design §20.4) | 9, 12 | `denied` | 4 |
| 13 | Enforce pull budget | each pull | `pull_broker` preflight + counters | Max pulls, cumulative bytes/tokens, per-pull + turn deadline (§10.3) | (bounded working set) | `budget_exhausted` | 4 |
| 14 | Approve a side effect | `action_proposal` present | `pull_broker`/executor + `comms.request_approval()` | Operator approval gate; deterministic executor applies only approved op (§13.5) | (least privilege) | block until approved / deny | 6 |
| 15 | Constrain B's own OS reach | process start | systemd unit + service account (§13.2) | Dedicated unprivileged UID, no `sudo`/`docker`/`lxd`; ro bind mounts; seccomp; cgroups | 1 | refuse to start (§20.4) | 0 (plan) / 6 |
| 16 | Gate B-only answering | route selection | `MctSessionLoop` + config `allow_b_only_answer=false` | Default disabled; if ever enabled, provenance made visible (invariant 10) | 10 | route to A or fail visibly | 1 |
| 17 | Keep local model out of authz | context build / ranking | `context_builder` / `retrieval` boundary | Model output feeds ranking only; authorization is steps 11–12 | 9 | deterministic fallback (§17) | 3 |
| 18 | Label retrieved text as data | any source ingest | `context_builder` + object metadata | Authority metadata attached *outside* retrieved bytes; instructions in sources not honored (§13.6) | (prompt-injection) | treat as data | 2 / 3 |

## Coverage of the design's privileged surface

Every "Must not do" in the roles table (§2) and every adversarial case (§22.1)
maps to at least one row above:

- *A reads arbitrary paths* → rows 3, 9, 11, 12.
- *A bypasses B* → rows 1, 3 (opaque handles), 16.
- *B widens its own host authority* → row 15.
- *B uses model judgement as the security boundary* → rows 12, 17.
- *B silently impersonates A* → row 16.
- *Summaries become untraceable truth* → row 4 + invariant 8 (lineage in `context-v1` fragments).
- *Cross-session pointer replay* → rows 2, 3, 5 (session-scoped tables + digest).

## Open Phase-0 decisions feeding this registry

1. Symlink policy per source root — reject-all vs allow-within-root (row 9). Default: **reject symlink traversal** unless a root opts in.
2. Whether row 15's dedicated account is provisioned before Phase 1 dev or mocked. Recommendation: **mock in dev, mandatory before Phase 6**; the preflight check (design §20.4) must hard-fail if the real account has root-equivalent groups.
