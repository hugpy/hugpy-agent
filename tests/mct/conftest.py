"""Shared test fixtures. Puts ``src/`` on the path and gives each test a fresh broker."""
import sys
import tempfile
from pathlib import Path

import pytest

# repo/tests/mct/conftest.py -> repo/src. This was `parent.parent / "src"`, i.e.
# repo/tests/src, which does not exist — so the shim was a no-op and the suite
# silently exercised whatever `hugpy_agent` happened to be pip-installed instead
# of this tree. Assert it, because a path shim that quietly misses is worse than
# no shim at all.
_SRC = Path(__file__).resolve().parent.parent.parent / "src"
assert (_SRC / "hugpy_agent" / "__init__.py").exists(), f"source tree not at {_SRC}"
sys.path.insert(0, str(_SRC))

from hugpy_agent.mct.session import BrokerServer  # noqa: E402


@pytest.fixture
def broker(tmp_path):
    printed = []
    server = BrokerServer(tmp_path, sink=printed.append)
    server.printed = printed  # expose captured "terminal" output to tests
    yield server
    server.close()


@pytest.fixture
def session(broker):
    sid = broker.open_session("test")
    return broker.session(sid)
