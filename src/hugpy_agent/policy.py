"""Back-compat shim: moved to hugpy_agent.runtime.policy."""
from .runtime import policy as _m
import sys as _sys
_sys.modules[__name__] = _m
