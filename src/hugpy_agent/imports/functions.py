"""Dependency-light helpers shared across hugpy_agent.

Nothing here imports a hugpy_* package or a vendor SDK; these are the small
primitives (env/.env parsing, truthiness, lazy import, the HUGPY_HOME resolver)
that config.py, _paths.py and the subpackages all reuse from one place.
"""
from __future__ import annotations

import importlib
import os

from . import constants as C


def lazy(path):
    """Import ``module`` or ``module:attr`` on first use (missing backend -> error)."""
    module, _, attr = path.partition(":")
    mod = importlib.import_module(module)
    return getattr(mod, attr) if attr else mod


def as_bool(value) -> bool | None:
    """Parse a config truthiness word; None when unrecognized (keep prior value)."""
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return None


def parse_env_file(path: str) -> dict:
    """Minimal .env parser: KEY=VALUE lines, '#' comments, optional quotes.
    Deliberately no interpolation/escapes — predictability over features."""
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def home_dir() -> str:
    """Resolve HUGPY_HOME (default ``~/.hugpy``)."""
    return os.environ.get(C.ENV_HUGPY_HOME) or os.path.join(
        os.path.expanduser("~"), C.HUGPY_HOME_DIRNAME)


def relocate(new: str, *legacy: str) -> str:
    """Return ``new`` after migrating the first existing ``~/<legacy>`` into it.

    Atomic rename, cross-fs move fallback; any OSError is swallowed so a failed
    migration never stops startup — the new (empty) location is used instead.
    """
    try:
        os.makedirs(os.path.dirname(new), exist_ok=True)
        if os.path.exists(new):
            return new
        for lname in legacy:
            old = os.path.join(os.path.expanduser("~"), lname)
            if os.path.exists(old) and os.path.abspath(old) != os.path.abspath(new):
                try:
                    os.replace(old, new)
                except OSError:
                    import shutil
                    shutil.move(old, new)
                break
    except OSError:
        pass
    return new
