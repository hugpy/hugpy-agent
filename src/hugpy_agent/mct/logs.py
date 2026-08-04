"""Three-party logs: a full record of C, B, and A, from durable state.

Each party's log is reconstructed entirely from the object store + append-only
ledger (§17.1) — never from live memory — so it is complete and reproducible:

- **C log** — the operator's view: every message C sent and every answer C was
  shown (or the explicit failure notice when B could not answer). §5.
- **B log** — the broker's authoritative, hash-chained event ledger for the
  session: every transition, pull decision, epoch change, and per-turn metric.
  Body-free by construction (§14, §18.3).
- **A log** — everything A received/opened/produced, including its full agent
  transcript (delegates to the A-cache mirror, §12).
"""
from __future__ import annotations

import json

from .protocol import make_pointer, parse_pointer


class Logs:
    def __init__(self, server):
        self.server = server

    # --- helpers -----------------------------------------------------------
    def _turns(self, session_id):
        return [dict(r) for r in self.server.ledger._db.execute(
            "SELECT turn_id, state, epoch FROM turns WHERE session_id=? ORDER BY turn_id",
            (session_id,)).fetchall()]

    def _resolve(self, session_id, object_id):
        try:
            return self.server.store.resolve(session_id, make_pointer(session_id, object_id)) \
                .decode("utf-8", "replace")
        except Exception:
            return None

    def _find_by_kind(self, session_id, turn_id, kind):
        led = self.server.ledger
        for ev in led.events_for_turn(session_id, turn_id):
            for oid in json.loads(ev["input_objects"]) + json.loads(ev["output_objects"]):
                meta = led.get_object(oid)
                if meta and meta["kind"] == kind:
                    return oid
        return None

    def _response_body(self, session_id, turn_id):
        oid = self._find_by_kind(session_id, turn_id, "response_manifest")
        if not oid:
            return None
        manifest = self._resolve(session_id, oid)
        try:
            body_ptr = json.loads(manifest)["body"]
            return self._resolve(session_id, parse_pointer(body_ptr)[1])
        except Exception:
            return None

    # --- C log -------------------------------------------------------------
    def c_log(self, session_id: str) -> list[dict]:
        led = self.server.ledger
        out = []
        for t in self._turns(session_id):
            op_oid = self._find_by_kind(session_id, t["turn_id"], "operator_turn")
            operator = self._resolve(session_id, op_oid) if op_oid else None
            rendered = led.is_rendered(session_id, t["turn_id"])
            answer = self._response_body(session_id, t["turn_id"]) if rendered else None
            out.append({
                "turn": t["turn_id"],
                "operator": operator,
                "assistant": answer,
                "shown": rendered,
                "notice": None if rendered else f"[A did not answer — turn {t['state']}]",
            })
        return out

    # --- B log (the authoritative hash-chained ledger) ---------------------
    def b_log(self, session_id: str) -> dict:
        led = self.server.ledger
        rows = led._db.execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY sequence", (session_id,)).fetchall()
        pulls = {(p["turn_id"], p["request_id"]): p["decision"] for p in
                 [dict(x) for x in led._db.execute(
                     "SELECT turn_id, request_id, decision FROM pulls WHERE session_id=?",
                     (session_id,)).fetchall()]}
        events = [{"sequence": r["sequence"], "turn": r["turn_id"], "epoch": r["epoch"],
                   "type": r["type"], "actor": r["actor"],
                   "input_objects": json.loads(r["input_objects"]),
                   "output_objects": json.loads(r["output_objects"]),
                   "policy_revision": r["policy_revision"], "timestamp": r["timestamp"],
                   "event_sha256": r["event_sha256"]} for r in rows]
        metrics = [dict(m) for m in led._db.execute(
            "SELECT * FROM metrics WHERE session_id=?", (session_id,)).fetchall()]
        return {"chain_verified": led.verify_chain(session_id), "events": events,
                "pull_decisions": [{"turn": k[0], "request": k[1], "decision": v}
                                   for k, v in pulls.items()],
                "metrics": metrics}

    # --- A log (delegates to the A-cache mirror) ---------------------------
    def a_log(self, session_id: str, *, include_content: bool = True) -> list[dict]:
        return self.server.a_cache.for_session(session_id, include_content=include_content)

    # --- rendering ---------------------------------------------------------
    def render_c(self, session_id: str) -> str:
        lines = ["# C log — operator conversation"]
        for e in self.c_log(session_id):
            lines.append(f"\n[{e['turn']}] you> {e['operator']}")
            if e["shown"]:
                lines.append(f"        A> {e['assistant']}")
            else:
                lines.append(f"        {e['notice']}")
        return "\n".join(lines)

    def render_b(self, session_id: str) -> str:
        b = self.b_log(session_id)
        lines = [f"# B log — event ledger (chain_verified={b['chain_verified']})"]
        for ev in b["events"]:
            io = ""
            if ev["input_objects"] or ev["output_objects"]:
                io = f"  in={ev['input_objects']} out={ev['output_objects']}"
            lines.append(f"{ev['sequence']:>4} {ev['timestamp']} {ev['actor']:<16} "
                         f"{ev['type']}{io}")
        if b["metrics"]:
            lines.append("\n## per-turn metrics")
            for m in b["metrics"]:
                lines.append(f"  {m['turn_id']}: op_bytes={m['operator_bytes']} "
                             f"ctx_tokens={m['context_tokens']} pulls={m['pull_count']} "
                             f"latency_ms={m['latency_ms']}")
        return "\n".join(lines)

    def render_a(self, session_id: str) -> str:
        return "# A log — everything A received/opened/produced\n" + \
            self.server.a_cache.dump(session_id)

    # --- the complete map: where every artifact physically lives -----------
    def object_index(self, session_id: str) -> list[dict]:
        """Every object in the session with its concrete file path — nothing opaque."""
        rows = self.server.ledger.list_objects(session_id, newest_first=False)
        out = []
        for m in rows:
            p = self.server.store.path_for(session_id, m["object_id"])
            out.append({"object": m["object_id"], "kind": m["kind"], "digest": m["digest"],
                        "size": m["size"], "media_type": m["media_type"],
                        "pointer": make_pointer(session_id, m["object_id"]),
                        "path": str(p) if p else None, "created_at": m["created_at"]})
        return out

    def where(self, session_id: str, ref: str) -> dict | None:
        """Resolve any pointer or object id to its file path + metadata."""
        oid = parse_pointer(ref)[1] if ref.startswith("mct://") else ref
        meta = self.server.ledger.get_object(oid)
        if not meta or meta["session_id"] != session_id:
            return None
        p = self.server.store.path_for(session_id, oid)
        return {"object": oid, "kind": meta["kind"], "digest": meta["digest"],
                "size": meta["size"], "path": str(p) if p else None,
                "pointer": make_pointer(session_id, oid)}

    def render_map(self, session_id: str) -> str:
        root = self.server.root
        lines = [
            "# MAP — where everything for this session physically lives",
            f"workspace root : {self.server.workspace_root}",
            f"ledger (SQLite): {root / 'mct.db'}",
            f"object store   : {root / 'objects' / 'sha256'}/<aa>/<bb>/<digest>",
            f"quarantine     : {root / 'quarantine'}",
            "",
            f"## objects ({session_id})",
            f"  {'kind':<20}{'size':>8}  {'object':<30} path",
        ]
        for o in self.object_index(session_id):
            lines.append(f"  {o['kind']:<20}{o['size']:>8}  {o['object']:<30} {o['path']}")
        return "\n".join(lines)

    def write_all(self, session_id: str, out_dir) -> dict:
        from pathlib import Path
        d = Path(out_dir)
        d.mkdir(parents=True, exist_ok=True)
        paths = {}
        for who, text in (("C", self.render_c(session_id)),
                          ("B", self.render_b(session_id)),
                          ("A", self.render_a(session_id)),
                          ("MAP", self.render_map(session_id))):
            p = d / f"{who}.log"
            p.write_text(text)
            paths[who] = str(p)
        return paths
