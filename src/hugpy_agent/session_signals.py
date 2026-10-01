"""Client-declared session state for hugpy central (identity + lease + transitions).

Central used to GUESS a session's state from socket liveness and job ages, so a
long prefill looked stuck and a crashed client's job sat "pending" forever. This
module lets the client SAY it, over names central already reads:

  * Identity on every hugpy call — the ``X-Hugpy-Client-*`` headers central's
    call log already parses (hugpy_control/calllog.py ``_request_context``):
    Session, Turn, Request, Process, Pid, User, Task, Platform, plus
    ``X-Hugpy-Client`` (v1_helpers ``_derive_caller``). No parallel names.
  * Lease — while a turn is open the client renews
    ``POST <api>/llm/sessions/<sid>/lease`` every LEASE_INTERVAL s (TTL
    LEASE_TTL s) with ``{state, turn_id, request_ids, ...}``. Central abandons
    (and cancels, through its authoritative cancel path) the in-flight jobs of
    a session whose lease lapses, and never cancels one whose lease is fresh.
  * Transitions — the same route carries ``event``: ``turn_start`` /
    ``turn_done`` (state idle) / ``session_closed`` (atexit, best effort — the
    lease covers crashes); a user abort cancels the request through
    ``POST <api>/llm/jobs/<client request id>/cancel``.

Everything here is best effort and silent: an old central answers 404/405 on
the lease route, which switches the lease off for that endpoint for the rest
of the process; the identity headers are simply ignored there. No call ever
raises into a model call because of this module.

The ONE in-process choke point is ``Gateway.chat`` (gateway.py) — every hugpy
model call (agent loop, B lookups, probes, evals, the Station FleetGateway)
goes through it. Harness traffic (OpenCode, Claude Code, Qwen Code, Hermes)
never touches our Gateway, so ``harness_env`` / ``HarnessLease`` give the
launcher the same identity + lease for the harness process it starts.
"""
from __future__ import annotations

import atexit
import contextlib
import getpass
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

LEASE_INTERVAL = 10.0
LEASE_TTL = 30
POST_TIMEOUT = 5.0

# Environment contract (child processes inherit the launch's session).
ENV_SESSION = "HUGPY_CLIENT_SESSION"
ENV_PROCESS = "HUGPY_CLIENT_PROCESS"
ENV_PID = "HUGPY_CLIENT_PID"
ENV_USER = "HUGPY_CLIENT_USER"
ENV_PLATFORM = "HUGPY_CLIENT_PLATFORM"
ENV_NAME = "HUGPY_CLIENT_NAME"
ENV_TASK = "HUGPY_CLIENT_TASK"
ENV_LEASE_OWNER = "HUGPY_CLIENT_LEASE_OWNER"
ENV_DISABLE = "HUGPY_SESSION_SIGNALS"          # "0"/"off" -> no headers, no lease

# Header names central already reads (calllog._request_context, _derive_caller).
H_CLIENT = "X-Hugpy-Client"
H_SESSION = "X-Hugpy-Client-Session"
H_TURN = "X-Hugpy-Client-Turn"
H_REQUEST = "X-Hugpy-Client-Request"
H_PROCESS = "X-Hugpy-Client-Process"
H_PID = "X-Hugpy-Client-Pid"
H_USER = "X-Hugpy-Client-User"
H_TASK = "X-Hugpy-Client-Task"
H_PLATFORM = "X-Hugpy-Client-Platform"

# The launch-scoped (static) headers a harness can carry, and the env var each
# one reads — config-file harnesses reference the env, never a literal.
HARNESS_HEADER_ENV = (
    (H_CLIENT, ENV_NAME),
    (H_SESSION, ENV_SESSION),
    (H_PROCESS, ENV_PROCESS),
    (H_PID, ENV_PID),
    (H_USER, ENV_USER),
    (H_PLATFORM, ENV_PLATFORM),
)


def enabled(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(ENV_DISABLE, "1").strip().lower() not in ("0", "off", "false", "no")


def new_id(prefix: str) -> str:
    return "%s-%s" % (prefix, uuid.uuid4().hex[:20])


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return ""


def _process_name() -> str:
    argv0 = os.path.basename(sys.argv[0] or "") if sys.argv else ""
    return "hugpy-agent" + (":" + argv0 if argv0 and argv0 != "hugpy-agent" else "")


def ensure_session_env(environ=None) -> str:
    """The launch's session id, minted once and exported so children share it."""
    env = os.environ if environ is None else environ
    sid = (env.get(ENV_SESSION) or "").strip()
    if not sid:
        sid = new_id("ha")
        env[ENV_SESSION] = sid
    return sid


def api_prefix(url: str) -> str:
    """The hugpy API root for a chat/models URL or a configured base:
    ``https://h/api/v1/chat/completions`` -> ``https://h/api``; a base without
    a ``/v1`` segment is taken as the root itself (``https://h/api``)."""
    u = (url or "").strip().rstrip("/")
    if "://" not in u and u:
        u = "https://" + u
    parts = urllib.parse.urlsplit(u)
    path = parts.path
    i = path.find("/v1/")
    if i >= 0:
        path = path[:i]
    elif path.endswith("/v1"):
        path = path[:-3]
    return "%s://%s%s" % (parts.scheme, parts.netloc, path.rstrip("/"))


def lease_url(prefix: str, session_id: str) -> str:
    return "%s/llm/sessions/%s/lease" % (prefix.rstrip("/"),
                                         urllib.parse.quote(session_id, safe=""))


def cancel_url(prefix: str, request_id: str) -> str:
    return "%s/llm/jobs/%s/cancel" % (prefix.rstrip("/"),
                                      urllib.parse.quote(request_id, safe=""))


def post_json(url: str, body: dict, key: str = "", timeout: float = POST_TIMEOUT) -> str:
    """'ok' | 'unsupported' (404/405 — an older central) | 'error'. Never raises."""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return "ok"
    except urllib.error.HTTPError as exc:
        return "unsupported" if exc.code in (404, 405) else "error"
    except Exception:  # noqa: BLE001 — transport trouble is data, never a crash
        return "error"


class _Request:
    """One in-flight model call: its identity headers and an abort hook."""

    def __init__(self, sig: "SessionSignals", endpoint, turn_id: str, request_id: str):
        self._sig = sig
        self._endpoint = endpoint
        self.turn_id = turn_id
        self.request_id = request_id
        self.headers = sig.headers(turn_id=turn_id, request_id=request_id)

    def cancel(self, reason: str = "client abort") -> str:
        prefix, key = self._endpoint
        return self._sig.poster(cancel_url(prefix, self.request_id),
                                {"reason": reason, "session_id": self._sig.session_id,
                                 "client_request": self.request_id}, key)


class SessionSignals:
    """Process-wide session: open turns, in-flight requests, one lease thread."""

    def __init__(self, session_id: str | None = None, process: str | None = None,
                 platform: str = "hugpy-agent", name: str = "hugpy-agent",
                 interval: float = LEASE_INTERVAL, ttl: int = LEASE_TTL,
                 poster=None, register_atexit: bool = True):
        self.session_id = session_id or ensure_session_env()
        self.process = process or os.environ.get(ENV_PROCESS) or _process_name()
        self.platform = platform
        self.name = name
        self.pid = os.getpid()
        self.user = _user()
        self.host = socket.gethostname()
        self.interval = interval
        self.ttl = ttl
        self.poster = poster or post_json
        self._register_atexit = register_atexit
        self._lock = threading.RLock()
        self._local = threading.local()
        self._turns: dict[str, dict] = {}          # turn_id -> {task, requests}
        self._requests: dict[str, str] = {}        # request_id -> turn_id
        self._endpoints: set[tuple[str, str]] = set()
        self._unsupported: set[str] = set()
        self._last_turn: str | None = None
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._atexit_done = False
        self.closed = False

    # ── identity ────────────────────────────────────────────────────────
    def headers(self, turn_id: str | None = None, request_id: str | None = None,
                task: str | None = None) -> dict:
        h = {H_CLIENT: self.name, H_SESSION: self.session_id, H_PROCESS: self.process,
             H_PID: str(self.pid), H_USER: self.user, H_PLATFORM: self.platform}
        if turn_id:
            h[H_TURN] = turn_id
        if request_id:
            h[H_REQUEST] = request_id
        task = task or (self._turns.get(turn_id or "", {}).get("task")) \
            or os.environ.get(ENV_TASK)
        if task:
            h[H_TASK] = str(task)[:200]
        return {k: v for k, v in h.items() if v}

    def state(self) -> str:
        with self._lock:
            if self._requests:
                return "waiting"
            return "active" if self._turns else "idle"

    def _body(self, state: str, event: str, turn_id: str | None = None) -> dict:
        with self._lock:
            return {"session_id": self.session_id, "state": state, "event": event,
                    "turn_id": turn_id or self._last_turn,
                    "request_ids": sorted(self._requests), "ttl": self.ttl,
                    "client_process": self.process, "pid": self.pid,
                    "user": self.user, "host": self.host, "platform": self.platform,
                    "client": self.name}

    # ── wire ────────────────────────────────────────────────────────────
    def _send(self, body: dict, endpoints=None) -> None:
        for prefix, key in list(endpoints if endpoints is not None else self._endpoints):
            if prefix in self._unsupported:
                continue
            if self.poster(lease_url(prefix, self.session_id), body, key) == "unsupported":
                self._unsupported.add(prefix)   # old central: stop, silently

    def _bind(self, prefix: str, key: str) -> tuple[str, str]:
        ep = (prefix, key or "")
        with self._lock:
            self._endpoints.add(ep)
            if self._register_atexit and not self._atexit_done:
                self._atexit_done = True
                atexit.register(self.close)
        return ep

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="hugpy-lease",
                                                daemon=True)
                self._thread.start()
        self._wake.set()

    def _run(self) -> None:
        while not self.closed:
            self._wake.clear()
            with self._lock:
                busy = bool(self._turns or self._requests)
            if not busy:
                return                     # nothing in flight: no lease to hold
            self._send(self._body(self.state(), "lease"))
            self._wake.wait(self.interval)

    # ── turns & requests ────────────────────────────────────────────────
    def _stack(self) -> list:
        st = getattr(self._local, "stack", None)
        if st is None:
            st = self._local.stack = []
        return st

    def current_turn(self) -> str | None:
        st = self._stack()
        return st[-1] if st else None

    @contextlib.contextmanager
    def turn(self, task: str | None = None, turn_id: str | None = None):
        """One user-visible turn (possibly many model calls). Nested turns on
        the same thread join the outer one."""
        outer = self.current_turn()
        if outer is not None:
            yield outer
            return
        tid = turn_id or new_id("turn")
        with self._lock:
            self._turns[tid] = {"task": task}
            self._last_turn = tid
        self._stack().append(tid)
        try:
            yield tid
        finally:
            self._stack().pop()
            with self._lock:
                self._turns.pop(tid, None)
                for rid in [r for r, t in self._requests.items() if t == tid]:
                    self._requests.pop(rid, None)
                st = "waiting" if self._requests else ("active" if self._turns else "idle")
            # Off the caller's path: a slow/unreachable central must never
            # add latency to the end of a turn.
            threading.Thread(target=self._send, args=(self._body(st, "turn_done", tid),),
                             name="hugpy-turn-done", daemon=True).start()
            self._wake.set()

    @contextlib.contextmanager
    def request(self, prefix: str, key: str = "", request_id: str | None = None):
        """One model call. Opens an implicit turn when none is open, so a bare
        call (a B lookup, a probe) is still a complete turn to central."""
        if self.current_turn() is None:
            with self.turn() as _tid:
                with self.request(prefix, key, request_id) as rq:
                    yield rq
            return
        tid = self.current_turn()
        ep = self._bind(prefix, key)
        rid = request_id or new_id("req")
        with self._lock:
            self._requests[rid] = tid
        self._ensure_thread()              # wakes the lease: announces at once
        try:
            yield _Request(self, ep, tid, rid)
        finally:
            with self._lock:
                self._requests.pop(rid, None)
            self._wake.set()

    def close(self) -> None:
        """session_closed to every endpoint seen (atexit; best effort)."""
        if self.closed:
            return
        self.closed = True
        self._wake.set()
        with self._lock:
            self._turns.clear()
            self._requests.clear()
        body = self._body("closed", "session_closed")
        for prefix, key in list(self._endpoints):
            if prefix not in self._unsupported:
                self.poster(lease_url(prefix, self.session_id), body, key, 2.0)


_SINGLETON: SessionSignals | None = None
_SINGLETON_LOCK = threading.Lock()


def signals() -> SessionSignals | None:
    """The process-wide session, or None when disabled (HUGPY_SESSION_SIGNALS=0)."""
    global _SINGLETON
    if not enabled():
        return None
    with _SINGLETON_LOCK:
        if _SINGLETON is None or _SINGLETON.closed:
            _SINGLETON = SessionSignals()
        return _SINGLETON


def reset_for_tests(instance: SessionSignals | None = None) -> None:
    global _SINGLETON
    with _SINGLETON_LOCK:
        _SINGLETON = instance


# ── harnesses (OpenCode, Claude Code, Qwen Code, Hermes, ...) ─────────────
def harness_env(harness: str, environ=None, pid: int | None = None) -> dict:
    """Env additions that give a harness launch its identity. The session is
    the launch's (inherited if a parent hugpy-agent already minted one)."""
    env = os.environ if environ is None else environ
    add = {
        ENV_SESSION: (env.get(ENV_SESSION) or "").strip() or new_id("ha"),
        ENV_NAME: harness,
        ENV_PROCESS: "hugpy-agent:" + harness,
        ENV_USER: env.get(ENV_USER) or _user(),
        ENV_PLATFORM: "hugpy-agent/harness",
    }
    if pid:
        add[ENV_PID] = str(pid)
    return add


def _identity_env_hook(harness: str, env: dict) -> None:
    """harness_settings.ENV_HOOKS entry: every harness child env assembled by
    frontends.prepare gets the launch's identity (HUGPY_CLIENT_*)."""
    if enabled(env):
        env.update(harness_env(harness, env))


def register_env_hook() -> bool:
    try:
        from . import harness_settings
    except ImportError:          # launcher without the hook: nothing to join
        return False
    if _identity_env_hook not in harness_settings.ENV_HOOKS:
        harness_settings.ENV_HOOKS.append(_identity_env_hook)
    return True


def harness_header_values(environ=None) -> dict:
    """Literal identity headers from the env (for env-var-only harnesses)."""
    env = os.environ if environ is None else environ
    return {h: env[v] for h, v in HARNESS_HEADER_ENV if env.get(v)}


def harness_header_refs(style: str) -> dict:
    """Identity headers as ENV REFERENCES for a config file shared across
    launches: ``{env:VAR}`` (OpenCode) or ``$VAR`` (Qwen Code)."""
    fmt = {"opencode": "{env:%s}", "qwen": "$%s"}[style]
    return {h: fmt % v for h, v in HARNESS_HEADER_ENV}


def merge_header_lines(existing: str, headers: dict) -> str:
    """ANTHROPIC_CUSTOM_HEADERS: newline 'Name: value' list; ours replace any
    same-named line, everything else is preserved."""
    ours = {k.lower() for k in headers}
    kept = [ln for ln in (existing or "").splitlines()
            if ln.strip() and ln.partition(":")[0].strip().lower() not in ours]
    return "\n".join(kept + ["%s: %s" % (k, v) for k, v in headers.items()])


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class HarnessLease:
    """Holds a harness launch's lease: 'active' every interval while the
    harness lives, then session_closed. Used in-thread (the launcher waits on
    the harness) or as a detached sidecar (the launcher exec()s the harness,
    which keeps the pid — the sidecar watches that pid)."""

    def __init__(self, prefix: str, key: str, session_id: str, process: str,
                 alive, interval: float = LEASE_INTERVAL, ttl: int = LEASE_TTL,
                 pid: int | None = None, harness: str = "", poster=None, sleep=None):
        self.prefix, self.key, self.session_id = prefix, key, session_id
        self.process, self.alive, self.pid, self.harness = process, alive, pid, harness
        self.interval, self.ttl = interval, ttl
        self.poster = poster or post_json
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self.sent: list[str] = []

    def _body(self, state: str, event: str) -> dict:
        return {"session_id": self.session_id, "state": state, "event": event,
                "turn_id": None, "request_ids": [], "ttl": self.ttl,
                "client_process": self.process, "pid": self.pid, "user": _user(),
                "host": socket.gethostname(), "platform": "hugpy-agent/harness",
                "client": self.harness}

    def _post(self, state: str, event: str) -> str:
        r = self.poster(lease_url(self.prefix, self.session_id), self._body(state, event), self.key)
        self.sent.append(event)
        return r

    def run(self) -> None:
        while not self._stop.is_set() and self.alive():
            if self._post("active", "lease") == "unsupported":
                return                     # old central: nothing to hold, no close
            self._sleep(self.interval)
        self._post("closed", "session_closed")

    def stop(self) -> None:
        self._stop.set()


@contextlib.contextmanager
def harness_lease(base: str, key: str, environ: dict, harness: str = ""):
    """In-process lease around a harness the caller waits on (subprocess.call).
    Exports ENV_LEASE_OWNER so a nested launcher does not double-lease."""
    if not enabled(environ) or not environ.get(ENV_SESSION):
        yield None               # no identity was declared: nothing to lease
        return
    flag = threading.Event()
    environ[ENV_LEASE_OWNER] = str(os.getpid())
    lease = HarnessLease(harness_prefix(base), key, environ[ENV_SESSION],
                         environ.get(ENV_PROCESS) or "hugpy-agent:" + harness,
                         alive=lambda: not flag.is_set(), harness=harness)
    t = threading.Thread(target=lease.run, name="hugpy-harness-lease", daemon=True)
    t.start()
    try:
        yield lease
    finally:
        flag.set()
        lease.stop()
        t.join(timeout=POST_TIMEOUT + 1)


def harness_prefix(base: str) -> str:
    """API root for a configured fleet base, normalized the way every
    frontend's models URL is (console.models_url)."""
    try:
        from .console import models_url
        return models_url(base)[: -len("/v1/models")]
    except Exception:  # noqa: BLE001
        return api_prefix(base)


def start_lease_sidecar(base: str, key: str, harness: str, environ=None,
                        pid: int | None = None, popen=None) -> bool:
    """Before exec()ing a harness: spawn a detached keeper that leases for
    this pid (exec keeps it) and sends session_closed when it exits. Skipped
    when disabled or when a live owner already leases this session."""
    env = os.environ if environ is None else environ
    if not enabled(env):
        return False
    pid = pid or os.getpid()
    owner = (env.get(ENV_LEASE_OWNER) or "").strip()
    if owner.isdigit() and _pid_alive(int(owner)):
        return False
    sid = env.get(ENV_SESSION) or ensure_session_env(env)
    child_env = dict(env)
    if key:
        child_env["HUGPY_LEASE_KEY"] = key
    argv = [sys.executable, "-m", "hugpy_agent.session_signals", "keep",
            "--base", base, "--pid", str(pid), "--session", sid,
            "--harness", harness]
    try:
        import subprocess
        (popen or subprocess.Popen)(argv, env=child_env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True, close_fds=True)
    except Exception:  # noqa: BLE001 — the lease is an aid, never a launch blocker
        return False
    env[ENV_LEASE_OWNER] = str(pid)
    return True


def _main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="hugpy_agent.session_signals")
    sub = p.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keep", help="lease a harness session while --pid lives")
    k.add_argument("--base", required=True)
    k.add_argument("--pid", type=int, required=True)
    k.add_argument("--session", required=True)
    k.add_argument("--harness", default="")
    a = p.parse_args(argv)
    lease = HarnessLease(harness_prefix(a.base), os.environ.get("HUGPY_LEASE_KEY", ""),
                         a.session, os.environ.get(ENV_PROCESS) or "hugpy-agent:" + a.harness,
                         alive=lambda: _pid_alive(a.pid), pid=a.pid, harness=a.harness)
    lease.run()
    return 0


register_env_hook()

if __name__ == "__main__":
    sys.exit(_main())
