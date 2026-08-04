"""Control-plane protocol: envelope validation, framing, pointers (design §8)."""
import pytest

from hugpy_agent.mct.errors import ProtocolError
from hugpy_agent.mct.protocol import (Envelope, decode, encode, make_pointer,
                                      parse_pointer)


def _env(**over):
    base = dict(type="context.ready", session_id="s_01J", turn_id="t_000042",
                sequence=1, epoch="e_01J")
    base.update(over)
    return Envelope(**base)


def test_encode_decode_roundtrip():
    env = _env(object=make_pointer("s_01J", "o_01J"))
    restored = decode(encode(env))
    assert restored.type == "context.ready" and restored.sequence == 1


def test_decode_rejects_wrong_version():
    with pytest.raises(ProtocolError):
        decode({"v": "mct/2", "type": "context.ready", "session_id": "s_01J",
                "turn_id": "t_000042", "sequence": 1, "epoch": "e_01J"})


def test_decode_rejects_unknown_type():
    with pytest.raises(ProtocolError):
        decode(_env(type="context.maybe").to_dict())


def test_decode_rejects_inline_body():
    d = _env().to_dict()
    d["prompt"] = "smuggled body"  # invariant 3
    with pytest.raises(ProtocolError):
        decode(d)


def test_oversized_frame_rejected():
    from hugpy_agent.mct.protocol import MAX_ENVELOPE_BYTES
    raw = (MAX_ENVELOPE_BYTES + 1).to_bytes(4, "big") + b"x" * (MAX_ENVELOPE_BYTES + 1)
    with pytest.raises(ProtocolError):
        decode(raw)


def test_pointer_roundtrip_and_rejects_paths():
    assert parse_pointer(make_pointer("s_A", "o_B")) == ("s_A", "o_B")
    for bad in ("/etc/shadow", "mct://broker/session/s_A/object/../x", "o_B", ""):
        with pytest.raises(ProtocolError):
            parse_pointer(bad)
