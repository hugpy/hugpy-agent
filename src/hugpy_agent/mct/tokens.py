"""Precise per-turn / per-session token accounting for A.

Source of truth is A's stored transcript (the ``a_transcript`` object holds
Claude Code's exact ``usage`` + ``total_cost_usd``), so this works for any
session — live or historical — with no separate bookkeeping.

Billing multipliers (Anthropic prompt caching), applied to *input* tokens:
  new input      × 1.00
  cache write    × 1.25 (5-min TTL)  or × 2.00 (1-hour TTL)
  cache read     × 0.10
Output tokens are billed separately and are unaffected by caching. We report a
rate-agnostic "input billed-equivalent" so the caching effect is exact without
hardcoding dollar prices, alongside Claude Code's own precise ``cost_usd``.
"""
from __future__ import annotations

import json


def summary_from_result(result: dict) -> dict:
    """Extract a precise token summary from a Claude Code result object."""
    if not isinstance(result, dict):
        return {}
    u = result.get("usage", {}) or {}
    cc = u.get("cache_creation", {}) or {}
    eph1h = cc.get("ephemeral_1h_input_tokens", 0)
    eph5m = cc.get("ephemeral_5m_input_tokens", 0)
    cache_write = u.get("cache_creation_input_tokens", 0)
    cache_read = u.get("cache_read_input_tokens", 0)
    new_input = u.get("input_tokens", 0)

    # exact billed-equivalent of the input side (in units of base input tokens)
    if eph1h or eph5m:
        billed_write = 2.0 * eph1h + 1.25 * eph5m
    else:
        billed_write = 1.25 * cache_write
    relayed = new_input + cache_write + cache_read           # total context sent to model
    billed_equiv = new_input + billed_write + 0.10 * cache_read
    return {
        "input": new_input,
        "cache_write": cache_write,
        "cache_read": cache_read,
        "output": u.get("output_tokens", 0),
        "relayed_input": relayed,
        "billed_input_equiv": round(billed_equiv, 1),
        "cost_usd": result.get("total_cost_usd", 0.0),
        "cached_pct": round(100.0 * cache_read / relayed, 1) if relayed else 0.0,
    }


class TokenUsage:
    def __init__(self, server):
        self.server = server

    def per_turn(self, session_id: str) -> list[dict]:
        out = []
        for m in self.server.ledger.list_objects(session_id, kinds=["a_transcript"],
                                                  newest_first=False):
            prov = json.loads(m.get("provenance") or "{}")
            from .protocol import make_pointer
            raw = self.server.store.resolve(session_id, make_pointer(session_id, m["object_id"])) \
                .decode("utf-8", "replace")
            result = None
            for line in raw.splitlines():
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("type") == "result":
                    result = o
            if result:
                s = summary_from_result(result)
                s["turn"] = prov.get("turn_id", "?")
                out.append(s)
        return out

    def report(self, session_id: str) -> dict:
        rows = self.per_turn(session_id)
        agg = {k: 0 for k in ("input", "cache_write", "cache_read", "output",
                              "relayed_input", "billed_input_equiv")}
        cost = 0.0
        for r in rows:
            for k in agg:
                agg[k] += r[k]
            cost += r["cost_usd"]
        relayed = agg["relayed_input"]
        no_cache = agg["relayed_input"]           # if nothing were cached, all input × 1.0
        savings = (1 - agg["billed_input_equiv"] / no_cache) if no_cache else 0.0
        return {
            "turns": len(rows), **agg, "cost_usd": round(cost, 4),
            "cached_pct": round(100.0 * agg["cache_read"] / relayed, 1) if relayed else 0.0,
            "input_cost_saved_by_cache_pct": round(100.0 * savings, 1),
        }

    def render(self, session_id: str) -> str:
        rows = self.per_turn(session_id)
        lines = [f"# Token usage — {session_id}",
                 f"  {'turn':<10}{'input':>8}{'cache-wr':>10}{'cache-rd':>11}"
                 f"{'output':>8}{'cached%':>9}{'cost$':>10}"]
        for r in rows:
            lines.append(f"  {r['turn']:<10}{r['input']:>8}{r['cache_write']:>10}"
                         f"{r['cache_read']:>11}{r['output']:>8}{r['cached_pct']:>8}%"
                         f"{r['cost_usd']:>10.4f}")
        rep = self.report(session_id)
        lines += [
            "  " + "-" * 56,
            f"  {'TOTAL':<10}{rep['input']:>8}{rep['cache_write']:>10}"
            f"{rep['cache_read']:>11}{rep['output']:>8}{rep['cached_pct']:>8}%"
            f"{rep['cost_usd']:>10.4f}",
            "",
            f"  context relayed to model : {rep['relayed_input']:,} input tokens "
            f"({rep['cached_pct']}% served from cache at 10%)",
            f"  billed input-equivalent  : {rep['billed_input_equiv']:,.0f} tokens "
            f"(caching saved {rep['input_cost_saved_by_cache_pct']}% of input cost)",
            f"  total cost               : ${rep['cost_usd']:.4f}",
        ]
        return "\n".join(lines)
