"""Keyboard-driven fleet control center; network work never owns the terminal."""
from __future__ import annotations

import curses
import json
import queue
import re
import subprocess
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from . import fleet_console as fleet
from . import fleet_benchmark as benchmark


class BenchmarkControl:
    """Thread-safe hierarchical cancellation for concurrent benchmark lanes."""
    def __init__(self):
        self._all = threading.Event()
        self._lock = threading.RLock()
        self._cancelled = {"worker": set(), "model": set(), "quant": set(), "call": set()}

    def is_set(self):
        return self._all.is_set()

    def set(self):
        self._all.set()

    def clear(self):
        self._all.clear()
        with self._lock:
            for values in self._cancelled.values(): values.clear()

    def cancel(self, scope="execution", worker_id=None, model=None, quant=None, call=None):
        if scope == "execution":
            self.set(); return
        value = {"worker": worker_id, "model": model, "quant": quant, "call": call}.get(scope)
        if not value:
            raise ValueError("%s cancellation requires its identifier" % scope)
        # Composite keys keep a same-named quant/call on another lane running.
        key = {"worker": worker_id,
               "model": (worker_id, model),
               "quant": (worker_id, model, quant),
               "call": (worker_id, model, quant, call)}[scope]
        with self._lock:
            self._cancelled[scope].add(key)

    def cancelled(self, worker_id=None, model=None, quant=None, call=None):
        if self.is_set(): return True
        with self._lock:
            return (worker_id in self._cancelled["worker"] or
                    (worker_id, model) in self._cancelled["model"] or
                    (worker_id, model, quant) in self._cancelled["quant"] or
                    (worker_id, model, quant, call) in self._cancelled["call"])
from . import frontends
from .branding import logo_lines

TABS = ("Models", "Workers", "Queue", "Metrics", "Results", "Frontends")
CHAT_TASKS = {"text-generation", "image-text-to-text"}
QUANT_RE = re.compile(r"(?<![A-Za-z0-9])((?:I?Q|F)[2-8](?:_[A-Z0-9]+)?)", re.I)


def clean(value):
    return "".join(c if c.isprintable() else " " for c in str(value))


def number(value, suffix=""):
    if value is None:
        return "?"
    if not isinstance(value, (int, float)):
        return str(value)
    return "%.1f%s" % (value, suffix)


def is_chat(model):
    return not model.get("blocked") and bool(CHAT_TASKS.intersection(set(model.get("tasks") or []) | {model.get("task")}))


def control_request(action, worker, model, mode=None):
    if action not in ("load", "unload", "assign") or not worker.get("id"):
        raise fleet.FleetError("Choose a valid worker and action")
    body = {"model_key": model}
    if mode:
        body["spill"] = {"alloc_mode": mode}
    return "/llm/workers/%s/%s" % (quote(worker["id"], safe=""), action), body


def gib(value):
    return "unknown" if not isinstance(value, (int, float)) else "%.1f GiB" % (value / 2**30)


def quant_label(model):
    for key in ("quant", "quantization", "quant_type"):
        if model.get(key):
            return str(model[key])
    match = QUANT_RE.search(str(model.get("model", "")))
    return match.group(1) if match else "quant unknown"


def _contains_model(value, model):
    if isinstance(value, str):
        return fleet.matches(model, [value])
    if isinstance(value, dict):
        key = value.get("model_key") or value.get("model") or value.get("name")
        return isinstance(key, str) and fleet.matches(model, [key])
    return False


class QueryProgress:
    """Convert observed worker telemetry into a de-duplicated activity feed."""
    def __init__(self, model, report, started_at=None):
        self.model = model
        self.key = model["model"]
        self.quant = quant_label(model)
        self.report = report
        self.started_at = time.time() if started_at is None else started_at
        self.was_hot = (model.get("state") == "hot"
                        or str(model.get("readiness", "")).startswith(("ready", "hot")))
        self.seen = set()

    def emit(self, stage, message, worker=None, details=None):
        signature = (stage, worker, message)
        if signature in self.seen:
            return
        self.seen.add(signature)
        self.report("progress", {"stage": stage, "model": self.key,
                                 "quant": self.quant, "worker": worker,
                                 "message": message, "details": details or {}})

    def observe(self, workers, active):
        queued = next((r for r in active if _contains_model(r, self.key)), None)
        if queued:
            state = queued.get("state") or "queued"
            self.emit("queue", "%s %s — request %s" % (self.key, self.quant, state),
                      details={"request_id": queued.get("request_id")})
        for worker in workers:
            name = worker.get("name") or worker.get("id") or "unknown worker"
            eviction = (worker.get("vram_evictions") or {}).get("last") or {}
            at = eviction.get("at") or (worker.get("vram_evictions") or {}).get("last_at")
            if (isinstance(at, (int, float)) and at >= self.started_at - 1
                    and _contains_model(eviction.get("subject", ""), self.key)):
                victim = eviction.get("victim") or "unknown model"
                self.emit("evicting", "Evicting %s from %s, freeing %s VRAM" %
                          (victim, name, gib(eviction.get("vram_freed"))), name,
                          {"victim": victim, "freed_bytes": eviction.get("vram_freed")})

            provisioning = worker.get("provisioning") or []
            progress = worker.get("provision_progress") or {}
            progress_row = next((v for k, v in progress.items()
                                 if fleet.matches(self.key, [str(k)])), None) if isinstance(progress, dict) else None
            if any(_contains_model(row, self.key) for row in provisioning) or progress_row is not None:
                disk = (worker.get("disk") or {}).get("root") or (worker.get("storage") or {}).get("store_root") or "unknown drive"
                suffix = ""
                if isinstance(progress_row, dict):
                    done = progress_row.get("bytes_done") or progress_row.get("downloaded_bytes")
                    total = progress_row.get("bytes_total") or progress_row.get("total_bytes")
                    if done is not None or total is not None:
                        suffix = " (%s / %s)" % (gib(done), gib(total))
                self.emit("downloading", "Downloading %s %s to %s drive %s%s" %
                          (self.key, self.quant, name, disk, suffix), name,
                          {"drive": disk, "progress": progress_row})

            if any(_contains_model(row, self.key) for row in (worker.get("loading") or [])):
                split = (worker.get("planned_split") or {}).get(self.key) or {}
                self.emit("loading", "Loading %s %s on %s: %s to VRAM & %s to RAM" %
                          (self.key, self.quant, name, gib(split.get("gpu_bytes")),
                           gib(split.get("ram_bytes"))), name, split)

            loaded = any(fleet.matches(self.key, [key])
                         for key in fleet.model_keys(worker, "loaded_models"))
            allocations = [a for a in (worker.get("allocations") or [])
                           if isinstance(a, dict) and _contains_model(a, self.key)]
            healthy = next((a for a in allocations if a.get("healthy") is not False), None)
            preparation_seen = any(stage in {"downloading", "loading"}
                                   for stage, _worker, _message in self.seen)
            if loaded and healthy and (not self.was_hot or preparation_seen):
                self.emit("loaded", "%s %s successfully loaded on %s: %s VRAM & %s RAM" %
                          (self.key, self.quant, name, gib(healthy.get("vram_bytes")),
                           gib(healthy.get("rss_anon_bytes") or healthy.get("ram_bytes"))),
                          name, healthy)
            # `serving` means the endpoint is healthy, including while idle;
            # `busy` is the observed evidence that this call is being answered.
            if any(a.get("busy") for a in allocations):
                self.emit("answering", "%s %s on %s answering..." %
                          (self.key, self.quant, name), name)


def monitor_query(client, model, done, report, interval=0.5):
    tracker = QueryProgress(model, report)
    tracker.emit("locating", "Locating %s %s in the fleet..." %
                 (model["model"], tracker.quant))
    while not done.is_set():
        try:
            workers = fleet.rows(client.request("/llm/workers"), "workers")
            active = fleet.rows(client.request("/llm/queue"), "active")
            tracker.observe(workers, active)
        except fleet.FleetError:
            # The query itself remains authoritative. Telemetry failure is
            # deliberately non-fatal and will be retried on the next poll.
            pass
        done.wait(interval)


def model_specs(model):
    """Small, stable spec summary suitable for a matrix result row."""
    return {key: model.get(key) for key in (
        "model", "task", "tasks", "quant", "quantization", "parameters",
        "parameter_count", "size", "size_bytes", "context_length")
            if model.get(key) is not None}


def _tok_speed(payload, started_at):
    entries = fleet.rows(payload, "entries")
    for entry in entries:
        stamp = entry.get("ts") or entry.get("at") or 0
        if isinstance(stamp, (int, float)) and stamp + 1 < started_at:
            continue
        speed = (entry.get("tok_per_s") or entry.get("tok_s")
                 or entry.get("tokens_per_second"))
        if isinstance(speed, (int, float)):
            return speed, entry
    return None, None


def run_matrix_tests(client, models, workers, prompt, tokens, stop, report):
    """Test every catalog model on every worker, with honest attribution.

    The worker probe is the authoritative per-pair fit/spec check. A bounded
    chat follows only for chat-capable models that fit. Speed is accepted only
    when that worker's own token ledger records a fresh sample; central routing
    to another worker is therefore never mislabeled as this pair's result.
    """
    pairs = [(worker, model) for worker in workers for model in models]
    for index, (worker, model) in enumerate(pairs):
        if stop.is_set():
            break
        worker_id = worker.get("id")
        worker_name = worker.get("name") or worker_id or "unknown worker"
        key = model["model"]
        base = {"matrix": True, "model": key, "worker": worker_name,
                "worker_id": worker_id, "specs": model_specs(model),
                "ok": False, "tok_s": None}
        report("notice", "Matrix %d/%d: %s on %s" %
               (index + 1, len(pairs), key, worker_name))
        if not worker_id or not fleet.eligible(worker):
            report("result", {**base, "error": "worker is not eligible/online"})
            continue
        started = time.time()
        try:
            path = "/llm/workers/%s/probe" % quote(worker_id, safe="")
            probe = client.request(path, "POST", {"model_key": key})
        except fleet.FleetError as exc:
            report("result", {**base, "error": str(exc)})
            continue
        result = {**base, "probe": probe}
        if not isinstance(probe, dict) or not probe.get("ok") or probe.get("fit") is False:
            result["error"] = (probe.get("error") if isinstance(probe, dict) else None) or "worker probe failed"
            report("result", result)
            continue
        if not is_chat(model):
            result.update(ok=True, note="fit passed; speed unavailable for non-chat model")
            report("result", result)
            continue
        try:
            response = client.request("/v1/chat/completions", "POST", {
                "model": key, "messages": [{"role": "user", "content": prompt}],
                "max_tokens": tokens, "max_chunks": 1, "stream": False,
                "no_makeroom": True})
            ledger_path = "/llm/workers/%s/toks?limit=5&model=%s" % (
                quote(worker_id, safe=""), quote(key, safe=""))
            ledger = client.request(ledger_path)
            speed, sample = _tok_speed(ledger, started)
            result.update(response=response, tok_s=speed, speed_sample=sample)
            if speed is None:
                result["error"] = "chat returned, but this worker did not record a fresh speed sample (likely routed elsewhere)"
            else:
                result["ok"] = True
        except fleet.FleetError as exc:
            result["error"] = str(exc)
        report("result", result)


def run_capacity_benchmark(client, workers, tokens, stop, report,
                           model_ids=None, worker_ids=None):
    """Plan once, then run worker/model lanes concurrently and safely."""
    # Central's exclusive lease blocks new normal inference. Drain work admitted
    # before the lease so benchmark timings never share a worker with user work.
    quiet_deadline = time.monotonic() + 900
    while time.monotonic() < quiet_deadline:
        payload = client.request("/llm/queue")
        active = payload if isinstance(payload, list) else payload.get("active", [])
        # The lease rejects new inference. Requests accepted just before it that
        # are still waiting must be cancelled; otherwise stale pending telemetry
        # can deadlock the exclusive run forever. Already-active generation is
        # allowed to finish normally.
        waiting = [row for row in active if row.get("state") == "waiting"]
        for row in waiting:
            request_id = row.get("request_id")
            if request_id:
                try:
                    client.request("/llm/jobs/%s/cancel" % quote(request_id, safe=""),
                                   "POST", {"reason": "exclusive fleet benchmark"})
                    report("notice", "Cancelled pre-benchmark waiting call " + request_id)
                except fleet.FleetError as exc:
                    report("notice", "Could not cancel waiting call %s: %s" % (request_id, exc))
        active = [row for row in active if row.get("state") != "waiting"]
        if not active:
            break
        report("notice", "Exclusive benchmark waiting for %d in-flight call(s) to drain" % len(active))
        if stop.is_set(): return
        time.sleep(2)
    else:
        raise fleet.FleetError("timed out waiting for pre-benchmark calls to drain")
    catalog = benchmark.verbose_catalog(client)
    if model_ids:
        wanted = set(model_ids)
        catalog = [model for model in catalog
                   if benchmark.model_id(model) in wanted]
    if worker_ids:
        wanted_workers = set(worker_ids)
        workers = [worker for worker in workers if
                   worker.get("id") in wanted_workers or
                   worker.get("name") in wanted_workers]
    results = []
    result_lock = threading.Lock()
    jobs = []
    lane_total = 0
    class DiskGate:
        def __init__(self, free):
            self.free = int(free or 0)
            self.cv = threading.Condition()
        def acquire(self, amount, worker=None, plan=None):
            started = time.monotonic()
            with self.cv:
                while amount > self.free and not stop.is_set():
                    self.cv.wait(timeout=5)
                    report("stage", {"stage": "disk_wait", "state": "waiting",
                        "elapsed_s": round(time.monotonic() - started, 1),
                        "worker": (worker or {}).get("name") or (worker or {}).get("id"),
                        "worker_id": (worker or {}).get("id"),
                        "model": (plan or {}).get("model"), "quant": (plan or {}).get("quant"),
                        "required_bytes": amount, "available_bytes": self.free})
                if stop.is_set(): return False
                self.free -= amount
                return True
        def release(self, amount):
            with self.cv:
                self.free += max(0, int(amount or 0)); self.cv.notify_all()
    all_plans = []
    for worker in workers:
        if stop.is_set():
            break
        wname = worker.get("name") or worker.get("id") or "unknown"
        if not fleet.eligible(worker):
            report("notice", "Skipping ineligible worker " + wname)
            continue
        plans = benchmark.plan_worker(catalog, worker)
        all_plans.extend((worker, p) for p in plans)
        infeasible = [p for p in plans if p["mode"] == "infeasible"
                      or not p.get("disk_feasible", True)]
        for plan in infeasible:
            result = {"matrix": True, "ok": False, "worker": wname,
                      "worker_id": worker.get("id"), "model": plan["model"],
                      "quant": plan["quant"], "config": plan["kind"],
                      "alloc_mode": plan["mode"], "tok_s": None,
                      "disk_bytes": plan["disk_bytes"],
                      "runtime_bytes": plan["runtime_bytes"],
                      "error": plan.get("reason") or "insufficient worker disk"}
            results.append(result); report("result", result)
        feasible = [p for p in plans if p["mode"] != "infeasible" and p.get("disk_feasible", True)]
        # One job per model: its quant/config mutations stay serial, while
        # different models use independent worker slots concurrently.
        by_model = {}
        for plan in feasible:
            by_model.setdefault(plan["model"], []).append(plan)
        lanes = ((worker.get("config") or {}).get("slot_count") or
                 (worker.get("serving_limits") or {}).get("max_concurrency") or
                 len(worker.get("slots") or []) or 1)
        lanes = max(1, min(int(lanes), 8))
        lane_total += lanes
        semaphore = threading.Semaphore(lanes)
        disk_gate = DiskGate((worker.get("disk") or {}).get("free_bytes"))
        for model_plans in by_model.values():
            jobs.append((worker, model_plans, semaphore, disk_gate))

    feasible_total = sum(1 for _w, p in all_plans
                         if p["mode"] != "infeasible" and p.get("disk_feasible", True))
    report("plan", {"total": len(all_plans), "runnable": feasible_total,
                    "workers": len({w.get("id") for w, _p in all_plans}),
                    "rows": [{"status": "N/A", "grade": "N/A", "tok_s": "N/A",
                              "elapsed_s": "N/A", "worker": w.get("name") or w.get("id"),
                              "worker_id": w.get("id"), "model": p["model"],
                              "quant": p["quant"], "config": p["kind"],
                              "alloc_mode": p["mode"]} for w, p in all_plans]})
    completed = 0
    completed_lock = threading.Lock()

    def run_model(worker, model_plans, semaphore, disk_gate):
        nonlocal completed
        wname = worker.get("name") or worker["id"]
        with semaphore:
            # Quant groups are serial for one model because its per-worker pin
            # and runtime levers are shared. Different models and workers run in
            # parallel up to their advertised lane counts.
            groups = []
            for plan in model_plans:
                if not groups or groups[-1][0] != plan["quant"]:
                    groups.append((plan["quant"], []))
                groups[-1][1].append(plan)
            for _quant, configs in groups:
                if stop.is_set() or (hasattr(stop, "cancelled") and stop.cancelled(
                        worker_id=worker.get("id"), model=configs[0]["model"], quant=_quant)):
                    break
                reserved = 0
                staged = bool(configs[0]["hot"])
                if not configs[0]["hot"]:
                    reserved = configs[0]["disk_bytes"]
                    if not disk_gate.acquire(reserved, worker, configs[0]):
                        break
                for config_index, plan in enumerate(configs):
                    if stop.is_set() or (hasattr(stop, "cancelled") and stop.cancelled(
                            worker_id=worker.get("id"), model=plan["model"], quant=plan["quant"])): break
                    report("notice", "Configuring %s/%s %s on %s" %
                           (plan["model"], plan["quant"], plan["mode"], wname))
                    config_started = time.monotonic()
                    try:
                        # Runtime knobs (full/4-bit/MoE and their explicit
                        # split) are fixed when a model is seated.  Preserve
                        # the hot worker copy, but unseat the preceding config
                        # so /load must instantiate this exact next layout.
                        if config_index:
                            client.request("/llm/workers/%s/evict" %
                                           quote(worker["id"], safe=""), "POST",
                                           {"model_key": plan["model"]})
                        benchmark.apply_config(client, worker, plan)
                        benchmark.start_cold_transfer(client, worker, plan, report=report)
                        benchmark.wait_until_ready(client, worker, plan,
                                                   stop=stop, report=report)
                        staged = True
                        ready_s = round(time.monotonic() - config_started, 4)
                        result = benchmark.grade_config(client, worker, plan, tokens, stop, report)
                        wall = round(time.monotonic() - config_started, 4)
                        result["cold_s"] = "N/A" if plan["hot"] else ready_s
                        result["config_s"] = wall
                    except fleet.FleetError as exc:
                        result = {"matrix": True, "ok": False, "worker": wname,
                                  "worker_id": worker.get("id"), "model": plan["model"],
                                  "quant": plan["quant"], "config": plan["kind"],
                                  "alloc_mode": plan["mode"], "tok_s": None, "error": str(exc)}
                    with result_lock: results.append(result)
                    report("result", result)
                    with completed_lock:
                        completed += 1
                        report("progress", {"completed": completed, "total": feasible_total,
                                            "percent": round(100 * completed / max(1, feasible_total), 2),
                                            "worker_id": worker.get("id"), "model": plan["model"],
                                            "quant": plan["quant"], "config": plan["kind"]})
                wid = quote(worker["id"], safe="")
                try:
                    client.request("/llm/workers/%s/evict" % wid, "POST", {"model_key": configs[0]["model"]})
                    cleaned = client.request("/llm/workers/%s/cache-evict" % wid, "POST", {"model_key": configs[0]["model"]})
                    freed = int(cleaned.get("freed_bytes") or 0)
                    # A cold download consumed its reservation; cleanup returns
                    # the real bytes. A pre-existing hot quant had no reservation,
                    # so its deletion grows the capacity available to cold lanes.
                    disk_gate.release(freed)
                    if reserved and not staged and freed < reserved:
                        disk_gate.release(reserved - freed)
                except fleet.FleetError as exc:
                    if reserved:
                        disk_gate.release(reserved)
                    report("notice", "Worker cache cleanup refused: " + str(exc))

    with ThreadPoolExecutor(max_workers=max(1, lane_total)) as pool:
        futures = [pool.submit(run_model, *job) for job in jobs]
        for future in futures:
            try: future.result()
            except Exception as exc:
                report("notice", "Benchmark lane failed: " + str(exc))
    def aggregate(keys):
        grouped = {}
        for row in results:
            key = tuple(row.get(k) for k in keys)
            bucket = grouped.setdefault(key, {k: row.get(k) for k in keys})
            bucket.setdefault("configurations", 0); bucket["configurations"] += 1
            bucket.setdefault("elapsed_s", 0.0); bucket["elapsed_s"] += float(row.get("total_latency_s") or 0)
            bucket.setdefault("score", 0); bucket["score"] += int(row.get("score") or 0)
            bucket.setdefault("max", 0); bucket["max"] += int(row.get("max") or 0)
        return list(grouped.values())
    report("summary", {"workers": aggregate(("worker_id", "worker")),
                       "models": aggregate(("worker_id", "worker", "model")),
                       "quants": aggregate(("worker_id", "worker", "model", "quant")),
                       "execution": {"elapsed_s": sum(float(r.get("total_latency_s") or 0) for r in results),
                                     "configurations": len(results)}})
    path = benchmark.write_report(results)
    report("notice", "Capacity benchmark complete: " + path)


def run_tests(client, models, prompt, tokens, allow_eviction, stop, report,
              progress_client=None):
    """No retry after timeout; retain each result and stop between calls."""
    for index, model in enumerate(models):
        if stop.is_set():
            break
        report("notice", "Testing %s (%d/%d)" % (model["model"], index + 1, len(models)))
        started = time.monotonic()
        monitor_done = threading.Event()
        monitor = None
        if progress_client is not None:
            monitor = threading.Thread(target=monitor_query,
                                       args=(progress_client, model, monitor_done, report),
                                       daemon=True)
            monitor.start()
        try:
            response = client.request("/v1/chat/completions", "POST", {
                "model": model["model"], "messages": [{"role": "user", "content": prompt}],
                "max_tokens": tokens, "max_chunks": 1, "stream": False,
                "no_makeroom": not allow_eviction})
            result = {"model": model["model"], "ok": True, "response": response}
        except fleet.FleetError as exc:
            result = {"model": model["model"], "ok": False, "error": str(exc),
                      "note": "No retry. A timed-out call may still be running; inspect Queue."}
        finally:
            monitor_done.set()
            if monitor is not None:
                monitor.join(timeout=1)
        result["elapsed_s"] = time.monotonic() - started
        report("result", result)
        if not result["ok"]:
            # Avoid overlapping uncertain work after transport failure.
            break


class Console:
    def __init__(self, screen, client):
        self.screen, self.client = screen, client
        self.read_client = fleet.Client(client.base, client.key, client.operator_token, min(client.timeout, 8))
        self.events = queue.Queue()
        self.state = {"workers": [], "catalog": [], "metrics": [], "queue": None, "errors": {}, "observed_at": time.time()}
        self.models, self.results = [], []
        self.frontends = frontends.available()
        self.tab, self.selected, self.search = 0, 0, ""
        self.worker_filter = None
        self.notice = "Connecting to fleet..."
        self.refreshing = self.busy = False
        self.last_refresh = 0
        # toolserver health (shared client; probed on each refresh, off the UI thread)
        self.toolserver = None
        self.closed, self.stop_tests = threading.Event(), BenchmarkControl()

    def send(self, kind, value):
        if not self.closed.is_set():
            self.events.put((kind, value))

    def start_refresh(self):
        if self.refreshing:
            return
        self.refreshing = True
        self.last_refresh = time.monotonic()
        def toolserver_work():
            try:
                from . import toolserver_client
                if toolserver_client.enabled():
                    self.send("toolserver", toolserver_client.default_client().status())
                else:
                    self.send("toolserver", {"ok": False, "auth": "disabled", "tool_count": 0})
            except Exception as exc:  # noqa: BLE001 — a status probe never takes the UI down
                self.send("toolserver", {"ok": False, "auth": "unknown", "tool_count": 0,
                                         "error": type(exc).__name__})
        threading.Thread(target=toolserver_work, daemon=True).start()
        def work():
            try:
                state = fleet.snapshot(self.read_client)
                self.send("snapshot", state)
                records = fleet.inventory(state)
                # Prioritize resident/local models, then assess in bounded batches.
                records.sort(key=lambda r: ({"hot": 0, "on_disk": 1, "cold": 2, "blocked": 3}[r["state"]], r["model"]))
                with ThreadPoolExecutor(max_workers=4) as pool:
                    for start in range(0, len(records), 4):
                        if self.closed.is_set():
                            break
                        def assess(record):
                            return fleet.assessment(self.read_client, state, records=[record])
                        for result in pool.map(assess, records[start:start + 4]):
                            self.send("models", result)
            except Exception as exc:
                self.send("notice", "Refresh failed: " + type(exc).__name__)
            finally:
                self.send("refreshed", None)
        threading.Thread(target=work, daemon=True).start()

    def drain(self):
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "snapshot":
                previous = self.items()
                chosen = previous[self.selected] if 0 <= self.selected < len(previous) else None
                self.state = value
                self.models = [{**r, "readiness": "checking capacity", "worker": None, "tok_s": None, "eta_s": None} for r in fleet.inventory(value)]
                self.models.sort(key=lambda r: ({"hot": 0, "on_disk": 1, "cold": 2, "blocked": 3}[r["state"]], r["model"]))
                identity = ("model", "id", "request_id", "model_name", "action", "id")[self.tab]
                if chosen and self.tab < 4:
                    self.selected = next((i for i, r in enumerate(self.items()) if r.get(identity) == chosen.get(identity)), 0)
            elif kind == "models":
                # Keep positions stable while the operator is navigating.
                updates = {r["model"]: r for r in value}
                self.models = [updates.get(r["model"], r) for r in self.models]
            elif kind == "toolserver":
                self.toolserver = value
            elif kind == "refreshed":
                self.refreshing = False
                if not self.busy:
                    self.notice = "Fleet updated. Enter opens controls for the selected row."
            elif kind == "notice":
                self.notice = value
            elif kind == "stage":
                detail = value if isinstance(value, dict) else {"stage": "work", "state": str(value)}
                self.results.insert(0, {"activity": True, **detail,
                    "message": "%s · %ss / %ss" % (
                        detail.get("state") or detail.get("stage"),
                        detail.get("elapsed_s", 0), detail.get("timeout_s", "?") )})
                self.notice = self.results[0]["message"]
            elif kind == "progress":
                self.results.insert(0, {"activity": True, **value})
                self.notice = value.get("message") or ("Benchmark %s/%s (%s%%)" % (
                    value.get("completed", 0), value.get("total", 0),
                    value.get("percent", 0)))
            elif kind == "plan":
                self.results.insert(0, {"activity": True, "stage": "plan",
                    "message": "%s runnable configurations; metrics initialized N/A" % value.get("runnable", 0),
                    "details": value})
            elif kind == "call":
                self.results.insert(0, {"activity": True, "stage": "call",
                    "message": "%s/%s %s %s" % (value.get("model"), value.get("quant"),
                                                   value.get("task"), value.get("grade")),
                    "details": value})
            elif kind == "result":
                self.results.insert(0, value)
                self.notice = ("Completed: " if value.get("ok") else "Failed: ") + value.get("model", value.get("action", "operation"))
            elif kind == "done":
                self.busy = False
                self.start_refresh()

    def items(self):
        values = (self.models, self.state["workers"] or [], self.state["queue"] or [], self.state["metrics"] or [], self.results, self.frontends)[self.tab]
        if self.tab == 0 and self.worker_filter:
            worker = next((w for w in self.state["workers"] or [] if w.get("id") == self.worker_filter), {})
            keys = set().union(*(fleet.model_keys(worker, f) for f in ("models", "models_local", "loaded_models")))
            values = [r for r in values if fleet.matches(r["model"], keys)]
        return [r for r in values if self.search.lower() in json.dumps(r).lower()]

    def toolserver_line(self):
        """Status-bar fragment: 'toolserver ok (198 tools)' / 'toolserver auth
        rejected' / 'toolserver unreachable' / 'toolserver: probing'."""
        if self.toolserver is None:
            return "toolserver: probing"
        if self.toolserver.get("auth") == "disabled":
            return "toolserver off"
        from .toolserver_client import status_line
        return status_line(self.toolserver)

    def put(self, y, x, value, attr=0):
        height, width = self.screen.getmaxyx()
        if 0 <= y < height and 0 <= x < width - 1:
            try:
                self.screen.addnstr(y, x, clean(value), width - x - 1, attr)
            except curses.error:
                pass

    def row(self, r):
        if self.tab == 0:
            return "%-22s %-12s %7s %8s  %s" % (r["readiness"], r.get("worker") or "?", number(r.get("tok_s")), number(r.get("eta_s"), "s"), r["model"])
        if self.tab == 1:
            free, total = r.get("vram_free"), r.get("vram_total")
            return "%-18s %-12s VRAM %s / %s GiB   %s tok/s   %d hot" % (r.get("name") or r.get("id"), "unreachable" if r.get("unreachable") else r.get("status", "?"), number(free / 2**30 if free is not None else None), number(total / 2**30 if total is not None else None), number((r.get("tok_stats") or {}).get("avg_tok_s")), len(fleet.model_keys(r, "loaded_models")))
        if self.tab == 2:
            return "%s  %s  %s tokens  %ss elapsed" % (r.get("state"), r.get("model_key") or r.get("model"), r.get("tokens", "?"), r.get("elapsed", "?"))
        if self.tab == 3:
            return "%s  @ %s  %s tok/s  %s / %s" % (r.get("model_name"), r.get("worker"), number(r.get("tok_per_s_avg") or r.get("tok_per_s")), r.get("quant"), r.get("alloc_mode"))
        if self.tab == 5:
            return "%-16s %-16s %s" % (r["name"], "installed" if r["path"] else "not installed", r["protocol"])
        if r.get("activity"):
            return "%-12s %s" % (r.get("stage", "progress").upper(), r.get("message", ""))
        if r.get("matrix"):
            return "%s  %-18s @ %-14s %s tok/s" % (
                "PASS" if r.get("ok") else "FAIL", r.get("model", "?"),
                r.get("worker", "?"), number(r.get("tok_s")))
        return "%s  %s  %s" % ("OK" if r.get("ok") else "FAILED", r.get("model") or r.get("action"), number(r.get("elapsed_s"), "s"))

    def draw(self):
        self.screen.erase()
        h, w = self.screen.getmaxyx()
        # Keep the branded home treatment visible while the first fleet
        # snapshot is arriving. It disappears naturally when real state lands,
        # with no timer and no swallowed keystroke.
        if (self.refreshing and not self.state.get("workers")
                and not self.state.get("catalog") and not self.results):
            lines = logo_lines()
            top = max(1, (h - len(lines) - 5) // 2)
            for offset, line in enumerate(lines):
                self.put(top + offset, max(0, (w - len(line)) // 2), line,
                         curses.A_BOLD)
            title = "HUGPY AGENT"
            subtitle = "Your models. Your workers. One fleet."
            self.put(top + len(lines) + 1, max(0, (w - len(title)) // 2),
                     title, curses.A_BOLD)
            self.put(top + len(lines) + 2,
                     max(0, (w - len(subtitle)) // 2), subtitle)
            status = "Connecting to " + self.client.base
            self.put(top + len(lines) + 4,
                     max(0, (w - len(status)) // 2), status)
            self.put(h - 2, 0, "Q quit", curses.A_BOLD)
            self.screen.refresh()
            return
        self.put(0, 0, "HUGPY FLEET   " + self.client.base, curses.A_BOLD)
        self.put(1, 0, "   ".join(("[%d %s]" if i == self.tab else "%d %s") % (i + 1, t) for i, t in enumerate(TABS)), curses.A_BOLD)
        age = max(0, int(time.time() - self.state["observed_at"]))
        self.put(2, 0, "%ds old | %s | filter: %s%s | %s" % (age, "refreshing" if self.refreshing else "live", self.search or "all", " | selected worker" if self.worker_filter else "", self.toolserver_line()))
        headers = ("READINESS              WORKER         tok/s    WAIT    MODEL", "WORKER / RESOURCES", "IN-FLIGHT REQUESTS", "RECORDED MODEL / WORKER PERFORMANCE", "SESSION RESULTS — Enter to read output", "AGENT FRONTEND   INSTALLATION     FLEET CONNECTION — Enter to choose model")
        self.put(4, 0, headers[self.tab], curses.A_BOLD)
        items = self.items()
        self.selected = max(0, min(self.selected, len(items) - 1))
        count = max(1, h - 10)
        start = (self.selected // count) * count
        for i, row in enumerate(items[start:start + count], start):
            self.put(5 + i - start, 0, self.row(row), curses.A_REVERSE if i == self.selected else 0)
        if not items:
            self.put(5, 0, "No rows yet." if self.refreshing else "No matching rows. Press R to refresh or / to change search.")
        errors = self.state.get("errors") or {}
        self.put(h - 4, 0, "Data unavailable: " + "; ".join(errors) if errors else "Wait is preparation estimate; ? means unknown. Tok/s is historical. Capacity is a guideline.")
        self.put(h - 3, 0, self.notice)
        self.put(h - 2, 0, "Up/Down | Enter controls | Tab views | / search | R refresh | Q quit", curses.A_BOLD)
        self.put(h - 1, 0, ("operation running | " if self.busy else "") + "T test list | M matrix test | S stop tests | C clear filters")
        self.screen.refresh()

    def choose(self, title, options):
        selected = 0
        while not self.closed.is_set():
            self.drain()
            self.screen.erase()
            h, _ = self.screen.getmaxyx()
            self.put(0, 0, title, curses.A_BOLD)
            count = max(1, h - 5)
            start = selected // count * count
            for i, label in enumerate(options[start:start + count], start):
                self.put(2 + i - start, 2, label, curses.A_REVERSE if i == selected else 0)
            self.put(h - 2, 0, "Up/Down select | Enter choose | Esc back")
            self.screen.refresh()
            key = self.screen.getch()
            if key in (27, ord("q")):
                return None
            if key in (curses.KEY_UP, ord("k")):
                selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = min(len(options) - 1, selected + 1)
            elif key in (10, 13, curses.KEY_ENTER) and options:
                return selected

    def prompt(self, title, default=""):
        value = default
        while True:
            self.screen.erase()
            self.put(0, 0, title, curses.A_BOLD)
            self.put(2, 0, value + "_")
            self.put(4, 0, "Type text | Backspace edit | Ctrl-U clear | Enter accept | Esc cancel")
            self.screen.refresh()
            key = self.screen.getch()
            if key == 27:
                return None
            if key in (10, 13):
                return value
            if key in (curses.KEY_BACKSPACE, 127, 8):
                value = value[:-1]
            elif key == 21:
                value = ""
            elif 32 <= key <= 126:
                value += chr(key)

    def view(self, title, value):
        offset = 0
        while True:
            self.drain()
            self.screen.erase()
            h, w = self.screen.getmaxyx()
            raw = describe(value)
            lines = [part for line in raw.splitlines() for part in (textwrap.wrap(line, max(10, w - 2), replace_whitespace=False) or [""])]
            self.put(0, 0, title, curses.A_BOLD)
            for i, line in enumerate(lines[offset:offset + max(1, h - 4)]):
                self.put(i + 2, 0, line)
            self.put(h - 1, 0, "Up/Down scroll | Esc/Enter back")
            self.screen.refresh()
            key = self.screen.getch()
            if key in (27, 10, 13, ord("q")):
                return
            if key in (curses.KEY_DOWN, curses.KEY_NPAGE):
                offset = min(max(0, len(lines) - 1), offset + (max(1, h - 4) if key == curses.KEY_NPAGE else 1))
            elif key in (curses.KEY_UP, curses.KEY_PPAGE):
                offset = max(0, offset - (max(1, h - 4) if key == curses.KEY_PPAGE else 1))

    def operation(self, title, fn):
        if self.busy:
            self.notice = "An operation is running. Check Results or stop the test batch first."
            return
        self.busy = True
        self.notice = title + "..."
        def work():
            try:
                value = fn()
                self.send("result", {"action": title, "ok": True, "response": value})
            except Exception as exc:
                self.send("result", {"action": title, "ok": False, "error": str(exc), "note": "Inspect state before retrying; a timed-out request may still be running."})
            finally:
                self.send("done", None)
        threading.Thread(target=work, daemon=True).start()

    def control(self, action, model, worker=None):
        if self.busy:
            self.notice = "Wait for the current operation to finish."
            return
        workers = self.state["workers"] or []
        if worker is None:
            choice = self.choose("%s %s — choose worker" % (action.title(), model), [self.worker_label(w) for w in workers])
            if choice is None:
                return
            worker = workers[choice]
        mode = None
        if action != "unload":
            choices = ["Keep existing allocation policy", "GPU only", "RAM only"]
            pick = self.choose("Allocation policy", choices)
            if pick is None:
                return
            mode = (None, "gpu_only", "ram_only")[pick]
        title = "%s %s on %s" % (action.title(), model, worker.get("name") or worker["id"])
        if self.choose(title, ["Back", "Confirm " + action]) != 1:
            return
        path, body = control_request(action, worker, model, mode)
        self.operation(title, lambda: self.client.request(path, "POST", body))

    def worker_label(self, w):
        free = w.get("vram_free")
        return "%s | %s | %s GiB free | %d hot" % (w.get("name") or w.get("id"), w.get("status", "?"), number(free / 2**30 if free is not None else None), len(fleet.model_keys(w, "loaded_models")))

    def test(self, models):
        models = [r for r in models if is_chat(r)]
        if not models or self.busy:
            self.notice = "No chat models selected, or an operation is already running."
            return
        models.sort(key=lambda r: (fleet.ORDER.get(r.get("readiness"), 99), r["model"]))
        prompt = self.prompt("Test prompt", "Reply with one short sentence introducing yourself.")
        if not prompt:
            return
        limit = self.choose("Response length", ["64 tokens — quick smoke test", "128 tokens", "512 tokens"])
        if limit is None:
            return
        policy = self.choose("Test %d models, one at a time, hot models first" % len(models), ["Back", "Start — preserve loaded models", "Start — allow evictions"])
        if policy not in (1, 2):
            return
        self.busy = True
        self.stop_tests.clear()
        def work():
            try:
                run_tests(self.client, models, prompt, (64, 128, 512)[limit],
                          policy == 2, self.stop_tests, self.send,
                          progress_client=self.read_client)
            finally:
                self.send("done", None)
        threading.Thread(target=work, daemon=True).start()
        self.tab, self.selected, self.search = 4, 0, ""

    def matrix_test(self):
        if self.busy:
            self.notice = "Wait for the current operation to finish."
            return
        workers = list(self.state.get("workers") or [])
        if not workers:
            self.notice = "Refresh first; capacity test mode needs workers."
            return
        limit = self.choose("Response length", ["64 tokens — quick", "128 tokens", "512 tokens"])
        if limit is None:
            return
        confirm = self.choose("Capacity-plan and grade all central models on %d workers" % len(workers), [
            "Back",
            "Start — stage from central, test all configs, rotate worker cache",
        ])
        if confirm != 1:
            return
        self.busy = True
        self.stop_tests.clear()
        def work():
            lease = None
            heartbeat_stop = threading.Event()
            try:
                lease = self.client.request("/llm/benchmark/lock", "POST", {"action": "acquire"})
                token = lease.get("lease_token")
                if not token:
                    raise fleet.FleetError("benchmark lease returned no token")
                def publish(kind, value):
                    self.send(kind, value)
                    self.client.request("/llm/benchmark/lock", "POST", {
                        "action": "event", "lease_token": token,
                        "kind": kind, "value": value})
                def keepalive():
                    while not heartbeat_stop.wait(10):
                        try:
                            self.client.request("/llm/benchmark/lock", "POST", {
                                "action": "heartbeat", "lease_token": token})
                        except fleet.FleetError as exc:
                            self.send("notice", "Benchmark heartbeat failed: " + str(exc))
                threading.Thread(target=keepalive, name="benchmark-heartbeat",
                                 daemon=True).start()
                run_capacity_benchmark(self.client, workers,
                                       (64, 128, 512)[limit],
                                       self.stop_tests, publish)
            except fleet.FleetError as exc:
                self.send("notice", "Benchmark could not start: " + str(exc))
            finally:
                heartbeat_stop.set()
                if lease and lease.get("lease_token"):
                    try:
                        self.client.request("/llm/benchmark/lock", "POST", {
                            "action": "release", "lease_token": lease["lease_token"],
                            "status": "cancelled" if self.stop_tests.is_set() else "complete"})
                    except fleet.FleetError as exc:
                        self.send("notice", "Benchmark lease release failed: " + str(exc))
                self.send("done", None)
        threading.Thread(target=work, daemon=True).start()
        self.tab, self.selected, self.search = 4, 0, ""

    def launch(self, model=None, frontend=None):
        if self.busy:
            self.notice = "Wait for the current operation to finish before opening a frontend."
            return
        self.frontends = frontends.available()
        if frontend is None:
            choice = self.choose("Open with fleet model: " + model, [self.frontend_label(f) for f in self.frontends])
            if choice is None:
                return
            frontend = self.frontends[choice]
        if not frontends.resolve(frontend):
            action = self.choose(frontend["name"] + " — not installed", [
                "Back",
                "Install now — " + frontend.get("install_text", "official installer"),
                "Show installation guide",
            ])
            if action == 2:
                self.view(frontend["name"] + " installation", frontend["docs"])
                return
            if action != 1:
                return
            confirm = self.choose("Install " + frontend["name"] + "?", [
                "Cancel",
                "Run the listed third-party installer as this user",
            ])
            if confirm != 1:
                return
            curses.def_prog_mode()
            curses.endwin()
            try:
                result = frontends.install(frontend)
            except fleet.FleetError as exc:
                result = None
                self.notice = str(exc)
            finally:
                curses.reset_prog_mode()
                self.screen.refresh()
            self.frontends = frontends.available()
            installed = next((f for f in self.frontends
                              if f["id"] == frontend["id"]), frontend)
            if result != 0 or not installed.get("path"):
                if result is not None:
                    self.notice = "%s install failed or its binary is not yet visible (exit %s)." % (frontend["name"], result)
                return
            frontend = installed
            self.notice = frontend["name"] + " installed. Launching it now."
        if model is None:
            models = [r for r in self.models if is_chat(r)]
            if not models:
                self.notice = "No chat-capable fleet models available. Refresh Models first."
                return
            choice = self.choose("Choose fleet model for " + frontend["name"], ["%s | %s | %s tok/s" % (r["model"], r["readiness"], number(r.get("tok_s"))) for r in models])
            if choice is None:
                return
            model = models[choice]["model"]
        try:
            argv, env = frontends.prepare(frontend, self.client, model)
            profile = frontends.configure(frontend, env, model)
        except (fleet.FleetError, OSError) as exc:
            self.view("Cannot launch", str(exc))
            return
        curses.def_prog_mode()
        curses.endwin()
        try:
            from . import session_signals
            with session_signals.harness_lease(self.client.base, self.client.key, env,
                                               frontend["id"]):
                result = subprocess.call(argv, env=env)
            self.results.insert(0, {"action": frontend["name"], "model": model, "ok": result == 0,
                                    "exit_code": result, "profile": profile})
            self.notice = "%s exited (%s). Back in fleet console." % (frontend["name"], result)
        except OSError as exc:
            self.notice = "Launch failed: " + str(exc)
        finally:
            curses.reset_prog_mode()
            self.screen.refresh()

    def frontend_label(self, spec):
        return "%s — %s" % (spec["name"], "installed" if spec["path"] else "not installed")

    def open_row(self, row):
        if self.tab == 0:
            actions = ["Inspect capacity and placement", "Load on worker", "Unload from worker", "Assign to worker"]
            if is_chat(row):
                actions += ["Test this model", "Open agent frontend"]
            pick = self.choose(row["model"] + " | " + row["readiness"], actions)
            if pick == 0:
                self.operation("Inspect " + row["model"], lambda: fleet.inspect_model(self.read_client, fleet.snapshot(self.read_client), row["model"]))
                self.tab, self.selected, self.search = 4, 0, ""
            elif pick in (1, 2, 3):
                self.control(("load", "unload", "assign")[pick - 1], row["model"])
            elif pick == 4:
                self.test([row])
            elif pick == 5:
                self.launch(row["model"])
        elif self.tab == 1:
            pick = self.choose(self.worker_label(row), ["Browse this worker's models", "Load a model", "Unload a resident model", "Inspect worker"])
            if pick == 0:
                self.worker_filter = row.get("id")
                self.tab, self.selected, self.search = 0, 0, ""
            elif pick in (1, 2):
                keys = [r["model"] for r in self.models if not r["blocked"]] if pick == 1 else fleet.model_keys(row, "loaded_models")
                selected = self.choose("Choose model", keys)
                if selected is not None:
                    self.control("load" if pick == 1 else "unload", keys[selected], row)
            elif pick == 3:
                self.view("Worker details", row)
        elif self.tab == 5:
            self.launch(frontend=row)
        else:
            self.view(TABS[self.tab], row)

    def run(self):
        self.screen.keypad(True)
        self.screen.timeout(150)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.start_refresh()
        try:
            while True:
                self.drain()
                if not self.busy and time.monotonic() - self.last_refresh > 30:
                    self.start_refresh()
                self.draw()
                key = self.screen.getch()
                if key in (ord("q"), ord("Q")):
                    if self.busy and self.choose("A request may still be running on the fleet", ["Stay", "Exit console and stop remaining tests"]) != 1:
                        continue
                    return 0
                if key in (curses.KEY_UP, ord("k")):
                    self.selected = max(0, self.selected - 1)
                elif key in (curses.KEY_DOWN, ord("j")):
                    self.selected += 1
                elif key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
                    self.selected += (1 if key == curses.KEY_NPAGE else -1) * max(1, self.screen.getmaxyx()[0] - 10)
                elif key == 9 or ord("1") <= key <= ord("6"):
                    self.tab = (self.tab + 1) % len(TABS) if key == 9 else key - ord("1")
                    self.selected, self.search = 0, ""
                elif key in (ord("r"), ord("R")):
                    self.frontends = frontends.available()
                    self.start_refresh()
                elif key == ord("/"):
                    value = self.prompt("Filter rows", self.search)
                    if value is not None:
                        self.search, self.selected = value, 0
                elif key in (ord("c"), ord("C")):
                    self.search, self.worker_filter, self.selected = "", None, 0
                elif key in (ord("t"), ord("T")) and self.tab == 0:
                    self.test(self.items())
                elif key in (ord("m"), ord("M")):
                    self.matrix_test()
                elif key in (ord("s"), ord("S")):
                    self.stop_tests.set()
                    self.notice = "Remaining tests stopped. An active fleet call finishes normally."
                elif key in (10, 13, curses.KEY_ENTER):
                    items = self.items()
                    if items:
                        self.open_row(items[self.selected])
        finally:
            self.closed.set()
            self.stop_tests.set()


def run(client):
    try:
        return curses.wrapper(lambda screen: Console(screen, client).run())
    except curses.error as exc:
        raise fleet.FleetError("Interactive console needs a terminal with TERM set (SSH with a PTY works).") from exc


def describe(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if value.get("activity"):
            return value.get("message", "") + "\n\n" + json.dumps(value.get("details") or {}, indent=2)
        if value.get("matrix"):
            headline = "%s — %s on %s" % (
                "PASS" if value.get("ok") else "FAIL",
                value.get("model"), value.get("worker"))
            return headline + "\nSpeed: %s tok/s\n\n%s" % (
                number(value.get("tok_s")), json.dumps(value, indent=2))
        response = value.get("response", value)
        if isinstance(response, dict) and "candidates" in response:
            lines = [response.get("model", "Model"), ""]
            for c in response["candidates"]:
                lines += ["%s: %s" % (c.get("worker"), c.get("readiness")),
                          "  Loaded: %s | Files present: %s" % (c.get("hot"), c.get("on_disk")),
                          "  Preparation: %s | Queue: %s" % (number(c.get("load_estimate_s"), "s"), number(c.get("queue_wait_s"), "s"))]
                if c.get("reason"):
                    lines.append("  " + c["reason"])
            lines += ["", response.get("eta_note", "")]
            return "\n".join(lines)
        if isinstance(response, dict) and response.get("choices"):
            content = response["choices"][0].get("message", {}).get("content", "")
            return "%s\nElapsed: %s\n\n%s\n\nUsage: %s" % (value.get("model", "Test result"), number(value.get("elapsed_s"), "s"), content, json.dumps(response.get("usage")))
    return json.dumps(value, indent=2)
