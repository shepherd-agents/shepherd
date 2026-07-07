"""Internal retained-output selection coordinator."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

import pygit2

from vcs_core._authority import (
    AUTHORITY_ROUTE_BY_TRANSACTION_KIND,
    AuthorityDecision,
    AuthorityOutcome,
    AuthoritySettlement,
    AuthorityTransactionKind,
    PendingAuthoritySettlement,
    PreparedRetainedOutputSelection,
    RetainedOutputAuthorityDecisionRecord,
    RetainedOutputClassificationBasis,
    RetainedOutputDecisionProvider,
    classify_retained_output_authority_request,
    make_retained_output_decision_record,
    prepare_retained_output_selection_authority,
    retained_output_authority_settlement_metadata,
)
from vcs_core._authority_transactions import (
    begin_pending_authority_settlement,
    clear_pending_authority_transaction,
    ensure_authority_operation_ids_available,
    record_authority_settlement_effect,
    update_pending_authority_settlement,
)
from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._operation_journal_inventory import probe_operation_journal
from vcs_core._permission_plan_evidence import PermissionPlanEvidenceError, validate_permission_plan_evidence
from vcs_core._retained_output_settlement import (
    read_retained_output_settlement,
    retained_output_settlement_ref,
    write_retained_output_settlement,
)
from vcs_core._sibling_group_blockers import refresh_sibling_group_recovery_blockers
from vcs_core._vcscore_admission import mutation_admission
from vcs_core._vcscore_seal import (
    ValidatedRetainedWorkspace,
    _required_snapshot_head,
    _scope_selector,
    _validate_handoff_head,
    _validate_retained_workspace_handle,
)
from vcs_core._world_authority_finalizer import MAX_AUTHORITY_RETRY_ATTEMPTS, WorldAuthorityFinalizer
from vcs_core._world_operation_builder import CandidateSelection, OperationFinalBuilder
from vcs_core._world_types import WORLD_TRANSITION_SCHEMA, WorldSnapshot, canonical_digest
from vcs_core.git_store import diff_workspace_trees
from vcs_core.types import (
    EffectRecord,
    FileChange,
    RetainedOutputSelectionResult,
    RetainedOutputSettlement,
    RetainedWorkspaceHandle,
    ScopeInfo,
    SealCandidateHandoff,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from vcs_core._mutation_admission import MutationAdmission
    from vcs_core._world_storage_manager import WorldStorageManager
    from vcs_core.vcscore import VcsCore


@dataclass(frozen=True)
class _RetainedSelectionAuthorityContext:
    authority_operation_id: str
    settlement_operation_id: str
    prepared: PreparedRetainedOutputSelection
    decisions: tuple[RetainedOutputAuthorityDecisionRecord, ...]
    permission_plan_digest: str
    permission_plan_descriptor: Mapping[str, object]
    authority_context: Mapping[str, object] | None = None


def select_retained_output(
    owner: VcsCore,
    scope_or_handle: ScopeInfo | RetainedWorkspaceHandle | str,
    *,
    parent: ScopeInfo,
    binding: str = "workspace",
    decide: RetainedOutputDecisionProvider | None = None,
    authority_operation_id: str | None = None,
    effective_match_digest: str | None = None,
    authority_surface_plan_digest: str | None = None,
    permission_plan_digest: str | None = None,
    permission_plan_descriptor: Mapping[str, object] | None = None,
    authority_context: Mapping[str, object] | None = None,
) -> RetainedOutputSelectionResult:
    """Select one retained binding output into a live parent binding.

    This is the Track 2 / early Slice 3 control-plane seed below the
    generalized RunOutput boundary-verb surface.
    """
    return _select_retained_candidate_set(
        owner,
        scope_or_handle,
        parent=parent,
        binding=binding,
        decide=decide,
        authority_operation_id=authority_operation_id,
        effective_match_digest=effective_match_digest,
        authority_surface_plan_digest=authority_surface_plan_digest,
        permission_plan_digest=permission_plan_digest,
        permission_plan_descriptor=permission_plan_descriptor,
        authority_context=authority_context,
    )


def _select_retained_candidate_set(
    owner: VcsCore,
    selected_or_handle: ScopeInfo | RetainedWorkspaceHandle | str,
    *,
    parent: ScopeInfo,
    binding: str = "workspace",
    archived: Sequence[ScopeInfo | RetainedWorkspaceHandle | str] = (),
    decide: RetainedOutputDecisionProvider | None = None,
    authority_operation_id: str | None = None,
    effective_match_digest: str | None = None,
    authority_surface_plan_digest: str | None = None,
    permission_plan_digest: str | None = None,
    permission_plan_descriptor: Mapping[str, object] | None = None,
    authority_context: Mapping[str, object] | None = None,
) -> RetainedOutputSelectionResult:
    """Publish one selected retained candidate plus optional archived candidate evidence."""
    with owner._lock:
        retained = owner._seal.validated_retained_workspace(_scope_selector(selected_or_handle))
        if isinstance(selected_or_handle, RetainedWorkspaceHandle):
            _validate_retained_workspace_handle(selected_or_handle, retained)
        parent = owner._live_scope(parent)
        handoff = retained.loaded.handoff
        if binding != handoff.binding:
            raise InvalidRepositoryStateError(
                f"retained output binding {handoff.binding!r} cannot settle requested binding {binding!r}"
            )
        if parent.ref != handoff.parent_ref:
            raise InvalidRepositoryStateError(
                f"retained output {handoff.scope_name!r} belongs to a different parent scope"
            )
        settlement_ref = retained_output_settlement_ref(
            scope_name=handoff.scope_name,
            scope_instance_id=handoff.scope_instance_id,
            binding=handoff.binding,
            candidate_id=handoff.candidate_id,
        )
        if read_retained_output_settlement(owner.store, settlement_ref, missing_ok=True) is not None:
            raise InvalidRepositoryStateError(f"retained output is already settled: {settlement_ref}")

        archived_retained = _validated_archived_candidates(
            owner,
            archived,
            selected=retained,
            parent=parent,
            binding=binding,
        )
        operation_id = _candidate_set_operation_id(
            handoff=handoff,
            settlement_ref=settlement_ref,
            archived_retained=archived_retained,
        )
        manager = owner._world_storage()
        recovered = _recover_published_retained_selection(
            owner,
            manager,
            retained=retained,
            parent=parent,
            operation_id=operation_id,
            settlement_ref=settlement_ref,
            archived_retained=archived_retained,
        )
        if recovered is not None:
            return recovered

        # Probe-uniformity (T1 task-10 tranche, S2 disposition): a published-but-unreceipted
        # APPLICATION world for this output is completed here (its receipt written) and the
        # select refuses as already-settled — never a misleading drift error. Function-local
        # import keeps the selection/application module graph acyclic.
        from vcs_core._retained_output_application import _recover_published_application

        foreign_application = _recover_published_application(
            owner,
            manager,
            retained=retained,
            parent=parent,
            settlement_ref=settlement_ref,
        )
        if foreign_application is not None:
            raise InvalidRepositoryStateError(f"retained output is already settled: {settlement_ref}")

        _selection_admission(owner).require_retained_output_selection_allowed(scope_selector=parent.ref)
        parent_world_oid = owner._current_v2_world_oid(manager, parent.ref)
        if parent_world_oid is None:
            raise InvalidRepositoryStateError(
                f"Cannot select retained output {handoff.scope_name!r}: parent has no current v2 world"
            )
        parent_world = manager.read_world(parent_world_oid)
        basis_world = manager.read_world(handoff.parent_basis_world_oid)
        basis_head = _snapshot_head_or_none(basis_world, handoff.binding)
        current_head = _snapshot_head_or_none(parent_world, handoff.binding)
        if not (basis_head is None and current_head is None) and current_head != basis_head:
            raise InvalidRepositoryStateError(
                "Cannot select retained output "
                f"{handoff.scope_name!r}: parent binding {handoff.binding!r} advanced since child fork basis"
            )

        selection_authority = _prepare_and_decide_retained_selection_authority(
            owner,
            manager,
            retained=retained,
            parent=parent,
            operation_id=operation_id,
            parent_world=parent_world,
            basis_world=basis_world,
            decide=decide,
            authority_operation_id=authority_operation_id,
            effective_match_digest=effective_match_digest,
            authority_surface_plan_digest=authority_surface_plan_digest,
            permission_plan_digest=permission_plan_digest,
            permission_plan_descriptor=permission_plan_descriptor,
            authority_context=authority_context,
        )
        pending_authority_settlement: PendingAuthoritySettlement | None = None
        if selection_authority is not None:
            pending_authority_settlement = begin_pending_authority_settlement(
                owner,
                _retained_output_authority_pending(
                    retained=retained,
                    parent=parent,
                    context=selection_authority,
                    outcome="allowed",
                    settlement="selected",
                    commit_outcome="pending",
                    reason_code="pending_retained_output_selection",
                ),
            )

        heads_by_binding = parent_world.snapshot.by_binding()
        heads_by_binding[handoff.binding] = retained.head
        parents = _selection_parent_worlds(parent_world_oid, retained, archived_retained)
        selection = _candidate_selection(retained)

        def prepared_factory(current_operation_id: str) -> Any:
            transition: dict[str, object] = {
                "schema": WORLD_TRANSITION_SCHEMA,
                "operation_id": current_operation_id,
                "parent_worlds": list(parents),
                "input_world": parent_world_oid,
                "semantic_op": "retained-output-selection",
                "handoff_ref": handoff.handoff_ref,
                "parent_basis_world_oid": handoff.parent_basis_world_oid,
            }
            if archived_retained:
                transition["archived_handoff_refs"] = [item.loaded.handoff.handoff_ref for item in archived_retained]
            builder = OperationFinalBuilder(current_operation_id).select_candidate_plan(
                plan=manager.plan_candidate_selection(
                    operation_id=current_operation_id,
                    selection=selection,
                    selection_kind="child-produced",
                    producer_operation_id=handoff.producer_operation_id,
                    producer_world_oid=handoff.output_world_oid,
                    role=retained.head.role,
                )
            )
            for existing in parent_world.snapshot.by_binding().values():
                if existing.binding == handoff.binding:
                    continue
                builder.select_unchanged(
                    plan=manager.plan_unchanged_selection(
                        operation_id=current_operation_id,
                        head=existing,
                        input_world_oid=parent_world_oid,
                    )
                )
            for archived_item in archived_retained:
                builder.archive_candidate(selection=_candidate_selection(archived_item))
            return builder.build_prepared(
                operation_kind="retained-output-selection",
                target_ref=parent.ref,
                input_world_oid=parent_world_oid,
                snapshot=WorldSnapshot.from_heads(heads_by_binding),
                transition=transition,
                parents=parents,
            )

        outcome = WorldAuthorityFinalizer(manager).publish_or_recover(
            operation_id=operation_id,
            prepared_factory=prepared_factory,
            target_ref=parent.ref,
            expected_input_world_oid=_existing_retained_selection_input_world_oid(manager, operation_id)
            or parent_world_oid,
        )
        if outcome.world_oid is None:
            raise InvalidRepositoryStateError(f"retained output selection {operation_id!r} did not publish a world")
        result = _write_retained_selection_settlement(
            owner,
            manager,
            retained=retained,
            parent=parent,
            operation_id=outcome.operation_id,
            parent_world_before=parent_world_oid,
            parent_world_after=outcome.world_oid,
            settlement_ref=settlement_ref,
            archived_retained=archived_retained,
            authority_operation_id=None if selection_authority is None else selection_authority.authority_operation_id,
            authority_settlement_operation_id=(
                None if selection_authority is None else selection_authority.settlement_operation_id
            ),
            authority_outcome=None if selection_authority is None else "allowed",
        )
        if selection_authority is not None:
            assert pending_authority_settlement is not None
            pending_authority_settlement = update_pending_authority_settlement(
                owner,
                pending_authority_settlement,
                phase="adopted",
                commit_outcome="selected",
                reason_code="selected_after_allowed_decision",
            )
            record_retained_output_authority_final_settlement(
                owner,
                parent=parent,
                settlement_operation_id=selection_authority.settlement_operation_id,
                authority_operation_id=selection_authority.authority_operation_id,
                selection_operation_id=selection_authority.prepared.selection_operation_id,
                cohort_id=selection_authority.prepared.cohort_id,
                candidate_digest=selection_authority.prepared.candidate_digest,
                outcome="allowed",
                settlement="selected",
                commit_outcome="selected",
                decision_ids=tuple(decision.decision_id for decision in selection_authority.decisions),
                reason_code="selected_after_allowed_decision",
                permission_plan_digest=selection_authority.permission_plan_digest,
                permission_plan_descriptor=selection_authority.permission_plan_descriptor,
                authority_context=selection_authority.authority_context,
            )
            clear_pending_authority_transaction(owner, selection_authority.settlement_operation_id)
            result = replace(
                result,
                authority_operation_id=selection_authority.authority_operation_id,
                authority_settlement_operation_id=selection_authority.settlement_operation_id,
                authority_outcome="allowed",
            )
        return result


def _recover_published_retained_selection(
    owner: VcsCore,
    manager: WorldStorageManager,
    *,
    retained: ValidatedRetainedWorkspace,
    parent: ScopeInfo,
    operation_id: str,
    settlement_ref: str,
    archived_retained: tuple[ValidatedRetainedWorkspace, ...] = (),
) -> RetainedOutputSelectionResult | None:
    finalizer = WorldAuthorityFinalizer(manager)
    for attempt_id in _retained_selection_attempt_ids(finalizer, operation_id):
        closed = _journal_fields(manager, attempt_id, family="closed")
        if closed is not None:
            parent_world_before = _required_journal_str(closed, "input_world_oid")
            outcome = finalizer.complete_existing(
                operation_id=attempt_id,
                target_ref=parent.ref,
                expected_input_world_oid=parent_world_before,
                missing_ok=True,
            )
            if outcome is not None and outcome.status != "retry_required":
                finalizer.require_terminal_authority(outcome, target_ref=parent.ref)
                assert outcome.world_oid is not None
                return _write_retained_selection_settlement(
                    owner,
                    manager,
                    retained=retained,
                    parent=parent,
                    operation_id=attempt_id,
                    parent_world_before=parent_world_before,
                    parent_world_after=outcome.world_oid,
                    settlement_ref=settlement_ref,
                    archived_retained=archived_retained,
                )

        opened = _journal_fields(manager, attempt_id, family="open")
        if opened is None:
            continue
        parent_world_before = _required_journal_str(opened, "input_world_oid")
        outcome = finalizer.complete_existing(
            operation_id=attempt_id,
            target_ref=parent.ref,
            expected_input_world_oid=parent_world_before,
            missing_ok=True,
        )
        if outcome is not None and outcome.status != "retry_required":
            finalizer.require_terminal_authority(outcome, target_ref=parent.ref)
            assert outcome.world_oid is not None
            return _write_retained_selection_settlement(
                owner,
                manager,
                retained=retained,
                parent=parent,
                operation_id=attempt_id,
                parent_world_before=parent_world_before,
                parent_world_after=outcome.world_oid,
                settlement_ref=settlement_ref,
                archived_retained=archived_retained,
            )
    return None


def _recover_current_parent_retained_selection(
    owner: VcsCore,
    manager: WorldStorageManager,
    *,
    retained: ValidatedRetainedWorkspace,
    parent: ScopeInfo,
    settlement_ref: str,
) -> RetainedOutputSelectionResult | None:
    current_world_oid = owner._current_v2_world_oid(manager, parent.ref)
    if current_world_oid is None:
        return None
    handoff = retained.loaded.handoff
    for world_oid in _reachable_parent_world_oids(manager, current_world_oid):
        world = manager.read_world(world_oid)
        if world.transition.get("semantic_op") != "retained-output-selection":
            continue
        if world.transition.get("handoff_ref") != handoff.handoff_ref:
            continue
        operation_id = _required_transition_str(world.transition, "operation_id")
        parent_world_before = _required_transition_str(world.transition, "input_world")
        return _write_retained_selection_settlement(
            owner,
            manager,
            retained=retained,
            parent=parent,
            operation_id=operation_id,
            parent_world_before=parent_world_before,
            parent_world_after=world_oid,
            settlement_ref=settlement_ref,
        )
    return None


# Per-kind negative-settlement vocabulary for the shared prepare/decide flow (T1 D7): the
# refusal/denial spelling is a function of the settling verb — the future settlement-action
# registry's per-verb row (g10).
_NEGATIVE_AUTHORITY_SETTLEMENT_BY_KIND: dict[str, tuple[str, str, str, str]] = {
    # kind -> (negative settlement, refused commit outcome, denied commit outcome, verb noun)
    "retained_output_selection": ("not_selected", "not_selected_refused", "not_selected_denied", "selection"),
    "retained_output_application": ("not_applied", "not_applied_refused", "not_applied_denied", "application"),
}


def _prepare_and_decide_retained_selection_authority(
    owner: VcsCore,
    manager: WorldStorageManager,
    *,
    retained: ValidatedRetainedWorkspace,
    parent: ScopeInfo,
    operation_id: str,
    parent_world: Any,
    basis_world: Any,
    decide: RetainedOutputDecisionProvider | None,
    authority_operation_id: str | None,
    effective_match_digest: str | None,
    authority_surface_plan_digest: str | None,
    permission_plan_digest: str | None,
    permission_plan_descriptor: Mapping[str, object] | None,
    authority_context: Mapping[str, object] | None,
    transaction_kind: AuthorityTransactionKind = "retained_output_selection",
) -> _RetainedSelectionAuthorityContext | None:
    if decide is None:
        return None
    negative_settlement, refused_outcome, denied_outcome, verb_noun = _NEGATIVE_AUTHORITY_SETTLEMENT_BY_KIND[
        transaction_kind
    ]
    try:
        validated_permission_plan_descriptor = validate_permission_plan_evidence(
            permission_plan_digest_value=permission_plan_digest,
            permission_plan_descriptor=permission_plan_descriptor,
            expected_route=AUTHORITY_ROUTE_BY_TRANSACTION_KIND[transaction_kind],
            expected_effective_match_digest=effective_match_digest,
            expected_authority_surface_plan_digest=authority_surface_plan_digest,
        )
    except PermissionPlanEvidenceError as exc:
        raise InvalidRepositoryStateError(f"retained-output authority PermissionPlan evidence invalid: {exc}") from exc
    validated_permission_plan_digest = cast("str", permission_plan_digest)
    handoff = retained.loaded.handoff
    file_changes = _retained_selection_authority_file_changes(manager, retained=retained, basis_world=basis_world)
    classification_basis = _retained_selection_classification_basis(file_changes, changed_paths=handoff.changed_paths)
    changed_paths = tuple(change.path for change in file_changes) if file_changes is not None else handoff.changed_paths
    prepared = prepare_retained_output_selection_authority(
        selection_operation_id=operation_id,
        handoff=handoff,
        parent=parent,
        changed_paths=changed_paths,
        classification_basis=classification_basis,
        transaction_kind=transaction_kind,
    )
    authority_operation_id = authority_operation_id or owner._new_operation_id()
    settlement_operation_id = f"{authority_operation_id}_settlement"
    ensure_authority_operation_ids_available(owner, operation_id, authority_operation_id, settlement_operation_id)
    requests = _retained_selection_authority_requests(
        prepared,
        file_changes=file_changes,
        parent_world=parent_world,
        retained=retained,
    )
    decisions = _record_retained_output_authority_decisions(
        owner,
        parent=parent,
        authority_operation_id=authority_operation_id,
        prepared=prepared,
        requests=requests,
        decide=decide,
        effective_match_digest=effective_match_digest,
        authority_surface_plan_digest=authority_surface_plan_digest,
        permission_plan_digest=validated_permission_plan_digest,
        permission_plan_descriptor=validated_permission_plan_descriptor,
        authority_context=authority_context,
    )
    context = _RetainedSelectionAuthorityContext(
        authority_operation_id=authority_operation_id,
        settlement_operation_id=settlement_operation_id,
        prepared=prepared,
        decisions=decisions,
        permission_plan_digest=validated_permission_plan_digest,
        permission_plan_descriptor=validated_permission_plan_descriptor,
        authority_context=authority_context,
    )
    for negative_outcome, commit_outcome in (("refused", refused_outcome), ("denied", denied_outcome)):
        if not any(decision.outcome == negative_outcome for decision in decisions):
            continue
        reason_code = f"{negative_outcome}_decision"
        begin_pending_authority_settlement(
            owner,
            _retained_output_authority_pending(
                retained=retained,
                parent=parent,
                context=context,
                outcome=cast("AuthorityOutcome", negative_outcome),
                settlement=cast("AuthoritySettlement", negative_settlement),
                commit_outcome=commit_outcome,
                reason_code=reason_code,
            ).with_update(phase="discarded"),
        )
        record_retained_output_authority_final_settlement(
            owner,
            parent=parent,
            settlement_operation_id=context.settlement_operation_id,
            authority_operation_id=context.authority_operation_id,
            cohort_id=context.prepared.cohort_id,
            candidate_digest=context.prepared.candidate_digest,
            outcome=cast("AuthorityOutcome", negative_outcome),
            settlement=negative_settlement,
            commit_outcome=commit_outcome,
            decision_ids=tuple(decision.decision_id for decision in decisions),
            reason_code=reason_code,
            permission_plan_digest=context.permission_plan_digest,
            permission_plan_descriptor=context.permission_plan_descriptor,
            authority_context=context.authority_context,
            **_settling_operation_kwarg(context.prepared),
        )
        clear_pending_authority_transaction(owner, context.settlement_operation_id)
        raise InvalidRepositoryStateError(
            f"retained-output {verb_noun} {negative_outcome} by authority: "
            f"{_authority_decision_reason(decisions, outcome=cast('AuthorityOutcome', negative_outcome))}"
        )
    return context


def _settling_operation_kwarg(prepared: PreparedRetainedOutputSelection) -> dict[str, str]:
    """Spell the settling operation id per transaction kind (T1 D7 evidence naming)."""
    if prepared.transaction_kind == "retained_output_application":
        return {"application_operation_id": prepared.selection_operation_id}
    return {"selection_operation_id": prepared.selection_operation_id}


def _retained_selection_authority_file_changes(
    manager: WorldStorageManager,
    *,
    retained: ValidatedRetainedWorkspace,
    basis_world: Any,
) -> tuple[FileChange, ...] | None:
    handoff = retained.loaded.handoff
    try:
        basis_head = basis_world.snapshot.head_for(handoff.binding)
    except KeyError:
        basis_head = None
    if basis_head == retained.head:
        return ()
    if basis_head is not None and (
        basis_head.store_id != retained.head.store_id
        or basis_head.resource_id != retained.head.resource_id
        or basis_head.binding != retained.head.binding
    ):
        return None
    try:
        substrate = manager.store(retained.head.store_id)
        candidate_metadata = substrate.read_revision_metadata(retained.head.head)
    except (InvalidRepositoryStateError, KeyError, ValueError):
        return None
    if candidate_metadata.byte_authority != "tree-backed" or candidate_metadata.git_tree_oid is None:
        return None
    candidate_tree_oid = pygit2.Oid(hex=candidate_metadata.git_tree_oid)
    if basis_head is None:
        try:
            empty_tree_oid = substrate.repo.TreeBuilder().write()
        except pygit2.GitError:
            return None
        return tuple(diff_workspace_trees(substrate.repo, empty_tree_oid, candidate_tree_oid))
    try:
        basis_metadata = substrate.read_revision_metadata(basis_head.head)
    except (InvalidRepositoryStateError, KeyError, ValueError):
        return None
    if basis_metadata.byte_authority != "tree-backed" or basis_metadata.git_tree_oid is None:
        return None
    return tuple(
        diff_workspace_trees(
            substrate.repo,
            pygit2.Oid(hex=basis_metadata.git_tree_oid),
            candidate_tree_oid,
        )
    )


def _retained_selection_authority_requests(
    prepared: PreparedRetainedOutputSelection,
    *,
    file_changes: tuple[FileChange, ...] | None,
    parent_world: Any,
    retained: ValidatedRetainedWorkspace,
) -> tuple[Any, ...]:
    del parent_world
    if file_changes is None:
        if prepared.changed_paths:
            return tuple(
                classify_retained_output_authority_request(
                    prepared=prepared,
                    candidate_index=index,
                    path=path,
                    status="modified",
                    mutates=True,
                    classification_basis="changed_paths_fallback",
                )
                for index, path in enumerate(prepared.changed_paths)
            )
        return (
            classify_retained_output_authority_request(
                prepared=prepared,
                candidate_index=0,
                path="",
                status="unknown",
                mutates=True,
                classification_basis="unclassifiable",
                reason_code="unclassifiable_retained_output",
            ),
        )
    if not file_changes:
        return (
            classify_retained_output_authority_request(
                prepared=prepared,
                candidate_index=0,
                path="",
                status="unchanged",
                mutates=False,
                classification_basis="exact_tree_diff",
            ),
        )
    return tuple(
        classify_retained_output_authority_request(
            prepared=prepared,
            candidate_index=index,
            path=change.path,
            status=change.status,
            mutates=True,
            classification_basis="exact_tree_diff",
        )
        for index, change in enumerate(file_changes)
    )


def _retained_selection_classification_basis(
    file_changes: tuple[FileChange, ...] | None,
    *,
    changed_paths: Sequence[str],
) -> RetainedOutputClassificationBasis:
    if file_changes is not None:
        return "exact_tree_diff"
    if changed_paths:
        return "changed_paths_fallback"
    return "unclassifiable"


def _record_retained_output_authority_decisions(
    owner: VcsCore,
    *,
    parent: ScopeInfo,
    authority_operation_id: str,
    prepared: PreparedRetainedOutputSelection,
    requests: Sequence[Any],
    decide: RetainedOutputDecisionProvider,
    effective_match_digest: str | None,
    authority_surface_plan_digest: str | None,
    permission_plan_digest: str,
    permission_plan_descriptor: Mapping[str, object],
    authority_context: Mapping[str, object] | None,
) -> tuple[RetainedOutputAuthorityDecisionRecord, ...]:
    decisions: list[RetainedOutputAuthorityDecisionRecord] = []
    authority_metadata: dict[str, object] = {
        "cohort_id": prepared.cohort_id,
        "candidate_digest": prepared.candidate_digest,
        "monitor_basis": "carrier_check_at_commit",
        "route": "retained_output_selection",
        "permission_plan_digest": permission_plan_digest,
        "permission_plan_descriptor": dict(permission_plan_descriptor),
    }
    if authority_context is not None:
        authority_metadata["authority_context"] = dict(authority_context)
    with owner.runtime_activity(
        scope=parent,
        operation_id=authority_operation_id,
        operation_label="skeleton retained-output selection authority",
        operation_kind="skeleton.authority.retained-output-selection",
        operation_metadata={"authority": authority_metadata},
    ) as operation:
        if operation is None:
            raise RuntimeError("retained-output authority requires an operation boundary.")
        owner._pipeline.record_one(
            EffectRecord(
                effect_type="PreparedRetainedOutputSelection",
                metadata=prepared.to_metadata(
                    operation_id=authority_operation_id,
                    authority_context=authority_context,
                ),
            ),
            substrate="vcscore.authority",
            scope=parent,
        )
        for decision_index, request in enumerate(requests):
            decision: AuthorityDecision | AuthorityOutcome
            if request.reason_code is not None:
                decision = AuthorityDecision(
                    outcome="refused",
                    reason_code=request.reason_code,
                    request_id=request.request_id,
                    monitor_basis=request.match_view.monitor_basis,
                    completeness="incomplete",
                )
            else:
                decision = decide(request)
            record = make_retained_output_decision_record(
                request,
                decision,
                decision_index=decision_index,
                effective_match_digest=effective_match_digest,
                authority_surface_plan_digest=authority_surface_plan_digest,
                permission_plan_digest=permission_plan_digest,
                permission_plan_descriptor=permission_plan_descriptor,
            )
            decisions.append(record)
            owner._pipeline.record_one(
                EffectRecord(
                    effect_type="RetainedOutputAuthorityDecision",
                    metadata=record.to_metadata(
                        cohort_id=prepared.cohort_id,
                        operation_id=authority_operation_id,
                        authority_context=authority_context,
                    ),
                ),
                substrate="vcscore.authority",
                scope=parent,
            )
    return tuple(decisions)


def _retained_output_authority_pending(
    *,
    retained: ValidatedRetainedWorkspace,
    parent: ScopeInfo,
    context: _RetainedSelectionAuthorityContext,
    outcome: AuthorityOutcome,
    settlement: AuthoritySettlement,
    commit_outcome: str,
    reason_code: str,
) -> PendingAuthoritySettlement:
    handoff = retained.loaded.handoff
    return PendingAuthoritySettlement(
        settlement_operation_id=context.settlement_operation_id,
        authority_operation_id=context.authority_operation_id,
        scope_name=handoff.scope_name,
        scope_ref=handoff.scope_ref,
        scope_instance_id=handoff.scope_instance_id,
        scope_world_id=handoff.output_world_oid,
        parent_scope_name=parent.name,
        parent_scope_ref=parent.ref,
        parent_scope_instance_id=parent.instance_id,
        parent_scope_world_id=parent.world_id,
        cohort_id=context.prepared.cohort_id,
        candidate_digest=context.prepared.candidate_digest,
        outcome=outcome,
        settlement=settlement,
        commit_outcome=commit_outcome,  # type: ignore[arg-type]
        decision_ids=tuple(decision.decision_id for decision in context.decisions),
        reason_code=reason_code,
        transaction_kind=context.prepared.transaction_kind,
        authority_context=None if context.authority_context is None else dict(context.authority_context),
        permission_plan_digest=context.permission_plan_digest,
        permission_plan_descriptor=dict(context.permission_plan_descriptor),
        **_settling_operation_kwarg(context.prepared),  # type: ignore[arg-type]
    )


def record_retained_output_authority_final_settlement(
    owner: VcsCore,
    *,
    parent: ScopeInfo,
    settlement_operation_id: str,
    authority_operation_id: str,
    cohort_id: str,
    candidate_digest: str,
    outcome: AuthorityOutcome,
    settlement: str,
    commit_outcome: str,
    decision_ids: Sequence[str],
    reason_code: str,
    selection_operation_id: str | None = None,
    application_operation_id: str | None = None,
    permission_plan_digest: str | None = None,
    permission_plan_descriptor: Mapping[str, object] | None = None,
    authority_context: Mapping[str, object] | None = None,
) -> None:
    verb = "application" if application_operation_id is not None else "selection"
    record_authority_settlement_effect(
        owner,
        scope=parent,
        settlement_operation_id=settlement_operation_id,
        authority_operation_id=authority_operation_id,
        cohort_id=cohort_id,
        candidate_digest=candidate_digest,
        monitor_basis="carrier_check_at_commit",
        operation_label=f"skeleton retained-output {verb} authority settlement",
        operation_kind=f"skeleton.authority.retained-output-{verb}.settlement",
        effect_type="RetainedOutputAuthoritySettlement",
        effect_metadata=retained_output_authority_settlement_metadata(
            operation_id=authority_operation_id,
            cohort_id=cohort_id,
            candidate_digest=candidate_digest,
            outcome=outcome,
            settlement=settlement,  # type: ignore[arg-type]
            commit_outcome=commit_outcome,
            decision_ids=decision_ids,
            reason_code=reason_code,
            selection_operation_id=selection_operation_id,
            application_operation_id=application_operation_id,
            permission_plan_digest=permission_plan_digest,
            permission_plan_descriptor=permission_plan_descriptor,
            authority_context=authority_context,
        ),
        authority_context=authority_context,
    )


def _authority_decision_reason(
    decisions: Sequence[RetainedOutputAuthorityDecisionRecord],
    *,
    outcome: AuthorityOutcome,
) -> str:
    for decision in decisions:
        if decision.outcome == outcome:
            return decision.reason_code
    return outcome


def _reachable_parent_world_oids(manager: WorldStorageManager, start_oid: str) -> tuple[str, ...]:
    pending = [start_oid]
    seen: set[str] = set()
    ordered: list[str] = []
    while pending:
        oid = pending.pop()
        if oid in seen:
            continue
        seen.add(oid)
        ordered.append(oid)
        world = manager.read_world(oid)
        pending.extend(parent for parent in world.parent_oids if parent not in seen)
    return tuple(ordered)


def _validated_archived_candidates(
    owner: VcsCore,
    archived: Sequence[ScopeInfo | RetainedWorkspaceHandle | str],
    *,
    selected: ValidatedRetainedWorkspace,
    parent: ScopeInfo,
    binding: str,
) -> tuple[ValidatedRetainedWorkspace, ...]:
    if not archived:
        return ()
    if isinstance(archived, str):
        raise TypeError("archived must be a sequence of retained outputs, not a single string")
    selected_handoff = selected.loaded.handoff
    seen = {_retained_candidate_identity(selected)}
    archived_retained: list[ValidatedRetainedWorkspace] = []
    for archived_item in archived:
        retained = owner._seal.validated_retained_workspace(_scope_selector(archived_item))
        if isinstance(archived_item, RetainedWorkspaceHandle):
            _validate_retained_workspace_handle(archived_item, retained)
        handoff = retained.loaded.handoff
        identity = _retained_candidate_identity(retained)
        if identity in seen:
            raise InvalidRepositoryStateError("retained output cohort names the same output more than once")
        seen.add(identity)
        if binding != handoff.binding:
            raise InvalidRepositoryStateError(
                f"retained output binding {handoff.binding!r} cannot archive requested binding {binding!r}"
            )
        if handoff.parent_ref != parent.ref:
            raise InvalidRepositoryStateError(
                f"retained output {handoff.scope_name!r} belongs to a different parent scope"
            )
        if handoff.parent_basis_world_oid != selected_handoff.parent_basis_world_oid:
            raise InvalidRepositoryStateError("retained output cohort must share one parent basis world")
        settlement_ref = retained_output_settlement_ref(
            scope_name=handoff.scope_name,
            scope_instance_id=handoff.scope_instance_id,
            binding=handoff.binding,
            candidate_id=handoff.candidate_id,
        )
        if read_retained_output_settlement(owner.store, settlement_ref, missing_ok=True) is not None:
            raise InvalidRepositoryStateError(f"retained output is already settled: {settlement_ref}")
        archived_retained.append(retained)
    return tuple(sorted(archived_retained, key=_retained_candidate_identity))


def _selection_parent_worlds(
    parent_world_oid: str,
    retained: ValidatedRetainedWorkspace,
    archived_retained: tuple[ValidatedRetainedWorkspace, ...],
) -> tuple[str, ...]:
    parents = [parent_world_oid, retained.loaded.handoff.output_world_oid]
    parents.extend(item.loaded.handoff.output_world_oid for item in archived_retained)
    return tuple(dict.fromkeys(parents))


def _candidate_selection(retained: ValidatedRetainedWorkspace) -> CandidateSelection:
    return CandidateSelection(
        retained.loaded.candidate_tuple.candidate,
        retained.loaded.candidate_tuple.candidate_commit,
        retained.loaded.candidate_tuple,
    )


def _retained_candidate_identity(retained: ValidatedRetainedWorkspace) -> tuple[str, str, str]:
    handoff = retained.loaded.handoff
    return (handoff.scope_ref, handoff.scope_instance_id, handoff.candidate_id)


def _write_retained_selection_settlement(
    owner: VcsCore,
    manager: WorldStorageManager,
    *,
    retained: ValidatedRetainedWorkspace,
    parent: ScopeInfo,
    operation_id: str,
    parent_world_before: str,
    parent_world_after: str,
    settlement_ref: str,
    archived_retained: tuple[ValidatedRetainedWorkspace, ...] = (),
    authority_operation_id: str | None = None,
    authority_settlement_operation_id: str | None = None,
    authority_outcome: str | None = None,
) -> RetainedOutputSelectionResult:
    handoff = retained.loaded.handoff
    _validate_retained_selection_world(
        manager,
        retained=retained,
        operation_id=operation_id,
        parent_world_before=parent_world_before,
        parent_world_after=parent_world_after,
        archived_retained=archived_retained,
    )
    settlement = RetainedOutputSettlement(
        scope_name=handoff.scope_name,
        scope_ref=handoff.scope_ref,
        scope_instance_id=handoff.scope_instance_id,
        parent_ref=parent.ref,
        handoff_ref=handoff.handoff_ref,
        output_world_oid=handoff.output_world_oid,
        binding=handoff.binding,
        store_id=handoff.store_id,
        resource_id=handoff.resource_id,
        candidate_id=handoff.candidate_id,
        candidate_head=handoff.candidate_head,
        action="selected",
        operation_id=operation_id,
        parent_world_before=parent_world_before,
        parent_world_after=parent_world_after,
        settlement_ref=settlement_ref,
        authority_operation_id=authority_operation_id,
        authority_settlement_operation_id=authority_settlement_operation_id,
        authority_outcome=authority_outcome,
    )
    write_retained_output_settlement(owner.store, settlement)
    return RetainedOutputSelectionResult(
        scope=retained.entry_scope,
        parent=parent,
        output_world_oid=handoff.output_world_oid,
        parent_world_before=parent_world_before,
        parent_world_after=parent_world_after,
        settlement=settlement,
        authority_operation_id=authority_operation_id,
        authority_settlement_operation_id=authority_settlement_operation_id,
        authority_outcome=authority_outcome,
    )


def _validate_retained_selection_world(
    manager: WorldStorageManager,
    *,
    retained: ValidatedRetainedWorkspace,
    operation_id: str,
    parent_world_before: str,
    parent_world_after: str,
    archived_retained: tuple[ValidatedRetainedWorkspace, ...] = (),
) -> None:
    handoff = retained.loaded.handoff
    world = manager.read_world(parent_world_after)
    transition = world.transition
    if transition.get("operation_id") != operation_id:
        raise InvalidRepositoryStateError("retained output selection world operation_id disagrees with settlement")
    if transition.get("semantic_op") != "retained-output-selection":
        raise InvalidRepositoryStateError("retained output selection world has unexpected semantic operation")
    if transition.get("handoff_ref") != handoff.handoff_ref:
        raise InvalidRepositoryStateError("retained output selection world handoff_ref disagrees with settlement")
    if transition.get("input_world") != parent_world_before:
        raise InvalidRepositoryStateError("retained output selection world input_world disagrees with settlement")
    if transition.get("parent_basis_world_oid") != handoff.parent_basis_world_oid:
        raise InvalidRepositoryStateError("retained output selection world parent basis disagrees with handoff")
    if parent_world_before not in world.parent_oids:
        raise InvalidRepositoryStateError("retained output selection world does not name parent input world")
    if handoff.output_world_oid not in world.parent_oids:
        raise InvalidRepositoryStateError("retained output selection world does not name retained output world")
    for archived in archived_retained:
        archived_handoff = archived.loaded.handoff
        if archived_handoff.output_world_oid not in world.parent_oids:
            raise InvalidRepositoryStateError("retained output selection world does not name archived output world")
    _validate_archived_handoff_refs(world.transition, archived_retained)
    _validate_handoff_head(
        handoff,
        _required_snapshot_head(world, handoff.binding, context="retained output selection"),
    )
    selected = world.operation_final.get("selected")
    if not isinstance(selected, dict) or selected.get(handoff.binding) != handoff.candidate_head:
        raise InvalidRepositoryStateError("retained output selection final record does not select handoff candidate")
    _validate_candidate_outcomes(world.operation_final, retained, archived_retained)


def _validate_archived_handoff_refs(
    transition: dict[str, object],
    archived_retained: tuple[ValidatedRetainedWorkspace, ...],
) -> None:
    raw_refs = transition.get("archived_handoff_refs")
    if not archived_retained:
        return
    expected = [archived.loaded.handoff.handoff_ref for archived in archived_retained]
    if raw_refs != expected:
        raise InvalidRepositoryStateError("retained output selection world archived handoff refs disagree")


def _snapshot_head_or_none(world: Any, binding: str) -> Any | None:
    try:
        return world.snapshot.head_for(binding)
    except KeyError:
        return None


def _validate_candidate_outcomes(
    operation_final: dict[str, object],
    retained: ValidatedRetainedWorkspace,
    archived_retained: tuple[ValidatedRetainedWorkspace, ...],
) -> None:
    raw_outcomes = operation_final.get("candidate_outcomes")
    if not isinstance(raw_outcomes, list):
        raise InvalidRepositoryStateError("retained output selection final record is missing candidate outcomes")
    expected = {_expected_candidate_outcome_key(retained, outcome="selected")}
    expected.update(_expected_candidate_outcome_key(archived, outcome="archived") for archived in archived_retained)
    actual = {_candidate_outcome_key(raw_outcome) for raw_outcome in raw_outcomes}
    if archived_retained and actual != expected:
        raise InvalidRepositoryStateError("retained output selection final record candidate outcomes disagree")
    if not archived_retained and not expected <= actual:
        raise InvalidRepositoryStateError("retained output selection final record candidate outcomes disagree")


def _expected_candidate_outcome_key(
    retained: ValidatedRetainedWorkspace,
    *,
    outcome: str,
) -> tuple[str, str, str, str]:
    handoff = retained.loaded.handoff
    return (
        handoff.binding,
        handoff.candidate_head,
        outcome,
        handoff.candidate_id,
    )


def _candidate_outcome_key(raw_outcome: object) -> tuple[str, str, str, str]:
    if not isinstance(raw_outcome, dict):
        raise InvalidRepositoryStateError("retained output selection final record has malformed candidate outcome")
    return (
        _required_outcome_str(raw_outcome, "binding"),
        _required_outcome_str(raw_outcome, "candidate"),
        _required_outcome_str(raw_outcome, "outcome"),
        _optional_outcome_str(raw_outcome, "candidate_id", default="primary"),
    )


def _required_outcome_str(raw_outcome: dict[object, object], field: str) -> str:
    value = raw_outcome.get(field)
    if not isinstance(value, str) or not value:
        raise InvalidRepositoryStateError(
            f"retained output selection final record has malformed candidate outcome {field!r}"
        )
    return value


def _optional_outcome_str(raw_outcome: dict[object, object], field: str, *, default: str) -> str:
    value = raw_outcome.get(field, default)
    if not isinstance(value, str) or not value:
        raise InvalidRepositoryStateError(
            f"retained output selection final record has malformed candidate outcome {field!r}"
        )
    return value


def _retained_selection_attempt_ids(finalizer: WorldAuthorityFinalizer, operation_id: str) -> tuple[str, ...]:
    return (
        operation_id,
        *(
            finalizer.retry_operation_id(operation_id, retry_count)
            for retry_count in range(1, MAX_AUTHORITY_RETRY_ATTEMPTS + 1)
        ),
    )


def _journal_fields(manager: WorldStorageManager, operation_id: str, *, family: str) -> dict[str, object] | None:
    item = probe_operation_journal(manager.world_store.repo, operation_id, family=family)
    if item.health.presence == "absent":
        return None
    if item.health.validity != "valid":
        issue_codes = ", ".join(item.health.issue_codes) or item.health.primary_issue
        raise InvalidRepositoryStateError(f"operation journal inventory item {item.id!r} is invalid: {issue_codes}")
    return dict(item.fields)


def _existing_retained_selection_input_world_oid(manager: WorldStorageManager, operation_id: str) -> str | None:
    finalizer = WorldAuthorityFinalizer(manager)
    for attempt_id in _retained_selection_attempt_ids(finalizer, operation_id):
        for family in ("closed", "archived", "open"):
            fields = _journal_fields(manager, attempt_id, family=family)
            if fields is None:
                continue
            return _required_journal_str(fields, "input_world_oid")
    return None


def _required_journal_str(fields: dict[str, object], field: str) -> str:
    value = fields.get(field)
    if not isinstance(value, str) or not value:
        raise InvalidRepositoryStateError(f"operation journal field {field!r} is required")
    return value


def _required_transition_str(fields: dict[str, object], field: str) -> str:
    value = fields.get(field)
    if not isinstance(value, str) or not value:
        raise InvalidRepositoryStateError(f"world transition field {field!r} is required")
    return value


def _selection_admission(owner: VcsCore) -> MutationAdmission:
    return mutation_admission(
        owner,
        sibling_group_blockers=lambda: refresh_sibling_group_recovery_blockers(owner),
    )


def _candidate_set_operation_id(
    *,
    handoff: SealCandidateHandoff,
    settlement_ref: str,
    archived_retained: tuple[ValidatedRetainedWorkspace, ...],
) -> str:
    if not archived_retained:
        return _settlement_operation_id(handoff=handoff, settlement_ref=settlement_ref)
    digest = canonical_digest(
        {
            "schema": "vcscore/retained-candidate-set-selection-operation-id/v1",
            "selected": _candidate_set_operation_identity(handoff, outcome="selected"),
            "archived": [
                _candidate_set_operation_identity(archived.loaded.handoff, outcome="archived")
                for archived in archived_retained
            ],
            "settlement_ref": settlement_ref,
        }
    ).removeprefix("sha256:")
    return f"select_retained_set_{digest[:32]}"


def _candidate_set_operation_identity(handoff: SealCandidateHandoff, *, outcome: str) -> dict[str, str]:
    return {
        "outcome": outcome,
        "parent_ref": handoff.parent_ref,
        "handoff_ref": handoff.handoff_ref,
        "scope_ref": handoff.scope_ref,
        "scope_instance_id": handoff.scope_instance_id,
        "binding": handoff.binding,
        "candidate_id": handoff.candidate_id,
        "candidate_head": handoff.candidate_head,
        "output_world_oid": handoff.output_world_oid,
        "parent_basis_world_oid": handoff.parent_basis_world_oid,
        "store_id": handoff.store_id,
        "resource_id": handoff.resource_id,
    }


def _settlement_operation_id(*, handoff: SealCandidateHandoff, settlement_ref: str) -> str:
    digest = canonical_digest(
        {
            "schema": "vcscore/retained-output-selection-operation-id/v1",
            "parent_ref": handoff.parent_ref,
            "handoff_ref": handoff.handoff_ref,
            "scope_ref": handoff.scope_ref,
            "scope_instance_id": handoff.scope_instance_id,
            "binding": handoff.binding,
            "candidate_id": handoff.candidate_id,
            "output_world_oid": handoff.output_world_oid,
            "parent_basis_world_oid": handoff.parent_basis_world_oid,
            "settlement_ref": settlement_ref,
        }
    ).removeprefix("sha256:")
    return f"select_retained_{digest[:32]}"
