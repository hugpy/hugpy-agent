"""Scripted fake A programs for the Phase-1 acceptance harness.

Design ref: §22 Phase 1 exit condition — *a scripted fake A completes, pulls,
retries, crashes, resumes, and never bypasses B.* These programs are the "A"
side; each is a plain ``callable(client)`` where ``client`` is the restricted
:class:`~hugpy_agent.mct.a_adapter.AAdapterClient`. They only ever touch the
brokered surface, which is the point.
"""
from __future__ import annotations

from typing import Callable

from .a_adapter import AAdapterClient

AProgram = Callable[[AAdapterClient], None]


def answer(text: str) -> AProgram:
    """A reads the verbatim operator turn, then answers. No pulls."""
    def program(client: AAdapterClient) -> None:
        operator = client.read_operator_turn()
        client.respond(f"{text}\n\n(operator asked: {operator})")
    return program


def answer_after_pull(need: str, query: str, preferred_form: str | None,
                      render_prefix: str = "Traced") -> AProgram:
    """A opens context, discovers it is insufficient, pulls, then cites the excerpt."""
    def program(client: AAdapterClient) -> None:
        client.open_manifest()
        client.read_operator_turn()
        outcome = client.submit_pull(
            need=need,
            target={"kind": "catalog-query", "query": query},
            preferred_form=preferred_form,
            required_fidelity="verbatim-source",
            reason="initial context lacked the causal event",
        )
        if not outcome.satisfied:
            client.respond(f"Could not complete: pull {outcome.decision}.")
            return
        excerpt = client.resolve(outcome.objects[0]["object"]).decode("utf-8")
        client.respond(f"{render_prefix} using {outcome.decision} evidence:\n{excerpt}")
    return program


def stream_answer(chunks: list[str]) -> AProgram:
    """A streams its answer as ordered frames, then seals it (§16.2)."""
    def program(client: AAdapterClient) -> None:
        client.read_operator_turn()
        program.render = client.respond_stream(chunks)  # type: ignore[attr-defined]
    return program


def double_respond(text: str) -> AProgram:
    """A sends the same response twice (retry / duplicate ``response.ready``).

    The second call must be idempotent — no second render (invariant 14)."""
    def program(client: AAdapterClient) -> None:
        client.read_operator_turn()
        first = client.respond(text)
        second = client.respond(text)  # same idempotency key
        # stash the two render outcomes for the harness to assert on
        program.renders = (first, second)  # type: ignore[attr-defined]
    return program


def silent() -> AProgram:
    """A returns without responding. B must NOT answer in its place (invariant 10)."""
    def program(client: AAdapterClient) -> None:
        client.read_operator_turn()
    return program


def try_bypass() -> AProgram:
    """A attempts to escape the broker. Every attempt must fail closed.

    Stashes the caught error codes on ``program.violations`` for the harness."""
    def program(client: AAdapterClient) -> None:
        from .errors import MctError
        from .protocol import make_pointer
        violations = []

        # 1. resolve a host path as if it were a pointer (invariant 5)
        try:
            client.resolve("/etc/shadow")
        except MctError as e:
            violations.append(e.code)

        # 2. resolve a pointer from another session (adversarial case 5)
        try:
            client.resolve(make_pointer("s_OTHERSESSION", "o_STOLEN"))
        except MctError as e:
            violations.append(e.code)

        # 3. pull an object outside the session (invariant 12)
        out = client.submit_pull(
            need="read another session's object",
            target={"kind": "object", "object": make_pointer("s_OTHERSESSION", "o_STOLEN")},
        )
        violations.append(out.decision)  # expected: "denied"

        program.violations = violations  # type: ignore[attr-defined]
        client.respond("bypass attempts complete")
    return program
