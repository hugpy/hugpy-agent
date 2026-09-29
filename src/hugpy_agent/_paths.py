"""hugpy_agent runtime paths — the ``~/.hugpy`` organization.

This is a STANDALONE package: it must not import ``abstract_hugpy_dev``, so it
carries its own tiny copy of the HUGPY_HOME resolver (kept behaviour-compatible
with ``abstract_hugpy_dev/_platform/paths.py``). Only the *home-based default*
workspaces move under ``~/.hugpy``; genuinely per-``<workspace>`` state
(``<workspace>/.hugpy_agent/...`` — journal, traces, queue) stays with its
workspace by design and is NOT touched here.

    HUGPY_HOME (default ~/.hugpy)
      mct/          default MCT workspace root  (was ~/.mct)
        repl/         default REPL conversation workspace
        repl_history  readline history
      agent/console/  default console (opencode) workspace (was ~/.hugpy_agent/console)

Each accessor migrates its legacy location in on first resolve (atomic rename,
cross-fs copy fallback), so an upgrade is seamless and a fresh install is clean.
"""
from __future__ import annotations

import os


def hugpy_home() -> str:
    return os.environ.get("HUGPY_HOME") or os.path.join(os.path.expanduser("~"), ".hugpy")


def _relocate(new: str, *legacy: str) -> str:
    try:
        os.makedirs(os.path.dirname(new), exist_ok=True)
        if os.path.exists(new):
            return new
        for lname in legacy:
            old = os.path.join(os.path.expanduser("~"), lname)
            if os.path.exists(old) and os.path.abspath(old) != os.path.abspath(new):
                try:
                    os.replace(old, new)
                except OSError:
                    import shutil
                    shutil.move(old, new)
                break
    except OSError:
        pass
    return new


def mct_dir() -> str:
    """Default MCT workspace root — migrates a legacy ``~/.mct`` in."""
    return _relocate(os.path.join(hugpy_home(), "mct"), ".mct")


def mct_repl_workspace() -> str:
    return os.path.join(mct_dir(), "repl")


def mct_repl_history() -> str:
    return os.path.join(mct_dir(), "repl_history")


def console_workspace() -> str:
    """Default console (opencode) workspace — migrates ``~/.hugpy_agent/console`` in."""
    return _relocate(os.path.join(hugpy_home(), "agent", "console"),
                     os.path.join(".hugpy_agent", "console"))
