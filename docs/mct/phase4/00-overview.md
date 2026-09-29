# Phase 4 — Real A adapter and pulls

Implements the design §22 Phase-4 build: a real reasoning model as A, automatic
read receipts, the real pull loop over filesystem sources, quotas/timeouts,
descriptor-confined I/O, and explicit A-unavailable behavior. Registry rows 9–13.

**A is Claude Code**, not the raw API — no API key; Claude Code uses its own auth.

## Exit condition (met)

> *A can complete representative tasks using curated context and recover from
> intentional omissions through pulls.*

Proven live by `tools/phase4_demo.py`: confined `claude` traces the §25 eviction
example — it resolves the manifest and operator turn, **pulls** the exact log line
from a `confined_io` snapshot, and answers, citing `line 512: DECISION evict
worker gpu-02 reason=preempt alloc=A17`. B renders it once. The full adapter data
path is also covered **offline** (no billed call) in
`tests/integration/test_phase4_claude_adapter.py`.

```console
pytest                                        # 81 offline tests
PYTHONPATH=src python3 tools/phase4_demo.py    # live, billed Claude Code call
```

## How A is confined (invariant 1, made structural)

A is a headless `claude -p` process. Its entire capability surface is three MCP
tools B serves — `resolve`, `submit_pull`, `respond` — via:

```
claude -p <prompt>
  --mcp-config <B's server>  --strict-mcp-config       # ONLY B's MCP server
  --allowedTools mcp__mct__resolve mcp__mct__submit_pull mcp__mct__respond
  --disallowedTools Bash Read Edit Write WebFetch ...   # no built-ins
  --append-system-prompt <A role>  --model sonnet  --output-format json
```

`--strict-mcp-config` + the allowlist + `-p` (which auto-denies any tool not
pre-approved) mean A has **no filesystem, shell, or network** — only brokered,
digest-verified, receipted operations. Verified: the live run showed A opening
only MCT objects and pulling only through B.

## New modules

| Module | Role | Registry |
|---|---|---|
| `confined_io.py` | `openat2` descriptor-rooted reads; `RESOLVE_BENEATH`/`NO_SYMLINKS`; O_NOFOLLOW fallback | row 9 |
| `mct_mcp_server.py` | Dependency-free stdio MCP server = A's only tools; rebuilds the turn from durable state (§17.1) | rows 3, 6 |
| `claude_adapter.py` | Launches + confines Claude Code as A; A-unavailable ⇒ B does not answer | rows 10, 16 |

Extended: `session.py` (`register_root`/`register_source_file`/`submit_via_claude`,
shared `_prepare_turn`), `pull_broker.py` (cumulative source-byte budget; all
excerpt selectors), `objects.py`/`excerpt.py` (bounded selectors over real files).

## Data-path topology

```
C ── B (parent) ── prepare turn (ingest, context, manifest) ──► spawn:
        claude -p  ──stdio/MCP──►  mct_mcp_server (child)
                                     resolve / submit_pull / respond
                                     (same on-disk store, confined_io snapshots)
        A calls respond ──► records a.response_ready event
   B (parent) reads the event ──► on_response_ready ──► validates + renders ONCE
```

Rendering stays in the parent (idempotent, invariant 14); the child only records
the sealed response objects. State is shared through the durable store, not memory.

## Security proven

- Path traversal / symlink escape / abs-path / oversize → fail closed (`confined_io`, `tests/security/test_confined_io.py`).
- A cannot name host paths — it pulls by catalog **name**; an unregistered target is `not_found` (`tests/integration/test_phase4_sources.py`).
- Cumulative source-byte budget → `budget_exhausted`.
- A-unavailable / silent → turn `Failed`, B answers nothing (invariant 10).

## Deliberately deferred

- **`hugpy_agent.policy.decide(…, args, …)` wiring** — confinement is currently
  enforced by `confined_io` + name-only catalog + size caps (which fully cover the
  threat model). Folding in the installed policy engine's argument-level rules
  (registry row 12) is a clean addition on the same seam, now that coexistence is
  fixed.
- **Action-proposal / approval flow (registry row 14)** — A has no write or
  side-effect tool yet (only resolve/pull/respond), so there is nothing to
  approve. Writes arrive with the proposal flow in a later phase.
- **Streaming, service-account sandboxing, remote transport** — Phase 6.

## Next: Phase 5 — Shadow evaluation

Run the same prompts three ways (full-context baseline; MCT curated; MCT with
injected resets/source changes) and measure correctness, instruction adherence,
evidence use, tokens, pulls, and failure transparency (design §22 Phase 5, §24).
