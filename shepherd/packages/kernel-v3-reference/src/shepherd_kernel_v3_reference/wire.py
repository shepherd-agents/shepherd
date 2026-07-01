"""Canonical wire serializers for the `-lite` contract objects.

This module is the single home for the *normative wire encoding* of the
conformance objects — the byte-stable JSON that Lean Phase 9 differential
testing consumes, and that the `-lite` fixture corpus freezes as
`expected.batches`.

It is deliberately distinct from `semantic.semantic_transition_batch_to_json`,
which is a raw debug/pressure-check helper (full `asdict` expansion). The
serializers here build each wire object explicitly, field by field, rather than
expanding a dataclass tree via `asdict` and patching known fields afterward.
That keeps the canonical bytes (which Lean Phase 9 must match) decoupled from
the debug helper's structural choices: a future field that needs special
encoding (e.g., another nested `SemanticProfile`) must be added here
deliberately — it cannot be silently auto-expanded into the wrong shape. The
wire encoding here pins the contract shape that Python and Lean must agree on:

- `SemanticProfile` encodes as its `name` string, not the
  `{name, version, validated}` implementation record (`validated` is a
  Python-side proof-status flag; `version` is recoverable from the name).
- The operational `ReplayableKernelTransition` on a `KernelResultEnvelope` is
  encoded by its `transition_id` reference only. The full transition is
  operational (it carries sidecar maps, content-hashed ids, and operational
  schema versions Lean does not model); the conformance projection lives in
  the batch.

`canonical_json(...)` (re-exported from `kernel.refs`) produces the final byte
string; the `*_to_wire(...)` functions produce JSON-compatible dicts. The
canonical encoding rules are in 260521-0600-kernel.md §"Canonical Encoding
Rules".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from shepherd_kernel_v3_reference.envelope import (
    CompletedResult,
    KernelRejection,
    KernelResultEnvelope,
    KernelResultPayload,
    SourceLocation,
    WireResult,
)
from shepherd_kernel_v3_reference.kernel.refs import canonical_json
from shepherd_kernel_v3_reference.kernel.replay import (
    ExternalEffectRequest,
    external_effect_request_to_json,
)
from shepherd_kernel_v3_reference.semantic import (
    AdmissionBasis,
    ExternalEvidenceLink,
    ProfileRejected,
    SemanticTransitionBatch,
)
from shepherd_kernel_v3_reference.trace.serde import trace_record_to_json

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.profiles import SemanticProfile


def _profile_to_wire(profile: SemanticProfile) -> str:
    """Encode a SemanticProfile as its name.

    `version` and `validated` are recoverable from the name on both Python and
    Lean sides and are not part of the wire state.
    """

    return profile.name


def _admission_basis_to_wire(basis: AdmissionBasis) -> dict[str, Any]:
    """Canonical wire dict for an AdmissionBasis.

    Built explicitly so the one nested `SemanticProfile` (`basis.profile`) is
    name-encoded and no other nested record is auto-expanded into a debug shape.
    """

    return {
        "source_ref": basis.source_ref,
        "source_kind": basis.source_kind,
        "source_generation": {"value": basis.source_generation.value},
        "observed_frontier": {"record_refs": list(basis.observed_frontier.record_refs)},
        "source_path_ref": basis.source_path_ref,
        "input_value_or_digest": basis.input_value_or_digest,
        "idempotency_key": basis.idempotency_key,
        "one_shot_key": {"value": basis.one_shot_key.value},
        "profile": _profile_to_wire(basis.profile),
        "program_ref": basis.program_ref,
        "kernel_version": basis.kernel_version,
        "record_schema_versions": list(basis.record_schema_versions),
        "continuation_object_schema_version": basis.continuation_object_schema_version,
        "external_evidence_refs_or_digests": list(basis.external_evidence_refs_or_digests),
    }


def _external_evidence_link_to_wire(link: ExternalEvidenceLink) -> dict[str, Any]:
    return {
        "semantic_record_ref": link.semantic_record_ref,
        "relation": link.relation,
        "external_system_kind": link.external_system_kind,
        "external_ref": link.external_ref,
        "external_schema_ref": link.external_schema_ref,
        "evidence_digest": link.evidence_digest,
        "external_status": link.external_status,
        "link_schema_version": link.link_schema_version,
    }


def semantic_batch_to_wire(batch: SemanticTransitionBatch) -> dict[str, Any]:
    """Canonical wire dict for a SemanticTransitionBatch.

    Built field by field. `profile` (batch-level and, when present,
    admission-basis-level) is name-encoded; every other field is rendered
    explicitly so the wire shape is a deliberate contract, not an `asdict`
    side effect. `canonical_json` sorts keys and normalizes tuples, so the
    construction order here is not significant to the final bytes.
    """

    return {
        "transition_id": batch.transition_id,
        "idempotency_key": batch.idempotency_key,
        "transition_kind": batch.transition_kind,
        "admission_basis": (
            None
            if batch.admission_basis is None
            else _admission_basis_to_wire(batch.admission_basis)
        ),
        "profile": _profile_to_wire(batch.profile),
        "program_ref": batch.program_ref,
        "parent_transition_refs": list(batch.parent_transition_refs),
        "records": [dict(record) for record in batch.records],
        "ref_map": {
            "entries": [[runtime, canonical] for runtime, canonical in batch.ref_map.entries],
            "map_schema_version": batch.ref_map.map_schema_version,
        },
        "continuation_objects": [dict(obj) for obj in batch.continuation_objects],
        "external_evidence_links": [
            _external_evidence_link_to_wire(link) for link in batch.external_evidence_links
        ],
        "semantic_context": dict(batch.semantic_context),
        "batch_schema_version": batch.batch_schema_version,
        "kernel_version": batch.kernel_version,
        "trace_record_schema_versions": list(batch.trace_record_schema_versions),
        "continuation_object_schema_version": batch.continuation_object_schema_version,
        "external_evidence_link_schema_version": batch.external_evidence_link_schema_version,
        "schema_refs": list(batch.schema_refs),
        "code_identity_refs": list(batch.code_identity_refs),
    }


def profile_rejected_to_wire(rejected: ProfileRejected) -> dict[str, Any]:
    """Canonical wire dict for a ProfileRejected projection."""

    return {
        "transition_id": rejected.transition_id,
        "profile": _profile_to_wire(rejected.profile),
        "program_ref": rejected.program_ref,
        "partial_records": [dict(record) for record in rejected.partial_records],
        "rejection_reason": rejected.rejection_reason,
        "consumed_source_keys": list(rejected.consumed_source_keys),
        "ref_map": {
            "entries": [[runtime, canonical] for runtime, canonical in rejected.ref_map.entries],
            "map_schema_version": rejected.ref_map.map_schema_version,
        },
    }


def _source_location_to_wire(location: SourceLocation | None) -> dict[str, Any] | None:
    if location is None:
        return None
    return {
        "construct_path": location.construct_path,
        "line": location.line,
        "column": location.column,
    }


def kernel_rejection_to_wire(rejection: KernelRejection) -> dict[str, Any]:
    """Canonical wire dict for a KernelRejection.

    All per-kind optional fields are emitted explicitly (as `null` when
    absent) per the §"Canonical Encoding Rules" explicit-null policy, so the
    shape is stable across kinds.
    """

    return {
        "kind": rejection.kind,
        "diagnostic": rejection.diagnostic,
        "program_ref": rejection.program_ref,
        "construct": rejection.construct,
        "source_location": _source_location_to_wire(rejection.source_location),
        "rejection_index": rejection.rejection_index,
        "rejection_class": rejection.rejection_class,
        "partial_records": [trace_record_to_json(record) for record in rejection.partial_records],
    }


def completed_result_to_wire(result: CompletedResult) -> dict[str, Any]:
    return {"program_ref": result.program_ref, "value": result.value}


def _payload_to_wire(payload: KernelResultPayload) -> dict[str, Any]:
    if isinstance(payload, CompletedResult):
        return {"payload_type": "completed", **completed_result_to_wire(payload)}
    if isinstance(payload, ExternalEffectRequest):
        return {
            "payload_type": "external-effect-request",
            **external_effect_request_to_json(payload),
        }
    if isinstance(payload, KernelRejection):
        return {"payload_type": "kernel-rejection", **kernel_rejection_to_wire(payload)}
    raise TypeError(f"unknown envelope payload type: {type(payload).__name__}")


def kernel_result_envelope_to_wire(envelope: KernelResultEnvelope) -> dict[str, Any]:
    """Canonical wire dict for a KernelResultEnvelope.

    The operational `transition` is encoded by its `transition_id` reference
    only (see module docstring).
    """

    return {
        "profile": _profile_to_wire(envelope.profile),
        "status": envelope.status,
        "kernel_version": envelope.kernel_version,
        "transition_id": (
            None if envelope.transition is None else envelope.transition.transition_id
        ),
        "payload": _payload_to_wire(envelope.payload),
    }


def wire_result_to_wire(wire: WireResult) -> dict[str, Any]:
    """Canonical wire dict for a WireResult (envelope summary + batch projection)."""

    batch = wire.batch
    if isinstance(batch, SemanticTransitionBatch):
        batch_wire = {"batch_type": "semantic-transition-batch", **semantic_batch_to_wire(batch)}
    elif isinstance(batch, ProfileRejected):
        batch_wire = {"batch_type": "profile-rejected", **profile_rejected_to_wire(batch)}
    else:
        raise TypeError(f"unknown WireResult.batch type: {type(batch).__name__}")
    return {
        "envelope": kernel_result_envelope_to_wire(wire.envelope),
        "batch": batch_wire,
    }


__all__ = [
    "canonical_json",
    "completed_result_to_wire",
    "kernel_rejection_to_wire",
    "kernel_result_envelope_to_wire",
    "profile_rejected_to_wire",
    "semantic_batch_to_wire",
    "wire_result_to_wire",
]
