"""Carrier-independent semantic transition and admission sketches.

These dataclasses are intentionally small and dependency-free. They name the
semantic control facts that later storage, carrier, and facade layers can cite
without making those layers part of the kernel.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from math import isfinite
from typing import TYPE_CHECKING, Any, Literal

from shepherd_kernel_v3_reference.kernel.continuation_objects import (
    CONTINUATION_OBJECT_SCHEMA_VERSION,
    ContinuationObject,
    ContinuationRoot,
    continuation_object_from_json,
    continuation_object_ref,
)
from shepherd_kernel_v3_reference.paths import source_path_ref, unhandled_source_path_ref
from shepherd_kernel_v3_reference.profiles import CORE_A, SemanticProfile

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.trace.records import (
        ContinuationPending,
        EffectDeclaration,
        ForkBranch,
        TraceRecord,
    )

JsonValue = Any

SourceKind = Literal[
    "ResumptionHandle",
    "UnhandledSuspension",
    "ContinuationPending",
    "ForkBranch",
]

TransitionKind = Literal[
    "initial_run_prefix",
    "callable_resume",
    "unhandled_top_level_resume",
    "pending_resume",
    "fork_branch_resume",
    "runtime_failure",
    "carrier_failure",
    "abandoned",
]

EvidenceRelation = Literal[
    "caused_by",
    "observed_by",
    "fulfilled_by",
    "produced_resume_input",
    "materialized_as",
]

ExternalSystemKind = Literal[
    "mock",
    "commons-vcs",
    "vcs-core",
    "shepherd2",
    "provider",
    "export",
]


SEMANTIC_KERNEL_VERSION = "shepherd_kernel_v3_reference.semantic-kernel.v0"
TRANSITION_BATCH_SCHEMA_VERSION = "shepherd_kernel_v3_reference.semantic-transition-batch.v2"
TRACE_RECORD_SCHEMA_VERSION = "shepherd_kernel_v3_reference.trace-records.v0"
EXTERNAL_EVIDENCE_LINK_SCHEMA_VERSION = "shepherd_kernel_v3_reference.external-evidence-link.v0"
CANONICAL_REF_MAP_SCHEMA_VERSION = "shepherd_kernel_v3_reference.canonical-ref-map.v1"

_CONTINUATION_REF_FIELDS_BY_RECORD_TYPE: Mapping[str, tuple[str, ...]] = {
    "EffectDeclaration": ("full_continuation_ref",),
    "HandlerSelection": ("captured_continuation_ref", "outer_continuation_ref"),
    "ResumptionHandle": ("continuation_ref",),
    "ContinuationResume": (
        "continuation_ref",
        "handler_continuation_ref",
        "handler_dynamic_tail_ref",
    ),
    "ResumeReturn": ("handler_continuation_ref", "handler_dynamic_tail_ref"),
    "ContinuationPending": ("continuation_ref",),
    "ForkBranch": ("continuation_ref", "terminal_continuation_ref"),
}


class SemanticTransitionBatchValidationError(ValueError):
    """Raised when a semantic transition batch violates payload invariants."""


@dataclass(frozen=True)
class CanonicalRefMap:
    """Mandatory projection from runtime-local refs to content-addressed canonical refs.

    Per 260521-0600-kernel.md §"Canonical Ref Map" (B-with-tightening policy).
    Entries are sorted by `runtime_ref` and unique. The projection function
    `semantic_batch_from_transition(...)` (commit #72) is the single
    canonicalization boundary; full coverage/tightness/dependency-order
    validation lives in `validate_semantic_batch(...)` (commit #72). This
    construction-time check enforces only the shape obligations.
    """

    entries: tuple[tuple[str, str], ...] = ()
    map_schema_version: str = CANONICAL_REF_MAP_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.map_schema_version != CANONICAL_REF_MAP_SCHEMA_VERSION:
            raise SemanticTransitionBatchValidationError(
                "CanonicalRefMap.map_schema_version must be "
                f"{CANONICAL_REF_MAP_SCHEMA_VERSION!r}"
            )
        seen: set[str] = set()
        previous = ""
        for index, entry in enumerate(self.entries):
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise SemanticTransitionBatchValidationError(
                    f"CanonicalRefMap.entries[{index}] must be a (runtime_ref, canonical_ref) pair"
                )
            runtime_ref, canonical_ref = entry
            if not isinstance(runtime_ref, str) or not runtime_ref:
                raise SemanticTransitionBatchValidationError(
                    f"CanonicalRefMap.entries[{index}].runtime_ref must be a non-empty string"
                )
            if not isinstance(canonical_ref, str) or not canonical_ref:
                raise SemanticTransitionBatchValidationError(
                    f"CanonicalRefMap.entries[{index}].canonical_ref must be a non-empty string"
                )
            if runtime_ref in seen:
                raise SemanticTransitionBatchValidationError(
                    f"CanonicalRefMap.entries[{index}] duplicates runtime_ref {runtime_ref!r}"
                )
            if runtime_ref < previous:
                raise SemanticTransitionBatchValidationError(
                    f"CanonicalRefMap.entries[{index}] {runtime_ref!r} is out of sorted order"
                )
            seen.add(runtime_ref)
            previous = runtime_ref

    def get(self, runtime_ref: str) -> str | None:
        for key, value in self.entries:
            if key == runtime_ref:
                return value
        return None

    def __contains__(self, runtime_ref: object) -> bool:
        return any(key == runtime_ref for key, _ in self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self.entries)


@dataclass(frozen=True)
class ProfileRejected:
    """Semantic projection for an operational transition that did not incur a
    full semantic obligation: admission-stage failure, profile-incompatible
    payload, or post-admission failure with no valid batch.

    Per 260521-0600-kernel.md §"Kernel Result Envelope" / §"Semantic
    Transition Batch": rejected transitions carry partial records plus a
    `ref_map` covering them; they do not 1-to-1 project to
    `SemanticTransitionBatch` because there is no admission basis to bind.
    """

    transition_id: str
    profile: SemanticProfile
    program_ref: str
    partial_records: tuple[Mapping[str, JsonValue], ...]
    rejection_reason: str
    consumed_source_keys: tuple[str, ...]
    ref_map: CanonicalRefMap

    def __post_init__(self) -> None:
        if not self.transition_id:
            raise ValueError("ProfileRejected.transition_id must be non-empty")
        if not self.program_ref:
            raise ValueError("ProfileRejected.program_ref must be non-empty")
        if not isinstance(self.ref_map, CanonicalRefMap):
            raise SemanticTransitionBatchValidationError(
                "ProfileRejected.ref_map must be a CanonicalRefMap"
            )
        for idx, record in enumerate(self.partial_records):
            _require_json_compatible(record, context=f"ProfileRejected.partial_records[{idx}]")


@dataclass(frozen=True)
class SourceGeneration:
    """Monotone generation for a source ref."""

    value: int = 0

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError("SourceGeneration.value must be non-negative")


@dataclass(frozen=True)
class OneShotKey:
    """Semantic one-shot admission key for a continuation source."""

    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("OneShotKey.value must be non-empty")


@dataclass(frozen=True)
class ObservedFrontier:
    """Trace prefix observed when a resumptive transition was admitted."""

    record_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContinuationSource:
    """Named source that may admit a later resumptive transition."""

    source_ref: str
    source_kind: SourceKind
    source_generation: SourceGeneration
    continuation_ref: str
    branch_ref: str
    one_shot_key: OneShotKey
    profile: SemanticProfile = CORE_A
    declaration_ref: str | None = None
    selection_ref: str | None = None
    selected_path_ref: str | None = None
    source_path_ref: str | None = None
    operation_result_schema_ref: str | None = None
    restart_continuation_ref: str | None = None
    worker_context_ref: str | None = None

    def __post_init__(self) -> None:
        if self.source_kind == "UnhandledSuspension" and self.selection_ref is not None:
            raise ValueError("UnhandledSuspension cannot cite a selected handler")
        if self.source_kind != "UnhandledSuspension" and self.selection_ref is None:
            raise ValueError(f"{self.source_kind} must cite a selected handler")
        if self.source_kind == "UnhandledSuspension" and self.selected_path_ref is not None:
            raise ValueError("UnhandledSuspension cannot cite a selected path")
        if self.source_kind != "UnhandledSuspension" and self.selected_path_ref is None:
            raise ValueError(f"{self.source_kind} must cite a selected path")
        if self.source_path_ref is None:
            raise ValueError(f"{self.source_kind} must cite a source path")


@dataclass(frozen=True)
class AdmissionBasis:
    """Why a resumptive semantic transition is admitted."""

    source_ref: str
    source_kind: SourceKind
    source_generation: SourceGeneration
    observed_frontier: ObservedFrontier
    source_path_ref: str
    input_value_or_digest: JsonValue | str
    idempotency_key: str
    one_shot_key: OneShotKey
    profile: SemanticProfile
    program_ref: str
    kernel_version: str = SEMANTIC_KERNEL_VERSION
    record_schema_versions: tuple[str, ...] = (TRACE_RECORD_SCHEMA_VERSION,)
    continuation_object_schema_version: str = CONTINUATION_OBJECT_SCHEMA_VERSION
    external_evidence_refs_or_digests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.idempotency_key:
            raise ValueError("AdmissionBasis.idempotency_key must be non-empty")
        if not self.source_path_ref:
            raise ValueError("AdmissionBasis.source_path_ref must be non-empty")
        _require_json_compatible(
            self.input_value_or_digest,
            context="AdmissionBasis.input_value_or_digest",
        )


@dataclass(frozen=True)
class ExternalEvidenceLink:
    """Carrier-neutral link from semantic control to external evidence."""

    semantic_record_ref: str
    relation: EvidenceRelation
    external_system_kind: ExternalSystemKind
    external_ref: str
    external_schema_ref: str
    evidence_digest: str
    external_status: str
    link_schema_version: str = EXTERNAL_EVIDENCE_LINK_SCHEMA_VERSION


@dataclass(frozen=True)
class SemanticTransitionBatch:
    """Atomic semantic transition payload for later retention.

    Per 2026-05-23 §"`SemanticTransitionBatch` v2 bump" settled decision, the
    `ref_map: CanonicalRefMap` field is mandatory at construction (non-defaulted
    field), so type-checkers catch the "I forgot the ref_map" bug class at
    write time rather than at validation time. Coverage, tightness,
    well-formedness, and dependency-order validation are deferred to
    `validate_semantic_batch(...)` (commit #72).
    """

    transition_id: str
    idempotency_key: str
    transition_kind: TransitionKind
    admission_basis: AdmissionBasis | None
    profile: SemanticProfile
    program_ref: str
    parent_transition_refs: tuple[str, ...]
    records: tuple[Mapping[str, JsonValue], ...]
    ref_map: CanonicalRefMap
    continuation_objects: tuple[Mapping[str, JsonValue], ...] = ()
    external_evidence_links: tuple[ExternalEvidenceLink, ...] = ()
    semantic_context: Mapping[str, JsonValue] = field(default_factory=dict)
    batch_schema_version: str = TRANSITION_BATCH_SCHEMA_VERSION
    kernel_version: str = SEMANTIC_KERNEL_VERSION
    trace_record_schema_versions: tuple[str, ...] = (TRACE_RECORD_SCHEMA_VERSION,)
    continuation_object_schema_version: str = CONTINUATION_OBJECT_SCHEMA_VERSION
    external_evidence_link_schema_version: str = EXTERNAL_EVIDENCE_LINK_SCHEMA_VERSION
    schema_refs: tuple[str, ...] = ()
    code_identity_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Defensive runtime check: the field is typed `CanonicalRefMap`
        # (non-defaulted), so static analysis catches omissions, but
        # dynamic construction (e.g. from JSON or via **kwargs) may still
        # supply the wrong type.
        if not isinstance(self.ref_map, CanonicalRefMap):
            raise SemanticTransitionBatchValidationError(
                "SemanticTransitionBatch.ref_map must be a CanonicalRefMap"
            )
        if not self.transition_id:
            raise ValueError("SemanticTransitionBatch.transition_id must be non-empty")
        if not self.idempotency_key:
            raise ValueError("SemanticTransitionBatch.idempotency_key must be non-empty")
        if self.transition_kind != "initial_run_prefix" and self.admission_basis is None:
            raise ValueError(f"{self.transition_kind} requires an explicit AdmissionBasis")
        if self.admission_basis is not None:
            if self.profile != self.admission_basis.profile:
                raise ValueError("SemanticTransitionBatch profile disagrees with admission")
            if self.program_ref != self.admission_basis.program_ref:
                raise ValueError("SemanticTransitionBatch program_ref disagrees with admission")
        for idx, record in enumerate(self.records):
            _require_json_compatible(record, context=f"SemanticTransitionBatch.records[{idx}]")
        for idx, obj in enumerate(self.continuation_objects):
            _require_json_compatible(
                obj,
                context=f"SemanticTransitionBatch.continuation_objects[{idx}]",
            )
        object_catalog = _validated_continuation_object_catalog(self.continuation_objects, self)
        cited_refs = _continuation_refs_from_records(self.records)
        missing_refs = sorted(cited_refs - set(object_catalog))
        if missing_refs:
            raise SemanticTransitionBatchValidationError(
                f"SemanticTransitionBatch is missing continuation objects for refs: {missing_refs!r}"
            )
        non_root_refs = sorted(ref for ref in cited_refs if not isinstance(object_catalog[ref], ContinuationRoot))
        if non_root_refs:
            raise SemanticTransitionBatchValidationError(
                f"SemanticTransitionBatch continuation refs must resolve to roots: {non_root_refs!r}"
            )
        _require_json_compatible(
            self.semantic_context,
            context="SemanticTransitionBatch.semantic_context",
        )


def observed_frontier_from_trace(trace: tuple[TraceRecord, ...]) -> ObservedFrontier:
    """Build the observed frontier named by a trace prefix."""

    return ObservedFrontier(tuple(record.ref for record in trace if hasattr(record, "ref")))


def unhandled_suspension_source_from_declaration(
    declaration: EffectDeclaration,
    *,
    one_shot_key: OneShotKey | None = None,
    source_generation: SourceGeneration = SourceGeneration(0),
    profile: SemanticProfile = CORE_A,
) -> ContinuationSource:
    """Treat an unhandled effect declaration as a top-level continuation source."""

    return ContinuationSource(
        source_ref=declaration.ref,
        source_kind="UnhandledSuspension",
        source_generation=source_generation,
        continuation_ref=declaration.full_continuation_ref,
        branch_ref=declaration.branch_ref,
        one_shot_key=one_shot_key or OneShotKey(f"oneshot:{declaration.ref}:0"),
        profile=profile,
        declaration_ref=declaration.ref,
        selected_path_ref=None,
        source_path_ref=unhandled_source_path_ref(
            declaration.ref,
            declaration.branch_ref,
        ),
        operation_result_schema_ref=declaration.operation_result_schema_ref,
        worker_context_ref=declaration.execution_context_ref,
    )


def pending_source_from_record(
    pending: ContinuationPending,
    *,
    one_shot_key: OneShotKey | None = None,
    source_generation: SourceGeneration = SourceGeneration(0),
    profile: SemanticProfile = CORE_A,
) -> ContinuationSource:
    """Build a source descriptor for a terminal pending continuation."""

    return ContinuationSource(
        source_ref=pending.ref,
        source_kind="ContinuationPending",
        source_generation=source_generation,
        continuation_ref=pending.continuation_ref,
        branch_ref=pending.branch_ref,
        one_shot_key=one_shot_key or OneShotKey(f"oneshot:{pending.ref}:0"),
        profile=profile,
        declaration_ref=pending.declaration_ref,
        selection_ref=pending.selection_ref,
        selected_path_ref=pending.selection_path_ref,
        source_path_ref=source_path_ref(
            pending.selection_ref,
            pending.ref,
            pending.branch_ref,
        ),
        operation_result_schema_ref=pending.operation_result_schema_ref,
        worker_context_ref=pending.worker_context_ref,
    )


def fork_branch_source_from_record(
    branch: ForkBranch,
    *,
    one_shot_key: OneShotKey | None = None,
    source_generation: SourceGeneration = SourceGeneration(0),
    profile: SemanticProfile = CORE_A,
) -> ContinuationSource:
    """Build a source descriptor for one terminal fork branch."""

    return ContinuationSource(
        source_ref=branch.ref,
        source_kind="ForkBranch",
        source_generation=source_generation,
        continuation_ref=branch.continuation_ref,
        branch_ref=branch.branch_ref,
        one_shot_key=one_shot_key or OneShotKey(f"oneshot:{branch.ref}:0"),
        profile=profile,
        declaration_ref=branch.declaration_ref,
        selection_ref=branch.selection_ref,
        selected_path_ref=branch.selection_path_ref,
        source_path_ref=source_path_ref(
            branch.selection_ref,
            branch.ref,
            branch.branch_ref,
        ),
        restart_continuation_ref=branch.terminal_continuation_ref,
    )


def admission_basis_from_source(
    source: ContinuationSource,
    *,
    observed_frontier: ObservedFrontier,
    input_value_or_digest: JsonValue | str,
    program_ref: str,
    idempotency_key: str | None = None,
    external_evidence_refs_or_digests: tuple[str, ...] = (),
) -> AdmissionBasis:
    """Build an admission basis for an externally resumed continuation source."""

    if source.source_path_ref is None:
        raise ValueError(f"{source.source_kind} source must cite a source path")
    return AdmissionBasis(
        source_ref=source.source_ref,
        source_kind=source.source_kind,
        source_generation=source.source_generation,
        observed_frontier=observed_frontier,
        source_path_ref=source.source_path_ref,
        input_value_or_digest=input_value_or_digest,
        idempotency_key=idempotency_key
        or f"idempotency:{source.source_kind}:{source.source_ref}:{source.source_generation.value}",
        one_shot_key=source.one_shot_key,
        profile=source.profile,
        program_ref=program_ref,
        external_evidence_refs_or_digests=external_evidence_refs_or_digests,
    )


def build_initial_transition_batch(
    *,
    program_ref: str,
    transition_id: str,
    records: tuple[Mapping[str, JsonValue], ...],
    ref_map: CanonicalRefMap,
    continuation_objects: tuple[Mapping[str, JsonValue], ...] = (),
    profile: SemanticProfile = CORE_A,
    idempotency_key: str | None = None,
) -> SemanticTransitionBatch:
    """Build the initial-run transition batch without an admission basis.

    `ref_map` is mandatory (mirrors `SemanticTransitionBatch.ref_map`); pass
    `CanonicalRefMap()` for batches without lifecycle refs.
    """

    return SemanticTransitionBatch(
        transition_id=transition_id,
        idempotency_key=idempotency_key or f"idempotency:{transition_id}",
        transition_kind="initial_run_prefix",
        admission_basis=None,
        profile=profile,
        program_ref=program_ref,
        parent_transition_refs=(),
        records=records,
        ref_map=ref_map,
        continuation_objects=continuation_objects,
    )


def build_admitted_transition_batch(
    *,
    program_ref: str,
    transition_id: str,
    transition_kind: TransitionKind,
    admission_basis: AdmissionBasis,
    parent_transition_refs: tuple[str, ...],
    records: tuple[Mapping[str, JsonValue], ...],
    ref_map: CanonicalRefMap,
    continuation_objects: tuple[Mapping[str, JsonValue], ...] = (),
    profile: SemanticProfile | None = None,
    idempotency_key: str | None = None,
    external_evidence_links: tuple[ExternalEvidenceLink, ...] = (),
) -> SemanticTransitionBatch:
    """Build an admitted resumptive transition batch.

    `ref_map` is mandatory (mirrors `SemanticTransitionBatch.ref_map`); pass
    `CanonicalRefMap()` for batches without lifecycle refs.
    """

    if transition_kind == "initial_run_prefix":
        raise ValueError("use build_initial_transition_batch for initial_run_prefix")
    batch_profile = profile or admission_basis.profile
    if batch_profile != admission_basis.profile:
        raise ValueError("transition batch profile must match admission profile")
    if program_ref != admission_basis.program_ref:
        raise ValueError("transition batch program_ref must match admission program_ref")
    return SemanticTransitionBatch(
        transition_id=transition_id,
        idempotency_key=idempotency_key or admission_basis.idempotency_key,
        transition_kind=transition_kind,
        admission_basis=admission_basis,
        profile=batch_profile,
        program_ref=program_ref,
        parent_transition_refs=parent_transition_refs,
        records=records,
        ref_map=ref_map,
        continuation_objects=continuation_objects,
        external_evidence_links=external_evidence_links,
    )


def semantic_transition_batch_to_json(
    batch: SemanticTransitionBatch,
) -> dict[str, Any]:
    """Return a JSON-compatible mapping for fixture and pressure-check tests."""

    encoded = _jsonify(asdict(batch))
    if not isinstance(encoded, dict):
        raise TypeError("encoded SemanticTransitionBatch must be a mapping")
    return encoded


def _jsonify(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_jsonify(item) for item in value]
    if isinstance(value, list):
        return [_jsonify(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonify(item) for key, item in value.items()}
    return value


def _require_json_compatible(value: Any, *, context: str) -> None:
    if value is None or isinstance(value, bool | int | str):
        return
    if isinstance(value, float):
        if not isfinite(value):
            raise TypeError(f"{context} contains a non-finite float")
        return
    if isinstance(value, tuple | list):
        for idx, item in enumerate(value):
            _require_json_compatible(item, context=f"{context}[{idx}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{context} contains a non-string mapping key")
            _require_json_compatible(item, context=f"{context}.{key}")
        return
    raise TypeError(f"{context} contains a non-JSON-compatible value: {value!r}")


def _validated_continuation_object_catalog(
    objects: tuple[Mapping[str, JsonValue], ...],
    batch: SemanticTransitionBatch,
) -> dict[str, ContinuationObject]:
    catalog: dict[str, ContinuationObject] = {}
    for idx, object_data in enumerate(objects):
        try:
            obj = continuation_object_from_json(object_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise SemanticTransitionBatchValidationError(
                f"invalid SemanticTransitionBatch.continuation_objects[{idx}]"
            ) from exc
        ref = continuation_object_ref(obj)
        if ref in catalog:
            raise SemanticTransitionBatchValidationError(f"duplicate continuation object ref: {ref!r}")
        if isinstance(obj, ContinuationRoot) and obj.program_ref != batch.program_ref:
            raise SemanticTransitionBatchValidationError(
                f"continuation object program_ref disagrees with batch program_ref: {ref!r}"
            )
        catalog[ref] = obj
    return catalog


def _continuation_refs_from_records(
    records: tuple[Mapping[str, JsonValue], ...],
) -> set[str]:
    refs: set[str] = set()
    for record in records:
        record_type = record.get("record_type")
        if not isinstance(record_type, str):
            continue
        for field_name in _CONTINUATION_REF_FIELDS_BY_RECORD_TYPE.get(
            record_type,
            (),
        ):
            value = record.get(field_name)
            if isinstance(value, str) and value.startswith("continuation-object:"):
                refs.add(value)
    return refs
