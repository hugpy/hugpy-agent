"""Tiered cognitive grading plus fixed-allocation fleet benchmarking."""
from __future__ import annotations

import json
import re
import time
from urllib.parse import quote

from . import fleet_benchmark as legacy
from .fleet_console import eligible


def _ints(s): return [int(x) for x in re.findall(r"-?\d+", s or "")]
def _last(n): return lambda s: _ints(s)[-1:] == [n]
def _has(word): return lambda s: word.lower() in (s or "").lower()
def _exact(value): return lambda s: (s or "").strip() == value


def _json_eq(expected):
    def check(value):
        match = re.search(r"\{.*\}", value or "", re.S)
        try: return bool(match) and json.loads(match.group(0)) == expected
        except ValueError: return False
    return check


TASKS_TIERED = {
    "math": (("easy", "Compute 15 + 22. Reply with only the final number.", _last(37)),
             ("medium", "Compute 17 * 23. Reply with only the final number.", _last(391)),
             ("hard", "Solve for x: 3x + 12 = 27. Reply with only the number.", _last(5))),
    "wordprob": (("easy", "John has 5 apples. He buys 3 more. How many? Reply number only.", _last(8)),
                 ("medium", "A store had 48 apples. Sold 19 in the morning, 12 in the afternoon. Left? Reply number only.", _last(17)),
                 ("hard", "Train A leaves at 60mph. 2 hours later Train B leaves at 80mph. Hours until they meet? Reply number only.", _last(6))),
    "factual": (("easy", "What planet is known as the Red Planet? Reply with only the planet name.", _has("mars")),
                ("medium", "What is the chemical symbol for gold? Reply with only the symbol.", _has("au")),
                ("hard", "Who discovered penicillin? Reply with only the last name.", _has("fleming"))),
    "format_primes": (("easy", "List the first three prime numbers separated by commas and nothing else.", lambda s: _ints(s)[:3] == [2, 3, 5]),
                      ("medium", "List the first five prime numbers separated by commas and nothing else.", lambda s: _ints(s)[:5] == [2, 3, 5, 7, 11]),
                      ("hard", "List the first five prime numbers in reverse order separated by commas and nothing else.", lambda s: _ints(s)[:5] == [11, 7, 5, 3, 2])),
    "logic": (("easy", "If all bloops are razzies, and all razzies are lazzies, are all bloops lazzies? Answer yes or no.", _has("yes")),
              ("medium", "A is taller than B. C is shorter than B. Who is the shortest? Reply only with the letter.", lambda s: (s or "").strip().lower() == "c"),
              ("hard", "Can a 3-gallon jug and a 5-gallon jug measure exactly 4 gallons? Answer yes or no.", _has("yes"))),
    "exact_instruction": (("easy", "Reply with exactly the single word: BANANA", _exact("BANANA")),
                          ("medium", "Reply with exactly the single word: BANANA, but in lowercase.", _exact("banana")),
                          ("hard", "Reply with exactly the string: [BANANA_123] and absolutely nothing else.", _exact("[BANANA_123]"))),
    "coding": (("easy", "Write a Python one-liner using sum() to total a list named xs. Reply with only code.", lambda s: "sum(xs)" in re.sub(r"\s+", "", s or "")),
               ("medium", "Write a Python list comprehension returning only even numbers from list xs. Reply with only code.", lambda s: "%2==0" in re.sub(r"\s+", "", s or "")),
               ("hard", "Write a recursive Python lambda named fib for Fibonacci. Reply with only code.", lambda s: "lambda" in s and "-1" in s and "-2" in s)),
    "json": (("easy", 'Output only a JSON object with key "a" set to 1.', _json_eq({"a": 1})),
             ("medium", 'Output only a JSON object with keys "a" set to 1 and "b" set to 2.', _json_eq({"a": 1, "b": 2})),
             ("hard", 'Output only a JSON object with key "a" containing a list of 1 and 2.', _json_eq({"a": [1, 2]}))),
    "letters": (("easy", "How many times does the letter e appear in the word tree? Reply number only.", _last(2)),
                ("medium", "How many times does the letter r appear in the word strawberry? Reply number only.", _last(3)),
                ("hard", "How many times does the letter s appear in the word mississippi? Reply number only.", _last(4))),
}


def _content(response):
    try: return response["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError): return ""


def _serving(client, model):
    try: return client.request("/llm/serving/" + quote(model, safe="")) or {}
    except Exception: return {}


def _quants(model, serving, worker_id):
    choices = serving.get("available_gguf_detail") or model.get("gguf_variants_detail") or []
    out = []
    for item in choices:
        name = (item.get("file") or item.get("filename") or item.get("quant")) if isinstance(item, dict) else item
        size = (item.get("bytes") or item.get("size_bytes")) if isinstance(item, dict) else None
        if name and not any(row["quant"] == name for row in out): out.append({"quant": name, "size_bytes": size})
    if not out:
        for name in serving.get("available_gguf") or model.get("gguf_variants") or []:
            if isinstance(name, str): out.append({"quant": name, "size_bytes": None})
    fallback = (serving.get("gguf_file_by_worker") or {}).get(worker_id) or serving.get("effective_gguf") or model.get("effective_gguf") or model.get("quant")
    return out or [{"quant": fallback or "runtime default", "size_bytes": model.get("effective_bytes") or model.get("size_bytes")}]


def _cap(worker, *keys):
    return next((worker[k] for k in keys if isinstance(worker.get(k), (int, float))), None)


def _variations(lane):
    model, joined, worker = lane["model_record"], lane["joined"], lane["worker_record"]
    size = lane.get("size_bytes") or model.get("effective_bytes") or model.get("size_bytes")
    vram, ram = _cap(worker, "max_vram_bytes", "vram_total"), _cap(worker, "max_ram_bytes", "ram_total")
    candidates = [("standard", "gpu_only", {}, size, vram), ("standard", "ram_only", {}, size, ram)]
    if joined.get("bnb_4bit") is not None or model.get("is_4bit_capable") or model.get("bnb_capable"):
        four = int(size * .55) if size else None
        candidates += [("4-bit", "gpu_only", {"bnb_4bit": True}, four, vram), ("4-bit", "ram_only", {"bnb_4bit": True}, four, ram)]
    if joined.get("moe") is not None or model.get("is_moe_capable") or model.get("moe"):
        spec = model.get("moe") if isinstance(model.get("moe"), dict) else {}
        total = (model.get("moe_explicit_vram") or spec.get("non_expert_bytes") or 0) + (model.get("moe_explicit_ram") or spec.get("expert_bytes") or 0)
        candidates.append(("standard", "explicit", {"moe": True}, total or size, vram))
    rows = []
    for precision, mode, extras, required, capacity in candidates:
        runnable = required is None or capacity is None or required <= capacity
        kind = "RAM" if mode == "ram_only" else "VRAM"
        reason = None if runnable else f"Size {required} bytes exceeds {kind} capacity {capacity} bytes"
        rows.append((precision, mode, extras, runnable, reason, required))
    return rows


def _lanes(client, workers, models):
    by_id, by_name, lanes = {w.get("id"): w for w in workers}, {w.get("name"): w for w in workers}, []
    for model in models:
        mid = legacy.model_id(model); serving = _serving(client, mid)
        for joined in model.get("workers") or []:
            worker = by_id.get(joined.get("worker_id")) or by_name.get(joined.get("worker"))
            if not worker or not eligible(worker) or not joined.get("designated"): continue
            for quant in _quants(model, serving, worker.get("id")):
                lanes.append({"model": mid, "model_record": model, "joined": joined, "worker_record": worker,
                              "worker": worker.get("name") or worker.get("id"), "worker_id": worker.get("id"), **quant})
    return lanes


def _select_quant(client, lane):
    if lane["quant"] == "runtime default": return
    path = "/llm/serving/" + quote(lane["model"], safe="")
    serving = client.request(path); pins = dict(serving.get("gguf_file_by_worker") or {})
    if pins.get(lane["worker_id"]) != lane["quant"]:
        pins[lane["worker_id"]] = lane["quant"]; client.request(path, "POST", {"gguf_file_by_worker": pins})


def _body(lane, variation, prompt, tokens):
    _precision, mode, extras = variation[:3]
    return {"model": lane["model"], "messages": [{"role": "user", "content": prompt + " /no_think"}],
            "max_tokens": tokens, "max_chunks": 1, "temperature": 0,
            "alloc": {"worker": lane["worker"], "alloc_mode": mode, **extras}}


def _call(client, lane, variation, prompt, tokens):
    started = time.time(); response = None; error = None
    try: response = client.request("/v1/chat/completions", "POST", _body(lane, variation, prompt, tokens)); answer = _content(response)
    except Exception as exc: answer, error = "", f"{type(exc).__name__}: {exc}"
    elapsed = time.time() - started; usage = response.get("usage") if isinstance(response, dict) else None
    generated = usage.get("completion_tokens") if isinstance(usage, dict) else None
    speed = generated / elapsed if isinstance(generated, (int, float)) and generated > 0 and elapsed > 0 else None
    prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    return answer, error, round(elapsed, 4), speed, prompt_tokens, generated


def run_capacity_benchmark(client, workers, tokens, stop, report, model_ids=None, worker_ids=None):
    """Grade once per precision and benchmark every physically valid allocation."""
    catalog = legacy.verbose_catalog(client)
    if model_ids: catalog = [m for m in catalog if legacy.model_id(m) in set(model_ids)]
    if worker_ids:
        wanted = set(worker_ids); workers = [w for w in workers if w.get("id") in wanted or w.get("name") in wanted]
    lanes = _lanes(client, workers, catalog); planned = []
    public = lambda lane: {k: v for k, v in lane.items() if k not in {"model_record", "joined", "worker_record", "size_bytes"}}
    for lane in lanes:
        for precision, mode, _extras, runnable, reason, runtime_bytes in _variations(lane):
            planned.append({**public(lane), "config": precision, "alloc_mode": mode, "runnable": runnable,
                            "status": "N/A" if runnable else "hardware_constraint_failed", "constraint_reason": reason,
                            "disk_bytes": lane.get("size_bytes"), "runtime_bytes": runtime_bytes,
                            "grade": "N/A", "tok_s": "N/A"})
    total = sum(row["runnable"] for row in planned)
    report("plan", {"total": len(planned), "runnable": total, "workers": len({r["worker_id"] for r in planned}), "rows": planned})
    completed, results = 0, []
    for lane in lanes:
        if stop.is_set(): break
        _select_quant(client, lane); grades = {}; cold_s = None
        for variation in _variations(lane):
            precision, mode, _extras, runnable, reason, runtime_bytes = variation
            plan = {**public(lane), "config": precision, "alloc_mode": mode,
                    "disk_bytes": lane.get("size_bytes"), "runtime_bytes": runtime_bytes}
            if not runnable:
                result = {**plan, "matrix": True, "ok": False, "status": "hardware_constraint_failed",
                          "constraint_reason": reason, "error": reason, "grade": "N/A", "tok_s": "N/A", "detail": {}}
                results.append(result); report("result", result); continue
            if stop.is_set() or (hasattr(stop, "cancelled") and stop.cancelled(worker_id=lane["worker_id"], model=lane["model"], quant=lane["quant"], call=precision + ":" + mode)): break
            seat_started = time.monotonic(); _answer, seat_error, _elapsed, _speed, _ctx_in, _ctx_out = _call(client, lane, variation, "Reply only: ready", 1)
            seat_s = round(time.monotonic() - seat_started, 4)
            if cold_s is None: cold_s, hot_s, phase = seat_s, None, "cold-load"
            else: hot_s, phase = seat_s, "hot-load"
            report("notice", {**plan, "phase": phase, "elapsed_s": seat_s, "error": seat_error})
            if precision not in grades and not seat_error:
                detail, answers, calls = {}, {}, []
                for category, tiers in TASKS_TIERED.items():
                    history = []
                    for tier, prompt, checker in tiers:
                        answer, error, elapsed, speed, ctx_in, ctx_out = _call(client, lane, variation, prompt, tokens)
                        passed = not error and bool(checker(answer)); history.append({"tier": tier, "pass": passed})
                        call = {**plan, "task": f"{category} ({tier})", "timestamp": time.time(),
                                "elapsed_s": elapsed, "tok_s": speed or "N/A",
                                "tok_s_avg": speed or "N/A", "ctx_in": ctx_in, "ctx_out": ctx_out,
                                "caller": "orchestrator", "output": answer,
                                "grade": "ERROR" if error else "PASS" if passed else "FAIL", "passed": passed, "error": error or "N/A"}
                        calls.append(call); report("call", call)
                        if not passed: break
                    detail[category] = {"tier": sum(x["pass"] for x in history), "max": 3, "history": history}; answers[category] = history
                depths = {k: v["tier"] for k, v in detail.items()}
                grades[precision] = {"detail": detail, "answers": answers, "calls": calls, "score": sum(depths.values()), "max": 27,
                                     "task_best": max(depths, key=depths.get), "task_worst": min(depths, key=depths.get)}
            grade = grades.get(precision, {"detail": {}, "answers": {}, "calls": [], "score": 0, "max": 27, "task_best": "N/A", "task_worst": "N/A"})
            _answer, speed_error, inference_s, speed, _ctx_in, _ctx_out = _call(client, lane, variation,
                "Generate a standard 100 word summary of theoretical optics and microfabrication parameters.", tokens)
            result = {**plan, **grade, "matrix": True, "ok": not seat_error and not speed_error,
                      "status": "complete" if not seat_error and not speed_error else "error", "grade": f"{grade['score']}/{grade['max']}",
                      "cold_s": cold_s, "cold_shared": True, "hot_load_s": hot_s, "inference_s": inference_s,
                      "tok_s": speed or "N/A", "tok_s_avg": speed or "N/A", "metrics_complete": speed is not None,
                      "error": seat_error or speed_error or "N/A", "finished": time.time()}
            results.append(result); completed += 1; report("result", result)
            report("progress", {"completed": completed, "total": total, "percent": round(100 * completed / max(1, total), 2), **plan})
    report("summary", {"execution": {"configurations": len(results)}, "workers": [], "models": [], "quants": []})
    report("notice", "HugPy-native benchmark complete")
