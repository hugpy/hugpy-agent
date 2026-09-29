"""MCT exception hierarchy.

Every privileged operation fails **closed** (design §3, §22.1). These exceptions
are the closed-failure signals. They carry a stable ``code`` so a control-plane
``error`` envelope can point to a structured ``error_detail`` object without
leaking bodies (design §6.1, §18.3).
"""
from __future__ import annotations


class MctError(Exception):
    """Base for all MCT failures. ``code`` is a stable, log-safe slug."""

    code = "mct.error"

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


class ProtocolError(MctError):
    """Malformed, oversized, wrong-version, or schema-invalid message (§8, §23)."""

    code = "mct.protocol"


class IntegrityError(MctError):
    """Digest mismatch or corrupt object; the object is quarantined (invariant 4)."""

    code = "mct.integrity"


class AuthorizationError(MctError):
    """Request outside the caller's effective capability (§13.1, invariant 12)."""

    code = "mct.authorization"


class IsolationError(AuthorizationError):
    """Cross-session / cross-tenant access attempt (§7.4, adversarial case 5)."""

    code = "mct.isolation"


class NotFoundError(MctError):
    """Authorized lookup completed without a match (§10.2 ``not_found``)."""

    code = "mct.not_found"


class StateError(MctError):
    """Illegal turn/epoch/sequence transition (§15)."""

    code = "mct.state"


class BudgetError(MctError):
    """Pull count / token / byte / time budget exhausted (§10.3)."""

    code = "mct.budget"


class LedgerUnavailable(MctError):
    """Audit state is unavailable; B must stop accepting turns (§17)."""

    code = "mct.ledger_unavailable"


class QuotaError(MctError):
    """Per-session disk quota exhausted; pause ingestion, preserve existing (§17)."""

    code = "mct.quota"


class HardeningError(MctError):
    """Process is not safely confined (e.g. root-equivalent groups); refuse to start (§13.2)."""

    code = "mct.hardening"
