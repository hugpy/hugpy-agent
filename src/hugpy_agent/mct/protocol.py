"""Control-plane protocol: envelopes, schema validation, pointers, framing.

Design ref: §6.1, §8, §21 (``protocol.py``). The control plane carries
references and routing metadata only — never conversational bodies (invariant 3).
This module is the single enforcement point for envelope shape (registry row 1).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

from .errors import ProtocolError

PROTOCOL_VERSION = "mct/1"
MAX_ENVELOPE_BYTES = 64 * 1024  # oversized envelopes are rejected (§23)

_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

# Message types and their legal directions (design §8.2). Direction is advisory
# metadata here; the session loop enforces who may send what.
MESSAGE_TYPES = {
    "context.ready": "B->A",
    "pull.requested": "A->B",
    "pull.ready": "B->A",
    "pull.denied": "B->A",
    "response.started": "A->B",
    "response.ready": "A->B",
    "receipt.ready": "adapter->B",
    "turn.cancelled": "B->A",
    "epoch.changed": "either",
    "error": "either",
}

_POINTER_RE = re.compile(r"^mct://broker/session/(s_[A-Za-z0-9]+)/object/(o_[A-Za-z0-9_]+)$")


@lru_cache(maxsize=None)
def load_validator(schema_name: str) -> Draft202012Validator:
    """Return a cached validator for a schema in ``schemas/`` (e.g. ``envelope-v1``)."""
    path = _SCHEMA_DIR / f"{schema_name}.json"
    if not path.exists():
        raise ProtocolError(f"unknown schema: {schema_name}")
    schema = json.loads(path.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_against(schema_name: str, payload: dict) -> None:
    """Raise :class:`ProtocolError` if ``payload`` violates the named schema."""
    validator = load_validator(schema_name)
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        loc = "/".join(str(p) for p in e.path) or "<root>"
        raise ProtocolError(f"{schema_name} invalid at {loc}: {e.message}")


# --- Pointers (design §7.2) ------------------------------------------------

def make_pointer(session_id: str, object_id: str) -> str:
    return f"mct://broker/session/{session_id}/object/{object_id}"


def parse_pointer(pointer: str) -> tuple[str, str]:
    """Return ``(session_id, object_id)`` or raise. A pointer is an opaque handle,
    never a filesystem path (invariant 5); anything path-shaped fails here."""
    if not isinstance(pointer, str):
        raise ProtocolError("pointer must be a string")
    m = _POINTER_RE.match(pointer)
    if not m:
        raise ProtocolError(f"not a valid mct:// pointer: {pointer!r}")
    return m.group(1), m.group(2)


# --- Envelopes (design §8.1) ----------------------------------------------

@dataclass
class Envelope:
    type: str
    session_id: str
    turn_id: str
    sequence: int
    epoch: str
    object: str | None = None
    sha256: str | None = None
    media_type: str | None = None
    bytes: int | None = None
    idempotency_key: str | None = None
    expires_at: str | None = None
    error_code: str | None = None
    v: str = PROTOCOL_VERSION

    def to_dict(self) -> dict:
        d = {
            "v": self.v,
            "type": self.type,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "sequence": self.sequence,
            "epoch": self.epoch,
        }
        for k in ("object", "sha256", "media_type", "bytes",
                  "idempotency_key", "expires_at", "error_code"):
            val = getattr(self, k)
            if val is not None:
                d[k] = val
        return d


def encode(env: Envelope) -> bytes:
    """Validate then serialize to length-prefixed framed bytes (stdio/socket framing)."""
    d = env.to_dict()
    validate_against("envelope-v1", d)
    body = json.dumps(d, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(body) > MAX_ENVELOPE_BYTES:
        raise ProtocolError("envelope exceeds size limit")
    return len(body).to_bytes(4, "big") + body


def decode(raw: bytes | dict) -> Envelope:
    """Enforcement point for accepting a control message (registry row 1).

    Accepts either framed bytes or an already-parsed dict (in-process transport).
    Rejects oversize, malformed JSON, wrong version, or schema-invalid messages.
    """
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) < 4:
            raise ProtocolError("truncated frame")
        length = int.from_bytes(raw[:4], "big")
        body = raw[4:4 + length]
        if len(body) != length:
            raise ProtocolError("frame length mismatch")
        if length > MAX_ENVELOPE_BYTES:
            raise ProtocolError("envelope exceeds size limit")
        try:
            d = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"malformed JSON: {exc}") from exc
    elif isinstance(raw, dict):
        d = raw
    else:
        raise ProtocolError("envelope must be bytes or dict")

    if d.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version: {d.get('v')!r}")
    validate_against("envelope-v1", d)
    known = {f for f in Envelope.__dataclass_fields__}
    return Envelope(**{k: v for k, v in d.items() if k in known})
