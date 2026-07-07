# under-test: vcs_core._projection_store
"""Focused tests for retained scope-registry status semantics."""

from __future__ import annotations

from typing import get_args

from vcs_core._projection_store import (
    REF_OWNING_SCOPE_STATUSES,
    RUNTIME_OPEN_SCOPE_STATUSES,
    TERMINAL_SCOPE_STATUSES,
    ScopeRegistryEntry,
    ScopeRegistryStatus,
)
from vcs_core.store import Store


def _entry(task, *, status: ScopeRegistryStatus) -> ScopeRegistryEntry:
    assert task.world_id is not None
    return ScopeRegistryEntry(
        name=task.name,
        ref=task.ref,
        instance_id=task.instance_id,
        creation_oid=task.creation_oid,
        parent_ref=Store.GROUND_REF,
        world_id=task.world_id,
        isolation_mode="shared",
        status=status,
    )


def test_retained_status_partitions_scope_lifecycle_literal() -> None:
    all_statuses = set(get_args(ScopeRegistryStatus))

    assert all_statuses == REF_OWNING_SCOPE_STATUSES | TERMINAL_SCOPE_STATUSES
    assert REF_OWNING_SCOPE_STATUSES.isdisjoint(TERMINAL_SCOPE_STATUSES)
    assert RUNTIME_OPEN_SCOPE_STATUSES < REF_OWNING_SCOPE_STATUSES
    assert "retained" in REF_OWNING_SCOPE_STATUSES
    assert "retained" not in RUNTIME_OPEN_SCOPE_STATUSES


def test_retained_status_round_trips_and_is_legitimate(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task")
    retained = _entry(task, status="retained")
    base = store.require_scope_registry_projection()

    assert store.publish_scope_registry_projection(entries=(retained,), expected_head_oid=base.head_oid)
    snapshot = store.load_scope_registry_projection()
    assert snapshot is not None
    assert snapshot.entries == (retained,)

    # `retained` is an ordinary ref-owning status now that seal-and-select is
    # unconditional: a retained scope is never a registry mismatch.
    assert store.scope_registry_projection_mismatches() == ()
