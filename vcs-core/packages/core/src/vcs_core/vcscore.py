"""VcsCore: coordination facade over Store + substrates.

VcsCore composes Store (pure Git) and registered substrates. It exposes
tree-shaped primitives (fork, merge, discard) that coordinate substrate
branch/merge/discard calls alongside Store's Git operations. VcsCore
owns no Git logic directly -- it delegates to Store.

Substrate ordering contract:
  - branch() calls iterate substrates in forward (dependency) order.
  - commit_merge() and discard() calls iterate in reverse order,
    matching the construct-forward / destruct-reverse convention.
  - Merge uses a two-phase protocol: prepare_merge() is called on all
    ContainSubstrates first; only if all succeed does commit_merge()
    proceed. On partial failure, successfully-prepared substrates are
    left in their pre-merge state (no commit occurred).
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterator, Mapping, Sequence

    import pygit2

    from vcs_core._authority import AuthorityMergeResult, DecisionProvider, RetainedOutputDecisionProvider
    from vcs_core._command_envelope import CommandExecutionOptions
    from vcs_core._command_values import CommandValueSource
    from vcs_core._lifecycle_run import LifecycleRun, LifecycleScopeState
    from vcs_core._materialization_coordinator import MaterializationRecoveryReport
    from vcs_core._parent_tree_manifest import ParentTreeManifest
    from vcs_core._projection_store import ScopeRegistryMismatch
    from vcs_core._query_inventory import InventorySnapshot
    from vcs_core._query_readiness import (
        MutationPrecondition,
        ReadinessFreshness,
        ReadinessOperationAuthority,
        ReadinessRequest,
        ReadinessResult,
        RuntimeAdmissionContext,
    )
    from vcs_core._runtime_types import OperationRefInfo
    from vcs_core._substrate_driver import ActiveSurface
    from vcs_core._world_storage_manager import WorldStorageManager
    from vcs_core._world_types import SubstrateRevisionMetadata
    from vcs_core.authority import SubstrateAuthority
    from vcs_core.materialization import MaterializationAssessment
    from vcs_core.recording import NestedParentAuthorization

from vcs_core import _vcscore_lifecycle, _vcscore_materialization, _vcscore_queries, _vcscore_runtime
from vcs_core._authority_inventory import (
    authority_settlement_pending_labels,
    read_valid_authority_settlement_pending_records,
)
from vcs_core._binding_contracts import BindingContractResolver
from vcs_core._binding_surface import BindingSurface
from vcs_core._capture_reducer import (
    CAPTURE_EVENT_EFFECT,
    CAPTURE_REDUCTION_KIND,
    capture_event_metadata,
    covered_capture_paths,
    ordered_capture_events,
    reduction_operation_id,
)
from vcs_core._claims import ClaimPolicy, ClaimRegistry, ResourceClaim
from vcs_core._errors import InvalidRepositoryStateError, StaleScopeError
from vcs_core._fork_hints import ForkHints
from vcs_core._identity import read_ground_world_id
from vcs_core._operation_start_authority import begin_capture_diagnostic_operation, begin_capture_reduction_operation
from vcs_core._operation_tx import OpenOperationGuard
from vcs_core._patch_manager import PatchManager
from vcs_core._python_runtime_capture_adapter import PythonRuntimeCaptureAdapter
from vcs_core._substrate_driver import CaptureAdapter, CaptureAdapterRegistry, SubstrateDriver
from vcs_core._substrate_runtime import BuiltInRuntimeBinding, RuntimeBoundSubstrate
from vcs_core._workspace_authority import (
    WorkspaceAuthorityPending,
    clear_pending_workspace_authority,
    pending_workspace_authority_records,
    workspace_authority_operation_labels,
    write_pending_workspace_authority,
)
from vcs_core._world_authority_finalizer import WorldAuthorityFinalizer
from vcs_core._world_refs import candidate_ref, encode_ref_component
from vcs_core.authority import validate_authority_report
from vcs_core.recording import RecordingPipeline
from vcs_core.store import GROUND_REF, Store
from vcs_core.substrates import FilesystemSubstrate
from vcs_core.types import (
    BoundSubstrate,
    CommitInfo,
    DiffSummary,
    EffectRecord,
    MaterializationPlan,
    OperationHistory,
    OperationSummary,
    RebaseResult,
    RecordedCommandOutcome,
    RecoverySnapshot,
    RetainedOutputIdentity,
    RetainedOutputQueryResult,
    RetainedOutputSelectionResult,
    RetainedOutputSettlementResult,
    RetainedOutputState,
    RetainedWorkspaceHandle,
    ScopeInfo,
    SealCandidateHandoff,
    SealResult,
    SelectedBindingRevision,
    Status,
    WorkspaceChange,
)

logger = logging.getLogger(__name__)


def _failed_command_origin(operation_id: str, command_metadata: dict[str, object]) -> dict[str, object] | None:
    status = command_metadata.get("status")
    if status in (None, "success"):
        return None
    origin: dict[str, object] = {
        "operation_id": operation_id,
        "exit_code": None,
        "signal": None,
    }
    exit_code = command_metadata.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        origin["exit_code"] = exit_code
    signal = command_metadata.get("signal")
    if isinstance(signal, int) and not isinstance(signal, bool):
        origin["signal"] = signal
    return origin


def _default_binding_parts(substrate: object) -> tuple[str, str]:
    """Return ``(binding_name, substrate_type)`` for direct construction."""
    if isinstance(substrate, SubstrateDriver):
        binding = getattr(substrate, "binding", None)
        if isinstance(binding, str) and binding:
            driver_id = getattr(substrate, "driver_id", None)
            if isinstance(driver_id, str) and driver_id:
                return binding, driver_id
            raise TypeError(
                f"Substrate driver {type(substrate).__name__} declares no non-empty 'driver_id'; "
                "it cannot be bound through substrates=."
            )
        raise TypeError(
            f"Substrate driver {type(substrate).__name__} declares no non-empty 'binding'; "
            "it cannot be bound through substrates=."
        )
    name = getattr(substrate, "name", None)
    if isinstance(name, str) and name:
        return name, name
    binding = getattr(substrate, "binding", None)
    if isinstance(binding, str) and binding:
        return binding, binding
    raise TypeError(
        f"Substrate {type(substrate).__name__} declares neither a 'name' (lifecycle provider) "
        f"nor a 'binding' (SPI driver); it cannot be bound."
    )


def _default_bound_substrates(substrates: Sequence[object]) -> list[BoundSubstrate]:
    counts: dict[str, int] = {}
    for substrate in substrates:
        binding_label, _substrate_type = _default_binding_parts(substrate)
        counts[binding_label] = counts.get(binding_label, 0) + 1

    seen: dict[str, int] = {}
    bound: list[BoundSubstrate] = []
    for substrate in substrates:
        binding_label, substrate_type = _default_binding_parts(substrate)
        if counts[binding_label] == 1:
            binding_name = binding_label
        else:
            index = seen.get(binding_label, 0) + 1
            seen[binding_label] = index
            binding_name = f"{binding_label}-{index}"
        bound.append(
            BoundSubstrate(
                binding_name=binding_name,
                substrate_type=substrate_type,
                instance=substrate,
            )
        )
    return bound


def _is_lifecycle_substrate(instance: object) -> bool:
    name = getattr(instance, "name", None)
    if not isinstance(name, str) or not name:
        return False
    return any(
        hasattr(instance, attr)
        for attr in (
            "activate",
            "deactivate",
            "python_patches",
            "system_hooks",
            "materializers",
            "branch",
            "prepare_merge",
            "discard",
        )
    )


def _lifecycle_bindings(bindings: Sequence[BoundSubstrate]) -> list[BoundSubstrate]:
    return [binding for binding in bindings if _is_lifecycle_substrate(binding.instance)]


class VcsCore:
    """Coordination facade: Store + substrates + tree-shaped primitives.

    Coordinator mutations are serialized by an ``RLock``. Long-lived
    runtime activities open and close their operation span under the
    lock, but do not hold it across arbitrary caller code. Callbacks
    fire outside the lock to prevent deadlock.

    Substrate ordering: branch() in forward dependency order;
    commit_merge() and discard() in reverse dependency order.
    """

    def __init__(
        self,
        workspace: str,
        substrates: list[object] | None = None,
        *,
        bindings: list[BoundSubstrate] | None = None,
        store: Store | None = None,
        allow_activate_init: bool = True,
    ) -> None:
        self._workspace = workspace
        self._repo_path = os.path.join(workspace, ".vcscore")  # noqa: PTH118 — _repo_path is consumed as str throughout
        self._store = store or Store(self._repo_path)
        self._allow_activate_init = allow_activate_init
        self._pipeline = RecordingPipeline(self._store)
        self._carrier_scopes: dict[tuple[str, str, str], ScopeInfo] = {}
        self._claim_registry = ClaimRegistry()
        self._patch_manager = PatchManager(Path(workspace), self._pipeline)
        self._patch_manager.set_runtime_activity_opener(self.runtime_activity)
        self._patch_manager.set_external_write_authorizer(self._ensure_runtime_mutation_allowed)
        self._runtime = BuiltInRuntimeBinding(
            pipeline=self._pipeline,
            control_plane_guard=self._patch_manager.guard,
            is_scope_or_ancestor_isolated=self._is_scope_or_ancestor_isolated,
            overlay_base_scope_name=self._overlay_base_scope_name,
            working_directory_for_scope=self._working_directory_for_scope,
            parent_scope=self._parent_scope,
            lookup_scope=self._lookup_scope,
            nearest_carrier_scope=self._nearest_carrier_scope,
            can_create_carrier=self._can_create_carrier,
            register_carrier=self._register_carrier,
            lookup_claim=self._lookup_claim,
            register_claim=lambda substrate, target_id, path, policy: self._register_claim(
                substrate,
                target_id,
                path,
                cast("ClaimPolicy", policy),
            ),
            ground_workspace_byte_source=self._read_v2_workspace_file_for_materialization,
            ground_workspace_is_tree_backed=self._ground_workspace_is_tree_backed,
        )
        if bindings is not None and substrates is not None:
            raise ValueError("Pass either bindings or substrates, not both.")
        self._bindings: list[BoundSubstrate] = list(bindings or _default_bound_substrates(substrates or []))
        self._bindings_by_name = {binding.binding_name: binding for binding in self._bindings}
        if len(self._bindings_by_name) != len(self._bindings):
            raise ValueError("Duplicate binding names are not allowed.")
        self._binding_contracts = BindingContractResolver(live_bindings=self._bindings)
        self._lifecycle_bindings = _lifecycle_bindings(self._bindings)
        self._lifecycle_substrates: list[object] = [binding.instance for binding in self._lifecycle_bindings]
        self._isolated_scopes: set[str] = set()
        # Bind lifecycle substrates to the coordinator-owned runtime.
        for sub in self._lifecycle_substrates:
            if isinstance(sub, RuntimeBoundSubstrate):
                sub.bind_runtime(self._runtime)
        self._lock = threading.RLock()
        self._session_id = uuid.uuid4().hex[:12]
        self._active_surface_stack: ContextVar[tuple[ActiveSurface, ...]] = ContextVar(
            f"vcs_core_active_surface_stack_{self._session_id}",
            default=(),
        )
        # Set by the owning session daemon (M3): the live daemon's instance id,
        # used at query_readiness to exclude the daemon's own active
        # shell-capture lease from orphaned-operation blockers. None for a
        # non-daemon VcsCore (the lease exclusion is then a no-op).
        self._active_daemon_instance_id: str | None = None
        self._active_scopes: dict[str, ScopeInfo] = {}
        self._scope_parents: dict[str, ScopeInfo] = {}
        self._restored_scopes: set[str] = set()
        self._merge_callbacks: list[Callable[[str], None]] = []
        self._discard_callbacks: list[Callable[[str], None]] = []
        self._ground: ScopeInfo | None = None
        self._ground_world_id: str | None = None
        self._orphaned_refs: list[str] = []
        self._scope_registry_mismatches: list[ScopeRegistryMismatch] = []
        self._orphaned_operations: list[OperationRefInfo] = []
        self._sibling_group_blockers: list[str] = []
        self._lifecycle_run: LifecycleRun | None = None
        self._world_storage_manager: WorldStorageManager | None = None
        self._pending_workspace_driver_effects: dict[str, tuple[ScopeInfo, list[EffectRecord], str, str, bool]] = {}
        self._parent_tree_manifests: dict[tuple[str, str], ParentTreeManifest] = {}
        self._command_admission_providers: list[object] = []
        self._pipeline.set_runtime_effect_recorder(self._record_runtime_effects)

        # CaptureAdapterRegistry (SPI v0.1 §Q2): per-VcsCore-instance registry
        # for cross-cutting capture adapters whose lifetime is owned by an
        # installation component rather than a single substrate driver. T2c
        # registers PythonRuntimeCaptureAdapter here (patch-manager-owned),
        # then freezes the registry. Driver-default adapters (the workspace
        # driver's OverlayCaptureAdapter) remain returned by
        # SubstrateDriver.capture_adapters and are NOT registered here per
        # the SPI doc §Q2 Discovery boundary criterion.
        self._capture_adapter_registry = CaptureAdapterRegistry()
        self._capture_adapter_registry.register_capture_adapter(PythonRuntimeCaptureAdapter())
        self._capture_adapter_registry.freeze()

    @property
    def store(self) -> Store:
        """Access the underlying Store for queries.

        Note: All writes to the DAG should flow through substrates,
        not via direct Store._emit_effect() calls. This property is
        intended for query methods (status, log, diff, filter_effects)
        and for passing the Store reference to substrate constructors.
        """
        return self._store

    @property
    def lifecycle_substrates(self) -> tuple[object, ...]:
        """Substrates that participate in activate/branch/merge/discard hooks."""
        return tuple(self._lifecycle_substrates)

    @contextmanager
    def preserve_runtime_context(self) -> Iterator[None]:
        """Restore the full ambient runtime context after internal inspection."""
        previous_context = self._pipeline.context
        try:
            yield
        finally:
            self._pipeline.set_context(previous_context)

    @contextmanager
    def _scoped(self, scope: ScopeInfo | None) -> Iterator[None]:
        """Run a block with an explicit runtime context set, then restore.

        The invariant this enforces:

          ``RecordingPipeline.context.world`` has exactly two states from the
          coordinator's perspective — ``None`` when no scoped operation
          is active, or ``S`` when the coordinator is inside a scoped
          operation on ``S``.

        Nestable: previous scope is restored on exit, so transient
        inner operations (e.g., substrate callbacks) don't leak a
        mutated scope back to their caller.
        """
        prev = self._pipeline.context
        if scope is None:
            self._pipeline.clear_execution_context()
        else:
            self._pipeline.set_execution_context(scope, session_id=self._session_id)
        try:
            yield
        finally:
            self._pipeline.restore_execution_context(prev)

    @property
    def bindings(self) -> tuple[BoundSubstrate, ...]:
        """Registered substrate bindings (read-only)."""
        return tuple(self._bindings)

    @property
    def binding_surface(self) -> BindingSurface:
        """Metadata-first all-binding read model."""
        return BindingSurface(live_bindings=self._bindings)

    @property
    def binding_contracts(self) -> BindingContractResolver:
        """Validated runtime binding contracts for driver-capable paths."""
        return self._binding_contracts

    def resolve_binding(self, name: str) -> BoundSubstrate:
        """Resolve a binding by exact binding name."""
        return self._resolve_binding(name)

    # --- Lifecycle ---

    def activate(
        self,
        *,
        recover: str | None = None,
        recover_lifecycle: str | None = None,
        defer_orphan_detection: bool = False,
    ) -> None:
        _vcscore_lifecycle.activate(
            self,
            recover=recover,
            recover_lifecycle=recover_lifecycle,
            defer_orphan_detection=defer_orphan_detection,
        )

    def deactivate(self, *, warn_on_open_scopes: bool = True) -> None:
        _vcscore_lifecycle.deactivate(self, warn_on_open_scopes=warn_on_open_scopes)

    def __enter__(self) -> Self:
        self.activate()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        self.deactivate()

    @property
    def ground(self) -> ScopeInfo:
        """The ground scope (root of the branch tree)."""
        if self._ground is None:
            msg = "VcsCore not activated. Call activate() first."
            raise RuntimeError(msg)
        return self._ground

    def recover_lifecycle(self, mode: str = "resume") -> str | None:
        return _vcscore_lifecycle.recover_lifecycle(self, mode=mode)

    def _persist_lifecycle_run(self, run: LifecycleRun) -> None:
        _vcscore_lifecycle._persist_lifecycle_run(self, run)

    def _update_lifecycle_run(
        self,
        *,
        phase: str | None = None,
        prepared_effect_counts: tuple[tuple[str, int], ...] | None = None,
        prepared_substrates: tuple[str, ...] | None = None,
        completed_substrates: tuple[str, ...] | None = None,
    ) -> LifecycleRun:
        return _vcscore_lifecycle._update_lifecycle_run(
            self,
            phase=phase,
            prepared_effect_counts=prepared_effect_counts,
            prepared_substrates=prepared_substrates,
            completed_substrates=completed_substrates,
        )

    def _clear_lifecycle_run(self) -> None:
        _vcscore_lifecycle._clear_lifecycle_run(self)

    def _ensure_no_interrupted_lifecycle(self, attempted: str) -> None:
        _vcscore_lifecycle._ensure_no_interrupted_lifecycle(self, attempted)

    def _scope_state(self, scope: ScopeInfo) -> LifecycleScopeState:
        return _vcscore_lifecycle._scope_state(self, scope)

    def _resolve_world_id(
        self,
        *,
        name: str,
        ref: str,
        instance_id: str,
        world_id: str | None,
    ) -> str:
        if world_id:
            return world_id
        if name == "ground" and ref == GROUND_REF:
            if self._ground_world_id is None:
                self._ground_world_id = read_ground_world_id(self._repo_path)
            return self._ground_world_id
        msg = (
            f"Scope {name!r} ({ref}) is missing durable world_id. "
            "Restore/recovery for non-ground scopes requires explicit durable world identity."
        )
        raise ValueError(msg)

    def _scope_world_id(self, scope: ScopeInfo) -> str:
        return self._resolve_world_id(
            name=scope.name,
            ref=scope.ref,
            instance_id=scope.instance_id,
            world_id=scope.world_id,
        )

    def _live_scope(self, scope: ScopeInfo) -> ScopeInfo:
        if self._ground is not None and scope.name == self._ground.name:
            if scope.ref != self._ground.ref or scope.instance_id != self._ground.instance_id:
                msg = f"Scope {scope.name!r} is stale or belongs to another session."
                raise StaleScopeError(msg)
            return self._ground

        tracked = self._active_scopes.get(scope.name)
        if tracked is None:
            msg = f"Scope {scope.name!r} is not a live scope."
            raise StaleScopeError(msg)
        if tracked.ref != scope.ref or tracked.instance_id != scope.instance_id:
            msg = f"Scope {scope.name!r} is stale or belongs to another session."
            raise StaleScopeError(msg)
        return tracked

    def _active_ancestor_states(self, scope: ScopeInfo) -> tuple[LifecycleScopeState, ...]:
        ancestors: list[LifecycleScopeState] = []
        current = scope
        while current.name != self.ground.name:
            parent = self._scope_parents.get(current.name)
            if parent is None or parent.name == self.ground.name:
                break
            ancestors.append(self._scope_state(parent))
            current = parent
        return tuple(ancestors)

    def _begin_lifecycle_run(self, *, operation: str, phase: str, scope: ScopeInfo, parent: ScopeInfo) -> None:
        _vcscore_lifecycle._begin_lifecycle_run(
            self,
            operation=operation,
            phase=phase,
            scope=scope,
            parent=parent,
        )

    def _scope_tip_matches(self, scope: ScopeInfo, effect_type: str, **expected: str) -> bool:
        return _vcscore_lifecycle._scope_tip_matches(self, scope, effect_type, **expected)

    def _ensure_scope_merge_effect(self, scope: ScopeInfo, parent: ScopeInfo) -> None:
        _vcscore_lifecycle._ensure_scope_merge_effect(self, scope, parent)

    def _ensure_discard_snapshot_effect(self, scope: ScopeInfo, parent: ScopeInfo) -> None:
        _vcscore_lifecycle._ensure_discard_snapshot_effect(self, scope, parent)

    def _finish_scope_removal(self, scope: ScopeInfo, parent: ScopeInfo) -> None:
        _vcscore_lifecycle._finish_scope_removal(self, scope, parent)

    def _mark_completed_substrate(self, substrate_name: str) -> None:
        _vcscore_lifecycle._mark_completed_substrate(self, substrate_name)

    def _mark_prepared_substrate(self, substrate_name: str) -> None:
        _vcscore_lifecycle._mark_prepared_substrate(self, substrate_name)

    def _prepared_effect_count(self, substrate_name: str) -> int:
        return _vcscore_lifecycle._prepared_effect_count(self, substrate_name)

    def _mark_prepared_effect_count(self, substrate_name: str, count: int) -> None:
        _vcscore_lifecycle._mark_prepared_effect_count(self, substrate_name, count)

    def _prepared_effect_matches_scope_commit(
        self,
        scope: ScopeInfo,
        *,
        substrate_name: str,
        effect: EffectRecord,
        commit: pygit2.Commit,
    ) -> bool:
        return _vcscore_lifecycle._prepared_effect_matches_scope_commit(
            self,
            scope,
            substrate_name=substrate_name,
            effect=effect,
            commit=commit,
        )

    def _recover_prepared_effect_count_from_scope_tip(
        self,
        scope: ScopeInfo,
        *,
        substrate_name: str,
        effects: Sequence[EffectRecord],
    ) -> int:
        return _vcscore_lifecycle._recover_prepared_effect_count_from_scope_tip(
            self,
            scope,
            substrate_name=substrate_name,
            effects=effects,
        )

    def _restore_lifecycle_scope(
        self,
        state: LifecycleScopeState,
        *,
        parent: ScopeInfo,
    ) -> ScopeInfo:
        return _vcscore_lifecycle._restore_lifecycle_scope(self, state, parent=parent)

    def _load_lifecycle_context(self, run: LifecycleRun) -> tuple[ScopeInfo, ScopeInfo]:
        return _vcscore_lifecycle._load_lifecycle_context(self, run)

    def _restore_lifecycle_substrate_state(self, run: LifecycleRun, *, scope: ScopeInfo, parent: ScopeInfo) -> None:
        _vcscore_lifecycle._restore_lifecycle_substrate_state(self, run, scope=scope, parent=parent)

    def _complete_merge_locked(self, scope: ScopeInfo, parent: ScopeInfo) -> str:
        return _vcscore_lifecycle._complete_merge_locked(self, scope, parent)

    def _snapshot_discard_effects_locked(self, scope: ScopeInfo, parent: ScopeInfo) -> None:
        _vcscore_lifecycle._snapshot_discard_effects_locked(self, scope, parent)

    def _complete_discard_locked(self, scope: ScopeInfo, parent: ScopeInfo) -> str:
        return _vcscore_lifecycle._complete_discard_locked(self, scope, parent)

    def _run_merge_callbacks(self, scope_name: str) -> None:
        _vcscore_lifecycle._run_merge_callbacks(self, scope_name)

    def _run_discard_callbacks(self, scope_name: str) -> None:
        _vcscore_lifecycle._run_discard_callbacks(self, scope_name)

    def _recover_lifecycle_locked(self, mode: str = "resume") -> tuple[str, str]:
        return _vcscore_lifecycle._recover_lifecycle_locked(self, mode=mode)

    def restore_scope(
        self,
        name: str,
        ref: str,
        instance_id: str,
        creation_oid: str,
        parent: ScopeInfo,
        *,
        world_id: str | None = None,
        isolated: bool = False,
    ) -> ScopeInfo:
        return _vcscore_lifecycle.restore_scope(
            self,
            name,
            ref,
            instance_id,
            creation_oid,
            parent,
            world_id=world_id,
            isolated=isolated,
        )

    def _restore_scope_locked(
        self,
        *,
        name: str,
        ref: str,
        instance_id: str,
        creation_oid: str,
        parent: ScopeInfo,
        world_id: str | None = None,
        isolated: bool = False,
    ) -> ScopeInfo:
        return _vcscore_lifecycle._restore_scope_locked(
            self,
            name=name,
            ref=ref,
            instance_id=instance_id,
            creation_oid=creation_oid,
            parent=parent,
            world_id=world_id,
            isolated=isolated,
        )

    def clear_restored_scope_state(self) -> None:
        _vcscore_lifecycle.clear_restored_scope_state(self)

    def restored_scope_names(self) -> frozenset[str]:
        """Return names of transient registry-restored handles currently installed."""
        return frozenset(self._restored_scopes)

    def retain_restored_scope(self, name: str) -> None:
        """Promote one restored handle to daemon-owned active state."""
        with self._lock:
            self._restored_scopes.discard(name)

    def clear_transient_restored_scopes(
        self,
        *,
        restored_names: set[str],
        previous_restored_names: set[str],
        retained_names: set[str],
    ) -> None:
        """Clear handles restored for a bounded app view unless retained."""
        with self._lock:
            app_restored = restored_names - previous_restored_names - retained_names
            for name in app_restored:
                self._active_scopes.pop(name, None)
                self._scope_parents.pop(name, None)
                self._isolated_scopes.discard(name)
                self._restored_scopes.discard(name)

    # --- Tree-shaped primitives ---

    def fork(
        self,
        parent: ScopeInfo,
        name: str,
        hints: ForkHints | Mapping[str, Any] | None = None,
    ) -> ScopeInfo:
        return _vcscore_lifecycle.fork(self, parent, name, hints=ForkHints.from_value(hints))

    def merge(self, scope: ScopeInfo, parent: ScopeInfo) -> str:
        return _vcscore_lifecycle.merge(self, scope, parent)

    def merge_with_authority(
        self,
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
        return _vcscore_lifecycle.merge_with_authority(
            self,
            scope,
            parent,
            binding_roots=binding_roots,
            decide=decide,
            operation_id=operation_id,
            effective_match_digest=effective_match_digest,
            authority_surface_plan_digest=authority_surface_plan_digest,
            permission_plan_digest=permission_plan_digest,
            permission_plan_descriptor=permission_plan_descriptor,
            authority_context=authority_context,
        )

    def register_command_admission_provider(self, provider: object) -> None:
        """Register a coordinator-owned command admission provider.

        Providers use the same internal ``validate_command_invocation`` shape as
        substrates and are consulted before the substrate-local hook.
        """
        validator = getattr(provider, "validate_command_invocation", None)
        if not callable(validator):
            raise TypeError("command admission provider must define validate_command_invocation(...)")
        self._command_admission_providers.append(provider)

    def seal(self, scope: ScopeInfo | str, *, output_binding: str | None = None) -> SealResult:
        if isinstance(scope, str):
            resolved = self.lookup_scope(scope)
            if resolved is None:
                raise StaleScopeError(f"Scope {scope!r} is not a live scope.")
        else:
            resolved = scope
        return _vcscore_lifecycle.seal(self, resolved, output_binding=output_binding)

    def discard(self, scope: ScopeInfo) -> str:
        return _vcscore_lifecycle.discard(self, scope)

    # --- Orphaned scope management ---

    def archive_orphaned_scopes(self, *, exclude_refs: Collection[str] = ()) -> list[str]:
        return _vcscore_lifecycle.archive_orphaned_scopes(self, exclude_refs=exclude_refs)

    def archive_orphaned_operations(self) -> list[str]:
        return _vcscore_lifecycle.archive_orphaned_operations(self)

    def list_orphaned_scope_refs(self) -> tuple[str, ...]:
        return _vcscore_lifecycle.list_orphaned_scope_refs(self)

    def list_orphaned_operations(self) -> tuple[OperationSummary, ...]:
        return _vcscore_queries.orphaned_operations(self)

    # --- Lifecycle notifications ---

    def on_merge(self, callback: Callable[[str], None]) -> None:
        _vcscore_lifecycle.on_merge(self, callback)

    def on_discard(self, callback: Callable[[str], None]) -> None:
        _vcscore_lifecycle.on_discard(self, callback)

    def _record_runtime_effects(
        self,
        effects: Sequence[EffectRecord],
        *,
        substrate: str,
        scope: ScopeInfo | None = None,
        boundary_policy: str = "append_or_root",
        operation_id: str | None = None,
        operation_kind: str | None = None,
        operation_label: str | None = None,
        operation_metadata: dict[str, object] | None = None,
        workspace_driver_command: str | None = None,
        workspace_output_binding: str = "workspace",
        workspace_effect_overlay: bool = False,
    ) -> list[str]:
        return _vcscore_runtime.record_runtime_effects(
            self,
            effects,
            substrate=substrate,
            scope=scope,
            boundary_policy=boundary_policy,
            operation_id=operation_id,
            operation_kind=operation_kind,
            operation_label=operation_label,
            operation_metadata=operation_metadata,
            workspace_driver_command=workspace_driver_command,
            workspace_output_binding=workspace_output_binding,
            workspace_effect_overlay=workspace_effect_overlay,
        )

    def _capture_adapter_by_mechanism(self, mechanism: str) -> CaptureAdapter | None:
        """Return the registry-owned capture adapter for ``mechanism`` if any.

        Driver-default adapters (returned by
        ``SubstrateDriver.capture_adapters``) are NOT included — those are
        driver-owned per SPI v0.1 §Q2 Discovery boundary criterion.

        Returns ``None`` when no registered adapter matches; callers may
        fall back to driver-default discovery via
        ``SubstrateDriver.capture_adapters(context)``.
        """
        for adapter in self._capture_adapter_registry.adapters():
            if adapter.mechanism == mechanism:
                return adapter
        return None

    def _build_operation_metadata(
        self,
        *,
        scope: ScopeInfo,
        default_label: str,
        default_kind: str,
        operation_id: str | None = None,
        operation_kind: str | None = None,
        operation_label: str | None = None,
        operation_metadata: dict[str, object] | None = None,
        nested_parent: NestedParentAuthorization | None = None,
    ) -> tuple[str, str, str, str | None, dict[str, object]]:
        return _vcscore_runtime.build_operation_metadata(
            self,
            scope=scope,
            default_label=default_label,
            default_kind=default_kind,
            operation_id=operation_id,
            operation_kind=operation_kind,
            operation_label=operation_label,
            operation_metadata=operation_metadata,
            nested_parent=nested_parent,
        )

    @contextmanager
    def _opened_runtime_operation(
        self,
        *,
        scope: ScopeInfo,
        default_label: str,
        default_kind: str,
        failure_policy: str = "abort_archive",
        operation_id: str | None = None,
        operation_kind: str | None = None,
        operation_label: str | None = None,
        operation_metadata: dict[str, object] | None = None,
        nested_parent: NestedParentAuthorization | None = None,
    ) -> Iterator[OperationRefInfo]:
        with _vcscore_runtime.opened_runtime_operation(
            self,
            scope=scope,
            default_label=default_label,
            default_kind=default_kind,
            failure_policy=failure_policy,
            operation_id=operation_id,
            operation_kind=operation_kind,
            operation_label=operation_label,
            operation_metadata=operation_metadata,
            nested_parent=nested_parent,
        ) as operation:
            yield operation

    @contextmanager
    def _runtime_operation_boundary(
        self,
        *,
        scope: ScopeInfo,
        boundary_policy: str,
        default_label: str,
        default_kind: str,
        failure_policy: str = "abort_archive",
        operation_id: str | None = None,
        operation_kind: str | None = None,
        operation_label: str | None = None,
        operation_metadata: dict[str, object] | None = None,
        nested_parent: NestedParentAuthorization | None = None,
    ) -> Iterator[OperationRefInfo | None]:
        with _vcscore_runtime.runtime_operation_boundary(
            self,
            scope=scope,
            boundary_policy=boundary_policy,
            default_label=default_label,
            default_kind=default_kind,
            failure_policy=failure_policy,
            operation_id=operation_id,
            operation_kind=operation_kind,
            operation_label=operation_label,
            operation_metadata=operation_metadata,
            nested_parent=nested_parent,
        ) as operation:
            yield operation

    @staticmethod
    def _default_handle_id(label: str) -> str:
        return _vcscore_runtime.default_handle_id(label)

    @staticmethod
    def _new_operation_id() -> str:
        return _vcscore_runtime.new_operation_id()

    @contextmanager
    def runtime_activity(
        self,
        *,
        scope: ScopeInfo,
        operation_label: str,
        operation_kind: str,
        boundary_policy: str = "explicit",
        failure_policy: str = "abort_archive",
        operation_id: str | None = None,
        operation_metadata: dict[str, object] | None = None,
        allowed_blocker_item_ids: tuple[str, ...] = (),
    ) -> Iterator[OperationRefInfo | None]:
        with _vcscore_runtime.runtime_activity(
            self,
            scope=scope,
            operation_label=operation_label,
            operation_kind=operation_kind,
            boundary_policy=boundary_policy,
            failure_policy=failure_policy,
            operation_id=operation_id,
            operation_metadata=operation_metadata,
            allowed_blocker_item_ids=allowed_blocker_item_ids,
        ) as operation:
            yield operation

    def _execute_recorded_in_operation(
        self,
        binding_name: str,
        command: str,
        *,
        scope: ScopeInfo,
        boundary_policy: str = "explicit",
        operation_id: str | None = None,
        operation_kind: str | None = None,
        operation_label: str | None = None,
        operation_metadata: dict[str, object] | None = None,
        execution_options: CommandExecutionOptions | None = None,
        **params: Any,
    ) -> RecordedCommandOutcome:
        return _vcscore_runtime.execute_recorded_in_operation(
            self,
            binding_name,
            command,
            scope=scope,
            boundary_policy=boundary_policy,
            operation_id=operation_id,
            operation_kind=operation_kind,
            operation_label=operation_label,
            operation_metadata=operation_metadata,
            execution_options=execution_options,
            **params,
        )

    def _admit_command_invocation(
        self,
        substrate: object,
        command: str,
        *,
        scope: ScopeInfo,
        params: dict[str, Any],
    ) -> None:
        _vcscore_runtime.admit_command(
            self,
            substrate,
            command,
            scope=scope,
            params=params,
        )

    def exec(
        self,
        binding_name: str,
        command: str,
        *,
        scope: ScopeInfo,
        execution_options: CommandExecutionOptions | None = None,
        **params: Any,
    ) -> RecordedCommandOutcome:
        return _vcscore_runtime.exec_command(
            self,
            binding_name,
            command,
            scope=scope,
            execution_options=execution_options,
            **params,
        )

    def _execute_recorded(
        self,
        binding_name: str,
        command: str,
        *,
        scope: ScopeInfo,
        execution_options: CommandExecutionOptions | None = None,
        **params: Any,
    ) -> RecordedCommandOutcome:
        return _vcscore_runtime.execute_recorded(
            self,
            binding_name,
            command,
            scope=scope,
            execution_options=execution_options,
            **params,
        )

    def record_child_workspace_write(
        self,
        *,
        scope: ScopeInfo,
        path: str,
        content: bytes,
        mode: int = 0o100644,
        operation_id: str,
        operation_kind: str,
        operation_metadata: dict[str, object] | None = None,
    ) -> RecordedCommandOutcome:
        """Record one child-scope workspace write under the current parent activity."""
        if not _vcscore_runtime._nested_operations_enabled():
            raise RuntimeError("child workspace writes require VCS_CORE_NESTED_OPERATIONS=1")
        current_operation = self._pipeline.current_operation()
        if current_operation is None:
            raise RuntimeError("child workspace writes require an active parent runtime activity")
        if current_operation.scope_ref == scope.ref:
            raise RuntimeError("child workspace writes require a child scope distinct from the active parent")
        if _vcscore_runtime._nested_parent_authorization(self, scope) is None:
            raise RuntimeError("child workspace writes require a child scope descended from the active parent")
        return self._execute_recorded_in_child_operation(
            "filesystem",
            "write",
            scope=scope,
            operation_id=operation_id,
            operation_kind=operation_kind,
            operation_metadata=operation_metadata,
            path=path,
            content=content,
            mode=mode,
        )

    def _execute_recorded_params(
        self,
        binding_name: str,
        command: str,
        *,
        scope: ScopeInfo,
        params: Mapping[str, Any],
        command_param_source: CommandValueSource,
        execution_options: CommandExecutionOptions | None = None,
    ) -> RecordedCommandOutcome:
        return _vcscore_runtime._execute_recorded_params(
            self,
            binding_name,
            command,
            scope=scope,
            boundary_policy="explicit",
            params=params,
            command_param_source=command_param_source,
            execution_options=execution_options,
        )

    def _execute_recorded_in_child_operation(
        self,
        binding_name: str,
        command: str,
        *,
        scope: ScopeInfo,
        operation_id: str,
        operation_kind: str,
        operation_metadata: dict[str, object] | None = None,
        execution_options: CommandExecutionOptions | None = None,
        workspace_output_binding: str = "workspace",
        **params: Any,
    ) -> RecordedCommandOutcome:
        return _vcscore_runtime.execute_recorded_in_child_operation(
            self,
            binding_name,
            command,
            scope=scope,
            operation_id=operation_id,
            operation_kind=operation_kind,
            operation_metadata=operation_metadata,
            execution_options=execution_options,
            workspace_output_binding=workspace_output_binding,
            **params,
        )

    def _record_in_child_operation(
        self,
        binding_name: str,
        effect_record: EffectRecord,
        *,
        scope: ScopeInfo,
        operation_id: str,
        operation_kind: str,
        operation_metadata: dict[str, object] | None = None,
    ) -> list[str]:
        return _vcscore_runtime.record_in_child_operation(
            self,
            binding_name,
            effect_record,
            scope=scope,
            operation_id=operation_id,
            operation_kind=operation_kind,
            operation_metadata=operation_metadata,
        )

    def _record_capture_event(
        self,
        binding_name: str,
        event: Any,
        *,
        command_operation_id: str,
        capture_epoch: str | None = None,
        global_seq: int,
        event_seq: int,
        capture_mechanism: str,
    ) -> str:
        """Append one raw filesystem capture event to its open command envelope."""
        operation = self._find_open_session_exec_operation(command_operation_id)
        metadata = capture_event_metadata(
            command_operation_id=command_operation_id,
            capture_epoch=capture_epoch,
            binding_name=binding_name,
            event=event,
            global_seq=global_seq,
            event_seq=event_seq,
            capture_mechanism=capture_mechanism,
        )
        return self.store.append_operation_effect(
            operation,
            CAPTURE_EVENT_EFFECT,
            metadata,
            substrate=binding_name,
        )

    def _record_capture_diagnostic(
        self,
        binding_name: str,
        event: Any,
        *,
        command_operation_id: str,
        capture_epoch: str | None = None,
        global_seq: int,
        event_seq: int,
        capture_mechanism: str,
        reason: str,
    ) -> str | None:
        """Record a non-authoritative diagnostic for a rejected command capture event."""
        scope = self._scope_for_capture_event(event)
        if scope is None:
            return None
        metadata = capture_event_metadata(
            command_operation_id=command_operation_id,
            capture_epoch=capture_epoch,
            binding_name=binding_name,
            event=event,
            global_seq=global_seq,
            event_seq=event_seq,
            capture_mechanism=capture_mechanism,
        )
        metadata["capture_status"] = "incomplete"
        metadata["capture_incomplete_reason"] = reason
        diagnostic_id = f"diag_{command_operation_id}_{global_seq}"
        if self.store.operation_id_exists(diagnostic_id):
            return None
        diagnostic = begin_capture_diagnostic_operation(
            self,
            scope,
            handle_id=diagnostic_id,
            world_id=self._scope_world_id(scope),
            scope_instance_id=scope.instance_id,
            operation_id=diagnostic_id,
            operation_label=f"capture diagnostic: {command_operation_id}",
            session_id=self._session_id,
            metadata={
                "capture": {
                    "command_operation_id": command_operation_id,
                    "capture_status": "incomplete",
                    "capture_stream_status": "rejected",
                    "capture_incomplete_reason": reason,
                    "covered_paths": [metadata["path"]],
                    "event_count": 1,
                }
            },
        )
        self.store.append_operation_effect(
            diagnostic,
            CAPTURE_EVENT_EFFECT,
            metadata,
            substrate=binding_name,
        )
        return self.store.finalize_operation(
            diagnostic,
            scope=scope,
            metadata={
                "capture": {
                    "command_operation_id": command_operation_id,
                    "capture_status": "incomplete",
                    "capture_stream_status": "rejected",
                    "capture_incomplete_reason": reason,
                    "covered_paths": [metadata["path"]],
                    "event_count": 1,
                }
            },
        )

    def _reduce_capture_for_command_operation(
        self,
        operation_id: str,
        *,
        command_metadata: dict[str, object],
    ) -> str | None:
        """Emit a linked scope-visible reducer operation for a captured command."""
        if command_metadata.get("status") is None:
            return None
        operation = self._find_open_session_exec_operation(operation_id)
        history = self.store.read_operation_history(operation.ref)
        events = ordered_capture_events(history.commits)
        if not events:
            return None
        scope = self._scope_for_operation(operation)
        filesystem = self._filesystem_substrate()
        if filesystem is None or not hasattr(filesystem, "effects_for_capture_reduction"):
            return None

        failed_origin = _failed_command_origin(operation_id, command_metadata)
        effects = filesystem.effects_for_capture_reduction(
            scope,
            events,
            failed_command_origin=failed_origin,
        )
        covered_paths = covered_capture_paths(events)
        reducer_id = reduction_operation_id(operation_id)
        if self.store.operation_id_exists(reducer_id):
            return None
        reducer_guard = OpenOperationGuard(self.store)
        try:
            reducer = reducer_guard.arm(
                begin_capture_reduction_operation(
                    self,
                    scope,
                    handle_id=reducer_id,
                    world_id=self._scope_world_id(scope),
                    scope_instance_id=scope.instance_id,
                    operation_id=reducer_id,
                    operation_label=f"capture reduction: {operation_id}",
                    session_id=self._session_id,
                    metadata={
                        "capture": {
                            "command_operation_id": operation_id,
                            "capture_status": "complete",
                            "capture_stream_status": "drained",
                            "covered_paths": list(covered_paths),
                            "event_count": len(events),
                        }
                    },
                )
            )
            for effect in effects:
                self.store.append_operation_effect(
                    reducer,
                    effect.effect_type,
                    effect.metadata,
                    list(effect.workspace_changes),
                    substrate="filesystem",
                )
            reducer_oid = reducer_guard.finalize(
                scope=scope,
                metadata={
                    "capture": {
                        "command_operation_id": operation_id,
                        "capture_status": "complete",
                        "capture_stream_status": "drained",
                        "covered_paths": list(covered_paths),
                        "event_count": len(events),
                        "reduced_effect_count": len(effects),
                    }
                },
            )
            try:
                self._shadow_workspace_capture_reduction(
                    scope=scope,
                    operation_id=operation_id,
                    reducer_id=reducer_id,
                    effects=effects,
                    events=events,
                    covered_paths=covered_paths,
                    failed_command_origin=failed_origin,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to shadow capture reduction %s through workspace driver.",
                    reducer_id,
                    exc_info=True,
                )
            return reducer_oid
        except Exception:
            reducer_guard.abort(
                metadata={
                    "capture": {
                        "command_operation_id": operation_id,
                        "capture_status": "incomplete",
                        "capture_stream_status": "incomplete",
                        "capture_incomplete_reason": "capture_reduction_failed",
                        "covered_paths": list(covered_paths),
                        "event_count": len(events),
                        "reduced_effect_count": len(effects),
                    }
                },
                status="error",
            )
            raise

    def _capture_covered_paths_for_scope(self, scope: ScopeInfo) -> frozenset[str]:
        """Return paths covered by completed capture reductions on this scope."""
        paths: set[str] = set()
        for summary in self.visible_operations(ref=scope.ref, max_count=1000):
            if summary.kind != CAPTURE_REDUCTION_KIND:
                continue
            history = self.resolve_operation_history(summary.operation_id, scope=scope)
            for commit in history.commits:
                capture = commit.metadata.get("capture")
                if not isinstance(capture, dict) or capture.get("capture_status") != "complete":
                    continue
                covered = capture.get("covered_paths")
                if isinstance(covered, list):
                    paths.update(path for path in covered if isinstance(path, str))
        return frozenset(paths)

    def _find_open_session_exec_operation(self, operation_id: str) -> Any:
        for operation in self.store.list_open_operations():
            if operation.durable_id == operation_id and operation.kind == "vcs_core.session_exec":
                return operation
        raise ValueError(f"No open session exec envelope matches operation id {operation_id!r}.")

    def _scope_for_operation(self, operation: Any) -> ScopeInfo:
        if operation.scope_ref == self.store.GROUND_REF:
            return self.ground
        scope_name = operation.scope_ref.rsplit("/", 1)[-1]
        scope = self.lookup_scope(scope_name)
        if scope is None:
            raise ValueError(f"No live scope for capture operation {operation.durable_id!r}.")
        if scope.instance_id != operation.scope_instance_id:
            raise ValueError(f"Scope instance mismatch for capture operation {operation.durable_id!r}.")
        return scope

    def _scope_for_capture_event(self, event: Any) -> ScopeInfo | None:
        scope_name = getattr(event, "scope", None)
        scope_instance_id = getattr(event, "scope_instance_id", None)
        if not isinstance(scope_name, str) or not isinstance(scope_instance_id, str):
            return None
        scope = self.ground if scope_name == self.ground.name else self.lookup_scope(scope_name)
        if scope is None or scope.instance_id != scope_instance_id:
            return None
        return scope

    def _filesystem_substrate(self) -> Any | None:
        for binding in self.bindings:
            if binding.binding_name == "filesystem":
                return binding.instance
        return None

    def _world_storage(self) -> WorldStorageManager:
        from vcs_core._world_storage_installation import open_or_init_default_world_storage

        if self._world_storage_manager is None:
            self._world_storage_manager = open_or_init_default_world_storage(self._repo_path)
        return self._world_storage_manager

    def _shadow_workspace_capture_reduction(
        self,
        *,
        scope: ScopeInfo,
        operation_id: str,
        reducer_id: str,
        effects: Sequence[EffectRecord],
        events: Sequence[Any],
        covered_paths: Sequence[str],
        failed_command_origin: dict[str, object] | None,
    ) -> None:
        from vcs_core._workspace_capture_manifest import workspace_capture_state_from_store
        from vcs_core._world_substrate_adapters import WorkspaceSubstrateAdapter, WorkspaceSubstrateDriver

        manager = self._world_storage()
        workspace = WorkspaceSubstrateAdapter(
            manager,
            driver=WorkspaceSubstrateDriver(),
        )
        raw = workspace.persist_capture_history_evidence(
            command_operation_id=operation_id,
            capture_events=tuple(events),
        )
        reduction = workspace_capture_state_from_store(
            store=self.store,
            scope=scope,
            command_operation_id=operation_id,
            effects=effects,
            covered_paths=covered_paths,
            event_count=len(events),
            failed_command_origin=failed_command_origin,
            tree_backed=True,
        )
        candidate_parents = self._current_v2_workspace_heads(manager, scope.ref)
        bundle = workspace.create_capture_reduction_candidate_from_evidence(
            operation_id=reducer_id,
            command_operation_id=operation_id,
            payload=reduction.payload,
            reduction_batch=manager.build_reduction_batch(raw.evidence_refs, citation_prefix="raw"),
            reduced_state_proof=reduction.reduced_state_proof,
            parents=candidate_parents,
            message=f"workspace capture reduction: {operation_id}",
            workspace_tree_oid=reduction.workspace_tree_oid,
        )
        self._select_workspace_driver_candidate(
            scope=scope,
            manager=manager,
            workspace=workspace,
            bundle=bundle,
            operation_id=reducer_id,
            operation_kind="workspace-capture-selection",
            semantic_op="workspace-capture-selection",
        )

    def _select_workspace_state_for_runtime_effects(
        self,
        effects: Sequence[EffectRecord],
        *,
        substrate: str,
        scope: ScopeInfo,
        operation_id: str,
        driver_command: str,
        workspace_output_binding: str = "workspace",
        workspace_effect_overlay: bool = False,
    ) -> None:
        if substrate != "filesystem" or not any(effect.workspace_changes for effect in effects):
            return
        self._select_workspace_state_from_store_required(
            scope=scope,
            operation_id=self._workspace_driver_operation_id(
                driver_command,
                operation_id,
                scope,
                workspace_output_binding=workspace_output_binding,
            ),
            source_operation_id=operation_id,
            driver_command=driver_command,
            message=f"workspace {driver_command}: {operation_id}",
            effects=effects,
            workspace_output_binding=workspace_output_binding,
            workspace_effect_overlay=workspace_effect_overlay,
        )

    def _queue_workspace_state_for_runtime_effects(
        self,
        effects: Sequence[EffectRecord],
        *,
        substrate: str,
        scope: ScopeInfo,
        operation_id: str,
        driver_command: str,
        workspace_output_binding: str = "workspace",
        workspace_effect_overlay: bool = False,
    ) -> None:
        if substrate != "filesystem" or not any(effect.workspace_changes for effect in effects):
            return
        pending = self._pending_workspace_driver_effects.get(operation_id)
        if pending is None:
            self._pending_workspace_driver_effects[operation_id] = (
                scope,
                list(effects),
                driver_command,
                workspace_output_binding,
                workspace_effect_overlay,
            )
            return
        pending_scope, pending_effects, pending_command, pending_binding, pending_effect_overlay = pending
        if pending_scope.ref != scope.ref or pending_scope.instance_id != scope.instance_id:
            raise RuntimeError("Workspace driver runtime effects for one operation must stay in one scope.")
        if pending_binding != workspace_output_binding:
            raise RuntimeError("Workspace driver runtime effects for one operation must target one output binding.")
        if pending_effect_overlay != workspace_effect_overlay:
            raise RuntimeError("Workspace driver runtime effects for one operation must use one state derivation mode.")
        pending_effects.extend(effects)
        self._pending_workspace_driver_effects[operation_id] = (
            pending_scope,
            pending_effects,
            pending_command if pending_command != "python-runtime-capture" else driver_command,
            pending_binding,
            pending_effect_overlay,
        )

    def _flush_workspace_state_for_runtime_operation(self, operation_id: str) -> None:
        pending = self._pending_workspace_driver_effects.pop(operation_id, None)
        if pending is None:
            return
        scope, effects, driver_command, workspace_output_binding, workspace_effect_overlay = pending
        self._select_workspace_state_for_runtime_effects(
            effects,
            substrate="filesystem",
            scope=scope,
            operation_id=operation_id,
            driver_command=driver_command,
            workspace_output_binding=workspace_output_binding,
            workspace_effect_overlay=workspace_effect_overlay,
        )

    def _select_workspace_state_from_store_required(
        self,
        *,
        scope: ScopeInfo,
        operation_id: str,
        source_operation_id: str,
        driver_command: str,
        message: str | None = None,
        advance_materialized: bool = False,
        effects: Sequence[EffectRecord] | None = None,
        workspace_output_binding: str = "workspace",
        workspace_effect_overlay: bool = False,
    ) -> None:
        pending = self._workspace_authority_pending(
            scope=scope,
            operation_id=operation_id,
            source_operation_id=source_operation_id,
            driver_command=driver_command,
            advance_materialized=advance_materialized,
            workspace_output_binding=workspace_output_binding,
        )
        write_pending_workspace_authority(self._repo_path, pending)
        self._select_workspace_state_from_store(
            scope=scope,
            operation_id=operation_id,
            source_operation_id=source_operation_id,
            driver_command=driver_command,
            message=message,
            effects=effects,
            workspace_output_binding=workspace_output_binding,
            workspace_effect_overlay=workspace_effect_overlay,
        )
        clear_pending_workspace_authority(self._repo_path, operation_id)

    def _workspace_authority_pending(
        self,
        *,
        scope: ScopeInfo,
        operation_id: str,
        source_operation_id: str,
        driver_command: str,
        advance_materialized: bool = False,
        workspace_output_binding: str = "workspace",
    ) -> WorkspaceAuthorityPending:
        manager = self._world_storage()
        source_commit = self.store.resolve_to_commit(scope.ref)
        return WorkspaceAuthorityPending(
            operation_id=operation_id,
            source_operation_id=source_operation_id,
            driver_command=driver_command,
            scope_name=scope.name,
            scope_ref=scope.ref,
            scope_instance_id=scope.instance_id,
            scope_world_id=scope.world_id,
            expected_input_world_oid=self._current_v2_world_oid(manager, scope.ref),
            scalar_source_commit=str(source_commit.id) if source_commit is not None else None,
            workspace_output_binding=workspace_output_binding,
            advance_materialized=advance_materialized,
        ).with_update(phase="scalar_committed")

    def _select_workspace_state_from_store(
        self,
        *,
        scope: ScopeInfo,
        operation_id: str,
        driver_command: str,
        message: str | None = None,
        source_operation_id: str | None = None,
        effects: Sequence[EffectRecord] | None = None,
        workspace_output_binding: str = "workspace",
        workspace_effect_overlay: bool = False,
    ) -> None:
        from vcs_core._workspace_capture_manifest import workspace_state_payload_from_store
        from vcs_core._world_substrate_adapters import WorkspaceSubstrateAdapter, WorkspaceSubstrateDriver

        if workspace_output_binding != "workspace":
            raise InvalidRepositoryStateError(
                f"workspace state selection can only publish the workspace binding; got {workspace_output_binding!r}."
            )
        manager = self._world_storage()
        workspace = WorkspaceSubstrateAdapter(
            manager,
            driver=WorkspaceSubstrateDriver(binding=workspace_output_binding),
        )
        state_effects = effects or ()
        if not workspace_effect_overlay:
            state_effects = ()
        state = workspace_state_payload_from_store(
            store=self.store,
            scope=scope,
            tree_backed=True,
            effects=state_effects,
        )
        candidate_parents = self._current_v2_binding_heads(manager, scope.ref, workspace_output_binding)
        if driver_command == "scan":
            bundle = workspace.create_scan_candidate(
                operation_id=operation_id,
                payload=state.payload,
                parents=candidate_parents,
                message=message,
                workspace_tree_oid=state.workspace_tree_oid,
            )
            operation_kind = "workspace-scan-selection"
            semantic_op = "workspace-scan-selection"
        elif driver_command == "adopt-baseline":
            bundle = workspace.create_adoption_candidate(
                operation_id=operation_id,
                payload=state.payload,
                parents=candidate_parents,
                message=message,
                workspace_tree_oid=state.workspace_tree_oid,
            )
            operation_kind = "workspace-adoption-selection"
            semantic_op = "workspace-adoption-selection"
        elif driver_command == "overlay-merge":
            bundle = workspace.create_overlay_merge_candidate(
                operation_id=operation_id,
                payload=state.payload,
                parents=candidate_parents,
                message=message,
                workspace_tree_oid=state.workspace_tree_oid,
            )
            operation_kind = "workspace-overlay-merge-selection"
            semantic_op = "workspace-overlay-merge-selection"
        elif driver_command == "python-runtime-capture":
            # T2c: Python-tier capture via the typed CaptureAdapter →
            # coordinator persist → ReduceRequest flow (replaces the
            # pre-T2c misclassification as "scan" which set
            # ingress_kind="command" on Python-tier writes).
            if source_operation_id is None or effects is None:
                raise InvalidRepositoryStateError(
                    "python-runtime-capture branch requires source_operation_id and effects"
                )
            bundle = self._python_runtime_capture_candidate(
                workspace=workspace,
                manager=manager,
                scope=scope,
                operation_id=operation_id,
                source_operation_id=source_operation_id,
                state=state,
                candidate_parents=candidate_parents,
                effects=effects,
                message=message,
            )
            operation_kind = "workspace-capture-reduction-selection"
            semantic_op = "workspace-capture-reduction-selection"
        else:
            raise ValueError(f"unsupported workspace driver command: {driver_command!r}")
        self._select_workspace_driver_candidate(
            scope=scope,
            manager=manager,
            workspace=workspace,
            bundle=bundle,
            operation_id=operation_id,
            operation_kind=operation_kind,
            semantic_op=semantic_op,
            carry_existing_heads=workspace_output_binding == "workspace",
        )

    def _workspace_driver_operation_id(
        self,
        driver_command: str,
        operation_id: str,
        scope: ScopeInfo,
        *,
        workspace_output_binding: str = "workspace",
    ) -> str:
        source_commit = self.store.resolve_to_commit(scope.ref)
        source_suffix = "nohead" if source_commit is None else str(source_commit.id)[:12]
        safe_command = driver_command.replace("-", "_")
        if workspace_output_binding == "workspace":
            return f"wv_{safe_command}_{operation_id}_{source_suffix}"
        safe_binding = workspace_output_binding.replace("-", "_")
        return f"wv_{safe_command}_{safe_binding}_{operation_id}_{source_suffix}"

    def recover_workspace_authority(self, mode: str = "resume") -> tuple[str, ...]:
        if mode != "resume":
            raise ValueError(f"Unknown workspace authority recovery mode: {mode!r}")
        from vcs_core._readiness_admission import (
            require_recovery_targets_allowed,
            workspace_authority_operation_journal_recovery_targets,
            workspace_authority_recovery_targets,
            workspace_authority_related_recovery_targets,
        )
        from vcs_core._workspace_authority_inventory import probe_workspace_authority_pending

        recovered: list[str] = []
        with self._lock:
            require_recovery_targets_allowed(
                self,
                attempted="recover workspace authority",
                targets=(
                    *workspace_authority_recovery_targets(self),
                    *workspace_authority_related_recovery_targets(self),
                    *workspace_authority_operation_journal_recovery_targets(self),
                ),
            )
            for item in probe_workspace_authority_pending(self._repo_path):
                if item.health.validity != "valid":
                    issue_codes = ", ".join(item.health.issue_codes) or item.health.primary_issue
                    raise InvalidRepositoryStateError(
                        f"workspace authority inventory item {item.id!r} is invalid: {issue_codes}"
                    )
            for pending in pending_workspace_authority_records(self._repo_path):
                self._recover_workspace_authority_pending(pending)
                recovered.append(pending.operation_id)
        return tuple(recovered)

    def recover_open_operation_journal_index(self) -> bool:
        """Rebuild a non-fresh open-journal accelerator from authority — a PROJECTION-ONLY repair.

        Returns ``False`` when the index is fresh or merely missing (a missing index self-heals via
        admission's read-only fallback, so it needs no recovery — consistent with
        ``fsck_operation_journals``). Otherwise rebuilds directly from the authoritative open refs,
        for both **corrupt** records and **stale** drift (over- or under-reporting a valid record).

        This is a *projection* rebuild — it READS authoritative open refs and WRITES only the derived
        index — so it is always safe and is deliberately **not** gated through recovery admission.
        Gating it would let an UNRELATED pending fact (e.g. a workspace-authority recovery) block the
        repair, which deadlocks against the corrupt-index fact that blocks that other recovery in
        turn — leaving no valid recovery ordering. The corrupt index still blocks ORDINARY mutation
        (and other authority recoveries) at the admission gate; this is the always-available repair
        that clears it, so it is the correct first recovery step. The authority full scan runs off
        the hot path; the authoritative open refs are unaffected.
        """
        with self._lock:
            manager = self._world_storage()
            if manager.verify_open_operation_journal_index().status not in ("stale", "corrupt"):
                return False
            manager.rebuild_open_operation_journal_index()
            return True

    def _recover_workspace_authority_pending(self, pending: WorkspaceAuthorityPending) -> None:
        scope = ScopeInfo(
            name=pending.scope_name,
            ref=pending.scope_ref,
            instance_id=pending.scope_instance_id,
            creation_oid="",
            world_id=pending.scope_world_id,
        )
        source_commit = self.store.resolve_to_commit(scope.ref)
        current_source = str(source_commit.id) if source_commit is not None else None
        if current_source != pending.scalar_source_commit:
            raise InvalidRepositoryStateError(
                f"Cannot recover workspace authority {pending.operation_id!r}: scalar source commit changed"
            )
        manager = self._world_storage()
        finalizer = WorldAuthorityFinalizer(manager)
        existing = finalizer.complete_existing(
            operation_id=pending.operation_id,
            target_ref=pending.scope_ref,
            expected_input_world_oid=pending.expected_input_world_oid,
            missing_ok=True,
        )
        if existing is not None and existing.status != "retry_required":
            if existing.world_oid is not None and finalizer.authority_ref_protects_world(
                pending.scope_ref,
                existing.world_oid,
            ):
                if pending.advance_materialized:
                    self.store.advance_materialized()
                clear_pending_workspace_authority(self._repo_path, pending.operation_id)
                return
            raise InvalidRepositoryStateError(
                f"Cannot recover workspace authority {pending.operation_id!r}: v2 authority ref changed"
            )
        current_input = self._current_v2_world_oid(manager, pending.scope_ref)
        if current_input != pending.expected_input_world_oid:
            raise InvalidRepositoryStateError(
                f"Cannot recover workspace authority {pending.operation_id!r}: v2 authority ref changed"
            )
        operation_id = pending.operation_id
        retry_required = existing is not None and existing.status == "retry_required"
        if existing is None:
            workspace_candidate_ref = candidate_ref(operation_id, pending.workspace_output_binding)
            retry_required = workspace_candidate_ref in manager.store("store_workspace").repo.references
        if retry_required:
            operation_id = finalizer.retry_operation_id(pending.operation_id, pending.retry_count + 1)
            replacement = pending.with_update(operation_id=operation_id, retry_count=pending.retry_count + 1)
            write_pending_workspace_authority(self._repo_path, replacement)
            clear_pending_workspace_authority(self._repo_path, pending.operation_id)
            pending = replacement
        # Pragmatic accommodation (T2c): the python-runtime-capture branch
        # requires ``effects`` to rebuild the evidence batch, but recovery
        # does not have the in-memory effects from the original command
        # (they were processed during the original ``mg.exec(...)`` and
        # not persisted as recovery-replayable state). On recovery, fall
        # back to scan semantics for python-runtime-capture pending records.
        # The resulting candidate is ``workspace-scan`` instead of
        # ``workspace-capture-reduction``; the state-manifest payload is
        # the same. T3 / a follow-on can implement full recovery (look up
        # the original observation evidence by operation id) if the
        # classification matters for recovery audit trails.
        recovery_driver_command = (
            "scan" if pending.driver_command == "python-runtime-capture" else pending.driver_command
        )
        self._select_workspace_state_from_store(
            scope=scope,
            operation_id=operation_id,
            driver_command=recovery_driver_command,
            message=f"workspace authority recovery: {pending.source_operation_id}",
            workspace_output_binding=pending.workspace_output_binding,
        )
        if pending.advance_materialized:
            self.store.advance_materialized()
        clear_pending_workspace_authority(self._repo_path, pending.operation_id)

    def recover_authority_settlements(self) -> tuple[str, ...]:
        return _vcscore_lifecycle.recover_authority_settlements(self)

    def _python_runtime_capture_candidate(
        self,
        *,
        workspace: Any,
        manager: WorldStorageManager,
        scope: ScopeInfo,
        operation_id: str,
        source_operation_id: str,
        state: Any,
        candidate_parents: tuple[str, ...],
        effects: Sequence[EffectRecord],
        message: str | None,
    ) -> Any:
        """T2c Python-tier capture flow: adapter → persist → typed ReduceRequest.

        Replaces the pre-T2c misclassification of Python-tier writes as
        ``workspace-scan`` (``ingress_kind="command"``) with a proper
        capture-and-reduce flow producing a ``workspace-capture-reduction``
        candidate carrying ``ingress_kind="reduce"``.

        The flow:

        1. Translate ``EffectRecord.workspace_changes`` → python-runtime
           raw event dicts.
        2. Resolve ``PythonRuntimeCaptureAdapter`` from the per-VcsCore
           registry.
        3. Call ``adapter.parse(...)`` with a ``TupleSink`` to collect
           ``ObservationDraft`` values.
        4. Check parsed observations against the active surface before
           evidence persistence.
        5. Persist the observations as evidence-only via the coordinator
           (using the workspace driver's identity since the workspace
           binding owns the candidate; the adapter only owns parsing).
        6. Build a ``ReductionBatch`` from the persisted evidence refs.
        7. Compute the reduction proof (manifest digest + byte authority
           + command_operation_id).
        8. Dispatch ``ReduceRequest`` through the coordinator-enforced
           typed SPI v0.1 surface.
        9. Lower the result to a candidate via
           ``manager.create_prepared_driver_candidate_bundle(...,
           ingress_kind="reduce")``.
        """
        from vcs_core._substrate_driver import (
            DriverIngressResult,
            ReduceRequest,
            TupleSink,
        )
        from vcs_core._substrate_evidence_kinds import Mechanism
        from vcs_core._vcscore_runtime import python_runtime_events_from_effects
        from vcs_core._world_types import canonical_digest

        adapter = self._capture_adapter_by_mechanism(Mechanism.PYTHON_RUNTIME)
        if adapter is None:
            raise InvalidRepositoryStateError(
                "python-runtime capture adapter not registered; "
                "VcsCore.__init__ should register it via CaptureAdapterRegistry"
            )

        raw_events = python_runtime_events_from_effects(
            effects,
            command_operation_id=source_operation_id,
            binding_name=workspace.driver.binding,
        )

        driver = workspace.driver
        active_surface = self._active_surface()
        capture_ctx = workspace._context(
            operation_id=source_operation_id,
            parents=candidate_parents,
            active_surface=active_surface,
        )

        sink = TupleSink()
        adapter.parse(capture_ctx, raw_events, sink)
        observations_result = DriverIngressResult(
            observations=tuple(sink.observations),
        )
        manager.validate_active_surface_result(driver, capture_ctx, observations_result)

        persisted = manager.persist_driver_evidence_only(
            driver.store_id,
            operation_id=source_operation_id,
            binding=driver.binding,
            result=observations_result,
            ingress_kind="capture",
            driver_id=driver.driver_id,
            driver_version=driver.driver_version,
            envelope_id="python-runtime-capture",
        )

        reduction_batch = manager.build_reduction_batch(
            persisted.evidence_refs,
            citation_prefix="raw",
        )

        manifest = state.payload["state_manifest"]
        proof: dict[str, object] = {
            "byte_authority": manifest["byte_authority"],
            "manifest_digest": canonical_digest(manifest),
            "command_operation_id": source_operation_id,
        }

        # Inject workspace_tree_oid into the reduction_payload so the
        # workspace driver's typed ReduceRequest handler can pick it up
        # for tree-backed candidates. This is a workspace-specific runtime
        # convention between this caller and the workspace driver; the
        # generic ReduceRequest type doesn't need to know about it.
        reduction_payload = dict(state.payload)
        if state.workspace_tree_oid is not None:
            reduction_payload["git_tree_oid"] = state.workspace_tree_oid

        reduce_ctx = workspace._context(
            operation_id=operation_id,
            parents=candidate_parents,
            active_surface=active_surface,
        )
        request = ReduceRequest(
            evidence_citations=reduction_batch,
            reduction_payload=reduction_payload,
            reduction_proof=proof,
        )
        result = manager.dispatch_driver_ingress(driver, reduce_ctx, request)

        return manager.create_prepared_driver_candidate_bundle(
            driver.store_id,
            operation_id=operation_id,
            binding=driver.binding,
            result=result,
            driver_id=driver.driver_id,
            driver_version=driver.driver_version,
            parents=candidate_parents,
            ingress_kind="reduce",
            reduction_batch=reduction_batch,
            message=message,
        )

    def _select_workspace_driver_candidate(
        self,
        *,
        scope: ScopeInfo,
        manager: WorldStorageManager,
        workspace: Any,
        bundle: Any,
        operation_id: str,
        operation_kind: str,
        semantic_op: str,
        carry_existing_heads: bool = True,
    ) -> None:
        from vcs_core._world_operation_builder import OperationFinalBuilder
        from vcs_core._world_types import WORLD_TRANSITION_SCHEMA, WorldSnapshot

        target_ref = scope.ref
        input_world_oid = self._current_v2_world_oid(manager, target_ref)
        parents = () if input_world_oid is None else (input_world_oid,)
        transition: dict[str, object] = {
            "schema": WORLD_TRANSITION_SCHEMA,
            "operation_id": operation_id,
            "parent_worlds": list(parents),
            "semantic_op": semantic_op,
        }
        if input_world_oid is not None:
            transition["input_world"] = input_world_oid
        head = workspace.head(bundle.candidate.head)
        heads_by_binding = (
            {}
            if input_world_oid is None or not carry_existing_heads
            else manager.read_world(input_world_oid).snapshot.by_binding()
        )
        heads_by_binding[head.binding] = head
        plan = workspace.plan_candidate_selection(bundle)
        builder = OperationFinalBuilder(operation_id).select_candidate_plan(plan=plan)
        if input_world_oid is not None:
            for existing in heads_by_binding.values():
                if existing.binding == head.binding:
                    continue
                builder.select_unchanged(
                    plan=manager.plan_unchanged_selection(
                        operation_id=operation_id,
                        head=existing,
                        input_world_oid=input_world_oid,
                    )
                )
        prepared = builder.build_prepared(
            operation_kind=operation_kind,
            target_ref=target_ref,
            input_world_oid=input_world_oid,
            snapshot=WorldSnapshot.from_heads(heads_by_binding),
            transition=transition,
            parents=parents,
        )
        try:
            WorldAuthorityFinalizer(manager).publish_prepared(prepared)
        except InvalidRepositoryStateError as exc:
            raise InvalidRepositoryStateError(
                f"Failed to publish v2 workspace selection {operation_id!r}: {exc}"
            ) from exc

    @staticmethod
    def _current_v2_world_oid(manager: WorldStorageManager, ref: str) -> str | None:
        if ref not in manager.world_store.repo.references:
            return None
        return str(manager.world_store.repo.references[ref].target)

    @staticmethod
    def _current_v2_workspace_heads(manager: WorldStorageManager, ref: str) -> tuple[str, ...]:
        return VcsCore._current_v2_binding_heads(manager, ref, "workspace")

    @staticmethod
    def _current_v2_binding_heads(manager: WorldStorageManager, ref: str, binding: str) -> tuple[str, ...]:
        world_oid = VcsCore._current_v2_world_oid(manager, ref)
        if world_oid is None:
            return ()
        world = manager.read_world(world_oid)
        try:
            return (world.snapshot.head_for(binding).head,)
        except KeyError:
            return ()

    def _ground_workspace_head_and_metadata(
        self,
    ) -> tuple[Any, str, SubstrateRevisionMetadata] | None:
        """Return ``(manager, head, metadata)`` for the ground world's workspace head.

        Shared lookup for ``_read_v2_workspace_file_for_materialization`` and
        ``_ground_workspace_is_tree_backed``. Returns ``None`` when no default
        world storage exists, the ground authority ref is unset, the world has
        no workspace binding, or metadata cannot be read.
        """
        from vcs_core._world_storage_installation import default_world_storage_exists
        from vcs_core._world_storage_manager import DEFAULT_GROUND_REF

        if self._world_storage_manager is None and not default_world_storage_exists(self._repo_path):
            return None
        try:
            manager = self._world_storage()
        except InvalidRepositoryStateError:
            return None
        heads = self._current_v2_workspace_heads(manager, DEFAULT_GROUND_REF)
        if not heads:
            return None
        head = heads[0]
        substrate = manager.store("store_workspace")
        try:
            metadata = substrate.read_revision_metadata(head)
        except (KeyError, ValueError, InvalidRepositoryStateError):
            return None
        return manager, head, metadata

    def _read_v2_workspace_file_for_materialization(self, path: str) -> tuple[bytes, int] | None:
        """Return ``(content, filemode)`` from the ground v2 workspace tree when tree-backed.

        Tranche 3 byte-source for filesystem materialization. Returns ``None``
        (and the caller falls back to scalar ``Store.read_workspace_file``)
        when:

        - the default world storage installation does not exist yet;
        - the ground authority ref is unset (no v2 ground world published);
        - the world has no workspace binding;
        - the selected substrate revision is digest-only;
        - the path does not resolve in the substrate's workspace tree.
        """
        from vcs_core._substrate_tree_read import read_substrate_workspace_file

        resolved = self._ground_workspace_head_and_metadata()
        if resolved is None:
            return None
        manager, head, metadata = resolved
        if metadata.byte_authority != "tree-backed":
            return None
        substrate = manager.store("store_workspace")
        return read_substrate_workspace_file(substrate.repo, head, path)

    def _ground_workspace_is_tree_backed(self) -> bool:
        """Return ``True`` when the ground world's workspace head is tree-backed.

        Companion query to ``_read_v2_workspace_file_for_materialization``: the
        filesystem substrate calls this once per materialization to decide
        whether a byte-source miss on a diff path should emit a diagnostic
        warning. Returns ``False`` for digest-only revisions and for any
        situation in which the byte source would also return ``None``.
        """
        resolved = self._ground_workspace_head_and_metadata()
        return resolved is not None and resolved[2].byte_authority == "tree-backed"

    def _fork_v2_scope_world(self, scope: ScopeInfo, parent: ScopeInfo) -> None:
        from vcs_core._world_storage_installation import default_world_storage_exists

        if self._world_storage_manager is None and not default_world_storage_exists(self._repo_path):
            return
        manager = self._world_storage()
        parent_world_oid = self._current_v2_world_oid(manager, parent.ref)
        if parent_world_oid is None:
            return
        if not manager.fork_world_ref(
            ref=scope.ref,
            world_oid=parent_world_oid,
            forked_from_ref=parent.ref,
            forked_from_world_oid=parent_world_oid,
        ):
            raise InvalidRepositoryStateError(f"v2 scope world ref already exists: {scope.ref}")

    def _merge_v2_scope_world(self, scope: ScopeInfo, parent: ScopeInfo) -> None:
        from vcs_core._world_operation_builder import OperationFinalBuilder
        from vcs_core._world_storage_installation import default_world_storage_exists
        from vcs_core._world_types import WORLD_TRANSITION_SCHEMA, WorldSnapshot

        if self._world_storage_manager is None and not default_world_storage_exists(self._repo_path):
            return
        manager = self._world_storage()
        child_world_oid = self._current_v2_world_oid(manager, scope.ref)
        if child_world_oid is None:
            return
        parent_world_oid = self._current_v2_world_oid(manager, parent.ref)
        if parent_world_oid == child_world_oid:
            manager.world_store.repo.references[scope.ref].delete()
            return
        child_world = manager.read_world(child_world_oid)
        parents = () if parent_world_oid is None else (parent_world_oid,)
        parent_heads_by_binding = (
            {} if parent_world_oid is None else manager.read_world(parent_world_oid).snapshot.by_binding()
        )
        child_heads_by_binding = child_world.snapshot.by_binding()
        heads_by_binding = dict(parent_heads_by_binding)
        heads_by_binding.update(child_heads_by_binding)
        operation_id = f"world_merge_{scope.instance_id}_{encode_ref_component(parent.ref)}"

        def prepared_factory(current_operation_id: str) -> Any:
            transition: dict[str, object] = {
                "schema": WORLD_TRANSITION_SCHEMA,
                "operation_id": current_operation_id,
                "parent_worlds": list(parents),
                "semantic_op": "workspace-scope-merge",
                "merged_scope": scope.name,
                "merged_scope_instance_id": scope.instance_id,
                "parent_scope": parent.name,
            }
            if parent_world_oid is not None:
                transition["input_world"] = parent_world_oid
            builder = OperationFinalBuilder(current_operation_id)
            for binding, head in sorted(heads_by_binding.items()):
                parent_head = parent_heads_by_binding.get(binding)
                if parent_world_oid is not None and parent_head == head:
                    builder.select_unchanged(
                        plan=manager.plan_unchanged_selection(
                            operation_id=current_operation_id,
                            head=head,
                            input_world_oid=parent_world_oid,
                        )
                    )
                else:
                    builder.select_existing(
                        plan=manager.plan_existing_head_selection(
                            operation_id=current_operation_id,
                            head=head,
                            selection_kind="import",
                            correlation_id=scope.instance_id,
                        )
                    )
            return builder.build_prepared(
                operation_kind="workspace-scope-merge",
                target_ref=parent.ref,
                input_world_oid=parent_world_oid,
                snapshot=WorldSnapshot.from_heads(heads_by_binding),
                transition=transition,
                parents=parents,
            )

        try:
            WorldAuthorityFinalizer(manager).publish_or_recover(
                operation_id=operation_id,
                prepared_factory=prepared_factory,
                target_ref=parent.ref,
                expected_input_world_oid=parent_world_oid,
            )
        except InvalidRepositoryStateError as exc:
            raise InvalidRepositoryStateError(f"Failed to publish v2 scope merge {operation_id!r}: {exc}") from exc
        if scope.ref in manager.world_store.repo.references:
            manager.world_store.repo.references[scope.ref].delete()

    def _discard_v2_scope_world(self, scope: ScopeInfo) -> None:
        from vcs_core._world_storage_installation import default_world_storage_exists

        if self._world_storage_manager is None and not default_world_storage_exists(self._repo_path):
            return
        manager = self._world_storage()
        if scope.ref not in manager.world_store.repo.references:
            return
        manager.world_store.repo.references[scope.ref].delete()

    # --- Materialization ---

    def push(
        self,
        dry_run: bool = False,
        up_to: str | None = None,
    ) -> MaterializationPlan:
        """Materialize: sync pending changes, advance ref.

        Raises OpenScopeError if a child scope is still live.
        """
        return _vcscore_materialization.push(self, dry_run=dry_run, up_to=up_to)

    def plan_push(self) -> MaterializationPlan:
        """Preview materialization after the same preflight used by push."""
        return _vcscore_materialization.plan_push(self)

    def assess_push(self) -> MaterializationAssessment:
        """Preview materialization and expected preflight blockers without recording reconcile state."""
        return _vcscore_materialization.assess_push(self)

    def reset_to_materialized(self) -> int:
        """Abandon unpushed work. Returns commits discarded."""
        return _vcscore_materialization.reset_to_materialized(self)

    def recover_dirty_push(self, mode: str = "repair") -> None:
        """Recover from a crashed push.

        mode="repair": Advance materialized ref and clear flag.
        mode="verify": Verify an already-recorded materialization run
            against external state when the active substrate set provides
            verify-capable materializers. Without such a run/materializer,
            raises NotImplementedError.
        mode="force": Rewind ground to materialized and clear flag.
        """
        self.recover_materialization(mode=mode)

    def recover_materialization(self, mode: str = "repair") -> MaterializationRecoveryReport:
        """Recover dirty-push and materialization-run recovery state."""
        return _vcscore_materialization.recover_materialization(self, mode=mode)

    def _make_ground_scope(self) -> ScopeInfo:
        return ScopeInfo(
            name="ground",
            ref=Store.GROUND_REF,
            instance_id=f"ground-{self._session_id}",
            creation_oid="",
            world_id=self._resolve_world_id(
                name="ground",
                ref=Store.GROUND_REF,
                instance_id=f"ground-{self._session_id}",
                world_id=None,
            ),
        )

    # --- Query delegations ---

    def status(self) -> Status:
        return _vcscore_queries.status(self)

    def diff(self) -> DiffSummary:
        return _vcscore_queries.diff(self)

    def log(self, ref: str | None = None, max_count: int = 50) -> list[CommitInfo]:
        return _vcscore_queries.log(self, ref=ref, max_count=max_count)

    def filter_effects(
        self,
        effect_type: str | None = None,
        substrate: str | None = None,
        ref: str | None = None,
        max_count: int = 100,
        scope: str | None = None,
    ) -> list[CommitInfo]:
        return _vcscore_queries.filter_effects(
            self,
            effect_type=effect_type,
            substrate=substrate,
            ref=ref,
            max_count=max_count,
            scope=scope,
        )

    def visible_operations(self, *, ref: str | None = None, max_count: int = 50) -> list[OperationSummary]:
        """Return operation summaries visible on the committed ref history."""
        return _vcscore_queries.visible_operations(self, ref=ref, max_count=max_count)

    def open_operations(
        self,
        *,
        scope: ScopeInfo | None = None,
        session_id: str | None = None,
    ) -> list[OperationSummary]:
        """Return staged-operation summaries for currently open operation refs."""
        return _vcscore_queries.open_operations(self, scope=scope, session_id=session_id)

    def world_oid(self, scope: ScopeInfo | None = None) -> str | None:
        """Durable v2 world-commit OID for ``scope`` (ground when ``None``).

        The run-identity readback ("``run`` returns identity" —
        containment-and-carriers.md §5 step 12): read before a run for the
        input/rewind handle, after for the output identity. ``None`` before the
        first world publication on the ref. Identity *composition* stays
        run-internal (runtime-call-api.md §2 Group E).

        Ground reads are intentionally available before activation so
        query-only facades can inspect selected-world state without taking the
        session lock. Non-ground scope reads still require the caller to supply
        an explicit scope value.
        """
        target_ref = GROUND_REF if scope is None else scope.ref
        return VcsCore._current_v2_world_oid(self._world_storage(), target_ref)

    def read_selected_binding_revision(
        self,
        binding_name: str,
        *,
        scope: ScopeInfo | None = None,
    ) -> dict[str, object] | None:
        """Read the current selected revision payload for one binding.

        This is the generic read-only twin of ``read_trace_revision(head=None)``:
        it resolves the current world for ``scope`` (ground when omitted), finds
        the selected head for ``binding_name``, and reads that head's payload
        through the owning substrate store. It does not expose world-store
        internals or compose identity on behalf of callers.
        """
        if not binding_name:
            raise ValueError("binding_name is required")
        selected = self.read_selected_binding_revision_with_head(binding_name, scope=scope)
        return None if selected is None else selected.payload

    def read_selected_binding_revision_with_head(
        self,
        binding_name: str,
        *,
        scope: ScopeInfo | None = None,
    ) -> SelectedBindingRevision | None:
        """Read the current selected revision payload and selected-head identity."""
        if not binding_name:
            raise ValueError("binding_name is required")
        manager = self._world_storage()
        world = self.world_oid(scope)
        if world is None:
            return None
        selected = manager.read_world(world).snapshot.by_binding().get(binding_name)
        if selected is None:
            return None
        return SelectedBindingRevision(
            binding=selected.binding,
            store_id=selected.store_id,
            resource_id=selected.resource_id,
            head=selected.head,
            payload=manager.store(selected.store_id).read_revision_payload(selected.head),
        )

    def read_selected_binding_entry(
        self,
        binding_name: str,
        path: str,
        *,
        scope: ScopeInfo | None = None,
    ) -> bytes | None:
        """Read one addressable blob from the selected revision for a binding."""
        if not binding_name:
            raise ValueError("binding_name is required")
        manager = self._world_storage()
        world = self.world_oid(scope)
        if world is None:
            return None
        selected = manager.read_world(world).snapshot.by_binding().get(binding_name)
        if selected is None:
            return None
        return manager.store(selected.store_id).read_revision_entry(selected.head, path)

    def read_selected_binding_json_entry(
        self,
        binding_name: str,
        path: str,
        *,
        scope: ScopeInfo | None = None,
    ) -> dict[str, object] | None:
        """Read one JSON-object blob from the selected revision for a binding."""
        if not binding_name:
            raise ValueError("binding_name is required")
        manager = self._world_storage()
        world = self.world_oid(scope)
        if world is None:
            return None
        selected = manager.read_world(world).snapshot.by_binding().get(binding_name)
        if selected is None:
            return None
        return manager.store(selected.store_id).read_revision_json_entry(selected.head, path)

    def read_selected_binding_json_entries(
        self,
        binding_name: str,
        prefix: str,
        *,
        scope: ScopeInfo | None = None,
    ) -> tuple[tuple[str, dict[str, object]], ...]:
        """Read JSON-object blobs under a prefix from the selected revision for a binding."""
        if not binding_name:
            raise ValueError("binding_name is required")
        manager = self._world_storage()
        world = self.world_oid(scope)
        if world is None:
            return ()
        selected = manager.read_world(world).snapshot.by_binding().get(binding_name)
        if selected is None:
            return ()
        return manager.store(selected.store_id).read_revision_json_entries(selected.head, prefix)

    def read_binding_revision(
        self,
        binding_name: str,
        head: str,
        *,
        scope: ScopeInfo | None = None,
        store_id: str | None = None,
        resource_id: str | None = None,
    ) -> dict[str, object]:
        """Read a specific substrate revision head for a known binding.

        The current selected world is used only to validate the binding's live
        store/resource identity when possible; the payload is read from
        ``head`` directly, so old selected heads remain dereferenceable.
        """
        if not binding_name:
            raise ValueError("binding_name is required")
        if not head:
            raise ValueError("head is required")
        manager = self._world_storage()
        selected = self.read_selected_binding_revision_with_head(binding_name, scope=scope)
        resolved_store_id = store_id
        if selected is not None:
            if store_id is not None and selected.store_id != store_id:
                raise ValueError(
                    f"binding {binding_name!r} selected store_id {selected.store_id!r} "
                    f"does not match expected {store_id!r}"
                )
            if resource_id is not None and selected.resource_id != resource_id:
                raise ValueError(
                    f"binding {binding_name!r} selected resource_id {selected.resource_id!r} "
                    f"does not match expected {resource_id!r}"
                )
            resolved_store_id = selected.store_id
        if resolved_store_id is None:
            bound = self._bindings_by_name.get(binding_name)
            driver = None if bound is None else bound.instance
            resolved_store_id = getattr(driver, "store_id", None)
        if resolved_store_id is None:
            raise ValueError(f"store_id is required to read unselected binding {binding_name!r}")
        store = manager.store(resolved_store_id)
        if resource_id is not None and store.identity.resource_id != resource_id:
            raise ValueError(
                f"store {resolved_store_id!r} resource_id {store.identity.resource_id!r} "
                f"does not match expected {resource_id!r}"
            )
        return store.read_revision_payload(head)

    def retained_workspace_handle(self, scope: ScopeInfo | str) -> RetainedWorkspaceHandle:
        """Return a copyable read handle for a sealed retained workspace."""
        from vcs_core._vcscore_seal import retained_workspace_handle

        return retained_workspace_handle(self, scope)

    def retained_workspace_handoff(
        self,
        scope_or_handle: ScopeInfo | RetainedWorkspaceHandle | str,
    ) -> SealCandidateHandoff:
        """Return the validated durable handoff for a retained workspace."""
        from vcs_core._vcscore_seal import retained_workspace_handoff

        return retained_workspace_handoff(self, scope_or_handle)

    def read_retained_workspace_file(self, scope: ScopeInfo | str, path: str) -> tuple[bytes, int] | None:
        """Read one file from a retained workspace's durable tree-backed basis."""
        from vcs_core._vcscore_seal import read_retained_workspace_file

        return read_retained_workspace_file(self, scope, path)

    def list_retained_outputs(
        self,
        *,
        parent: ScopeInfo | str | None = None,
        binding: str | None = None,
        state: RetainedOutputState | None = None,
    ) -> tuple[RetainedOutputQueryResult, ...]:
        """Classify retained outputs from lower-layer custody and settlement facts."""
        from vcs_core._retained_output_queries import list_retained_outputs

        return list_retained_outputs(self, parent=parent, binding=binding, state=state)

    def get_retained_output(self, identity: RetainedOutputIdentity) -> RetainedOutputQueryResult | None:
        """Classify one retained output from exact lower-layer custody identity."""
        from vcs_core._retained_output_queries import get_retained_output

        return get_retained_output(self, identity)

    def select_retained_output(
        self,
        scope_or_handle: ScopeInfo | RetainedWorkspaceHandle | str,
        *,
        parent: ScopeInfo,
        binding: str = "workspace",
        decide: RetainedOutputDecisionProvider | None = None,
        authority_operation_id: str | None = None,
        effective_match_digest: str | None = None,
        authority_surface_plan_digest: str | None = None,
        permission_plan_digest: str | None = None,
        permission_plan_descriptor: Mapping[str, object] | None = None,
        authority_context: Mapping[str, object] | None = None,
    ) -> RetainedOutputSelectionResult:
        """Select one retained binding output into a live parent binding.

        Provisional control-plane seed below the generalized RunOutput
        boundary-verb surface.
        """
        from vcs_core._retained_output_selection import select_retained_output

        return select_retained_output(
            self,
            scope_or_handle,
            parent=parent,
            binding=binding,
            decide=decide,
            authority_operation_id=authority_operation_id,
            effective_match_digest=effective_match_digest,
            authority_surface_plan_digest=authority_surface_plan_digest,
            permission_plan_digest=permission_plan_digest,
            permission_plan_descriptor=permission_plan_descriptor,
            authority_context=authority_context,
        )

    def release_retained_output(
        self,
        scope_or_handle: ScopeInfo | RetainedWorkspaceHandle | str,
        *,
        parent: ScopeInfo,
        binding: str = "workspace",
    ) -> RetainedOutputSettlementResult:
        """Consume one retained binding output without selecting it."""
        from vcs_core._retained_output_settlement_ops import release_retained_output

        return release_retained_output(self, scope_or_handle, parent=parent, binding=binding)

    def discard_retained_output(
        self,
        scope_or_handle: ScopeInfo | RetainedWorkspaceHandle | str,
        *,
        parent: ScopeInfo,
        binding: str = "workspace",
    ) -> RetainedOutputSettlementResult:
        """Consume one retained binding output as discarded without lifecycle discard."""
        from vcs_core._retained_output_settlement_ops import discard_retained_output

        return discard_retained_output(self, scope_or_handle, parent=parent, binding=binding)

    def read_trace_revision(
        self, head: str | None = None, *, scope: ScopeInfo | None = None
    ) -> dict[str, object] | None:
        """Read one durable trace-revision payload (B4b slice 3 W1).

        The inspection read the dialect's ``run.trace`` surface rides — the
        ``world_oid`` precedent: read-only, never composition. An explicit
        ``head`` reads that revision; ``head=None`` resolves the trace
        binding's currently selected head on ``scope``'s current world
        (ground when ``None``). Returns ``None`` when no world or no trace
        head exists yet. Payloads are digest-validated on read
        (``read_revision_payload`` — a torn payload is loud).
        """
        manager = self._world_storage()
        if head is None:
            return self.read_selected_binding_revision("trace", scope=scope)
        return manager.store("store_trace").read_revision_payload(head)

    def archived_operations(
        self,
        *,
        max_count: int = 50,
        world_id: str | None = None,
        operation_id: str | None = None,
    ) -> list[OperationSummary]:
        """Return archived-operation summaries, newest first."""
        return _vcscore_queries.archived_operations(
            self,
            max_count=max_count,
            world_id=world_id,
            operation_id=operation_id,
        )

    def operation_history(self, ref: str) -> OperationHistory:
        """Return the committed history carried by one operation ref."""
        return _vcscore_queries.operation_history(self, ref)

    def recovery_snapshot(self, *, archived_max_count: int = 50) -> RecoverySnapshot:
        """Return the current non-canonical recovery/debug state."""
        return _vcscore_queries.recovery_snapshot(self, archived_max_count=archived_max_count)

    def recovery_inventory(self) -> InventorySnapshot:
        """Return private inventory for current recovery/debug state."""
        from vcs_core._recovery_inventory import recovery_inventory_snapshot

        return recovery_inventory_snapshot(self)

    def query_readiness(
        self,
        request: ReadinessRequest | None = None,
    ) -> ReadinessResult:
        """Return first-cut Shepherd/vcs-core readiness under the coordinator lock."""
        return self._query_readiness_with_context(request, runtime_admission_context=None)

    def _query_readiness_for_runtime(
        self,
        request: ReadinessRequest | None = None,
        *,
        runtime_admission_context: RuntimeAdmissionContext | None,
    ) -> ReadinessResult:
        """Return readiness with coordinator-derived runtime admission authority."""
        return self._query_readiness_with_context(request, runtime_admission_context=runtime_admission_context)

    def _query_readiness_with_context(
        self,
        request: ReadinessRequest | None = None,
        *,
        runtime_admission_context: RuntimeAdmissionContext | None,
    ) -> ReadinessResult:
        """Shared readiness implementation for public and runtime-owned callers."""
        from vcs_core._query_readiness import ReadinessRequest, evaluate_readiness
        from vcs_core._readiness_admission import exclude_active_daemon_leases

        current_request = request or ReadinessRequest.create(command="shepherd.status")
        with self._lock:
            freshness = current_request.requested_freshness
            forced_freshness: ReadinessFreshness = "best_effort" if freshness == "best_effort" else "locked"
            result = evaluate_readiness(
                self._repo_path,
                current_request,
                owner=self,
                force_freshness=forced_freshness,
                runtime_admission_context=runtime_admission_context,
            )
            # Single chokepoint for both the runtime-enforcement and lifecycle
            # blocker-derivation paths: exclude the live daemon's own active
            # shell-capture lease from orphaned-op blockers (M3).
            return exclude_active_daemon_leases(self, result)

    def revalidate_readiness_precondition(
        self,
        request: ReadinessRequest,
        precondition: MutationPrecondition | Mapping[str, object],
    ) -> ReadinessResult:
        """Revalidate an opaque readiness precondition under the coordinator lock."""
        return self._revalidate_readiness_precondition_with_context(
            request,
            precondition,
            runtime_admission_context=None,
        )

    def _revalidate_readiness_precondition_for_runtime(
        self,
        request: ReadinessRequest,
        precondition: MutationPrecondition | Mapping[str, object],
        *,
        runtime_admission_context: RuntimeAdmissionContext | None,
    ) -> ReadinessResult:
        """Revalidate a precondition with coordinator-derived runtime authority."""
        return self._revalidate_readiness_precondition_with_context(
            request,
            precondition,
            runtime_admission_context=runtime_admission_context,
        )

    def _revalidate_readiness_precondition_with_context(
        self,
        request: ReadinessRequest,
        precondition: MutationPrecondition | Mapping[str, object],
        *,
        runtime_admission_context: RuntimeAdmissionContext | None,
    ) -> ReadinessResult:
        """Shared revalidation implementation for public and runtime-owned callers."""
        from vcs_core._query_readiness import revalidate_mutation_precondition

        with self._lock:
            return revalidate_mutation_precondition(
                self._repo_path,
                request,
                precondition,
                owner=self,
                runtime_admission_context=runtime_admission_context,
            )

    def _orphaned_operation_summaries(self) -> tuple[OperationSummary, ...]:
        return _vcscore_lifecycle._orphaned_operation_summaries(self)

    def _orphaned_operation_world_id(self, operation: OperationRefInfo) -> str:
        return _vcscore_lifecycle._orphaned_operation_world_id(self, operation)

    def resolve_operation_history(
        self,
        selector: str,
        *,
        scope: ScopeInfo | None = None,
        max_count: int = 200,
    ) -> OperationHistory:
        """Resolve one operation selector across visible, staged, and archived views."""
        return _vcscore_queries.resolve_operation_history(self, selector, scope=scope, max_count=max_count)

    def _operation_direct_matches(
        self,
        selector: str,
        *,
        scope: ScopeInfo | None,
    ) -> list[OperationSummary]:
        return _vcscore_queries.operation_direct_matches(self, selector, scope=scope)

    def _operation_id_matches(
        self,
        selector: str,
        *,
        scope: ScopeInfo | None,
    ) -> dict[str, OperationSummary]:
        return _vcscore_queries.operation_id_matches(self, selector, scope=scope)

    def _read_operation_summary_history(self, summary: OperationSummary) -> OperationHistory:
        return _vcscore_queries.read_operation_summary_history(self, summary)

    @staticmethod
    def _describe_operation_selector_match(summary: OperationSummary) -> str:
        return _vcscore_queries.describe_operation_selector_match(summary)

    def rebase(self, source: ScopeInfo, onto: ScopeInfo) -> RebaseResult:
        return self._store.rebase(source, onto.ref)

    def coverage(self) -> list[SubstrateAuthority]:
        """Return runtime authority reports for the active substrate set."""
        if self._ground is None:
            raise RuntimeError("VcsCore not activated. Call activate() first.")
        return [
            validate_authority_report(binding.substrate_type, binding.instance.authority())
            for binding in self._lifecycle_bindings
        ]

    def _resolve_binding(self, name: str) -> BoundSubstrate:
        binding = self._bindings_by_name.get(name)
        if binding is not None:
            return binding

        raise ValueError(f"Unknown binding: {name!r}")

    def _validate_scope(self, scope: ScopeInfo) -> None:
        if self._ground is None:
            raise RuntimeError("VcsCore not activated. Call activate() first.")

        if (
            scope.name == self._ground.name
            and scope.ref == self._ground.ref
            and scope.instance_id == self._ground.instance_id
        ):
            return

        tracked = self._active_scopes.get(scope.name)
        if tracked is None:
            raise StaleScopeError(f"Scope {scope.name!r} is not a live scope in this VcsCore session.")

        if tracked.ref != scope.ref or tracked.instance_id != scope.instance_id:
            raise StaleScopeError(f"Scope handle for {scope.name!r} is stale or belongs to another session.")

    @staticmethod
    def _scope_name_for_ref(ref: str) -> str:
        if ref == GROUND_REF:
            return "ground"
        return ref.rsplit("/", 1)[-1]

    def _format_operation_label(self, operation: OperationRefInfo) -> str:
        return operation.durable_id

    def _ensure_no_open_operation(self, attempted: str) -> None:
        _vcscore_lifecycle._ensure_no_open_operation(self, attempted)

    @contextmanager
    def _use_active_surface(self, surface: ActiveSurface) -> Iterator[None]:
        stack = self._active_surface_stack.get()
        token = self._active_surface_stack.set((*stack, surface))
        try:
            yield
        finally:
            self._active_surface_stack.reset(token)

    def _active_surface(self) -> ActiveSurface | None:
        stack = self._active_surface_stack.get()
        return stack[-1] if stack else None

    def _ensure_runtime_mutation_allowed(
        self,
        attempted: str,
        *,
        authorized_operations: tuple[ReadinessOperationAuthority, ...] = (),
        scope_selector: str | None = None,
        runtime_admission_context: RuntimeAdmissionContext | None = None,
    ) -> None:
        self._ensure_active_surface_allows_external_write(attempted)
        _vcscore_lifecycle._ensure_runtime_mutation_allowed(
            self,
            attempted,
            authorized_operations=authorized_operations,
            scope_selector=scope_selector,
            runtime_admission_context=runtime_admission_context,
        )

    def _ensure_active_surface_allows_external_write(self, attempted: str) -> None:
        """Refuse a patched-builtins (python-tier, in-process) write under a denying surface.

        Shares its allow/deny core with the session-capture admission gate via
        ``check_active_surface_admits`` rather than forking a divergent copy.
        """
        surface = self._active_surface()
        if surface is None or not attempted.startswith("external write via "):
            return

        from vcs_core._active_surface_profiles import check_active_surface_admits
        from vcs_core._substrate_evidence_kinds import EvidenceKind

        evidence_kind = (
            EvidenceKind.PYTHON_RUNTIME_DELETE
            if _external_write_attempt_is_delete(attempted)
            else EvidenceKind.PYTHON_RUNTIME_WRITE
        )
        check_active_surface_admits(
            surface,
            evidence_kind=evidence_kind,
            semantic_op="workspace-capture-reduction",
            operation=attempted,
        )

    def _ensure_active_surface_allows_session_capture(self, operation: str = "session exec --capture") -> None:
        """Refuse a capturing session exec when the active surface denies overlay writes (Rung B).

        The shepherd caller also enforces this caller-side via
        ``_active_surface_profiles.ensure_session_capture_admitted`` before
        dispatching to the session daemon.
        """
        from vcs_core._active_surface_profiles import ensure_session_capture_admitted

        ensure_session_capture_admitted(self._active_surface(), operation=operation)

    def list_sibling_group_blockers(self) -> tuple[str, ...]:
        return _vcscore_lifecycle.list_sibling_group_blockers(self)

    def list_workspace_authority_pending(self) -> tuple[str, ...]:
        return workspace_authority_operation_labels(self._repo_path)

    def list_authority_settlement_pending(self) -> tuple[str, ...]:
        return authority_settlement_pending_labels(self._repo_path)

    def authority_settlement_pending_records(self) -> tuple[dict[str, object], ...]:
        return tuple(record.to_dict() for record in read_valid_authority_settlement_pending_records(self._repo_path))

    def _archive_orphaned_operations_locked(
        self,
        operations: Sequence[OperationRefInfo],
    ) -> list[OperationRefInfo]:
        return _vcscore_lifecycle._archive_orphaned_operations_locked(self, operations)

    def _is_scope_or_ancestor_isolated(self, scope: ScopeInfo) -> bool:
        current = scope
        while True:
            if current.name in self._isolated_scopes:
                return True
            parent = self._scope_parents.get(current.name)
            if parent is None:
                return False
            current = parent

    def _overlay_base_scope_name(self, scope: ScopeInfo) -> str:
        current = scope
        while True:
            if current.name in self._isolated_scopes:
                return current.name
            parent = self._scope_parents.get(current.name)
            if parent is None:
                return self.ground.name
            current = parent

    def _working_directory_for_scope(self, scope: ScopeInfo) -> Path:
        filesystem = self._filesystem_overlay_substrate()
        if filesystem is None or not self._is_scope_or_ancestor_isolated(scope):
            return Path(self._workspace).resolve()
        layer_name = self._overlay_base_scope_name(scope)
        mount_path = filesystem.overlay_mount_path(layer_name)
        if mount_path is None:
            return Path(self._workspace).resolve()
        return mount_path

    def working_directory_for_scope(self, scope: ScopeInfo) -> Path:
        return self._working_directory_for_scope(scope)

    def _parent_scope(self, scope: ScopeInfo) -> ScopeInfo | None:
        if self._ground is not None and scope.name == self._ground.name:
            return None
        return self._scope_parents.get(scope.name)

    def _lookup_scope(self, name: str) -> ScopeInfo | None:
        if self._ground is not None and name == self._ground.name:
            return self._ground
        return self._active_scopes.get(name)

    def lookup_scope(self, name: str) -> ScopeInfo | None:
        return self._lookup_scope(name)

    def execute_recorded(
        self,
        binding_name: str,
        command: str,
        *,
        scope: ScopeInfo,
        execution_options: CommandExecutionOptions | None = None,
        **params: Any,
    ) -> RecordedCommandOutcome:
        return self._execute_recorded(
            binding_name,
            command,
            scope=scope,
            execution_options=execution_options,
            **params,
        )

    def overlay_mount_path_for_scope(self, scope: ScopeInfo) -> Path:
        filesystem = self._filesystem_overlay_substrate()
        if filesystem is None:
            return Path(self._workspace).resolve()
        base = self._overlay_base_scope_name(scope)
        if base == self.ground.name:
            # Ground is the real working copy, never a carrier layer. The loud
            # non-reversible opt-out (isolation="ground") must keep writing
            # auditable residue to the actual workspace even now that carrier
            # auto-resolution always finds a backend (copy-carrier floor).
            return Path(self._workspace).resolve()
        mount_path = filesystem.overlay_mount_path(base)
        if mount_path is None:
            return Path(self._workspace).resolve()
        return mount_path

    def overlay_changes_for_scope(self, scope: ScopeInfo) -> list[WorkspaceChange]:
        filesystem = self._filesystem_overlay_substrate()
        if filesystem is None:
            return []
        return filesystem.overlay_changes(self._overlay_base_scope_name(scope))

    def _register_carrier(self, substrate: str, target_id: str, scope: ScopeInfo) -> None:
        self._carrier_scopes[(substrate, target_id, scope.name)] = scope

    def _nearest_carrier_scope(self, substrate: str, target_id: str, scope: ScopeInfo) -> ScopeInfo | None:
        current = scope
        while True:
            carrier = self._carrier_scopes.get((substrate, target_id, current.name))
            if carrier is not None:
                return carrier
            parent = self._scope_parents.get(current.name)
            if parent is None:
                return None
            current = parent

    def _can_create_carrier(self, substrate: str, target_id: str, scope: ScopeInfo) -> bool:
        existing = self._nearest_carrier_scope(substrate, target_id, scope)
        if existing is None:
            return True
        if existing.name == scope.name:
            return False
        return scope.name in self._isolated_scopes

    def _register_claim(
        self,
        substrate: str,
        target_id: str,
        path: str | Path,
        policy: ClaimPolicy,
    ) -> ResourceClaim:
        return self._claim_registry.register(
            substrate=substrate,
            target_id=target_id,
            path=path,
            policy=policy,
        )

    def _lookup_claim(self, path: str | Path) -> ResourceClaim | None:
        return self._claim_registry.lookup(path)

    def _drop_scope_runtime_state(self, scope_name: str) -> None:
        self._isolated_scopes.discard(scope_name)
        self._carrier_scopes = {key: carrier for key, carrier in self._carrier_scopes.items() if key[2] != scope_name}
        self._parent_tree_manifests = {
            key: manifest
            for key, manifest in self._parent_tree_manifests.items()
            if key[0].rsplit("/", 1)[-1] != scope_name
        }

    def _notify_scope_merged(self, scope_name: str, parent_scope_name: str) -> None:
        for substrate in self._lifecycle_substrates:
            handler = getattr(substrate, "on_scope_merged", None)
            if callable(handler):
                try:
                    handler(scope_name, parent_scope_name)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Substrate %s raised during post-merge notification for scope %r; lifecycle state is already committed.",
                        getattr(substrate, "name", substrate),
                        scope_name,
                        exc_info=True,
                    )

    def _notify_scope_discarded(self, scope_name: str) -> None:
        for substrate in self._lifecycle_substrates:
            handler = getattr(substrate, "on_scope_discarded", None)
            if callable(handler):
                try:
                    handler(scope_name)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Substrate %s raised during post-discard notification for scope %r; lifecycle state is already committed.",
                        getattr(substrate, "name", substrate),
                        scope_name,
                        exc_info=True,
                    )

    def _filesystem_overlay_substrate(self) -> FilesystemSubstrate | None:
        for substrate in self._lifecycle_substrates:
            if isinstance(substrate, FilesystemSubstrate):
                return substrate
        return None

    # --- Config-driven construction ---

    @classmethod
    def from_config(
        cls,
        workspace: str,
        additional_substrates: list[object] | None = None,
    ) -> VcsCore:
        """Create VcsCore from config files, with optional programmatic substrates.

        Config-driven substrates are discovered and instantiated from
        vcscore.toml + .vcscore/config.toml. additional_substrates are
        appended after config-driven substrates.

        A single Store instance is shared between VcsCore and all substrates.

        Built-in substrates are instantiated through discovery with an
        internal construction context carrying Store/workspace/config.
        Third-party plugins receive only the public minimal init
        context. The cls() call below then creates the coordinator-owned
        runtime and binds it to built-ins before activation.
        """
        from vcs_core.config import load_config
        from vcs_core.discovery import resolve_bindings

        repo_path = os.path.join(workspace, ".vcscore")  # noqa: PTH118 — repo_path stays str for downstream string use
        store = Store.open_existing(repo_path)

        config = load_config(workspace)
        bindings = resolve_bindings(config, Path(workspace), store)
        if additional_substrates:
            bindings.extend(_default_bound_substrates(additional_substrates))

        return cls(workspace, bindings=bindings, store=store, allow_activate_init=False)


def _external_write_attempt_is_delete(attempted: str) -> bool:
    delete_tokens = (
        "os.remove",
        "os.unlink",
        "pathlib.Path.unlink",
        "shutil.rmtree",
    )
    return any(token in attempted for token in delete_tokens)
