"""Identifier generation.

ULID-style identifiers (48-bit millisecond timestamp + 80 bits of randomness,
Crockford base32) give sortable, opaque IDs matching the design's ``s_01J...``
form. Turn and pull-request identifiers are *numeric counters* to satisfy the
frozen schema patterns (``t_[0-9]{6,}``, ``pr_[0-9]{4,}``) and to be monotonic
per their parent (design §9.1, §15.1); those are minted by the ledger, not here.
"""
from __future__ import annotations

import os
import time

# Crockford's base32 alphabet (no I, L, O, U).
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid() -> str:
    """26-char Crockford base32 ULID: 10 chars time + 16 chars randomness."""
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rnd = int.from_bytes(os.urandom(10), "big")  # 80 bits
    value = (ms << 80) | rnd
    chars = []
    for _ in range(26):
        chars.append(_B32[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_session_id() -> str:
    return "s_" + _ulid()


def new_epoch_id() -> str:
    return "e_" + _ulid()


def new_object_id() -> str:
    return "o_" + _ulid()


def new_event_id() -> str:
    return "ev_" + _ulid()


def format_turn_id(n: int) -> str:
    """Monotonic per-session turn id, e.g. ``t_000042`` (schema: ``t_[0-9]{6,}``)."""
    return f"t_{n:06d}"


def format_request_id(n: int) -> str:
    """Monotonic per-turn pull-request id, e.g. ``pr_0003`` (schema: ``pr_[0-9]{4,}``)."""
    return f"pr_{n:04d}"
