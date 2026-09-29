"""Comprehensive session metrics — cache contents, totals, and every dimension.

One consolidated view over durable state: the object store ("B's cache"), token/
cost accounting (Claude Code's server-side cache), turns, pulls, reads, memory,
and ledger integrity. Everything is derived from the store + ledger, so it is
exact and reproducible for any session.
"""
from __future__ import annotations

import json


class Metrics:
    def __init__(self, server):
        self.server = server

    def _q(self, sql, params=()):
        return self.server.ledger._db.execute(sql, params).fetchall()

    def full_report(self, session_id: str) -> dict:
        led = self.server.ledger
        s = (session_id,)

        # --- B's cache: the object store contents ---
        by_kind = {r["kind"]: {"count": r["n"], "bytes": r["b"]} for r in self._q(
            "SELECT kind, COUNT(*) n, COALESCE(SUM(size),0) b FROM objects "
            "WHERE session_id=? GROUP BY kind ORDER BY b DESC", s)}
        total_objs = sum(v["count"] for v in by_kind.values())
        total_bytes = sum(v["bytes"] for v in by_kind.values())
        distinct = self._q("SELECT COUNT(DISTINCT digest) d FROM objects WHERE session_id=?", s)[0]["d"]

        # --- tokens / cost (Claude Code cache) ---
        tokens = self.server.tokens.report(session_id)

        # --- turns ---
        turns_by_state = {r["state"]: r["n"] for r in self._q(
            "SELECT state, COUNT(*) n FROM turns WHERE session_id=? GROUP BY state", s)}

        # --- per-turn metrics (latency, context tokens, pulls) ---
        m = self._q("SELECT COALESCE(AVG(latency_ms),0) al, COALESCE(MAX(latency_ms),0) ml, "
                    "COALESCE(AVG(context_tokens),0) ac, COALESCE(SUM(pull_count),0) sp, "
                    "COALESCE(SUM(operator_bytes),0) ob FROM metrics WHERE session_id=?", s)[0]

        # --- pulls by decision ---
        pulls = {r["decision"]: r["n"] for r in self._q(
            "SELECT decision, COUNT(*) n FROM pulls WHERE session_id=? GROUP BY decision", s)}

        # --- what A actually opened (receipts), by kind ---
        reads = {r["kind"]: r["n"] for r in self._q(
            "SELECT o.kind kind, COUNT(*) n FROM receipts r JOIN objects o "
            "ON r.object_id=o.object_id WHERE r.session_id=? GROUP BY o.kind", s)}

        # --- durable memory ---
        facts = {r["kind"]: r["n"] for r in self._q(
            "SELECT kind, COUNT(*) n FROM facts WHERE session_id=? AND superseded_by IS NULL "
            "GROUP BY kind", s)}
        superseded = self._q("SELECT COUNT(*) n FROM facts WHERE session_id=? AND superseded_by "
                             "IS NOT NULL", s)[0]["n"]
        near_dups = self._q("SELECT COUNT(*) n FROM near_duplicates WHERE session_id=?", s)[0]["n"] // 2
        conflicts = sum(1 for r in self._q(
            "SELECT conflict_with FROM facts WHERE session_id=?", s)
            if json.loads(r["conflict_with"]) != [])

        # --- ledger integrity ---
        events = self._q("SELECT COUNT(*) n FROM events WHERE session_id=?", s)[0]["n"]
        epochs = self._q("SELECT COUNT(*) n FROM epochs WHERE session_id=?", s)[0]["n"]

        return {
            "session": session_id,
            "cache_store": {
                "objects": total_objs, "total_bytes": total_bytes,
                "total_tokens_est": total_bytes // 4, "distinct_digests": distinct,
                "dedup_saved_objects": total_objs - distinct, "by_kind": by_kind,
                "path": str(self.server.root / "objects" / "sha256"),
            },
            "tokens": tokens,
            "turns": {"total": sum(turns_by_state.values()), "by_state": turns_by_state,
                      "operator_bytes_total": m["ob"]},
            "latency_ms": {"avg": round(m["al"], 1), "max": round(m["ml"], 1)},
            "context": {"avg_context_tokens": round(m["ac"], 1)},
            "pulls": {"total": sum(pulls.values()), "by_decision": pulls},
            "reads": {"total": sum(reads.values()), "by_kind": reads},
            "memory": {"facts_live": sum(facts.values()), "by_kind": facts,
                       "superseded": superseded, "near_duplicates": near_dups,
                       "conflicts": conflicts},
            "ledger": {"events": events, "epochs": epochs,
                       "chain_verified": led.verify_chain(session_id)},
        }

    def render(self, session_id: str) -> str:
        r = self.full_report(session_id)
        cs, tk = r["cache_store"], r["tokens"]
        L = [f"# METRICS — {session_id}", ""]

        L.append("## B's cache (object store)")
        L.append(f"  objects: {cs['objects']}   bytes: {cs['total_bytes']:,}   "
                 f"~tokens: {cs['total_tokens_est']:,}   "
                 f"dedup-saved: {cs['dedup_saved_objects']} objects")
        L.append(f"  path: {cs['path']}")
        L.append("  contents by kind:")
        for kind, v in cs["by_kind"].items():
            L.append(f"    {kind:<20}{v['count']:>4}  {v['bytes']:>10,} B")

        L.append("\n## A's cache (tokens relayed to the model, Claude Code cache)")
        L.append(f"  turns: {tk['turns']}   cost: ${tk['cost_usd']:.4f}")
        L.append(f"  input relayed: {tk['relayed_input']:,}  "
                 f"(new={tk['input']:,}  cache-write={tk['cache_write']:,}  "
                 f"cache-read={tk['cache_read']:,})")
        L.append(f"  output: {tk['output']:,}   cached: {tk['cached_pct']}%   "
                 f"input-cost saved by cache: {tk['input_cost_saved_by_cache_pct']}%")

        L.append("\n## turns / latency / context")
        L.append(f"  turns: {r['turns']['total']}  states={r['turns']['by_state']}")
        L.append(f"  latency_ms avg={r['latency_ms']['avg']} max={r['latency_ms']['max']}   "
                 f"avg context tokens/turn={r['context']['avg_context_tokens']}")

        L.append("\n## pulls / reads / memory")
        L.append(f"  pulls: {r['pulls']['total']}  by_decision={r['pulls']['by_decision']}")
        L.append(f"  A reads: {r['reads']['total']}  by_kind={r['reads']['by_kind']}")
        me = r["memory"]
        L.append(f"  memory: facts_live={me['facts_live']} by_kind={me['by_kind']} "
                 f"superseded={me['superseded']} near_dups={me['near_duplicates']} "
                 f"conflicts={me['conflicts']}")

        L.append("\n## ledger integrity")
        L.append(f"  events: {r['ledger']['events']}   epochs: {r['ledger']['epochs']}   "
                 f"chain_verified: {r['ledger']['chain_verified']}")
        return "\n".join(L)
