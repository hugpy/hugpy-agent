"""Agent node mode (P3.2): the client half of central's `/agent/*` registry.

`hugpy-agent serve --node` turns a P2.7 daemon into a *fleet node*: it enrolls
with central once, heartbeats its liveness, and pulls operator-dispatched
tasks — the remote counterpart of the local task sources in serve.py, and a
client of the P3.1 blueprint (`comms/agent_nodes.py` + `routes/agent_routes.py`
in the abstract_hugpy_dev staging copy). The wire contract this targets, exactly:

  POST /agent/register  {name, host, capabilities}
      -> 201 {id, token, ...}   (the token is minted ONCE, returned here only;
      central stores just its sha256). When central's site API-key policy is on
      the call needs a console key as `Authorization: Bearer hp_...` — the same
      gate /v1 and /ml use — so we ride `cfg.api_key` on register. 401 => the
      key was required/invalid.
  POST /agent/<id>/heartbeat  {status, current_task, version}
      Node-token auth (`Authorization: Bearer agt_...`). 200 -> the node view.
  GET  /agent/<id>/tasks?since=<seq>
      Node-token auth. -> {tasks: [{seq, task, ...}], cursor, ...}; the pull is
      idempotent — advance `since` to the returned cursor.
  POST /agent/<id>/tasks/<seq>/result  {status, result}
      Node-token auth (P3.1b). Finalizes a pulled task: {status:"done"|"error",
      result:<string>}. 200 records it; 409 means it was already finalized
      (first-report-wins) — the node treats BOTH as "recorded". Central caps the
      result at 64 KiB (truncates, never rejects), so we send the FULL answer.

Fail-closed node states, honoured on every node-token call:
  * 410 unknown node  -> central has forgotten us: FORGET the creds and
    re-register (a fresh id+token), then carry on. A db reset on central must
    self-heal, not wedge the daemon.
  * 403 revoked       -> the operator killed this node: surfaced as data, we
    stop presenting the (now useless) token; a human must re-enroll.
  * 401 bad/missing   -> surfaced as data (should not happen with a good token).

Doctrines (the same the rest of the harness keeps):
  * errors-as-data — NO NodeClient method raises on a network or HTTP failure;
    each returns a {"ok": bool, ...} dict. The daemon can never die from an
    unreachable central (design §Ph3 risk: "central SPOF for nodes").
  * fail-closed + backoff lives in AgentNodeSource: when central is
    unreachable the source defers its next attempt with bounded exponential
    backoff (it simply does no central I/O until the window elapses — the
    daemon's own idle loop keeps the process alive/heartbeating to journalctl).
  * the enroll token is a SECRET: it is persisted only to the 0600 state file
    and is NEVER put in an event, a log line, or a returned dict.

A task's lifecycle rides two channels. Its in-flight signal is the heartbeat:
`busy` + current_task=<seq> when it is picked up, back to `idle` +
current_task="" when the batch drains. Its OUTCOME (P3.1b) rides the dedicated
result route: `report_result` POSTs {status, result} to
`/agent/<id>/tasks/<seq>/result`, transitioning the task queued → done/error and
landing the run's full answer/error on central (where the P3.3 operator panel
reads it). The result POST is what now anchors the at-least-once cursor advance —
the persisted cursor moves only once central has durably recorded the outcome.
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request

from . import __version__
from .config import Config

DEFAULT_CAPABILITIES = ["chat", "tools"]
STATE_NAME = "node_state.json"

# AgentNodeSource cadence / resilience knobs.
HEARTBEAT_INTERVAL = 30.0      # seconds between liveness heartbeats (~30s spec)
BACKOFF_START = 15.0           # first defer after central goes unreachable
BACKOFF_MAX = 60.0             # cap: never idle a node longer than 1 min


def default_state_path(workspace: str) -> str:
    """Where the persisted {id, token, cursor} live. Under the gitignored
    `.hugpy_agent/` state dir, written 0600 — the token is a secret."""
    return os.path.join(os.path.realpath(workspace), ".hugpy_agent", STATE_NAME)


def _normalize(base: str) -> str:
    return (base or "").strip().rstrip("/")


def _default_transport(url: str, method: str = "GET", payload=None,
                       timeout: int = 30, headers: dict | None = None):
    """One JSON request -> (status_code, parsed_body). Distinct from
    comms._default_transport: the node MUST see 401/403/410 to drive its
    fail-closed state machine, so an HTTP error status is DATA (returned as the
    code), not an exception. Only genuine transport failures (URLError without a
    code, socket timeout) raise — NodeClient turns those into errors-as-data."""
    data = None
    hdrs = {"Accept": "application/json"}
    hdrs.update(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode(errors="replace")
            status = getattr(resp, "status", 200) or 200
    except urllib.error.HTTPError as exc:      # 4xx/5xx: a status IS the answer
        try:
            raw = exc.read().decode(errors="replace")
        except Exception:
            raw = ""
        status = exc.code
    try:
        body = json.loads(raw) if raw.strip() else {}
    except ValueError:
        body = {}
    return status, body


class NodeClient:
    """Stateful client for ONE node's relationship with central. Holds the
    persisted identity ({id, token, cursor}) and speaks the M2M routes
    (register / heartbeat / pull / report_result). Every verb returns data —
    nothing raises out of here."""

    def __init__(self, cfg: Config, transport=None,
                 state_path: str | None = None, version: str | None = None):
        self.central = _normalize(getattr(cfg, "agent_central", "")
                                  or getattr(cfg, "base", ""))
        self.api_key = getattr(cfg, "api_key", "") or ""
        host = socket.gethostname() or "unknown-host"
        self.name = (getattr(cfg, "agent_name", "") or "").strip() or host
        self.host = host
        caps = list(getattr(cfg, "agent_capabilities", None) or [])
        self.capabilities = caps or list(DEFAULT_CAPABILITIES)
        self.transport = transport or _default_transport
        self.timeout = int(getattr(cfg, "timeout", 30) or 30)
        self.version = version or __version__
        self.state_path = (state_path
                           or (getattr(cfg, "agent_state", "") or "").strip()
                           or default_state_path(getattr(cfg, "workspace", ".")))
        self.node_id: str | None = None
        self.token: str | None = None      # SECRET — never logged/returned
        self.cursor = 0
        self._load_state()

    # ── persistence (0600; token is a secret) ────────────────────────────
    def _load_state(self) -> None:
        """Reuse a prior enrollment iff it targets the SAME central+name —
        pointing the daemon at a new central (or renaming the node) re-enrolls
        rather than presenting a token central never minted."""
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(st, dict):
            return
        if st.get("central") != self.central or st.get("name") != self.name:
            return                          # config drift: ignore stale creds
        nid, tok = st.get("id"), st.get("token")
        if nid and tok:
            self.node_id, self.token = str(nid), str(tok)
            try:
                self.cursor = int(st.get("cursor") or 0)
            except (TypeError, ValueError):
                self.cursor = 0

    def _save_state(self) -> None:
        """Persist {id, token, cursor} at mode 0600, created tight from the
        first byte (no world-readable window for the token)."""
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        payload = json.dumps({
            "central": self.central, "name": self.name,
            "id": self.node_id, "token": self.token, "cursor": self.cursor,
        }, indent=2)
        tmp = self.state_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.state_path)    # atomic; keeps the 0600 mode
        try:
            os.chmod(self.state_path, 0o600)
        except OSError:
            pass

    def _forget(self) -> None:
        """Drop the local identity (410/403). The next call re-registers."""
        self.node_id = self.token = None
        self.cursor = 0

    # ── auth headers ─────────────────────────────────────────────────────
    def _register_headers(self) -> dict | None:
        # Console API-key gate on /agent/register (P3.1 deviation #1) — rides
        # cfg.api_key exactly like /v1. Harmless when the site policy is off.
        return ({"Authorization": "Bearer %s" % self.api_key}
                if self.api_key else None)

    def _node_headers(self) -> dict:
        # The node's OWN enroll token — its M2M credential on every node route.
        return {"Authorization": "Bearer %s" % (self.token or "")}

    def enrolled(self) -> bool:
        return bool(self.node_id and self.token)

    # ── the three M2M verbs ──────────────────────────────────────────────
    def register(self) -> dict:
        """Bootstrap enrollment -> persist {id, token}. Errors-as-data."""
        url = self.central + "/agent/register"
        payload = {"name": self.name, "host": self.host,
                   "capabilities": self.capabilities}
        try:
            status, body = self.transport(url, method="POST", payload=payload,
                                          timeout=self.timeout,
                                          headers=self._register_headers())
        except Exception as exc:            # noqa: BLE001 — transport fault is data
            return {"ok": False, "error": "register: %s: %s"
                    % (type(exc).__name__, exc)}
        if status in (200, 201):
            nid, tok = (body or {}).get("id"), (body or {}).get("token")
            if not (nid and tok):
                return {"ok": False,
                        "error": "register: 2xx without id/token"}
            self.node_id, self.token, self.cursor = str(nid), str(tok), 0
            self._save_state()
            return {"ok": True, "id": self.node_id}
        if status == 401:
            return {"ok": False, "status": 401,
                    "error": "register: 401 (central API-key policy on; set "
                             "HUGPY_API_KEY to a console key)"}
        return {"ok": False, "status": status,
                "error": "register: HTTP %s" % status}

    def ensure_enrolled(self) -> dict:
        if self.enrolled():
            return {"ok": True, "id": self.node_id}
        return self.register()

    def heartbeat(self, status: str = "idle",
                  current_task: str | None = None) -> dict:
        """Report liveness. Self-heals on 410 (re-register). Errors-as-data."""
        pre = self.ensure_enrolled()
        if not pre["ok"]:
            return pre
        url = self.central + "/agent/%s/heartbeat" % self.node_id
        payload = {"status": status, "current_task": current_task,
                   "version": self.version}
        try:
            code, body = self.transport(url, method="POST", payload=payload,
                                        timeout=self.timeout,
                                        headers=self._node_headers())
        except Exception as exc:            # noqa: BLE001
            return {"ok": False, "error": "heartbeat: %s: %s"
                    % (type(exc).__name__, exc)}
        if code == 200:
            return {"ok": True, "node": body}
        if code == 410:                     # central forgot us -> re-enroll
            self._forget()
            reg = self.register()
            reg["reenrolled"] = True
            return reg
        if code == 403:                     # revoked by the operator
            self._forget()
            return {"ok": False, "status": 403,
                    "error": "heartbeat: 403 (node revoked; re-enroll needed)"}
        return {"ok": False, "status": code,
                "error": "heartbeat: HTTP %s" % code}

    def pull(self, since: int | None = None) -> dict:
        """Fetch tasks with seq > cursor. Returns {ok, tasks, cursor}. Self
        -heals on 410. Errors-as-data; does NOT advance the persisted cursor —
        the caller advances it as tasks are handed off / reported."""
        pre = self.ensure_enrolled()
        if not pre["ok"]:
            return dict(pre, tasks=[], cursor=self.cursor)
        s = self.cursor if since is None else int(since)
        url = self.central + "/agent/%s/tasks?since=%d" % (self.node_id, s)
        try:
            code, body = self.transport(url, method="GET",
                                        timeout=self.timeout,
                                        headers=self._node_headers())
        except Exception as exc:            # noqa: BLE001
            return {"ok": False, "tasks": [], "cursor": self.cursor,
                    "error": "pull: %s: %s" % (type(exc).__name__, exc)}
        if code == 200:
            tasks = list((body or {}).get("tasks") or [])
            cursor = (body or {}).get("cursor", s)
            try:
                cursor = int(cursor)
            except (TypeError, ValueError):
                cursor = s
            return {"ok": True, "tasks": tasks, "cursor": cursor}
        if code == 410:
            self._forget()
            reg = self.register()
            return {"ok": reg["ok"], "tasks": [], "cursor": self.cursor,
                    "reenrolled": True,
                    **({"error": reg["error"]} if not reg["ok"] else {})}
        if code == 403:
            self._forget()
            return {"ok": False, "tasks": [], "cursor": self.cursor,
                    "status": 403,
                    "error": "pull: 403 (node revoked; re-enroll needed)"}
        return {"ok": False, "tasks": [], "cursor": self.cursor,
                "status": code, "error": "pull: HTTP %s" % code}

    def report_result(self, seq: int, status: str = "done",
                      result: str = "") -> dict:
        """Report a pulled task's OUTCOME to central's P3.1b result route
        (mirrors heartbeat/pull: node-token auth, errors-as-data, 410 self
        -heal). POSTs {status, result} to /agent/<id>/tasks/<seq>/result; the
        FULL result is sent — central caps it at 64 KiB (truncates, never
        rejects) and records only the FIRST report.

        Response mapping — `recorded` means central durably persisted the
        outcome, which is what the source gates its cursor advance on:
          * 200 or 409 -> {ok:True,  recorded:True,  status:code}
            (409 = already finalized; first-report-wins, so a crash-retry re
            -post is safe + idempotent — both codes mean "recorded")
          * 410        -> _forget() + re-register (the heartbeat self-heal path);
            {ok:reg.ok, recorded:False, reenrolled:True}
          * 403        -> revoked: drop the dead token, surfaced as data
          * 404        -> central has no such task (recorded:False)
          * 401/other/transport fault -> errors-as-data, recorded:False."""
        pre = self.ensure_enrolled()
        if not pre["ok"]:
            return dict(pre, recorded=False)
        url = self.central + "/agent/%s/tasks/%s/result" % (self.node_id, seq)
        payload = {"status": status, "result": result}
        try:
            code, body = self.transport(url, method="POST", payload=payload,
                                        timeout=self.timeout,
                                        headers=self._node_headers())
        except Exception as exc:            # noqa: BLE001
            return {"ok": False, "recorded": False,
                    "error": "report_result: %s: %s"
                    % (type(exc).__name__, exc)}
        if code in (200, 409):              # done, OR already-finalized: recorded
            return {"ok": True, "recorded": True, "status": code}
        if code == 410:                     # central forgot us -> re-enroll
            self._forget()
            reg = self.register()
            return {"ok": reg["ok"], "recorded": False, "reenrolled": True,
                    **({"error": reg["error"]} if not reg["ok"] else {})}
        if code == 403:                     # revoked by the operator
            self._forget()
            return {"ok": False, "recorded": False, "status": 403,
                    "error": "report_result: 403 (node revoked; re-enroll "
                             "needed)"}
        if code == 404:                     # central doesn't know this task
            return {"ok": False, "recorded": False, "status": 404,
                    "error": "report_result: 404 (central has no such task)"}
        return {"ok": False, "recorded": False, "status": code,
                "error": "report_result: HTTP %s" % code}

    def set_cursor(self, seq: int) -> None:
        """Advance + persist the pull cursor (called after a task is reported,
        so a crash re-pulls only UN-reported tasks — at-least-once)."""
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            return
        if seq > self.cursor:
            self.cursor = seq
            if self.enrolled():
                self._save_state()


def task_text(task) -> str:
    """A dispatched task's payload is operator-defined JSON (P3.1 stores it
    opaquely). Map the common shapes to the task STRING that Loop.run wants;
    anything else round-trips as JSON so no task is ever silently dropped."""
    if isinstance(task, str):
        return task
    if isinstance(task, dict):
        for key in ("prompt", "task", "text", "instruction"):
            v = task.get(key)
            if isinstance(v, str) and v.strip():
                return v
    return json.dumps(task)


class AgentNodeSource:
    """serve.py task source backed by a NodeClient: enroll -> heartbeat ->
    pull -> run (via the daemon's loop) -> POST the outcome to central's result
    route -> beat back to idle.

    Slots into Daemon exactly like QueueFileSource / DiscordInboxSource — but
    it also owns the ~30s heartbeat cadence and the fail-closed backoff, using
    an injectable monotonic clock (the daemon's fake clock in tests). It NEVER
    raises: an unreachable central just defers the next attempt."""

    name = "agent-node"

    def __init__(self, client: NodeClient, monotonic=None):
        import time
        self.client = client
        self.monotonic = monotonic or time.monotonic
        self._last_beat: float | None = None
        self._next_attempt = 0.0
        self._backoff = 0.0
        # In-memory high-water of what we've PULLED (so we don't re-pull the
        # same tasks next cycle). Distinct from client.cursor, which is the
        # PERSISTED position and advances only when a task is reported done —
        # that split is what makes delivery at-least-once across a crash.
        self._pulled = 0
        # tasks pulled this cycle, awaiting report(): [(seq, task_text)].
        self._inflight: list[tuple[int, str]] = []
        self.last_error: str | None = None

    # ── backoff bookkeeping ──────────────────────────────────────────────
    def _fail(self, now: float, error: str) -> None:
        self.last_error = error
        self._backoff = min(max(BACKOFF_START, self._backoff * 2), BACKOFF_MAX)
        self._next_attempt = now + self._backoff

    def _ok(self) -> None:
        self.last_error = None
        self._backoff = 0.0
        self._next_attempt = 0.0

    def _beat(self, now: float, status: str,
              current_task: str | None) -> bool:
        res = self.client.heartbeat(status=status, current_task=current_task)
        if res.get("ok"):
            self._last_beat = now
            return True
        self._fail(now, res.get("error") or "heartbeat failed")
        return False

    # ── the source protocol ──────────────────────────────────────────────
    def poll(self) -> list:
        now = self.monotonic()
        if now < self._next_attempt:        # inside a backoff window: defer
            return []
        # 1) make sure we have an identity (register once, or re-register).
        pre = self.client.ensure_enrolled()
        if not pre.get("ok"):
            self._fail(now, pre.get("error") or "enroll failed")
            return []
        # A re-enroll (410 self-heal) mints a NEW id with a fresh queue, so the
        # in-memory pull high-water must reset — the persisted cursor already
        # did (in _forget).
        if getattr(self, "_node_id", None) != self.client.node_id:
            self._node_id = self.client.node_id
            self._pulled = self.client.cursor
        # 2) periodic liveness beat (idle) when nothing is in flight. NB the
        # P3.1 heartbeat treats a NULL current_task as "leave unchanged" — only
        # a non-null value writes — so an EMPTY string is what actually clears a
        # stale busy marker back to "no task".
        if self._last_beat is None or now - self._last_beat >= HEARTBEAT_INTERVAL:
            if not self._beat(now, "idle", ""):
                return []
        # 3) pull the queue from the highest seq we've already pulled (or the
        # persisted cursor on a fresh start / after a re-enroll).
        since = max(self.client.cursor, self._pulled)
        res = self.client.pull(since=since)
        if not res.get("ok"):
            self._fail(now, res.get("error") or "pull failed")
            return []
        self._ok()
        tasks = res.get("tasks") or []
        if not tasks:
            return []
        self._inflight = [(int(t.get("seq") or 0), task_text(t.get("task")))
                          for t in tasks]
        # Advance the in-memory pull high-water so we don't re-offer the same
        # tasks next cycle; the PERSISTED cursor only moves in report().
        self._pulled = max(self._pulled, int(res.get("cursor") or 0))
        # 4) flip to busy so central sees the node working on the first task.
        first_id = str(self._inflight[0][0])
        self._beat(now, "busy", first_id)
        return [text for _, text in self._inflight]

    def report(self, task: str, report: dict) -> None:
        """A task finished. POST its outcome to central's P3.1b result route,
        then gate the at-least-once cursor advance on central having RECORDED
        it, and — when the batch is drained — beat back to idle. The outcome
        also rides on_event + the journal as before; the result route is what
        lands the run's full answer on central for the P3.3 operator panel."""
        seq = None
        for i, (s, text) in enumerate(self._inflight):
            if text == task:
                seq = s
                self._inflight.pop(i)
                break
        if seq is None and self._inflight:
            seq, _ = self._inflight.pop(0)  # order fallback
        if seq is not None:
            # Report the outcome to central BEFORE advancing the cursor — the
            # at-least-once boundary now anchors on central having PERSISTED
            # the result, not just local bookkeeping.
            status = "done" if report.get("outcome") == "done" else "error"
            # the FULL answer/error, NOT format_reply — that clips to the
            # 1900-char discord limit; central caps at 64 KiB and truncates.
            body = (report.get("answer") if status == "done"
                    else report.get("error"))
            res = self.client.report_result(seq, status=status,
                                            result=body or "")
            if res.get("recorded") or res.get("status") == 404:
                # recorded (200/409), OR futile to retry (404 = the task
                # vanished server-side): advance + persist the cursor.
                self.client.set_cursor(seq)
            # else: leave the cursor un-advanced so a crash-restart re-pulls +
            # re-reports the task (at-least-once, gated on central persistence).
        if not self._inflight:
            # Batch drained: clear the busy marker (empty, not null — see poll).
            self._beat(self.monotonic(), "idle", "")


class MultiSource:
    """Poll several task sources in one daemon cycle (the `--node` +
    `--task-source` "alongside" case). Tasks are concatenated in source order;
    a completion report is routed back to the source that produced it (the
    daemon reports in the same order poll() returned, so a first-match by task
    text is exact)."""

    name = "multi"

    def __init__(self, sources: list):
        self.sources = list(sources)
        self._route: list[tuple] = []       # [(source, task)] this cycle

    def poll(self) -> list:
        self._route = []
        tasks: list = []
        for src in self.sources:
            for task in (src.poll() or []):
                self._route.append((src, task))
                tasks.append(task)
        return tasks

    def report(self, task: str, report: dict) -> None:
        for i, (src, t) in enumerate(self._route):
            if t == task:
                self._route.pop(i)
                src.report(task, report)
                return
        if self.sources:                    # fallback: hand to the first source
            self.sources[0].report(task, report)
