#!/usr/bin/env python3
"""Phase 0 contract check.

Proves two things with no LLM and no MCT runtime:

  1. Every schema in src/hugpy_agent/mct/schemas/ is a well-formed JSON Schema
     (2020-12) that its own metaschema accepts.
  2. The canonical example payloads transcribed from the design document
     (mediated-context-terminal-design.md §8, §14) validate against them, and a
     set of deliberately malformed payloads are rejected.

This is the executable half of "Phase 0 — Freeze the contract": the schemas are
frozen only once positive and negative fixtures both pass.

Usage:  python3 tools/validate_schemas.py
Exit code 0 = contract frozen; non-zero = a schema/fixture disagreement.
"""
from __future__ import annotations

import json
import pathlib
import sys

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

SCHEMA_DIR = pathlib.Path(__file__).resolve().parent.parent / "src" / "hugpy_agent" / "mct" / "schemas"

# Canonical positive fixtures, quoted from the design doc sections noted.
VALID = {
    # §8.1 common control envelope
    "envelope-v1.json": {
        "v": "mct/1",
        "type": "context.ready",
        "session_id": "s_01JABCDEF",
        "turn_id": "t_000042",
        "sequence": 109,
        "epoch": "e_01JABCDEF",
        "object": "mct://broker/session/s_01JABCDEF/object/o_01JXYZ",
        "sha256": "4d7f" + "0" * 60,
        "media_type": "application/vnd.hugpy.mct-context+json",
        "bytes": 14288,
        "idempotency_key": "s_01JABCDEF:t_000042:context:1",
        "expires_at": "2026-08-01T20:15:00Z",
    },
    # §8.3 context manifest
    "context-v1.json": {
        "schema": "mct.context/1",
        "session_id": "s_01JABCDEF",
        "turn_id": "t_000042",
        "epoch": "e_01JABCDEF",
        "operator_turn": {
            "object": "mct://broker/session/s_01JABCDEF/object/o_prompt",
            "required": True,
            "verbatim": True,
        },
        "fragments": [
            {
                "object": "mct://broker/session/s_01JABCDEF/object/o_policy",
                "role": "governing_instruction",
                "priority": 100,
                "source": "session-policy",
                "revision": 7,
                "token_estimate": 620,
                "required": True,
            },
            {
                "object": "mct://broker/session/s_01JABCDEF/object/o_decisions",
                "role": "decision_memory",
                "priority": 85,
                "source_objects": ["o_turn_31", "o_turn_36"],
                "token_estimate": 410,
                "required": False,
            },
        ],
        "budget": {
            "maximum_input_tokens": 24000,
            "reserved_output_tokens": 8000,
            "pull_tokens_remaining": 12000,
        },
        "catalog": "mct://broker/session/s_01JABCDEF/object/o_catalog",
        "previous_manifest_sha256": "6f24" + "0" * 60,
    },
    # §8.4 pull request
    "pull-request-v1.json": {
        "schema": "mct.pull-request/1",
        "session_id": "s_01JABCDEF",
        "turn_id": "t_000042",
        "epoch": "e_01JABCDEF",
        "request_id": "pr_0003",
        "need": "Locate the first eviction decision for worker gpu-02",
        "target": {"kind": "catalog-query", "query": "gpu-02 eviction first decision"},
        "preferred_form": "matching lines with 20 lines of surrounding context",
        "maximum_tokens": 2400,
        "reason": "The supplied summary identifies the worker but not the first causal event",
        "required_fidelity": "verbatim-source",
        "allow_summary_fallback": True,
    },
    # §8.5 pull result
    "pull-result-v1.json": {
        "schema": "mct.pull-result/1",
        "request_id": "pr_0003",
        "decision": "reduced",
        "objects": [
            {
                "object": "mct://broker/session/s_01JABCDEF/object/o_excerpt",
                "selector": "lines 819-873",
                "source": "mct://broker/session/s_01JABCDEF/object/o_log_snapshot",
                "sha256": "a907" + "0" * 60,
                "token_estimate": 1210,
            }
        ],
        "omitted": {"source_bytes": 41943040, "reason": "Request was satisfiable from a bounded excerpt"},
        "policy_revision": 12,
    },
    # §8.6 response manifest
    "response-v1.json": {
        "schema": "mct.response/1",
        "session_id": "s_01JABCDEF",
        "turn_id": "t_000042",
        "epoch": "e_01JABCDEF",
        "body": "mct://broker/session/s_01JABCDEF/object/o_response",
        "format": "text/markdown",
        "final": True,
        "opened_context": "mct://broker/session/s_01JABCDEF/object/o_receipt",
        "proposed_actions": [],
        "body_sha256": "138c" + "0" * 60,
    },
    # §14.1 ledger event
    "event-v1.json": {
        "event_id": "ev_01JABCDEF",
        "session_id": "s_01JABCDEF",
        "turn_id": "t_000042",
        "sequence": 113,
        "epoch": "e_01JABCDEF",
        "type": "pull.fulfilled",
        "actor": "B.pull-broker",
        "input_objects": ["o_pull_request", "o_log_snapshot"],
        "output_objects": ["o_excerpt", "o_pull_result"],
        "policy_revision": 12,
        "timestamp": "2026-08-01T19:55:42.417Z",
        "previous_event_sha256": "9b12" + "0" * 60,
    },
}

# Negative fixtures: each must be REJECTED. Encodes an invariant the schema guards.
INVALID = {
    "envelope-v1.json": [
        ("wrong protocol version (§23 negotiation)", {**VALID["envelope-v1.json"], "v": "mct/2"}),
        ("unknown message type", {**VALID["envelope-v1.json"], "type": "context.maybe"}),
        ("host path leaked into pointer field (invariant 5)",
         {**VALID["envelope-v1.json"], "object": "/etc/shadow"}),
        ("inline body smuggled onto control plane (invariant 3)",
         {**VALID["envelope-v1.json"], "prompt": "hello"}),
        ("malformed digest", {**VALID["envelope-v1.json"], "sha256": "nothex"}),
    ],
    "context-v1.json": [
        ("operator_turn not verbatim (invariant 2)",
         {**VALID["context-v1.json"],
          "operator_turn": {**VALID["context-v1.json"]["operator_turn"], "verbatim": False}}),
        ("missing required budget", {k: v for k, v in VALID["context-v1.json"].items() if k != "budget"}),
    ],
    "pull-request-v1.json": [
        ("host path as target (invariant 5, §13.6)",
         {**VALID["pull-request-v1.json"], "target": {"kind": "path", "path": "/etc/shadow"}}),
    ],
    "pull-result-v1.json": [
        ("unknown decision code (§10.2)", {**VALID["pull-result-v1.json"], "decision": "maybe"}),
    ],
    "response-v1.json": [
        ("missing body digest (§16.2)",
         {k: v for k, v in VALID["response-v1.json"].items() if k != "body_sha256"}),
    ],
    "event-v1.json": [
        ("inline prompt body in ledger (§18.3)", {**VALID["event-v1.json"], "prompt": "secret"}),
    ],
}


def main() -> int:
    failures: list[str] = []
    checked = 0

    for path in sorted(SCHEMA_DIR.glob("*.json")):
        name = path.name
        schema = json.loads(path.read_text())

        # 1. schema is itself valid against the 2020-12 metaschema
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            failures.append(f"[{name}] not a valid JSON Schema: {exc.message}")
            continue
        validator = Draft202012Validator(schema)

        # 2. positive fixture validates
        if name in VALID:
            checked += 1
            errs = sorted(validator.iter_errors(VALID[name]), key=lambda e: e.path)
            if errs:
                failures.append(f"[{name}] canonical example REJECTED: {errs[0].message}")
            else:
                print(f"  ok   {name:<24} canonical example accepted")
        else:
            print(f"  warn {name:<24} no positive fixture")

        # 3. negative fixtures are rejected
        for label, payload in INVALID.get(name, []):
            checked += 1
            try:
                validator.validate(payload)
            except ValidationError:
                print(f"  ok   {name:<24} rejects: {label}")
            else:
                failures.append(f"[{name}] FAILED to reject: {label}")

    print()
    if failures:
        print(f"FAIL — {len(failures)} contract violation(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"PASS — {checked} fixtures checked across {len(list(SCHEMA_DIR.glob('*.json')))} schemas. Contract frozen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
