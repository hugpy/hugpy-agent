"""The Fleet arm toolset: ``{category: {route: callable}}`` for the UI's API.

Dependency-light by contract — importing this module pulls only the standard
library and hugpy_agent's own stdlib-only helpers, so a host (core, or the
standalone server) can import the dict without flask. ``register_categories``
turns each entry into ``/<category>/<route>`` (GET+POST); every route is under
``agent`` so ``/api/agent/*`` is clear of core's routes.

A route function takes plain kwargs and returns a JSON-serialisable value
(wrapped as ``{"result": ...}``) or — for the binary download — a flask
Response (passed through untouched). None is never returned.
"""
from __future__ import annotations

import inspect

from .imports import constants as C
from .fleet import console_artifacts as _ca
from .fleet import install_links as _il


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("hugpy-agent")
    except Exception:
        return "0.0.0"


def health() -> dict:
    """Liveness + identity of the Fleet arm."""
    return {"ok": True, "arm": C.ARM_NAME, "version": _version()}


def get_toolset(include_optional=False):
    """Return ``{category: {route: callable}}`` for the Fleet arm; nothing starts."""
    return {
        "agent": {
            "health": health,
            "manifest": get_manifest,
            "console_info": _ca.console_info,
            "console_file": _ca.console_file,
            "install_links": _il.list_links,
            "install_links_mint": _il.mint,
            "install_links_revoke": _il.revoke,
            "install_links_remove": _il.remove,
            "install_links_prune": _il.prune,
            # The links minted above resolve here (reached through core's arm
            # proxy /api/agent/* -> :7342); both stream raw (flask Response).
            "install_script": _il.install_script,
            "install_file": _il.install_file,
        }
    }


def get_manifest(include_optional=False) -> dict:
    """A JSON-serialisable description of the arm and every toolset route."""
    from hugpy_tools.schema import build_parameters, _first_paragraph
    cats = {}
    for cat, routes in get_toolset(include_optional).items():
        cats[cat] = {
            route: {
                "target": f"{fn.__module__}:{fn.__qualname__}",
                "description": _first_paragraph(inspect.getdoc(fn)),
                "inputSchema": build_parameters(fn),
            }
            for route, fn in routes.items()
        }
    return {"arm": C.ARM_NAME, "version": _version(), "ui": C.UI_BASE,
            "categories": cats}
