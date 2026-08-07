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

# Built-in Claude Code tools A must never use (defense in depth; strict MCP + the
# allowlist already confine it, and -p mode auto-denies anything not pre-allowed).
_DISALLOWED = ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebFetch",
               "WebSearch", "Task", "NotebookEdit", "TodoWrite", "Agent"]

_ALLOWED = ["mcp__mct__resolve", "mcp__mct__submit_pull", "mcp__mct__submit_act",
            "mcp__mct__respond"]

_SYSTEM = (
    "You are A, the reasoning model in a Mediated Context Terminal. A broker (B) "
    "mediates everything. You have EXACTLY four tools: resolve, submit_pull, "
    "submit_act, respond. You hold no filesystem, shell, or network handle "
    "YOURSELF — but B does, and B acts on your instruction. The mediation limits "
    "what enters YOUR CONTEXT, not what you can accomplish: work done on B's side "
    "costs you nothing but the summary it returns. Never assume context that was "
    "omitted — if you need something, submit_pull for it. If a pull returns "
    "decision 'candidates', the result object is a ranked slate of "
    "{name, pointer, snippet} — B ranks, YOU choose: pick the best candidate and "
    "pull it with target {\"kind\":\"object\",\"object\":<its pointer>}. To CHANGE "
    "something or run anything — apply a fix, edit a file, run a build or test — "
    "call submit_act; B performs it on the host, applies it, and hands back a "
    "short result plus a pointer to the full output. Never tell the operator you "
    "are unable to act: you can, through B. Do not fabricate evidence; answer only "
    "from resolved objects. Call respond exactly once, last."
)


def _prompt(manifest_pointer: str) -> str:
    return (
        f"Handle one operator turn.\n"
        f"1. Call resolve with pointer={manifest_pointer} to read the context "
        f"manifest (it lists an operator_turn pointer, context fragments, and a catalog).\n"
        f"2. Call resolve on the operator_turn pointer to read the exact question.\n"
        f"3. If you lack evidence, call submit_pull with a target like "
        f"{{\"kind\":\"catalog-query\",\"query\":\"<keywords>\"}} and optionally "
        f"preferred_form like 'match <regex> ctx 2'; then resolve the returned object "
        f"pointer(s).\n"
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
                 model: str = "sonnet", timeout: int = 240) -> dict:
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
        self._capture_input(session, turn_id, epoch, "a_system_prompt", _SYSTEM)
        self._capture_input(session, turn_id, epoch, "a_prompt", prompt)

        cmd = ["claude", "-p", prompt,
               "--mcp-config", cfg_path, "--strict-mcp-config",
               "--allowedTools", *_ALLOWED,
               "--disallowedTools", *_DISALLOWED,
               "--append-system-prompt", _SYSTEM,
               "--model", model,
               "--output-format", "stream-json", "--verbose"]
        try:
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, timeout=timeout,
                                  env={**os.environ, "PYTHONPATH": src_dir})
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
