"""hugpy_tools -> agent tools. Thin glue: all logic lives in hugpy_tools (stdlib-only).

Every path goes through hugpy_tools.confine (realpath + parent-confine on write),
re-raised as ValueError with the same wording as tools/fs.py. Risk classes reuse
the registry constants so policy.py gates writes and network like fs_write/http_fetch.
"""
from __future__ import annotations

import json

import hugpy_tools as ht

from . import RISK_NETWORK, RISK_READONLY, RISK_WRITE, ToolSpec


def _confine(workspace: str, path: str, for_write: bool = False) -> str:
    try:
        return ht.confine(workspace, path, for_write=for_write)
    except ht.PathEscape:
        raise ValueError("path %r escapes the workspace (%s)" % (path, workspace))


def _out(obj) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


def _obj(props: dict, required: list) -> dict:
    return {"type": "object", "properties": props, "required": required}


_PATH = {"type": "string", "description": "workspace-relative path"}


def specs(workspace: str) -> list[ToolSpec]:
    def fs_read_lines(path: str, start: int = 1, end: int | None = None, number: bool = False) -> str:
        return _out(ht.read_lines(_confine(workspace, path), start=start, end=end, number=number))

    def fs_list(path: str = ".", glob: str | None = None, files_only: bool = False,
                dirs_only: bool = False, limit: int = 500) -> str:
        return _out(ht.list_dir(_confine(workspace, path), glob=glob, files_only=files_only,
                                dirs_only=dirs_only, limit=limit))

    def fs_tree(path: str = ".", max_depth: int = 3, max_entries: int = 500, show_files: bool = True) -> str:
        return _out(ht.tree(_confine(workspace, path), max_depth=max_depth,
                            max_entries=max_entries, show_files=show_files))

    def fs_info(path: str, hash_files: bool = False) -> str:
        return _out(ht.file_info(_confine(workspace, path), hash_files=hash_files))

    def fs_edit(path: str, old: str, new: str, count: int | None = None) -> str:
        return _out(ht.edit_replace(_confine(workspace, path, for_write=True), old, new, count=count))

    def data_read(path: str, fmt: str | None = None) -> str:
        return _out(ht.read_data(_confine(workspace, path), fmt=fmt))

    def data_write(path: str, obj, indent: int = 2, sort_keys: bool = False) -> str:
        return _out(ht.write_json(_confine(workspace, path, for_write=True), obj,
                                  indent=indent, sort_keys=sort_keys))

    def text_count_tokens(text: str) -> str:
        return _out({"tokens": ht.count_tokens(text)})

    def text_chunk(text: str, by: str = "tokens", size: int = 1000, overlap: int = 0) -> str:
        if by == "lines":
            chunks = ht.chunk_by_lines(text, max_lines=size, overlap=overlap)
        elif by == "tokens":
            chunks = ht.chunk_by_tokens(text, max_tokens=size)
        else:
            raise ValueError("by must be 'tokens' or 'lines', got %r" % by)
        return _out({"by": by, "count": len(chunks), "chunks": chunks})

    def text_diff(before: str, after: str, before_name: str = "before",
                  after_name: str = "after", context: int = 3) -> str:
        return ht.unified_diff(before, after, before_name=before_name,
                               after_name=after_name, context=context)

    def web_assess(url: str, max_chars: int = 12000, max_links: int = 50,
                   force_render: bool = False) -> str:
        return _out(ht.assess_webpage(url, max_chars=max_chars, max_links=max_links,
                                      force_render=force_render))

    def web_prescreen(url: str, max_chars: int = 600, force_render: bool = False) -> str:
        return _out(ht.prescreen_webpage(url, max_chars=max_chars, force_render=force_render))

    S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
    return [
        ToolSpec("fs_read_lines", "Read a line range (1-based, inclusive) of a workspace text file; number=true prefixes line numbers.",
                 _obj({"path": _PATH, "start": I, "end": I, "number": B}, ["path"]), fs_read_lines, RISK_READONLY),
        ToolSpec("fs_list", "List a workspace directory (optional glob, files_only/dirs_only, limit).",
                 _obj({"path": _PATH, "glob": S, "files_only": B, "dirs_only": B, "limit": I}, []), fs_list, RISK_READONLY),
        ToolSpec("fs_tree", "Tree view of a workspace directory (max_depth, max_entries, show_files).",
                 _obj({"path": _PATH, "max_depth": I, "max_entries": I, "show_files": B}, []), fs_tree, RISK_READONLY),
        ToolSpec("fs_info", "File/dir info: size, mtime, type, encoding guess; hash_files=true adds sha256.",
                 _obj({"path": _PATH, "hash_files": B}, ["path"]), fs_info, RISK_READONLY),
        ToolSpec("fs_edit", "Replace exact text `old` with `new` in a workspace file (atomic write; count limits replacements).",
                 _obj({"path": _PATH, "old": S, "new": S, "count": I}, ["path", "old", "new"]), fs_edit, RISK_WRITE),
        ToolSpec("data_read", "Read JSON/TOML/YAML from a workspace file (fmt auto from extension).",
                 _obj({"path": _PATH, "fmt": S}, ["path"]), data_read, RISK_READONLY),
        ToolSpec("data_write", "Write an object as JSON to a workspace file (atomic).",
                 _obj({"path": _PATH, "obj": {}, "indent": I, "sort_keys": B}, ["path", "obj"]), data_write, RISK_WRITE),
        ToolSpec("text_count_tokens", "Estimate the token count of a text (dependency-free).",
                 _obj({"text": S}, ["text"]), text_count_tokens, RISK_READONLY),
        ToolSpec("text_chunk", "Split text into chunks by tokens (paragraph-aware) or lines (with overlap).",
                 _obj({"text": S, "by": {"type": "string", "enum": ["tokens", "lines"]}, "size": I, "overlap": I}, ["text"]),
                 text_chunk, RISK_READONLY),
        ToolSpec("text_diff", "Unified diff between two texts.",
                 _obj({"before": S, "after": S, "before_name": S, "after_name": S, "context": I}, ["before", "after"]),
                 text_diff, RISK_READONLY),
        ToolSpec("web_assess", "Fetch and assess a webpage: title, description, metadata, JSON-LD, headings, budgeted text, same-site links.",
                 _obj({"url": S, "max_chars": I, "max_links": I, "force_render": B}, ["url"]), web_assess, RISK_NETWORK),
        ToolSpec("web_prescreen", "Cheap relevance pre-screen of a webpage: title, description, lede.",
                 _obj({"url": S, "max_chars": I, "force_render": B}, ["url"]), web_prescreen, RISK_NETWORK),
    ]
