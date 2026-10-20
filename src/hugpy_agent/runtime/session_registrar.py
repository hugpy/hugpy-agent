"""Register the box's harness sessions with central — the inbound arm's job.

The harness session server (`hugpy-harness serve`, which `hugpy-agent serve`
runs with the agent's loop) is a client-side, local surface. hugpy-agent is
what tells the fleet about it: every named session on this box is registered
with central's `/api/sessions/register`, kept fresh with `/api/sessions/beat`,
and withdrawn with `/api/sessions/remove` when it disappears. Central then
proxies `/api/sessions/<sid>/*` to this serve, so the session has one route
from the fleet's routing without the caller knowing the box, port or token.

Doctrines (the same the node registry keeps, node.py):
  * errors-as-data — a failing registration is reported in the sync summary
    and retried next cycle; nothing raises out of the thread.
  * the serve token is a SECRET central needs to call back; it is sent once
    per registration over the fleet connection and never logged.
  * a loopback-only serve registers as `local_only`: listed by central, not
    routable, so an operator sees the session exists and why it isn't reachable.
"""
from __future__ import annotations

import socket
import threading
import time
from urllib.parse import urlsplit

from ..node import _default_transport

LOOPBACK = ("127.0.0.1", "localhost", "::1")
INTERVAL = 30.0            # central's ONLINE_TTL is 90 s: three missed beats = offline
BACKOFF_MAX = 120.0


def advertised_url(host: str, port: int, environ=None) -> str:
    """The URL central should call: ``HUGPY_SERVE_URL`` wins; a wildcard bind
    advertises this box's primary address; else the bound host."""
    import os
    env = os.environ if environ is None else environ
    explicit = (env.get("HUGPY_SERVE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    if host in ("0.0.0.0", "::", ""):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))       # no packet is sent; picks the egress address
            host = s.getsockname()[0]
        except OSError:
            host = socket.gethostname()
        finally:
            s.close()
    return "http://%s:%d" % (host, port)


class SessionRegistrar:
    def __init__(self, service, central: str, api_key: str = "", serve_url: str = "",
                 serve_token: str = "", locus: str = "", interval: float = INTERVAL,
                 transport=None, sleep=None):
        self.service = service
        self.central = (central or "").strip().rstrip("/")
        self.api_key = api_key or ""
        self.serve_url = (serve_url or "").rstrip("/")
        self.serve_token = serve_token or ""
        self.locus = locus or socket.gethostname()
        self.interval = interval
        self.transport = transport or _default_transport
        self._sleep = sleep or (lambda s: self._stop.wait(s))
        self.local_only = (urlsplit(self.serve_url).hostname or "") in LOOPBACK or not self.serve_token
        self.known: set[str] = set()
        self.last: dict = {}
        self.failures = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- wire ----------------------------------------------------------------
    def _headers(self):
        return {"Authorization": "Bearer " + self.api_key} if self.api_key else None

    def _post(self, route: str, payload: dict):
        try:
            return self.transport(self.central + "/api/sessions/" + route, "POST", payload,
                                  timeout=10, headers=self._headers())
        except (OSError, ValueError) as exc:         # transport failure is data
            return 0, {"error": str(exc)}

    def payload(self, s: dict) -> dict:
        settings = s.get("settings") or {}
        return {"session_id": s["id"], "name": s.get("name", ""), "route": s.get("route") or "session:" + s["id"],
                "serve_url": self.serve_url, "serve_token": self.serve_token, "local_only": self.local_only,
                # host: lets central route a loopback-only serve when it is on
                # central's own box (the Core host's helper sessions)
                "host": socket.gethostname(),
                "locus": self.locus, "model": s.get("model", "") or "", "base": settings.get("base", "") or "",
                "mode": settings.get("mode", "") or "", "pin": str(settings.get("pin", "") or ""),
                "status": s.get("status", "idle") or "idle"}

    # -- one cycle -----------------------------------------------------------
    def sync_once(self) -> dict:
        report = {"registered": [], "beat": [], "removed": [], "errors": []}
        sessions = self.service.sessions()
        current = {s["id"] for s in sessions}
        for s in sessions:
            sid = s["id"]
            if sid in self.known:
                status, _ = self._post("beat", {"session_id": sid, "status": s.get("status"),
                                                "model": s.get("model"), "name": s.get("name")})
                if 200 <= status < 300:
                    report["beat"].append(sid)
                    continue
                if status != 404:                    # 404: central forgot it -> re-register below
                    report["errors"].append({"session_id": sid, "status": status})
                    continue
                self.known.discard(sid)
            status, _ = self._post("register", self.payload(s))
            if 200 <= status < 300:
                self.known.add(sid)
                report["registered"].append(sid)
            else:
                report["errors"].append({"session_id": sid, "status": status})
        for sid in sorted(self.known - current):
            status, _ = self._post("remove", {"session_id": sid})
            self.known.discard(sid)
            report["removed"].append(sid)
        self.failures = self.failures + 1 if report["errors"] and not (report["registered"] or report["beat"]) else 0
        self.last = report
        return report

    def withdraw(self) -> None:
        for sid in sorted(self.known):
            self._post("remove", {"session_id": sid})
        self.known.clear()

    # -- thread ----------------------------------------------------------------
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync_once()
            except Exception as exc:  # noqa: BLE001 — the registrar must outlive any fault
                self.last = {"error": str(exc)}
                self.failures += 1
            delay = min(self.interval * (2 ** min(self.failures, 4)), BACKOFF_MAX) if self.failures else self.interval
            self._sleep(delay)

    def start(self):
        self._thread = threading.Thread(target=self.run, name="hugpy-session-registrar", daemon=True)
        self._thread.start()
        return self

    def stop(self, withdraw: bool = True) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if withdraw:
            self.withdraw()
