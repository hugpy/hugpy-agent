"""Back-compat shim: moved to hugpy_agent.runtime.loop."""
from .runtime import loop as _m
import sys as _sys
_sys.modules[__name__] = _m
