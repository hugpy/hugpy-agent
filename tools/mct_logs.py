#!/usr/bin/env python3
"""Dump the full C / B / A logs for an MCT workspace.

Each log is reconstructed from the durable object store + ledger — complete and
reproducible. C = operator conversation; B = hash-chained event ledger; A =
everything A received/opened/produced (incl. its transcript).

Usage:
  PYTHONPATH=src python3 tools/mct_logs.py <workspace> [--session ID]
      [--who a|b|c|all] [--format text|jsonl] [--out DIR]

With no --session, lists sessions in the workspace. --out writes A.log/B.log/C.log.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hugpy_agent.mct.session import BrokerServer


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workspace")
    ap.add_argument("--session")
    ap.add_argument("--who", choices=["a", "b", "c", "map", "tokens", "metrics", "all"],
                    default="all")
    ap.add_argument("--format", choices=["text", "jsonl"], default="text")
    ap.add_argument("--out")
    args = ap.parse_args()

    server = BrokerServer(args.workspace, sink=lambda *_: None)
    try:
        sessions = [dict(r) for r in server.ledger._db.execute(
            "SELECT session_id, created_at FROM sessions ORDER BY created_at").fetchall()]
        if not args.session:
            print(f"{len(sessions)} session(s) in {args.workspace}:")
            for s in sessions:
                print(f"  {s['session_id']}  ({s['created_at']})")
            print("\nre-run with --session <id> [--who a|b|c|all]")
            return 0

        sid = args.session
        logs = server.logs
        if args.out:
            paths = logs.write_all(sid, args.out)
            print("wrote:", ", ".join(f"{k}={v}" for k, v in paths.items()))
            return 0

        if args.format == "jsonl":
            data = {"C": logs.c_log(sid), "B": logs.b_log(sid), "A": logs.a_log(sid)}
            which = {"a": ["A"], "b": ["B"], "c": ["C"], "all": ["C", "B", "A"]}[args.who]
            for w in which:
                print(json.dumps({w: data[w]}))
            return 0

        renderers = {"c": logs.render_c, "b": logs.render_b, "a": logs.render_a,
                     "map": logs.render_map, "tokens": server.tokens.render,
                     "metrics": server.metrics.render}
        order = ["metrics", "tokens", "map", "c", "b", "a"]
        for who in (order if args.who == "all" else [args.who]):
            print(renderers[who](sid) + "\n")
        return 0
    finally:
        server.close()


if __name__ == "__main__":
    sys.exit(main())
