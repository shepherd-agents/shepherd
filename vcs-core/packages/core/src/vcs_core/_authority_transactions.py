"""Shared mechanics for internal authority transactions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vcs_core._authority import (
    PendingAuthoritySettlement,
    clear_pending_authority_settlement,
    normalize_authority_context,
    write_pending_authority_settlement,
)
from vcs_core._authority_inventory import probe_authority_settlement_pending_record
from vcs_core.types import EffectRecord

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vcs_core.types import ScopeInfo
    from vcs_core.vcscore import VcsCore


def ensure_authority_operation_ids_available(owner: VcsCore, *operation_ids: str) -> None:
    """Fail before an authority transaction crosses its action boundary."""
    for operation_id in operation_ids:
        if owner._store.operation_id_exists(operation_id):
            raise ValueError(f"Operation id {operation_id!r} is already present in repository history.")


def begin_pending_authority_settlement(
    owner: VcsCore,
    pending: PendingAuthoritySettlement,
) -> PendingAuthoritySettlement:
    """Persist a pending authority settlement before the protected action."""
    updated = pending.with_update()
    write_pending_authority_settlement(owner._repo_path, updated)
    return updated


def update_pending_authority_settlement(
    owner: VcsCore,
    pending: PendingAuthoritySettlement,
    **changes: object,
) -> PendingAuthoritySettlement:
    """Persist a phase/outcome update for an open authority settlement."""
    updated = pending.with_update(**changes)
    write_pending_authority_settlement(owner._repo_path, updated)
    return updated


def clear_pending_authority_transaction(owner: VcsCore, settlement_operation_id: str) -> None:
    """Clear an authority settlement after final settlement evidence is durable."""
    clear_pending_authority_settlement(owner._repo_path, settlement_operation_id)


def record_authority_settlement_effect(
    owner: VcsCore,
    *,
    scope: ScopeInfo,
    settlement_operation_id: str,
    authority_operation_id: str,
    cohort_id: str,
    candidate_digest: str,
    monitor_basis: str,
    operation_label: str,
    operation_kind: str,
    effect_type: str,
    effect_metadata: Mapping[str, object],
    authority_context: Mapping[str, object] | None = None,
) -> None:
    """Record final settlement evidence behind its pending blocker."""
    authority_metadata: dict[str, object] = {
        "authority_operation_id": authority_operation_id,
        "cohort_id": cohort_id,
        "candidate_digest": candidate_digest,
        "monitor_basis": monitor_basis,
    }
    normalized_context = normalize_authority_context(dict(authority_context) if authority_context is not None else None)
    if normalized_context is not None:
        authority_metadata["authority_context"] = normalized_context
    with owner.runtime_activity(
        scope=scope,
        operation_id=settlement_operation_id,
        operation_label=operation_label,
        operation_kind=operation_kind,
        operation_metadata={"authority": authority_metadata},
        allowed_blocker_item_ids=(
            probe_authority_settlement_pending_record(owner._repo_path, settlement_operation_id).id,
        ),
    ) as operation:
        if operation is None:
            raise RuntimeError("authority settlement requires an operation boundary.")
        owner._pipeline.record_one(
            EffectRecord(effect_type=effect_type, metadata=dict(effect_metadata)),
            substrate="vcscore.authority",
            scope=scope,
        )
