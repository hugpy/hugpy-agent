"""Backend-neutral serve protocol types (h26 §1.1).

Everything the TUI reducer sees comes through these dataclasses; the adapters
(abstract_serve.py, hugpy_serve.py) normalise raw serve JSON into them so the
reducer never touches a serve shape. No curses imports here, ever.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import NamedTuple


# Internal Event.kind vocabulary (h26 §2). Adapters MUST emit only these.
KINDS = ("user", "assistant", "thinking", "tool", "tool_result", "approval",
         "question", "resolved", "status", "system", "note", "usage", "done")


class ServeError(RuntimeError):
    """HTTP error body `{error}` or transport failure, with the status code
    when there was one (400 "No pending approval" is handled by code)."""

    def __init__(self, detail, code=None):
        super().__init__(str(detail))
        self.code = code


@dataclass
class Session:
    id: str
    role: str = ""            # keeper|chat|worker|local or "" for plain cs-* rows
    label: str = ""
    backend: str = ""
    model: str = ""
    pending_model: str | None = None
    busy: bool = False
    paused: bool = False
    native_id: str = ""
    updated: float = 0.0


@dataclass
class ProviderOption:
    backend: str
    model: str
    label: str


@dataclass
class Roster:
    roles: list = field(default_factory=list)          # standing roles, in order
    sessions: list = field(default_factory=list)       # every cs-* row (updated DESC)
    provider_options: list = field(default_factory=list)
    cwd: str = ""
    defaults: dict = field(default_factory=dict)

    def find(self, sid):
        for row in self.roles + self.sessions:
            if row.id == sid:
                return row
        return None


@dataclass
class Event:
    seq: int
    ts: float
    session_id: str
    kind: str
    text: str = ""
    name: str = ""
    detail: str = ""
    request_id: str = ""
    options: list = field(default_factory=list)
    ok: bool | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class Receipt:
    session_id: str
    message_ids: list = field(default_factory=list)
    cursor: str = ""
    queued: bool = False


@dataclass
class QueueItem:
    id: str
    text: str
    ts: float = 0.0


@dataclass
class QueueView:
    auto: bool = True
    busy: bool = False
    paused: bool = False
    items: list = field(default_factory=list)


@dataclass
class Approval:
    request_id: str
    kind: str                 # 'approval' (gpt) | 'question' (hugpy)
    title: str
    options: list = field(default_factory=list)
    params: dict | str = ""
    ts: float = 0.0
    session_id: str = ""


@dataclass
class Usage:
    in_tokens: int = 0
    out_tokens: int = 0
    context_window: int = 0
    cost_usd: float = 0.0
    source: str = ""


class EventPage(NamedTuple):
    """What `Client.events` returns: unpacks as (events, busy, queue, cursor)
    plus the source tag and a truncation flag for the first-sight page cap."""
    events: list
    busy: bool
    queue: QueueView | None
    cursor: str
    source: str = ""
    truncated: bool = False


class Client(ABC):
    kind = ""

    def __init__(self, base):
        self.base = base.rstrip("/")

    @abstractmethod
    def probe(self) -> dict: ...
    @abstractmethod
    def roster(self) -> Roster: ...
    @abstractmethod
    def events(self, sid, since, on_page=None) -> EventPage: ...
    @abstractmethod
    def send(self, sid, text, on_event=None) -> Receipt: ...
    @abstractmethod
    def interrupt(self, sid): ...
    @abstractmethod
    def queue(self, sid) -> QueueView | None: ...
    @abstractmethod
    def queue_action(self, sid, action, **kw): ...
    @abstractmethod
    def answer(self, sid, request_id, decision): ...
    @abstractmethod
    def models(self) -> list: ...
    @abstractmethod
    def set_provider(self, role, backend, model): ...
    @abstractmethod
    def set_model(self, role, model): ...
    @abstractmethod
    def select_model(self, row, option): ...
    @abstractmethod
    def state(self) -> dict: ...

    def usage(self, sid) -> Usage | None:
        return None

    def rollover(self) -> dict:
        return {}

    def clear(self, sid):
        """Wipe a session's model context (display kept). Serves that cannot
        do this say so, so the TUI can surface a notice."""
        raise ServeError("this serve has no context clear")

    def emergency_preflight(self):
        """Local llama-server + GGUFs available for a break-glass launch."""
        raise ServeError("this serve has no emergency inference")

    def launch_emergency(self, model_path=None, gpu_layers="auto"):
        raise ServeError("this serve has no emergency inference")
