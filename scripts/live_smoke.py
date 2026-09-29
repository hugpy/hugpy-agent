#!/usr/bin/env python3
"""Live smoke test against a real fleet — gated so CI/offline runs never
touch the network.

    HUGPY_AGENT_LIVE=1 python scripts/live_smoke.py

Three checks, deliberately tiny (max_tokens <= 64):
  1. models list resolves and is non-empty
  2. one small chat completes (streaming path)
  3. one prompted tool round-trip: schema in system prompt -> <tool_call>
     parsed -> tool_response sent back -> model acknowledges

ML section (SPENDS GPU — one embed + ONE small sd-turbo image) is separately
gated because casual smoke runs must not burn fleet compute:

    HUGPY_AGENT_LIVE=1 HUGPY_AGENT_LIVE_ML=1 python scripts/live_smoke.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from hugpy_agent.adapter import Adapter                      # noqa: E402
from hugpy_agent.config import load_config                   # noqa: E402
from hugpy_agent.gateway import Gateway                      # noqa: E402
from hugpy_agent.tools import ToolContext, ToolSpec          # noqa: E402
from hugpy_agent.tools.fleet import FleetTools               # noqa: E402


def ml_section(cfg, gw) -> None:
    """One sync amenity + one minimal generation, per the Phase-1.5 brief."""
    ws = tempfile.mkdtemp(prefix="hugpy-smoke-")
    ft = FleetTools(gw, ws)
    print("ML workspace: %s" % ws)

    # 4. one sync amenity (embed — smallest GPU footprint)
    out = json.loads(ft.embed("the quick brown fox"))
    print("[4] embed: %s" % json.dumps(out)[:160])
    assert "dims" in out, "embed failed: %s" % out

    # 5. ONE small generation on sd-turbo (<=512x512, few steps)
    ctx = ToolContext(max_generations=1,
                      on_event=lambda *a: print("    job:", a[1:], flush=True))
    out = json.loads(ft.generate_image(
        "a lighthouse at dawn, minimal, flat colors",
        width=512, height=512, steps=4, guidance=1.0,
        model_key="sd-turbo", _context=ctx))
    print("[5] generate_image: %s" % json.dumps(out)[:240])
    assert "artifact" in out, "generation failed: %s" % out
    path = os.path.join(ws, out["artifact"])
    size = os.path.getsize(path)
    print("    artifact on disk: %s (%d bytes)" % (path, size))
    assert size > 0


def main() -> int:
    if os.environ.get("HUGPY_AGENT_LIVE") != "1":
        print("skipped: set HUGPY_AGENT_LIVE=1 to run the live smoke test")
        return 0
    cfg = load_config()
    gw = Gateway.from_config(cfg)
    print("base=%s model=%s key=%s" % (cfg.base, cfg.model,
                                       "set" if cfg.api_key else "NOT SET"))

    # 1. models
    # Since 2026-07-14 (evening) the /v1 family on dev requires a Bearer key
    # (mint in the console under API access; set HUGPY_API_KEY). The /api/ml
    # amenities + /api/models catalog were still open at that time, so the
    # ML section may pass even when [1]-[3] cannot.
    try:
        models = gw.models()
    except Exception as exc:
        if "401" in str(exc):
            print("[1] BLOCKED: /v1 requires an API key (%s). Mint one in "
                  "the console and set HUGPY_API_KEY." % exc)
            if os.environ.get("HUGPY_AGENT_LIVE_ML") == "1":
                ml_section(cfg, gw)
            return 1
        raise
    ids = [m.get("id") or m.get("name") for m in models]
    print("[1] models: %d found (routes: %s)" % (len(ids), gw.resolve()[1]))
    assert ids, "no models returned"
    if cfg.model not in ids:
        print("    WARNING: configured model %r not in list" % cfg.model)

    # 2. small chat
    res = gw.chat([{"role": "user",
                    "content": "Reply with exactly: SMOKE-OK /no_think"}],
                  max_tokens=64)
    print("[2] chat ok=%s text=%r err=%r" % (res.ok, res.text[:80], res.error))
    assert res.ok and res.text.strip(), "chat failed"

    # 3. prompted tool round-trip
    spec = ToolSpec(
        name="get_secret_number",
        description="Returns the secret number. Call it when asked for the secret number.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda: "417")
    ad = Adapter("prompted")
    system = ("You are a tool-using assistant.\n"
              + ad.system_prompt_block([spec])
              + "\nAfter receiving the tool result, reply with just the number.")
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": "What is the secret number? /no_think"}]
    res = gw.chat(msgs, max_tokens=64)
    out = ad.extract(res.text)
    print("[3a] tool call parsed: %s (errors=%s) raw=%r"
          % ([c.name for c in out.calls], out.errors, res.text[:120]))
    assert out.calls and out.calls[0].name == "get_secret_number", \
        "model did not emit the prompted tool call"
    msgs.append({"role": "assistant", "content": res.text})
    msgs.append(ad.tool_response_message("get_secret_number", "417"))
    res2 = gw.chat(msgs, max_tokens=64)
    print("[3b] final: %r" % res2.text[:120])
    assert "417" in res2.text, "model did not use the tool result"

    if os.environ.get("HUGPY_AGENT_LIVE_ML") == "1":
        ml_section(cfg, gw)
    else:
        print("[4-5] ML section skipped (set HUGPY_AGENT_LIVE_ML=1; "
              "spends GPU)")

    print("live smoke: ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
