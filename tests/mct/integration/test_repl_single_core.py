"""C exists exactly once.

C is the operator conversation — a prompt, a response display, and a live relay
of the A↔B exchange. Nothing A or B does constrains it, which is precisely why
it was free to fork: ``tools/mct_repl.py`` grew ``/files``/``/quiet``/the live
feed while ``hugpy_agent.mct.repl`` kept ``/b``/``/bstate``/``/frontier``, and
the same filesystem gate answered to ``/fsreq`` in one and ``/allow`` in the
other. Whichever command you typed decided which terminal you got.

These tests fail if that divergence returns.
"""
import inspect
import re
from pathlib import Path

from hugpy_agent.mct import repl

_REPO = Path(__file__).resolve().parents[3]


def _commands(src: str) -> set:
    return set(re.findall(r'"(/[a-z-]+)"', src))


def test_the_repo_launcher_is_a_shim_not_a_copy():
    """tools/mct_repl.py must delegate, never reimplement."""
    text = (_REPO / "tools" / "mct_repl.py").read_text()
    assert "from hugpy_agent.mct.repl import main" in text
    # a shim has no command table and no terminal of its own
    assert not _commands(text), f"launcher defines its own commands: {_commands(text)}"
    assert "class LiveFeed" not in text
    assert len(text.splitlines()) < 60, "launcher is growing a second terminal again"


def test_every_entry_point_resolves_to_the_same_run():
    """`hugpy-agent mct`, `python -m hugpy_agent.mct`, and the repo launcher."""
    from hugpy_agent import cli
    from hugpy_agent.mct import __main__ as pkg_main

    assert pkg_main.main is repl.main
    assert "from .mct.repl import run" in inspect.getsource(cli.cmd_mct)


def test_the_union_of_both_command_sets_survived():
    """Neither side's features were dropped in the merge."""
    src = inspect.getsource(repl._command)
    cmds = _commands(src)
    from_packaged = {"/b", "/bstate", "/bmodel", "/frontier", "/fsreq"}
    from_launcher = {"/files", "/quiet", "/allow"}
    assert from_packaged <= cmds, f"lost: {from_packaged - cmds}"
    assert from_launcher <= cmds, f"lost: {from_launcher - cmds}"


def test_the_fs_gate_answers_to_both_names(tmp_path):
    """/fsreq and /allow were the same switch under two names — they must stay
    one switch, persisted to one file, or they can disagree."""
    from hugpy_agent.mct.fs_policy import load_policy
    from hugpy_agent.mct.session import BrokerServer

    server = BrokerServer(tmp_path, sink=lambda *_: None)
    sess = server.session(server.open_session("t"))
    state = {"model": "x", "last": None, "frontier": True, "queue": [], "quiet": False}
    try:
        for name in ("/fsreq", "/allow"):
            repl._command(f"{name} on", sess, server, state)
            assert load_policy(tmp_path)["allow_frontier_fs_requests"] is True
            repl._command(f"{name} off", sess, server, state)
            assert load_policy(tmp_path)["allow_frontier_fs_requests"] is False
    finally:
        server.close()


def test_help_documents_what_the_core_actually_dispatches():
    """A command that works but is undocumented is a command nobody finds —
    which is how /allow and /fsreq drifted apart in the first place."""
    doc = repl.__doc__ or ""
    for cmd in _commands(inspect.getsource(repl._command)):
        if cmd == "/quit":            # documented as the /exit alias
            continue
        assert cmd in doc, f"{cmd} is dispatched but not in /help"


def test_quiet_is_plumbed_from_every_launcher():
    from hugpy_agent import cli

    assert "quiet" in inspect.signature(repl.run).parameters
    assert "quiet" in inspect.getsource(cli.cmd_mct)
    assert "--quiet" in inspect.getsource(repl.main)
