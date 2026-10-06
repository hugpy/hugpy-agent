"""Emergency inference — bring a local GGUF up as an agent with NO dependency on
the hugpy wrapper, central, fitevict or the toolserver. The break-glass path:
stdlib plus a ``llama-server`` binary only, so it works precisely when the rest
of the stack cannot serve.

Reliability is the whole point here:
- discovery never raises (OSError -> empty/None), so a missing mount or a bad
  permission degrades to "nothing found", never a crash;
- launch captures the server's own log and the readiness wait is bounded and
  detects a crashed process, so the caller never hangs;
- everything the TUI window needs is returned as plain data it can render.

The launched server speaks the OpenAI chat API, so a plain ``openai-chat``
profile (``base_url`` = ``http://127.0.0.1:<port>/v1``) points the agent's
Gateway straight at it — the wrapper is entirely out of the path.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# Ordered llama-server candidates (first existing+executable wins). The hugpy
# worker engine binary is the canonical one; the rest are real fallbacks seen on
# the fleet. HUGPY_EMERGENCY_LLAMA_BIN overrides, then PATH.
_LLAMA_BIN_CANDIDATES = (
    "/srv/pyit/dev/hugpy_worker/engine/bin/llama-server",
    "/opt/qwen3-coder-next/bin/llama-server",
    "/mnt/16T_toshiba/llm_storage/engine/bin/llama-server",
    "/srv/abstractendeavors/models/llama.cpp/build/bin/llama-server",
)

# GGUF roots (recursed). HUGPY_EMERGENCY_MODEL_DIRS (os.pathsep-joined) overrides.
_MODEL_ROOTS = (
    "/mnt/16T_toshiba/llm_storage/models/gguf",
    "/mnt/nvmes/2T_samsung_990/hot990/aeb/models/gguf",
    "/mnt/llm_storage/models",
)

# Names that are not stand-alone chat models — skipped from the pickable list.
# "imatrix"/"imat" are importance-matrix calibration files, not weights (they
# are small and would fail to load), so they must never be auto-picked.
_NON_CHAT_HINTS = ("mmproj", "projector", "encoder", "vae", "clip", "t5",
                   "embed", "rerank", "whisper", "tts", "video", "diffus",
                   "imatrix", "imat")
# A real LLM GGUF is far larger than any stray artifact; this is a reliability
# backstop under the name filter, not the primary gate.
_MIN_MODEL_BYTES = 20 * (1 << 20)
# Mild preference for instruct/chat models when auto-picking.
_CHAT_HINTS = ("instruct", "-it", "chat", "qwen", "llama", "mistral", "gemma", "phi")


@dataclass
class Model:
    path: str
    name: str
    size_bytes: int
    mmproj: str = ""        # vision projector beside the model, if any


@dataclass
class Preflight:
    llama_bin: str = ""
    threads: int = 1
    models: list = field(default_factory=list)   # [Model]
    errors: list = field(default_factory=list)   # human-readable, shown in the window

    @property
    def ready(self) -> bool:
        return bool(self.llama_bin and self.models)


def find_llama_bin() -> str:
    """First usable llama-server path, or "" if none. Never raises."""
    env = os.environ.get("HUGPY_EMERGENCY_LLAMA_BIN", "").strip()
    cands = ([env] if env else []) + list(_LLAMA_BIN_CANDIDATES)
    for p in cands:
        try:
            if p and os.path.isfile(p) and os.access(p, os.X_OK):
                return p
        except OSError:
            continue
    return shutil.which("llama-server") or ""


def _model_roots() -> list:
    env = os.environ.get("HUGPY_EMERGENCY_MODEL_DIRS", "").strip()
    if env:
        return [d for d in env.split(os.pathsep) if d]
    return list(_MODEL_ROOTS)


def _is_mmproj(name: str) -> bool:
    n = name.lower()
    return n.endswith(".gguf") and ("mmproj" in n or "projector" in n)


def _multipart_first(name: str) -> bool:
    """A sharded GGUF (``-00001-of-00004``) is loaded via its FIRST shard only;
    later shards must not appear as separate pickable models."""
    import re
    m = re.search(r"-(\d{5})-of-(\d{5})", name)
    return (m is None) or (m.group(1) == "00001")


def _find_mmproj(model_path: str) -> str:
    try:
        d = os.path.dirname(model_path)
        for f in sorted(os.listdir(d)):
            if _is_mmproj(f):
                return os.path.join(d, f)
    except OSError:
        pass
    return ""


def list_models(limit: int = 200) -> list:
    """Pickable chat GGUFs across the roots, best-first (chat-ish then smallest).
    Never raises; unreadable roots are skipped."""
    seen = set()
    out = []
    for root in _model_roots():
        try:
            walker = os.walk(root)
        except OSError:
            continue
        for dirpath, _dirs, files in walker:
            for f in files:
                low = f.lower()
                if not low.endswith(".gguf") or _is_mmproj(low):
                    continue
                if any(h in low for h in _NON_CHAT_HINTS):
                    continue
                if not _multipart_first(f):
                    continue
                path = os.path.join(dirpath, f)
                if path in seen:
                    continue
                seen.add(path)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                if size and size < _MIN_MODEL_BYTES:
                    continue
                out.append(Model(path=path, name=f, size_bytes=size,
                                 mmproj=_find_mmproj(path)))
    out.sort(key=lambda m: (0 if any(h in m.name.lower() for h in _CHAT_HINTS) else 1,
                            m.size_bytes or 1 << 62))
    return out[:limit]


def model_for(path: str):
    """A Model for an explicit GGUF path (with its mmproj if present), or None
    if the path is not a readable file. Never raises."""
    try:
        if not (path and os.path.isfile(path)):
            return None
        size = os.path.getsize(path)
    except OSError:
        return None
    return Model(path=path, name=os.path.basename(path), size_bytes=size,
                 mmproj=_find_mmproj(path))


def default_threads() -> int:
    try:
        n = os.cpu_count() or 2
    except Exception:
        n = 2
    return max(1, n // 2)


def free_port(start: int = 8900, end: int = 9099) -> int:
    """A bindable loopback port in a range clear of the serve/TUI ports."""
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port in %d-%d" % (start, end))


def preflight() -> Preflight:
    """Everything the Emergency window renders before launch. Never raises."""
    pf = Preflight(llama_bin=find_llama_bin(), threads=default_threads(),
                   models=list_models())
    if not pf.llama_bin:
        pf.errors.append("no llama-server binary found (set HUGPY_EMERGENCY_LLAMA_BIN)")
    if not pf.models:
        pf.errors.append("no GGUF models found (set HUGPY_EMERGENCY_MODEL_DIRS)")
    return pf


class EmergencyServer:
    """A llama-server subprocess serving one GGUF over the OpenAI chat API.

    ``start`` launches and returns at once; ``wait_ready`` blocks up to
    ``timeout`` and reports (ok, detail), detecting a crash so the caller never
    hangs. ``base_url`` + ``profile`` wire it into the agent as an openai-chat
    model with the wrapper out of the path."""

    def __init__(self, model: Model, *, llama_bin: str = "", port: int = 0,
                 threads: int = 0, ctx: int = 8192, gpu_layers: str = "auto",
                 log_dir: str = ""):
        self.model = model
        self.llama_bin = llama_bin or find_llama_bin()
        self.port = port or free_port()
        self.threads = threads or default_threads()
        self.ctx = ctx
        self.gpu_layers = gpu_layers
        self.log_path = os.path.join(log_dir or os.environ.get("TMPDIR", "/tmp"),
                                     "hugpy-emergency-%d.log" % self.port)
        self.proc = None
        self._log = None

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d/v1" % self.port

    def _argv(self) -> list:
        argv = [self.llama_bin, "-m", self.model.path, "--host", "127.0.0.1",
                "--port", str(self.port), "-c", str(self.ctx), "-t", str(self.threads)]
        if self.model.mmproj:
            argv += ["--mmproj", self.model.mmproj]
        if self.gpu_layers == "cpu":
            argv += ["-ngl", "0"]
        elif self.gpu_layers not in ("auto", ""):
            argv += ["-ngl", str(self.gpu_layers)]
        return argv

    def start(self):
        if not self.llama_bin:
            raise RuntimeError("no llama-server binary (set HUGPY_EMERGENCY_LLAMA_BIN)")
        if not (self.model.path and os.path.isfile(self.model.path)):
            raise RuntimeError("model file missing: %s" % self.model.path)
        self._log = open(self.log_path, "w")
        self.proc = subprocess.Popen(self._argv(), stdout=self._log,
                                     stderr=subprocess.STDOUT)
        return self

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def wait_ready(self, timeout: float = 180.0):
        """(ok, detail). Polls /v1/models; a crashed process ends the wait at
        once with the tail of the server log."""
        url = self.base_url + "/models"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.alive():
                return False, "llama-server exited (code %s)\n%s" % (
                    getattr(self.proc, "returncode", "?"), self._log_tail())
            try:
                with urllib.request.urlopen(url, timeout=3) as r:
                    if r.status == 200:
                        return True, "ready on :%d" % self.port
            except (urllib.error.URLError, OSError, ConnectionError):
                time.sleep(1.0)
        return False, "not ready after %.0fs (see %s)" % (timeout, self.log_path)

    def _log_tail(self, lines: int = 12) -> str:
        try:
            with open(self.log_path) as f:
                return "".join(f.readlines()[-lines:])
        except OSError:
            return ""

    def profile(self, name: str = "") -> dict:
        """openai-chat profile for service.runtime — the wrapper is not involved."""
        label = name or ("Emergency: " + self.model.name)
        return {
            "protocol": "openai-chat",
            "base_url": self.base_url,
            "model": self.model.name,
            "label": label,
            "context_length": max(2048, self.ctx),
            "max_tokens": min(2048, max(256, self.ctx // 4)),
            "emergency": True,
        }

    def stop(self, grace: float = 5.0):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._log is not None:
            self._log.close()
            self._log = None
