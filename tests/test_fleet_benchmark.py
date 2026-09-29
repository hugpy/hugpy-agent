import _bootstrap  # noqa: F401
import io
import os
import tempfile
import threading
import urllib.error
from unittest.mock import Mock

from hugpy_agent import fleet_benchmark as b


def worker(**changes):
    row = {"id": "w1", "name": "gpu-a", "status": "online",
           "admission": "approved", "vram_total": 12 * b.GIB,
           "ram_total": 32 * b.GIB, "disk": {"free_bytes": 5 * b.GIB},
           "storage": {"models": []}}
    row.update(changes)
    return row


def model(**changes):
    row = {"model_key": "org~model", "tasks": ["text-generation"],
           "framework": "gguf", "effective_gguf": "model-Q4_K_M.gguf",
           "gguf_variants": [
               {"filename": "model-Q4_K_M.gguf", "bytes": 8 * b.GIB,
                "complete": True},
               {"filename": "model-Q8_0.gguf", "bytes": 16 * b.GIB,
                "complete": True}],
           "moe": {"non_expert_bytes": 4 * b.GIB}, "bnb_capable": True,
           "workers": []}
    row.update(changes)
    return row


def test_full_quant_is_disk_prerequisite_for_4bit_and_moe():
    plans = b.plan_worker([model()], worker(disk={"free_bytes": 20 * b.GIB}))
    q8 = [p for p in plans if p["quant"] == "model-Q8_0.gguf"]
    assert q8
    assert {p["disk_bytes"] for p in q8} == {16 * b.GIB}
    four = next(p for p in q8 if p["kind"] == "4bit" and p["mode"] == "gpu_only")
    assert four["runtime_bytes"] < four["disk_bytes"]
    moe = next(p for p in q8 if p["kind"] == "moe" and p["mode"] == "gpu_only")
    assert moe["runtime_bytes"] == 4 * b.GIB


def test_gpu_fit_is_exclusively_gpu_only_not_spill_or_ram():
    plans = b.plan_worker([model()], worker(vram_total=40 * b.GIB,
                                            ram_total=64 * b.GIB,
                                            disk={"free_bytes": 40 * b.GIB}))
    modes = {p["mode"] for p in plans if p["mode"] != "infeasible"}
    assert modes == {"gpu_only"}


def test_gguf_that_misses_vram_but_fits_combined_is_spill_only():
    plans = b.plan_worker([model(bnb_capable=False, moe={})],
                          worker(vram_total=10 * b.GIB, ram_total=32 * b.GIB,
                                 disk={"free_bytes": 40 * b.GIB}))
    q8 = [p for p in plans if p["quant"] == "model-Q8_0.gguf" and p["kind"] == "full"]
    assert {p["mode"] for p in q8} == {"max_gpu"}


def test_sharded_gguf_quant_uses_the_full_shard_set_size():
    m = model(gguf_variants=[
        {"filename": "coder-Q4_K_M-00001-of-00004.gguf", "bytes": 5 * b.GIB, "complete": True},
        {"filename": "coder-Q4_K_M-00002-of-00004.gguf", "bytes": 5 * b.GIB, "complete": True},
        {"filename": "coder-Q4_K_M-00003-of-00004.gguf", "bytes": 5 * b.GIB, "complete": True},
        {"filename": "coder-Q4_K_M-00004-of-00004.gguf", "bytes": 5 * b.GIB, "complete": True},
    ], bnb_capable=False, moe={})
    plans = b.plan_worker([m], worker(vram_total=12 * b.GIB, ram_total=32 * b.GIB,
                                      disk={"free_bytes": 30 * b.GIB}))
    assert {p["disk_bytes"] for p in plans} == {20 * b.GIB}
    assert {p["mode"] for p in plans} == {"max_gpu"}
    assert plans[0]["file"] == "coder-Q4_K_M-00001-of-00004.gguf"


def test_only_effective_worker_quant_is_hot():
    joined = {"worker": "gpu-a", "worker_id": "w1",
              "on_disk_bytes": 8 * b.GIB, "bnb_4bit": False, "moe": False}
    plans = b.plan_worker([model(workers=[joined])], worker())
    assert all(p["hot"] for p in plans if p["quant"] == "model-Q4_K_M.gguf")
    assert not any(p["hot"] for p in plans if p["quant"] == "model-Q8_0.gguf")


def test_reclaimable_worker_cache_counts_toward_rolling_capacity():
    storage = {"models": [{"model_key": "old", "bytes": 20 * b.GIB,
                            "protected": False, "counts_toward_budget": True}]}
    plans = b.plan_worker([model()], worker(storage=storage))
    q8 = [p for p in plans if p["quant"] == "model-Q8_0.gguf"
          and p["mode"] != "infeasible"]
    assert q8 and all(p["disk_feasible"] for p in q8)


def test_report_writes_json_and_ods_shape():
    result = {"model": "m", "worker": "w", "config": "full", "quant": "q4",
              "alloc_mode": "gpu_only", "detail": {name: 1 for name, *_ in b.TASKS},
              "score": len(b.TASKS), "max": len(b.TASKS)}
    with tempfile.TemporaryDirectory() as tmp:
        out = b.write_report([result], os.path.join(tmp, "metrics.json"))
        assert os.path.exists(os.path.join(tmp, "metrics.json"))
        assert os.path.exists(out)
        assert out.endswith((".ods", ".json"))


def test_apply_config_requests_an_actual_background_load():
    client = Mock()
    client.request.side_effect = [{"gguf_file_by_worker": {}}, {}, {}, {}, {}]
    plan = {"model": "org~model", "file": "m.gguf", "bnb_supported": True,
            "bnb": False, "moe_supported": True, "moe": False,
            "mode": "max_gpu"}
    b.apply_config(client, worker(), plan)
    path, method, body = client.request.call_args.args
    assert path == "/llm/workers/w1/load"
    assert method == "POST"
    assert body == {"model_key": "org~model", "spill": {"alloc_mode": "max_gpu"}}


def test_cold_transfer_uses_real_worker_inference_and_logs_call(monkeypatch):
    client = Mock()
    client.request.return_value = {"models_local": [], "loading": []}
    infer = Mock(return_value={"text": "READY"})
    monkeypatch.setattr(b, "worker_infer", infer)
    report = Mock()
    plan = {"model": "org~model", "quant": "m.gguf", "kind": "full",
            "mode": "max_gpu"}
    response = b.start_cold_transfer(client, worker(url="http://worker"), plan,
                                     report=report)
    assert response == {"text": "READY"}
    infer.assert_called_once_with(worker(url="http://worker"), plan,
                                  "Reply READY.", 1, timeout=900)
    assert report.call_args.args[0] == "call"
    assert report.call_args.args[1]["task"] == "__seat__"


def test_blocking_inference_emits_structured_heartbeat(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(b, "worker_infer",
                        lambda *_a, **_k: (gate.wait(.05), {"text": "ok"})[1])
    report = Mock()
    result = b.infer_with_heartbeat(
        worker(url="http://worker"),
        {"model": "org~model", "quant": "m.gguf"}, "hi", 1,
        timeout=1, heartbeat=.01, report=report, task="call:math")
    assert result == {"text": "ok"}
    stages = [call.args[1] for call in report.call_args_list
              if call.args[0] == "stage"]
    assert stages and stages[0]["stage"] == "call:math"
    assert stages[0]["worker_id"] == "w1"


def test_worker_http_error_preserves_json_diagnostic(monkeypatch):
    failure = urllib.error.HTTPError(
        "http://worker/infer", 500, "Internal Server Error", {},
        io.BytesIO(b'{"error":"tokenizer files are incomplete"}'))
    monkeypatch.setattr(b.urllib.request, "urlopen", Mock(side_effect=failure))
    plan = {"model": "org~model", "mode": "gpu_only"}
    try:
        b.worker_infer(worker(url="http://worker"), plan, "hi", 1)
    except b.FleetError as exc:
        assert str(exc) == "worker HTTP 500: tokenizer files are incomplete"
    else:
        assert False, "expected FleetError"


def test_grading_requests_no_think_and_marks_transport_failure_error(monkeypatch):
    client = Mock()
    monkeypatch.setattr(b, "assure_quant_status", Mock())
    infer = Mock(side_effect=b.FleetError("worker HTTP 500: tokenizer broken"))
    monkeypatch.setattr(b, "infer_with_heartbeat", infer)
    report = Mock()
    plan = {"model": "org~model", "quant": "m.gguf", "kind": "full",
            "mode": "gpu_only", "hot": True, "disk_bytes": 1,
            "runtime_bytes": 1}
    result = b.grade_config(client, worker(), plan, 64, threading.Event(), report)
    first_prompt = infer.call_args_list[0].args[2]
    assert first_prompt.endswith(" /no_think")
    calls = [c.args[1] for c in report.call_args_list if c.args[0] == "call"]
    assert calls[0]["grade"] == "ERROR"
    assert calls[0]["output"] == ""
    assert calls[0]["error"] == "worker HTTP 500: tokenizer broken"


def test_completed_but_wrong_answers_do_not_mark_aggregate_ok(monkeypatch):
    monkeypatch.setattr(b, "assure_quant_status", Mock())
    monkeypatch.setattr(b, "infer_with_heartbeat",
                        Mock(return_value={"text": "wrong answer"}))
    plan = {"model": "org~model", "quant": "m.gguf", "kind": "full",
            "mode": "gpu_only", "hot": True, "disk_bytes": 1,
            "runtime_bytes": 1}
    result = b.grade_config(Mock(), worker(), plan, 64, threading.Event(), Mock())
    assert result["grade"] == "0/9"
    assert result["score"] == 0
    assert result["ok"] is False


def test_hot_quant_does_not_issue_seating_inference(monkeypatch):
    client = Mock()
    client.request.return_value = {"models_local": ["org~model"]}
    infer = Mock()
    monkeypatch.setattr(b, "worker_infer", infer)
    plan = {"model": "org~model", "quant": "m.gguf", "kind": "full",
            "mode": "max_gpu"}
    assert b.start_cold_transfer(client, worker(), plan) is None
    infer.assert_not_called()


def test_wait_until_ready_survives_delayed_download_and_loading(monkeypatch):
    states = iter([
        {"models_local": [], "loaded_models": [], "loading": []},
        {"models_local": ["org~model"], "loaded_models": [], "loading": ["org~model"]},
        {"models_local": ["org~model"], "loaded_models": ["org~model"],
         "loading": [], "model_alloc_modes": {"org~model": "max-gpu"},
         "allocations": [{"model_key": "org~model", "healthy": True}]},
    ])
    client = Mock()
    client.request.side_effect = lambda path, *a: ({"gguf_file_by_worker": {"w1": "m.gguf"}}
                                                   if path.startswith("/llm/serving/") else next(states))
    monkeypatch.setattr(b.time, "sleep", lambda _n: None)
    plan = {"model": "org~model", "quant": "m.gguf", "file": "m.gguf",
            "mode": "max_gpu", "bnb_supported": False, "moe_supported": False}
    live = b.wait_until_ready(client, worker(), plan, timeout=30, poll=0,
                              stop=threading.Event(), report=Mock())
    assert live["loaded_models"] == ["org~model"]
    assert client.request.call_count == 4


def test_explicit_allocation_is_valid_for_moe_split(monkeypatch):
    live = {"models_local": ["org~model"], "loaded_models": ["org~model"],
            "loading": [], "model_alloc_modes": {"org~model": "explicit"},
            "moe_effective": {"org~model": True},
            "allocations": [{"model_key": "org~model", "healthy": True}]}
    client = Mock()
    client.request.return_value = live
    plan = {"model": "org~model", "quant": "m.gguf", "file": None,
            "mode": "max_gpu", "bnb_supported": False,
            "moe_supported": True, "moe": True}
    assert b.assure_quant_status(client, worker(), plan, live=live) is live
