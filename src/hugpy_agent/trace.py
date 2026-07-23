"""Structured delegation — dispatch packets and trace artifacts (P2.6).

Two harvested ideas (pattern source: the opencode-harness role-IO contracts;
the code here is original):

  1. DISPATCH CONTRACT: a delegation may carry a structured `packet` whose
     required fields are validated BEFORE the child runs. A brief that says
     only "fix it" produces children that wander; a packet forces the parent
     to state the objective, the scope, how it will VERIFY the work, and
     what it expects handed back — the keeper's assess→assign→verify loop
     made machine-checkable. Validation is FAIL-CLOSED, matching subagent.py:
     a malformed packet refuses the spawn as data (an error string the model
     can repair), never runs a child on a half-stated contract.

  2. TRACE ARTIFACT: every completed delegation leaves one markdown file —
     `<workspace>/.hugpy_agent/traces/<parent_run_id>/<child_run_id>.md` —
     recording what the child was ASKED (the packet), what HAPPENED (the
     journaled outcome), and the handoff expectation. The journal already
     holds every message; the artifact is the human/auditor view: greppable,
     diffable, one delegation per file, same doctrine as memory.py's
     markdown-not-a-DB choice. Artifact WRITING is FAIL-OPEN, matching
     audit.py: losing a trace line is bad; killing the spawn path over it is
     worse — failures degrade to an on_event line.

The asymmetry is deliberate and mirrors the rest of the codebase: gates that
decide whether work RUNS fail closed; evidence writers that record work that
ALREADY ran fail open.

File format: YAML-ish frontmatter (flat `key: value` lines between `---`
fences — hand-rolled, no yaml dep) + `## Dispatch` / `## Outcome` /
`## Handoff` sections with `- **field**: value` bullets. read_trace() parses
it back; round-trip fidelity is tested. Writes are atomic (tmp + rename in
the same directory) so a crash mid-write never leaves a torn artifact.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile

# The dispatch contract. Required string fields must be non-empty after
# strip; list fields must be lists of strings (may be empty). Kept flat and
# hand-rolled — the package is stdlib-only by doctrine, and five fields do
# not justify a schema engine.
REQUIRED_STR = ("objective", "scope", "verification", "handoff_expectations")
REQUIRED_LIST = ("constraints",)
OPTIONAL_LIST = ("context_refs",)
PACKET_FIELDS = REQUIRED_STR + REQUIRED_LIST + OPTIONAL_LIST


def validate_packet(packet) -> list[str]:
    """Errors in a dispatch packet; empty list = valid. Every problem is
    reported (not first-fail) so the model can repair the whole packet in
    one round-trip instead of discovering fields one refusal at a time."""
    errors: list[str] = []
    if not isinstance(packet, dict):
        return ["packet must be an object with fields: %s"
                % ", ".join(REQUIRED_STR + REQUIRED_LIST)]
    for name in REQUIRED_STR:
        val = packet.get(name)
        if not isinstance(val, str) or not val.strip():
            errors.append("packet.%s must be a non-empty string" % name)
    for name in REQUIRED_LIST + OPTIONAL_LIST:
        val = packet.get(name)
        if val is None:
            if name in REQUIRED_LIST:
                errors.append("packet.%s must be a list of strings "
                              "(may be empty)" % name)
            continue
        if (not isinstance(val, (list, tuple))
                or any(not isinstance(x, str) for x in val)):
            errors.append("packet.%s must be a list of strings" % name)
    unknown = sorted(k for k in packet if k not in PACKET_FIELDS)
    if unknown:
        errors.append("packet has unknown field(s): %s — allowed: %s"
                      % (", ".join(unknown), ", ".join(PACKET_FIELDS)))
    return errors


def synthesize_packet(task_text: str) -> dict:
    """The unstructured fallback: a spawn WITHOUT a packet still leaves a
    trace artifact, built from the only contract that exists — the brief.
    Marked `unstructured` so an auditor can tell a stated contract from a
    reconstructed one (never dress up a bare brief as a full dispatch)."""
    return {"objective": str(task_text or "").strip() or "(no brief)",
            "scope": "(unstated)",
            "constraints": [],
            "verification": "(unstated)",
            "handoff_expectations": "(unstated)",
            "unstructured": True}


def traces_dir(workspace: str) -> str:
    return os.path.join(os.path.realpath(workspace), ".hugpy_agent", "traces")


def trace_path(workspace: str, parent_run_id: str, child_run_id: str) -> str:
    return os.path.join(traces_dir(workspace), parent_run_id,
                        child_run_id + ".md")


def _fmt_value(val) -> str:
    """One packet/outcome value as artifact text. Lists render as JSON so
    empty lists, commas-in-items, and round-trip parsing stay unambiguous."""
    if isinstance(val, (list, tuple)):
        return json.dumps(list(val))
    return " ".join(str(val).split())    # single line; sections stay parseable


def _atomic_write(path: str, text: str) -> None:
    """tmp-in-same-dir + rename: readers see the old file or the whole new
    one, never a torn middle — same discipline as the store's staged
    downloads. fsync is skipped on purpose (trace loss on power cut is
    acceptable; the journal is the durable record)."""
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".trace-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def render_trace(parent_run_id: str, child_run_id: str, packet: dict,
                 outcome: dict, created_at: str | None = None) -> str:
    """The artifact text. `outcome` keys used: status (required-ish;
    defaults 'unknown'), summary, evidence — extra keys are ignored so
    callers can pass a journal report verbatim-ish without pre-cleaning."""
    status = str(outcome.get("status") or "unknown")
    created = created_at or _dt.datetime.now(_dt.timezone.utc).isoformat()
    lines = [
        "---",
        "parent_run_id: %s" % parent_run_id,
        "child_run_id: %s" % child_run_id,
        "created_at: %s" % created,
        "status: %s" % status,
        "---",
        "",
        "# Delegation trace: %s" % child_run_id,
        "",
        "## Dispatch",
        "",
    ]
    for name in PACKET_FIELDS + ("unstructured",):
        if name in packet:
            lines.append("- **%s**: %s" % (name, _fmt_value(packet[name])))
    lines += [
        "",
        "## Outcome",
        "",
        "- **status**: %s" % status,
        "- **summary**: %s" % _fmt_value(outcome.get("summary") or ""),
        "- **evidence**: %s" % _fmt_value(outcome.get("evidence") or ""),
        "",
        "## Handoff",
        "",
        "- **expected**: %s" % _fmt_value(
            packet.get("handoff_expectations") or "(unstated)"),
        "- **delivered**: %s" % _fmt_value(outcome.get("summary") or ""),
        "",
    ]
    return "\n".join(lines)


def write_trace(workspace: str, parent_run_id: str, child_run_id: str,
                packet: dict, outcome: dict,
                created_at: str | None = None) -> str:
    """Write one delegation's artifact atomically and append its INDEX.md
    line. Returns the artifact path. Raises on failure — the CALLER decides
    the failure posture (subagent.py wraps this fail-open; a CLI audit
    command would want the exception)."""
    path = trace_path(workspace, parent_run_id, child_run_id)
    _atomic_write(path, render_trace(parent_run_id, child_run_id, packet,
                                     outcome, created_at))
    _append_index(workspace, parent_run_id, child_run_id,
                  str(packet.get("objective") or ""),
                  str(outcome.get("status") or "unknown"))
    return path


def _append_index(workspace: str, parent_run_id: str, child_run_id: str,
                  objective: str, status: str) -> None:
    """One line per artifact in `<traces>/INDEX.md` — the same
    index-plus-fact-files shape as memory.py, so `cat INDEX.md` answers
    "what has been delegated here?" without walking the tree. Plain append
    (O_APPEND line-atomicity, like audit.py) — the per-artifact files are
    the record; the index is a convenience view."""
    index = os.path.join(traces_dir(workspace), "INDEX.md")
    os.makedirs(os.path.dirname(index), exist_ok=True)
    objective = " ".join(objective.split())[:80]
    line = ("- [%s](%s/%s.md) — %s — %s\n"
            % (child_run_id, parent_run_id, child_run_id, objective, status))
    new = not os.path.exists(index)
    with open(index, "a", encoding="utf-8") as fh:
        if new:
            fh.write("# Delegation traces\n\n")
        fh.write(line)
        fh.flush()


def read_trace(path: str) -> dict:
    """Parse an artifact back to {frontmatter fields..., 'dispatch': {...},
    'outcome': {...}, 'handoff': {...}}. Tolerant of extra prose between
    bullets (humans may annotate artifacts); strict about the frontmatter
    fence. Raises ValueError on a file that is not a trace artifact."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("not a trace artifact (missing frontmatter): %s" % path)
    out: dict = {}
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        if ":" in lines[i]:
            k, _, v = lines[i].partition(":")
            out[k.strip()] = v.strip()
        i += 1
    if i >= len(lines):
        raise ValueError("unterminated frontmatter: %s" % path)
    section = None
    sections: dict[str, dict] = {"dispatch": {}, "outcome": {}, "handoff": {}}
    for line in lines[i + 1:]:
        stripped = line.strip()
        if stripped.startswith("## "):
            section = stripped[3:].strip().lower()
            continue
        if section in sections and stripped.startswith("- **"):
            body = stripped[4:]
            key, sep, val = body.partition("**:")
            if sep:
                val = val.strip()
                if val.startswith("["):          # list fields round-trip as JSON
                    try:
                        val = json.loads(val)
                    except json.JSONDecodeError:
                        pass
                elif val == "True":
                    val = True
                elif val == "False":
                    val = False
                sections[section][key.strip()] = val
    out.update(sections)
    return out
