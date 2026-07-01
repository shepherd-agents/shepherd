"""Coordinator-owned authority for opening operation refs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from vcs_core._capture_reducer import CAPTURE_DIAGNOSTIC_KIND, CAPTURE_REDUCTION_KIND

if TYPE_CHECKING:
    from vcs_core._query_readiness import ReadinessOperationAuthority
    from vcs_core._runtime_types import OperationRefInfo
    from vcs_core.types import ScopeInfo
    from vcs_core.vcscore import VcsCore

AllowlistedOperationStartReason = Literal[
    "not_admitted_shell_command",
    "capture_diagnostic",
    "capture_reduction",
]


def begin_executable_operation(
    owner: VcsCore,
    scope: ScopeInfo,
    *,
    attempted: str,
    handle_id: str,
    kind: str,
    world_id: str,
    scope_instance_id: str,
    operation_id: str,
    operation_label: str,
    session_id: str | None = None,
    metadata: dict[str, object] | None = None,
    authorized_operations: tuple[ReadinessOperationAuthority, ...] = (),
) -> OperationRefInfo:
    """Open a new executable/session operation after coordinator admission."""
    owner._ensure_runtime_mutation_allowed(
        attempted,
        authorized_operations=authorized_operations,
        scope_selector=scope.ref,
    )
    return owner.store.begin_operation(
        scope.ref,
        handle_id=handle_id,
        kind=kind,
        world_id=world_id,
        scope_instance_id=scope_instance_id,
        operation_id=operation_id,
        operation_label=operation_label,
        session_id=session_id,
        metadata=dict(metadata or {}),
    )


def _begin_allowlisted_operation(
    owner: VcsCore,
    scope: ScopeInfo,
    *,
    reason: AllowlistedOperationStartReason,
    handle_id: str,
    kind: str,
    world_id: str,
    scope_instance_id: str,
    operation_id: str,
    operation_label: str,
    session_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> OperationRefInfo:
    """Open diagnostic/reduction evidence that cannot launch executable work."""
    if not reason:
        raise ValueError("allowlisted operation starts require an explicit reason.")
    return owner.store.begin_operation(
        scope.ref,
        handle_id=handle_id,
        kind=kind,
        world_id=world_id,
        scope_instance_id=scope_instance_id,
        operation_id=operation_id,
        operation_label=operation_label,
        session_id=session_id,
        metadata=dict(metadata or {}),
    )


def begin_not_admitted_shell_command_operation(
    owner: VcsCore,
    scope: ScopeInfo,
    *,
    handle_id: str,
    world_id: str,
    scope_instance_id: str,
    operation_id: str,
    operation_label: str,
    session_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> OperationRefInfo:
    """Open and immediately terminalize a shell command that admission rejected."""
    return _begin_allowlisted_operation(
        owner,
        scope,
        reason="not_admitted_shell_command",
        handle_id=handle_id,
        kind="vcs_core.session_exec",
        world_id=world_id,
        scope_instance_id=scope_instance_id,
        operation_id=operation_id,
        operation_label=operation_label,
        session_id=session_id,
        metadata=metadata,
    )


def begin_capture_diagnostic_operation(
    owner: VcsCore,
    scope: ScopeInfo,
    *,
    handle_id: str,
    world_id: str,
    scope_instance_id: str,
    operation_id: str,
    operation_label: str,
    session_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> OperationRefInfo:
    """Open a capture diagnostic operation for rejected capture evidence."""
    return _begin_allowlisted_operation(
        owner,
        scope,
        reason="capture_diagnostic",
        handle_id=handle_id,
        kind=CAPTURE_DIAGNOSTIC_KIND,
        world_id=world_id,
        scope_instance_id=scope_instance_id,
        operation_id=operation_id,
        operation_label=operation_label,
        session_id=session_id,
        metadata=metadata,
    )


def begin_capture_reduction_operation(
    owner: VcsCore,
    scope: ScopeInfo,
    *,
    handle_id: str,
    world_id: str,
    scope_instance_id: str,
    operation_id: str,
    operation_label: str,
    session_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> OperationRefInfo:
    """Open a reducer operation that terminalizes already captured command evidence."""
    return _begin_allowlisted_operation(
        owner,
        scope,
        reason="capture_reduction",
        handle_id=handle_id,
        kind=CAPTURE_REDUCTION_KIND,
        world_id=world_id,
        scope_instance_id=scope_instance_id,
        operation_id=operation_id,
        operation_label=operation_label,
        session_id=session_id,
        metadata=metadata,
    )
