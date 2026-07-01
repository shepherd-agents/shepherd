"""Reference interpreter for the shepherd-kernel-v3 source calculus (§02).

The top-level surface here is the *source-author API* — what you import
to write a kernel program: source AST (``Lit``, ``Var``, ``Let``,
``Perform``, ``Handle``, ``Return``, ``Resume``, ``Abort``,
``RecordExpr``, ``Computation``, ``Expr``, ``CoreComputation``),
schemas (``AnySchema``, ``TypeSchema``, ``TaggedRecordSchema``,
``RecordSchema``), handlers (``HandlerEnv``, ``StaticHandlerInstall``,
``DynamicHandlerInstall``), outcomes (``Completed``, ``Suspended``,
``Delayed``, ``Forked``), well-formedness (``validate_program``,
``validate_handler_body``), and the direct source evaluator
(:func:`run`). The :func:`run` driver evaluates a closed source program
and returns a :class:`SourceOutcome`; schemas validate at perform /
resume / handler-answer boundaries when an :class:`EffectRegistry` is
supplied.

The *normative consumer API* — for running prepared programs, validating
admitted observations, and projecting wire-shape results — lives in
named submodules. Import directly from those modules rather than from
this top-level namespace:

- ``run``: ``start_kernel_run``, ``resume_kernel_run``,
  ``validate_observation_stream``
- ``envelope``: ``KernelResultEnvelope``, ``WireResult``,
  ``KernelRejection``, ``CompletedResult``
- ``projection``: ``semantic_batch_from_transition``,
  ``project_envelope_to_wire``, ``validate_semantic_batch``
- ``profiles``: ``CORE_REFERENCE_V0_LITE``, ``CORE_A``, ``CORE0``,
  ``SemanticProfile``
- ``profile_admission``: ``validate_profile_admission``
- ``semantic``: ``SemanticTransitionBatch``, ``CanonicalRefMap``,
  ``ProfileRejected``, ``AdmissionBasis``, ``ContinuationSource``,
  ``ObservedFrontier``, ``OneShotKey``, ``SourceGeneration``
- ``kernel.admission``: ``AdmittedObservation``,
  ``validate_admitted_observation``, ``AdmittedObservationError``
- ``kernel.replay``: ``ReplayableKernelTransition``, ``HostCompleted``,
  ``ExternalEffectRequest``, ``KernelReplayState``,
  ``KernelReplayJournal``, ``KernelReplaySession``
- ``kernel``: ``admit_and_prepare`` (the only minter of a
  ``requires_source_admission`` profile), ``prepare_kernel_program``,
  ``ensure_prepared_kernel_program``

This split is deliberate (see ``260524-post-72-design-pass.md`` follow-up
review and the post-#77 cleanup tranche). The source-author surface and
the consumer surface address different audiences; keeping them in
distinct namespaces makes each import line name what role the symbol
plays. Top-level promotion remains additive and reversible if external
consumers ever justify shorter import paths; until then, namespaced
imports are the convention.
"""

from shepherd_kernel_v3_reference.proof_envelope import (
    ProofEnvelope,
    ProofEnvelopeError,
    ProofProfile,
    ProofStrength,
    classify_trace_envelope,
    reference_core_a_envelope,
    runtime_only_envelope,
)
from shepherd_kernel_v3_reference.schemas import (
    AnySchema,
    RecordSchema,
    Schema,
    TaggedRecordSchema,
    TypeSchema,
    ValidationError,
    schema_fingerprint,
)
from shepherd_kernel_v3_reference.source.effects import EffectRegistry, EffectSignature
from shepherd_kernel_v3_reference.source.eval_direct import AbortAfterResume, run
from shepherd_kernel_v3_reference.source.handlers import (
    AnswerCompletion,
    DynamicHandlerInstall,
    HandlerEnv,
    HandlerInstall,
    StaticHandlerInstall,
)
from shepherd_kernel_v3_reference.source.outcomes import (
    Completed,
    Continuation,
    Delayed,
    Forked,
    ResumptionUsed,
    SourceOutcome,
    Suspended,
)
from shepherd_kernel_v3_reference.source.syntax import (
    Abort,
    Computation,
    CoreComputation,
    Expr,
    Handle,
    Let,
    Lit,
    Perform,
    RecordExpr,
    Resume,
    Return,
    Var,
)
from shepherd_kernel_v3_reference.source.values import Env
from shepherd_kernel_v3_reference.source.wellformed import (
    SourceFormError,
    validate_handler_body,
    validate_program,
)
from shepherd_kernel_v3_reference.vcscore_certificate import (
    VcsCoreCertificateError,
    VcsCoreRunCertificate,
    is_vcscore_run_proof_envelope,
    validate_vcscore_run_proof_envelope,
    vcscore_run_certificate_from_json,
    vcscore_run_certificate_from_run_record,
    vcscore_run_proof_envelope,
    vcscore_run_proof_envelope_from_run_record,
)

__all__ = [
    "Abort",
    "AbortAfterResume",
    "AnswerCompletion",
    "AnySchema",
    "Completed",
    "Computation",
    "Continuation",
    "CoreComputation",
    "Delayed",
    "DynamicHandlerInstall",
    "EffectRegistry",
    "EffectSignature",
    "Env",
    "Expr",
    "Forked",
    "Handle",
    "HandlerEnv",
    "HandlerInstall",
    "Let",
    "Lit",
    "Perform",
    "ProofEnvelope",
    "ProofEnvelopeError",
    "ProofProfile",
    "ProofStrength",
    "RecordExpr",
    "RecordSchema",
    "Resume",
    "ResumptionUsed",
    "Return",
    "Schema",
    "SourceFormError",
    "SourceOutcome",
    "StaticHandlerInstall",
    "Suspended",
    "TaggedRecordSchema",
    "TypeSchema",
    "ValidationError",
    "Var",
    "VcsCoreCertificateError",
    "VcsCoreRunCertificate",
    "classify_trace_envelope",
    "is_vcscore_run_proof_envelope",
    "reference_core_a_envelope",
    "run",
    "runtime_only_envelope",
    "schema_fingerprint",
    "validate_handler_body",
    "validate_program",
    "validate_vcscore_run_proof_envelope",
    "vcscore_run_certificate_from_json",
    "vcscore_run_certificate_from_run_record",
    "vcscore_run_proof_envelope",
    "vcscore_run_proof_envelope_from_run_record",
]
