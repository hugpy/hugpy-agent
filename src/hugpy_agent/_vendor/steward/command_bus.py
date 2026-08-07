#!/usr/bin/env python3
"""
command_bus — the single write path (spec §2). Every effector call, from the
operator or the keeper, passes through dispatch(): stamp origin -> Steward
check -> execute -> emit. Enforcement is here, below the model; the keeper
requests, the bus decides.

Pure orchestration over `steward` — no sockets, no server framework — so it
tests in isolation and drops into server.py as `bus.dispatch(...)` wrapping
the existing effector coroutines.

Folds in two spec §6 seams up front:
  - per-target serialization: two writers can't interleave on one target
  - ONCE-grant retirement: a consumed grant is handed back for the store to drop
"""
from __future__ import annotations
import asyncio
import uuid
from typing import Awaitable, Callable

from . import steward
from .steward import Policy, Decision


class StewardDenied(Exception):
    """Raised when a frontier-origin action exceeds the current ceiling. Carries
    a legible `detail` (why + current reach + escalation path) for the model."""
    def __init__(self, decision: Decision, detail: dict | None = None):
        super().__init__(decision.reason)
        self.decision = decision
        self.detail = detail or {"reason": decision.reason}


class CommandBus:
    def __init__(self,
                 policy_provider: Callable[[], Policy],
                 emit: Callable[[dict], None],
                 on_consume: Callable[[object], None] | None = None):
        self._policy = policy_provider      # returns the CURRENT ceiling each call
        self._emit = emit                   # audit sink (append-only, §4)
        self._on_consume = on_consume       # retire a ONCE grant
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, target: str) -> asyncio.Lock:
        # one lock per target; serializes mutations on the same VM/path (§6)
        lk = self._locks.get(target)
        if lk is None:
            lk = self._locks[target] = asyncio.Lock()
        return lk

    async def dispatch(self, actor: str, operation: str, target: str,
                       effector: Callable[[], Awaitable], *,
                       correlation_id: str | None = None,
                       serialize: bool = True):
        """Run one effector under the bus. `effector` is a zero-arg coroutine
        factory (thunk) — the caller closes over its own args. Returns the
        effector's result; raises StewardDenied before running anything if the
        ceiling refuses a frontier-origin action."""
        cid = correlation_id or uuid.uuid4().hex
        policy = self._policy()
        decision = steward.decide(policy, actor, operation, target)

        # intent, stamped with origin, BEFORE anything executes (§4)
        self._emit(steward.event(policy, actor, operation, target,
                                 "requested", decision=decision, correlation_id=cid))

        if not decision.allowed:
            self._emit(steward.event(policy, actor, operation, target,
                                     "failed", decision=decision, correlation_id=cid))
            raise StewardDenied(decision,
                                steward.denial_detail(policy, operation, target, decision))

        lock = self._lock(target) if serialize else None
        if lock is not None:
            await lock.acquire()
        try:
            result = await effector()
        except asyncio.CancelledError:
            # a disconnecting client cancels the handler mid-effector; without
            # a terminal event the correlation_id dangles open forever and the
            # ONCE grant question below never resolves — record it, then let
            # the cancellation propagate.
            ev = steward.event(policy, actor, operation, target, "failed",
                               decision=decision, correlation_id=cid)
            ev["error"] = "cancelled"
            self._emit(ev)
            raise
        except Exception as exc:
            ev = steward.event(policy, actor, operation, target, "failed",
                               decision=decision, correlation_id=cid)
            ev["error"] = f"{type(exc).__name__}: {exc}"
            self._emit(ev)
            raise
        finally:
            if lock is not None:
                lock.release()

        # retire a ONCE grant only AFTER the effector succeeded — a failed
        # effector must not burn the operator's single delegation for nothing.
        if decision.consumed is not None and self._on_consume is not None:
            self._on_consume(decision.consumed)

        self._emit(steward.event(policy, actor, operation, target,
                                 "completed", decision=decision, correlation_id=cid))
        return result


# --------------------------------------------------------------------------- #
# self-tests — run `python3 command_bus.py`
# --------------------------------------------------------------------------- #
def _selftest() -> None:
    from steward import Grant, USER, LOCAL, FRONTIER, SESSION, ONCE

    def run(coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def ok(cond, label):
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        assert cond, label

    events: list[dict] = []
    consumed: list = []
    policy = steward.Policy(
        version=7, default="deny",
        grant=(Grant("vm.snapshot", "*", SESSION), Grant("vm.exec", "sandbox", ONCE)),
    )
    bus = CommandBus(lambda: policy, events.append, on_consume=consumed.append)

    async def effector(tag="ran"):
        return tag

    print("user parity — runs, emits requested+completed, no steward verdict:")
    r = run(bus.dispatch(USER, "vm.delete", "sandbox", lambda: effector("del")))
    ph = [e["phase"] for e in events if e["operation"] == "vm.delete"]
    ok(r == "del", "effector result returned")
    ok(ph == ["requested", "completed"], "two events in order")
    ok(all("steward" not in e for e in events if e["operation"] == "vm.delete"), "no verdict on parity path")

    print("frontier denied — raises before running, no completed event:")
    events.clear()
    ran = {"v": False}
    async def spy():
        ran["v"] = True; return "should-not-run"
    denied = False
    try:
        run(bus.dispatch(FRONTIER, "vm.delete", "sandbox", spy))
    except StewardDenied as e:
        denied = True; ok(e.decision.reason.startswith("no grant"), "carries denial reason")
    ok(denied, "StewardDenied raised")
    ok(not ran["v"], "effector never executed")
    ph = [e["phase"] for e in events]
    ok(ph == ["requested", "failed"] and events[0]["steward"] == "denied", "requested(denied)+failed emitted")

    print("frontier granted — runs; ONCE grant handed back for retirement:")
    events.clear()
    r = run(bus.dispatch(FRONTIER, "vm.exec", "sandbox", lambda: effector("exec")))
    ok(r == "exec", "granted effector ran")
    ok(len(consumed) == 1 and consumed[0].scope == ONCE, "ONCE grant marked consumed")

    print("serialization — two ops on one target do not interleave (§6):")
    order: list[str] = []
    async def slow(tag):
        order.append(f"{tag}:start"); await asyncio.sleep(0.02); order.append(f"{tag}:end"); return tag
    async def two():
        await asyncio.gather(
            bus.dispatch(USER, "vm.exec", "sandbox", lambda: slow("A")),
            bus.dispatch(USER, "vm.exec", "sandbox", lambda: slow("B")),
        )
    run(two())
    # whichever wins, one must fully finish before the other starts
    ok(order in (["A:start","A:end","B:start","B:end"], ["B:start","B:end","A:start","A:end"]),
       f"no interleave on same target ({order})")

    print("different targets DO run concurrently:")
    order.clear()
    async def two_t():
        await asyncio.gather(
            bus.dispatch(USER, "vm.exec", "alpha", lambda: slow("A")),
            bus.dispatch(USER, "vm.exec", "beta", lambda: slow("B")),
        )
    run(two_t())
    ok(order[0].endswith(":start") and order[1].endswith(":start"), f"distinct targets overlap ({order})")

    print("\nALL PASS")


if __name__ == "__main__":
    _selftest()
