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
stdout: {"reply": str, "offline": bool}

Never raises for gateway trouble — a dead gateway degrades to B's
deterministic state readout with the error named (same contract as the
console's own fallback). B never impersonates A.
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    req = json.load(sys.stdin)
    ws = req.get("workspace") or ""
    text = (req.get("text") or "").strip()
    history = req.get("history") or []
    if not ws or not text:
        json.dump({"reply": "b_answer: workspace and text are required",
                   "offline": True}, sys.stdout)
        return 2

    from hugpy_agent.mct.repl import _b_state_text
    from hugpy_agent.mct.session import BrokerServer
    srv = BrokerServer(ws, sink=lambda *_a, **_k: None)
    try:
        sess = srv.session(srv.open_session("console-b"))
        ground = _b_state_text(sess, srv, {"last": None})
        sysmsg = (
            "You are B — the broker/curator of this station's Mediated Context "
            "Terminal. You curate bounded context for A (a confined Claude) "
            "and keep the ledger, catalog, and derived memory. Answer the "
            "operator directly, concisely, in first person as B. Ground every "
            "claim in the state below; when the state does not contain the "
            "answer, say so plainly.\n\n=== your current state ===\n" + ground)
        msgs = [{"role": "system", "content": sysmsg}]
        for m in history[-20:]:
            r, c = m.get("role"), m.get("content")
            if r in ("user", "assistant") and isinstance(c, str):
                msgs.append({"role": r, "content": c})
        msgs.append({"role": "user", "content": text})
        try:
            from hugpy_agent.config import load_config
            from hugpy_agent.gateway import Gateway
            gw = Gateway.from_config(load_config(workspace=ws))
            res = gw.chat(msgs, max_tokens=700)
            reply = getattr(res, "text", None) or getattr(res, "content", None)
            # gateways report failure as a result object, not an exception —
            # surface it as the offline readout, never as a repr'd ChatResult.
            if getattr(res, "ok", True) is False or not (reply or "").strip():
                raise RuntimeError(getattr(res, "error", None)
                                   or "gateway returned no text")
            out = {"reply": reply.strip(), "offline": False}
        except Exception as exc:
            out = {"reply": "[B offline - deterministic state readout]\n"
                            f"(gateway unavailable: {type(exc).__name__}: {exc})"
                            "\n\n" + ground,
                   "offline": True}
    finally:
        try:
            srv.close()
        except Exception:
            pass
    json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
