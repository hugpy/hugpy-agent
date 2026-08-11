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
                    "decision and result object pointers; then resolve those pointers. "
                    "decision 'candidates' means the query was ambiguous: the result "
                    "object (shown in preview) is a ranked slate of {name, pointer, "
                    "snippet} — choose one and pull it with "
                    "{\"kind\":\"object\",\"object\":<pointer>}. "
                    "DIRECT B LIKE AN AGENT with "
                    "{\"kind\":\"search\",\"spec\":{...}} instead of guessing "
                    "keywords: spec takes all[] (every term must appear), any[] "
                    "(at least one), none[] (drop any file containing these), "
                    "ext[], path_include[]/path_exclude[] globs, "
                    "modified_after/modified_before (ISO dates), limit and "
                    "context_lines. B runs it across the granted roots for free "
                    "and returns only matching lines — far cheaper than pulling "
                    "files to filter them yourself.",
     "inputSchema": {"type": "object", "properties": {
         "need": {"type": "string"},
         "target": {"type": "object"},
         "preferred_form": {"type": "string"},
         "required_fidelity": {"type": "string"}},
         "required": ["need", "target"]}},
    {"name": "submit_act",
     "description": "Have B DO something on your behalf — B is the actor, you are the "
                    "driver. B runs unrestricted on the host and auto-applies the "
                    "result; you get back only a short status plus a 'full_output' "
                    "pointer you can resolve if you actually need the detail. Use this "
                    "to apply fixes, run builds/tests, or drive any tool: it costs no "
                    "tokens beyond the summary. kind='write' {path, content}; "
                    "kind='edit' {path, old, new, all?} (old must be unique unless "
                    "all=true); kind='exec' {command, cwd?, timeout?}.",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["write", "edit", "exec"]},
         "path": {"type": "string"}, "content": {"type": "string"},
         "old": {"type": "string"}, "new": {"type": "string"},
         "all": {"type": "boolean"},
         "command": {"type": "string"}, "cwd": {"type": "string"},
         "timeout": {"type": "integer"}},
         "required": ["kind"]}},
    {"name": "submit_ask",
     "description": "Ask the OPERATOR a clarifying question WITHOUT ending your turn. "
                    "Use it the moment a choice would change your answer — which of "
                    "two things they meant, whether to apply a change, which file is "
                    "the real one. B relays it to the terminal and blocks until they "
                    "reply, then hands you the answer and you carry on with the "
                    "context you already have. Ending the turn to ask costs a full "
                    "context rebuild; this costs a sentence. If nobody answers in "
                    "time you get told so — then proceed on your best assumption and "
                    "say which one you made.",
     "inputSchema": {"type": "object", "properties": {
         "question": {"type": "string"},
         "timeout": {"type": "integer", "description": "seconds to wait (default 300)"}},
         "required": ["question"]}},
    {"name": "respond",
     "description": "Deliver your final answer for this turn. Call exactly once, last.",
     "inputSchema": {"type": "object", "properties": {
         "body": {"type": "string"},
         "format": {"type": "string", "enum": ["text/markdown", "text/plain"]}},
         "required": ["body"]}},
    {"name": "todo",
     "description": "Read or update the operator-shared session to-do board (todo.v1) "
                    "- the durable, UI-visible task list for THIS session, run by the "
                    "canonical vm_mgr todo mechanism (t<n> ids, flock-serialized, "
                    "atomic writes). Use it to accumulate work so nothing is lost "
                    "between turns. Canonical ops: op='list' reads; "
                    "'add' {item:{text, type?, note?, priority?}} appends; "
                    "'update' {id, fields:{text?, note?, status?, type?, priority?, "
                    "comments?, query?}} edits in place; 'del' {id} removes; "
                    "'replace' {items:[...]} rewrites the whole board (existing unique "
                    "ids are preserved). Shorthands also accepted: 'add' {text,...}, "
                    "'set' {id, status}, 'edit' {id, ...}, 'remove' {id}, 'comment' "
                    "{id, text}. status is open|doing|done; type is "
                    "todo|request|bookmark|operator|proposal; priority=low clears the "
                    "field. Use type='operator' for a step only the operator can take "
                    "(a host/root action), and type='proposal' for a decision you want "
                    "from the operator - a proposal may carry pros[], cons[], rec, "
                    "which the console renders as a card with accept/decline; the "
                    "operator's verdict comes back as status=done with an "
                    "'[accepted]'/'[declined]' prefix on the note. The operator sees "
                    "and edits the same board live in the console.",
     "inputSchema": {"type": "object", "properties": {
         "op": {"type": "string", "enum": ["list", "add", "update", "del", "replace",
                                           "set", "edit", "remove", "comment"]},
         "item": {"type": "object"},
         "fields": {"type": "object"},
         "items": {"type": "array", "items": {"type": "object"}},
         "text": {"type": "string"}, "id": {"type": "string"},
         "status": {"type": "string", "enum": ["open", "doing", "done"]},
         "note": {"type": "string"}, "type": {"type": "string"},
         "priority": {"type": "string", "enum": ["low", "medium", "high"]},
         "pros": {"type": "array", "items": {"type": "string"}},
         "cons": {"type": "array", "items": {"type": "string"}},
         "rec": {"type": "string"}},
         "required": ["op"]}},
]


def _todo_apply(workspace, args):
    """Read/mutate the session's todo.v1 board at <workspace>/todo.json — the
    CANONICAL vm_mgr todo mechanism (console-api port): flock on .todo.lock
    (10s) around fresh-read -> todo_apply -> atomic write; sequential t<n> ids
    with o10 collision/ambiguity hardening; id-preserving replace (o15);
    todo_norm limits (text<=500, note<=2000, by<=24, comments<=50x2000);
    unparseable file is an ERROR, never a blank board. Canonical ops:
    add{item} | update{id,fields} | del{id} | replace{items}; the legacy A
    dialect (add{text,..}, set, edit, remove, comment) translates onto them.
    A proposal's pros/cons/rec are attached verbatim after the normed add —
    the keeper-authored extra-field contract the board UI renders as a card
    (update never strips fields it does not whitelist). Module-level so it is
    trivially testable; the file stays the shared operator/agent interface."""
    import time as _t, re as _re, fcntl as _f
    from pathlib import Path as _P
    TYPES = {"todo", "request", "bookmark", "operator", "proposal"}
    STATUS = {"open", "doing", "done"}
    MAX_ITEMS = 500
    ws = _P(workspace)
    p = ws / "todo.json"

    def read():
        if not p.exists():
            return {"schema": "todo.v1", "items": []}, ""
        try:
            data = json.loads(p.read_text("utf-8"))
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise ValueError("no items list")
            return {"schema": "todo.v1", "items": items}, ""
        except (OSError, ValueError):
            return None, f"{p} is not valid todo JSON - fix or remove it"

    def next_id(items):
        n = 0
        for it in items:
            m = _re.match(r"^t(\d+)$", str(it.get("id", "")))
            if m:
                n = max(n, int(m.group(1)))
        return n + 1

    def norm(raw, by, nid):
        if not isinstance(raw, dict):
            return None
        text = str(raw.get("text", "")).strip()[:500]
        if not text:
            return None
        typ = str(raw.get("type", "todo")).strip().lower()
        status = str(raw.get("status", "open")).strip().lower()
        return {"id": f"t{nid}",
                "type": typ if typ in TYPES else "todo",
                "text": text,
                "note": str(raw.get("note", "") or "").strip()[:2000],
                **({"priority": str(raw.get("priority")).strip().lower()}
                   if str(raw.get("priority", "")).strip().lower() in ("medium", "high")
                   else {}),
                "status": status if status in STATUS else "open",
                "by": str(raw.get("by") or by)[:24],
                "ts": int(_t.time())}

    def apply(state, body):
        items = state["items"]
        op = body.get("op")
        if op == "add":
            it = norm(body.get("item") or {}, "A", next_id(items))
            if not it:
                return None, "item needs non-empty text"
            if len(items) >= MAX_ITEMS:
                return None, f"list is full (>{MAX_ITEMS})"
            ids = {str(x.get("id")) for x in items if isinstance(x, dict)}
            while str(it.get("id")) in ids:      # o10: never mint a held id
                m = _re.match(r"^([A-Za-z]+)(\d+)$", str(it.get("id")))
                it["id"] = (m.group(1) + str(int(m.group(2)) + 1)) if m else (str(it.get("id")) + "x")
            items.append(it)
            return state, ""
        if op == "update":
            tid = str(body.get("id", ""))
            twins = [x for x in items if isinstance(x, dict) and str(x.get("id")) == tid]
            if len(twins) > 1:                   # o10 ambiguity: refuse
                return None, f"ambiguous id {tid!r}: {len(twins)} items share it - re-id required (o10)"
            it = next((i for i in items if i.get("id") == body.get("id")), None)
            if not it:
                return None, f"no item {body.get('id')}"
            f = body.get("fields") or {}
            if "text" in f and str(f["text"]).strip():
                it["text"] = str(f["text"]).strip()[:500]
            if "note" in f:
                it["note"] = str(f["note"] or "").strip()[:2000]
            if str(f.get("priority", "")).strip().lower() in ("low", "medium", "high"):
                pr = str(f["priority"]).strip().lower()
                if pr == "low":
                    it.pop("priority", None)
                else:
                    it["priority"] = pr
            if str(f.get("status", "")).lower() in STATUS:
                it["status"] = str(f["status"]).lower()
            if str(f.get("type", "")).lower() in TYPES:
                it["type"] = str(f["type"]).lower()
            if isinstance(f.get("comments"), list):
                clean = []
                for c in f["comments"][:50]:
                    if not isinstance(c, dict):
                        continue
                    ctext = str(c.get("text", "")).strip()[:2000]
                    if not ctext:
                        continue
                    cts = c.get("ts")
                    clean.append({"by": str(c.get("by") or "A")[:24],
                                  "ts": int(cts) if isinstance(cts, (int, float)) else int(_t.time()),
                                  "text": ctext})
                it["comments"] = clean
            if "query" in f:
                q = str(f.get("query") or "").strip()[:500]
                if q:
                    it["query"] = q
                else:
                    it.pop("query", None)
            it["ts"] = int(_t.time())
            return state, ""
        if op == "del":
            twins = [i for i in items if i.get("id") == body.get("id")]
            if len(twins) > 1:                   # o10 ambiguity: refuse
                return None, f"ambiguous id {body.get('id')!r}: {len(twins)} items share it - re-id required (o10)"
            n = len(items)
            state["items"] = [i for i in items if i.get("id") != body.get("id")]
            return (state, "") if len(state["items"]) < n else (None, f"no item {body.get('id')}")
        if op == "replace":
            new = body.get("items")
            if not isinstance(new, list) or len(new) > MAX_ITEMS:
                return None, "replace needs items: [...]"
            out, nid = [], next_id(new)          # keep survivors' ids (o15)
            counts = {}
            for raw in new:
                if isinstance(raw, dict):
                    k = str(raw.get("id", "") or "").strip()
                    if k:
                        counts[k] = counts.get(k, 0) + 1
            for raw in new:
                it = norm(raw, str(raw.get("by") or "A") if isinstance(raw, dict) else "A", 0)
                if not it:
                    continue
                rid = str(raw.get("id", "") or "").strip() if isinstance(raw, dict) else ""
                if rid and counts.get(rid, 0) == 1:
                    it["id"] = rid
                    it["ts"] = int(raw.get("ts") or it["ts"])
                else:
                    it["id"] = f"t{nid}"
                    nid += 1
                seen = {str(x.get("id")) for x in out}
                while str(it.get("id")) in seen:
                    m = _re.match(r"^([A-Za-z]+)(\d+)$", str(it.get("id")))
                    it["id"] = (m.group(1) + str(int(m.group(2)) + 1)) if m else (str(it.get("id")) + "x")
                out.append(it)
            state["items"] = out
            return state, ""
        return None, f"unknown op {op!r}"

    op = (args.get("op") or "list").strip()
    if op == "list":
        state, err = read()
        return json.dumps(state if state is not None else {"error": err})

    ws.mkdir(parents=True, exist_ok=True)
    fh = open(ws / ".todo.lock", "a+")
    try:
        deadline = _t.time() + 10
        while True:
            try:
                _f.flock(fh, _f.LOCK_EX | _f.LOCK_NB)
                break
            except OSError:
                if _t.time() >= deadline:
                    return json.dumps({"error": "todo lock: timed out after 10s"})
                _t.sleep(0.2)
        state, err = read()
        if state is None:
            return json.dumps({"error": err})
        items = state["items"]

        def find(i):
            for it in items:
                if isinstance(it, dict) and it.get("id") == i:
                    return it
            return None

        extras = None
        # legacy A dialect -> canonical ops
        if op == "add" and "item" not in args:
            args = {"op": "add", "item": {
                "type": args.get("type"), "text": args.get("text"),
                "note": args.get("note"), "priority": args.get("priority"),
                "by": "A"},
                "pros": args.get("pros"), "cons": args.get("cons"),
                "rec": args.get("rec")}
        elif op == "set":
            args = {"op": "update", "id": args.get("id"),
                    "fields": {"status": args.get("status")}}
        elif op == "edit":
            f = {k: args[k] for k in ("text", "note", "priority") if k in args}
            if "priority" in args and args.get("priority") not in ("medium", "high"):
                f["priority"] = "low"
            args = {"op": "update", "id": args.get("id"), "fields": f}
        elif op == "remove":
            args = {"op": "del", "id": args.get("id")}
        elif op == "comment":
            ctext = (args.get("text") or "").strip()
            if not ctext:
                return json.dumps({"error": "comment needs text"})
            it = find(args.get("id"))
            if not it:
                return json.dumps({"error": "no such item"})
            comments = [c for c in (it.get("comments") or []) if isinstance(c, dict)]
            comments.append({"by": "A", "ts": int(_t.time()), "text": ctext})
            args = {"op": "update", "id": args.get("id"),
                    "fields": {"comments": comments}}
        if args.get("op") == "add":
            # keeper-authored extra-field contract: pros/cons/rec ride verbatim
            # on a proposal item; norm never sees them, update never strips them
            extras = {k: args.get(k) for k in ("pros", "cons", "rec") if args.get(k)}
        state, err = apply(state, args)
        if state is None:
            return json.dumps({"error": err})
        if extras and state["items"] and (args.get("item") or {}).get("type") == "proposal":
            it = state["items"][-1]
            if isinstance(extras.get("pros"), list):
                it["pros"] = [str(x) for x in extras["pros"]]
            if isinstance(extras.get("cons"), list):
                it["cons"] = [str(x) for x in extras["cons"]]
            if extras.get("rec"):
                it["rec"] = str(extras["rec"])
        try:
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, indent=2) + "\n", "utf-8")
            tmp.replace(p)
        except OSError as e:
            return json.dumps({"error": str(e)})
        return json.dumps(state)
    finally:
        try:
            _f.flock(fh, _f.LOCK_UN)
        except OSError:
            pass
        fh.close()


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
                # bounded read: billed at preview size, not object size
                preview = self.binding.resolve(out.objects[0]["object"], None,
                                               "read", 4000) \
                    .decode("utf-8", errors="replace")
            except Exception:
                preview = ""
        return json.dumps({"decision": out.decision, "objects": out.objects,
                           "preview": preview,
                           "denial_reason": out.payload.get("denial_reason"),
                           "budget": self.binding.budget_state()})

    def submit_act(self, args: dict) -> str:
        kind = args.get("kind") or ""
        kw = {k: v for k, v in args.items() if k != "kind" and v is not None}
        self.session._active_turn = (self.turn_id, self.epoch)  # for the audit line
        return json.dumps(self.session.broker_act(kind, **kw))

    def submit_ask(self, args: dict) -> str:
        self.session._active_turn = (self.turn_id, self.epoch)
        return json.dumps(self.session.broker_ask(
            str(args.get("question") or ""), timeout=args.get("timeout")))

    def respond(self, args: dict) -> str:
        # Build the sealed response objects but do NOT render here — the parent B
        # validates and renders exactly once (invariant 14). We only surface the
        # response manifest pointer via an event the parent reads.
        import hashlib
        body_bytes = args["body"].encode("utf-8")
        # Normalize loose format labels ("text", "markdown", …) to the schema's
        # media types — a mislabeled hint must not void a valid answer.
        fmt = str(args.get("format") or "text/markdown").lower()
        if fmt not in ("text/markdown", "text/plain"):
            fmt = "text/plain" if "plain" in fmt or fmt == "text" else "text/markdown"
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

    def todo(self, args: dict) -> str:
        return _todo_apply(os.environ["MCT_WORKSPACE"], args)


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
