# The A-cache mirror

A's own context/KV cache is **ephemeral and opaque** — it lives inside the per-turn
Claude Code process and dies when that process exits. The purpose of MCT is that B
keeps a **complete, durable mirror** of it: everything A reads, receives, or
produces is captured as immutable objects and reassembled on demand. This is the
design's cache #2 (the A-adapter working-set, §12.1–12.2) taken to its full form.

## What is captured (all immutable objects in B's store)

| A received / produced | Object kind | Recorded by |
|---|---|---|
| Exact system prompt delivered to A | `a_system_prompt` | `claude_adapter._capture_input` |
| Exact `-p` prompt delivered to A (incl. manifest pointer) | `a_prompt` | `claude_adapter._capture_input` |
| Context manifest A opened | `context_manifest` | receipt (`_ABinding.resolve`) |
| Every object A opened (operator turn, catalog, pulled excerpts…) | receipts + the objects | receipt (invariant 6) |
| Every pull request A issued | `pull_request` | MCP `submit_pull` → object + event |
| Every pull result A received | `pull_result` / `excerpt` | pull broker |
| A's final answer | `response_body` | MCP `respond` |
| **A's full agent transcript** (every `tool_use`, `tool_result`, text) | `a_transcript` | `claude_adapter` (`--output-format stream-json`) |

Because every item is a content-addressed object plus a ledger record, the mirror
is **fully reconstructable from durable state alone** — reopen B over the same
store and the entire A-cache rebuilds (tested in `test_a_cache.py`).

## Reading it

`server.a_cache` is an `AWorkingSet`:

```python
server.a_cache.for_turn(session_id, turn_id)      # ordered inputs / reads / outputs / transcript
server.a_cache.for_session(session_id)            # every turn
server.a_cache.stats(session_id)                  # counts
server.a_cache.dump(session_id)                   # human-readable
```

In the interactive terminal: **`/acache`** prints the stats and a full dump.

Example (`/acache` after one turn):

```text
{'turns': 1, 'objects_A_opened': 5, 'pulls_A_issued': 2, 'transcripts_captured': 1}
── turn t_000000 (epoch e_01KZ…) ──
  IN  system_prompt: 'You are A, the reasoning model…'
  IN  prompt: 'Handle one operator turn. …Manifest=mct://…'
  READ context_manifest (read): '{"schema": "mct.context/1", …}'
  READ operator_turn (read): 'What database and version do we run?'
  READ source_snapshot (read): 'The DB is postgres 15 on db-01; backups at 02:00 UTC.'
  OUT pull_request: '{"need": "Find what database…", "target": {"kind":"catalog-query",…}}'
  OUT response: 'We run PostgreSQL 15, hosted on db-01…'
  TRANSCRIPT: 23877 bytes (object o_01KZ…)
```

## Why it is B's, not A's

A owns reasoning; B owns state (§28). A holds nothing durable — so the record of
"what A had in context" is authoritative precisely *because* B captured it via the
transport (receipts), not because A reported it. B never treats this as *current*
residency across turns (invariant 7): it is the durable history of what A was
delivered, epoch by epoch — the answer to "reconstruct exactly what Claude saw."
