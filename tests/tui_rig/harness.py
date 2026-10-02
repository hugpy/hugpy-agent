"""PTY harness for `hugpy-agent tui`: drive it in a detached tmux session and
assert on the captured screen. Rerunnable:  python3 harness.py

Starts the stub serve, runs the TUI from source (PYTHONPATH), sends keys with
`tmux send-keys`, captures with `tmux capture-pane -p`, and checks screen text.
Cleans up the tmux session and stub serve on exit (even on failure).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "..", "src"))
SESSION = "tuirig"
WIDTH, HEIGHT = 180, 45

_RESULTS = []


def sh(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def start_stub():
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "stub_serve.py"), "0"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = proc.stdout.readline().strip()
    m = re.match(r"PORT (\d+)", line)
    if not m:
        raise SystemExit("stub serve did not report a port: %r" % line)
    return proc, int(m.group(1))


def tmux(*args):
    return sh("tmux", *args)


def kill_session():
    tmux("kill-session", "-t", SESSION)


def start_tui(port):
    kill_session()
    # env prefix guarantees PYTHONPATH/TERM in the tmux-spawned shell regardless
    # of the tmux server's own environment.
    cmd = ("env PYTHONPATH=%s TERM=xterm-256color HUGPY_TUI_MOUSE=1 ESCDELAY=25 "
           "%s -m hugpy_agent.cli tui --serve http://127.0.0.1:%d"
           % (SRC, sys.executable, port))
    r = tmux("new-session", "-d", "-s", SESSION, "-x", str(WIDTH), "-y", str(HEIGHT), cmd)
    if r.returncode != 0:
        raise SystemExit("tmux new-session failed: %s" % r.stderr)
    time.sleep(2.5)


def keys(*ks, delay=0.35):
    tmux("send-keys", "-t", SESSION, *ks)
    time.sleep(delay)


def literal(text, delay=0.35):
    tmux("send-keys", "-t", SESSION, "-l", text)
    time.sleep(delay)


def capture():
    r = tmux("capture-pane", "-p", "-t", SESSION)
    return r.stdout


def capture_e():
    """Capture WITH escape sequences so A_REVERSE (SELECT/HELD) is visible."""
    r = tmux("capture-pane", "-pe", "-t", SESSION)
    return r.stdout


_SGR = re.compile(r"\x1b\[([0-9;]*)m")


def _has_reverse(raw):
    """True if any SGR sequence on the line sets attribute 7 (reverse video),
    e.g. \\e[7m or \\e[0;7m or \\e[7;1m — but NOT 37/47/27."""
    for params in _SGR.findall(raw):
        if "7" in params.split(";"):
            return True
    return False


def reversed_lines(screen_e):
    """Return the plain text of lines that contain a reverse-video (SELECT) run."""
    out = []
    for raw in screen_e.splitlines():
        if _has_reverse(raw):
            out.append(re.sub(r"\x1b\[[0-9;]*m", "", raw))
    return out


def paste(text, delay=0.6):
    """Send `text` as a bracketed paste (tmux -p wraps it in ESC[200~/201~ when
    the app has DECSET 2004 enabled)."""
    tmux("set-buffer", "--", text)
    tmux("paste-buffer", "-p", "-t", SESSION)
    time.sleep(delay)


def busy_seconds(screen):
    """Parse the status bar's 'BUSY Ns' counter, or None."""
    m = re.search(r"BUSY (\d+)s", screen)
    return int(m.group(1)) if m else None


def resize(w, h):
    tmux("resize-window", "-t", SESSION, "-x", str(w), "-y", str(h))
    time.sleep(0.6)


def check(name, cond, screen=None):
    ok = bool(cond)
    _RESULTS.append((name, ok))
    mark = "PASS" if ok else "FAIL"
    print("[%s] %s" % (mark, name))
    if not ok and screen is not None:
        print("------ screen ------")
        print(screen)
        print("--------------------")


def has_traceback(screen):
    return "Traceback (most recent call last)" in screen or "Error:" in screen and "ServeError" not in screen
