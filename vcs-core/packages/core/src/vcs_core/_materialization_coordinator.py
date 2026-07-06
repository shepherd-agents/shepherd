from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, cast

from vcs_core._dirty_flag import clear_dirty_flag, read_dirty_flag, write_dirty_flag
from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._materialization_recovery import MaterializationRecoveryState, probe_materialization_recovery_state
from vcs_core._materialization_run import (
    MaterializationRun,
    clear_materialization_run,
    mark_materialization_units_completed,
    materialization_run_directory,
    read_materialization_run,
    write_materialization_run,
)
from vcs_core._mutation_admission import MutationAdmission
from vcs_core._workspace_external_state import assert_workspace_admissible
from vcs_core.materialization import (
    MaterializationAssessment,
    MaterializationPlanningStore,
    apply_materialization,
    assess_materialization,
    build_materializers,
    plan_materialization,
    prepare_run_artifacts,
    verify_materialization,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from contextlib import AbstractContextManager
    from pathlib import Path

    from vcs_core._query_readiness import ReadinessOperationAuthority
    from vcs_core.materialization import (
        InternalMaterializer,
        MaterializationUnit,
        PlannedMaterialization,
        PreflightMode,
    )
    from vcs_core.types import MaterializationPlan, ScopeInfo


@dataclass(frozen=True)
class MaterializationAdmission:
    """Admission checks that gate materialization mutations."""

    active_scope_names: Callable[[], tuple[str, ...]]
    ensure_no_interrupted_lifecycle: Callable[[str], None]
    ensure_no_open_operation: Callable[[str], None]
    # Invoked for side effect (raises if the command class is blocked). Required:
    # the only admission path is readiness (see MutationAdmission.readiness_admission).
    readiness_admission: Callable[[str, str, tuple[ReadinessOperationAuthority, ...], str | None], None]

    def _mutation_admission(self) -> MutationAdmission:
        return MutationAdmission(
            ensure_no_interrupted_lifecycle=self.ensure_no_interrupted_lifecycle,
            ensure_no_open_operation=self.ensure_no_open_operation,
            active_scope_names=self.active_scope_names,
            readiness_admission=self.readiness_admission,
        )

    def require_push_allowed(self) -> None:
        self._mutation_admission().require_push_allowed()

    def require_reset_allowed(self) -> None:
        self._mutation_admission().require_reset_to_materialized_allowed()


class MaterializationStore(MaterializationPlanningStore, Protocol):
    """Store capability required by materialization orchestration."""

    def list_workspace_files(self, ref: str) -> Iterable[tuple[str, str, int]]: ...

    def read_workspace_file(self, ref: str, path: str) -> bytes | None: ...

    def advance_materialized(self) -> None: ...

    def reset_ground_to_materialized(self) -> int: ...


class MaterializerSource(Protocol):
    """Source of currently active materializers."""

    def build(self) -> tuple[InternalMaterializer, ...]: ...


class MaterializationState(Protocol):
    """Durable materialization run/dirty state."""

    def run_directory(self, run_id: str) -> Path: ...

    def probe_recovery_state(self) -> MaterializationRecoveryState: ...

    def read_run(self) -> MaterializationRun | None: ...

    def write_run(self, run: MaterializationRun) -> None: ...

    def mark_units_completed(self, units: Sequence[MaterializationUnit]) -> None: ...

    def read_dirty_flag(self) -> tuple[str, float] | None: ...

    def write_dirty_flag(self, session_id: str) -> None: ...

    def clear(self) -> None: ...


class GroundScopeAccess(Protocol):
    """Access to the temporary ground scope used during verify recovery."""

    def get(self) -> ScopeInfo | None: ...

    def set(self, scope: ScopeInfo | None) -> None: ...

    def make(self) -> ScopeInfo: ...


@dataclass(frozen=True)
class SubstrateMaterializerSource:
    """Build materializers from the currently active substrates."""

    substrates: Sequence[object]

    def build(self) -> tuple[InternalMaterializer, ...]:
        return build_materializers(self.substrates)


@dataclass(frozen=True)
class FileMaterializationState:
    """File-backed materialization state under the repository control directory."""

    repo_path: str

    def run_directory(self, run_id: str) -> Path:
        return materialization_run_directory(self.repo_path, run_id)

    def probe_recovery_state(self) -> MaterializationRecoveryState:
        return probe_materialization_recovery_state(self.repo_path)

    def read_run(self) -> MaterializationRun | None:
        return read_materialization_run(self.repo_path)

    def write_run(self, run: MaterializationRun) -> None:
        write_materialization_run(self.repo_path, run)

    def mark_units_completed(self, units: Sequence[MaterializationUnit]) -> None:
        mark_materialization_units_completed(
            self.repo_path,
            tuple(unit.unit_id for unit in units),
        )

    def read_dirty_flag(self) -> tuple[str, float] | None:
        return read_dirty_flag(self.repo_path)

    def write_dirty_flag(self, session_id: str) -> None:
        write_dirty_flag(self.repo_path, session_id)

    def clear(self) -> None:
        clear_materialization_run(self.repo_path)
        clear_dirty_flag(self.repo_path)


@dataclass(frozen=True)
class MaterializationDependencies:
    """Explicit VcsCore adapter dependencies for materialization work."""

    store: MaterializationStore
    admission: MaterializationAdmission
    state: MaterializationState
    materializer_source: MaterializerSource
    session_id: str
    workspace: Path
    patch_guard: Callable[[], AbstractContextManager[None]]
    ground: GroundScopeAccess
    is_external_workspace_path_admitted: Callable[[Path], bool] = field(default=lambda _path: False)


@dataclass(frozen=True)
class MaterializationRecoveryReport:
    """Diagnostic report for materialization recovery execution."""

    mode: Literal["repair", "verify", "force"]
    dirty_present: bool
    run_present: bool
    dirty_validity: str | None
    run_validity: str | None
    advanced_materialized: bool = False
    reset_ground: bool = False
    cleared_dirty: bool = False
    cleared_run: bool = False
    changed_refs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class MaterializationCoordinator:
    """Coordinator-owned materialization service."""

    def __init__(self, deps: MaterializationDependencies) -> None:
        self._deps = deps

    def plan_push(self) -> MaterializationPlan:
        return self._plan_push(preflight_mode="pure").plan

    def assess_push(self) -> MaterializationAssessment:
        return self._assess_push(preflight_mode="pure")

    def push(self, *, dry_run: bool = False, up_to: str | None = None) -> MaterializationPlan:
        if up_to is not None:
            raise NotImplementedError(
                "Phase-gated push (up_to) requires multi-substrate intents (R1b). "
                "Use push() without up_to, or push(dry_run=True) to preview."
            )

        planned = self._plan_push(preflight_mode="pure" if dry_run else "recording")
        if dry_run:
            return planned.plan

        with self._guard():
            materializers = self._build_materializers()

            run = self._create_run(planned)
            run_dir = self._run_directory(run.run_id)
            unit_state = prepare_run_artifacts(
                planned,
                materializers=materializers,
                run_directory=run_dir,
            )
            if unit_state:
                run = MaterializationRun(
                    session_id=run.session_id,
                    run_id=run.run_id,
                    timestamp=run.timestamp,
                    planned_unit_ids=run.planned_unit_ids,
                    completed_unit_ids=run.completed_unit_ids,
                    unit_state=unit_state,
                )
            self._write_run(run)
            self._write_dirty_flag()

            apply_materialization(
                planned,
                materializers=materializers,
                on_units_completed=self._mark_units_completed,
            )
            self._deps.store.advance_materialized()
            self.clear_materialization_state()
        return planned.plan

    def _plan_push(self, *, preflight_mode: PreflightMode) -> PlannedMaterialization:
        assessment = self._assess_push(preflight_mode=preflight_mode)
        if assessment.preflight_blockers:
            from vcs_core.materialization import MaterializationPreflightError

            raise MaterializationPreflightError(assessment.preflight_blockers)
        return assessment.planned

    def _assess_push(self, *, preflight_mode: PreflightMode) -> MaterializationAssessment:
        self._deps.admission.require_push_allowed()
        assert_workspace_admissible(
            self._deps.store,
            self._deps.workspace,
            action="materialize",
            is_path_admitted=self._deps.is_external_workspace_path_admitted,
        )

        with self._guard():
            materializers = self._build_materializers()
            return assess_materialization(
                self._deps.store,
                materializers=materializers,
                preflight_mode=preflight_mode,
            )

    def reset_to_materialized(self) -> int:
        self._deps.admission.require_reset_allowed()
        return self._deps.store.reset_ground_to_materialized()

    def recover_materialization(self, mode: str = "repair") -> MaterializationRecoveryReport:
        recovery_mode = _recovery_mode(mode)
        state = self._probe_recovery_state()
        if not state.required:
            return _recovery_report(recovery_mode, state)

        if recovery_mode == "verify":
            return self._recover_materialization_verify(state, mode=recovery_mode)

        advanced_materialized = False
        reset_ground = False
        changed_refs: tuple[str, ...] = ()
        if state.dirty_present:
            if recovery_mode == "repair":
                self._deps.store.advance_materialized()
                advanced_materialized = True
                changed_refs = ("refs/vcscore/materialized",)
            elif recovery_mode == "force":
                self._deps.store.reset_ground_to_materialized()
                reset_ground = True
                changed_refs = ("refs/vcscore/ground",)
        self.clear_materialization_state()
        return _recovery_report(
            recovery_mode,
            state,
            advanced_materialized=advanced_materialized,
            reset_ground=reset_ground,
            cleared_dirty=state.dirty_present,
            cleared_run=state.run_present,
            changed_refs=changed_refs,
        )

    def recover_dirty_push(self, mode: str = "repair") -> None:
        self.recover_materialization(mode=mode)

    def _recover_materialization_verify(
        self,
        state: MaterializationRecoveryState,
        *,
        mode: Literal["verify"],
    ) -> MaterializationRecoveryReport:
        if not state.dirty_present:
            self.clear_materialization_state()
            return _recovery_report(mode, state, cleared_run=state.run_present)
        if not state.run_present:
            raise InvalidRepositoryStateError(
                "Cannot verify materialization recovery because the materialization run ledger is missing. "
                "Use mode='repair' or mode='force'."
            )
        if state.run.validity == "corrupt" or state.run.run is None:
            raise InvalidRepositoryStateError(
                "Cannot verify materialization recovery because the materialization run ledger is unreadable. "
                "Use mode='repair' or mode='force'."
            )
        if not state.run.run.planned_unit_ids:
            raise InvalidRepositoryStateError(
                "Cannot verify materialization recovery without a materialization run ledger for a substrate "
                "with external state. Use mode='repair' or mode='force'."
            )
        self._recover_dirty_push_verify(run=state.run.run)
        return _recovery_report(
            mode,
            state,
            advanced_materialized=True,
            cleared_dirty=True,
            cleared_run=True,
            changed_refs=("refs/vcscore/materialized",),
        )

    def _recover_dirty_push_verify(self, *, run: MaterializationRun | None = None) -> None:
        run = run or self._read_run()
        if run is None or not run.planned_unit_ids:
            raise InvalidRepositoryStateError(
                "Verification requires a materialization run ledger for a substrate with external state. "
                "Use mode='repair' or mode='force'."
            )
        temporary_ground = self._ensure_ground()
        try:
            materializers = self._build_materializers()
            planned = plan_materialization(self._deps.store, materializers=materializers, skip_preflight=True)
            current_unit_ids = tuple(unit.unit_id for unit in planned.units)
            if current_unit_ids != run.planned_unit_ids:
                raise RuntimeError(
                    "Materialization verification requires pending units to match the recorded run ledger."
                )
            verify_materialization(
                planned,
                materializers=materializers,
                run_state=run.unit_state,
                run_directory=self._run_directory(run.run_id),
            )
            self._deps.store.advance_materialized()
            self.clear_materialization_state()
        finally:
            if temporary_ground:
                self._clear_temporary_ground()

    def clear_materialization_state(self) -> None:
        self._deps.state.clear()

    def _guard(self) -> AbstractContextManager[None]:
        return self._deps.patch_guard()

    def _build_materializers(self) -> tuple[InternalMaterializer, ...]:
        return self._deps.materializer_source.build()

    def _create_run(self, planned: PlannedMaterialization) -> MaterializationRun:
        return MaterializationRun(
            session_id=self._deps.session_id,
            run_id=uuid.uuid4().hex[:12],
            timestamp=time.time(),
            planned_unit_ids=tuple(unit.unit_id for unit in planned.units),
        )

    def _run_directory(self, run_id: str) -> Path:
        return self._deps.state.run_directory(run_id)

    def _probe_recovery_state(self) -> MaterializationRecoveryState:
        return self._deps.state.probe_recovery_state()

    def _read_run(self) -> MaterializationRun | None:
        return self._deps.state.read_run()

    def _write_run(self, run: MaterializationRun) -> None:
        self._deps.state.write_run(run)

    def _mark_units_completed(self, units: Sequence[MaterializationUnit]) -> None:
        self._deps.state.mark_units_completed(units)

    def _read_dirty_flag(self) -> tuple[str, float] | None:
        return self._deps.state.read_dirty_flag()

    def _write_dirty_flag(self) -> None:
        self._deps.state.write_dirty_flag(self._deps.session_id)

    def _ensure_ground(self) -> bool:
        if self._deps.ground.get() is not None:
            return False
        self._deps.ground.set(self._deps.ground.make())
        return True

    def _clear_temporary_ground(self) -> None:
        self._deps.ground.set(None)


def _recovery_mode(mode: str) -> Literal["repair", "verify", "force"]:
    if mode in {"repair", "verify", "force"}:
        return cast("Literal['repair', 'verify', 'force']", mode)
    msg = f"Unknown recovery mode: {mode!r}"
    raise ValueError(msg)


def _recovery_report(
    mode: Literal["repair", "verify", "force"],
    state: MaterializationRecoveryState,
    *,
    advanced_materialized: bool = False,
    reset_ground: bool = False,
    cleared_dirty: bool = False,
    cleared_run: bool = False,
    changed_refs: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
) -> MaterializationRecoveryReport:
    return MaterializationRecoveryReport(
        mode=mode,
        dirty_present=state.dirty_present,
        run_present=state.run_present,
        dirty_validity=state.dirty.validity,
        run_validity=state.run.validity,
        advanced_materialized=advanced_materialized,
        reset_ground=reset_ground,
        cleared_dirty=cleared_dirty,
        cleared_run=cleared_run,
        changed_refs=changed_refs,
        warnings=warnings,
    )
