"""Bounded excerpt selectors.

Design ref: §7.3 (``excerpt`` = "Bounded lines, bytes, JSON fields, AST symbols,
or search matches"), §22 Phase 2. Pure functions over object bytes: given the
committed source bytes and a selector string, return the bounded slice. The
selector string is recorded in the ``pull_result`` / fragment so the excerpt is
reproducible and its lineage is exact (invariant 8).

Selector grammar (all 1-indexed where line-oriented):

    lines A-B          inclusive line range
    bytes A-B          byte slice ``data[A:B]`` (0-indexed, end-exclusive)
    json PATH          e.g. ``json a.b[0]`` or ``json $.items[2].name``
    symbol NAME        Python def/class source by name (via ``ast``)
    match REGEX        lines matching REGEX
    match REGEX ctx N  matching lines plus N context lines each side
"""
from __future__ import annotations

import ast
import json
import re

from .errors import NotFoundError, ProtocolError

_CTX_RE = re.compile(r"\s+ctx\s+(\d+)\s*$")


def apply_selector(data: bytes, selector: str) -> bytes:
    selector = selector.strip()
    if selector.startswith("lines "):
        return _lines(data, selector[len("lines "):])
    if selector.startswith("bytes "):
        return _bytes(data, selector[len("bytes "):])
    if selector.startswith("json "):
        return _json(data, selector[len("json "):].strip())
    if selector.startswith("symbol "):
        return _symbol(data, selector[len("symbol "):].strip())
    if selector.startswith("match "):
        return _match(data, selector[len("match "):])
    raise ProtocolError(f"unsupported selector: {selector!r}")


def _lines(data: bytes, span: str) -> bytes:
    start_s, _, end_s = span.strip().partition("-")
    start, end = int(start_s), int(end_s or start_s)
    if start < 1 or end < start:
        raise ProtocolError(f"invalid line span: {span!r}")
    lines = data.splitlines(keepends=True)
    out = lines[start - 1:end]
    if not out:
        raise NotFoundError(f"lines {span} out of range")
    return b"".join(out)


def _bytes(data: bytes, span: str) -> bytes:
    start_s, _, end_s = span.strip().partition("-")
    start, end = int(start_s), int(end_s or len(data))
    if start < 0 or end < start:
        raise ProtocolError(f"invalid byte span: {span!r}")
    return data[start:end]


def _json(data: bytes, path: str) -> bytes:
    try:
        node = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"object is not JSON: {exc}") from exc
    for key in _json_tokens(path):
        try:
            node = node[key]
        except (KeyError, IndexError, TypeError):
            raise NotFoundError(f"json path not found: {path!r}") from None
    return json.dumps(node, sort_keys=True).encode("utf-8")


def _json_tokens(path: str):
    path = path.lstrip("$").lstrip(".")
    for part in re.split(r"\.", path):
        if not part:
            continue
        m = re.match(r"^([^\[]+)?((?:\[\d+\])*)$", part)
        if m and m.group(1):
            yield m.group(1)
        if m and m.group(2):
            for idx in re.findall(r"\[(\d+)\]", m.group(2)):
                yield int(idx)


def _symbol(data: bytes, name: str) -> bytes:
    source = data.decode("utf-8", errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ProtocolError(f"cannot parse Python source: {exc}") from exc
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name == name:
            segment = ast.get_source_segment(source, node)
            if segment is not None:
                return segment.encode("utf-8")
    raise NotFoundError(f"symbol not found: {name!r}")


def _match(data: bytes, rest: str) -> bytes:
    ctx = 0
    m = _CTX_RE.search(rest)
    if m:
        ctx = int(m.group(1))
        rest = rest[:m.start()]
    pattern = rest.strip()
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise ProtocolError(f"invalid regex: {exc}") from exc
    lines = data.splitlines(keepends=True)
    keep: set[int] = set()
    for i, line in enumerate(lines):
        if rx.search(line.decode("utf-8", errors="replace")):
            for j in range(max(0, i - ctx), min(len(lines), i + ctx + 1)):
                keep.add(j)
    if not keep:
        raise NotFoundError(f"no lines match {pattern!r}")
    return b"".join(lines[i] for i in sorted(keep))
