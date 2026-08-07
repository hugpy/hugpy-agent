"""MCT interactive terminal (design §20.8, §5).

An ordinary-feeling chat: you type, an answer appears. Underneath, B ingests your
message, curates a bounded context, and hands a pointer to A — a confined Claude
Code process that reads/pulls only through B and answers. The illusion breaks
explicitly only when it must (A unavailable, turn failed) per §5.2.

Launch:
  python -m hugpy_agent.mct [WORKSPACE] [--model sonnet] [--no-model]
  hugpy-agent mct           [WORKSPACE] [--model sonnet] [--no-model]
(a fresh WORKSPACE dir = a new conversation; default ~/.mct/repl)

In-session commands:
  /help                      show this help
  /policy <text>             set the governing instruction (always in context)
  /root <name> <path>        grant confined read access to a directory
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
  /files [n] [cat]           who read/wrote which file, live (A = mediated, B =
                             host); 'cat' inlines the actual correspondence bytes
  /native none|off_host|all  A's native Claude Code tools ('all' lets A read and
                             write the host directly, outside B's ledger)
  /quiet [on|off]            toggle the inline A/B relay
  /model <name>              switch A's model (e.g. sonnet, opus, haiku)
  /frontier on|off           Frontier Keeper: Enabled/Disabled. Off: messages are
                             recorded + queued (B never answers in A's voice);
                             Local Keeper (/b) and your shell are unaffected
  /fsreq on|off  (= /allow)  Allow Frontier filesystem requests. Off (default):
                             A pulls only B-whitelisted sources. On: a missed
                             pull may be brokered by B against granted roots —
                             through B, never direct filesystem access
  /b <text>                  prompt B directly (the broker answers from its own
                             state via the hugpy fleet model; offline it answers
                             deterministically from the ledger/catalog)
  /bstate                    view B: policy, catalog, facts, epochs, token spend
  /bmodel <name>             switch B's model (fleet model id; default from config)
  /exit                      quit
"""
from __future__ import annotations

import argparse
import atexit
import itertools
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

try:  # line editing (backspace/arrows/Ctrl-A/E) + history for input()
    import readline
except ImportError:  # non-GNU platforms: input() still works, just bare
    readline = None

from hugpy_agent.mct.claude_adapter import ClaudeCodeAdapter
from hugpy_agent.mct.session import BrokerConfig, BrokerServer

DIM, BOLD, CYAN, YELLOW, RED, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[31m", "\033[0m"
MAGENTA = "\033[35m"   # B's voice — visually distinct from A's cyan
GREEN = "\033[32m"     # B answering A — distinct from B's host-side work

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


def _term_width(default: int = 100) -> int:
    try:
        return max(60, shutil.get_terminal_size((default, 24)).columns)
    except Exception:
        return default


def _elide(text: str, width: int) -> str:
    """Middle-elide a path so both the root and the filename stay readable —
    the head tells you which tree, the tail which file; nobody reads the middle."""
    if len(text) <= width or width < 12:
        return text[:width]
    keep = width - 1
    head = keep // 3
    return f"{text[:head]}…{text[-(keep - head):]}"


def _human(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    for unit, size in (("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return f"{n}B"


# Colour per side of the exchange so the conversation is scannable at a glance.
_ACTOR_STYLE = {"A->B": CYAN, "B->A": GREEN, "A": CYAN, "B": YELLOW}
_ARROW = {"A->B": "A→B", "B->A": "B→A"}

# OSC 8 hyperlink. Terminals that support it (VTE/GNOME, iTerm2, kitty, WezTerm,
# Windows Terminal, Konsole) make the text clickable; ones that don't ignore the
# sequence and show the text unchanged, so emitting it is safe by default.
# MCT_NO_LINKS=1 opts out for the rare emulator that renders OSC badly.
_LINKS = os.environ.get("MCT_NO_LINKS", "") not in ("1", "true", "yes")


def _link(text: str, url: str) -> str:
    if not _LINKS or not url or not sys.stdout.isatty():
        return text
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


class LiveFeed:
    """Relay the A↔B exchange into C, inline, as it happens.

    Follows ``access.jsonl`` rather than hooking the in-process AccessLog,
    because A's MCP server runs in a SEPARATE process with its own broker — an
    in-process callback would show B's half of the conversation and silently
    miss A's. Tailing the file is the only vantage point that sees both, and it
    is also what lets any other frontend (a TUI, a console) render the same
    stream without owning a broker handle.

    Nothing here interprets MCT semantics: each record already carries its host
    path and object pointer, so C stays a renderer."""

    def __init__(self, path, quiet: bool = False, label: str = "A is reasoning",
                 path_for=None):
        self.path = Path(path)
        self.quiet = quiet
        self.label = label
        # (session_id, object_id) -> blob path, so the object id on each line
        # can link to the immutable bytes A was actually served.
        self._path_for = path_for
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._offset = 0
        self._spin = itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")

    def _clear(self):
        if sys.stdout.isatty():
            sys.stdout.write("\r" + " " * (_term_width() - 1) + "\r")

    def _render(self, row: dict) -> str:
        actor = row.get("actor", "?")
        style = _ACTOR_STYLE.get(actor, "")
        who = _ARROW.get(actor, f"{actor} ")
        verb = str(row.get("verb", ""))[:11]
        size = _human(row.get("bytes")) if row.get("bytes") else ""
        oid = row.get("object", "")
        short = oid.rsplit("/", 1)[-1] if oid else ""
        tag = short[-6:]
        fixed = 4 + 4 + 12 + len(size) + len(tag) + 4
        shown = _elide(str(row.get("target", "")), max(20, _term_width() - fixed))
        # The PATH is the clickable thing — that is what an operator wants to
        # open. Elision is cosmetic: the link carries the full host path even
        # when the visible text is shortened, so a truncated line is still live.
        target = _link(shown, f"file://{row['path']}") if row.get("path") else shown
        right = f"  {DIM}{size}{RESET}" if size else ""
        # The object id stays clickable too, pointing at the immutable snapshot
        # — "the file now" and "the bytes A was served" are different questions.
        ref = ""
        if tag:
            url = ""
            if self._path_for:
                try:
                    p = self._path_for(row.get("session", ""), short)
                    url = f"file://{p}" if p else ""
                except Exception:
                    url = ""
            ref = f" {DIM}·{_link(tag, url)}{RESET}"
        return f"  {style}{who}{RESET} {DIM}{verb:<11}{RESET} {target}{right}{ref}"

    def _drain(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                fh.seek(self._offset)
                for line in fh:
                    if not line.endswith("\n"):       # partial write: retry later
                        break
                    self._offset += len(line.encode("utf-8"))
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not self.quiet:
                        self._clear()
                        sys.stdout.write(self._render(row) + "\n")
        except OSError:
            pass

    def _run(self):
        while not self._stop.is_set():
            self._drain()
            if sys.stdout.isatty() and not self.quiet:
                sys.stdout.write(f"\r{DIM}{next(self._spin)} {self.label}…{RESET}")
                sys.stdout.flush()
            self._stop.wait(0.1)

    def __enter__(self):
        try:    # start at EOF: this turn's records only
            self._offset = self.path.stat().st_size
        except OSError:
            self._offset = 0
        if not sys.stdout.isatty() and not self.quiet:
            sys.stdout.write(f"{DIM}{self.label}…{RESET}\n")
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join()
        self._drain()          # never drop the tail of the exchange
        self._clear()
        sys.stdout.flush()


def run(workspace: str, model: str = "sonnet", use_model: bool = True,
        allow_fs_requests: bool = False, quiet: bool = False,
        native_tools: str = "off_host") -> int:
    """Launch the Mediated Context Terminal against ``workspace``.

    A fresh workspace dir is a new conversation; reuse a dir to continue one.
    Returns a process exit code. Callable directly from the CLI subcommand and
    from ``python -m hugpy_agent.mct`` (via :func:`main`)."""
    if not ClaudeCodeAdapter(None).available():
        print(f"{RED}The `claude` CLI is not on PATH — A cannot run.{RESET}")
        return 1

    state = {"model": model, "last": None, "frontier": True, "queue": [],
             "quiet": quiet, "native": native_tools}

    def render(body: str):  # B renders A's answer in the assistant position (§5.1)
        print(f"\n{CYAN}{body}{RESET}\n")

    # An explicit --allow-fs-requests seeds the persistent policy (so the flag
    # and the console button share one truth); otherwise the workspace's saved
    # policy governs. server.session() applies the policy (allow flag + granted
    # roots) on open, and re-reads it live on every turn.
    from .fs_policy import set_allow, load_policy
    if allow_fs_requests:
        try:
            set_allow(workspace, True)
        except Exception:
            pass
    server = BrokerServer(workspace, sink=render,
                          config=BrokerConfig(use_model=use_model,
                                              allow_frontier_fs_requests=allow_fs_requests))
    sess = server.session(server.open_session("repl"))

    fsreq = "enabled" if server.config.allow_frontier_fs_requests else "disabled"
    print(f"{BOLD}Mediated Context Terminal{RESET}  {DIM}workspace={workspace}  A=claude:{state['model']}"
          f"  Frontier Keeper: Enabled  fs-requests: {fsreq}{RESET}")
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
        if not state["frontier"]:
            # Frontier Keeper disabled: record + queue; B never answers in A's
            # voice, and never implies A has seen this (design §control-state).
            server.store.commit(sess.session_id, line.encode("utf-8"),
                                media_type="text/plain", kind="operator_message",
                                provenance={"queued_while_frontier_disabled": True})
            state["queue"].append(line)
            print(f"{YELLOW}[Frontier Keeper: Disabled — message recorded and queued "
                  f"({len(state['queue'])} pending). A has NOT seen it.]{RESET}")
            print(f"{DIM}  /b <text> asks the Local Keeper instead · /frontier on resumes "
                  f"(queued messages ride the next turn as one ordered delta){RESET}\n")
            continue
        try:
            with LiveFeed(server.access.path, quiet=state["quiet"],
                          path_for=server.store.path_for):
                r = sess.submit_via_claude(_with_queued(state, line),
                                           model=state["model"],
                                           native_tools=state["native"])
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


def main(argv=None) -> int:
    """``python -m hugpy_agent.mct`` entry: parse args, then :func:`run`."""
    ap = argparse.ArgumentParser(description="Mediated Context Terminal (MCT)")
    ap.add_argument("workspace", nargs="?", default=str(Path.home() / ".mct" / "repl"),
                    help="workspace dir (a fresh dir = a new conversation)")
    ap.add_argument("--model", default="sonnet", help="A's model (e.g. sonnet, opus, haiku)")
    ap.add_argument("--no-model", dest="no_model", action="store_true",
                    help="disable B's local ranking model")
    ap.add_argument("--native-tools", dest="native_tools", default="off_host",
                    choices=["none", "off_host", "all"],
                    help="A's native Claude Code tools. off_host (default) adds "
                         "web+todo and bypasses nothing; all adds "
                         "Read/Grep/Edit/Write/Bash, letting A work without B")
    ap.add_argument("--quiet", action="store_true",
                    help="do not relay the A/B exchange inline (spinner only)")
    ap.add_argument("--allow-fs-requests", action="store_true",
                    help="Allow Frontier filesystem requests (Steward trigger): "
                         "a missed pull may be brokered by B against granted "
                         "roots — through B, never direct filesystem access")
    args = ap.parse_args(argv)
    return run(args.workspace, model=args.model, use_model=not args.no_model,
               allow_fs_requests=args.allow_fs_requests, quiet=args.quiet,
               native_tools=args.native_tools)


def _with_queued(state, line: str) -> str:
    """Fold messages queued while the Frontier Keeper was disabled into one
    ordered delta ahead of the current message. Order is preserved; nothing is
    deduplicated away (a correction must survive); later items govern."""
    q = state["queue"]
    if not q:
        return line
    state["queue"] = []
    delta = "\n".join(f"{i + 1}. {m}" for i, m in enumerate(q))
    return ("[Ordered delta — operator messages sent while the Frontier Keeper "
            "was disabled, in original order; later items and the current "
            "message supersede earlier ones where they conflict:]\n"
            + delta + "\n\n[Current message:]\n" + line)


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


def _b_state_text(sess, server, state) -> str:
    """One-screen view of B — what the broker itself knows right now."""
    lines = []
    pol = getattr(sess, "_policy_text", None) or "(none set)"
    lines.append(f"policy: {pol}")
    try:
        sess._materialize_file_sources()
        names = sorted(sess._catalog)
    except Exception:
        names = []
    lines.append(f"catalog ({len(names)}): {', '.join(names) or '(empty)'}")
    try:
        facts = server.compaction.facts(sess.session_id)
        lines.append(f"derived memory ({len(facts)}):")
        for f in facts[-8:]:
            lines.append(f"  [{f.kind}] {f.text}")
    except Exception:
        pass
    try:
        lines.append("tokens: " + server.tokens.render(sess.session_id).strip().splitlines()[-1])
    except Exception:
        pass
    r = state.get("last")
    if r is not None:
        lines.append(f"last turn: {r.turn_id} state={r.state}"
                     + (f" error={r.error}" if getattr(r, "error", None) else ""))
    lines.append(f"rolling log: {server.ledger.event_log_path}")
    return "\n".join(lines)


def _b_prompt(text: str, sess, server, state) -> str:
    """B answers as itself. Fleet-model-backed when the hugpy gateway is
    reachable; deterministic (state readout) otherwise. B never impersonates A
    and never invents context — its grounding is its own broker state."""
    ground = _b_state_text(sess, server, state)
    try:
        from hugpy_agent.config import load_config
        from hugpy_agent.gateway import Gateway
        cfg = load_config()
        gw = Gateway.from_config(cfg)
        sysmsg = (
            "You are B — the broker/curator of a Mediated Context Terminal. "
            "You curate bounded context for A (a confined Claude) and keep the "
            "ledger, catalog, and derived memory. Answer the operator directly, "
            "concisely, in first person as B. Ground every claim in the state "
            "below; when the state does not contain the answer, say so plainly.\n\n"
            "=== your current state ===\n" + ground)
        model = state.get("bmodel") or None
        res = gw.chat([{"role": "system", "content": sysmsg},
                       {"role": "user", "content": text}],
                      model=model, max_tokens=700)
        body = getattr(res, "text", None) or getattr(res, "content", None) or str(res)
        return body.strip()
    except Exception as exc:
        return ("[B offline — deterministic answer]\n"
                f"(gateway unavailable: {type(exc).__name__}: {exc})\n\n" + ground)


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
            # Validate first, then persist so the grant reaches the MCP child
            # and is not silently dropped by the next fs-policy sync.
            sess.register_root(rest[0], rest[1])
            from .fs_policy import add_root
            add_root(sess.server.workspace_root, rest[0], rest[1])
            print(f"{DIM}root '{rest[0]}' -> {rest[1]}{RESET}")
        except Exception as e:
            print(f"{RED}{e}{RESET}")
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
    elif cmd == "/b" and rest:
        with Spinner("B is answering"):
            body = _b_prompt(" ".join(rest), sess, server, state)
        print(f"\n{MAGENTA}B> {body}{RESET}\n")
    elif cmd == "/bstate":
        print(f"{MAGENTA}── B ──{RESET}\n{_b_state_text(sess, server, state)}")
    elif cmd == "/bmodel" and rest:
        state["bmodel"] = rest[0]; print(f"{DIM}B model -> {rest[0]}{RESET}")
    elif cmd == "/model" and rest:
        state["model"] = rest[0]; print(f"{DIM}A model -> {rest[0]}{RESET}")
    elif cmd == "/frontier" and rest and rest[0] in ("on", "off"):
        state["frontier"] = rest[0] == "on"
        if state["frontier"]:
            n = len(state["queue"])
            extra = (f" {n} queued message(s) will ride your next turn as one "
                     f"ordered delta." if n else "")
            print(f"{DIM}Frontier Keeper: Enabled.{extra}{RESET}")
        else:
            print(f"{DIM}Frontier Keeper: Disabled — applies only to A. The Local "
                  f"Keeper (/b) and your shell are unaffected; messages you send "
                  f"are recorded and queued, never answered in A's voice.{RESET}")
    elif cmd == "/files":
        n = int(rest[0]) if rest and rest[0].isdigit() else 40
        want_cat = any(a in ("cat", "--cat", "-c") for a in rest)
        nb = next((int(a) for a in rest if a.isdigit() and int(a) > 400), 2000)
        print(server.access.render(sess.session_id, limit=n, cat=want_cat, cat_bytes=nb))
        if not want_cat:
            print(f"{DIM}/files cat  — inline the actual correspondence bytes{RESET}")
        print(f"{DIM}live: tail -f {server.access.path}  (or the interleaved "
              f"{server.ledger.event_log_path}){RESET}")
    elif cmd == "/native":
        want = rest[0] if rest else ""
        if want not in ("none", "off_host", "all"):
            print(f"{RED}usage: /native none|off_host|all{RESET}")
            print(f"{DIM}  none      MCT tools only\n"
                  f"  off_host  + WebSearch/WebFetch/TodoWrite (bypasses nothing)\n"
                  f"  all       + Read/Grep/Edit/Write/Bash — A can work WITHOUT B:\n"
                  f"            files land in A's context in full, no snapshot is\n"
                  f"            kept, and /files only sees what still goes through B"
                  f"{RESET}")
        else:
            state["native"] = want
            print(f"{DIM}A native tools: {want}{RESET}")
            if want == "all":
                print(f"{YELLOW}[A can now read and write the host directly. The "
                      f"access log will only show brokered work.]{RESET}")
    elif cmd == "/quiet":
        state["quiet"] = not state["quiet"] if not rest else rest[0] == "on"
        print(f"{DIM}inline A/B relay: {'off' if state['quiet'] else 'on'}{RESET}")
    # /allow is the same switch under the name the console button uses; both
    # spellings persist to the one fs-policy file, so they can never disagree.
    elif cmd in ("/fsreq", "/allow") and rest and rest[0] in ("on", "off"):
        # Persist to the workspace fs-policy so it survives, and so the console's
        # directory-accessibility button and this command share one truth.
        from .fs_policy import set_allow
        try:
            set_allow(server.workspace_root, rest[0] == "on")
        except Exception:
            pass
        server.config.allow_frontier_fs_requests = rest[0] == "on"
        if server.config.allow_frontier_fs_requests:
            print(f"{DIM}Frontier filesystem requests: enabled — A may ask for more, "
                  f"but every request is still brokered, confined, and snapshotted "
                  f"by B; A never touches the filesystem directly.{RESET}")
        else:
            print(f"{DIM}Frontier filesystem requests: disabled (default) — A pulls "
                  f"only sources B has proactively whitelisted.{RESET}")
    else:
        print(f"{RED}unknown or malformed command; /help for usage.{RESET}")
    return False


if __name__ == "__main__":
    sys.exit(main())
