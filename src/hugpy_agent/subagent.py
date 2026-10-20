"""Back-compat shim: moved to hugpy_agent.runtime.subagent."""
from .runtime import subagent as _m
import sys as _sys
_sys.modules[__name__] = _m
