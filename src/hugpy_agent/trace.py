"""Back-compat shim: moved to hugpy_agent.runtime.trace."""
from .runtime import trace as _m
import sys as _sys
_sys.modules[__name__] = _m
