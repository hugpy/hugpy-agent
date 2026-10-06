"""Locus registry for the TUI: named serves the operator can switch between.

A locus is a serve endpoint, local (`serve` URL) or remote behind SSH
(`ssh` user@host + the serve's port on that host, tunnelled with
`ssh -N -L` exactly as hugpy-station reaches its remotes).

The list comes from the toolserver's loci registry (operator 2026-10-06: serves
publish their address on the toolserver; ports and files are the fallback):
every active locus whose pointer carries `serve_url`. The file
~/.hugpy/tui-loci.json adds the loci the registry does not name, and is the
whole list when the toolserver cannot be reached:

    [{"locus": "hugpy",    "serve": "http://127.0.0.1:9125"},
     {"locus": "keeper",   "serve": "http://127.0.0.1:9124"},
     {"locus": "hs-fresh", "ssh": "ubuntu@10.237.23.104", "port": 9125}]

An explicit $HUGPY_TUI_LOCI names the file AND turns the registry off: that
file is then the whole list.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from urllib.parse import urlsplit

from .discovery import probe

PATH = os.path.expanduser(os.environ.get("HUGPY_TUI_LOCI") or "~/.hugpy/tui-loci.json")
REGISTRY_TIMEOUT = 4.0


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


def registry_enabled(environ=None):
    """The registry is read unless the operator named a loci file or turned the
    toolserver off for this process (HUGPY_AGENT_TOOLSERVER=0)."""
    environ = os.environ if environ is None else environ
    if environ.get("HUGPY_TUI_LOCI"):
        return False
    return (environ.get("HUGPY_AGENT_TOOLSERVER") or "").strip().lower() not in ("0", "false", "no", "off")


def _is_local(host):
    """True when `host` is this machine: loopback, our hostname, or an address
    the kernel routes from itself (the source it picks for it IS that address)."""
    if not host or host == "localhost" or host.startswith("127.") or host == socket.gethostname():
        return True
    try:
        addr = socket.gethostbyname(host)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((addr, 9))
            return s.getsockname()[0] == addr
    except OSError:
        return False


def _ssh_of(row):
    """(user@host, ssh port, host) of a registry row: pointer.ssh, else its endpoint."""
    ssh = (row.get("pointer") or {}).get("ssh")
    if isinstance(ssh, dict) and ssh.get("host"):
        target = "%s@%s" % (ssh["user"], ssh["host"]) if ssh.get("user") else ssh["host"]
        return target, int(ssh.get("port") or 22), ssh["host"]
    endpoint = str(row.get("endpoint") or "")
    host = endpoint.rsplit("@", 1)[-1]
    if not host or "@" not in endpoint:
        return "", 22, ""
    host, _, port = host.partition(":")
    return "%s@%s" % (endpoint.rsplit("@", 1)[0], host), int(port) if port.isdigit() else 22, host


def registry_loci(rows, is_local=_is_local):
    """Loci entries from the toolserver's loci_list rows: each active locus that
    publishes its serve (pointer.serve_url). A loopback serve_url is an address
    on the LOCUS'S host: used as is when that host is this one, otherwise it is
    the port behind the locus's ssh endpoint. Any other serve_url is used as is."""
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not row.get("locus") or row.get("status") not in (None, "active"):
            continue
        pointer = row.get("pointer") if isinstance(row.get("pointer"), dict) else {}
        url = str(pointer.get("serve_url") or "").rstrip("/")
        parts = urlsplit(url)
        if not parts.scheme or not parts.hostname:
            continue
        target, ssh_port, host = _ssh_of(row)
        loopback = parts.hostname == "localhost" or parts.hostname.startswith("127.")
        if not loopback or (host and is_local(host)):
            out.append({"locus": row["locus"], "serve": url, "source": "registry"})
        elif target and parts.port:
            entry = {"locus": row["locus"], "ssh": target, "port": parts.port, "source": "registry"}
            if ssh_port != 22:
                entry["ssh_port"] = ssh_port
            out.append(entry)
    return out


def fetch_registry(client=None, timeout=REGISTRY_TIMEOUT):
    """The registry's loci entries, read through the toolserver client. Raises
    when the toolserver cannot be reached or refuses (the caller keeps the file)."""
    if client is None:
        from ..toolserver_client import ToolserverClient
        client = ToolserverClient()
    rows = client.call("loci_list", {"kind": "", "status": "active"}, timeout=timeout)
    if isinstance(rows, dict) and rows.get("error"):
        raise RuntimeError(str(rows["error"]))
    return registry_loci(rows)


def merge(registry, local):
    """The registry's loci first; the file's loci it does not name after them."""
    named = {e["locus"] for e in registry}
    return list(registry) + [e for e in local if e["locus"] not in named]


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
               "-o", "ConnectTimeout=5", "-L", "127.0.0.1:%d:127.0.0.1:%d" % (lp, port)]
        if entry.get("ssh_port"):
            cmd += ["-p", str(int(entry["ssh_port"]))]
        cmd.append(entry["ssh"])
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
