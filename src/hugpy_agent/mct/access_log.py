"""Real-time file-access tracker — who touched which file, while it happens.

Design ref: §18.2 (explainability), invariant 5 (A never receives a host path),
§13.4 (snapshot boundary).

The event ledger already records *that* a pull or a read occurred, but it speaks
in object ids: ``a.read object=o_01KZ…`` tells an operator nothing about which
file was involved. When A and B are mid-turn the question is usually much more
concrete — "what is it reading right now, and which side is doing it?"

This tracker answers exactly that, live:

* **B** entries are the real filesystem: the roots B scanned, the files it lifted
  off disk, the paths it wrote, the commands it ran.
* **A** entries are the mediated view: A only ever resolves objects, so an A read
  is attributed back to a file by the snapshot's provenance. When an object has
  no file behind it (a manifest, a pull result) the object kind is shown instead.

Two sinks, both append-only and line-buffered so ``tail -f`` is immediate:

* a formatted line into the existing rolling ``mct.log``, so one stream shows
  events and file access interleaved in true order;
* ``access.jsonl`` beside it, so ``/files`` can aggregate without re-parsing
  human text.

Host paths appear here because this log is B's, written for the operator. It is
never an object and never enters A's context, so invariant 5 is untouched.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

# Keep the column widths in step with Ledger's event lines so the two kinds of
# record read as one table when interleaved in mct.log.
_ACTOR_W, _VERB_W = 16, 20

# Verbs that name an actual file on disk (or an object standing in for one).
# Everything else — pull/act/respond/scan — is protocol flow or a directory
# sweep, and must not be counted as "this file was accessed".
FILE_VERBS = frozenset({"read", "serve", "peek", "write", "edit"})


def _fmt_tokens(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "0"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class AccessLog:
    """Append-only record of file access, attributed to A or B."""

    def __init__(self, path: Path, ledger=None, enabled: bool = True, resolver=None):
        self.path = Path(path)
        self._ledger = ledger
        # (session_id, pointer) -> bytes. Lets the log `cat` the actual
        # correspondence instead of only naming it; None simply disables that.
        self._resolve = resolver
        self._lock = threading.Lock()
        self._fh = None
        # Running tokens-into-A, keyed (session, turn). In-memory: a live meter,
        # not an accounting record — TokenUsage owns the durable, precise one.
        self._spent: dict[tuple[str, str], int] = {}
        if enabled:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(self.path, "a", buffering=1)  # line-buffered
            except OSError:
                self._fh = None

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def spend(self, session: str, turn: str) -> int:
        """Tokens handed to A so far on this turn (0 when the turn is new).

        Counted from A's side of the boundary — what A actually resolved into
        its context — because that is the number being paid for. Bytes B read
        while searching are free and deliberately excluded; conflating them
        would make the meter read high and the mediation look worthless."""
        return self._spent.get((session, turn), 0)

    def session_spend(self, session: str) -> int:
        return sum(v for (s, _t), v in self._spent.items() if s == session)

    def record(self, actor: str, verb: str, target: str, *, detail: str = "",
               session: str = "", turn: str = "", bytes_: int | None = None,
               obj: str | None = None, path: str | None = None,
               tokens: int | None = None) -> None:
        """Log one access. Never raises — a logging fault must not fail a turn.

        ``obj`` is the pointer to the bytes A was served; ``path`` is the file on
        this host. Both are recorded because they answer different questions —
        "what did A see" versus "which file is that" — and because a frontend
        relaying this log must be able to linkify a path without knowing
        anything about MCT pointers or owning a broker handle. The record has to
        be self-describing; C is just a renderer."""
        row = {"ts": _now(), "actor": actor, "verb": verb, "target": target,
               "detail": detail, "session": session, "turn": turn}
        if bytes_ is not None:
            row["bytes"] = int(bytes_)
        if obj:
            row["object"] = obj
        if path:
            row["path"] = str(path)
        if tokens:
            key = (session, turn)
            self._spent[key] = self._spent.get(key, 0) + int(tokens)
            row["tokens"] = int(tokens)
            row["turn_tokens"] = self._spent[key]      # rolling, as it happens
        try:
            if self._fh is not None:
                with self._lock:
                    self._fh.write(json.dumps(row) + "\n")
        except (OSError, ValueError):
            pass
        # Mirror into the rolling log so one `tail -f mct.log` shows event flow
        # and file access in true interleaved order.
        if self._ledger is not None:
            size = f"  {bytes_}B" if bytes_ is not None else ""
            extra = f"  {detail}" if detail else ""
            try:
                self._ledger._write_log(
                    f"{row['ts']}  {actor:<{_ACTOR_W}} {verb:<{_VERB_W}} "
                    f"{target}{size}{extra}")
            except Exception:
                pass

    # --- read-back ---------------------------------------------------------
    def entries(self, session: str = "", limit: int | None = None) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                row = json.loads(ln)
            except ValueError:
                continue
            if session and row.get("session") not in ("", session):
                continue
            out.append(row)
        return out[-limit:] if limit else out

    def cat(self, session: str, pointer: str, max_bytes: int = 2000) -> str:
        """The actual bytes behind one record, bounded and printable."""
        if not self._resolve or not pointer:
            return "(no content)"
        try:
            data = self._resolve(session, pointer)
        except Exception as exc:
            return f"(unreadable: {type(exc).__name__}: {exc})"
        text = data[:max_bytes].decode("utf-8", errors="replace")
        if len(data) > max_bytes:
            text += f"\n… [{len(data) - max_bytes} more bytes]"
        return text

    def render(self, session: str = "", limit: int = 40, cat: bool = False,
               cat_bytes: int = 2000) -> str:
        """Operator view: the recent tail plus a per-file summary.

        With ``cat`` the actual correspondence is inlined under each line that
        has bytes behind it — the pull A sent, the slate B answered with, the
        snapshot A read. Reading the exchange should not require chasing
        pointers by hand through ``/where``."""
        rows = self.entries(session)
        if not rows:
            return "no file access recorded yet."
        tail = rows[-limit:]
        lines = [f"# file access — {len(rows)} record(s), showing last {len(tail)}"
                 + ("  [--cat: content inlined]" if cat else ""), ""]
        for r in tail:
            size = f"  {r['bytes']}B" if r.get("bytes") else ""
            det = f"  {r['detail']}" if r.get("detail") else ""
            ref = f"  {r['object'].rsplit('/', 1)[-1]}" if r.get("object") and not cat else ""
            run = f"  [Σ{_fmt_tokens(r['turn_tokens'])}]" if r.get("turn_tokens") else ""
            lines.append(f"  {r['ts'][11:23]}  {r['actor']:<10} {r['verb']:<12} "
                         f"{r['target']}{size}{det}{run}{ref}")
            if cat and r.get("object"):
                body = self.cat(session or r.get("session", ""), r["object"], cat_bytes)
                lines.append(f"      ┌─ {r['object'].rsplit('/', 1)[-1]}")
                lines += [f"      │ {ln}" for ln in body.splitlines() or [""]]
                lines.append("      └─")
        # Which files did each side touch, and how often? Only verbs that name a
        # real file count — protocol chatter and root scans are flow, not access.
        by: dict[tuple[str, str], int] = {}
        for r in rows:
            if r["verb"] not in FILE_VERBS:
                continue
            by[(r["target"], r["actor"])] = by.get((r["target"], r["actor"]), 0) + 1
        into_a = sum(r.get("tokens", 0) for r in rows)
        if into_a:
            lines += ["", f"# tokens into A: {_fmt_tokens(into_a)} "
                          f"(what A resolved; B's own reads are free)"]
        if by:
            lines += ["", "# per-file totals (A = mediated read, B = host access)"]
            for (target, actor), n in sorted(by.items(), key=lambda kv: (-kv[1], kv[0])):
                lines.append(f"  {n:>4}x  {actor:<10} {target}")
        return "\n".join(lines)
