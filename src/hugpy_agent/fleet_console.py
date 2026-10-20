"""Back-compat shim: moved to hugpy_agent.fleet.fleet_console."""
from .fleet import fleet_console as _m
import sys as _sys
_sys.modules[__name__] = _m
