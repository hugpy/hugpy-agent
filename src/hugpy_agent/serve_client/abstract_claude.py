"""hugpy_agent.serve_client.abstract_claude — re-export of the identical hugpy_harness core module.

Byte-identical to the harness original; hugpy_agent re-exports it so there is
one source of truth. A pip-installed hugpy-harness supplies it; the source-tree
shim on sys.path does when running uninstalled.
"""
from hugpy_harness.serve_client import abstract_claude as _m
import sys as _sys
_sys.modules[__name__] = _m
