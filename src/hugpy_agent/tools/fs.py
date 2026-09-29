"""Workspace-jailed filesystem tools: fs_read, fs_write, fs_glob.

Every path is resolved through os.path.realpath and must land under the
realpath of the workspace root — this rejects `..` escapes AND symlink
escapes in one check (a symlink inside the workspace pointing outside
resolves outside and is refused). WHY realpath on the PARENT for writes: the
target file may not exist yet, but its directory does after we create it, and
that directory is what must be confined.

Fail-closed: any ambiguity about where a path lands is an error, not a guess.
"""
from __future__ import annotations

import glob as _glob
import json
import os

from . import RISK_READONLY, RISK_WRITE, ToolSpec

READ_CAP = 64 * 1024          # bytes shown to the model per read
GLOB_CAP = 200                # matches returned


def _confine(workspace: str, path: str, for_write: bool = False) -> str:
    """Resolve `path` (absolute or workspace-relative) to a real absolute
    path confined under the workspace. Raises ValueError on escape."""
    root = os.path.realpath(workspace)
    candidate = path if os.path.isabs(path) else os.path.join(root, path)
    if for_write:
        # The file may not exist yet; confine its (resolved) parent, then
        # re-attach the basename. realpath on the full candidate would let a
        # dangling path with a symlinked ancestor slip through unchecked.
        parent = os.path.realpath(os.path.dirname(candidate) or root)
        resolved = os.path.join(parent, os.path.basename(candidate))
    else:
        resolved = os.path.realpath(candidate)
    if resolved != root and not resolved.startswith(root + os.sep):
        raise ValueError("path %r escapes the workspace (%s)" % (path, root))
    return resolved


def specs(workspace: str) -> list[ToolSpec]:
    def fs_read(path: str, offset: int = 0) -> str:
        real = _confine(workspace, path)
        with open(real, "rb") as fh:
            fh.seek(max(0, int(offset)))
            data = fh.read(READ_CAP + 1)
        truncated = len(data) > READ_CAP
        text = data[:READ_CAP].decode("utf-8", errors="replace")
        if truncated:
            text += ("\n[... truncated at %d bytes; call fs_read again with "
                     "offset=%d for more]" % (READ_CAP, offset + READ_CAP))
        return text

    def fs_write(path: str, content: str, append: bool = False) -> str:
        real = _confine(workspace, path, for_write=True)
        os.makedirs(os.path.dirname(real), exist_ok=True)
        mode = "a" if append else "w"
        with open(real, mode, encoding="utf-8") as fh:
            fh.write(content)
        rel = os.path.relpath(real, os.path.realpath(workspace))
        return json.dumps({"written": rel, "bytes": len(content.encode()),
                           "mode": "append" if append else "overwrite"})

    def fs_glob(pattern: str) -> str:
        root = os.path.realpath(workspace)
        matches = _glob.glob(os.path.join(root, pattern), recursive=True)
        rels = []
        for m in matches[:GLOB_CAP]:
            real = os.path.realpath(m)
            # A glob can traverse a symlink out of the jail; confine each hit.
            if real == root or real.startswith(root + os.sep):
                rels.append(os.path.relpath(real, root))
        rels.sort()
        out = {"matches": rels, "count": len(rels)}
        if len(matches) > GLOB_CAP:
            out["note"] = "capped at %d matches; narrow the pattern" % GLOB_CAP
        return json.dumps(out)

    return [
        ToolSpec(
            name="fs_read",
            description=("Read a text file inside the workspace. Returns up to "
                         "64KB from `offset` (default 0)."),
            parameters={"type": "object",
                        "properties": {
                            "path": {"type": "string",
                                     "description": "workspace-relative file path"},
                            "offset": {"type": "integer",
                                       "description": "byte offset to start from"}},
                        "required": ["path"]},
            handler=fs_read, risk_class=RISK_READONLY),
        ToolSpec(
            name="fs_write",
            description=("Write a text file inside the workspace (creates parent "
                         "dirs). Overwrites unless append=true."),
            parameters={"type": "object",
                        "properties": {
                            "path": {"type": "string",
                                     "description": "workspace-relative file path"},
                            "content": {"type": "string"},
                            "append": {"type": "boolean"}},
                        "required": ["path", "content"]},
            handler=fs_write, risk_class=RISK_WRITE),
        ToolSpec(
            name="fs_glob",
            description=("List workspace files matching a glob pattern, e.g. "
                         "'*.py' or '**/*.md' (recursive)."),
            parameters={"type": "object",
                        "properties": {
                            "pattern": {"type": "string"}},
                        "required": ["pattern"]},
            handler=fs_glob, risk_class=RISK_READONLY),
    ]
