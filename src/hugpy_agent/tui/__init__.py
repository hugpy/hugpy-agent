"""hugpy-agent TUI — the hugpy_harness terminal harness, branded "hugpy-agent".

Discovery, the pure reducer, the views and the curses app are all the harness
TUI (one source of truth; the sibling modules in this package are thin
re-export shims). This package only:

  1. overrides the harness service name to "hugpy-agent" (the SERVICE_NAME
     knob) so every discovery/branding string reads it, and
  2. adds the in-process WrapperClient fallback so `hugpy-agent tui` needs no
     serve: with nothing discovered — or --local / HUGPY_AGENT_SERVE=local — it
     talks to a local wrapper (else the fleet endpoint, resolved like the
     harness) directly, journaling in the shared Harness session directory.

If a serve IS discovered, behavior is unchanged (the serve path). Serve-only
features (rollover, service slash-commands) hit the WrapperClient's own clear
errors on the local path.
"""
from __future__ import annotations

from hugpy_harness.imports.constants import service_name, set_service_name

# Rebrand the shared harness TUI as the agent before anything reads service_name
# (importing any hugpy_agent.tui.* submodule runs this first).
set_service_name("hugpy-agent")

from .discovery import discover, identify  # noqa: E402,F401

# Interactive sessions are owned by the shared Harness. Existing batch-agent
# journals stay in .hugpy_agent; HUGPY_SESSION_STATE_DIR can reopen those too.
from hugpy_harness.sessions import session_state_dir
AGENT_STATE_DIR = session_state_dir()


def _wants_local(base, local) -> bool:
    import os
    if local or str(base or "").strip().lower() == "local":
        return True
    return os.environ.get("HUGPY_AGENT_SERVE", "").strip().lower() == "local"


def run(base=None, token=None, session=None, kind="auto", toolserver_status=None,
        local=False, workspace=".", model="", policy="ask"):
    """Discover a serve and run the harness curses app against it. With no serve
    found — or local=True / --local / $HUGPY_AGENT_SERVE=local — fall back to the
    in-process harness WrapperClient against the harness-resolved local endpoint
    (local wrapper, else HUGPY_FLEET_URL / fleet.endpoint.json), sharing the Harness
    session directory. Returns an exit code; serve is never required."""
    import curses

    from ..serve_client import ServeError, connect
    from .diag import Diag

    client = None
    if not _wants_local(base, local):
        try:
            found, found_kind = discover(base, kind or "auto")
            client = connect(found, found_kind, token)
        except ServeError:
            client = None          # serve optional: fall through to the wrapper

    if client is None:
        from hugpy_harness.cli.chat import local_endpoint, endpoint_key
        endpoint = local_endpoint()
        if not endpoint:
            print("%s tui: no serve found and no local wrapper/fleet endpoint "
                  "(set HUGPY_WRAPPER_URL or HUGPY_FLEET_URL, run a serve, or "
                  "pass --base URL)" % service_name())
            return 1
        try:
            client = connect(endpoint, "wrapper", endpoint_key(),
                             workspace=workspace, model=model, policy=policy,
                             state_dir=session_state_dir())
        except (ServeError, OSError) as exc:
            print("%s tui: %s" % (service_name(), exc))
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
        print("%s tui: the terminal client needs a TTY with TERM set (SSH with a PTY works): %s"
              % (service_name(), exc))
    except ServeError as exc:
        code = 1
        undo()
        print("%s tui: %s" % (service_name(), exc))
    except Exception as exc:               # noqa: BLE001 — outside the crash guard (setup/teardown)
        diag.error("tui crashed", exc)
        undo()
        print("%s tui: crashed: %s: %s%s" % (service_name(), type(exc).__name__, exc,
                                             " — traceback in %s" % diag.path if diag.path else ""))
        code = 1
    finally:
        undo()
        diag.close()
    if ui is not None and ui.exit_message:
        print(ui.exit_message)
    return code
