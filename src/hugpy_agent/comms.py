"""Operator comms — the Discord session client behind ask_operator (P2.3).

Wire contract (verified against the live central code, 2026-07-15:
`flask_app/app/routes/discord_routes.py` + `bot/bot.py` in the RO
abstract_hugpy_dev tree):

  POST <session>/send   {"content": question, "options": [label, ...]}
      -> 201 {"ok": true, "message": {"id", "ts", "direction": "out", ...}}
      `options` is 1..5 non-empty strings, each <= 80 chars; content <= 1900
      chars (central 413s above that). The bot renders the options as
      Discord buttons whose custom_id is `esc:<channel>:<msg>:<idx>`.

  GET  <session>/messages?since=<ts>
      -> {"messages": [{"direction", "source", "content", "ts", ...}]}
      `since` filters strictly (ts > since). The operator's button click
      rides back through the bot's DynamicItem handler, which relays the
      chosen LABEL inbound via central's /discord/inbox — so the reply
      appears here as a message with direction == "in" whose content is
      exactly the clicked label.

Doctrines:
  * errors-as-data — ask() never raises; every failure mode returns the
    {answered, choice, timed_out} dict (plus an `error` note).
  * fail closed — no session configured => not answered, and the caller
    (the escalation gate) turns that into a deny.
  * the poll is STRICTLY bounded by a monotonic deadline; a hung transport
    can cost at most timeout + one request.
  * the session token (it rides in the URL) is a secret: it is never
    logged, journaled, or persisted by this module — a minted token lives
    in this process only.

Transport is injectable (tests run offline); the default is stdlib urllib,
mirroring gateway.py.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from .config import DEFAULT_ASK_TIMEOUT

POLL_INTERVAL = 3.0            # seconds between messages polls
MAX_OPTIONS = 5                # Discord: one action row of buttons
MAX_OPTION_CHARS = 80          # central 400s above this per label
MAX_CONTENT_CHARS = 1900       # central 413s above this per message


def _default_transport(url: str, method: str = "GET", payload=None,
                       timeout: int = 30, headers: dict | None = None):
    """One JSON request -> parsed JSON. Raises urllib errors; ask() owns
    turning those into data. Session verbs need no auth header (the token
    in the URL IS the credential); the mint call passes a Bearer via
    `headers`."""
    data = None
    hdrs = {"Accept": "application/json"}
    hdrs.update(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs,
                                 method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode(errors="replace")
    return json.loads(raw) if raw.strip() else {}


def _result(answered: bool, choice: str | None, timed_out: bool,
            error: str | None = None) -> dict:
    out = {"answered": answered, "choice": choice, "timed_out": timed_out}
    if error:
        out["error"] = error
    return out


class Comms:
    """Client for ONE operator channel session. `session_url` is the full
    session endpoint (…/api/discord/session/<token>); empty means
    unconfigured — every ask() then fails closed as data.

    `mint` + `base` + `channel_id` is the alternative: the first ask() mints
    a session via POST /api/discord/sessions (operator-gated — the api_key
    rides as a Bearer for that one call). The minted token stays in-process.
    """

    def __init__(self, session_url: str = "", timeout: int = DEFAULT_ASK_TIMEOUT,
                 transport=None, poll_interval: float = POLL_INTERVAL,
                 monotonic=None, sleep=None, mint: bool = False,
                 base: str = "", channel_id: str = "", api_key: str = ""):
        self.session_url = (session_url or "").strip().rstrip("/")
        self.timeout = timeout
        self.transport = transport or _default_transport
        self.poll_interval = poll_interval
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.mint = bool(mint)
        self.base = (base or "").strip().rstrip("/")
        self.channel_id = str(channel_id or "").strip()
        self.api_key = api_key or ""

    @classmethod
    def from_config(cls, cfg, transport=None) -> "Comms":
        return cls(session_url=getattr(cfg, "discord_session", ""),
                   timeout=getattr(cfg, "ask_timeout", DEFAULT_ASK_TIMEOUT),
                   transport=transport,
                   mint=getattr(cfg, "discord_mint", False),
                   base=getattr(cfg, "base", ""),
                   channel_id=getattr(cfg, "discord_channel", ""),
                   api_key=getattr(cfg, "api_key", ""))

    # ── session resolution ───────────────────────────────────────────────
    def configured(self) -> bool:
        return bool(self.session_url) or (self.mint and self.base
                                          and self.channel_id)

    def _origin(self) -> str:
        p = urllib.parse.urlsplit(self.base)
        if p.scheme and p.netloc:
            return "%s://%s" % (p.scheme, p.netloc)
        return self.base

    def _ensure_session(self) -> str | None:
        """The session endpoint URL, minting one if so configured. None =>
        no channel available (the caller fails closed). The token is kept
        on this instance only — never logged or persisted."""
        if self.session_url:
            return self.session_url
        if not (self.mint and self.base and self.channel_id):
            return None
        url = self._origin() + "/api/discord/sessions"
        # Mint is operator-gated on central: this one call carries the Bearer.
        headers = ({"Authorization": "Bearer %s" % self.api_key}
                   if self.api_key else None)
        try:
            data = self.transport(url, method="POST",
                                  payload={"channel_id": self.channel_id,
                                           "label": "hugpy-agent"},
                                  timeout=30, headers=headers)
        except Exception:
            return None            # mint failed => unconfigured (fail closed)
        token = (data or {}).get("token")
        if not token:
            return None
        self.session_url = self._origin() + "/api/discord/session/" + token
        return self.session_url

    # ── the one public verb ──────────────────────────────────────────────
    def ask(self, question: str, options: list, timeout: int | None = None,
            stop=None) -> dict:
        """Send `question` with clickable `options` (1..5 labels) to the
        operator channel and block — bounded — for the clicked reply.

        Returns {answered: bool, choice: str|None, timed_out: bool} (+ an
        `error` note on the non-timeout failure paths). Never raises.

        Only an inbound message matching one of the offered labels counts as
        the answer (matched case-insensitively; the canonical option label is
        returned) — unrelated channel chatter is ignored, fail-closed.
        `stop` is an optional callable checked each poll so an operator
        Ctrl-C doesn't hang for the full timeout.
        """
        question = (question or "").strip()
        if not question:
            return _result(False, None, False, "empty question")
        labels = [str(o).strip() for o in (options or []) if str(o).strip()]
        if not labels or len(labels) > MAX_OPTIONS:
            return _result(False, None, False,
                           "options must be 1..%d non-empty labels (got %d)"
                           % (MAX_OPTIONS, len(labels)))
        labels = [l[:MAX_OPTION_CHARS] for l in labels]
        question = question[:MAX_CONTENT_CHARS]

        session = self._ensure_session()
        if not session:
            return _result(False, None, False,
                           "no operator channel configured (set "
                           "HUGPY_DISCORD_SESSION, or HUGPY_DISCORD_MINT=1 "
                           "with HUGPY_DISCORD_CHANNEL)")

        # 1. send — errors here are terminal for this ask (nothing to poll).
        try:
            sent = self.transport(session + "/send", method="POST",
                                  payload={"content": question,
                                           "options": labels},
                                  timeout=30)
        except Exception as exc:
            return _result(False, None, False,
                           "send failed: %s: %s" % (type(exc).__name__, exc))
        # `since` baseline: central filters strictly (ts > since), so the
        # echoed outbound's own ts is the exact watermark — no client/server
        # clock-skew hole. Fall back to 0 only when no bridge echoed it
        # (the reply match below still keys off direction+label).
        msg = (sent or {}).get("message") or {}
        since = float(msg.get("ts") or 0.0)

        # 2. poll — strictly bounded by a monotonic deadline.
        wait = self.timeout if timeout is None else timeout
        deadline = self.monotonic() + max(0.0, float(wait))
        by_lower = {l.lower(): l for l in labels}
        while True:
            if stop is not None and stop():
                return _result(False, None, False, "interrupted before reply")
            try:
                data = self.transport(
                    "%s/messages?since=%s" % (session, since),
                    method="GET", timeout=30)
            except Exception:
                data = {}          # transient poll error: keep trying till deadline
            for m in (data or {}).get("messages") or []:
                ts = float(m.get("ts") or 0.0)
                if ts > since:
                    since = ts     # advance the watermark past chatter too
                if m.get("direction") != "in":
                    continue       # our own outbounds echo back — skip them
                content = str(m.get("content") or "").strip()
                choice = by_lower.get(content.lower())
                if choice is not None:
                    return _result(True, choice, False)
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return _result(False, None, True)
            self.sleep(min(self.poll_interval, remaining))
