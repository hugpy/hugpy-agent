"""Imported at the top of every test module (before hugpy_agent imports).

`python -m unittest discover tests` loads test files as TOP-LEVEL modules
(the tests/ dir goes on sys.path, the package __init__ never runs), so the
src/ path shim must live in a module the tests import explicitly. After
`pip install -e .` this is a no-op.
"""
import os
import sys

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# The TUI's loci come from the toolserver registry and ~/.hugpy/tui-loci.json;
# an explicit file turns the registry off, so no unit test reads the operator's
# loci or calls a live toolserver.
os.environ.setdefault("HUGPY_TUI_LOCI", os.path.join(os.path.dirname(__file__), "no-such-loci.json"))
