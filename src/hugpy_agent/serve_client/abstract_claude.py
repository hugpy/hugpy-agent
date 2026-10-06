"""Backward-compatible import alias for the provider-neutral Serve client.

New code should import :mod:`abstract_serve`; the API is the shared Serve
protocol, not a Claude provider adapter.
"""
from .abstract_serve import *  # noqa: F401,F403
from .abstract_serve import AbstractServeClient as AbstractClaudeClient
