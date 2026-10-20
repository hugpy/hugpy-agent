"""Back-compat shim: moved to hugpy_agent.eval.existing_system_benchmark."""
from .eval import existing_system_benchmark as _m
import sys as _sys
_sys.modules[__name__] = _m
