"""MctSessionLoop — B's orchestration of a mediated turn.

Design ref: §9 (normal turn flow), §15 (turn state machine), §20.5 (a sibling to
``AgentLoop``, not a rewrite), §21 (``a_adapter``/broker glue). This is the B side
of the boundary; :mod:`a_adapter` is the A side.

Phase 1 is in-process and LLM-free: the "A" passed to :meth:`MctSession.submit` is
any callable ``a_program(client)`` (e.g. :mod:`hugpy_agent.mct.fake_a`). Every read,
pull, and response it issues is brokered and recorded here.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import ids
from .a_adapter import AAdapterClient
from .cache_epochs import EpochManager, seal_receipt_bytes
from .capabilities import Capabilities
from .compaction import Compaction
from .context_builder import ContextBuilder
from .errors import IntegrityError, ProtocolError, StateError
from .ledger import Ledger
from .objects import ObjectStore
from .protocol import Envelope, encode, make_pointer, parse_pointer
from .pull_broker import PullBroker, PullBudget, TurnPullState
from .renderer import Renderer
from .response import ResponseValidator
from .retrieval import Retrieval
from .telemetry import Telemetry


@dataclass
class BrokerConfig:
    # Decision §26 default: B may NOT answer without A (invariant 10, registry row 16).
    allow_b_only_answer: bool = False
    pull_budget: PullBudget = field(default_factory=PullBudget)
    max_input_tokens: int = 24000
    reserved_output_tokens: int = 8000
    # Phase 3: local model for ranking/summaries/extraction. None keeps the
    # engine fully deterministic (Phase 2 behavior). It never authorizes anything.
    use_model: bool = False
    # Phase 6: per-session disk quota on the object store (0 = unlimited).
    session_quota_bytes: int = 0
    # Rolling append-only log (<workspace>/.hugpy_agent/mct/mct.log). On by default.
    event_log: bool = True


@dataclass
class TurnResult:
    turn_id: str
    epoch: str
    state: str
    rendered: bool
    already_rendered: bool
    body: str | None
    response_manifest: str | None
    receipt: str | None
    context_trace: dict | None = None  # §18.2 "why included/omitted" explanation
    error: str | None = None           # why a turn failed (A unavailable / no answer)
    tokens: dict | None = None         # precise token/cost summary for this A turn


class BrokerServer:
    """Owns durable state and the deterministic enforcement points (registry)."""

    def __init__(self, workspace_root: str | Path, *, sink: Callable[[str], None] = print,
                 config: BrokerConfig | None = None, local_model=None):
        self.config = config or BrokerConfig()
        self.workspace_root = Path(workspace_root)
        root = Path(workspace_root) / ".hugpy_agent" / "mct"
        self.root = root
        self.ledger = Ledger(root / "mct.db", event_log=self.config.event_log)
        self.store = ObjectStore(root, self.ledger,
                                 session_quota_bytes=self.config.session_quota_bytes)
        self.epochs = EpochManager(self.ledger)
        self.caps = Capabilities()
        # Local model is advisory only (invariant 9). Explicit arg wins; else the
        # config flag opts into the deterministic offline model.
        self.model = local_model
        if self.model is None and self.config.use_model:
            from .local_model import DeterministicLocalModel
            self.model = DeterministicLocalModel()
        self.retrieval = Retrieval(self.ledger, self.store, model=self.model)
        self.compaction = Compaction(self.store, self.ledger, model=self.model)
        self.context_builder = ContextBuilder(self.store, self.ledger, self.retrieval,
                                              model=self.model)
        self.pull_broker = PullBroker(self.store, self.ledger, self.caps, self.config.pull_budget)
        self.validator = ResponseValidator(self.store, self.ledger)
        self.renderer = Renderer(self.ledger, sink)
        self.telemetry = Telemetry(self)
        from .a_cache import AWorkingSet
        from .logs import Logs
        from .tokens import TokenUsage
        from .metrics import Metrics
        self.a_cache = AWorkingSet(self)  # durable mirror of everything A received
        self.logs = Logs(self)            # full C / B / A logs from durable state
        self.tokens = TokenUsage(self)    # precise per-turn/session token accounting
        self.metrics = Metrics(self)      # comprehensive session metrics dashboard

    def open_session(self, workspace: str = "") -> str:
        session_id, _ = self.ledger.create_session(workspace)
        return session_id

    def session(self, session_id: str) -> "MctSession":
        return MctSession(self, session_id)

    def close(self) -> None:
        self.ledger.close()


class MctSession:
    def __init__(self, server: BrokerServer, session_id: str):
        self.server = server
        self.session_id = session_id
        self._catalog: dict[str, str] = {}  # name -> pointer (§11.6)
        self._roots: dict[str, object] = {}  # name -> ConfinedRoot (session capability grant)
        self._file_sources: dict[str, tuple[str, str]] = {}  # catalog name -> (root, relpath)
        self._policy_pointer: str | None = None
        self._last_trace = None

    # --- filesystem source grants (design §13.1 Grant_session, §13.4) -------
    def register_root(self, name: str, path: str, *, allow_symlinks: bool = False):
        """Grant this session confined read access to a directory root."""
        from .confined_io import ConfinedRoot
        self._roots[name] = ConfinedRoot(path, allow_symlinks=allow_symlinks)
        return self._roots[name]

    def register_source_file(self, catalog_name: str, root_name: str, relpath: str) -> None:
        """Expose a file (under a granted root) as a catalog entry. A pulls it by
        name via catalog-query — it never sees the host path (invariant 5)."""
        if root_name not in self._roots:
            raise StateError(f"unknown root: {root_name}")
        self._file_sources[catalog_name] = (root_name, relpath)

    def _materialize_file_sources(self) -> None:
        """Snapshot each registered file into the immutable store (once, cached).
        Confined read + snapshot happen here (§13.4); failures drop the entry so a
        denied/oversized/missing source simply becomes not_found for A."""
        for name, (root_name, relpath) in self._file_sources.items():
            if name in self._catalog:
                continue
            root = self._roots.get(root_name)
            if root is None:
                continue
            try:
                data = root.read(relpath)
            except Exception:
                continue
            ref = self.server.store.commit(
                self.session_id, data, media_type="text/plain", kind="source_snapshot",
                provenance={"root": root_name, "relpath": relpath, "catalog_name": name})
            self._catalog[name] = ref.pointer

    def invalidate_source_cache(self, name: str | None = None) -> None:
        """Drop cached file-source snapshots so the next build re-snapshots the
        current bytes — a fresh working set after an epoch/source change (§12.3)."""
        for n in ([name] if name else list(self._file_sources)):
            self._catalog.pop(n, None)

    def rebuild_catalog_from_store(self) -> None:
        """Reconstruct name -> pointer from durable object provenance (§17.1).

        Used by the out-of-process A adapter (MCP child), which rebuilds session
        state from the object store rather than sharing the parent's memory."""
        for meta in self.server.ledger.list_objects(self.session_id, newest_first=False):
            prov = json.loads(meta.get("provenance") or "{}")
            name = prov.get("catalog_name")
            if name:
                self._catalog[name] = make_pointer(self.session_id, meta["object_id"])

    def set_policy(self, text: str) -> str:
        """Register the session's governing instruction (L0, always required, §11.1)."""
        ref = self.server.store.commit(self.session_id, text.encode("utf-8"),
                                       media_type="text/plain", kind="policy_snapshot",
                                       provenance={"role": "governing_instruction"})
        self._policy_pointer = ref.pointer
        return ref.pointer

    # --- source registration (stands in for confined snapshotting, §13.4) ---
    def register_source(self, name: str, data: bytes | str, *, kind: str = "source_snapshot",
                        media_type: str = "text/plain") -> str:
        if isinstance(data, str):
            data = data.encode("utf-8")
        ref = self.server.store.commit(self.session_id, data, media_type=media_type, kind=kind,
                                       provenance={"catalog_name": name})
        self._catalog[name] = ref.pointer
        return ref.pointer

    # --- the mediated turn (design §9) --------------------------------------
    def _prepare_turn(self, raw_message: str, fragments):
        """§9 steps 1-6: ingest, store the exact prompt, build context, send the
        manifest pointer. Shared by the scripted and Claude-Code A drivers."""
        srv, ledger = self.server, self.server.ledger
        epoch = srv.epochs.active(self.session_id)
        turn_id = ledger.next_turn_id(self.session_id)

        ledger.set_turn_state(self.session_id, turn_id, "Ingested", epoch)
        ledger.append_event(self.session_id, turn_id, epoch, "turn.ingested", "B.gateway")

        op_ref = srv.store.commit(self.session_id, raw_message.encode("utf-8"),
                                  media_type="text/plain", kind="operator_turn",
                                  provenance={"role": "operator_turn"})

        manifest_pointer, manifest_sha, trace = self._build_manifest(
            turn_id, epoch, op_ref, raw_message, fragments)
        ledger.set_turn_state(self.session_id, turn_id, "ContextBuilt", epoch)
        ledger.append_event(self.session_id, turn_id, epoch, "context.built", "B.context-engine",
                            input_objects=[op_ref.object_id], output_objects=[_oid(manifest_pointer)])

        ready = Envelope(type="context.ready", session_id=self.session_id, turn_id=turn_id,
                         sequence=ledger.next_sequence(self.session_id), epoch=epoch,
                         object=manifest_pointer, sha256=manifest_sha,
                         media_type="application/vnd.hugpy.mct-context+json")
        encode(ready)  # validate + frame the envelope (would be written to the socket)
        ledger.set_turn_state(self.session_id, turn_id, "SentToA", epoch)
        return turn_id, epoch, op_ref, manifest_pointer, manifest_sha, trace

    def _record_metric(self, turn_id, op_ref, trace, rendered, t0) -> None:
        pulls = self.server.ledger._db.execute(
            "SELECT COUNT(*) n FROM pulls WHERE session_id=? AND turn_id=?",
            (self.session_id, turn_id)).fetchone()["n"]
        ex = trace.explain()
        self.server.ledger.record_metric(
            self.session_id, turn_id, op_ref.size,
            ex["required_tokens"] + ex["selected_tokens"], pulls, 0, rendered,
            round((time.perf_counter() - t0) * 1000, 2))

    def submit(self, raw_message: str, a_program: Callable[[AAdapterClient], None],
               *, fragments: list[dict] | None = None) -> TurnResult:
        srv, ledger = self.server, self.server.ledger
        t0 = time.perf_counter()
        turn_id, epoch, op_ref, manifest_pointer, manifest_sha, trace = \
            self._prepare_turn(raw_message, fragments)

        binding = _ABinding(self, turn_id, epoch, manifest_pointer, manifest_sha)
        client = AAdapterClient(binding)
        ledger.set_turn_state(self.session_id, turn_id, "Reasoning", epoch)

        a_program(client)

        # §9 step 11 + invariant 10: if A produced no valid, renderable response,
        # B does NOT answer in its place.
        if not binding.responded or binding.discarded:
            reason = "a.no_response" if not binding.responded else "response.rejected"
            ledger.append_event(self.session_id, turn_id, epoch, reason, "B.session")
            state = "Cancelled" if binding.cancelled else "Failed"
            ledger.set_turn_state(self.session_id, turn_id, state, epoch)
            return TurnResult(turn_id, epoch, state, False, False, None,
                              binding.response_manifest, None, trace.explain())

        # Seal the adapter receipt (§12.2) and commit final turn state (§9 step 11).
        receipts = ledger.receipts_for_turn(self.session_id, turn_id)
        receipt_ref = srv.store.commit(self.session_id, seal_receipt_bytes(receipts),
                                       media_type="application/vnd.hugpy.mct-receipt+json",
                                       kind="context_receipt",
                                       provenance={"turn_id": turn_id})
        ledger.set_turn_state(self.session_id, turn_id, "Committed", epoch)
        ledger.append_event(self.session_id, turn_id, epoch, "turn.committed", "B.session",
                            output_objects=[_oid(binding.response_manifest), receipt_ref.object_id])

        # §11.4: deterministically derive durable memory for future turns.
        self._extract_memory(op_ref.object_id, binding.response_manifest)
        self._record_metric(turn_id, op_ref, trace, binding.render["rendered"], t0)

        return TurnResult(turn_id, epoch, "Committed", binding.render["rendered"],
                          binding.render["already_rendered"], binding.body,
                          binding.response_manifest, receipt_ref.pointer, trace.explain())

    def submit_via_claude(self, raw_message: str, *, model: str = "sonnet",
                          timeout: int = 240, fragments: list[dict] | None = None) -> TurnResult:
        """Run a real turn with Claude Code as A (design §22 Phase 4).

        A is a headless ``claude`` process confined by ``--strict-mcp-config`` to
        B's ``resolve``/``submit_pull``/``respond`` MCP tools — no ambient reach
        (invariant 1). If A is unavailable or returns nothing, B does NOT answer
        in its place (invariant 10)."""
        from .claude_adapter import ClaudeCodeAdapter
        srv, ledger = self.server, self.server.ledger
        t0 = time.perf_counter()
        turn_id, epoch, op_ref, manifest_pointer, manifest_sha, trace = \
            self._prepare_turn(raw_message, fragments)
        ledger.set_turn_state(self.session_id, turn_id, "Reasoning", epoch)

        outcome = ClaudeCodeAdapter(self.server).run_turn(
            self, turn_id, epoch, manifest_pointer, model=model, timeout=timeout)

        if not outcome.get("response_manifest"):
            # A unavailable / silent -> explicit failure, no B substitution (§5.2, §17).
            reason = outcome.get("error") or "A produced no renderable answer"
            ledger.append_event(self.session_id, turn_id, epoch,
                                "a.unavailable", "B.a-adapter", policy_revision=None)
            ledger.set_turn_state(self.session_id, turn_id, "Failed", epoch)
            return TurnResult(turn_id, epoch, "Failed", False, False, None, None, None,
                              trace.explain(), error=reason, tokens=outcome.get("tokens"))

        key = f"{self.session_id}:{turn_id}:response:1"
        res = self.on_response_ready(turn_id, epoch, outcome["response_manifest"], key)
        self._extract_memory(op_ref.object_id, outcome["response_manifest"])
        self._record_metric(turn_id, op_ref, trace, res["rendered"], t0)
        turn = ledger.get_turn(self.session_id, turn_id)
        return TurnResult(turn_id, epoch, turn["state"], res["rendered"],
                          res["already_rendered"], res.get("body"),
                          outcome["response_manifest"], None, trace.explain(),
                          tokens=outcome.get("tokens"))

    def on_response_ready(self, turn_id: str, epoch: str, manifest_pointer: str,
                          idempotency_key: str) -> dict:
        """Accept a (possibly resent) ``response.ready`` for an in-flight turn.

        This is the resume path (§17): after a B restart, the adapter resends the
        same response pointer. Idempotency + the render ledger guarantee it renders
        at most once total (invariant 14). Commits the turn if it was interrupted
        before its final state was written.
        """
        binding = _ABinding(self, turn_id, epoch, manifest_pointer, "")
        result = binding.handle_response(manifest_pointer, idempotency_key)
        turn = self.server.ledger.get_turn(self.session_id, turn_id)
        if (not result.get("discarded") and binding.responded
                and turn and turn["state"] not in ("Committed", "Cancelled", "Failed")):
            receipts = self.server.ledger.receipts_for_turn(self.session_id, turn_id)
            receipt_ref = self.server.store.commit(
                self.session_id, seal_receipt_bytes(receipts),
                media_type="application/vnd.hugpy.mct-receipt+json",
                kind="context_receipt", provenance={"turn_id": turn_id})
            self.server.ledger.set_turn_state(self.session_id, turn_id, "Committed", epoch)
            self.server.ledger.append_event(self.session_id, turn_id, epoch, "turn.committed",
                                            "B.recovery", output_objects=[receipt_ref.object_id])
        return result

    def cancel(self, turn_id: str) -> None:
        """Mark the active turn cancelled; later response pointers must not render (§5.3)."""
        epoch = self.server.epochs.active(self.session_id)
        self.server.ledger.set_turn_state(self.session_id, turn_id, "Cancelled", epoch)
        self.server.ledger.append_event(self.session_id, turn_id, epoch, "turn.cancelled", "B.session")

    def new_epoch(self, reason: str) -> str:
        return self.server.epochs.change(self.session_id, reason)

    # --- manifest construction (deterministic context engine, §11) ----------
    def _build_manifest(self, turn_id, epoch, op_ref, raw_message, fragments):
        self._materialize_file_sources()  # so file sources appear in the catalog (§11.6)
        required_specs = []
        if self._policy_pointer:
            required_specs.append({"object": self._policy_pointer,
                                   "role": "governing_instruction", "priority": 100})
        for spec in fragments or []:
            required_specs.append(self._materialize_required(spec))

        budget = {
            "maximum_input_tokens": self.server.config.max_input_tokens,
            "reserved_output_tokens": self.server.config.reserved_output_tokens,
            "pull_tokens_remaining": self.server.config.pull_budget.max_tokens,
        }
        pointer, sha, trace = self.server.context_builder.build(
            self.session_id, turn_id, epoch, op_ref, raw_message,
            budget=budget, required_specs=required_specs, catalog=self._catalog)
        self._last_trace = trace
        return pointer, sha, trace

    def _materialize_required(self, spec: dict) -> dict:
        """Turn an explicit fragment spec into a required manifest fragment."""
        if "object" in spec:
            out = {"object": spec["object"], "role": spec.get("role", "decision_memory"),
                   "priority": int(spec.get("priority", 60))}
        else:
            data = spec["data"]
            if isinstance(data, str):
                data = data.encode("utf-8")
            ref = self.server.store.commit(self.session_id, data, media_type="text/plain",
                                           kind="summary", provenance={"role": spec.get("role", "")})
            out = {"object": ref.pointer, "role": spec.get("role", "decision_memory"),
                   "priority": int(spec.get("priority", 60))}
        if spec.get("source_objects"):
            out["source_objects"] = spec["source_objects"]
        return out

    def _extract_memory(self, operator_object_id: str, response_manifest: str | None) -> None:
        srv = self.server
        try:
            # Deterministic regex extraction is always the baseline (Phase 2).
            regex_facts = srv.compaction.extract(self.session_id, operator_object_id)
            # Model extraction COMPLEMENTS regex — it only runs when regex found
            # nothing, so the two never create near-duplicate facts (Phase 3).
            if srv.model is not None and not regex_facts:
                srv.compaction.extract_semantic(self.session_id, operator_object_id)
            if response_manifest:
                rmanifest = json.loads(srv.store.resolve(self.session_id, response_manifest))
                srv.compaction.extract(self.session_id, parse_pointer(rmanifest["body"])[1])
        except Exception:
            pass  # memory extraction is best-effort; never blocks turn commit


class _ABinding:
    """Server-side narrow binding handed to :class:`AAdapterClient` (one turn/epoch)."""

    def __init__(self, session: MctSession, turn_id: str, epoch: str,
                 manifest_pointer: str, manifest_sha: str):
        self._s = session
        self.session_id = session.session_id
        self.turn_id = turn_id
        self.epoch = epoch
        self.manifest_pointer = manifest_pointer
        self._manifest_sha = manifest_sha
        self._pull_state = TurnPullState()
        # turn outcome, read back by MctSession.submit after a_program returns
        self.responded = False
        self.cancelled = False
        self.discarded = False
        self._stream_next = 0
        self._stream_buf = ""
        self.body: str | None = None
        self.response_manifest: str | None = None
        self.render = {"rendered": False, "already_rendered": False, "body_sha256": ""}

    # --- brokered operations the client may call ---------------------------
    def resolve(self, pointer: str, selector: str | None, purpose: str) -> bytes:
        srv = self._s.server
        data = srv.store.resolve(self.session_id, pointer, selector=selector)
        _, object_id = parse_pointer(pointer)
        meta = srv.ledger.get_object(object_id)
        srv.ledger.record_receipt(self.session_id, self.turn_id, self.epoch, object_id,
                                  meta["digest"], selector, True, purpose, self._manifest_sha)
        return data

    def create_object(self, data: bytes, media_type: str, kind: str, provenance: dict | None) -> str:
        ref = self._s.server.store.commit(self.session_id, data, media_type=media_type,
                                          kind=kind, provenance=provenance or {})
        return ref.pointer

    def next_request_id(self) -> str:
        return self._s.server.ledger.next_request_id(self.session_id, self.turn_id)

    def handle_pull(self, request_pointer: str) -> tuple[dict, str]:
        srv = self._s.server
        self._s._materialize_file_sources()  # confined snapshot of any registered files
        request = json.loads(srv.store.resolve(self.session_id, request_pointer))
        # confirm session/turn/epoch binding (§10.1 step 2)
        if (request.get("session_id") != self.session_id
                or request.get("turn_id") != self.turn_id
                or request.get("epoch") != self.epoch):
            raise StateError("pull request is not bound to the active turn/epoch")
        _, req_oid = parse_pointer(request_pointer)
        srv.ledger.append_event(self.session_id, self.turn_id, self.epoch, "pull.requested",
                                "A.adapter", input_objects=[req_oid])
        payload, result_pointer = srv.pull_broker.arbitrate(
            self.session_id, self.turn_id, self.epoch, request, self._s._catalog, self._pull_state)
        srv.ledger.append_event(self.session_id, self.turn_id, self.epoch,
                                f"pull.{payload['decision']}", "B.pull-broker",
                                input_objects=[req_oid], output_objects=[_oid(result_pointer)],
                                policy_revision=payload.get("policy_revision"))
        return payload, result_pointer

    # --- streaming (design §16.2) ------------------------------------------
    def handle_stream_frame(self, chunk_pointer: str, seq: int) -> None:
        srv = self._s.server
        if seq != self._stream_next:  # ordered frames only (§16.2 step 3)
            raise StateError(f"stream frame out of order: expected {self._stream_next}, got {seq}")
        text = srv.store.resolve(self.session_id, chunk_pointer).decode("utf-8")  # digest-verified
        self._stream_buf += text
        self._stream_next += 1
        srv.renderer.render_frame(self.session_id, self.turn_id, text)
        _, oid = parse_pointer(chunk_pointer)
        srv.ledger.append_event(self.session_id, self.turn_id, self.epoch,
                                "response.frame", "A.adapter", output_objects=[oid])

    def handle_stream_seal(self, manifest_pointer: str, idempotency_key: str) -> dict:
        srv = self._s.server
        self.responded = True
        self.response_manifest = manifest_pointer
        prior = srv.ledger.idempotency_get(idempotency_key)
        if prior is not None:
            self.body = prior.get("body")
            self.render = {"rendered": False, "already_rendered": True,
                           "body_sha256": prior["body_sha256"]}
            return {**self.render, "discarded": False, "body": self.body}

        turn = srv.ledger.get_turn(self.session_id, self.turn_id)
        cancelled = turn and turn["state"] == "Cancelled"
        try:
            body, _m = srv.validator.validate(self.session_id, self.turn_id, self.epoch,
                                              manifest_pointer, cancelled=bool(cancelled))
        except (StateError, ProtocolError, IntegrityError):
            self.discarded = True
            srv.ledger.append_event(self.session_id, self.turn_id, self.epoch,
                                    "response.rejected", "B.renderer")
            self.render = {"rendered": False, "already_rendered": False, "body_sha256": ""}
            return {**self.render, "discarded": True, "body": None}

        # Verify the streamed frames reassemble to the sealed body (§16.2 step 5).
        if self._stream_buf != body:
            self.discarded = True
            srv.ledger.append_event(self.session_id, self.turn_id, self.epoch,
                                    "response.rejected", "B.renderer")
            self.render = {"rendered": False, "already_rendered": False, "body_sha256": ""}
            return {**self.render, "discarded": True, "body": None}

        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        result = srv.renderer.seal_stream(self.session_id, self.turn_id, digest)
        self.body = body
        self.render = {"rendered": result.rendered, "already_rendered": result.already_rendered,
                       "body_sha256": result.body_sha256}
        srv.ledger.idempotency_put(idempotency_key, self.session_id,
                                   {"body_sha256": digest, "body": body})
        srv.ledger.append_event(self.session_id, self.turn_id, self.epoch, "response.sealed",
                                "B.renderer", output_objects=[_oid(manifest_pointer)])
        return {**self.render, "discarded": False, "body": self.body}

    def handle_response(self, manifest_pointer: str, idempotency_key: str) -> dict:
        srv = self._s.server
        self.responded = True
        self.response_manifest = manifest_pointer

        # Idempotent replay: a resent response.ready returns the original outcome
        # and never re-renders (§15.2, invariant 14).
        prior = srv.ledger.idempotency_get(idempotency_key)
        if prior is not None:
            srv.ledger.append_event(self.session_id, self.turn_id, self.epoch,
                                    "response.replayed", "B.renderer")
            self.body = prior.get("body")
            self.render = {"rendered": False, "already_rendered": True,
                           "body_sha256": prior["body_sha256"]}
            return {**self.render, "discarded": False, "body": self.body}

        turn = srv.ledger.get_turn(self.session_id, self.turn_id)
        cancelled = turn and turn["state"] == "Cancelled"
        try:
            body, _manifest = srv.validator.validate(
                self.session_id, self.turn_id, self.epoch, manifest_pointer, cancelled=bool(cancelled))
        except (StateError, ProtocolError, IntegrityError):
            # Stale/cancelled/malformed response: stop rendering, show nothing,
            # never guess (§16.3, §5.3, adversarial cases 8 & 9). B does not invent.
            self.discarded = True
            self.cancelled = bool(cancelled)
            srv.ledger.append_event(self.session_id, self.turn_id, self.epoch,
                                    "response.rejected", "B.renderer")
            self.render = {"rendered": False, "already_rendered": False, "body_sha256": ""}
            return {**self.render, "discarded": True}

        result = srv.renderer.render(self.session_id, self.turn_id, body)
        self.body = body
        self.render = {"rendered": result.rendered, "already_rendered": result.already_rendered,
                       "body_sha256": result.body_sha256}
        srv.ledger.idempotency_put(idempotency_key, self.session_id,
                                   {"body_sha256": result.body_sha256, "body": body})
        srv.ledger.append_event(self.session_id, self.turn_id, self.epoch,
                                "response.rendered" if result.rendered else "response.suppressed",
                                "B.renderer", output_objects=[_oid(manifest_pointer)])
        return {**self.render, "discarded": False, "body": self.body}


def _oid(pointer: str | None) -> str | None:
    if pointer is None:
        return None
    return parse_pointer(pointer)[1]
