"""Synthetic, publishable demo content for the README screenshots
(`screenshots.py` runs `stub_serve.py --demo`). Nothing here comes from a real
session: a keeper on a Hugpy fleet model chasing a worker that dropped out of
the fleet, with consecutive calls (a ⚙ chip), a failed call, an operator
permission decision and a call still running. Roles run on three different
providers on purpose — the TUI is provider-neutral."""
from __future__ import annotations

import json
import time

KEEPER = "cs-7f3a91c2d04b4e5f9a1b2c3d4e5f6a7b"
CHAT = "cs-1c2d3e4f5a6b4c7d8e9f0a1b2c3d4e5f"
WORKER = "cs-9e8d7c6b5a4f4e3d2c1b0a9f8e7d6c5b"

ROLES = [
    {"role": "keeper", "label": "Keeper", "live_session_id": KEEPER,
     "backend": "hugpy", "model": "hugpy-fleet:Qwen3-Coder-Next-GGUF", "pending_model": None},
    {"role": "chat", "label": "Chat", "live_session_id": CHAT,
     "backend": "claude", "model": "claude-opus-5", "pending_model": None},
    {"role": "worker", "label": "Worker", "live_session_id": WORKER,
     "backend": "gpt", "model": "gpt-6", "pending_model": None},
    {"role": "local", "label": "Local", "live_session_id": "local", "backend": "b", "model": "", "pending_model": None},
]
_NOW = time.time()
CONSOLE_SESSIONS = [
    {"id": KEEPER, "label": "Keeper", "backend": "hugpy", "model": "hugpy-fleet:Qwen3-Coder-Next-GGUF",
     "busy": True, "paused": False, "updated": _NOW},
    {"id": CHAT, "label": "Chat", "backend": "claude", "model": "claude-opus-5", "busy": False, "updated": _NOW - 60},
    {"id": WORKER, "label": "Worker", "backend": "gpt", "model": "gpt-6", "busy": False, "updated": _NOW - 120},
    {"id": "cs-4b5c6d7e8f9a4b0c1d2e3f4a5b6c7d8e", "label": "release notes", "backend": "claude",
     "updated": _NOW - 900},
    {"id": "cs-2a3b4c5d6e7f4a8b9c0d1e2f3a4b5c6d", "label": "grader sweep", "backend": "hugpy",
     "updated": _NOW - 1800},
    {"id": "cs-6f7a8b9c0d1e4f2a3b4c5d6e7f8a9b0c", "label": "db audit", "backend": "gpt",
     "updated": _NOW - 3600},
]
PROVIDER_OPTIONS = [
    {"backend": "hugpy", "model": "hugpy-fleet:Qwen3-Coder-Next-GGUF", "label": "Hugpy · Qwen3 Coder Next"},
    {"backend": "claude", "model": "claude-opus-5", "label": "Claude · Opus 5"},
    {"backend": "gpt", "model": "gpt-6", "label": "GPT · 6"},
]

TOOL_NAMES = ["Bash", "Read", "Edit", "Grep", "mcp__toolserver__toolserver_call"]


def _events():
    t = time.mktime(time.strptime(time.strftime("%Y-%m-%d") + " 09:31:00", "%Y-%m-%d %H:%M:%S"))
    seq = [0]
    out = []

    def add(dt, **row):
        seq[0] += 1
        out.append(dict(row, seq=seq[0], ts=t + dt, session_id=KEEPER))

    def call(dt, ctx, outp):
        add(dt, type="call", usage={"in": 6, "cr": ctx - 1800, "cw": 1794, "out": outp})

    def tool(dt, tid, name, args, result, dur, error=False):
        add(dt, type="tool", id=tid, name=name, input=json.dumps(args), summary="")
        add(dt + dur, type="tool_result", tool_use_id=tid, is_error=error, text=result)

    add(0, type="user", via="operator",
        text="gpu-box-2 dropped out of the fleet list at 09:14 — find out why and bring it back.")
    add(2, type="system", model="Qwen3-Coder-Next-GGUF", tools=len(TOOL_NAMES), tool_names=TOOL_NAMES)
    call(4, 31200, 96)
    add(5, type="text", text="Checking the worker unit, its log and its last heartbeat in the fleet table.")
    tool(6, "t1", "Bash", {"command": "ssh gpu-box-2 systemctl --user status hugpy-worker --no-pager | head -3"},
         "● hugpy-worker.service - hugpy worker\n   Active: active (running) since 09:02", 0.8)
    tool(7, "t2", "Read", {"file_path": "/home/worker/hugpy_worker/logs/worker.log"},
         "09:14:02 heartbeat → core: connection timed out (5.0 s)\n09:14:07 retrying in 30 s", 0.1)
    tool(8, "t3", "mcp__toolserver__toolserver_call",
         {"name": "db_query", "arguments": {"query": "select worker, last_seen from workers"}},
         '[{"worker": "gpu-box-2", "last_seen": "09:13:58"}]', 0.4)
    call(12, 38900, 240)
    add(13, type="text", text="The unit is healthy but every heartbeat since 09:14 times out on its way to core. "
                              "Checking the route from gpu-box-2 to core:")
    tool(14, "t4", "Bash", {"command": "ssh gpu-box-2 curl -s -m 5 http://core.fleet:7002/healthz"},
         "curl: (28) Connection timed out after 5001 milliseconds", 5.1, error=True)
    call(21, 41700, 310)
    add(22, type="text", text="Core answers from everywhere else, so the box lost its tunnel. Restarting it "
                              "needs your approval.")
    add(23, type="permission", request_id="perm-demo-1", tool="Bash",
        input={"command": "ssh gpu-box-2 sudo systemctl restart wg-quick@fleet",
               "description": "restart the fleet tunnel on gpu-box-2"},
        summary="ssh gpu-box-2 sudo systemctl restart wg-quick@fleet",
        decisions=["allow_once", "allow_session", "deny"])
    add(41, type="permission_resolved", request_id="perm-demo-1", tool="Bash", decision="allow_once",
        by="operator")
    tool(42, "t5", "Bash", {"command": "ssh gpu-box-2 sudo systemctl restart wg-quick@fleet"}, "", 1.6)
    call(46, 43900, 180)
    add(47, type="text", text="Tunnel is back up. Waiting for the next heartbeat to confirm gpu-box-2 "
                              "re-registered:")
    add(48, type="tool", id="t6", name="Bash", summary="",
        input=json.dumps({"command": "ssh gpu-box-2 journalctl --user -u hugpy-worker -f | grep -m1 registered"}))
    return out


EVENTS = _events()


def apply(stub):
    """Swap the stub serve's data for the demo set (module globals)."""
    stub.ROLES = ROLES
    stub.CONSOLE_SESSIONS = CONSOLE_SESSIONS
    stub.PROVIDER_OPTIONS = stub.MODELS = PROVIDER_OPTIONS
    stub.WORKER = KEEPER
    stub.WORKER_EVENTS[:] = EVENTS
    stub.CONTROL["busy"] = True
