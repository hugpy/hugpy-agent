"""Tool-call adapter — the heart of Phase 1 (design §3.1).

The /v1 seam silently ignores `tools` today (live-probed §2.1), so the harness
owns tool-calling with a three-tier fallback chain:

  1. NATIVE     — pass `tools` through, parse `message.tool_calls`. Dormant
                  until a probe shows the seam supports it; kept so it
                  activates the day platform P0 lands, with zero code change
                  above this module.
  2. PROMPTED   — inject tool JSON schemas into the system prompt using the
                  Qwen2.5/Hermes convention and parse
                  `<tool_call>{...}</tool_call>` blocks. Default tier: the
                  fleet's Qwen-family GGUFs were trained on exactly this
                  format, so a 3B model can drive it.
  3. CONSTRAINED — plain-JSON answer contract for models with no
                  function-calling template at all.

Sloppy-small-model posture (design §8 risk #1): schema validation with benign
type coercion, ONE repair round-trip on invalid output, and errors returned
as data — the loop decides when repeated failure becomes a structured abort.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from .gateway import CONTINUATION_LEAK

MODE_NATIVE = "native"
MODE_PROMPTED = "prompted"
MODE_CONSTRAINED = "constrained"

_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
# A reply that LEADS with the final_answer tool name (optionally backticked /
# colon-separated) and then the answer as prose — explicit termination signal,
# degraded envelope. See Adapter._marker_final_answer.
_FINAL_MARKER_RE = re.compile(
    r"^\s*`{0,3}final_answer`{0,3}\s*[:\n]\s*(.+?)\s*$", re.DOTALL)
# Thinking-model reasoning spans. A Qwen3-family brain may emit <think>…</think>
# even when asked not to; the reasoning must never reach the tool_call parser
# or a final answer. Two patterns: a closed block, and a DANGLING open tag
# (budget ran out before the close) — the latter is dropped from the tag to the
# end of the text. Applied unconditionally (belt-and-suspenders on top of the
# /no_think wire suffix) so BOTH tiers benefit.
_THINK_CLOSED_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)
# Known junk strings the platform can leak into replies (design §2.3). Scrubbed
# before parsing because a leak INSIDE a JSON block corrupts structured output.
_LEAKS = (CONTINUATION_LEAK,)


def strip_think(text: str) -> str:
    """Remove <think>…</think> reasoning spans, preserving all text outside
    them. Closed blocks first, then any remaining dangling <think> (no close)
    to end-of-text. Whitespace at the seams is collapsed so a stripped block
    doesn't leave a gaping gap the JSON scanner has to step over."""
    if not text or "<think>" not in text.lower():
        return text
    text = _THINK_CLOSED_RE.sub("", text)
    text = _THINK_OPEN_RE.sub("", text)
    return text.strip()


@dataclass
class ToolCall:
    name: str
    arguments: dict
    raw: str = ""


@dataclass
class ParseOutcome:
    calls: list[ToolCall] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)   # human-readable, sent back to the model
    plain_text: str = ""                              # text with tool_call blocks removed


def scrub(text: str) -> str:
    """Remove known platform leak strings. Defense in depth on top of
    max_chunks:1 — a leaked continuation prompt mid-JSON is unparseable."""
    for leak in _LEAKS:
        text = text.replace(leak, "")
    return text


# ── prompt rendering ────────────────────────────────────────────────────────
_PROMPTED_TEMPLATE = """
# Tools

You may call ONE function per reply to assist with the task.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{tool_lines}
</tools>

For a function call, return a json object with function name and arguments \
within <tool_call></tool_call> XML tags, then STOP:
<tool_call>
{{"name": "<function-name>", "arguments": {{<args-json-object>}}}}
</tool_call>

The function result will come back inside <tool_response></tool_response> tags.
Never invent a function result — wait for the real one.
""".rstrip()

_CONSTRAINED_TEMPLATE = """
# Actions

Reply with ONLY a single JSON object (no prose, no markdown fences) choosing
one action per reply:
  {{"name": "<action-name>", "arguments": {{...}}}}

Available actions and their argument schemas:
{tool_lines}

The action result comes back as a JSON message. Never invent a result.
""".rstrip()


class Adapter:
    """Renders tool schemas into the system prompt and extracts/validates
    tool calls from model output, per the active mode."""

    def __init__(self, mode: str = MODE_PROMPTED):
        self.mode = mode

    # ── system-prompt injection ──────────────────────────────────────────
    def system_prompt_block(self, tool_specs) -> str:
        """tool_specs: iterable with .name, .description, .parameters."""
        lines = []
        for t in tool_specs:
            lines.append(json.dumps({
                "type": "function",
                "function": {"name": t.name, "description": t.description,
                             "parameters": t.parameters},
            }, separators=(",", ":")))
        joined = "\n".join(lines)
        if self.mode == MODE_NATIVE:
            # Native: the wire carries the schemas; the prompt only sets rules.
            return ("# Tools\nUse the provided function-calling interface. "
                    "Call ONE function per reply.")
        if self.mode == MODE_CONSTRAINED:
            return _CONSTRAINED_TEMPLATE.format(tool_lines=joined)
        return _PROMPTED_TEMPLATE.format(tool_lines=joined)

    def wire_tools(self, tool_specs):
        """OpenAI `tools` array for the native tier; None otherwise (sending
        it in prompted mode would waste context on a field /v1 ignores)."""
        if self.mode != MODE_NATIVE:
            return None
        return [{"type": "function",
                 "function": {"name": t.name, "description": t.description,
                              "parameters": t.parameters}}
                for t in tool_specs]

    # ── extraction ───────────────────────────────────────────────────────
    def extract(self, text: str, native_tool_calls=None) -> ParseOutcome:
        out = ParseOutcome()
        # Strip reasoning spans BEFORE scrubbing/parsing: a <think> block can
        # contain braces and prose that would otherwise derail the JSON scanner
        # or become plain_text/a final answer.
        text = scrub(strip_think(text or ""))
        if self.mode == MODE_NATIVE and native_tool_calls:
            for c in native_tool_calls:
                fn = (c or {}).get("function") or {}
                args_raw = fn.get("arguments")
                try:
                    args = (json.loads(args_raw)
                            if isinstance(args_raw, str) else (args_raw or {}))
                    if not isinstance(args, dict):
                        raise ValueError("arguments is not an object")
                    out.calls.append(ToolCall(fn.get("name") or "", args,
                                              raw=json.dumps(c)))
                except (json.JSONDecodeError, ValueError) as exc:
                    out.errors.append("invalid native tool_call arguments for %r: %s"
                                      % (fn.get("name"), exc))
            out.plain_text = text.strip()
            return out

        remaining = text
        for m in _TOOL_CALL_RE.finditer(text):
            block = m.group(1)
            remaining = remaining.replace(m.group(0), "")
            call, err = self._parse_call_json(block)
            if call:
                out.calls.append(call)
            else:
                out.errors.append(err)
        out.plain_text = remaining.strip()

        if not out.calls and not out.errors:
            # Tolerance for sloppy small models: an un-fenced bare JSON call
            # object anywhere in the reply (also the constrained tier's
            # primary format). Better to accept a slightly-off format than to
            # burn a repair round-trip on it.
            call, err = self._bare_call(text)
            if not call and not err:
                call = self._marker_final_answer(text)
            if call:
                out.calls.append(call)
                out.plain_text = ""
            elif err:
                out.errors.append(err)
        return out

    def _marker_final_answer(self, text: str):
        """`final_answer\\n<prose>` — the model declared termination but
        skipped the JSON envelope. The signal is explicit (this is NOT bare
        prose, which stays rejected), so honor it: the alternative is
        aborting a run whose answer is already in hand."""
        m = _FINAL_MARKER_RE.match(text)
        if m and m.group(1).strip():
            return ToolCall("final_answer", {"answer": m.group(1).strip()},
                            raw=text)
        return None

    def _parse_call_json(self, block: str):
        try:
            data = json.loads(block)
        except json.JSONDecodeError as exc:
            return None, ("tool_call block is not valid JSON (%s). Block was: %s"
                          % (exc, block[:300]))
        if isinstance(data, dict) and set(data) == {"final_answer"}:
            # Shorthand small models fall into constantly: {"final_answer":
            # "..."} instead of {"name": "final_answer", "arguments":
            # {"answer": "..."}}. The intent is unambiguous — accept it
            # rather than abort a run whose answer is already in hand.
            fa = data["final_answer"]
            if isinstance(fa, dict) and isinstance(fa.get("answer"), str):
                return ToolCall("final_answer", {"answer": fa["answer"]},
                                raw=block), ""
            if isinstance(fa, str):
                return ToolCall("final_answer", {"answer": fa}, raw=block), ""
        if not isinstance(data, dict) or not data.get("name"):
            return None, ("tool_call JSON must be an object with 'name' and "
                          "'arguments'. Got: %s" % block[:300])
        args = data.get("arguments", {})
        if isinstance(args, str):
            # Models sometimes double-encode arguments; unwrap one level.
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return None, ("'arguments' for %r is a string that is not valid "
                              "JSON: %s" % (data["name"], args[:200]))
        if not isinstance(args, dict):
            return None, "'arguments' for %r must be a JSON object" % data["name"]
        return ToolCall(str(data["name"]), args, raw=block), ""

    def _bare_call(self, text: str):
        """Find the first standalone {"name":..., "arguments":...} object.
        Scans balanced-brace candidates rather than regex-matching JSON, since
        nested braces defeat any regex."""
        idx = 0
        while True:
            start = text.find("{", idx)
            if start < 0:
                return None, ""
            depth = 0
            for i in range(start, len(text)):
                ch = text[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:i + 1]
                        if '"name"' in candidate or '"final_answer"' in candidate:
                            call, err = self._parse_call_json(candidate)
                            if call:
                                return call, ""
                        break
            else:
                return None, ""
            idx = start + 1

    # ── validation ───────────────────────────────────────────────────────
    def validate(self, schema: dict, args: dict):
        """(errors, normalized_args) against a JSON-schema subset: object
        properties, required, primitive types, enum, array items.

        Benign coercions (str->int/float/bool where unambiguous) are applied
        instead of rejected: small models quote numbers constantly, and a
        silent fix here saves a whole model round-trip (design §8 risk #1).
        """
        errors: list[str] = []
        norm = dict(args)
        props = schema.get("properties") or {}
        for req in schema.get("required") or []:
            if req not in args:
                errors.append("missing required argument %r" % req)
        for k, v in args.items():
            spec = props.get(k)
            if spec is None:
                continue  # unknown extras tolerated; schemas here are advisory
            ok, coerced, err = _check_type(k, v, spec)
            if not ok:
                errors.append(err)
            else:
                norm[k] = coerced
        return errors, norm

    # ── messages back to the model ───────────────────────────────────────
    def tool_response_message(self, name: str, result: str) -> dict:
        """Wrap a tool result for the wire. Prompted/constrained tiers send it
        as a user turn (the /v1 seam has no real 'tool' role today); the Qwen
        convention is <tool_response> tags."""
        if self.mode == MODE_NATIVE:
            return {"role": "tool", "name": name, "content": result}
        if self.mode == MODE_CONSTRAINED:
            return {"role": "user",
                    "content": json.dumps({"action_result": {"name": name,
                                                             "result": result}})}
        return {"role": "user",
                "content": "<tool_response>\n%s\n</tool_response>"
                           % json.dumps({"name": name, "result": result})}

    def repair_message(self, errors: list[str]) -> dict:
        """The ONE repair round-trip: tell the model exactly what was wrong
        and restate the contract. Sent as a user turn so any model sees it."""
        detail = "\n".join("- %s" % e for e in errors)
        if self.mode == MODE_CONSTRAINED:
            fmt = 'a single JSON object {"name": ..., "arguments": {...}}'
        else:
            fmt = ('<tool_call>\n{"name": "<function-name>", "arguments": '
                   '{<args>}}\n</tool_call>')
        return {"role": "user", "content":
                "Your last reply had an invalid tool call:\n%s\n\n"
                "Reply again with a corrected call, formatted EXACTLY as:\n%s\n"
                "Output nothing else." % (detail, fmt)}


def _check_type(key: str, value, spec: dict):
    """One property check. Returns (ok, coerced_value, error)."""
    t = spec.get("type")
    enum = spec.get("enum")
    if enum is not None and value not in enum:
        return False, value, ("argument %r must be one of %s, got %r"
                              % (key, enum, value))
    if t is None:
        return True, value, ""
    if t == "string":
        if isinstance(value, str):
            return True, value, ""
        return False, value, "argument %r must be a string, got %s" % (key, type(value).__name__)
    if t == "integer":
        if isinstance(value, bool):
            return False, value, "argument %r must be an integer, got bool" % key
        if isinstance(value, int):
            return True, value, ""
        if isinstance(value, str):
            try:
                return True, int(value.strip()), ""
            except ValueError:
                pass
        if isinstance(value, float) and value.is_integer():
            return True, int(value), ""
        return False, value, "argument %r must be an integer, got %r" % (key, value)
    if t == "number":
        if isinstance(value, bool):
            return False, value, "argument %r must be a number, got bool" % key
        if isinstance(value, (int, float)):
            return True, value, ""
        if isinstance(value, str):
            try:
                return True, float(value.strip()), ""
            except ValueError:
                pass
        return False, value, "argument %r must be a number, got %r" % (key, value)
    if t == "boolean":
        if isinstance(value, bool):
            return True, value, ""
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return True, value.strip().lower() == "true", ""
        return False, value, "argument %r must be a boolean, got %r" % (key, value)
    if t == "array":
        if not isinstance(value, list):
            return False, value, "argument %r must be an array, got %s" % (key, type(value).__name__)
        items = spec.get("items")
        if items:
            coerced = []
            for i, item in enumerate(value):
                ok, c, err = _check_type("%s[%d]" % (key, i), item, items)
                if not ok:
                    return False, value, err
                coerced.append(c)
            return True, coerced, ""
        return True, value, ""
    if t == "object":
        if isinstance(value, dict):
            return True, value, ""
        return False, value, "argument %r must be an object, got %s" % (key, type(value).__name__)
    return True, value, ""  # unknown schema type: fail open, schemas are ours


def probe_native(gateway, model: str) -> bool:
    """One tiny live call: does the seam pass `tools` through? If the model
    comes back with native tool_calls for a trivial forced call, the native
    tier is real. Any error means 'no' — fail closed to prompted, which
    always works.

    THIS PROBE IS EXPENSIVE SERVER-SIDE TODAY — never call it unasked, and
    only ever through cached_probe_native(). The hugpy central keeper's
    2026-07-14 packet capture (tracing this very request as a "mystery
    caller" incident) showed the /v1 shim does NOT forward `max_chunks` to
    the worker: the central->worker hop carried max_chunks:null despite our
    max_chunks:1, so a model that never saw the tools rambles and central's
    continuation machinery extends the generation — ~58s of GPU per probe.
    Hence max_tokens=16 (the smallest budget that still fits a tool_calls
    reply) and a short dedicated 25s timeout so a stalled request cannot
    hold a deployment for the full chat timeout. That same non-forwarding
    bug is why tools-carrying requests appear to 'stall' at the seam."""
    tools = [{"type": "function",
              "function": {"name": "ping",
                           "description": "Reply check. Call this.",
                           "parameters": {"type": "object", "properties": {},
                                          "required": []}}}]
    try:
        res = gateway.chat(
            [{"role": "user", "content": "Call the ping function now."}],
            model=model, max_tokens=16, stream=False, tools=tools, retries=0,
            timeout=25)
        return bool(res.ok and res.native_tool_calls)
    except Exception:
        return False


# ── probe result cache (per box, NOT per workspace) ─────────────────────────
# Incident root cause #1 (2026-07-14): the probe result was cached in the
# per-workspace journal, so every fresh workspace/test re-probed dev. The
# cache now lives in one per-user file so a box probes a (base, model) pair
# at most once per TTL across all workspaces.
PROBE_TTL = 7 * 24 * 3600     # seconds; seam capabilities change rarely


def probe_cache_path() -> str:
    """~/.cache/hugpy_agent/probe.json, honoring XDG_CACHE_HOME. Resolved on
    every call (not at import) so tests and sandboxes can redirect it via
    the environment."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache")
    return os.path.join(base, "hugpy_agent", "probe.json")


def _load_probe_cache(path: str) -> dict:
    """Corruption-tolerant read: an unreadable/invalid file means an empty
    cache (worst case: ONE extra probe, then it is rewritten valid), never a
    crash and never a probe loop."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _store_probe_cache(path: str, data: dict) -> None:
    """Atomic write (tmp file + os.replace): concurrent agents on one box
    must never observe a half-written cache — a torn file would read as
    'empty' and trigger avoidable re-probes."""
    import tempfile
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def cached_probe_native(gateway, model: str, ttl: int = PROBE_TTL,
                        cache_path: str | None = None) -> bool:
    """probe_native() behind the per-box file cache — the ONLY entry point
    callers may use. Guarantees at most one live probe per (base, model) per
    TTL on this machine, regardless of how many workspaces exist."""
    import time
    path = cache_path or probe_cache_path()
    key = "%s|%s" % (getattr(gateway, "base", "?"), model)
    cache = _load_probe_cache(path)
    entry = cache.get(key)
    if (isinstance(entry, dict) and isinstance(entry.get("ts"), (int, float))
            and "native" in entry and time.time() - entry["ts"] < ttl):
        return bool(entry["native"])
    result = probe_native(gateway, model)
    cache[key] = {"native": result, "ts": time.time()}
    _store_probe_cache(path, cache)
    return result
