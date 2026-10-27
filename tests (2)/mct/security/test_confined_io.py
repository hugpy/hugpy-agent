"""Descriptor-confined filesystem reads (design §13.3-13.4, registry row 9)."""
import os

import pytest

from hugpy_agent.mct.confined_io import ConfinedRoot
from hugpy_agent.mct.errors import AuthorizationError, BudgetError, NotFoundError


@pytest.fixture
def root(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "log.txt").write_text("line1\nline2\nSECRET\n")
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("outside")
    os.symlink(outside, tmp_path / "escape")
    os.symlink("/etc/hostname", tmp_path / "hostlink")
    r = ConfinedRoot(str(tmp_path))
    yield r
    r.close()


def test_reads_file_beneath_root(root):
    assert root.read("sub/log.txt").startswith(b"line1")


def test_absolute_path_rejected(root):
    with pytest.raises(AuthorizationError):
        root.read("/etc/hostname")


def test_parent_traversal_rejected(root):
    with pytest.raises(AuthorizationError):
        root.read("../outside_secret.txt")


def test_symlink_escape_rejected(root):
    with pytest.raises(AuthorizationError):
        root.read("escape")


def test_symlink_to_etc_rejected(root):
    with pytest.raises(AuthorizationError):
        root.read("hostlink")


def test_missing_file(root):
    with pytest.raises(NotFoundError):
        root.read("nope.txt")


def test_oversized_read_fails_closed(tmp_path):
    (tmp_path / "big.txt").write_bytes(b"x" * 5000)
    r = ConfinedRoot(str(tmp_path), max_bytes=1000)
    with pytest.raises(BudgetError):
        r.read("big.txt")
    r.close()
