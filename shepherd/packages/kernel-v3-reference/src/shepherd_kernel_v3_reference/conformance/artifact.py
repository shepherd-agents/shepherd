"""Offline conformance artifacts for continuation-backed kernel-v3 traces."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from math import isfinite
from typing import TYPE_CHECKING, Any, Literal, NoReturn, TypeAlias

from shepherd_kernel_v3_reference.kernel.continuation_objects import (
    CONTINUATION_OBJECT_SCHEMA_VERSION,
    ContinuationObject,
    ContinuationRoot,
    continuation_object_child_refs,
    continuation_object_from_json,
    continuation_object_ref,
    continuation_object_to_json,
)
from shepherd_kernel_v3_reference.trace.records import (
    ContinuationDelay,
    ContinuationPending,
    ForkBranch,
    ForkSummary,
    HandlerForward,
    TerminalResumeResult,
    TraceRecord,
)
from shepherd_kernel_v3_reference.trace.serde import trace_from_json, trace_to_json
from shepherd_kernel_v3_reference.trace.validate import (
    TRACE_EVIDENCE_BUNDLE_SCHEMA_VERSION,
    TraceEvidenceBundle,
    TraceEvidenceValidationProfile,
    TraceValidationError,
    validate_trace_evidence,
)

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.kernel.ir import Ref
    from shepherd_kernel_v3_reference.trace.machine import TraceResult

JsonValue: TypeAlias = Any
CONFORMANCE_ARTIFACT_SCHEMA_VERSION = "shepherd_kernel_v3_reference.conformance-artifact.v2"
ConformanceArtifactKind = Literal["shepherd-kernel-v3-conformance"]

_ARTIFACT_KIND: ConformanceArtifactKind = "shepherd-kernel-v3-conformance"
_PROGRAM_PROFILE_KEYS = frozenset({"name", "version", "validated"})
_ARTIFACT_JSON_KEYS = frozenset(
    {
        "artifact_schema_version",
        "artifact_kind",
        "validation_profile",
        "program_ref",
        "program_profile",
        "trace",
        "continuation_root_refs",
        "continuation_ref_map",
        "continuation_control_ref_map",
        "context_ref_map",
        "continuation_objects",
        "source_outcome",
        "schema_versions",
        "tool_versions",
    }
)
_CONTINUATION_ENTRY_JSON_KEYS = frozenset({"ref", "object"})
_CONTINUATION_REF_FIELDS = (
    "full_continuation_ref",
    "captured_continuation_ref",
    "outer_continuation_ref",
    "continuation_ref",
    "handler_continuation_ref",
    "handler_dynamic_tail_ref",
    "terminal_continuation_ref",
)
_PUBLICATION_EVIDENCE_UNSUPPORTED = (
    "publication-experimental continuation evidence artifacts are not supported; "
    "validate publication trace lifecycle with validate_publication_experimental_trace(...)"
)
_PUBLICATION_EXPERIMENTAL_RECORD_TYPES = (
    ContinuationDelay,
    ContinuationPending,
    ForkBranch,
    ForkSummary,
    HandlerForward,
    TerminalResumeResult,
)


class ConformanceArtifactValidationError(TraceValidationError):
    """Raised when a conformance artifact violates the I7 boundary."""


class ConformanceArtifactSerializationError(ValueError):
    """Raised when conformance artifact JSON cannot be decoded."""


class _FrozenJsonDict(dict[str, Any]):
    """Dict-shaped immutable mapping so JSON encoders still see an object."""

    __slots__ = ()

    def _blocked(self) -> NoReturn:
        raise TypeError("ConformanceArtifact JSON mappings are immutable")

    def __setitem__(self, key: str, value: Any) -> NoReturn:
        self._blocked()

    def __delitem__(self, key: str) -> NoReturn:
        self._blocked()

    def clear(self) -> NoReturn:
        self._blocked()

    def pop(self, key: str, default: Any = None) -> NoReturn:
        self._blocked()

    def popitem(self) -> NoReturn:
        self._blocked()

    def setdefault(self, key: str, default: Any = None) -> NoReturn:
        self._blocked()

    def update(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._blocked()

    def _blocked_operator(self, *args: object, **kwargs: object) -> NoReturn:
        self._blocked()


if not TYPE_CHECKING:
    _FrozenJsonDict.__or__ = _FrozenJsonDict._blocked_operator
    _FrozenJsonDict.__ior__ = _FrozenJsonDict._blocked_operator


@dataclass(frozen=True)
class ConformanceContinuationObject:
    ref: Ref
    object_json: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not isinstance(self.ref, str):
            raise TypeError("ConformanceContinuationObject.ref must be a string")
        object.__setattr__(
            self,
            "object_json",
            _freeze_mapping_value(self.object_json, context="ConformanceContinuationObject.object_json"),
        )


@dataclass(frozen=True)
class ConformanceArtifact:
    artifact_schema_version: str
    artifact_kind: ConformanceArtifactKind
    validation_profile: TraceEvidenceValidationProfile
    trace_json: tuple[Mapping[str, JsonValue], ...]
    continuation_root_refs: tuple[Ref, ...]
    continuation_objects: tuple[ConformanceContinuationObject, ...]
    program_ref: Ref | None
    program_profile: Mapping[str, JsonValue] | None
    source_outcome_json: Mapping[str, JsonValue] | None
    schema_versions: Mapping[str, str]
    tool_versions: Mapping[str, str]
    continuation_ref_map: Mapping[Ref, Ref] = field(default_factory=dict)
    continuation_control_ref_map: Mapping[Ref, Ref] = field(default_factory=dict)
    context_ref_map: Mapping[Ref, Ref] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.artifact_schema_version != CONFORMANCE_ARTIFACT_SCHEMA_VERSION:
            raise ConformanceArtifactValidationError(
                f"ConformanceArtifact.artifact_schema_version must be {CONFORMANCE_ARTIFACT_SCHEMA_VERSION!r}"
            )
        if self.artifact_kind != _ARTIFACT_KIND:
            raise ConformanceArtifactValidationError(f"ConformanceArtifact.artifact_kind must be {_ARTIFACT_KIND!r}")
        if self.validation_profile not in ("lifecycle-only", "runtime-with-continuations"):
            raise ConformanceArtifactValidationError(
                f"unknown ConformanceArtifact.validation_profile: {self.validation_profile!r}"
            )
        if self.program_ref is not None and not isinstance(self.program_ref, str):
            raise TypeError("ConformanceArtifact.program_ref must be a string or None")

        object.__setattr__(
            self,
            "trace_json",
            tuple(
                _freeze_mapping_value(record, context=f"ConformanceArtifact.trace_json[{idx}]")
                for idx, record in enumerate(self.trace_json)
            ),
        )
        object.__setattr__(
            self,
            "continuation_root_refs",
            _sorted_unique_refs(self.continuation_root_refs, context="ConformanceArtifact.continuation_root_refs"),
        )
        object.__setattr__(
            self,
            "continuation_ref_map",
            _freeze_str_mapping(self.continuation_ref_map, context="ConformanceArtifact.continuation_ref_map"),
        )
        object.__setattr__(
            self,
            "continuation_control_ref_map",
            _freeze_str_mapping(
                self.continuation_control_ref_map,
                context="ConformanceArtifact.continuation_control_ref_map",
            ),
        )
        object.__setattr__(
            self,
            "context_ref_map",
            _freeze_str_mapping(self.context_ref_map, context="ConformanceArtifact.context_ref_map"),
        )
        object.__setattr__(
            self,
            "continuation_objects",
            _sorted_unique_object_entries(self.continuation_objects),
        )
        object.__setattr__(
            self,
            "program_profile",
            None if self.program_profile is None else _freeze_program_profile(self.program_profile),
        )
        object.__setattr__(
            self,
            "source_outcome_json",
            None
            if self.source_outcome_json is None
            else _freeze_mapping_value(self.source_outcome_json, context="ConformanceArtifact.source_outcome_json"),
        )
        object.__setattr__(
            self,
            "schema_versions",
            _freeze_schema_versions(self.schema_versions),
        )
        object.__setattr__(
            self,
            "tool_versions",
            _freeze_str_mapping(self.tool_versions, context="ConformanceArtifact.tool_versions"),
        )


def conformance_artifact_to_json(artifact: ConformanceArtifact) -> dict[str, JsonValue]:
    """Return the explicit durable JSON mapping for a conformance artifact."""

    return {
        "artifact_schema_version": artifact.artifact_schema_version,
        "artifact_kind": artifact.artifact_kind,
        "validation_profile": artifact.validation_profile,
        "program_ref": artifact.program_ref,
        "program_profile": _jsonify(artifact.program_profile),
        "trace": [_jsonify(record) for record in artifact.trace_json],
        "continuation_root_refs": list(artifact.continuation_root_refs),
        "continuation_ref_map": dict(artifact.continuation_ref_map),
        "continuation_control_ref_map": dict(artifact.continuation_control_ref_map),
        "context_ref_map": dict(artifact.context_ref_map),
        "continuation_objects": [
            {
                "ref": entry.ref,
                "object": _jsonify(entry.object_json),
            }
            for entry in artifact.continuation_objects
        ],
        "source_outcome": _jsonify(artifact.source_outcome_json),
        "schema_versions": dict(artifact.schema_versions),
        "tool_versions": dict(artifact.tool_versions),
    }


def conformance_artifact_from_json(data: Mapping[str, JsonValue]) -> ConformanceArtifact:
    """Decode a conformance artifact from its durable JSON mapping."""

    _reject_unknown_keys(data, _ARTIFACT_JSON_KEYS, context="conformance artifact")
    continuation_entries = []
    for idx, item in enumerate(_require_list(data, "continuation_objects")):
        item_map = _require_mapping_value(item, context=f"continuation_objects[{idx}]")
        _reject_unknown_keys(item_map, _CONTINUATION_ENTRY_JSON_KEYS, context=f"continuation_objects[{idx}]")
        continuation_entries.append(
            ConformanceContinuationObject(
                ref=_require_str(item_map, "ref"),
                object_json=_require_mapping(item_map, "object"),
            )
        )
    return ConformanceArtifact(
        artifact_schema_version=_require_str(data, "artifact_schema_version"),
        artifact_kind=_require_artifact_kind(data, "artifact_kind"),
        validation_profile=_require_validation_profile(data, "validation_profile"),
        trace_json=tuple(
            _require_mapping_value(record, context=f"trace[{idx}]")
            for idx, record in enumerate(_require_list(data, "trace"))
        ),
        continuation_root_refs=tuple(_require_str_list(data, "continuation_root_refs")),
        continuation_ref_map=_require_str_mapping(data, "continuation_ref_map"),
        continuation_control_ref_map=_require_str_mapping(data, "continuation_control_ref_map"),
        context_ref_map=_require_str_mapping(data, "context_ref_map"),
        continuation_objects=tuple(continuation_entries),
        program_ref=_require_optional_str(data, "program_ref"),
        program_profile=_require_optional_mapping(data, "program_profile"),
        source_outcome_json=_require_optional_mapping(data, "source_outcome"),
        schema_versions=_require_str_mapping(data, "schema_versions"),
        tool_versions=_require_str_mapping(data, "tool_versions"),
    )


def dumps_conformance_artifact(artifact: ConformanceArtifact) -> str:
    """Serialize a conformance artifact to canonical JSON text."""

    return json.dumps(
        conformance_artifact_to_json(artifact),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def loads_conformance_artifact(data: str) -> ConformanceArtifact:
    """Deserialize a conformance artifact from canonical JSON text."""

    decoded = json.loads(data)
    if not isinstance(decoded, Mapping):
        raise ConformanceArtifactSerializationError("conformance artifact JSON must decode to an object")
    return conformance_artifact_from_json(decoded)


def validate_conformance_artifact(artifact: ConformanceArtifact) -> None:
    """Validate an offline conformance artifact by delegating to K2.1 evidence checks."""

    trace = trace_from_json(list(artifact.trace_json))
    _require_runtime_conformance_trace(trace)
    continuation_objects = _decode_continuation_objects(artifact.continuation_objects)

    if artifact.validation_profile == "lifecycle-only":
        if (
            artifact.continuation_root_refs
            or continuation_objects
            or artifact.continuation_ref_map
            or artifact.continuation_control_ref_map
            or artifact.context_ref_map
        ):
            raise ConformanceArtifactValidationError(
                "lifecycle-only conformance artifacts must not carry continuation evidence"
            )
    else:
        trace_refs = _trace_continuation_refs(trace)
        continuation_ref_map = dict(artifact.continuation_ref_map) or {ref: ref for ref in trace_refs}
        if trace_refs and not artifact.continuation_root_refs:
            raise ConformanceArtifactValidationError(
                "runtime-with-continuations artifacts with trace continuation refs must carry root refs"
            )
        _validate_exact_reachable_snapshot(artifact.continuation_root_refs, continuation_objects)

    bundle = TraceEvidenceBundle(
        bundle_schema_version=TRACE_EVIDENCE_BUNDLE_SCHEMA_VERSION,
        trace=trace,
        continuation_root_refs=artifact.continuation_root_refs,
        continuation_objects=continuation_objects,
        validation_profile=artifact.validation_profile,
        continuation_ref_map=continuation_ref_map if artifact.validation_profile != "lifecycle-only" else {},
        continuation_control_ref_map=artifact.continuation_control_ref_map,
        context_ref_map=artifact.context_ref_map,
    )
    validate_trace_evidence(bundle)
    _validate_program_ref_metadata(artifact.program_ref, trace, continuation_objects.values())


def artifact_from_trace_result(
    result: TraceResult,
    *,
    program_profile: Mapping[str, JsonValue] | None = None,
    source_outcome_json: Mapping[str, JsonValue] | None = None,
    tool_versions: Mapping[str, str] | None = None,
) -> ConformanceArtifact:
    """Build a runtime-with-continuations artifact from a traced reference run."""

    _require_runtime_conformance_trace(result.trace)
    evidence = result.require_debug_evidence()
    root_refs = _sorted_unique_refs(
        evidence.continuation_root_refs,
        context="TraceResult.debug_evidence.continuation_root_refs",
    )
    continuation_objects = _reachable_snapshot(evidence.continuation_objects, root_refs)
    artifact = _artifact_from_parts(
        trace_json=trace_to_json(result.trace),
        continuation_root_refs=root_refs,
        continuation_ref_map=evidence.continuation_ref_map,
        continuation_control_ref_map=evidence.continuation_control_ref_map,
        context_ref_map=evidence.context_ref_map,
        continuation_objects=continuation_objects,
        validation_profile="runtime-with-continuations",
        program_ref=evidence.program_ref,
        program_profile=program_profile,
        source_outcome_json=source_outcome_json,
        tool_versions=tool_versions,
    )
    validate_conformance_artifact(artifact)
    return artifact


def artifact_from_trace_evidence_bundle(
    bundle: TraceEvidenceBundle,
    *,
    program_ref: Ref | None = None,
    program_profile: Mapping[str, JsonValue] | None = None,
    source_outcome_json: Mapping[str, JsonValue] | None = None,
    tool_versions: Mapping[str, str] | None = None,
) -> ConformanceArtifact:
    """Build a canonical conformance artifact from an already-normalized evidence bundle."""

    trace = tuple(bundle.trace.kernel) if hasattr(bundle.trace, "kernel") else tuple(bundle.trace)
    _require_runtime_conformance_trace(trace)
    validate_trace_evidence(bundle)
    if bundle.validation_profile == "lifecycle-only" and (bundle.continuation_root_refs or bundle.continuation_objects):
        raise ConformanceArtifactValidationError(
            "lifecycle-only conformance artifacts must not carry continuation evidence"
        )
    root_refs = _sorted_unique_refs(bundle.continuation_root_refs, context="TraceEvidenceBundle.continuation_root_refs")
    continuation_objects = (
        {}
        if bundle.validation_profile == "lifecycle-only"
        else _reachable_snapshot(bundle.continuation_objects, root_refs)
    )
    artifact = _artifact_from_parts(
        trace_json=trace_to_json(trace),
        continuation_root_refs=root_refs,
        continuation_ref_map=bundle.continuation_ref_map,
        continuation_control_ref_map=bundle.continuation_control_ref_map,
        context_ref_map=bundle.context_ref_map,
        continuation_objects=continuation_objects,
        validation_profile=bundle.validation_profile,
        program_ref=program_ref,
        program_profile=program_profile,
        source_outcome_json=source_outcome_json,
        tool_versions=tool_versions,
    )
    validate_conformance_artifact(artifact)
    return artifact


def _require_runtime_conformance_trace(trace: tuple[TraceRecord, ...] | list[TraceRecord]) -> None:
    if any(isinstance(record, _PUBLICATION_EXPERIMENTAL_RECORD_TYPES) for record in trace):
        raise ConformanceArtifactValidationError(_PUBLICATION_EVIDENCE_UNSUPPORTED)


def _artifact_from_parts(
    *,
    trace_json: Iterable[Mapping[str, JsonValue]],
    continuation_root_refs: Iterable[Ref],
    continuation_ref_map: Mapping[Ref, Ref] | None = None,
    continuation_control_ref_map: Mapping[Ref, Ref] | None = None,
    context_ref_map: Mapping[Ref, Ref] | None = None,
    continuation_objects: Mapping[Ref, ContinuationObject],
    validation_profile: TraceEvidenceValidationProfile,
    program_ref: Ref | None,
    program_profile: Mapping[str, JsonValue] | None,
    source_outcome_json: Mapping[str, JsonValue] | None,
    tool_versions: Mapping[str, str] | None,
) -> ConformanceArtifact:
    return ConformanceArtifact(
        artifact_schema_version=CONFORMANCE_ARTIFACT_SCHEMA_VERSION,
        artifact_kind=_ARTIFACT_KIND,
        validation_profile=validation_profile,
        trace_json=tuple(trace_json),
        continuation_root_refs=tuple(continuation_root_refs),
        continuation_ref_map={} if continuation_ref_map is None else continuation_ref_map,
        continuation_control_ref_map={} if continuation_control_ref_map is None else continuation_control_ref_map,
        context_ref_map={} if context_ref_map is None else context_ref_map,
        continuation_objects=_continuation_object_entries(continuation_objects),
        program_ref=program_ref,
        program_profile=program_profile,
        source_outcome_json=source_outcome_json,
        schema_versions=_default_schema_versions(),
        tool_versions={} if tool_versions is None else tool_versions,
    )


def _continuation_object_entries(
    continuation_objects: Mapping[Ref, ContinuationObject],
) -> tuple[ConformanceContinuationObject, ...]:
    return tuple(
        ConformanceContinuationObject(ref=ref, object_json=continuation_object_to_json(continuation_objects[ref]))
        for ref in sorted(continuation_objects)
    )


def _decode_continuation_objects(
    entries: Iterable[ConformanceContinuationObject],
) -> dict[Ref, ContinuationObject]:
    objects: dict[Ref, ContinuationObject] = {}
    for entry in entries:
        obj = continuation_object_from_json(entry.object_json)
        actual_ref = continuation_object_ref(obj)
        if actual_ref != entry.ref:
            raise ConformanceArtifactValidationError(
                f"continuation object entry ref {entry.ref!r} does not match content ref {actual_ref!r}"
            )
        if entry.ref in objects:
            raise ConformanceArtifactValidationError(f"duplicate continuation object ref: {entry.ref!r}")
        objects[entry.ref] = obj
    return objects


def _reachable_snapshot(
    objects: Mapping[Ref, ContinuationObject],
    root_refs: Iterable[Ref],
) -> dict[Ref, ContinuationObject]:
    refs = _collect_reachable_refs(objects, root_refs)
    return {ref: objects[ref] for ref in sorted(refs)}


def _validate_exact_reachable_snapshot(
    root_refs: Iterable[Ref],
    objects: Mapping[Ref, ContinuationObject],
) -> None:
    reachable_refs = _collect_reachable_refs(objects, root_refs)
    object_refs = set(objects)
    if object_refs != reachable_refs:
        missing = sorted(reachable_refs - object_refs)
        extra = sorted(object_refs - reachable_refs)
        raise ConformanceArtifactValidationError(
            "runtime-with-continuations artifact continuation_objects must equal the reachable snapshot "
            f"(missing={missing!r}, extra={extra!r})"
        )


def _collect_reachable_refs(
    objects: Mapping[Ref, ContinuationObject],
    root_refs: Iterable[Ref],
) -> set[Ref]:
    refs: set[Ref] = set()
    work = list(root_refs)
    while work:
        ref = work.pop()
        if ref in refs:
            continue
        obj = objects.get(ref)
        if obj is None:
            raise ConformanceArtifactValidationError(f"missing continuation object evidence for {ref!r}")
        if ref not in refs and ref in root_refs and not isinstance(obj, ContinuationRoot):
            raise ConformanceArtifactValidationError(f"continuation root ref {ref!r} does not resolve to a root object")
        refs.add(ref)
        work.extend(continuation_object_child_refs(obj))
    return refs


def _validate_program_ref_metadata(
    program_ref: Ref | None,
    trace: Iterable[object],
    continuation_objects: Iterable[ContinuationObject],
) -> None:
    if program_ref is None:
        return
    observed: set[Ref] = set()
    for record in trace:
        record_program_ref = getattr(record, "program_ref", None)
        if isinstance(record_program_ref, str):
            observed.add(record_program_ref)
    for obj in continuation_objects:
        if isinstance(obj, ContinuationRoot):
            observed.add(obj.program_ref)
    mismatches = sorted(ref for ref in observed if ref != program_ref)
    if mismatches:
        raise ConformanceArtifactValidationError(
            f"artifact program_ref {program_ref!r} does not match trace/root program refs {mismatches!r}"
        )


def _trace_continuation_refs(trace: Iterable[object]) -> set[Ref]:
    refs: set[Ref] = set()
    for record in trace:
        for field_name in _CONTINUATION_REF_FIELDS:
            value = getattr(record, field_name, None)
            if isinstance(value, str):
                refs.add(value)
    return refs


def _default_schema_versions() -> Mapping[str, str]:
    return {
        "conformance_artifact": CONFORMANCE_ARTIFACT_SCHEMA_VERSION,
        "trace_evidence_bundle": TRACE_EVIDENCE_BUNDLE_SCHEMA_VERSION,
        "continuation_object": CONTINUATION_OBJECT_SCHEMA_VERSION,
    }


def _freeze_program_profile(value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    context = "ConformanceArtifact.program_profile"
    frozen = _freeze_mapping_value(value, context=context)
    keys = frozenset(frozen)
    if keys != _PROGRAM_PROFILE_KEYS:
        missing = sorted(_PROGRAM_PROFILE_KEYS - keys)
        extra = sorted(keys - _PROGRAM_PROFILE_KEYS)
        raise TypeError(
            f"{context} must have exactly name/version/validated keys (missing={missing!r}, extra={extra!r})"
        )
    if not isinstance(frozen["name"], str):
        raise TypeError(f"{context}.name must be a string")
    if not isinstance(frozen["version"], str):
        raise TypeError(f"{context}.version must be a string")
    if not isinstance(frozen["validated"], bool):
        raise TypeError(f"{context}.validated must be a bool")
    return frozen


def _freeze_schema_versions(value: Mapping[str, str]) -> Mapping[str, str]:
    context = "ConformanceArtifact.schema_versions"
    frozen = _freeze_str_mapping(value, context=context)
    for key, expected in _default_schema_versions().items():
        actual = frozen.get(key)
        if actual is None:
            raise TypeError(f"{context} missing required key {key!r}")
        if actual != expected:
            raise TypeError(f"{context}.{key} must be {expected!r}, got {actual!r}")
    return frozen


def _sorted_unique_object_entries(
    entries: Iterable[ConformanceContinuationObject],
) -> tuple[ConformanceContinuationObject, ...]:
    by_ref: dict[Ref, ConformanceContinuationObject] = {}
    for entry in entries:
        if entry.ref in by_ref:
            raise ConformanceArtifactValidationError(f"duplicate continuation object ref: {entry.ref!r}")
        by_ref[entry.ref] = entry
    return tuple(by_ref[ref] for ref in sorted(by_ref))


def _sorted_unique_refs(refs: Iterable[Ref], *, context: str) -> tuple[Ref, ...]:
    seen: set[Ref] = set()
    result: list[Ref] = []
    for ref in refs:
        if not isinstance(ref, str):
            raise TypeError(f"{context} entries must be strings")
        if ref in seen:
            raise ConformanceArtifactValidationError(f"duplicate ref in {context}: {ref!r}")
        seen.add(ref)
        result.append(ref)
    return tuple(sorted(result))


def _freeze_mapping_value(value: Any, *, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{context} must be a mapping")
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError(f"{context} contains a non-string mapping key")
        frozen[key] = _freeze_json_compatible(item, context=f"{context}.{key}")
    return _FrozenJsonDict(frozen)


def _freeze_json_compatible(value: Any, *, context: str) -> Any:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise TypeError(f"{context} contains a non-finite float")
        return value
    if isinstance(value, tuple | list):
        return tuple(_freeze_json_compatible(item, context=f"{context}[{idx}]") for idx, item in enumerate(value))
    if isinstance(value, Mapping):
        return _freeze_mapping_value(value, context=context)
    raise TypeError(f"{context} contains a non-JSON-compatible value: {value!r}")


def _freeze_str_mapping(value: Mapping[str, str], *, context: str) -> Mapping[str, str]:
    frozen = _freeze_mapping_value(value, context=context)
    for key, item in frozen.items():
        if not isinstance(item, str):
            raise TypeError(f"{context}.{key} must be a string")
    return frozen


def _jsonify(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_jsonify(item) for item in value]
    if isinstance(value, list):
        return [_jsonify(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _jsonify(item) for key, item in value.items()}
    return value


def _require_str(data: Mapping[str, JsonValue], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ConformanceArtifactSerializationError(f"{key} must be a string")
    return value


def _reject_unknown_keys(data: Mapping[str, JsonValue], expected: frozenset[str], *, context: str) -> None:
    extra = sorted(str(key) for key in data if key not in expected)
    if extra:
        raise ConformanceArtifactSerializationError(f"{context} contains unknown keys: {extra!r}")


def _require_optional_str(data: Mapping[str, JsonValue], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConformanceArtifactSerializationError(f"{key} must be a string or null")
    return value


def _require_artifact_kind(data: Mapping[str, JsonValue], key: str) -> ConformanceArtifactKind:
    value = _require_str(data, key)
    if value != _ARTIFACT_KIND:
        raise ConformanceArtifactSerializationError(f"{key} must be {_ARTIFACT_KIND!r}")
    return value


def _require_validation_profile(data: Mapping[str, JsonValue], key: str) -> TraceEvidenceValidationProfile:
    value = _require_str(data, key)
    if value == "lifecycle-only":
        return "lifecycle-only"
    if value == "runtime-with-continuations":
        return "runtime-with-continuations"
    raise ConformanceArtifactSerializationError(f"unknown {key}: {value!r}")


def _require_list(data: Mapping[str, JsonValue], key: str) -> list[JsonValue]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ConformanceArtifactSerializationError(f"{key} must be a list")
    return value


def _require_str_list(data: Mapping[str, JsonValue], key: str) -> list[str]:
    value = _require_list(data, key)
    if not all(isinstance(item, str) for item in value):
        raise ConformanceArtifactSerializationError(f"{key} entries must be strings")
    return value


def _require_mapping(data: Mapping[str, JsonValue], key: str) -> Mapping[str, JsonValue]:
    return _require_mapping_value(data.get(key), context=key)


def _require_optional_mapping(data: Mapping[str, JsonValue], key: str) -> Mapping[str, JsonValue] | None:
    value = data.get(key)
    if value is None:
        return None
    return _require_mapping_value(value, context=key)


def _require_mapping_value(value: JsonValue, *, context: str) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise ConformanceArtifactSerializationError(f"{context} must be an object")
    return value


def _require_str_mapping(data: Mapping[str, JsonValue], key: str) -> Mapping[str, str]:
    value = _require_mapping(data, key)
    result: dict[str, str] = {}
    for item_key, item in value.items():
        if not isinstance(item_key, str) or not isinstance(item, str):
            raise ConformanceArtifactSerializationError(f"{key} must map strings to strings")
        result[item_key] = item
    return result
