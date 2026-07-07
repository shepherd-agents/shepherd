from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal, cast

import pygit2

from vcs_core._authority import (
    AuthorityBindingRootsError,
    AuthorityCommitOutcome,
    AuthorityDecision,
    AuthorityMergeDriftError,
    AuthorityMergeResult,
    AuthorityOutcome,
    AuthoritySettlement,
    DecisionProvider,
    PendingAuthoritySettlement,
    PreparedAuthorityMerge,
    classify_gitrepo_authority_request,
    digest_effects,
    make_decision_record,
    normalize_authority_context,
    normalize_gitrepo_binding_roots,
    prepare_authority_merge,
    settlement_metadata,
)
from vcs_core._authority_inventory import (
    read_valid_authority_settlement_pending_records,
)
from vcs_core._authority_transactions import (
    begin_pending_authority_settlement,
    clear_pending_authority_transaction,
    ensure_authority_operation_ids_available,
    record_authority_settlement_effect,
    update_pending_authority_settlement,
)
from vcs_core._dirty_flag import check_dirty_flag
from vcs_core._errors import (
    InterruptedLifecycleError,
    InvalidRepositoryStateError,
    ParentWorkingTreeDivergedError,
    ScopeAdmissionError,
    StaleScopeError,
)
from vcs_core._fork_hints import ForkHints
from vcs_core._identity import read_ground_world_id
from vcs_core._lifecycle_progress import LifecycleProgress
from vcs_core._lifecycle_recovery import LifecycleRecovery, LifecycleRecoveryDependencies
from vcs_core._lifecycle_run import LifecycleRun, LifecycleScopeState
from vcs_core._lifecycle_state import LifecycleRunState
from vcs_core._lock import acquire_session_lock, release_session_lock
from vcs_core._parent_tree_manifest import capture_parent_tree_manifest, diff_parent_tree_manifest
from vcs_core._permission_plan_evidence import PermissionPlanEvidenceError, validate_permission_plan_evidence
from vcs_core._projection_store import ScopeRegistryEntry, ScopeRegistryStatus
from vcs_core._readiness_admission import (
    authority_settlement_recovery_targets,
    recovery_operation_targets_for_scope_refs,
    recovery_targets_for_kinds,
    recovery_targets_for_scope_refs,
    require_recovery_targets_allowed,
    workspace_authority_recovery_targets,
)
from vcs_core._sibling_group_blockers import (
    list_sibling_group_blockers as _list_sibling_group_blockers,
)
from vcs_core._sibling_group_blockers import (
    refresh_sibling_group_blockers,
    refresh_sibling_group_recovery_blockers,
)
from vcs_core._substrate_runtime import ContainmentSubstrate
from vcs_core._substrate_tree_read import read_substrate_workspace_file
from vcs_core._vcscore_admission import mutation_admission
from vcs_core._workspace_authority import (
    WorkspaceAuthorityPending,
    clear_pending_workspace_authority_for_scope,
    pending_workspace_authority_records,
)
from vcs_core.substrates import FilesystemSubstrate
from vcs_core.types import (
    EffectRecord,
    OperationSummary,
    ScopeInfo,
    SealResult,
    normalize_git_filemode,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence

    from vcs_core._mutation_admission import MutationAdmission
    from vcs_core._query_readiness import ReadinessOperationAuthority, RuntimeAdmissionContext
    from vcs_core._runtime_types import OperationRefInfo
    from vcs_core._vcscore_seal import PreparedSealHandoff
    from vcs_core.vcscore import VcsCore

logger = logging.getLogger(__name__)

# Durable operation-kind vocabulary for the authority lane. Named constants so a
# raw literal cannot silently diverge from the token it records (W1a).
AUTHORITY_MERGE_OPERATION_KIND = "skeleton.authority.merge"
AUTHORITY_SETTLEMENT_OPERATION_KIND = "skeleton.authority.settlement"


def _admission(owner: VcsCore) -> MutationAdmission:
    return mutation_admission(
        owner,
        sibling_group_blockers=lambda: refresh_sibling_group_recovery_blockers(owner),
    )


def _lifecycle_state(owner: VcsCore) -> LifecycleRunState:
    def set_current(run: LifecycleRun | None) -> None:
        owner._lifecycle_run = run

    return LifecycleRunState(
        repo_path=owner._repo_path,
        current=lambda: owner._lifecycle_run,
        set_current=set_current,
    )


def _lifecycle_progress(owner: VcsCore) -> LifecycleProgress:
    return LifecycleProgress(_lifecycle_state(owner))


def _lifecycle_recovery(owner: VcsCore) -> LifecycleRecovery:
    return LifecycleRecovery(
        LifecycleRecoveryDependencies(
            state=_lifecycle_state(owner),
            progress=_lifecycle_progress(owner),
            substrates=owner._lifecycle_substrates,
            scope_ref_exists=lambda scope: owner._store.ref_exists(scope.ref),
            load_context=lambda run: _load_lifecycle_context(owner, run),
            restore_substrate_state=lambda run, scope, parent: _restore_lifecycle_substrate_state(
                owner,
                run,
                scope=scope,
                parent=parent,
            ),
            snapshot_merge_effects=lambda scope, parent: _snapshot_merge_effects_locked(owner, scope, parent),
            snapshot_discard_effects=lambda scope, parent: _snapshot_discard_effects_locked(owner, scope, parent),
            complete_merge=lambda scope, parent: _complete_merge_locked(owner, scope, parent),
            complete_discard=lambda scope, parent: _complete_discard_locked(owner, scope, parent),
            complete_seal=lambda scope, parent: _complete_seal_locked(owner, scope, parent).handoff.scope_name,
        )
    )


def activate(
    owner: VcsCore,
    *,
    recover: str | None = None,
    recover_lifecycle: str | None = None,
    defer_orphan_detection: bool = False,
    auto_recover_orphaned_operations: bool = False,
) -> None:
    with owner._lock:
        if recover is not None and recover_lifecycle is not None:
            raise InvalidRepositoryStateError(
                "Cannot combine materialization recovery and lifecycle recovery in one activation. "
                "Run materialization recovery first, then lifecycle recovery if it is still required."
            )
        acquire_session_lock(owner._repo_path, owner._session_id)
        try:
            if recover is not None:
                owner.recover_materialization(mode=recover)
            else:
                check_dirty_flag(owner._repo_path)
            with owner._patch_manager.guard():
                if owner._store.is_empty:
                    if not owner._allow_activate_init:
                        raise InvalidRepositoryStateError(
                            f"{owner._repo_path} has no commits. Run `vcs-core init` first."
                        )
                    owner._store.create_root_commit()
                owner._ground_world_id = read_ground_world_id(owner._repo_path)
                owner._store.require_scope_registry_projection()
            for substrate in owner._lifecycle_substrates:
                cast("Any", substrate).activate()
            owner._ground = owner._make_ground_scope()
            owner._patch_manager.install_substrates(owner._lifecycle_substrates)

            lifecycle_run = _lifecycle_state(owner).current_or_read()
            if lifecycle_run is not None:
                pending = _authority_pending_for_lifecycle_run(
                    read_valid_authority_settlement_pending_records(owner._repo_path),
                    lifecycle_run,
                )
                if pending is not None:
                    if recover_lifecycle is not None:
                        raise InvalidRepositoryStateError(
                            "Cannot recover a owned lifecycle through generic lifecycle activation. "
                            "Activate without recover_lifecycle and run recover_authority_settlements()."
                        )
                    logger.warning(
                        "authority lifecycle recovery is pending for scope %r. "
                        "Run recover_authority_settlements() before mutating work.",
                        lifecycle_run.scope.name,
                    )
                elif recover_lifecycle is None:
                    raise InterruptedLifecycleError(
                        operation=lifecycle_run.operation,
                        scope_name=lifecycle_run.scope.name,
                        phase=lifecycle_run.phase,
                    )
                else:
                    _recover_lifecycle_locked(owner, mode=recover_lifecycle)

            owner._orphaned_operations = owner._store.list_open_operations()
            if owner._orphaned_operations:
                logger.warning(
                    "Orphaned operation refs detected from a prior session: %s. "
                    "Call archive_orphaned_operations() to clean up.",
                    ", ".join(owner._format_operation_label(op) for op in owner._orphaned_operations),
                )

            owner._scope_registry_mismatches = list(owner._store.scope_registry_projection_mismatches())
            refresh_sibling_group_blockers(owner)
            if owner._sibling_group_blockers:
                logger.warning(
                    "Sibling-group recovery blockers detected: %s. "
                    "Resume, cancel, archive, or complete these groups before mutating.",
                    ", ".join(owner._sibling_group_blockers),
                )
            if auto_recover_orphaned_operations and owner._orphaned_operations:
                _auto_recover_orphaned_operations(owner)
            if not defer_orphan_detection:
                owner._orphaned_refs = _orphaned_scope_refs_from_registry(owner)
                if owner._orphaned_refs:
                    names = [ref.rsplit("/", 1)[-1] for ref in owner._orphaned_refs]
                    logger.warning(
                        "Orphaned scope refs detected from a prior session: %s. "
                        "Call archive_orphaned_scopes() to clean up, or merge/discard them.",
                        ", ".join(names),
                    )
            if owner._scope_registry_mismatches:
                logger.warning(
                    "Scope registry mismatches detected: %s.",
                    ", ".join(
                        mismatch.scope_name or mismatch.ref.rsplit("/", 1)[-1]
                        for mismatch in owner._scope_registry_mismatches
                    ),
                )
        except Exception:
            owner._patch_manager.uninstall_all()
            for substrate in reversed(owner._lifecycle_substrates):
                try:
                    cast("Any", substrate).deactivate()
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Substrate %s raised during activate cleanup; continuing cleanup",
                        getattr(substrate, "name", substrate),
                        exc_info=True,
                    )
            with owner._patch_manager.guard():
                release_session_lock(owner._repo_path, owner._session_id)
            owner._ground = None
            owner._ground_world_id = None
            owner._pipeline.reset()
            owner._active_scopes.clear()
            owner._scope_parents.clear()
            owner._isolated_scopes.clear()
            owner._restored_scopes.clear()
            owner._carrier_scopes.clear()
            owner._claim_registry.clear()
            owner._pending_workspace_driver_effects.clear()
            owner._parent_tree_manifests.clear()
            owner._orphaned_refs.clear()
            owner._scope_registry_mismatches.clear()
            owner._orphaned_operations.clear()
            owner._sibling_group_blockers.clear()
            owner._lifecycle_run = None
            raise


def deactivate(owner: VcsCore, *, warn_on_open_scopes: bool = True) -> None:
    with owner._lock:
        if warn_on_open_scopes and owner._active_scopes:
            logger.warning(
                "Deactivating with %d open scope(s): %s. Their refs will persist as orphans in the bare repo.",
                len(owner._active_scopes),
                ", ".join(owner._active_scopes),
            )

        owner._patch_manager.uninstall_all()
        for substrate in reversed(owner._lifecycle_substrates):
            try:
                cast("Any", substrate).deactivate()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Substrate %s raised during deactivate; continuing cleanup",
                    getattr(substrate, "name", substrate),
                    exc_info=True,
                )

        with owner._patch_manager.guard():
            release_session_lock(owner._repo_path, owner._session_id)
        owner._ground = None
        owner._ground_world_id = None
        owner._pipeline.reset()
        owner._active_scopes.clear()
        owner._scope_parents.clear()
        owner._isolated_scopes.clear()
        owner._restored_scopes.clear()
        owner._carrier_scopes.clear()
        owner._claim_registry.clear()
        owner._pending_workspace_driver_effects.clear()
        owner._parent_tree_manifests.clear()
        owner._orphaned_refs.clear()
        owner._scope_registry_mismatches.clear()
        owner._orphaned_operations.clear()
        owner._sibling_group_blockers.clear()
        owner._lifecycle_run = None


def recover_lifecycle(owner: VcsCore, mode: str = "resume") -> str | None:
    callback_name: str | None = None
    callback_kind: str | None = None
    with owner._lock:
        if owner._ground is None:
            raise RuntimeError("VcsCore not activated. Call activate() first.")
        lifecycle_run = _lifecycle_state(owner).current_or_read()
        if lifecycle_run is None:
            return None
        if _authority_pending_for_lifecycle_run(
            read_valid_authority_settlement_pending_records(owner._repo_path),
            lifecycle_run,
        ):
            raise InvalidRepositoryStateError(
                "Cannot recover lifecycle directly while it is owned by a pending authority settlement. "
                "Run recover_authority_settlements() instead."
            )
        callback_kind, callback_name = _recover_lifecycle_locked(owner, mode=mode)

    if callback_kind == "merge" and callback_name is not None:
        _run_merge_callbacks(owner, callback_name)
    elif callback_kind == "discard" and callback_name is not None:
        _run_discard_callbacks(owner, callback_name)
    return callback_name


def _authority_pending_for_lifecycle_run(
    pending_records: Sequence[PendingAuthoritySettlement],
    lifecycle_run: LifecycleRun,
) -> PendingAuthoritySettlement | None:
    for pending in pending_records:
        if pending.transaction_kind != "filesystem_merge":
            continue
        if pending.scope_ref != lifecycle_run.scope.ref or pending.parent_scope_ref != lifecycle_run.parent.ref:
            continue
        if lifecycle_run.operation == "merge" and pending.settlement == "merged" and pending.outcome == "allowed":
            return pending
        if (
            lifecycle_run.operation == "discard"
            and pending.settlement == "discarded"
            and pending.outcome in {"denied", "refused"}
        ):
            return pending
    return None


def _persist_lifecycle_run(owner: VcsCore, run: LifecycleRun) -> None:
    _lifecycle_state(owner).persist(run)


def _update_lifecycle_run(
    owner: VcsCore,
    *,
    phase: str | None = None,
    prepared_effect_counts: tuple[tuple[str, int], ...] | None = None,
    prepared_substrates: tuple[str, ...] | None = None,
    completed_substrates: tuple[str, ...] | None = None,
) -> LifecycleRun:
    return _lifecycle_state(owner).update(
        phase=phase,
        prepared_effect_counts=prepared_effect_counts,
        prepared_substrates=prepared_substrates,
        completed_substrates=completed_substrates,
    )


def _clear_lifecycle_run(owner: VcsCore) -> None:
    _lifecycle_state(owner).clear()


def _ensure_no_interrupted_lifecycle(owner: VcsCore, attempted: str) -> None:
    _admission(owner).require_no_interrupted_lifecycle(attempted)


def _ensure_no_open_operation(owner: VcsCore, attempted: str) -> None:
    _admission(owner).require_no_open_operation(attempted)


def _ensure_runtime_mutation_allowed(
    owner: VcsCore,
    attempted: str,
    *,
    authorized_operations: tuple[ReadinessOperationAuthority, ...] = (),
    scope_selector: str | None = None,
    runtime_admission_context: RuntimeAdmissionContext | None = None,
) -> None:
    _admission(owner).require_runtime_mutation_allowed(
        attempted,
        authorized_operations=authorized_operations,
        scope_selector=scope_selector,
        runtime_admission_context=runtime_admission_context,
    )


def list_sibling_group_blockers(owner: VcsCore) -> tuple[str, ...]:
    return _list_sibling_group_blockers(owner)


def _scope_state(owner: VcsCore, scope: ScopeInfo) -> LifecycleScopeState:
    return LifecycleScopeState(
        name=scope.name,
        ref=scope.ref,
        instance_id=scope.instance_id,
        creation_oid=scope.creation_oid,
        world_id=owner._scope_world_id(scope),
        isolated=scope.name in owner._isolated_scopes,
    )


def _orphaned_scope_refs_from_registry(owner: VcsCore) -> list[str]:
    from vcs_core._recovery_inventory import orphaned_scope_refs_from_store

    return orphaned_scope_refs_from_store(
        owner._store,
        owner._repo_path,
        mismatches=tuple(owner._scope_registry_mismatches),
    )


def _v2_scope_authority_refs(owner: VcsCore) -> tuple[str, ...]:
    from vcs_core._world_storage_installation import default_world_storage_exists, open_existing_default_world_storage

    if not default_world_storage_exists(owner._repo_path):
        return ()
    manager = open_existing_default_world_storage(owner._repo_path)
    return tuple(sorted(ref for ref in manager.world_store.repo.references if ref.startswith("refs/vcscore/scopes/")))


def _publish_scope_registry_entries_locked(
    owner: VcsCore,
    entries: dict[str, ScopeRegistryEntry],
    *,
    expected_head_oid: str | None = None,
) -> None:
    if expected_head_oid is None:
        expected_head_oid = owner._store.require_scope_registry_projection().head_oid
    published = owner._store.publish_scope_registry_projection(
        entries=tuple(sorted(entries.values(), key=lambda entry: entry.ref)),
        expected_head_oid=expected_head_oid,
    )
    if not published:
        raise InvalidRepositoryStateError(
            "Failed to publish the scope-registry projection against the current live-scope frontier."
        )
    owner._scope_registry_mismatches = list(owner._store.scope_registry_projection_mismatches())


def _publish_scope_registry_fork_locked(
    owner: VcsCore,
    *,
    scope: ScopeInfo,
    parent: ScopeInfo,
    isolated: bool,
    expected_head_oid: str | None,
) -> None:
    snapshot = owner._store.require_scope_registry_projection()
    entries = {entry.ref: entry for entry in snapshot.entries}
    entries[scope.ref] = ScopeRegistryEntry(
        name=scope.name,
        ref=scope.ref,
        instance_id=scope.instance_id,
        creation_oid=scope.creation_oid,
        parent_ref=parent.ref,
        world_id=owner._scope_world_id(scope),
        isolation_mode="isolated" if isolated else "shared",
        status="live",
    )
    published = owner._store.publish_scope_registry_projection(
        entries=tuple(sorted(entries.values(), key=lambda entry: entry.ref)),
        expected_head_oid=expected_head_oid,
    )
    if not published:
        raise InvalidRepositoryStateError(
            "Failed to publish the scope-registry projection against the current live-scope frontier."
        )
    owner._scope_registry_mismatches = list(owner._store.scope_registry_projection_mismatches())


def _publish_scope_registry_status_locked(
    owner: VcsCore,
    *,
    scope: ScopeInfo,
    status: ScopeRegistryStatus,
    parent: ScopeInfo | None = None,
) -> None:
    snapshot = owner._store.require_scope_registry_projection()
    entries = {entry.ref: entry for entry in snapshot.entries}
    existing = entries.get(scope.ref)
    if existing is not None and _scope_registry_status_converged(
        existing,
        scope=scope,
        parent=parent,
        status=status,
    ):
        owner._scope_registry_mismatches = list(owner._store.scope_registry_projection_mismatches())
        return
    if existing is None:
        if parent is None or scope.world_id is None:
            return
        existing = ScopeRegistryEntry(
            name=scope.name,
            ref=scope.ref,
            instance_id=scope.instance_id,
            creation_oid=scope.creation_oid,
            parent_ref=parent.ref,
            world_id=scope.world_id,
            isolation_mode="isolated" if scope.name in owner._isolated_scopes else "shared",
            status="live",
        )
    entries[scope.ref] = replace(existing, status=status)
    expected_head_oid = snapshot.head_oid
    if owner._lifecycle_run is not None and owner._lifecycle_run.scope_registry_head_oid is not None:
        expected_head_oid = owner._lifecycle_run.scope_registry_head_oid
    _publish_scope_registry_entries_locked(owner, entries, expected_head_oid=expected_head_oid)


def _scope_registry_status_converged(
    entry: ScopeRegistryEntry,
    *,
    scope: ScopeInfo,
    parent: ScopeInfo | None,
    status: ScopeRegistryStatus,
) -> bool:
    if entry.status != status:
        return False
    if (
        entry.name != scope.name
        or entry.ref != scope.ref
        or entry.instance_id != scope.instance_id
        or entry.creation_oid != scope.creation_oid
    ):
        return False
    if scope.world_id is not None and entry.world_id != scope.world_id:
        return False
    return not (parent is not None and entry.parent_ref != parent.ref)


def _begin_lifecycle_run(owner: VcsCore, *, operation: str, phase: str, scope: ScopeInfo, parent: ScopeInfo) -> None:
    snapshot = owner._store.require_scope_registry_projection()
    _persist_lifecycle_run(
        owner,
        LifecycleRun(
            session_id=owner._session_id,
            operation=operation,
            phase=phase,
            scope=_scope_state(owner, scope),
            parent=_scope_state(owner, parent),
            scope_registry_head_oid=snapshot.head_oid,
            active_ancestors=owner._active_ancestor_states(parent),
        ),
    )


def _scope_tip_matches(owner: VcsCore, scope: ScopeInfo, effect_type: str, **expected: str) -> bool:
    if not owner._store.ref_exists(scope.ref):
        return False
    tip = owner._store.log(ref=scope.ref, max_count=1)
    if not tip:
        return False
    metadata = tip[0].metadata
    if metadata.get("type") != effect_type:
        return False
    return all(metadata.get(key) == value for key, value in expected.items())


def _set_runtime_context(owner: VcsCore, scope: ScopeInfo | None) -> None:
    if scope is None:
        owner._pipeline.clear_execution_context()
        return
    owner._pipeline.set_execution_context(scope, session_id=owner._session_id)


def _post_lifecycle_context(owner: VcsCore, parent: ScopeInfo) -> ScopeInfo | None:
    if owner._ground is not None and parent.ref == owner._ground.ref:
        return None
    return parent


def _ensure_scope_merge_effect(owner: VcsCore, scope: ScopeInfo, parent: ScopeInfo) -> None:
    if _scope_tip_matches(owner, scope, "ScopeMerge", merged_into=parent.name):
        return
    owner._pipeline.record_one(
        EffectRecord(
            effect_type="ScopeMerge",
            metadata={
                "merged_into": parent.name,
                "parent_world_id": owner._scope_world_id(parent),
            },
        ),
        substrate="vcscore",
        scope=scope,
    )


def _ensure_discard_snapshot_effect(owner: VcsCore, scope: ScopeInfo, parent: ScopeInfo) -> None:
    if _scope_tip_matches(owner, scope, "DiscardSnapshot", discarded_scope=scope.name):
        return
    owner._pipeline.record_one(
        EffectRecord(
            effect_type="DiscardSnapshot",
            metadata={
                "discarded_scope": scope.name,
                "parent_world_id": owner._scope_world_id(parent),
            },
        ),
        substrate="vcscore",
        scope=scope,
    )


def _finish_scope_removal(owner: VcsCore, scope: ScopeInfo, parent: ScopeInfo) -> None:
    owner._scope_parents.pop(scope.name, None)
    owner._active_scopes.pop(scope.name, None)
    owner._drop_scope_runtime_state(scope.name)
    current = owner._pipeline.context.world
    if current is not None and current.ref == scope.ref and current.instance_id == scope.instance_id:
        _set_runtime_context(owner, _post_lifecycle_context(owner, parent))


def _mark_completed_substrate(owner: VcsCore, substrate_name: str) -> None:
    _lifecycle_progress(owner).mark_completed_substrate(substrate_name)


def _mark_prepared_substrate(owner: VcsCore, substrate_name: str) -> None:
    _lifecycle_progress(owner).mark_prepared_substrate(substrate_name)


def _prepared_effect_count(owner: VcsCore, substrate_name: str) -> int:
    return _lifecycle_progress(owner).prepared_effect_count(substrate_name)


def _mark_prepared_effect_count(owner: VcsCore, substrate_name: str, count: int) -> None:
    _lifecycle_progress(owner).mark_prepared_effect_count(substrate_name, count)


def _prepared_effect_matches_scope_commit(
    owner: VcsCore,
    scope: ScopeInfo,
    *,
    substrate_name: str,
    effect: EffectRecord,
    commit: pygit2.Commit,
) -> bool:
    metadata = owner._store._commit_info(commit).metadata
    if metadata.get("type") != effect.effect_type:
        return False
    if metadata.get("substrate") != substrate_name:
        return False
    if metadata.get("scope") != scope.name:
        return False
    expected = {
        **effect.metadata,
        "world_id": owner._scope_world_id(scope),
        "scope_instance_id": scope.instance_id,
        "type": effect.effect_type,
        "substrate": substrate_name,
        "scope": scope.name,
    }
    observed = {key: value for key, value in metadata.items() if key != "timestamp"}
    return observed == expected


def _recover_prepared_effect_count_from_scope_tip(
    owner: VcsCore,
    scope: ScopeInfo,
    *,
    substrate_name: str,
    effects: Sequence[EffectRecord],
) -> int:
    if not effects or not owner._store.ref_exists(scope.ref):
        return 0
    tip = owner._store._repo.revparse_single(scope.ref)
    if not isinstance(tip, pygit2.Commit):
        return 0
    history = list(owner._store._repo.walk(tip.id, cast("Any", pygit2.GIT_SORT_TOPOLOGICAL)))[: len(effects)]
    for candidate_count in range(len(history), 0, -1):
        recorded_history = history[:candidate_count]
        expected_prefix = effects[:candidate_count]
        if all(
            _prepared_effect_matches_scope_commit(
                owner,
                scope,
                substrate_name=substrate_name,
                effect=effect,
                commit=commit,
            )
            for effect, commit in zip(reversed(expected_prefix), recorded_history, strict=True)
        ):
            return candidate_count
    return 0


def _restore_lifecycle_scope(
    owner: VcsCore,
    state: LifecycleScopeState,
    *,
    parent: ScopeInfo,
) -> ScopeInfo:
    if state.name == owner.ground.name and state.ref == owner.ground.ref:
        return owner.ground
    existing = owner._active_scopes.get(state.name)
    if existing is not None:
        return existing
    return _restore_scope_locked(
        owner,
        name=state.name,
        ref=state.ref,
        instance_id=state.instance_id,
        creation_oid=state.creation_oid,
        world_id=state.world_id,
        parent=parent,
        isolated=state.isolated,
    )


def _load_lifecycle_context(owner: VcsCore, run: LifecycleRun) -> tuple[ScopeInfo, ScopeInfo]:
    current_parent = owner.ground
    for ancestor_state in reversed(run.active_ancestors):
        current_parent = _restore_lifecycle_scope(owner, ancestor_state, parent=current_parent)
    if run.parent.name == owner.ground.name and run.parent.ref == owner.ground.ref:
        parent = owner.ground
    else:
        parent = _restore_lifecycle_scope(owner, run.parent, parent=current_parent)
    existing_scope = owner._active_scopes.get(run.scope.name)
    if existing_scope is not None:
        return existing_scope, parent
    if owner._store.ref_exists(run.scope.ref):
        scope = _restore_lifecycle_scope(owner, run.scope, parent=parent)
    else:
        scope = ScopeInfo(
            name=run.scope.name,
            ref=run.scope.ref,
            instance_id=run.scope.instance_id,
            creation_oid=run.scope.creation_oid,
            world_id=owner._resolve_world_id(
                name=run.scope.name,
                ref=run.scope.ref,
                instance_id=run.scope.instance_id,
                world_id=run.scope.world_id,
            ),
        )
    return scope, parent


def _restore_lifecycle_substrate_state(
    owner: VcsCore,
    run: LifecycleRun,
    *,
    scope: ScopeInfo,
    parent: ScopeInfo,
) -> None:
    if "filesystem" in run.completed_substrates or not run.scope.isolated:
        return
    for substrate in owner._lifecycle_substrates:
        if getattr(substrate, "name", None) != "filesystem":
            continue
        if not isinstance(substrate, FilesystemSubstrate) or not substrate.has_overlay_layer(scope.name):
            return
        substrate.branch(
            scope.name,
            parent_scope=parent,
            hints=ForkHints(isolated=True, restore=True).to_branch_hints(),
        )
        return


def _complete_merge_locked(owner: VcsCore, scope: ScopeInfo, parent: ScopeInfo) -> str:
    _update_lifecycle_run(owner, phase="scope_merge_effect")
    if owner._store.ref_exists(scope.ref):
        _ensure_scope_merge_effect(owner, scope, parent)
    _update_lifecycle_run(owner, phase="merge_store")
    if owner._store.ref_exists(scope.ref):
        owner._store.merge(scope, parent.ref)
    owner._merge_v2_scope_world(scope, parent)
    _update_lifecycle_run(owner, phase="merge_registry")
    _publish_scope_registry_status_locked(owner, scope=scope, parent=parent, status="merged")
    _finish_scope_removal(owner, scope, parent)
    _clear_lifecycle_run(owner)
    with owner._patch_manager.guard():
        owner._notify_scope_merged(scope.name, parent.name)
    return scope.name


def _snapshot_merge_effects_locked(
    owner: VcsCore,
    scope: ScopeInfo,
    parent: ScopeInfo,
    *,
    effects_by_substrate: Mapping[str, Sequence[EffectRecord]] | None = None,
) -> None:
    for substrate in owner._lifecycle_substrates:
        if not isinstance(substrate, ContainmentSubstrate):
            continue
        substrate_name = _lifecycle_substrate_name(substrate)
        if owner._lifecycle_run is not None and substrate_name in owner._lifecycle_run.prepared_substrates:
            continue
        effects = tuple(
            effects_by_substrate[substrate_name]
            if effects_by_substrate is not None and substrate_name in effects_by_substrate
            else substrate.prepare_merge(scope, parent)
        )
        already_recorded = max(
            _prepared_effect_count(owner, substrate_name),
            _recover_prepared_effect_count_from_scope_tip(
                owner,
                scope,
                substrate_name=substrate_name,
                effects=effects,
            ),
        )
        if already_recorded > len(effects):
            msg = (
                f"Lifecycle recovery state for substrate {substrate_name!r} claims "
                f"{already_recorded} prepared effect(s), but prepare_merge() returned only {len(effects)}."
            )
            raise RuntimeError(msg)
        for effect_index, effect in enumerate(effects[already_recorded:], start=already_recorded + 1):
            with owner._scoped(scope):
                owner._pipeline.record([effect], substrate=substrate_name, scope=scope)
            _mark_prepared_effect_count(owner, substrate_name, effect_index)
        _mark_prepared_substrate(owner, substrate_name)


def _snapshot_discard_effects_locked(owner: VcsCore, scope: ScopeInfo, parent: ScopeInfo) -> None:
    for substrate in owner._lifecycle_substrates:
        if not hasattr(substrate, "prepare_merge") or not hasattr(substrate, "discard"):
            continue
        substrate_name = _lifecycle_substrate_name(substrate)
        if owner._lifecycle_run is not None and substrate_name in owner._lifecycle_run.prepared_substrates:
            continue
        effects = tuple(cast("ContainmentSubstrate", substrate).prepare_merge(scope, parent))
        already_recorded = max(
            _prepared_effect_count(owner, substrate_name),
            _recover_prepared_effect_count_from_scope_tip(
                owner,
                scope,
                substrate_name=substrate_name,
                effects=effects,
            ),
        )
        if already_recorded > len(effects):
            msg = (
                f"Lifecycle recovery state for substrate {substrate_name!r} claims "
                f"{already_recorded} prepared effect(s), but prepare_merge() returned only {len(effects)}."
            )
            raise RuntimeError(msg)
        for effect_index, effect in enumerate(effects[already_recorded:], start=already_recorded + 1):
            with owner._scoped(scope):
                owner._pipeline.record([effect], substrate=substrate_name, scope=scope)
            _mark_prepared_effect_count(owner, substrate_name, effect_index)
        _mark_prepared_substrate(owner, substrate_name)


def _snapshot_seal_effects_locked(owner: VcsCore, scope: ScopeInfo, parent: ScopeInfo) -> None:
    for substrate in owner._lifecycle_substrates:
        if not isinstance(substrate, ContainmentSubstrate):
            continue
        effects = tuple(substrate.prepare_merge(scope, parent))
        if substrate.name == "filesystem":
            effects = _uncaptured_workspace_effects(owner, scope, effects)
        if effects:
            owner._record_runtime_effects(
                effects,
                substrate=substrate.name,
                scope=scope,
                operation_kind=f"{substrate.name}.seal_snapshot",
                operation_label=f"{substrate.name}-seal-snapshot",
                workspace_driver_command="overlay-merge" if substrate.name == "filesystem" else None,
            )


def _uncaptured_workspace_effects(
    owner: VcsCore,
    scope: ScopeInfo,
    effects: Sequence[EffectRecord],
) -> tuple[EffectRecord, ...]:
    if not effects:
        return ()
    manager = owner._world_storage()
    current_world_oid = owner._current_v2_world_oid(manager, scope.ref)
    if current_world_oid is None:
        return tuple(effects)
    try:
        world = manager.read_world(current_world_oid)
        head = world.snapshot.head_for("workspace")
        substrate = manager.store(head.store_id)
        metadata = substrate.read_revision_metadata(head.head)
    except (KeyError, InvalidRepositoryStateError):
        return tuple(effects)
    if metadata.byte_authority != "tree-backed":
        return tuple(effects)
    return tuple(
        effect
        for effect in effects
        if not _workspace_effect_already_captured(
            substrate.repo,
            head.head,
            effect,
        )
    )


def _workspace_effect_already_captured(repo: pygit2.Repository, head_oid: str, effect: EffectRecord) -> bool:
    if not effect.workspace_changes:
        return False
    return all(_workspace_change_already_captured(repo, head_oid, change) for change in effect.workspace_changes)


def _workspace_change_already_captured(
    repo: pygit2.Repository,
    head_oid: str,
    change: tuple[str, bytes | None] | tuple[str, bytes | None, int],
) -> bool:
    path = change[0]
    expected_content = change[1]
    observed = read_substrate_workspace_file(repo, head_oid, path)
    if expected_content is None:
        return observed is None
    expected_mode = normalize_git_filemode(change[2]) if len(change) > 2 else 0o100644
    return observed == (expected_content, expected_mode)


def _complete_discard_locked(owner: VcsCore, scope: ScopeInfo, parent: ScopeInfo) -> str:
    _update_lifecycle_run(owner, phase="discard_snapshot_effect")
    if owner._store.ref_exists(scope.ref):
        _ensure_discard_snapshot_effect(owner, scope, parent)
    _update_lifecycle_run(owner, phase="discard_store")
    if owner._store.ref_exists(scope.ref):
        owner._store.discard(scope)
    owner._discard_v2_scope_world(scope)
    _update_lifecycle_run(owner, phase="discard_registry")
    _publish_scope_registry_status_locked(owner, scope=scope, parent=parent, status="discarded")
    _finish_scope_removal(owner, scope, parent)
    _clear_lifecycle_run(owner)
    with owner._patch_manager.guard():
        owner._notify_scope_discarded(scope.name)
    return scope.name


def _close_retained_substrates_locked(owner: VcsCore, scope: ScopeInfo, parent: ScopeInfo) -> None:
    close_failures: list[tuple[str, Exception]] = []
    for substrate in reversed(owner._lifecycle_substrates):
        close_retained = getattr(substrate, "close_retained", None)
        if not callable(close_retained):
            continue
        substrate_name = _lifecycle_substrate_name(substrate)
        if owner._lifecycle_run is not None and substrate_name in owner._lifecycle_run.completed_substrates:
            continue
        try:
            cast("Any", substrate).close_retained(scope.name, parent_scope=parent)
        except Exception as exc:  # noqa: BLE001
            close_failures.append((substrate_name, exc))
            logger.warning(
                "Substrate %s raised during retained runtime close of scope %r; continuing cleanup",
                substrate_name,
                scope.name,
                exc_info=True,
            )
        else:
            _mark_completed_substrate(owner, substrate_name)

    if close_failures:
        failed = ", ".join(name for name, _error in close_failures)
        msg = (
            f"Seal of scope {scope.name!r} failed while closing retained runtime state in substrate(s): {failed}. "
            "Scope remains active for recovery."
        )
        raise RuntimeError(msg) from close_failures[0][1]


def _lifecycle_substrate_name(substrate: object) -> str:
    name = getattr(substrate, "name", None)
    if isinstance(name, str):
        return name
    return repr(substrate)


def _complete_seal_locked(
    owner: VcsCore,
    scope: ScopeInfo,
    parent: ScopeInfo,
    *,
    prepared: PreparedSealHandoff | None = None,
    output_binding: str | None = None,
) -> SealResult:
    _update_lifecycle_run(owner, phase="seal_handoff")
    if prepared is None:
        prepared = owner._seal.prepare_seal_handoff(scope=scope, parent=parent, output_binding=output_binding)
    loaded = owner._seal.write_prepared_seal_handoff(prepared=prepared)
    _update_lifecycle_run(owner, phase="seal_runtime_close")
    _close_retained_substrates_locked(owner, scope, parent)
    _update_lifecycle_run(owner, phase="seal_registry")
    _publish_scope_registry_status_locked(owner, scope=scope, parent=parent, status="retained")
    _finish_scope_removal(owner, scope, parent)
    _clear_lifecycle_run(owner)
    return SealResult(scope=scope, parent=parent, handoff=loaded.handoff)


def _run_merge_callbacks(owner: VcsCore, scope_name: str) -> None:
    with owner._patch_manager.guard():
        for callback in owner._merge_callbacks:
            callback(scope_name)


def _run_discard_callbacks(owner: VcsCore, scope_name: str) -> None:
    with owner._patch_manager.guard():
        for callback in owner._discard_callbacks:
            callback(scope_name)


def _recover_lifecycle_locked(owner: VcsCore, mode: str = "resume") -> tuple[str, str]:
    result = _lifecycle_recovery(owner).recover(mode=mode)
    return result.callback_kind, result.scope_name


def restore_scope(
    owner: VcsCore,
    name: str,
    ref: str,
    instance_id: str,
    creation_oid: str,
    parent: ScopeInfo,
    *,
    world_id: str | None = None,
    isolated: bool = False,
) -> ScopeInfo:
    with owner._lock:
        return _restore_scope_locked(
            owner,
            name=name,
            ref=ref,
            instance_id=instance_id,
            creation_oid=creation_oid,
            world_id=world_id,
            parent=parent,
            isolated=isolated,
        )


def _restore_scope_locked(
    owner: VcsCore,
    *,
    name: str,
    ref: str,
    instance_id: str,
    creation_oid: str,
    parent: ScopeInfo,
    world_id: str | None = None,
    isolated: bool = False,
) -> ScopeInfo:
    if not owner._store.ref_exists(ref):
        raise StaleScopeError(f"Scope ref missing: {ref}")
    scope = ScopeInfo(
        name=name,
        ref=ref,
        instance_id=instance_id,
        creation_oid=creation_oid,
        world_id=owner._resolve_world_id(
            name=name,
            ref=ref,
            instance_id=instance_id,
            world_id=world_id,
        ),
    )
    owner._active_scopes[name] = scope
    owner._scope_parents[name] = parent
    owner._restored_scopes.add(name)
    if isolated:
        owner._isolated_scopes.add(name)
    owner._orphaned_refs = [candidate for candidate in owner._orphaned_refs if candidate != ref]
    _set_runtime_context(owner, scope)
    return scope


def clear_restored_scope_state(owner: VcsCore) -> None:
    with owner._lock:
        for name in list(owner._restored_scopes):
            owner._active_scopes.pop(name, None)
            owner._scope_parents.pop(name, None)
            owner._isolated_scopes.discard(name)
            owner._parent_tree_manifests = {
                key: manifest
                for key, manifest in owner._parent_tree_manifests.items()
                if key[0].rsplit("/", 1)[-1] != name
            }
        owner._restored_scopes.clear()
        _set_runtime_context(owner, None)


def _assert_can_fork_from_registry(
    registry_snapshot: Any,
    *,
    parent: ScopeInfo,
    child_name: str,
) -> None:
    for entry in registry_snapshot.entries:
        if entry.status == "retained" and entry.name == child_name:
            raise ScopeAdmissionError(
                f"Cannot create scope {child_name!r} from {parent.name!r}: scope name is retained. "
                "Select, release, or discard the retained output before reusing the name."
            )
        if entry.status == "live" and entry.parent_ref == parent.ref:
            raise ScopeAdmissionError(
                f"Cannot create scope {child_name!r} from {parent.name!r}: parent already has live child "
                f"scope {entry.name!r}. Merge or discard {entry.name!r} before creating another child."
            )


def _capture_parent_tree_manifest_for_fork(
    owner: VcsCore,
    *,
    scope: ScopeInfo,
    parent: ScopeInfo,
    isolated: bool,
) -> tuple[str, str] | None:
    from vcs_core._vcscore_runtime import _nested_operations_enabled

    if not isolated or not _nested_operations_enabled():
        return None
    filesystem = owner._filesystem_overlay_substrate()
    if filesystem is None:
        return None
    parent_layer = owner._overlay_base_scope_name(parent)
    parent_root = filesystem.overlay_mount_path(parent_layer)
    if parent_root is None:
        return None
    if not parent_root.is_dir():
        return None
    manifest = capture_parent_tree_manifest(parent_root, layer_name=parent_layer)
    if _is_root_only_unverifiable_manifest(manifest):
        return None
    key = (scope.ref, scope.instance_id)
    owner._parent_tree_manifests[key] = manifest
    return key


def _is_root_only_unverifiable_manifest(manifest: Any) -> bool:
    if len(manifest.entries) != 1:
        return False
    entry = next(iter(manifest.entries.values()))
    return entry.kind == "unverifiable" and entry.path in {"", "."}


def _assert_parent_tree_manifest_clean(owner: VcsCore, scope: ScopeInfo) -> None:
    from vcs_core._vcscore_runtime import _nested_operations_enabled

    if not _nested_operations_enabled():
        return
    manifest = owner._parent_tree_manifests.get((scope.ref, scope.instance_id))
    if manifest is None:
        return
    filesystem = owner._filesystem_overlay_substrate()
    if filesystem is None:
        return
    parent_root = filesystem.overlay_mount_path(manifest.layer_name)
    if parent_root is None:
        return
    divergences = diff_parent_tree_manifest(manifest, parent_root)
    if not divergences:
        return
    sample = ", ".join(f"{divergence.path or '.'} ({divergence.reason})" for divergence in divergences[:5])
    remainder = len(divergences) - min(len(divergences), 5)
    suffix = f", and {remainder} more" if remainder > 0 else ""
    raise ParentWorkingTreeDivergedError(
        f"Parent working tree changed after scope {scope.name!r} was forked: {sample}{suffix}. "
        f"Record the parent's changes through vcs-core and fork fresh, or discard child scope {scope.name!r}; "
        "discard archives the child scope, it does not delete recorded parent history."
    )


def fork(
    owner: VcsCore,
    parent: ScopeInfo,
    name: str,
    hints: ForkHints | None = None,
) -> ScopeInfo:
    hints = hints or ForkHints()
    with owner._lock:
        _admission(owner).require_lifecycle_mutation_allowed("fork")
        parent = owner._live_scope(parent)
        registry_snapshot = owner._store.require_scope_registry_projection()
        _assert_can_fork_from_registry(registry_snapshot, parent=parent, child_name=name)
        scope = owner._store.fork(parent.ref, name)

        previous_context = owner._pipeline.context
        _set_runtime_context(owner, scope)

        branched: list[Any] = []
        manifest_key: tuple[str, str] | None = None
        try:
            for substrate in owner._lifecycle_substrates:
                if hasattr(substrate, "branch") and hasattr(substrate, "discard"):
                    substrate.branch(scope.name, parent_scope=parent, hints=hints.to_branch_hints())
                    branched.append(substrate)
            owner._fork_v2_scope_world(scope, parent)
            manifest_key = _capture_parent_tree_manifest_for_fork(
                owner,
                scope=scope,
                parent=parent,
                isolated=hints.isolated,
            )
        except Exception:
            if manifest_key is not None:
                owner._parent_tree_manifests.pop(manifest_key, None)
            for branched_substrate in reversed(branched):
                try:
                    branched_substrate.discard(scope.name)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Failed to clean up substrate %s after fork failure",
                        getattr(branched_substrate, "name", branched_substrate),
                        exc_info=True,
                    )
            try:
                owner._store.discard(scope)
            except Exception:  # noqa: BLE001
                logger.warning("Failed to clean up Store ref after fork failure", exc_info=True)
            try:
                owner._discard_v2_scope_world(scope)
            except Exception:  # noqa: BLE001
                logger.warning("Failed to clean up v2 scope world after fork failure", exc_info=True)
            owner._pipeline.set_context(previous_context)
            raise

        try:
            _publish_scope_registry_fork_locked(
                owner,
                scope=scope,
                parent=parent,
                isolated=hints.isolated,
                expected_head_oid=registry_snapshot.head_oid,
            )
        except Exception:
            if manifest_key is not None:
                owner._parent_tree_manifests.pop(manifest_key, None)
            for branched_substrate in reversed(branched):
                try:
                    branched_substrate.discard(scope.name)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Failed to clean up substrate %s after scope-registry fork publish failure",
                        getattr(branched_substrate, "name", branched_substrate),
                        exc_info=True,
                    )
            try:
                owner._store.discard(scope)
            except Exception:  # noqa: BLE001
                logger.warning("Failed to clean up Store ref after scope-registry fork publish failure", exc_info=True)
            try:
                owner._discard_v2_scope_world(scope)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to clean up v2 scope world after scope-registry fork publish failure",
                    exc_info=True,
                )
            owner._pipeline.set_context(previous_context)
            raise

        owner._active_scopes[scope.name] = scope
        owner._scope_parents[scope.name] = parent
        owner._restored_scopes.discard(scope.name)
        if hints.isolated:
            owner._isolated_scopes.add(scope.name)
        return scope


def merge(owner: VcsCore, scope: ScopeInfo, parent: ScopeInfo) -> str:
    scope_name: str
    with owner._lock:
        _admission(owner).require_lifecycle_mutation_allowed("merge")
        owner._validate_scope(scope)
        owner._validate_scope(parent)
        scope = owner._live_scope(scope)
        parent = owner._live_scope(parent)
        owner._store.assert_mergeable(scope, parent.ref)
        _assert_parent_tree_manifest_clean(owner, scope)
        prepared_substrates: list[ContainmentSubstrate] = []
        for substrate in owner._lifecycle_substrates:
            if isinstance(substrate, ContainmentSubstrate):
                effects = substrate.prepare_merge(scope, parent)
                if effects:
                    owner._record_runtime_effects(
                        effects,
                        substrate=substrate.name,
                        scope=scope,
                        workspace_driver_command="overlay-merge" if substrate.name == "filesystem" else None,
                    )
                prepared_substrates.append(substrate)

        _begin_lifecycle_run(owner, operation="merge", phase="commit_substrates", scope=scope, parent=parent)
        for containment_substrate in reversed(prepared_substrates):
            containment_substrate.commit_merge(scope.name, parent_scope=parent)
            _mark_completed_substrate(owner, containment_substrate.name)

        scope_name = _complete_merge_locked(owner, scope, parent)

    _run_merge_callbacks(owner, scope_name)
    return scope_name


def merge_with_authority(
    owner: VcsCore,
    scope: ScopeInfo,
    parent: ScopeInfo,
    *,
    binding_roots: Mapping[str, str],
    decide: DecisionProvider,
    operation_id: str | None = None,
    effective_match_digest: str | None = None,
    authority_surface_plan_digest: str | None = None,
    permission_plan_digest: str | None = None,
    permission_plan_descriptor: Mapping[str, object] | None = None,
    authority_context: Mapping[str, object] | None = None,
) -> AuthorityMergeResult:
    """Merge a scope through the first internal authority lane.

    The lane is intentionally narrow: filesystem carrier candidates are prepared
    once, classified into flat GitRepo v0 requests, judged by a data-only
    decision provider, and adopted only if the whole cohort is allowed. Denied,
    refused, or no-drift-failed cohorts are discarded through an authority-safe
    cleanup path that does not snapshot candidate filesystem changes as ordinary
    workspace facts.
    """
    callback: tuple[str, str] | None = None
    result: AuthorityMergeResult
    with owner._lock:
        normalized_authority_context = normalize_authority_context(
            dict(authority_context) if authority_context is not None else None
        )
        try:
            validated_permission_plan_descriptor = validate_permission_plan_evidence(
                permission_plan_digest_value=permission_plan_digest,
                permission_plan_descriptor=permission_plan_descriptor,
                expected_route="carrier_diff",
                expected_effective_match_digest=effective_match_digest,
                expected_authority_surface_plan_digest=authority_surface_plan_digest,
            )
        except PermissionPlanEvidenceError as exc:
            raise InvalidRepositoryStateError(f"authority merge PermissionPlan evidence invalid: {exc}") from exc
        validated_permission_plan_digest = cast("str", permission_plan_digest)
        _admission(owner).require_lifecycle_mutation_allowed("merge_with_authority")
        owner._validate_scope(scope)
        owner._validate_scope(parent)
        scope = owner._live_scope(scope)
        parent = owner._live_scope(parent)
        owner._store.assert_mergeable(scope, parent.ref)
        _assert_parent_tree_manifest_clean(owner, scope)
        manager = owner._world_storage()
        parent_world_before = owner._current_v2_world_oid(manager, parent.ref)
        parent_world_after = parent_world_before
        try:
            normalized_binding_roots = normalize_gitrepo_binding_roots(binding_roots)
            binding_roots_error: str | None = None
        except AuthorityBindingRootsError as exc:
            normalized_binding_roots = {}
            binding_roots_error = exc.reason_code

        prepared_substrates: list[ContainmentSubstrate] = []
        effects_by_substrate: dict[str, tuple[EffectRecord, ...]] = {}
        for substrate in owner._lifecycle_substrates:
            if not isinstance(substrate, ContainmentSubstrate):
                continue
            effects = tuple(substrate.prepare_merge(scope, parent))
            prepared_substrates.append(substrate)
            effects_by_substrate[substrate.name] = effects

        prepared = prepare_authority_merge(scope=scope, parent=parent, effects_by_substrate=effects_by_substrate)
        authority_operation_id = operation_id or owner._new_operation_id()
        settlement_operation_id = f"{authority_operation_id}_settlement"
        ensure_authority_operation_ids_available(owner, authority_operation_id, settlement_operation_id)
        decisions = []
        outcome: AuthorityOutcome = "allowed"
        settlement: AuthoritySettlement = "merged"
        commit_outcome: AuthorityCommitOutcome = "pending"
        settlement_reason = "all_allowed"

        with owner.runtime_activity(
            scope=scope,
            operation_id=authority_operation_id,
            operation_label="skeleton filesystem authority",
            operation_kind=AUTHORITY_MERGE_OPERATION_KIND,
            operation_metadata={
                "authority": {
                    "cohort_id": prepared.cohort_id,
                    "candidate_digest": prepared.candidate_digest,
                    "monitor_basis": "carrier_check_at_commit",
                    "permission_plan_digest": validated_permission_plan_digest,
                    "permission_plan_descriptor": validated_permission_plan_descriptor,
                    **(
                        {"authority_context": normalized_authority_context}
                        if normalized_authority_context is not None
                        else {}
                    ),
                }
            },
        ) as operation:
            if operation is None:
                raise RuntimeError("authority merge requires an operation boundary.")
            owner._pipeline.record_one(
                EffectRecord(
                    effect_type="PreparedAuthorityMerge",
                    metadata=prepared.to_metadata(
                        operation_id=authority_operation_id,
                        authority_context=normalized_authority_context,
                    ),
                ),
                substrate="vcscore.authority",
                scope=scope,
            )
            decision_index = 0
            for substrate_name, effects in effects_by_substrate.items():
                for candidate_index, effect in enumerate(effects):
                    if substrate_name != "filesystem":
                        decision: AuthorityDecision | AuthorityOutcome = AuthorityDecision(
                            outcome="refused",
                            reason_code="unsupported_authority_substrate",
                        )
                        request = classify_gitrepo_authority_request(
                            effect,
                            candidate_index=candidate_index,
                            candidate_digest=prepared.candidate_digest,
                            candidate_effect_ref=f"{substrate_name}:{candidate_index}",
                            substrate=substrate_name,
                            scope=scope,
                            parent=parent,
                            binding_roots=normalized_binding_roots,
                            monitor_basis="carrier_check_at_commit",
                        )
                    else:
                        request = classify_gitrepo_authority_request(
                            effect,
                            candidate_index=candidate_index,
                            candidate_digest=prepared.candidate_digest,
                            candidate_effect_ref=f"{substrate_name}:{candidate_index}",
                            substrate=substrate_name,
                            scope=scope,
                            parent=parent,
                            binding_roots=normalized_binding_roots,
                            monitor_basis="carrier_check_at_commit",
                        )
                        if binding_roots_error is not None:
                            decision = AuthorityDecision(outcome="refused", reason_code=binding_roots_error)
                        elif request.reason_code is not None:
                            decision = AuthorityDecision(outcome="refused", reason_code=request.reason_code)
                        else:
                            decision = decide(request)
                    record = make_decision_record(
                        request,
                        decision,
                        decision_index=decision_index,
                        effective_match_digest=effective_match_digest,
                        authority_surface_plan_digest=authority_surface_plan_digest,
                        permission_plan_digest=validated_permission_plan_digest,
                        permission_plan_descriptor=validated_permission_plan_descriptor,
                    )
                    decision_index += 1
                    decisions.append(record)
                    owner._pipeline.record_one(
                        EffectRecord(
                            effect_type="AuthorityDecision",
                            metadata=record.to_metadata(
                                cohort_id=prepared.cohort_id,
                                operation_id=authority_operation_id,
                                authority_context=normalized_authority_context,
                            ),
                        ),
                        substrate="vcscore.authority",
                        scope=scope,
                    )

            if any(decision.outcome == "refused" for decision in decisions):
                outcome = "refused"
                settlement = "discarded"
                commit_outcome = "not_committed_refused"
                settlement_reason = "refused_decision"
            elif any(decision.outcome == "denied" for decision in decisions):
                outcome = "denied"
                settlement = "discarded"
                commit_outcome = "not_committed_denied"
                settlement_reason = "denied_decision"
            else:
                try:
                    _verify_prepared_authority_merge(
                        prepared,
                        scope=scope,
                        parent=parent,
                        substrates=prepared_substrates,
                    )
                except AuthorityMergeDriftError:
                    outcome = "refused"
                    settlement = "discarded"
                    commit_outcome = "not_committed_refused"
                    settlement_reason = "substrate_no_drift_failed"
                else:
                    commit_outcome = "pending"
                    settlement_reason = "merged_after_allowed_decision"

        workspace_publication_operation_id = (
            _authority_workspace_publication_operation_id(
                owner,
                scope=scope,
                authority_operation_id=authority_operation_id,
                effects_by_substrate=effects_by_substrate,
            )
            if outcome == "allowed"
            else None
        )

        pending_settlement = begin_pending_authority_settlement(
            owner,
            _authority_settlement_pending(
                scope=scope,
                parent=parent,
                settlement_operation_id=settlement_operation_id,
                authority_operation_id=authority_operation_id,
                cohort_id=prepared.cohort_id,
                candidate_digest=prepared.candidate_digest,
                outcome=outcome,
                settlement=settlement,
                commit_outcome=commit_outcome,
                decision_ids=tuple(decision.decision_id for decision in decisions),
                reason_code=settlement_reason,
                workspace_publication_operation_id=workspace_publication_operation_id,
                parent_world_before=parent_world_before,
                authority_context=normalized_authority_context,
                permission_plan_digest=validated_permission_plan_digest,
                permission_plan_descriptor=validated_permission_plan_descriptor,
            ),
        )

        if outcome == "allowed":
            _begin_lifecycle_run(owner, operation="merge", phase="prepare_merge_effects", scope=scope, parent=parent)
            _snapshot_merge_effects_locked(
                owner,
                scope,
                parent,
                effects_by_substrate=effects_by_substrate,
            )
            _update_lifecycle_run(owner, phase="commit_substrates")
            for containment_substrate in reversed(prepared_substrates):
                containment_substrate.commit_merge(scope.name, parent_scope=parent)
                _mark_completed_substrate(owner, containment_substrate.name)
            _select_authority_workspace_state_for_allowed_merge(
                owner,
                scope=scope,
                authority_operation_id=authority_operation_id,
                effects_by_substrate=effects_by_substrate,
                workspace_publication_operation_id=workspace_publication_operation_id,
            )
            scope_name = _complete_merge_locked(owner, scope, parent)
            parent_world_after = owner._current_v2_world_oid(manager, parent.ref)
            commit_outcome = "merged"
            settlement_reason = "merged_after_allowed_decision"
            pending_settlement = update_pending_authority_settlement(
                owner,
                pending_settlement,
                phase="adopted",
                commit_outcome=commit_outcome,
                reason_code=settlement_reason,
                parent_world_after=parent_world_after,
            )
            callback = ("merge", scope_name)
        else:
            scope_name = _authority_safe_discard_locked(owner, scope, parent)
            pending_settlement = update_pending_authority_settlement(
                owner,
                pending_settlement,
                phase="discarded",
                commit_outcome=commit_outcome,
                reason_code=settlement_reason,
                parent_world_after=parent_world_after,
            )
            callback = ("discard", scope_name)
        _record_authority_final_settlement(
            owner,
            scope=parent,
            settlement_operation_id=settlement_operation_id,
            authority_operation_id=authority_operation_id,
            cohort_id=prepared.cohort_id,
            candidate_digest=prepared.candidate_digest,
            outcome=outcome,
            settlement=settlement,
            commit_outcome=commit_outcome,
            decision_ids=tuple(decision.decision_id for decision in decisions),
            reason_code=settlement_reason,
            workspace_publication_operation_id=workspace_publication_operation_id,
            parent_world_before=parent_world_before,
            parent_world_after=parent_world_after,
            permission_plan_digest=validated_permission_plan_digest,
            permission_plan_descriptor=validated_permission_plan_descriptor,
            authority_context=normalized_authority_context,
        )
        clear_pending_authority_transaction(owner, settlement_operation_id)
        result = AuthorityMergeResult(
            scope_name=scope_name,
            authority_operation_id=authority_operation_id,
            settlement_operation_id=settlement_operation_id,
            cohort_id=prepared.cohort_id,
            candidate_digest=prepared.candidate_digest,
            outcome=outcome,
            settlement=settlement,
            parent_world_before=parent_world_before,
            parent_world_after=parent_world_after,
            decisions=tuple(decisions),
            permission_plan_digest=validated_permission_plan_digest,
            permission_plan_descriptor=validated_permission_plan_descriptor,
        )

    if callback == ("merge", result.scope_name):
        _run_merge_callbacks(owner, result.scope_name)
    elif callback == ("discard", result.scope_name):
        _run_discard_callbacks(owner, result.scope_name)
    return result


def _verify_prepared_authority_merge(
    prepared: PreparedAuthorityMerge,
    *,
    scope: ScopeInfo,
    parent: ScopeInfo,
    substrates: Sequence[ContainmentSubstrate],
) -> None:
    current = {substrate.name: tuple(substrate.prepare_merge(scope, parent)) for substrate in substrates}
    current_digests = {substrate_name: digest_effects(effects) for substrate_name, effects in current.items()}
    if current_digests != prepared.prepared_substrate_digests:
        raise AuthorityMergeDriftError("prepared authority merge substrate digest changed before settlement")


def _select_authority_workspace_state_for_allowed_merge(
    owner: VcsCore,
    *,
    scope: ScopeInfo,
    authority_operation_id: str,
    effects_by_substrate: Mapping[str, Sequence[EffectRecord]],
    workspace_publication_operation_id: str | None = None,
) -> None:
    operation_id = workspace_publication_operation_id or _authority_workspace_publication_operation_id(
        owner,
        scope=scope,
        authority_operation_id=authority_operation_id,
        effects_by_substrate=effects_by_substrate,
    )
    if operation_id is None:
        return
    effects = tuple(effects_by_substrate.get("filesystem", ()))
    owner._select_workspace_state_from_store_required(
        scope=scope,
        operation_id=operation_id,
        source_operation_id=authority_operation_id,
        driver_command="overlay-merge",
        message=f"skeleton authority workspace overlay merge: {authority_operation_id}",
        effects=effects,
    )


def _authority_workspace_publication_operation_id(
    owner: VcsCore,
    *,
    scope: ScopeInfo,
    authority_operation_id: str,
    effects_by_substrate: Mapping[str, Sequence[EffectRecord]],
) -> str | None:
    effects = tuple(effects_by_substrate.get("filesystem", ()))
    if not any(effect.workspace_changes for effect in effects):
        return None
    return owner._workspace_driver_operation_id("overlay-merge", authority_operation_id, scope)


def _authority_settlement_pending(
    *,
    scope: ScopeInfo,
    parent: ScopeInfo,
    settlement_operation_id: str,
    authority_operation_id: str,
    cohort_id: str,
    candidate_digest: str,
    outcome: AuthorityOutcome,
    settlement: AuthoritySettlement,
    commit_outcome: AuthorityCommitOutcome,
    decision_ids: Sequence[str],
    reason_code: str,
    parent_world_before: str | None,
    workspace_publication_operation_id: str | None = None,
    parent_world_after: str | None = None,
    authority_context: Mapping[str, object] | None = None,
    permission_plan_digest: str | None = None,
    permission_plan_descriptor: Mapping[str, object] | None = None,
) -> PendingAuthoritySettlement:
    return PendingAuthoritySettlement(
        settlement_operation_id=settlement_operation_id,
        authority_operation_id=authority_operation_id,
        scope_name=scope.name,
        scope_ref=scope.ref,
        scope_instance_id=scope.instance_id,
        scope_world_id=scope.world_id,
        parent_scope_name=parent.name,
        parent_scope_ref=parent.ref,
        parent_scope_instance_id=parent.instance_id,
        parent_scope_world_id=parent.world_id,
        cohort_id=cohort_id,
        candidate_digest=candidate_digest,
        outcome=outcome,
        settlement=settlement,
        commit_outcome=commit_outcome,
        decision_ids=tuple(decision_ids),
        reason_code=reason_code,
        workspace_publication_operation_id=workspace_publication_operation_id,
        parent_world_before=parent_world_before,
        parent_world_after=parent_world_after,
        authority_context=dict(authority_context) if authority_context is not None else None,
        permission_plan_digest=permission_plan_digest,
        permission_plan_descriptor=dict(permission_plan_descriptor) if permission_plan_descriptor is not None else None,
    )


def _record_authority_final_settlement(
    owner: VcsCore,
    *,
    scope: ScopeInfo,
    settlement_operation_id: str,
    authority_operation_id: str,
    cohort_id: str,
    candidate_digest: str,
    outcome: AuthorityOutcome,
    settlement: AuthoritySettlement,
    commit_outcome: AuthorityCommitOutcome,
    decision_ids: Sequence[str],
    reason_code: str,
    workspace_publication_operation_id: str | None = None,
    parent_world_before: str | None = None,
    parent_world_after: str | None = None,
    permission_plan_digest: str | None = None,
    permission_plan_descriptor: Mapping[str, object] | None = None,
    authority_context: Mapping[str, object] | None = None,
) -> None:
    record_authority_settlement_effect(
        owner,
        scope=scope,
        settlement_operation_id=settlement_operation_id,
        authority_operation_id=authority_operation_id,
        cohort_id=cohort_id,
        candidate_digest=candidate_digest,
        monitor_basis="carrier_check_at_commit",
        operation_label="skeleton filesystem authority settlement",
        operation_kind=AUTHORITY_SETTLEMENT_OPERATION_KIND,
        effect_type="AuthoritySettlement",
        effect_metadata=settlement_metadata(
            operation_id=authority_operation_id,
            cohort_id=cohort_id,
            candidate_digest=candidate_digest,
            outcome=outcome,
            settlement=settlement,
            commit_outcome=commit_outcome,
            decision_ids=decision_ids,
            reason_code=reason_code,
            workspace_publication_operation_id=workspace_publication_operation_id,
            parent_world_before=parent_world_before,
            parent_world_after=parent_world_after,
            permission_plan_digest=permission_plan_digest,
            permission_plan_descriptor=permission_plan_descriptor,
            authority_context=authority_context,
        ),
        authority_context=authority_context,
    )


def _owns_workspace_publication_pending(
    pending: PendingAuthoritySettlement,
    workspace_pending: WorkspaceAuthorityPending,
) -> bool:
    if pending.transaction_kind != "filesystem_merge":
        return False
    if pending.workspace_publication_operation_id is not None:
        return workspace_pending.operation_id == pending.workspace_publication_operation_id
    return (
        workspace_pending.source_operation_id == pending.authority_operation_id
        and workspace_pending.scope_ref == pending.scope_ref
    )


def _owned_workspace_publication_pending_records(
    owner: VcsCore,
    pending_records: Sequence[PendingAuthoritySettlement],
) -> tuple[WorkspaceAuthorityPending, ...]:
    return tuple(
        workspace_pending
        for workspace_pending in pending_workspace_authority_records(owner._repo_path)
        if any(_owns_workspace_publication_pending(pending, workspace_pending) for pending in pending_records)
    )


def _recover_owned_workspace_publication(
    owner: VcsCore,
    pending: PendingAuthoritySettlement,
) -> None:
    for workspace_pending in _owned_workspace_publication_pending_records(owner, (pending,)):
        owner._recover_workspace_authority_pending(workspace_pending)


def _recover_owned_lifecycle(
    owner: VcsCore,
    pending_records: Sequence[PendingAuthoritySettlement],
) -> tuple[str, str] | None:
    lifecycle_run = owner._lifecycle_run
    if lifecycle_run is None:
        return None
    pending = _authority_pending_for_lifecycle_run(pending_records, lifecycle_run)
    if pending is None:
        return None
    if pending.settlement == "merged":
        _recover_owned_workspace_publication(owner, pending)
    return _recover_lifecycle_locked(owner, mode="resume")


def recover_authority_settlements(owner: VcsCore) -> tuple[str, ...]:
    callbacks: list[tuple[str, str]] = []
    recovered: list[str] = []
    with owner._lock:
        if owner._ground is None:
            raise RuntimeError("VcsCore not activated. Call activate() first.")
        pending_records = read_valid_authority_settlement_pending_records(owner._repo_path)
        lifecycle_pending = None
        if owner._lifecycle_run is not None:
            lifecycle_pending = _authority_pending_for_lifecycle_run(pending_records, owner._lifecycle_run)
            if lifecycle_pending is None:
                raise InvalidRepositoryStateError(
                    "Cannot recover authority settlements while unrelated lifecycle recovery is pending. "
                    "Run recover_lifecycle() first."
                )
        pending_scope_refs = {
            scope_ref for pending in pending_records for scope_ref in (pending.scope_ref, pending.parent_scope_ref)
        }
        owned_workspace_publication_operation_ids = {
            workspace_pending.operation_id
            for workspace_pending in _owned_workspace_publication_pending_records(owner, pending_records)
        }
        require_recovery_targets_allowed(
            owner,
            attempted="recover authority settlements",
            targets=(
                *authority_settlement_recovery_targets(owner),
                *workspace_authority_recovery_targets(
                    owner,
                    operation_ids=owned_workspace_publication_operation_ids,
                ),
                *recovery_targets_for_scope_refs(owner, pending_scope_refs),
                *recovery_operation_targets_for_scope_refs(owner, pending_scope_refs),
            ),
        )
        if lifecycle_pending is not None:
            lifecycle_callback = _recover_owned_lifecycle(owner, pending_records)
            if lifecycle_callback is None:
                raise InvalidRepositoryStateError(
                    "Cannot recover authority settlements while unrelated lifecycle recovery is pending. "
                    "Run recover_lifecycle() first."
                )
            callbacks.append(lifecycle_callback)
            pending_records = read_valid_authority_settlement_pending_records(owner._repo_path)
        for pending in pending_records:
            settled_pending, callback = _recover_one_authority_settlement(owner, pending)
            if settled_pending.transaction_kind in {"retained_output_selection", "retained_output_application"}:
                from vcs_core._retained_output_selection import record_retained_output_authority_final_settlement

                settling_operation_id = _retained_pending_settling_operation_id(settled_pending)
                if settling_operation_id is None:
                    raise InvalidRepositoryStateError(
                        f"Cannot recover authority settlement {settled_pending.settlement_operation_id!r}: "
                        f"missing retained-output settling operation id for {settled_pending.transaction_kind}"
                    )
                settling_kwarg = (
                    {"application_operation_id": settling_operation_id}
                    if settled_pending.transaction_kind == "retained_output_application"
                    else {"selection_operation_id": settling_operation_id}
                )
                record_retained_output_authority_final_settlement(
                    owner,
                    parent=_pending_parent_scope(owner, settled_pending),
                    settlement_operation_id=settled_pending.settlement_operation_id,
                    authority_operation_id=settled_pending.authority_operation_id,
                    cohort_id=settled_pending.cohort_id,
                    candidate_digest=settled_pending.candidate_digest,
                    outcome=settled_pending.outcome,
                    settlement=settled_pending.settlement,
                    commit_outcome=settled_pending.commit_outcome,
                    decision_ids=settled_pending.decision_ids,
                    reason_code=settled_pending.reason_code,
                    permission_plan_digest=settled_pending.permission_plan_digest,
                    permission_plan_descriptor=settled_pending.permission_plan_descriptor,
                    authority_context=settled_pending.authority_context,
                    **settling_kwarg,
                )
            else:
                _record_authority_final_settlement(
                    owner,
                    scope=_pending_parent_scope(owner, settled_pending),
                    settlement_operation_id=settled_pending.settlement_operation_id,
                    authority_operation_id=settled_pending.authority_operation_id,
                    cohort_id=settled_pending.cohort_id,
                    candidate_digest=settled_pending.candidate_digest,
                    outcome=settled_pending.outcome,
                    settlement=settled_pending.settlement,
                    commit_outcome=settled_pending.commit_outcome,
                    decision_ids=settled_pending.decision_ids,
                    reason_code=settled_pending.reason_code,
                    workspace_publication_operation_id=settled_pending.workspace_publication_operation_id,
                    parent_world_before=settled_pending.parent_world_before,
                    parent_world_after=settled_pending.parent_world_after,
                    permission_plan_digest=settled_pending.permission_plan_digest,
                    permission_plan_descriptor=settled_pending.permission_plan_descriptor,
                    authority_context=settled_pending.authority_context,
                )
            clear_pending_authority_transaction(owner, settled_pending.settlement_operation_id)
            recovered.append(settled_pending.settlement_operation_id)
            if callback is not None:
                callbacks.append(callback)

    for callback_kind, scope_name in callbacks:
        if callback_kind == "merge":
            _run_merge_callbacks(owner, scope_name)
        elif callback_kind == "discard":
            _run_discard_callbacks(owner, scope_name)
    return tuple(recovered)


def _recover_one_authority_settlement(
    owner: VcsCore,
    pending: PendingAuthoritySettlement,
) -> tuple[PendingAuthoritySettlement, tuple[str, str] | None]:
    if pending.transaction_kind in {"retained_output_selection", "retained_output_application"}:
        return _recover_one_retained_output_authority_settlement(owner, pending), None
    if pending.phase in {"adopted", "discarded"}:
        return pending, None
    scope = _pending_scope(owner, pending)
    parent = _pending_parent_scope(owner, pending)
    if not owner._store.ref_exists(scope.ref):
        phase: Literal["adopted", "discarded"] = "adopted" if pending.settlement == "merged" else "discarded"
        commit_outcome = "merged" if pending.settlement == "merged" else pending.commit_outcome
        parent_world_after = pending.parent_world_after or pending.parent_world_before
        if pending.settlement == "merged" and parent_world_after == pending.parent_world_before:
            parent_world_after = owner._current_v2_world_oid(owner._world_storage(), parent.ref)
        recovered = update_pending_authority_settlement(
            owner,
            pending,
            phase=phase,
            commit_outcome=commit_outcome,
            parent_world_after=parent_world_after,
        )
        return recovered, None
    if pending.settlement == "merged":
        if pending.outcome != "allowed":
            raise InvalidRepositoryStateError(
                f"Cannot recover authority settlement {pending.settlement_operation_id!r}: "
                "non-allowed pending record requests merge"
            )
        prepared_substrates: list[ContainmentSubstrate] = []
        effects_by_substrate: dict[str, tuple[EffectRecord, ...]] = {}
        for substrate in owner._lifecycle_substrates:
            if not isinstance(substrate, ContainmentSubstrate):
                continue
            effects = tuple(substrate.prepare_merge(scope, parent))
            prepared_substrates.append(substrate)
            effects_by_substrate[substrate.name] = effects
        prepared = prepare_authority_merge(scope=scope, parent=parent, effects_by_substrate=effects_by_substrate)
        if prepared.candidate_digest != pending.candidate_digest:
            raise InvalidRepositoryStateError(
                f"Cannot recover authority settlement {pending.settlement_operation_id!r}: "
                "prepared candidate digest changed"
            )
        _begin_lifecycle_run(owner, operation="merge", phase="prepare_merge_effects", scope=scope, parent=parent)
        _snapshot_merge_effects_locked(owner, scope, parent, effects_by_substrate=effects_by_substrate)
        _update_lifecycle_run(owner, phase="commit_substrates")
        for containment_substrate in reversed(prepared_substrates):
            containment_substrate.commit_merge(scope.name, parent_scope=parent)
            _mark_completed_substrate(owner, containment_substrate.name)
        _select_authority_workspace_state_for_allowed_merge(
            owner,
            scope=scope,
            authority_operation_id=pending.authority_operation_id,
            effects_by_substrate=effects_by_substrate,
            workspace_publication_operation_id=pending.workspace_publication_operation_id,
        )
        scope_name = _complete_merge_locked(owner, scope, parent)
        parent_world_after = owner._current_v2_world_oid(owner._world_storage(), parent.ref)
        recovered = update_pending_authority_settlement(
            owner,
            pending,
            phase="adopted",
            commit_outcome="merged",
            reason_code="merged_after_allowed_decision",
            parent_world_after=parent_world_after,
        )
        return recovered, ("merge", scope_name)

    scope_name = _authority_safe_discard_locked(owner, scope, parent)
    recovered = update_pending_authority_settlement(
        owner,
        pending,
        phase="discarded",
        parent_world_after=pending.parent_world_after or pending.parent_world_before,
    )
    return recovered, ("discard", scope_name)


# Per-kind recovery vocabulary for retained-output authority settlements (T1 D7): the future
# settlement-action registry's recovery column (g10).
_RETAINED_AUTHORITY_RECOVERY_VOCAB: dict[str, tuple[str, str, str, str]] = {
    # kind -> (positive settlement, negative settlement, adopted reason code, discarded reason code)
    "retained_output_selection": (
        "selected",
        "not_selected",
        "selected_after_allowed_decision",
        "recovered_before_retained_output_selection",
    ),
    "retained_output_application": (
        "applied",
        "not_applied",
        "applied_after_allowed_decision",
        "recovered_before_retained_output_application",
    ),
}


def _retained_pending_settling_operation_id(pending: PendingAuthoritySettlement) -> str | None:
    if pending.transaction_kind == "retained_output_application":
        return pending.application_operation_id
    return pending.selection_operation_id


def _recover_one_retained_output_authority_settlement(
    owner: VcsCore,
    pending: PendingAuthoritySettlement,
) -> PendingAuthoritySettlement:
    positive, negative, adopted_reason, discarded_reason = _RETAINED_AUTHORITY_RECOVERY_VOCAB[pending.transaction_kind]
    if pending.phase in {"adopted", "discarded"}:
        return pending
    if pending.settlement == negative:
        return update_pending_authority_settlement(owner, pending, phase="discarded")
    if pending.settlement != positive:
        raise InvalidRepositoryStateError(
            f"Cannot recover retained-output authority settlement {pending.settlement_operation_id!r}: "
            f"unsupported settlement {pending.settlement!r}"
        )
    if pending.outcome != "allowed":
        raise InvalidRepositoryStateError(
            f"Cannot recover retained-output authority settlement {pending.settlement_operation_id!r}: "
            f"non-allowed pending record requests {pending.transaction_kind}"
        )
    settling_operation_id = _retained_pending_settling_operation_id(pending)
    if settling_operation_id is not None and owner._store.operation_id_exists(settling_operation_id):
        recovered = update_pending_authority_settlement(
            owner,
            pending,
            phase="adopted",
            commit_outcome=positive,
            reason_code=adopted_reason,
        )
    else:
        recovered = update_pending_authority_settlement(
            owner,
            pending,
            phase="discarded",
            settlement=negative,
            commit_outcome="commit_failed_non_authority",
            reason_code=discarded_reason,
        )
    return recovered


def _pending_scope(owner: VcsCore, pending: PendingAuthoritySettlement) -> ScopeInfo:
    return _scope_from_pending(
        owner,
        name=pending.scope_name,
        ref=pending.scope_ref,
        instance_id=pending.scope_instance_id,
        world_id=pending.scope_world_id,
    )


def _pending_parent_scope(owner: VcsCore, pending: PendingAuthoritySettlement) -> ScopeInfo:
    return _scope_from_pending(
        owner,
        name=pending.parent_scope_name,
        ref=pending.parent_scope_ref,
        instance_id=pending.parent_scope_instance_id,
        world_id=pending.parent_scope_world_id,
    )


def _scope_from_pending(
    owner: VcsCore,
    *,
    name: str,
    ref: str,
    instance_id: str,
    world_id: str | None,
) -> ScopeInfo:
    if owner._ground is not None and name == owner._ground.name and ref == owner._ground.ref:
        return owner._ground
    active = owner._active_scopes.get(name)
    if active is not None and active.ref == ref and active.instance_id == instance_id:
        return active
    return ScopeInfo(name=name, ref=ref, instance_id=instance_id, creation_oid="", world_id=world_id)


def _authority_safe_discard_locked(owner: VcsCore, scope: ScopeInfo, parent: ScopeInfo) -> str:
    _begin_lifecycle_run(owner, operation="discard", phase="discard_substrates", scope=scope, parent=parent)
    discard_failures: list[tuple[str, Exception]] = []
    for substrate in reversed(owner._lifecycle_substrates):
        if not hasattr(substrate, "discard"):
            continue
        substrate_name = _lifecycle_substrate_name(substrate)
        try:
            cast("Any", substrate).discard(scope.name)
            _mark_completed_substrate(owner, substrate_name)
        except Exception as exc:  # noqa: BLE001
            discard_failures.append((substrate_name, exc))
            logger.warning(
                "Substrate %s raised during authority-safe discard of scope %r; continuing cleanup",
                substrate_name,
                scope.name,
                exc_info=True,
            )
    if discard_failures:
        failed = ", ".join(name for name, _error in discard_failures)
        msg = f"Authority-safe discard of scope {scope.name!r} failed in substrate(s): {failed}."
        raise RuntimeError(msg) from discard_failures[0][1]
    return _complete_discard_locked(owner, scope, parent)


def discard(owner: VcsCore, scope: ScopeInfo) -> str:
    scope_name: str
    with owner._lock:
        _admission(owner).require_lifecycle_mutation_allowed("discard")
        owner._validate_scope(scope)
        scope = owner._live_scope(scope)
        parent = owner._scope_parents[scope.name]
        _begin_lifecycle_run(owner, operation="discard", phase="prepare_discard_effects", scope=scope, parent=parent)
        _snapshot_discard_effects_locked(owner, scope, parent)
        _update_lifecycle_run(owner, phase="discard_substrates")
        discard_failures: list[tuple[str, Exception]] = []
        for substrate in reversed(owner._lifecycle_substrates):
            if hasattr(substrate, "discard"):
                substrate_name = _lifecycle_substrate_name(substrate)
                try:
                    cast("Any", substrate).discard(scope.name)
                    _mark_completed_substrate(owner, substrate_name)
                except Exception as exc:  # noqa: BLE001
                    discard_failures.append((substrate_name, exc))
                    logger.warning(
                        "Substrate %s raised during discard of scope %r; continuing cleanup",
                        getattr(substrate, "name", substrate),
                        scope.name,
                        exc_info=True,
                    )
        if discard_failures:
            failed = ", ".join(name for name, _error in discard_failures)
            msg = (
                f"Discard of scope {scope.name!r} failed in substrate(s): {failed}. Scope remains active for recovery."
            )
            raise RuntimeError(msg) from discard_failures[0][1]

        scope_name = _complete_discard_locked(owner, scope, parent)

    _run_discard_callbacks(owner, scope_name)
    return scope_name


def seal(owner: VcsCore, scope: ScopeInfo, *, output_binding: str | None = None) -> SealResult:
    with owner._lock:
        _admission(owner).require_lifecycle_mutation_allowed("seal")
        owner._validate_scope(scope)
        scope = owner._live_scope(scope)
        parent = owner._scope_parents[scope.name]
        if owner._scope_registry_mismatches:
            raise InvalidRepositoryStateError("Cannot seal scope while scope-registry mismatches are present.")
        _snapshot_seal_effects_locked(owner, scope, parent)
        prepared = owner._seal.prepare_seal_handoff(scope=scope, parent=parent, output_binding=output_binding)
        _begin_lifecycle_run(owner, operation="seal", phase="seal_handoff", scope=scope, parent=parent)
        return _complete_seal_locked(owner, scope, parent, prepared=prepared, output_binding=output_binding)


def archive_orphaned_scopes(owner: VcsCore, *, exclude_refs: Collection[str] = ()) -> list[str]:
    with owner._lock:
        from vcs_core._recovery_inventory import scope_ref_recovery_classification

        _admission(owner).require_recovery_cleanup_allowed("archive orphaned scopes")
        excluded = set(exclude_refs)
        cleanup_classification = scope_ref_recovery_classification(
            owner._store,
            owner._repo_path,
            mismatches=tuple(owner._scope_registry_mismatches),
        )
        allowed_mismatch_item_ids = cleanup_classification.reclaimable_mismatch_item_ids
        recovery_refs = {ref for ref in owner._orphaned_refs if ref not in excluded}
        blocked_mismatch_item_ids = cleanup_classification.non_reclaimable_mismatch_item_ids
        if not recovery_refs and blocked_mismatch_item_ids:
            blocked_labels = sorted(
                str(item.fields.get("scope_name") or item.fields.get("ref") or item.locator or item.id)
                for item in owner.recovery_inventory().items
                if item.id in blocked_mismatch_item_ids
            )
            detail = ", ".join(blocked_labels[:5]) or "scope registry mismatch"
            raise InvalidRepositoryStateError(f"Cannot archive orphaned scopes: readiness blocked by {detail}.")
        require_recovery_targets_allowed(
            owner,
            attempted="archive orphaned scopes",
            targets=(
                *recovery_targets_for_scope_refs(owner, recovery_refs),
                *recovery_operation_targets_for_scope_refs(owner, recovery_refs),
                *workspace_authority_recovery_targets(owner, scope_refs=recovery_refs),
            ),
            allowed_blocker_item_ids=allowed_mismatch_item_ids,
        )
        archived: list[str] = []
        remaining_refs: list[str] = []
        for ref in owner._orphaned_refs:
            if ref in excluded:
                continue
            scope_operations = [operation for operation in owner._orphaned_operations if operation.scope_ref == ref]
            if scope_operations:
                failures = _archive_orphaned_operations_locked(owner, scope_operations)
                if failures:
                    logger.warning(
                        "Skipping orphaned scope ref %s because %d child operation ref(s) could not be archived.",
                        ref,
                        len(failures),
                    )
                    remaining_refs.append(ref)
                    continue
            try:
                scope = _orphaned_scope_info(owner, ref)
                if owner._store.ref_exists(ref):
                    owner._store.discard(scope)
                _publish_scope_registry_status_locked(owner, scope=scope, status="discarded")
                owner._discard_v2_scope_world(scope)
                clear_pending_workspace_authority_for_scope(owner._repo_path, ref)
                archived.append(scope.name)
            except Exception:  # noqa: BLE001
                remaining_refs.append(ref)
                logger.warning(
                    "Failed to archive orphaned scope ref %s",
                    ref,
                    exc_info=True,
                )
        owner._orphaned_refs = remaining_refs
        return archived


def _orphaned_scope_info(owner: VcsCore, ref: str) -> ScopeInfo:
    snapshot = owner._store.load_scope_registry_projection()
    if snapshot is not None:
        entry = snapshot.entries_by_ref.get(ref)
        if entry is not None:
            return ScopeInfo(
                name=entry.name,
                ref=entry.ref,
                instance_id=entry.instance_id,
                creation_oid=entry.creation_oid,
                world_id=entry.world_id,
            )
    name = ref.rsplit("/", 1)[-1]
    return ScopeInfo(
        name=name,
        ref=ref,
        instance_id=f"orphan-{name}",
        creation_oid="",
    )


def _auto_recover_orphaned_operations(owner: VcsCore) -> None:
    """Reclaim orphaned operation refs left by a dead prior session, at activation.

    Safe by construction: activation has already acquired the cross-process session
    lock (``acquire_session_lock``), which reclaims a *dead* owner's lock but refuses
    while a *live* session holds it. So reaching here means no live session owns the
    repo, and every open operation ref is from a crashed/killed prior run whose
    unpublished world state the reversible substrate never committed — bookkeeping,
    not lost work.

    Recovery reuses the same guarded path as the manual
    ``archive_orphaned_operations`` (``owner._lock`` is re-entrant, so the nested
    acquire is fine). That path fails closed on an interrupted lifecycle, a
    sibling-group blocker, or an entangled orphaned scope; on any such block this
    leaves the orphan in place, so the caller still gets today's detect-and-refuse
    behavior rather than a surprise. It is loud on success — the archived operation
    journal is durable, and the reclamation is logged — so recurring auto-recovery
    (a symptom of repeated crashes) stays visible rather than silently smoothed over.
    """
    labels = ", ".join(owner._format_operation_label(op) for op in owner._orphaned_operations)
    try:
        recovered = archive_orphaned_operations(owner)
    except Exception:  # noqa: BLE001 — auto-recovery must never turn a recoverable wedge
        # into an activation failure; fall back to detect-and-refuse.
        logger.warning(
            "Auto-recovery of orphaned operation refs was declined (recovery is blocked by "
            "other pending state); leaving them for explicit archive_orphaned_operations(). "
            "Refs: %s",
            labels,
            exc_info=True,
        )
        return
    if recovered:
        logger.warning(
            "Auto-recovered %d orphaned operation ref(s) from a dead prior session: %s. "
            "A prior run was interrupted; its unpublished state was discarded and the run "
            "journal archived.",
            len(recovered),
            ", ".join(recovered),
        )


def archive_orphaned_operations(owner: VcsCore) -> list[str]:
    with owner._lock:
        _admission(owner).require_recovery_cleanup_allowed("archive orphaned operations")
        operation_scope_refs = {operation.scope_ref for operation in owner._orphaned_operations}
        require_recovery_targets_allowed(
            owner,
            attempted="archive orphaned operations",
            targets=(
                *recovery_targets_for_kinds(owner, "orphaned_operation_ref"),
                *recovery_targets_for_scope_refs(owner, operation_scope_refs),
            ),
        )
        candidates = list(owner._orphaned_operations)
        failures = _archive_orphaned_operations_locked(owner, candidates)
        failed_refs = {operation.ref for operation in failures}
        return [owner._format_operation_label(op) for op in candidates if op.ref not in failed_refs]


def list_orphaned_scope_refs(owner: VcsCore) -> tuple[str, ...]:
    return tuple(owner._orphaned_refs)


def list_orphaned_operations(owner: VcsCore) -> tuple[OperationSummary, ...]:
    return _orphaned_operation_summaries(owner)


def on_merge(owner: VcsCore, callback: Callable[[str], None]) -> None:
    owner._merge_callbacks.append(callback)


def on_discard(owner: VcsCore, callback: Callable[[str], None]) -> None:
    owner._discard_callbacks.append(callback)


def _orphaned_operation_summaries(owner: VcsCore) -> tuple[OperationSummary, ...]:
    summaries: list[OperationSummary] = []
    for operation in owner._orphaned_operations:
        if owner._store.ref_exists(operation.ref):
            summaries.append(owner._store.read_operation_history(operation.ref).summary)
            continue
        summaries.append(
            OperationSummary(
                operation_id=operation.durable_id,
                label=operation.display_label,
                kind=operation.kind,
                status="open",
                visibility="staged",
                world_id=_orphaned_operation_world_id(owner, operation),
                world_name=owner._scope_name_for_ref(operation.scope_ref),
                world_ref=operation.scope_ref,
                carrier_ref=operation.ref,
                parent_operation_id=operation.parent_operation_id,
            )
        )
    return tuple(summaries)


def _orphaned_operation_world_id(owner: VcsCore, operation: OperationRefInfo) -> str:
    if operation.world_id:
        return operation.world_id
    if owner._ground is not None and operation.scope_ref == owner._ground.ref:
        return owner._scope_world_id(owner._ground)
    for scope in owner._active_scopes.values():
        if scope.ref == operation.scope_ref:
            return owner._scope_world_id(scope)
    return "unknown"


def _archive_orphaned_operations_locked(
    owner: VcsCore,
    operations: Sequence[OperationRefInfo],
) -> list[OperationRefInfo]:
    failed: list[OperationRefInfo] = []
    candidate_refs = {operation.ref for operation in operations}
    for operation in operations:
        try:
            owner._store.archive_operation(operation)
        except Exception:  # noqa: BLE001
            failed.append(operation)
            logger.warning(
                "Failed to archive orphaned operation ref %s",
                operation.ref,
                exc_info=True,
            )
    failed_refs = {operation.ref for operation in failed}
    owner._orphaned_operations = [
        operation
        for operation in owner._orphaned_operations
        if operation.ref not in candidate_refs or operation.ref in failed_refs
    ]
    return failed
