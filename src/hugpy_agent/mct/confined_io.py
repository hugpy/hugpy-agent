"""Descriptor-confined filesystem reads.

Design ref: §13.3 (safe path resolution), §13.4 (snapshot boundary), §20.4 (fix
the ``fs_read`` realpath race), §21 (``confined_io.py``). Enforcement rows 9–10.

Authorization is **descriptor-based, not string-prefix**. We pre-open an allowed
root directory once and resolve every path *beneath that fd* with ``openat2`` and
``RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS`` (plus ``RESOLVE_NO_SYMLINKS`` unless a
root opts in). ``realpath()`` alone is rejected by the design because it leaves a
time-of-check/time-of-use race between the check and the ``open`` — here there is
no window, because the kernel resolves relative to the pinned fd and refuses to
escape it. A per-component ``O_NOFOLLOW`` ``openat`` fallback covers kernels
without ``openat2``.
"""
from __future__ import annotations

import ctypes
import errno
import os

from .errors import AuthorizationError, BudgetError, NotFoundError

# openat2(2) — x86_64 syscall number; struct open_how resolve flags (uapi/linux/openat2.h)
_SYS_openat2 = 437
RESOLVE_NO_XDEV = 0x01
RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08

_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


class _open_how(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64),
                ("resolve", ctypes.c_uint64)]


def _openat2(dirfd: int, path: str, flags: int, resolve: int) -> int:
    how = _open_how(flags=flags, mode=0, resolve=resolve)
    res = _libc.syscall(ctypes.c_long(_SYS_openat2), ctypes.c_int(dirfd),
                        ctypes.c_char_p(path.encode()), ctypes.byref(how),
                        ctypes.c_size_t(ctypes.sizeof(how)))
    if res < 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    return res


class ConfinedRoot:
    """A pinned allowed-directory fd; opens files only strictly beneath it."""

    def __init__(self, root_path: str, *, allow_symlinks: bool = False,
                 max_bytes: int = 16 * 1024 * 1024):
        self.root_path = os.path.realpath(root_path)
        if not os.path.isdir(self.root_path):
            raise NotFoundError(f"root is not a directory: {root_path}")
        self.dirfd = os.open(self.root_path, os.O_RDONLY | os.O_DIRECTORY)
        self.allow_symlinks = allow_symlinks
        self.max_bytes = max_bytes

    def close(self) -> None:
        try:
            os.close(self.dirfd)
        except OSError:
            pass

    def _reject_relpath(self, relpath: str) -> None:
        if relpath.startswith("/"):
            raise AuthorizationError("absolute paths are not permitted")
        if ".." in relpath.split("/"):
            raise AuthorizationError("parent traversal is not permitted")

    def open_read(self, relpath: str) -> int:
        """Open ``relpath`` beneath the root, returning a file descriptor."""
        self._reject_relpath(relpath)
        resolve = RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV
        if not self.allow_symlinks:
            resolve |= RESOLVE_NO_SYMLINKS
        try:
            return _openat2(self.dirfd, relpath, os.O_RDONLY, resolve)
        except OSError as exc:
            if exc.errno in (errno.ENOSYS, errno.EPERM):
                return self._fallback_open(relpath)      # kernel/sandbox lacks openat2
            if exc.errno in (errno.ELOOP, errno.EXDEV):
                raise AuthorizationError("symlink/mount escape rejected") from exc
            if exc.errno == errno.ENOENT:
                raise NotFoundError(f"no such source: {relpath}") from exc
            raise AuthorizationError(f"confined open failed: {os.strerror(exc.errno)}") from exc

    def _fallback_open(self, relpath: str) -> int:
        """Per-component O_NOFOLLOW openat traversal (no openat2 available)."""
        parent = os.open(".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=self.dirfd)
        parts = [p for p in relpath.split("/") if p and p != "."]
        try:
            for comp in parts[:-1]:
                nxt = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = nxt
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if not self.allow_symlinks:
                flags |= os.O_NOFOLLOW
            return os.open(parts[-1], flags, dir_fd=parent)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise AuthorizationError("symlink rejected") from exc
            if exc.errno == errno.ENOENT:
                raise NotFoundError(f"no such source: {relpath}") from exc
            raise AuthorizationError(f"confined open failed: {os.strerror(exc.errno)}") from exc
        finally:
            os.close(parent)

    def read(self, relpath: str, *, max_bytes: int | None = None) -> bytes:
        """Read bounded bytes from a confined file. Oversized reads fail closed."""
        cap = self.max_bytes if max_bytes is None else min(max_bytes, self.max_bytes)
        fd = self.open_read(relpath)
        try:
            st = os.fstat(fd)
            if not (st.st_mode & 0o170000) == 0o100000:  # S_ISREG
                raise AuthorizationError("not a regular file")
            if st.st_size > cap:
                raise BudgetError(f"source {relpath} is {st.st_size} bytes, exceeds cap {cap}")
            return os.read(fd, st.st_size)
        finally:
            os.close(fd)
