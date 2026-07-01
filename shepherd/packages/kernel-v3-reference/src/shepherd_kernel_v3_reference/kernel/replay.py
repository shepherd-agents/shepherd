"""Serialized continuation replay boundary for the kernel v3 reference runtime."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, cast

from shepherd_kernel_v3_reference.kernel.continuation_objects import (
    ContinuationObject,
    ContinuationRoot,
    continuation_object_child_refs,
    continuation_object_from_json,
    continuation_object_ref,
    continuation_object_to_json,
)
from shepherd_kernel_v3_reference.kernel.program_admission import (
    KernelProgramInput,
    PreparedKernelProgram,
    ensure_prepared_kernel_program,
)
from shepherd_kernel_v3_reference.kernel.program_identity import project_program_identity
from shepherd_kernel_v3_reference.kernel.refs import content_ref
from shepherd_kernel_v3_reference.kernel.step_machine import StepKernelEvaluator
from shepherd_kernel_v3_reference.profiles import SemanticProfile
from shepherd_kernel_v3_reference.source.outcomes import (
    Completed,
    Delayed,
    Forked,
    ResumptionUsed,
    SourceOutcome,
    Suspended,
)
from shepherd_kernel_v3_reference.trace.machine import TraceDebugEvidence, TraceResult, record_from_event, run_trace
from shepherd_kernel_v3_reference.trace.records import EffectDeclaration, TraceRecord
from shepherd_kernel_v3_reference.trace.serde import (
    trace_from_json,
    trace_record_from_json,
    trace_record_to_json,
    trace_to_json,
)

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.kernel.ir import Ref
    from shepherd_kernel_v3_reference.source.effects import EffectRegistry
    from shepherd_kernel_v3_reference.source.values import Env

CONTINUATION_REPLAY_ARTIFACT_SCHEMA_VERSION = "shepherd_kernel_v3_reference.continuation-replay-artifact.v2"
CONTINUATION_SOURCE_KEY_SCHEMA_VERSION = "shepherd_kernel_v3_reference.continuation-source-key.v1"
EXTERNAL_EFFECT_REQUEST_SCHEMA_VERSION = "shepherd_kernel_v3_reference.external-effect-request.v1"
HOST_COMPLETED_SCHEMA_VERSION = "shepherd_kernel_v3_reference.host-completed.v2"
KERNEL_REPLAY_JOURNAL_SCHEMA_VERSION = "shepherd_kernel_v3_reference.kernel-replay-journal.v3"
KERNEL_REPLAY_STATE_SCHEMA_VERSION = "shepherd_kernel_v3_reference.kernel-replay-state.v3"
REPLAYABLE_KERNEL_TRANSITION_SCHEMA_VERSION = "shepherd_kernel_v3_reference.replayable-kernel-transition.v7"

JsonValue: TypeAlias = Any
SourceRecordType: TypeAlias = Literal["EffectDeclaration", "ResumptionHandle"]
ReplayableKernelTransitionStatus: TypeAlias = Literal["completed", "external-effect-request", "rejected"]

_ARTIFACT_JSON_KEYS = frozenset(
    {
        "artifact_schema_version",
        "root_ref",
        "program_ref",
        "source_key",
        "source_ref",
        "source_record_type",
        "effect_kind",
        "operation_result_schema_ref",
        "continuation_objects",
    }
)
_ARTIFACT_RECORD_JSON_KEYS = frozenset(
    {
        "artifact_schema_version",
        "root_ref",
        "program_ref",
        "source_key",
        "source_ref",
        "source_record_type",
        "effect_kind",
        "operation_result_schema_ref",
    }
)
_ARTIFACT_ENTRY_JSON_KEYS = frozenset({"ref", "artifact"})
_OBJECT_ENTRY_JSON_KEYS = frozenset({"ref", "object"})
_SOURCE_RECORD_TYPES = frozenset({"EffectDeclaration", "ResumptionHandle"})
_REPLAYABLE_KERNEL_TRANSITION_STATUSES = frozenset({"completed", "external-effect-request", "rejected"})
_EXTERNAL_EFFECT_REQUEST_JSON_KEYS = frozenset(
    {
        "request_schema_version",
        "declaration",
        "replay_artifact",
        "trace_prefix",
    }
)
_HOST_COMPLETED_JSON_KEYS = frozenset(
    {
        "observation_schema_version",
        "value",
        "evidence_refs",
    }
)
_REPLAYABLE_KERNEL_TRANSITION_JSON_KEYS = frozenset(
    {
        "transition_schema_version",
        "transition_id",
        "parent_transition_refs",
        "program_ref",
        "resume_observation_ref",
        "status",
        "payload",
        "trace_delta",
        "context_ref_map",
        "continuation_ref_map",
        "continuation_control_ref_map",
    }
)
_REPLAYABLE_COMPLETED_JSON_KEYS = frozenset({"payload_type", "program_ref", "value", "trace"})
_REPLAYABLE_REJECTED_JSON_KEYS = frozenset(
    {
        "payload_type",
        "program_ref",
        "source_key",
        "request_ref",
        "reason_type",
        "reason_message",
        "trace",
    }
)
_REPLAYABLE_REQUEST_JSON_KEYS = frozenset({"payload_type", "request"})
_REPLAYABLE_REQUEST_REF_JSON_KEYS = frozenset(
    {
        "payload_type",
        "request_ref",
        "declaration",
        "artifact_ref",
        "trace_prefix",
    }
)
_OPEN_REPLAY_REQUEST_JSON_KEYS = frozenset(
    {
        "source_key",
        "request_ref",
        "request_transition_ref",
        "program_ref",
        "declaration_ref",
        "effect_kind",
        "root_ref",
        "operation_result_schema_ref",
        "trace_prefix_ref",
    }
)
_KERNEL_REPLAY_STATE_JSON_KEYS = frozenset(
    {
        "state_schema_version",
        "program_ref",
        "profile",
        "open_requests",
        "consumed_source_keys",
        "transition_refs",
        "trace",
        "terminal",
        "rejected",
    }
)
_KERNEL_REPLAY_JOURNAL_JSON_KEYS = frozenset(
    {
        "journal_schema_version",
        "program_ref",
        "continuation_objects",
        "artifacts",
        "transitions",
    }
)


class ContinuationReplayError(RuntimeError):
    """Raised when a replay artifact is incompatible with the requested replay."""


class ContinuationReplaySerializationError(ValueError):
    """Raised when continuation replay artifact JSON cannot be decoded."""


class _ContinuationReplayTraceFailure(Exception):
    """Internal carrier for trace records emitted before replay rejection."""

    def __init__(self, reason: Exception, trace_delta: tuple[TraceRecord, ...]) -> None:
        super().__init__(str(reason))
        self.reason = reason
        self.trace_delta = trace_delta


@dataclass(frozen=True)
class _ReplayObservationTransition:
    """Internal transition result for one admitted host observation."""

    transition: ReplayableKernelTransition
    rejection_reason: BaseException | None = None


@dataclass(frozen=True)
class ReplayableCompleted:
    """Completed kernel result paired with the trace delta that produced it."""

    program_ref: Ref
    outcome: Completed
    trace: tuple[TraceRecord, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "program_ref", _require_str(self.program_ref, "ReplayableCompleted.program_ref"))
        _require_json_compatible(self.outcome.value, "ReplayableCompleted.value")
        object.__setattr__(self, "trace", tuple(self.trace))

    @property
    def value(self) -> JsonValue:
        return self.outcome.value


@dataclass(frozen=True)
class ReplayableRejected:
    """Rejected replay result after an admitted host observation consumed a source."""

    program_ref: Ref
    source_key: str
    request_ref: Ref
    reason_type: str
    reason_message: str
    trace: tuple[TraceRecord, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "program_ref", _require_str(self.program_ref, "ReplayableRejected.program_ref"))
        object.__setattr__(self, "source_key", _require_str(self.source_key, "ReplayableRejected.source_key"))
        object.__setattr__(self, "request_ref", _require_str(self.request_ref, "ReplayableRejected.request_ref"))
        object.__setattr__(self, "reason_type", _require_str(self.reason_type, "ReplayableRejected.reason_type"))
        object.__setattr__(
            self,
            "reason_message",
            _require_str(self.reason_message, "ReplayableRejected.reason_message"),
        )
        object.__setattr__(self, "trace", tuple(self.trace))


@dataclass(frozen=True)
class HostCompleted:
    """Admitted completed host observation for the current replay profile."""

    value: JsonValue
    evidence_refs: tuple[str, ...] = ()
    observation_schema_version: str = HOST_COMPLETED_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.observation_schema_version != HOST_COMPLETED_SCHEMA_VERSION:
            raise ContinuationReplayError(
                f"HostCompleted.observation_schema_version must be {HOST_COMPLETED_SCHEMA_VERSION!r}"
            )
        _require_json_compatible(self.value, "HostCompleted.value")
        evidence_refs = _require_sequence(self.evidence_refs, "HostCompleted.evidence_refs")
        object.__setattr__(
            self,
            "evidence_refs",
            tuple(_require_str(ref, "HostCompleted.evidence_refs") for ref in evidence_refs),
        )


@dataclass(frozen=True)
class ExternalEffectRequestDescriptor:
    """Stable host-facing view of an external request.

    This is intentionally not an executable replay capability. Hosts inspect
    this descriptor to produce observations; replay resumes through a live
    session or a journal/catalog materialization boundary.
    """

    request_ref: Ref
    source_key: str
    declaration_ref: Ref
    program_ref: Ref | None
    effect_kind: str
    payload: JsonValue
    payload_schema_ref: Ref | None
    operation_result_schema_ref: Ref | None
    root_ref: Ref
    replay_artifact_ref: Ref
    trace_prefix_ref: Ref

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_ref", _require_str(self.request_ref, "ExternalEffectRequestDescriptor.ref"))
        object.__setattr__(
            self,
            "source_key",
            _require_str(self.source_key, "ExternalEffectRequestDescriptor.source_key"),
        )
        object.__setattr__(
            self,
            "declaration_ref",
            _require_str(self.declaration_ref, "ExternalEffectRequestDescriptor.declaration_ref"),
        )
        _require_optional_str(self.program_ref, "ExternalEffectRequestDescriptor.program_ref")
        object.__setattr__(
            self,
            "effect_kind",
            _require_str(self.effect_kind, "ExternalEffectRequestDescriptor.effect_kind"),
        )
        _require_json_compatible(self.payload, "ExternalEffectRequestDescriptor.payload")
        object.__setattr__(
            self,
            "payload",
            _json_value_snapshot(self.payload),
        )
        _require_optional_str(self.payload_schema_ref, "ExternalEffectRequestDescriptor.payload_schema_ref")
        _require_optional_str(
            self.operation_result_schema_ref,
            "ExternalEffectRequestDescriptor.operation_result_schema_ref",
        )
        object.__setattr__(self, "root_ref", _require_str(self.root_ref, "ExternalEffectRequestDescriptor.root_ref"))
        object.__setattr__(
            self,
            "replay_artifact_ref",
            _require_str(self.replay_artifact_ref, "ExternalEffectRequestDescriptor.replay_artifact_ref"),
        )
        object.__setattr__(
            self,
            "trace_prefix_ref",
            _require_str(self.trace_prefix_ref, "ExternalEffectRequestDescriptor.trace_prefix_ref"),
        )


@dataclass(frozen=True)
class ExternalEffectRequest:
    """First-order host request plus executable restart artifact."""

    declaration: EffectDeclaration
    replay_artifact: ContinuationReplayArtifact
    trace_prefix: tuple[TraceRecord, ...] = ()
    request_schema_version: str = EXTERNAL_EFFECT_REQUEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.request_schema_version != EXTERNAL_EFFECT_REQUEST_SCHEMA_VERSION:
            raise ContinuationReplayError(
                f"ExternalEffectRequest.request_schema_version must be {EXTERNAL_EFFECT_REQUEST_SCHEMA_VERSION!r}"
            )
        if not isinstance(self.declaration, EffectDeclaration):
            raise TypeError("ExternalEffectRequest.declaration must be an EffectDeclaration")
        _require_json_compatible(self.declaration.payload, "ExternalEffectRequest.declaration.payload")
        if self.replay_artifact.source_record_type != "EffectDeclaration":
            raise ContinuationReplayError("ExternalEffectRequest replay artifact must come from an EffectDeclaration")
        if self.replay_artifact.source_ref != self.declaration.ref:
            raise ContinuationReplayError("ExternalEffectRequest replay artifact source_ref disagrees with declaration")
        if self.replay_artifact.effect_kind != self.declaration.effect_kind:
            raise ContinuationReplayError(
                "ExternalEffectRequest replay artifact effect_kind disagrees with declaration"
            )
        if self.replay_artifact.operation_result_schema_ref != self.declaration.operation_result_schema_ref:
            raise ContinuationReplayError(
                "ExternalEffectRequest replay artifact operation_result_schema_ref disagrees with declaration"
            )
        if (
            self.declaration.program_ref is not None
            and self.replay_artifact.program_ref is not None
            and self.replay_artifact.program_ref != self.declaration.program_ref
        ):
            raise ContinuationReplayError(
                "ExternalEffectRequest replay artifact program_ref disagrees with declaration"
            )
        if self.replay_artifact.source_key is None:
            raise ContinuationReplayError("ExternalEffectRequest replay artifact requires source_key")
        object.__setattr__(self, "trace_prefix", tuple(self.trace_prefix))

    @property
    def source_key(self) -> str:
        source_key = self.replay_artifact.source_key
        if source_key is None:
            raise ContinuationReplayError("ExternalEffectRequest replay artifact requires source_key")
        return source_key

    @property
    def declaration_ref(self) -> Ref:
        return self.declaration.ref

    @property
    def program_ref(self) -> Ref | None:
        return self.declaration.program_ref

    @property
    def effect_kind(self) -> str:
        return self.declaration.effect_kind

    @property
    def payload(self) -> JsonValue:
        return self.declaration.payload

    @property
    def payload_schema_ref(self) -> Ref | None:
        return self.declaration.payload_schema_ref

    @property
    def operation_result_schema_ref(self) -> Ref | None:
        return self.declaration.operation_result_schema_ref

    @property
    def replay_artifact_ref(self) -> Ref:
        return continuation_replay_artifact_ref(self.replay_artifact)

    @property
    def root_ref(self) -> Ref:
        return self.replay_artifact.root_ref

    @property
    def descriptor(self) -> ExternalEffectRequestDescriptor:
        return _external_effect_request_descriptor(self)


@dataclass(frozen=True)
class ExternalEffectRequestRef:
    """Compact durable external request payload backed by a journal artifact catalog."""

    declaration: EffectDeclaration
    artifact_ref: Ref
    artifact_record: ContinuationReplayArtifactRecord
    trace_prefix: tuple[TraceRecord, ...] = ()
    request_schema_version: str = EXTERNAL_EFFECT_REQUEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.request_schema_version != EXTERNAL_EFFECT_REQUEST_SCHEMA_VERSION:
            raise ContinuationReplayError(
                f"ExternalEffectRequestRef.request_schema_version must be {EXTERNAL_EFFECT_REQUEST_SCHEMA_VERSION!r}"
            )
        if not isinstance(self.declaration, EffectDeclaration):
            raise TypeError("ExternalEffectRequestRef.declaration must be an EffectDeclaration")
        if not isinstance(self.artifact_record, ContinuationReplayArtifactRecord):
            raise TypeError("ExternalEffectRequestRef.artifact_record must be a ContinuationReplayArtifactRecord")
        _require_json_compatible(self.declaration.payload, "ExternalEffectRequestRef.declaration.payload")
        artifact_ref = _require_str(self.artifact_ref, "ExternalEffectRequestRef.artifact_ref")
        if artifact_ref != continuation_replay_artifact_record_ref(self.artifact_record):
            raise ContinuationReplayError("ExternalEffectRequestRef artifact_ref does not match artifact record")
        if self.artifact_record.source_record_type != "EffectDeclaration":
            raise ContinuationReplayError("ExternalEffectRequestRef artifact must come from an EffectDeclaration")
        if self.artifact_record.source_ref != self.declaration.ref:
            raise ContinuationReplayError("ExternalEffectRequestRef artifact source_ref disagrees with declaration")
        if self.artifact_record.effect_kind != self.declaration.effect_kind:
            raise ContinuationReplayError("ExternalEffectRequestRef artifact effect_kind disagrees with declaration")
        if self.artifact_record.operation_result_schema_ref != self.declaration.operation_result_schema_ref:
            raise ContinuationReplayError(
                "ExternalEffectRequestRef artifact operation_result_schema_ref disagrees with declaration"
            )
        if (
            self.declaration.program_ref is not None
            and self.artifact_record.program_ref is not None
            and self.artifact_record.program_ref != self.declaration.program_ref
        ):
            raise ContinuationReplayError("ExternalEffectRequestRef artifact program_ref disagrees with declaration")
        if self.artifact_record.source_key is None:
            raise ContinuationReplayError("ExternalEffectRequestRef artifact requires source_key")
        object.__setattr__(self, "artifact_ref", artifact_ref)
        object.__setattr__(self, "trace_prefix", tuple(self.trace_prefix))

    @property
    def source_key(self) -> str:
        source_key = self.artifact_record.source_key
        if source_key is None:
            raise ContinuationReplayError("ExternalEffectRequestRef artifact requires source_key")
        return source_key

    @property
    def declaration_ref(self) -> Ref:
        return self.declaration.ref

    @property
    def program_ref(self) -> Ref | None:
        return self.declaration.program_ref

    @property
    def effect_kind(self) -> str:
        return self.declaration.effect_kind

    @property
    def payload(self) -> JsonValue:
        return self.declaration.payload

    @property
    def payload_schema_ref(self) -> Ref | None:
        return self.declaration.payload_schema_ref

    @property
    def operation_result_schema_ref(self) -> Ref | None:
        return self.declaration.operation_result_schema_ref

    @property
    def root_ref(self) -> Ref:
        return self.artifact_record.root_ref

    @property
    def replay_artifact_ref(self) -> Ref:
        return self.artifact_ref

    @property
    def descriptor(self) -> ExternalEffectRequestDescriptor:
        return _external_effect_request_descriptor(self)


ReplayableExternalEffectRequest: TypeAlias = ExternalEffectRequest | ExternalEffectRequestRef
ReplayableKernelResult: TypeAlias = ReplayableCompleted | ReplayableRejected | ReplayableExternalEffectRequest


@dataclass(frozen=True)
class ReplayableKernelTransition:
    """Validated durable envelope for one replayable kernel transition.

    `context_ref_map`, `continuation_ref_map`, and `continuation_control_ref_map`
    expose the runtime's `<kind>:runtime:N` → canonical `<kind>:sha256:HEX`
    mappings needed by the conformance projection
    (`semantic_batch_from_transition(...)`). The maps are operational data
    only: they do not enter the `transition_id` content hash and do not
    appear on `SemanticTransitionBatch`. The runtime populates them from
    `_KernelRuntimeServices._{context,continuation,continuation_control}_ref_map`
    at transition-construction time when sidecar-evidence mode is enabled.
    Trace-mode transitions emit canonical refs inline; their maps are
    empty and the projection's pre-passes become no-ops.
    """

    transition_id: Ref
    program_ref: Ref
    status: ReplayableKernelTransitionStatus
    payload: ReplayableKernelResult
    trace_delta: tuple[TraceRecord, ...]
    resume_observation_ref: Ref | None = None
    parent_transition_refs: tuple[Ref, ...] = ()
    context_ref_map: Mapping[Ref, Ref] = field(default_factory=lambda: MappingProxyType({}))
    continuation_ref_map: Mapping[Ref, Ref] = field(default_factory=lambda: MappingProxyType({}))
    continuation_control_ref_map: Mapping[Ref, Ref] = field(default_factory=lambda: MappingProxyType({}))
    transition_schema_version: str = REPLAYABLE_KERNEL_TRANSITION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.transition_schema_version != REPLAYABLE_KERNEL_TRANSITION_SCHEMA_VERSION:
            raise ContinuationReplayError(
                "ReplayableKernelTransition.transition_schema_version must be "
                f"{REPLAYABLE_KERNEL_TRANSITION_SCHEMA_VERSION!r}"
            )
        for attr_name in ("context_ref_map", "continuation_ref_map", "continuation_control_ref_map"):
            mapping = getattr(self, attr_name)
            if not isinstance(mapping, Mapping):
                raise ContinuationReplayError(
                    f"ReplayableKernelTransition.{attr_name} must be a mapping"
                )
            for key, value in mapping.items():
                _require_str(key, f"ReplayableKernelTransition.{attr_name} key")
                _require_str(value, f"ReplayableKernelTransition.{attr_name} value")
            object.__setattr__(self, attr_name, MappingProxyType(dict(mapping)))
        status = _require_str(self.status, "ReplayableKernelTransition.status")
        if status not in _REPLAYABLE_KERNEL_TRANSITION_STATUSES:
            raise ContinuationReplayError(f"unknown ReplayableKernelTransition.status: {status!r}")
        if status == "completed" and not isinstance(self.payload, ReplayableCompleted):
            raise ContinuationReplayError("completed replay transition requires ReplayableCompleted payload")
        if status == "external-effect-request" and not isinstance(
            self.payload,
            ExternalEffectRequest | ExternalEffectRequestRef,
        ):
            raise ContinuationReplayError(
                "external-effect-request replay transition requires external request payload"
            )
        if status == "rejected" and not isinstance(self.payload, ReplayableRejected):
            raise ContinuationReplayError("rejected replay transition requires ReplayableRejected payload")
        trace_delta = tuple(self.trace_delta)
        expected_trace_delta = _payload_trace_delta(self.payload)
        if trace_delta != expected_trace_delta:
            raise ContinuationReplayError("ReplayableKernelTransition trace_delta disagrees with payload trace")
        program_ref = _require_str(self.program_ref, "ReplayableKernelTransition.program_ref")
        payload_program_ref = _payload_program_ref(self.payload)
        if payload_program_ref is not None and program_ref != payload_program_ref:
            raise ContinuationReplayError("ReplayableKernelTransition program_ref disagrees with payload")
        parent_transition_refs = tuple(
            _require_str(ref, "ReplayableKernelTransition.parent_transition_refs") for ref in self.parent_transition_refs
        )
        resume_observation_ref = _optional_str(
            self.resume_observation_ref,
            "ReplayableKernelTransition.resume_observation_ref",
        )
        if parent_transition_refs and resume_observation_ref is None:
            raise ContinuationReplayError("resumed replay transition requires resume_observation_ref")
        if status == "rejected" and len(parent_transition_refs) != 1:
            raise ContinuationReplayError("rejected replay transition requires exactly one parent transition")
        expected_transition_id = _replayable_transition_id(
            program_ref=program_ref,
            status=cast("ReplayableKernelTransitionStatus", status),
            parent_transition_refs=parent_transition_refs,
            resume_observation_ref=resume_observation_ref,
            payload=self.payload,
            trace_delta=trace_delta,
        )
        transition_id = _require_str(self.transition_id, "ReplayableKernelTransition.id")
        if transition_id != expected_transition_id:
            raise ContinuationReplayError("ReplayableKernelTransition transition_id does not match canonical payload")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "transition_id", transition_id)
        object.__setattr__(self, "program_ref", program_ref)
        object.__setattr__(self, "resume_observation_ref", resume_observation_ref)
        object.__setattr__(self, "parent_transition_refs", parent_transition_refs)
        object.__setattr__(self, "trace_delta", tuple(self.trace_delta))


@dataclass(frozen=True)
class ContinuationReplayArtifactRecord:
    """Compact catalog record for a replay artifact.

    The durable journal stores continuation object payloads once in a shared
    catalog. Artifact records identify the root plus source metadata needed to
    materialize the host-facing `ContinuationReplayArtifact` from that catalog.
    """

    root_ref: Ref
    program_ref: Ref | None = None
    source_key: str | None = None
    source_ref: Ref | None = None
    source_record_type: SourceRecordType | None = None
    effect_kind: str | None = None
    operation_result_schema_ref: Ref | None = None
    artifact_schema_version: str = CONTINUATION_REPLAY_ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.artifact_schema_version != CONTINUATION_REPLAY_ARTIFACT_SCHEMA_VERSION:
            raise ContinuationReplayError(
                "ContinuationReplayArtifactRecord.artifact_schema_version must be "
                f"{CONTINUATION_REPLAY_ARTIFACT_SCHEMA_VERSION!r}"
            )
        object.__setattr__(self, "root_ref", _require_str(self.root_ref, "ContinuationReplayArtifactRecord.root_ref"))
        _require_optional_str(self.program_ref, "ContinuationReplayArtifactRecord.program_ref")
        _require_optional_str(self.source_key, "ContinuationReplayArtifactRecord.source_key")
        _require_optional_str(self.source_ref, "ContinuationReplayArtifactRecord.source_ref")
        _require_optional_str(self.effect_kind, "ContinuationReplayArtifactRecord.effect_kind")
        _require_optional_str(
            self.operation_result_schema_ref,
            "ContinuationReplayArtifactRecord.operation_result_schema_ref",
        )
        if self.source_record_type is not None and self.source_record_type not in _SOURCE_RECORD_TYPES:
            raise ContinuationReplayError(
                f"unknown ContinuationReplayArtifactRecord.source_record_type: {self.source_record_type!r}"
            )
        if (self.source_ref is None) != (self.source_record_type is None):
            raise ContinuationReplayError("ContinuationReplayArtifactRecord source_ref and source_record_type must pair")


@dataclass(frozen=True)
class OpenReplayRequest:
    """Capability record for an external request emitted by replay state."""

    source_key: str
    request_ref: Ref
    request_transition_ref: Ref
    program_ref: Ref
    declaration_ref: Ref
    effect_kind: str
    root_ref: Ref
    operation_result_schema_ref: Ref | None
    trace_prefix_ref: Ref

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_key", _require_str(self.source_key, "OpenReplayRequest.source_key"))
        object.__setattr__(self, "request_ref", _require_str(self.request_ref, "OpenReplayRequest.request_ref"))
        object.__setattr__(
            self,
            "request_transition_ref",
            _require_str(self.request_transition_ref, "OpenReplayRequest.request_transition_ref"),
        )
        object.__setattr__(self, "program_ref", _require_str(self.program_ref, "OpenReplayRequest.program_ref"))
        object.__setattr__(
            self,
            "declaration_ref",
            _require_str(self.declaration_ref, "OpenReplayRequest.declaration_ref"),
        )
        object.__setattr__(self, "effect_kind", _require_str(self.effect_kind, "OpenReplayRequest.effect_kind"))
        object.__setattr__(self, "root_ref", _require_str(self.root_ref, "OpenReplayRequest.root_ref"))
        _require_optional_str(
            self.operation_result_schema_ref,
            "OpenReplayRequest.operation_result_schema_ref",
        )
        object.__setattr__(
            self,
            "trace_prefix_ref",
            _require_str(self.trace_prefix_ref, "OpenReplayRequest.trace_prefix_ref"),
        )


@dataclass(frozen=True)
class KernelReplayState:
    """In-memory replay ledger for the minimal runtime-shaped boundary.

    Carries `profile: SemanticProfile` per 2026-05-24 §"Post-#72 design pass"
    item A. `state.profile` must agree with `state.prepared_program.profile`;
    the post-init validates the agreement so callers cannot drift the two.
    """

    prepared_program: PreparedKernelProgram
    program_ref: Ref
    profile: SemanticProfile
    open_requests: Mapping[str, OpenReplayRequest] = field(default_factory=dict)
    consumed_source_keys: tuple[str, ...] = ()
    transition_refs: tuple[Ref, ...] = ()
    trace: tuple[TraceRecord, ...] = ()
    terminal: bool = False
    rejected: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.prepared_program, PreparedKernelProgram):
            raise TypeError("KernelReplayState.prepared_program must be a PreparedKernelProgram")
        if not isinstance(self.profile, SemanticProfile):
            raise TypeError("KernelReplayState.profile must be a SemanticProfile")
        if self.profile != self.prepared_program.profile:
            raise ContinuationReplayError(
                f"KernelReplayState.profile {self.profile.name!r} disagrees with "
                f"prepared_program.profile {self.prepared_program.profile.name!r}"
            )
        program_ref = _require_str(self.program_ref, "KernelReplayState.program_ref")
        prepared_program_ref = project_program_identity(self.prepared_program).program_ref
        if prepared_program_ref != program_ref:
            raise ContinuationReplayError("KernelReplayState program_ref disagrees with prepared program")
        open_requests: dict[str, OpenReplayRequest] = {}
        for source_key, request in self.open_requests.items():
            source_key = _require_str(source_key, "KernelReplayState.open_requests.source_key")
            if not isinstance(request, OpenReplayRequest):
                raise TypeError("KernelReplayState.open_requests values must be OpenReplayRequest")
            if request.source_key != source_key:
                raise ContinuationReplayError("KernelReplayState open request key disagrees with source_key")
            if request.program_ref != program_ref:
                raise ContinuationReplayError("KernelReplayState open request program_ref disagrees with state")
            open_requests[source_key] = request
        consumed_source_keys = tuple(
            _require_str(key, "KernelReplayState.consumed_source_keys") for key in self.consumed_source_keys
        )
        if len(set(consumed_source_keys)) != len(consumed_source_keys):
            raise ContinuationReplayError("KernelReplayState consumed_source_keys must be unique")
        if set(consumed_source_keys).intersection(open_requests):
            raise ContinuationReplayError("KernelReplayState source cannot be both open and consumed")
        transition_refs = tuple(_require_str(ref, "KernelReplayState.transition_refs") for ref in self.transition_refs)
        if len(set(transition_refs)) != len(transition_refs):
            raise ContinuationReplayError("KernelReplayState transition_refs must be unique")
        if len(open_requests) > 1:
            raise ContinuationReplayError("KernelReplayState supports exactly one open request")
        transition_ref_set = set(transition_refs)
        for request in open_requests.values():
            if request.request_transition_ref not in transition_ref_set:
                raise ContinuationReplayError("KernelReplayState open request transition is not in transition_refs")
            if not transition_refs:
                raise ContinuationReplayError("KernelReplayState open request requires transition_refs")
            if request.request_transition_ref != transition_refs[-1]:
                raise ContinuationReplayError("KernelReplayState open request transition must be the current frontier")
        if self.terminal and self.rejected:
            raise ContinuationReplayError("KernelReplayState cannot be both terminal and rejected")
        if (self.terminal or self.rejected) and open_requests:
            raise ContinuationReplayError("KernelReplayState terminal or rejected state cannot have open requests")
        object.__setattr__(self, "program_ref", program_ref)
        object.__setattr__(self, "open_requests", MappingProxyType(open_requests))
        object.__setattr__(self, "consumed_source_keys", consumed_source_keys)
        object.__setattr__(self, "transition_refs", transition_refs)
        object.__setattr__(self, "trace", tuple(self.trace))

    @property
    def open_source_keys(self) -> tuple[str, ...]:
        return tuple(self.open_requests)


@dataclass(frozen=True)
class KernelReplayJournal:
    """Validated sequential replay transition log.

    `KernelReplayState.trace` is a diagnostic snapshot when state is persisted
    directly. A journal is the authoritative replay history: state can be
    reconstructed from the validated transition envelopes instead of trusting
    the serialized snapshot.
    """

    program_ref: Ref
    transitions: tuple[ReplayableKernelTransition, ...]
    continuation_objects: Mapping[Ref, ContinuationObject] = field(default_factory=dict)
    artifacts: Mapping[Ref, ContinuationReplayArtifactRecord] = field(default_factory=dict)
    journal_schema_version: str = KERNEL_REPLAY_JOURNAL_SCHEMA_VERSION
    _catalog: ReplayArtifactCatalog = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.journal_schema_version != KERNEL_REPLAY_JOURNAL_SCHEMA_VERSION:
            raise ContinuationReplayError(
                f"KernelReplayJournal.journal_schema_version must be {KERNEL_REPLAY_JOURNAL_SCHEMA_VERSION!r}"
            )
        program_ref = _require_str(self.program_ref, "KernelReplayJournal.program_ref")
        transitions = tuple(self.transitions)
        if not transitions:
            raise ContinuationReplayError("KernelReplayJournal transitions must not be empty")
        derived_objects, derived_artifacts = _catalogs_from_transitions(transitions)
        continuation_objects = _validated_continuation_object_catalog(
            self.continuation_objects,
            required=derived_objects,
        )
        artifacts = _validated_artifact_catalog(
            self.artifacts,
            continuation_objects=continuation_objects,
            required=derived_artifacts,
        )
        transition_refs: set[Ref] = set()
        previous_ref: Ref | None = None
        previous_transition: ReplayableKernelTransition | None = None
        for index, transition in enumerate(transitions):
            if not isinstance(transition, ReplayableKernelTransition):
                raise TypeError("KernelReplayJournal.transitions must be ReplayableKernelTransition values")
            if transition.program_ref != program_ref:
                raise ContinuationReplayError("KernelReplayJournal transition program_ref disagrees with journal")
            if transition.transition_id in transition_refs:
                raise ContinuationReplayError("KernelReplayJournal transition_id must be unique")
            if index == 0:
                if transition.parent_transition_refs:
                    raise ContinuationReplayError("KernelReplayJournal first transition must not have parents")
            elif transition.parent_transition_refs != (previous_ref,):
                raise ContinuationReplayError("KernelReplayJournal transition parent must be the previous frontier")
            if isinstance(transition.payload, ReplayableCompleted | ReplayableRejected) and index != len(
                transitions
            ) - 1:
                raise ContinuationReplayError("KernelReplayJournal terminal or rejected transition must be last")
            if isinstance(transition.payload, ReplayableRejected) and (
                previous_transition is None
                or not isinstance(previous_transition.payload, ExternalEffectRequest | ExternalEffectRequestRef)
            ):
                raise ContinuationReplayError("KernelReplayJournal rejected transition must close an external request")
            if (
                isinstance(transition.payload, ReplayableRejected)
                and previous_transition is not None
                and isinstance(previous_transition.payload, ExternalEffectRequest | ExternalEffectRequestRef)
            ):
                previous_request = previous_transition.payload
                if transition.payload.source_key != previous_request.source_key:
                    raise ContinuationReplayError("KernelReplayJournal rejected transition source_key disagrees")
                if transition.payload.request_ref != _external_effect_request_ref(previous_request):
                    raise ContinuationReplayError("KernelReplayJournal rejected transition request_ref disagrees")
            transition_refs.add(transition.transition_id)
            previous_ref = transition.transition_id
            previous_transition = transition
        object.__setattr__(self, "program_ref", program_ref)
        object.__setattr__(self, "transitions", transitions)
        object.__setattr__(self, "continuation_objects", MappingProxyType(continuation_objects))
        object.__setattr__(self, "artifacts", MappingProxyType(artifacts))
        object.__setattr__(
            self,
            "_catalog",
            ReplayArtifactCatalog._from_validated(
                continuation_objects=continuation_objects,
                artifacts=artifacts,
            ),
        )

    @classmethod
    def from_transitions(
        cls,
        *,
        program_ref: Ref,
        transitions: tuple[ReplayableKernelTransition, ...],
        continuation_objects: Mapping[Ref, ContinuationObject] | None = None,
        artifacts: Mapping[Ref, ContinuationReplayArtifactRecord] | None = None,
    ) -> KernelReplayJournal:
        return cls(
            program_ref=program_ref,
            transitions=transitions,
            continuation_objects={} if continuation_objects is None else continuation_objects,
            artifacts={} if artifacts is None else artifacts,
        )

    @property
    def catalog(self) -> ReplayArtifactCatalog:
        return self._catalog


class KernelReplayRejected(ContinuationReplayError):
    """Raised when an admitted host observation consumes a source but cannot resume."""

    def __init__(
        self,
        message: str,
        *,
        state: KernelReplayState,
        transition: ReplayableKernelTransition,
        reason: BaseException,
    ) -> None:
        super().__init__(message)
        self.state = state
        self.transition = transition
        self.reason = reason


@dataclass(frozen=True)
class ContinuationReplayArtifact:
    """Executable restart artifact for a serialized value-position continuation.

    `source_key` is the lifecycle key used by `ContinuationReplayLedger`.
    `source_ref`, `source_record_type`, and `effect_kind` are diagnostic/carrier
    metadata; this artifact does not certify trace provenance. `program_ref` and
    `operation_result_schema_ref` are optional cross-checks against the
    content-addressed `ContinuationRoot`.
    """

    root_ref: Ref
    continuation_objects: Mapping[Ref, ContinuationObject]
    program_ref: Ref | None = None
    source_key: str | None = None
    source_ref: Ref | None = None
    source_record_type: SourceRecordType | None = None
    effect_kind: str | None = None
    operation_result_schema_ref: Ref | None = None
    artifact_schema_version: str = CONTINUATION_REPLAY_ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.artifact_schema_version != CONTINUATION_REPLAY_ARTIFACT_SCHEMA_VERSION:
            raise ContinuationReplayError(
                "ContinuationReplayArtifact.artifact_schema_version must be "
                f"{CONTINUATION_REPLAY_ARTIFACT_SCHEMA_VERSION!r}"
            )
        _require_optional_str(self.program_ref, "ContinuationReplayArtifact.program_ref")
        _require_optional_str(self.source_key, "ContinuationReplayArtifact.source_key")
        _require_optional_str(self.source_ref, "ContinuationReplayArtifact.source_ref")
        _require_optional_str(self.effect_kind, "ContinuationReplayArtifact.effect_kind")
        _require_optional_str(
            self.operation_result_schema_ref,
            "ContinuationReplayArtifact.operation_result_schema_ref",
        )
        if self.source_record_type is not None and self.source_record_type not in _SOURCE_RECORD_TYPES:
            raise ContinuationReplayError(
                f"unknown ContinuationReplayArtifact.source_record_type: {self.source_record_type!r}"
            )
        if (self.source_ref is None) != (self.source_record_type is None):
            raise ContinuationReplayError("ContinuationReplayArtifact source_ref and source_record_type must pair")

        objects = _reachable_continuation_objects(self.root_ref, self.continuation_objects)
        root = objects[self.root_ref]
        if not isinstance(root, ContinuationRoot):
            raise ContinuationReplayError(f"ContinuationReplayArtifact root {self.root_ref!r} is not a root")
        if root.continuation_kind == "empty-terminal":
            raise ContinuationReplayError("ContinuationReplayArtifact does not support empty-terminal roots")
        if self.program_ref is not None and self.program_ref != root.program_ref:
            raise ContinuationReplayError("ContinuationReplayArtifact program_ref does not match ContinuationRoot")
        if self.operation_result_schema_ref is not None and self.operation_result_schema_ref != root.result_schema_ref:
            raise ContinuationReplayError(
                "ContinuationReplayArtifact operation_result_schema_ref does not match ContinuationRoot"
            )
        source_key = self.source_key
        if self.source_ref is not None and self.source_record_type is not None:
            canonical_source_key = _continuation_source_key(
                root_ref=self.root_ref,
                program_ref=root.program_ref,
                source_ref=self.source_ref,
                source_record_type=self.source_record_type,
                effect_kind=self.effect_kind,
                operation_result_schema_ref=root.result_schema_ref,
            )
            if source_key is not None and source_key != canonical_source_key:
                raise ContinuationReplayError(
                    "ContinuationReplayArtifact source_key does not match canonical continuation source key"
                )
            source_key = canonical_source_key

        object.__setattr__(self, "root_ref", _require_str(self.root_ref, "ContinuationReplayArtifact.root_ref"))
        object.__setattr__(self, "source_key", source_key)
        object.__setattr__(self, "continuation_objects", MappingProxyType(objects))


class ContinuationReplayLedger:
    """In-memory one-shot replay ledger for carrier/runtime integration tests."""

    def __init__(self, consumed_source_keys: Iterable[str] | None = None) -> None:
        self._consumed_source_keys: set[str] = set()
        if consumed_source_keys is not None:
            for source_key in consumed_source_keys:
                self._consumed_source_keys.add(_require_str(source_key, "ContinuationReplayLedger.source_key"))

    @property
    def consumed_source_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._consumed_source_keys))

    def consume(self, source_key: str) -> None:
        source_key = _require_str(source_key, "ContinuationReplayLedger.source_key")
        if source_key in self._consumed_source_keys:
            raise ResumptionUsed(f"continuation replay source {source_key!r} already consumed")
        self._consumed_source_keys.add(source_key)


def continuation_replay_artifact_from_objects(
    root_ref: Ref,
    continuation_objects: Mapping[Ref, ContinuationObject],
    *,
    program_ref: Ref | None = None,
    source_key: str | None = None,
    source_ref: Ref | None = None,
    source_record_type: SourceRecordType | None = None,
    effect_kind: str | None = None,
    operation_result_schema_ref: Ref | None = None,
) -> ContinuationReplayArtifact:
    return ContinuationReplayArtifact(
        root_ref=root_ref,
        continuation_objects=continuation_objects,
        program_ref=program_ref,
        source_key=source_key,
        source_ref=source_ref,
        source_record_type=source_record_type,
        effect_kind=effect_kind,
        operation_result_schema_ref=operation_result_schema_ref,
    )


def continuation_replay_artifact_record_from_artifact(
    artifact: ContinuationReplayArtifact,
) -> ContinuationReplayArtifactRecord:
    return ContinuationReplayArtifactRecord(
        artifact_schema_version=artifact.artifact_schema_version,
        root_ref=artifact.root_ref,
        program_ref=artifact.program_ref,
        source_key=artifact.source_key,
        source_ref=artifact.source_ref,
        source_record_type=artifact.source_record_type,
        effect_kind=artifact.effect_kind,
        operation_result_schema_ref=artifact.operation_result_schema_ref,
    )


def continuation_replay_artifact_ref(artifact: ContinuationReplayArtifact) -> Ref:
    return continuation_replay_artifact_record_ref(continuation_replay_artifact_record_from_artifact(artifact))


def continuation_replay_artifact_record_ref(record: ContinuationReplayArtifactRecord) -> Ref:
    return content_ref("continuation-replay-artifact", continuation_replay_artifact_record_to_json(record))


def continuation_replay_artifact_from_record(
    record: ContinuationReplayArtifactRecord,
    continuation_objects: Mapping[Ref, ContinuationObject],
) -> ContinuationReplayArtifact:
    return ContinuationReplayArtifact(
        artifact_schema_version=record.artifact_schema_version,
        root_ref=record.root_ref,
        program_ref=record.program_ref,
        source_key=record.source_key,
        source_ref=record.source_ref,
        source_record_type=record.source_record_type,
        effect_kind=record.effect_kind,
        operation_result_schema_ref=record.operation_result_schema_ref,
        continuation_objects=continuation_objects,
    )


@dataclass(frozen=True)
class ReplayArtifactCatalog:
    """Validated closure for compact replay artifact refs."""

    continuation_objects: Mapping[Ref, ContinuationObject]
    artifacts: Mapping[Ref, ContinuationReplayArtifactRecord]

    def __post_init__(self) -> None:
        continuation_objects = _validated_continuation_object_catalog(self.continuation_objects, required={})
        artifacts = _validated_artifact_catalog(
            self.artifacts,
            continuation_objects=continuation_objects,
            required={},
        )
        object.__setattr__(self, "continuation_objects", MappingProxyType(continuation_objects))
        object.__setattr__(self, "artifacts", MappingProxyType(artifacts))

    @classmethod
    def _from_validated(
        cls,
        *,
        continuation_objects: Mapping[Ref, ContinuationObject],
        artifacts: Mapping[Ref, ContinuationReplayArtifactRecord],
    ) -> ReplayArtifactCatalog:
        catalog = cls.__new__(cls)
        object.__setattr__(catalog, "continuation_objects", MappingProxyType(dict(continuation_objects)))
        object.__setattr__(catalog, "artifacts", MappingProxyType(dict(artifacts)))
        return catalog

    def require_closed_request(self, request: ExternalEffectRequestRef) -> None:
        record = self.artifacts.get(request.artifact_ref)
        if record is None:
            raise ContinuationReplayError(f"ReplayArtifactCatalog missing artifact {request.artifact_ref!r}")
        if record != request.artifact_record:
            raise ContinuationReplayError("ReplayArtifactCatalog artifact record disagrees with request")
        _validate_artifact_record_root(record, self.continuation_objects)

    def materialize(self, request: ExternalEffectRequestRef) -> ExternalEffectRequest:
        self.require_closed_request(request)
        artifact = continuation_replay_artifact_from_record(
            request.artifact_record,
            _reachable_continuation_objects_from_catalog(request.root_ref, self.continuation_objects),
        )
        executable = ExternalEffectRequest(
            declaration=request.declaration,
            replay_artifact=artifact,
            trace_prefix=request.trace_prefix,
            request_schema_version=request.request_schema_version,
        )
        if _external_effect_request_ref(executable) != _external_effect_request_ref(request):
            raise ContinuationReplayError("ReplayArtifactCatalog materialized request does not match compact ref")
        return executable


def continuation_replay_artifact_record_to_json(
    record: ContinuationReplayArtifactRecord,
) -> dict[str, JsonValue]:
    return {
        "artifact_schema_version": record.artifact_schema_version,
        "root_ref": record.root_ref,
        "program_ref": record.program_ref,
        "source_key": record.source_key,
        "source_ref": record.source_ref,
        "source_record_type": record.source_record_type,
        "effect_kind": record.effect_kind,
        "operation_result_schema_ref": record.operation_result_schema_ref,
    }


def continuation_replay_artifact_record_from_json(
    data: Mapping[str, JsonValue],
) -> ContinuationReplayArtifactRecord:
    try:
        _require_keys(data, _ARTIFACT_RECORD_JSON_KEYS, "ContinuationReplayArtifactRecord")
        return ContinuationReplayArtifactRecord(
            artifact_schema_version=_require_str(
                data["artifact_schema_version"],
                "ContinuationReplayArtifactRecord.artifact_schema_version",
            ),
            root_ref=_require_str(data["root_ref"], "ContinuationReplayArtifactRecord.root_ref"),
            program_ref=_optional_str(data["program_ref"], "ContinuationReplayArtifactRecord.program_ref"),
            source_key=_optional_str(data["source_key"], "ContinuationReplayArtifactRecord.source_key"),
            source_ref=_optional_str(data["source_ref"], "ContinuationReplayArtifactRecord.source_ref"),
            source_record_type=cast(
                "SourceRecordType | None",
                _optional_str(data["source_record_type"], "ContinuationReplayArtifactRecord.source_record_type"),
            ),
            effect_kind=_optional_str(data["effect_kind"], "ContinuationReplayArtifactRecord.effect_kind"),
            operation_result_schema_ref=_optional_str(
                data["operation_result_schema_ref"],
                "ContinuationReplayArtifactRecord.operation_result_schema_ref",
            ),
        )
    except ContinuationReplayError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ContinuationReplaySerializationError(str(exc)) from exc


def continuation_replay_artifact_to_json(artifact: ContinuationReplayArtifact) -> dict[str, JsonValue]:
    return {
        "artifact_schema_version": artifact.artifact_schema_version,
        "root_ref": artifact.root_ref,
        "program_ref": artifact.program_ref,
        "source_key": artifact.source_key,
        "source_ref": artifact.source_ref,
        "source_record_type": artifact.source_record_type,
        "effect_kind": artifact.effect_kind,
        "operation_result_schema_ref": artifact.operation_result_schema_ref,
        "continuation_objects": [
            {"ref": ref, "object": continuation_object_to_json(obj)}
            for ref, obj in sorted(artifact.continuation_objects.items())
        ],
    }


def continuation_replay_artifact_from_json(data: Mapping[str, JsonValue]) -> ContinuationReplayArtifact:
    try:
        _require_keys(data, _ARTIFACT_JSON_KEYS, "ContinuationReplayArtifact")
        entries = _require_sequence(data["continuation_objects"], "ContinuationReplayArtifact.continuation_objects")
        objects: dict[Ref, ContinuationObject] = {}
        for index, item in enumerate(entries):
            entry = _require_mapping(item, f"ContinuationReplayArtifact.continuation_objects[{index}]")
            _require_keys(entry, _OBJECT_ENTRY_JSON_KEYS, f"ContinuationReplayArtifact.continuation_objects[{index}]")
            ref = _require_str(entry["ref"], f"ContinuationReplayArtifact.continuation_objects[{index}].ref")
            objects[ref] = continuation_object_from_json(
                _require_mapping(entry["object"], f"ContinuationReplayArtifact.continuation_objects[{index}].object")
            )
        return ContinuationReplayArtifact(
            artifact_schema_version=_require_str(
                data["artifact_schema_version"],
                "ContinuationReplayArtifact.artifact_schema_version",
            ),
            root_ref=_require_str(data["root_ref"], "ContinuationReplayArtifact.root_ref"),
            program_ref=_optional_str(data["program_ref"], "ContinuationReplayArtifact.program_ref"),
            source_key=_optional_str(data["source_key"], "ContinuationReplayArtifact.source_key"),
            source_ref=_optional_str(data["source_ref"], "ContinuationReplayArtifact.source_ref"),
            source_record_type=cast(
                "SourceRecordType | None",
                _optional_str(data["source_record_type"], "ContinuationReplayArtifact.source_record_type"),
            ),
            effect_kind=_optional_str(data["effect_kind"], "ContinuationReplayArtifact.effect_kind"),
            operation_result_schema_ref=_optional_str(
                data["operation_result_schema_ref"],
                "ContinuationReplayArtifact.operation_result_schema_ref",
            ),
            continuation_objects=objects,
        )
    except ContinuationReplayError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ContinuationReplaySerializationError(str(exc)) from exc


def host_completed_to_json(observation: HostCompleted) -> dict[str, JsonValue]:
    return {
        "observation_schema_version": observation.observation_schema_version,
        "value": observation.value,
        "evidence_refs": list(observation.evidence_refs),
    }


def host_completed_from_json(data: Mapping[str, JsonValue]) -> HostCompleted:
    try:
        _require_keys(data, _HOST_COMPLETED_JSON_KEYS, "HostCompleted")
        return HostCompleted(
            observation_schema_version=_require_str(
                data["observation_schema_version"],
                "HostCompleted.observation_schema_version",
            ),
            value=data["value"],
            evidence_refs=tuple(
                _require_str(ref, "HostCompleted.evidence_refs")
                for ref in _require_sequence(data["evidence_refs"], "HostCompleted.evidence_refs")
            ),
        )
    except ContinuationReplayError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ContinuationReplaySerializationError(str(exc)) from exc


def external_effect_request_to_json(request: ExternalEffectRequest) -> dict[str, JsonValue]:
    return {
        "request_schema_version": request.request_schema_version,
        "declaration": trace_record_to_json(request.declaration),
        "replay_artifact": continuation_replay_artifact_to_json(request.replay_artifact),
        "trace_prefix": trace_to_json(request.trace_prefix),
    }


def external_effect_request_from_json(data: Mapping[str, JsonValue]) -> ExternalEffectRequest:
    try:
        _require_keys(data, _EXTERNAL_EFFECT_REQUEST_JSON_KEYS, "ExternalEffectRequest")
        request_schema_version = _require_str(
            data["request_schema_version"],
            "ExternalEffectRequest.request_schema_version",
        )
        declaration = trace_record_from_json(_require_mapping(data["declaration"], "ExternalEffectRequest.declaration"))
        if not isinstance(declaration, EffectDeclaration):
            raise TypeError("ExternalEffectRequest.declaration must decode to an EffectDeclaration")
        return ExternalEffectRequest(
            request_schema_version=request_schema_version,
            declaration=declaration,
            replay_artifact=continuation_replay_artifact_from_json(
                _require_mapping(data["replay_artifact"], "ExternalEffectRequest.replay_artifact")
            ),
            trace_prefix=trace_from_json(
                [
                    _require_mapping(item, "ExternalEffectRequest.trace_prefix")
                    for item in _require_sequence(data["trace_prefix"], "ExternalEffectRequest.trace_prefix")
                ]
            ),
        )
    except ContinuationReplayError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ContinuationReplaySerializationError(str(exc)) from exc


def replayable_kernel_transition_to_json(transition: ReplayableKernelTransition) -> dict[str, JsonValue]:
    return {
        "transition_schema_version": transition.transition_schema_version,
        "transition_id": transition.transition_id,
        "parent_transition_refs": list(transition.parent_transition_refs),
        "program_ref": transition.program_ref,
        "resume_observation_ref": transition.resume_observation_ref,
        "status": transition.status,
        "payload": _replayable_payload_to_json(transition.payload),
        "trace_delta": trace_to_json(transition.trace_delta),
        "context_ref_map": dict(transition.context_ref_map),
        "continuation_ref_map": dict(transition.continuation_ref_map),
        "continuation_control_ref_map": dict(transition.continuation_control_ref_map),
    }


def _ref_map_from_json(data: JsonValue, context: str) -> dict[Ref, Ref]:
    mapping = _require_mapping(data, context)
    return {
        _require_str(key, f"{context} key"): _require_str(value, f"{context} value")
        for key, value in mapping.items()
    }


def replayable_kernel_transition_from_json(data: Mapping[str, JsonValue]) -> ReplayableKernelTransition:
    try:
        _require_keys(data, _REPLAYABLE_KERNEL_TRANSITION_JSON_KEYS, "ReplayableKernelTransition")
        payload = _replayable_payload_from_json(_require_mapping(data["payload"], "ReplayableKernelTransition.payload"))
        return ReplayableKernelTransition(
            transition_schema_version=_require_str(
                data["transition_schema_version"],
                "ReplayableKernelTransition.transition_schema_version",
            ),
            transition_id=_require_str(data["transition_id"], "ReplayableKernelTransition.transition_id"),
            parent_transition_refs=tuple(
                _require_str(ref, "ReplayableKernelTransition.parent_transition_refs")
                for ref in _require_sequence(
                    data["parent_transition_refs"],
                    "ReplayableKernelTransition.parent_transition_refs",
                )
            ),
            program_ref=_require_str(data["program_ref"], "ReplayableKernelTransition.program_ref"),
            resume_observation_ref=_optional_str(
                data["resume_observation_ref"],
                "ReplayableKernelTransition.resume_observation_ref",
            ),
            status=cast(
                "ReplayableKernelTransitionStatus",
                _require_str(data["status"], "ReplayableKernelTransition.status"),
            ),
            payload=payload,
            trace_delta=trace_from_json(
                [
                    _require_mapping(item, "ReplayableKernelTransition.trace_delta")
                    for item in _require_sequence(data["trace_delta"], "ReplayableKernelTransition.trace_delta")
                ]
            ),
            context_ref_map=_ref_map_from_json(
                data["context_ref_map"], "ReplayableKernelTransition.context_ref_map"
            ),
            continuation_ref_map=_ref_map_from_json(
                data["continuation_ref_map"], "ReplayableKernelTransition.continuation_ref_map"
            ),
            continuation_control_ref_map=_ref_map_from_json(
                data["continuation_control_ref_map"],
                "ReplayableKernelTransition.continuation_control_ref_map",
            ),
        )
    except ContinuationReplayError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ContinuationReplaySerializationError(str(exc)) from exc


def kernel_replay_state_to_json(state: KernelReplayState) -> dict[str, JsonValue]:
    return {
        "state_schema_version": KERNEL_REPLAY_STATE_SCHEMA_VERSION,
        "program_ref": state.program_ref,
        "profile": state.profile.name,
        "open_requests": [
            _open_replay_request_to_json(request) for _source_key, request in sorted(state.open_requests.items())
        ],
        "consumed_source_keys": list(state.consumed_source_keys),
        "transition_refs": list(state.transition_refs),
        "trace": trace_to_json(state.trace),
        "terminal": state.terminal,
        "rejected": state.rejected,
    }


def kernel_replay_state_from_json(
    program: KernelProgramInput,
    data: Mapping[str, JsonValue],
) -> KernelReplayState:
    try:
        _require_keys(data, _KERNEL_REPLAY_STATE_JSON_KEYS, "KernelReplayState")
        state_schema_version = _require_str(data["state_schema_version"], "KernelReplayState.state_schema_version")
        if state_schema_version != KERNEL_REPLAY_STATE_SCHEMA_VERSION:
            raise ContinuationReplayError(
                f"KernelReplayState.state_schema_version must be {KERNEL_REPLAY_STATE_SCHEMA_VERSION!r}"
            )
        profile_name = _require_str(data["profile"], "KernelReplayState.profile")
        from shepherd_kernel_v3_reference.profiles import lookup_profile
        try:
            profile = lookup_profile(profile_name)
        except KeyError as exc:
            raise ContinuationReplayError(str(exc)) from exc
        prepared = ensure_prepared_kernel_program(program, profile=profile)
        program_ref = _require_str(data["program_ref"], "KernelReplayState.program_ref")
        prepared_program_ref = project_program_identity(prepared).program_ref
        if program_ref != prepared_program_ref:
            raise ContinuationReplayError("KernelReplayState program_ref disagrees with prepared program")
        open_requests: dict[str, OpenReplayRequest] = {}
        for item in _require_sequence(data["open_requests"], "KernelReplayState.open_requests"):
            request = _open_replay_request_from_json(_require_mapping(item, "KernelReplayState.open_requests"))
            if request.source_key in open_requests:
                raise ContinuationReplayError("KernelReplayState open request source_key must be unique")
            open_requests[request.source_key] = request
        return KernelReplayState(
            prepared_program=prepared,
            program_ref=program_ref,
            profile=prepared.profile,
            open_requests=open_requests,
            consumed_source_keys=tuple(
                _require_str(key, "KernelReplayState.consumed_source_keys")
                for key in _require_sequence(data["consumed_source_keys"], "KernelReplayState.consumed_source_keys")
            ),
            transition_refs=tuple(
                _require_str(ref, "KernelReplayState.transition_refs")
                for ref in _require_sequence(data["transition_refs"], "KernelReplayState.transition_refs")
            ),
            trace=trace_from_json(
                [
                    _require_mapping(item, "KernelReplayState.trace")
                    for item in _require_sequence(data["trace"], "KernelReplayState.trace")
                ]
            ),
            terminal=_require_bool(data["terminal"], "KernelReplayState.terminal"),
            rejected=_require_bool(data["rejected"], "KernelReplayState.rejected"),
        )
    except ContinuationReplayError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ContinuationReplaySerializationError(str(exc)) from exc


def kernel_replay_journal_to_json(journal: KernelReplayJournal) -> dict[str, JsonValue]:
    return {
        "journal_schema_version": journal.journal_schema_version,
        "program_ref": journal.program_ref,
        "continuation_objects": [
            {"ref": ref, "object": continuation_object_to_json(obj)}
            for ref, obj in sorted(journal.continuation_objects.items())
        ],
        "artifacts": [
            {"ref": ref, "artifact": continuation_replay_artifact_record_to_json(record)}
            for ref, record in sorted(journal.artifacts.items())
        ],
        "transitions": [_journal_transition_to_json(transition) for transition in journal.transitions],
    }


def kernel_replay_journal_from_json(data: Mapping[str, JsonValue]) -> KernelReplayJournal:
    try:
        _require_keys(data, _KERNEL_REPLAY_JOURNAL_JSON_KEYS, "KernelReplayJournal")
        journal_schema_version = _require_str(
            data["journal_schema_version"],
            "KernelReplayJournal.journal_schema_version",
        )
        if journal_schema_version != KERNEL_REPLAY_JOURNAL_SCHEMA_VERSION:
            raise ContinuationReplayError(
                f"KernelReplayJournal.journal_schema_version must be {KERNEL_REPLAY_JOURNAL_SCHEMA_VERSION!r}"
            )
        continuation_objects: dict[Ref, ContinuationObject] = {}
        for index, item in enumerate(_require_sequence(data["continuation_objects"], "KernelReplayJournal.objects")):
            entry = _require_mapping(item, f"KernelReplayJournal.continuation_objects[{index}]")
            _require_keys(entry, _OBJECT_ENTRY_JSON_KEYS, f"KernelReplayJournal.continuation_objects[{index}]")
            ref = _require_str(entry["ref"], f"KernelReplayJournal.continuation_objects[{index}].ref")
            if ref in continuation_objects:
                raise ContinuationReplayError("KernelReplayJournal continuation object refs must be unique")
            obj = continuation_object_from_json(
                _require_mapping(entry["object"], f"KernelReplayJournal.continuation_objects[{index}].object")
            )
            actual_ref = continuation_object_ref(obj)
            if ref != actual_ref:
                raise ContinuationReplayError(
                    f"KernelReplayJournal continuation object ref mismatch for {ref!r}: "
                    f"payload hashes to {actual_ref!r}"
                )
            continuation_objects[ref] = obj
        artifacts: dict[Ref, ContinuationReplayArtifactRecord] = {}
        for index, item in enumerate(_require_sequence(data["artifacts"], "KernelReplayJournal.artifacts")):
            entry = _require_mapping(item, f"KernelReplayJournal.artifacts[{index}]")
            _require_keys(entry, _ARTIFACT_ENTRY_JSON_KEYS, f"KernelReplayJournal.artifacts[{index}]")
            ref = _require_str(entry["ref"], f"KernelReplayJournal.artifacts[{index}].ref")
            if ref in artifacts:
                raise ContinuationReplayError("KernelReplayJournal artifact refs must be unique")
            record = continuation_replay_artifact_record_from_json(
                _require_mapping(entry["artifact"], f"KernelReplayJournal.artifacts[{index}].artifact")
            )
            actual_ref = continuation_replay_artifact_record_ref(record)
            if ref != actual_ref:
                raise ContinuationReplayError(
                    f"KernelReplayJournal artifact ref mismatch for {ref!r}: payload hashes to {actual_ref!r}"
                )
            artifacts[ref] = record
        return KernelReplayJournal(
            program_ref=_require_str(data["program_ref"], "KernelReplayJournal.program_ref"),
            transitions=tuple(
                _journal_transition_from_json(
                    _require_mapping(item, "KernelReplayJournal.transitions"),
                    artifacts=artifacts,
                )
                for item in _require_sequence(data["transitions"], "KernelReplayJournal.transitions")
            ),
            continuation_objects=continuation_objects,
            artifacts=artifacts,
            journal_schema_version=journal_schema_version,
        )
    except ContinuationReplayError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ContinuationReplaySerializationError(str(exc)) from exc


def kernel_replay_state_from_journal(
    program: KernelProgramInput,
    journal: KernelReplayJournal,
) -> KernelReplayState:
    prepared = ensure_prepared_kernel_program(program)
    prepared_program_ref = project_program_identity(prepared).program_ref
    if journal.program_ref != prepared_program_ref:
        raise ContinuationReplayError("KernelReplayJournal program_ref disagrees with prepared program")
    state: KernelReplayState | None = None
    transitions_by_id: dict[Ref, ReplayableKernelTransition] = {}
    for transition in journal.transitions:
        consumed_source_keys = state.consumed_source_keys if state is not None else ()
        closed_source_key: str | None = None
        if transition.parent_transition_refs:
            parent_transition = transitions_by_id[transition.parent_transition_refs[0]]
            if isinstance(parent_transition.payload, ExternalEffectRequest | ExternalEffectRequestRef):
                closed_source_key = parent_transition.payload.source_key
                consumed_source_keys = _append_consumed_source_key(consumed_source_keys, closed_source_key)
        state = _state_after_transition(
            prepared=prepared,
            previous=state,
            transition=transition,
            consumed_source_keys=consumed_source_keys,
            closed_source_key=closed_source_key,
        )
        transitions_by_id[transition.transition_id] = transition
    if state is None:
        raise ContinuationReplayError("KernelReplayJournal transitions must not be empty")
    return state


def kernel_replay_journal_current_request(journal: KernelReplayJournal) -> ExternalEffectRequest | None:
    """Materialize the current executable request from a validated replay journal."""

    transition = journal.transitions[-1]
    payload = transition.payload
    if isinstance(payload, ReplayableCompleted | ReplayableRejected):
        return None
    if isinstance(payload, ExternalEffectRequest):
        return payload
    if isinstance(payload, ExternalEffectRequestRef):
        return journal.catalog.materialize(payload)
    raise ContinuationReplayError(f"unsupported replayable kernel payload: {type(payload).__name__}")


def kernel_replay_journal_current_request_descriptor(
    journal: KernelReplayJournal,
) -> ExternalEffectRequestDescriptor | None:
    """Return the host-facing current request without materializing executable replay state."""

    transition = journal.transitions[-1]
    payload = transition.payload
    if isinstance(payload, ReplayableCompleted | ReplayableRejected):
        return None
    if isinstance(payload, ExternalEffectRequest | ExternalEffectRequestRef):
        return payload.descriptor
    raise ContinuationReplayError(f"unsupported replayable kernel payload: {type(payload).__name__}")


def resume_kernel_replay_from_journal(
    program: KernelProgramInput,
    journal: KernelReplayJournal,
    observation: HostCompleted,
    *,
    registry: EffectRegistry | None = None,
) -> tuple[KernelReplayState, ReplayableKernelTransition]:
    """Resume the open request described by a replay journal."""

    request = kernel_replay_journal_current_request(journal)
    if request is None:
        raise ContinuationReplayError("KernelReplayJournal has no open external effect request")
    state = kernel_replay_state_from_journal(program, journal)
    return resume_kernel_replay(state, request, observation, registry=registry)


def start_replayable_kernel_run(
    program: KernelProgramInput,
    env: Env | None = None,
    registry: EffectRegistry | None = None,
) -> ReplayableKernelResult:
    return start_replayable_kernel_transition(program, env=env, registry=registry).payload


def start_replayable_kernel_transition(
    program: KernelProgramInput,
    env: Env | None = None,
    registry: EffectRegistry | None = None,
) -> ReplayableKernelTransition:
    result = run_trace(program, env=env, registry=registry, include_debug_evidence=True)
    payload = _replayable_payload_from_trace_result(result)
    if isinstance(result.outcome, Delayed | Forked):
        raise ContinuationReplayError(
            f"replayable kernel run does not support {type(result.outcome).__name__} outcomes"
        )
    evidence = result.require_debug_evidence()
    return _replayable_transition(
        program_ref=evidence.program_ref,
        payload=payload,
        trace_delta=result.trace,
        context_ref_map=evidence.context_ref_map,
        continuation_ref_map=evidence.continuation_ref_map,
        continuation_control_ref_map=evidence.continuation_control_ref_map,
    )


def start_kernel_replay(
    program: KernelProgramInput,
    env: Env | None = None,
    registry: EffectRegistry | None = None,
) -> tuple[KernelReplayState, ReplayableKernelTransition]:
    prepared = ensure_prepared_kernel_program(program)
    transition = start_replayable_kernel_transition(prepared, env=env, registry=registry)
    return _state_after_transition(
        prepared=prepared,
        previous=None,
        transition=transition,
    ), transition


class KernelReplaySession:
    """Mutable replay session that keeps evaluator replay caches on the hot path."""

    def __init__(
        self,
        state: KernelReplayState,
        *,
        evaluator: StepKernelEvaluator | None = None,
        records: list[TraceRecord] | None = None,
        transition: ReplayableKernelTransition | None = None,
        outcome: SourceOutcome | None = None,
        transitions: tuple[ReplayableKernelTransition, ...] = (),
    ) -> None:
        if evaluator is None or records is None or transition is None or outcome is None or not transitions:
            raise ContinuationReplayError(
                "KernelReplaySession requires live evaluator state; use KernelReplaySession.start(...)"
            )
        if transitions[-1] != transition:
            raise ContinuationReplayError("KernelReplaySession transition must match the live frontier")
        self.state = state
        self._evaluator = evaluator
        self._records = records
        self._transition = transition
        self._outcome = outcome
        self._transitions = transitions

    @classmethod
    def start(
        cls,
        program: KernelProgramInput,
        env: Env | None = None,
        registry: EffectRegistry | None = None,
    ) -> tuple[KernelReplaySession, ReplayableKernelTransition]:
        prepared = ensure_prepared_kernel_program(program)
        records: list[TraceRecord] = []
        evaluator = StepKernelEvaluator(
            prepared,
            registry=registry,
            event_sink=lambda event: records.append(record_from_event(event)),
            evidence_mode="trace",
        )
        outcome = evaluator.run(env)
        if isinstance(outcome, Delayed | Forked):
            raise ContinuationReplayError(
                f"replayable kernel run does not support {type(outcome).__name__} outcomes"
            )
        transition = _session_replayable_transition(
            evaluator=evaluator,
            outcome=outcome,
            trace_delta=tuple(records),
        )
        state = _state_after_transition(
            prepared=prepared,
            previous=None,
            transition=transition,
        )
        return (
            cls(
                state,
                evaluator=evaluator,
                records=records,
                transition=transition,
                outcome=outcome,
                transitions=(transition,),
            ),
            transition,
        )

    @property
    def transitions(self) -> tuple[ReplayableKernelTransition, ...]:
        return self._transitions

    def current_request(self) -> ReplayableExternalEffectRequest | None:
        if self.state.terminal or self.state.rejected:
            return None
        if self._transition is None:
            return None
        payload = self._transition.payload
        if isinstance(payload, ExternalEffectRequest | ExternalEffectRequestRef):
            return payload
        return None

    def current_request_descriptor(self) -> ExternalEffectRequestDescriptor | None:
        request = self.current_request()
        if request is None:
            return None
        return request.descriptor

    def resume(
        self,
        request: ReplayableExternalEffectRequest,
        observation: HostCompleted,
        *,
        registry: EffectRegistry | None = None,
    ) -> ReplayableKernelTransition:
        if self.state.terminal:
            raise ContinuationReplayError("KernelReplayState is already terminal")
        if self.state.rejected:
            raise ContinuationReplayError("KernelReplayState is rejected")
        current_request = self.current_request()
        if current_request is not None and _external_effect_request_ref(request) == _external_effect_request_ref(
            current_request
        ):
            return self.resume_current(observation, registry=registry)
        if current_request is None:
            raise ContinuationReplayError("KernelReplaySession has no open external effect request")
        raise ContinuationReplayError("KernelReplaySession.resume request does not match current live request")

    def resume_current(
        self,
        observation: HostCompleted,
        *,
        registry: EffectRegistry | None = None,
    ) -> ReplayableKernelTransition:
        if registry is not None and self._evaluator is not None:
            self._evaluator.registry = registry
        if self.state.terminal:
            raise ContinuationReplayError("KernelReplayState is already terminal")
        if self.state.rejected:
            raise ContinuationReplayError("KernelReplayState is rejected")
        if self._evaluator is None or self._outcome is None:
            request = self.current_request()
            if request is None or not isinstance(request, ExternalEffectRequest):
                raise ContinuationReplayError("KernelReplaySession has no executable live request")
            return self.resume(request, observation, registry=registry)
        if not isinstance(self._outcome, Suspended):
            raise ContinuationReplayError("KernelReplaySession has no open external effect request")
        request = self.current_request()
        if request is None:
            raise ContinuationReplayError("KernelReplaySession has no open external effect request")
        if request.source_key in self.state.consumed_source_keys:
            raise ResumptionUsed(f"continuation replay source {request.source_key!r} already consumed")
        open_request = self.state.open_requests.get(request.source_key)
        if open_request is None:
            raise ContinuationReplayError("ExternalEffectRequest source_key is not open in KernelReplayState")
        request_ref = _external_effect_request_ref(request)
        if request_ref != open_request.request_ref:
            raise ContinuationReplayError("ExternalEffectRequest does not match open request in KernelReplayState")

        consumed_source_keys = _append_consumed_source_key(self.state.consumed_source_keys, request.source_key)
        trace_start = len(self._records)
        try:
            outcome = self._outcome.continuation.apply(observation.value)
            trace_delta = tuple(self._records[trace_start:])
            if isinstance(outcome, Delayed | Forked):
                raise ContinuationReplayError(
                    f"replayable external effect resume does not support {type(outcome).__name__} outcomes"
                )
            transition = _session_replayable_transition(
                evaluator=self._evaluator,
                outcome=outcome,
                trace_delta=trace_delta,
                parent_transition_refs=(open_request.request_transition_ref,),
                resume_observation_ref=_host_completed_ref(observation),
            )
        except Exception as exc:
            rejected_transition = _rejected_transition(
                program_ref=self.state.program_ref,
                request=request,
                trace_delta=tuple(self._records[trace_start:]),
                parent_transition_refs=(open_request.request_transition_ref,),
                resume_observation_ref=_host_completed_ref(observation),
                reason=exc,
                context_ref_map=self._evaluator.context_ref_map,
                continuation_ref_map=self._evaluator.continuation_ref_map,
                continuation_control_ref_map=self._evaluator.continuation_control_ref_map,
            )
            rejected_state = _state_after_transition(
                prepared=self.state.prepared_program,
                previous=self.state,
                transition=rejected_transition,
                consumed_source_keys=consumed_source_keys,
                closed_source_key=request.source_key,
            )
            self.state = rejected_state
            self._transition = rejected_transition
            self._transitions = (*self._transitions, rejected_transition)
            raise KernelReplayRejected(
                "KernelReplayState rejected admitted host observation",
                state=rejected_state,
                transition=rejected_transition,
                reason=exc,
            ) from exc
        self.state = _state_after_transition(
            prepared=self.state.prepared_program,
            previous=self.state,
            transition=transition,
            consumed_source_keys=consumed_source_keys,
            closed_source_key=request.source_key,
        )
        self._transition = transition
        self._outcome = outcome
        self._transitions = (*self._transitions, transition)
        return transition

    def to_journal(self) -> KernelReplayJournal:
        continuation_objects: dict[Ref, ContinuationObject] = {}
        artifacts: dict[Ref, ContinuationReplayArtifactRecord] = {}
        root_refs: list[Ref] = []
        if self._evaluator is not None:
            for transition in self._transitions:
                payload = transition.payload
                if not isinstance(payload, ExternalEffectRequestRef):
                    continue
                artifacts[payload.artifact_ref] = payload.artifact_record
                root_refs.append(payload.root_ref)
            continuation_objects.update(_reachable_continuation_objects_from_store_many(self._evaluator, root_refs))
        return KernelReplayJournal(
            program_ref=self.state.program_ref,
            transitions=self._transitions,
            continuation_objects=continuation_objects,
            artifacts=artifacts,
        )


def resume_external_effect_request(
    program: KernelProgramInput,
    request: ExternalEffectRequest,
    observation: HostCompleted,
    *,
    registry: EffectRegistry | None = None,
    ledger: ContinuationReplayLedger | None = None,
    parent_transition_refs: tuple[Ref, ...] = (),
) -> ReplayableKernelResult:
    return resume_replayable_kernel_transition(
        program,
        request,
        observation,
        registry=registry,
        ledger=ledger,
        parent_transition_refs=parent_transition_refs,
    ).payload


def resume_kernel_replay(
    state: KernelReplayState,
    request: ExternalEffectRequest,
    observation: HostCompleted,
    *,
    registry: EffectRegistry | None = None,
) -> tuple[KernelReplayState, ReplayableKernelTransition]:
    if state.terminal:
        raise ContinuationReplayError("KernelReplayState is already terminal")
    if state.rejected:
        raise ContinuationReplayError("KernelReplayState is rejected")
    if not isinstance(request, ExternalEffectRequest):
        raise ContinuationReplayError("resume_kernel_replay requires an executable ExternalEffectRequest")
    if request.source_key in state.consumed_source_keys:
        raise ResumptionUsed(f"continuation replay source {request.source_key!r} already consumed")
    open_request = state.open_requests.get(request.source_key)
    if open_request is None:
        raise ContinuationReplayError("ExternalEffectRequest source_key is not open in KernelReplayState")
    request_ref = _external_effect_request_ref(request)
    if request_ref != open_request.request_ref:
        raise ContinuationReplayError("ExternalEffectRequest does not match open request in KernelReplayState")
    consumed_source_keys = _append_consumed_source_key(state.consumed_source_keys, request.source_key)
    parent_transition_refs = (open_request.request_transition_ref,)
    result = _resume_observation_to_transition(
        state.prepared_program,
        request,
        observation,
        registry=registry,
        ledger=None,
        parent_transition_refs=parent_transition_refs,
    )
    transition = result.transition
    if isinstance(transition.payload, ReplayableRejected):
        rejected_state = _state_after_transition(
            prepared=state.prepared_program,
            previous=state,
            transition=transition,
            consumed_source_keys=consumed_source_keys,
            closed_source_key=request.source_key,
        )
        reason = result.rejection_reason or ContinuationReplayError(transition.payload.reason_message)
        raise KernelReplayRejected(
            "KernelReplayState rejected admitted host observation",
            state=rejected_state,
            transition=transition,
            reason=reason,
        ) from reason
    return _state_after_transition(
        prepared=state.prepared_program,
        previous=state,
        transition=transition,
        consumed_source_keys=consumed_source_keys,
        closed_source_key=request.source_key,
    ), transition


def resume_replayable_kernel_transition(
    program: KernelProgramInput,
    request: ExternalEffectRequest,
    observation: HostCompleted,
    *,
    registry: EffectRegistry | None = None,
    ledger: ContinuationReplayLedger | None = None,
    parent_transition_refs: tuple[Ref, ...] = (),
) -> ReplayableKernelTransition:
    return _resume_observation_to_transition(
        program,
        request,
        observation,
        registry=registry,
        ledger=ledger,
        parent_transition_refs=parent_transition_refs,
    ).transition


def _resume_observation_to_transition(
    program: KernelProgramInput,
    request: ExternalEffectRequest,
    observation: HostCompleted,
    *,
    registry: EffectRegistry | None,
    ledger: ContinuationReplayLedger | None,
    parent_transition_refs: tuple[Ref, ...],
) -> _ReplayObservationTransition:
    if not isinstance(request, ExternalEffectRequest):
        raise ContinuationReplayError("replay observation admission requires an executable ExternalEffectRequest")
    prepared = ensure_prepared_kernel_program(program)
    program_ref = project_program_identity(prepared).program_ref
    records: list[TraceRecord] = []
    evaluator = StepKernelEvaluator(
        prepared,
        registry=registry,
        event_sink=lambda event: records.append(record_from_event(event)),
        evidence_mode="trace",
    )
    replay_state = _validated_replay_state(evaluator, request.replay_artifact)
    if ledger is not None:
        if request.source_key is None:
            raise ContinuationReplayError("ContinuationReplayLedger requires ExternalEffectRequest.source_key")
        ledger.consume(request.source_key)
    try:
        outcome = evaluator._resume_value_from_continuation_state(
            replay_state,
            observation.value,
            source_label=_source_label(request.replay_artifact),
        )
        trace_delta = tuple(records)
        evidence = TraceDebugEvidence(
            continuation_root_refs=evaluator.continuation_root_refs,
            continuation_objects=evaluator.continuation_objects,
            program_ref=evaluator.program_ref,
            continuation_ref_map=evaluator.continuation_ref_map,
            continuation_control_ref_map=evaluator.continuation_control_ref_map,
            context_ref_map=evaluator.context_ref_map,
        )
        transition = _replayable_resume_transition(
            program_ref=evidence.program_ref,
            outcome=outcome,
            trace_delta=trace_delta,
            evidence=evidence,
            parent_transition_refs=parent_transition_refs,
            resume_observation_ref=_host_completed_ref(observation),
        )
        return _ReplayObservationTransition(transition)
    except Exception as exc:
        trace_delta = tuple(records)
        if len(parent_transition_refs) != 1:
            raise ContinuationReplayError(
                "post-admission replay rejection requires exactly one parent transition ref"
            ) from exc
        return _ReplayObservationTransition(
            _rejected_transition(
                program_ref=program_ref,
                request=request,
                trace_delta=trace_delta,
                parent_transition_refs=parent_transition_refs,
                resume_observation_ref=_host_completed_ref(observation),
                reason=exc,
                context_ref_map=evaluator.context_ref_map,
                continuation_ref_map=evaluator.continuation_ref_map,
                continuation_control_ref_map=evaluator.continuation_control_ref_map,
            ),
            rejection_reason=exc,
        )


def _replayable_resume_transition(
    *,
    program_ref: Ref,
    outcome: SourceOutcome,
    trace_delta: tuple[TraceRecord, ...],
    evidence: TraceDebugEvidence,
    parent_transition_refs: tuple[Ref, ...],
    resume_observation_ref: Ref,
) -> ReplayableKernelTransition:
    if isinstance(outcome, Completed):
        payload: ReplayableKernelResult = ReplayableCompleted(program_ref, outcome, trace_delta)
    if isinstance(outcome, Suspended):
        payload = _external_effect_request_from_trace_result(TraceResult(outcome, trace_delta, evidence))
    elif isinstance(outcome, Delayed | Forked):
        raise ContinuationReplayError(
            f"replayable external effect resume does not support {type(outcome).__name__} outcomes"
        )
    elif not isinstance(outcome, Completed):
        raise ContinuationReplayError(f"unsupported replayable kernel outcome: {type(outcome).__name__}")
    return _replayable_transition(
        program_ref=program_ref,
        payload=payload,
        trace_delta=trace_delta,
        parent_transition_refs=parent_transition_refs,
        resume_observation_ref=resume_observation_ref,
        context_ref_map=evidence.context_ref_map,
        continuation_ref_map=evidence.continuation_ref_map,
        continuation_control_ref_map=evidence.continuation_control_ref_map,
    )


def resume_continuation(
    program: KernelProgramInput,
    artifact: ContinuationReplayArtifact,
    value: JsonValue,
    *,
    registry: EffectRegistry | None = None,
    ledger: ContinuationReplayLedger | None = None,
    source_label: str | None = None,
) -> SourceOutcome:
    prepared = ensure_prepared_kernel_program(program)
    evaluator = StepKernelEvaluator(prepared, registry=registry, evidence_mode="none")
    replay_state = _validated_replay_state(evaluator, artifact)
    if ledger is not None:
        if artifact.source_key is None:
            raise ContinuationReplayError("ContinuationReplayLedger requires ContinuationReplayArtifact.source_key")
        ledger.consume(artifact.source_key)
    return evaluator._resume_value_from_continuation_state(
        replay_state,
        value,
        source_label=source_label or _source_label(artifact),
    )


def _resume_continuation_with_trace(
    program: KernelProgramInput,
    artifact: ContinuationReplayArtifact,
    value: JsonValue,
    *,
    registry: EffectRegistry | None,
    ledger: ContinuationReplayLedger | None,
    source_label: str | None = None,
) -> tuple[SourceOutcome, tuple[TraceRecord, ...], TraceDebugEvidence]:
    try:
        return _resume_continuation_with_trace_capture_failures(
            program,
            artifact,
            value,
            registry=registry,
            ledger=ledger,
            source_label=source_label,
        )
    except _ContinuationReplayTraceFailure as failure:
        raise failure.reason


def _resume_continuation_with_trace_capture_failures(
    program: KernelProgramInput,
    artifact: ContinuationReplayArtifact,
    value: JsonValue,
    *,
    registry: EffectRegistry | None,
    ledger: ContinuationReplayLedger | None,
    source_label: str | None = None,
) -> tuple[SourceOutcome, tuple[TraceRecord, ...], TraceDebugEvidence]:
    records: list[TraceRecord] = []
    prepared = ensure_prepared_kernel_program(program)
    evaluator = StepKernelEvaluator(
        prepared,
        registry=registry,
        event_sink=lambda event: records.append(record_from_event(event)),
        evidence_mode="trace",
    )
    try:
        replay_state = _validated_replay_state(evaluator, artifact)
        if ledger is not None:
            if artifact.source_key is None:
                raise ContinuationReplayError("ContinuationReplayLedger requires ContinuationReplayArtifact.source_key")
            ledger.consume(artifact.source_key)
        outcome = evaluator._resume_value_from_continuation_state(
            replay_state,
            value,
            source_label=source_label or _source_label(artifact),
        )
    except Exception as exc:
        raise _ContinuationReplayTraceFailure(exc, tuple(records)) from exc
    evidence = TraceDebugEvidence(
        continuation_root_refs=evaluator.continuation_root_refs,
        continuation_objects=evaluator.continuation_objects,
        program_ref=evaluator.program_ref,
        continuation_ref_map=evaluator.continuation_ref_map,
        continuation_control_ref_map=evaluator.continuation_control_ref_map,
        context_ref_map=evaluator.context_ref_map,
    )
    return outcome, tuple(records), evidence


def _replayable_payload_from_trace_result(result: TraceResult) -> ReplayableKernelResult:
    if isinstance(result.outcome, Completed):
        return ReplayableCompleted(result.require_debug_evidence().program_ref, result.outcome, result.trace)
    if isinstance(result.outcome, Suspended):
        return _external_effect_request_from_trace_result(result)
    raise ContinuationReplayError(f"unsupported replayable kernel outcome: {type(result.outcome).__name__}")


def _session_replayable_transition(
    *,
    evaluator: StepKernelEvaluator,
    outcome: SourceOutcome,
    trace_delta: tuple[TraceRecord, ...],
    parent_transition_refs: tuple[Ref, ...] = (),
    resume_observation_ref: Ref | None = None,
) -> ReplayableKernelTransition:
    if isinstance(outcome, Completed):
        payload: ReplayableKernelResult = ReplayableCompleted(evaluator.program_ref, outcome, trace_delta)
    elif isinstance(outcome, Suspended):
        payload = _external_effect_request_ref_from_trace_delta(evaluator, trace_delta, outcome.effect_kind)
    else:
        raise ContinuationReplayError(f"unsupported replayable kernel outcome: {type(outcome).__name__}")
    return _replayable_transition(
        program_ref=evaluator.program_ref,
        payload=payload,
        trace_delta=trace_delta,
        parent_transition_refs=parent_transition_refs,
        resume_observation_ref=resume_observation_ref,
        context_ref_map=evaluator.context_ref_map,
        continuation_ref_map=evaluator.continuation_ref_map,
        continuation_control_ref_map=evaluator.continuation_control_ref_map,
    )


def _state_after_transition(
    *,
    prepared: PreparedKernelProgram,
    previous: KernelReplayState | None,
    transition: ReplayableKernelTransition,
    consumed_source_keys: tuple[str, ...] = (),
    closed_source_key: str | None = None,
) -> KernelReplayState:
    if previous is not None and transition.program_ref != previous.program_ref:
        raise ContinuationReplayError("replay transition program_ref disagrees with KernelReplayState")
    open_requests = dict(previous.open_requests if previous is not None else {})
    terminal = False
    rejected = False
    if isinstance(transition.payload, ExternalEffectRequest | ExternalEffectRequestRef):
        if closed_source_key is not None:
            open_requests.pop(closed_source_key, None)
        open_request = _open_request_from_transition(transition)
        open_requests[open_request.source_key] = open_request
    elif isinstance(transition.payload, ReplayableCompleted):
        if closed_source_key is not None:
            open_requests.pop(closed_source_key, None)
        terminal = True
    elif isinstance(transition.payload, ReplayableRejected):
        if previous is None:
            raise ContinuationReplayError("rejected replay transition requires previous replay state")
        rejected_open_request = previous.open_requests.get(transition.payload.source_key)
        if rejected_open_request is None:
            raise ContinuationReplayError("rejected replay transition source_key is not open")
        if transition.parent_transition_refs != (rejected_open_request.request_transition_ref,):
            raise ContinuationReplayError("rejected replay transition parent disagrees with open request frontier")
        if rejected_open_request.request_ref != transition.payload.request_ref:
            raise ContinuationReplayError("rejected replay transition request_ref disagrees with open request")
        if closed_source_key != transition.payload.source_key:
            raise ContinuationReplayError("rejected replay transition must close its source_key")
        if transition.payload.source_key not in consumed_source_keys:
            raise ContinuationReplayError("rejected replay transition must consume its source_key")
        open_requests.pop(closed_source_key, None)
        rejected = True
    else:
        raise ContinuationReplayError(f"unsupported replayable kernel payload: {type(transition.payload).__name__}")
    trace = (previous.trace if previous is not None else ()) + transition.trace_delta
    transition_refs = (previous.transition_refs if previous is not None else ()) + (transition.transition_id,)
    return KernelReplayState(
        prepared_program=prepared,
        program_ref=transition.program_ref,
        profile=prepared.profile,
        open_requests=open_requests,
        consumed_source_keys=consumed_source_keys,
        transition_refs=transition_refs,
        trace=trace,
        terminal=terminal,
        rejected=rejected,
    )


def _rejected_transition(
    *,
    program_ref: Ref,
    request: ReplayableExternalEffectRequest,
    trace_delta: tuple[TraceRecord, ...],
    parent_transition_refs: tuple[Ref, ...],
    resume_observation_ref: Ref,
    reason: BaseException,
    context_ref_map: Mapping[Ref, Ref] | None = None,
    continuation_ref_map: Mapping[Ref, Ref] | None = None,
    continuation_control_ref_map: Mapping[Ref, Ref] | None = None,
) -> ReplayableKernelTransition:
    payload = ReplayableRejected(
        program_ref=program_ref,
        source_key=request.source_key,
        request_ref=_external_effect_request_ref(request),
        reason_type=type(reason).__name__,
        reason_message=str(reason),
        trace=trace_delta,
    )
    return _replayable_transition(
        program_ref=program_ref,
        payload=payload,
        trace_delta=trace_delta,
        parent_transition_refs=parent_transition_refs,
        resume_observation_ref=resume_observation_ref,
        context_ref_map=context_ref_map,
        continuation_ref_map=continuation_ref_map,
        continuation_control_ref_map=continuation_control_ref_map,
    )


def _open_request_from_transition(transition: ReplayableKernelTransition) -> OpenReplayRequest:
    if not isinstance(transition.payload, ExternalEffectRequest | ExternalEffectRequestRef):
        raise ContinuationReplayError("replay transition does not carry an external effect request")
    request = transition.payload
    return OpenReplayRequest(
        source_key=request.source_key,
        request_ref=_external_effect_request_ref(request),
        request_transition_ref=transition.transition_id,
        program_ref=transition.program_ref,
        declaration_ref=request.declaration_ref,
        effect_kind=request.effect_kind,
        root_ref=request.root_ref if isinstance(request, ExternalEffectRequestRef) else request.replay_artifact.root_ref,
        operation_result_schema_ref=request.operation_result_schema_ref,
        trace_prefix_ref=_trace_prefix_ref(request.trace_prefix),
    )


def _open_replay_request_to_json(request: OpenReplayRequest) -> dict[str, JsonValue]:
    return {
        "source_key": request.source_key,
        "request_ref": request.request_ref,
        "request_transition_ref": request.request_transition_ref,
        "program_ref": request.program_ref,
        "declaration_ref": request.declaration_ref,
        "effect_kind": request.effect_kind,
        "root_ref": request.root_ref,
        "operation_result_schema_ref": request.operation_result_schema_ref,
        "trace_prefix_ref": request.trace_prefix_ref,
    }


def _open_replay_request_from_json(data: Mapping[str, JsonValue]) -> OpenReplayRequest:
    _require_keys(data, _OPEN_REPLAY_REQUEST_JSON_KEYS, "OpenReplayRequest")
    return OpenReplayRequest(
        source_key=_require_str(data["source_key"], "OpenReplayRequest.source_key"),
        request_ref=_require_str(data["request_ref"], "OpenReplayRequest.request_ref"),
        request_transition_ref=_require_str(
            data["request_transition_ref"],
            "OpenReplayRequest.request_transition_ref",
        ),
        program_ref=_require_str(data["program_ref"], "OpenReplayRequest.program_ref"),
        declaration_ref=_require_str(data["declaration_ref"], "OpenReplayRequest.declaration_ref"),
        effect_kind=_require_str(data["effect_kind"], "OpenReplayRequest.effect_kind"),
        root_ref=_require_str(data["root_ref"], "OpenReplayRequest.root_ref"),
        operation_result_schema_ref=_optional_str(
            data["operation_result_schema_ref"],
            "OpenReplayRequest.operation_result_schema_ref",
        ),
        trace_prefix_ref=_require_str(data["trace_prefix_ref"], "OpenReplayRequest.trace_prefix_ref"),
    )


def _external_effect_request_ref(request: ReplayableExternalEffectRequest) -> Ref:
    return content_ref("external-effect-request", _external_effect_request_identity_json(request))


def _external_effect_request_identity_json(request: ReplayableExternalEffectRequest) -> dict[str, JsonValue]:
    return {
        "request_schema_version": request.request_schema_version,
        "declaration": trace_record_to_json(request.declaration),
        "artifact_ref": request.replay_artifact_ref,
        "trace_prefix_ref": _trace_prefix_ref(request.trace_prefix),
    }


def _external_effect_request_descriptor(request: ReplayableExternalEffectRequest) -> ExternalEffectRequestDescriptor:
    return ExternalEffectRequestDescriptor(
        request_ref=_external_effect_request_ref(request),
        source_key=request.source_key,
        declaration_ref=request.declaration_ref,
        program_ref=request.program_ref,
        effect_kind=request.effect_kind,
        payload=request.payload,
        payload_schema_ref=request.payload_schema_ref,
        operation_result_schema_ref=request.operation_result_schema_ref,
        root_ref=request.root_ref,
        replay_artifact_ref=request.replay_artifact_ref,
        trace_prefix_ref=_trace_prefix_ref(request.trace_prefix),
    )


def _trace_prefix_ref(trace: tuple[TraceRecord, ...]) -> Ref:
    return content_ref("trace-prefix", trace_to_json(trace))


def _append_consumed_source_key(consumed_source_keys: tuple[str, ...], source_key: str) -> tuple[str, ...]:
    if source_key in consumed_source_keys:
        raise ResumptionUsed(f"continuation replay source {source_key!r} already consumed")
    return (*consumed_source_keys, source_key)


def _replayable_transition(
    *,
    program_ref: Ref,
    payload: ReplayableKernelResult,
    trace_delta: tuple[TraceRecord, ...],
    parent_transition_refs: tuple[Ref, ...] = (),
    resume_observation_ref: Ref | None = None,
    context_ref_map: Mapping[Ref, Ref] | None = None,
    continuation_ref_map: Mapping[Ref, Ref] | None = None,
    continuation_control_ref_map: Mapping[Ref, Ref] | None = None,
) -> ReplayableKernelTransition:
    status: ReplayableKernelTransitionStatus
    if isinstance(payload, ReplayableCompleted):
        status = "completed"
    elif isinstance(payload, ExternalEffectRequest | ExternalEffectRequestRef):
        status = "external-effect-request"
    elif isinstance(payload, ReplayableRejected):
        status = "rejected"
    else:
        raise ContinuationReplayError(f"unsupported replayable kernel payload: {type(payload).__name__}")
    transition_id = _replayable_transition_id(
        program_ref=program_ref,
        status=status,
        parent_transition_refs=parent_transition_refs,
        resume_observation_ref=resume_observation_ref,
        payload=payload,
        trace_delta=trace_delta,
    )
    return ReplayableKernelTransition(
        transition_id=transition_id,
        program_ref=program_ref,
        status=status,
        payload=payload,
        trace_delta=trace_delta,
        resume_observation_ref=resume_observation_ref,
        parent_transition_refs=parent_transition_refs,
        context_ref_map=MappingProxyType(dict(context_ref_map)) if context_ref_map else MappingProxyType({}),
        continuation_ref_map=MappingProxyType(dict(continuation_ref_map)) if continuation_ref_map else MappingProxyType({}),
        continuation_control_ref_map=MappingProxyType(dict(continuation_control_ref_map)) if continuation_control_ref_map else MappingProxyType({}),
    )


def _replayable_transition_id(
    *,
    program_ref: Ref,
    status: ReplayableKernelTransitionStatus,
    parent_transition_refs: tuple[Ref, ...],
    resume_observation_ref: Ref | None,
    payload: ReplayableKernelResult,
    trace_delta: tuple[TraceRecord, ...],
) -> Ref:
    return content_ref(
        "kernel-replay-transition",
        {
            "transition_schema_version": REPLAYABLE_KERNEL_TRANSITION_SCHEMA_VERSION,
            "program_ref": program_ref,
            "status": status,
            "parent_transition_refs": list(parent_transition_refs),
            "resume_observation_ref": resume_observation_ref,
            "payload_ref": _replayable_payload_ref(payload),
            "trace_delta_ref": _trace_prefix_ref(trace_delta),
        },
    )


def _host_completed_ref(observation: HostCompleted) -> Ref:
    return content_ref("host-completed", host_completed_to_json(observation))


def _payload_trace_delta(payload: ReplayableKernelResult) -> tuple[TraceRecord, ...]:
    if isinstance(payload, ReplayableCompleted):
        return payload.trace
    if isinstance(payload, ReplayableRejected):
        return payload.trace
    if isinstance(payload, ExternalEffectRequest | ExternalEffectRequestRef):
        return payload.trace_prefix
    raise TypeError(f"unsupported replayable kernel payload: {payload!r}")


def _payload_program_ref(payload: ReplayableKernelResult) -> Ref | None:
    if isinstance(payload, ExternalEffectRequest):
        return payload.program_ref or payload.replay_artifact.program_ref
    if isinstance(payload, ExternalEffectRequestRef):
        return payload.program_ref or payload.artifact_record.program_ref
    if isinstance(payload, ReplayableCompleted):
        return payload.program_ref
    if isinstance(payload, ReplayableRejected):
        return payload.program_ref
    raise TypeError(f"unsupported replayable kernel payload: {payload!r}")


def _replayable_payload_to_json(payload: ReplayableKernelResult) -> dict[str, JsonValue]:
    if isinstance(payload, ReplayableCompleted):
        return {
            "payload_type": "ReplayableCompleted",
            "program_ref": payload.program_ref,
            "value": payload.value,
            "trace": trace_to_json(payload.trace),
        }
    if isinstance(payload, ReplayableRejected):
        return {
            "payload_type": "ReplayableRejected",
            "program_ref": payload.program_ref,
            "source_key": payload.source_key,
            "request_ref": payload.request_ref,
            "reason_type": payload.reason_type,
            "reason_message": payload.reason_message,
            "trace": trace_to_json(payload.trace),
        }
    if isinstance(payload, ExternalEffectRequest):
        return {
            "payload_type": "ExternalEffectRequest",
            "request": external_effect_request_to_json(payload),
        }
    if isinstance(payload, ExternalEffectRequestRef):
        raise ContinuationReplaySerializationError(
            "ExternalEffectRequestRef is scoped to a live KernelReplaySession or KernelReplayJournal; "
            "serialize compact request refs with kernel_replay_journal_to_json(...)"
        )
    raise TypeError(f"unsupported replayable kernel payload: {payload!r}")


def _replayable_payload_ref(payload: ReplayableKernelResult) -> Ref:
    if isinstance(payload, ReplayableCompleted):
        return content_ref("replayable-completed", _replayable_payload_to_json(payload))
    if isinstance(payload, ReplayableRejected):
        return content_ref("replayable-rejected", _replayable_payload_to_json(payload))
    if isinstance(payload, ExternalEffectRequest | ExternalEffectRequestRef):
        return _external_effect_request_ref(payload)
    raise TypeError(f"unsupported replayable kernel payload: {payload!r}")


def _replayable_payload_from_json(data: Mapping[str, JsonValue]) -> ReplayableKernelResult:
    payload_type = _require_str(data.get("payload_type"), "ReplayableKernelTransition.payload.payload_type")
    if payload_type == "ReplayableCompleted":
        _require_keys(data, _REPLAYABLE_COMPLETED_JSON_KEYS, "ReplayableCompleted payload")
        return ReplayableCompleted(
            _require_str(data["program_ref"], "ReplayableCompleted.program_ref"),
            Completed(data["value"]),
            trace_from_json(
                [
                    _require_mapping(item, "ReplayableCompleted.trace")
                    for item in _require_sequence(data["trace"], "ReplayableCompleted.trace")
                ]
            ),
        )
    if payload_type == "ReplayableRejected":
        _require_keys(data, _REPLAYABLE_REJECTED_JSON_KEYS, "ReplayableRejected payload")
        return ReplayableRejected(
            program_ref=_require_str(data["program_ref"], "ReplayableRejected.program_ref"),
            source_key=_require_str(data["source_key"], "ReplayableRejected.source_key"),
            request_ref=_require_str(data["request_ref"], "ReplayableRejected.request_ref"),
            reason_type=_require_str(data["reason_type"], "ReplayableRejected.reason_type"),
            reason_message=_require_str(data["reason_message"], "ReplayableRejected.reason_message"),
            trace=trace_from_json(
                [
                    _require_mapping(item, "ReplayableRejected.trace")
                    for item in _require_sequence(data["trace"], "ReplayableRejected.trace")
                ]
            ),
        )
    if payload_type == "ExternalEffectRequest":
        _require_keys(data, _REPLAYABLE_REQUEST_JSON_KEYS, "ExternalEffectRequest payload")
        return external_effect_request_from_json(_require_mapping(data["request"], "ExternalEffectRequest payload"))
    raise ContinuationReplaySerializationError(f"unknown replayable payload_type: {payload_type!r}")


def _journal_transition_to_json(transition: ReplayableKernelTransition) -> dict[str, JsonValue]:
    return {
        "transition_schema_version": transition.transition_schema_version,
        "transition_id": transition.transition_id,
        "parent_transition_refs": list(transition.parent_transition_refs),
        "program_ref": transition.program_ref,
        "resume_observation_ref": transition.resume_observation_ref,
        "status": transition.status,
        "payload": _journal_payload_to_json(transition.payload),
        "trace_delta": trace_to_json(transition.trace_delta),
        "context_ref_map": dict(transition.context_ref_map),
        "continuation_ref_map": dict(transition.continuation_ref_map),
        "continuation_control_ref_map": dict(transition.continuation_control_ref_map),
    }


def _journal_transition_from_json(
    data: Mapping[str, JsonValue],
    *,
    artifacts: Mapping[Ref, ContinuationReplayArtifactRecord],
) -> ReplayableKernelTransition:
    try:
        _require_keys(data, _REPLAYABLE_KERNEL_TRANSITION_JSON_KEYS, "ReplayableKernelTransition")
        payload = _journal_payload_from_json(
            _require_mapping(data["payload"], "ReplayableKernelTransition.payload"),
            artifacts=artifacts,
        )
        return ReplayableKernelTransition(
            transition_schema_version=_require_str(
                data["transition_schema_version"],
                "ReplayableKernelTransition.transition_schema_version",
            ),
            transition_id=_require_str(data["transition_id"], "ReplayableKernelTransition.transition_id"),
            parent_transition_refs=tuple(
                _require_str(ref, "ReplayableKernelTransition.parent_transition_refs")
                for ref in _require_sequence(
                    data["parent_transition_refs"],
                    "ReplayableKernelTransition.parent_transition_refs",
                )
            ),
            program_ref=_require_str(data["program_ref"], "ReplayableKernelTransition.program_ref"),
            resume_observation_ref=_optional_str(
                data["resume_observation_ref"],
                "ReplayableKernelTransition.resume_observation_ref",
            ),
            status=cast(
                "ReplayableKernelTransitionStatus",
                _require_str(data["status"], "ReplayableKernelTransition.status"),
            ),
            payload=payload,
            trace_delta=trace_from_json(
                [
                    _require_mapping(item, "ReplayableKernelTransition.trace_delta")
                    for item in _require_sequence(data["trace_delta"], "ReplayableKernelTransition.trace_delta")
                ]
            ),
            context_ref_map=_ref_map_from_json(
                data["context_ref_map"], "ReplayableKernelTransition.context_ref_map"
            ),
            continuation_ref_map=_ref_map_from_json(
                data["continuation_ref_map"], "ReplayableKernelTransition.continuation_ref_map"
            ),
            continuation_control_ref_map=_ref_map_from_json(
                data["continuation_control_ref_map"],
                "ReplayableKernelTransition.continuation_control_ref_map",
            ),
        )
    except ContinuationReplayError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ContinuationReplaySerializationError(str(exc)) from exc


def _journal_payload_to_json(payload: ReplayableKernelResult) -> dict[str, JsonValue]:
    if isinstance(payload, ReplayableCompleted | ReplayableRejected):
        return _replayable_payload_to_json(payload)
    if isinstance(payload, ExternalEffectRequest):
        return _external_effect_request_ref_payload_to_json(
            request_ref=_external_effect_request_ref(payload),
            declaration=payload.declaration,
            artifact_ref=payload.replay_artifact_ref,
            trace_prefix=payload.trace_prefix,
        )
    if isinstance(payload, ExternalEffectRequestRef):
        return _external_effect_request_ref_payload_to_json(
            request_ref=_external_effect_request_ref(payload),
            declaration=payload.declaration,
            artifact_ref=payload.artifact_ref,
            trace_prefix=payload.trace_prefix,
        )
    raise TypeError(f"unsupported replayable kernel payload: {payload!r}")


def _external_effect_request_ref_payload_to_json(
    *,
    request_ref: Ref,
    declaration: EffectDeclaration,
    artifact_ref: Ref,
    trace_prefix: tuple[TraceRecord, ...],
) -> dict[str, JsonValue]:
    return {
        "payload_type": "ExternalEffectRequestRef",
        "request_ref": request_ref,
        "declaration": trace_record_to_json(declaration),
        "artifact_ref": artifact_ref,
        "trace_prefix": trace_to_json(trace_prefix),
    }


def _journal_payload_from_json(
    data: Mapping[str, JsonValue],
    *,
    artifacts: Mapping[Ref, ContinuationReplayArtifactRecord],
) -> ReplayableKernelResult:
    payload_type = _require_str(data.get("payload_type"), "ReplayableKernelTransition.payload.payload_type")
    if payload_type in {"ReplayableCompleted", "ReplayableRejected"}:
        return _replayable_payload_from_json(data)
    if payload_type == "ExternalEffectRequestRef":
        _require_keys(data, _REPLAYABLE_REQUEST_REF_JSON_KEYS, "ExternalEffectRequestRef payload")
        artifact_ref = _require_str(data["artifact_ref"], "ExternalEffectRequestRef.artifact_ref")
        record = artifacts.get(artifact_ref)
        if record is None:
            raise ContinuationReplayError(f"KernelReplayJournal missing artifact {artifact_ref!r}")
        declaration = trace_record_from_json(_require_mapping(data["declaration"], "ExternalEffectRequestRef.declaration"))
        if not isinstance(declaration, EffectDeclaration):
            raise TypeError("ExternalEffectRequestRef.declaration must decode to an EffectDeclaration")
        request = ExternalEffectRequestRef(
            declaration=declaration,
            artifact_ref=artifact_ref,
            artifact_record=record,
            trace_prefix=trace_from_json(
                [
                    _require_mapping(item, "ExternalEffectRequestRef.trace_prefix")
                    for item in _require_sequence(data["trace_prefix"], "ExternalEffectRequestRef.trace_prefix")
                ]
            ),
        )
        request_ref = _require_str(data["request_ref"], "ExternalEffectRequestRef.request_ref")
        if request_ref != _external_effect_request_ref(request):
            raise ContinuationReplayError("ExternalEffectRequestRef request_ref does not match canonical payload")
        return request
    raise ContinuationReplaySerializationError(f"unknown replayable payload_type: {payload_type!r}")


def _external_effect_request_from_trace_result(result: TraceResult) -> ExternalEffectRequest:
    if not isinstance(result.outcome, Suspended):
        raise ContinuationReplayError("trace result does not contain a suspended external effect")
    declaration = _last_effect_declaration(result.trace, result.outcome.effect_kind)
    evidence = result.require_debug_evidence()
    try:
        root_ref = evidence.continuation_ref_map.get(
            declaration.full_continuation_ref,
            declaration.full_continuation_ref,
        )
    except KeyError as exc:
        raise ContinuationReplayError(f"missing continuation object ref for declaration {declaration.ref!r}") from exc
    artifact = continuation_replay_artifact_from_objects(
        root_ref,
        evidence.continuation_objects,
        program_ref=evidence.program_ref,
        source_ref=declaration.ref,
        source_record_type="EffectDeclaration",
        effect_kind=declaration.effect_kind,
        operation_result_schema_ref=declaration.operation_result_schema_ref,
    )
    return ExternalEffectRequest(declaration=declaration, replay_artifact=artifact, trace_prefix=result.trace)


def _external_effect_request_ref_from_trace_delta(
    evaluator: StepKernelEvaluator,
    trace_delta: tuple[TraceRecord, ...],
    effect_kind: str,
) -> ExternalEffectRequestRef:
    declaration = _last_effect_declaration(trace_delta, effect_kind)
    root_ref = evaluator.continuation_ref_map.get(
        declaration.full_continuation_ref,
        declaration.full_continuation_ref,
    )
    root = evaluator.get_continuation_object(root_ref)
    if not isinstance(root, ContinuationRoot):
        raise ContinuationReplayError(f"continuation object {root_ref!r} is not a root")
    record = ContinuationReplayArtifactRecord(
        root_ref=root_ref,
        program_ref=evaluator.program_ref,
        source_key=_continuation_source_key(
            root_ref=root_ref,
            program_ref=root.program_ref,
            source_ref=declaration.ref,
            source_record_type="EffectDeclaration",
            effect_kind=declaration.effect_kind,
            operation_result_schema_ref=root.result_schema_ref,
        ),
        source_ref=declaration.ref,
        source_record_type="EffectDeclaration",
        effect_kind=declaration.effect_kind,
        operation_result_schema_ref=declaration.operation_result_schema_ref,
    )
    artifact_ref = continuation_replay_artifact_record_ref(record)
    return ExternalEffectRequestRef(
        declaration=declaration,
        artifact_ref=artifact_ref,
        artifact_record=record,
        trace_prefix=trace_delta,
    )


def _last_effect_declaration(trace: tuple[TraceRecord, ...], effect_kind: str) -> EffectDeclaration:
    for record in reversed(trace):
        if isinstance(record, EffectDeclaration) and record.effect_kind == effect_kind:
            return record
    raise ContinuationReplayError(f"trace has no EffectDeclaration for suspended effect {effect_kind!r}")


def _continuation_source_key(
    *,
    root_ref: Ref,
    program_ref: Ref,
    source_ref: Ref,
    source_record_type: SourceRecordType,
    effect_kind: str | None,
    operation_result_schema_ref: Ref | None,
) -> Ref:
    return content_ref(
        "continuation-source",
        {
            "source_key_schema_version": CONTINUATION_SOURCE_KEY_SCHEMA_VERSION,
            "program_ref": program_ref,
            "source_record_type": source_record_type,
            "source_ref": source_ref,
            "effect_kind": effect_kind,
            "root_ref": root_ref,
            "operation_result_schema_ref": operation_result_schema_ref,
        },
    )


def _validated_replay_state(evaluator: StepKernelEvaluator, artifact: ContinuationReplayArtifact) -> Any:
    try:
        return evaluator._continuation_replay_state_from_objects(artifact.root_ref, artifact.continuation_objects)
    except RuntimeError as exc:
        raise ContinuationReplayError(str(exc)) from exc


def _source_label(artifact: ContinuationReplayArtifact) -> str:
    if artifact.effect_kind is not None:
        return f"resume({artifact.effect_kind!r})"
    if artifact.source_ref is not None:
        return f"resume({artifact.source_ref!r})"
    return "resume(continuation)"


def _reachable_continuation_objects(
    root_ref: Ref,
    objects: Mapping[Ref, ContinuationObject],
) -> dict[Ref, ContinuationObject]:
    root_ref = _require_str(root_ref, "ContinuationReplayArtifact.root_ref")
    by_ref: dict[Ref, ContinuationObject] = {}
    for ref, obj in objects.items():
        ref = _require_str(ref, "ContinuationReplayArtifact.continuation_objects.ref")
        actual_ref = continuation_object_ref(obj)
        if ref != actual_ref:
            raise ContinuationReplayError(
                f"continuation object ref mismatch for {ref!r}: payload hashes to {actual_ref!r}"
            )
        by_ref[ref] = obj

    reachable: dict[Ref, ContinuationObject] = {}
    pending = [root_ref]
    while pending:
        ref = pending.pop()
        if ref in reachable:
            continue
        if ref not in by_ref:
            raise ContinuationReplayError(f"continuation replay artifact is missing object {ref!r}")
        obj = by_ref[ref]
        reachable[ref] = obj
        pending.extend(child_ref for child_ref in continuation_object_child_refs(obj) if child_ref not in reachable)
    return dict(sorted(reachable.items()))


def _reachable_continuation_objects_from_catalog(
    root_ref: Ref,
    objects: Mapping[Ref, ContinuationObject],
) -> dict[Ref, ContinuationObject]:
    root_ref = _require_str(root_ref, "ContinuationReplayArtifact.root_ref")
    reachable: dict[Ref, ContinuationObject] = {}
    pending = [root_ref]
    while pending:
        ref = pending.pop()
        if ref in reachable:
            continue
        obj = objects.get(ref)
        if obj is None:
            raise ContinuationReplayError(f"continuation replay artifact is missing object {ref!r}")
        reachable[ref] = obj
        pending.extend(child_ref for child_ref in continuation_object_child_refs(obj) if child_ref not in reachable)
    return dict(sorted(reachable.items()))


def _reachable_continuation_objects_from_store(
    evaluator: StepKernelEvaluator,
    root_ref: Ref,
) -> dict[Ref, ContinuationObject]:
    return _reachable_continuation_objects_from_store_many(evaluator, (root_ref,))


def _reachable_continuation_objects_from_store_many(
    evaluator: StepKernelEvaluator,
    root_refs: Iterable[Ref],
) -> dict[Ref, ContinuationObject]:
    reachable: dict[Ref, ContinuationObject] = {}
    pending = [_require_str(root_ref, "ContinuationReplayArtifact.root_ref") for root_ref in root_refs]
    while pending:
        ref = pending.pop()
        if ref in reachable:
            continue
        obj = evaluator.get_continuation_object(ref)
        reachable[ref] = obj
        pending.extend(child_ref for child_ref in continuation_object_child_refs(obj) if child_ref not in reachable)
    return dict(sorted(reachable.items()))


def _catalogs_from_transitions(
    transitions: tuple[ReplayableKernelTransition, ...],
) -> tuple[dict[Ref, ContinuationObject], dict[Ref, ContinuationReplayArtifactRecord]]:
    continuation_objects: dict[Ref, ContinuationObject] = {}
    artifacts: dict[Ref, ContinuationReplayArtifactRecord] = {}
    for transition in transitions:
        if isinstance(transition.payload, ExternalEffectRequestRef):
            record = transition.payload.artifact_record
            existing_record = artifacts.get(transition.payload.artifact_ref)
            if existing_record is not None and existing_record != record:
                raise ContinuationReplayError("KernelReplayJournal artifact catalog has conflicting artifact ref")
            artifacts[transition.payload.artifact_ref] = record
            continue
        if not isinstance(transition.payload, ExternalEffectRequest):
            continue
        artifact = transition.payload.replay_artifact
        artifact_ref = continuation_replay_artifact_ref(artifact)
        record = continuation_replay_artifact_record_from_artifact(artifact)
        existing_record = artifacts.get(artifact_ref)
        if existing_record is not None and existing_record != record:
            raise ContinuationReplayError("KernelReplayJournal artifact catalog has conflicting artifact ref")
        artifacts[artifact_ref] = record
        for ref, obj in artifact.continuation_objects.items():
            existing_obj = continuation_objects.get(ref)
            if existing_obj is not None and existing_obj != obj:
                raise ContinuationReplayError("KernelReplayJournal continuation object catalog has conflicting ref")
            continuation_objects[ref] = obj
    return dict(sorted(continuation_objects.items())), dict(sorted(artifacts.items()))


def _validated_continuation_object_catalog(
    continuation_objects: Mapping[Ref, ContinuationObject],
    *,
    required: Mapping[Ref, ContinuationObject],
) -> dict[Ref, ContinuationObject]:
    catalog: dict[Ref, ContinuationObject] = {}
    for ref, obj in continuation_objects.items():
        ref = _require_str(ref, "KernelReplayJournal.continuation_objects.ref")
        actual_ref = continuation_object_ref(obj)
        if ref != actual_ref:
            raise ContinuationReplayError(
                f"KernelReplayJournal continuation object ref mismatch for {ref!r}: payload hashes to {actual_ref!r}"
            )
        catalog[ref] = obj
    for ref, obj in required.items():
        existing = catalog.get(ref)
        if existing is not None and existing != obj:
            raise ContinuationReplayError("KernelReplayJournal continuation object catalog conflicts with transition")
        catalog[ref] = obj
    return dict(sorted(catalog.items()))


def _validated_artifact_catalog(
    artifacts: Mapping[Ref, ContinuationReplayArtifactRecord],
    *,
    continuation_objects: Mapping[Ref, ContinuationObject],
    required: Mapping[Ref, ContinuationReplayArtifactRecord],
) -> dict[Ref, ContinuationReplayArtifactRecord]:
    catalog: dict[Ref, ContinuationReplayArtifactRecord] = {}
    for ref, record in artifacts.items():
        if not isinstance(record, ContinuationReplayArtifactRecord):
            raise TypeError("KernelReplayJournal.artifacts values must be ContinuationReplayArtifactRecord")
        ref = _require_str(ref, "KernelReplayJournal.artifacts.ref")
        actual_ref = continuation_replay_artifact_record_ref(record)
        if ref != actual_ref:
            raise ContinuationReplayError(
                f"KernelReplayJournal artifact ref mismatch for {ref!r}: payload hashes to {actual_ref!r}"
            )
        catalog[ref] = record
    for ref, record in required.items():
        existing = catalog.get(ref)
        if existing is not None and existing != record:
            raise ContinuationReplayError("KernelReplayJournal artifact catalog conflicts with transition")
        catalog[ref] = record
    _validate_artifact_catalog_closure(catalog.values(), continuation_objects)
    return dict(sorted(catalog.items()))


def _validate_artifact_catalog_closure(
    artifacts: Iterable[ContinuationReplayArtifactRecord],
    continuation_objects: Mapping[Ref, ContinuationObject],
) -> None:
    pending: list[Ref] = []
    for record in artifacts:
        _validate_artifact_record_root(record, continuation_objects)
        pending.append(record.root_ref)

    reachable: set[Ref] = set()
    while pending:
        ref = pending.pop()
        if ref in reachable:
            continue
        obj = continuation_objects.get(ref)
        if obj is None:
            raise ContinuationReplayError(f"KernelReplayJournal artifact is missing continuation object {ref!r}")
        reachable.add(ref)
        pending.extend(child_ref for child_ref in continuation_object_child_refs(obj) if child_ref not in reachable)


def _validate_artifact_record_root(
    record: ContinuationReplayArtifactRecord,
    continuation_objects: Mapping[Ref, ContinuationObject],
) -> None:
    root = continuation_objects.get(record.root_ref)
    if root is None:
        raise ContinuationReplayError(f"KernelReplayJournal artifact is missing root object {record.root_ref!r}")
    if not isinstance(root, ContinuationRoot):
        raise ContinuationReplayError(f"KernelReplayJournal artifact root {record.root_ref!r} is not a root")
    if root.continuation_kind == "empty-terminal":
        raise ContinuationReplayError("KernelReplayJournal artifact does not support empty-terminal roots")
    if record.program_ref is not None and record.program_ref != root.program_ref:
        raise ContinuationReplayError("KernelReplayJournal artifact program_ref does not match ContinuationRoot")
    if record.operation_result_schema_ref is not None and record.operation_result_schema_ref != root.result_schema_ref:
        raise ContinuationReplayError(
            "KernelReplayJournal artifact operation_result_schema_ref does not match ContinuationRoot"
        )
    if record.source_ref is not None and record.source_record_type is not None:
        canonical_source_key = _continuation_source_key(
            root_ref=record.root_ref,
            program_ref=root.program_ref,
            source_ref=record.source_ref,
            source_record_type=record.source_record_type,
            effect_kind=record.effect_kind,
            operation_result_schema_ref=root.result_schema_ref,
        )
        if record.source_key != canonical_source_key:
            raise ContinuationReplayError("KernelReplayJournal artifact source_key is not canonical")


def _require_mapping(value: object, context: str) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{context} must be a mapping")
    return value


def _require_sequence(value: object, context: str) -> tuple[object, ...]:
    if not isinstance(value, list | tuple):
        raise TypeError(f"{context} must be a sequence")
    return tuple(value)


def _require_json_compatible(value: object, context: str) -> None:
    if value is None or isinstance(value, bool | int | str):
        return
    if isinstance(value, float):
        if not isfinite(value):
            raise TypeError(f"{context} contains a non-finite float")
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _require_json_compatible(item, f"{context}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{context} contains a non-string mapping key")
            _require_json_compatible(item, f"{context}.{key}")
        return
    raise TypeError(f"{context} contains a non-JSON-compatible value: {value!r}")


def _json_value_snapshot(value: JsonValue) -> JsonValue:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [_json_value_snapshot(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _json_value_snapshot(item) for key, item in value.items()}
    raise TypeError(f"value of type {type(value).__name__} is not JSON-compatible")


def _require_keys(value: Mapping[str, JsonValue], expected: frozenset[str], context: str) -> None:
    keys = frozenset(value)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        raise TypeError(f"{context} keys disagree (missing={missing!r}, extra={extra!r})")


def _require_str(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{context} must be a string")
    return value


def _require_bool(value: object, context: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{context} must be a bool")
    return value


def _optional_str(value: object, context: str) -> str | None:
    if value is None:
        return None
    return _require_str(value, context)


def _require_optional_str(value: object, context: str) -> None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{context} must be a string or None")
