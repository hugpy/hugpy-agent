"""Dependency-free fleet control console. Observations are not reservations."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class FleetError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward fleet credentials to a login page or another origin.
        return None


class Client:
    def __init__(self, base, key="", operator_token="", timeout=30):
        base = base.rstrip("/")
        self.base = base[:-3] if base.endswith("/v1") else base
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
            raise FleetError("base must be an http(s) API root without credentials, query, or fragment")
        self.key, self.operator_token, self.timeout = key, operator_token, timeout

    def request(self, path, method="GET", body=None):
        if not path.startswith("/") or path.startswith("//") or "#" in path or ".." in urllib.parse.unquote(path).split("/"):
            raise FleetError("request path must be relative to the configured API root")
        headers = {"Accept": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        if self.operator_token:
            headers["X-Operator-Token"] = self.operator_token
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=self.timeout) as response:
                result = json.load(response)
            if isinstance(result, dict) and result.get("error"):
                raise FleetError("API reported an error at " + path)
            return result
        except urllib.error.HTTPError as exc:
            raise FleetError("HTTP %s at %s" % (exc.code, path)) from None
        except (OSError, ValueError) as exc:
            raise FleetError("%s at %s" % (type(exc).__name__, path)) from None


def rows(payload, key):
    result = payload if isinstance(payload, list) else payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(result, list) or any(not isinstance(r, dict) for r in result):
        raise FleetError("invalid %s response" % key)
    return result


def snapshot(client):
    paths = {"workers": "/llm/workers", "catalog": "/v1/models",
             "queue": "/llm/queue", "metrics": "/llm/model-metrics2?limit=5000"}
    result = {"schema_version": 1, "observed_at": time.time(), "errors": {}}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {name: pool.submit(client.request, path) for name, path in paths.items()}
        for name, future in futures.items():
            try:
                payload = future.result()
                result[name] = rows(payload, {"workers": "workers", "catalog": "data", "queue": "active", "metrics": "rows"}[name])
            except FleetError as exc:
                result[name] = None
                result["errors"][name] = str(exc)
    return result


def model_keys(worker, field):
    return [k for k in (worker.get(field) or []) if isinstance(k, str)]


def matches(key, candidates):
    # Only use a bare alias when it identifies exactly one candidate. Never
    # merge two owners' different repositories by stripping both owners.
    if key in candidates:
        return True
    if "~" in key:
        return key.split("~", 1)[1] in candidates
    return len([k for k in candidates if k.split("~")[-1] == key]) == 1


def eligible(worker):
    return (worker.get("status") == "online" and not worker.get("unreachable")
            and worker.get("admission") == "approved" and worker.get("serve_mode") != "off")


def resident(worker, model):
    if not matches(model, model_keys(worker, "loaded_models")):
        return False
    return not any(a.get("healthy") is False and matches(model, [a.get("model_key", "")])
                   for a in (worker.get("allocations") or []) if isinstance(a, dict))


def inventory(state):
    workers = state["workers"] or []
    catalog = {r["id"]: r for r in state["catalog"] or [] if isinstance(r.get("id"), str)}
    keys = set(catalog)
    for worker in workers:
        for field in ("loaded_models", "models_local", "models"):
            for key in model_keys(worker, field):
                if not matches(key, catalog):
                    keys.add(key)
    out = []
    for key in sorted(keys):
        meta = catalog.get(key, {})
        denied = bool(meta.get("blocked") or meta.get("adapter") or meta.get("serveable") is False)
        hot = [w.get("id") for w in workers if resident(w, key)]
        available = [w.get("id") for w in workers if resident(w, key) and eligible(w)]
        disk = [w.get("id") for w in workers if matches(key, model_keys(w, "models_local"))]
        out.append({"model": key, "task": meta.get("task"), "tasks": meta.get("tasks", []),
                    "blocked": denied, "hot_workers": hot, "eligible_hot_workers": available,
                    "disk_workers": disk,
                    "state": "blocked" if denied else "hot" if available else "on_disk" if disk else "cold",
                    "callable_now": False if denied else None,
                    "callable_at_max": False if denied else None,
                    "reason": meta.get("unserveable_reason") or "Use inspect for capacity preflight; residency is not a reservation"})
    return out


def inspect_model(client, state, model):
    placement = client.request("/llm/models/%s/placement" % urllib.parse.quote(model, safe=""))
    meta = next((r for r in state["catalog"] or [] if r.get("id") == model), {})
    denied = bool(placement.get("blocked") or meta.get("blocked") or meta.get("adapter") or meta.get("serveable") is False)
    candidates = []
    for p in rows(placement, "workers"):
        worker = next((w for w in state["workers"] or [] if w.get("id") == p.get("id")), {})
        hot = resident(worker, model)
        permitted = eligible(worker) and not denied
        # Old servers coerce an unknown fit to feasible=True. Do not consume
        # that field as a positive admission decision.
        fit = p.get("fits_free_vram")
        max_fit = p.get("fits_total_vram")
        measured = [m for m in state["metrics"] or []
                    if matches(model, [m.get("model_name", "")])
                    and m.get("worker") in (worker.get("id"), worker.get("name"))]
        local = p.get("already_has") is True
        load_field = "hot_load_s" if local else "cold_load_s"
        timings = [m[load_field] for m in measured if isinstance(m.get(load_field), (int, float)) and m[load_field] >= 0]
        load_s = 0.0 if hot else max(timings) if timings else None
        if not permitted:
            readiness = "unavailable"
        elif hot:
            readiness = "ready now" if state["queue"] == [] else "hot / queue unknown" if state["queue"] is None else "hot / queue active"
        elif p.get("disk_ok") is False:
            readiness = "disk blocked"
        elif fit is True:
            readiness = "load into free VRAM" if local else "transfer + load"
        elif max_fit is True:
            readiness = "eviction needed"
        elif max_fit is False:
            readiness = "won't fit GPU"
        else:
            readiness = "capacity unknown"
        pending = state["queue"]
        # The queue is fleet-wide, so even an unrelated request may contend
        # on this GPU. Empty snapshot is only a momentary observation.
        queue_s = 0.0 if pending == [] else None
        eta_s = load_s if queue_s == 0 and readiness in ("ready now", "load into free VRAM") else None
        candidates.append({"worker_id": p.get("id"), "worker": p.get("name"),
                           "eligible": permitted, "hot": hot, "on_disk": local,
                           "readiness": readiness,
                           "fits_current_capacity": fit, "fits_total_capacity": max_fit,
                           "callable_now": False if not permitted else True if hot else False if p.get("disk_ok") is False else fit if local else False,
                           "callable_at_max": False if not permitted else True if hot else False if p.get("disk_ok") is False else max_fit,
                           "load_estimate_s": load_s,
                           "load_estimate_source": "resident heartbeat" if hot else "historical slowest matching load" if timings else "unknown",
                           "queue_wait_s": queue_s, "inference_start_eta_s": eta_s,
                           "reason": p.get("reason"), "max_reason": p.get("max_reason"),
                           "measurements": measured})
    return {"model": model, "observed_at": state["observed_at"], "blocked": denied,
            "candidates": candidates, "placement": placement, "queue": state["queue"],
            "errors": state["errors"],
            "eta_note": "Estimates exclude prompt prefill. An empty queue is a snapshot, not a reservation. When work is queued or eviction is needed, start ETA is unknown."}


ORDER = {name: i for i, name in enumerate(("ready now", "hot / queue active", "hot / queue unknown", "load into free VRAM", "transfer + load", "eviction needed", "capacity unknown", "disk blocked", "won't fit GPU", "unavailable", "blocked"))}


def assessment(client, state, task=None, records=None):
    records = inventory(state) if records is None else records
    if task:
        records = [r for r in records if task in set(r["tasks"]) | {r["task"]}]
    def assess(record):
        row = {**record, "readiness": "blocked" if record["blocked"] else "capacity unknown", "worker": None, "tok_s": None, "eta_s": None}
        if record["blocked"]:
            return row
        try:
            detail = inspect_model(client, state, record["model"])
            candidates = detail["candidates"]
            for candidate in candidates:
                speeds = [m.get("tok_per_s_avg") or m.get("tok_per_s") for m in candidate["measurements"]]
                candidate["tok_s"] = max((v for v in speeds if isinstance(v, (int, float))), default=None)
            candidates.sort(key=lambda c: (ORDER[c["readiness"]], -(c["tok_s"] or 0), str(c["worker"])))
            if candidates:
                best = candidates[0]
                row.update(readiness=best["readiness"], worker=best["worker"], tok_s=best["tok_s"],
                           eta_s=best["inference_start_eta_s"], callable_now=best["callable_now"], callable_at_max=best["callable_at_max"])
            if detail["blocked"]:
                row.update(readiness="blocked", callable_now=False, callable_at_max=False)
        except FleetError as exc:
            row["error"] = str(exc)
        return row
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(assess, records))
    return sorted(result, key=lambda r: (ORDER[r["readiness"]], -(r["tok_s"] or 0), r["model"]))


def display_models(records):
    print("STATE                  WORKER          tok/s    START EST.  MODEL")
    for r in records:
        speed = "?" if r["tok_s"] is None else "%.1f" % r["tok_s"]
        eta = "unknown" if r["eta_s"] is None else "~%.1fs" % r["eta_s"]
        print("%-22s %-14s %7s %12s  %s" % (r["readiness"], r["worker"] or "—", speed, eta, r["model"]))
        if r.get("error"):
            print("  " + r["error"])
    print("tok/s: historical best matching measurement. Capacity is a guideline; routing, locks and concurrent work can change admission.")


def display_workers(workers):
    print("WORKER          STATUS       FREE/TOTAL VRAM GiB     TOK/S AVG   HOT")
    for w in workers or []:
        def gib(n):
            return "?" if n is None else "%.1f" % (n / 2**30)
        ts = w.get("tok_stats") or {}
        speed = ts.get("avg_tok_s")
        print("%-15s %-12s %9s / %-9s %9s %5s" % (
            w.get("name") or w.get("id"), "unreachable" if w.get("unreachable") else w.get("status", "?"),
            gib(w.get("vram_free")), gib(w.get("vram_total")), "?" if speed is None else "%.1f" % speed,
            len(model_keys(w, "loaded_models"))))


def emit(value):
    print(json.dumps(value, indent=2, sort_keys=True))


def parser():
    p = argparse.ArgumentParser(prog="hugpy-agent console")
    p.add_argument("--json", action="store_true", help="machine-readable snapshot")
    sub = p.add_subparsers(dest="command")
    for name in ("status", "workers", "models", "queue", "metrics", "repl", "tui"):
        child = sub.add_parser(name)
        child.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    sub.add_parser("inspect").add_argument("model")
    plan = sub.add_parser("plan", help="read-only model test order: hot, local, cold")
    plan.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    plan.add_argument("--task", default="text-generation")
    req = sub.add_parser("request", help="access any central control endpoint; mutations require an explicit method")
    req.add_argument("path")
    req.add_argument("--method", choices=["GET", "POST", "PUT", "PATCH", "DELETE"], default="GET")
    req.add_argument("--body", help="JSON object, or @path to a JSON file")
    call = sub.add_parser("call", help="one bounded chat call (no automatic retry)")
    call.add_argument("model")
    call.add_argument("prompt")
    call.add_argument("--max-tokens", type=int, default=128)
    call.add_argument("--allow-eviction", action="store_true")
    for action in ("load", "unload", "assign"):
        control = sub.add_parser(action, help="%s a model on a worker (explicit mutation)" % action)
        control.add_argument("worker_id")
        control.add_argument("model")
        if action != "unload":
            control.add_argument("--alloc-mode", choices=["gpu_only", "ram_only", "explicit", "max_gpu", "max_ram"])
    run = sub.add_parser("exec", help="launch a headless OpenAI-compatible program with fleet environment")
    run.add_argument("model")
    run.add_argument("argv", nargs=argparse.REMAINDER)
    return p


def dispatch(client, args):
    cmd = args.command or "status"
    if cmd in ("load", "unload", "assign"):
        body = {"model_key": args.model}
        if getattr(args, "alloc_mode", None):
            body["spill"] = {"alloc_mode": args.alloc_mode}
        emit(client.request("/llm/workers/%s/%s" % (urllib.parse.quote(args.worker_id, safe=""), cmd), "POST", body))
        return 0
    if cmd == "request":
        body = args.body
        if body and body.startswith("@"):
            with open(body[1:], encoding="utf-8") as source:
                body = source.read()
        body = json.loads(body) if body else None
        if body is not None and not isinstance(body, dict):
            raise FleetError("body must be a JSON object")
        if body is not None and args.method == "GET":
            raise FleetError("GET does not accept a body; specify the intended method")
        emit(client.request(args.path, args.method, body))
        return 0
    if cmd == "call":
        if args.max_tokens < 1:
            raise FleetError("max-tokens must be positive")
        started = time.monotonic()
        response = client.request("/v1/chat/completions", "POST", {
            "model": args.model, "messages": [{"role": "user", "content": args.prompt}],
            "max_tokens": args.max_tokens, "max_chunks": 1, "stream": False,
            "no_makeroom": not args.allow_eviction})
        emit({"model": args.model, "elapsed_s": time.monotonic() - started, "response": response})
        return 0
    if cmd == "exec":
        argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        if not argv:
            raise FleetError("exec requires a program after the model")
        catalog = rows(client.request("/v1/models"), "data")
        record = next((r for r in catalog if r.get("id") == args.model), None)
        if not record or record.get("blocked") or record.get("serveable") is False or record.get("adapter"):
            raise FleetError("choose an exact, unblocked, serveable model id from models")
        tasks = set(record.get("tasks") or []) | {record.get("task")}
        if not tasks.intersection({"text-generation", "image-text-to-text"}):
            raise FleetError("headless chat programs require a chat-capable model")
        env = dict(os.environ)
        env.pop("HUGPY_OPERATOR_TOKEN", None)
        env.update(OPENAI_BASE_URL=client.base + "/v1", OPENAI_API_BASE=client.base + "/v1",
                   OPENAI_API_KEY=client.key or "hugpy-open-fleet", OPENAI_MODEL=args.model)
        return subprocess.call(argv, env=env)
    state = snapshot(client)
    if cmd in ("status", "models", "plan"):
        state["models"] = assessment(client, state, getattr(args, "task", None))
        for row in state["models"]:
            if row.get("error"):
                state["errors"]["placement:" + row["model"]] = row["error"]
        if args.json:
            emit(state)
        else:
            if cmd == "status":
                display_workers(state["workers"])
                print()
            display_models(state["models"])
            for name, error in state["errors"].items():
                print("%s: %s" % (name, error), file=sys.stderr)
    elif cmd == "workers" and not args.json:
        display_workers(state["workers"])
        for name, error in state["errors"].items():
            print("%s: %s" % (name, error), file=sys.stderr)
    elif cmd in ("workers", "queue", "metrics"):
        emit({cmd: state[cmd], "errors": state["errors"], "observed_at": state["observed_at"]})
    elif cmd == "inspect":
        emit(inspect_model(client, state, args.model))
    return 1 if state["errors"] else 0


def main(argv=None, cfg=None):
    from .config import load_config
    cfg = cfg or load_config()
    client = Client(cfg.base, cfg.api_key, os.environ.get("HUGPY_OPERATOR_TOKEN", ""),
                    timeout=float(os.environ.get("HUGPY_CONSOLE_TIMEOUT", "120")))
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.command in ("repl", "tui") or (args.command is None and sys.stdin.isatty() and not args.json):
            try:
                from .fleet_tui import run
            except ImportError:
                raise FleetError("Interactive console requires Python's curses module (included on Linux/macOS).") from None
            return run(client)
        return dispatch(client, args)
    except (FleetError, ValueError, OSError) as exc:
        emit({"error": str(exc)})
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
