# Frozen decisions and defaults

Phase 0 deliverable (design §22, §26). These are the decisions frozen before code
starts. The headline Phase 0 requirement is an **explicit decision on B-only
answers, defaulting to disabled** — recorded first below.

## 1. Headline decision: B-only answers are DISABLED by default

| | |
|---|---|
| **Decision** | B **may not** answer without A. `allow_b_only_answer = false` is the default. |
| **Rationale** | Preserves provenance; avoids silently substituting the local model for the declared answering model (design §5.2, §26, non-goal §1.3). |
| **Config flag** | `mct.allow_b_only_answer` (bool, default `false`). |
| **Enforcement** | Registry row 16 — `MctSessionLoop` route selection. If ever set `true`, provenance **must** be made visible in the rendered output (invariant 10). |
| **When the illusion breaks anyway** | A unavailable, approval required, redaction/rewrite, context-retrieval failure, epoch reset — these surface explicitly (design §5.2) but are *not* B answering as A. |

## 2. Recommended defaults (design §26)

| Decision | Default | Reason |
|---|---|---|
| May B answer without A? | **No** | Provenance; no silent model substitution |
| Does A see the exact current prompt? | **Yes, always** | Operator intent not replaced by B's interpretation (INV-2) |
| Does A get direct host paths? | **No** | B stays the real capability boundary (INV-5) |
| Does A read live files? | **No — snapshot by default** | Reproducibility, race resistance (§13.4) |
| Can summaries replace originals? | **No** | No irreversible context loss (INV-8, INV-15) |
| Is semantic dedup destructive? | **No** | Similar wording can differ meaningfully (§11.5) |
| Uncertain A continuity? | **New epoch** | Correctness before token savings (INV-13) |
| Trust provider cache claims? | **Only explicit adapter evidence** | Hidden caches aren't a correctness primitive (§12.1) |
| First response mode | **Complete sealed body** | Smallest reliable prototype (§16.1) |
| First deployment | **One test station/session** | Fastest safe validation |
| Fleet design | **Same protocol, later transport** | Avoids prototype lock-in |
| Prototype DB | **SQLite WAL** | Simple durable single-host ledger (§14.2) |
| Authorization engine | **Deterministic code + OS controls** | Local-model judgement is advisory only (INV-9) |

## 3. Layout & integration decisions (this build)

| Decision | Choice | Reason |
|---|---|---|
| Code layout | Mirror design §21: `src/hugpy_agent/mct/…` | Matches the design; eases eventual upstream integration |
| Project root | `/home/op/Desktop/mct/` | Keep a clean root rather than scattering across Desktop |
| Relationship to `hugpy-agent` | Compose the installed `0.1.43` surfaces (§20.3); don't fork a second tool-policy system | Design §20 doctrine |
| Execution-mode split | New `MctSessionLoop` sibling to `AgentLoop`; do **not** rewrite `AgentLoop` | Preserves published behavior (§20.5) |
| Store placement (prototype) | Sibling `.hugpy_agent/mct/`; do not alter `journal.db` schema yet | Protect crash-critical resume path (§20.7) |
| First milestone | Phase 0 contract artifacts only (this directory) | Per operator instruction |

## 4. Preflight items to correct (design §20.9)

Recorded so they are not forgotten; neither blocks the architecture:

1. **Version mismatch.** Installed wheel metadata reports `0.1.43` but
   `hugpy_agent.__version__` is `0.1.3` (confirmed on this host, 2026-08-02).
   MCT feature negotiation must use `importlib.metadata.version("hugpy-agent")`,
   **not** the package constant, until the constant is generated from the
   release version.
2. **License.** The wheel carries the Hugpy source-available license, not MIT.
   MCT packaging must inherit the intended license explicitly rather than
   copying older metadata. (Relevant to distribution, not to prototyping here.)

## 5. Open decisions deferred past Phase 0

- Symlink policy per source root (reject-all vs allow-within-root) — default **reject** (see registry §"Open decisions").
- Whether the dedicated service account is provisioned for dev or mocked until Phase 6 — recommend **mock in dev, mandatory before fleet**.
- Streaming frame format details — deferred to Phase 6 (§16.2); complete-body mode is first.
