"""MCP server exposing B's A-facing tools to a Claude Code process.

Design ref: §4 (A sandbox), §6.2 (data plane), §22 Phase 4. This runs as a
subprocess of a headless ``claude`` (A). It is the *only* capability surface A
has: three MCP tools — ``resolve``, ``submit_pull``, ``respond`` — each bound to
one ``(session, turn, epoch)`` and each routed through the same restricted broker
binding used by the in-process adapter. A gets no filesystem, network, or shell.

It is a dependency-free stdio JSON-RPC 2.0 server (newline-delimited). The turn is
identified by environment variables set by :mod:`hugpy_agent.mct.claude_adapter`;
session state is rebuilt from the durable object store (§17.1), not shared memory.
"""
from __future__ import annotations

import json
import os
import sys

from .a_adapter import AAdapterClient
from .protocol import parse_pointer
from .session import BrokerServer, _ABinding


def _log(msg: str) -> None:
    sys.stderr.write(f"[mct-mcp] {msg}\n")
    sys.stderr.flush()


TOOLS = [
    {"name": "resolve",
     "description": "Read an MCT object by its opaque pointer (optionally a bounded "
                    "selector like 'lines 5-9', 'match foo ctx 2', 'symbol name'). "
                    "Use it on the context manifest pointer, the operator_turn pointer, "
                    "and any pull result object.",
     "inputSchema": {"type": "object", "properties": {
         "pointer": {"type": "string"}, "selector": {"type": "string"}},
         "required": ["pointer"]}},
    {"name": "submit_pull",
     "description": "Ask B for missing context. Provide a plain-language 'need' and a "
                    "'target' (usually {\"kind\":\"catalog-query\",\"query\":\"...\"}). "
                    "Optionally 'preferred_form' e.g. 'match ERROR ctx 3'. Returns a "
                    "decision and result object pointers; then resolve those pointers.",
     "inputSchema": {"type": "object", "properties": {
         "need": {"type": "string"},
         "target": {"type": "object"},
         "preferred_form": {"type": "string"},
         "required_fidelity": {"type": "string"}},
         "required": ["need", "target"]}},
    {"name": "respond",
     "description": "Deliver your final answer for this turn. Call exactly once, last.",
     "inputSchema": {"type": "object", "properties": {
         "body": {"type": "string"}, "format": {"type": "string"}},
         "required": ["body"]}},
]


class TurnTools:
    """Rebuilds the per-turn binding from durable state and serves the three tools."""

    def __init__(self):
        self.session_id = os.environ["MCT_SESSION"]
        self.turn_id = os.environ["MCT_TURN"]
        self.epoch = os.environ["MCT_EPOCH"]
        self.manifest_pointer = os.environ["MCT_MANIFEST"]
        self.server = BrokerServer(os.environ["MCT_WORKSPACE"], sink=lambda *_: None)
        self.session = self.server.session(self.session_id)
        self.session.rebuild_catalog_from_store()  # §17.1: state from the store
        _, manifest_oid = parse_pointer(self.manifest_pointer)
        manifest_sha = self.server.ledger.get_object(manifest_oid)["digest"]
        self.binding = _ABinding(self.session, self.turn_id, self.epoch,
                                 self.manifest_pointer, manifest_sha)
        self.client = AAdapterClient(self.binding)

    def resolve(self, args: dict) -> str:
        data = self.binding.resolve(args["pointer"], args.get("selector"), "read")
        return data.decode("utf-8", errors="replace")

    def submit_pull(self, args: dict) -> str:
        out = self.client.submit_pull(
            need=args["need"], target=args["target"],
            preferred_form=args.get("preferred_form"),
            required_fidelity=args.get("required_fidelity", "reduced"))
        preview = ""
        if out.objects:
            try:
                preview = self.binding.resolve(out.objects[0]["object"], None, "read") \
                    .decode("utf-8", errors="replace")[:4000]
            except Exception:
                preview = ""
        return json.dumps({"decision": out.decision, "objects": out.objects,
                           "preview": preview, "denial_reason": out.payload.get("denial_reason")})

    def respond(self, args: dict) -> str:
        # Build the sealed response objects but do NOT render here — the parent B
        # validates and renders exactly once (invariant 14). We only surface the
        # response manifest pointer via an event the parent reads.
        import hashlib
        body_bytes = args["body"].encode("utf-8")
        fmt = args.get("format", "text/markdown")
        body_ptr = self.binding.create_object(body_bytes, fmt, "response_body", None)
        manifest = {
            "schema": "mct.response/1", "session_id": self.session_id,
            "turn_id": self.turn_id, "epoch": self.epoch, "body": body_ptr,
            "format": fmt, "final": True, "proposed_actions": [],
            "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
        }
        manifest_ptr = self.binding.create_object(
            json.dumps(manifest).encode("utf-8"),
            "application/vnd.hugpy.mct-response+json", "response_manifest", None)
        _, manifest_oid = parse_pointer(manifest_ptr)
        self.server.ledger.append_event(self.session_id, self.turn_id, self.epoch,
                                        "a.response_ready", "A.claude",
                                        output_objects=[manifest_oid])
        return "Response recorded. This turn is complete."


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    tools = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid, method = req.get("id"), req.get("method")
        if method == "initialize":
            pv = req.get("params", {}).get("protocolVersion", "2025-06-18")
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": pv, "capabilities": {"tools": {}},
                "serverInfo": {"name": "mct", "version": "1.0"}}})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = req.get("params", {})
            name, args = params.get("name"), params.get("arguments", {})
            try:
                if tools is None:
                    tools = TurnTools()
                text = getattr(tools, name)(args)
                send({"jsonrpc": "2.0", "id": mid,
                      "result": {"content": [{"type": "text", "text": text}]}})
            except Exception as exc:  # tool error -> visible to A, fail closed
                _log(f"tool {name} error: {exc}")
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"ERROR: {type(exc).__name__}: {exc}"}],
                    "isError": True}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
