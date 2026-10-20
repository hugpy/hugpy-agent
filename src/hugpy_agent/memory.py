"""Back-compat shim: moved to hugpy_agent.runtime.memory."""
from .runtime import memory as _m
import sys as _sys
_sys.modules[__name__] = _m
