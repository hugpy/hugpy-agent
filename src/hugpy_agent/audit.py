"""Audit trail — append-only JSONL, one line per tool call (P2.2).

The SQLite journal is RUN STATE (it exists so resume works); this file is
the AUDIT TRAIL (it exists so an operator can answer "what did the agent
do, when, and was it allowed?"). Keeping them separate means the audit log
survives journal compaction/deletion and can be shipped off-box.

Line schema (one JSON object per line, keys always present):

    {ts_iso, run_id, step, tool, risk, decision, model,
     args_sha256, result_sha256, result_len, duration_ms, error_bool}

`model` is the ACTIVE brain at call time — with a second-in-line brain
configured it can change once mid-run (capacity fallback), and the audit
trail is where that switch stays visible per call.

Doctrines:
  * Args/results are HASHED, never stored — an audit trail must not become
    a data leak (secrets ride through tool args). `verbose=True`
    (HUGPY_AUDIT_VERBOSE=1 / --audit-verbose) opts into TRUNCATED plaintext
    for debugging; the hashes remain so lines stay correlatable.
  * The writer NEVER raises into the run: any failure (unwritable path,
    full disk, bad payload) is reported via on_event("audit_error", ...)
    and the run continues. Losing an audit line is bad; killing the run
    over it is worse.
  * Clock-free: the caller passes `ts` (the loop sends
    `datetime.now(timezone.utc)`), so tests are deterministic.
  * Append is a single open('a') + write + flush per line — O_APPEND makes
    concurrent writers line-atomic for our line sizes on POSIX.
"""
from __future__ import annotations

import hashlib
import json
import os

# Chars of plaintext kept per field in verbose mode. Enough to debug a call;
# small enough that a huge fs_read result cannot bloat the log.
VERBOSE_MAX = 500


def sha256_of(value) -> str:
    """Stable content hash. Non-strings (arg dicts) are canonicalized —
    sorted keys, no whitespace — so the same logical args hash identically
    across runs, processes, and dict insertion orders."""
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def default_audit_path(workspace: str) -> str:
    return os.path.join(os.path.realpath(workspace), ".hugpy_agent",
                        "audit.jsonl")


class AuditLog:
    """Append-only JSONL writer. `path` empty/None => auditing disabled
    (every record() is a no-op). `on_event` receives ("audit_error", msg)
    on write failure — the only failure signal this class ever emits."""

    def __init__(self, path: str | None, verbose: bool = False,
                 on_event=None):
        self.path = path or ""
        self.verbose = bool(verbose)
        self.on_event = on_event or (lambda *a, **k: None)

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    def record(self, ts, *, run_id: str, step: int, tool: str, risk: str,
               decision: str, args: dict, result: str, duration_ms: int,
               error: bool, args_sha256: str | None = None,
               model: str = "") -> None:
        """Append one line for a resolved tool call. `ts` is an aware
        datetime supplied by the caller (injectable clock). Never raises.
        `args_sha256` lets a caller that already hashed the args (the loop
        computes it once per call for the P2.4 loop-guard) pass it in
        instead of hashing twice; omitted, it is computed here."""
        if not self.path:
            return
        try:
            entry = {
                "ts_iso": ts.isoformat(),
                "run_id": run_id,
                "step": step,
                "tool": tool,
                "risk": risk,
                "decision": decision,
                "model": model,
                "args_sha256": args_sha256 or sha256_of(args),
                "result_sha256": sha256_of(result),
                "result_len": len(result),
                "duration_ms": duration_ms,
                "error_bool": bool(error),
            }
            if self.verbose:
                # Debug opt-in: truncated plaintext ALONGSIDE the hashes
                # (hashes stay the correlation key across modes).
                entry["args_text"] = json.dumps(
                    args, sort_keys=True, separators=(",", ":"))[:VERBOSE_MAX]
                entry["result_text"] = result[:VERBOSE_MAX]
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
                fh.flush()
        except Exception as exc:  # audit must NEVER break a run (doctrine)
            self.on_event("audit_error",
                          "%s: %s" % (type(exc).__name__, exc))
