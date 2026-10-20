"""Back-compat shim: moved to hugpy_agent.runtime.session_signals."""
from .runtime import session_signals as _m
import sys as _sys
_sys.modules[__name__] = _m
