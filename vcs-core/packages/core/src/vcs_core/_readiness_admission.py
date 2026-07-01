"""Compatibility exception projection for readiness-backed admission."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from vcs_core._authority_inventory import (
    authority_settlement_pending_label,
    probe_authority_settlement_pending,
)
from vcs_core._errors import (
    InvalidRepositoryStateError,
    OpenScopeError,
    OrphanedOperationsError,
    SiblingGroupRecoveryRequiredError,
    WorkspaceAuthorityRecoveryRequiredError,
    WorldQuiescenceError,
)
from vcs_core._query_readiness import (
    ReadinessOperationAuthority,
    ReadinessRequest,
    ReadinessTarget,
)
from vcs_core._workspace_authority_inventory import probe_workspace_authority_pending, workspace_authority_pending_label

if TYPE_CHECKING:
    from collections.abc import Collection

    from vcs_core._query_inventory import InventoryItem
    from vcs_core._query_readiness import ReadinessResult, RuntimeAdmissionContext
    from vcs_core.store import Store
    from vcs_core.vcscore import VcsCore


def require_readiness_allowed(
    owner: VcsCore,
    *,
    command: str,
    attempted: str,
    authorized_operations: tuple[ReadinessOperationAuthority,...] = (),
    scope_selector: str | None = None,
    runtime_admission_context: RuntimeAdmissionContext | None = None,
) -> ReadinessResult:
    """Require command readiness and raise legacy-compatible errors on failure."""
    request = ReadinessRequest.create(
        command=command,
        requested_freshness="locked",
        allow_best_effort=False,
        authorized_operations=authorized_operations,
        scope=_scope_selector_for_admission(owner, command=command, scope_selector=scope_selector),
    )
    if runtime_admission_context is None:
        result = owner.query_readiness(request)
    else:
        result = owner._query_readiness_for_runtime(request, runtime_admission_context=runtime_admission_context)
    if runtime_admission_context is not None and runtime_admission_context.allowed_blocker_item_ids and result.blockers:
        allowed = set(runtime_admission_context.allowed_blocker_item_ids)
        blockers = tuple(blocker for blocker in result.blockers if blocker.item_id not in allowed)
        if len(blockers) != len(result.blockers):
            result = replace(
                result,
                blockers=blockers,
                state="blocked" if blockers else "safe_to_run",
                allowed=not blockers,
                admission_authoritative=True,
                mutation_precondition=None,
            )
    if result.allowed:
        return result
    raise _with_readiness_result(_exception_from_readiness(result, attempted=attempted), result)


def _scope_selector_for_admission(owner: VcsCore, *, command: str, scope_selector: str | None) -> str | None:
    if command != "vcscore.runtime" or scope_selector is not None:
        return scope_selector
    current_operation = owner._pipeline.current_operation()
    if current_operation is not None:
        return current_operation.scope_ref
    current_scope = owner._pipeline.context.world
    if current_scope is not None:
        return current_scope.ref
    return None


def require_recovery_targets_allowed(
    owner: VcsCore,
    *,
    attempted: str,
    targets: tuple[ReadinessTarget,...],
    allowed_blocker_item_ids: Collection[str] = (),
) -> ReadinessResult | None:
    """Require targeted recovery readiness for concrete current recovery facts."""
    if not targets:
        return None
    result = owner.query_readiness(
        ReadinessRequest.create(
            command="vcscore.recover",
            requested_freshness="locked",
            allow_best_effort=False,
            targets=targets,
        )
    )
    if allowed_blocker_item_ids and result.blockers:
        blockers = tuple(blocker for blocker in result.blockers if blocker.item_id not in allowed_blocker_item_ids)
        if len(blockers) != len(result.blockers):
            result = replace(
                result,
                blockers=blockers,
                state="blocked" if blockers else "safe_to_run",
                allowed=not blockers,
                admission_authoritative=True,
                mutation_precondition=None,
            )
    if result.allowed:
        return result
    raise _with_readiness_result(_exception_from_readiness(result, attempted=attempted), result)


def workspace_authority_recovery_targets(
    owner: VcsCore,
    *,
    scope_refs: set[str] | None = None,
    operation_ids: set[str] | None = None,
) -> tuple[ReadinessTarget,...]:
    """Workspace-authority recovery targets for present pending items.

    When ``scope_refs`` is provided, restrict to items whose ``scope_ref`` is in
    that set; when ``operation_ids`` is provided, restrict to matching operation
    ids. Otherwise return every present pending item.
    """
    return tuple(
        _workspace_authority_target_for_item(item)
        for item in probe_workspace_authority_pending(owner._repo_path)
        if item.health.presence == "present"
        and (scope_refs is None or item.fields.get("scope_ref") in scope_refs)
        and (
            operation_ids is None
            or item.fields.get("operation_id") in operation_ids
            or item.fields.get("payload_operation_id") in operation_ids
        )
    )


def authority_settlement_recovery_targets(owner: VcsCore) -> tuple[ReadinessTarget,...]:
    return tuple(
        _authority_settlement_target_for_item(item)
        for item in probe_authority_settlement_pending(owner._repo_path)
        if item.health.presence == "present"
    )


def workspace_authority_related_recovery_targets(owner: VcsCore) -> tuple[ReadinessTarget,...]:
    scope_refs = {
        scope_ref
        for item in probe_workspace_authority_pending(owner._repo_path)
        if item.health.presence == "present" and isinstance((scope_ref:= item.fields.get("scope_ref")), str)
    }
    return tuple(
        _recovery_target_for_item(item)
        for item in owner.recovery_inventory().items
        if item.domain == "recovery"
        and item.health.presence == "present"
        and (
            (item.kind == "orphaned_scope_ref" and item.locator in scope_refs)
            or (item.kind == "orphaned_operation_ref" and item.fields.get("scope_ref") in scope_refs)
        )
    )


def workspace_authority_operation_journal_recovery_targets(owner: VcsCore) -> tuple[ReadinessTarget,...]:
    from vcs_core._operation_journal_inventory import admission_operation_journal_items
    from vcs_core._world_storage_installation import default_world_storage_exists, open_existing_default_world_storage

    operation_ids = {
        operation_id
        for item in probe_workspace_authority_pending(owner._repo_path)
        if item.health.presence == "present"
        and isinstance((operation_id:= item.fields.get("operation_id")), str)
        and operation_id
    }
    if not operation_ids or not default_world_storage_exists(owner._repo_path):
        return ()
    try:
        manager = open_existing_default_world_storage(owner._repo_path)
    except Exception: # noqa: BLE001
        return ()
    return tuple(
        _operation_journal_target_for_item(item)
        # Consumer audit: recovery commands mutate AND need the open set for targeting, so they must
        # read the BOUNDED index-backed source — not keep a probe_operation_journals(family="open")
        # scan that would defeat the admission count-contract (260622-admission-tier-open-ops-index.md,
        # Part B). The corrupt-index fact this source can emit is filtered by _operation_journal_needs_recovery.
        for item in admission_operation_journal_items(manager)
        if _operation_journal_needs_recovery(item)
        and (
            _field_str(item, "operation_id") in operation_ids
            or _field_str(item, "payload_operation_id") in operation_ids
        )
    )


def recovery_targets_for_kinds(owner: VcsCore, *kinds: str) -> tuple[ReadinessTarget,...]:
    selected_kinds = set(kinds)
    return tuple(
        _recovery_target_for_item(item)
        for item in owner.recovery_inventory().items
        if item.domain == "recovery" and item.kind in selected_kinds and item.health.presence == "present"
    )


def recovery_targets_for_scope_refs(owner: VcsCore, refs: set[str]) -> tuple[ReadinessTarget,...]:
    return tuple(
        _recovery_target_for_item(item)
        for item in owner.recovery_inventory().items
        if item.domain == "recovery"
        and item.kind == "orphaned_scope_ref"
        and item.locator in refs
        and item.health.presence == "present"
    )


def recovery_operation_targets_for_scope_refs(owner: VcsCore, refs: set[str]) -> tuple[ReadinessTarget,...]:
    return tuple(
        _recovery_target_for_item(item)
        for item in owner.recovery_inventory().items
        if item.domain == "recovery"
        and item.kind == "orphaned_operation_ref"
        and item.fields.get("scope_ref") in refs
        and item.health.presence == "present"
    )


def _workspace_authority_target_for_item(item: InventoryItem) -> ReadinessTarget:
    return ReadinessTarget(
        domain="workspace_authority",
        item_id=item.id,
        operation_id=_field_str(item, "operation_id") or _field_str(item, "payload_operation_id"),
    )


def _authority_settlement_target_for_item(item: InventoryItem) -> ReadinessTarget:
    return ReadinessTarget(
        domain="authority_settlement",
        kind=item.kind,
        item_id=item.id,
    )


def _recovery_target_for_item(item: InventoryItem) -> ReadinessTarget:
    return ReadinessTarget(
        domain="recovery",
        kind=item.kind,
        item_id=item.id,
        locator=item.locator,
        operation_id=_field_str(item, "operation_id"),
    )


def _operation_journal_target_for_item(item: InventoryItem) -> ReadinessTarget:
    return ReadinessTarget(
        domain="operation_journal",
        kind=item.kind,
        item_id=item.id,
        locator=item.locator,
        operation_id=_field_str(item, "operation_id") or _field_str(item, "payload_operation_id"),
        family=_field_str(item, "family"),
    )


def _operation_journal_needs_recovery(item: InventoryItem) -> bool:
    if item.domain != "operation_journal" or item.health.presence != "present":
        return False
    status = item.fields.get("status")
    return item.health.lifecycle == "active" or status in {"failed", "recovery_required"}


def exclude_active_daemon_leases(owner: VcsCore, result: ReadinessResult) -> ReadinessResult:
    """Exclude the live daemon's own open shell-capture leases from orphaned-op blockers.

    Ungated by command class: a lease held by the *current* daemon is the
    legitimate context commands run within — not an orphaned operation. A
    crashed prior session's lease carries a different
    ``daemon_instance_id`` and correctly stays blocked. Applied at
    ``VcsCore.query_readiness``, the single point both the runtime-enforcement
    path (``require_readiness_allowed``) and the lifecycle blocker-derivation
    path (``_app._readiness_blockers``) route through.
    """
    daemon_instance_id = getattr(owner, "_active_daemon_instance_id", None)
    if daemon_instance_id is None:
        return result
    lease_ids = _active_daemon_shell_lease_ids(owner.store, daemon_instance_id)
    if not lease_ids:
        return result
    items_by_id = {item.id: item for item in result.snapshot.items}
    blockers = tuple(
        blocker
        for blocker in result.blockers
        if not _is_active_daemon_lease(items_by_id.get(blocker.item_id), lease_ids)
    )
    excluded = tuple(sorted(lease_ids))
    if len(blockers) == len(result.blockers):
        return replace(result, excluded_daemon_lease_ids=excluded)
    if blockers:
        return replace(result, blockers=blockers, excluded_daemon_lease_ids=excluded)
    return replace(
        result,
        blockers=(),
        allowed=True,
        state="safe_to_run",
        admission_authoritative=True,
        excluded_daemon_lease_ids=excluded,
    )


def _active_daemon_shell_lease_ids(store: Store, daemon_instance_id: str) -> frozenset[str]:
    """Operation ids of the given daemon's open ``vcs_core.session_shell`` leases.

    Daemon-scoped via the lease's start-metadata ``shell.daemon_instance_id``
    tag (written at lease open). Reads start metadata per open lease — in
    practice one lease at a time. (v0.2: surface ``daemon_instance_id`` onto
    ``OperationRefInfo`` so ``list_open_operations`` carries it and this
    per-lease read disappears.)
    """
    ids: set[str] = set()
    for operation in store.list_open_operations():
        if operation.kind != "vcs_core.session_shell":
            continue
        start_metadata = store._read_operation_start_metadata(operation.ref)
        shell = start_metadata.get("shell")
        if isinstance(shell, dict) and shell.get("daemon_instance_id") == daemon_instance_id:
            ids.add(operation.durable_id)
    return frozenset(ids)


def _is_active_daemon_lease(item: InventoryItem | None, lease_ids: frozenset[str]) -> bool:
    if item is None or item.domain != "recovery" or item.kind != "orphaned_operation_ref":
        return False
    return item.fields.get("operation_id") in lease_ids


def _exception_from_readiness(result: ReadinessResult, *, attempted: str) -> Exception:
    blocked_items = _blocked_items(result)
    workspace_authority = tuple(item for item in blocked_items if item.domain == "workspace_authority")
    if workspace_authority:
        return WorkspaceAuthorityRecoveryRequiredError(
            attempted=attempted,
            operations=[workspace_authority_pending_label(item) for item in workspace_authority],
        )
    authority_settlements = tuple(item for item in blocked_items if item.domain == "authority_settlement")
    if authority_settlements:
        operations = ", ".join(authority_settlement_pending_label(item) for item in authority_settlements)
        return InvalidRepositoryStateError(
            f"{attempted} blocked by pending authority settlement recovery: {operations}. "
            "Run recover_authority_settlements() before starting mutating work."
        )
    orphaned_operations = tuple(
        item for item in blocked_items if item.domain == "recovery" and item.kind == "orphaned_operation_ref"
    )
    if orphaned_operations:
        return OrphanedOperationsError(
            attempted=attempted,
            operations=[
                _field_str(item, "operation_label") or _field_str(item, "operation_id") or item.id
                for item in orphaned_operations
            ],
        )
    sibling_groups = tuple(
        item for item in blocked_items if item.domain == "recovery" and item.kind == "sibling_group_blocker"
    )
    if sibling_groups:
        return SiblingGroupRecoveryRequiredError(
            attempted=attempted,
            groups=[_field_str(item, "label") or _field_str(item, "group_id") or item.id for item in sibling_groups],
        )
    orphaned_scopes = tuple(
        item for item in blocked_items if item.domain == "recovery" and item.kind == "orphaned_scope_ref"
    )
    if orphaned_scopes:
        names = [
            (_field_str(item, "scope_name") or (item.locator or item.id).rsplit("/", 1)[-1]) for item in orphaned_scopes
        ]
        msg = (
            f"{attempted} blocked by {len(orphaned_scopes)} orphaned scope ref(s) "
            f"from a prior session: {', '.join(names)}. "
            "Call archive_orphaned_scopes() to clean up."
        )
        return OpenScopeError(msg)
    nested_quiescence = tuple(
        item for item in blocked_items if item.domain == "operation" and item.kind == "nested_child_quiescence"
    )
    if nested_quiescence:
        item = nested_quiescence[0]
        fields = item.fields
        child_scope = fields.get("scope_ref") or "unknown child scope"
        parent_scope = fields.get("requested_scope_ref") or "unknown parent scope"
        operation_id = fields.get("operation_id") or item.locator or item.id
        disposition = fields.get("world_disposition") or "adopt"
        return WorldQuiescenceError(
            f"Cannot {attempted}: live child operation {operation_id} on {child_scope} "
            f"has world disposition {disposition!r} and blocks parent mutation on {parent_scope}. "
            "Finish or archive the child operation before mutating its parent, or discard the child scope and fork fresh."
        )
    details = ", ".join(_blocked_item_label(item) for item in blocked_items[:5]) or "unknown readiness blocker"
    remainder = len(blocked_items) - min(len(blocked_items), 5)
    suffix = f", and {remainder} more" if remainder > 0 else ""
    return InvalidRepositoryStateError(f"Cannot {attempted}: readiness blocked by {details}{suffix}.")


def _with_readiness_result(exc: Exception, result: ReadinessResult) -> Exception:
    exc._vcscore_readiness_result = result # type: ignore[attr-defined]
    exc._vcscore_readiness_issue_ids = tuple( # type: ignore[attr-defined]
        blocker.issue_id for blocker in result.blockers if blocker.issue_id
    )
    return exc


def _blocked_items(result: ReadinessResult) -> tuple[InventoryItem,...]:
    items_by_id = {item.id: item for item in result.snapshot.items}
    return tuple(item for blocker in result.blockers if (item:= items_by_id.get(blocker.item_id)) is not None)


def _blocked_item_label(item: InventoryItem) -> str:
    if item.domain == "operation_journal":
        return _field_str(item, "operation_id") or _field_str(item, "payload_operation_id") or item.id
    if item.domain == "recovery":
        return (
            _field_str(item, "label")
            or _field_str(item, "operation_label")
            or _field_str(item, "scope_name")
            or item.id
        )
    return item.locator or item.id


def _field_str(item: InventoryItem, key: str) -> str | None:
    value = item.fields.get(key)
    return value if isinstance(value, str) and value else None
