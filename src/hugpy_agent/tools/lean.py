"""Lean tools — the token-efficiency kit, packaged for the local keeper.

Everything the 2026-07-29 session proved about limiting Anthropic token spend,
made model-invokable so ANY keeper working through hugpy-agent gets it natively:

  * lean_find      — abstract-search via central /api/finder/search: ONLY file
                     paths + the matched lines. The cheapest way to locate
                     target data (replaces reading files to find things).
  * lean_digest    — spool + mechanical collapse (+ fleet-model digest of big
                     residue): fat text enters the conversation as a short
                     digest + a spool path; NOTHING is destroyed.
  * lean_logs      — journalctl for a unit -> the same digest treatment
                     (the single biggest raw-token class in the accounting).
  * lean_evictions — the structured eviction telemetry for a time window
                     (central /api/llm/evictions) — the journal story of a
                     call period, already parsed.
  * lean_deliver   — hand a file over by PATH on the share instead of pasting
                     its body into a reply (the biggest OUTPUT-token habit).

Doctrine (inherited from the session's measurements, not aspiration):
  * Mechanical first, model second. Timestamp-masked duplicate collapse turns
    an 873KB journal into ~15 unique lines; a model pass only runs on residue
    still too big to hand over, and its think-spill is stripped by a local,
    dependency-free no-think seam (regex).
  * Zero information loss. Every original byte is spooled under the workspace
    before compression; every digest names its spool path.
  * Errors as data. Every failure returns as a string the model can act on.
"""
from __future__ import annotations

import collections
import json
import os
import re
import shutil
import subprocess
import time

from . import RISK_READONLY, RISK_WRITE, ToolSpec

DELIVER_DIR = os.environ.get("HUGPY_LEAN_DELIVER",
                             "/srv/share/projects/hugpy/deliver")
DIGEST_MODEL = os.environ.get("HUGPY_LEAN_MODEL", "Qwen~Qwen3-Coder-Next-GGUF")
MAX_MODEL_CHARS = 24000
SMALL_RESIDUE = 3500          # collapsed residue this small IS the digest
KEEP_SPOOLS = 200

_NOISE = re.compile(
    r'("GET /(status|health)[^"]*" 200|"POST /(load|heartbeat)[^"]*" 200'
    r'|INFO:werkzeug|update_slots: all slots are idle|GET /llm/workers HTTP)')
_TS = re.compile(r"\b\d{2}:\d{2}:\d{2}[.,]?\d*\b|\b\d{4}-\d{2}-\d{2}[ T]?")

DIGEST_PROMPT = """You compress operational output for an engineer who will act on it.
Rules — these are hard requirements:
- Reproduce every ERROR, WARNING, refusal, traceback line and exit code VERBATIM.
- Reproduce every number, version, path, hash, port and model name EXACTLY.
- Then summarize the routine remainder in as few lines as possible.
- End with one line: "OMITTED: <what kinds of lines were compressed away, and how many>".
- No preamble, no commentary, no advice. Output the digest only.

INPUT ({nchars} chars{trunc}):
{body}"""


def _strip_think(s: str) -> str:
    # Local, dependency-free no-think seam: drop <think>…</think> blocks and any
    # dangling unterminated <think> tail (was the abstract_hugpy_dev fallback).
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.S | re.I)
    s = re.sub(r"<think>.*", "", s, flags=re.S | re.I)
    return s.strip()


def _collapse(text: str) -> str:
    """Noise-strip + timestamp-masked duplicate collapse. The spool keeps
    every line; this only shapes what a model (or the keeper) must read."""
    kept, dropped = [], 0
    counts: collections.Counter = collections.Counter()
    first: dict = {}
    order: list = []
    for l in text.splitlines():
        if _NOISE.search(l):
            dropped += 1
            continue
        key = _TS.sub("", l).strip()
        if not key:
            continue
        counts[key] += 1
        if key not in first:
            first[key] = l
            order.append(key)
    for key in order:
        n = counts[key]
        kept.append(first[key] + (f"   [x{n} occurrences]" if n > 1 else ""))
    collapsed = sum(n - 1 for n in counts.values())
    if dropped or collapsed:
        kept.append(f"[collapse: {dropped} status/access lines removed, "
                    f"{collapsed} repeats folded into x-counts; every line is "
                    f"in the spool]")
    return "\n".join(kept)


class LeanTools:
    def __init__(self, gateway, workspace: str):
        self.gw = gateway
        self.workspace = os.path.realpath(workspace)
        self.spool_dir = os.path.join(self.workspace, ".hugpy-lean-spool")

    # ── plumbing ─────────────────────────────────────────────────────────
    def _spool(self, text: str, slug: str) -> str:
        os.makedirs(self.spool_dir, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", slug)[:60] or "spool"
        path = os.path.join(self.spool_dir,
                            f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}.txt")
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            f.write(text)
        try:
            for old in sorted(os.listdir(self.spool_dir))[:-KEEP_SPOOLS]:
                os.unlink(os.path.join(self.spool_dir, old))
        except OSError:
            pass
        return path

    def _model_digest(self, text: str) -> str | None:
        body = text[-MAX_MODEL_CHARS:]
        trunc = (f", last {MAX_MODEL_CHARS} of {len(text)}"
                 if len(text) > MAX_MODEL_CHARS else "")
        prompt = DIGEST_PROMPT.format(nchars=len(text), trunc=trunc, body=body)
        prompt += " /no_think"   # local no-think seam (was abstract_hugpy_dev)
        try:
            out = self.gw.api_json("/api/prompt", method="POST", payload={
                "task": "text-generation", "model_key": DIGEST_MODEL,
                "prompt": prompt, "max_new_tokens": 700, "temperature": 0.0,
                "do_sample": False, "max_chunks": 1}, timeout=240)
        except Exception:
            return None
        for k in ("response", "text", "result", "output", "answer"):
            v = out.get(k) if isinstance(out, dict) else None
            if isinstance(v, str) and _strip_think(v):
                return _strip_think(v)
        return None

    def _digest_text(self, text: str, slug: str) -> str:
        path = self._spool(text, slug)
        if len(text) < 600:
            body = text
        else:
            filtered = _collapse(text)
            if len(filtered) <= SMALL_RESIDUE:
                body = filtered
            else:
                body = self._model_digest(filtered) or filtered[:SMALL_RESIDUE] \
                    + "\n[model digest unavailable — truncated collapse shown]"
        return f"{body}\n\n[full output spooled: {path}  ({len(text)} chars)]"

    # ── handlers (every return is a string; every failure is data) ──────
    def find(self, phrase: str, root: str = "dev", limit: int = 60) -> str:
        try:
            import urllib.parse
            d = self.gw.api_json(
                f"/api/finder/search?q={urllib.parse.quote(phrase)}"
                f"&root={urllib.parse.quote(root)}&limit={int(limit)}",
                timeout=120)
        except Exception as e:
            return f"finder error: {type(e).__name__}: {e}"
        if not isinstance(d, dict):
            return f"finder returned unexpected shape: {str(d)[:200]}"
        if d.get("error"):
            return f"finder error: {json.dumps(d)[:300]}"
        lines = []
        for h in d.get("hits") or []:
            lines.append(h.get("file_path", "?"))
            for l in (h.get("lines") or [])[:8]:
                lines.append(f"    {l.get('line')}: {str(l.get('content'))[:110]}")
        for s in d.get("skipped_subtrees") or []:
            lines.append(f"[skipped: {s.get('subtree')} — {str(s.get('error'))[:80]}]")
        lines.append(f"[{d.get('count')} file(s), root={d.get('root')}]")
        return "\n".join(lines) or "no matches"

    def digest(self, path: str = "", text: str = "") -> str:
        if not path and not text:
            return "error: give path or text"
        if path:
            from .fs import _confine
            try:
                real = _confine(self.workspace, path)
                text = open(real, errors="replace").read()
                slug = os.path.basename(real)
            except Exception as e:
                return f"read error: {type(e).__name__}: {e}"
        else:
            slug = "text"
        try:
            return self._digest_text(text, slug)
        except Exception as e:
            return f"digest error: {type(e).__name__}: {e}"

    def logs(self, unit: str, since: str = "1 hour ago") -> str:
        for scope in (["journalctl"], ["journalctl", "--user"]):
            try:
                p = subprocess.run(
                    scope + ["-u", unit, "--since", since, "--no-pager"],
                    capture_output=True, text=True, timeout=60,
                    env={**os.environ, "XDG_RUNTIME_DIR": "/run/user/1000"})
            except Exception as e:
                return f"journalctl error: {type(e).__name__}: {e}"
            if p.returncode == 0 and p.stdout.strip():
                break
        if not (p.stdout or "").strip():
            return f"no journal lines for unit {unit!r} since {since!r} " \
                   f"(stderr: {(p.stderr or '')[:150]})"
        try:
            return self._digest_text(p.stdout, f"logs-{unit}")
        except Exception as e:
            return f"digest error: {type(e).__name__}: {e}"

    def evictions(self, since_seconds: int = 600) -> str:
        try:
            d = self.gw.api_json(
                f"/api/llm/evictions?since={time.time() - int(since_seconds)}"
                f"&limit=500", timeout=60)
        except Exception as e:
            return f"telemetry error: {type(e).__name__}: {e}"
        events = (d or {}).get("events") or []
        if not events:
            return f"no eviction events in the last {since_seconds}s"
        lines = [f"{len(events)} eviction event(s) in the last {since_seconds}s:"]
        for e in events[-60:]:
            lines.append(json.dumps(
                {k: e.get(k) for k in ("ts", "worker_id", "model_key", "stage",
                                       "reason", "outcome", "evicted", "note")
                 if e.get(k) is not None})[:250])
        return "\n".join(lines)

    def deliver(self, path: str) -> str:
        from .fs import _confine
        try:
            real = _confine(self.workspace, path)
            os.makedirs(DELIVER_DIR, exist_ok=True)
            dest = os.path.join(DELIVER_DIR, os.path.basename(real))
            shutil.copy2(real, dest)
            return (f"delivered — operator path: {dest} "
                    f"({os.path.getsize(dest)} bytes). Reference this PATH in "
                    f"the reply; never paste the body.")
        except Exception as e:
            return f"deliver error: {type(e).__name__}: {e}"


def _p(**props) -> dict:
    required = props.pop("_required", [])
    return {"type": "object", "properties": props, "required": required}


def specs(gateway, workspace: str) -> list[ToolSpec]:
    lt = LeanTools(gateway, workspace)
    S = {"type": "string"}
    return [
        ToolSpec("lean_find",
                 "Locate data by phrase across the VM's trees via the central "
                 "finder (abstract-search). Returns ONLY file paths + matched "
                 "lines — use this BEFORE reading files to find things. "
                 "roots: dev|station|comms|spool.",
                 _p(phrase=S, root=S, limit={"type": "integer"},
                    _required=["phrase"]),
                 lt.find, RISK_READONLY),
        ToolSpec("lean_digest",
                 "Compress fat text/file into a short digest + spool path. "
                 "Nothing is lost: the full original is spooled to disk and "
                 "the digest names it. Use for any output too big to read raw.",
                 _p(path=S, text=S),
                 lt.digest, RISK_WRITE),
        ToolSpec("lean_logs",
                 "journalctl for a systemd unit, digested (noise-stripped, "
                 "repeats collapsed to x-counts, errors kept verbatim, full "
                 "log spooled). since e.g. '30 minutes ago'.",
                 _p(unit=S, since=S, _required=["unit"]),
                 lt.logs, RISK_WRITE),
        ToolSpec("lean_evictions",
                 "Structured eviction telemetry for the last N seconds "
                 "(model_key, stage, reason, outcome per event) — the "
                 "journal story of a call window, already parsed.",
                 _p(since_seconds={"type": "integer"}),
                 lt.evictions, RISK_READONLY),
        ToolSpec("lean_deliver",
                 "Hand a workspace file to the operator BY PATH on the share "
                 "instead of pasting its body into a reply.",
                 _p(path=S, _required=["path"]),
                 lt.deliver, RISK_WRITE),
    ]
