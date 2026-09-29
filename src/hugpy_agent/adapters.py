"""Vendor-neutral adapter seam — how third-party capabilities plug into
hugpy_agent WITHOUT hugpy_agent importing them.

hugpy_agent ships zero third-party integrations: no ``abstract_*`` package, no
vendor SDK (anthropic / openai / google …), in the core or in any extra. A
capability provider registers itself under the ``hugpy_agent.adapters``
entry-point group and this module discovers it at first use, handing back a
duck-typed object. If nothing is registered the lookup returns ``None`` and the
caller degrades explicitly (a stated fallback) — never a silent vendor import,
never a hard crash for a missing optional package.

The provider declares the entry point in ITS OWN packaging, e.g. an
abstract-search-backed local finder shipped by abstract-toolserver::

    # provider's pyproject.toml
    [project.entry-points."hugpy_agent.adapters"]
    local_search = "abstract_toolserver.hugpy_adapter:local_search_provider"

The target is a zero-argument factory callable returning the provider object.
Any failure to import or build a provider is swallowed and cached as "absent",
so a broken plugin degrades to the neutral fallback instead of taking B down.

Known adapter names and the duck-typed surface each provider must expose:

  ``local_search`` — in-process content search over granted roots. Attributes:
      * ``find_content(directory, strings, parse_lines, get_lines, **kw)``
      * ``get_file_filters(root_path, **kw) -> (dirs, cfg, allowed, inc, recursive)``
      * ``get_files_and_dirs(directory, cfg, recursive) -> (dirs, files)``
      * ``read_any_file(path) -> str``
    (the abstract-search API, re-exported by the provider). Absent → B falls
    back to the central HTTP finder (``/api/finder/search``).
"""
from __future__ import annotations

from typing import Any

_GROUP = "hugpy_agent.adapters"
_cache: dict[str, Any] = {}


def _iter_entry_points(name: str):
    """Yield entry points in ``hugpy_agent.adapters`` named ``name``, across the
    3.10 (dict) and 3.12 (selectable) ``importlib.metadata`` shapes."""
    try:
        from importlib.metadata import entry_points
    except Exception:                       # pragma: no cover - importlib always present
        return []
    try:
        eps = entry_points()
    except Exception:
        return []
    if hasattr(eps, "select"):              # Python 3.12+ selectable API
        return list(eps.select(group=_GROUP, name=name))
    return [e for e in eps.get(_GROUP, []) if e.name == name]  # pragma: no cover


def get_adapter(name: str):
    """Return the provider registered under ``name`` in the
    ``hugpy_agent.adapters`` group, or ``None`` when none is installed or it
    fails to load. The result — including ``None`` — is cached for the process
    (measure-once), so discovery cost is paid at most once per name."""
    if name in _cache:
        return _cache[name]
    provider = None
    for ep in _iter_entry_points(name):
        try:
            factory = ep.load()
            provider = factory() if callable(factory) else factory
        except Exception:
            provider = None                 # broken plugin → neutral fallback
            continue
        if provider is not None:
            break
    _cache[name] = provider
    return provider
