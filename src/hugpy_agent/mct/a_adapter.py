"""Restricted A adapter — the sandbox bridge A talks to.

Design ref: §4 (A sandbox), §6.2 (data plane), §12.2 (adapter records reads),
§21 (``a_adapter.py``). This client is the *entire* surface A has. It is how the
core invariant "A never bypasses B" is made structural: A holds no reference to
the object store, the ledger, the filesystem, or any other session — only these
brokered operations, each bound to one ``(session, turn, epoch)``.

Every method routes through B, which authorizes, verifies digests, and records
receipts. A cannot enumerate the store; it may create objects and read back what
B authorizes for it (the brokered object-creation API of §6.2).

THE DISCLOSURE GATE (k113; IDEA_PHASE/POLICY-rights-consent-disclosure.md §3).
This client is the last point on B's side before bytes enter A's context, so
it is where ``ArtifactManifest.frontier_may_access`` / ``disclosure`` are
ENFORCED, not merely recorded: every ``resolve`` and every object a pull hands
back is checked against the artifact's manifest (``manifest.disclosure_
verdict``). A refused artifact never reaches A — a read raises the typed
``DisclosureRefused``, a pull listing entry is replaced by a redacted stub —
and each refusal is a typed ``DisclosureRefusal`` record, kept on the client
(``_refusals``, B-side) and handed to the broker's ``record_disclosure_refusal`` when
it has one, so the ledger sees it. Objects with NO manifest (the turn manifest,
pull requests, A's own response objects) are not artifacts under k96 and pass
through to the existing pull-broker / fs-policy controls.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from .manifest import ArtifactManifest, DisclosureVerdict, disclosure_verdict

#: ``(pointer) -> ArtifactManifest | None`` — how the gate finds the manifest
#: that governs an object. Supplied by B (``ManifestStore.for_content`` over the
#: pointer's content digest); the client never enumerates anything itself.
ManifestLookup = Callable[[str], "ArtifactManifest | None"]

REFUSAL_SCHEMA = "mct.disclosure-refusal/1"


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


@dataclass(frozen=True)
class DisclosureRefusal:
    """Typed record of one artifact B declined to hand A. Names the object and
    the verdict; never the content."""
    pointer: str
    purpose: str
    verdict: DisclosureVerdict
    session_id: str
    turn_id: str
    epoch: str
    selector: str | None = None
    at: str = ""
    schema: str = REFUSAL_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "session_id": self.session_id,
            "turn_id": self.turn_id, "epoch": self.epoch,
            "pointer": self.pointer, "purpose": self.purpose,
            "selector": self.selector, "at": self.at,
            "outcome": "refused", "verdict": self.verdict.to_dict(),
        }

    def redacted_stub(self) -> dict[str, Any]:
        """What replaces the object in a pull listing A sees: that something
        was withheld and why, nothing about what it was."""
        return {"redacted": True, "outcome": "refused",
                "reason": self.verdict.reason,
                "artifact_type": self.verdict.artifact_type}


class DisclosureRefused(PermissionError):
    """Raised to A for a read of an artifact B does not disclose. Carries the
    typed record so the caller (and the ledger) have the same evidence."""

    def __init__(self, record: DisclosureRefusal):
        super().__init__(f"B refused disclosure of {record.pointer}: "
                         f"{record.verdict.reason}")
        self.record = record


@dataclass
class PullOutcome:
    decision: str
    result_pointer: str
    payload: dict
    objects: list[dict] = field(default_factory=list)
    refused: list[DisclosureRefusal] = field(default_factory=list)

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
    def __init__(self, broker: _Broker, manifest_lookup: ManifestLookup | None = None):
        self._b = broker
        self.session_id = broker.session_id
        self.turn_id = broker.turn_id
        self.epoch = broker.epoch
        self.manifest_pointer = broker.manifest_pointer
        # The gate's manifest source: an explicit lookup, else whatever the
        # binding offers, else None (no manifest plane -> nothing to consult).
        self._manifests: ManifestLookup | None = (
            manifest_lookup or getattr(broker, "artifact_manifest_for", None))
        self._refusals: list[DisclosureRefusal] = []  # B-side audit; not part of A's surface

    # --- the disclosure gate (policy §3) ------------------------------------
    def _manifest_for(self, pointer: str) -> ArtifactManifest | None:
        if self._manifests is None:
            return None
        return self._manifests(pointer)

    def _refuse(self, pointer: str, purpose: str, verdict: DisclosureVerdict,
                selector: str | None) -> DisclosureRefusal:
        record = DisclosureRefusal(
            pointer=pointer, purpose=purpose, verdict=verdict, selector=selector,
            session_id=self.session_id, turn_id=self.turn_id, epoch=self.epoch,
            at=datetime.now(timezone.utc).isoformat())
        self._refusals.append(record)
        sink = getattr(self._b, "record_disclosure_refusal", None)
        if callable(sink):
            sink(record.to_dict())
        return record

    def _gate(self, pointer: str, selector: str | None, purpose: str
              ) -> DisclosureVerdict | None:
        """The verdict for one object, or None when no manifest governs it.
        Raises ``DisclosureRefused`` (after recording) on a refusal."""
        manifest = self._manifest_for(pointer)
        if manifest is None:
            return None
        verdict = disclosure_verdict(manifest, selector=selector)
        if not verdict.allowed:
            raise DisclosureRefused(self._refuse(pointer, purpose, verdict, selector))
        return verdict

    def _redact_objects(self, objects: list[dict]) -> tuple[list[dict], list[DisclosureRefusal]]:
        """Pull listings: every entry whose manifest refuses disclosure is
        replaced by a redacted stub, so A learns neither pointer nor digest."""
        kept: list[dict] = []
        refused: list[DisclosureRefusal] = []
        for obj in objects:
            pointer = obj.get("object") if isinstance(obj, dict) else None
            manifest = self._manifest_for(pointer) if pointer else None
            if manifest is not None:
                verdict = disclosure_verdict(manifest, selector=obj.get("selector"))
                if not verdict.allowed:
                    record = self._refuse(pointer, "pull", verdict, obj.get("selector"))
                    refused.append(record)
                    kept.append(record.redacted_stub())
                    continue
            kept.append(obj)
        return kept, refused

    # --- reads (receipts recorded by B, §12.2) -----------------------------
    def resolve(self, pointer: str, selector: str | None = None, purpose: str = "read") -> bytes:
        verdict = self._gate(pointer, selector, purpose)
        data = self._b.resolve(pointer, selector, purpose)
        if verdict is not None and verdict.limit is not None and len(data) > verdict.limit:
            data = data[:verdict.limit]      # excerpt_only: bounded, policy §3.4
        return data

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
        objects, refused = self._redact_objects(list(payload.get("objects") or []))
        if refused:
            payload = dict(payload)
            payload["objects"] = objects
            payload["disclosure_refusals"] = [r.to_dict() for r in refused]
        return PullOutcome(
            decision=payload["decision"],
            result_pointer=result_pointer,
            payload=payload,
            objects=objects,
            refused=refused,
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
