"""hugpy_agent runtime paths — the ``~/.hugpy`` organization.

The HUGPY_HOME resolver and the atomic-migration helper now live in the shared
``imports`` layer (``functions.home_dir`` / ``functions.relocate``); this module
keeps the per-accessor policy — which legacy location migrates into which home
subdir — in one place. Genuinely per-``<workspace>`` state
(``<workspace>/.hugpy_agent/...`` — journal, traces, queue) stays with its
workspace by design and is NOT touched here.

    HUGPY_HOME (default ~/.hugpy)
      mct/          default MCT workspace root  (was ~/.mct)
        repl/         default REPL conversation workspace
        repl_history  readline history
      agent/console/  default console (opencode) workspace (was ~/.hugpy_agent/console)
"""
from __future__ import annotations

import os

from .imports import constants as C
from .imports.functions import home_dir as hugpy_home
from .imports.functions import relocate as _relocate

__all__ = ["hugpy_home", "mct_dir", "mct_repl_workspace", "mct_repl_history",
           "console_workspace"]


def mct_dir() -> str:
    """Default MCT workspace root — migrates a legacy ``~/.mct`` in."""
    return _relocate(os.path.join(hugpy_home(), "mct"), C.LEGACY_MCT_DIR)


def mct_repl_workspace() -> str:
    return os.path.join(mct_dir(), "repl")


def mct_repl_history() -> str:
    return os.path.join(mct_dir(), "repl_history")


def console_workspace() -> str:
    """Default console (opencode) workspace — migrates ``~/.hugpy_agent/console`` in."""
    return _relocate(os.path.join(hugpy_home(), "agent", "console"),
                     C.LEGACY_CONSOLE_DIR)
