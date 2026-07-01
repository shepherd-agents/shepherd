"""Read-only retained-output inventory backed by seal and settlement facts."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._retained_output_selection import _validate_retained_selection_world
from vcs_core._retained_output_settlement import read_retained_output_settlement, retained_output_settlement_ref
from vcs_core._retained_output_settlement_ops import ReceiptOnlyAction, _receipt_only_operation_id
from vcs_core._vcscore_seal import (
    ValidatedRetainedWorkspace,
    _require_public_retained_read_allowed,
    _validated_retained_workspace,
)
from vcs_core.store import GROUND_REF
from vcs_core.types import (
    RetainedOutputIdentity,
    RetainedOutputQueryResult,
    RetainedOutputSettlement,
    RetainedOutputState,
    ScopeInfo,
    SealCandidateHandoff,
)

if TYPE_CHECKING:
    from vcs_core._projection_store import ScopeRegistryEntry, ScopeRegistrySnapshot
    from vcs_core.vcscore import VcsCore

_VALID_STATES = frozenset({"unconsumed", "selected", "applied", "released", "discarded", "invalid"})


def list_retained_outputs(
    owner: VcsCore,
    *,
    parent: ScopeInfo | str | None = None,
    binding: str | None = None,
    state: RetainedOutputState | None = None,
) -> tuple[RetainedOutputQueryResult, ...]:
    """Classify retained outputs from registry, seal handoff, and settlement refs.

    This is intentionally a lower-layer query. It does not consult skeleton trace
    metadata, and it does not maintain a second query ledger.
    """
    if state is not None and state not in _VALID_STATES:
        raise ValueError(f"unsupported retained-output state filter: {state!r}")
    with owner._lock:
        _require_public_retained_read_allowed(owner)
        registry = owner.store.require_scope_registry_projection()
        parent_ref = _resolve_parent_ref(owner, parent)
        rows: list[RetainedOutputQueryResult] = []
        for entry in registry.entries:
            if entry.status != "retained":
                continue
            if parent_ref is not None and entry.parent_ref != parent_ref:
                continue
            row = _query_retained_entry(owner, registry, entry, binding=binding)
            if row is None:
                continue
            if state is None or row.state == state:
                rows.append(row)
        return tuple(rows)


def get_retained_output(
    owner: VcsCore,
    identity: RetainedOutputIdentity,
) -> RetainedOutputQueryResult | None:
    """Classify one retained output by exact retained custody identity."""
    if not isinstance(identity, RetainedOutputIdentity):
        raise TypeError("retained-output direct lookup requires RetainedOutputIdentity")
    with owner._lock:
        _require_public_retained_read_allowed(owner)
        registry = owner.store.require_scope_registry_projection()
        entry = registry.entries_by_ref.get(identity.scope_ref)
        if entry is None:
            return None
        _validate_identity_entry(identity, entry)
        row = _query_retained_entry(owner, registry, entry, binding=identity.binding)
        if row is None:
            return None
        _validate_identity_row(identity, row)
        return row


def _query_retained_entry(
    owner: VcsCore,
    registry: ScopeRegistrySnapshot,
    entry: ScopeRegistryEntry,
    *,
    binding: str | None,
) -> RetainedOutputQueryResult | None:
    scope = owner.store.scope_info_from_registry_entry(entry)
    parent_scope_name, parent_scope_instance_id = _parent_identity_from_ref(owner, registry, entry.parent_ref)
    try:
        retained = _validated_retained_workspace(owner, scope)
    except InvalidRepositoryStateError as exc:
        return _invalid_result(
            entry,
            str(exc),
            parent_scope_name=parent_scope_name,
            parent_scope_instance_id=parent_scope_instance_id,
        )

    handoff = retained.loaded.handoff
    if binding is not None and handoff.binding != binding:
        return None
    settlement_ref = retained_output_settlement_ref(
        scope_name=handoff.scope_name,
        scope_instance_id=handoff.scope_instance_id,
        binding=handoff.binding,
        candidate_id=handoff.candidate_id,
    )
    try:
        settlement = read_retained_output_settlement(owner.store, settlement_ref, missing_ok=True)
        if settlement is not None:
            _validate_settlement_matches_handoff(owner, retained, settlement, handoff)
    except InvalidRepositoryStateError as exc:
        return _result_from_handoff(
            handoff,
            state="invalid",
            settlement_ref=settlement_ref,
            invalid_reason=str(exc),
            parent_scope_name=parent_scope_name,
            parent_scope_instance_id=parent_scope_instance_id,
        )

    if settlement is None:
        return _result_from_handoff(
            handoff,
            state="unconsumed",
            settlement_ref=settlement_ref,
            parent_scope_name=parent_scope_name,
            parent_scope_instance_id=parent_scope_instance_id,
        )
    return _result_from_handoff(
        handoff,
        state=settlement.action,
        settlement_ref=settlement_ref,
        settlement=settlement,
        parent_scope_name=parent_scope_name,
        parent_scope_instance_id=parent_scope_instance_id,
    )


def _validate_identity_entry(identity: RetainedOutputIdentity, entry: ScopeRegistryEntry) -> None:
    if entry.name != identity.scope_name:
        raise InvalidRepositoryStateError("retained output identity scope_name disagrees with registry")
    if entry.instance_id != identity.scope_instance_id:
        raise InvalidRepositoryStateError("retained output identity scope_instance_id disagrees with registry")
    if entry.parent_ref != identity.parent_ref:
        raise InvalidRepositoryStateError("retained output identity parent_ref disagrees with registry")
    if entry.status != "retained":
        raise InvalidRepositoryStateError("retained output identity does not name a retained scope")


def _validate_identity_row(identity: RetainedOutputIdentity, row: RetainedOutputQueryResult) -> None:
    expected = {
        "scope_name": identity.scope_name,
        "scope_ref": identity.scope_ref,
        "scope_instance_id": identity.scope_instance_id,
        "parent_ref": identity.parent_ref,
        "parent_scope_name": identity.parent_scope_name,
        "parent_scope_instance_id": identity.parent_scope_instance_id,
        "binding": identity.binding,
        "output_world_oid": identity.output_world_oid,
        "handoff_ref": identity.handoff_ref,
        "parent_basis_world_oid": identity.parent_basis_world_oid,
        "store_id": identity.store_id,
        "resource_id": identity.resource_id,
        "candidate_id": identity.candidate_id,
        "candidate_ref": identity.candidate_ref,
        "candidate_head": identity.candidate_head,
    }
    for field_name, expected_value in expected.items():
        if getattr(row, field_name) != expected_value:
            raise InvalidRepositoryStateError(f"retained output identity field {field_name} disagrees with custody row")


def _invalid_result(
    entry: ScopeRegistryEntry,
    reason: str,
    *,
    parent_scope_name: str | None,
    parent_scope_instance_id: str | None,
) -> RetainedOutputQueryResult:
    return RetainedOutputQueryResult(
        scope_name=entry.name,
        scope_ref=entry.ref,
        scope_instance_id=entry.instance_id,
        parent_ref=entry.parent_ref,
        parent_scope_name=parent_scope_name,
        parent_scope_instance_id=parent_scope_instance_id,
        state="invalid",
        invalid_reason=reason,
    )


def _result_from_handoff(
    handoff: SealCandidateHandoff,
    *,
    state: RetainedOutputState,
    settlement_ref: str,
    settlement: RetainedOutputSettlement | None = None,
    invalid_reason: str | None = None,
    parent_scope_name: str | None,
    parent_scope_instance_id: str | None,
) -> RetainedOutputQueryResult:
    return RetainedOutputQueryResult(
        scope_name=handoff.scope_name,
        scope_ref=handoff.scope_ref,
        scope_instance_id=handoff.scope_instance_id,
        parent_ref=handoff.parent_ref,
        parent_scope_name=parent_scope_name,
        parent_scope_instance_id=parent_scope_instance_id,
        state=state,
        binding=handoff.binding,
        output_world_oid=handoff.output_world_oid,
        handoff_ref=handoff.handoff_ref,
        parent_basis_world_oid=handoff.parent_basis_world_oid,
        store_id=handoff.store_id,
        resource_id=handoff.resource_id,
        candidate_id=handoff.candidate_id,
        candidate_ref=handoff.candidate_ref,
        candidate_head=handoff.candidate_head,
        changed_paths=handoff.changed_paths,
        settlement_ref=settlement_ref,
        settlement=settlement,
        invalid_reason=invalid_reason,
    )


def _parent_identity_from_ref(
    owner: VcsCore,
    registry: ScopeRegistrySnapshot,
    parent_ref: str,
) -> tuple[str | None, str | None]:
    if parent_ref == GROUND_REF:
        return "ground", None
    entry = registry.entries_by_ref.get(parent_ref)
    if entry is None:
        return None, None
    return entry.name, entry.instance_id


def _validate_settlement_matches_handoff(
    owner: VcsCore,
    retained: ValidatedRetainedWorkspace,
    settlement: RetainedOutputSettlement,
    handoff: SealCandidateHandoff,
) -> None:
    if (
        settlement.scope_name != handoff.scope_name
        or settlement.scope_ref != handoff.scope_ref
        or settlement.scope_instance_id != handoff.scope_instance_id
        or settlement.parent_ref != handoff.parent_ref
        or settlement.handoff_ref != handoff.handoff_ref
        or settlement.output_world_oid != handoff.output_world_oid
        or settlement.binding != handoff.binding
        or settlement.store_id != handoff.store_id
        or settlement.resource_id != handoff.resource_id
        or settlement.candidate_id != handoff.candidate_id
        or settlement.candidate_head != handoff.candidate_head
    ):
        raise InvalidRepositoryStateError("retained output settlement disagrees with seal handoff")
    manager = owner._world_storage()
    if settlement.action in {"selected", "applied"}:
        try:
            _validate_retained_selection_world(
                manager,
                retained=retained,
                operation_id=settlement.operation_id,
                parent_world_before=settlement.parent_world_before,
                parent_world_after=settlement.parent_world_after,
            )
        except InvalidRepositoryStateError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidRepositoryStateError("retained output selected settlement world is unreadable") from exc
        return

    if settlement.action not in {"released", "discarded"}:
        raise InvalidRepositoryStateError(f"unsupported retained output settlement action: {settlement.action!r}")
    receipt_action = cast("ReceiptOnlyAction", settlement.action)
    expected_operation_id = _receipt_only_operation_id(
        handoff=handoff,
        settlement_ref=settlement.settlement_ref,
        action=receipt_action,
    )
    if settlement.operation_id != expected_operation_id:
        raise InvalidRepositoryStateError("retained output receipt-only settlement operation_id disagrees with action")
    if settlement.parent_world_before != settlement.parent_world_after:
        raise InvalidRepositoryStateError("retained output receipt-only settlement must not publish a parent world")
    try:
        manager.read_world(settlement.parent_world_before)
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidRepositoryStateError("retained output receipt-only settlement parent world is unreadable") from exc


def _resolve_parent_ref(owner: VcsCore, parent: ScopeInfo | str | None) -> str | None:
    if parent is None:
        return None
    if isinstance(parent, ScopeInfo):
        return parent.ref
    if parent in {"ground", GROUND_REF}:
        return GROUND_REF
    entry = owner.store.scope_registry_entry(parent)
    if entry is not None:
        return entry.ref
    live = owner.lookup_scope(parent)
    if live is not None:
        return live.ref
    raise InvalidRepositoryStateError(f"retained-output query parent is unknown: {parent!r}")
