"""Per-model eval harness (design §7, plan P3.4): score a brain, don't vibe it.

A small suite of deterministic tasks is run against each candidate model
through the SAME agent loop the product uses (real journal, real tools, real
prompted tool-calling), and every model gets one row of a comparative
scorecard: {model, passed, steps_avg, tokens_avg, wall_avg, tool_accuracy}.
"Data, not vibes" is the whole point, so every checker is deterministic — an
artifact appeared on disk, the final answer contains a required fact, the run
finished under its step cap — never an LLM judging an LLM.

Live-readiness doctrine (hard-won on dev, 2026-07-15; see the ground-truth
memory): a worker that is DOWN or still LOADING answers `/v1/chat/completions`
with HTTP 200 whose *body* is an error string (`[error: … 404 NOT FOUND …]`),
and `/api/llm/serving/<key>` reports its configured `mode`, not live state.
So `model_ready()` NEVER gates on a status code or a serving flag: it gates on
a chat round-trip actually echoing an exact requested token. Warmups (a cold
model's first request) are handled by polite polling, not by trusting a 200.

The engine (this module) is packaged and unit-tested offline with a scripted
gateway; the operator-facing surface (task suite listing + a live runner that
writes scorecards) lives in the top-level `evals/` directory.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import Config
from .gateway import Gateway
from .journal import Journal
from .loop import AgentLoop, default_journal_path
from .memory import Memory

# A distinctive token the readiness ping asks the model to echo VERBATIM. It
# is deliberately not a real English word: the false-200 error body
# (`[error: … NOT FOUND …]`) can never contain it, so "token in reply" both
# proves the worker is really serving AND rules out the loading/down decoy.
READY_TOKEN = "HUGPY-EVAL-READY-7F"
READY_PROMPT = ("Reply with exactly this token and nothing else: %s"
                % READY_TOKEN)


# ── task specs ───────────────────────────────────────────────────────────
@dataclass
class EvalTask:
    """One scored task. `setup` seeds the fresh workspace (deterministic
    inputs); `check` is the deterministic pass predicate over the finished
    run. A pass ALSO requires outcome=="done" and steps<=step_cap (enforced
    by `run_task`), so `check` only asserts the task-specific fact/artifact.
    Small by design: tiny prompts + a low step cap + a small token budget keep
    live GPU cost bounded (plan P3.4: "bounded live cost")."""
    name: str
    prompt: str
    check: Callable[["CheckContext"], bool]
    setup: Callable[[str], None] | None = None
    step_cap: int = 6
    max_tokens: int = 512


@dataclass
class CheckContext:
    """What a checker sees: the workspace it ran in and the structured report.
    `answer` is hoisted for the common "output contains the required fact"
    check."""
    workspace: str
    report: dict
    answer: str = ""

    def file_text(self, rel: str) -> str | None:
        """Contents of a workspace-relative artifact, or None if absent."""
        path = os.path.join(self.workspace, rel)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return None


def file_contains(rel: str, needle: str) -> Callable[[CheckContext], bool]:
    """Checker: the artifact `rel` appeared and contains `needle` (case-
    insensitive — a small model's casing is not what we are scoring)."""
    def _check(ctx: CheckContext) -> bool:
        text = ctx.file_text(rel)
        return text is not None and needle.lower() in text.lower()
    return _check


def answer_contains(*needles: str) -> Callable[[CheckContext], bool]:
    """Checker: the final answer mentions every required fact (case-
    insensitive)."""
    def _check(ctx: CheckContext) -> bool:
        low = (ctx.answer or "").lower()
        return all(n.lower() in low for n in needles)
    return _check


def all_of(*checks: Callable[[CheckContext], bool]) -> Callable[[CheckContext], bool]:
    def _check(ctx: CheckContext) -> bool:
        return all(c(ctx) for c in checks)
    return _check


def _seed_files(files: dict[str, str]) -> Callable[[str], None]:
    def _setup(workspace: str) -> None:
        for rel, content in files.items():
            with open(os.path.join(workspace, rel), "w", encoding="utf-8") as fh:
                fh.write(content)
    return _setup


# The built-in suite. Four tasks spanning the tool surface an agent actually
# leans on — write an artifact, read+report a fact, aggregate over a glob, and
# a two-step read->transform->write — each with a deterministic checker and a
# tight step cap. Portable (shipped in the wheel) so `hugpy-agent eval` runs
# the same suite on any box; the top-level evals/tasks.py re-exports it as the
# operator-editable surface.
DEFAULT_TASKS: list[EvalTask] = [
    EvalTask(
        name="write_artifact",
        prompt=("Create a file named answer.txt whose entire contents are the "
                "single word BLACKBIRD (uppercase, nothing else). Then call "
                "final_answer naming the file you wrote."),
        check=file_contains("answer.txt", "BLACKBIRD"),
        step_cap=4,
    ),
    EvalTask(
        name="read_fact",
        prompt=("Read the file config.txt in the workspace and report the "
                "launch code it contains in your final answer."),
        setup=_seed_files({"config.txt": "system=hugpy\nlaunch_code=4471\n"}),
        check=answer_contains("4471"),
        step_cap=4,
    ),
    EvalTask(
        name="glob_count",
        prompt=("Count how many files ending in .log are in the workspace and "
                "state the exact count as a number in your final answer."),
        setup=_seed_files({"a.log": "x", "b.log": "y", "c.log": "z",
                           "notes.txt": "ignore me"}),
        check=answer_contains("3"),
        step_cap=5,
    ),
    EvalTask(
        name="read_transform_write",
        prompt=("Read input.txt, then write its text converted to UPPERCASE "
                "into a new file output.txt. Finish once output.txt exists."),
        setup=_seed_files({"input.txt": "hello world"}),
        check=file_contains("output.txt", "HELLO WORLD"),
        step_cap=6,
    ),
]


def task_by_name(name: str, tasks=None) -> EvalTask | None:
    for t in (tasks or DEFAULT_TASKS):
        if t.name == name:
            return t
    return None


# ── results ──────────────────────────────────────────────────────────────
@dataclass
class TaskResult:
    name: str
    passed: bool
    outcome: str
    steps: int
    est_tokens: int
    wall_s: float
    tool_calls: int
    tool_ok: int
    error: str = ""
    answer_excerpt: str = ""

    @property
    def tool_accuracy(self) -> float:
        return (self.tool_ok / self.tool_calls) if self.tool_calls else 1.0

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed,
                "outcome": self.outcome, "steps": self.steps,
                "est_tokens": self.est_tokens, "wall_s": round(self.wall_s, 2),
                "tool_calls": self.tool_calls, "tool_ok": self.tool_ok,
                "tool_accuracy": round(self.tool_accuracy, 3),
                "error": self.error, "answer_excerpt": self.answer_excerpt}


@dataclass
class Scorecard:
    """One model's row. `ready` records the readiness-gate verdict — a model
    that never became servable gets a row too (passed=0) so the blocker is in
    the data, not just a log line."""
    model: str
    ready: bool
    tasks: list = field(default_factory=list)
    ready_detail: str = ""
    worker: str = ""

    @property
    def passed(self) -> int:
        return sum(1 for t in self.tasks if t.passed)

    @property
    def total(self) -> int:
        return len(self.tasks)

    def _avg(self, attr: str) -> float:
        vals = [getattr(t, attr) for t in self.tasks]
        return (sum(vals) / len(vals)) if vals else 0.0

    @property
    def steps_avg(self) -> float:
        return self._avg("steps")

    @property
    def tokens_avg(self) -> float:
        return self._avg("est_tokens")

    @property
    def wall_avg(self) -> float:
        return self._avg("wall_s")

    @property
    def tool_accuracy(self) -> float:
        """Aggregate over ALL tool calls the model made across the suite
        (successful, non-error results / total) — a truer signal than the
        mean of per-task ratios, which would over-weight a one-call task."""
        calls = sum(t.tool_calls for t in self.tasks)
        ok = sum(t.tool_ok for t in self.tasks)
        return (ok / calls) if calls else 1.0

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "ready": self.ready,
            "ready_detail": self.ready_detail,
            "worker": self.worker,
            "passed": self.passed,
            "total": self.total,
            "steps_avg": round(self.steps_avg, 2),
            "tokens_avg": round(self.tokens_avg, 1),
            "wall_avg": round(self.wall_avg, 2),
            "tool_accuracy": round(self.tool_accuracy, 3),
            "tasks": [t.to_dict() for t in self.tasks],
        }


# ── readiness gate ───────────────────────────────────────────────────────
def _looks_like_error_body(text: str) -> bool:
    """The false-200 decoy: a 200 whose body is a serving error string. Used
    only for a clearer `ready_detail` — the real gate is token echo below."""
    low = (text or "").lower()
    return ("[error" in low or "not found" in low or "no worker" in low
            or "loading" in low)


def model_ready(gateway: Gateway, *, tries: int = 40, poll_interval: float = 20.0,
                token: str = READY_TOKEN, prompt: str = READY_PROMPT,
                max_tokens: int = 32, sleep=time.sleep,
                on_event=None) -> tuple[bool, str]:
    """Poll until the model echoes an exact token, or the try budget runs out.

    Returns (ready, detail). Gating on the ECHO — not on HTTP 200, not on a
    serving mode flag — is the whole doctrine: a down/loading worker returns a
    200 error body that can never contain our token. `tries * poll_interval`
    bounds the polite wait (default ~13min; the runner sets it from the
    operator's timeout budget). Cold-load first requests just take a few polls.
    """
    emit = on_event or (lambda *a, **k: None)
    last = ""
    for i in range(max(1, tries)):
        t0 = time.monotonic()
        try:
            res = gateway.chat([{"role": "user", "content": prompt}],
                               max_tokens=max_tokens, stream=False,
                               temperature=0.0)
            dt = time.monotonic() - t0
        except Exception as exc:  # noqa: BLE001 — readiness is best-effort data
            last = "chat raised: %s" % exc
            emit("eval_ready", "try %d/%d: %s" % (i + 1, tries, last))
            if i + 1 < tries:
                sleep(poll_interval)
            continue
        text = (res.text or "").strip()
        if res.ok and token in text:
            detail = "ready after %d attempt(s), ~%.1fs round-trip" % (i + 1, dt)
            emit("eval_ready", detail)
            return True, detail
        # Not ready: distinguish "loading decoy 200" from a real transport
        # error for a useful blocker report.
        if not res.ok:
            last = "not ok: %s" % (res.error or "unknown")
        elif _looks_like_error_body(text):
            last = "loading/serving error body: %r" % text[:160]
        else:
            last = "unexpected reply (token not echoed): %r" % text[:160]
        emit("eval_ready", "try %d/%d: %s" % (i + 1, tries, last))
        if i + 1 < tries:
            sleep(poll_interval)
    return False, "not ready after %d attempts; last: %s" % (tries, last)


def probe_worker(gateway: Gateway, model: str) -> str:
    """Best-effort: which worker is serving `model`, for the report ONLY (never
    a gate — serving flags lie about live state, per doctrine). Tries the
    serving-config route with the BARE model name (the `owner/…` id form only
    works on the /v1 seam), and scavenges any host/worker-ish field. Errors are
    swallowed to "": worker attribution is a nice-to-have, not a dependency."""
    bare = model.split("/")[-1]
    for path in ("/api/llm/serving/%s" % bare, "/api/llm/serving/%s" % model):
        try:
            data = gateway.api_json(path, timeout=15)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for key in ("worker", "worker_id", "node", "host", "server", "gpu",
                    "device", "backend"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        # Some deployments nest it under a serving/config block.
        for blob in (data.get("serving"), data.get("config"), data.get("state")):
            if isinstance(blob, dict):
                for key in ("worker", "node", "host", "server"):
                    val = blob.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
    return ""


# ── running one task ─────────────────────────────────────────────────────
def _tool_stats(journal: Journal, run_id: str) -> tuple[int, int]:
    """(total tool calls, non-error tool calls) for a run — the tool-accuracy
    numerator/denominator. `final_answer` never reaches the ledger (the loop
    intercepts it), so this counts only real tool executions. A call whose
    journaled status is anything but 'error' (i.e. 'done') counts as OK."""
    rows = journal.tool_call_rows(run_id)
    total = len(rows)
    ok = sum(1 for r in rows if r.get("status") != "error")
    return total, ok


def run_task(task: EvalTask, cfg: Config, *, gateway: Gateway | None = None,
             workspace: str | None = None, on_event=None, extra_tools=None) -> TaskResult:
    """Run one task through a real AgentLoop and score it deterministically.

    A fresh temp workspace (or the caller's) is seeded by `task.setup`, then
    driven with a per-task step cap. The pass predicate is strict-AND:
    outcome=="done" AND steps<=step_cap AND task.check(...) — the three
    deterministic gates from the P3.4 contract (finished, under the cap, and
    the artifact/fact is really there). `gateway` is injectable so tests drive
    a scripted model with no network.
    """
    emit = on_event or (lambda *a, **k: None)
    tmp = None
    if workspace is None:
        tmp = tempfile.TemporaryDirectory(prefix="hugpy-eval-")
        workspace = tmp.name
    try:
        ws = os.path.realpath(workspace)
        if task.setup:
            task.setup(ws)
        # Per-task config: the model under test, this task's caps, and an
        # eval-appropriate posture — policy auto (the eval operator has
        # consented to these bounded write tasks; the default `ask` would
        # deny every fs_write with no comms channel) and RAG off (no stray
        # embed round-trips; we score the brain, not the index).
        run_cfg = Config(**{**cfg.__dict__})
        run_cfg.sources = dict(cfg.sources)
        run_cfg.workspace = ws
        run_cfg.model = cfg.model
        run_cfg.max_steps = task.step_cap
        run_cfg.max_tokens = task.max_tokens
        run_cfg.policy_mode = "auto"
        run_cfg.rag_enabled = False
        gw = gateway or Gateway.from_config(run_cfg)
        journal = Journal(default_journal_path(ws))
        loop = AgentLoop(run_cfg, gateway=gw, journal=journal,
                         memory=Memory(ws), on_event=on_event)
        for _spec in (extra_tools or ()):   # e.g. the steward eval's gated vm.* surface
            loop.registry.register(_spec)
        t0 = time.monotonic()
        report = loop.run(task.prompt)
        wall = time.monotonic() - t0
        total_calls, ok_calls = _tool_stats(journal, report["run_id"])
        answer = report.get("answer", "") or ""
        ctx = CheckContext(workspace=ws, report=report, answer=answer)
        passed = (report.get("outcome") == "done"
                  and report.get("steps", 10 ** 9) <= task.step_cap
                  and _safe_check(task.check, ctx))
        result = TaskResult(
            name=task.name, passed=passed, outcome=report.get("outcome", "?"),
            steps=report.get("steps", 0), est_tokens=report.get("est_tokens", 0),
            wall_s=wall, tool_calls=total_calls, tool_ok=ok_calls,
            error=report.get("error", ""), answer_excerpt=answer[:200])
        journal.close()
        emit("eval_task", task.name, result.to_dict())
        return result
    finally:
        if tmp is not None:
            tmp.cleanup()


def _safe_check(check, ctx: CheckContext) -> bool:
    """A checker bug must not crash the harness — a raising checker is a
    failed task, reported as such."""
    try:
        return bool(check(ctx))
    except Exception:  # noqa: BLE001
        return False


# ── scoring one model / the whole scorecard ──────────────────────────────
def score_model(model: str, cfg: Config, tasks=None, *,
                gateway_factory: Callable[[str], Gateway] | None = None,
                ready_tries: int = 40, ready_poll: float = 20.0,
                gate_ready: bool = True, sleep=time.sleep,
                on_event=None) -> Scorecard:
    """Gate readiness (unless gate_ready=False), then run every task, returning
    the model's Scorecard row. `gateway_factory(model)` builds the client
    (injected in tests; live it's a real Gateway with the model + think knob).
    A model that never becomes ready gets a row with ready=False and no task
    results — the blocker is data."""
    emit = on_event or (lambda *a, **k: None)
    tasks = tasks or DEFAULT_TASKS

    def make_gw() -> Gateway:
        if gateway_factory is not None:
            return gateway_factory(model)
        c = Config(**{**cfg.__dict__})
        c.model = model
        return Gateway.from_config(c)

    emit("eval_model", model)
    card = Scorecard(model=model, ready=True)
    if gate_ready:
        ready, detail = model_ready(make_gw(), tries=ready_tries,
                                    poll_interval=ready_poll, sleep=sleep,
                                    on_event=on_event)
        card.ready = ready
        card.ready_detail = detail
        if not ready:
            emit("eval_model_blocked", model, detail)
            return card
        card.worker = probe_worker(make_gw(), model)
    for task in tasks:
        run_cfg = Config(**{**cfg.__dict__})
        run_cfg.sources = dict(cfg.sources)
        run_cfg.model = model
        gw = None if gateway_factory is None else gateway_factory(model)
        card.tasks.append(run_task(task, run_cfg, gateway=gw, on_event=on_event))
    return card


def run_scorecard(models: list[str], cfg: Config, tasks=None, *,
                  gateway_factory: Callable[[str], Gateway] | None = None,
                  ready_tries: int = 40, ready_poll: float = 20.0,
                  gate_ready: bool = True, sleep=time.sleep,
                  on_event=None) -> list[Scorecard]:
    """Score every model into a comparative list of Scorecards."""
    return [score_model(m, cfg, tasks, gateway_factory=gateway_factory,
                        ready_tries=ready_tries, ready_poll=ready_poll,
                        gate_ready=gate_ready, sleep=sleep, on_event=on_event)
            for m in models]


# ── rendering ────────────────────────────────────────────────────────────
_COLS = (("model", 42), ("ready", 5), ("worker", 10), ("passed", 7),
         ("steps", 7), ("tokens", 8), ("wall_s", 8), ("tool_acc", 8))


def format_table(cards: list[Scorecard]) -> str:
    """A small fixed-width comparative table (stdout-friendly, no deps)."""
    head = "  ".join(name.ljust(w) for name, w in _COLS)
    lines = [head, "  ".join("-" * w for _, w in _COLS)]
    for c in cards:
        cells = [
            c.model[:42].ljust(42),
            ("yes" if c.ready else "NO").ljust(5),
            (c.worker or "-")[:10].ljust(10),
            ("%d/%d" % (c.passed, c.total)).ljust(7),
            ("%.2f" % c.steps_avg).ljust(7),
            ("%.0f" % c.tokens_avg).ljust(8),
            ("%.2f" % c.wall_avg).ljust(8),
            ("%.2f" % c.tool_accuracy).ljust(8),
        ]
        lines.append("  ".join(cells))
    return "\n".join(lines)


def write_results(cards: list[Scorecard], out_dir: str) -> tuple[str, str]:
    """Persist the scorecard as JSON + a readable table. Returns (json_path,
    table_path). The JSON is the machine record; the table is the at-a-glance
    summary the plan asks for."""
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    payload = {
        "generated_utc": stamp,
        "cards": [c.to_dict() for c in cards],
    }
    json_path = os.path.join(out_dir, "scorecard-%s.json" % stamp)
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    table_path = os.path.join(out_dir, "scorecard-%s.txt" % stamp)
    with open(table_path, "w", encoding="utf-8") as fh:
        fh.write(format_table(cards) + "\n")
    return json_path, table_path
