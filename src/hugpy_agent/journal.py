"""SQLite (WAL) run ledger — the crash-safety spine of the agent.

Doctrine (design §3.2): anything per-process/in-memory silently breaks, so
every message and every tool call is journaled. A tool call is recorded
BEFORE execution with an idempotency key and its result recorded after, so
`resume(run_id)` can replay completed calls instead of re-executing side
effects.

The ambiguity case is handled fail-closed: a call found in `pending` state on
resume means the process died DURING execution — for a side-effecting tool we
cannot know whether the effect happened, so resume reports "outcome unknown"
to the model as data rather than blindly re-running it. Read-only tools are
safely re-executed.

Every write is its own committed transaction; WAL keeps readers unblocked and
makes a mid-write SIGKILL recoverable.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    task        TEXT NOT NULL,
    model       TEXT NOT NULL,
    status      TEXT NOT NULL,          -- running|done|aborted|interrupted|max_steps
    outcome     TEXT,                   -- final report JSON
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    parent_run_id TEXT                  -- spawning run for subagent children (P2.5)
);
CREATE TABLE IF NOT EXISTS messages (
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    role        TEXT NOT NULL,          -- system|user|assistant|tool|summary
    content     TEXT NOT NULL,          -- JSON-encoded (string or content-parts)
    meta        TEXT,                   -- JSON, e.g. {"replaces_upto": seq} for summaries
    created_at  REAL NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS tool_calls (
    idem_key    TEXT PRIMARY KEY,       -- sha256(run|assistant_seq|name|args)
    run_id      TEXT NOT NULL,
    assistant_seq INTEGER NOT NULL,     -- seq of the assistant message that requested it
    name        TEXT NOT NULL,
    arguments   TEXT NOT NULL,          -- canonical JSON
    status      TEXT NOT NULL,          -- pending|done|error
    result      TEXT,
    created_at  REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

# Schema evolution: `CREATE TABLE IF NOT EXISTS` never retrofits columns onto
# a database created by an older build, so additive columns ALSO ride an
# idempotent ALTER pass keyed off PRAGMA table_info (run on every open).
# Additive-only by doctrine — the journal is the crash-safety spine; a
# migration that rewrites rows would be its own crash window.
_MIGRATIONS = (
    ("runs", "parent_run_id",
     "ALTER TABLE runs ADD COLUMN parent_run_id TEXT"),          # P2.5
)


def idem_key(run_id: str, assistant_seq: int, name: str, args: dict) -> str:
    """Deterministic identity of one tool invocation. Keyed on the journal seq
    of the assistant message that requested it (stable across resume — a loop
    iteration counter would not be) plus the canonical argument JSON."""
    blob = "%s|%d|%s|%s" % (run_id, assistant_seq, name,
                            json.dumps(args, sort_keys=True, separators=(",", ":")))
    return hashlib.sha256(blob.encode()).hexdigest()[:40]


class Journal:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        for table, column, ddl in _MIGRATIONS:
            cols = {r[1] for r in
                    self._conn.execute("PRAGMA table_info(%s)" % table)}
            if column not in cols:
                self._conn.execute(ddl)
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ── runs ─────────────────────────────────────────────────────────────
    def create_run(self, task: str, model: str, run_id: str | None = None,
                   parent_run_id: str | None = None) -> str:
        """`parent_run_id` links a subagent child to the run that spawned it
        (P2.5) — the runs table doubles as the delegation tree."""
        rid = run_id or uuid.uuid4().hex[:12]
        now = time.time()
        self._conn.execute(
            "INSERT INTO runs(run_id, task, model, status, created_at,"
            " updated_at, parent_run_id) VALUES (?,?,?,?,?,?,?)",
            (rid, task, model, "running", now, now, parent_run_id))
        self._conn.commit()
        return rid

    def get_run(self, run_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id=?",
                                 (run_id,)).fetchone()
        return dict(row) if row else None

    def set_run_status(self, run_id: str, status: str, outcome: dict | None = None) -> None:
        self._conn.execute(
            "UPDATE runs SET status=?, outcome=?, updated_at=? WHERE run_id=?",
            (status, json.dumps(outcome) if outcome is not None else None,
             time.time(), run_id))
        self._conn.commit()

    def list_runs(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT run_id, task, model, status, created_at, parent_run_id"
            " FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── messages ─────────────────────────────────────────────────────────
    def append_message(self, run_id: str, role: str, content, meta: dict | None = None) -> int:
        """Append one message; returns its seq. Content is JSON-encoded so
        content-parts (vision) survive round-trips unchanged."""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE run_id=?",
            (run_id,)).fetchone()
        seq = row[0]
        self._conn.execute(
            "INSERT INTO messages(run_id, seq, role, content, meta, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (run_id, seq, role, json.dumps(content),
             json.dumps(meta) if meta else None, time.time()))
        self._conn.commit()
        return seq

    def raw_messages(self, run_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT seq, role, content, meta FROM messages WHERE run_id=?"
            " ORDER BY seq", (run_id,)).fetchall()
        out = []
        for r in rows:
            out.append({"seq": r["seq"], "role": r["role"],
                        "content": json.loads(r["content"]),
                        "meta": json.loads(r["meta"]) if r["meta"] else None})
        return out

    def wire_messages(self, run_id: str, pin_upto: int = 1) -> list[dict]:
        """The message list as sent to the model, honoring compaction.

        If summary rows exist, the LATEST one stands in for everything after
        the pinned prefix (seq <= pin_upto: system prompt + task brief, never
        compacted) up to and including its `replaces_upto`. The journal keeps
        the full history — compaction only changes what goes on the wire, so
        an audit can always reconstruct the truth.
        """
        rows = self.raw_messages(run_id)
        summary = None
        for r in rows:
            if r["role"] == "summary":
                summary = r  # latest wins
        wire = []
        cutoff = -1
        if summary:
            cutoff = int((summary["meta"] or {}).get("replaces_upto", -1))
        for r in rows:
            if r["role"] == "summary":
                continue
            if summary and pin_upto < r["seq"] <= cutoff:
                continue
            wire.append({"role": r["role"], "content": r["content"]})
            if summary and r["seq"] == pin_upto:
                wire.append({"role": "user",
                             "content": "[Summary of earlier progress]\n%s"
                                        % summary["content"]})
        return wire

    def last_message(self, run_id: str) -> dict | None:
        rows = self.raw_messages(run_id)
        for r in reversed(rows):
            if r["role"] != "summary":
                return r
        return None

    def assistant_step_count(self, run_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE run_id=? AND role='assistant'",
            (run_id,)).fetchone()
        return row[0]

    # ── tool calls ───────────────────────────────────────────────────────
    def lookup_call(self, key: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM tool_calls WHERE idem_key=?",
                                 (key,)).fetchone()
        return dict(row) if row else None

    def record_call_start(self, key: str, run_id: str, assistant_seq: int,
                          name: str, args: dict) -> None:
        """MUST be committed before the handler runs — that ordering is what
        makes a crash mid-execution detectable (a lingering 'pending' row)."""
        self._conn.execute(
            "INSERT OR IGNORE INTO tool_calls(idem_key, run_id, assistant_seq,"
            " name, arguments, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (key, run_id, assistant_seq, name,
             json.dumps(args, sort_keys=True), "pending", time.time()))
        self._conn.commit()

    def record_call_result(self, key: str, status: str, result: str) -> None:
        self._conn.execute(
            "UPDATE tool_calls SET status=?, result=?, finished_at=?"
            " WHERE idem_key=?", (status, result, time.time(), key))
        self._conn.commit()

    def call_count(self, run_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE run_id=?", (run_id,)).fetchone()
        return row[0]

    def successful_call_count(self, run_id: str) -> int:
        """Journaled tool calls that actually EXECUTED and returned a
        non-error result (status='done'). Policy-denied, interrupted and
        errored calls are recorded status='error' and do NOT count, and
        `final_answer` never lands here. The final_answer guard requires
        >= 1 of these, so a rejected/failed call cannot unlock termination."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE run_id=? AND status='done'",
            (run_id,)).fetchone()
        return row[0]

    def tool_call_rows(self, run_id: str) -> list[dict]:
        """(name, status) per journaled tool call, oldest first. Read-only;
        used by the eval harness to compute tool-accuracy (non-error calls /
        total). `final_answer` never lands here — the loop intercepts it — so
        this counts real tool executions only."""
        rows = self._conn.execute(
            "SELECT name, status FROM tool_calls WHERE run_id=?"
            " ORDER BY created_at", (run_id,)).fetchall()
        return [{"name": r["name"], "status": r["status"]} for r in rows]

    # ── durable per-call scratch state ───────────────────────────────────
    # For tools whose side effect is a REMOTE handle (e.g. an async media
    # job_id): the handler journals the handle the moment the remote accepts
    # the work, so a crash between enqueue and completion leaves a pending
    # call WITH state — resume re-attaches to the same remote job instead of
    # enqueueing a duplicate (design §6 Phase 1.5). Kept in kv with a
    # structured key so it rides the same WAL durability as everything else.

    def set_call_state(self, run_id: str, idem_key: str, state: dict) -> None:
        self.kv_set("callstate|%s|%s" % (run_id, idem_key), json.dumps(state))

    def get_call_state(self, run_id: str, idem_key: str) -> dict | None:
        raw = self.kv_get("callstate|%s|%s" % (run_id, idem_key))
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None  # torn state = no state; fail-closed rules apply

    def list_call_states(self, run_id: str) -> dict:
        """idem_key -> state dict for one run (drives the per-run generation
        cap: each enqueued job left exactly one state entry)."""
        prefix = "callstate|%s|" % run_id
        rows = self._conn.execute(
            "SELECT k, v FROM kv WHERE k LIKE ?", (prefix + "%",)).fetchall()
        out = {}
        for r in rows:
            try:
                data = json.loads(r["v"])
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                out[r["k"][len(prefix):]] = data
        return out

    # ── kv (capability cache etc.) ───────────────────────────────────────
    def kv_get(self, k: str) -> str | None:
        row = self._conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row[0] if row else None

    def kv_set(self, k: str, v: str) -> None:
        self._conn.execute(
            "INSERT INTO kv(k, v) VALUES (?,?)"
            " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        self._conn.commit()
