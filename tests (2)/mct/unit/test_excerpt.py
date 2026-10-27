"""Excerpt selectors: lines/bytes/JSON/AST/match (design §7.3, §22 Phase 2)."""
import json

import pytest

from hugpy_agent.mct.errors import NotFoundError, ProtocolError
from hugpy_agent.mct.excerpt import apply_selector

TEXT = b"alpha\nbeta\ngamma\ndelta\n"
CODE = b"import os\n\n\ndef foo(x):\n    return x + 1\n\n\nclass Bar:\n    y = 2\n"


def test_lines():
    assert apply_selector(TEXT, "lines 2-3") == b"beta\ngamma\n"
    assert apply_selector(TEXT, "lines 1") == b"alpha\n"


def test_lines_out_of_range():
    with pytest.raises(NotFoundError):
        apply_selector(TEXT, "lines 99-100")


def test_bytes():
    assert apply_selector(TEXT, "bytes 0-5") == b"alpha"
    assert apply_selector(TEXT, "bytes 6-10") == b"beta"


def test_json_path():
    data = json.dumps({"a": {"b": [10, 20, 30]}}).encode()
    assert apply_selector(data, "json a.b[1]") == b"20"
    assert apply_selector(data, "json $.a.b[2]") == b"30"


def test_json_missing_path():
    with pytest.raises(NotFoundError):
        apply_selector(json.dumps({"a": 1}).encode(), "json a.b.c")


def test_symbol_function_and_class():
    assert apply_selector(CODE, "symbol foo") == b"def foo(x):\n    return x + 1"
    assert apply_selector(CODE, "symbol Bar").startswith(b"class Bar:")


def test_symbol_not_found():
    with pytest.raises(NotFoundError):
        apply_selector(CODE, "symbol nonexistent")


def test_match_with_context():
    assert apply_selector(TEXT, "match beta") == b"beta\n"
    assert apply_selector(TEXT, "match gamma ctx 1") == b"beta\ngamma\ndelta\n"


def test_unknown_selector_rejected():
    with pytest.raises(ProtocolError):
        apply_selector(TEXT, "wat 1-2")
