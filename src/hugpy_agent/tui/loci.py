"""Locus registry for the TUI: named serves the operator can switch between.

A locus is a serve endpoint, local (`serve` URL) or remote behind SSH
(`ssh` user@host + the serve's port on that host, tunnelled with
`ssh -N -L` exactly as hugpy-station reaches its remotes). The list lives
in ~/.hugpy/tui-loci.json (override: $HUGPY_TUI_LOCI), e.g.:

    [{"locus": "hugpy",    "serve": "http://127.0.0.1:9125"},
     {"locus": "keeper",   "serve": "http://127.0.0.1:9124"},
     {"locus": "hs-fresh", "ssh": "ubuntu@10.237.23.104", "port": 9125}]
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time

from .discovery import probe

PATH = os.path.expanduser(os.environ.get("HUGPY_TUI_LOCI") or "~/.hugpy/tui-loci.json")


def load_loci(path=None):
    """[{locus, serve?|ssh?, port?}] from disk; [] when absent/invalid."""
    try:
        with open(path or PATH) as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return []
    out = []
    for row in doc if isinstance(doc, list) else []:
        if isinstance(row, dict) and row.get("locus") and (row.get("serve") or row.get("ssh")):
            out.append(row)
    return out


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Tunnels:
    """One `ssh -N -L` per remote locus, opened lazily, kept for the TUI's life."""

    def __init__(self):
        self.procs = {}       # locus -> (Popen, local base url)

    def base_for(self, entry, wait=6.0):
        """Resolve a loci entry to a reachable base URL (raises RuntimeError)."""
        if entry.get("serve"):
            return entry["serve"].rstrip("/")
        locus = entry["locus"]
        held = self.procs.get(locus)
        if held and held[0].poll() is None:
            return held[1]
        port = int(entry.get("port") or 9125)
        lp = _free_port()
        cmd = ["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
               "-o", "ConnectTimeout=5", "-L", "127.0.0.1:%d:127.0.0.1:%d" % (lp, port),
               entry["ssh"]]
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = "http://127.0.0.1:%d" % lp
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("ssh to %s exited (%s)" % (entry["ssh"], proc.returncode))
            if probe(base, timeout=0.5) is not None:
                self.procs[locus] = (proc, base)
                return base
            time.sleep(0.25)
        proc.terminate()
        raise RuntimeError("no serve behind %s:%d after %.0fs" % (entry["ssh"], port, wait))

    def close(self):
        for proc, _ in self.procs.values():
            if proc.poll() is None:
                proc.terminate()
        self.procs.clear()
