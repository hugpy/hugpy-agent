"""`hugpy-agent tui` — terminal harness over abstract-claude serve (:9124/:9125)
or hugpy-agent serve (:9126). `cli.cmd_tui` imports `run` from here.

Package layout (h26 design §1.2): discovery.py finds a serve, state.py is the
pure reducer, layout.py the geometry, views/* draw, app.py owns the curses loop
and the poll/send threads. `serve_client/` holds the protocol adapters.
"""
from __future__ import annotations

from .discovery import discover, identify  # noqa: F401


def run(base=None, token=None, session=None, kind="auto", toolserver_status=None):
    """Entry point: discover, connect, run the curses app. Returns an exit code."""
    import curses

    from ..serve_client import ServeError, connect
    try:
        found, found_kind = discover(base, kind or "auto")
        client = connect(found, found_kind, token)
    except ServeError as exc:
        print("hugpy-agent tui: %s" % exc)
        return 1
    from .app import App
    try:
        return curses.wrapper(lambda screen: App(screen, client, session=session,
                                                 toolserver_status=toolserver_status).run())
    except curses.error as exc:
        print("hugpy-agent tui: the terminal client needs a TTY with TERM set (SSH with a PTY works): %s" % exc)
        return 1
    except ServeError as exc:
        print("hugpy-agent tui: %s" % exc)
        return 1
