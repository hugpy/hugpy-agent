"""hugpy-agent CLI: run | chat | resume | models | runs | serve.

SIGINT handling lives here (not in the loop): the first Ctrl-C requests a
graceful stop at the next step boundary — the journal stays consistent and
the run is resumable; a second Ctrl-C falls through to the default handler
for a hard exit (the journal is still safe: every write is committed).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys

from .config import load_config
from .gateway import Gateway
from .journal import Journal
from .loop import AgentLoop, default_journal_path


def _add_common(p: argparse.ArgumentParser, model: bool = True) -> None:
    p.add_argument("--base", help="fleet base URL (default env HUGPY_BASE or dev)")
    if model:
        # `eval` owns a repeatable --model of its own, so it opts out here.
        p.add_argument("--model", help="model id (default env HUGPY_MODEL)")
    p.add_argument("--workspace", help="workspace dir (default env HUGPY_WORKSPACE or cwd)")
    p.add_argument("--max-steps", type=int, dest="max_steps")
    p.add_argument("--tools-mode", dest="tools_mode",
                   choices=["auto", "native", "prompted", "constrained"])
    p.add_argument("--policy", dest="policy_mode",
                   choices=["readonly", "ask", "auto"],
                   help="permission mode (default env HUGPY_POLICY or 'ask'; "
                        "'ask' escalates to the operator via "
                        "HUGPY_DISCORD_SESSION and fails closed to deny "
                        "when no channel is configured)")
    p.add_argument("--audit-verbose", dest="audit_verbose",
                   action="store_true", default=None,
                   help="store truncated plaintext args/results in the audit "
                        "log (default: sha256 hashes only; see "
                        "HUGPY_AUDIT_LOG)")
    # Think suppression is ON by default (Qwen3-family brains are unusable for
    # tool-calling otherwise). --think turns it OFF for a reasoning-heavy model.
    p.add_argument("--think", dest="no_think", action="store_false",
                   default=None,
                   help="let the model think (disables /no_think suffix)")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="only print the final report JSON")


def _cfg(args) -> "Config":
    # `eval` carries a repeatable --model (a list); the base config takes no
    # single model there (score_model sets it per model under test).
    model = getattr(args, "model", None)
    if isinstance(model, list):
        model = None
    return load_config(overrides={
        "base": getattr(args, "base", None),
        "model": model,
        "workspace": getattr(args, "workspace", None),
        "max_steps": getattr(args, "max_steps", None),
        "tools_mode": getattr(args, "tools_mode", None),
        "no_think": getattr(args, "no_think", None),
        "policy_mode": getattr(args, "policy_mode", None),
        "audit_verbose": getattr(args, "audit_verbose", None),
        "task_source": getattr(args, "task_source", None),
        "task_queue": getattr(args, "task_queue", None),
        "poll_interval": getattr(args, "poll_interval", None),
        "agent_node": getattr(args, "agent_node", None),
        "agent_central": getattr(args, "agent_central", None),
    })


def _printer(quiet: bool):
    """Human-readable progress on stderr; stdout is reserved for the report
    JSON so `hugpy-agent run ... | jq` composes."""
    def on_event(kind, *a):
        if quiet:
            return
        if kind == "assistant":
            print("\n[assistant]\n%s" % a[0], file=sys.stderr)
        elif kind == "tool":
            name, args, result, replayed = a
            tag = " (replayed from journal)" if replayed else ""
            print("[tool%s] %s(%s)\n  -> %s"
                  % (tag, name, json.dumps(args)[:300], str(result)[:500]),
                  file=sys.stderr)
        elif kind == "repair":
            print("[repair round-trip] %s" % a[0], file=sys.stderr)
        elif kind == "nudge":
            print("[nudge] model replied without a tool call", file=sys.stderr)
        elif kind == "compaction":
            print("[compaction] summarized %d messages" % a[1], file=sys.stderr)
        elif kind == "chat_error":
            print("[chat error] %s" % a[0], file=sys.stderr)
        elif kind in ("run_start", "resume"):
            print("[%s] run_id=%s" % (kind, a[0]), file=sys.stderr)
        elif kind == "mode":
            print("[mode] %s" % a[0], file=sys.stderr)
        # second-in-line brain events: the run-start choice and the ONE
        # mid-run capacity fallback (a WARNING — the operator's primary
        # brain refused for capacity and the run switched for good).
        elif kind == "brain":
            print("[brain] using %s — %s" % (a[0], a[1]), file=sys.stderr)
        elif kind == "brain_fallback":
            print("[brain] WARNING: capacity refusal from the active brain; "
                  "switching to %s for the rest of the run (%s)"
                  % (a[0], a[1]), file=sys.stderr)
        elif kind == "policy":
            print("[policy] %s -> %s" % (a[0], a[1]), file=sys.stderr)
        elif kind == "ask":
            print("[ask] %s -> operator: %s" % (a[0], a[1]), file=sys.stderr)
        elif kind == "audit_error":
            print("[audit error] %s" % a[0], file=sys.stderr)
        # serve daemon events (P2.7) — these are what journalctl shows.
        elif kind == "serve":
            print("[serve] %s (poll every %ss)" % (a[0], a[1]), file=sys.stderr)
        elif kind == "heartbeat":
            print("[heartbeat] %s" % a[0], file=sys.stderr)
        elif kind == "task_start":
            print("[task] %s" % a[0][:200], file=sys.stderr)
        elif kind == "task_done":
            print("[task done] outcome=%s run=%s steps=%s"
                  % (a[0].get("outcome"), a[0].get("run_id"),
                     a[0].get("steps")), file=sys.stderr)
        elif kind == "serve_error":
            print("[serve error] %s" % a[0], file=sys.stderr)
        elif kind == "serve_exit":
            print("[serve exit] %s" % json.dumps(a[0]), file=sys.stderr)
        elif kind == "final":
            print("\n[final answer]\n%s" % a[0], file=sys.stderr)
        # eval harness events (P3.4).
        elif kind == "eval_model":
            print("\n[eval] scoring model %s" % a[0], file=sys.stderr)
        elif kind == "eval_ready":
            print("[eval ready] %s" % a[0], file=sys.stderr)
        elif kind == "eval_model_blocked":
            print("[eval BLOCKED] %s: %s" % (a[0], a[1]), file=sys.stderr)
        elif kind == "eval_task":
            print("[eval task] %s -> passed=%s outcome=%s steps=%s"
                  % (a[0], a[1].get("passed"), a[1].get("outcome"),
                     a[1].get("steps")), file=sys.stderr)
    return on_event


def _install_sigint(loop: AgentLoop) -> None:
    def handler(signum, frame):
        if loop.stop_requested:            # second Ctrl-C: hard exit
            signal.signal(signal.SIGINT, signal.default_int_handler)
            raise KeyboardInterrupt
        loop.stop_requested = True
        print("\n[interrupt] finishing the current step, then stopping — "
              "resume with `hugpy-agent resume <run_id>` (Ctrl-C again to "
              "force-quit)", file=sys.stderr)
    signal.signal(signal.SIGINT, handler)


def cmd_run(args) -> int:
    cfg = _cfg(args)
    loop = AgentLoop(cfg, on_event=_printer(args.quiet))
    _install_sigint(loop)
    report = loop.run(args.task)
    print(json.dumps(report, indent=2))
    return 0 if report.get("outcome") == "done" else 1


# --- case (k95): sentinel-spawned one-shot diagnosis run --------------------
#
# The sentinel (abstract_hugpy_dev.sentinel.runner) spawns exactly one of
# these per opened case. The profile is pinned HERE as CLI-layer overrides —
# the strongest layer in load_config — so no workspace .env/agent.toml or
# process environment can widen it: readonly mode denies every non-readonly
# tool; fs_write and http_fetch are allowed back in (fs_write is jailed to
# the workspace, which `case` forces to the case dir, and http_fetch is how
# the agent reads central's /llm + /oracle surfaces to diagnose); the deny
# list re-closes the mutation-shaped tools even against an explicit
# HUGPY_TOOL_ALLOW in the environment (deny beats allow beats mode).

CASE_TOOL_ALLOW = ["fs_write", "http_fetch"]
CASE_TOOL_DENY = ["shell", "spawn", "generate_image", "generate_scene",
                  "lean_deliver", "lean_digest", "lean_logs", "remember"]


def cmd_case(args) -> int:
    if args.brief == "-":
        brief = sys.stdin.read()
    else:
        with open(args.brief, "r", encoding="utf-8") as fh:
            brief = fh.read()
    if not brief.strip():
        print("case: empty brief", file=sys.stderr)
        return 2
    cfg = load_config(overrides={
        "base": getattr(args, "base", None),
        "model": getattr(args, "model", None),
        "workspace": args.case_dir,
        "max_steps": getattr(args, "max_steps", None),
        "tools_mode": getattr(args, "tools_mode", None),
        "no_think": getattr(args, "no_think", None),
        "policy_mode": "readonly",
        "tool_allow": CASE_TOOL_ALLOW,
        "tool_deny": CASE_TOOL_DENY,
    })
    loop = AgentLoop(cfg, on_event=_printer(args.quiet))
    _install_sigint(loop)
    report = loop.run(brief)
    print(json.dumps(report, indent=2))
    return 0 if report.get("outcome") == "done" else 1


def cmd_resume(args) -> int:
    cfg = _cfg(args)
    loop = AgentLoop(cfg, on_event=_printer(args.quiet))
    _install_sigint(loop)
    report = loop.resume(args.run_id)
    print(json.dumps(report, indent=2))
    return 0 if report.get("outcome") == "done" else 1


def cmd_chat(args) -> int:
    cfg = _cfg(args)
    loop = AgentLoop(cfg, on_event=_printer(quiet=False))
    run_id = loop.start_chat()
    print("hugpy-agent chat — model %s @ %s (run %s). Ctrl-D or 'exit' to quit."
          % (cfg.model, cfg.base, run_id), file=sys.stderr)
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            break
        if not line:
            continue
        if line.lower() in ("exit", "quit"):
            break
        loop.send_user(run_id, line)
        loop.stop_requested = False
        report = loop._drive(run_id)
        if report.get("outcome") == "done":
            # Keep the run alive for the next turn — 'done' here just means
            # the model delivered this turn's final_answer.
            loop.journal.set_run_status(run_id, "running")
            print(report.get("answer", ""))
        else:
            print("[turn ended: %s] %s" % (report.get("outcome"),
                                           report.get("error", "")),
                  file=sys.stderr)
    return 0


def cmd_serve(args) -> int:
    """The daemon mode the systemd unit runs (P2.7). Graceful stop: the
    FIRST SIGTERM/SIGINT lets the current task finish (the journal stays
    consistent, the outcome still gets reported), then run() returns and we
    exit 0 — under Restart=on-failure the unit stays stopped. A second
    signal falls through to the default handler for a hard exit (every
    journal write is committed, so this is still safe)."""
    from .serve import Daemon
    cfg = _cfg(args)
    daemon = Daemon(cfg, on_event=_printer(args.quiet))

    def handler(signum, frame):
        if daemon.stop_requested:          # second signal: hard exit
            signal.signal(signum, signal.SIG_DFL)
            signal.raise_signal(signum)
            return
        daemon.stop_requested = True
        print("\n[serve] stop requested — finishing the current task, then "
              "exiting (signal again to force-quit)", file=sys.stderr)
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)

    summary = daemon.run(max_cycles=args.max_cycles)
    print(json.dumps(summary))
    return 0


def cmd_eval(args) -> int:
    """Per-model eval scorecard (P3.4). Runs the built-in suite against each
    --model through the real agent loop, gating each model on a chat token-echo
    readiness round-trip (NEVER on a 200 or a serving flag — a loading worker
    returns a 200 error body). Writes JSON + a table to --out and prints the
    table. Exit 0 iff every requested model became ready."""
    from . import eval as evalmod
    if not args.model:
        print(json.dumps({"error": "eval needs at least one --model "
                          "(repeatable), e.g. --model A --model B"}))
        return 2
    cfg = _cfg(args)
    tasks = evalmod.DEFAULT_TASKS
    if args.task:
        picked = [evalmod.task_by_name(n) for n in args.task]
        missing = [n for n, t in zip(args.task, picked) if t is None]
        if missing:
            print(json.dumps({"error": "unknown task(s): %s; available: %s"
                              % (missing, [t.name for t in evalmod.DEFAULT_TASKS])}))
            return 2
        tasks = picked
    # ready-timeout (seconds) budget -> polite poll count at the given cadence.
    poll = max(1.0, float(args.ready_poll))
    tries = max(1, int(args.ready_timeout / poll)) if args.ready_timeout else 1
    cards = evalmod.run_scorecard(
        args.model, cfg, tasks,
        ready_tries=tries, ready_poll=poll,
        gate_ready=not args.no_ready_gate,
        on_event=_printer(args.quiet))
    out_dir = args.out or os.path.join(cfg.workspace, "evals", "results")
    json_path, table_path = evalmod.write_results(cards, out_dir)
    table = evalmod.format_table(cards)
    print(table)
    print("\nwrote %s\n      %s" % (json_path, table_path), file=sys.stderr)
    return 0 if all(c.ready for c in cards) else 1


def cmd_console(args) -> int:
    """`hugpy-agent console` — exec OpenCode as the fleet's TUI face (see
    console.py for the doctrines: optional peer never auto-installed, key
    written as an env reference never a literal, model map generated live
    from /v1/models honoring operator BLOCKs). On success this call never
    returns — the process execs into OpenCode."""
    from . import console as consolemod
    cfg = _cfg(args)
    # --claude-code / --qwen-code are shorthands for --frontend <name>.
    if getattr(args, "claude_code", False):
        frontend = "claude-code"
    elif getattr(args, "qwen_code", False):
        frontend = "qwen-code"
    else:
        frontend = getattr(args, "frontend", "opencode")
    try:
        return consolemod.run_console(
            cfg,
            workspace=args.console_workspace,
            sync=args.sync,
            offline=args.offline,
            model=getattr(args, "model", None),
            print_config=args.print_config,
            frontend=frontend,
            all_models=getattr(args, "all_models", False))
    except consolemod.ConsoleError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_mct(args) -> int:
    """`hugpy-agent mct` — launch the Mediated Context Terminal (the mct
    subpackage's pointer-mediated REPL): a confined Claude (A) answers only
    through B's curated context. Equivalent to `python -m hugpy_agent.mct`."""
    from .mct.repl import run
    return run(args.workspace, model=args.model, use_model=not args.no_model,
               allow_fs_requests=args.allow_fs_requests)


def cmd_mct_usage(args) -> int:
    """`hugpy-agent mct-usage` — one JSON document of precise token/cost
    accounting plus cache-shadow timing for an MCT workspace. Read-oriented and
    machine-consumed (the fleet console's steward drawer polls it); safe to run
    while a REPL holds the workspace."""
    import json as _json
    import sqlite3
    import time
    from datetime import datetime, timezone
    from pathlib import Path

    ws = Path(args.workspace)
    db = ws / ".hugpy_agent" / "mct" / "mct.db"
    if not db.exists():
        print(_json.dumps({"workspace": str(ws), "sessions": [],
                           "error": "no MCT workspace here yet"}))
        return 0
    from .mct.session import BrokerConfig, BrokerServer
    srv = BrokerServer(ws, config=BrokerConfig(event_log=False))
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        sessions = [r[0] for r in con.execute(
            "SELECT session_id FROM sessions ORDER BY created_at")]
        out = []
        for sid in sessions:
            row = con.execute(
                "SELECT MAX(created_at) FROM objects WHERE session_id=? "
                "AND kind='a_transcript'", (sid,)).fetchone()
            last_at, cache = row[0] if row else None, None
            if last_at:
                try:
                    ts = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
                    age = max(0.0, time.time() - ts.timestamp())
                    # 5-min TTL, refreshed by each confirmed read (design §12):
                    # never claim provider knowledge — this is the shadow's
                    # expected state, not a confirmed one.
                    cache = {"last_a_turn_at": last_at,
                             "age_sec": round(age, 1), "ttl_sec": 300,
                             "state": "expected-valid" if age < 300 else "expired"}
                except ValueError:
                    pass
            per_turn = srv.tokens.per_turn(sid)
            out.append({"session_id": sid,
                        "report": srv.tokens.report(sid),
                        "per_turn": per_turn[-args.turns:],
                        "cache": cache})
        con.close()
        print(_json.dumps({"workspace": str(ws), "sessions": out}))
        return 0
    finally:
        srv.close()


def cmd_mct_fs(args) -> int:
    """`hugpy-agent mct-fs` — read or set the frontier filesystem policy for an
    MCT workspace: whether the frontier model (A) may reach the filesystem at
    all, and WHICH directories. This is what the fleet console's directory-
    accessibility button drives (symmetric with `mct-usage`, which it polls).

    No mutating flags => print the current policy JSON. Otherwise apply the
    changes and print the resulting policy. Changes take effect on the running
    session's next A turn (the broker re-reads this file live)."""
    import json as _json
    from .mct import fs_policy as _fp

    ws = args.workspace
    changed = False
    if args.allow is not None:
        _fp.set_allow(ws, args.allow == "on"); changed = True
    for spec in (args.add_root or []):
        if "=" not in spec:
            print(_json.dumps({"error": f"--add-root expects NAME=PATH, got {spec!r}"}))
            return 2
        name, path = spec.split("=", 1)
        _fp.add_root(ws, name, path); changed = True
    for name in (args.remove_root or []):
        _fp.remove_root(ws, name); changed = True
    print(_json.dumps(_fp.load_policy(ws), indent=2))
    return 0


def cmd_models(args) -> int:
    cfg = _cfg(args)
    gw = Gateway.from_config(cfg)
    try:
        entries = gw.models()
    except Exception as exc:
        print(json.dumps({"error": "could not list models from %s: %s"
                          % (cfg.base, exc)}))
        return 1
    for e in entries:
        mid = e.get("id") or e.get("name") or "?"
        ctx = ""
        for k in ("context_length", "ctx_size", "n_ctx"):
            if e.get(k):
                ctx = "  ctx=%s" % e[k]
                break
        print("%s%s" % (mid, ctx))
    print("(%d models @ %s)" % (len(entries), gw.resolve()[1]), file=sys.stderr)
    return 0


def cmd_runs(args) -> int:
    cfg = _cfg(args)
    journal = Journal(default_journal_path(cfg.workspace))
    for r in journal.list_runs():
        # subagent children (P2.5) name their spawning run
        link = ("  [child of %s]" % r["parent_run_id"]
                if r.get("parent_run_id") else "")
        print("%s  %-12s  %s%s" % (r["run_id"], r["status"],
                                   r["task"][:70], link))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="hugpy-agent",
        description="Portable agent runtime on the hugpy fleet")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run one task to completion")
    p.add_argument("task")
    _add_common(p)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("case", help="one-shot sentinel case run under the "
                                    "pinned document-only profile (readonly "
                                    "policy + case-dir-jailed fs_write + "
                                    "http_fetch; shell/spawn hard-denied)")
    p.add_argument("brief", help="path to the case-brief file, or - for stdin")
    p.add_argument("--case-dir", required=True,
                   help="case directory; becomes the workspace, so journal, "
                        "audit log and any fs_write stay inside it")
    # Deliberately NOT _add_common: --workspace and --policy must not exist
    # here — the case dir IS the workspace and the policy profile is pinned.
    p.add_argument("--base", help="fleet base URL (default env HUGPY_BASE or dev)")
    p.add_argument("--model", help="model id (default env HUGPY_MODEL)")
    p.add_argument("--max-steps", type=int, dest="max_steps")
    p.add_argument("--tools-mode", dest="tools_mode",
                   choices=["auto", "native", "prompted", "constrained"])
    p.add_argument("--think", dest="no_think", action="store_false",
                   default=None,
                   help="let the model think (disables /no_think suffix)")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="only print the final report JSON")
    p.set_defaults(fn=cmd_case)

    p = sub.add_parser("chat", help="interactive REPL")
    _add_common(p)
    p.set_defaults(fn=cmd_chat)

    p = sub.add_parser("resume", help="resume an interrupted run")
    p.add_argument("run_id")
    _add_common(p)
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("models", help="list fleet models")
    _add_common(p)
    p.set_defaults(fn=cmd_models)

    p = sub.add_parser("runs", help="list journaled runs in this workspace")
    _add_common(p)
    p.set_defaults(fn=cmd_runs)

    p = sub.add_parser("eval", help="score one or more models on the eval "
                                    "suite (comparative scorecard)")
    _add_common(p, model=False)
    p.add_argument("--model", dest="model", action="append", default=[],
                   help="model to score; repeatable (this overrides the "
                        "single-model --model of the other subcommands)")
    p.add_argument("--task", dest="task", action="append", default=[],
                   help="run only this task by name; repeatable (default: the "
                        "whole suite)")
    p.add_argument("--out", dest="out",
                   help="results dir (default <workspace>/evals/results)")
    p.add_argument("--ready-timeout", dest="ready_timeout", type=float,
                   default=1200.0,
                   help="seconds to politely poll for a model to become "
                        "servable before scoring it blocked (default 1200)")
    p.add_argument("--ready-poll", dest="ready_poll", type=float, default=20.0,
                   help="seconds between readiness polls (default 20)")
    p.add_argument("--no-ready-gate", dest="no_ready_gate", action="store_true",
                   help="skip the readiness round-trip (offline/testing)")
    p.set_defaults(fn=cmd_eval)

    p = sub.add_parser("console", help="launch OpenCode (optional npm peer) "
                                       "as the interactive fleet console")
    # Deliberately NOT _add_common: the console workspace is its OWN dir
    # (default ~/.hugpy_agent/console/, where opencode.json lives), distinct
    # from the agent-loop workspace, so --workspace here must not feed the
    # shared config resolver's workspace knob.
    p.add_argument("--base", help="fleet base URL (default env HUGPY_BASE or dev)")
    p.add_argument("--frontend", choices=["opencode", "claude-code", "qwen-code"],
                   default="opencode",
                   help="terminal frontend to launch (default opencode). "
                        "claude-code points Claude Code at the fleet's "
                        "Anthropic Messages shim (/v1/messages); qwen-code "
                        "points Qwen Code (a Claude-Code-style TUI, no "
                        "Anthropic anything) at the fleet's OpenAI /v1")
    p.add_argument("--claude-code", dest="claude_code", action="store_true",
                   help="shorthand for --frontend claude-code")
    p.add_argument("--qwen-code", dest="qwen_code", action="store_true",
                   help="shorthand for --frontend qwen-code")
    p.add_argument("--model", help="override the default model OpenCode opens with")
    p.add_argument("--workspace", dest="console_workspace",
                   help="console dir holding opencode.json "
                        "(default ~/.hugpy_agent/console/)")
    p.add_argument("--sync", dest="sync", action="store_true", default=True,
                   help="refresh the model map from the fleet before launch "
                        "(the default)")
    p.add_argument("--no-sync", dest="sync", action="store_false",
                   help="skip the model-map refresh; reuse the existing "
                        "opencode.json")
    p.add_argument("--offline", action="store_true",
                   help="no network at all: skip sync and launch on the "
                        "existing opencode.json")
    p.add_argument("--print-config", dest="print_config", action="store_true",
                   help="print the opencode.json that would be used, then "
                        "exit (no write on sync path, no launch)")
    p.add_argument("--all-models", dest="all_models", action="store_true",
                   help="list EVERY non-blocked fleet model in the picker, not "
                        "just chat-drivable ones (also HUGPY_CONSOLE_ALL_MODELS=1). "
                        "opencode only; a non-chat model selected here will fail")
    p.set_defaults(fn=cmd_console)

    p = sub.add_parser("mct", help="Mediated Context Terminal — pointer-mediated "
                                   "chat where a confined Claude answers only "
                                   "through B's curated context")
    p.add_argument("workspace", nargs="?",
                   default=os.path.expanduser("~/.mct/repl"),
                   help="workspace dir (a fresh dir = a new conversation)")
    p.add_argument("--model", default="sonnet",
                   help="A's model (e.g. sonnet, opus, haiku)")
    p.add_argument("--no-model", dest="no_model", action="store_true",
                   help="disable B's local ranking model")
    p.add_argument("--allow-fs-requests", action="store_true",
                   help="Allow Frontier filesystem requests (Steward trigger): "
                        "a missed pull may be brokered by B against granted "
                        "roots — through B, never direct filesystem access")
    p.set_defaults(fn=cmd_mct)

    p = sub.add_parser("mct-usage", help="JSON token/cost accounting + cache "
                                         "timing for an MCT workspace (polled "
                                         "by the fleet console steward drawer)")
    p.add_argument("workspace", nargs="?",
                   default=os.path.expanduser("~/.mct/repl"),
                   help="MCT workspace dir (default ~/.mct/repl)")
    p.add_argument("--turns", type=int, default=5,
                   help="how many most-recent per-turn rows to include")
    p.set_defaults(fn=cmd_mct_usage)

    p = sub.add_parser("mct-fs", help="read/set the frontier filesystem policy "
                                      "(allow/disallow + granted directories) for "
                                      "an MCT workspace — the console's directory-"
                                      "accessibility control")
    p.add_argument("workspace", nargs="?",
                   default=os.path.expanduser("~/.mct/repl"),
                   help="MCT workspace dir (default ~/.mct/repl)")
    p.add_argument("--allow", choices=["on", "off"],
                   help="allow (on) or disallow (off) frontier filesystem access")
    p.add_argument("--add-root", action="append", metavar="NAME=PATH",
                   help="grant a directory the frontier may reach (repeatable)")
    p.add_argument("--remove-root", action="append", metavar="NAME",
                   help="revoke a granted directory by name (repeatable)")
    p.set_defaults(fn=cmd_mct_fs)

    p = sub.add_parser("serve", help="daemon: poll the task source and run "
                                     "each task (what the systemd unit runs)")
    _add_common(p)
    p.add_argument("--task-source", dest="task_source",
                   choices=["discord-inbox", "queue"],
                   help="task source (default env HUGPY_TASK_SOURCE; none "
                        "configured => fail-closed idle + heartbeat)")
    p.add_argument("--queue", dest="task_queue",
                   help="queue file path for --task-source queue (default "
                        "<workspace>/.hugpy_agent/tasks.queue)")
    p.add_argument("--poll-interval", type=int, dest="poll_interval",
                   help="seconds between source polls (default env "
                        "HUGPY_POLL_INTERVAL or 10)")
    p.add_argument("--node", dest="agent_node", action="store_true",
                   default=None,
                   help="agent node mode (P3.2): enroll with central's "
                        "/agent/* registry, heartbeat, and run "
                        "operator-dispatched tasks — alongside --task-source "
                        "(both polled) or on its own (env HUGPY_AGENT_NODE)")
    p.add_argument("--central", dest="agent_central",
                   help="central base URL for /agent/* (default env "
                        "HUGPY_AGENT_CENTRAL, else HUGPY_BASE)")
    p.add_argument("--max-cycles", type=int, dest="max_cycles", default=None,
                   help="exit after N poll cycles (smoke/testing; default "
                        "run until stopped)")
    p.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
