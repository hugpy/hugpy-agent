"""Back-compat shim: moved to hugpy_agent.runtime.audit."""
from .runtime import audit as _m
import sys as _sys
_sys.modules[__name__] = _m
