"""Back-compat shim: moved to hugpy_agent.fleet.fleet_benchmark."""
from .fleet import fleet_benchmark as _m
import sys as _sys
_sys.modules[__name__] = _m
