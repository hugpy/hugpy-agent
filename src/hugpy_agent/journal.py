"""Back-compat shim: moved to hugpy_agent.runtime.journal."""
from .runtime import journal as _m
import sys as _sys
_sys.modules[__name__] = _m
