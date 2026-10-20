"""Back-compat shim: moved to hugpy_agent.eval.steward_eval_runner."""
from .eval import steward_eval_runner as _m
import sys as _sys
_sys.modules[__name__] = _m
