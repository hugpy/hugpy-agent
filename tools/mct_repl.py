#!/usr/bin/env python3
"""Repo-local launcher for the Mediated Context Terminal.

This is a SHIM, not a second terminal. C — the operator conversation: a prompt,
a response display, and a live relay of the A↔B exchange — lives in
:mod:`hugpy_agent.mct.repl` and exists exactly once.

It used to be a full copy, and the two drifted apart in both directions: this
file grew ``/files``/``/quiet``/the live feed while the packaged module kept
``/b``/``/bstate``/``/frontier``, and the same filesystem gate answered to
``/fsreq`` in one and ``/allow`` in the other. Anyone comparing the two saw a
different terminal depending on which command launched it. One implementation,
two entry points, is the only arrangement that cannot drift.

The only thing this file owns is pointing at the repo's ``src/`` so the launcher
runs the working tree rather than whatever is pip-installed — which is the whole
reason to invoke a script instead of ``hugpy-agent mct``.

Usage (unchanged):
  mct_repl.py [WORKSPACE] [--model sonnet] [--no-model] [--quiet]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hugpy_agent.mct.repl import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
