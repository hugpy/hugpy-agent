"""Policy engine — per-tool-call permission decisions (P2.1, design Phase 2).

`decide` is a PURE function: no I/O, no config lookup, no clock. The loop
passes everything in, which keeps the entire permission matrix offline-
testable and puts the precedence rule in exactly one place:

    explicit deny  >  explicit allow  >  mode default

Modes (fail-closed doctrine throughout):
  readonly — only readonly-risk tools run; everything else is denied.
  ask      — the default posture. Readonly tools auto-allow; any
             UNSAFE_TO_RERUN risk class escalates to the operator ("ask").
             Until the escalation channel ships (P2.3) the loop degrades
             an "ask" decision to deny — fail closed, never fail open.
  auto     — allow, still subject to the allow/deny lists.

An UNKNOWN mode is treated as `ask`: a typo'd HUGPY_POLICY must tighten the
gate, never open it. Likewise an unknown risk class is treated as unsafe —
anything not provably readonly gets the unsafe default for the mode.

A denial is never an exception: the loop turns it into an error-as-data
string for the model (doctrine: a blocked tool is an observation, not a
crash).
"""
from __future__ import annotations

from .tools import RISK_READONLY, UNSAFE_TO_RERUN, ToolSpec

ALLOW = "allow"
ASK = "ask"
DENY = "deny"

MODES = ("readonly", "ask", "auto")


def decide(mode: str, spec: ToolSpec, args: dict,
           allow: list | None = None, deny: list | None = None) -> str:
    """Decide `allow` | `ask` | `deny` for one tool call.

    `args` is part of the contract (future arg-level rules, e.g. per-path
    write policies) but unused today. `allow`/`deny` are tool-name lists;
    an explicit entry overrides the mode in every mode — deny first.
    """
    if deny and spec.name in deny:
        return DENY
    if allow and spec.name in allow:
        return ALLOW

    mode = (mode or "").strip().lower()
    if mode not in MODES:
        mode = "ask"                       # unknown mode: fail closed
    # Not provably readonly => unsafe. Keyed off BOTH the risk-class constant
    # and UNSAFE_TO_RERUN so a future class added to one set but not the
    # other still fails closed.
    unsafe = spec.risk_class in UNSAFE_TO_RERUN or spec.risk_class != RISK_READONLY

    if mode == "auto":
        return ALLOW
    if mode == "readonly":
        return DENY if unsafe else ALLOW
    return ASK if unsafe else ALLOW        # mode == "ask"
