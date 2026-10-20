"""Offline unit tests. No test in this package may touch the network —
model behavior is scripted through FakeGateway (see helpers.py).

Make `hugpy_agent` importable straight from the source tree so
`python -m unittest discover tests` works even before `pip install -e .`.
"""
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, os.path.abspath(_SRC))

# hugpy_agent builds on the hugpy_harness core; make the sibling source
# package importable when running uninstalled (a pip-installed one wins).
_HARNESS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "hugpy_harness", "src"))
if os.path.isdir(os.path.join(_HARNESS, "hugpy_harness")) and _HARNESS not in sys.path:
    sys.path.insert(0, _HARNESS)
