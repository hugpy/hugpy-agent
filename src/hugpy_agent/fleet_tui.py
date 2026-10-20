"""Back-compat shim: moved to hugpy_agent.fleet.fleet_tui."""
from .fleet import fleet_tui as _m
import sys as _sys
_sys.modules[__name__] = _m
