"""The packaged Fleet console arm, loaded only when a server serves /fleet.

Mirrors ``hugpy_media.ui``: the built single-page app lives in the wheel under
``hugpy_agent/ui_dist``; import this only when actually serving the UI.
"""
from __future__ import annotations

from importlib import resources
from pathlib import Path


def dist_dir() -> Path | None:
    """Return this wheel's built Fleet UI directory when it contains an index."""
    root = Path(str(resources.files("hugpy_agent").joinpath("ui_dist")))
    return root if (root / "index.html").is_file() else None
