"""B answers lookups WITHOUT inference — the model is for synthesis only.

Operator ask (2026-09-29, seeing B's system prompt carry an EMPTY state block
to the fleet's one Coder-Next slot): "does this take Coder-Next's inference?
isn't it a standard string search?" — it did, for every message. This module
puts a deterministic classifier in front of the model call so that anything
the state block can answer by lookup is answered by lookup:

  ack       "ok", "thanks"                        -> one-line ack
  help      "help", "what can you do"             -> the command list
  meta      "does this use inference / what model" -> how B answers, which model
  state     "what is in the catalog", "status"    -> the state fields, verbatim
  search    "where is X", "find X", "which entries mention X"
                                                  -> substring search over policy,
                                                     catalog bytes, derived memory,
                                                     ledger objects, rolling log,
                                                     with pointers
  synth     anything else                         -> the model, ONLY when the state
                                                     is non-empty, one in flight
                                                     per workspace, skipped when
                                                     central's /api/llm/queue is
                                                     already backed up

Empty state (no policy, no catalog, no memory) never reaches the model: the
answer is one templated sentence saying so and what would populate it.

Every reply ends with the metadata line the operator requires everywhere —
``(timestamp · tokens N · duration · model)`` — where lookups show
``tokens 0 · lookup`` so it is visible at a glance that no inference was spent.

Shared by :mod:`hugpy_agent.mct.b_answer` (the station's /api/b/chat one-shot)
and :func:`hugpy_agent.mct.repl._b_prompt` (the ``/b`` command).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

MAX_TOKENS = 700                 # the existing per-reply cap, unchanged
LOOKUP_MODEL = "lookup"          # the model field on every non-inference reply
MAX_HITS = 20                    # search results per reply
EXCERPT = 120                    # chars of context per hit
OBJECT_READ_CAP = 256 * 1024     # bytes of one object we will scan
LOG_TAIL_LINES = 2000            # rolling-log lines we will scan
QUEUE_PROBE_TIMEOUT = 3.0        # seconds; a dead probe never blocks the reply

B_SYSMSG = (
    "You are B — the broker/curator of this station's Mediated Context "
    "Terminal. You curate bounded context for A (a confined Claude) and keep "
    "the ledger, catalog, and derived memory. Answer the operator directly, "
    "concisely, in first person as B. Ground every claim in the state below; "
    "when the state does not contain the answer, say so plainly.\n\n"
    "=== your current state ===\n")

HELP_TEXT = (
    "I answer from my own state without inference: ask what is in the catalog / "
    "policy / memory / ledger / tokens / log, or 'status'; search it with "
    "'where is X', 'find X', 'which entries mention X'. Only a request that "
    "needs synthesis over non-empty state (summarize, judge, rewrite) goes to "
    "the fleet model. In the terminal: /bstate shows the state, /policy sets "
    "the governing instruction, /root + /file or /source populate the catalog, "
    "/memory lists derived facts, /tokens the spend, /tail the rolling log.")


# ── classifier ───────────────────────────────────────────────────────────────
@dataclass
class Intent:
    kind: str                       # ack | help | meta | state | search | synth
    fields: list[str] = field(default_factory=list)   # state: which fields
    query: str = ""                 # search: the needle


_ACK_WORDS = {"ok", "okay", "k", "kk", "thanks", "thank", "thx", "ty", "got",
              "it", "cool", "great", "nice", "noted", "ack", "yes", "no", "sure",
              "alright", "roger", "cheers", "you", "fine", "good", "perfect",
              "done", "right", "yep", "yup", "nope"}

_HELP_RE = re.compile(
    r"^\s*/?help\b|^\s*(commands|usage)\s*\??\s*$|what can you do|how do i (use|talk|ask)"
    r"|what (commands|do you (support|accept))", re.I)

_META_RE = re.compile(
    r"\binference\b|\b(which|what) model\b|are you (using|calling|running) (a |the )?model"
    r"|does (this|it|that) (use|take|cost|spend|need|hit) |string search"
    r"|\bllm\b|are you (an? )?(llm|model)|do you (use|call|need) (a |the )?(model|llm)",
    re.I)

# search triggers — the needle is whatever follows the trigger
_SEARCH_RE = re.compile(
    r"^\s*(?:(?:can|could|would) you |please )?"
    r"(?:where (?:is|are|was|were|do you have|did you (?:put|record))|find(?: me)?|search(?: for)?"
    r"|grep(?: for)?|look(?: )?up|locate|which (?:entries|entry|objects|facts|sources|files|"
    r"catalog entries|lines|turns)?\s*(?:mention|contain|reference|have|include|say)"
    r"|(?:what|anything) (?:mentions|contains|references|about)|do you have anything (?:about|on)"
    r"|is there anything (?:about|on)|show me (?:everything|anything) (?:about|on|mentioning))"
    r"\s*[:\-]?\s*(?P<q>.+?)[?.!\s]*$", re.I)

_STATE_FIELD_WORDS = {
    "policy": ("policy", "policies", "governing instruction", "instruction"),
    "catalog": ("catalog", "catalogue", "sources", "source", "entries", "files",
                "objects", "pullable"),
    "memory": ("memory", "memories", "facts", "fact", "decisions", "decision",
               "derived", "remember", "learned"),
    "tokens": ("tokens", "token", "cost", "spend", "spent", "usage", "budget"),
    "log": ("log", "logs", "rolling log", "ledger", "events", "event log", "history"),
    "turn": ("last turn", "turn", "previous turn", "last answer"),
}
_STATE_ASK_RE = re.compile(
    r"\b(what|which|show|list|print|dump|display|tell me|how many|how much|do you have"
    r"|have you|is there|are there|any|give me|report|read out|readout|status|state"
    r"|whats|what's|contents?|current)\b", re.I)
_STATE_BARE_RE = re.compile(
    r"^\s*(status|state|what do you have|what do you know|what have you got|what is your state"
    r"|what'?s your state|readout|overview|summary of (your )?state|/?bstate)\s*[?.!]*\s*$", re.I)


def classify(text: str) -> Intent:
    """Deterministic intent — no model, no I/O. Order matters: help, then
    search (so 'grep coder-next' is a search, not a question about the model),
    then meta (so 'what model' is not a state lookup), then state."""
    t = (text or "").strip()
    low = t.lower()
    words = re.findall(r"[a-z']+", low)
    if not t:
        return Intent("ack")
    if len(words) <= 4 and "?" not in t and words and all(w in _ACK_WORDS for w in words):
        return Intent("ack")
    if _HELP_RE.search(t):
        return Intent("help")
    m = _SEARCH_RE.match(t)
    if m and m.group("q").strip():
        q = m.group("q").strip().strip("'\"`“”‘’")
        return Intent("search", query=q)
    if _META_RE.search(t):
        return Intent("meta")
    if _STATE_BARE_RE.match(t):
        return Intent("state", fields=["all"])
    fields = [f for f, ws in _STATE_FIELD_WORDS.items()
              if any(re.search(r"\b%s\b" % re.escape(w), low) for w in ws)]
    if fields and _STATE_ASK_RE.search(t):
        return Intent("state", fields=fields)
    return Intent("synth")


# ── state snapshot ───────────────────────────────────────────────────────────
@dataclass
class BState:
    policy: str = ""                                   # "" = none set
    catalog: dict[str, str] = field(default_factory=dict)   # name -> pointer
    catalog_text: dict[str, str] = field(default_factory=dict)  # name -> bytes as text
    memory: list[tuple[str, str, str]] = field(default_factory=list)  # (kind, text, object_id)
    ledger: list[tuple[str, str, str]] = field(default_factory=list)  # (kind, object_id, text)
    tokens_line: str = ""
    last_turn: str = ""
    log_path: str = ""
    log_lines: list[str] = field(default_factory=list)
    session_id: str = ""
    state_dir: Path | None = None

    @property
    def empty(self) -> bool:
        return not (self.policy or self.catalog or self.memory)

    def readout(self) -> str:
        """The same one-screen text the prompt used to carry."""
        lines = [f"policy: {self.policy or '(none set)'}",
                 f"catalog ({len(self.catalog)}): "
                 f"{', '.join(sorted(self.catalog)) or '(empty)'}",
                 f"derived memory ({len(self.memory)}):"]
        for kind, text, _oid in self.memory[-8:]:
            lines.append(f"  [{kind}] {text}")
        if self.tokens_line:
            lines.append("tokens: " + self.tokens_line)
        if self.last_turn:
            lines.append(self.last_turn)
        if self.log_path:
            lines.append(f"rolling log: {self.log_path}")
        return "\n".join(lines)


def _decode(data: bytes) -> str:
    return data[:OBJECT_READ_CAP].decode("utf-8", "replace")


def collect_state(sess, server, state: dict | None = None) -> BState:
    """Read B's state from the live broker objects. Every read is best-effort:
    a field that cannot be read is simply absent, never an exception."""
    st = BState(session_id=getattr(sess, "session_id", "") or "")
    state = state or {}
    ptr = getattr(sess, "_policy_pointer", None)
    txt = getattr(sess, "_policy_text", None)
    if isinstance(txt, str) and txt:
        st.policy = txt
    elif ptr:
        try:
            st.policy = _decode(server.store.resolve(st.session_id, ptr))
        except Exception:
            st.policy = ""
    try:
        sess._materialize_file_sources()
    except Exception:
        pass
    try:
        st.catalog = dict(getattr(sess, "_catalog", {}) or {})
    except Exception:
        st.catalog = {}
    for name, pointer in st.catalog.items():
        try:
            st.catalog_text[name] = _decode(server.store.resolve(st.session_id, pointer))
        except Exception:
            st.catalog_text[name] = ""
    try:
        for f in server.compaction.facts(st.session_id):
            st.memory.append((f.kind, f.text, getattr(f, "object_id", "")))
    except Exception:
        pass
    try:
        for meta in server.ledger.list_objects(st.session_id, newest_first=False):
            mt = str(meta.get("media_type") or "")
            if not (mt.startswith("text/") or mt.endswith("json")):
                continue
            kind = str(meta.get("kind") or "")
            if kind in ("policy_snapshot", "source_snapshot") or kind.startswith("fact"):
                continue          # already covered by policy / catalog / memory
            try:
                path = server.store.path_for(st.session_id, meta["object_id"])
                if path is None:
                    continue
                st.ledger.append((kind, meta["object_id"], _decode(path.read_bytes())))
            except Exception:
                continue
    except Exception:
        pass
    try:
        st.tokens_line = server.tokens.render(st.session_id).strip().splitlines()[-1].strip()
    except Exception:
        pass
    r = state.get("last")
    if r is not None:
        st.last_turn = (f"last turn: {r.turn_id} state={r.state}"
                        + (f" error={r.error}" if getattr(r, "error", None) else ""))
    try:
        p = Path(server.ledger.event_log_path)
        st.log_path = str(p)
        st.state_dir = p.parent
        if p.exists():
            st.log_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_TAIL_LINES:]
    except Exception:
        pass
    return st


# ── deterministic answers ────────────────────────────────────────────────────
def empty_state_reply(st: BState, intent: Intent) -> str:
    what = ("Nothing to search: " if intent.kind == "search" else "")
    return (f"{what}{'m' if what else 'M'}y state is empty — no policy, no catalog entries, no derived "
            "memory. A policy is set with /policy <text>; the catalog fills when "
            "sources are registered (/root + /file, /source) or A pulls through me; "
            "derived memory is written by compaction after A's turns."
            + (f" Rolling log: {st.log_path}" if st.log_path else ""))


def state_reply(st: BState, fields: list[str]) -> str:
    if "all" in fields or not fields:
        return st.readout()
    out = []
    for f in fields:
        if f == "policy":
            out.append(f"policy: {st.policy or '(none set)'}")
        elif f == "catalog":
            if st.catalog:
                out.append(f"catalog ({len(st.catalog)}):")
                out += [f"  {n}  {st.catalog[n]}" for n in sorted(st.catalog)]
            else:
                out.append("catalog (0): (empty)")
        elif f == "memory":
            if st.memory:
                out.append(f"derived memory ({len(st.memory)}):")
                out += [f"  [{k}] {t}" for k, t, _ in st.memory]
            else:
                out.append("derived memory (0): (empty)")
        elif f == "tokens":
            out.append("tokens: " + (st.tokens_line or "(no turns yet)"))
        elif f == "log":
            n = len(st.log_lines)
            out.append(f"rolling log: {st.log_path or '(none)'} ({n} line(s) scanned)")
            out += [f"  {ln}" for ln in st.log_lines[-10:]]
            out.append(f"ledger objects ({len(st.ledger)}): "
                       + (", ".join(f"{k}:{oid}" for k, oid, _ in st.ledger[-10:]) or "(none)"))
        elif f == "turn":
            out.append(st.last_turn or "last turn: (none yet)")
    return "\n".join(out)


def _hits_in(text: str, needles: list[str]) -> list[tuple[int, str]]:
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        if any(n in low for n in needles):
            out.append((i, line.strip()[:EXCERPT]))
    return out


def search(st: BState, query: str) -> list[str]:
    """Case-insensitive substring search. The whole phrase first; if nothing
    matches, any single term of it. Each hit is a pointer + line + excerpt."""
    phrase = query.lower().strip()
    if not phrase:
        return []
    terms = [w for w in re.findall(r"[\w./:@-]+", phrase) if len(w) >= 2]
    for needles in ([phrase], terms):
        if not needles:
            continue
        hits: list[str] = []
        if st.policy and _hits_in(st.policy, needles):
            hits.append(f"policy: {st.policy.strip()[:EXCERPT]}")
        for name in sorted(st.catalog):
            for ln, ex in _hits_in(st.catalog_text.get(name, ""), needles):
                hits.append(f"catalog:{name} ({st.catalog[name]}) line {ln}: {ex}")
            if any(n in name.lower() for n in needles):
                hits.append(f"catalog:{name} ({st.catalog[name]}) [name match]")
        for kind, text, oid in st.memory:
            if any(n in text.lower() for n in needles):
                hits.append(f"memory:[{kind}] {oid}: {text[:EXCERPT]}")
        for kind, oid, text in st.ledger:
            for ln, ex in _hits_in(text, needles):
                hits.append(f"ledger:{kind} {oid} line {ln}: {ex}")
        for i, line in enumerate(st.log_lines, 1):
            if any(n in line.lower() for n in needles):
                hits.append(f"log:{st.log_path}:{i}: {line.strip()[:EXCERPT]}")
        if hits:
            return hits
    return []


def search_reply(st: BState, query: str) -> str:
    hits = search(st, query)
    if not hits:
        return (f"No match for '{query}' in my state (policy, {len(st.catalog)} catalog "
                f"entr{'y' if len(st.catalog) == 1 else 'ies'}, {len(st.memory)} derived "
                f"fact(s), {len(st.ledger)} ledger object(s), {len(st.log_lines)} log line(s)).")
    more = len(hits) - MAX_HITS
    body = "\n".join(f"  {h}" for h in hits[:MAX_HITS])
    tail = f"\n  … {more} more (narrow the query)" if more > 0 else ""
    return f"{len(hits)} match(es) for '{query}':\n{body}{tail}"


def meta_reply(model_name: str, base: str) -> str:
    return ("Lookups over my own state (what/where/find/status) are answered by "
            "string search — no inference, tokens 0, model 'lookup' on the metadata "
            "line. Only synthesis over non-empty state (summaries, judgements, "
            f"rewrites) goes to the fleet model: {model_name or 'default'} via "
            f"{base or 'the configured gateway'}, capped at {MAX_TOKENS} tokens, one in "
            "flight per workspace, skipped while central's inference queue is backed up.")


# ── guards around the one model call ─────────────────────────────────────────
class _Inflight:
    """One synthesis per workspace at a time, across processes (the station
    execs one b_answer per question) — an flock on the mct state dir."""

    def __init__(self, state_dir: Path | None):
        self.path = (state_dir / "b-inflight.lock") if state_dir else None
        self.fh = None

    def acquire(self) -> bool:
        if self.path is None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = open(self.path, "a+")
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if self.fh:
                try:
                    self.fh.close()
                except OSError:
                    pass
                self.fh = None
            return False

    def release(self) -> None:
        if self.fh:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_UN)
                self.fh.close()
            except OSError:
                pass
            self.fh = None


def queue_busy(base: str, api_key: str = "", timeout: float = QUEUE_PROBE_TIMEOUT) -> bool:
    """True when central's inference queue already has requests WAITING — B's
    synthesis is never worth queueing behind real work. A probe that fails
    (no central, old central, timeout) means 'not busy': the guard degrades
    to the plain model call, never to a silent refusal."""
    if os.environ.get("HUGPY_B_SKIP_IF_BUSY", "1") in ("0", "false", "no"):
        return False
    try:
        from hugpy_agent.gateway import origin
        url = origin(base) + "/api/llm/queue"
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            doc = json.loads(resp.read().decode("utf-8", "replace"))
        counts = doc.get("counts") if isinstance(doc, dict) else None
        waiting = int((counts or {}).get("waiting") or 0)
        limit = int(os.environ.get("HUGPY_B_QUEUE_WAITING_MAX", "0"))
        return waiting > limit
    except Exception:
        return False


# ── metadata line ────────────────────────────────────────────────────────────
def meta_line(model: str, tokens: int, started: float, ts: datetime | None = None) -> str:
    ts = ts or datetime.now(timezone.utc)
    dur = max(0.0, time.monotonic() - started)
    return (f"({ts.strftime('%Y-%m-%dT%H:%M:%SZ')} · tokens {int(tokens)} · "
            f"{dur:.3f}s · {model})")


# ── the entry both callers use ───────────────────────────────────────────────
def default_chat(workspace: str | None = None, model: str | None = None):
    """Build the gateway lazily — only a synth reply pays for the import,
    the config load, and the route probe."""
    from hugpy_agent.config import load_config
    from hugpy_agent.gateway import Gateway
    cfg = load_config(workspace=workspace) if workspace else load_config()
    gw = Gateway.from_config(cfg)
    name = model or gw.model or "default"

    def chat(messages):
        return gw.chat(messages, model=model, max_tokens=MAX_TOKENS)

    return chat, name, cfg.base, getattr(cfg, "api_key", "") or ""


def respond(text: str, sess, server, state: dict | None = None, *,
            history: list | None = None, workspace: str | None = None,
            model: str | None = None, chat: Callable | None = None,
            model_name: str = "", base: str = "", api_key: str = "",
            busy: Callable[[], bool] | None = None) -> dict:
    """Answer ``text`` as B. Returns
    ``{"reply", "offline", "mode", "model", "tokens", "meta"}`` where ``reply``
    already ends with the metadata line. ``chat`` (messages -> ChatResult-like)
    is injectable so tests can prove the model is never touched on lookups."""
    started = time.monotonic()
    st = collect_state(sess, server, state)
    intent = classify(text)
    state = state or {}

    def done(body: str, mode: str, *, offline=False, mdl=LOOKUP_MODEL, tokens=0):
        line = meta_line(mdl, tokens, started)
        return {"reply": f"{body.rstrip()}\n{line}", "offline": offline,
                "mode": mode, "model": mdl, "tokens": int(tokens), "meta": line}

    if intent.kind == "ack":
        return done("Noted.", "ack")
    if intent.kind == "help":
        return done(HELP_TEXT, "help")
    if intent.kind == "meta":
        name = model_name or model or state.get("bmodel") or _configured_model_name(workspace)
        return done(meta_reply(name, base or _configured_base(workspace)), "meta")
    if st.empty:
        return done(empty_state_reply(st, intent), "empty")
    if intent.kind == "state":
        return done(state_reply(st, intent.fields), "state")
    if intent.kind == "search":
        return done(search_reply(st, intent.query), "search")

    # synth — the only path that may spend inference
    ground = st.readout()
    try:
        if chat is None:
            chat, model_name, base, api_key = default_chat(workspace, model or state.get("bmodel"))
        model_name = model_name or model or "default"
        if (busy() if busy else queue_busy(base, api_key)):
            return done("[inference skipped — central's queue is backed up; "
                        "deterministic readout instead]\n\n" + ground, "busy", offline=True)
        lock = _Inflight(st.state_dir)
        if not lock.acquire():
            return done("[inference skipped — I am already answering another "
                        "question for this workspace; deterministic readout instead]\n\n"
                        + ground, "busy", offline=True)
        try:
            msgs = [{"role": "system", "content": B_SYSMSG + ground}]
            for m in (history or [])[-20:]:
                r, c = m.get("role"), m.get("content")
                if r in ("user", "assistant") and isinstance(c, str):
                    msgs.append({"role": r, "content": c})
            msgs.append({"role": "user", "content": text})
            res = chat(msgs)
        finally:
            lock.release()
        reply = getattr(res, "text", None) or getattr(res, "content", None)
        if getattr(res, "ok", True) is False or not (reply or "").strip():
            raise RuntimeError(getattr(res, "error", None) or "gateway returned no text")
        tokens = getattr(res, "est_tokens", 0) or _estimate(reply)
        return done(reply.strip(), "synth", mdl=model_name, tokens=tokens)
    except Exception as exc:
        return done("[B offline - deterministic state readout]\n"
                    f"(gateway unavailable: {type(exc).__name__}: {exc})\n\n" + ground,
                    "offline", offline=True)


def _estimate(text: str) -> int:
    try:
        from hugpy_agent.gateway import estimate_tokens
        return estimate_tokens(text)
    except Exception:
        return max(1, len(text) // 4)


def _configured_model_name(workspace: str | None) -> str:
    try:
        from hugpy_agent.config import load_config
        cfg = load_config(workspace=workspace) if workspace else load_config()
        return cfg.model or "default"
    except Exception:
        return "default"


def _configured_base(workspace: str | None) -> str:
    try:
        from hugpy_agent.config import load_config
        cfg = load_config(workspace=workspace) if workspace else load_config()
        return cfg.base
    except Exception:
        return ""
