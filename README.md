# hugpy-agent

hugpy-agent is the agent runtime for the Hugpy self-hosted LLM fleet, and it
**replaces the legacy "hugpy-fleet"**. The package provides:

- **An agent loop on the fleet.** It runs assess → act → observe with a
  crash-safe SQLite journal, workspace-jailed tools, a policy gate with
  operator escalation, audit, subagents and RAG memory. The loop is the
  [hugpy-terminal](../hugpy_terminal/README.md) core, extended with fleet extras.
- **The operator CLIs.** `hugpy-agent run`, `chat`, `case` and `resume`; the
  headless fleet cockpit (`console`); launchers for OpenCode, Claude Code,
  Qwen Code, Hermes and Aider (`launch`); a terminal client for Serve sessions
  (`tui`); the toolserver client (`tools`); and MCT.
- **The agent arm (backend only).** `hugpy-agent console-serve` serves the
  `/api/agent/*` toolset (install links, manifest) on `127.0.0.1:7342`, and
  hugpy-core proxies to it. The `/fleet/` page was retired on 2026-10-20: the
  agent is a backend; the Core console's Station tab is the harness.
- **The Core host's sessions service.** The deb installs `hugpy-agent-sessions.service`:
  `hugpy-agent serve` on `127.0.0.1:9127` registered with Core, the home of the
  console's Station tab sessions and the brain (`/etc/hugpy/sessions.env` tunes it).
- **The Hugpy session server.** `hugpy-agent serve` is `hugpy-harness serve`
  with the agent's loop and gateway swapped in: the harness's named sessions
  on `127.0.0.1:9126` for Station and `hugpy-agent tui`, plus RAG, Discord
  comms, the fleet toolset and session identity/lease signals to central.

The full reference is [`docs/agent-and-harness.md`](../../docs/agent-and-harness.md).
It covers the harness hooks, chat-loop behavior, every subcommand, the
services, the TUI keys, delegation, eval, known gaps and open topics.

## Status (2026-10-18)

| where | version |
|---|---|
| this source | **0.1.124**, prepared for release; requires `hugpy-harness>=0.1.10,<0.2` |
| installed on ae | 0.1.123 deb (`/opt/hugpy-agent/venv`) with local Harness source fixes |
| PyPI | 0.1.123; publish Harness 0.1.10 before Agent 0.1.124 |

## Install

**The /fleet console arm (deb, apt repo).** This is what a hugpy-core host
uses.

```sh
curl -fsSL https://dev.hugpy.ai/apt/setup.sh | sudo bash
sudo apt install hugpy-agent
curl -s http://127.0.0.1:7342/api/agent/health      # {"result":{"arm":"agent","ok":true,"version":"0.1.118"}}
```

The deb does the following:

- It builds `/opt/hugpy-agent/venv` offline from bundled wheels: hugpy-agent
  with `[console]`, hugpy-tools, abstract-toolserver, hugpy-harness and Flask.
- It creates the `hugpy-agent` system user.
- It writes `/usr/share/hugpy/arms.d/agent.json` and starts the system unit
  `hugpy-agent.service` (`hugpy-agent console-serve`).
- It adds `/usr/bin/hugpy-agent` only if nothing else already provides that
  path.

Overrides go in `/etc/hugpy/agent.env`, for example
`HUGPY_AGENT_CONSOLE_PORT` or `HUGPY_AGENT_ARTIFACTS_DIR`.

**The agent runtime and CLIs (pip).**

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e ../hugpy_tools -e ../hugpy_terminal -e ../abstract_toolserver -e '.[console,mct,serve]'   # from this tree
hugpy-agent models
```

Python 3.10 or later is required. The base dependencies are `hugpy-tools`,
`abstract-toolserver>=0.0.54` and `hugpy-harness>=0.1.7,<0.2`. The extras are
optional:

| extra | adds | needed for |
|---|---|---|
| `[console]` | `abstract-flask`, `flask`, `flask-cors` | `console-serve` (the /fleet arm) |
| `[serve]` | nothing (kept so existing `hugpy-agent[serve]` installs resolve) | — the session server is the harness's, a base dependency |
| `[mct]` | `jsonschema` | `mct`, `mct-serve` |

**A user service with `bootstrap.sh`.**
`bash bootstrap.sh --central URL [--key K] [--src DIR | --package SPEC]` sets
up `~/hugpy-agent/venv`, a 0600 `~/.config/hugpy-agent/agent.env`, linger, and
a systemd **user** unit. That unit runs `hugpy-agent serve` (the :9126 session
server over the harness-resolved wrapper/fleet endpoint). Only the older
provider-neutral service (`serve --profiles PATH`) needs a profiles file.

**Install links.** `install/install_hugpy_agent.py` is the cross-platform
installer behind the console's install links. **Pending:** the /fleet arm
mints these links, but nothing in this tree serves
`/agent/install/<id>.sh|.ps1|.zip` yet.

## Use

```sh
hugpy-agent run "Describe this project and write report.md"   # one task; report JSON on stdout
hugpy-agent chat                                               # interactive REPL (task-mode turns)
hugpy-agent resume <run_id>                                    # continue an interrupted run
hugpy-agent models                                             # fleet models
hugpy-agent launch [--harness claude-code|qwen-code|hermes|aider] [--yolo]   # OpenCode by default
hugpy-agent console                                            # headless fleet cockpit
hugpy-agent tui                                                # client for :9124/:9125/:9126 serves
hugpy-agent tools health | list | call NAME --json '{...}'     # toolserver client
hugpy-agent console-serve                                      # the /fleet arm on 127.0.0.1:7342
hugpy-agent serve                                              # harness session server on 127.0.0.1:9126
hugpy-agent serve --profiles profiles.json                     # older provider-neutral service (API / native-CLI profiles)
hugpy-agent serve --daemon --task-source queue                 # task daemon (queue / discord-inbox / --node)
```

All subcommands: `launch`, `run`, `case`, `chat`, `resume`, `models`, `runs`,
`eval`, `console`, `harness`, `mct`, `mct-serve`, `mct-usage`, `mct-fs`,
`serve`, `console-serve`, `seat-report`, `tui` and `tools`. Each is described in
[the CLI reference](../../docs/agent-and-harness.md#hugpy-agent).

Behaviors to know:

- **Interrupts.** The first Ctrl-C stops at the next step boundary and prints
  the resume command. A second Ctrl-C quits. The journal stays consistent
  either way.
- **`hugpy-agent runs`** currently reads the harness's journal path instead of
  `.hugpy_agent/journal.db`, so it does not list agent runs. Use the run_id
  printed at `run_start` instead.
- **`serve` has three modes.** Bare `serve` runs the harness session server
  on :9126 with the agent's loop and gateway (`hugpy-harness serve` route
  vocabulary; `hugpy-agent tui --serve http://127.0.0.1:9126` is its console).
  `serve --profiles PATH` runs the older provider-neutral session service
  (third-party API and native-CLI profiles), kept while station loci still use
  it. `serve --daemon` runs the task daemon. `serve --console` (the shared
  abstract-serve-core console) was retired on 2026-10-20.

## Configuration

The workspace is resolved first: `--workspace`, then `HUGPY_WORKSPACE`, then
cwd. Then these layers apply, highest first:

1. CLI flags
2. the process environment
3. `<workspace>/.env`
4. `<workspace>/agent.toml`
5. built-in defaults

`~/.hugpy/.env` (`$HUGPY_HOME/.env`) fills in variables that the shell does not
export. Secrets belong in the environment or `.env`, never in `agent.toml`.

| variable | default | notes |
|---|---|---|
| `HUGPY_BASE` | `http://127.0.0.1:7002` (env, then `~/.hugpy/.env`, then default) | fleet base. It may end in `/api` or `/v1`, or be a bare origin. On ae, the core is `:7020`. |
| `HUGPY_API_KEY` | none | Bearer token, sent whenever set |
| `HUGPY_AGENT_BRAIN` / `HUGPY_MODEL` | `Qwen~Qwen3-Coder-Next-GGUF` | `HUGPY_AGENT_BRAIN` wins. `HUGPY_AGENT_BRAINS` (csv) sets a warm-first ladder. |
| `HUGPY_POLICY` | `ask` | `readonly`, `ask` or `auto`. `ask` with no operator channel fails closed to deny. |
| `HUGPY_DISCORD_SESSION` | none | operator channel for `ask_operator` and approvals |
| `HUGPY_TOOLS_MODE` | `prompted` | only `auto` and `native` send the native-tools probe, which is cached per box |
| `HUGPY_MAX_STEPS` / `HUGPY_MAX_TOKENS` / `HUGPY_TIMEOUT` | 25 / 1024 / 300 | |
| `HUGPY_RAG` | `true` | RAG index in `.hugpy_agent/memory_vectors.db`; `recall` exists only with RAG |
| `HUGPY_AGENT_TOOLSERVER` | on | `ts_categories`, `ts_list` and `ts_call` when a toolserver is reachable. Privileged tools need `HUGPY_AGENT_TOOLSERVER_ALLOW`. |
| `HUGPY_SESSION_SIGNALS` | on | `0` disables session identity headers and leases |

The full table, with every variable, is in the
[configuration reference](../../docs/agent-and-harness.md#configuration-reference).

**Per-workspace state** lives in `<workspace>/.hugpy_agent/`: `journal.db`,
`audit.jsonl`, `memory_vectors.db`, `traces/`, `tasks.queue` and
`node_state.json`. Remembered facts live in `<workspace>/memory/` with an
index in `MEMORY.md`.

## The harness core

hugpy-agent does not fork the loop. `runtime/loop.py` subclasses
`hugpy_terminal.loop.AgentLoop` and supplies these hooks:

- `rag_factory`: the fleet-embedding RAG index.
- `comms_factory`: Discord operator comms.
- `catalog_provider`: the curated `toolset_tools` catalog.
- `extra_tool_providers`: http, fleet ML and generation, the lean kit,
  toolserver, `remember`/`recall` and `spawn`.
- An override of `turn_ctx`, which reports one turn to central per drive.

`gateway.py` subclasses `hugpy_terminal.gateway.Gateway` and implements
`request_ctx`, which adds identity headers and a lease to every model call and
cancels the call on abort. `Config.state_dir = ".hugpy_agent"` relocates the
journal and the audit log.

Modules that did not change are re-exported straight from the harness:
`adapter`, `branding`, `journal`, `memory`, `policy`, `tools/fs` and
`tools/shell`, plus parts of `serve_client` and `tui`.

The agent therefore inherits the harness's chat-loop fixes:

- an empty or failed reply aborts, with no re-injection;
- `<tool_call>` and `<tool_response>` markup is gated out of reply text;
- each user message gets its own step budget;
- an invalid `final_answer` is repaired.

Of these, the per-message budget and the abort-on-empty-reply rule apply in
**chat mode**, which is what the :9126 session service uses. `hugpy-agent chat`
runs task-mode turns.

See [the hooks a host uses](../../docs/agent-and-harness.md#the-hooks-a-host-uses).

## The /fleet console

`console-serve` serves:

- `GET /` and `/fleet`, which redirect to `/fleet/`;
- `/fleet/*`, the built `@hugpy/agents-ui` SPA from `hugpy_agent/ui_dist/`,
  with an SPA fallback;
- `/api/agent/<route>` (GET or POST), which returns `{"result": ...}`.

| route | purpose |
|---|---|
| `health`, `manifest` | liveness and identity; the arm and route manifest with input schemas |
| `console_info`, `console_file` | newest console `.deb` and agent `.whl` in `HUGPY_AGENT_ARTIFACTS_DIR`, with size and sha256; download |
| `install_links`, `install_links_mint`, `install_links_revoke`, `install_links_remove`, `install_links_prune` | the install-link ledger in `HUGPY_AGENT_LINKS_DIR` |

The arm identity is `agent`. hugpy-core discovers it through
`/usr/share/hugpy/arms.d/agent.json` and reverse-proxies `/fleet/` and
`/api/agent/*` to it; see [`docs/arms.md`](../../docs/arms.md). The same toolset
is also exposed through entry points for an in-process host:
`hugpy.toolsets: agent` and `hugpy.arms: agent` (`hugpy_agent.routes:fleet_bp`).

The console arm is a different service from the **:9126 session server**.
The session server runs the harness's named chat sessions (stdlib HTTP,
loopback by default, not proxied by core); its routes are documented in the
hugpy-harness README under "Serve". Registering a session with core so it has
a fleet route is the agent's inbound job and is tracked separately.

## Layout

```
src/hugpy_agent/
  imports/      constants, functions, classes, init_imports (shared layer)
  runtime/      loop (over the harness), session_signals, subagent, trace, audit, emergency,
                journal/memory/policy (harness re-exports)
  eval/         eval suite + steward eval/gate + benchmarks
  fleet/        cockpit (fleet_console, fleet_tui, fleet_benchmark) + /fleet arm backends
                (install_links, console_artifacts)
  tools/        providers over the harness registry: toolset_tools, http, fleet, lean, toolserver
  service/      :9126 session service
  serve_client/ tui/ mct/ _vendor/steward/ ui_dist/
  cli.py config.py gateway.py comms.py rag.py serve.py node.py install.py console.py
  frontends.py harness_settings.py toolserver_client.py toolset.py routes.py serve_fleet.py ui.py
  loop.py journal.py memory.py policy.py audit.py subagent.py trace.py session_signals.py
  emergency.py fleet_console.py fleet_tui.py fleet_benchmark.py steward_*.py
  existing_system_benchmark.py   <- top-level alias shims to the subpackages above
evals/          operator surface for the eval suite (tasks.py, runner.py, results/)
examples/       serve-profiles.json, hugpy-agent-serve.service
packaging/debian/  build.sh, postinst, prerm, postrm
bin/hugpy-dispatch, bootstrap.sh, install/install_hugpy_agent.py
tests/          unittest suite
```

## Development

```sh
python -m unittest discover -s tests                     # offline; tests/_bootstrap.py adds src/ + ../hugpy_terminal
HUGPY_AGENT_LIVE=1 python scripts/live_smoke.py          # 3 tiny live calls
BUILD_PYTHON=/usr/bin/python3 PIP_CACHE_DIR=/var/tmp/pip-cache-$USER bash packaging/debian/build.sh
```

The deb build needs the built UI in `src/hugpy_agent/ui_dist/`, which comes
from `ui/agents_ui`. It bundles the sibling `hugpy_tools`,
`abstract_toolserver` and `hugpy_terminal` source as wheels.

The suite has known pre-existing failures, and the release flow and UI build
steps are documented elsewhere; see
[`docs/development.md`](../../docs/development.md).


<!-- hugpy-public-downloads-gallery -->
## Downloads and feature gallery

Get the Debian packages, architecture details, SHA-256 checksums and installation commands from [Hugpy Downloads](https://hugpy.ai/downloads/). The [feature gallery](https://hugpy.ai/downloads/#features) shows the console, Agent, Station, Media and Video interfaces. Screenshots reflect the versions captured; the current interface may differ.

<details>
<summary>Fleet and agent setup</summary>

The agent side of the fleet, with account-based setup and install links.

![Fleet and agent setup](https://hugpy.ai/downloads/gallery/hugpy/hugpy-fleet.png)

</details>

<details>
<summary>Agent startup</summary>

The terminal agent connecting to its local runtime.

![Agent startup](https://hugpy.ai/downloads/gallery/hugpy-agent/hugpy-agent.png)

</details>

<details>
<summary>Agent terminal sessions</summary>

Roles, persistent sessions, conversations and tool activity in the terminal.

![Agent terminal sessions](https://hugpy.ai/downloads/gallery/hugpy-agent/hugpy-agent-tui.png)

</details>

Generic file, data, text and local-search operations are imported directly from
`hugpy-resources`. The Tools package supplies capability registration and web
backends; legacy generic Tools module paths are no longer used.
