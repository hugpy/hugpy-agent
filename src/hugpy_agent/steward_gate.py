"""Back-compat shim: moved to hugpy_agent.eval.steward_gate."""
from .eval import steward_gate as _m
import sys as _sys
_sys.modules[__name__] = _m
