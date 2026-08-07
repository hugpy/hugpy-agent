"""Event ledger + durable session state (SQLite WAL).

Design ref: §14 (append-only, hash-chained events), §7.4/§20.7 (single ``mct.db``),
§15 (turn state), §15.2 (idempotency), §21 (``ledger.py``). This is B's
authoritative, rebuildable state (§17.1). It also owns the ``objects`` metadata
table so the whole store lives in one crash-consistent database for the prototype.

Enforcement points served here: registry row 5 (append-only hash-chained record)
and the idempotency half of rows 7/8.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import ids
from .errors import LedgerUnavailable, StateError

TERMINAL_TURN_STATES = {"Committed", "Cancelled", "Failed"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY, workspace TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS counters (
    session_id TEXT, name TEXT, value INTEGER, PRIMARY KEY (session_id, name)
);
CREATE TABLE IF NOT EXISTS turns (
    session_id TEXT, turn_id TEXT, state TEXT, epoch TEXT, created_at TEXT,
    PRIMARY KEY (session_id, turn_id)
);
CREATE TABLE IF NOT EXISTS events (
    rowid_global INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE, session_id TEXT, turn_id TEXT, sequence INTEGER,
    epoch TEXT, type TEXT, actor TEXT, input_objects TEXT, output_objects TEXT,
    policy_revision INTEGER, idempotency_key TEXT, timestamp TEXT,
    previous_event_sha256 TEXT, event_sha256 TEXT
);
CREATE TABLE IF NOT EXISTS objects (
    object_id TEXT PRIMARY KEY, session_id TEXT, digest TEXT, media_type TEXT,
    kind TEXT, size INTEGER, provenance TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, turn_id TEXT,
    epoch TEXT, object_id TEXT, digest TEXT, selector TEXT,
    placed_in_input INTEGER, purpose TEXT, timestamp TEXT, manifest_sha256 TEXT
);
CREATE TABLE IF NOT EXISTS idempotency (
    idempotency_key TEXT PRIMARY KEY, session_id TEXT, result TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS epochs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, epoch TEXT,
    reason TEXT, created_at TEXT, active INTEGER
);
CREATE TABLE IF NOT EXISTS renders (
    session_id TEXT, turn_id TEXT, body_sha256 TEXT, rendered_at TEXT,
    PRIMARY KEY (session_id, turn_id)
);
CREATE TABLE IF NOT EXISTS pulls (
    session_id TEXT, turn_id TEXT, request_id TEXT, decision TEXT,
    result_object TEXT, created_at TEXT,
    PRIMARY KEY (session_id, turn_id, request_id)
);
CREATE TABLE IF NOT EXISTS facts (
    object_id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, method TEXT,
    confidence REAL, created_at TEXT, validated_at TEXT, superseded_by TEXT,
    sensitivity TEXT, scope TEXT, source_objects TEXT, conflict_with TEXT
);
CREATE TABLE IF NOT EXISTS embeddings (
    object_id TEXT, model TEXT, session_id TEXT, dim INTEGER, vector TEXT,
    created_at TEXT, PRIMARY KEY (object_id, model)
);
CREATE TABLE IF NOT EXISTS near_duplicates (
    object_a TEXT, object_b TEXT, session_id TEXT, similarity REAL, model TEXT,
    created_at TEXT, PRIMARY KEY (object_a, object_b)
);
CREATE TABLE IF NOT EXISTS metrics (
    session_id TEXT, turn_id TEXT, operator_bytes INTEGER, context_tokens INTEGER,
    pull_count INTEGER, pull_source_bytes INTEGER, rendered INTEGER, latency_ms REAL,
    PRIMARY KEY (session_id, turn_id)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")


class Ledger:
    def __init__(self, db_path: str | Path, *, event_log: bool = True):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # Rolling append-only log written as things happen — tail -f friendly,
        # body-free (§18.3). One line per event and per A read (§14, §12.2).
        self.event_log_path = Path(self.db_path).parent / "mct.log"
        self._log_lock = threading.Lock()
        self._log_fh = None
        if event_log:
            try:
                self._log_fh = open(self.event_log_path, "a", buffering=1)  # line-buffered
            except OSError:
                self._log_fh = None
        # One broker can serve concurrent sessions across threads. Each thread gets
        # its OWN connection to the shared WAL file (SQLite's supported concurrency
        # model), so cursors never interleave across threads (§7.4, §14.2, §Load).
        self._local = threading.local()
        db = self._connect()
        for attempt in range(12):  # tolerate transient lock during one-time schema init
            try:
                db.executescript(_SCHEMA)
                db.commit()
                break
            except sqlite3.OperationalError as exc:
                if ("locked" not in str(exc) and "busy" not in str(exc)) or attempt == 11:
                    raise LedgerUnavailable(f"schema init failed: {exc}") from exc
                time.sleep(0.25)

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            try:
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
            except sqlite3.Error as exc:  # pragma: no cover
                raise LedgerUnavailable(f"cannot open ledger: {exc}") from exc
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=8000")  # wait on write locks, don't error
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @property
    def _db(self) -> sqlite3.Connection:
        return self._connect()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None

    def _write_log(self, line: str) -> None:
        if self._log_fh is None:
            return
        try:
            with self._log_lock:
                self._log_fh.write(line + "\n")  # O_APPEND + line-buffered = atomic per line
        except (OSError, ValueError):
            pass

    # --- counters -----------------------------------------------------------
    def _next_counter(self, session_id: str, name: str) -> int:
        cur = self._db.execute(
            "SELECT value FROM counters WHERE session_id=? AND name=?", (session_id, name)
        ).fetchone()
        value = 0 if cur is None else cur["value"] + 1
        self._db.execute(
            "INSERT INTO counters(session_id,name,value) VALUES(?,?,?) "
            "ON CONFLICT(session_id,name) DO UPDATE SET value=excluded.value",
            (session_id, name, value),
        )
        return value

    def next_sequence(self, session_id: str) -> int:
        """Monotonic per-session sequence shared by all envelopes and events (§15.1)."""
        seq = self._next_counter(session_id, "sequence")
        self._db.commit()
        return seq

    def next_turn_id(self, session_id: str) -> str:
        n = self._next_counter(session_id, "turn")
        self._db.commit()
        return ids.format_turn_id(n)

    def next_request_id(self, session_id: str, turn_id: str) -> str:
        n = self._next_counter(session_id, f"req:{turn_id}")
        self._db.commit()
        return ids.format_request_id(n)

    # --- sessions & epochs --------------------------------------------------
    def create_session(self, workspace: str = "") -> tuple[str, str]:
        session_id = ids.new_session_id()
        self._db.execute(
            "INSERT INTO sessions(session_id,workspace,created_at) VALUES(?,?,?)",
            (session_id, workspace, now_iso()),
        )
        epoch = self._new_epoch(session_id, reason="session-start")
        self._db.commit()
        return session_id, epoch

    def _new_epoch(self, session_id: str, reason: str) -> str:
        epoch = ids.new_epoch_id()
        self._db.execute("UPDATE epochs SET active=0 WHERE session_id=?", (session_id,))
        self._db.execute(
            "INSERT INTO epochs(session_id,epoch,reason,created_at,active) VALUES(?,?,?,?,1)",
            (session_id, epoch, reason, now_iso()),
        )
        return epoch

    def new_epoch(self, session_id: str, reason: str) -> str:
        """Bump the epoch on any A-continuity break (§12.3, invariant 13)."""
        epoch = self._new_epoch(session_id, reason)
        self._db.commit()
        return epoch

    def active_epoch(self, session_id: str) -> str:
        row = self._db.execute(
            "SELECT epoch FROM epochs WHERE session_id=? AND active=1", (session_id,)
        ).fetchone()
        if row is None:
            raise StateError(f"no active epoch for session {session_id}")
        return row["epoch"]

    # --- turns --------------------------------------------------------------
    def set_turn_state(self, session_id: str, turn_id: str, state: str, epoch: str) -> None:
        self._db.execute(
            "INSERT INTO turns(session_id,turn_id,state,epoch,created_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(session_id,turn_id) DO UPDATE SET state=excluded.state, epoch=excluded.epoch",
            (session_id, turn_id, state, epoch, now_iso()),
        )
        self._db.commit()

    def get_turn(self, session_id: str, turn_id: str) -> dict | None:
        row = self._db.execute(
            "SELECT * FROM turns WHERE session_id=? AND turn_id=?", (session_id, turn_id)
        ).fetchone()
        return dict(row) if row else None

    def unfinished_turns(self) -> list[dict]:
        placeholders = ",".join("?" for _ in TERMINAL_TURN_STATES)
        rows = self._db.execute(
            f"SELECT * FROM turns WHERE state NOT IN ({placeholders})",
            tuple(TERMINAL_TURN_STATES),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- events (append-only, hash-chained) ---------------------------------
    def last_event_hash(self, session_id: str) -> str | None:
        row = self._db.execute(
            "SELECT event_sha256 FROM events WHERE session_id=? "
            "ORDER BY rowid_global DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return row["event_sha256"] if row else None

    def append_event(
        self,
        session_id: str,
        turn_id: str,
        epoch: str,
        type: str,
        actor: str,
        *,
        input_objects: list[str] | None = None,
        output_objects: list[str] | None = None,
        policy_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        seq = self._next_counter(session_id, "sequence")
        prev = self.last_event_hash(session_id)
        event = {
            "event_id": ids.new_event_id(),
            "session_id": session_id,
            "turn_id": turn_id,
            "sequence": seq,
            "epoch": epoch,
            "type": type,
            "actor": actor,
            "input_objects": input_objects or [],
            "output_objects": output_objects or [],
            "policy_revision": policy_revision,
            "idempotency_key": idempotency_key,
            "timestamp": now_iso(),
            "previous_event_sha256": prev,
        }
        event_sha256 = hashlib.sha256(_canonical(event)).hexdigest()
        self._db.execute(
            "INSERT INTO events(event_id,session_id,turn_id,sequence,epoch,type,actor,"
            "input_objects,output_objects,policy_revision,idempotency_key,timestamp,"
            "previous_event_sha256,event_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event["event_id"], session_id, turn_id, seq, epoch, type, actor,
                json.dumps(event["input_objects"]), json.dumps(event["output_objects"]),
                policy_revision, idempotency_key, event["timestamp"], prev, event_sha256,
            ),
        )
        self._db.commit()
        event["event_sha256"] = event_sha256
        io = ""
        if event["input_objects"]:
            io += f" in={self._label_objs(event['input_objects'])}"
        if event["output_objects"]:
            io += f" out={self._label_objs(event['output_objects'])}"
        self._write_log(f"{event['timestamp']}  {actor:<16} {type:<20} {session_id} {turn_id}"
                        f" seq={seq}{io}")
        return event

    def _label_objs(self, oids: list[str]) -> str:
        """Annotate object ids with kind (+ source name) so log lines are self-describing."""
        parts = []
        for oid in oids:
            m = self.get_object(oid)
            if not m:
                parts.append(f"?:{oid}")
                continue
            prov = json.loads(m.get("provenance") or "{}")
            name = prov.get("catalog_name") or prov.get("relpath") or ""
            parts.append(f"{m['kind']}{f'({name})' if name else ''}:{oid}")
        return "[" + ", ".join(parts) + "]"

    def record_metric(self, session_id, turn_id, operator_bytes, context_tokens,
                      pull_count, pull_source_bytes, rendered, latency_ms):
        self._db.execute(
            "INSERT OR REPLACE INTO metrics(session_id,turn_id,operator_bytes,context_tokens,"
            "pull_count,pull_source_bytes,rendered,latency_ms) VALUES(?,?,?,?,?,?,?,?)",
            (session_id, turn_id, operator_bytes, context_tokens, pull_count,
             pull_source_bytes, 1 if rendered else 0, latency_ms))
        self._db.commit()

    def get_metric(self, session_id: str, turn_id: str) -> dict | None:
        row = self._db.execute("SELECT * FROM metrics WHERE session_id=? AND turn_id=?",
                               (session_id, turn_id)).fetchone()
        return dict(row) if row else None

    def latest_event(self, session_id: str, turn_id: str, type: str) -> dict | None:
        row = self._db.execute(
            "SELECT * FROM events WHERE session_id=? AND turn_id=? AND type=? "
            "ORDER BY rowid_global DESC LIMIT 1",
            (session_id, turn_id, type),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["output_objects"] = json.loads(d["output_objects"])
        d["input_objects"] = json.loads(d["input_objects"])
        return d

    def verify_chain(self, session_id: str) -> bool:
        """Recompute the hash chain for tamper evidence (§14.2)."""
        rows = self._db.execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY rowid_global ASC",
            (session_id,),
        ).fetchall()
        prev = None
        for r in rows:
            recomputed = hashlib.sha256(_canonical({
                "event_id": r["event_id"], "session_id": r["session_id"],
                "turn_id": r["turn_id"], "sequence": r["sequence"], "epoch": r["epoch"],
                "type": r["type"], "actor": r["actor"],
                "input_objects": json.loads(r["input_objects"]),
                "output_objects": json.loads(r["output_objects"]),
                "policy_revision": r["policy_revision"],
                "idempotency_key": r["idempotency_key"],
                "timestamp": r["timestamp"], "previous_event_sha256": prev,
            })).hexdigest()
            if r["previous_event_sha256"] != prev or r["event_sha256"] != recomputed:
                return False
            prev = r["event_sha256"]
        return True

    # --- objects metadata ---------------------------------------------------
    def record_object(self, object_id, session_id, digest, media_type, kind, size, provenance):
        self._db.execute(
            "INSERT INTO objects(object_id,session_id,digest,media_type,kind,size,provenance,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (object_id, session_id, digest, media_type, kind, size,
             json.dumps(provenance or {}), now_iso()),
        )
        self._db.commit()

    def get_object(self, object_id: str) -> dict | None:
        row = self._db.execute("SELECT * FROM objects WHERE object_id=?", (object_id,)).fetchone()
        return dict(row) if row else None

    def session_object_bytes(self, session_id: str) -> int:
        row = self._db.execute(
            "SELECT COALESCE(SUM(size),0) AS n FROM objects WHERE session_id=?", (session_id,)
        ).fetchone()
        return row["n"]

    def all_object_digests(self) -> set[str]:
        return {r["digest"] for r in self._db.execute("SELECT DISTINCT digest FROM objects").fetchall()}

    def events_for_turn(self, session_id: str, turn_id: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM events WHERE session_id=? AND turn_id=? ORDER BY rowid_global ASC",
            (session_id, turn_id)).fetchall()
        return [dict(r) for r in rows]

    def list_objects(self, session_id: str, kinds: list[str] | None = None,
                     newest_first: bool = True, limit: int | None = None) -> list[dict]:
        """List committed objects for candidate generation (design §11.2)."""
        sql = "SELECT * FROM objects WHERE session_id=?"
        params: list = [session_id]
        if kinds:
            sql += " AND kind IN (%s)" % ",".join("?" for _ in kinds)
            params.extend(kinds)
        sql += " ORDER BY created_at %s, rowid %s" % (
            ("DESC", "DESC") if newest_first else ("ASC", "ASC"))
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    # --- facts / compaction metadata (design §11.4) ------------------------
    def record_fact_meta(self, object_id, session_id, kind, method, confidence,
                         sensitivity, scope, source_objects):
        self._db.execute(
            "INSERT OR REPLACE INTO facts(object_id,session_id,kind,method,confidence,"
            "created_at,validated_at,superseded_by,sensitivity,scope,source_objects,conflict_with)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (object_id, session_id, kind, method, confidence, now_iso(), now_iso(),
             None, sensitivity, scope, json.dumps(source_objects or []), json.dumps([])),
        )
        self._db.commit()

    def facts(self, session_id: str, kind: str | None = None,
              include_superseded: bool = False) -> list[dict]:
        sql = "SELECT * FROM facts WHERE session_id=?"
        params: list = [session_id]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if not include_superseded:
            sql += " AND superseded_by IS NULL"
        rows = self._db.execute(sql + " ORDER BY created_at ASC", params).fetchall()
        return [dict(r) for r in rows]

    def supersede_fact(self, old_object_id: str, new_object_id: str) -> None:
        self._db.execute("UPDATE facts SET superseded_by=? WHERE object_id=?",
                         (new_object_id, old_object_id))
        self._db.commit()

    def mark_conflict(self, a_object_id: str, b_object_id: str) -> None:
        for x, y in ((a_object_id, b_object_id), (b_object_id, a_object_id)):
            row = self._db.execute("SELECT conflict_with FROM facts WHERE object_id=?", (x,)).fetchone()
            if row is None:
                continue
            conflicts = set(json.loads(row["conflict_with"]))
            conflicts.add(y)
            self._db.execute("UPDATE facts SET conflict_with=? WHERE object_id=?",
                             (json.dumps(sorted(conflicts)), x))
        self._db.commit()

    # --- embeddings + near-duplicates (Phase 3, model-assisted) -------------
    def put_embedding(self, object_id, session_id, model, vector: list[float]) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO embeddings(object_id,model,session_id,dim,vector,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (object_id, model, session_id, len(vector), json.dumps(vector), now_iso()),
        )
        self._db.commit()

    def get_embedding(self, object_id: str, model: str) -> list[float] | None:
        row = self._db.execute(
            "SELECT vector FROM embeddings WHERE object_id=? AND model=?", (object_id, model)
        ).fetchone()
        return json.loads(row["vector"]) if row else None

    def mark_near_duplicate(self, a, b, session_id, similarity, model) -> None:
        """Record a semantic near-duplicate link. Originals are never deleted
        (§11.5 rule 3); this only annotates."""
        for x, y in ((a, b), (b, a)):
            self._db.execute(
                "INSERT OR REPLACE INTO near_duplicates(object_a,object_b,session_id,similarity,model,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (x, y, session_id, similarity, model, now_iso()),
            )
        self._db.commit()

    def near_duplicates(self, object_id: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT object_b, similarity, model FROM near_duplicates WHERE object_a=?",
            (object_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- receipts -----------------------------------------------------------
    def record_receipt(self, session_id, turn_id, epoch, object_id, digest, selector,
                        placed_in_input, purpose, manifest_sha256):
        self._db.execute(
            "INSERT INTO receipts(session_id,turn_id,epoch,object_id,digest,selector,"
            "placed_in_input,purpose,timestamp,manifest_sha256) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (session_id, turn_id, epoch, object_id, digest, selector,
             1 if placed_in_input else 0, purpose, now_iso(), manifest_sha256),
        )
        self._db.commit()
        # Self-describing read line: what A opened (kind + source name), not just an id.
        meta = self.get_object(object_id) or {}
        prov = json.loads(meta.get("provenance") or "{}")
        name = prov.get("catalog_name") or prov.get("relpath") or ""
        what = meta.get("kind", "?") + (f"({name})" if name else "")
        sel = f" selector={selector}" if selector else ""
        self._write_log(f"{now_iso()}  {'A.adapter':<16} {'a.read':<20} {session_id} {turn_id} "
                        f"read={what:<24} object={object_id}{sel}")

    def receipts_for_turn(self, session_id: str, turn_id: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM receipts WHERE session_id=? AND turn_id=? ORDER BY id ASC",
            (session_id, turn_id),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- idempotency --------------------------------------------------------
    def idempotency_get(self, key: str) -> dict | None:
        row = self._db.execute(
            "SELECT result FROM idempotency WHERE idempotency_key=?", (key,)
        ).fetchone()
        return json.loads(row["result"]) if row else None

    def idempotency_put(self, key: str, session_id: str, result: dict) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO idempotency(idempotency_key,session_id,result,created_at)"
            " VALUES(?,?,?,?)",
            (key, session_id, json.dumps(result), now_iso()),
        )
        self._db.commit()

    # --- renders (idempotent display, invariant 14) -------------------------
    def is_rendered(self, session_id: str, turn_id: str) -> bool:
        return self._db.execute(
            "SELECT 1 FROM renders WHERE session_id=? AND turn_id=?", (session_id, turn_id)
        ).fetchone() is not None

    def record_render(self, session_id: str, turn_id: str, body_sha256: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO renders(session_id,turn_id,body_sha256,rendered_at)"
            " VALUES(?,?,?,?)",
            (session_id, turn_id, body_sha256, now_iso()),
        )
        self._db.commit()

    # --- pulls --------------------------------------------------------------
    def record_pull(self, session_id, turn_id, request_id, decision, result_object):
        self._db.execute(
            "INSERT OR REPLACE INTO pulls(session_id,turn_id,request_id,decision,result_object,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (session_id, turn_id, request_id, decision, result_object, now_iso()),
        )
        self._db.commit()
