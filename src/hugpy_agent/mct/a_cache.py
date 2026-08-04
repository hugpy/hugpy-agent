"""A-cache mirror: a durable, reconstructable record of everything A received.

A's own KV/prompt cache is ephemeral and opaque (it lives inside the per-turn
Claude Code process and dies when it exits). The point of MCT is that B keeps a
*complete, durable* mirror of it: every byte A was delivered, every object it
opened, every pull it made, its full agent transcript, and its answer — all
immutable objects in B's store, reassembled here into A's working set.

Design ref: §12.1 (cache #2, the A-adapter working-set), §12.2 (receipts),
invariant 6 (all A reads observed by B's transport), §18 (traces). This is the
authoritative answer to "what did A actually have in context?" — reconstructed
from durable state, never from A's word.
"""
from __future__ import annotations

import json

from .protocol import make_pointer, parse_pointer


class AWorkingSet:
    def __init__(self, server):
        self.server = server

    # --- content resolution -------------------------------------------------
    def _content(self, session_id, object_id, selector=None, limit=None):
        try:
            data = self.server.store.resolve(session_id, make_pointer(session_id, object_id), selector)
            text = data.decode("utf-8", "replace")
            return text if limit is None else text[:limit]
        except Exception:
            return None

    def _objects_of_kind(self, session_id, turn_id, kinds):
        out = []
        for meta in self.server.ledger.list_objects(session_id, kinds=kinds, newest_first=False):
            prov = json.loads(meta.get("provenance") or "{}")
            if prov.get("turn_id") == turn_id:
                out.append(meta)
        return out

    # --- per-turn reconstruction -------------------------------------------
    def for_turn(self, session_id: str, turn_id: str, *, include_content: bool = True,
                 content_limit: int | None = 2000) -> dict:
        led = self.server.ledger

        def content(oid, selector=None):
            return self._content(session_id, oid, selector, content_limit) if include_content else None

        def path(oid):  # the concrete file backing every object — nothing is opaque
            p = self.server.store.path_for(session_id, oid)
            return str(p) if p else None

        inputs = []
        for meta in self._objects_of_kind(session_id, turn_id, ["a_system_prompt", "a_prompt"]):
            role = "system_prompt" if meta["kind"] == "a_system_prompt" else "prompt"
            inputs.append({"role": role, "object": meta["object_id"], "path": path(meta["object_id"]),
                           "bytes": meta["size"], "content": content(meta["object_id"])})

        # everything A opened, in the order the adapter recorded it (invariant 6)
        reads = []
        for r in led.receipts_for_turn(session_id, turn_id):
            kind = (led.get_object(r["object_id"]) or {}).get("kind")
            reads.append({"role": "opened", "object": r["object_id"], "kind": kind,
                          "path": path(r["object_id"]),
                          "digest": r["digest"], "selector": r["selector"],
                          "purpose": r["purpose"], "placed_in_input": bool(r["placed_in_input"]),
                          "content": content(r["object_id"], r["selector"])})

        # A's outputs: pull requests it issued, and its response
        outputs = []
        pulls = {p["request_id"]: p for p in [dict(x) for x in led._db.execute(
            "SELECT * FROM pulls WHERE session_id=? AND turn_id=?", (session_id, turn_id)).fetchall()]}
        for ev in led.events_for_turn(session_id, turn_id):
            if ev["type"] == "pull.requested":
                for oid in json.loads(ev["input_objects"]):
                    req = self._content(session_id, oid) if include_content else None
                    outputs.append({"role": "pull_request", "object": oid,
                                    "path": path(oid), "content": req})
        resp = led.latest_event(session_id, turn_id, "a.response_ready") or \
            led.latest_event(session_id, turn_id, "response.rendered")
        if resp and resp["output_objects"]:
            manifest_oid = resp["output_objects"][0]
            manifest = self._content(session_id, manifest_oid)
            body_ptr = None
            try:
                body_ptr = json.loads(manifest).get("body") if manifest else None
            except Exception:
                pass
            body = None
            if body_ptr and include_content:
                body = self._content(session_id, parse_pointer(body_ptr)[1])
            body_oid = parse_pointer(body_ptr)[1] if body_ptr else manifest_oid
            outputs.append({"role": "response", "object": body_oid, "path": path(body_oid),
                            "manifest": manifest_oid, "content": body})

        transcript = None
        for meta in self._objects_of_kind(session_id, turn_id, ["a_transcript"]):
            transcript = {"object": meta["object_id"], "path": path(meta["object_id"]),
                          "bytes": meta["size"], "content": content(meta["object_id"])}

        turn = led.get_turn(session_id, turn_id) or {}
        return {"turn_id": turn_id, "epoch": turn.get("epoch"),
                "inputs": inputs, "reads": reads, "outputs": outputs, "transcript": transcript}

    def for_session(self, session_id: str, *, include_content: bool = False) -> list[dict]:
        turns = self.server.ledger._db.execute(
            "SELECT turn_id FROM turns WHERE session_id=? ORDER BY turn_id", (session_id,)).fetchall()
        return [self.for_turn(session_id, t["turn_id"], include_content=include_content)
                for t in turns]

    def stats(self, session_id: str) -> dict:
        turns = self.for_session(session_id, include_content=False)
        reads = sum(len(t["reads"]) for t in turns)
        pulls = sum(1 for t in turns for o in t["outputs"] if o["role"] == "pull_request")
        transcripts = sum(1 for t in turns if t["transcript"])
        return {"turns": len(turns), "objects_A_opened": reads, "pulls_A_issued": pulls,
                "transcripts_captured": transcripts}

    def dump(self, session_id: str, *, content_limit: int = 400) -> str:
        """Human-readable dump of A's full accumulated cache across the session."""
        lines = []
        for t in self.for_session(session_id, include_content=True):
            lines.append(f"── turn {t['turn_id']} (epoch {t['epoch']}) ──")
            for i in t["inputs"]:
                lines.append(f"  IN  {i['role']}  @ {i['path']}")
                lines.append(f"        {(i['content'] or '')[:content_limit]!r}")
            for r in t["reads"]:
                sel = f" [{r['selector']}]" if r["selector"] else ""
                lines.append(f"  READ {r['kind']}{sel} ({r['purpose']})  @ {r['path']}")
                lines.append(f"        {(r['content'] or '')[:content_limit]!r}")
            for o in t["outputs"]:
                lines.append(f"  OUT {o['role']}  @ {o.get('path')}")
                lines.append(f"        {(o['content'] or '')[:content_limit]!r}")
            if t["transcript"]:
                lines.append(f"  TRANSCRIPT: {t['transcript']['bytes']} bytes  "
                             f"@ {t['transcript']['path']}")
        return "\n".join(lines)
