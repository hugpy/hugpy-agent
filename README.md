# hugpy-agent

Portable agent runtime that uses the [hugpy] self-hosted LLM fleet as its
inference brain. Phase 1 of `AGENT-SYSTEM-DESIGN.md`: gateway + tool-call
adapter, assess→act→observe loop with a crash-safe SQLite journal, a
workspace-jailed toolset, markdown memory, and a CLI. **Python ≥ 3.10,
stdlib only — zero dependencies.**

## Install

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
hugpy-agent models        # lists fleet models from the configured base
```

### Install as a service (one-command enrollment)

`bootstrap.sh` takes a bare box to a running systemd **user** service
(idempotent — re-run to upgrade):

```sh
export HUGPY_API_KEY=<key>              # prefer env over flags for secrets
bash bootstrap.sh --central https://dev.hugpy.ai/api \
    [--session <discord-session-endpoint-url>] \
    [--task-source discord-inbox|queue] [--workspace <dir>]
journalctl --user -u hugpy-agent -f     # watch it heartbeat / run tasks
```

It creates `~/hugpy-agent/venv`, pip-installs the package (from the local
checkout for now; PyPI later), then runs `python -m hugpy_agent.install`,
which writes `~/.config/systemd/user/hugpy-agent.service`
(`Restart=on-failure`, `%h`-portable, `ExecStart=… serve`), writes all
config **including the key** to the **0600** env file
`~/.config/hugpy-agent/agent.env` (never the unit file, never argv), enables
linger, and enables + starts the unit.

The `serve` daemon polls a task source every `HUGPY_POLL_INTERVAL` (10s)
and runs each task as a normal journaled run (policy, audit, escalation all
apply). Sources (`HUGPY_TASK_SOURCE`):

- `discord-inbox` — polls the operator session (`HUGPY_DISCORD_SESSION`)
  for inbound messages starting with **`task:`**; the rest of the message is
  the task. The outcome is replied into the channel
  (`task finished (run <id>): outcome=… steps=…`). On startup the first poll
  only sets the message watermark — historical `task:` messages are never
  replayed (re-send a task that landed while the daemon was down).
- `queue` — a local file (`HUGPY_TASK_QUEUE`, default
  `<workspace>/.hugpy_agent/tasks.queue`), one task per line; append lines
  to dispatch. One task is consumed (atomically) per poll cycle.
- *(unset)* — **fail-closed idle**: the daemon heartbeats and does nothing.

SIGTERM (`systemctl --user stop hugpy-agent`) finishes the current task,
then exits cleanly.

### Agent node mode (`serve --node`)

`serve --node` (or `HUGPY_AGENT_NODE=true`) makes the daemon a **fleet node**:
it enrolls once with central's `/agent/register`, heartbeats every ~30s, and
pulls **operator-dispatched** tasks — running each exactly like any other task
(policy, audit, escalation, journal all apply). It composes with the local
task source: `serve --node` alone runs node tasks only; `serve --node
--task-source queue` runs **both** (a queue file *and* dispatched tasks). Node
config:

- `HUGPY_AGENT_CENTRAL` (or `--central`) — base URL of central's `/agent/*`
  routes; empty falls back to `HUGPY_BASE` (the `/api` dual-mount serves
  `/agent` there). When central's site API-key policy is on, register needs a
  console key — set `HUGPY_API_KEY` (it rides as the `Bearer` on register).
- `HUGPY_AGENT_NAME` / `HUGPY_AGENT_CAPABILITIES` — what the node advertises at
  register (defaults: the box hostname / `chat,tools`).

The node's enroll token is minted **once** by register and persisted, with the
node id and pull cursor, to `<workspace>/.hugpy_agent/node_state.json` (mode
**0600**, gitignored) — never logged, never committed. If central forgets the
node (a `410`, e.g. a db reset) the daemon silently re-enrolls; a revoked node
(`403`) drops its dead token and idles until re-enrolled. An unreachable
central never crashes the daemon — it backs off (exponential, capped) and keeps
heartbeating to `journalctl`.

## Configure

Precedence: **env > `.env` in the workspace > `agent.toml` in the workspace**.
CLI flags (`--base`, `--model`, `--workspace`, …) beat everything.

Each variable is two rows: the top row is `variable · default · values` (the
shape/enum it accepts), and the row beneath it is the full purpose.

| variable | default | values |
|---|---|---|
| `HUGPY_BASE` | `https://dev.hugpy.ai/api` | URL |
| **purpose** | fleet base URL — may end in `/api`, `/v1`, `/api/v1`, or be a bare origin; routes are normalized and probed | |
| `HUGPY_API_KEY` | *(none)* | `hp_…` token |
| **purpose** | Bearer token, sent whenever set. **Required for chat since 2026-07-14**: dev's `/v1` family now enforces keys (mint in the console under API access); the `/api/ml/*` amenities and `/api/models` catalog were still open at that time | |
| `HUGPY_MODEL` | `Qwen~Qwen3-Coder-Next-GGUF` | model id |
| **purpose** | a model id from `/v1/models` (default = the coder brain, 2026-07-17 switch: reliability over speed; for thinking-family brains see `HUGPY_NO_THINK`) | |
| `HUGPY_AGENT_BRAIN` | *(unset)* | model id |
| **purpose** | the DEDICATED agent-brain override — same effect as `HUGPY_MODEL` but named so it can't be conflated with other components' model knobs on a shared box; wins over `HUGPY_MODEL` when both are set. Central's copy of this default lives in `constants.DEFAULT_AGENT_BRAIN` | |
| `HUGPY_WORKSPACE` | cwd | path |
| **purpose** | the directory the agent works in (and is jailed to) | |
| `HUGPY_MAX_STEPS` | `25` | int |
| **purpose** | step cap per run | |
| `HUGPY_MAX_GENERATIONS` | `2` | int |
| **purpose** | per-run cap on async GPU generation jobs (`generate_image` / `generate_scene`); exceeding it returns a structured refusal to the model, not an exception | |
| `HUGPY_NO_THINK` | `false` | bool |
| **purpose** | suppress model "thinking": append ` /no_think` to the **wire copy** of the latest user turn on every chat call (never to stored history). Required for Qwen3-family brains, which otherwise spend the whole token budget inside `<think>` and never emit a tool call. Off by default since the 2026-07-17 coder-brain switch (~10% faster without the suffix); enable it when pointing at a thinking-family brain. Regardless of this knob, `<think>…</think>` spans are always stripped from output before parsing | |
| `HUGPY_TOOLS_MODE` | `prompted` | `prompted` \| `constrained` \| `auto` \| `native` |
| **purpose** | how tool-calls are delivered. The default makes **zero probe traffic**; only `auto`/`native` run the native-tools probe — one tiny live call per (base, model) per box, cached 7 days in `~/.cache/hugpy_agent/probe.json` (honors `XDG_CACHE_HOME`). The probe is expensive server-side today (the `/v1` shim drops `max_chunks` on the central→worker hop, 2026-07-14 capture), so it never fires unasked | |

`agent.toml` uses bare attribute names (`model = "..."`, optionally under
`[agent]`). Secrets belong in env or `.env` (gitignored), never in
`agent.toml` or the source.

## Use

```sh
hugpy-agent run "Read the files in this workspace, describe what the project \
does, and write your findings to report.md" --model Qwen2.5-3B-Instruct-GGUF
hugpy-agent chat                  # interactive REPL, same tools
hugpy-agent runs                  # journaled runs in this workspace
hugpy-agent resume <run_id>       # continue an interrupted run
```

Progress streams to stderr; the final structured report
`{outcome, steps, tool_calls, est_tokens, answer}` prints to stdout (pipeable
to `jq`).

### Tools

| group | tools | notes |
|---|---|---|
| local | `fs_read` / `fs_write` / `fs_glob`, `shell`, `http_fetch` | workspace-jailed (symlink-safe); `shell` risk-classed destructive; fetch is GET-only, 64KB cap |
| fleet: text ML | `summarize`, `keywords`, `embed`, `similarity` | sync `POST /api/ml/*`; risk class `remote_compute` |
| fleet: file ML | `transcribe`, `classify`, `detect`, `segment`, `depth`, `vision` | multipart upload of a workspace file; image-ref results (depth maps, masks) are fetched into `artifacts/` and the path returned |
| fleet: generation | `generate_image`, `generate_scene` | async job: enqueue → poll (2s cadence, 10min ceiling, SIGINT-safe) → artifact saved as `artifacts/<job_id>.<ext>`; **capped per run** via `HUGPY_MAX_GENERATIONS`; the `job_id` is journaled the moment enqueue returns, so `resume` re-polls the same job and never enqueues a duplicate |
| meta | `models_list`, `remember`, `final_answer` | `final_answer` is the schema'd termination signal |

Every fleet ML tool takes an optional `model_key`; when omitted it is
resolved from the `/api/models` catalog by task (cached per run). An
unresolvable task, a capacity gap (`local_serving_disabled`), or any job
failure comes back to the model verbatim as structured data — never an
exception, never masked.

### Tool calling on a fleet that ignores `tools`

The `/v1` seam silently drops the OpenAI `tools` field today, so the harness
owns tool-calling (design §3.1): tool JSON schemas are injected into the
system prompt using the Qwen/Hermes `<tool_call>{…}</tool_call>` convention,
parsed, schema-validated (with benign string→number/bool coercion), given
ONE repair round-trip on invalid output, and aborted with a structured error
if the model keeps failing. A native passthrough tier exists but is opt-in:
set `HUGPY_TOOLS_MODE=auto` (or `native`) to probe for seam support. The
probe result is cached per box — not per workspace — with a 7-day TTL, so a
machine probes each (base, model) pair at most once; under the default
`prompted` mode no probe request is ever sent.

Every chat payload carries `"max_chunks": 1` (kills the known
continuation-prompt leak) and known leak strings are scrubbed defensively.
`usage` is null at the seam today, so token accounting is a client-side
estimate (`gateway.estimate_tokens`).

## Interactive console (OpenCode co-install)

`hugpy-agent console` launches [OpenCode](https://opencode.ai) — the
open-source terminal coding agent — pre-wired to the hugpy fleet: it fetches
the fleet's live model list, writes an `opencode.json` (in
`~/.hugpy_agent/console/` by default) with a `hugpy` provider pointed at the
fleet's OpenAI-compatible `/v1` endpoint, and drops you into the OpenCode
TUI with the agent brain preselected. Your API key is passed as an
environment reference, never written into the config file. **OpenCode is an
optional peer, not a dependency** — install it once with npm, and everything
else in hugpy-agent works fully without it:

```sh
npm config set prefix ~/.npm-global && npm install -g opencode-ai   # once
hugpy-agent console                                                  # every time
```

Useful flags: `--model KEY` to open on a different model, `--no-sync` /
`--offline` to skip the model-map refresh and reuse the last-written config
(e.g. when the fleet is unreachable), `--workspace DIR` to keep a separate
console dir, and `--print-config` to inspect the generated config without
launching. Each sync keeps the previous config as `opencode.json.bak`.

## Terminal dispatch client

`hugpy-dispatch` is a **separate, standalone tool** from the agent runtime
above: a pure-bash operator client (bash + curl + python3 for JSON — no
Python client, no venv) for dispatching a task to an already-enrolled
**agent node** (a box running `hugpy-agent serve --node`) and watching it run,
entirely from a terminal on any box — no browser, no console.

### Install (one line, any box)

```sh
curl -fsSL https://dev.hugpy.ai/api/agent/client.sh | bash -s install
```

This downloads the client and installs it to `$HOME/.local/bin/hugpy-dispatch`
(pass `--prefix DIR` to install elsewhere), and creates a config template at
`~/.config/hugpy/dispatch.env` (mode `600`) if one doesn't already exist.
Re-running is safe — it reinstalls the script but never overwrites an
existing config. Add `$HOME/.local/bin` to your `PATH` if it isn't already
(the installer warns if it's missing).

### Configure

Edit `~/.config/hugpy/dispatch.env` (keep it mode `600` — it holds a secret).
Real environment variables always win over this file:

| variable | default | purpose |
|---|---|---|
| `HUGPY_CENTRAL` | `https://dev.hugpy.ai/api` | central's API base URL |
| `HUGPY_OPERATOR_TOKEN` | *(none)* | **required** for every call — the same operator token the console uses (`X-Operator-Token`) |
| `HUGPY_DISPATCH_NODE` | *(none)* | default target node id (`agn_...`) or name, used when `-n` is omitted |

### Use

```sh
hugpy-dispatch "reply with the single word pong"       # dispatch to the default node
hugpy-dispatch -n demo-node-vm "summarize report.md"    # dispatch to a specific node, by name or agn_ id
hugpy-dispatch --timeout 600 "a longer task..."          # override the 300s poll ceiling
hugpy-dispatch nodes                                     # table of every enrolled node
```

`hugpy-dispatch` (default action) dispatches, then polls every 2s until the
task finishes:

- **done** → result prints to stdout, exit `0`
- **error** → result prints to stderr, exit `1`
- **timeout** (default 300s, `--timeout SECS`) → the node id and task seq
  print to stderr so you can poll it again later, exit `2`

`-n/--node` accepts either a node's exact id (`agn_...`) or its name; a name
is resolved via `GET /agent/nodes`, preferring a non-revoked node and, if more
than one node shares the name, the most recently seen one.

`hugpy-dispatch nodes` prints id, name, status, revoked, `last_seen` (as
"Ns/m/h/d ago"), and current_task for every enrolled node.

**A task's result travels the wire as a plain string.** When the node
returns structured output it arrives `json.dumps`'d; `hugpy-dispatch` tries
to `json.loads` and pretty-print it, and falls back to printing the raw
string when it doesn't parse.

## Eval harness (per-model scoring)

"Which brain" is data, not vibes (design §7). `hugpy-agent eval` runs a small
suite of deterministic tasks against each model through the *real* agent loop
and emits a comparative scorecard:

```sh
hugpy-agent eval --model Qwen~Qwen3-Coder-Next-GGUF \
                 --model ponpoke/flux2-klein-9b-uncensored-text-encoder
# or straight from a checkout, no install:
python evals/runner.py --model A --model B
```

Each task has a **deterministic** checker (an artifact appeared on disk / the
final answer contains a required fact / the run finished under its step cap) —
never an LLM judging an LLM. The scorecard row per model is
`{model, ready, worker, passed, steps_avg, tokens_avg, wall_avg, tool_accuracy}`;
JSON + a readable table land in `evals/results/` (timestamped runs gitignored;
the chosen summary committed). Live cost is bounded by design: tiny prompts,
low per-task step caps, small token budgets.

**Readiness is gated on a chat token-echo**, never on HTTP 200 or an
`/api/llm/serving` mode flag — a worker that is down or still loading answers
`/v1/chat/completions` with a 200 whose *body* is an error string
(`[error: … 404 NOT FOUND …]`). A cold model's first request is handled by
polite polling (`--ready-timeout`/`--ready-poll`); a model that never becomes
servable gets a `ready=NO` row so the blocker is in the data. The engine lives
in `hugpy_agent.eval` (packaged, unit-tested); `evals/` is the operator surface
(`tasks.py` suite + `runner.py`).

## Crash resume

Every message and every tool call is journaled to
`<workspace>/.hugpy_agent/journal.db` (SQLite WAL). Tool calls are recorded
**before** execution with an idempotency key and their result recorded after,
so resume replays completed calls instead of re-executing side effects; a
call left `pending` (killed mid-execution) is reported to the model as
"outcome unknown — verify before retrying" rather than blindly re-run
(fail-closed), unless the tool is read-only, in which case it is safely
re-executed.

**Manual kill/resume procedure** (what the automated tests simulate):

```sh
hugpy-agent run "some multi-step task" &
sleep 10 && kill -9 %1           # SIGKILL mid-run — no cleanup possible
hugpy-agent runs                 # find the run_id (status: running)
hugpy-agent resume <run_id>      # continues; completed side effects replayed
```

A single Ctrl-C is gentler: the loop finishes the current step, marks the run
`interrupted`, and prints the resume command. A second Ctrl-C force-quits
(the journal is still consistent — every write is its own transaction).

## Structured delegation: dispatch packets & trace artifacts

The `spawn` tool (subagent delegation) accepts an optional **dispatch
packet** — a small contract stating not just what the subagent should do,
but how the work will be verified and what must come back:

```json
{"brief": "audit the config loader",
 "packet": {
   "objective": "audit config.py for env-precedence bugs",
   "scope": "src/hugpy_agent/config.py only, read-only",
   "constraints": ["do not edit files", "no network"],
   "verification": "parent re-reads the cited lines",
   "handoff_expectations": "list of findings with file:line citations"}}
```

The packet is validated **before** the subagent runs (all problems reported
in one refusal, as data — same fail-closed posture as the tool-subset gate);
a valid packet's contract is appended to the subagent's task text, so the
child is briefed with exactly the fields the audit will check.

Every completed delegation — packeted or not — leaves a **trace artifact**:
one markdown file under `<workspace>/.hugpy_agent/traces/<parent_run_id>/`
plus a line in `traces/INDEX.md`, so `cat INDEX.md` answers "what has been
delegated here, and how did it go?" without opening the journal. A spawn
without a packet gets a minimal artifact marked `unstructured: true` — a
reconstructed brief is never dressed up as a stated contract:

```markdown
---
parent_run_id: 3f2a…
child_run_id: 9c1b…
created_at: 2026-07-22T14:03:11+00:00
status: done
---

## Dispatch
- **objective**: audit config.py for env-precedence bugs
- **scope**: src/hugpy_agent/config.py only, read-only
- **constraints**: ["do not edit files", "no network"]
- **verification**: parent re-reads the cited lines
- **handoff_expectations**: list of findings with file:line citations

## Outcome
- **status**: done
- **summary**: two precedence findings, cited
- **evidence**: journal run 9c1b…: steps=6 tool_calls=4

## Handoff
- **expected**: list of findings with file:line citations
- **delivered**: two precedence findings, cited
```

Artifacts are written atomically (tmp + rename) and never duplicated on
crash-resume; a trace write failure degrades to a `trace_error` event and
never breaks the spawn result (the journal remains the durable record).

## Tests

```sh
python -m unittest discover tests            # offline, no network
HUGPY_AGENT_LIVE=1 python scripts/live_smoke.py   # 3 tiny live calls to dev
# ML section (spends GPU: one embed + one small sd-turbo image) needs BOTH:
HUGPY_AGENT_LIVE=1 HUGPY_AGENT_LIVE_ML=1 python scripts/live_smoke.py
```

## Layout

```
src/hugpy_agent/
  config.py    env > .env > agent.toml resolution
  gateway.py   OpenAI-compat client: SSE + fallback, retries, route probing,
               max_chunks=1, client-side token estimate, cancel
  adapter.py   tool-calling tiers: native | prompted (Qwen/Hermes) | constrained
  journal.py   SQLite WAL ledger: runs, messages, tool_calls, idempotency
  loop.py      assess→act→observe, step cap, compaction, structured report
  memory.py    memory/ markdown facts + MEMORY.md index
  tools/       registry + shell, fs, http, fleet (ML amenities + generation)
  serve.py     the daemon loop: task sources (discord-inbox | queue) + heartbeat
  node.py      agent node mode (P3.2): register/heartbeat/pull client of
               central's /agent/* registry + the serve source that drives it
  install.py   systemd user unit + 0600 env file + linger (injectable runner)
  eval.py      per-model eval engine: tasks + deterministic checkers, token-
               echo readiness gate, scorecard math + rendering (P3.4)
  cli.py       run | chat | resume | models | runs | serve | eval
evals/         operator surface: tasks.py (suite) + runner.py + results/
```

Seed lineage: the wire-contract client code is lifted from the field-tested
`abstract_ide` hugpyTab/servicesTab clients (see module docstrings).
