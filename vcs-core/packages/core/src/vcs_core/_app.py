"""Internal application/control-plane seam for vcs-core commands."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from vcs_core._admission.identifiers import ParseError, ScopeName, parse_optional_scope_name
from vcs_core._app_blockers import AppBlocker, dedupe_app_blockers, sort_app_blockers
from vcs_core._app_readiness_projection import app_blockers_from_readiness_result
from vcs_core._errors import (
    ActivationError,
    DirtyPushError,
    InterruptedLifecycleError,
    InvalidRepositoryStateError,
    LifecycleRecoveryRequiredError,
    OpenScopeError,
    OrphanedOperationsError,
    ScopeAdmissionError,
    SiblingGroupRecoveryRequiredError,
    StaleScopeError,
    SubstrateCommandError,
    UnsupportedOverlayEntryError,
    VcsCoreError,
    WorkspaceAuthorityRecoveryRequiredError,
)
from vcs_core._fork_hints import ForkHints
from vcs_core._projection_store import TERMINAL_SCOPE_STATUSES
from vcs_core._query_readiness import ReadinessRequest
from vcs_core._recovery_inventory import scope_ref_recovery_classification
from vcs_core._workspace_external_state import (
    ExternalStateBlocker,
    ExternalWorkspaceStateError,
    external_workspace_blockers,
)
from vcs_core.materialization import MaterializationPreflightBlocker, MaterializationPreflightError
from vcs_core.store import GROUND_REF
from vcs_core.vcscore import VcsCore

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vcs_core._command_values import CommandValueSource
    from vcs_core._projection_store import ScopeRegistryEntry, ScopeRegistryIsolationMode
    from vcs_core._recovery_inventory import ScopeRefRecoveryClassification
    from vcs_core.types import MaterializationPlan, OperationSummary, RecordedCommandOutcome, ScopeInfo


class AppOpenMode(Enum):
    """Lifecycle mode for an app-layer repository view."""

    CONTROL = "control"
    RECOVERY = "recovery"
    ACTIVE = "active"


@dataclass(frozen=True)
class AppScopeEntry:
    """Registry-derived live scope entry resolved to ScopeInfo handles."""

    name: str
    ref: str
    parent_name: str
    parent_ref: str
    instance_id: str
    creation_oid: str
    world_id: str | None
    isolation_mode: ScopeRegistryIsolationMode
    scope: ScopeInfo
    parent: ScopeInfo


@dataclass(frozen=True)
class BranchResult:
    """Result of creating a scope."""

    name: str
    parent: str
    parent_ref: str
    ref: str
    instance_id: str
    world_id: str | None
    isolated: bool
    mount_path: str | None = None


@dataclass(frozen=True)
class MergeResult:
    """Result of merging a scope."""

    merged: str
    into: str
    into_ref: str


@dataclass(frozen=True)
class DiscardResult:
    """Result of discarding a scope."""

    discarded: str
    parent: str
    parent_ref: str


@dataclass(frozen=True)
class PushResult:
    """Result of a materialization request."""

    plan: MaterializationPlan
    dry_run: bool


@dataclass(frozen=True)
class RepoStatusSummary:
    """App-level repository status summary."""

    workspace: str
    local_changes: int
    commits_ahead: int
    live_scopes: tuple[AppScopeEntry, ...]
    retained_scopes: tuple[ScopeRegistryEntry, ...]
    blockers: tuple[AppBlocker, ...]
    pending_plan: MaterializationPlan | None
    current_scope: str | None = None
    orphaned_operations: tuple[OperationSummary, ...] = ()


class AppError(VcsCoreError):
    """Base class for expected app-layer failures."""


@dataclass
class AppCommandBlocked(AppError):  # noqa: N818 - matches product vocabulary in the active design.
    """Expected command failure carrying structured blockers."""

    command: str
    blockers: tuple[AppBlocker, ...]


@dataclass
class AppCommandFailed(AppError):  # noqa: N818 - matches product vocabulary in the active design.
    """Expected command execution failure after admission succeeded."""

    command: str
    detail: str
    hint: str | None = None


@dataclass
class AppRepositoryError(AppError):
    """Expected repository-open or activation failure."""

    message: str


@dataclass
class AppScopeResolutionError(AppError):
    """Expected failure to resolve a registry-backed scope."""

    name: str
    blockers: tuple[AppBlocker, ...]


@dataclass
class AppScopeNotFound(AppError):  # noqa: N818 - matches product vocabulary in the active design.
    """Expected failure to resolve a user-selected scope name."""

    name: str


@dataclass
class AppScopeTerminalState(AppError):  # noqa: N818 - matches product vocabulary in the active design.
    """Expected failure to use a terminal registry scope as a live scope."""

    name: str
    status: str
    ref: str


class ScopeIndex:
    """One-command registry-derived view of live scopes."""

    def __init__(
        self,
        *,
        mg: VcsCore,
        entries: tuple[AppScopeEntry, ...],
        terminal_entries: tuple[ScopeRegistryEntry, ...],
        blockers: tuple[AppBlocker, ...],
        restored_names: tuple[str, ...],
        retained_entries: tuple[ScopeRegistryEntry, ...] = (),
    ) -> None:
        self._mg = mg
        self._entries = entries
        self._terminal_entries_by_name = {entry.name: entry for entry in terminal_entries}
        self._retained_entries = retained_entries
        self._blockers = _sort_blockers(blockers)
        self._restored_names = restored_names
        self._entries_by_name = {entry.name: entry for entry in entries}
        self._entries_by_ref = {entry.ref: entry for entry in entries}

    @property
    def entries(self) -> tuple[AppScopeEntry, ...]:
        return self._entries

    @property
    def blockers(self) -> tuple[AppBlocker, ...]:
        return self._blockers

    @property
    def restored_names(self) -> tuple[str, ...]:
        return self._restored_names

    @property
    def retained(self) -> tuple[ScopeRegistryEntry, ...]:
        """Sealed-but-undisposed scopes: ref-owning and adoptable, not runtime-open or terminal."""
        return self._retained_entries

    @property
    def live_scope_blockers(self) -> tuple[AppBlocker, ...]:
        return tuple(
            AppBlocker(
                kind="live_scope",
                subject=entry.name,
                detail=f"Live scope {entry.name!r} must be merged or discarded before materialization.",
                hint=f"Run `vcs-core merge {entry.name}` or `vcs-core discard {entry.name}`.",
            )
            for entry in self._entries
            if entry.name != "ground"
        )

    def resolve_scope(self, name: str) -> ScopeInfo:
        if name == "ground":
            return self._mg.ground
        entry = self._entries_by_name.get(name)
        if entry is None:
            blockers = self._resolution_blockers_for(name)
            if blockers:
                raise AppScopeResolutionError(name=name, blockers=blockers)
            self._raise_if_terminal(name)
            raise AppScopeNotFound(name=name)
        return entry.scope

    def resolve_entry(self, name: str) -> AppScopeEntry:
        if name == "ground":
            raise AppScopeResolutionError(
                name=name,
                blockers=(
                    AppBlocker(
                        kind="scope_registry_mismatch",
                        subject=name,
                        detail="scope 'ground' is not a live child scope.",
                    ),
                ),
            )
        entry = self._entries_by_name.get(name)
        if entry is None:
            blockers = self._resolution_blockers_for(name)
            if blockers:
                raise AppScopeResolutionError(name=name, blockers=blockers)
            self._raise_if_terminal(name)
            raise AppScopeNotFound(name=name)
        return entry

    def resolve_parent(self, name: str) -> ScopeInfo:
        entry = self.resolve_entry(name)
        return entry.parent

    def _resolution_blockers_for(self, name: str) -> tuple[AppBlocker, ...]:
        return tuple(
            blocker
            for blocker in self._blockers
            if blocker.kind == "scope_registry_mismatch" and blocker.subject == name
        )

    def _raise_if_terminal(self, name: str) -> None:
        entry = self._terminal_entries_by_name.get(name)
        if entry is not None:
            raise AppScopeTerminalState(name=name, status=entry.status, ref=entry.ref)


class VcsCoreApp:
    """Internal domain service for control-plane command semantics."""

    def __init__(
        self,
        *,
        mg: VcsCore,
        mode: AppOpenMode,
        scope_index: ScopeIndex,
        activation_blockers: tuple[AppBlocker, ...] = (),
    ) -> None:
        self._mg = mg
        self._mode = mode
        self._scope_index = scope_index
        self._activation_blockers = _sort_blockers(activation_blockers)
        self._retained_restored_scope_names: set[str] = set()

    @classmethod
    @contextmanager
    def open_existing(
        cls,
        workspace: str = ".",
        *,
        mode: AppOpenMode = AppOpenMode.CONTROL,
        recover: str | None = None,
        recover_lifecycle: str | None = None,
        auto_recover_orphaned_operations: bool = False,
    ) -> Iterator[VcsCoreApp]:
        workspace_path = os.path.abspath(workspace)
        try:
            mg = VcsCore.from_config(workspace_path)
        except FileNotFoundError as exc:
            raise AppRepositoryError("not a vcs-core repository. Run `vcs-core init` first.") from exc
        except InvalidRepositoryStateError as exc:
            raise AppRepositoryError(str(exc)) from exc

        activation_blockers: tuple[AppBlocker, ...] = ()
        try:
            try:
                mg.activate(
                    recover=recover,
                    recover_lifecycle=recover_lifecycle,
                    defer_orphan_detection=mode is AppOpenMode.CONTROL,
                    # Reclaim a dead prior run's orphaned operation refs (bookkeeping the
                    # reversible substrate never published) only when the caller is starting
                    # work — never on a read-only open like `status`, which must still *report*
                    # an interrupted run rather than silently discard it.
                    auto_recover_orphaned_operations=auto_recover_orphaned_operations,
                )
            except (DirtyPushError, InterruptedLifecycleError, LifecycleRecoveryRequiredError) as exc:
                activation_blockers = (_blocker_from_exception(exc),)
                raise AppCommandBlocked(command="open", blockers=activation_blockers) from exc
            except ActivationError as exc:
                raise AppRepositoryError(str(exc)) from exc
            except InvalidRepositoryStateError as exc:
                raise AppRepositoryError(str(exc)) from exc

            with mg.preserve_runtime_context():
                scope_index = _build_scope_index(mg)
            app = cls(mg=mg, mode=mode, scope_index=scope_index, activation_blockers=activation_blockers)
            yield app
        finally:
            try:
                if mode is AppOpenMode.CONTROL:
                    mg.clear_restored_scope_state()
                mg.deactivate(warn_on_open_scopes=False)
            except UnboundLocalError:
                pass

    @classmethod
    def from_active(cls, mg: VcsCore, *, current_scope: str = "ground") -> VcsCoreApp:
        del current_scope
        with mg.preserve_runtime_context():
            scope_index = _build_scope_index(mg)
        return cls(mg=mg, mode=AppOpenMode.ACTIVE, scope_index=scope_index)

    @classmethod
    @contextmanager
    def active_view(cls, mg: VcsCore, *, current_scope: str = "ground") -> Iterator[VcsCoreApp]:
        previous_restored = set(mg.restored_scope_names())
        with mg.preserve_runtime_context():
            app = cls.from_active(mg)
            app.retain_restored_scope(current_scope)
            try:
                yield app
            finally:
                mg.clear_transient_restored_scopes(
                    restored_names=set(app._scope_index.restored_names),
                    previous_restored_names=previous_restored,
                    retained_names=app._retained_restored_scope_names,
                )

    @property
    def mg(self) -> VcsCore:
        return self._mg

    @property
    def scope_index(self) -> ScopeIndex:
        return self._scope_index

    def resolve_scope(self, name: str) -> ScopeInfo:
        return self._scope_index.resolve_scope(name)

    def retain_restored_scope(self, name: str) -> None:
        """Keep an app-restored handle installed after an ACTIVE app view exits."""
        if self._mode is not AppOpenMode.ACTIVE or name == "ground":
            return
        if name in self._scope_index.restored_names:
            self._retained_restored_scope_names.add(name)
            self._mg.retain_restored_scope(name)

    def resolve_parent(self, name: str) -> ScopeInfo:
        return self._scope_index.resolve_parent(name)

    def branch(self, *, name: str, parent: str = "ground", isolated: bool = False) -> BranchResult:
        name = _parse_scope_name_for_command("branch", name, allow_ground=False)
        parent = _parse_optional_scope_name_for_command("branch", parent) or "ground"
        if isolated and self._mode is AppOpenMode.CONTROL:
            raise AppCommandBlocked(
                command="branch",
                blockers=(
                    AppBlocker(
                        kind="isolated_scope_requires_session",
                        subject=name,
                        detail="isolated scopes require a persistent session; stateless CLI only supports recording-only scopes.",
                        hint="Start a session with: vcs-core session start",
                    ),
                ),
            )
        self._raise_if_mutation_blocked("branch")
        parent_scope = self.resolve_scope(parent)
        hints = ForkHints(isolated=isolated)
        try:
            scope = self._mg.fork(parent_scope, name, hints=hints)
        except ScopeAdmissionError as exc:
            raise AppCommandBlocked(command="branch", blockers=(_blocker_from_exception(exc),)) from exc
        return BranchResult(
            name=scope.name,
            parent=parent,
            parent_ref=parent_scope.ref,
            ref=scope.ref,
            instance_id=scope.instance_id,
            world_id=scope.world_id,
            isolated=isolated,
        )

    def merge(self, *, name: str) -> MergeResult:
        name = _parse_scope_name_for_command("merge", name)
        self._raise_if_mutation_blocked("merge")
        entry = self._scope_index.resolve_entry(name)
        if entry.isolation_mode == "isolated" and self._mode is AppOpenMode.CONTROL:
            raise AppCommandBlocked(
                command="merge",
                blockers=(
                    AppBlocker(
                        kind="isolated_scope_requires_session",
                        subject=name,
                        detail=f"isolated scopes require a persistent session; stateless CLI cannot merge isolated scope {name!r}.",
                    ),
                ),
            )
        try:
            self._mg.merge(entry.scope, entry.parent)
        except UnsupportedOverlayEntryError as exc:
            raise AppCommandBlocked(command="merge", blockers=(_unsupported_overlay_blocker(exc),)) from exc
        return MergeResult(merged=name, into=entry.parent_name, into_ref=entry.parent_ref)

    def discard(self, *, name: str) -> DiscardResult:
        name = _parse_scope_name_for_command("discard", name)
        self._raise_if_mutation_blocked("discard")
        entry = self._scope_index.resolve_entry(name)
        if entry.isolation_mode == "isolated" and self._mode is AppOpenMode.CONTROL:
            raise AppCommandBlocked(
                command="discard",
                blockers=(
                    AppBlocker(
                        kind="isolated_scope_requires_session",
                        subject=name,
                        detail=f"isolated scopes require a persistent session; stateless CLI cannot discard isolated scope {name!r}.",
                    ),
                ),
            )
        self._mg.discard(entry.scope)
        return DiscardResult(discarded=name, parent=entry.parent_name, parent_ref=entry.parent_ref)

    def execute(
        self,
        *,
        binding_name: str,
        command: str,
        scope_name: str,
        params: dict[str, object],
        execution_options: object | None = None,
        command_source: CommandValueSource = "native",
    ) -> RecordedCommandOutcome:
        from vcs_core._command_envelope import CommandExecutionOptions

        if execution_options is None:
            execution_options = CommandExecutionOptions()
        if not isinstance(execution_options, CommandExecutionOptions):
            raise TypeError(
                f"execution_options must be CommandExecutionOptions, got {type(execution_options).__name__}."
            )
        scope = self._resolve_runtime_scope(command="exec", scope_name=scope_name)
        try:
            return self._mg._execute_recorded_params(
                binding_name,
                command,
                scope=scope,
                params=params,
                command_param_source=command_source,
                execution_options=execution_options,
            )
        except SubstrateCommandError as exc:
            substrate_label = "SQLite" if exc.substrate == "sqlite" else exc.substrate
            detail = f"{substrate_label} {exc.command} failed for binding {binding_name!r}: {exc.message}"
            raise AppCommandFailed(command="exec", detail=detail) from exc

    def push_blockers(self) -> tuple[AppBlocker, ...]:
        blockers: list[AppBlocker] = []
        blockers.extend(self._activation_blockers)
        blockers.extend(self._scope_index.blockers)
        blockers.extend(
            self._readiness_blockers(
                "vcscore.push-status",
                include_orphaned_scopes=self._mode is AppOpenMode.RECOVERY,
            )
        )
        blockers.extend(self._scope_index.live_scope_blockers)
        return _dedupe_blockers(_sort_blockers(blockers))

    def push(self, *, dry_run: bool = False, up_to: str | None = None) -> PushResult:
        blockers = list(self.push_blockers())
        if up_to is not None:
            blockers.append(
                AppBlocker(
                    kind="unsupported_feature",
                    subject="up_to",
                    detail="Phase-gated push is not implemented yet.",
                    hint="Use `vcs-core push` without `--up-to`, or `vcs-core push --dry-run` to preview.",
                )
            )
        blockers_tuple = _dedupe_blockers(_sort_blockers(blockers))
        if blockers_tuple:
            raise AppCommandBlocked(command="push", blockers=blockers_tuple)
        try:
            return PushResult(plan=self._mg.push(dry_run=dry_run, up_to=up_to), dry_run=dry_run)
        except ExternalWorkspaceStateError as exc:
            raise AppCommandBlocked(command="push", blockers=_physical_workspace_blockers(exc.blockers)) from exc
        except MaterializationPreflightError as exc:
            raise AppCommandBlocked(command="push", blockers=_materialization_preflight_blockers(exc.blockers)) from exc
        except (
            OpenScopeError,
            OrphanedOperationsError,
            LifecycleRecoveryRequiredError,
            SiblingGroupRecoveryRequiredError,
            WorkspaceAuthorityRecoveryRequiredError,
        ) as exc:
            raise AppCommandBlocked(command="push", blockers=(_blocker_from_exception(exc),)) from exc

    def archive_orphaned_scopes(self) -> list[str]:
        cleanup_classification = _scope_ref_recovery_classification(self._mg)
        blockers = tuple(
            blocker
            for blocker in self.push_blockers()
            if blocker.kind
            in {
                "scope_registry_mismatch",
                "orphaned_operation",
                "dirty_push",
                "interrupted_lifecycle",
                "sibling_group",
            }
            and blocker.source_item_id not in cleanup_classification.reclaimable_mismatch_item_ids
        )
        if blockers:
            raise AppCommandBlocked(command="archive-orphaned-scopes", blockers=blockers)

        try:
            return self._mg.archive_orphaned_scopes(exclude_refs=cleanup_classification.protected_ref_owning_refs)
        except (
            OpenScopeError,
            OrphanedOperationsError,
            SiblingGroupRecoveryRequiredError,
            WorkspaceAuthorityRecoveryRequiredError,
        ) as exc:
            raise AppCommandBlocked(
                command="archive-orphaned-scopes", blockers=(_blocker_from_exception(exc),)
            ) from exc

    def archive_orphaned_operations(self) -> list[str]:
        blockers = tuple(
            blocker
            for blocker in self.push_blockers()
            if blocker.kind in {"scope_registry_mismatch", "dirty_push", "interrupted_lifecycle", "sibling_group"}
        )
        if blockers:
            raise AppCommandBlocked(command="archive-orphaned-operations", blockers=blockers)
        try:
            return self._mg.archive_orphaned_operations()
        except (
            OpenScopeError,
            OrphanedOperationsError,
            SiblingGroupRecoveryRequiredError,
            WorkspaceAuthorityRecoveryRequiredError,
        ) as exc:
            raise AppCommandBlocked(
                command="archive-orphaned-operations", blockers=(_blocker_from_exception(exc),)
            ) from exc

    def repo_status(self, *, current_scope: str | None = None) -> RepoStatusSummary:
        status = self._mg.status()
        blockers = self.push_blockers()
        pending_plan = None
        # T4a: push admission compares worktree against GROUND_REF (where
        # pending substrate-aware changes live before push) rather than
        # MATERIALIZED_REF (which only advances on push). Pre-T4a
        # comparison against MATERIALIZED_REF wrongly rejected push after
        # Python-tier capture committed to ground but before push
        # advanced MATERIALIZED_REF.
        from vcs_core.store import GROUND_REF

        workspace_blockers = external_workspace_blockers(
            self._mg.store,
            Path(self._mg.store.repo_path).parent,
            is_path_admitted=self._external_workspace_path_admitted,
            reference=GROUND_REF,
        )
        if workspace_blockers:
            blockers = _dedupe_blockers(_sort_blockers((*blockers, *_physical_workspace_blockers(workspace_blockers))))
        if not blockers:
            assessment = self._mg.assess_push()
            pending_plan = assessment.planned.plan
            blockers = _dedupe_blockers(
                _sort_blockers((*blockers, *_materialization_preflight_blockers(assessment.preflight_blockers)))
            )
        return RepoStatusSummary(
            workspace=str(Path(self._mg.store.repo_path).parent),
            local_changes=status.local_changes,
            commits_ahead=status.commits_ahead,
            live_scopes=self._scope_index.entries,
            retained_scopes=self._scope_index.retained,
            blockers=blockers,
            pending_plan=pending_plan,
            current_scope=current_scope,
            orphaned_operations=self._mg.recovery_snapshot().orphaned_operations,
        )

    def _external_workspace_path_admitted(self, path: Path) -> bool:
        claim = self._mg._lookup_claim(path)
        return claim is not None and claim.policy in {"exclusive", "authoritative_suppress_fs"}

    def _raise_if_mutation_blocked(
        self,
        command: str,
        *,
        readiness_command: str = "vcscore.lifecycle",
        scope_selector: str | None = None,
    ) -> None:
        blockers: list[AppBlocker] = []
        blockers.extend(self._activation_blockers)
        blockers.extend(self._scope_index.blockers)
        blockers.extend(
            self._readiness_blockers(
                readiness_command,
                include_orphaned_scopes=False,
                scope_selector=scope_selector,
            )
        )
        blockers_tuple = _dedupe_blockers(_sort_blockers(blockers))
        if blockers_tuple:
            raise AppCommandBlocked(command=command, blockers=blockers_tuple)

    def _readiness_blockers(
        self,
        command: str,
        *,
        include_orphaned_scopes: bool,
        scope_selector: str | None = None,
    ) -> tuple[AppBlocker, ...]:
        request = ReadinessRequest.create(
            command=command,
            scope=scope_selector,
            requested_freshness="locked",
            allow_best_effort=False,
        )
        result = self._mg.query_readiness(request)
        blockers = app_blockers_from_readiness_result(result)
        if not blockers:
            return ()
        protected_refs = (
            _scope_ref_recovery_classification(self._mg).protected_ref_owning_refs
            if include_orphaned_scopes
            else frozenset()
        )
        item_locators = {item.id: item.locator for item in result.snapshot.items}
        contextualized: list[AppBlocker] = []
        for blocker in blockers:
            if blocker.kind == "orphaned_scope":
                if not include_orphaned_scopes:
                    continue
                if blocker.source_item_id is not None and item_locators.get(blocker.source_item_id) in protected_refs:
                    continue
            contextualized.append(blocker)
        return tuple(contextualized)

    def _resolve_runtime_scope(self, *, command: str, scope_name: str) -> ScopeInfo:
        scope_name = _parse_scope_name_for_command(command, scope_name)
        self._raise_if_mutation_blocked(command, readiness_command="vcscore.runtime", scope_selector=scope_name)
        scope = self.resolve_scope(scope_name)
        if scope_name == "ground":
            return scope
        entry = self._scope_index.resolve_entry(scope_name)
        if entry.isolation_mode == "isolated" and self._mode is AppOpenMode.CONTROL:
            raise AppCommandBlocked(
                command=command,
                blockers=(
                    AppBlocker(
                        kind="isolated_scope_requires_session",
                        subject=scope_name,
                        detail=(
                            "isolated scopes require a persistent session; "
                            f"stateless CLI cannot {command} isolated scope {scope_name!r}."
                        ),
                    ),
                ),
            )
        return scope


def render_app_error(error: AppError) -> tuple[int, tuple[str, ...]]:
    """Render expected app errors into stable process-exit content."""
    if isinstance(error, AppRepositoryError):
        return 1, (f"Error: {error.message}",)
    if isinstance(error, AppScopeTerminalState):
        return 1, (f"Error: scope {error.name!r} is already {error.status} and is no longer live.",)
    if isinstance(error, AppScopeNotFound):
        return 1, (f"Error: no tracked scope {error.name!r}.",)
    if isinstance(error, AppScopeResolutionError):
        return _render_blocked(f"scope {error.name!r}", error.blockers)
    if isinstance(error, AppCommandBlocked):
        return _render_blocked(error.command, error.blockers)
    if isinstance(error, AppCommandFailed):
        lines = [f"Error: {error.detail}"]
        if error.hint:
            lines.append(f"  {error.hint}")
        return 1, tuple(lines)
    return 1, (f"Error: {error}",)


def app_error_message(error: AppError) -> str:
    """Render an expected app error as a single transport-safe message."""
    _exit_code, lines = render_app_error(error)
    return "\n".join(lines)


def _render_blocked(subject: str, blockers: tuple[AppBlocker, ...]) -> tuple[int, tuple[str, ...]]:
    lines = [f"Error: cannot {subject}:"]
    for blocker in blockers:
        lines.append(f"  - {blocker.detail}")
        if blocker.hint:
            lines.append(f"    {blocker.hint}")
    return 1, tuple(lines)


def _build_scope_index(mg: VcsCore) -> ScopeIndex:
    snapshot = mg.store.require_scope_registry_projection()
    entries_by_ref = snapshot.entries_by_ref
    scopes_by_ref: dict[str, ScopeInfo] = {GROUND_REF: mg.ground}
    names_by_ref: dict[str, str] = {GROUND_REF: "ground"}
    entries: list[AppScopeEntry] = []
    blockers: list[AppBlocker] = []
    restored_names: list[str] = []
    # Only live scopes are restored as active runtime handles and checked against the live
    # runtime state below. retained (sealed) scopes have no active handle, so they are
    # intentionally excluded here and surfaced separately via ScopeIndex.retained.
    remaining = [entry for entry in snapshot.entries if entry.status == "live"]

    while remaining:
        progress = False
        next_remaining: list[ScopeRegistryEntry] = []
        for entry in remaining:
            parent = scopes_by_ref.get(entry.parent_ref)
            if parent is None:
                next_remaining.append(entry)
                continue
            parent_name = names_by_ref[entry.parent_ref]
            try:
                existing = mg.lookup_scope(entry.name)
                if existing is None:
                    scope = mg.restore_scope(
                        name=entry.name,
                        ref=entry.ref,
                        instance_id=entry.instance_id,
                        creation_oid=entry.creation_oid,
                        world_id=entry.world_id,
                        parent=parent,
                        isolated=entry.isolation_mode == "isolated",
                    )
                    restored_names.append(entry.name)
                else:
                    mismatch = _active_scope_mismatch(mg, entry=entry, scope=existing, parent=parent)
                    if mismatch is not None:
                        blockers.append(mismatch)
                        progress = True
                        continue
                    scope = existing
            except StaleScopeError:
                blockers.append(
                    AppBlocker(
                        kind="scope_registry_mismatch",
                        subject=entry.name,
                        detail=f"Registry marks scope {entry.name!r} live, but ref {entry.ref!r} is missing.",
                    )
                )
                progress = True
                continue
            scopes_by_ref[entry.ref] = scope
            names_by_ref[entry.ref] = entry.name
            entries.append(_app_scope_entry(entry, scope=scope, parent=parent, parent_name=parent_name))
            progress = True
        if not progress:
            for entry in next_remaining:
                parent_entry = entries_by_ref.get(entry.parent_ref)
                parent_subject = parent_entry.name if parent_entry is not None else entry.parent_ref
                blockers.append(
                    AppBlocker(
                        kind="scope_registry_mismatch",
                        subject=entry.name,
                        detail=f"Live scope {entry.name!r} has unresolved parent {parent_subject!r}.",
                    )
                )
            break
        remaining = next_remaining

    entries.sort(key=lambda item: item.name)
    return ScopeIndex(
        mg=mg,
        entries=tuple(entries),
        terminal_entries=tuple(entry for entry in snapshot.entries if entry.status in TERMINAL_SCOPE_STATUSES),
        retained_entries=tuple(entry for entry in snapshot.entries if entry.status == "retained"),
        blockers=tuple(blockers),
        restored_names=tuple(restored_names),
    )


def _app_scope_entry(
    entry: ScopeRegistryEntry,
    *,
    scope: ScopeInfo,
    parent: ScopeInfo,
    parent_name: str,
) -> AppScopeEntry:
    return AppScopeEntry(
        name=entry.name,
        ref=entry.ref,
        parent_name=parent_name,
        parent_ref=entry.parent_ref,
        instance_id=entry.instance_id,
        creation_oid=entry.creation_oid,
        world_id=entry.world_id,
        isolation_mode=entry.isolation_mode,
        scope=scope,
        parent=parent,
    )


def _active_scope_mismatch(
    mg: VcsCore,
    *,
    entry: ScopeRegistryEntry,
    scope: ScopeInfo,
    parent: ScopeInfo,
) -> AppBlocker | None:
    mismatches: list[str] = []
    if scope.ref != entry.ref:
        mismatches.append(f"ref {scope.ref!r} != {entry.ref!r}")
    if scope.instance_id != entry.instance_id:
        mismatches.append(f"instance_id {scope.instance_id!r} != {entry.instance_id!r}")
    if scope.creation_oid != entry.creation_oid:
        mismatches.append(f"creation_oid {scope.creation_oid!r} != {entry.creation_oid!r}")
    if scope.world_id != entry.world_id:
        mismatches.append(f"world_id {scope.world_id!r} != {entry.world_id!r}")
    active_parent = mg._scope_parents.get(entry.name)
    if active_parent is None:
        mismatches.append("parent handle is missing")
    elif active_parent.ref != parent.ref or active_parent.instance_id != parent.instance_id:
        mismatches.append(f"parent {active_parent.ref!r} != {parent.ref!r}")
    active_isolation = "isolated" if entry.name in mg._isolated_scopes else "shared"
    if active_isolation != entry.isolation_mode:
        mismatches.append(f"isolation {active_isolation!r} != {entry.isolation_mode!r}")
    if not mismatches:
        return None
    return AppBlocker(
        kind="scope_registry_mismatch",
        subject=entry.name,
        detail=f"Active scope {entry.name!r} disagrees with the scope registry: {', '.join(mismatches)}.",
    )


def _scope_ref_recovery_classification(mg: VcsCore) -> ScopeRefRecoveryClassification:
    return scope_ref_recovery_classification(
        mg.store,
        mg.store.repo_path,
        mismatches=tuple(mg.store.scope_registry_projection_mismatches()),
    )


def _parse_scope_name_for_command(command: str, raw: str, *, allow_ground: bool = True) -> str:
    try:
        return str(ScopeName.parse(raw, allow_ground=allow_ground))
    except ParseError as exc:
        raise AppCommandBlocked(
            command=command,
            blockers=(AppBlocker(kind="invalid_input", subject=raw, detail=str(exc)),),
        ) from exc


def _parse_optional_scope_name_for_command(
    command: str,
    raw: str | None,
    *,
    allow_ground: bool = True,
) -> str | None:
    try:
        return parse_optional_scope_name(raw, allow_ground=allow_ground)
    except ParseError as exc:
        raise AppCommandBlocked(
            command=command,
            blockers=(AppBlocker(kind="invalid_input", subject="" if raw is None else raw, detail=str(exc)),),
        ) from exc


def _materialization_preflight_blockers(
    blockers: tuple[MaterializationPreflightBlocker, ...],
) -> tuple[AppBlocker, ...]:
    return tuple(
        AppBlocker(
            kind="materialization_preflight",
            subject=blocker.unit.unit_id,
            detail=blocker.message,
            hint="Resolve the upstream conflict or reset pending materialization before pushing.",
        )
        for blocker in blockers
    )


def _physical_workspace_blockers(blockers: tuple[ExternalStateBlocker, ...]) -> tuple[AppBlocker, ...]:
    return tuple(
        AppBlocker(
            kind="physical_workspace",
            subject=blocker.path,
            detail=f"Physical workspace path {blocker.path!r} is not cleanly adopted ({blocker.reason}).",
            hint="Run `vcs-core init --adopt git-head --all` or `vcs-core init --adopt worktree --all` after cleaning the workspace.",
        )
        for blocker in blockers
    )


def _unsupported_overlay_blocker(exc: UnsupportedOverlayEntryError) -> AppBlocker:
    return AppBlocker(
        kind="unsupported_feature",
        subject=exc.path,
        detail=str(exc),
        hint="Remove or replace the unsupported overlay entry before merging the scope.",
    )


def _blocker_from_exception(exc: BaseException) -> AppBlocker:
    if isinstance(exc, DirtyPushError):
        return AppBlocker(
            kind="dirty_push",
            subject=exc.session_id,
            detail=str(exc),
            hint="Run `vcs-core recover-materialization --mode repair` before mutating or materializing.",
        )
    if isinstance(exc, InterruptedLifecycleError):
        return AppBlocker(
            kind="interrupted_lifecycle",
            subject=exc.scope_name,
            detail=str(exc),
            hint="Recover the interrupted lifecycle first.",
        )
    if isinstance(exc, LifecycleRecoveryRequiredError):
        return AppBlocker(
            kind="interrupted_lifecycle",
            subject=exc.scope_name,
            detail=str(exc),
            hint="Recover the interrupted lifecycle first.",
        )
    if isinstance(exc, OrphanedOperationsError):
        return AppBlocker(
            kind="orphaned_operation",
            subject=exc.attempted,
            detail=str(exc),
            hint="Run `vcs-core archive-orphaned-operations` first.",
        )
    if isinstance(exc, SiblingGroupRecoveryRequiredError):
        subject = exc.groups[0] if exc.groups else exc.attempted
        return AppBlocker(
            kind="sibling_group",
            subject=subject,
            detail=str(exc),
            hint="Resume, cancel, archive, or complete the sibling group first.",
        )
    if isinstance(exc, WorkspaceAuthorityRecoveryRequiredError):
        subject = exc.operations[0] if exc.operations else exc.attempted
        return AppBlocker(
            kind="workspace_authority",
            subject=subject,
            detail=str(exc),
            hint="Run `vcs-core recover-workspace-authority` first.",
        )
    if isinstance(exc, ScopeAdmissionError):
        return AppBlocker(kind="live_scope", subject="scope", detail=str(exc))
    return AppBlocker(kind="scope_registry_mismatch", subject=exc.__class__.__name__, detail=str(exc))


def _sort_blockers(blockers: tuple[AppBlocker, ...] | list[AppBlocker]) -> tuple[AppBlocker, ...]:
    return sort_app_blockers(blockers)


def _dedupe_blockers(blockers: tuple[AppBlocker, ...]) -> tuple[AppBlocker, ...]:
    return dedupe_app_blockers(blockers)
