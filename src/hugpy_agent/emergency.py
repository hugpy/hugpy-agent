"""Back-compat shim: moved to hugpy_agent.runtime.emergency."""
from .runtime import emergency as _m
import sys as _sys
_sys.modules[__name__] = _m
