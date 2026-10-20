"""Shared base errors for hugpy_agent.

Module-specific errors (gateway, console, fleet, rag, mct) keep their own
classes; this is the common base so a caller can ``except AgentError`` across
the package without importing every module.
"""


class AgentError(Exception):
    """Base class for hugpy_agent runtime errors."""
