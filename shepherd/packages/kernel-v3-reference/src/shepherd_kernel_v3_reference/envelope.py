"""Kernel result envelope and rejection taxonomy for the `-lite` contract.

Per `260521-0600-kernel.md` §"Kernel Result Envelope" and 2026-05-24
§"Settled Design Decisions" entries "Post-#72 design pass" (item D/E) and
"Pre-#73 micro-design pass" (KernelRejection field shape).

The envelope is the uniform return shape for the normative API
(`start_kernel_run`, `resume_kernel_run`, `validate_observation_stream`).
It carries the operational `transition` (cheap, always present except for
pre-execution `profile-admission` rejections) and a typed `payload`. The
conformance projection (`WireResult.batch`) is produced on demand by
`project_envelope_to_wire(...)` (lives in `projection.py`).

Status enum (4 values):

  completed                — terminal success
  external-effect-request  — paused for host observation
  rejected                 — runtime/execution rejection (post-admission failure)
  profile-rejected         — pre-execution rejection (admission-stage failure)

The `transition` field is `Optional`: required for `completed` /
`external-effect-request` / `rejected`; None for `profile-rejected`
(no transition was constructed because admission rejected the program).

KernelRejection is a single frozen dataclass with optional per-kind
fields and a `__post_init__` that enforces which fields are populated
for which kind (single-type-with-discriminator pattern matching the
existing `ContinuationSource.__post_init__` precedent in `semantic.py`).
The four-value `kind` taxonomy maps 1:1 to the validator call chain:

  profile-admission       — non-`-lite` source construct
  kernel-admission        — structural failure (cycles, missing refs)
  execution-failure       — post-admission deterministic kernel failure
  observation-admission   — specific observation in a stream fails admission
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from shepherd_kernel_v3_reference.kernel.replay import (
    ExternalEffectRequest,
    ReplayableKernelTransition,
)

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.profiles import SemanticProfile
    from shepherd_kernel_v3_reference.semantic import (
        ProfileRejected,
        SemanticTransitionBatch,
    )
    from shepherd_kernel_v3_reference.trace.records import TraceRecord

KERNEL_VERSION = "shepherd_kernel_v3_reference.kernel.v0"

EnvelopeStatus = Literal[
    "completed",
    "external-effect-request",
    "rejected",
    "profile-rejected",
]

RejectionKind = Literal[
    "profile-admission",
    "kernel-admission",
    "execution-failure",
    "observation-admission",
]


@dataclass(frozen=True)
class SourceLocation:
    """Best-effort source-AST location for diagnostic precision.

    Populated by `validate_profile_admission(...)` in #75 when it rejects
    a non-`-lite` construct. Fields are best-effort: source positions
    are not always recoverable from the source AST today, so most fields
    default to None and consumers should treat any populated field as a
    pointer rather than an authoritative location.
    """

    construct_path: str | None = None  # e.g., "Handle.body.Let[1].body"
    line: int | None = None
    column: int | None = None


@dataclass(frozen=True)
class CompletedResult:
    """Terminal successful run payload."""

    program_ref: str
    value: object

    def __post_init__(self) -> None:
        if not self.program_ref:
            raise ValueError("CompletedResult.program_ref must be non-empty")


@dataclass(frozen=True)
class KernelRejection:
    """Pre-execution or runtime rejection payload for KernelResultEnvelope.

    Single frozen dataclass with optional per-kind fields and a
    `__post_init__` that enforces which fields are populated for which
    kind. Single-type-with-discriminator pattern; consumers discriminate
    on `.kind` and read the kind-specific optional fields.

    Per 2026-05-24 §"Settled Design Decisions" — "Pre-#73 micro-design pass".
    """

    kind: RejectionKind
    diagnostic: str
    program_ref: str | None = None

    # profile-admission only:
    construct: str | None = None
    source_location: SourceLocation | None = None

    # observation-admission only:
    rejection_index: int | None = None
    rejection_class: str | None = None

    # execution-failure only:
    partial_records: tuple[TraceRecord, ...] = ()

    def __post_init__(self) -> None:
        if not self.diagnostic:
            raise ValueError("KernelRejection.diagnostic must be non-empty")

        if self.kind == "profile-admission":
            if self.construct is None:
                raise ValueError(
                    "KernelRejection(kind='profile-admission') requires `construct`"
                )
            if self.program_ref is not None:
                raise ValueError(
                    "KernelRejection(kind='profile-admission') must not carry "
                    "`program_ref`: rejection occurs before program identity is computed"
                )
        else:
            if self.program_ref is None:
                raise ValueError(
                    f"KernelRejection(kind={self.kind!r}) requires `program_ref`"
                )
            if self.construct is not None or self.source_location is not None:
                raise ValueError(
                    f"KernelRejection(kind={self.kind!r}) must not carry `construct` "
                    "or `source_location` (those are profile-admission-only fields)"
                )

        if self.kind == "observation-admission":
            if self.rejection_index is None or self.rejection_class is None:
                raise ValueError(
                    "KernelRejection(kind='observation-admission') requires both "
                    "`rejection_index` and `rejection_class`"
                )
        elif self.rejection_index is not None or self.rejection_class is not None:
            raise ValueError(
                f"KernelRejection(kind={self.kind!r}) must not carry "
                "`rejection_index` or `rejection_class` "
                "(those are observation-admission-only fields)"
            )

        if self.kind != "execution-failure" and self.partial_records:
            raise ValueError(
                f"KernelRejection(kind={self.kind!r}) must not carry `partial_records` "
                "(that is execution-failure-only)"
            )


KernelResultPayload = CompletedResult | ExternalEffectRequest | KernelRejection


@dataclass(frozen=True)
class KernelResultEnvelope:
    """Uniform return shape for the normative API.

    `transition` is required for status in {completed, external-effect-request,
    rejected}; None for `profile-rejected` (no transition was constructed
    because the program failed admission before any execution).

    Per 260521-0600-kernel.md §"Kernel Result Envelope" + 2026-05-24
    §"Post-#72 design pass" item D/E.
    """

    profile: SemanticProfile
    status: EnvelopeStatus
    payload: KernelResultPayload
    transition: ReplayableKernelTransition | None = None
    kernel_version: str = KERNEL_VERSION

    def __post_init__(self) -> None:
        # Status-conditioned transition presence
        if self.status == "profile-rejected":
            if self.transition is not None:
                raise ValueError(
                    "KernelResultEnvelope(status='profile-rejected') must not carry "
                    "a transition (no transition is constructed for pre-execution "
                    "admission failures)"
                )
        elif self.transition is None:
            raise ValueError(
                f"KernelResultEnvelope(status={self.status!r}) requires a transition"
            )

        # Payload <-> status agreement
        if self.status == "completed":
            if not isinstance(self.payload, CompletedResult):
                raise TypeError(
                    "KernelResultEnvelope(status='completed') payload must be a "
                    "CompletedResult"
                )
        elif self.status == "external-effect-request":
            if not isinstance(self.payload, ExternalEffectRequest):
                raise TypeError(
                    "KernelResultEnvelope(status='external-effect-request') payload "
                    "must be an ExternalEffectRequest"
                )
        elif self.status == "rejected":
            if not isinstance(self.payload, KernelRejection):
                raise TypeError(
                    "KernelResultEnvelope(status='rejected') payload must be a "
                    "KernelRejection"
                )
            if self.payload.kind == "profile-admission":
                raise ValueError(
                    "KernelResultEnvelope(status='rejected') payload kind must not "
                    "be 'profile-admission' (use status='profile-rejected' for that)"
                )
        elif self.status == "profile-rejected":
            if not isinstance(self.payload, KernelRejection):
                raise TypeError(
                    "KernelResultEnvelope(status='profile-rejected') payload must be "
                    "a KernelRejection"
                )
            if self.payload.kind != "profile-admission":
                raise ValueError(
                    "KernelResultEnvelope(status='profile-rejected') payload kind "
                    "must be 'profile-admission'"
                )


@dataclass(frozen=True)
class WireResult:
    """Conformance projection of a KernelResultEnvelope.

    Carries the operational envelope plus its semantic projection
    (`SemanticTransitionBatch` or `ProfileRejected`). Produced by
    `project_envelope_to_wire(envelope, state, catalog)` from projection.py.

    `WireResult` is what Lean Phase 9 differential testing consumes.
    """

    envelope: KernelResultEnvelope
    batch: SemanticTransitionBatch | ProfileRejected = field()


__all__ = [
    "KERNEL_VERSION",
    "CompletedResult",
    "EnvelopeStatus",
    "KernelRejection",
    "KernelResultEnvelope",
    "KernelResultPayload",
    "RejectionKind",
    "SourceLocation",
    "WireResult",
]
