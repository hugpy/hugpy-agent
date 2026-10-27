"""hugpy_agent gateway — the hugpy_terminal Gateway core with session signals
ON for every model call.

The whole request/response surface (route resolution, payload build, SSE
streaming, error-as-data, the brain-ladder helpers) lives in
hugpy_terminal.gateway and is re-exported here unchanged. The one hugpy-fleet
addition is session_signals: identity headers + a renewed lease on every chat,
and cancel-on-abort — wired through the harness's Gateway.request_ctx hook so
the harness core never has to import this package.
"""
from __future__ import annotations

import contextlib

from hugpy_terminal.gateway import (  # noqa: F401  (re-exported surface)
    CONTINUATION_LEAK,
    ChatResult,
    Gateway as _BaseGateway,
    apply_no_think,
    brain_matches_key,
    candidate_routes,
    estimate_tokens,
    is_capacity_error,
    normalize_base,
    origin,
    pick_ladder_brain,
    pick_resident_brain,
    resolve_brain_ladder,
    slot_model_keys,
    warm_model_keys,
)


class Gateway(_BaseGateway):
    """Hugpy fleet gateway: session_signals ON. A third-party provider
    subclass sets ``session_signals = False`` so no X-Hugpy-* identity ever
    leaves for someone else's API (the original doctrine, preserved)."""

    session_signals = True

    def request_ctx(self, chat_url, key=""):
        """THE in-process choke point for session state: every hugpy model
        call carries session/turn/request identity and holds the session's
        lease while in flight; an abort (Ctrl-C, stream cut) cancels it on
        central. Disabled -> the harness's no-op request."""
        if not self.session_signals:
            return super().request_ctx(chat_url, key)
        from .runtime import session_signals as _ss
        sig = _ss.signals()
        if sig is None:
            return super().request_ctx(chat_url, key)
        return _session_request(sig, _ss.api_prefix(chat_url), key)


@contextlib.contextmanager
def _session_request(sig, prefix, key):
    with sig.request(prefix, key) as rq:   # rq has .headers and .cancel
        yield rq
