"""Restricted A adapter — the sandbox bridge A talks to.

Design ref: §4 (A sandbox), §6.2 (data plane), §12.2 (adapter records reads),
§21 (``a_adapter.py``). This client is the *entire* surface A has. It is how the
core invariant "A never bypasses B" is made structural: A holds no reference to
the object store, the ledger, the filesystem, or any other session — only these
brokered operations, each bound to one ``(session, turn, epoch)``.

Every method routes through B, which authorizes, verifies digests, and records
receipts. A cannot enumerate the store; it may create objects and read back what
B authorizes for it (the brokered object-creation API of §6.2).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol


class _Broker(Protocol):
    """Narrow server-side binding the client is allowed to call (Phase-1 in-process)."""
    session_id: str
    turn_id: str
    epoch: str
    manifest_pointer: str

    def resolve(self, pointer: str, selector: str | None, purpose: str) -> bytes: ...
    def create_object(self, data: bytes, media_type: str, kind: str,
                      provenance: dict | None) -> str: ...
    def next_request_id(self) -> str: ...
    def handle_pull(self, request_pointer: str) -> tuple[dict, str]: ...
    def handle_response(self, manifest_pointer: str, idempotency_key: str) -> dict: ...


@dataclass
class PullOutcome:
    decision: str
    result_pointer: str
    payload: dict
    objects: list[dict] = field(default_factory=list)

    @property
    def satisfied(self) -> bool:
        return self.decision in ("exact", "reduced", "summarized", "redacted")


@dataclass
class TurnRender:
    rendered: bool
    already_rendered: bool
    body_sha256: str
    discarded: bool = False
    body: str | None = None


class AAdapterClient:
    def __init__(self, broker: _Broker):
        self._b = broker
        self.session_id = broker.session_id
        self.turn_id = broker.turn_id
        self.epoch = broker.epoch
        self.manifest_pointer = broker.manifest_pointer

    # --- reads (receipts recorded by B, §12.2) -----------------------------
    def resolve(self, pointer: str, selector: str | None = None, purpose: str = "read") -> bytes:
        return self._b.resolve(pointer, selector, purpose)

    def open_manifest(self) -> dict:
        return json.loads(self.resolve(self.manifest_pointer, purpose="manifest"))

    def read_operator_turn(self) -> str:
        """Resolve the required, verbatim operator prompt from the manifest (§11.3)."""
        manifest = self.open_manifest()
        ptr = manifest["operator_turn"]["object"]
        return self.resolve(ptr, purpose="operator_turn").decode("utf-8")

    # --- pulls (§10) --------------------------------------------------------
    def submit_pull(
        self,
        need: str,
        target: dict,
        *,
        preferred_form: str | None = None,
        maximum_tokens: int | None = None,
        reason: str | None = None,
        required_fidelity: str = "reduced",
        allow_summary_fallback: bool = False,
    ) -> PullOutcome:
        request = {
            "schema": "mct.pull-request/1",
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "epoch": self.epoch,
            "request_id": self._b.next_request_id(),
            "need": need,
            "target": target,
            "required_fidelity": required_fidelity,
            "allow_summary_fallback": allow_summary_fallback,
        }
        if preferred_form is not None:
            request["preferred_form"] = preferred_form
        if maximum_tokens is not None:
            request["maximum_tokens"] = maximum_tokens
        if reason is not None:
            request["reason"] = reason

        request_pointer = self._b.create_object(
            json.dumps(request).encode("utf-8"),
            "application/vnd.hugpy.mct-pull-request+json", "pull_request", None,
        )
        payload, result_pointer = self._b.handle_pull(request_pointer)
        return PullOutcome(
            decision=payload["decision"],
            result_pointer=result_pointer,
            payload=payload,
            objects=payload.get("objects", []),
        )

    # --- response (§9 step 9, §16.1) ---------------------------------------
    def respond(
        self,
        body: str,
        *,
        response_format: str = "text/markdown",
        idempotency_key: str | None = None,
        proposed_actions: list[str] | None = None,
    ) -> TurnRender:
        import hashlib
        body_bytes = body.encode("utf-8")
        body_pointer = self._b.create_object(body_bytes, response_format, "response_body", None)
        manifest = {
            "schema": "mct.response/1",
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "epoch": self.epoch,
            "body": body_pointer,
            "format": response_format,
            "final": True,
            "proposed_actions": proposed_actions or [],
            "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
        }
        manifest_pointer = self._b.create_object(
            json.dumps(manifest).encode("utf-8"),
            "application/vnd.hugpy.mct-response+json", "response_manifest", None,
        )
        key = idempotency_key or f"{self.session_id}:{self.turn_id}:response:1"
        result = self._b.handle_response(manifest_pointer, key)
        return TurnRender(**result)

    def respond_stream(self, chunks: list[str], *, response_format: str = "text/markdown",
                       idempotency_key: str | None = None) -> TurnRender:
        """Stream a response as ordered frames, then seal it (design §16.2).

        Each frame is a committed, digest-verified object; B renders complete
        frames in order and verifies the assembled body against the sealed
        digest before committing. Response text never rides the control plane."""
        import hashlib
        for i, chunk in enumerate(chunks):
            ptr = self._b.create_object(chunk.encode("utf-8"), response_format,
                                        "response_stream_chunk", {"seq": i})
            self._b.handle_stream_frame(ptr, i)  # validated + rendered in order

        full = "".join(chunks)
        body_bytes = full.encode("utf-8")
        body_ptr = self._b.create_object(body_bytes, response_format, "response_body", None)
        manifest = {
            "schema": "mct.response/1", "session_id": self.session_id,
            "turn_id": self.turn_id, "epoch": self.epoch, "body": body_ptr,
            "format": response_format, "final": True, "proposed_actions": [],
            "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
        }
        manifest_ptr = self._b.create_object(
            json.dumps(manifest).encode("utf-8"),
            "application/vnd.hugpy.mct-response+json", "response_manifest", None)
        key = idempotency_key or f"{self.session_id}:{self.turn_id}:response:1"
        return TurnRender(**self._b.handle_stream_seal(manifest_ptr, key))
