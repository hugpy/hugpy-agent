"""Fleet-console download artifacts (.deb / agent .whl) for the Fleet arm.

Faithful local port of legacy ``agent_routes.console_dist_info`` /
``console_dist_download``: scan a directory, report the NEWEST matching file,
compute size/sha256 from the real file, and stream it. Nothing is fabricated —
when the directory holds no matching file the field is ``null`` (explicit
absence), and sha256 is a real digest (from a ``<name>.sha256`` sidecar when
present, else computed once and cached by path+size+mtime).

Directory: ``$HUGPY_AGENT_ARTIFACTS_DIR`` (or legacy ``$HUGPY_CONSOLE_ARTIFACTS_DIR``),
default ``/usr/share/hugpy-agent/console``.
"""
from __future__ import annotations

import hashlib
import os
import re

from ..imports import constants as C

_DEB_PREFIXES = ("hugpy-agent_", "fleet-console_")
_WHL_PREFIX = "hugpy_agent-"
_DEFAULT_DIR = "/usr/share/hugpy-agent/console"
# A private shelf outside the public apt document root. Links are the only
# HTTP path to these files; staging them in repo/pool would bypass revocation.
_DEFAULT_POOL = "/srv/hugpy-apt/installers"
PACKAGE_KINDS = {
    "agent": "hugpy-agent", "console": "hugpy-station",
    "hugpy-agent": "hugpy-agent", "hugpy-station": "hugpy-station",
    "hugpy-core": "hugpy-core", "hugpy-wrapper": "hugpy-wrapper",
    "hugpy-media": "hugpy-media", "hugpy-video": "hugpy-video",
    "hugpy-harness": "hugpy-harness", "hugpy-llama-server": "hugpy-llama-server",
    "hugpy-tools": "hugpy-tools", "hugpy-toolserver": "hugpy-toolserver",
    "hugpy-oracle": "hugpy-oracle",
}

_SHA_CACHE: dict = {}  # path -> (size, mtime, sha256)


def _artifacts_dir() -> str:
    return (os.environ.get(C.ENV_CONSOLE_ARTIFACTS_DIR)
            or os.environ.get("HUGPY_CONSOLE_ARTIFACTS_DIR")
            or _DEFAULT_DIR)


def _version_key(name: str):
    """Natural-version sort key: the numeric groups in the filename, in order."""
    return [int(n) for n in re.findall(r"\d+", name)]


def _newest(dirpath: str, prefixes, suffix: str):
    try:
        names = os.listdir(dirpath)
    except OSError:
        return None
    cands = [n for n in names
             if n.endswith(suffix) and any(n.startswith(p) for p in prefixes)]
    if not cands:
        return None
    cands.sort(key=lambda n: (_version_key(n), os.path.getmtime(os.path.join(dirpath, n))))
    return cands[-1]


def _sha256(path: str):
    name = os.path.basename(path)
    sidecar = os.path.join(os.path.dirname(path), name + ".sha256")
    try:
        with open(sidecar, encoding="utf-8") as fh:
            tok = fh.read().split()
            if tok:
                return tok[0]
    except OSError:
        pass
    try:
        st = os.stat(path)
    except OSError:
        return None
    cached = _SHA_CACHE.get(path)
    if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
        return cached[2]
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    digest = h.hexdigest()
    _SHA_CACHE[path] = (st.st_size, st.st_mtime_ns, digest)
    return digest


def _artifact(dirpath: str, name):
    if not name:
        return None
    path = os.path.join(dirpath, name)
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    return {
        "filename": name,
        "size_bytes": size,
        "url": f"/api/agent/console_file?name={name}",
        "sha256": _sha256(path),
    }


def newest_deb_name():
    """The newest staged console .deb filename, or None when none is staged.
    The install-link script embeds this exact name in its install_file URL."""
    return _newest(_artifacts_dir(), _DEB_PREFIXES, ".deb")


def package_deb(kind):
    """Return the newest staged Debian artifact for a supported install kind."""
    package = PACKAGE_KINDS.get(kind)
    if not package:
        return None
    pool = os.environ.get("HUGPY_INSTALLER_POOL_DIR", _DEFAULT_POOL)
    directory = os.path.join(pool, package)
    name = _newest(directory, (package + "_",), ".deb")
    if not name and package == "hugpy-agent":
        directory = _artifacts_dir()
        name = _newest(directory, (package + "_",), ".deb")
    return (directory, name) if name else None


def package_catalog():
    """Artifact facts for the install-link picker; missing packages stay visible."""
    return {kind: ({"filename": found[1], "sha256": _sha256(os.path.join(*found))}
                   if (found := package_deb(kind)) else None)
            for kind in PACKAGE_KINDS}


def package_file(kind, name):
    """Stream only the artifact pinned to this link's package kind."""
    from flask import Response, send_from_directory
    package = PACKAGE_KINDS.get(kind)
    if not package or not name or os.path.basename(name) != name or \
            not re.fullmatch(re.escape(package) + r"_[A-Za-z0-9.+:~_-]+_(?:amd64|all)\.deb", name):
        return Response("not found", status=404, mimetype="text/plain")
    pool = os.environ.get("HUGPY_INSTALLER_POOL_DIR", _DEFAULT_POOL)
    directory = os.path.join(pool, package)
    if package == "hugpy-agent" and not os.path.isfile(os.path.join(directory, name)):
        directory = _artifacts_dir()
    if not os.path.isfile(os.path.join(directory, name)):
        return Response("not found", status=404, mimetype="text/plain")
    return send_from_directory(directory, name, as_attachment=True,
                               download_name=name, conditional=False)


def console_info() -> dict:
    """Report the newest console .deb and paired agent .whl in the artifacts dir.

    ``deb`` / ``agent_whl`` are null when no matching file is staged.
    """
    d = _artifacts_dir()
    deb_name = _newest(d, _DEB_PREFIXES, ".deb")
    whl_name = _newest(d, (_WHL_PREFIX,), ".whl")
    deb = _artifact(d, deb_name)
    whl = _artifact(d, whl_name)
    install = None
    if deb:
        install = {"latest_deb": deb["filename"],
                   "url": f"/api/agent/console_file?name={deb['filename']}"}
    return {"dir": d, "deb": deb, "agent_whl": whl, "install": install}


def console_file(name=None):
    """Stream one staged console artifact (bare ``.deb``/``.whl`` filename).

    Returns a flask Response (passed through the toolset wrapper untouched): the
    file on success, a 404 Response when the name is unsafe or no such file is
    staged (a raw ``abort`` would be caught and turned into a 500 upstream).
    """
    from flask import Response, send_from_directory
    if not name or "/" in name or "\\" in name or not name.endswith((".deb", ".whl")):
        return Response("not found", status=404, mimetype="text/plain")
    d = _artifacts_dir()
    if not os.path.isfile(os.path.join(d, name)):
        return Response("not found", status=404, mimetype="text/plain")
    return send_from_directory(d, name, as_attachment=True,
                               download_name=name, conditional=True)
