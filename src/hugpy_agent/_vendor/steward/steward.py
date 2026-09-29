#!/usr/bin/env python3
"""
steward — the delegation ceiling and the enforcement decision for the shared
command bus. Pure, stdlib-only, importable by the console backend; no I/O, no
sockets, so it unit-tests in isolation and wires in with one `import`.

The invariant this module exists to hold (spec §2, §6):

    the actor a constraint governs never enforces that constraint.

So enforcement lives HERE, called by the bus BELOW the keeper — never inside
the model. The keeper requests; `decide()` rules.

Tiers (spec §1):
  Tier 0  admin      who may edit the policy itself  (user always, keeper by
                     switch, frontier never — not editable)
  Tier 1  grant      the ceiling the frontier may borrow, as verb×target×scope
  Tier 2  bus        calls decide() and stamps origin  (this module's caller)
"""
from __future__ import annotations
import fnmatch
import time
import uuid
from dataclasses import dataclass, field, replace

# origin tags the bus stamps BEFORE the check (spec §4). Authority is flat for
# the first two; only the third is ever gated.
USER = "user"
LOCAL = "local-keeper"
FRONTIER = "frontier-via-local-keeper"
ACTORS = (USER, LOCAL, FRONTIER)

# scopes a grant can carry (spec §3)
STANDING = "standing"   # persists until the policy changes
SESSION = "session"     # valid for the current keeper session
ONCE = "once"           # consumed on first matching use

# operations that touch the host itself rather than a guest are a distinct,
# separately-delegable capability (spec §2/§3): default-off for the frontier.
HOST_TARGETS = frozenset({"host", "ae", "manager", "self"})


@dataclass(frozen=True)
class Grant:
    verb: str            # "vm.snapshot", "vm.*", "file.read", ...
    target: str = "*"    # glob against the op target ("sandbox", "vm/*", "*")
    scope: str = SESSION

    def matches(self, verb: str, target: str) -> bool:
        return fnmatch.fnmatch(verb, self.verb) and fnmatch.fnmatch(target or "", self.target)


@dataclass(frozen=True)
class Policy:
    version: int = 0
    # Tier 0 — admission to editing THIS object. user is always "always";
    # keeper is the §1 switch (on|off); frontier is the invariant "never".
    keeper_admin: bool = False
    grant: tuple[Grant, ...] = ()
    default: str = "deny"          # anything ungranted -> refused

    # ---- Tier 0: who may change the ceiling (spec §1) -------------------
    def may_admin(self, actor: str) -> bool:
        """The meta-authority rule. Frontier can NEVER widen its own leash;
        the keeper only if the user flipped the switch; the user always."""
        if actor == USER:
            return True
        if actor == LOCAL:
            return self.keeper_admin
        return False               # FRONTIER and anything unknown: never

    def with_switch(self, on: bool, by: str) -> "Policy":
        """Flip the keeper-admin switch. Only a Tier-0 admin may do so, and by
        construction the frontier is refused before it reaches here."""
        if not self.may_admin(by):
            raise PermissionError(f"{by} may not administer the Steward")
        return replace(self, keeper_admin=on, version=self.version + 1)

    def with_grant(self, grants: tuple[Grant, ...], by: str) -> "Policy":
        if not self.may_admin(by):
            raise PermissionError(f"{by} may not administer the Steward")
        return replace(self, grant=tuple(grants), version=self.version + 1)


@dataclass
class Decision:
    allowed: bool
    reason: str
    gated: bool           # True when the Steward actually adjudicated (frontier)
    consumed: Grant | None = None   # a ONCE grant the caller must retire


def decide(policy: Policy, actor: str, verb: str, target: str) -> Decision:
    """The single enforcement point. The bus calls this AFTER stamping origin
    and BEFORE executing. User and local-keeper are at parity (spec §0) — flat
    allow. Only frontier-origin actions are measured against the ceiling."""
    if actor in (USER, LOCAL):
        return Decision(True, "parity", gated=False)
    if actor != FRONTIER:
        return Decision(False, f"unknown actor {actor!r}", gated=True)

    # host-targeting is its own capability; only a grant that LITERALLY names a
    # host target confers it — never a glob ("a*", "*t", "[hx]ost" all match
    # "ae"/"host" under fnmatch, which is exactly how an innocuous-looking
    # station grant would silently hand the frontier the host), and never the
    # default-allow fallback (the reason below must always match the verdict).
    host_hit = (target or "").lower() in HOST_TARGETS
    for g in policy.grant:
        if not g.matches(verb, target):
            continue
        if host_hit and g.target.lower() not in HOST_TARGETS:
            continue
        return Decision(True, f"grant {g.verb}@{g.target}", gated=True,
                        consumed=g if g.scope == ONCE else None)

    if host_hit:
        return Decision(False, "host target requires an explicit host grant", gated=True)
    return Decision(policy.default == "allow",
                    f"no grant for {verb} on {target or '*'}", gated=True)


def event(policy: Policy, actor: str, operation: str, target: str,
          phase: str, decision: Decision | None = None,
          correlation_id: str | None = None) -> dict:
    """Shape one bus event (spec §4). Emitted at 'requested' (intent) and again
    at 'completed'/'failed' (outcome), linked by correlation_id. Denials are
    first-class — they carry reason and the policy_version that refused."""
    ev = {
        "correlation_id": correlation_id or uuid.uuid4().hex,
        "ts": time.time(),
        "actor": actor,
        "operation": operation,
        "target": target,
        "phase": phase,                        # requested | completed | failed
        "policy_version": policy.version,
    }
    if decision is not None and decision.gated:
        ev["steward"] = "allowed" if decision.allowed else "denied"
        ev["reason"] = decision.reason
    return ev


# --------------------------------------------------------------------------- #
# the legible cage — the frontier's read of its own ceiling (visibility, never
# control). Handing this to the model turns "act and hit an opaque wall" into
# "plan within known bounds, and ask for more the one sanctioned way." Most of
# the observed constraint-anxiety is uncertainty about the cage, not the cage.
# --------------------------------------------------------------------------- #
def reach(policy: Policy) -> dict:
    """A read-only description of what the frontier may do right now. Safe to
    surface to the model (it's the principal table's 'read effective state')."""
    return {
        "policy_version": policy.version,
        "granted": [{"verb": g.verb, "target": g.target, "scope": g.scope}
                    for g in policy.grant],
        "default": policy.default,
        "host_targets": ("need a grant that literally names a host target; "
                         "globs and default-allow never reach the host"),
    }


def denial_detail(policy: Policy, verb: str, target: str, decision: Decision) -> dict:
    """A legible refusal: why, what you CAN do, and the one way to get more.
    The escalation is a request to the operator — the frontier cannot self-grant
    (Tier 0), so this names the path without implying the model can walk it."""
    return {
        "reason": decision.reason,
        "reach": reach(policy),
        "escalation": (f"not in your current reach. To proceed, ask your operator "
                       f"to grant {verb!r} on {target or '*'!r}; the keeper relays "
                       f"the request — the frontier cannot widen its own ceiling."),
    }


def reach_brief(policy: Policy) -> str:
    """The compact, deterministic rendering for the init prompt (spec: state the
    cage up front so the model never spends the first attempt-plus-denial
    learning it). Token-sparing by design and STABLE for a given policy (so it
    doesn't churn the prompt cache); carries the version so a mid-session grant
    change is detectable. Empty when there is nothing to say — don't spend
    tokens describing an absent cage. Under default-allow the grants are not a
    ceiling (everything ungranted is permitted anyway), so describing them as
    one would lie to the model — say nothing instead."""
    if policy.default == "allow":
        return ""
    if policy.grant:
        allowed = ", ".join(f"{g.verb}@{g.target}" for g in policy.grant)
        head = f"Delegated authority (policy v{policy.version}): {allowed}."
    else:
        head = f"No delegated authority yet (policy v{policy.version})."
    return (head + " Anything else is denied until your operator grants it; a denied "
            "call returns exactly what to request. You cannot widen this yourself.")


# --------------------------------------------------------------------------- #
# self-tests — run `python3 steward.py`
# --------------------------------------------------------------------------- #
def _selftest() -> None:
    base = Policy(
        version=7, keeper_admin=False, default="deny",
        grant=(
            Grant("vm.snapshot", "*", SESSION),
            Grant("vm.exec", "sandbox", SESSION),
            Grant("vm.list", "*", STANDING),
        ),
    )

    def ok(cond, label):
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        assert cond, label

    print("parity — user/keeper are ungated:")
    ok(decide(base, USER, "vm.delete", "sandbox").allowed, "user may delete")
    ok(decide(base, LOCAL, "vm.exec", "host").allowed, "keeper may host-exec (parity)")
    ok(not decide(base, USER, "vm.start", "x").gated, "user path is never gated")

    print("frontier — measured against the ceiling:")
    ok(decide(base, FRONTIER, "vm.snapshot", "keeper").allowed, "granted snapshot, any target")
    ok(decide(base, FRONTIER, "vm.exec", "sandbox").allowed, "granted exec on sandbox")
    ok(not decide(base, FRONTIER, "vm.exec", "keeper").allowed, "exec on ungranted target denied")
    ok(not decide(base, FRONTIER, "vm.delete", "sandbox").allowed, "delete never granted -> deny")
    d = decide(base, FRONTIER, "vm.exec", "host")
    ok(not d.allowed and "host" in d.reason, "host-exec needs explicit host grant")
    ok(not decide(base, FRONTIER, "vm.snapshot", "host").allowed, "wildcard grant does not reach host")

    print("Tier 0 — meta-authority / the switch:")
    ok(base.may_admin(USER), "user always admin")
    ok(not base.may_admin(LOCAL), "keeper not admin while switch off")
    ok(not base.may_admin(FRONTIER), "frontier never admin")
    on = base.with_switch(True, by=USER)
    ok(on.may_admin(LOCAL) and on.version == 8, "user flips switch -> keeper admin, version bumps")
    try:
        base.with_switch(True, by=FRONTIER); raised = False
    except PermissionError:
        raised = True
    ok(raised, "frontier cannot flip its own switch")

    print("host-exec as an explicit, separately-delegable grant:")
    hostpol = base.with_grant(base.grant + (Grant("vm.exec", "host", ONCE),), by=USER)
    hd = decide(hostpol, FRONTIER, "vm.exec", "host")
    ok(hd.allowed and hd.consumed is not None, "explicit host grant allows, ONCE marked for retire")

    print("events — intent + first-class denials:")
    cid = uuid.uuid4().hex
    req = event(base, FRONTIER, "vm.delete", "sandbox", "requested",
                decision=decide(base, FRONTIER, "vm.delete", "sandbox"), correlation_id=cid)
    ok(req["steward"] == "denied" and req["policy_version"] == 7, "denial event carries verdict + version")
    ok(req["correlation_id"] == cid, "correlation id threads intent->outcome")
    user_ev = event(base, USER, "vm.delete", "sandbox", "completed",
                    decision=decide(base, USER, "vm.delete", "sandbox"))
    ok("steward" not in user_ev, "ungated user action carries no steward verdict")

    print("\nALL PASS")


if __name__ == "__main__":
    _selftest()
