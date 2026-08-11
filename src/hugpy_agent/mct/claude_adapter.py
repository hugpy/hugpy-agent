"""Claude Code as A — the real reasoning adapter.

Design ref: §4 (A sandbox), §22 Phase 4, invariants 1 & 10. B launches a headless
``claude`` process and confines it with ``--strict-mcp-config`` +
``--allowedTools`` to exactly B's ``resolve``/``submit_pull``/``respond`` MCP tools
(served by :mod:`hugpy_agent.mct.mct_mcp_server`). A therefore has no filesystem,
network, or shell — only brokered, receipted operations. No API key: Claude Code
uses its own auth.

The parent does not render here; A's ``respond`` records the sealed response
objects and an ``a.response_ready`` event, and the caller renders exactly once via
``MctSession.on_response_ready`` (invariant 14).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile

from .protocol import make_pointer
from .tokens import summary_from_result

# A's native Claude Code tools, split by whether granting one can bypass B.
#
# The distinction is not "safe vs dangerous" — B is unrestricted and A can already
# reach the whole host through submit_act. It is about whether the MEDIATION
# still means anything:
#
#   * OFF-HOST tools touch nothing on this filesystem. Granting them costs A its
#     own tokens for the result and bypasses nothing, because B never mediated
#     the web to begin with. Pure capability gain.
#
#   * HOST tools read and write this machine directly. Granting them does not
#     make A *more* capable — submit_act already does everything Bash does — it
#     makes A capable WITHOUT B. Three things stop being true: whole files land
#     in A's context at frontier prices instead of B-selected excerpts; reads
#     leave no snapshot, so there is no immutable record of what A saw; and the
#     access log goes blind, reporting only the fraction that still went through
#     B while A reads freely around it. A log that under-reports is worse than
#     no log, so enabling these is recorded explicitly (see ``native_tools``).
_NATIVE_OFF_HOST = ["WebSearch", "WebFetch", "TodoWrite"]
_NATIVE_HOST = ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "NotebookEdit"]
_NEVER = ["Task", "Agent"]   # a subagent would inherit tools B cannot see

_MCT_TOOLS = ["mcp__mct__resolve", "mcp__mct__submit_pull", "mcp__mct__submit_act",
              "mcp__mct__submit_ask", "mcp__mct__respond", "mcp__mct__todo"]

# Back-compat aliases: the previous flat lists, as the default posture.
_ALLOWED = list(_MCT_TOOLS)
_DISALLOWED = _NATIVE_HOST + _NATIVE_OFF_HOST + _NEVER


def tool_policy(native_tools: str = "off_host") -> tuple[list, list]:
    """Return ``(allowed, disallowed)`` for a native-tool posture.

    ``none``      — MCT tools only (the original, strictest posture).
    ``off_host``  — default: adds web + todo. Bypasses nothing.
    ``all``       — also grants Read/Grep/Edit/Write/Bash. A can now work
                    without B; the mediation becomes advisory.
    """
    allowed = list(_MCT_TOOLS)
    if native_tools in ("off_host", "all"):
        allowed += _NATIVE_OFF_HOST
    if native_tools == "all":
        allowed += _NATIVE_HOST
    disallowed = [t for t in (_NATIVE_HOST + _NATIVE_OFF_HOST + _NEVER)
                  if t not in allowed]
    return allowed, disallowed


# Appended when A holds host tools, so the cheap path stays the default one.
_SYSTEM_NATIVE = (
    "\n\nYou ALSO hold Claude Code's own tools (Read, Grep, Glob, Edit, Write, "
    "Bash). They work, but they are the EXPENSIVE path: whatever they return "
    "lands in your context in full and is billed to you, and B keeps no record "
    "of it. Prefer submit_pull to find and read — B searches the host for free "
    "and returns only what matters — and submit_act to change things, so the "
    "edit is snapshotted and auditable. Reach for a native tool when B genuinely "
    "cannot serve the need, not as the first move."
)

_SYSTEM = (
    "You are A, the reasoning model in a Mediated Context Terminal (MCT). This is "
    "your real, operator-sanctioned runtime — not an untrusted message, a roleplay, "
    "or a prompt-injection attempt. The tools named below are your genuine tools, "
    "served to you over MCP by the broker (B); use them as your normal way of "
    "working. If a tool call ever fails, treat it as a transient error to retry or "
    "report — never as evidence the environment is fake — and do not refuse, warn "
    "about, or flag this setup as suspicious; just operate within it. A broker (B) "
    "mediates everything. Your core tools are resolve, submit_pull, submit_act, "
    "submit_ask, and respond; you ALSO have todo — a durable, operator-shared "
    "to-do board for THIS session (ops list/add/update/del/replace). Use it to "
    "accumulate work so nothing is lost between turns; the operator sees and edits "
    "the same board. You hold no filesystem, shell, or network handle "
    "YOURSELF — but B does, and B acts on your instruction. The mediation limits "
    "what enters YOUR CONTEXT, not what you can accomplish: work done on B's side "
    "costs you nothing but the summary it returns. Never assume context that was "
    "omitted — if you need something, submit_pull for it. ROUTE EACH CONTEXT NEED "
    "BY ITS SHAPE, first try, no probing: "
    "(1) LAYOUT — what exists, where things live, which file is the manifest — "
    "target {\"kind\":\"browse\",\"spec\":{\"path\":\"<root>:<rel>\" or omit for a "
    "roots overview, \"depth\":N}}; B returns a bounded tree listing, and states "
    "truncation in-band so one narrower call finishes the job. "
    "(2) CONTENT — where is X defined, which files mention Y — "
    "{\"kind\":\"search\",\"spec\":{...}}: all[] (every term must appear), any[] "
    "(at least one), none[] (file-level veto), ext[], path_include[]/path_exclude[] "
    "PATH globs (e.g. **/mct/*.py), modified_after/modified_before ISO dates, "
    "limit, context_lines. all/any/none terms are LITERAL strings; for patterns "
    "use all_re[]/any_re[]/none_re[] (Python regex, matched per line — e.g. "
    "all_re:[\"def\\\\s+\\\\w+_adapter\"]); selectors also take 'match <regex> ctx 2'. "
    "B returns only matching numbered lines. "
    "(3) A RANGE of a known object — resolve with a selector ('lines A-B', "
    "'match <re> ctx N', 'symbol name'); never pull a whole file for a slice. "
    "(4) THIS SESSION'S earlier objects — {\"kind\":\"catalog-query\"}; the "
    "catalog is EMPTY in a fresh session, so it is never your first move for "
    "host facts. If a pull returns decision 'candidates', the result object is a "
    "ranked slate of {name, pointer, snippet} — B ranks, YOU choose: pull the "
    "best with target {\"kind\":\"object\",\"object\":<its pointer>}. A miss "
    "names the surfaces searched (fs switch, granted roots) — believe it and "
    "reroute, never retry a closed surface, and never probe the host with exec "
    "(ls/pwd/grep) for what browse/search answer leaner. To CHANGE "
    "something or run anything — apply a fix, edit a file, run a build or test — "
    "call submit_act; B performs it on the host, applies it, and hands back a "
    "short result plus a pointer to the full output. Never tell the operator you "
    "are unable to act: you can, through B. If a CHOICE would change your answer — \
which of two things they meant, whether to apply a change — call submit_ask and \
the operator answers mid-turn; do not end the turn to ask, and do not guess when \
asking is one sentence. Do not fabricate evidence; answer only "
    "from resolved objects. Call respond exactly once, last."
)


def _prompt(manifest_pointer: str) -> str:
    return (
        f"Handle one operator turn.\n"
        f"1. Call resolve with pointer={manifest_pointer} to read the context "
        f"manifest (it lists an operator_turn pointer, context fragments, and a catalog).\n"
        f"2. Call resolve on the operator_turn pointer to read the exact question.\n"
        f"3. If you lack evidence, call submit_pull and route by shape: layout -> "
        f"{{\"kind\":\"browse\",\"spec\":{{\"path\":\"<root>:<rel>\"|omit,\"depth\":N}}}}; "
        f"content -> {{\"kind\":\"search\",\"spec\":{{\"all\":[<literal terms>],...}}}} "
        f"(B greps the granted roots, returns matching numbered lines); THIS "
        f"session's earlier objects -> {{\"kind\":\"catalog-query\"}} (empty in a "
        f"fresh session); a range of a known object -> resolve with a selector "
        f"('lines A-B', 'match <regex> ctx 2'). Then resolve returned pointer(s). "
        f"A miss names the searched surfaces — believe it: reroute once, never "
        f"retry a closed surface, never exec ls/grep for what browse/search do.\n"
        f"4. If the turn asks you to CHANGE or RUN anything, call submit_act "
        f"(kind='edit'/'write'/'exec') — B applies it on the host and returns a "
        f"short result. Do not report that you cannot act.\n"
        f"5. Call respond exactly once with your final answer. Cite the evidence you used."
    )


class ClaudeCodeAdapter:
    def __init__(self, server):
        self.server = server

    def available(self) -> bool:
        return shutil.which("claude") is not None

    def run_turn(self, session, turn_id, epoch, manifest_pointer, *,
                 model: str = "sonnet", timeout: int = 240,
                 native_tools: str = "off_host") -> dict:
        """Launch confined Claude Code as A. Returns
        ``{"response_manifest": pointer|None, "raw": <claude result>, "error": ...}``."""
        if not self.available():
            return {"response_manifest": None, "error": "claude CLI not found"}

        src_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        env_for_server = {
            "MCT_WORKSPACE": str(self.server.workspace_root),
            "MCT_SESSION": session.session_id, "MCT_TURN": turn_id,
            "MCT_EPOCH": epoch, "MCT_MANIFEST": manifest_pointer,
            "PYTHONPATH": src_dir,
        }
        cfg = {"mcpServers": {"mct": {
            "command": "python3",
            "args": ["-m", "hugpy_agent.mct.mct_mcp_server"],
            "env": env_for_server,
        }}}

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(cfg, fh)
            cfg_path = fh.name

        prompt = _prompt(manifest_pointer)
        # Document A's exact inputs as immutable objects — part of the durable
        # "A cache" mirror (everything A receives is captured, not just pointers).
        allowed, disallowed = tool_policy(native_tools)
        system = _SYSTEM + (_SYSTEM_NATIVE if native_tools == "all" else "")
        # A gets ONE sanctioned writable location — the per-session scratch dir —
        # declared up front so it never probes the host for somewhere to write
        # (its inherited cwd is often root-owned, e.g. the installed backend).
        # B's steward resolves relative act paths here and defaults exec cwd here.
        scratch = os.path.join(
            os.path.abspath(str(self.server.workspace_root)), "scratch")
        try:
            os.makedirs(scratch, exist_ok=True)
        except OSError:
            pass
        system += (f" Your dedicated writable directory is {scratch} — put new "
                   "files, build artifacts, and test output there by default; "
                   "relative submit_act paths resolve there and exec runs there "
                   "unless you pass cwd. Write elsewhere only when the operator "
                   "names a destination.")
        if native_tools == "all":
            # A can now read and write the host WITHOUT B. The access log only
            # sees brokered work, so it would silently under-report from here on
            # — say so in the ledger rather than let the record imply coverage
            # it does not have.
            try:
                self.server.ledger.append_event(
                    session.session_id, turn_id, epoch,
                    "a.native_host_tools", "B.a-adapter")
                self.server.access.record(
                    "A", "unmediated", "native host tools granted",
                    detail="Read/Grep/Edit/Write/Bash bypass B; this log sees "
                           "only what still goes through it",
                    session=session.session_id, turn=turn_id)
            except Exception:
                pass
        self._capture_input(session, turn_id, epoch, "a_system_prompt", system)
        self._capture_input(session, turn_id, epoch, "a_prompt", prompt)

        # Per-session .claude scope: give A its own config/state dir under THIS
        # session's workspace so sessions never collide (projects/history/todos/
        # sessions are isolated per named session). Auth + settings are SHARED:
        # .credentials.json is SYMLINKED to ~/.claude's, not copied — OAuth
        # refresh tokens rotate on use, so a private copy is invalidated the
        # first time any other session refreshes, and the copy's holder then
        # fails auth permanently. A stale regular-file copy from an older build
        # is replaced by the symlink here.
        a_cfg = os.path.join(os.path.abspath(str(self.server.workspace_root)), ".claude")
        try:
            os.makedirs(a_cfg, exist_ok=True)
            home_claude = os.path.join(os.path.expanduser("~"), ".claude")
            src_cred = os.path.join(home_claude, ".credentials.json")
            dst_cred = os.path.join(a_cfg, ".credentials.json")
            if os.path.exists(src_cred) and not os.path.islink(dst_cred):
                if os.path.exists(dst_cred):
                    os.unlink(dst_cred)
                os.symlink(src_cred, dst_cred)
            src_f = os.path.join(home_claude, "settings.json")
            dst_f = os.path.join(a_cfg, "settings.json")
            if os.path.exists(src_f) and not os.path.exists(dst_f):
                shutil.copy2(src_f, dst_f)
                try:
                    os.chmod(dst_f, 0o644)
                except OSError:
                    pass
        except OSError:
            a_cfg = ""   # seeding failed — fall back to the shared ~/.claude

        cmd = ["claude", "-p", prompt,
               "--mcp-config", cfg_path, "--strict-mcp-config",
               "--allowedTools", *allowed,
               *(["--disallowedTools", *disallowed] if disallowed else []),
               "--append-system-prompt", system,
               "--model", model,
               "--output-format", "stream-json", "--verbose"]
        try:
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, timeout=timeout,
                                  env={**os.environ, "PYTHONPATH": src_dir,
                                       **({"CLAUDE_CONFIG_DIR": a_cfg} if a_cfg else {})})
        except subprocess.TimeoutExpired:
            return {"response_manifest": None, "error": f"claude timed out after {timeout}s"}
        finally:
            try:
                os.unlink(cfg_path)
            except OSError:
                pass

        # Persist A's FULL agent transcript (every tool_use, tool_result, and text
        # A emitted/received) — the durable equivalent of Claude's working cache.
        raw_stdout = proc.stdout or ""
        transcript_ptr = None
        if raw_stdout.strip():
            ref = self.server.store.commit(
                session.session_id, raw_stdout.encode("utf-8"),
                media_type="application/x-ndjson", kind="a_transcript",
                provenance={"turn_id": turn_id, "epoch": epoch, "model": model})
            transcript_ptr = ref.pointer
            self.server.ledger.append_event(session.session_id, turn_id, epoch,
                                            "a.transcript", "A.claude",
                                            output_objects=[ref.object_id])

        result = {}
        for line in reversed(raw_stdout.splitlines()):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "result":
                result = obj
                break

        ev = self.server.ledger.latest_event(session.session_id, turn_id, "a.response_ready")
        manifest_pointer_out = None
        if ev and ev["output_objects"]:
            manifest_pointer_out = make_pointer(session.session_id, ev["output_objects"][0])

        error = None
        if manifest_pointer_out is None:
            # Claude sometimes emits its final answer as plain text instead of
            # calling the respond() tool. That text IS A's answer — relay it
            # (B is not inventing anything; it is using A's real output).
            final_text = result.get("result") if isinstance(result, dict) else None
            if final_text and not result.get("is_error"):
                manifest_pointer_out = self._ingest_text_answer(session, turn_id, epoch, final_text)
            else:
                error = self._failure_reason(result, proc)

        return {"response_manifest": manifest_pointer_out, "raw": result, "error": error,
                "transcript": transcript_ptr, "returncode": proc.returncode,
                "tokens": summary_from_result(result)}

    def _ingest_text_answer(self, session, turn_id, epoch, text: str) -> str:
        """Wrap A's plain-text final answer into a sealed response object, as if it
        had called respond() — same path, same validation."""
        body_bytes = text.encode("utf-8")
        body_ref = self.server.store.commit(
            session.session_id, body_bytes, media_type="text/markdown",
            kind="response_body", provenance={"turn_id": turn_id, "delivery": "final-text"})
        manifest = {"schema": "mct.response/1", "session_id": session.session_id,
                    "turn_id": turn_id, "epoch": epoch, "body": body_ref.pointer,
                    "format": "text/markdown", "final": True, "proposed_actions": [],
                    "body_sha256": hashlib.sha256(body_bytes).hexdigest()}
        mref = self.server.store.commit(
            session.session_id, json.dumps(manifest).encode("utf-8"),
            media_type="application/vnd.hugpy.mct-response+json", kind="response_manifest",
            provenance={"turn_id": turn_id, "delivery": "final-text"})
        self.server.ledger.append_event(session.session_id, turn_id, epoch,
                                        "a.response_ready", "A.claude",
                                        output_objects=[mref.object_id])
        return mref.pointer

    @staticmethod
    def _failure_reason(result: dict, proc) -> str:
        if not isinstance(result, dict) or not result:
            return (f"claude exited rc={getattr(proc, 'returncode', '?')}; "
                    f"stderr: {(getattr(proc, 'stderr', '') or '')[:300]}").strip()
        if result.get("is_error"):
            return (f"claude reported an error: subtype={result.get('subtype')} "
                    f"api_status={result.get('api_error_status')}").strip()
        return "A returned no answer (no respond() call and no final text)"

    def _capture_input(self, session, turn_id, epoch, kind, text: str) -> None:
        ref = self.server.store.commit(session.session_id, text.encode("utf-8"),
                                       media_type="text/plain", kind=kind,
                                       provenance={"turn_id": turn_id, "epoch": epoch})
        self.server.ledger.append_event(session.session_id, turn_id, epoch,
                                        "a.input", "B.a-adapter", output_objects=[ref.object_id])
