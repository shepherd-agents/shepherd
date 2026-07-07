"""Retained-output custody helpers for workspace-control RunOutput values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from shepherd_dialect.workspace_control.errors import WorkspaceControlError
from shepherd_dialect.workspace_control.run_outputs import RunOutput

if TYPE_CHECKING:
    from shepherd2.schemas.run_outputs import RunOutputRef
    from vcs_core.types import RetainedWorkspaceHandle, ScopeInfo


@dataclass(frozen=True)
class _RetainedRunOutputSettlementRequest:
    output: RunOutputRef
    handle: RetainedWorkspaceHandle
    parent: ScopeInfo
    binding: str


def _validated_retained_run_output_settlement_request(
    workspace: Any,
    output: Any,
) -> _RetainedRunOutputSettlementRequest:
    from shepherd2.schemas.run_outputs import RunOutputRef

    if not isinstance(output, RunOutput):
        raise WorkspaceControlError("run-output settlement requires a RunOutput from this workspace")
    if output._workspace is not workspace:
        raise WorkspaceControlError("run-output settlement requires an output from this workspace")
    ref = output.ref
    if not isinstance(ref, RunOutputRef):
        raise WorkspaceControlError("run-output settlement requires a resolved RunOutputRef")
    if ref.owner.kind != "run" or ref.owner.run_id is None:
        raise WorkspaceControlError("run-output settlement requires a run-owned output")
    if ref.state != "unconsumed":
        raise WorkspaceControlError(f"run-output settlement requires an unconsumed output; got {ref.state!r}")
    if ref.descriptor.materialization_kind != "tree":
        raise WorkspaceControlError("run-output settlement currently requires a tree materialization")
    identity = ref.identity
    parent = _live_parent_scope_for_run_output(workspace.mg, ref)
    return _RetainedRunOutputSettlementRequest(
        output=ref,
        handle=_retained_workspace_handle_for_run_output(ref),
        parent=parent,
        binding=identity.binding,
    )


def _validate_retained_run_output_read_handle(workspace: Any, output: RunOutputRef) -> None:
    validator = getattr(workspace.mg, "retained_workspace_handoff", None)
    if not callable(validator):
        raise WorkspaceControlError("VcsCore.retained_workspace_handoff is required for run-output reads")
    validator(_retained_workspace_handle_for_run_output(output))


def _retained_workspace_handle_for_run_output(output: RunOutputRef) -> RetainedWorkspaceHandle:
    from vcs_core.types import RetainedWorkspaceHandle

    identity = output.identity
    return RetainedWorkspaceHandle(
        scope_name=identity.scope_name,
        scope_ref=identity.scope_ref,
        scope_instance_id=identity.scope_instance_id,
        output_world_oid=identity.output_world_oid,
        binding=identity.binding,
        store_id=output.store_id,
        resource_id=output.resource_id,
        head=identity.candidate_head,
        basis_ref=identity.handoff_ref,
        changed_paths=output.changed_paths,
    )


def _live_parent_scope_for_run_output(mg: Any, output: RunOutputRef) -> ScopeInfo:
    identity = output.identity
    ground = getattr(mg, "ground", None)
    if ground is not None and identity.parent_ref == getattr(ground, "ref", None):
        if identity.parent_scope_name != getattr(ground, "name", None) or identity.parent_scope_instance_id is not None:
            raise WorkspaceControlError("run-output parent identity disagrees with workspace ground scope")
        return ground

    lookup_scope = getattr(mg, "lookup_scope", None)
    parent = lookup_scope(identity.parent_scope_name) if callable(lookup_scope) else None
    if (
        parent is not None
        and getattr(parent, "ref", None) == identity.parent_ref
        and getattr(parent, "instance_id", None) == identity.parent_scope_instance_id
    ):
        return parent
    raise WorkspaceControlError("run-output parent scope is not live in this workspace")
