"""Private coordinator for world-vector transition preparation and selection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

import pygit2

from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._payload_authority import validate_payload_descriptor_claim
from vcs_core._pygit2_helpers import require_blob, require_commit
from vcs_core._substrate_driver import (
    ActiveSurface,
    CapabilitySet,
    DriverContext,
    DriverIngressResult,
    DriverSchema,
    EvidenceCitation,
    IngressRequest,
    ObservationDraft,
    ReductionBatch,
    SubstrateDriver,
    SurfacePolicyError,
    TransitionDraft,
    UnsupportedRequestError,
    validate_driver_identity,
    validate_driver_ingress,
    validate_driver_ingress_result,
)
from vcs_core._transition_kernel import PreparedCandidateDraft
from vcs_core._transition_kernel_records import (
    CandidateCommitRecord,
    CandidateOutcomeRecord,
    EvidenceOnlyEnvelopeRecord,
    EvidenceRecord,
    EvidenceRef,
    HeadSelectionEvidence,
    HeadSelectionRecord,
    LogicalTransition,
    PayloadDescriptorClaim,
    PreparedRevisionPlan,
    RetentionPolicyRequirement,
    RevisionPreparationRecord,
    ValidatedPayloadDescriptor,
)
from vcs_core._world_operation_builder import CandidateSelection, CandidateSelectionPlan, SelectionRequirementPlan
from vcs_core._world_selection_policy import (
    allowed_existing_head_semantic_ops,
    resolve_candidate_selection_kind,
    selection_retention_policy_requirements,
    stable_selection_policy_digest,
    validate_root_selection_policy,
    validate_unchanged_head_identity,
    validate_unchanged_selection_policy,
)
from vcs_core._world_types import (
    WORLD_REF_SUBSTRATE_KIND,
    CandidateRevision,
    SubstrateHead,
    WorldRefPayload,
    canonical_digest,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vcs_core._substrate_store import SubstrateStore
    from vcs_core._transition_kernel import TransitionKernelDriver
    from vcs_core._transition_kernel_records import (
        CandidateCommitRecord,
        RelationshipRequirement,
    )
    from vcs_core._world_operation_builder import PreparedWorldOperation
    from vcs_core._world_store import WorldStore


@dataclass(frozen=True)
class CoordinatorPreparedCandidate:
    """Coordinator-produced candidate plus typed transition evidence."""

    candidate: CandidateRevision
    candidate_commit: CandidateCommitRecord
    transition: LogicalTransition
    plan: PreparedRevisionPlan
    preparation: RevisionPreparationRecord


@dataclass(frozen=True)
class CoordinatorPreparedRevision:
    """Coordinator-produced non-candidate revision plus typed transition evidence."""

    head: str
    ref: str
    transition: LogicalTransition
    plan: PreparedRevisionPlan
    preparation: RevisionPreparationRecord


@dataclass(frozen=True)
class CoordinatorEvidenceOnlyIngress:
    """Coordinator-persisted evidence-only ingress anchored by an envelope ref."""

    evidence_refs: tuple[EvidenceRef, ...]
    envelope_ref: str
    envelope: EvidenceOnlyEnvelopeRecord


class WorldTransitionCoordinatorProtocol(Protocol):
    """Internal contract for world-vector transition lowering and selection planning.

    Substrate adapters and manager wrappers should cross this boundary for
    candidate preparation, evidence persistence, selection planning, retention
    derivation, and prepared-operation admission. The concrete coordinator may
    keep using JSON-only helpers while the public SPI is still being replaced.
    """

    def store(self, store_id: str) -> SubstrateStore: ...

    def dispatch(
        self,
        driver: SubstrateDriver,
        context: DriverContext,
        request: IngressRequest,
    ) -> DriverIngressResult: ...

    def validate_active_surface_result(
        self,
        driver: SubstrateDriver,
        context: DriverContext,
        result: DriverIngressResult,
    ) -> None: ...

    def create_prepared_json_revision(
        self,
        store_id: str,
        ref: str,
        *,
        operation_id: str,
        binding: str,
        payload: dict[str, Any],
        parents: tuple[str | pygit2.Oid, ...] = (),
        ingress_kind: str = "command",
        semantic_op: str = "json-revision",
        driver: TransitionKernelDriver | None = None,
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        message: str | None = None,
    ) -> CoordinatorPreparedRevision: ...

    def create_prepared_json_candidate(
        self,
        store_id: str,
        *,
        operation_id: str,
        binding: str,
        candidate_id: str = "primary",
        payload: dict[str, Any],
        parents: tuple[str | pygit2.Oid, ...] = (),
        ingress_kind: str = "command",
        semantic_op: str = "json-revision",
        driver: TransitionKernelDriver | None = None,
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        message: str | None = None,
    ) -> CoordinatorPreparedCandidate: ...

    def create_prepared_driver_revision(
        self,
        store_id: str,
        ref: str,
        *,
        operation_id: str,
        binding: str,
        result: DriverIngressResult,
        driver_id: str,
        driver_version: str,
        parents: tuple[str | pygit2.Oid, ...] = (),
        ingress_kind: str = "command",
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        reduction_batch: ReductionBatch | None = None,
        message: str | None = None,
    ) -> CoordinatorPreparedRevision: ...

    def create_prepared_driver_candidate(
        self,
        store_id: str,
        *,
        operation_id: str,
        binding: str,
        result: DriverIngressResult,
        driver_id: str,
        driver_version: str,
        candidate_id: str = "primary",
        parents: tuple[str | pygit2.Oid, ...] = (),
        ingress_kind: str = "command",
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        reduction_batch: ReductionBatch | None = None,
        message: str | None = None,
    ) -> CoordinatorPreparedCandidate: ...

    def lower_driver_ingress_candidate(
        self,
        store_id: str,
        *,
        operation_id: str,
        binding: str,
        result: DriverIngressResult,
        parents: tuple[str | pygit2.Oid, ...] = (),
        ingress_kind: str = "command",
        driver_id: str,
        driver_version: str,
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        reduction_batch: ReductionBatch | None = None,
    ) -> PreparedCandidateDraft: ...

    def persist_driver_evidence_only(
        self,
        store_id: str,
        *,
        operation_id: str,
        binding: str,
        result: DriverIngressResult,
        ingress_kind: str,
        driver_id: str,
        driver_version: str,
        envelope_id: str = "primary",
    ) -> CoordinatorEvidenceOnlyIngress: ...

    def persist_driver_diagnostics(
        self,
        store_id: str,
        *,
        operation_id: str,
        binding: str,
        result: DriverIngressResult,
        ingress_kind: str,
        driver_id: str,
        driver_version: str,
        envelope_id: str = "diagnostics",
    ) -> CoordinatorEvidenceOnlyIngress: ...

    def build_reduction_batch(
        self,
        evidence_refs: tuple[EvidenceRef, ...],
        *,
        citation_prefix: str = "evidence",
    ) -> ReductionBatch: ...

    def create_existing_head_selection_evidence(
        self,
        *,
        operation_id: str,
        head: SubstrateHead,
        selection_kind: Literal["bootstrap", "checkpoint", "import", "revert"],
        selected_from: str | None = None,
        mechanism: str | None = None,
        correlation_id: str | None = None,
    ) -> EvidenceRef: ...

    def plan_existing_head_selection(
        self,
        *,
        operation_id: str,
        head: SubstrateHead,
        selection_kind: Literal["bootstrap", "checkpoint", "import", "revert"],
        selected_from: str | None = None,
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        retention_policy_requirements: tuple[RetentionPolicyRequirement, ...] = (),
        selection_policy_digest: str | None = None,
        mechanism: str | None = None,
        correlation_id: str | None = None,
    ) -> SelectionRequirementPlan: ...

    def plan_unchanged_selection(
        self,
        *,
        operation_id: str,
        head: SubstrateHead,
        input_world_oid: str,
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        retention_policy_requirements: tuple[RetentionPolicyRequirement, ...] = (),
        selection_policy_digest: str | None = None,
    ) -> SelectionRequirementPlan: ...

    def plan_candidate_selection(
        self,
        *,
        operation_id: str,
        selection: CandidateSelection,
        selection_kind: Literal["new-candidate", "child-produced"] | None = None,
        producer_operation_id: str | None = None,
        producer_world_oid: str | None = None,
        role: str = "",
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        retention_policy_requirements: tuple[RetentionPolicyRequirement, ...] = (),
        selection_policy_digest: str | None = None,
    ) -> CandidateSelectionPlan: ...

    def selection_retention_policy_requirements(
        self,
        head: SubstrateHead,
        *,
        explicit_requirements: tuple[RetentionPolicyRequirement, ...] = (),
    ) -> tuple[RetentionPolicyRequirement, ...]: ...

    def read_world_ref_payload(self, head: SubstrateHead) -> WorldRefPayload: ...

    def validate_prepared_operation_admission(self, prepared: PreparedWorldOperation) -> None: ...


def dispatch_driver(
    driver: SubstrateDriver,
    context: DriverContext,
    request: IngressRequest,
    *,
    capabilities: CapabilitySet | None = None,
    schema: DriverSchema | None = None,
    execution: Any | None = None,
) -> DriverIngressResult:
    """Validated SPI dispatch around ``driver.prepare`` / ``prepare_bound``.

    Capability acceptance + ``ActiveSurface`` policy before the call, the
    three-layer ingress validator after. Module-level so dispatch sites
    outside the world-transition coordinator (the ``mg exec`` → SPI
    ``prepare`` bridge in ``_vcscore_runtime``) reuse the exact same checks
    instead of calling ``prepare`` bare.

    ``execution`` is the per-run ``ExecutionCapability`` for an
    ``ExecutionBoundDriver`` dispatch (PD1): when supplied, the driver's
    ``prepare_bound(context, request, execution)`` is invoked instead of
    ``prepare`` — authority flows through the call, never stored.
    """
    _check_capability_accepts(driver, request, capabilities=capabilities)
    _check_active_surface_pre_dispatch(driver, context.active_surface, request)
    if execution is not None:
        from vcs_core._execution_capability import ExecutionBoundDriver

        if not isinstance(driver, ExecutionBoundDriver):
            raise TypeError(
                f"Execution authority offered to {type(driver).__name__}, which has not opted in "
                "(no ExecutionBoundDriver implementation); refusing rather than dispatching bare."
            )
        result = driver.prepare_bound(context, request, execution)
    else:
        result = driver.prepare(context, request)
    validate_driver_ingress(request, result, driver, schema=schema)
    _check_active_surface_post_dispatch(driver, context.active_surface, result)
    return result


class WorldTransitionCoordinator:
    """Coordinates private v2 transition lowering, evidence, and selection policy."""

    def __init__(self, *, world_store: WorldStore, stores: Mapping[str, SubstrateStore]) -> None:
        self._world_store = world_store
        self._stores = dict(stores)

    def store(self, store_id: str) -> SubstrateStore:
        try:
            return self._stores[store_id]
        except KeyError as exc:
            raise InvalidRepositoryStateError(f"world storage installation has no store {store_id!r}") from exc

    def dispatch(
        self,
        driver: SubstrateDriver,
        context: DriverContext,
        request: IngressRequest,
    ) -> DriverIngressResult:
        """Typed-ingress entry point (SPI v0.1).

        Pre-dispatch enforces capability acceptance and the ``ActiveSurface``
        request-type policy (Q5a). The driver's ``prepare(context, request)``
        is then invoked. Post-dispatch runs the three-layer validator
        (generic + per-request + driver-side) and the ``ActiveSurface``
        evidence-kind / semantic-op policy.

        Coordinator lowering (``lower_driver_ingress_candidate`` etc.) is a
        separate concern; callers decide whether to lower the validated
        result. This keeps ``dispatch`` lightweight and reusable for
        diagnostic or test invocations.
        """
        return dispatch_driver(driver, context, request)

    def validate_active_surface_result(
        self,
        driver: SubstrateDriver,
        context: DriverContext,
        result: DriverIngressResult,
    ) -> None:
        """Validate adapter-produced observations against the active surface.

        Capture adapters can produce observation batches before a typed driver
        request exists. This hook lets production callers apply the same
        post-dispatch ``ActiveSurface`` policy before persisting evidence.
        """
        validate_driver_ingress_result(result)
        _check_active_surface_post_dispatch(driver, context.active_surface, result)

    def lower_driver_ingress_candidate(
        self,
        store_id: str,
        *,
        operation_id: str,
        binding: str,
        result: DriverIngressResult,
        parents: tuple[str | pygit2.Oid, ...] = (),
        ingress_kind: str = "command",
        driver_id: str,
        driver_version: str,
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        reduction_batch: ReductionBatch | None = None,
    ) -> PreparedCandidateDraft:
        """Lower one validated driver transition into the existing prepared-candidate shape."""
        store = self.store(store_id)
        validate_driver_identity(driver_id=driver_id, driver_version=driver_version)
        validate_driver_ingress_result(result)
        if len(result.transitions) != 1:
            raise InvalidRepositoryStateError("driver ingress lowering requires exactly one transition draft")
        if result.retention_hints:
            raise InvalidRepositoryStateError("driver retention hints are not supported by Phase 1 lowering")
        if result.selection_requirements:
            raise InvalidRepositoryStateError("driver selection requirements are not supported by Phase 1 lowering")
        transition_draft = result.transitions[0]
        parent_heads = tuple(str(parent) for parent in parents)
        if transition_draft.base_heads != parent_heads:
            raise InvalidRepositoryStateError("driver transition base_heads disagree with requested parents")
        if transition_draft.relationship_requirements != relationship_requirements:
            raise InvalidRepositoryStateError("driver transition requirements disagree with request")
        if not transition_draft.observation_ids:
            raise InvalidRepositoryStateError("driver Phase 1 transition requires at least one observation")
        observations_by_id = {observation.observation_id: observation for observation in result.observations}
        payload_digest = canonical_digest(transition_draft.payload)
        content_digest, plan_entries = store.plan_revision_content(
            transition_draft.content,
            payload_digest=payload_digest,
            parents=parents,
        )
        evidence_records = tuple(
            _evidence_record_from_observation(
                observation=observations_by_id[observation_id],
                operation_id=operation_id,
                binding=binding,
                store_id=store.identity.store_id,
                substrate_kind=store.identity.kind,
                ingress_kind=ingress_kind,
                default_payload_digest=payload_digest,
                default_mechanism=driver_id,
            )
            for observation_id in transition_draft.observation_ids
        )
        cited_evidence_refs = self._resolve_transition_citations(
            transition_draft,
            reduction_batch=reduction_batch,
            binding=binding,
            store_id=store.identity.store_id,
            substrate_kind=store.identity.kind,
            ingress_kind=ingress_kind,
        )
        all_evidence_digests = tuple(record.evidence_digest() for record in evidence_records) + tuple(
            ref.evidence_digest for ref in cited_evidence_refs
        )
        transition = LogicalTransition(
            binding=binding,
            store_id=store.identity.store_id,
            resource_id=store.identity.resource_id,
            substrate_kind=store.identity.kind,
            driver=driver_id,
            driver_version=driver_version,
            base_heads=parent_heads,
            ingress_kind=ingress_kind,
            semantic_op=transition_draft.semantic_op,
            payload_digest=payload_digest,
            evidence_digests=all_evidence_digests,
            requirements=relationship_requirements,
        )
        plan = PreparedRevisionPlan(
            binding=binding,
            store_id=store.identity.store_id,
            transition_digest=transition.transition_digest(),
            base_heads=parent_heads,
            expected_parent_heads=parent_heads,
            content_digest=content_digest,
            materialization_class=transition_draft.materialization_class,
            entries=plan_entries,
            git_tree_oid=transition_draft.git_tree_oid,
        )
        prepared = PreparedCandidateDraft(
            transition=transition,
            plan=plan,
            evidence_records=evidence_records,
            payload_descriptor_claim=transition_draft.payload_descriptor_claim
            or PayloadDescriptorClaim.for_json_payload(transition_draft.payload),
            payload=transition_draft.payload,
            parents=parents,
            cited_evidence_refs=cited_evidence_refs,
            content=transition_draft.content,
        )
        self._validate_prepared_candidate_draft(
            prepared,
            store=store,
            operation_id=operation_id,
            binding=binding,
            ingress_kind=ingress_kind,
            semantic_op=transition_draft.semantic_op,
            relationship_requirements=relationship_requirements,
        )
        return prepared

    def _resolve_transition_citations(
        self,
        transition: TransitionDraft,
        *,
        reduction_batch: ReductionBatch | None,
        binding: str,
        store_id: str,
        substrate_kind: str,
        ingress_kind: str,
    ) -> tuple[EvidenceRef, ...]:
        if not transition.evidence_citation_ids:
            return ()
        if reduction_batch is None:
            raise InvalidRepositoryStateError("driver transition cites evidence without a reduction batch")
        citations_by_id: dict[str, EvidenceCitation] = {}
        batch_evidence_refs: set[str] = set()
        batch_record_digests: set[str] = set()
        for citation in reduction_batch.citations:
            if not citation.citation_id:
                raise InvalidRepositoryStateError("reduction batch citation_id is required")
            if citation.citation_id in citations_by_id:
                raise InvalidRepositoryStateError("reduction batch contains duplicate citation_id")
            if citation.evidence_ref.ref in batch_evidence_refs:
                raise InvalidRepositoryStateError("reduction batch contains duplicate evidence ref")
            if citation.record_digest in batch_record_digests:
                raise InvalidRepositoryStateError("reduction batch contains duplicate evidence record")
            batch_evidence_refs.add(citation.evidence_ref.ref)
            batch_record_digests.add(citation.record_digest)
            citations_by_id[citation.citation_id] = citation
        cited_refs: list[EvidenceRef] = []
        selected_evidence_refs: set[str] = set()
        for citation_id in transition.evidence_citation_ids:
            try:
                citation = citations_by_id[citation_id]
            except KeyError as exc:
                raise InvalidRepositoryStateError("driver transition cites evidence outside reduction batch") from exc
            if citation.evidence_ref.ref in selected_evidence_refs:
                raise InvalidRepositoryStateError("driver transition cites duplicate evidence ref")
            selected_evidence_refs.add(citation.evidence_ref.ref)
            if citation.binding != binding:
                raise InvalidRepositoryStateError("evidence citation binding disagrees with transition")
            if citation.store_id != store_id:
                raise InvalidRepositoryStateError("evidence citation store_id disagrees with transition")
            if citation.substrate_kind != substrate_kind:
                raise InvalidRepositoryStateError("evidence citation substrate kind disagrees with transition")
            record = self._world_store.resolve_evidence_ref(
                citation.evidence_ref,
                expected_operation_id=citation.producer_operation_id,
            )
            if record.evidence_digest() != citation.evidence_digest:
                raise InvalidRepositoryStateError("evidence citation evidence_digest disagrees with record")
            if record.record_digest() != citation.record_digest:
                raise InvalidRepositoryStateError("evidence citation record_digest disagrees with record")
            if record.payload_digest != citation.payload_digest:
                raise InvalidRepositoryStateError("evidence citation payload_digest disagrees with record")
            if record.evidence_kind != citation.evidence_kind:
                raise InvalidRepositoryStateError("evidence citation evidence_kind disagrees with record")
            if record.binding != citation.binding:
                raise InvalidRepositoryStateError("evidence citation record binding disagrees with citation")
            if record.store_id != citation.store_id:
                raise InvalidRepositoryStateError("evidence citation record store_id disagrees with citation")
            if record.substrate_kind != citation.substrate_kind:
                raise InvalidRepositoryStateError("evidence citation record substrate kind disagrees with citation")
            self._validate_transition_citation_policy(
                transition=transition,
                citation=citation,
                ingress_kind=ingress_kind,
            )
            cited_refs.append(citation.evidence_ref)
        return tuple(cited_refs)

    # Evidence-kind whitelist for workspace-capture-reduction citations.
    # Pre-T2c: "capture:filesystem-event" only (overlay capture path).
    # T2c added the python-runtime mechanism for Python-tier writes; the
    # adapter declares "python-runtime:write" and "python-runtime:delete"
    # evidence kinds via _substrate_evidence_kinds. Future mechanisms
    # (shell/LD_PRELOAD per T3) extend this set as they land.
    _WORKSPACE_CAPTURE_REDUCTION_EVIDENCE_KINDS: frozenset[str] = frozenset(
        {
            "capture:filesystem-event",
            "python-runtime:write",
            "python-runtime:delete",
            "python-runtime:patch",
        }
    )

    def _validate_transition_citation_policy(
        self,
        *,
        transition: TransitionDraft,
        citation: EvidenceCitation,
        ingress_kind: str,
    ) -> None:
        if (
            ingress_kind == "reduce"
            and transition.semantic_op == "workspace-capture-reduction"
            and citation.evidence_kind not in self._WORKSPACE_CAPTURE_REDUCTION_EVIDENCE_KINDS
        ):
            raise InvalidRepositoryStateError(
                "workspace capture reduction citations require capture-mechanism "
                "evidence (overlay or python-runtime); got "
                f"evidence_kind={citation.evidence_kind!r}"
            )

    def persist_driver_evidence_only(
        self,
        store_id: str,
        *,
        operation_id: str,
        binding: str,
        result: DriverIngressResult,
        ingress_kind: str,
        driver_id: str,
        driver_version: str,
        envelope_id: str = "primary",
    ) -> CoordinatorEvidenceOnlyIngress:
        """Persist observation-only driver evidence without preparing a candidate."""
        store = self.store(store_id)
        validate_driver_identity(driver_id=driver_id, driver_version=driver_version)
        validate_driver_ingress_result(result)
        if result.transitions:
            raise InvalidRepositoryStateError("evidence-only driver ingress must not include transition drafts")
        if result.retention_hints:
            raise InvalidRepositoryStateError("evidence-only driver ingress must not include retention hints")
        if result.selection_requirements:
            raise InvalidRepositoryStateError("evidence-only driver ingress must not include selection requirements")
        if result.diagnostics:
            raise InvalidRepositoryStateError("diagnostic driver ingress requires the diagnostic evidence path")
        if not result.observations:
            raise InvalidRepositoryStateError("evidence-only driver ingress requires at least one observation")
        if not envelope_id:
            raise InvalidRepositoryStateError("evidence-only envelope_id is required")
        evidence_records = tuple(
            self._evidence_only_record_from_observation(
                observation,
                operation_id=operation_id,
                binding=binding,
                store_id=store.identity.store_id,
                substrate_kind=store.identity.kind,
                ingress_kind=ingress_kind,
                default_mechanism=driver_id,
            )
            for observation in result.observations
        )
        evidence_refs = tuple(self._world_store.store_evidence_record(record) for record in evidence_records)
        envelope = EvidenceOnlyEnvelopeRecord(
            producer_operation_id=operation_id,
            envelope_id=envelope_id,
            binding=binding,
            store_id=store.identity.store_id,
            resource_id=store.identity.resource_id,
            substrate_kind=store.identity.kind,
            ingress_kind=ingress_kind,
            evidence_refs=evidence_refs,
            evidence_kinds=tuple(record.evidence_kind for record in evidence_records),
        )
        envelope_ref = self._world_store.store_evidence_only_envelope(envelope)
        return CoordinatorEvidenceOnlyIngress(evidence_refs=evidence_refs, envelope_ref=envelope_ref, envelope=envelope)

    def persist_driver_diagnostics(
        self,
        store_id: str,
        *,
        operation_id: str,
        binding: str,
        result: DriverIngressResult,
        ingress_kind: str,
        driver_id: str,
        driver_version: str,
        envelope_id: str = "diagnostics",
    ) -> CoordinatorEvidenceOnlyIngress:
        validate_driver_ingress_result(result)
        if result.diagnostics:
            raise InvalidRepositoryStateError("diagnostic ingress must use typed diagnostic observations")
        if not result.observations:
            raise InvalidRepositoryStateError("diagnostic ingress requires at least one observation")
        for observation in result.observations:
            if not observation.evidence_kind.startswith("diagnostic:"):
                raise InvalidRepositoryStateError("diagnostic ingress observations must use diagnostic evidence kinds")
        return self.persist_driver_evidence_only(
            store_id,
            operation_id=operation_id,
            binding=binding,
            result=result,
            ingress_kind=ingress_kind,
            driver_id=driver_id,
            driver_version=driver_version,
            envelope_id=envelope_id,
        )

    def build_reduction_batch(
        self,
        evidence_refs: tuple[EvidenceRef, ...],
        *,
        citation_prefix: str = "evidence",
    ) -> ReductionBatch:
        if not citation_prefix:
            raise InvalidRepositoryStateError("reduction batch citation_prefix is required")
        citations: list[EvidenceCitation] = []
        for index, evidence_ref in enumerate(evidence_refs):
            record = self._world_store.resolve_evidence_ref(evidence_ref)
            citations.append(
                EvidenceCitation(
                    citation_id=f"{citation_prefix}-{index}",
                    producer_operation_id=record.operation_id,
                    evidence_ref=evidence_ref,
                    evidence_digest=record.evidence_digest(),
                    record_digest=record.record_digest(),
                    payload_digest=record.payload_digest,
                    binding=record.binding or "",
                    store_id=record.store_id or "",
                    substrate_kind=record.substrate_kind or "",
                    evidence_kind=record.evidence_kind,
                )
            )
        return ReductionBatch(citations=tuple(citations))

    def _evidence_only_record_from_observation(
        self,
        observation: ObservationDraft,
        *,
        operation_id: str,
        binding: str,
        store_id: str,
        substrate_kind: str,
        ingress_kind: str,
        default_mechanism: str,
    ) -> EvidenceRecord:
        if observation.evidence_payload_descriptor_claim is None:
            raise InvalidRepositoryStateError("evidence-only observation requires an evidence payload descriptor claim")
        return _evidence_record_from_observation(
            observation=observation,
            operation_id=operation_id,
            binding=binding,
            store_id=store_id,
            substrate_kind=substrate_kind,
            ingress_kind=ingress_kind,
            default_payload_digest=canonical_digest(observation.stable_observation),
            default_mechanism=default_mechanism,
        )

    def create_prepared_json_revision(
        self,
        store_id: str,
        ref: str,
        *,
        operation_id: str,
        binding: str,
        payload: dict[str, Any],
        parents: tuple[str | pygit2.Oid, ...] = (),
        ingress_kind: str = "command",
        semantic_op: str = "json-revision",
        driver: TransitionKernelDriver | None = None,
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        message: str | None = None,
    ) -> CoordinatorPreparedRevision:
        store = self.store(store_id)
        prepared = self._prepare_json_candidate_draft(
            store,
            operation_id=operation_id,
            binding=binding,
            payload=payload,
            parents=parents,
            ingress_kind=ingress_kind,
            semantic_op=semantic_op,
            driver=driver,
            relationship_requirements=relationship_requirements,
        )
        preparation = self._persist_preparation(
            store,
            prepared,
            operation_id=operation_id,
            binding=binding,
            relationship_requirements=relationship_requirements,
        )
        payload_descriptor = self._validated_payload_descriptor(prepared)
        head = store.create_revision_from_prepared(
            ref,
            transition=prepared.transition,
            plan=prepared.plan,
            preparation=preparation,
            payload_descriptor=payload_descriptor,
            payload=prepared.payload,
            content=prepared.content,
            parents=prepared.parents,
            message=message,
            evidence_resolver=self._world_store.resolve_evidence_ref,
        )
        return CoordinatorPreparedRevision(
            head=head,
            ref=ref,
            transition=prepared.transition,
            plan=prepared.plan,
            preparation=preparation,
        )

    def create_prepared_json_candidate(
        self,
        store_id: str,
        *,
        operation_id: str,
        binding: str,
        candidate_id: str = "primary",
        payload: dict[str, Any],
        parents: tuple[str | pygit2.Oid, ...] = (),
        ingress_kind: str = "command",
        semantic_op: str = "json-revision",
        driver: TransitionKernelDriver | None = None,
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        message: str | None = None,
    ) -> CoordinatorPreparedCandidate:
        store = self.store(store_id)
        prepared = self._prepare_json_candidate_draft(
            store,
            operation_id=operation_id,
            binding=binding,
            payload=payload,
            parents=parents,
            ingress_kind=ingress_kind,
            semantic_op=semantic_op,
            driver=driver,
            relationship_requirements=relationship_requirements,
        )
        preparation = self._persist_preparation(
            store,
            prepared,
            operation_id=operation_id,
            binding=binding,
            relationship_requirements=relationship_requirements,
        )
        payload_descriptor = self._validated_payload_descriptor(prepared)
        candidate = store.create_candidate_from_prepared(
            transition=prepared.transition,
            plan=prepared.plan,
            preparation=preparation,
            payload_descriptor=payload_descriptor,
            payload=prepared.payload,
            content=prepared.content,
            candidate_id=candidate_id,
            parents=prepared.parents,
            message=message,
            evidence_resolver=self._world_store.resolve_evidence_ref,
        )
        candidate_commit = store.candidate_commit_record(
            candidate,
            evidence_resolver=self._world_store.resolve_evidence_ref,
        )
        return CoordinatorPreparedCandidate(
            candidate=candidate,
            candidate_commit=candidate_commit,
            transition=prepared.transition,
            plan=prepared.plan,
            preparation=preparation,
        )

    def create_prepared_driver_revision(
        self,
        store_id: str,
        ref: str,
        *,
        operation_id: str,
        binding: str,
        result: DriverIngressResult,
        driver_id: str,
        driver_version: str,
        parents: tuple[str | pygit2.Oid, ...] = (),
        ingress_kind: str = "command",
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        reduction_batch: ReductionBatch | None = None,
        message: str | None = None,
    ) -> CoordinatorPreparedRevision:
        store = self.store(store_id)
        prepared = self.lower_driver_ingress_candidate(
            store_id,
            operation_id=operation_id,
            binding=binding,
            result=result,
            parents=parents,
            ingress_kind=ingress_kind,
            driver_id=driver_id,
            driver_version=driver_version,
            relationship_requirements=relationship_requirements,
            reduction_batch=reduction_batch,
        )
        preparation = self._persist_preparation(
            store,
            prepared,
            operation_id=operation_id,
            binding=binding,
            relationship_requirements=relationship_requirements,
        )
        payload_descriptor = self._validated_payload_descriptor(prepared)
        head = store.create_revision_from_prepared(
            ref,
            transition=prepared.transition,
            plan=prepared.plan,
            preparation=preparation,
            payload_descriptor=payload_descriptor,
            payload=prepared.payload,
            content=prepared.content,
            parents=prepared.parents,
            message=message,
            evidence_resolver=self._world_store.resolve_evidence_ref,
        )
        return CoordinatorPreparedRevision(
            head=head,
            ref=ref,
            transition=prepared.transition,
            plan=prepared.plan,
            preparation=preparation,
        )

    def create_prepared_driver_candidate(
        self,
        store_id: str,
        *,
        operation_id: str,
        binding: str,
        result: DriverIngressResult,
        driver_id: str,
        driver_version: str,
        candidate_id: str = "primary",
        parents: tuple[str | pygit2.Oid, ...] = (),
        ingress_kind: str = "command",
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        reduction_batch: ReductionBatch | None = None,
        message: str | None = None,
    ) -> CoordinatorPreparedCandidate:
        store = self.store(store_id)
        prepared = self.lower_driver_ingress_candidate(
            store_id,
            operation_id=operation_id,
            binding=binding,
            result=result,
            parents=parents,
            ingress_kind=ingress_kind,
            driver_id=driver_id,
            driver_version=driver_version,
            relationship_requirements=relationship_requirements,
            reduction_batch=reduction_batch,
        )
        preparation = self._persist_preparation(
            store,
            prepared,
            operation_id=operation_id,
            binding=binding,
            relationship_requirements=relationship_requirements,
        )
        payload_descriptor = self._validated_payload_descriptor(prepared)
        candidate = store.create_candidate_from_prepared(
            transition=prepared.transition,
            plan=prepared.plan,
            preparation=preparation,
            payload_descriptor=payload_descriptor,
            payload=prepared.payload,
            content=prepared.content,
            candidate_id=candidate_id,
            parents=prepared.parents,
            message=message,
            evidence_resolver=self._world_store.resolve_evidence_ref,
        )
        candidate_commit = store.candidate_commit_record(
            candidate,
            evidence_resolver=self._world_store.resolve_evidence_ref,
        )
        return CoordinatorPreparedCandidate(
            candidate=candidate,
            candidate_commit=candidate_commit,
            transition=prepared.transition,
            plan=prepared.plan,
            preparation=preparation,
        )

    def create_existing_head_selection_evidence(
        self,
        *,
        operation_id: str,
        head: SubstrateHead,
        selection_kind: Literal["bootstrap", "checkpoint", "import", "revert"],
        selected_from: str | None = None,
        mechanism: str | None = None,
        correlation_id: str | None = None,
    ) -> EvidenceRef:
        """Persist operation-local evidence for selecting an existing prepared substrate head."""
        store = self.store(head.store_id)
        if store.identity.kind != head.kind:
            raise InvalidRepositoryStateError("existing-head selection evidence substrate kind disagrees with store")
        if store.identity.resource_id != head.resource_id:
            raise InvalidRepositoryStateError("existing-head selection evidence resource_id disagrees with store")
        allowed_ops = allowed_existing_head_semantic_ops(selection_kind)
        try:
            provenance = store.validate_prepared_revision(
                head.head,
                evidence_resolver=self._world_store.resolve_evidence_ref,
            )
        except (InvalidRepositoryStateError, KeyError, TypeError, ValueError) as exc:
            raise InvalidRepositoryStateError(
                f"{selection_kind} selection requires prepared revision provenance"
            ) from exc
        if provenance.transition.semantic_op not in allowed_ops:
            allowed = " or ".join(sorted(allowed_ops))
            raise InvalidRepositoryStateError(
                f"{selection_kind} selection requires original {allowed} revision provenance"
            )
        if selection_kind == "revert":
            if selected_from is None:
                raise InvalidRepositoryStateError("revert selection requires selected_from")
            if not _head_descends_from(store.repo, selected_head=selected_from, required_head=head.head):
                raise InvalidRepositoryStateError("revert selected_from must descend from selected_head")
        stable_observation: dict[str, object] = {
            "binding": head.binding,
            "store_id": head.store_id,
            "resource_id": head.resource_id,
            "substrate_kind": head.kind,
            "head": head.head,
            "kind": selection_kind,
        }
        if selected_from is not None:
            stable_observation["selected_from"] = selected_from
        return self._world_store.store_evidence_record(
            EvidenceRecord(
                operation_id=operation_id,
                binding=head.binding,
                store_id=head.store_id,
                substrate_kind=head.kind,
                ingress_kind="coordinator",
                observed_head=head.head,
                evidence_kind=selection_kind,
                payload_digest=canonical_digest(stable_observation),
                stable_observation=stable_observation,
                mechanism=mechanism,
                correlation_id=correlation_id,
            )
        )

    def plan_existing_head_selection(
        self,
        *,
        operation_id: str,
        head: SubstrateHead,
        selection_kind: Literal["bootstrap", "checkpoint", "import", "revert"],
        selected_from: str | None = None,
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        retention_policy_requirements: tuple[RetentionPolicyRequirement, ...] = (),
        selection_policy_digest: str | None = None,
        mechanism: str | None = None,
        correlation_id: str | None = None,
    ) -> SelectionRequirementPlan:
        evidence_ref = self.create_existing_head_selection_evidence(
            operation_id=operation_id,
            head=head,
            selection_kind=selection_kind,
            selected_from=selected_from,
            mechanism=mechanism,
            correlation_id=correlation_id,
        )
        return SelectionRequirementPlan(
            operation_id=operation_id,
            binding=head.binding,
            store_id=head.store_id,
            resource_id=head.resource_id,
            selected_head=head.head,
            selection_kind=selection_kind,
            selected_from=selected_from,
            relationship_requirements=relationship_requirements,
            retention_policy_requirements=self.selection_retention_policy_requirements(
                head,
                explicit_requirements=retention_policy_requirements,
            ),
            selection_policy_digest=selection_policy_digest
            or stable_selection_policy_digest(binding=head.binding, head=head.head),
            evidence_refs=(evidence_ref,),
        )

    def plan_unchanged_selection(
        self,
        *,
        operation_id: str,
        head: SubstrateHead,
        input_world_oid: str,
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        retention_policy_requirements: tuple[RetentionPolicyRequirement, ...] = (),
        selection_policy_digest: str | None = None,
    ) -> SelectionRequirementPlan:
        try:
            input_head = self._world_store.read_world_commit(input_world_oid).snapshot.head_for(head.binding)
        except KeyError as exc:
            raise InvalidRepositoryStateError("unchanged selection binding is missing from input world") from exc
        if input_head != head:
            raise InvalidRepositoryStateError("unchanged selection must match input world head")
        return SelectionRequirementPlan(
            operation_id=operation_id,
            binding=head.binding,
            store_id=head.store_id,
            resource_id=head.resource_id,
            selected_head=head.head,
            selection_kind="unchanged",
            relationship_requirements=relationship_requirements,
            retention_policy_requirements=self.selection_retention_policy_requirements(
                head,
                explicit_requirements=retention_policy_requirements,
            ),
            selection_policy_digest=selection_policy_digest
            or stable_selection_policy_digest(binding=head.binding, head=head.head),
        )

    def plan_candidate_selection(
        self,
        *,
        operation_id: str,
        selection: CandidateSelection,
        selection_kind: Literal["new-candidate", "child-produced"] | None = None,
        producer_operation_id: str | None = None,
        producer_world_oid: str | None = None,
        role: str = "",
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        retention_policy_requirements: tuple[RetentionPolicyRequirement, ...] = (),
        selection_policy_digest: str | None = None,
    ) -> CandidateSelectionPlan:
        if not role:
            raise InvalidRepositoryStateError("candidate selection planning requires role")
        if selection.candidate_tuple is None:
            raise InvalidRepositoryStateError("candidate selection planning requires a prepared candidate tuple")
        candidate = selection.candidate
        candidate_commit = selection.candidate_commit
        resolved_producer_operation_id = producer_operation_id or candidate_commit.operation_id
        resolved_selection_kind = resolve_candidate_selection_kind(
            operation_id=operation_id,
            producer_operation_id=resolved_producer_operation_id,
            producer_world_oid=producer_world_oid,
            requested_kind=selection_kind,
        )
        head = self.store(candidate.store_id).substrate_head(
            binding=candidate.binding,
            head=candidate.head,
            role=role,
        )
        return CandidateSelectionPlan(
            operation_id=operation_id,
            selection=selection,
            selection_kind=resolved_selection_kind,
            producer_operation_id=resolved_producer_operation_id,
            producer_world_oid=producer_world_oid,
            relationship_requirements=relationship_requirements,
            retention_policy_requirements=self.selection_retention_policy_requirements(
                head,
                explicit_requirements=retention_policy_requirements,
            ),
            selection_policy_digest=selection_policy_digest
            or stable_selection_policy_digest(binding=candidate.binding, head=candidate.head),
        )

    def selection_retention_policy_requirements(
        self,
        head: SubstrateHead,
        *,
        explicit_requirements: tuple[RetentionPolicyRequirement, ...] = (),
    ) -> tuple[RetentionPolicyRequirement, ...]:
        return selection_retention_policy_requirements(
            head,
            explicit_requirements=explicit_requirements,
            world_ref_payload=self.read_world_ref_payload(head) if head.kind == WORLD_REF_SUBSTRATE_KIND else None,
        )

    def read_world_ref_payload(self, head: SubstrateHead) -> WorldRefPayload:
        store = self.store(head.store_id)
        if store.identity.kind != WORLD_REF_SUBSTRATE_KIND:
            raise InvalidRepositoryStateError("world-ref payload requires a vcscore.world_ref store")
        commit = require_commit(store.repo, pygit2.Oid(hex=head.head), context="world-ref substrate revision")
        try:
            entry = commit.tree["revision.json"]
            blob = require_blob(store.repo, entry.id, context="world-ref revision payload")
            raw = json.loads(bytes(blob.data).decode("utf-8"))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InvalidRepositoryStateError("invalid world-ref substrate payload") from exc
        try:
            return WorldRefPayload.from_json(raw)
        except (TypeError, ValueError) as exc:
            raise InvalidRepositoryStateError("invalid world-ref substrate payload") from exc

    def validate_prepared_operation_admission(self, prepared: PreparedWorldOperation) -> None:
        """Validate coordinator-owned selection evidence before journaling or committing."""
        selected = dict(prepared.selected or {})
        selections: dict[str, HeadSelectionRecord] = {}
        for item in prepared.head_selections:
            try:
                selection = HeadSelectionRecord.from_json(dict(item))
            except (TypeError, ValueError) as exc:
                raise InvalidRepositoryStateError(str(exc)) from exc
            selections[selection.binding] = selection
        evidences: dict[str, HeadSelectionEvidence] = {}
        for item in prepared.selection_evidence:
            try:
                evidence = HeadSelectionEvidence.from_json(dict(item))
            except (TypeError, ValueError) as exc:
                raise InvalidRepositoryStateError(str(exc)) from exc
            evidences[evidence.binding] = evidence
        if set(selections) != set(selected) or set(evidences) != set(selected):
            raise InvalidRepositoryStateError("prepared operation selections must explain every selected binding")
        for binding, selected_head in selected.items():
            selection = selections[binding]
            evidence = evidences[binding]
            try:
                snapshot_head = prepared.snapshot.head_for(binding)
            except KeyError as exc:
                raise InvalidRepositoryStateError(
                    "prepared operation selection binding is missing from snapshot"
                ) from exc
            if selection.store_id != snapshot_head.store_id or evidence.store_id != snapshot_head.store_id:
                raise InvalidRepositoryStateError("prepared operation selection store_id disagrees with snapshot")
            if selection.resource_id != snapshot_head.resource_id or evidence.resource_id != snapshot_head.resource_id:
                raise InvalidRepositoryStateError("prepared operation selection resource_id disagrees with snapshot")
            if selection.selected_head != selected_head or evidence.selected_head != selected_head:
                raise InvalidRepositoryStateError("prepared operation selection disagrees with selected head")
            validate_root_selection_policy(
                input_world_oid=prepared.input_world_oid,
                selection_kind=selection.selection_kind,
            )
            if selection.selection_kind == "unchanged":
                self._validate_prepared_unchanged_selection(prepared, selection, evidence)
            elif selection.selection_kind in {"bootstrap", "checkpoint", "import", "revert"}:
                self._validate_prepared_existing_head_selection(selection, evidence)
        self._validate_prepared_candidate_outcomes(
            prepared,
            selected=selected,
            selections=selections,
            evidences=evidences,
        )

    def _prepare_json_candidate_draft(
        self,
        store: SubstrateStore,
        *,
        operation_id: str,
        binding: str,
        payload: dict[str, Any],
        parents: tuple[str | pygit2.Oid, ...],
        ingress_kind: str,
        semantic_op: str,
        driver: TransitionKernelDriver | None,
        relationship_requirements: tuple[RelationshipRequirement, ...],
    ) -> PreparedCandidateDraft:
        if driver is None:
            driver_id = f"vcs-core.{store.identity.kind}.json"
            return self.lower_driver_ingress_candidate(
                store.identity.store_id,
                operation_id=operation_id,
                binding=binding,
                result=_json_driver_ingress_result(
                    store=store,
                    binding=binding,
                    payload=payload,
                    parents=parents,
                    ingress_kind=ingress_kind,
                    semantic_op=semantic_op,
                    driver_id=driver_id,
                    relationship_requirements=relationship_requirements,
                ),
                parents=parents,
                ingress_kind=ingress_kind,
                driver_id=driver_id,
                driver_version="v1",
                relationship_requirements=relationship_requirements,
            )
        prepared = driver.prepare_candidate(
            store=store,
            operation_id=operation_id,
            binding=binding,
            payload=payload,
            parents=parents,
            ingress_kind=ingress_kind,
            semantic_op=semantic_op,
            relationship_requirements=relationship_requirements,
        )
        self._validate_prepared_candidate_draft(
            prepared,
            store=store,
            operation_id=operation_id,
            binding=binding,
            ingress_kind=ingress_kind,
            semantic_op=semantic_op,
            relationship_requirements=relationship_requirements,
        )
        return prepared

    def _persist_preparation(
        self,
        store: SubstrateStore,
        prepared: PreparedCandidateDraft,
        *,
        operation_id: str,
        binding: str,
        relationship_requirements: tuple[RelationshipRequirement, ...],
    ) -> RevisionPreparationRecord:
        evidence_refs = tuple(self._world_store.store_evidence_record(record) for record in prepared.evidence_records)
        all_evidence_refs = evidence_refs + prepared.cited_evidence_refs
        return RevisionPreparationRecord(
            operation_id=operation_id,
            binding=binding,
            store_id=store.identity.store_id,
            resource_id=store.identity.resource_id,
            transition_digest=prepared.transition.transition_digest(),
            revision_plan_digest=prepared.plan.revision_plan_digest(),
            content_digest=prepared.plan.content_digest,
            evidence_digests=prepared.transition.evidence_digests,
            evidence_refs=all_evidence_refs,
            cited_evidence_refs=prepared.cited_evidence_refs,
            relationship_requirements=relationship_requirements,
        )

    def _validate_prepared_candidate_draft(
        self,
        prepared: PreparedCandidateDraft,
        *,
        store: SubstrateStore,
        operation_id: str,
        binding: str,
        ingress_kind: str,
        semantic_op: str,
        relationship_requirements: tuple[RelationshipRequirement, ...],
    ) -> None:
        transition = prepared.transition
        plan = prepared.plan
        payload_digest = canonical_digest(prepared.payload)
        parent_heads = tuple(str(parent) for parent in prepared.parents)
        if transition.binding != binding or plan.binding != binding:
            raise InvalidRepositoryStateError("prepared candidate draft binding disagrees with request")
        if transition.store_id != store.identity.store_id or plan.store_id != store.identity.store_id:
            raise InvalidRepositoryStateError("prepared candidate draft store_id disagrees with store identity")
        if transition.resource_id != store.identity.resource_id:
            raise InvalidRepositoryStateError("prepared candidate draft resource_id disagrees with store identity")
        if transition.substrate_kind != store.identity.kind:
            raise InvalidRepositoryStateError("prepared candidate draft substrate kind disagrees with store identity")
        if transition.ingress_kind != ingress_kind or transition.semantic_op != semantic_op:
            raise InvalidRepositoryStateError("prepared candidate draft ingress disagrees with request")
        expected_content_digest, _entries = store.plan_revision_content(
            prepared.content,
            payload_digest=payload_digest,
            parents=prepared.parents,
        )
        if transition.payload_digest != payload_digest or plan.content_digest != expected_content_digest:
            raise InvalidRepositoryStateError("prepared candidate draft payload digest disagrees with payload")
        self._validated_payload_descriptor(prepared)
        if transition.base_heads != parent_heads or plan.base_heads != parent_heads:
            raise InvalidRepositoryStateError("prepared candidate draft base_heads disagree with parents")
        if plan.expected_parent_heads != parent_heads:
            raise InvalidRepositoryStateError("prepared candidate draft expected parents disagree with parents")
        if plan.transition_digest != transition.transition_digest():
            raise InvalidRepositoryStateError(
                "prepared candidate draft plan transition_digest disagrees with transition"
            )
        evidence_digests = tuple(record.evidence_digest() for record in prepared.evidence_records) + tuple(
            ref.evidence_digest for ref in prepared.cited_evidence_refs
        )
        if sorted(evidence_digests) != sorted(transition.evidence_digests):
            raise InvalidRepositoryStateError("prepared candidate draft evidence digests disagree with transition")
        expected_requirement_digests = sorted(canonical_digest(req.to_json()) for req in relationship_requirements)
        transition_requirement_digests = sorted(canonical_digest(req.to_json()) for req in transition.requirements)
        if transition_requirement_digests != expected_requirement_digests:
            raise InvalidRepositoryStateError("prepared candidate draft requirements disagree with request")
        for record in prepared.evidence_records:
            if record.operation_id != operation_id:
                raise InvalidRepositoryStateError(
                    "prepared candidate draft evidence operation_id disagrees with request"
                )
            if record.binding != binding:
                raise InvalidRepositoryStateError("prepared candidate draft evidence binding disagrees with request")
            if record.store_id != store.identity.store_id:
                raise InvalidRepositoryStateError("prepared candidate draft evidence store_id disagrees with store")
            if record.substrate_kind != store.identity.kind:
                raise InvalidRepositoryStateError(
                    "prepared candidate draft evidence substrate kind disagrees with store"
                )
            if record.ingress_kind != ingress_kind:
                raise InvalidRepositoryStateError("prepared candidate draft evidence ingress disagrees with request")
            try:
                EvidenceRecord.from_canonical_bytes(record.canonical_bytes())
            except (TypeError, ValueError) as exc:
                raise InvalidRepositoryStateError("prepared candidate draft evidence record is invalid") from exc
        for evidence_ref in prepared.cited_evidence_refs:
            try:
                record = self._world_store.resolve_evidence_ref(evidence_ref)
            except (InvalidRepositoryStateError, KeyError, TypeError, ValueError) as exc:
                raise InvalidRepositoryStateError("prepared candidate draft cited evidence ref is invalid") from exc
            if record.binding != binding:
                raise InvalidRepositoryStateError(
                    "prepared candidate draft cited evidence binding disagrees with request"
                )
            if record.store_id != store.identity.store_id:
                raise InvalidRepositoryStateError(
                    "prepared candidate draft cited evidence store_id disagrees with store"
                )
            if record.substrate_kind != store.identity.kind:
                raise InvalidRepositoryStateError(
                    "prepared candidate draft cited evidence substrate kind disagrees with store"
                )

    def _validated_payload_descriptor(self, prepared: PreparedCandidateDraft) -> ValidatedPayloadDescriptor:
        return validate_payload_descriptor_claim(prepared.payload_descriptor_claim, payload=prepared.payload)

    def _validate_prepared_unchanged_selection(
        self,
        prepared: PreparedWorldOperation,
        selection: HeadSelectionRecord,
        evidence: HeadSelectionEvidence,
    ) -> None:
        input_world_oid = prepared.input_world_oid
        validate_unchanged_selection_policy(
            input_world_oid=input_world_oid,
            evidence_refs=evidence.evidence_refs,
        )
        assert input_world_oid is not None
        try:
            input_head = self._world_store.read_world_commit(input_world_oid).snapshot.head_for(selection.binding)
            selected_head = prepared.snapshot.head_for(selection.binding)
        except KeyError as exc:
            raise InvalidRepositoryStateError("unchanged selection binding is missing from input world") from exc
        validate_unchanged_head_identity(input_head=input_head, selected_head=selected_head)

    def _validate_prepared_existing_head_selection(
        self,
        selection: HeadSelectionRecord,
        evidence: HeadSelectionEvidence,
    ) -> None:
        required_kinds = allowed_existing_head_semantic_ops(selection.selection_kind)  # type: ignore[arg-type]
        try:
            evidence_records = tuple(
                self._world_store.resolve_evidence_ref(ref, expected_operation_id=evidence.operation_id)
                for ref in evidence.evidence_refs
            )
        except (InvalidRepositoryStateError, KeyError, TypeError, ValueError) as exc:
            raise InvalidRepositoryStateError("existing-head selection evidence ref is invalid") from exc
        matching_records = tuple(record for record in evidence_records if record.evidence_kind in required_kinds)
        if not matching_records:
            allowed = " or ".join(sorted(required_kinds))
            raise InvalidRepositoryStateError(f"{selection.selection_kind} selection requires {allowed} evidence")
        store = self.store(selection.store_id)
        if not any(
            _is_coordinator_selection_evidence(record, selection=selection, store=store) for record in matching_records
        ):
            raise InvalidRepositoryStateError(
                f"{selection.selection_kind} selection evidence must exactly observe selected head as coordinator-owned evidence"
            )
        try:
            provenance = store.validate_prepared_revision(
                selection.selected_head,
                evidence_resolver=self._world_store.resolve_evidence_ref,
            )
        except (InvalidRepositoryStateError, KeyError, TypeError, ValueError) as exc:
            raise InvalidRepositoryStateError(
                f"{selection.selection_kind} selection requires prepared revision provenance"
            ) from exc
        if provenance.transition.semantic_op not in required_kinds:
            allowed = " or ".join(sorted(required_kinds))
            raise InvalidRepositoryStateError(
                f"{selection.selection_kind} selection requires original {allowed} revision provenance"
            )
        if selection.selection_kind != "revert":
            return
        if selection.selected_from is None:
            raise InvalidRepositoryStateError("revert selection requires selected_from")
        if not _head_descends_from(
            store.repo,
            selected_head=selection.selected_from,
            required_head=selection.selected_head,
        ):
            raise InvalidRepositoryStateError("revert selected_from must descend from selected_head")

    def _validate_prepared_candidate_outcomes(
        self,
        prepared: PreparedWorldOperation,
        *,
        selected: dict[str, str],
        selections: dict[str, HeadSelectionRecord],
        evidences: dict[str, HeadSelectionEvidence],
    ) -> None:
        commits: dict[tuple[str, str, str, str], CandidateCommitRecord] = {}
        for commit_record in prepared.candidate_commits:
            key = (
                commit_record.operation_id,
                commit_record.binding,
                commit_record.candidate_id,
                commit_record.candidate_head,
            )
            if key in commits:
                raise InvalidRepositoryStateError("prepared operation contains duplicate candidate commit record")
            commits[key] = commit_record
        selected_outcomes: set[str] = set()
        for outcome in prepared.candidate_outcomes:
            producer_operation_id = outcome.producer_operation_id or prepared.operation_id
            key = (producer_operation_id, outcome.binding, outcome.candidate_id, outcome.candidate)
            matching_commit = commits.get(key)
            if matching_commit is None:
                raise InvalidRepositoryStateError("prepared operation candidate outcome lacks matching commit record")
            self._validate_prepared_candidate_outcome_provenance(outcome, matching_commit, producer_operation_id)
            if outcome.binding not in selected:
                raise InvalidRepositoryStateError("prepared operation candidate outcome names unknown binding")
            if outcome.outcome == "archived":
                if outcome.candidate == selected[outcome.binding]:
                    raise InvalidRepositoryStateError("archived candidate outcome must not name selected head")
                continue
            if outcome.candidate != selected[outcome.binding]:
                raise InvalidRepositoryStateError("selected candidate outcome disagrees with selected head")
            if outcome.binding in selected_outcomes:
                raise InvalidRepositoryStateError("prepared operation contains duplicate selected candidate outcome")
            selected_outcomes.add(outcome.binding)
            selection = selections[outcome.binding]
            evidence = evidences[outcome.binding]
            if selection.selection_kind == "new-candidate":
                if producer_operation_id != prepared.operation_id:
                    raise InvalidRepositoryStateError("new-candidate selection requires current operation producer")
                if outcome.producer_world_oid is not None:
                    raise InvalidRepositoryStateError("new-candidate selection must not carry producer_world_oid")
            elif selection.selection_kind == "child-produced":
                if outcome.producer_world_oid is None:
                    raise InvalidRepositoryStateError("child-produced selection requires producer_world_oid")
                if evidence.producer_operation_id != producer_operation_id:
                    raise InvalidRepositoryStateError(
                        "child-produced evidence producer_operation_id disagrees with outcome"
                    )
            else:
                raise InvalidRepositoryStateError("selected candidate outcome requires candidate-backed head selection")
            if evidence.revision_preparation_digest != matching_commit.revision_preparation_digest:
                raise InvalidRepositoryStateError(
                    "selection evidence revision_preparation_digest disagrees with commit"
                )
            if evidence.candidate_commit_digest != matching_commit.candidate_commit_digest():
                raise InvalidRepositoryStateError("selection evidence candidate_commit_digest disagrees with commit")
            if evidence.candidate_ref != matching_commit.candidate_ref:
                raise InvalidRepositoryStateError("selection evidence candidate_ref disagrees with commit")

    def _validate_prepared_candidate_outcome_provenance(
        self,
        outcome: CandidateOutcomeRecord,
        commit: CandidateCommitRecord,
        producer_operation_id: str,
    ) -> None:
        try:
            store = self.store(commit.store_id)
            provenance = store.validate_prepared_candidate(
                commit.candidate_head,
                expected_revision_preparation_digest=commit.revision_preparation_digest,
                evidence_resolver=self._world_store.resolve_evidence_ref,
            )
        except (InvalidRepositoryStateError, KeyError, TypeError, ValueError) as exc:
            raise InvalidRepositoryStateError("prepared operation candidate commit provenance is invalid") from exc
        preparation = provenance.preparation
        if commit.binding != preparation.binding:
            raise InvalidRepositoryStateError("candidate commit record binding disagrees with prepared candidate")
        if commit.resource_id != preparation.resource_id:
            raise InvalidRepositoryStateError("candidate commit record resource_id disagrees with prepared candidate")
        if commit.candidate_head != provenance.head:
            raise InvalidRepositoryStateError(
                "candidate commit record candidate_head disagrees with prepared candidate"
            )
        if outcome.store_id != commit.store_id:
            raise InvalidRepositoryStateError("candidate outcome store_id disagrees with candidate commit")
        if outcome.resource_id != commit.resource_id:
            raise InvalidRepositoryStateError("candidate outcome resource_id disagrees with candidate commit")
        if outcome.transition_digest != provenance.transition.transition_digest():
            raise InvalidRepositoryStateError("candidate outcome transition_digest disagrees with prepared candidate")
        if outcome.revision_plan_digest != provenance.plan.revision_plan_digest():
            raise InvalidRepositoryStateError(
                "candidate outcome revision_plan_digest disagrees with prepared candidate"
            )
        if outcome.content_digest != provenance.plan.content_digest:
            raise InvalidRepositoryStateError("candidate outcome content_digest disagrees with prepared candidate")
        if outcome.revision_preparation_digest != preparation.revision_preparation_digest():
            raise InvalidRepositoryStateError(
                "candidate outcome revision_preparation_digest disagrees with prepared candidate"
            )
        if outcome.candidate_commit_digest != commit.candidate_commit_digest():
            raise InvalidRepositoryStateError(
                "candidate outcome candidate_commit_digest disagrees with candidate commit"
            )
        if sorted(outcome.evidence_digests) != sorted(preparation.evidence_digests):
            raise InvalidRepositoryStateError("candidate outcome evidence_digests disagree with prepared candidate")
        if sorted(canonical_digest(ref.to_json()) for ref in outcome.evidence_refs) != sorted(
            canonical_digest(ref.to_json()) for ref in preparation.evidence_refs
        ):
            raise InvalidRepositoryStateError("candidate outcome evidence_refs disagree with prepared candidate")


def _check_capability_accepts(
    driver: SubstrateDriver,
    request: IngressRequest,
    *,
    capabilities: CapabilitySet | None = None,
) -> None:
    effective_capabilities = capabilities or driver.capabilities
    if type(request) not in effective_capabilities.accepts:
        raise UnsupportedRequestError(driver_id=driver.driver_id, request_type=type(request))


def _check_active_surface_pre_dispatch(
    driver: SubstrateDriver,
    surface: ActiveSurface | None,
    request: IngressRequest,
) -> None:
    if surface is None:
        return
    request_type = type(request)
    if surface.allow_request_types is not None and request_type not in surface.allow_request_types:
        raise SurfacePolicyError(
            driver_id=driver.driver_id,
            reason="request type not in active-surface allow set",
            offending=request_type.__name__,
        )
    if request_type in surface.deny_request_types:
        raise SurfacePolicyError(
            driver_id=driver.driver_id,
            reason="request type denied by active surface",
            offending=request_type.__name__,
        )


def _check_active_surface_post_dispatch(
    driver: SubstrateDriver,
    surface: ActiveSurface | None,
    result: DriverIngressResult,
) -> None:
    if surface is None:
        return
    for observation in result.observations:
        kind = observation.evidence_kind
        if surface.allow_evidence_kinds is not None and kind not in surface.allow_evidence_kinds:
            raise SurfacePolicyError(
                driver_id=driver.driver_id,
                reason="observation evidence_kind not in active-surface allow set",
                offending=kind,
            )
        if kind in surface.deny_evidence_kinds:
            raise SurfacePolicyError(
                driver_id=driver.driver_id,
                reason="observation evidence_kind denied by active surface",
                offending=kind,
            )
    for transition in result.transitions:
        op = transition.semantic_op
        if surface.allow_semantic_ops is not None and op not in surface.allow_semantic_ops:
            raise SurfacePolicyError(
                driver_id=driver.driver_id,
                reason="transition semantic_op not in active-surface allow set",
                offending=op,
            )
        if op in surface.deny_semantic_ops:
            raise SurfacePolicyError(
                driver_id=driver.driver_id,
                reason="transition semantic_op denied by active surface",
                offending=op,
            )


def _json_driver_ingress_result(
    *,
    store: SubstrateStore,
    binding: str,
    payload: dict[str, Any],
    parents: tuple[str | pygit2.Oid, ...],
    ingress_kind: str,
    semantic_op: str,
    driver_id: str,
    relationship_requirements: tuple[RelationshipRequirement, ...],
) -> DriverIngressResult:
    parent_heads = tuple(str(parent) for parent in parents)
    payload_digest = canonical_digest(payload)
    observation = ObservationDraft(
        observation_id="payload",
        evidence_kind=f"{ingress_kind}:{semantic_op}",
        stable_observation={
            "binding": binding,
            "store_id": store.identity.store_id,
            "resource_id": store.identity.resource_id,
            "substrate_kind": store.identity.kind,
            "semantic_op": semantic_op,
            "parent_heads": list(parent_heads),
            "payload_digest": payload_digest,
        },
        mechanism=driver_id,
    )
    transition = TransitionDraft(
        transition_id="primary",
        semantic_op=semantic_op,
        payload=payload,
        observation_ids=(observation.observation_id,),
        base_heads=parent_heads,
        payload_descriptor_claim=PayloadDescriptorClaim.for_json_payload(payload),
        relationship_requirements=relationship_requirements,
    )
    return DriverIngressResult(observations=(observation,), transitions=(transition,))


def _evidence_record_from_observation(
    *,
    observation: ObservationDraft,
    operation_id: str,
    binding: str,
    store_id: str,
    substrate_kind: str,
    ingress_kind: str,
    default_payload_digest: str,
    default_mechanism: str,
) -> EvidenceRecord:
    payload_digest = _evidence_payload_digest_from_observation(
        observation,
        default_payload_digest=default_payload_digest,
    )
    return EvidenceRecord(
        operation_id=operation_id,
        binding=binding,
        store_id=store_id,
        substrate_kind=substrate_kind,
        ingress_kind=ingress_kind,
        observed_head=observation.observed_head,
        evidence_kind=observation.evidence_kind,
        payload_digest=payload_digest,
        stable_observation=observation.stable_observation,
        observed_at_unix_ns=observation.observed_at_unix_ns,
        mechanism=observation.mechanism or default_mechanism,
        correlation_id=observation.correlation_id,
    )


def _evidence_payload_digest_from_observation(
    observation: ObservationDraft,
    *,
    default_payload_digest: str,
) -> str:
    claim = observation.evidence_payload_descriptor_claim
    if claim is None:
        return default_payload_digest
    descriptor = validate_payload_descriptor_claim(claim, payload=observation.stable_observation)
    return descriptor.payload_digest


def _head_descends_from(repo: pygit2.Repository, *, selected_head: str, required_head: str) -> bool:
    if selected_head == required_head:
        return True
    try:
        selected = pygit2.Oid(hex=selected_head)
        required = pygit2.Oid(hex=required_head)
    except ValueError as exc:
        raise InvalidRepositoryStateError("existing-head selection contains malformed head") from exc
    if not isinstance(repo.get(selected), pygit2.Commit) or not isinstance(repo.get(required), pygit2.Commit):
        raise InvalidRepositoryStateError("existing-head selection names a missing commit")
    return bool(repo.descendant_of(selected, required))


def _is_coordinator_selection_evidence(
    record: EvidenceRecord,
    *,
    selection: HeadSelectionRecord,
    store: SubstrateStore,
) -> bool:
    if (
        record.ingress_kind != "coordinator"
        or record.binding != selection.binding
        or record.store_id != selection.store_id
        or record.substrate_kind != store.identity.kind
        or record.observed_head != selection.selected_head
    ):
        return False
    stable_observation: dict[str, object] = {
        "binding": selection.binding,
        "store_id": selection.store_id,
        "resource_id": selection.resource_id,
        "substrate_kind": store.identity.kind,
        "head": selection.selected_head,
        "kind": record.evidence_kind,
    }
    if selection.selected_from is not None:
        stable_observation["selected_from"] = selection.selected_from
    return record.stable_observation == stable_observation and record.payload_digest == canonical_digest(
        stable_observation
    )
