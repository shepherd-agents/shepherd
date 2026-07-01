from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from vcs_core._lock import acquire_session_lock, release_session_lock
from vcs_core._materialization_coordinator import (
    FileMaterializationState,
    GroundScopeAccess,
    MaterializationAdmission,
    MaterializationCoordinator,
    MaterializationDependencies,
    SubstrateMaterializerSource,
)
from vcs_core._readiness_admission import (
    recovery_targets_for_kinds,
    require_readiness_allowed,
    require_recovery_targets_allowed,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vcs_core._materialization_coordinator import MaterializationRecoveryReport
    from vcs_core._query_readiness import ReadinessOperationAuthority
    from vcs_core.materialization import MaterializationAssessment
    from vcs_core.types import MaterializationPlan, ScopeInfo
    from vcs_core.vcscore import VcsCore


class _VcsCoreGroundScopeAccess(GroundScopeAccess):
    def __init__(self, owner: VcsCore) -> None:
        self._owner = owner

    def get(self) -> ScopeInfo | None:
        return self._owner._ground

    def set(self, scope: ScopeInfo | None) -> None:
        self._owner._ground = scope

    def make(self) -> ScopeInfo:
        return self._owner._make_ground_scope()


def _is_external_workspace_path_admitted(owner: VcsCore, path: Path) -> bool:
    claim = owner._lookup_claim(path)
    return claim is not None and claim.policy in {"exclusive", "authoritative_suppress_fs"}


def _dependencies(owner: VcsCore) -> MaterializationDependencies:
    workspace = Path(getattr(owner, "_workspace", Path(owner._repo_path).parent)).resolve()

    def readiness_admission(
        command: str,
        attempted: str,
        authorized_operations: tuple[ReadinessOperationAuthority, ...],
        scope_selector: str | None,
    ) -> None:
        require_readiness_allowed(
            owner,
            command=command,
            attempted=attempted,
            authorized_operations=authorized_operations,
            scope_selector=scope_selector,
        )

    return MaterializationDependencies(
        store=owner._store,
        admission=MaterializationAdmission(
            active_scope_names=lambda: tuple(owner._active_scopes),
            ensure_no_interrupted_lifecycle=owner._ensure_no_interrupted_lifecycle,
            ensure_no_open_operation=owner._ensure_no_open_operation,
            readiness_admission=readiness_admission,
        ),
        state=FileMaterializationState(owner._repo_path),
        materializer_source=SubstrateMaterializerSource(owner._lifecycle_substrates),
        session_id=owner._session_id,
        workspace=workspace,
        patch_guard=owner._patch_manager.guard,
        ground=_VcsCoreGroundScopeAccess(owner),
        is_external_workspace_path_admitted=lambda path: _is_external_workspace_path_admitted(owner, path),
    )


def plan_push(owner: VcsCore) -> MaterializationPlan:
    with owner._lock:
        return MaterializationCoordinator(_dependencies(owner)).plan_push()


def assess_push(owner: VcsCore) -> MaterializationAssessment:
    with owner._lock:
        return MaterializationCoordinator(_dependencies(owner)).assess_push()


def push(
    owner: VcsCore,
    *,
    dry_run: bool = False,
    up_to: str | None = None,
) -> MaterializationPlan:
    with owner._lock:
        return MaterializationCoordinator(_dependencies(owner)).push(dry_run=dry_run, up_to=up_to)


def reset_to_materialized(owner: VcsCore) -> int:
    with owner._lock:
        return MaterializationCoordinator(_dependencies(owner)).reset_to_materialized()


def recover_materialization(owner: VcsCore, mode: str = "repair") -> MaterializationRecoveryReport:
    with owner._lock:
        require_recovery_targets_allowed(
            owner,
            attempted="recover materialization",
            targets=recovery_targets_for_kinds(owner, "dirty_push", "materialization_run"),
        )
        with _recovery_session_lock(owner):
            return MaterializationCoordinator(_dependencies(owner)).recover_materialization(mode=mode)


def recover_dirty_push(owner: VcsCore, mode: str = "repair") -> None:
    recover_materialization(owner, mode=mode)


def clear_materialization_state(owner: VcsCore) -> None:
    with owner._lock:
        MaterializationCoordinator(_dependencies(owner)).clear_materialization_state()


@contextmanager
def _recovery_session_lock(owner: VcsCore) -> Iterator[None]:
    if _session_lock_matches(owner):
        yield
        return
    acquire_session_lock(owner._repo_path, owner._session_id)
    try:
        yield
    finally:
        release_session_lock(owner._repo_path, owner._session_id)


def _session_lock_matches(owner: VcsCore) -> bool:
    lock_path = Path(owner._repo_path) / "session.lock"
    try:
        session_id = lock_path.read_text().splitlines()[0]
    except (OSError, IndexError):
        return False
    return session_id == owner._session_id
