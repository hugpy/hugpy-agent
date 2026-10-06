"""`hugpy-agent tui` — provider-neutral terminal harness over shared Serve
(:9124/:9125) or hugpy-agent Serve (:9126). `cli.cmd_tui` imports `run` here.

Package layout (h26 design §1.2): discovery.py finds a serve, state.py is the
pure reducer, layout.py the geometry, views/* draw, app.py owns the curses loop
and the poll/send threads, diag.py the operational log, output.py clipboard +
export. `serve_client/` holds the protocol adapters.
"""
from __future__ import annotations

from .discovery import discover, identify  # noqa: F401


def run(base=None, token=None, session=None, kind="auto", toolserver_status=None):
    """Entry point: discover, connect, run the curses app. Returns an exit code."""
    import curses

    from ..serve_client import ServeError, connect
    from .diag import Diag
    try:
        found, found_kind = discover(base, kind or "auto")
        client = connect(found, found_kind, token)
    except ServeError as exc:
        print("hugpy-agent tui: %s" % exc)
        return 1
    from .app import App
    diag = Diag(file=True)
    undo = diag.install_hooks()            # thread tracebacks / stderr -> the log, not the screen
    ui = None

    def main(screen):
        nonlocal ui
        ui = App(screen, client, session=session, toolserver_status=toolserver_status, diag=diag)
        return ui.run()
    try:
        code = curses.wrapper(main)
    except curses.error as exc:
        code = 1
        undo()
        print("hugpy-agent tui: the terminal client needs a TTY with TERM set (SSH with a PTY works): %s" % exc)
    except ServeError as exc:
        code = 1
        undo()
        print("hugpy-agent tui: %s" % exc)
    except Exception as exc:               # noqa: BLE001 — outside the crash guard (setup/teardown)
        diag.error("tui crashed", exc)
        undo()
        print("hugpy-agent tui: crashed: %s: %s%s" % (type(exc).__name__, exc,
                                                      " — traceback in %s" % diag.path if diag.path else ""))
        code = 1
    finally:
        undo()
        diag.close()
    if ui is not None and ui.exit_message:
        print(ui.exit_message)
    return code
