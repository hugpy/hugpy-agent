"""hugpy_agent.tui.views.transcript — re-export of the identical hugpy_harness core module.

Byte-identical to the harness original (branding is driven by
hugpy_harness.imports.constants.service_name(), which hugpy_agent.tui sets to
"hugpy-agent"); re-exported so there is one source of truth. A pip-installed
hugpy-harness supplies it; the source-tree shim on sys.path does when running
uninstalled.
"""
from hugpy_harness.tui.views import transcript as _m
import sys as _sys
_sys.modules[__name__] = _m
