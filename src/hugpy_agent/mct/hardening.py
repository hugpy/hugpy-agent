"""Runtime hardening checks.

Design ref: §13.2 (B process identity), §20.4 (dedicated account before deploy),
registry row 15. B must run as a dedicated unprivileged account — never one with
root-equivalent group membership. This is a deterministic startup gate: if the
process can escalate, deployment stops (§20.4).
"""
from __future__ import annotations

import os

from .errors import HardeningError

# Groups that confer root-equivalent power. Membership here means B could escape
# its intended confinement regardless of MCT's own controls.
ROOT_EQUIVALENT_GROUPS = {"sudo", "wheel", "root", "docker", "lxd", "kvm", "adm"}


def _group_names() -> set[str]:
    names: set[str] = set()
    try:
        import grp
        for gid in set(os.getgroups()) | {os.getgid()}:
            try:
                names.add(grp.getgrgid(gid).gr_name)
            except KeyError:
                pass
    except Exception:  # pragma: no cover - non-POSIX
        pass
    return names


def check_service_account() -> list[str]:
    """Return a list of hardening violations (empty means OK)."""
    violations = []
    if os.getuid() == 0:
        violations.append("running as root (uid 0)")
    bad = _group_names() & ROOT_EQUIVALENT_GROUPS
    if bad:
        violations.append(f"member of root-equivalent groups: {sorted(bad)}")
    return violations


def assert_hardened(*, enforce: bool = True) -> list[str]:
    """Fail closed if the process is not safely confined (§20.4).

    ``enforce=False`` (dev default) returns the violations without raising so a
    prototype can run under a developer account; production sets ``enforce=True``.
    """
    violations = check_service_account()
    if violations and enforce:
        raise HardeningError("refusing to start: " + "; ".join(violations),
                             detail={"violations": violations})
    return violations
