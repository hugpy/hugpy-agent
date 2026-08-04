"""Shared test fixtures. Puts ``src/`` on the path and gives each test a fresh broker."""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

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
