# hugpy-agent

Portable agent runtime that uses the [hugpy] self-hosted LLM fleet as its
inference brain. Phase 1 of `AGENT-SYSTEM-DESIGN.md`: gateway + tool-call
adapter, assess→act→observe loop with a crash-safe SQLite journal, a
workspace-jailed toolset, markdown memory, and a CLI. **Python ≥ 3.10,
stdlib only — zero dependencies.**

## Install

```sh
python3 -m venv .venv && . .venv/bin/activate
python -m pip install --upgrade pip   # old distro pips choke on modern wheels
pip install -e .
hugpy-agent models        # lists fleet models from the configured base
```

## Optional integrations

`hugpy-agent` imports **no** `abstract-*` package and **no** vendor SDK
(anthropic / openai / google …), in the core or in any extra. `import
hugpy_agent` and `hugpy-agent --help` run with nothing else installed.

Two ways capabilities are added, both opt-in:

- **`mct` extra** — `pip install hugpy-agent[mct]` adds the Mediated Context
  Terminal (`hugpy_agent.mct`). Its only dependency is `jsonschema` (control-
  plane envelope validation); it is not a vendor SDK.
- **Adapter plugins** (`hugpy_agent.adapters` entry-point group) — third-party
  capability providers register themselves; hugpy-agent discovers them at first
  use and never imports them directly (see `hugpy_agent/adapters.py`). Install
  the provider package separately and it lights up; absent one, hugpy-agent
  states its fallback rather than failing:

  | adapter name   | provided by (example)          | capability                              | fallback when absent           |
  | -------------- | ------------------------------ | --------------------------------------- | ------------------------------ |
  | `local_search` | `abstract-toolserver[files]`   | in-process content search over roots    | central HTTP finder (`/api/finder/search`) |

  Claude Code as the MCT reasoning model ("A") needs only the `claude` CLI on
  `PATH` (driven as a subprocess); no Python package is imported for it.

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

It creates `~/hugpy-agent/venv`, upgrades that venv's pip (old distro pips
choke on modern wheels), pip-installs the package (from the local checkout for
now; PyPI later), installs a desktop launcher for the terminal console (skipped
silently on a headless box), then runs `python -m hugpy_agent.install`,
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

### macOS

The secure install link's `.sh` one-liner works unchanged on macOS
(`curl -fsSL …/agent/install/<id>.sh | bash` — it curls the same Python
installer and runs it under the system `python3`). The installer creates its
venv, writes the credential, and builds a **user-level app bundle**
`~/Applications/hugpy Agent.app` whose icon opens the terminal console in
**Terminal.app** (via the same hold-open launcher script Linux uses). No sudo,
nothing in `/Applications`. On a headless Mac reached over SSH with no GUI
login the bundle is skipped.

Honest floors (below these, don't expect a working install rather than a
half-working one):

- **hugpy-agent** needs **python3 ≥ 3.10** — install the Xcode Command Line
  Tools (`xcode-select --install`) or Homebrew python.
- **OpenCode** (the interactive console TUI) needs **Node ≥ 18**
  (`brew install node`); it's an optional peer — the agent runs without it.
- Mountain-Lion-era Macs (OS X 10.8) are **below the floor** on both counts
  (their python and TLS stack are too old for modern PyPI/wheels); use a
  modern macOS.

macOS **service mode** (a launchd equivalent of the systemd user unit) is
**out of scope** here — `bootstrap.sh` is Linux/systemd only. On macOS run the
console from the app bundle (or `hugpy-agent serve` by hand).

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

### Toolserver integration

Every harness and client in this package reaches the station toolserver
(`abstract_toolserver`, :7004, ~200 tools: `fs_*`, `exchange_*`, `ledger_*`,
`todo_*`, `prompt_*`, `comms_*`, `ui_*`, `vl_*`, `vm_*`, `db_*`, …) through ONE
stdlib client, `hugpy_agent.toolserver_client.ToolserverClient`:

| method | what |
|---|---|
| `list_tools()` | cached `[{name, description, input_schema}]` (`POST /mcp?mode=flat tools/list`; falls back to `/ts/categories` + `/ts/list`) |
| `call(name, args, timeout)` | `POST /ts/call`; returns the tool's value (tool errors are the `{"error"}` data the server sent); transport/auth raise `ToolserverError` / `ToolserverAuthError` |
| `call_json(name, args)` | errors-as-data JSON string (what tool handlers feed the model) |
| `health()` / `status()` | `{url, ok, tool_count, auth: ok\|open\|missing\|rejected, version, latency_ms, error}`; `status()` is cached 30 s so UIs can poll it |
| `as_openai_tools()` / `as_anthropic_tools()` | `{"type":"function","function":{…}}` / `{name, description, input_schema}` lists (allowlisted tools only by default) |
| `classify(name)` / `allowed(name)` | `readonly` \| `mutating` \| `privileged`, and the allowlist verdict |

Configuration (env > `~/.hugpy/toolserver.env` > station/operator env files):

| variable | meaning |
|---|---|
| `TOOLSERVER_URL` | base URL (default `http://127.0.0.1:7004`; `STATION_CONSOLE_TOOLSERVER` also read) |
| `TOOLSERVER_OPERATOR_TOKEN` | the operator token — the same variable the toolserver itself reads; sent as `X-Operator-Token` (+ `Authorization: Bearer`). `TOOLSERVER_TOKEN`, `HUGPY_OPERATOR_TOKEN`, `STATION_CONSOLE_TOOLSERVER_TOKEN` are accepted aliases. A 401 with no token set names this variable in the error. |
| `HUGPY_AGENT_TOOLSERVER=0` | switch the bridge off for the process (CLI: `--no-toolserver`) |
| `HUGPY_AGENT_TOOLSERVER_ALLOW` / `_DENY` | comma lists of tool names (`*` and `vm_*` globs); deny wins |
| `HUGPY_AGENT_TOOLSERVER_TOOLS` | `meta` (default) or `flat` — see below |
| `HUGPY_AGENT_TOOLSERVER_URL` / `_TOKEN` / `HUGPY_AGENT_LOCUS` | per-workspace overrides (`agent.toml` / `.env`; the token never belongs in `agent.toml`) |

Allowlist defaults (`toolserver_client.classify`, decided from the tool name):
**readonly** tools (`*_list/get/read/state/find/search/…`) are on everywhere;
**mutating** tools (`todo_add`, `ledger_put`, `comms_ping`, `exchange_record`,
…) are on but pass the loop's policy gate as `network` risk (`--policy ask`
escalates, `auto` allows, `readonly` denies); **privileged** tools — `vm_*`,
`vmpool_*`, `sys_*` (`sys_run_cmd`), `browser_*`, `handoff_*`, `fs_write_file`,
`db_query` unless it is a `SELECT`, oauth/session control (`claude_oauth_*`,
`gpt_oauth_*`, `gpt_login_*`, `claude_reset/restore/set_model`,
`session_spin/release`, `ui_click_verify`) — are OFF until named in
`HUGPY_AGENT_TOOLSERVER_ALLOW` (or `*`); a refused call comes back to the model
as `{"error": "... set HUGPY_AGENT_TOOLSERVER_ALLOW=<name> ..."}`, never a crash.

Per harness:

- `chat` / `run` / `resume` / `serve --daemon` (the station seat backend,
  `hugpy-agent chat --model …`): the agent loop registers three meta-tools by
  default — `ts_categories`, `ts_list`, `ts_call` — whose handlers go through
  the shared client (`tools/toolserver.py`); `ts_call` is risk-classed by its
  TARGET tool, so the existing policy gate sees what it actually does. Startup
  prints one `[toolserver] ready|unavailable|disabled: …` line. `--no-toolserver`
  removes them for the run. `HUGPY_AGENT_TOOLSERVER_TOOLS=flat` additionally
  registers every allowed toolserver tool as its own ToolSpec (native
  tool-calling models); a local jailed tool of the same name (`fs_glob`) wins.
- `hugpy-agent serve` (:9126, `service/`): API-profile sessions get the same
  meta-tools; `GET /api/state` carries `toolserver: {url, ok, tool_count, auth}`
  and `GET /api/tools` lists the catalog with `class`, `risk` and `allowed` per
  tool. Native `claude-code` / `codex` profiles keep their own client-side
  tool configuration.
- `mct` / `mct-serve`: A (`claude -p --strict-mcp-config`) gets the toolserver
  as a second MCP server (`type: http`, `<url>/mcp?mode=flat`, token in the
  header) with `--allowedTools mcp__toolserver__<name>` for the allowlisted
  tools and `--disallowedTools` for the privileged ones; the grant is recorded
  in the ledger (`a.toolserver_tools`) and the access log, like
  `--native-tools all`. `--no-toolserver` (or `HUGPY_AGENT_TOOLSERVER=0`)
  keeps A confined to B's tools alone.
- `fleet` TUI (`fleet_tui.py`): the status bar shows `toolserver ok (N tools)`
  / `auth rejected` / `unreachable`, probed off the UI thread on each refresh.
- subagents (`spawn`) inherit a filtered view of the parent's registry, so
  they hold at most the parent's toolserver tools.

CLI:

```
hugpy-agent tools health                      # OK  url=… auth=ok tools=198 version=0.0.28
hugpy-agent tools list [--all] [--json-out]   # name  class  description (privileged hidden unless --all)
hugpy-agent tools call fs_glob --json '{"path":"/srv/vm_mgr/docs","pattern":"*.md"}'
hugpy-agent tools call vm_stop --json '{…}' --allow-privileged   # one-off bypass
```

Exit codes: 0 ok · 1 failure / tool error · 2 auth (missing or rejected token).

## Headless fleet console

`hugpy-agent console` opens a full-screen terminal control center (including
over SSH); when piped, it prints one status snapshot. It needs no `hcon` script,
browser, or Node installation. Python's standard `curses` module supplies the
terminal interface on Linux/macOS.

Use **Tab** or **1–6** to move between **Models**, **Workers**, **Queue**,
**Metrics**, **Results**, and **Frontends**. **Up/Down** selects a row; **Enter** opens its
action menu. **/** filters rows, **C** clears filters, **R** refreshes, and
**Q** exits. Worker/model names are picked from lists, never retyped.

- Model menu: inspect capacity, load, unload, assign, test, or open an agent
  frontend with that model (Hermes Agent, Claude Code, Qwen Code, OpenCode, Aider).
- Frontends tab: installed/not-installed indicators; choose a frontend and then
  select a chat-capable fleet model. A missing tool can be installed from the
  console after an explicit confirmation, or its installation guide can be opened.
- Worker menu: browse its models, load a chosen model, unload a resident model,
  or inspect its resources.
- Test dialog: enter a prompt, choose a token limit and whether evictions are
  allowed. **T** tests the visible chat models, hot models first, one at a time.
  **S** stops the remaining tests after the current call finishes. A failed call
  stops the batch to avoid overlapping work after an uncertain timeout.
- Capacity benchmark: press **M** to derive exact central quants from one verbose
  catalog snapshot, classify GPU-only/spill/RAM-only configurations against each
  worker, and run the deterministic nine-task grading battery. Workers run in
  parallel; distinct models also use the worker's advertised concurrent lanes.
  The same model's quant/config mutations remain serial. Before every task, the
  console rechecks that the exact worker is hot, loaded, pinned to the intended
  quant, and using the intended allocation/4-bit/MoE settings, then calls that
  worker directly. A base quant's full bytes are charged to disk and transfer;
  4-bit and MoE only change runtime-memory estimates. Finished base quants are
  removed from that worker's cache to make room for cold central quants; central
  `llm_storage` is never a deletion target. Results are retained in the console
  and exported as JSON plus an ODS metrics workbook under
  `~/.hugpy_agent/console/reports/`.
- Results retain responses and failures for the current session; select a
  result and press Enter to read it. Opening another frontend suspends the
  console and returns to it when that program exits.

Frontend adapters supply the selected fleet endpoint, model and API key only
to the child process. Hermes uses a named custom provider with Chat Completions,
configured in a fresh console-owned profile under `~/.hugpy_agent/console/hermes/`.
The profile references an environment variable for the key; it retains session
files after exit, and its path is recorded in Results. Your original Hermes
profile is untouched. Aider uses `openai/<fleet-model>` for the main, weak and editor
models. Claude Code uses Hugpy's Anthropic Messages shim. OpenCode refreshes its
fleet model map; Qwen Code receives the selected model even when a different
`OPENAI_MODEL` was previously set. These are agent frontends; inference remains
on Hugpy's workers. Binary detection does not certify model/tool-call compatibility.
Adapters were checked against the [Hermes provider documentation](https://hermes-agent.nousresearch.com/docs/integrations/providers/)
and [Aider OpenAI-compatible documentation](https://aider.chat/docs/llms/openai-compat.html).

Reads and calls run in the background, so navigation stays responsive. The
screen refreshes automatically, showing snapshot age, missing data and progress.
Load/unload confirmations name the target model and worker. Exiting does not
cancel a request already accepted by the fleet. The commands below remain
available for scripting; they are not required for the interactive workflow.

The main view ranks models by preparation needed: **ready now**, **hot / queue
active**, **load into free VRAM**, **transfer + load**, **eviction needed**, or
**won't fit GPU**. Each row shows a candidate worker, historical tok/s and a start
estimate where supported. Worker rows show free/total VRAM in GiB.

```sh
hugpy-agent console --base http://127.0.0.1:7002 status
hugpy-agent console workers --json
hugpy-agent console models --json
hugpy-agent console inspect MODEL
hugpy-agent console plan --task text-generation
hugpy-agent console call MODEL 'Reply with hello' --max-tokens 64
hugpy-agent console load WORKER_ID MODEL --alloc-mode gpu_only
hugpy-agent console unload WORKER_ID MODEL
hugpy-agent console request /llm/model-groups
hugpy-agent console request /llm/workers/WORKER_ID/config --method POST --body @config.json
hugpy-agent console exec MODEL -- your-headless-program its-arguments
```

Use `HUGPY_BASE` and `HUGPY_API_KEY` through the normal agent configuration.
Operator-only APIs additionally use `HUGPY_OPERATOR_TOKEN` from the environment;
the console never prints this token. An API key alone may not grant placement
or control access. Explicit bases retain their path (`/api`, `/v1`, `/api/v1`);
bare origins address central directly. Set `HUGPY_CONSOLE_TIMEOUT` to adjust the
per-request timeout (120 seconds by default).

`status`/`models`/`plan` inspect placements with at most four concurrent reads.
`plan` provides a hot-first manual testing order, not a reservation or automatic
sweep. `call` sends one bounded chat request without retry and defaults to
`no_makeroom`; add `--allow-eviction` when you intend to displace other models.
Other task types and controls are available through `request`, with an explicit
HTTP method for mutations. A timed-out mutation or call may still be running:
inspect queue/worker state before retrying. `load` acceptance means loading was
requested, not that the model is ready; verify residency afterward.

`exec` validates the exact catalog model and launches an existing program with
`OPENAI_BASE_URL`, `OPENAI_API_BASE`, `OPENAI_API_KEY` and `OPENAI_MODEL` in its
environment. It preserves arguments without a shell, strips the operator token,
and returns the child's exit code. Programs must support these environment
variables; it does not install or configure arbitrary third-party tools.

Capacity classifications use central's `fits_free_vram` / `fits_total_vram`
placement fields, including its MoE/headroom/calibration rules. The central API
must run the accompanying source change; older servers show **capacity unknown**.
Total VRAM fit means eviction may help, not that pinned allocations can be
evicted. Admission, policy, context size, routing and concurrent calls can change
the result. CPU/offload feasibility remains visible in `inspect`'s raw placement.

Load estimates use matching worker/model historical measurements across recorded
variants (conservatively the slowest); throughput shows the fastest recorded
matching variant and is not a promise for the next call. Queue records lack
worker attribution and remaining token budgets, so busy queues and eviction have
**unknown** start ETA. Idle/hot is approximately zero preparation time, excluding
prompt prefill. Missing observations are reported as unknown with visible errors;
partial snapshots exit nonzero. JSON output is available for automation.

## Interactive console (OpenCode co-install)

`hugpy-agent console --opencode` launches [OpenCode](https://opencode.ai) — the
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
  cli.py       harness | run | chat | resume | models | runs | serve | eval
evals/         operator surface: tasks.py (suite) + runner.py + results/
```

Seed lineage: the wire-contract client code is lifted from the field-tested
`abstract_ide` hugpyTab/servicesTab clients (see module docstrings).

## Terminal client (`hugpy-agent tui`)

`hugpy-agent tui` is a curses harness (stdlib only) over **abstract-claude
serve** (`/api/console/*`, the keeper console on `:9124` / hugpy locus `:9125`)
and, unchanged from before, **hugpy-agent serve** (`:9126`). Discovery order:
`--serve URL` → `$HUGPY_AGENT_SERVE` → `127.0.0.1:9124` → `:9125` → `:9126`,
each probed with `GET /api/state`; the serve kind is detected from the reply's
shape (`--kind abstract-claude|hugpy` pins it, `--session <cs-id|uuid|role>`
opens a row directly, `--token` / `HUGPY_SERVE_TOKEN` adds a bearer). Standing
roles (Keeper, Chat, Worker, Local) sit in the sidebar; cs-* rows are read via
`/api/console/events` (raw events: text deltas, thinking, tool cards,
approvals), native Claude uuid rows via `/api/session/events` + the chat SSE.
The status bar shows serve, session, provider/model (`→ staged`), busy/held,
queue depth, tokens (`n/a` for cs-* claude rows), `tools: N ✓` (toolserver) and
the network state; the splash uses the Hugpy Agent wordmark.

| key | action |
|---|---|
| Enter · `\`+Enter / Alt+Enter | send · newline |
| Ctrl-C | clear composer → interrupt (busy) → quit |
| Ctrl-X · Ctrl-K · Ctrl-P · Ctrl-G | interrupt · queue modal · model picker · session picker |
| Tab / Shift-Tab · F2 | next/previous role · focus transcript ↔ composer |
| PgUp/PgDn, Ctrl-U/Ctrl-D · End · Ctrl-T | scroll · follow tail · expand latest tool card |
| Up/Down (transcript) · Enter/Space | select block · expand/collapse card |
| `y a n c` / digits | answer an approval / question modal (Esc hides, Ctrl-A reopens) |
| `r` (transcript) · Ctrl-L · Ctrl-Q | retry / un-hold · redraw · quit |

Slash commands: `/model /session <id> /queue /retry /expand [n] /status /tools /help /quit`.
Tests: `PYTHONPATH=tests:src python -m pytest tests/test_serve_client_*.py tests/test_tui_*.py`
(`HUGPY_TUI_LIVE=1` adds a GET-only smoke against `127.0.0.1:9124`).

## Headless sessions for Station

For the normal terminal entry point, run:

```sh
hugpy-agent harness
```

This generates the live model map and execs OpenCode as the terminal harness.
Use `hugpy-agent console` for the fleet cockpit, or `hugpy-agent serve` for the
browser/session service and task-daemon options.

`hugpy-agent serve` still polls task queues. To run the provider-neutral HTTP
session service instead, use `hugpy-agent serve --http --profiles profiles.json`
or `hugpy-agent-serve --profiles profiles.json --workspace /your/workspace`.
The default listener is `127.0.0.1:9126`; state and journals persist under
`~/.local/state/hugpy-agent-serve`. Run queue polling and HTTP as separate processes.

Copy `examples/serve-profiles.json` and adjust the model ID, endpoint and context
budget to your locus. Each profile selects `openai-chat`, `openai-responses`,
`anthropic`, or the existing `hugpy` gateway. API bases include their version
prefix (for example `https://api.openai.com/v1` or `https://api.anthropic.com/v1`).
Set `api_key_env` to the name of an environment variable containing the credential;
do not put secrets in the JSON. `parameters` supplies provider-specific request
options. Chat APIs may select `token_parameter: "max_completion_tokens"`.

API profiles use Hugpy Agent's tool loop, journal, policy and Toolserver bridge.
The default `--policy ask` presents write/tool approvals in the session UI.
The existing loop requires a successful tool call before final answers. Stop takes
effect after the current API/tool operation; interrupted API runs can resume.
Anthropic and Responses output currently arrives after each model step completes;
Chat Completions can stream deltas. Toolserver credentials use the runtime's
existing environment/file discovery.

Claude Code and Codex are discovered only when installed on the service machine.
Discovery never launches or installs them. They start only when their profile is
selected and use their own login, tools, MCP and permission configuration. Their
headless runs cannot present native interactive approvals in this UI. Codex runs
with workspace-write sandboxing and no interactive approval prompts, following
its [non-interactive interface](https://learn.chatgpt.com/docs/non-interactive-mode).
Set `discover_clients: false` to disable discovery. Explicit native profiles may
set a `model` and `timeout` in seconds.

Use `examples/hugpy-agent-serve.service` as a user-unit template. Create the
workspace and profile file before enabling the unit. For a remote listener,
`HUGPY_SERVE_TOKEN` is required; use TLS or an SSH tunnel for transport.

Station reads `serve-loci.json` from its state directory, keyed by locus:

```json
{"a-brain": {"url": "http://127.0.0.1:19126", "label": "Hugpy Agent · a-brain"}}
```

In this example an SSH tunnel on the Station host forwards port 19126 to a-brain's
loopback port 9126. The Serve pane opens `/serve/@a-brain/`. Use `host` as the key
for the Station machine itself. A `token_file` entry can name a private file on
the Station host containing an upstream service token. Station keeps it out of
browser state. Legacy `ac-loci.json` continues to work; generic entries override
matching loci. Neither SSH locus setup nor VM setup installs Claude Code or Codex.

The HTTP contract is `GET /api/state`, `GET /api/profiles`,
`POST /api/sessions {"profile":"…"}`, `GET /api/sessions/<id>?after=<event-id>`,
and POST actions `messages {"text":"…"}`, `answer {"id":"…","choice":"…"}`,
`stop {}`, and `resume {}` below `/api/sessions/<id>/`.

The session model dropdown can change providers between turns without creating a
new window or losing the conversation. Switching clears provider-specific resume
IDs and carries the shared transcript into the next turn. Hugpy Fleet is the
example default; `discover_models: true` adds its current chat models to the same
dropdown (catalog cached for 60 seconds). GPT and Claude API profiles appear there
too; absent credentials mark them unavailable. Installed native clients remain
optional entries. `POST /api/sessions/<id>/profile {"profile":"…"}` changes a model
while idle. Fleet URL and all provider model IDs are operator configuration.
