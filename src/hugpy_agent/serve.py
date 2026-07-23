"""The `serve` daemon (P2.7): poll a task source, run each task, report.

This is what the systemd user unit runs (`ExecStart=... serve`, see
install.py). The daemon is a thin scheduler around the existing loop — one
task at a time, each via a FRESH AgentLoop (journal/policy/audit all apply
exactly as for a CLI run), outcome reported through `on_event` and, for the
discord-inbox source, replied into the operator channel.

Task sources (config `HUGPY_TASK_SOURCE`):

  discord-inbox   Poll the operator session's `GET <session>/messages` (the
                  same endpoint + transport style as comms.py / ask_operator).
                  CONVENTION: an inbound message whose content starts with
                  `task:` (case-insensitive) is a task; the remainder of the
                  message is the task text. The run outcome is replied via
                  `POST <session>/send` as
                  `task finished (run <id>): outcome=<o> steps=<n>\\n<answer|error>`
                  clipped to the 1900-char wire limit. The FIRST poll after
                  startup only sets the `since` watermark and executes
                  NOTHING — a daemon restart must never replay historical
                  `task:` messages (tasks are side-effectful; fail closed —
                  re-send a task that landed while the daemon was down).

  queue           A local file (HUGPY_TASK_QUEUE, default
                  <workspace>/.hugpy_agent/tasks.queue), one task per line,
                  '#' comments and blank lines ignored. ONE task is consumed
                  per poll cycle, atomically: the remaining lines are written
                  to a temp file and os.replace()d over the queue, so a crash
                  mid-consume leaves either the old or the new queue, never a
                  torn one. Append lines to dispatch work.

  (empty/unknown) FAIL CLOSED: the daemon idles, emitting a periodic
                  heartbeat event, and never crashes — a misconfigured box
                  must be visibly alive and doing nothing, not doing
                  something nobody configured.

Doctrines:
  * errors-as-data — a failing poll, task run, or reply becomes a
    `serve_error` event and the daemon carries on; nothing raises out of
    Daemon.run().
  * graceful stop — `stop_requested` (set by the CLI's SIGTERM/SIGINT
    handler) is checked between tasks and between cycles only: the current
    task always finishes and the journal stays consistent, then run()
    returns and the process exits 0 (Restart=on-failure leaves it stopped).
  * everything time-like is injectable (sleep/monotonic) so tests run the
    schedule instantly and offline.
  * the session URL embeds the token — it is never logged or included in
    events (same posture as comms.py).
"""
from __future__ import annotations

import os
import time

from .comms import MAX_CONTENT_CHARS, _default_transport
from .config import Config

SOURCE_DISCORD = "discord-inbox"
SOURCE_QUEUE = "queue"
TASK_PREFIX = "task:"          # discord-inbox: inbound messages that ARE tasks
HEARTBEAT_EVERY = 60.0         # seconds between heartbeat events
DEFAULT_QUEUE_NAME = "tasks.queue"


def default_queue_path(workspace: str) -> str:
    return os.path.join(os.path.realpath(workspace), ".hugpy_agent",
                        DEFAULT_QUEUE_NAME)


def format_reply(report: dict) -> str:
    """The completion message sent back to the operator channel. The body is
    the answer on success, the error otherwise — clipped to the same wire
    limit comms.py enforces (central 413s above it)."""
    outcome = report.get("outcome", "?")
    body = (report.get("answer") if outcome == "done"
            else report.get("error")) or ""
    text = ("task finished (run %s): outcome=%s steps=%s\n%s"
            % (report.get("run_id", "?"), outcome,
               report.get("steps", "?"), body)).strip()
    return text[:MAX_CONTENT_CHARS]


class QueueFileSource:
    """Local task queue: one task per line. poll() consumes AT MOST ONE task
    per call (bounded work per cycle; the rest keep their place in line)."""

    name = SOURCE_QUEUE

    def __init__(self, path: str):
        self.path = path

    def poll(self) -> list:
        try:
            with open(self.path, encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            return []                      # no queue file yet: nothing to do
        task = None
        rest = []
        for line in lines:
            text = line.strip()
            if task is None and text and not text.startswith("#"):
                task = text
            else:
                rest.append(line)
        if task is None:
            return []
        # Atomic consume: rewrite-then-replace, so a crash between the read
        # and the replace re-offers the SAME task (at-least-once) rather than
        # losing it or leaving a torn file.
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(rest)
        os.replace(tmp, self.path)
        return [task]

    def report(self, task: str, report: dict) -> None:
        # Outcomes ride on_event + the journal (`hugpy-agent runs`); the
        # queue file itself carries only pending work.
        return None


class DiscordInboxSource:
    """Operator-channel inbox: the serve-side sibling of comms.py, reusing
    its transport and the session /messages + /send wire contract."""

    name = SOURCE_DISCORD

    def __init__(self, session_url: str, transport=None, timeout: int = 30):
        self.session_url = (session_url or "").strip().rstrip("/")
        self.transport = transport or _default_transport
        self.timeout = timeout
        self.since = 0.0
        self.primed = False    # first poll only sets the watermark (no replay)

    def poll(self) -> list:
        data = self.transport(
            "%s/messages?since=%s" % (self.session_url, self.since),
            method="GET", timeout=self.timeout)
        tasks = []
        for m in (data or {}).get("messages") or []:
            ts = float(m.get("ts") or 0.0)
            if ts > self.since:
                self.since = ts            # advance past chatter too
            if m.get("direction") != "in":
                continue                   # our own outbound echoes
            content = str(m.get("content") or "").strip()
            if content.lower().startswith(TASK_PREFIX):
                task = content[len(TASK_PREFIX):].strip()
                if task:
                    tasks.append(task)
        if not self.primed:
            # Startup baseline: history is watermarked, never executed. A
            # restart replaying every past `task:` message would re-run side
            # effects nobody asked for twice (fail closed).
            self.primed = True
            return []
        return tasks

    def report(self, task: str, report: dict) -> None:
        self.transport(self.session_url + "/send", method="POST",
                       payload={"content": format_reply(report)},
                       timeout=self.timeout)


def _make_task_source(cfg: Config, transport=None):
    """The local `HUGPY_TASK_SOURCE` (queue | discord-inbox), or (None, note)
    when unconfigured/unknown — an unknown name or a half-configured source must
    idle loudly, not guess."""
    name = (getattr(cfg, "task_source", "") or "").strip().lower()
    if not name:
        return None, ("no task source configured (set HUGPY_TASK_SOURCE to "
                      "'discord-inbox' or 'queue'); idling")
    if name == SOURCE_QUEUE:
        path = getattr(cfg, "task_queue", None) or \
            default_queue_path(cfg.workspace)
        return QueueFileSource(path), "queue file %s" % path
    if name == SOURCE_DISCORD:
        session = (getattr(cfg, "discord_session", "") or "").strip()
        if not session:
            return None, ("task source 'discord-inbox' needs "
                          "HUGPY_DISCORD_SESSION; idling")
        return (DiscordInboxSource(session, transport=transport,
                                   timeout=getattr(cfg, "timeout", 30)),
                "discord-inbox (operator session)")
    return None, "unknown task source %r; idling" % name


def make_node_source(cfg: Config, transport=None, monotonic=None):
    """The P3.2 agent-node source (register/heartbeat/pull against central's
    /agent/*). Returns (source_or_None, note). The node needs a central URL;
    `agent_central` falls back to `base` (the /api dual-mount serves /agent
    there), so this is usable whenever a base is set."""
    from .node import AgentNodeSource, NodeClient
    central = (getattr(cfg, "agent_central", "") or ""
               or getattr(cfg, "base", "") or "").strip()
    if not central:
        return None, ("agent node mode needs HUGPY_AGENT_CENTRAL (or HUGPY_BASE); "
                      "node disabled")
    client = NodeClient(cfg, transport=transport)
    return (AgentNodeSource(client, monotonic=monotonic),
            "agent-node -> %s (as %r)" % (client.central, client.name))


def make_source(cfg: Config, transport=None, monotonic=None):
    """Resolve the effective source for the daemon. Composes the local task
    source (`HUGPY_TASK_SOURCE`) with the agent-node source (`--node` /
    `HUGPY_AGENT_NODE`): the node runs INSTEAD OF the local source (node only),
    ALONGSIDE it (a MultiSource polling both), or neither runs and the daemon
    fails closed to an idle heartbeat. Returns (source_or_None, note)."""
    base_src, base_note = _make_task_source(cfg, transport)
    if not getattr(cfg, "agent_node", False):
        return base_src, base_note
    node_src, node_note = make_node_source(cfg, transport, monotonic=monotonic)
    if node_src is None:                     # --node but no central: keep base
        return base_src, "%s; %s" % (base_note, node_note)
    if base_src is None:                     # node only (no/idle local source)
        return node_src, node_note
    from .node import MultiSource            # both: poll alongside each other
    return MultiSource([base_src, node_src]), "%s + %s" % (base_note, node_note)


class Daemon:
    """The serve loop. `loop_factory` builds the runner for ONE task (default
    a fresh AgentLoop — constructed lazily, so an idle daemon touches no
    gateway/registry at all); tests inject a stub. `max_cycles` bounds the
    loop for tests and smoke runs; None (the default) runs until stopped."""

    def __init__(self, cfg: Config, source=None, source_note: str | None = None,
                 loop_factory=None, on_event=None, sleep=None, monotonic=None,
                 transport=None):
        self.cfg = cfg
        self.monotonic = monotonic or time.monotonic
        if source is None and source_note is None:
            # Share the daemon's clock with a node source so its ~30s heartbeat
            # cadence and backoff move on the same (injectable) time base.
            source, source_note = make_source(cfg, transport=transport,
                                              monotonic=self.monotonic)
        self.source = source
        self.source_note = source_note or getattr(source, "name", "custom")
        self.loop_factory = loop_factory or self._default_loop_factory
        self.on_event = on_event or (lambda *a, **k: None)
        self.sleep = sleep or time.sleep
        self.interval = max(1, int(getattr(cfg, "poll_interval", 10) or 10))
        self.stop_requested = False

    def _default_loop_factory(self):
        from .loop import AgentLoop         # deferred: idle serve stays cheap
        return AgentLoop(self.cfg, on_event=self.on_event)

    # ── the daemon loop ──────────────────────────────────────────────────
    def run(self, max_cycles: int | None = None) -> dict:
        """Poll → run → report until stopped (or max_cycles). Returns a
        summary dict; never raises."""
        self.on_event("serve", self.source_note, self.interval)
        cycles = 0
        tasks_run = 0
        errors = 0
        last_beat = None
        while not self.stop_requested:
            cycles += 1
            tasks = []
            if self.source is not None:
                try:
                    tasks = list(self.source.poll() or [])
                except Exception as exc:  # noqa: BLE001 — poll must not kill the daemon
                    errors += 1
                    self.on_event("serve_error", "poll failed: %s: %s"
                                  % (type(exc).__name__, exc))
            for task in tasks:
                tasks_run += 1
                self.on_event("task_start", task)
                try:
                    report = self.loop_factory().run(task)
                except Exception as exc:  # noqa: BLE001 — a broken run is data
                    errors += 1
                    report = {"outcome": "aborted",
                              "error": "serve: task crashed: %s: %s"
                                       % (type(exc).__name__, exc)}
                    self.on_event("serve_error", report["error"])
                self.on_event("task_done", report)
                try:
                    self.source.report(task, report)
                except Exception as exc:  # noqa: BLE001 — reply failure is not run failure
                    errors += 1
                    self.on_event("serve_error", "report failed: %s: %s"
                                  % (type(exc).__name__, exc))
                if self.stop_requested:
                    break                  # finish THIS task, skip the rest
            now = self.monotonic()
            if last_beat is None or now - last_beat >= HEARTBEAT_EVERY:
                # Liveness for journalctl — especially the fail-closed idle
                # mode, where the heartbeat is the only sign of life.
                last_beat = now
                self.on_event("heartbeat",
                              "%s | cycles=%d tasks=%d errors=%d"
                              % (self.source_note, cycles, tasks_run, errors))
            if self.stop_requested:
                break
            if max_cycles is not None and cycles >= max_cycles:
                break
            self.sleep(self.interval)
        summary = {"cycles": cycles, "tasks_run": tasks_run, "errors": errors,
                   "stopped": self.stop_requested}
        self.on_event("serve_exit", summary)
        return summary
