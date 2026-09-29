"""Observability: per-turn metrics and content-safe traces.

Design ref: §18 (metrics, structured traces, content privacy), §21 (``telemetry.py``).

Operational telemetry stores object IDs, digests, sizes, decision codes, and
timing — **never prompt bodies or secrets** (§18.3). ``turn_report`` assembles a
trace that lets an operator answer "what did A open? why was a pull reduced?
which policy governed it?" from the append-only ledger, and ``is_content_safe``
verifies no body text leaked into it.
"""
from __future__ import annotations

# Fields that would carry conversational bodies; must never appear in telemetry.
_FORBIDDEN_KEYS = {"body", "prompt", "text", "content", "message", "answer"}


class Telemetry:
    def __init__(self, server):
        self.server = server

    def turn_report(self, session_id: str, turn_id: str) -> dict:
        led = self.server.ledger
        events = led.events_for_turn(session_id, turn_id)
        pulls = led._db.execute(
            "SELECT request_id, decision FROM pulls WHERE session_id=? AND turn_id=?",
            (session_id, turn_id)).fetchall()
        return {
            "session_id": session_id,
            "turn_id": turn_id,
            "metric": led.get_metric(session_id, turn_id),
            "events": [{"type": e["type"], "actor": e["actor"], "sequence": e["sequence"],
                        "input_objects": e["input_objects"], "output_objects": e["output_objects"],
                        "policy_revision": e["policy_revision"], "timestamp": e["timestamp"]}
                       for e in events],
            "pull_decisions": [dict(p) for p in pulls],
        }

    def session_summary(self, session_id: str) -> dict:
        rows = self.server.ledger._db.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(context_tokens),0) ctx, "
            "COALESCE(SUM(pull_count),0) pulls, COALESCE(AVG(latency_ms),0) lat "
            "FROM metrics WHERE session_id=?", (session_id,)).fetchone()
        return {"turns": rows["n"], "total_context_tokens": rows["ctx"],
                "total_pulls": rows["pulls"], "avg_latency_ms": round(rows["lat"], 2)}

    @staticmethod
    def is_content_safe(report: dict) -> bool:
        """True iff the report contains no conversational-body fields (§18.3)."""
        stack = [report]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for k, v in node.items():
                    if k in _FORBIDDEN_KEYS:
                        return False
                    stack.append(v)
            elif isinstance(node, list):
                stack.extend(node)
        return True
