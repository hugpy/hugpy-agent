"""Operational log for the terminal harness: what went wrong, when, where.

Everything the TUI says to the operator (notices), every swallowed failure
(poller, send threads, the main-loop crash guard) and anything written to
stderr while curses owns the screen goes to one `logging` logger with:

* a ring buffer the `/log` viewer reads (always on), and
* a rotating file, ~/.hugpy/logs/tui.log (2 MB x 3; $HUGPY_TUI_LOG overrides,
  `0`/`off` disables) — attached only by `tui.run`, never by tests.

The logger does not propagate, so a library's root handler can never print
over the curses screen.
"""
from __future__ import annotations

import collections
import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback

DEFAULT_PATH = "~/.hugpy/logs/tui.log"
RING = 1000
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 3


def log_path(env=None):
    """Resolved log file path, or "" when file logging is off."""
    env = os.environ if env is None else env
    raw = env.get("HUGPY_TUI_LOG")
    if raw is not None and raw.strip().lower() in ("", "0", "off", "no", "false"):
        return ""
    return os.path.expanduser(raw or DEFAULT_PATH)


class _Ring(logging.Handler):
    def __init__(self, size):
        super().__init__(logging.DEBUG)
        self.rows = collections.deque(maxlen=size)

    def emit(self, record):
        try:
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
            self.rows.append((record.created, record.levelname, text))
        except Exception:               # a log call must never raise into the UI
            pass


class Diag:
    """One per App. `file=True` adds the rotating file handler."""

    def __init__(self, path=None, file=False, ring=RING):
        self.logger = logging.getLogger("hugpy_agent.tui.%x" % id(self))
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.ring = _Ring(ring)
        self.logger.addHandler(self.ring)
        self.path = ""
        self.file_error = ""
        if file:
            self.attach_file(log_path() if path is None else path)

    def attach_file(self, path):
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS,
                                                           encoding="utf-8", delay=True)
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s %(message)s"))
            handler.setLevel(logging.INFO)
            self.logger.addHandler(handler)
            self.path = path
        except OSError as exc:
            self.file_error = "%s: %s" % (path, exc)

    def close(self):
        for handler in list(self.logger.handlers):
            if handler is not self.ring:
                try:
                    handler.close()
                except Exception:
                    pass
                self.logger.removeHandler(handler)

    # -- writing -------------------------------------------------------------
    def info(self, text):
        self.logger.info(text)

    def warn(self, text):
        self.logger.warning(text)

    def error(self, text, exc=None):
        if exc is not None:
            self.logger.error(text, exc_info=(type(exc), exc, exc.__traceback__))
        else:
            self.logger.error(text)

    # -- reading -------------------------------------------------------------
    def rows(self):
        return list(self.ring.rows)

    def counts(self):
        out = collections.Counter(level for _, level, _ in self.ring.rows)
        return out.get("ERROR", 0), out.get("WARNING", 0)

    def lines(self):
        """Newest last, one entry per row, multi-line entries indented."""
        out = []
        for ts, level, text in self.ring.rows:
            stamp = time.strftime("%H:%M:%S", time.localtime(ts))
            first, *rest = (text or "").split("\n")
            out.append("%s %-5s %s" % (stamp, level[:5], first))
            out.extend("               " + r for r in rest)
        return out

    # -- process hooks ---------------------------------------------------------
    def install_hooks(self):
        """Route uncaught thread exceptions and stderr writes into the log
        while curses owns the screen. Returns an undo callable."""
        old_hook, old_stderr = threading.excepthook, sys.stderr

        def hook(args):
            if args.exc_type is SystemExit:
                return
            self.logger.error("thread %s died", getattr(args.thread, "name", "?"),
                              exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        threading.excepthook = hook
        sys.stderr = _LogStream(self.logger)

        def undo():
            threading.excepthook = old_hook
            sys.stderr = old_stderr
        return undo


class _LogStream:
    """File-like stderr replacement: complete lines become WARNING records."""

    def __init__(self, logger):
        self.logger, self.buf = logger, ""
        self.lock = threading.Lock()

    def write(self, text):
        with self.lock:
            self.buf += str(text)
            while "\n" in self.buf:
                line, self.buf = self.buf.split("\n", 1)
                if line.strip():
                    self.logger.warning("stderr: %s", line)
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False

    @property
    def encoding(self):
        return "utf-8"
