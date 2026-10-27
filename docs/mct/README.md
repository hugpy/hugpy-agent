# Mediated Context Terminal (MCT)

A three-position, pointer-mediated terminal harness. See the full design in
[the design reference](mediated-context-terminal-design.md).

- **C** — operator, ordinary terminal.
- **B** — [`hugpy-agent`](https://pypi.org/project/hugpy-agent/) + local model: the only component that sees raw operator input; context broker, memory, capability broker, response relay.
- **A** — Claude / keeper-class model: high-value reasoning over a *pointer* to a context object, never an inline transcript.

## References

- [Design](mediated-context-terminal-design.md): architecture and protocol design.
- [Threat model](phase0/01-threat-model.md) and [service-account plan](phase0/02-service-account-and-mounts.md).
- [Decisions](phase0/04-decisions.md) and [enforcement points](phase0/05-enforcement-point-registry.md).
- [Operational runbook](phase6/runbook.md).

The implementation is in `src/hugpy_agent/mct/`, relative to the Python package
root. Run source-tree commands below from that root. Design and security
requirements do not by themselves certify a deployment's operating-system isolation.

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
reconstructable on demand. See [cache reference](a-cache.md); inspect it with
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

## Relationship to hugpy-agent

MCT is a new **execution mode** that composes the installed `hugpy-agent` surfaces
(`Config`, `Journal`, `Gateway`, `Policy`, `Comms`, `Audit`, `Memory`, `RagIndex`),
not a fork (design §20). It adds a sibling `MctSessionLoop` beside the existing
`AgentLoop` rather than rewriting it.
