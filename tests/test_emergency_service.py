import _bootstrap  # noqa: F401
import json
import time
from unittest.mock import patch

import pytest
from hugpy_agent.config import Config
from hugpy_agent.service.runtime import Runtime
import hugpy_agent.emergency as em


def runtime(tmp_path):
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps({"discover_clients": False, "profiles": {
        "local": {"base_url": "http://localhost:8000/v1", "model": "test", "context_length": 32768}}}))
    return Runtime(Config(workspace=str(tmp_path), audit_log="", max_steps=5, rag_enabled=False),
                   tmp_path / "state", profiles)


class _FakeServer:
    """Stand-in for a launched llama-server: no subprocess, ready at once."""
    def __init__(self, model, **kw):
        self.model = model
        self.port = 8950

    def start(self):
        return self

    def profile(self, name=""):
        return {"protocol": "openai-chat", "base_url": "http://127.0.0.1:8950/v1",
                "model": self.model.name, "label": name or "Emergency",
                "context_length": 8192, "max_tokens": 2048, "emergency": True}

    def wait_ready(self, timeout=240):
        return True, "ready on :8950"

    def stop(self, grace=5.0):
        pass


def _pf(**kw):
    kw.setdefault("llama_bin", "/bin/llama-server")
    kw.setdefault("threads", 4)
    kw.setdefault("models", [em.Model(path="/x/m.gguf", name="m.gguf", size_bytes=10)])
    return em.Preflight(**kw)


def test_emergency_launch_opens_local_openai_session(tmp_path):
    rt = runtime(tmp_path)
    with patch.object(em, "preflight", _pf), patch.object(em, "EmergencyServer", _FakeServer):
        view = rt.launch_emergency()
    sid = view["id"]
    assert view["profile"].startswith("emergency:")
    prof = rt.emergency_profiles[view["profile"]]
    assert prof["protocol"] == "openai-chat"            # bypasses the hugpy wrapper
    assert prof["base_url"] == "http://127.0.0.1:8950/v1"
    assert sid in rt.emergency_servers
    # the background readiness wait posts a visible 'ready' note
    until = time.monotonic() + 5
    while time.monotonic() < until:
        notes = [e["data"] for e in rt.view(sid)["events"] if e["kind"] == "note"]
        if any("ready" in n for n in notes):
            break
        time.sleep(.01)
    else:
        pytest.fail("no ready note: %s" % rt.view(sid)["events"])


def test_emergency_launch_requires_a_binary(tmp_path):
    rt = runtime(tmp_path)
    with patch.object(em, "preflight", lambda: _pf(llama_bin="", models=[],
                                                   errors=["no llama-server binary found"])):
        with pytest.raises(ValueError):
            rt.launch_emergency()


def test_emergency_launch_requires_a_model(tmp_path):
    rt = runtime(tmp_path)
    with patch.object(em, "preflight", lambda: _pf(models=[], errors=["no GGUF models found"])):
        with pytest.raises(ValueError):
            rt.launch_emergency()


def test_emergency_preflight_serializes(tmp_path):
    rt = runtime(tmp_path)
    pf = _pf(models=[em.Model(path="/x/a.gguf", name="a.gguf", size_bytes=5, mmproj="/x/p.gguf")])
    with patch.object(em, "preflight", lambda: pf):
        doc = rt.emergency_preflight()
    assert doc["llama_bin"] == "/bin/llama-server" and doc["threads"] == 4 and doc["ready"] is True
    assert doc["models"][0] == {"path": "/x/a.gguf", "name": "a.gguf", "size_bytes": 5, "mmproj": "/x/p.gguf"}


def test_failed_launch_marks_session_interrupted(tmp_path):
    rt = runtime(tmp_path)

    class _Crash(_FakeServer):
        def wait_ready(self, timeout=240):
            return False, "llama-server exited (code 7)"

    with patch.object(em, "preflight", _pf), patch.object(em, "EmergencyServer", _Crash):
        view = rt.launch_emergency()
    sid = view["id"]
    until = time.monotonic() + 5
    while time.monotonic() < until:
        if rt.view(sid)["status"] == "interrupted":
            break
        time.sleep(.01)
    else:
        pytest.fail("session not marked interrupted: %s" % rt.view(sid))
    assert sid not in rt.emergency_servers
    assert any("failed" in e["data"] for e in rt.view(sid)["events"] if e["kind"] == "note")
