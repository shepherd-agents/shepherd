"""Internal retained-output *application* coordinator (the whole-output `apply` verb, T1 D1-A).

`select` is fast-forward-only: it advances a parent binding to a retained
candidate iff the parent has not moved since the candidate's fork basis. `apply`
lifts that restriction for the disjoint case: when the parent has advanced but the
candidate's changed paths do not overlap the parent's, `apply` three-way-merges
the candidate delta onto the current parent and publishes the merged state as a
new **application** world (``semantic_op: "retained-output-application"``).

Soundness (T1 D2): `apply` succeeds only when the candidate's changed paths and
the parent's changed-since-basis paths are **equal-or-prefix-or-alias disjoint**
(compared over fs-alias-normalized paths — casefold + Unicode NFC/NFD). Any
overlap fails closed. There is therefore no content synthesis at the boundary;
the merged tree is exactly (base plus the candidate delta plus the parent delta) with no path
contested. This is whole-output only: no per-binding or sub-root apply (that is
the within-run proper subset gated on ``commit_prepared`` — see
``docs/engineering/convergence/p030-goals/keystone-commit-prepared.md``).

Authority (T1 D7): with ``decide=`` the D7 authority lane runs before publication —
signature parity with ``select_retained_output``, under the
``retained_output_application`` transaction kind (its own settlements, commit
outcomes, and PermissionPlan route; never a reuse of the selection spelling). A
denied/refused decision publishes no world and writes no receipt.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import pygit2

from vcs_core._authority_transactions import (
    begin_pending_authority_settlement,
    clear_pending_authority_transaction,
    update_pending_authority_settlement,
)
from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._retained_output_selection import (
    _candidate_selection,
    _prepare_and_decide_retained_selection_authority,
    _reachable_parent_world_oids,
    _recover_published_retained_selection,
    _required_transition_str,
    _retained_output_authority_pending,
    _selection_admission,
    _settlement_operation_id,
    _snapshot_head_or_none,
    record_retained_output_authority_final_settlement,
)
from vcs_core._retained_output_settlement import (
    read_retained_output_settlement,
    retained_output_settlement_ref,
    write_retained_output_settlement,
)
from vcs_core._substrate_driver import DriverIngressResult, ObservationDraft, TransitionDraft
from vcs_core._transition_kernel_records import PayloadDescriptorClaim
from vcs_core._vcscore_seal import (
    ValidatedRetainedWorkspace,
    _scope_selector,
    _validate_retained_workspace_handle,
)
from vcs_core._world_authority_finalizer import WorldAuthorityFinalizer
from vcs_core._world_operation_builder import CandidateSelection, OperationFinalBuilder
from vcs_core._world_substrate_adapters import WorkspaceSubstrateDriver, workspace_state_revision_payload
from vcs_core._world_types import WORLD_TRANSITION_SCHEMA, WorldSnapshot
from vcs_core.git_store import diff_workspace_trees
from vcs_core.types import (
    RetainedOutputSettlement,
    RetainedOutputSettlementResult,
    RetainedWorkspaceHandle,
    ScopeInfo,
)

if TYPE_CHECKING:
    from vcs_core._world_storage_manager import WorldStorageManager
    from vcs_core.vcscore import VcsCore

_APPLICATION_SEMANTIC_OP = "retained-output-application"


def apply_retained_output(
    owner: VcsCore,
    scope_or_handle: ScopeInfo | RetainedWorkspaceHandle | str,
    *,
    parent: ScopeInfo,
    binding: str = "workspace",
    decide: Any = None,
    authority_operation_id: str | None = None,
    effective_match_digest: str | None = None,
    authority_surface_plan_digest: str | None = None,
    permission_plan_digest: str | None = None,
    permission_plan_descriptor: Any = None,
    authority_context: Any = None,
) -> RetainedOutputSettlementResult:
    """Apply one retained binding output onto its (possibly advanced) parent binding.

    Fast-forward-degenerate (parent unmoved) reduces to the same post-state and
    head as ``select`` but records an ``applied`` settlement. Non-degenerate
    three-way-merges the disjoint delta and publishes an application world.

    With ``decide=`` the D7 authority lane runs before publication (signature parity
    with ``select_retained_output``): a denied/refused decision publishes no world and
    writes no receipt; an allowed decision records pending + final authority settlement
    evidence under the ``retained_output_application`` transaction kind.
    """
    with owner._lock:
        retained = owner._seal.validated_retained_workspace(_scope_selector(scope_or_handle))
        if isinstance(scope_or_handle, RetainedWorkspaceHandle):
            _validate_retained_workspace_handle(scope_or_handle, retained)
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

        operation_id = _apply_operation_id(handoff.handoff_ref, settlement_ref)
        manager = owner._world_storage()

        recovered = _recover_published_application(
            owner, manager, retained=retained, parent=parent, settlement_ref=settlement_ref
        )
        if recovered is not None:
            return recovered

        # Probe-uniformity (T1 task-10 tranche, S2 disposition): every settlement verb runs the
        # OTHER verbs' published-world recovery probes at entry — a published-but-unreceipted
        # selection is completed (its receipt written) and this apply refuses as already-settled,
        # instead of surfacing a misleading D2/drift error. The per-verb probe pair is the future
        # settlement-action registry's recovery column (g10).
        foreign_selection = _recover_published_retained_selection(
            owner,
            manager,
            retained=retained,
            parent=parent,
            operation_id=_settlement_operation_id(handoff=handoff, settlement_ref=settlement_ref),
            settlement_ref=settlement_ref,
        )
        if foreign_selection is not None:
            raise InvalidRepositoryStateError(f"retained output is already settled: {settlement_ref}")

        _selection_admission(owner).require_retained_output_selection_allowed(scope_selector=parent.ref)
        parent_world_oid = owner._current_v2_world_oid(manager, parent.ref)
        if parent_world_oid is None:
            raise InvalidRepositoryStateError(
                f"Cannot apply retained output {handoff.scope_name!r}: parent has no current v2 world"
            )
        parent_world = manager.read_world(parent_world_oid)
        basis_world = manager.read_world(handoff.parent_basis_world_oid)
        basis_head = _snapshot_head_or_none(basis_world, handoff.binding)
        current_head = _snapshot_head_or_none(parent_world, handoff.binding)
        if basis_head is None or current_head is None:
            raise InvalidRepositoryStateError(
                f"Cannot apply retained output {handoff.scope_name!r}: parent binding has no head to merge onto"
            )
        cand_head = retained.head

        substrate, basis_tree = _tree_oid_for(manager, basis_head)
        _, current_tree = _tree_oid_for(manager, current_head)
        _, cand_tree = _tree_oid_for(manager, cand_head)
        git_repo = substrate.repo

        degenerate = current_head.head == basis_head.head
        merged_tree: pygit2.Oid | None = None
        if not degenerate:
            _assert_apply_disjoint(git_repo, basis_tree, current_tree, cand_tree, scope_name=handoff.scope_name)
            merged_tree = _three_way_merge(git_repo, basis_tree, current_tree, cand_tree)

        # The D7 authority lane: classify + decide over the candidate delta, fail closed on
        # denied/refused (no world, no receipt), pending-record the allowed decision.
        application_authority = _prepare_and_decide_retained_selection_authority(
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
            transaction_kind="retained_output_application",
        )
        pending_authority = None
        if application_authority is not None:
            pending_authority = begin_pending_authority_settlement(
                owner,
                _retained_output_authority_pending(
                    retained=retained,
                    parent=parent,
                    context=application_authority,
                    outcome="allowed",
                    settlement="applied",
                    commit_outcome="pending",
                    reason_code="pending_retained_output_application",
                ),
            )

        outcome_world_oid, applied_head = _publish_application_world(
            manager,
            parent=parent,
            parent_world=parent_world,
            parent_world_oid=parent_world_oid,
            handoff=handoff,
            operation_id=operation_id,
            degenerate=degenerate,
            retained=retained,
            substrate=substrate,
            git_repo=git_repo,
            merged_tree=merged_tree,
            current_head=current_head,
            cand_head=cand_head,
        )
        result = _write_application_settlement(
            owner,
            manager,
            retained=retained,
            parent=parent,
            operation_id=operation_id,
            parent_world_before=parent_world_oid,
            parent_world_after=outcome_world_oid,
            applied_head=applied_head,
            settlement_ref=settlement_ref,
            authority_operation_id=(
                None if application_authority is None else application_authority.authority_operation_id
            ),
            authority_settlement_operation_id=(
                None if application_authority is None else application_authority.settlement_operation_id
            ),
            authority_outcome=None if application_authority is None else "allowed",
        )
        if application_authority is not None:
            assert pending_authority is not None
            update_pending_authority_settlement(
                owner,
                pending_authority,
                phase="adopted",
                commit_outcome="applied",
                reason_code="applied_after_allowed_decision",
            )
            record_retained_output_authority_final_settlement(
                owner,
                parent=parent,
                settlement_operation_id=application_authority.settlement_operation_id,
                authority_operation_id=application_authority.authority_operation_id,
                application_operation_id=application_authority.prepared.selection_operation_id,
                cohort_id=application_authority.prepared.cohort_id,
                candidate_digest=application_authority.prepared.candidate_digest,
                outcome="allowed",
                settlement="applied",
                commit_outcome="applied",
                decision_ids=tuple(decision.decision_id for decision in application_authority.decisions),
                reason_code="applied_after_allowed_decision",
                permission_plan_digest=application_authority.permission_plan_digest,
                permission_plan_descriptor=application_authority.permission_plan_descriptor,
                authority_context=application_authority.authority_context,
            )
            clear_pending_authority_transaction(owner, application_authority.settlement_operation_id)
            result = replace(
                result,
                authority_operation_id=application_authority.authority_operation_id,
                authority_settlement_operation_id=application_authority.settlement_operation_id,
                authority_outcome="allowed",
            )
        return result


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------


def _publish_application_world(
    manager: WorldStorageManager,
    *,
    parent: ScopeInfo,
    parent_world: Any,
    parent_world_oid: str,
    handoff: Any,
    operation_id: str,
    degenerate: bool,
    retained: ValidatedRetainedWorkspace,
    substrate: Any,
    git_repo: pygit2.Repository,
    merged_tree: pygit2.Oid | None,
    current_head: Any,
    cand_head: Any,
) -> tuple[str, str]:
    # child-produced selection (degenerate FF) names the retained output world as
    # a parent; a freshly-minted new-candidate (three-way) needs only the parent world.
    parent_worlds: tuple[str, ...] = (parent_world_oid, handoff.output_world_oid) if degenerate else (parent_world_oid,)

    def prepared_factory(current_operation_id: str) -> Any:
        transition: dict[str, object] = {
            "schema": WORLD_TRANSITION_SCHEMA,
            "operation_id": current_operation_id,
            "parent_worlds": list(parent_worlds),
            "input_world": parent_world_oid,
            "semantic_op": _APPLICATION_SEMANTIC_OP,
            "handoff_ref": handoff.handoff_ref,
            "parent_basis_world_oid": handoff.parent_basis_world_oid,
        }
        if degenerate:
            published_head = cand_head
            plan = manager.plan_candidate_selection(
                operation_id=current_operation_id,
                selection=_candidate_selection(retained),
                selection_kind="child-produced",
                producer_operation_id=handoff.producer_operation_id,
                producer_world_oid=handoff.output_world_oid,
                role=cand_head.role,
            )
        else:
            assert merged_tree is not None
            # Mint the merged-tree candidate INSIDE the factory, keyed to the
            # attempt id, so a publish retry re-mints under the retry op id.
            bundle = _mint_application_candidate(
                manager,
                substrate=substrate,
                git_repo=git_repo,
                merged_tree=merged_tree,
                binding=handoff.binding,
                current_head=current_head,
                operation_id=current_operation_id,
            )
            published_head = _head_at(current_head, bundle.candidate.head)
            plan = manager.plan_candidate_selection(
                operation_id=current_operation_id,
                selection=CandidateSelection.from_bundle(bundle),
                selection_kind="new-candidate",
                producer_operation_id=current_operation_id,
                role=published_head.role,
            )
        heads_by_binding = dict(parent_world.snapshot.by_binding())
        heads_by_binding[handoff.binding] = published_head
        builder = OperationFinalBuilder(current_operation_id).select_candidate_plan(plan=plan)
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
        return builder.build_prepared(
            operation_kind=_APPLICATION_SEMANTIC_OP,
            target_ref=parent.ref,
            input_world_oid=parent_world_oid,
            snapshot=WorldSnapshot.from_heads(heads_by_binding),
            transition=transition,
            parents=parent_worlds,
        )

    outcome = WorldAuthorityFinalizer(manager).publish_or_recover(
        operation_id=operation_id,
        prepared_factory=prepared_factory,
        target_ref=parent.ref,
        expected_input_world_oid=parent_world_oid,
    )
    if outcome.world_oid is None:
        raise InvalidRepositoryStateError(f"retained output application {operation_id!r} did not publish a world")
    published = manager.read_world(outcome.world_oid).snapshot.by_binding().get(handoff.binding)
    if published is None:
        raise InvalidRepositoryStateError("retained output application world has no head for the settled binding")
    _validate_retained_application_world(
        manager,
        retained=retained,
        operation_id=outcome.operation_id,
        parent_world_before=parent_world_oid,
        parent_world_after=outcome.world_oid,
        applied_head=published.head,
    )
    return outcome.world_oid, published.head


def _mint_application_candidate(
    manager: WorldStorageManager,
    *,
    substrate: Any,
    git_repo: pygit2.Repository,
    merged_tree: pygit2.Oid,
    binding: str,
    current_head: Any,
    operation_id: str,
) -> Any:
    """Mint a tree-backed candidate for the merged tree under the application op.

    The store validates a tree-backed candidate against a workspace-state manifest
    covering every blob in the tree; the merged manifest is synthesised by walking
    the merged tree (path-disjoint by construction under D2).
    """
    payload = workspace_state_revision_payload(
        tuple(_merged_manifest_entries(git_repo, git_repo[merged_tree])),
        byte_authority="tree-backed",
    )
    driver = WorkspaceSubstrateDriver()
    identity = substrate.identity
    payload_digest = PayloadDescriptorClaim.for_json_payload(payload).payload_digest
    observation = ObservationDraft(
        observation_id="payload",
        evidence_kind=f"command:{_APPLICATION_SEMANTIC_OP}",
        stable_observation={
            "binding": binding,
            "store_id": identity.store_id,
            "resource_id": identity.resource_id,
            "substrate_kind": identity.kind,
            "semantic_op": _APPLICATION_SEMANTIC_OP,
            "parent_heads": [current_head.head],
            "payload_digest": payload_digest,
        },
        mechanism=driver.driver_id,
    )
    transition = TransitionDraft(
        transition_id="primary",
        semantic_op=_APPLICATION_SEMANTIC_OP,
        payload=payload,
        observation_ids=(observation.observation_id,),
        base_heads=(current_head.head,),
        payload_descriptor_claim=PayloadDescriptorClaim.for_json_payload(payload),
        materialization_class="external",
        git_tree_oid=str(merged_tree),
    )
    ingress = DriverIngressResult(observations=(observation,), transitions=(transition,))
    return manager.create_prepared_driver_candidate_bundle(
        identity.store_id,
        operation_id=operation_id,
        binding=binding,
        result=ingress,
        driver_id=driver.driver_id,
        driver_version=driver.driver_version,
        parents=(current_head.head,),
    )


# ---------------------------------------------------------------------------
# Validation / recovery
# ---------------------------------------------------------------------------


def _validate_retained_application_world(
    manager: WorldStorageManager,
    *,
    retained: ValidatedRetainedWorkspace,
    operation_id: str,
    parent_world_before: str,
    parent_world_after: str,
    applied_head: str,
) -> None:
    handoff = retained.loaded.handoff
    world = manager.read_world(parent_world_after)
    transition = world.transition
    if transition.get("operation_id") != operation_id:
        raise InvalidRepositoryStateError("retained output application world operation_id disagrees with settlement")
    if transition.get("semantic_op") != _APPLICATION_SEMANTIC_OP:
        raise InvalidRepositoryStateError("retained output application world has unexpected semantic operation")
    if transition.get("handoff_ref") != handoff.handoff_ref:
        raise InvalidRepositoryStateError("retained output application world handoff_ref disagrees with settlement")
    if transition.get("input_world") != parent_world_before:
        raise InvalidRepositoryStateError("retained output application world input_world disagrees with settlement")
    if parent_world_before not in world.parent_oids:
        raise InvalidRepositoryStateError("retained output application world does not name parent input world")
    published = world.snapshot.by_binding().get(handoff.binding)
    if published is None or published.head != applied_head:
        raise InvalidRepositoryStateError("retained output application world head disagrees with applied head")


def _recover_published_application(
    owner: VcsCore,
    manager: WorldStorageManager,
    *,
    retained: ValidatedRetainedWorkspace,
    parent: ScopeInfo,
    settlement_ref: str,
) -> RetainedOutputSettlementResult | None:
    """If an interrupted apply already published its world, re-derive + write the receipt."""
    current_world_oid = owner._current_v2_world_oid(manager, parent.ref)
    if current_world_oid is None:
        return None
    handoff = retained.loaded.handoff
    for world_oid in _reachable_parent_world_oids(manager, current_world_oid):
        world = manager.read_world(world_oid)
        if world.transition.get("semantic_op") != _APPLICATION_SEMANTIC_OP:
            continue
        if world.transition.get("handoff_ref") != handoff.handoff_ref:
            continue
        operation_id = _required_transition_str(world.transition, "operation_id")
        parent_world_before = _required_transition_str(world.transition, "input_world")
        published = world.snapshot.by_binding().get(handoff.binding)
        if published is None:
            continue
        return _write_application_settlement(
            owner,
            manager,
            retained=retained,
            parent=parent,
            operation_id=operation_id,
            parent_world_before=parent_world_before,
            parent_world_after=world_oid,
            applied_head=published.head,
            settlement_ref=settlement_ref,
        )
    return None


def _write_application_settlement(
    owner: VcsCore,
    manager: WorldStorageManager,
    *,
    retained: ValidatedRetainedWorkspace,
    parent: ScopeInfo,
    operation_id: str,
    parent_world_before: str,
    parent_world_after: str,
    applied_head: str,
    settlement_ref: str,
    authority_operation_id: str | None = None,
    authority_settlement_operation_id: str | None = None,
    authority_outcome: str | None = None,
) -> RetainedOutputSettlementResult:
    handoff = retained.loaded.handoff
    _validate_retained_application_world(
        manager,
        retained=retained,
        operation_id=operation_id,
        parent_world_before=parent_world_before,
        parent_world_after=parent_world_after,
        applied_head=applied_head,
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
        action="applied",
        operation_id=operation_id,
        parent_world_before=parent_world_before,
        parent_world_after=parent_world_after,
        settlement_ref=settlement_ref,
        applied_head=applied_head,
        authority_operation_id=authority_operation_id,
        authority_settlement_operation_id=authority_settlement_operation_id,
        authority_outcome=authority_outcome,
    )
    write_retained_output_settlement(owner.store, settlement)
    return RetainedOutputSettlementResult(
        scope=retained.entry_scope,
        parent=parent,
        output_world_oid=handoff.output_world_oid,
        parent_world_before=parent_world_before,
        parent_world_after=parent_world_after,
        settlement=settlement,
    )


# ---------------------------------------------------------------------------
# Conflict semantics (T1 D2) + tree helpers
# ---------------------------------------------------------------------------


def _normalize_alias(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _assert_apply_disjoint(
    git_repo: pygit2.Repository,
    basis_tree: pygit2.Oid,
    current_tree: pygit2.Oid,
    cand_tree: pygit2.Oid,
    *,
    scope_name: str,
) -> None:
    """Refuse unless candidate and parent deltas are equal-or-prefix-or-alias disjoint.

    Compared over fs-alias-normalized paths (casefold + NFC) — a policy, never a
    host probe, so it refuses identically on case-sensitive and case-insensitive
    volumes (see spike ``260706-apply-three-way-settlement`` S6).
    """
    cand_paths = [c.path for c in diff_workspace_trees(git_repo, basis_tree, cand_tree)]
    current_paths = [c.path for c in diff_workspace_trees(git_repo, basis_tree, current_tree)]
    offenders: list[tuple[str, str]] = []
    for cp in cand_paths:
        ncp = _normalize_alias(cp)
        for pp in current_paths:
            npp = _normalize_alias(pp)
            if ncp == npp or ncp.startswith(npp + "/") or npp.startswith(ncp + "/"):
                offenders.append((cp, pp))
    if offenders:
        pairs = ", ".join(f"{a!r}~{b!r}" for a, b in offenders[:8])
        raise InvalidRepositoryStateError(
            f"Cannot apply retained output {scope_name!r}: candidate and parent changes overlap "
            f"(equal/prefix/alias) at {pairs}. Use release/discard, or re-run against the current parent."
        )


def _three_way_merge(
    git_repo: pygit2.Repository,
    basis_tree: pygit2.Oid,
    current_tree: pygit2.Oid,
    cand_tree: pygit2.Oid,
) -> pygit2.Oid:
    # Rename detection OFF (MergeFlag(0)) — never content-weave (T1 D6); the D2
    # path check is the load-bearing invariant, the conflict index is belt-and-braces.
    index = git_repo.merge_trees(basis_tree, current_tree, cand_tree, flags=pygit2.enums.MergeFlag(0))
    if index.conflicts is not None:
        conflicting = sorted({(c[1].path if c[1] else (c[2].path if c[2] else "?")) for c in index.conflicts})
        raise InvalidRepositoryStateError(f"retained output application produced merge conflicts at {conflicting!r}")
    return index.write_tree(git_repo)


def _merged_manifest_entries(repo: pygit2.Repository, tree: Any, prefix: str = "") -> Any:
    for entry in tree:
        path = f"{prefix}{entry.name}"
        obj = repo[entry.id]
        if entry.type_str == "tree":
            yield from _merged_manifest_entries(repo, obj, prefix=f"{path}/")
        else:
            blob = cast("pygit2.Blob", obj)
            yield {
                "path": path,
                "state": "present",
                "mode": int(entry.filemode),
                "content_digest": "sha256:" + hashlib.sha256(blob.data).hexdigest(),
            }


def _tree_oid_for(manager: WorldStorageManager, head: Any) -> tuple[Any, pygit2.Oid]:
    substrate = manager.store(head.store_id)
    meta = substrate.read_revision_metadata(head.head)
    if meta.byte_authority != "tree-backed" or meta.git_tree_oid is None:
        raise InvalidRepositoryStateError(
            f"retained output application requires tree-backed revisions; got {meta.byte_authority!r}"
        )
    return substrate, pygit2.Oid(hex=meta.git_tree_oid)


def _head_at(template_head: Any, new_head_oid: str) -> Any:
    from dataclasses import replace as _replace

    return _replace(template_head, head=new_head_oid)


def _apply_operation_id(handoff_ref: str, settlement_ref: str) -> str:
    from vcs_core._world_types import canonical_digest

    digest = canonical_digest(
        {"kind": _APPLICATION_SEMANTIC_OP, "handoff_ref": handoff_ref, "settlement_ref": settlement_ref}
    )[:24]
    return f"apply_retained_{digest}"
