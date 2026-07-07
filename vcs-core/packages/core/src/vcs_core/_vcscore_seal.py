"""Seal collaborator: bridges retained scopes to v2 substrate handles.

`SealController` is a VcsCore collaborator (constructed in `VcsCore.__init__`) with
injected dependencies and no back-reference to `VcsCore` — the P3 replacement for the
former `owner: VcsCore` free-function module. The pure validation/rendering helpers
stay module-level (several are imported by `_retained_output_*`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pygit2

from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._seal_handoff import LoadedSealHandoff, read_seal_handoff, seal_handoff_ref, write_seal_handoff
from vcs_core._substrate_tree_read import read_substrate_workspace_file
from vcs_core.git_store import diff_workspace_trees
from vcs_core.types import (
    RetainedWorkspaceHandle,
    ScopeInfo,
    SealCandidateHandoff,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vcs_core._projection_store import ScopeRegistryEntry
    from vcs_core._world_operation_builder import PreparedCandidateTupleRecord
    from vcs_core._world_storage_manager import WorldStorageManager
    from vcs_core._world_types import SubstrateHead, WorldCommit
    from vcs_core.store import Store


@dataclass(frozen=True)
class PreparedSealHandoff:
    """Read-only seal plan validated before lifecycle state is persisted."""

    handoff: SealCandidateHandoff
    candidate_tuple: PreparedCandidateTupleRecord


@dataclass(frozen=True)
class ValidatedRetainedWorkspace:
    """Retained workspace custody proven across registry, handoff, world, and candidate refs."""

    entry: ScopeRegistryEntry
    entry_scope: ScopeInfo
    loaded: LoadedSealHandoff
    world: WorldCommit
    head: SubstrateHead


class SealController:
    """Seal-handoff and retained-workspace-read operations for one VcsCore session.

    Dependencies are injected (no `VcsCore` back-reference):
      * ``store`` — the session's :class:`Store`;
      * ``world_storage`` — the lazy accessor returning the ``WorldStorageManager``;
      * ``current_v2_world_oid`` — the pure ``(manager, ref) -> str | None`` ref-target lookup.
    """

    def __init__(
        self,
        *,
        store: Store,
        world_storage: Callable[[], WorldStorageManager],
        current_v2_world_oid: Callable[[WorldStorageManager, str], str | None],
    ) -> None:
        self._store = store
        self._world_storage = world_storage
        self._current_v2_world_oid = current_v2_world_oid

    def create_or_load_seal_handoff(
        self,
        *,
        scope: ScopeInfo,
        parent: ScopeInfo,
        output_binding: str | None = None,
    ) -> LoadedSealHandoff:
        """Create or load the durable candidate handoff for one retained child."""
        return self.write_prepared_seal_handoff(
            prepared=self.prepare_seal_handoff(scope=scope, parent=parent, output_binding=output_binding),
        )

    def prepare_seal_handoff(
        self,
        *,
        scope: ScopeInfo,
        parent: ScopeInfo,
        output_binding: str | None = None,
    ) -> PreparedSealHandoff:
        """Validate sealability and build the candidate handoff without mutating lifecycle state."""
        manager = self._world_storage()
        child_world_oid = self._current_v2_world_oid(manager, scope.ref)
        if child_world_oid is None:
            raise InvalidRepositoryStateError(f"Cannot seal scope {scope.name!r}: no v2 child world is published")
        parent_basis_world_oid = manager.fork_origin_world_oid(scope.ref, expected_forked_from_ref=parent.ref)
        child_world = manager.read_world(child_world_oid)
        head = _output_head(child_world, output_binding=output_binding)
        provenance = manager.resolve_selected_head_candidate_provenance(child_world_oid, binding=head.binding)
        head = provenance.selected_head
        producer_operation_id = provenance.producer_operation_id
        candidate_tuple = provenance.candidate_tuple
        candidate = candidate_tuple.candidate
        changed_paths = _changed_paths(
            manager,
            parent_world_oid=parent_basis_world_oid,
            child_world_oid=child_world_oid,
            binding=head.binding,
        )
        handoff = SealCandidateHandoff(
            seal_operation_id=_seal_operation_id(scope, child_world_oid),
            producer_operation_id=producer_operation_id,
            scope_name=scope.name,
            scope_ref=scope.ref,
            scope_instance_id=scope.instance_id,
            scope_world_id=scope.world_id,
            parent_ref=parent.ref,
            parent_basis_world_oid=parent_basis_world_oid,
            output_world_oid=child_world_oid,
            binding=candidate.binding,
            store_id=candidate.store_id,
            resource_id=candidate.resource_id,
            candidate_id=candidate.candidate_id,
            candidate_ref=candidate.ref,
            candidate_head=candidate.head,
            candidate_tuple_digest=candidate_tuple.tuple_digest(),
            handoff_ref=seal_handoff_ref(scope),
            changed_paths=changed_paths,
        )
        return PreparedSealHandoff(handoff=handoff, candidate_tuple=candidate_tuple)

    def write_prepared_seal_handoff(self, *, prepared: PreparedSealHandoff) -> LoadedSealHandoff:
        """Persist a preflighted seal handoff, or load the existing identical record."""
        return write_seal_handoff(self._store, handoff=prepared.handoff, candidate_tuple=prepared.candidate_tuple)

    def retained_workspace_handle(self, scope_or_name: ScopeInfo | str) -> RetainedWorkspaceHandle:
        """Return a copyable retained workspace handle for a sealed scope."""
        retained = self.validated_retained_workspace(scope_or_name)
        handoff = retained.loaded.handoff
        return RetainedWorkspaceHandle(
            scope_name=handoff.scope_name,
            scope_ref=handoff.scope_ref,
            scope_instance_id=handoff.scope_instance_id,
            output_world_oid=handoff.output_world_oid,
            binding=handoff.binding,
            store_id=handoff.store_id,
            resource_id=handoff.resource_id,
            head=handoff.candidate_head,
            basis_ref=handoff.handoff_ref,
            changed_paths=handoff.changed_paths,
        )

    def retained_workspace_handoff(
        self,
        scope_or_handle: ScopeInfo | RetainedWorkspaceHandle | str,
    ) -> SealCandidateHandoff:
        """Return the validated durable handoff for a retained workspace."""
        retained = self.validated_retained_workspace(_scope_selector(scope_or_handle))
        if isinstance(scope_or_handle, RetainedWorkspaceHandle):
            _validate_retained_workspace_handle(scope_or_handle, retained)
        return retained.loaded.handoff

    def read_retained_workspace_file(self, scope_or_name: ScopeInfo | str, path: str) -> tuple[bytes, int] | None:
        """Read one file from a retained workspace handle's durable tree-backed basis."""
        retained = self.validated_retained_workspace(scope_or_name)
        manager = self._world_storage()
        substrate = manager.store(retained.head.store_id)
        metadata = substrate.read_revision_metadata(retained.head.head)
        if metadata.byte_authority != "tree-backed":
            return None
        return read_substrate_workspace_file(substrate.repo, retained.head.head, path)

    def validated_retained_workspace(self, scope_or_name: ScopeInfo | str) -> ValidatedRetainedWorkspace:
        """Prove retained custody across registry, handoff, world, and candidate refs.

        Public: also consumed by the `_retained_output_*` modules via ``owner._seal``.
        """
        self.require_public_retained_read_allowed()
        entry, entry_scope = self._retained_scope_info(scope_or_name)
        loaded = read_seal_handoff(self._store, entry_scope)
        if loaded is None:  # read_seal_handoff only returns None with missing_ok=True
            raise InvalidRepositoryStateError(f"seal handoff is missing for retained scope {entry_scope.name!r}")
        handoff = loaded.handoff
        _validate_handoff_scope_identity(entry, entry_scope, handoff)

        manager = self._world_storage()
        current_world_oid = self._current_v2_world_oid(manager, entry_scope.ref)
        if current_world_oid is None:
            raise InvalidRepositoryStateError(f"retained v2 scope ref is missing for {entry_scope.name!r}")
        if current_world_oid != handoff.output_world_oid:
            raise InvalidRepositoryStateError(
                f"retained v2 scope ref target disagrees with seal handoff for {entry_scope.name!r}"
            )
        world = manager.read_world(current_world_oid)
        try:
            head = world.snapshot.head_for(handoff.binding)
        except KeyError as exc:
            raise InvalidRepositoryStateError(
                f"retained world {current_world_oid!r} has no binding {handoff.binding!r}"
            ) from exc
        _validate_handoff_head(handoff, head)
        _validate_handoff_selected_head_provenance(manager, handoff, loaded)
        try:
            substrate = manager.store(handoff.store_id)
        except KeyError as exc:
            raise InvalidRepositoryStateError(
                f"retained handoff names unknown substrate store {handoff.store_id!r}"
            ) from exc
        substrate.validate_candidate_ref(
            operation_id=handoff.producer_operation_id,
            binding=handoff.binding,
            candidate_id=handoff.candidate_id,
            expected_head=handoff.candidate_head,
        )
        return ValidatedRetainedWorkspace(entry=entry, entry_scope=entry_scope, loaded=loaded, world=world, head=head)

    def require_public_retained_read_allowed(self) -> None:
        """Public: also consumed by `_retained_output_queries` via ``owner._seal``."""
        mismatches = self._store.scope_registry_projection_mismatches()
        if mismatches:
            raise InvalidRepositoryStateError(
                "Cannot read retained workspace while scope-registry mismatches are present."
            )

    def _retained_scope_info(self, scope_or_name: ScopeInfo | str) -> tuple[ScopeRegistryEntry, ScopeInfo]:
        if isinstance(scope_or_name, ScopeInfo):
            entry = self._store.scope_registry_entry(scope_or_name.name, status="retained")
            if (
                entry is None
                or entry.ref != scope_or_name.ref
                or entry.instance_id != scope_or_name.instance_id
                or entry.creation_oid != scope_or_name.creation_oid
            ):
                raise InvalidRepositoryStateError(f"scope {scope_or_name.name!r} is not retained")
            return entry, self._store.scope_info_from_registry_entry(entry)
        entry = self._store.scope_registry_entry(scope_or_name, status="retained")
        if entry is None:
            raise InvalidRepositoryStateError(f"scope {scope_or_name!r} is not retained")
        return entry, self._store.scope_info_from_registry_entry(entry)


def _validate_handoff_scope_identity(
    entry: ScopeRegistryEntry,
    entry_scope: ScopeInfo,
    handoff: SealCandidateHandoff,
) -> None:
    if (
        handoff.scope_name != entry_scope.name
        or handoff.scope_ref != entry_scope.ref
        or handoff.scope_instance_id != entry_scope.instance_id
        or handoff.scope_world_id != entry_scope.world_id
    ):
        raise InvalidRepositoryStateError(f"seal handoff identity disagrees with retained scope {entry_scope.name!r}")
    if handoff.parent_ref != entry.parent_ref:
        raise InvalidRepositoryStateError(f"seal handoff parent_ref disagrees with retained scope {entry_scope.name!r}")


def _validate_handoff_head(handoff: SealCandidateHandoff, head: SubstrateHead) -> None:
    if (
        head.binding != handoff.binding
        or head.store_id != handoff.store_id
        or head.resource_id != handoff.resource_id
        or head.head != handoff.candidate_head
    ):
        raise InvalidRepositoryStateError("retained world head disagrees with seal handoff")


def _validate_handoff_selected_head_provenance(
    manager: WorldStorageManager,
    handoff: SealCandidateHandoff,
    loaded: LoadedSealHandoff,
) -> None:
    provenance = manager.resolve_selected_head_candidate_provenance(
        handoff.output_world_oid,
        binding=handoff.binding,
    )
    candidate_tuple = provenance.candidate_tuple
    candidate = candidate_tuple.candidate
    if provenance.producer_operation_id != handoff.producer_operation_id:
        raise InvalidRepositoryStateError("seal handoff producer_operation_id disagrees with selected-head provenance")
    if (
        candidate.binding != handoff.binding
        or candidate.store_id != handoff.store_id
        or candidate.resource_id != handoff.resource_id
        or candidate.candidate_id != handoff.candidate_id
        or candidate.ref != handoff.candidate_ref
        or candidate.head != handoff.candidate_head
    ):
        raise InvalidRepositoryStateError("seal handoff candidate disagrees with selected-head provenance")
    if handoff.candidate_tuple_digest != candidate_tuple.tuple_digest():
        raise InvalidRepositoryStateError("seal handoff candidate tuple digest disagrees with selected-head provenance")
    if loaded.candidate_tuple != candidate_tuple:
        raise InvalidRepositoryStateError("seal handoff candidate tuple disagrees with selected-head provenance")


def _output_head(world: WorldCommit, *, output_binding: str | None) -> SubstrateHead:
    if output_binding is not None:
        return _required_snapshot_head(world, output_binding, context="seal output")
    return _workspace_head(world)


def _workspace_head(world: WorldCommit) -> SubstrateHead:
    try:
        return world.snapshot.head_for("workspace")
    except KeyError:
        pass
    filesystem_heads = tuple(head for head in world.snapshot.heads if head.kind == "filesystem")
    if len(filesystem_heads) == 1:
        return filesystem_heads[0]
    raise InvalidRepositoryStateError(f"world {world.oid!r} has no unique workspace/filesystem head")


def _required_snapshot_head(world: WorldCommit, binding: str, *, context: str) -> SubstrateHead:
    try:
        return world.snapshot.head_for(binding)
    except KeyError as exc:
        raise InvalidRepositoryStateError(f"{context} world {world.oid!r} has no binding {binding!r}") from exc


def _changed_paths(
    manager: WorldStorageManager,
    *,
    parent_world_oid: str | None,
    child_world_oid: str,
    binding: str,
) -> tuple[str, ...]:
    if parent_world_oid is None:
        return ()
    try:
        parent = manager.read_world(parent_world_oid)
        child = manager.read_world(child_world_oid)
        parent_head = parent.snapshot.head_for(binding)
        child_head = child.snapshot.head_for(binding)
        substrate = manager.store(child_head.store_id)
        parent_metadata = substrate.read_revision_metadata(parent_head.head)
        child_metadata = substrate.read_revision_metadata(child_head.head)
    except (InvalidRepositoryStateError, KeyError, ValueError):
        return ()
    if parent_metadata.byte_authority != "tree-backed" or child_metadata.byte_authority != "tree-backed":
        return ()
    if parent_metadata.git_tree_oid is None or child_metadata.git_tree_oid is None:
        return ()
    changes = diff_workspace_trees(
        substrate.repo,
        pygit2.Oid(hex=parent_metadata.git_tree_oid),
        pygit2.Oid(hex=child_metadata.git_tree_oid),
    )
    return tuple(change.path for change in changes)


def _seal_operation_id(scope: ScopeInfo, child_world_oid: str) -> str:
    return f"seal_{scope.instance_id}_{child_world_oid[:12]}"


def _scope_selector(scope_or_handle: ScopeInfo | RetainedWorkspaceHandle | str) -> ScopeInfo | str:
    if isinstance(scope_or_handle, RetainedWorkspaceHandle):
        return scope_or_handle.scope_name
    return scope_or_handle


def _validate_retained_workspace_handle(
    handle: RetainedWorkspaceHandle,
    retained: ValidatedRetainedWorkspace,
) -> None:
    handoff = retained.loaded.handoff
    if (
        handle.scope_name != handoff.scope_name
        or handle.scope_ref != handoff.scope_ref
        or handle.scope_instance_id != handoff.scope_instance_id
        or handle.output_world_oid != handoff.output_world_oid
        or handle.binding != handoff.binding
        or handle.store_id != handoff.store_id
        or handle.resource_id != handoff.resource_id
        or handle.head != handoff.candidate_head
        or handle.basis_ref != handoff.handoff_ref
        or handle.changed_paths != handoff.changed_paths
    ):
        raise InvalidRepositoryStateError("retained workspace handle disagrees with retained custody")
