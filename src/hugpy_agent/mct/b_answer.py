"""B answers as itself, grounded in a NAMED mct workspace — one shot, for a
remote console.

Grounding follows the session (operator, 2026-08-12): the frontier session's
state (ledger, catalog, derived memory) lives on the machine A runs on, so a
console hosted ELSEWHERE must ask B *here* rather than grounding a lookalike B
in its own (empty) workspace. The fleet-console's /api/b/chat execs this
module inside the model VM:

    echo '{"workspace": "...", "text": "...", "history": [...]}' \\
      | python3 -m hugpy_agent.mct.b_answer

stdin : {"workspace": <mct workspace dir>, "text": <operator message>,
         "history"?: [{"role": "user"|"assistant", "content": str}, ...]}
stdout: {"reply": str, "offline": bool, "mode": str, "model": str,
         "tokens": int, "meta": str}

Lookup-first (operator, 2026-09-29): the message is classified BEFORE any
model call (:mod:`hugpy_agent.mct.b_lookup`). State questions, searches over
the catalog/memory/ledger/log, acks, help, and every question against an EMPTY
state are answered deterministically — ``mode`` says which, ``model`` is
``"lookup"`` and ``tokens`` 0 — so the fleet's Coder-Next slot is only spent
on synthesis over real content. Never raises for gateway trouble — a dead
gateway degrades to B's deterministic state readout with the error named
(same contract as the console's own fallback). B never impersonates A.
"""
from __future__ import annotations

import json
import sys


def answer(ws: str, text: str, history: list | None = None, **kw) -> dict:
    """The one-shot body, importable for tests: open the workspace, answer,
    close. ``kw`` passes through to :func:`b_lookup.respond` (``chat`` etc.)."""
    from hugpy_agent.mct.b_lookup import respond
    from hugpy_agent.mct.session import BrokerServer
    srv = BrokerServer(ws, sink=lambda *_a, **_k: None)
    try:
        sess = srv.session(srv.open_session("console-b"))
        return respond(text, sess, srv, {"last": None}, history=history or [],
                       workspace=ws, **kw)
    finally:
        try:
            srv.close()
        except Exception:
            pass


def main() -> int:
    req = json.load(sys.stdin)
    ws = req.get("workspace") or ""
    text = (req.get("text") or "").strip()
    history = req.get("history") or []
    if not ws or not text:
        json.dump({"reply": "b_answer: workspace and text are required",
                   "offline": True}, sys.stdout)
        return 2
    json.dump(answer(ws, text, history), sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
