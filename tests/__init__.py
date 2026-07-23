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
