# Mediated Context Terminal (MCT)

A three-position, pointer-mediated terminal harness. See the full design in
[`../project_dir/mediated-context-terminal-design.md`](../project_dir/mediated-context-terminal-design.md).

- **C** — operator, ordinary terminal.
- **B** — [`hugpy-agent`](https://pypi.org/project/hugpy-agent/) `0.1.43` + local model: the only component that sees raw operator input; context broker, memory, capability broker, response relay.
- **A** — Claude / keeper-class model: high-value reasoning over a *pointer* to a context object, never an inline transcript.

## Status

**All phases (0–6) complete.** A durable, capability-mediated conversation runtime:
pointer control plane, immutable authorized data plane, deterministic context
curation with model-assisted ranking, real Claude Code as a confined A, measured
token savings, passing under concurrency and fault injection.

| Phase | State |
|---|---|
| 0 · Freeze the contract | ✅ done → [`docs/phase0/`](docs/phase0/00-contract-overview.md) |
| 1 · Non-LLM transport core | ✅ done → [`docs/phase1/`](docs/phase1/00-overview.md) |
| 2 · Deterministic context engine | ✅ done → [`docs/phase2/`](docs/phase2/00-overview.md) |
| 3 · Local-model assistance | ✅ done → [`docs/phase3/`](docs/phase3/00-overview.md) |
| 4 · Real A adapter and pulls | ✅ done → [`docs/phase4/`](docs/phase4/00-overview.md) |
| 5 · Shadow evaluation | ✅ done → [`docs/phase5/`](docs/phase5/00-overview.md) |
| 6 · Hardening and fleet packaging | ✅ done → [`docs/phase6/`](docs/phase6/00-overview.md) · [runbook](docs/phase6/runbook.md) |

## Layout (mirrors design §21)

```text
mct/
├── README.md
├── docs/phase0/               # frozen contract, threat model, decisions
├── src/hugpy_agent/mct/
│   └── schemas/               # 6 frozen JSON Schemas (§8, §14)
├── tools/validate_schemas.py  # executable contract check
└── tests/{unit,protocol,security,recovery,integration,evaluation}/
```

The Phase-1 runtime modules (`protocol.py`, `objects.py`, `ledger.py`,
`pull_broker.py`, `a_adapter.py`, `session.py`, …) are implemented under
`src/hugpy_agent/mct/` per design §21.

## Run it (interactive terminal)

```console
PYTHONPATH=src python3 tools/mct_repl.py
```

An ordinary chat: you type, an answer appears. Underneath, B curates a bounded
context and hands a pointer to A — a confined Claude Code process that reads and
pulls only through B. Uses Claude Code's own auth (no API key); turns make real
(billed) calls. In-session commands:

```text
/policy <text>              set the governing instruction (always in context)
/root <name> <path>         grant confined read access to a directory
/file <catalog> <root> <rel>    expose a file A can pull by name
/source <catalog> <text>    add an inline source A can pull by name
/trace                      why each fragment was included/omitted last turn
/acache                     A's durable cache — everything A read/received/produced
/metrics                    token / pull / latency summary   ·   /memory · /model · /exit
```

B keeps a **durable mirror of A's cache** — every byte Claude Code reads, receives,
or emits (including its full agent transcript) is captured as immutable objects and
reconstructable on demand. See [`docs/a-cache.md`](docs/a-cache.md); inspect it with
`/acache` or `server.a_cache.for_turn(...)`.

**Rolling log (always on).** Like any server, MCT continuously appends every
event to `<workspace>/.hugpy_agent/mct/mct.log` as it happens — B's ingest/context/
pull/render and A's reads/pulls, one chronological line each, body-free (§18.3):

```console
tail -f ~/.mct/repl/.hugpy_agent/mct/mct.log     # watch it live
```
In the terminal: `/tail [n]`. Disable with `BrokerConfig(event_log=False)`.

**Where is everything?** Every object is a real file; `/map` (or
`mct_logs.py --who map`) prints each object's kind, digest, and **exact path on
disk**, and `/where <pointer|id>` resolves any handle to its file. After each answer
the terminal prints a footer with the answer-document and transcript paths.

**Full three-party logs.** Reconstruct the complete record of each party from
durable state — C (operator conversation), B (hash-chained event ledger), A
(everything A received/opened/produced + transcript, with file paths):

```console
PYTHONPATH=src python3 tools/mct_logs.py <workspace>                 # list sessions
PYTHONPATH=src python3 tools/mct_logs.py <workspace> --session <id> --who all
PYTHONPATH=src python3 tools/mct_logs.py <workspace> --session <id> --out ./logs   # A.log B.log C.log
```

In the terminal: `/log a|b|c|all` and `/save-logs [dir]`.

Example: `/source runbook The DB is postgres 15 on db-01.` then ask
"what database do we run?" — A pulls the source through B and answers with a citation.

## Verify

```console
pip install jsonschema pytest

python3 tools/validate_schemas.py      # frozen protocol contract (Phase 0)
# PASS — 17 fixtures checked across 6 schemas. Contract frozen.

PYTHONPATH=src python3 tools/phase1_demo.py   # Phase-1 acceptance (transport core)
# ALL PHASE-1 ACCEPTANCE CHECKS PASSED

PYTHONPATH=src python3 tools/phase2_demo.py   # Phase-2 acceptance (context engine)
# ALL PHASE-2 ACCEPTANCE CHECKS PASSED

PYTHONPATH=src python3 tools/phase3_demo.py   # Phase-3 acceptance (local model, offline)
# ALL PHASE-3 ACCEPTANCE CHECKS PASSED

PYTHONPATH=src python3 tools/phase4_demo.py   # Phase-4 acceptance — REAL Claude Code as A (billed)
# PHASE-4 ACCEPTANCE: PASSED

PYTHONPATH=src python3 tools/phase5_eval.py       # Phase-5 shadow evaluation (offline)
# PHASE-5 SHADOW EVALUATION: PASSED

PYTHONPATH=src python3 tools/phase6_hardening.py   # Phase-6 hardening (concurrency + faults)
# PHASE-6 HARDENING: PASSED

pytest                                 # 107 offline tests (no billed calls), stable across runs
```

**A = Claude Code.** Phase 4 runs a headless, confined `claude` as the reasoning
model A (via a strict MCP server exposing only `resolve`/`submit_pull`/`respond`).
It uses Claude Code's own auth — no API key. Only `tools/phase4_demo.py` makes a
real (billed) call; everything else, including the full adapter data path, is
tested offline.

## Relationship to hugpy-agent

MCT is a new **execution mode** that composes the installed `hugpy-agent` surfaces
(`Config`, `Journal`, `Gateway`, `Policy`, `Comms`, `Audit`, `Memory`, `RagIndex`),
not a fork (design §20). It adds a sibling `MctSessionLoop` beside the existing
`AgentLoop` rather than rewriting it.
