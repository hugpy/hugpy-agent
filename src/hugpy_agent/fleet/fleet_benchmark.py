"""Capacity-aware fleet benchmark planning and deterministic grading.

Central model storage is immutable.  A quant is *cold* when it exists in the
central catalog but is absent from a worker drive and *hot* when the worker's
verbose catalog join reports on_disk_bytes.  4-bit and MoE are runtime configs:
their disk/transfer prerequisite is always the complete base quant.
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import quote

from .fleet_console import FleetError, eligible, rows

GIB = 2 ** 30
HEADROOM = GIB


def _ints(value):
    return [int(x) for x in re.findall(r"-?\d+", value or "")]


def _json_ok(value):
    match = re.search(r"\{.*\}", value or "", re.S)
    if not match:
        return False
    try:
        return json.loads(match.group(0)) == {"a": 1, "b": 2}
    except ValueError:
        return False


TASKS = (
    ("math", "Compute 17 * 23. Reply with only the final number.",
     lambda s: bool(_ints(s)) and _ints(s)[-1] == 391),
    ("wordprob", "A store had 48 apples. It sold 19 in the morning and 12 in the afternoon. How many are left? Reply with only the number.",
     lambda s: bool(_ints(s)) and _ints(s)[-1] == 17),
    ("factual", "What is the chemical symbol for gold? Reply with only the symbol.",
     lambda s: re.search(r"\bAu\b", s or "") is not None),
    ("format_primes", "List the first five prime numbers separated by commas and nothing else.",
     lambda s: _ints(s)[:5] == [2, 3, 5, 7, 11]),
    ("logic", "If all bloops are razzies, and all razzies are lazzies, are all bloops lazzies? Answer with only yes or no.",
     lambda s: bool(re.search(r"\byes\b", s or "", re.I)) and not re.search(r"\bno\b", s or "", re.I)),
    ("exact_instruction", "Reply with exactly the single word: BANANA",
     lambda s: (s or "").strip() == "BANANA"),
    ("coding", "Write a Python one-liner using sum() that returns the total of a list named xs. Reply with only the code.",
     lambda s: "sum(xs)" in re.sub(r"\s+", "", s or "")),
    ("json", 'Output only a JSON object with keys "a" set to 1 and "b" set to 2.', _json_ok),
    ("letters", "How many times does the letter r appear in the word strawberry? Reply with only the number.",
     lambda s: bool(_ints(s)) and _ints(s)[-1] == 3),
)


def verbose_catalog(client):
    """One catalog fetch, matching the established HugPy grader."""
    payload = client.request("/models?verbose=1")
    return rows(payload, "models") if isinstance(payload, dict) else rows(payload, "models")


def model_id(model):
    return model.get("model_key") or model.get("id") or model.get("name")


def worker_join(model, worker):
    wid, name = worker.get("id"), worker.get("name")
    return next((row for row in (model.get("workers") or [])
                 if row.get("worker_id") == wid or row.get("worker") == name), {})


def _variant_rows(model):
    variants = model.get("gguf_variants") or []
    out, shards = [], {}
    for variant in variants:
        if not isinstance(variant, dict) or variant.get("complete") is False:
            continue
        size = variant.get("bytes") or variant.get("size_bytes") or 0
        name = variant.get("filename") or variant.get("quant") or variant.get("id")
        if name and size:
            shard = re.match(r"^(.*)-\d{5}-of-\d{5}(\.gguf)$", name, re.I)
            if shard:
                quant = shard.group(1) + shard.group(2)
                row = shards.setdefault(quant, {"quant": quant, "file": name,
                                                 "disk_bytes": 0, "gguf": True,
                                                 "shards": []})
                row["disk_bytes"] += int(size)
                row["shards"].append(name)
            else:
                out.append({"quant": name, "file": variant.get("filename") or name,
                            "disk_bytes": int(size), "gguf": True})
    out.extend(shards.values())
    if out:
        return out
    size = model.get("effective_bytes") or model.get("size_bytes") or model.get("dir_bytes") or 0
    return [{"quant": model.get("effective_gguf") or model.get("quant") or "base",
             "file": model.get("effective_gguf"), "disk_bytes": int(size or 0),
             "gguf": (model.get("framework") or "").lower() in ("gguf", "llama_cpp")}]


def _cap(worker, *keys):
    for key in keys:
        value = worker.get(key)
        if isinstance(value, (int, float)):
            return max(0, int(value) - HEADROOM)
    return 0


def plan_worker(catalog, worker):
    """Return exact quant/config rows classified by worker capacity.

    Disk bytes always equal the complete base quant. Runtime bytes may be
    repriced for 4-bit or split for MoE, but never reduce transfer size.
    """
    gpu = _cap(worker, "vram_total", "vram_free")
    ram = _cap(worker, "ram_total", "free_ram")
    disk_free = int((worker.get("disk") or {}).get("free_bytes") or 0)
    # Rolling-cache capacity: bytes currently in the worker's reapable cache
    # become available as hot bases finish. Never include shared/central rows.
    reclaimable = sum(int(row.get("bytes") or 0)
                      for row in ((worker.get("storage") or {}).get("models") or [])
                      if isinstance(row, dict) and row.get("counts_toward_budget", True)
                      and not row.get("protected"))
    rolling_disk = disk_free + reclaimable
    planned = []
    for model in catalog:
        mid = model_id(model)
        if not mid or model.get("blocked"):
            continue
        tasks = set(model.get("tasks") or []) | {model.get("task")}
        if "text-generation" not in tasks and "image-text-to-text" not in tasks:
            continue
        join = worker_join(model, worker)
        on_disk = join.get("on_disk_bytes")
        variants = _variant_rows(model)
        for variant in variants:
            # A GGUF repo can contain many central variants while the transfer
            # manifest stages one elected quant. Model-level on_disk truth is
            # narrowed by the measured bytes/effective filename where possible.
            hot = on_disk is not None and (len(variants) == 1 or
                  variant.get("file") == model.get("effective_gguf") or
                  abs(int(on_disk or 0) - variant["disk_bytes"]) < 64 * 1024 ** 2)
            base = variant["disk_bytes"]
            configs = [("full", base, base, False, False)]
            # BitsAndBytes and MoE are runtime variants requiring the full base.
            bnb_supported = join.get("bnb_4bit") is not None or model.get("bnb_capable")
            moe_supported = join.get("moe") is not None or bool(model.get("moe"))
            if bnb_supported:
                configs.append(("4bit", max(1, int(base * .38)), base, True, False))
            moe = model.get("moe") or {}
            if moe_supported:
                gpu_part = int(moe.get("non_expert_bytes") or min(base, gpu))
                configs.append(("moe", gpu_part, base, False, True))
            for kind, runtime, disk_bytes, bnb, is_moe in configs:
                # Classification is exclusive, not a list of every place the
                # bytes *could* fit. Prefer full GPU residency; GGUF that misses
                # VRAM may spill across GPU+RAM; only non-GGUF runtimes fall
                # back to RAM-only. This prevents a GPU-fit quant being tested
                # again (and misleadingly reported) as RAM-only.
                if runtime and runtime <= gpu:
                    modes = ["gpu_only"]
                elif variant["gguf"] and runtime and runtime <= gpu + ram:
                    modes = ["max_gpu"]
                elif not variant["gguf"] and runtime and runtime <= ram:
                    modes = ["ram_only"]
                else:
                    modes = []
                if not modes:
                    planned.append({"model": mid, **variant, "kind": kind,
                                    "mode": "infeasible", "hot": hot,
                                    "disk_bytes": disk_bytes, "runtime_bytes": runtime,
                                    "reason": "does not fit worker GPU+RAM", "bnb": bnb,
                                    "moe": is_moe, "bnb_supported": bnb_supported,
                                    "moe_supported": moe_supported, "model_record": model})
                else:
                    for mode in modes:
                        planned.append({"model": mid, **variant, "kind": kind,
                                        "mode": mode, "hot": hot,
                                        "disk_bytes": disk_bytes, "runtime_bytes": runtime,
                                        "stage_bytes": 0 if hot else disk_bytes,
                                        "disk_feasible": hot or disk_bytes <= rolling_disk,
                                        "bnb": bnb, "moe": is_moe,
                                        "bnb_supported": bnb_supported,
                                        "moe_supported": moe_supported,
                                        "model_record": model})
    # Grade what is already present first (largest first); stage cold smallest
    # first so the rolling cache maximizes completed variants.
    return sorted(planned, key=lambda p: (0 if p["hot"] else 1,
                                         -p["disk_bytes"] if p["hot"] else p["disk_bytes"],
                                         p["model"], p["quant"], p["mode"]))


def _content(response):
    if isinstance(response, dict) and isinstance(response.get("text"), str):
        return response["text"]
    try:
        return response["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        return ""


def worker_infer(worker, plan, prompt, tokens, timeout=900):
    """Call the selected worker directly; attribution is structural, not inferred."""
    url = (worker.get("url") or "").rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise FleetError("worker has no usable direct inference URL")
    payload = {"model_key": plan["model"],
               "messages": [{"role": "user", "content": prompt}],
               "max_new_tokens": tokens, "temperature": 0,
               "request_id": "benchmark-%d" % time.time_ns(),
               "spill": {"alloc_mode": plan["mode"]}}
    request = urllib.request.Request(url + "/infer", data=json.dumps(payload).encode(),
                                     method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        # HTTPError is also the response object.  Preserve a bounded diagnostic
        # instead of reducing every useful worker failure to "HTTP 500".
        try:
            raw = exc.read(8193)
            detail = raw[:8192].decode("utf-8", "replace").strip()
            if detail:
                try:
                    body = json.loads(detail)
                    if isinstance(body, dict):
                        detail = str(body.get("error") or body.get("message") or detail)
                except ValueError:
                    pass
                detail = re.sub(r"\s+", " ", detail)
        except (OSError, ValueError):
            detail = ""
        message = "worker HTTP %s" % exc.code
        if detail:
            message += ": " + detail
        raise FleetError(message) from None
    except (OSError, ValueError) as exc:
        raise FleetError("worker %s" % type(exc).__name__) from None
    attribution = result.get("worker") or {}
    if attribution and attribution.get("id") not in (None, worker.get("id")):
        raise FleetError("worker attribution mismatch")
    if result.get("ok") is False:
        raise FleetError(str(result.get("error") or "worker inference failed"))
    return result


def infer_with_heartbeat(worker, plan, prompt, tokens, timeout=900, report=None,
                         task="inference", heartbeat=10):
    """Run a blocking worker call while keeping operators visibly informed."""
    finished = queue.Queue(maxsize=1)
    started = time.monotonic()

    def work():
        try:
            finished.put((True, worker_infer(worker, plan, prompt, tokens,
                                             timeout=timeout)))
        except BaseException as exc:
            finished.put((False, exc))

    threading.Thread(target=work, name="benchmark-%s" % task,
                     daemon=True).start()
    while True:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise FleetError("%s timed out after %ss for %s/%s on %s" % (
                task, timeout, plan["model"], plan["quant"],
                worker.get("name") or worker.get("id")))
        try:
            ok, value = finished.get(timeout=min(heartbeat, remaining))
        except queue.Empty:
            if report:
                report("stage", {"stage": task, "state": "waiting",
                                  "elapsed_s": round(time.monotonic() - started, 1),
                                  "timeout_s": timeout,
                                  "worker": worker.get("name") or worker.get("id"),
                                  "worker_id": worker.get("id"),
                                  "model": plan["model"], "quant": plan["quant"]})
            continue
        if ok:
            return value
        if isinstance(value, FleetError):
            raise value
        raise FleetError("%s failed: %s: %s" %
                         (task, type(value).__name__, value))


def assure_quant_status(client, worker, plan, live=None):
    """Freshly prove the exact worker/model/quant/config before every call."""
    wid, mid = worker["id"], plan["model"]
    live = live or client.request("/llm/workers/" + quote(wid, safe=""))
    local = set(live.get("models_local") or [])
    loaded = set(live.get("loaded_models") or [])
    if mid not in local or mid not in loaded:
        raise FleetError("exact worker status is not hot+loaded for " + mid)
    observed_mode = (live.get("model_alloc_modes") or {}).get(mid)
    normalize_mode = lambda value: str(value).lower().replace("-", "_") if value is not None else None
    explicit_moe = normalize_mode(observed_mode) == "explicit" and plan.get("moe_supported")
    if (observed_mode is not None and not explicit_moe and
            normalize_mode(observed_mode) != normalize_mode(plan["mode"])):
        raise FleetError("worker allocation mode does not match test plan")
    if plan.get("bnb_supported") and bool((live.get("bnb_by_model") or {}).get(mid)) != bool(plan["bnb"]):
        raise FleetError("worker 4-bit status does not match test plan")
    if plan.get("moe_supported") and bool((live.get("moe_effective") or {}).get(mid)) != bool(plan["moe"]):
        raise FleetError("worker MoE status does not match test plan")
    if plan.get("file"):
        serving = client.request("/llm/serving/" + quote(mid, safe=""))
        pin = (serving.get("gguf_file_by_worker") or {}).get(wid)
        if pin and pin != plan["file"]:
            raise FleetError("worker quant pin does not match test plan")
    return live


def apply_config(client, worker, plan):
    wid, mid = worker["id"], plan["model"]
    if plan.get("file"):
        serving = client.request("/llm/serving/" + quote(mid, safe=""))
        pins = dict(serving.get("gguf_file_by_worker") or {})
        pins[wid] = plan["file"]
        client.request("/llm/serving/" + quote(mid, safe=""), "POST",
                       {"gguf_file_by_worker": pins})
    if plan.get("bnb_supported"):
        client.request("/llm/workers/%s/bnb" % quote(wid, safe=""), "POST",
                       {"model_key": mid, "enabled": bool(plan["bnb"])})
    if plan.get("moe_supported"):
        client.request("/llm/workers/%s/moe" % quote(wid, safe=""), "POST",
                       {"model_key": mid, "value": bool(plan["moe"])})
    # /load performs assign + provision + background warm.  /assign alone only
    # records intent and was the source of false benchmark failures: the grader
    # checked residency before a seat had ever been requested.
    client.request("/llm/workers/%s/load" % quote(wid, safe=""), "POST",
                   {"model_key": mid, "spill": {"alloc_mode": plan["mode"]}})


def start_cold_transfer(client, worker, plan, report=None):
    """Make a cold model's lazy worker path perform the real transfer.

    Worker ``/load`` is intentionally only a probe for a non-local model.  A
    real inference request is the operation that provisions it from central,
    so issue a one-token, ungraded seating call before waiting for residency.
    Every such call is retained in the benchmark call log.
    """
    wid, mid = worker["id"], plan["model"]
    live = client.request("/llm/workers/" + quote(wid, safe=""))
    if mid in set(live.get("models_local") or []):
        return None
    if report:
        report("notice", "Starting central-to-worker transfer for %s/%s on %s" % (
            mid, plan["quant"], worker.get("name") or wid))
    started = time.time()
    response = None
    error = None
    try:
        response = infer_with_heartbeat(worker, plan, "Reply READY.", 1,
                                        timeout=900, report=report,
                                        task="cold_seat")
    except FleetError as exc:
        error = str(exc)
        # A client timeout or transitional HTTP response can occur after the
        # worker accepted provisioning.  Only tolerate it with fresh evidence.
        fresh = client.request("/llm/workers/" + quote(wid, safe=""))
        active = set(fresh.get("models_local") or []) | set(fresh.get("loading") or [])
        active.update(str(row.get("model_key") or row) for row in
                      (fresh.get("provisioning") or []) if row is not None)
        if mid not in active:
            raise FleetError("cold transfer did not start: " + error) from None
    if report:
        report("call", {"worker": worker.get("name") or wid, "worker_id": wid,
                        "model": mid, "quant": plan["quant"],
                        "config": plan["kind"], "alloc_mode": plan["mode"],
                        "task": "__seat__", "prompt": "Reply READY.",
                        "started": started, "finished": time.time(),
                        "elapsed_s": round(time.time() - started, 4),
                        "tok_s": "N/A", "output": _content(response or {}),
                        "response": response if response is not None else "N/A",
                        "grade": "N/A", "passed": "N/A", "error": error or "N/A"})
    return response


def wait_until_hot(client, worker, model, timeout=1800, poll=10):
    deadline = time.monotonic() + timeout
    path = "/models/%s?verbose=1" % quote(model, safe="")
    while time.monotonic() < deadline:
        row = client.request(path)
        if worker_join(row, worker).get("on_disk_bytes") is not None:
            return True
        time.sleep(poll)
    return False


def wait_until_ready(client, worker, plan, timeout=3600, poll=5, stop=None,
                     report=None):
    """Patiently wait for worker disk presence *and* a healthy seated model.

    Loading is asynchronous and can legitimately take many minutes.  A model is
    testable only after fresh worker state says it is local, loaded, no longer in
    the loading list, and its allocation is not explicitly unhealthy.
    """
    deadline = time.monotonic() + timeout
    wid, mid = worker["id"], plan["model"]
    last = None
    while time.monotonic() < deadline:
        if stop is not None and (stop.is_set() or
                (hasattr(stop, "cancelled") and stop.cancelled(
                    worker_id=wid, model=mid, quant=plan["quant"]))):
            raise FleetError("test cancelled while waiting for model seat")
        try:
            live = client.request("/llm/workers/" + quote(wid, safe=""))
            local = mid in set(live.get("models_local") or [])
            loaded = mid in set(live.get("loaded_models") or [])
            loading = mid in set(live.get("loading") or [])
            allocations = [a for a in (live.get("allocations") or [])
                           if isinstance(a, dict) and a.get("model_key") == mid]
            healthy = not any(a.get("healthy") is False for a in allocations)
            stage = "seated" if local and loaded and not loading and healthy else (
                    "loading" if loading or local else "downloading")
            elapsed = round(timeout - max(0, deadline - time.monotonic()), 1)
            if report and (stage != last or int(elapsed) % 10 < poll):
                report("stage", {"stage": stage, "state": stage,
                                  "elapsed_s": elapsed, "timeout_s": timeout,
                                  "worker": worker.get("name") or wid,
                                  "worker_id": wid, "model": mid,
                                  "quant": plan["quant"],
                                  "provision_progress":
                                      (live.get("provision_progress") or {}).get(mid)})
                if stage != last:
                    report("notice", "Waiting for %s/%s on %s: %s" % (
                        mid, plan["quant"], worker.get("name") or wid, stage))
                last = stage
            if stage == "seated":
                assure_quant_status(client, worker, plan, live=live)
                return live
        except FleetError as exc:
            if report and str(exc) != last:
                report("notice", "Still waiting for %s on %s: %s" % (
                    mid, worker.get("name") or wid, exc))
                last = str(exc)
        time.sleep(poll)
    raise FleetError("timed out after %ss waiting for a healthy model seat" % timeout)


def grade_config(client, worker, plan, tokens, stop, report):
    started = time.time()
    detail = {name: "N/A" for name, _prompt, _checker in TASKS}
    answers = {name: "N/A" for name, _prompt, _checker in TASKS}
    calls, total, speeds = [], 0.0, []
    complete_analyses = 0
    error = None
    for name, prompt, checker in TASKS:
        if stop.is_set() or (hasattr(stop, "cancelled") and stop.cancelled(
                worker_id=worker.get("id"), model=plan["model"],
                quant=plan["quant"], call=name)):
            break
        before = time.monotonic()
        wall_started = time.time()
        response = None
        call_error = None
        try:
            assure_quant_status(client, worker, plan)
            # Deterministic grading wants the requested short answer, not a
            # reasoning scratchpad consuming the entire output allowance.
            wire_prompt = prompt + " /no_think"
            response = infer_with_heartbeat(worker, plan, wire_prompt, tokens,
                                            timeout=900, report=report,
                                            task="call:" + name)
            answer = _content(response)
            ok = bool(checker(answer))
        except FleetError as exc:
            answer, ok, call_error = "", False, str(exc)
            error = call_error
        elapsed = time.monotonic() - before
        total += elapsed
        detail[name], answers[name] = int(ok), answer[:300]
        timings = response.get("timings") if isinstance(response, dict) else {}
        speed = ((timings or {}).get("predicted_per_second") or
                 (timings or {}).get("tokens_per_second") or
                 (timings or {}).get("tok_per_s"))
        speed_source = ((timings or {}).get("measurement_source") or
                        ("engine_timings" if isinstance(speed, (int, float)) else None))
        usage = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(speed, (int, float)) and isinstance(usage, dict):
            completion_tokens = usage.get("completion_tokens")
            if isinstance(completion_tokens, (int, float)) and completion_tokens > 0 and elapsed > 0:
                speed = completion_tokens / elapsed
                speed_source = "usage/client_wall"
        finish_reason = (response.get("finish_reason")
                         if isinstance(response, dict) else None)
        generated = ((timings or {}).get("predicted_n")
                     if isinstance(timings, dict) else None)
        if generated is None and isinstance(usage, dict):
            generated = usage.get("completion_tokens")
        truncated = (finish_reason in ("length", "max_tokens") or
                     isinstance(generated, (int, float)) and generated >= tokens)
        analysis_complete = (call_error is None and
                             isinstance(speed, (int, float)) and speed > 0 and
                             bool(speed_source) and not truncated)
        if isinstance(speed, (int, float)):
            speeds.append(speed)
        if analysis_complete:
            complete_analyses += 1
        grade = ("ERROR" if call_error else
                 "INCOMPLETE" if not analysis_complete else
                 "PASS" if ok else "FAIL")
        call = {"worker": worker.get("name") or worker.get("id"),
                "worker_id": worker.get("id"), "model": plan["model"],
                "quant": plan["quant"], "config": plan["kind"],
                "alloc_mode": plan["mode"], "task": name, "prompt": prompt,
                "started": wall_started, "finished": time.time(),
                "elapsed_s": round(elapsed, 4), "tok_s": speed or "N/A",
                "tok_s_source": speed_source or "N/A",
                "finish_reason": finish_reason or "N/A",
                "analysis_complete": analysis_complete,
                "truncated": truncated,
                "output": answer, "response": response if response is not None else "N/A",
                "grade": grade, "answer_passed": bool(ok),
                "passed": bool(ok and analysis_complete),
                "error": call_error or "N/A"}
        calls.append(call)
        report("call", call)
        report("notice", "%s/%s %s: %s" %
               (plan["model"], plan["quant"], name, call["grade"]))
    wid = worker["id"]
    completed = all(isinstance(v, int) for v in detail.values())
    passed_all = completed and all(v == 1 for v in detail.values())
    metrics_complete = complete_analyses == len(TASKS)
    result = {"matrix": True, "ok": not error and passed_all and metrics_complete,
              "worker": worker.get("name") or wid, "worker_id": wid,
              "model": plan["model"], "quant": plan["quant"],
              "config": plan["kind"], "alloc_mode": plan["mode"],
              # These calls run only after the model seat is healthy, so they
              # are warm/hot inference regardless of whether the artifact was
              # initially present on the worker drive. Cold acquisition/load
              # timing belongs to the orchestration layer around this call.
              "cold_s": "N/A",
              "hot_s": round(total, 4),
              "inference_s": round(total, 4),
              "tok_s": speeds[-1] if speeds else "N/A",
              "tok_s_avg": sum(speeds) / len(speeds) if speeds else "N/A",
              "metrics_complete": metrics_complete,
              "analyses_complete": complete_analyses,
              "detail": detail, "answers": answers, "calls": calls,
              "score": sum(v for v in detail.values() if isinstance(v, int)), "max": len(TASKS),
              "total_latency_s": round(total, 2), "disk_bytes": plan["disk_bytes"],
              "runtime_bytes": plan["runtime_bytes"], "started": started,
              "finished": time.time(), "grade": "%s/%s" % (
                  sum(v for v in detail.values() if isinstance(v, int)), len(TASKS))}
    if error:
        result["error"] = error
    return result


def write_report(results, path=None):
    """Persist durable JSON metrics in the same logical columns as the ODS."""
    root = os.path.expanduser("~/.hugpy_agent/console/reports")
    os.makedirs(root, mode=0o700, exist_ok=True)
    path = path or os.path.join(root, "metrics-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as output:
        json.dump(results, output, indent=2, sort_keys=True)
    os.replace(tmp, path)
    try:
        from odf.opendocument import OpenDocumentSpreadsheet
        from odf.table import Table, TableCell, TableRow
        from odf.text import P
        doc = OpenDocumentSpreadsheet()
        sheet = Table(name="metrics")
        columns = (["model", "worker", "config", "quant", "alloc_mode",
                    "cold_s", "hot_s", "tok_per_s", "tok_per_s_avg"] +
                   [task[0] for task in TASKS] +
                   ["task_ideal", "task_worst", "overall", "error"])
        def add(values):
            row = TableRow()
            for value in values:
                cell = TableCell(valuetype="string")
                cell.addElement(P(text="" if value is None else str(value)))
                row.addElement(cell)
            sheet.addElement(row)
        add(columns)
        for result in results:
            detail = result.get("detail") or {}
            passed = [name for name, value in detail.items() if value]
            failed = [name for name, value in detail.items() if not value]
            add([result.get("model"), result.get("worker"), result.get("config"),
                 result.get("quant"), result.get("alloc_mode"), result.get("cold_s"),
                 result.get("hot_s"), result.get("tok_s"), result.get("tok_s_avg")] +
                [detail.get(task[0]) for task in TASKS] +
                [passed[0] if passed else "", failed[0] if failed else "",
                 "%s/%s" % (result.get("score", 0), result.get("max", len(TASKS))),
                 result.get("error", "")])
        doc.spreadsheet.addElement(sheet)
        ods_path = os.path.splitext(path)[0] + ".ods"
        doc.save(ods_path)
        return ods_path
    except (ImportError, OSError):
        return path
