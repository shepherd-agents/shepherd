"""Proof-envelope claims for kernel-v3 traces.

The envelope is intentionally conservative: the Python reference validator can
mint reference-validation claims, while Lean-backed claims require explicit
evidence and theorem identifiers from the checked proof surface.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypeAlias

from shepherd_kernel_v3_reference.kernel.refs import content_ref
from shepherd_kernel_v3_reference.trace.serde import trace_to_json
from shepherd_kernel_v3_reference.trace.validate import (
    TraceValidationError,
    validate_core_a_trace,
    validate_core_a_trace_prefix,
)

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.kernel.ir import Ref
    from shepherd_kernel_v3_reference.trace.records import TraceRecord

JsonValue: TypeAlias = Any
MetadataItems: TypeAlias = tuple[tuple[str, JsonValue], ...]

PROOF_ENVELOPE_SCHEMA_VERSION = "shepherd_kernel_v3_reference.proof-envelope.v1"
PROOF_ENVELOPE_VALIDATOR = "shepherd-kernel-v3-reference.proof-envelope.v1"
PROOF_EVIDENCE_REF_KIND = "proof-evidence"
_PROOF_EVIDENCE_RE = re.compile(r"^proof-evidence:sha256:[0-9a-f]{64}$")
_PROGRAM_REF_RE = re.compile(r"^program:sha256:[0-9a-f]{64}$")
_TRACE_REF_RE = re.compile(r"^trace(?:-prefix)?:sha256:[0-9a-f]{64}$")

PROOF_SURFACE_THEOREM_IDS = (
    "source_eval_to_machine",
    "core0_machine_eval_to_source",
    "coreA_machine_eval_to_source",
    "core0h_source_eval_to_machine",
    "trace_monotonic",
    "core0h_trace_monotonic",
    "single_child_branch_replay_sound",
)
EXTENSION_PROOF_SURFACE_THEOREM_IDS = (
    "vcscore_run_record_sound",
)


class ProofEnvelopeError(ValueError):
    """Raised when a proof envelope overclaims its evidence."""


class ProofProfile(StrEnum):
    """Claim profiles named by the frozen paper proof surface."""

    RUNTIME_ONLY = "runtime_only"
    REFERENCE_CORE_A = "reference_core_a"
    CORE0 = "core0"
    CORE_A = "core_a"
    CORE0H = "core0h"
    EXTENSION = "extension"


class ProofStrength(StrEnum):
    """Claim strength attached to a proof profile."""

    RUNTIME_ONLY = "runtime_only"
    REFERENCE_VALIDATED = "reference_validated"
    FORWARD_SIMULATION = "forward_simulation"
    SEMANTIC_ADEQUACY = "semantic_adequacy"


_PROFILE_STRENGTHS: Mapping[ProofProfile, frozenset[ProofStrength]] = {
    ProofProfile.RUNTIME_ONLY: frozenset({ProofStrength.RUNTIME_ONLY}),
    ProofProfile.REFERENCE_CORE_A: frozenset({ProofStrength.REFERENCE_VALIDATED}),
    ProofProfile.CORE0: frozenset({ProofStrength.SEMANTIC_ADEQUACY}),
    ProofProfile.CORE_A: frozenset({ProofStrength.SEMANTIC_ADEQUACY}),
    ProofProfile.CORE0H: frozenset({ProofStrength.FORWARD_SIMULATION}),
    ProofProfile.EXTENSION: frozenset(
        {ProofStrength.RUNTIME_ONLY, ProofStrength.REFERENCE_VALIDATED, ProofStrength.SEMANTIC_ADEQUACY}
    ),
}

_PROFILE_THEOREM_IDS: Mapping[ProofProfile, tuple[str, ...]] = {
    ProofProfile.CORE0: (
        "source_eval_to_machine",
        "core0_machine_eval_to_source",
        "trace_monotonic",
    ),
    ProofProfile.CORE_A: (
        "source_eval_to_machine",
        "coreA_machine_eval_to_source",
        "trace_monotonic",
    ),
    ProofProfile.CORE0H: (
        "core0h_source_eval_to_machine",
        "core0h_trace_monotonic",
    ),
}


@dataclass(frozen=True)
class ProofEnvelope:
    """Inspectable proof claim attached to a run or reference trace."""

    profile: ProofProfile
    strength: ProofStrength
    evidence_id: Ref | None = None
    program_ref: Ref | None = None
    trace_ref: Ref | None = None
    theorem_ids: tuple[str, ...] = ()
    validator: str = PROOF_ENVELOPE_VALIDATOR
    schema_version: str = PROOF_ENVELOPE_SCHEMA_VERSION
    metadata: MetadataItems | Mapping[str, JsonValue] | Iterable[tuple[str, JsonValue]] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        profile = _coerce_profile(self.profile)
        strength = _coerce_strength(self.strength)
        theorem_ids = tuple(self.theorem_ids)
        if not theorem_ids and profile in _PROFILE_THEOREM_IDS:
            theorem_ids = _PROFILE_THEOREM_IDS[profile]

        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "strength", strength)
        object.__setattr__(self, "theorem_ids", theorem_ids)
        object.__setattr__(self, "metadata", _metadata_items(self.metadata))

        _require_ref_or_none(self.evidence_id, "ProofEnvelope.evidence_id")
        _require_ref_or_none(self.program_ref, "ProofEnvelope.program_ref")
        _require_ref_or_none(self.trace_ref, "ProofEnvelope.trace_ref")
        _require_program_ref_or_none(self.program_ref)
        _require_trace_ref_or_none(self.trace_ref)
        if self.validator != PROOF_ENVELOPE_VALIDATOR:
            raise ProofEnvelopeError(
                f"ProofEnvelope.validator must be {PROOF_ENVELOPE_VALIDATOR!r}, got {self.validator!r}"
            )
        if self.schema_version != PROOF_ENVELOPE_SCHEMA_VERSION:
            raise ProofEnvelopeError(
                f"ProofEnvelope.schema_version must be {PROOF_ENVELOPE_SCHEMA_VERSION!r}, got {self.schema_version!r}"
            )

        allowed = _PROFILE_STRENGTHS[profile]
        if strength not in allowed:
            raise ProofEnvelopeError(
                f"profile {profile.value!r} cannot carry strength {strength.value!r}; "
                f"allowed strengths are {[item.value for item in sorted(allowed, key=lambda value: value.value)]!r}"
            )
        if strength != ProofStrength.RUNTIME_ONLY and self.evidence_id is None:
            raise ProofEnvelopeError(f"{strength.value!r} envelopes require evidence_id")
        if strength != ProofStrength.RUNTIME_ONLY:
            _require_proof_evidence_ref(self.evidence_id)
            if self.program_ref is None:
                raise ProofEnvelopeError(f"{strength.value!r} envelopes require program_ref")
            if self.trace_ref is None:
                raise ProofEnvelopeError(f"{strength.value!r} envelopes require trace_ref")
        if strength == ProofStrength.RUNTIME_ONLY and theorem_ids:
            raise ProofEnvelopeError("runtime_only envelopes must not list Lean theorem ids")
        if profile in _PROFILE_THEOREM_IDS and theorem_ids != _PROFILE_THEOREM_IDS[profile]:
            raise ProofEnvelopeError(
                f"profile {profile.value!r} theorem ids must be {_PROFILE_THEOREM_IDS[profile]!r}"
            )
        if profile == ProofProfile.EXTENSION and strength == ProofStrength.SEMANTIC_ADEQUACY:
            if not theorem_ids:
                raise ProofEnvelopeError("semantic_adequacy extension envelopes require Lean theorem ids")
            unsupported = sorted(set(theorem_ids) - set(EXTENSION_PROOF_SURFACE_THEOREM_IDS))
            if unsupported:
                raise ProofEnvelopeError(f"unsupported extension theorem ids: {unsupported!r}")

    @property
    def lean_backed(self) -> bool:
        """Whether this envelope claims coverage by a Lean theorem surface."""

        return self.strength in {ProofStrength.FORWARD_SIMULATION, ProofStrength.SEMANTIC_ADEQUACY}

    @property
    def proof_backed(self) -> bool:
        """Whether this envelope claims proof-backed semantic coverage."""

        return self.lean_backed

    @property
    def reference_validated(self) -> bool:
        """Whether this envelope's evidence is executable reference validation."""

        return self.strength is ProofStrength.REFERENCE_VALIDATED

    def to_json(self) -> dict[str, JsonValue]:
        """Return the stable JSON-compatible envelope shape."""

        return {
            "schema_version": self.schema_version,
            "validator": self.validator,
            "profile": self.profile.value,
            "strength": self.strength.value,
            "evidence_id": self.evidence_id,
            "program_ref": self.program_ref,
            "trace_ref": self.trace_ref,
            "theorem_ids": list(self.theorem_ids),
            "metadata": dict(self.metadata),
        }

    def envelope_ref(self) -> Ref:
        """Content reference for the envelope itself."""

        return content_ref("proof-envelope", self.to_json())


def proof_envelope_from_json(data: Mapping[str, JsonValue]) -> ProofEnvelope:
    """Decode a JSON-compatible proof envelope."""

    metadata = data.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ProofEnvelopeError("ProofEnvelope.metadata must be a mapping")
    theorem_ids = data.get("theorem_ids", ())
    if not isinstance(theorem_ids, list | tuple):
        raise ProofEnvelopeError("ProofEnvelope.theorem_ids must be a list")
    return ProofEnvelope(
        profile=_required_str(data, "profile"),
        strength=_required_str(data, "strength"),
        evidence_id=_optional_str(data, "evidence_id"),
        program_ref=_optional_str(data, "program_ref"),
        trace_ref=_optional_str(data, "trace_ref"),
        theorem_ids=tuple(_require_str_item(item, "ProofEnvelope.theorem_ids") for item in theorem_ids),
        validator=_required_str(data, "validator"),
        schema_version=_required_str(data, "schema_version"),
        metadata=metadata,
    )


def runtime_only_envelope(*, reason: str | None = None) -> ProofEnvelope:
    """Return the default non-proof-backed envelope for ordinary Python runs."""

    metadata: MetadataItems = () if reason is None else (("reason", reason),)
    return ProofEnvelope(
        profile=ProofProfile.RUNTIME_ONLY,
        strength=ProofStrength.RUNTIME_ONLY,
        metadata=metadata,
    )


def trace_ref(trace: tuple[TraceRecord, ...] | list[TraceRecord], *, completed: bool = True) -> Ref:
    """Return the content ref used by proof envelopes for a trace."""

    kind = "trace" if completed else "trace-prefix"
    return content_ref(kind, trace_to_json(trace))


def reference_core_a_envelope(
    trace: tuple[TraceRecord, ...] | list[TraceRecord],
    *,
    program_ref: Ref,
    completed: bool = True,
) -> ProofEnvelope:
    """Validate a trace against the executable Core-A validator and wrap it."""

    normalized = tuple(trace)
    if completed:
        validate_core_a_trace(normalized)
    else:
        validate_core_a_trace_prefix(normalized)

    ref = trace_ref(normalized, completed=completed)
    evidence_payload = {
        "proof_authority": PROOF_ENVELOPE_VALIDATOR,
        "validator": "validate_core_a_trace" if completed else "validate_core_a_trace_prefix",
        "envelope_schema_version": PROOF_ENVELOPE_SCHEMA_VERSION,
        "profile": ProofProfile.REFERENCE_CORE_A.value,
        "strength": ProofStrength.REFERENCE_VALIDATED.value,
        "program_ref": program_ref,
        "trace_ref": ref,
        "completed": completed,
    }
    return ProofEnvelope(
        profile=ProofProfile.REFERENCE_CORE_A,
        strength=ProofStrength.REFERENCE_VALIDATED,
        evidence_id=content_ref(PROOF_EVIDENCE_REF_KIND, evidence_payload),
        program_ref=program_ref,
        trace_ref=ref,
        metadata=(
            ("completed", completed),
            ("validator", evidence_payload["validator"]),
        ),
    )


def classify_trace_envelope(
    trace: tuple[TraceRecord, ...] | list[TraceRecord],
    *,
    program_ref: Ref | None = None,
    completed: bool = True,
) -> ProofEnvelope:
    """Return the strongest envelope the Python reference validator can defend."""

    if program_ref is None:
        return runtime_only_envelope(reason="missing-program-ref")
    try:
        return reference_core_a_envelope(trace, program_ref=program_ref, completed=completed)
    except TraceValidationError:
        return runtime_only_envelope(reason="trace-not-reference-core-a")


def _coerce_profile(value: ProofProfile | str) -> ProofProfile:
    try:
        return value if isinstance(value, ProofProfile) else ProofProfile(value)
    except ValueError:
        raise ProofEnvelopeError(f"unknown proof profile: {value!r}") from None


def _coerce_strength(value: ProofStrength | str) -> ProofStrength:
    try:
        return value if isinstance(value, ProofStrength) else ProofStrength(value)
    except ValueError:
        raise ProofEnvelopeError(f"unknown proof strength: {value!r}") from None


def _metadata_items(value: MetadataItems | Mapping[str, JsonValue] | Iterable[tuple[str, JsonValue]]) -> MetadataItems:
    if isinstance(value, Mapping):
        items = tuple(value.items())
    else:
        items = tuple(value)
    seen: set[str] = set()
    normalized: list[tuple[str, JsonValue]] = []
    for key, item in items:
        if not isinstance(key, str):
            raise ProofEnvelopeError(f"ProofEnvelope.metadata keys must be strings, got {key!r}")
        if key in seen:
            raise ProofEnvelopeError(f"duplicate ProofEnvelope.metadata key: {key!r}")
        seen.add(key)
        content_ref("proof-envelope-metadata-value", item)
        normalized.append((key, item))
    return tuple(sorted(normalized, key=lambda entry: entry[0]))


def _require_ref_or_none(value: object, context: str) -> None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{context} must be a ref string or None")


def _require_program_ref_or_none(value: Ref | None) -> None:
    if value is not None and _PROGRAM_REF_RE.fullmatch(value) is None:
        raise ProofEnvelopeError("ProofEnvelope.program_ref must be a program:sha256:<digest> ref")


def _require_trace_ref_or_none(value: Ref | None) -> None:
    if value is not None and _TRACE_REF_RE.fullmatch(value) is None:
        raise ProofEnvelopeError("ProofEnvelope.trace_ref must be a trace:sha256:<digest> or trace-prefix:sha256:<digest> ref")


def _require_proof_evidence_ref(value: Ref | None) -> None:
    if value is None or _PROOF_EVIDENCE_RE.fullmatch(value) is None:
        raise ProofEnvelopeError("non-runtime proof envelopes require a proof-evidence:sha256:<digest> evidence_id")


def _required_str(data: Mapping[str, JsonValue], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ProofEnvelopeError(f"ProofEnvelope.{key} must be a string")
    return value


def _optional_str(data: Mapping[str, JsonValue], key: str) -> str | None:
    value = data.get(key)
    if value is None or isinstance(value, str):
        return value
    raise ProofEnvelopeError(f"ProofEnvelope.{key} must be a string or None")


def _require_str_item(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        raise ProofEnvelopeError(f"{context} entries must be strings")
    return value


__all__ = [
    "EXTENSION_PROOF_SURFACE_THEOREM_IDS",
    "PROOF_ENVELOPE_SCHEMA_VERSION",
    "PROOF_ENVELOPE_VALIDATOR",
    "PROOF_EVIDENCE_REF_KIND",
    "PROOF_SURFACE_THEOREM_IDS",
    "ProofEnvelope",
    "ProofEnvelopeError",
    "ProofProfile",
    "ProofStrength",
    "classify_trace_envelope",
    "proof_envelope_from_json",
    "reference_core_a_envelope",
    "runtime_only_envelope",
    "trace_ref",
]
