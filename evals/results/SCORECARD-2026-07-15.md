# Eval scorecard — 2026-07-15 (P3.4 first live run)

Run `20260715T122110Z`, base `https://dev.hugpy.ai/api`, default suite
(4 deterministic tasks), per-task step caps 4–6, `max_tokens` 512.
Both models were live and passed the token-echo readiness gate on the first
attempt (no warmup needed).

```
model                                       ready  passed  steps_avg  tokens_avg  wall_avg  tool_acc
------------------------------------------  -----  ------  ---------  ----------  --------  --------
ponpoke/flux2-klein-9b-uncensored-text-enc  yes    3/4    3.25       6189        5.60s     1.00
Qwen/Qwen3-Coder-Next-GGUF                  yes    4/4    2.25       6972        25.86s    1.00
```

Per-task:

| task | flux2-klein | Qwen3-Coder-Next |
|---|---|---|
| write_artifact | pass (2 steps, 4.5s) | pass (2 steps, 24.1s) |
| read_fact | pass (3 steps, 5.6s) | pass (2 steps, 21.1s) |
| glob_count | **FAIL — max_steps** (6.8s) | pass (2 steps, 25.2s) |
| read_transform_write | pass (3 steps, 5.5s) | pass (3 steps, 33.1s) |

`tool_accuracy` = 1.00 for both: every executed tool call returned a non-error
result (no bad paths / invalid args).

## Reading the data

- **Reliability:** the challenger **Qwen3-Coder-Next passed 4/4**; the incumbent
  **flux2-klein passed 3/4**. flux2-klein failed `glob_count` by looping to the
  step cap: it globbed correctly (found count=3) but then emitted the bare word
  `final_answer` as prose instead of a `<tool_call>{…}</tool_call>` envelope, so
  the loop nudged and it re-globbed rather than terminating. A real,
  reproducible tool-call-discipline weakness on the aggregate-and-report task.
- **Latency:** flux2-klein is **~4–5× faster** in wall time (5.60s vs 25.86s
  avg per task) — notable, since Qwen3-Coder-Next was the operator's "fast"
  candidate. It is not fast here; it is the slower, more reliable brain.
- **Steps/tokens:** comparable; Qwen uses slightly fewer steps on average
  (2.25 vs 3.25, inflated by flux2's capped loop) and marginally more est.
  tokens.

## Verdict

The data does **not** unambiguously back the flux2-klein choice on task
completion: on a bounded suite the challenger completed every task with cleaner
tool-call discipline, while flux2-klein tripped its own prompted-format
terminator on one task. flux2-klein's case rests on **latency** (4–5× faster),
which is a real operational argument for an interactive brain. This is a
genuine speed-vs-reliability tradeoff for the keeper to weigh — not a clean win
for the incumbent. A larger suite would tighten the pass-rate signal.

## Notes for reproduction / think-mode

- Serving-id forms: the `owner/…` id (`ponpoke/…`, `Qwen/…`) works on the `/v1`
  chat seam; the central catalog lists bare/`~`-separated names
  (`flux2-klein-9b-uncensored-text-encoder`, `Qwen~Qwen3-Coder-Next-GGUF`).
- Think suffix: the harness appends ` /no_think` to the wire copy for both.
  flux2-klein (Qwen3-family) honors it. **Qwen3-Coder-Next echoed `/no_think`
  literally** in the readiness ping — a Coder variant has no thinking mode, so
  the suffix is inert text, neither needed nor harmful. Tool-calling was clean
  with it applied; no separate suffix handling is required.
- `worker` came back empty: `/api/llm/serving/<name>` did not expose a
  host/worker field to scavenge, so attribution is unresolved (best-effort,
  never gated on — per doctrine). Per the fleet map, LLM/chat slots serve from
  worker `ae`.
