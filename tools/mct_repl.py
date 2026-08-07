#!/usr/bin/env python3
"""MCT interactive terminal (design §20.8, §5).

An ordinary-feeling chat: you type, an answer appears. Underneath, B ingests your
message, curates a bounded context, and hands a pointer to A — a confined Claude
Code process that reads/pulls only through B and answers. The illusion breaks
explicitly only when it must (A unavailable, turn failed) per §5.2.

Usage:
  PYTHONPATH=src python3 tools/mct_repl.py [WORKSPACE] [--model sonnet] [--no-model]

In-session commands:
  /help                      show this help
  /policy <text>             set the governing instruction (always in context)
  /root <name> <path>        grant confined read access to a directory (persisted)
  /allow on|off              let B broker A's fs requests against granted roots
  /file <catalog> <root> <rel>   expose a file under a root as a pullable source
  /source <catalog> <text>   add an inline source A can pull by name
  /sources                   list the catalog A can pull from
  /trace                     why each fragment was included/omitted last turn
  /tokens                    precise token + cost accounting (per turn + session total)
  /metrics                   comprehensive dashboard: cache contents + every metric
  /acache                    A's durable cache — everything A read/received/produced
  /log a|b|c|all             full log of A, B (ledger), or C (conversation)
  /tail [n]                   last n lines of the live rolling log (mct.log)
  /map                       where every object physically lives (paths on disk)
  /where <pointer|id>        resolve any object handle to its file path + content
  /save-logs [dir]           write A/B/C/MAP logs to a directory
  /memory                    decisions B has extracted so far
  /model <name>              switch A's model (e.g. sonnet, opus, haiku)
  /exit                      quit
"""
from __future__ import annotations

import argparse
import atexit
import itertools
import os
import sys
import threading
import time
from pathlib import Path

try:  # line editing (backspace/arrows/Ctrl-A/E) + history for input()
    import readline
except ImportError:  # non-GNU platforms: input() still works, just bare
    readline = None

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hugpy_agent.mct.claude_adapter import ClaudeCodeAdapter
from hugpy_agent.mct.session import BrokerConfig, BrokerServer

DIM, BOLD, CYAN, YELLOW, RED, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[31m", "\033[0m"

# Readline needs non-printing prompt chars wrapped in \001…\002 so it can
# measure the visible width correctly (otherwise long lines wrap mid-word).
PROMPT = f"\001{BOLD}\002you>\001{RESET}\002 " if readline else f"{BOLD}you>{RESET} "


def _setup_readline():
    """Persistent cross-session input history under ~/.mct/."""
    if readline is None:
        return
    hist = Path.home() / ".mct" / "repl_history"
    hist.parent.mkdir(parents=True, exist_ok=True)
    try:
        readline.read_history_file(hist)
    except OSError:
        pass
    readline.set_history_length(1000)

    def _save():
        try:
            readline.write_history_file(hist)
        except OSError:
            pass
    atexit.register(_save)


class Spinner:
    """A tiny 'A is reasoning' indicator while the confined claude runs."""
    def __init__(self, label="A is reasoning"):
        self.label = label
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._spin, daemon=True)

    def _spin(self):
        if not sys.stdout.isatty():  # piped/logged: one static line, no animation
            sys.stdout.write(f"{DIM}{self.label}…{RESET}\n"); sys.stdout.flush()
            self._stop.wait(); return
        for ch in itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
            if self._stop.is_set():
                break
            sys.stdout.write(f"\r{DIM}{ch} {self.label}…{RESET}")
            sys.stdout.flush()
            time.sleep(0.08)
        sys.stdout.write("\r" + " " * (len(self.label) + 12) + "\r")
        sys.stdout.flush()

    def __enter__(self):
        self._t.start(); return self

    def __exit__(self, *a):
        self._stop.set(); self._t.join()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workspace", nargs="?", default=str(Path.home() / ".mct" / "repl"))
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--no-model", action="store_true", help="disable B's local ranking model")
    args = ap.parse_args()

    if not ClaudeCodeAdapter(None).available():
        print(f"{RED}The `claude` CLI is not on PATH — A cannot run.{RESET}")
        return 1

    state = {"model": args.model, "last": None}

    def render(body: str):  # B renders A's answer in the assistant position (§5.1)
        print(f"\n{CYAN}{body}{RESET}\n")

    server = BrokerServer(args.workspace, sink=render,
                          config=BrokerConfig(use_model=not args.no_model))
    sess = server.session(server.open_session("repl"))

    print(f"{BOLD}Mediated Context Terminal{RESET}  {DIM}workspace={args.workspace}  A=claude:{state['model']}{RESET}")
    print(f"{DIM}rolling log: {server.ledger.event_log_path}   (tail -f it){RESET}")
    print(f"{DIM}Type a message, or /help for commands. Ctrl-C cancels a turn, /exit quits.{RESET}\n")

    _setup_readline()
    while True:
        try:
            line = input(PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye."); break
        if not line:
            continue
        if line.startswith("/"):
            if _command(line, sess, server, state):
                break
            continue
        # ordinary turn
        try:
            with Spinner():
                r = sess.submit_via_claude(line, model=state["model"])
            state["last"] = r
            if r.state != "Committed":   # the illusion breaks explicitly (§5.2)
                print(f"{YELLOW}[A did not answer — B does not answer in its place.]{RESET}")
                print(f"{DIM}  reason: {r.error or r.state}")
                print(f"  inspect: /tail 20 · /log b · /acache{RESET}\n")
            else:
                _print_provenance_footer(sess, server, r.turn_id, r.tokens)
        except KeyboardInterrupt:
            print(f"\n{YELLOW}[turn cancelled]{RESET}\n")
        except Exception as exc:  # never crash the terminal on one bad turn
            print(f"{RED}[error: {type(exc).__name__}: {exc}]{RESET}\n")

    server.close()
    return 0


def _print_provenance_footer(sess, server, turn_id, tokens=None):
    """After every answer, make cost + provenance evident."""
    rec = server.a_cache.for_turn(sess.session_id, turn_id, include_content=False)
    resp = next((o for o in rec["outputs"] if o["role"] == "response"), None)
    reads = len(rec["reads"])
    pulls = sum(1 for o in rec["outputs"] if o["role"] == "pull_request")
    if tokens:
        t = tokens
        print(f"{DIM}  ├─ tokens: in={t['input']} cache-wr={t['cache_write']} "
              f"cache-rd={t['cache_read']} out={t['output']}  "
              f"| relayed={t['relayed_input']:,} ({t['cached_pct']}% cached)  "
              f"| cost=${t['cost_usd']:.4f}")
    print(f"{DIM}  ├─ A opened {reads} context objects (not host files), issued {pulls} pull(s)")
    if resp:
        print(f"{DIM}  ├─ answer document: {resp['path']}")
    if rec["transcript"]:
        print(f"{DIM}  ├─ A transcript:    {rec['transcript']['path']}")
    print(f"{DIM}  └─ detail: /tokens · /log all · /map · /acache · /where <id>{RESET}\n")


def _command(line: str, sess, server, state) -> bool:
    """Handle a /command. Returns True to exit."""
    parts = line.split()
    cmd, rest = parts[0], parts[1:]
    if cmd in ("/exit", "/quit"):
        print("bye."); return True
    elif cmd == "/help":
        print(__doc__)
    elif cmd == "/policy":
        sess.set_policy(" ".join(rest)); print(f"{DIM}policy set.{RESET}")
    elif cmd == "/root" and len(rest) == 2:
        try:
            # Validate first, then persist to fs_policy so the grant reaches
            # the MCP child (which rebuilds roots from the policy file, not
            # parent memory) and survives reopening the workspace.
            sess.register_root(rest[0], rest[1])
            from hugpy_agent.mct.fs_policy import add_root
            add_root(server.workspace_root, rest[0], rest[1])
            print(f"{DIM}root '{rest[0]}' -> {rest[1]}{RESET}")
        except Exception as e:
            print(f"{RED}{e}{RESET}")
    elif cmd == "/allow" and len(rest) == 1 and rest[0] in ("on", "off"):
        from hugpy_agent.mct.fs_policy import set_allow
        set_allow(server.workspace_root, rest[0] == "on")
        print(f"{DIM}frontier fs requests: {rest[0]}{RESET}")
    elif cmd == "/file" and len(rest) == 3:
        try:
            sess.register_source_file(rest[0], rest[1], rest[2])
            print(f"{DIM}source '{rest[0]}' registered.{RESET}")
        except Exception as e:
            print(f"{RED}{e}{RESET}")
    elif cmd == "/source" and len(rest) >= 2:
        sess.register_source(rest[0], " ".join(rest[1:]))
        print(f"{DIM}inline source '{rest[0]}' registered.{RESET}")
    elif cmd == "/sources":
        sess._materialize_file_sources()
        names = sorted(sess._catalog) or ["(none)"]
        print(f"{DIM}catalog: {', '.join(names)}{RESET}")
    elif cmd == "/trace":
        r = state["last"]
        if not r or not r.context_trace:
            print(f"{DIM}no turn yet.{RESET}")
        else:
            t = r.context_trace
            print(f"{DIM}included:{RESET}")
            for row in t["included"]:
                print(f"  + {row['role']:<22} {row.get('reason','')}")
            for row in t["omitted"][:5]:
                print(f"  {DIM}- {row['role']:<22} {row['reason']}{RESET}")
    elif cmd == "/metrics":
        print(server.metrics.render(sess.session_id))
    elif cmd == "/acache":
        # A's durable cache: everything Claude Code read/received/produced.
        print(f"{DIM}{server.a_cache.stats(sess.session_id)}{RESET}")
        print(server.a_cache.dump(sess.session_id))
    elif cmd == "/log" and rest:
        who = rest[0].lower()
        r = {"c": server.logs.render_c, "b": server.logs.render_b,
             "a": server.logs.render_a}
        if who == "all":
            for fn in (r["c"], r["b"], r["a"]):
                print(fn(sess.session_id) + "\n")
        elif who in r:
            print(r[who](sess.session_id))
        else:
            print(f"{RED}usage: /log a|b|c|all{RESET}")
    elif cmd == "/save-logs":
        out = rest[0] if rest else "./mct-logs"
        paths = server.logs.write_all(sess.session_id, out)
        print(f"{DIM}wrote {paths}{RESET}")
    elif cmd == "/tail":
        n = int(rest[0]) if rest and rest[0].isdigit() else 20
        p = server.ledger.event_log_path
        try:
            lines = p.read_text().splitlines()[-n:]
            print(f"{DIM}{p}{RESET}")
            print("\n".join(lines))
        except OSError:
            print(f"{RED}no log yet{RESET}")
    elif cmd == "/tokens":
        print(server.tokens.render(sess.session_id))
    elif cmd == "/map":
        print(server.logs.render_map(sess.session_id))
    elif cmd == "/where" and rest:
        w = server.logs.where(sess.session_id, rest[0])
        print(f"{DIM}{w}{RESET}" if w else f"{RED}unknown object{RESET}")
    elif cmd == "/memory":
        facts = server.compaction.facts(sess.session_id)
        if not facts:
            print(f"{DIM}no derived memory yet.{RESET}")
        for f in facts:
            print(f"  {DIM}[{f.kind}]{RESET} {f.text}")
    elif cmd == "/model" and rest:
        state["model"] = rest[0]; print(f"{DIM}A model -> {rest[0]}{RESET}")
    else:
        print(f"{RED}unknown or malformed command; /help for usage.{RESET}")
    return False


if __name__ == "__main__":
    sys.exit(main())
