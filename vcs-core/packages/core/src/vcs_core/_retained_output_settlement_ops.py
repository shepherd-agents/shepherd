"""Receipt-only settlement operations for retained binding outputs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._retained_output_selection import (
    _recover_current_parent_retained_selection,
    _recover_published_retained_selection,
    _settlement_operation_id,
)
from vcs_core._retained_output_settlement import (
    read_retained_output_settlement,
    retained_output_settlement_ref,
    write_retained_output_settlement,
)
from vcs_core._vcscore_seal import (
    ValidatedRetainedWorkspace,
    _scope_selector,
    _validate_retained_workspace_handle,
    _validated_retained_workspace,
)
from vcs_core._world_types import canonical_digest
from vcs_core.types import (
    RetainedOutputSettlement,
    RetainedOutputSettlementResult,
    RetainedWorkspaceHandle,
    ScopeInfo,
    SealCandidateHandoff,
)

if TYPE_CHECKING:
    from vcs_core._world_storage_manager import WorldStorageManager
    from vcs_core.vcscore import VcsCore

ReceiptOnlyAction = Literal["released", "discarded"]


def release_retained_output(
    owner: VcsCore,
    scope_or_handle: ScopeInfo | RetainedWorkspaceHandle | str,
    *,
    parent: ScopeInfo,
    binding: str = "workspace",
) -> RetainedOutputSettlementResult:
    """Consume a retained binding output as deliberately released."""
    return _settle_retained_output(owner, scope_or_handle, parent=parent, binding=binding, action="released")


def discard_retained_output(
    owner: VcsCore,
    scope_or_handle: ScopeInfo | RetainedWorkspaceHandle | str,
    *,
    parent: ScopeInfo,
    binding: str = "workspace",
) -> RetainedOutputSettlementResult:
    """Consume a retained binding output as deliberately discarded."""
    return _settle_retained_output(owner, scope_or_handle, parent=parent, binding=binding, action="discarded")


def _settle_retained_output(
    owner: VcsCore,
    scope_or_handle: ScopeInfo | RetainedWorkspaceHandle | str,
    *,
    parent: ScopeInfo,
    binding: str,
    action: ReceiptOnlyAction,
) -> RetainedOutputSettlementResult:
    with owner._lock:
        retained = _validated_retained_workspace(owner, _scope_selector(scope_or_handle))
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

        manager = owner._world_storage()
        settlement_ref = _require_receipt_only_settlement_available(
            owner,
            manager,
            retained=retained,
            parent=parent,
            handoff=handoff,
        )
        parent_world_oid = owner._current_v2_world_oid(manager, parent.ref)
        if parent_world_oid is None:
            raise InvalidRepositoryStateError(
                f"Cannot settle retained output {handoff.scope_name!r}: parent has no current v2 world"
            )
        operation_id = _receipt_only_operation_id(handoff=handoff, settlement_ref=settlement_ref, action=action)
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
            action=action,
            operation_id=operation_id,
            parent_world_before=parent_world_oid,
            parent_world_after=parent_world_oid,
            settlement_ref=settlement_ref,
        )
        write_retained_output_settlement(owner.store, settlement)
        return RetainedOutputSettlementResult(
            scope=retained.entry_scope,
            parent=parent,
            output_world_oid=handoff.output_world_oid,
            parent_world_before=parent_world_oid,
            parent_world_after=parent_world_oid,
            settlement=settlement,
        )


def _require_receipt_only_settlement_available(
    owner: VcsCore,
    manager: WorldStorageManager,
    *,
    retained: ValidatedRetainedWorkspace,
    parent: ScopeInfo,
    handoff: SealCandidateHandoff,
) -> str:
    """Return the terminal settlement ref if receipt-only settlement is allowed.

    Receipt-only verbs do not publish a parent world, so they intentionally do
    not run parent-world mutation admission and do not require the parent binding
    to remain fresh against the child fork basis. Their admission floor is
    custody validation, a live matching parent, no existing terminal receipt, and
    recovery of any already-published selected world for this same retained
    output before writing a released/discarded receipt.
    """
    settlement_ref = retained_output_settlement_ref(
        scope_name=handoff.scope_name,
        scope_instance_id=handoff.scope_instance_id,
        binding=handoff.binding,
        candidate_id=handoff.candidate_id,
    )
    if read_retained_output_settlement(owner.store, settlement_ref, missing_ok=True) is not None:
        raise InvalidRepositoryStateError(f"retained output is already settled: {settlement_ref}")

    selected_operation_id = _settlement_operation_id(handoff=handoff, settlement_ref=settlement_ref)
    recovered = _recover_published_retained_selection(
        owner,
        manager,
        retained=retained,
        parent=parent,
        operation_id=selected_operation_id,
        settlement_ref=settlement_ref,
    )
    if recovered is not None:
        raise InvalidRepositoryStateError(f"retained output is already settled: {settlement_ref}")
    recovered = _recover_current_parent_retained_selection(
        owner,
        manager,
        retained=retained,
        parent=parent,
        settlement_ref=settlement_ref,
    )
    if recovered is not None:
        raise InvalidRepositoryStateError(f"retained output is already settled: {settlement_ref}")
    return settlement_ref


def _receipt_only_operation_id(
    *,
    handoff: SealCandidateHandoff,
    settlement_ref: str,
    action: ReceiptOnlyAction,
) -> str:
    digest = canonical_digest(
        {
            "schema": "vcscore/retained-output-receipt-only-operation-id/v1",
            "action": action,
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
    prefix = "release_retained" if action == "released" else "discard_retained"
    return f"{prefix}_{digest[:32]}"
