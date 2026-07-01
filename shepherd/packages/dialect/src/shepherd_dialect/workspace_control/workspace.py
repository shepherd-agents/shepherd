"""Product facade for the Shepherd workspace-control core loop."""

from __future__ import annotations

import ast
import importlib
import inspect
import os
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, NoReturn, Protocol, get_type_hints

from shepherd2.schemas.execution import execution_id_for
from vcs_core import InvalidRepositoryStateError
from vcs_core.runtime_api import native_jail_available
from vcs_core.runtime_substrate import TaskTraceSubstrateDriver, resolve_task_id

from shepherd_dialect.workspace_control._confined_task_executor import (
    ConfinedProcessTaskExecutorDescriptor,
    ConfinedRootTaskProvider,
    ConfinedTaskExecutionError,
)
from shepherd_dialect.workspace_control.authority_declarations import (
    AuthorityDeclarationError,
    compile_gitrepo_grant_from_annotation,
    compile_gitrepo_grant_from_ast_annotation,
    raw_annotation_looks_like_authority,
)
from shepherd_dialect.workspace_control.drivers import (
    TASK_ARTIFACT_RESOURCE_ID,
    TASK_ARTIFACT_STORE_ID,
    ShepherdRunLedgerDriver,
    ShepherdTaskArtifactDriver,
    ShepherdTaskLedgerDriver,
    mint_ledger_write_authority,
)
from shepherd_dialect.workspace_control.errors import WorkspaceControlError
from shepherd_dialect.workspace_control.feature_flags import _seal_and_select_enabled
from shepherd_dialect.workspace_control.identities import (
    RunRef,
    RunRefInput,
    RunSelectorInput,
    TaskRefInput,
    WorkspaceRef,
    coerce_exact_run_ref,
    coerce_optional_run_ref,
    coerce_run_ref,
    coerce_run_selector,
    coerce_task_ref,
)
from shepherd_dialect.workspace_control.input_refs import (
    RunArtifactInputRef,
    build_run_args_payload,
    iter_run_artifact_input_refs,
    validate_run_artifact_input_refs,
)
from shepherd_dialect.workspace_control.may import (
    DEFAULT_WORKSPACE_MAY_PROFILE,
    MayProfileError,
    WorkspaceAuthorityDecision,
    canonical_may_profile_name,
    resolve_workspace_authority_decision,
)
from shepherd_dialect.workspace_control.queries import (
    TASK_ARTIFACT_BINDING,
    TASK_ARTIFACT_SCHEMA,
    TASK_LEDGER_BINDING,
    TASK_LEDGER_SCHEMA,
    get_run,
    get_task,
    list_runs,
    list_tasks,
    outputs_for_exact_run,
    outputs_for_run,
    resolve_run_selector,
    resolve_task,
    run_output_citations,
    run_output_citations_for_exact_run,
    run_vcscore_projection,
    run_vcscore_projection_for_exact_run,
    show_run,
    trace_exact_run,
    trace_run,
)
from shepherd_dialect.workspace_control.retained_output_authority import retained_output_authority_provider_for_context
from shepherd_dialect.workspace_control.retained_outputs import _validated_retained_run_output_settlement_request
from shepherd_dialect.workspace_control.run_ledger import (
    RunLedgerPublishError,
    append_resolution,
    canonical_digest,
    publish_run_record,
    publish_terminal_run_record,
    run_ledger_payload,
    utc_now,
)
from shepherd_dialect.workspace_control.run_outputs import RunOutput
from shepherd_dialect.workspace_control.runtime_provider import (
    CLAUDE_WORKSPACE_INPUT_DIR,
    ClaudeWorkspaceRuntimeProvider,
    RuntimeProviderTaskExecutorDescriptor,
    StaticWorkspaceRuntimeProvider,
    WorkspaceRunRuntimePlan,
    WorkspaceRuntimeInputArtifact,
    WorkspaceRuntimePlanError,
    resolve_workspace_run_runtime_plan,
)
from shepherd_dialect.workspace_control.schemas import (
    DeclaredTaskDependency,
    ResolvedTask,
    ResolvedTaskGraph,
    RunEnforcement,
    RunEnforcementBasis,
    RunExecutionEvidence,
    RunLaunchContext,
    RunOperationRefs,
    RunOutputCitationRef,
    RunRecord,
    RunRetainedCustody,
    RunSummary,
    RunTerminalization,
    TaskArtifactLock,
    TaskArtifactRef,
    TaskDefinitionVersion,
    TaskDependencyLock,
    TaskExecutionRecord,
    TaskResolutionRecord,
    TaskSummary,
    TraceRef,
    run_can_produce_source_identity,
    run_has_published_workspace_output,
    run_trace_terminal_status,
)
from shepherd_dialect.workspace_control.workspace_authority import (
    WORKSPACE_FILESYSTEM_AUTHORITY_BINDING_ROOTS,
    run_authority_context_for_decision,
    workspace_filesystem_authority_grant_clamp,
)

if TYPE_CHECKING:
    from shepherd_runtime.nucleus import GitRepo
    from vcs_core.runtime_api import AuthorityDecision
    from vcs_core.types import RetainedOutputSelectionResult, RetainedOutputSettlementResult

    from shepherd_dialect.runtime_options import RuntimeOptions
    from shepherd_dialect.workspace_control.changesets import Changeset
    from shepherd_dialect.workspace_control.flow_context import FlowRunContext
    from shepherd_dialect.workspace_control.run_handles import WorkspaceRun
    from shepherd_dialect.workspace_control.task_handles import WorkspaceTask

JsonObject = dict[str, object]
LaunchSurfaceValue = Literal["python", "cli", "model_tool", "sdk", "operator"]
WorkspaceRunPlacement = Literal["auto", "advisory", "jail"]
WorkspaceBackend = Literal["clonefile", "fuse", "kernel", "copy"]
BindingPolicy = Literal["pinned", "once_per_run", "live"]
TaskLibraryMutationKind = Literal["create", "derive"]
DeclaredDependencyInput = Mapping[str, object] | str | DeclaredTaskDependency

_TASK_ARTIFACT_REF_SCHEMA = "shepherd.workspace_control.task_artifact_ref.v1"
_BINDING_POLICIES = frozenset({"pinned", "once_per_run", "live"})
_ARTIFACT_PUT_RETRIES = 3
_FENCED_RUN_START_ENV = "SHEPHERD_ENABLE_FENCED_RUN_START"
_FENCED_RUN_START_MESSAGE = (
    "V1D-015: runs.start is fenced as a compatibility entry point; use workspace.run(..., repo=...) "
    f"for the launch path, or set {_FENCED_RUN_START_ENV}=1 only for historical run-start probes"
)
_CURRENT_TASK_RUNTIME: ContextVar[TaskRuntimeContext | None] = ContextVar(
    "shepherd_workspace_control_task_runtime",
    default=None,
)


class TaskNotFoundError(WorkspaceControlError):
    """Raised when a task ref cannot be resolved from the task library."""


class TaskRegistrationError(WorkspaceControlError):
    """Raised when a task source cannot be registered."""


class RunStartError(WorkspaceControlError):
    """Raised when a run cannot be started or recorded."""


class _NucleusRunExecutionError(Exception):
    """Internal wrapper preserving diagnostics collected before runtime failure."""

    def __init__(
        self,
        cause: BaseException,
        *,
        task_resolutions: tuple[TaskResolutionRecord,...],
        task_executions: tuple[TaskExecutionRecord,...],
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.task_resolutions = task_resolutions
        self.task_executions = task_executions


class _NucleusRetainedRunExecutionError(_NucleusRunExecutionError):
    """Retained-output runtime wrapper failure."""


class _NucleusAuthorityRunExecutionError(_NucleusRunExecutionError):
    """Authority-terminalized runtime wrapper failure."""


@dataclass(frozen=True)
class TaskExecutionRequest:
    """Input to the workspace-control task executor seam."""

    run_ref: str
    task_lock: TaskArtifactLock
    repo: Any
    kwargs: Mapping[str, Any]
    call_kind: Literal["root_run", "linked_call"]
    resolution_id: str | None = None
    alias_path: str | None = None
    metadata: Mapping[str, object] | None = None


class TaskExecutor(Protocol):
    """Executes one exact task artifact lock under a recorded policy."""

    executor_kind: Literal["in_process", "process", "confined_process"]
    executor_id: str
    executor_policy: str

    def execute(self, workspace: Any, request: TaskExecutionRequest) -> Any:
        """Execute the request and return the task body's value."""


class InProcessTaskExecutor:
    """Bridge executor for today's trusted same-interpreter task invocation."""

    executor_kind: Literal["in_process"] = "in_process"
    executor_id = "shepherd.workspace_control.executor.in_process.v0"
    executor_policy = "trusted_bridge"

    def execute(self, workspace: Any, request: TaskExecutionRequest) -> Any:
        with _loaded_task_callable(workspace.mg, request.task_lock.artifact_ref) as task_body:
            return task_body(request.repo, **request.kwargs)


@dataclass(frozen=True)
class RetainedExecutionPlan:
    """Selected retained-run execution monitor plan and durable evidence shape."""

    mode: Literal["in_process", "confined_process"]
    provider: str
    executor_kind: Literal["in_process", "confined_process"]
    profile: str
    authority_basis: str
    requested_monitor: str | None = None
    monitor_required: bool = False

    def to_descriptor(
        self,
        *,
        established_monitor: str | None = None,
        monitor_refusal: Mapping[str, object] | None = None,
        prelaunch_refusal: Mapping[str, object] | None = None,
        body_refusal: Mapping[str, object] | None = None,
    ) -> JsonObject:
        return {
            "mode": self.mode,
            "provider": self.provider,
            "executor_kind": self.executor_kind,
            "profile": self.profile,
            "authority_basis": self.authority_basis,
            "requested_monitor": self.requested_monitor,
            "monitor_required": self.monitor_required,
            "established_monitor": established_monitor,
            "monitor_refusal": None if monitor_refusal is None else dict(monitor_refusal),
            "prelaunch_refusal": None if prelaunch_refusal is None else dict(prelaunch_refusal),
            "body_refusal": None if body_refusal is None else dict(body_refusal),
        }


@dataclass(frozen=True)
class _WorkspaceRunPlacementDecision:
    requested: WorkspaceRunPlacement
    resolved: Literal["advisory", "jail"]
    execution_descriptor: JsonObject
    initial_enforcement_basis: RunEnforcementBasis

    def task_execution_metadata(self) -> JsonObject:
        return {
            "requested_placement": self.requested,
            "resolved_placement": self.resolved,
            "placement": self.resolved,
        }

    def evidence(self) -> RunExecutionEvidence:
        return RunExecutionEvidence(
            requested_placement=self.requested,
            resolved_placement=self.resolved,
            enforcement_basis=self.initial_enforcement_basis,
            execution_descriptor=self.execution_descriptor,
        )


@dataclass
class TaskHandle:
    """Callable in-run task handle resolved by the workspace-control linker."""

    runtime: TaskRuntimeContext
    selector: str
    policy: BindingPolicy
    reason: str = "dynamic_lookup"
    declared_alias: str | None = None
    pinned_lock: TaskArtifactLock | None = None
    source_resolution: TaskResolutionRecord | None = None
    cached_resolution: TaskResolutionRecord | None = None
    alias_path: str | None = None
    handle_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.selector, str) or not self.selector:
            raise RunStartError("task handle selector must be a non-empty string")
        if self.policy not in _BINDING_POLICIES:
            raise RunStartError(f"unsupported task handle binding policy: {self.policy!r}")
        if not isinstance(self.reason, str) or not self.reason:
            raise RunStartError("task handle reason must be a non-empty string")
        if self.policy == "pinned" and self.pinned_lock is None:
            raise RunStartError("pinned task handles require an exact task lock")
        if self.handle_id == "":
            self.handle_id = f"task-handle-{uuid.uuid4().hex[:12]}"

    def resolve(self) -> TaskResolutionRecord:
        """Resolve or reuse the exact task lock selected by this handle."""
        if self.policy == "once_per_run" and self.cached_resolution is not None:
            return self.cached_resolution
        if self.policy == "pinned":
            if self.cached_resolution is None:
                assert self.pinned_lock is not None
                self.cached_resolution = self.runtime._resolution_for_lock(
                    self.pinned_lock,
                    selector=self.selector,
                    reason=self.reason,
                    declared_alias=self.declared_alias,
                    source_resolution=self.source_resolution,
                    metadata=self._metadata(),
                )
            return self.cached_resolution
        resolution = self.runtime.resolve_task(
            self.selector,
            reason=self.reason,
            declared_alias=self.declared_alias,
            metadata=self._metadata(),
        )
        if self.policy == "once_per_run":
            self.cached_resolution = resolution
        return resolution

    def __call__(self, **kwargs: Any) -> Any:
        """Execute the linked task artifact inside the current run scope."""
        resolution = self.resolve()
        return self.runtime._invoke_lock(
            resolution.task_lock,
            kwargs,
            resolution=resolution,
            alias_path=self.alias_path,
        )

    def _metadata(self) -> JsonObject:
        metadata: JsonObject = {
            "binding_policy": self.policy,
            "resolution_kind": "exact_lock" if self.policy == "pinned" else "symbolic",
            "selector": self.selector,
            "handle_id": self.handle_id,
            "call_index": self.runtime._next_call_index(),
        }
        if self.source_resolution is not None:
            metadata["source_resolution_id"] = self.source_resolution.resolution_id
        if self.alias_path is not None:
            metadata["alias_path"] = self.alias_path
        return metadata


class RuntimeTaskLibrary:
    """Task-library facade available inside one active task runtime context."""

    def __init__(self, runtime: TaskRuntimeContext) -> None:
        self._runtime = runtime

    def handle(
        self,
        selector: str,
        *,
        policy: BindingPolicy = "live",
        reason: str = "dynamic_lookup",
    ) -> TaskHandle:
        return TaskHandle(
            runtime=self._runtime,
            selector=selector,
            policy=policy,
            reason=reason,
        )

    def pinned(self, lock_or_resolution: TaskArtifactLock | TaskResolutionRecord) -> TaskHandle:
        source_resolution = lock_or_resolution if isinstance(lock_or_resolution, TaskResolutionRecord) else None
        lock = lock_or_resolution.task_lock if source_resolution is not None else lock_or_resolution
        if not isinstance(lock, TaskArtifactLock):
            raise RunStartError("pinned task handles require a TaskArtifactLock or TaskResolutionRecord")
        return TaskHandle(
            runtime=self._runtime,
            selector=f"{lock.task_id}@{lock.version}",
            policy="pinned",
            reason="pinned",
            pinned_lock=lock,
            source_resolution=source_resolution,
        )

    def declared(self, alias: str) -> TaskHandle:
        if not isinstance(alias, str) or not alias:
            raise RunStartError("child task alias must be a non-empty string")
        current = self._runtime._current_lock
        alias_path = self._runtime._child_alias_path(alias)
        cache_key = (current.task_id, current.version, current.artifact_digest, alias_path)
        cached = self._runtime._declared_handles.get(cache_key)
        if cached is not None:
            return cached
        dependency = self._runtime._declared_dependency(alias)
        selector = (
            dependency.task_id if dependency.selector == "active" else f"{dependency.task_id}@{dependency.selector}"
        )
        handle = TaskHandle(
            runtime=self._runtime,
            selector=selector,
            policy="once_per_run",
            reason="declared_alias",
            declared_alias=alias,
            alias_path=alias_path,
        )
        self._runtime._declared_handles[cache_key] = handle
        return handle

    def register(
        self,
        source: str | Callable[..., Any],
        *,
        task_id: str | None = None,
        may_default: str | None = None,
        declared_dependencies: Mapping[str, DeclaredDependencyInput] | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> TaskDefinitionVersion:
        self._raise_task_library_mutation_unsupported()

    def update(
        self,
        task_id: str,
        source: str | Callable[..., Any],
        *,
        base_version: str,
        produced_by_run: str | None = None,
        derived_from: tuple[str,...] = (),
        source_identity: str | None = None,
        may_default: str | None = None,
        declared_dependencies: Mapping[str, DeclaredDependencyInput] | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> TaskDefinitionVersion:
        self._raise_task_library_mutation_unsupported()

    def register_source(
        self,
        *,
        task_id: str,
        module: str,
        source_text: str,
        entrypoint: str,
        may_default: str | None = None,
        declared_dependencies: Mapping[str, DeclaredDependencyInput] | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> TaskDefinitionVersion:
        self._raise_task_library_mutation_unsupported()

    def update_source(
        self,
        task_id: str,
        *,
        base_version: str,
        module: str,
        source_text: str,
        entrypoint: str,
        may_default: str | None = None,
        declared_dependencies: Mapping[str, DeclaredDependencyInput] | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> TaskDefinitionVersion:
        self._raise_task_library_mutation_unsupported()

    def _raise_task_library_mutation_unsupported(self) -> NoReturn:
        raise RunStartError(
            "task-library mutation during a retained nucleus run is not supported; "
            "register or update tasks before starting the run"
        )


class TaskRuntimeContext:
    """Runtime surface exposed to task bodies for declared child-task calls."""

    def __init__(
        self,
        *,
        workspace: ShepherdWorkspace,
        run_ref: str,
        graph: ResolvedTaskGraph,
        repo: Any,
        root_resolution: TaskResolutionRecord,
        task_execution_metadata: Mapping[str, object] | None = None,
    ) -> None:
        self._workspace = workspace
        self._run_ref = run_ref
        self._graph = graph
        self._repo = repo
        self._task_stack: list[TaskArtifactLock] = [root_resolution.task_lock]
        self._alias_path_stack: list[str | None] = [None]
        self._resolutions: list[TaskResolutionRecord] = [root_resolution]
        self._executions: list[TaskExecutionRecord] = []
        self._task_execution_metadata = dict(task_execution_metadata or {})
        self._declared_handles: dict[tuple[str, str, str, str], TaskHandle] = {}
        self._call_index = 0
        self.tasks = RuntimeTaskLibrary(self)

    @property
    def run_ref(self) -> str:
        return self._run_ref

    @property
    def ref(self) -> RunRef:
        """Return this task run's typed public identity value."""
        return RunRef(id=self._run_ref)

    @property
    def graph(self) -> ResolvedTaskGraph:
        return self._graph

    @property
    def task_resolutions(self) -> tuple[TaskResolutionRecord,...]:
        return tuple(self._resolutions)

    @property
    def task_executions(self) -> tuple[TaskExecutionRecord,...]:
        return tuple(self._executions)

    def resolve_task(
        self,
        task_ref: TaskRefInput,
        /,
        *,
        reason: str = "dynamic_lookup",
        declared_alias: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> TaskResolutionRecord:
        """Resolve a symbolic task ref and record the lock in this run's link map."""
        task_ref_id = coerce_task_ref(task_ref)
        task_payload, task_ledger_head = _selected_task_ledger_payload_with_head(self._workspace.mg)
        task = _get_task_from_payload(task_payload, task_ref_id)
        if task is None:
            raise TaskNotFoundError(f"no active task matches {task_ref_id!r}")
        if task.status == "draft":
            raise RunStartError(f"task {task.task_id}@{task.version} is draft; activate it after dependencies resolve")
        resolution = _task_resolution_record(
            task_ref=task_ref_id,
            task=task,
            reason=reason,
            task_ledger_head=task_ledger_head,
            parent_run_ref=self._run_ref,
            requester_task_id=self._current_lock.task_id,
            requester_task_version=self._current_lock.version,
            declared_alias=declared_alias,
            launch_surface="python",
            metadata=metadata,
        )
        self._remember_resolution(resolution)
        return resolution

    def run_task(self, resolution: TaskResolutionRecord | TaskArtifactLock, /, **kwargs: Any) -> Any:
        """Run an already resolved task lock inside the current run context."""
        lock = resolution.task_lock if isinstance(resolution, TaskResolutionRecord) else resolution
        if not isinstance(lock, TaskArtifactLock):
            raise RunStartError("run_task requires a TaskResolutionRecord or TaskArtifactLock")
        return self._invoke_lock(lock, kwargs)

    def call_task(self, alias: str, /, **kwargs: Any) -> Any:
        """Call a declared child task by alias through a once-per-run handle."""
        return self.tasks.declared(alias)(**kwargs)

    @property
    def _current_lock(self) -> TaskArtifactLock:
        return self._task_stack[-1]

    @property
    def _current_alias_path(self) -> str | None:
        return self._alias_path_stack[-1]

    def _child_alias_path(self, alias: str) -> str:
        current = self._current_alias_path
        return alias if current is None else f"{current}.{alias}"

    def _next_call_index(self) -> int:
        self._call_index += 1
        return self._call_index

    def _remember_resolution(self, resolution: TaskResolutionRecord) -> None:
        if not any(existing.resolution_id == resolution.resolution_id for existing in self._resolutions):
            self._resolutions.append(resolution)

    def _resolution_for_lock(
        self,
        lock: TaskArtifactLock,
        *,
        selector: str,
        reason: str,
        declared_alias: str | None,
        source_resolution: TaskResolutionRecord | None,
        metadata: Mapping[str, object],
    ) -> TaskResolutionRecord:
        task_ledger_head = None if source_resolution is None else source_resolution.task_ledger_head
        resolution = TaskResolutionRecord(
            resolution_id=f"task-resolution-{uuid.uuid4().hex[:12]}",
            reason=reason,
            requested_ref=selector,
            task_ledger_head=task_ledger_head,
            task_lock=lock,
            parent_run_ref=self._run_ref,
            requester_task_id=self._current_lock.task_id,
            requester_task_version=self._current_lock.version,
            declared_alias=declared_alias,
            launch_surface="python",
            resolved_at=_utc_now(),
            metadata=dict(metadata),
        )
        self._remember_resolution(resolution)
        return resolution

    def _declared_dependency(self, alias: str) -> DeclaredTaskDependency:
        payload = _read_task_artifact(self._workspace.mg, self._current_lock.artifact_ref)
        raw_dependencies = payload.get("declared_dependencies", {})
        if not isinstance(raw_dependencies, Mapping):
            raise RunStartError("task artifact declared_dependencies must be an object")
        raw_dependency = raw_dependencies.get(alias)
        if not isinstance(raw_dependency, Mapping):
            raise RunStartError(
                f"task {self._current_lock.task_id}@{self._current_lock.version} "
                f"did not declare a dependency alias {alias!r}"
            )
        return DeclaredTaskDependency.from_json(raw_dependency)

    def _invoke_lock(
        self,
        lock: TaskArtifactLock,
        kwargs: Mapping[str, Any],
        *,
        resolution: TaskResolutionRecord | None = None,
        alias_path: str | None = None,
    ) -> Any:
        self._task_stack.append(lock)
        self._alias_path_stack.append(alias_path)
        request = TaskExecutionRequest(
            run_ref=self._run_ref,
            task_lock=lock,
            repo=self._repo,
            kwargs=dict(kwargs),
            call_kind="linked_call",
            resolution_id=None if resolution is None else resolution.resolution_id,
            alias_path=alias_path,
            metadata=dict(self._task_execution_metadata),
        )
        started = _started_task_execution_record(self._workspace.task_executor, request)
        try:
            result = self._workspace.task_executor.execute(self._workspace, request)
        except Exception as exc:
            self._executions.append(_failed_task_execution_record(started, exc))
            raise
        else:
            self._executions.append(_completed_task_execution_record(started))
            return result
        finally:
            self._alias_path_stack.pop()
            self._task_stack.pop()


def current_task_context() -> TaskRuntimeContext:
    """Return the current workspace-control task runtime context."""
    context = _CURRENT_TASK_RUNTIME.get()
    if context is None:
        raise RuntimeError("no Shepherd task runtime context is active")
    return context


class ShepherdWorkspace:
    """Shepherd workspace-control facade over a vcs-core workspace."""

    def __init__(
        self,
        mg: Any,
        *,
        trace_store_path: str | Path | None = None,
        workspace_path: str | Path | None = None,
        task_executor: TaskExecutor | None = None,
    ) -> None:
        self.mg = mg
        self.workspace_path = None if workspace_path is None else Path(workspace_path)
        self.trace_store_path = (
            Path(trace_store_path) if trace_store_path is not None else _default_trace_store_path(self.workspace_path)
        )
        self.task_executor = task_executor or InProcessTaskExecutor()
        self.tasks = TaskLibraryClient(self)
        self.runs = RunControlClient(self)
        from shepherd_dialect.workspace_control.flows import FlowControlClient

        self.flows = FlowControlClient(self)

    @classmethod
    def discover(
        cls,
        cwd: str | Path = ".",
        *,
        activate: bool = True,
        backend: WorkspaceBackend | None = None,
    ) -> ShepherdWorkspace:
        """Open an activated Shepherd workspace-control facade at ``cwd``.

        The workspace-control read APIs are selected-world queries, so the
        facade is readable by default. ``activate=False`` is reserved for
        callers that only need an inert VcsCore handle.

        ``backend`` selects the filesystem carrier. The default ``None`` resolves
        per platform (APFS clonefile on macOS, kernel/FUSE overlay on Linux, and
        the portable copy carrier as a universal floor); pass ``"clonefile"``,
        ``"fuse"``, ``"kernel"``, or ``"copy"`` to force one explicitly.
        """
        workspace = Path(cwd).resolve()
        repo_path = workspace / ".vcscore"
        if not repo_path.exists():
            raise WorkspaceControlError("not a Shepherd workspace. Run `sp init` first.")
        from vcs_core import FilesystemSubstrate, MarkerSubstrate, Store, VcsCore, build_builtin_substrate_context

        from shepherd_dialect.run_driver import ShepherdRunDriver

        store = Store(str(repo_path))
        config = {} if backend is None else {"backend": backend}
        context = build_builtin_substrate_context(store=store, workspace=workspace, config=config)
        mg = VcsCore(
            str(workspace),
            substrates=[
                MarkerSubstrate(context),
                FilesystemSubstrate(context),
                TaskTraceSubstrateDriver(),
                ShepherdTaskLedgerDriver(),
                ShepherdTaskArtifactDriver(),
                ShepherdRunLedgerDriver(),
                ShepherdRunDriver(),
            ],
            store=store,
        )
        if activate:
            with _seal_and_select_enabled():
                mg.activate()
        return cls(mg, workspace_path=workspace)

    def close(self) -> None:
        """Deactivate the underlying vcs-core handle when supported."""
        deactivate = getattr(self.mg, "deactivate", None)
        if callable(deactivate):
            deactivate()

    @property
    def ref(self) -> WorkspaceRef:
        """Return this facade's typed workspace identity value."""
        if self.workspace_path is None:
            raise WorkspaceControlError("workspace identity requires a workspace path")
        return WorkspaceRef.from_path(self.workspace_path)

    def git_repo(self) -> GitRepo:
        """Return the current selected workspace binding as a GitRepo value noun."""
        from shepherd_dialect.workspace_control.gitrepo_handles import selected_workspace_git_repo

        return selected_workspace_git_repo(self.mg)

    def run(
        self,
        task_ref: TaskRefInput,
        *,
        repo: GitRepo,
        args: Mapping[str, Any] | None = None,
        may: str | None = None,
        placement: WorkspaceRunPlacement = "auto",
        runtime: Mapping[str, object] | RuntimeOptions | None = None,
    ) -> WorkspaceRun:
        """Run a task against the current selected workspace GitRepo basis.

        This is the first narrow facade: handle in, retained output
        views out. Execution routes through the nucleus/vcs-core retained-output
        producer. ``placement="auto"`` uses the native jail on jail-capable
        hosts and records advisory execution otherwise; ``placement="jail"``
        is fail-closed. Callers should reacquire ``workspace.git_repo()`` after
        selecting an output before starting the next run.
        """
        return self._run_retained_workspace(
            task_ref,
            repo=repo,
            args=args,
            may=may,
            placement=placement,
            runtime=runtime,
            flow_context=None,
        )

    def _run_with_flow_context(
        self,
        task_ref: TaskRefInput,
        *,
        repo: GitRepo,
        flow_context: FlowRunContext,
        args: Mapping[str, Any] | None = None,
        may: str | None = None,
        placement: WorkspaceRunPlacement = "auto",
        runtime: Mapping[str, object] | RuntimeOptions | None = None,
    ) -> WorkspaceRun:
        """Run a task with internal workflow metadata attached at run start."""
        return self._run_retained_workspace(
            task_ref,
            repo=repo,
            args=args,
            may=may,
            placement=placement,
            runtime=runtime,
            flow_context=flow_context,
        )

    def _run_retained_workspace(
        self,
        task_ref: TaskRefInput,
        *,
        repo: GitRepo,
        args: Mapping[str, Any] | None,
        may: str | None,
        placement: WorkspaceRunPlacement,
        runtime: Mapping[str, object] | RuntimeOptions | None,
        flow_context: FlowRunContext | None,
    ) -> WorkspaceRun:
        from shepherd_dialect.workspace_control.gitrepo_handles import require_selected_workspace_git_repo
        from shepherd_dialect.workspace_control.run_handles import WorkspaceRun

        require_selected_workspace_git_repo(self.mg, repo)
        record = self.runs._start_retained_workspace_run(
            coerce_task_ref(task_ref),
            args=args,
            may=may,
            placement=placement,
            runtime=runtime,
            launch_surface="python",
            flow_context=flow_context,
        )
        return WorkspaceRun(self, record)

    def select(self, output: RunOutput) -> RetainedOutputSelectionResult:
        """Select a resolved retained run output into its live parent world."""
        return self._settle_retained_run_output(output, method_name="select_retained_output")

    def release(self, output: RunOutput) -> RetainedOutputSettlementResult:
        """Consume a resolved retained run output without selecting it."""
        return self._settle_retained_run_output(output, method_name="release_retained_output")

    def discard(self, output: RunOutput) -> RetainedOutputSettlementResult:
        """Consume a resolved retained run output as discarded."""
        return self._settle_retained_run_output(output, method_name="discard_retained_output")

    def _settle_retained_run_output(self, output: RunOutput, *, method_name: str) -> Any:
        request = _validated_retained_run_output_settlement_request(self, output)
        kwargs: dict[str, Any] = {}
        if method_name == "select_retained_output":
            provider = _retained_output_selection_authority_provider(self.mg, request.output)
            kwargs["decide"] = provider
            kwargs["effective_match_digest"] = provider.effective_match_digest
            kwargs["authority_surface_plan_digest"] = provider.authority_surface_plan_digest
            kwargs["permission_plan_digest"] = provider.permission_plan_digest
            kwargs["permission_plan_descriptor"] = provider.permission_plan_descriptor
            if provider.authority_context is not None:
                kwargs["authority_context"] = dict(provider.authority_context)
        method = getattr(self.mg, method_name, None)
        if not callable(method):
            raise WorkspaceControlError(f"VcsCore.{method_name} is required for run-output settlement")
        with _seal_and_select_enabled():
            try:
                return method(request.handle, parent=request.parent, binding=request.binding, **kwargs)
            except InvalidRepositoryStateError as exc:
                message = str(exc)
                if method_name == "select_retained_output" and message.startswith("retained-output selection"):
                    raise WorkspaceControlError(message) from exc
                raise


class TaskLibraryClient:
    """Task-library read and write operations."""

    def __init__(self, workspace: ShepherdWorkspace) -> None:
        self._workspace = workspace

    @property
    def mg(self) -> Any:
        return self._workspace.mg

    def list(self, *, status: str | None = None, prefix: str | None = None) -> tuple[TaskSummary,...]:
        return list_tasks(self.mg, status=status, prefix=prefix)

    def get(self, task_ref: TaskRefInput) -> TaskDefinitionVersion | None:
        return get_task(self.mg, coerce_task_ref(task_ref))

    def describe(self, task_ref: TaskRefInput) -> JsonObject | None:
        """Return a user-facing task definition description."""
        task = self.get(task_ref)
        if task is None:
            return None
        artifact: JsonObject | None = None
        artifact_error: str | None = None
        if task.artifact_ref is not None:
            try:
                payload = _read_task_artifact(self.mg, task.artifact_ref)
            except (TypeError, ValueError, RuntimeError) as exc:
                artifact_error = str(exc)
            else:
                artifact = _task_artifact_description(payload)
        return {
            "task": task.to_json(),
            "artifact": artifact,
            "artifact_error": artifact_error,
        }

    def task(self, task_ref: TaskRefInput) -> WorkspaceTask:
        """Return a workspace-scoped task noun for the handle-in run facade."""
        from shepherd_dialect.workspace_control.task_handles import WorkspaceTask

        return WorkspaceTask(self._workspace, coerce_task_ref(task_ref))

    def resolve(self, task_ref: TaskRefInput) -> ResolvedTask | None:
        return resolve_task(self.mg, coerce_task_ref(task_ref))

    def register(
        self,
        source: str | Callable[..., Any],
        *,
        task_id: str | None = None,
        may_default: str | None = None,
        declared_dependencies: Mapping[str, DeclaredDependencyInput] | None = None,
        metadata: Mapping[str, object] | None = None,
        produced_by_run: str | None = None,
        derived_from: tuple[str,...] = (),
        source_identity: str | None = None,
    ) -> TaskDefinitionVersion:
        """Register a task source as a new task version.

        Versions with unresolved declared dependencies are accepted as
        ``draft``; active versions must have a fully resolvable dependency graph.
        """
        task_source = _resolve_task_source(source)
        resolved_task_id = task_id or _default_task_id(task_source.import_path)
        return self._apply_mutation(
            _TaskLibraryMutation(
                kind="create",
                task_id=resolved_task_id,
                source=task_source,
                may_default=_resolve_task_may_default(may_default, task_source),
                declared_dependencies=_coerce_declared_dependencies(declared_dependencies),
                metadata=dict(metadata or {}),
                base_version=None,
                produced_by_run=produced_by_run,
                derived_from=derived_from,
                source_identity=source_identity,
            )
        )

    def update_source(
        self,
        task_id: str,
        *,
        base_version: str,
        module: str,
        source_text: str,
        entrypoint: str,
        may_default: str | None = None,
        declared_dependencies: Mapping[str, DeclaredDependencyInput] | None = None,
        metadata: Mapping[str, object] | None = None,
        produced_by_run: str | None = None,
        derived_from: tuple[str,...] = (),
    ) -> TaskDefinitionVersion:
        """Commit a generated source update derived from an existing task version."""
        task_source = _task_source_from_source_text(
            module_name=module,
            qualname=entrypoint,
            source_text=source_text,
        )
        return self._apply_mutation(
            _TaskLibraryMutation(
                kind="derive",
                task_id=task_id,
                source=task_source,
                may_default=_resolve_task_may_default(may_default, task_source),
                declared_dependencies=_coerce_declared_dependencies(declared_dependencies),
                metadata=dict(metadata or {}),
                base_version=base_version,
                produced_by_run=produced_by_run,
                derived_from=derived_from,
                source_identity=None,
            )
        )

    def register_source(
        self,
        *,
        task_id: str,
        module: str,
        source_text: str,
        entrypoint: str,
        may_default: str | None = None,
        declared_dependencies: Mapping[str, DeclaredDependencyInput] | None = None,
        metadata: Mapping[str, object] | None = None,
        produced_by_run: str | None = None,
        derived_from: tuple[str,...] = (),
    ) -> TaskDefinitionVersion:
        """Register generated task source directly into the task artifact store."""
        task_source = _task_source_from_source_text(
            module_name=module,
            qualname=entrypoint,
            source_text=source_text,
        )
        return self._apply_mutation(
            _TaskLibraryMutation(
                kind="create",
                task_id=task_id,
                source=task_source,
                may_default=_resolve_task_may_default(may_default, task_source),
                declared_dependencies=_coerce_declared_dependencies(declared_dependencies),
                metadata=dict(metadata or {}),
                base_version=None,
                produced_by_run=produced_by_run,
                derived_from=derived_from,
                source_identity=None,
            )
        )

    def update(
        self,
        task_id: str,
        source: str | Callable[..., Any],
        *,
        base_version: str,
        produced_by_run: str | None = None,
        derived_from: tuple[str,...] = (),
        source_identity: str | None = None,
        may_default: str | None = None,
        declared_dependencies: Mapping[str, DeclaredDependencyInput] | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> TaskDefinitionVersion:
        """Commit an updated task definition version."""
        if produced_by_run is not None and source_identity is not None:
            _validate_run_produced_source_identity(self.mg, produced_by_run, source_identity)
        task_source = _resolve_task_source(source)
        return self._apply_mutation(
            _TaskLibraryMutation(
                kind="derive",
                task_id=task_id,
                source=task_source,
                may_default=_resolve_task_may_default(may_default, task_source),
                declared_dependencies=_coerce_declared_dependencies(declared_dependencies),
                metadata=dict(metadata or {}),
                base_version=base_version,
                produced_by_run=produced_by_run,
                derived_from=derived_from,
                source_identity=source_identity,
            )
        )

    def activate(self, task_ref: TaskRefInput) -> TaskDefinitionVersion:
        """Mark a registered task version active after dependency resolution succeeds."""
        task_ref_id = coerce_task_ref(task_ref)
        payload, expected_head = _selected_task_ledger_payload_with_head(self.mg)
        task = _get_task_from_payload(payload, task_ref_id)
        if task is None:
            raise TaskRegistrationError(f"no task version matches {task_ref_id!r}")
        if task.artifact_ref is None:
            raise TaskRegistrationError(f"task {task.task_id}@{task.version} has no artifact_ref")
        _resolve_task_graph_from_payload(self.mg, payload, task)
        existing_versions = _task_versions_for_payload(payload, task.task_id)
        updated_versions: list[TaskDefinitionVersion] = []
        activated: TaskDefinitionVersion | None = None
        for version in existing_versions:
            if version.version == task.version:
                activated = replace(version, status="active")
                updated_versions.append(activated)
            elif version.status == "active":
                updated_versions.append(replace(version, status="superseded"))
            else:
                updated_versions.append(version)
        if activated is None:
            raise TaskRegistrationError(f"no task version matches {task_ref_id!r}")
        tasks_payload = payload["tasks"]
        assert isinstance(tasks_payload, dict)
        tasks_payload[task.task_id] = [item.to_json() for item in updated_versions]
        self._publish_payload(payload, expected_head=expected_head)
        return activated

    def _apply_mutation(self, mutation: _TaskLibraryMutation) -> TaskDefinitionVersion:
        if mutation.kind == "derive" and mutation.base_version is None:
            raise TaskRegistrationError("task derivation requires base_version")
        if mutation.kind == "create" and mutation.base_version is not None:
            raise TaskRegistrationError("task creation cannot specify base_version")
        return self._publish_new_version(
            task_id=mutation.task_id,
            source=mutation.source,
            may_default=mutation.may_default,
            declared_dependencies=mutation.declared_dependencies,
            metadata=mutation.metadata,
            base_version=mutation.base_version,
            produced_by_run=mutation.produced_by_run,
            derived_from=mutation.derived_from,
            source_identity=mutation.source_identity,
        )

    def _publish_new_version(
        self,
        *,
        task_id: str,
        source: _TaskSource,
        may_default: str,
        declared_dependencies: Mapping[str, DeclaredTaskDependency],
        metadata: JsonObject,
        base_version: str | None,
        produced_by_run: str | None,
        derived_from: tuple[str,...],
        source_identity: str | None,
    ) -> TaskDefinitionVersion:
        if not task_id:
            raise TaskRegistrationError("task_id must be a non-empty string")
        artifact_ref, artifact_digest = self._publish_artifact(
            _task_artifact_payload(
                source=source,
                declared_dependencies=declared_dependencies,
                source_identity=source_identity,
                produced_by_run=produced_by_run,
            )
        )
        payload, expected_head = _selected_task_ledger_payload_with_head(self.mg)
        existing_versions = _task_versions_for_payload(payload, task_id)
        if base_version is not None and not any(version.version == base_version for version in existing_versions):
            raise TaskRegistrationError(f"task {task_id!r} cannot update from missing base_version {base_version!r}")
        active_versions = [version for version in existing_versions if version.status == "active"]
        if base_version is not None and not any(version.version == base_version for version in active_versions):
            raise TaskRegistrationError(f"task {task_id!r} cannot update from stale base_version {base_version!r}")
        next_version = _next_version(existing_versions)
        signature_schema = _task_source_signature_schema(source)
        status = "draft"
        version = TaskDefinitionVersion(
            task_id=task_id,
            version=next_version,
            base_version=base_version,
            import_path=source.import_path,
            artifact_ref=artifact_ref,
            artifact_digest=artifact_digest,
            source_identity=source_identity,
            schema_digest=_task_schema_digest(
                import_path=source.import_path,
                signature_schema=signature_schema,
                may_default=may_default,
                artifact_digest=artifact_digest,
            ),
            signature_schema=signature_schema,
            declared_dependencies=declared_dependencies,
            may_default=may_default,
            status=status,
            metadata=metadata,
            produced_by_run=produced_by_run,
            derived_from=derived_from,
            created_at=_utc_now(),
        )
        if _task_dependencies_resolve(self.mg, payload, version):
            version = replace(version, status="active")
        superseded = (
            [replace(item, status="superseded") for item in active_versions] if version.status == "active" else []
        )
        carried = [item for item in existing_versions if item.status != "active" or version.status != "active"]
        tasks_payload = payload["tasks"]
        assert isinstance(tasks_payload, dict)
        tasks_payload[task_id] = [item.to_json() for item in (*carried, *superseded, version)]
        self._publish_payload(payload, expected_head=expected_head)
        return version

    def _publish_artifact(self, payload: Mapping[str, object]) -> tuple[TaskArtifactRef, str]:
        artifact_digest = _artifact_digest_from_payload(payload)
        artifact_payload = dict(payload)
        artifact_payload["artifact_id"] = f"task-artifact:{artifact_digest.removeprefix('sha256:')}"
        artifact_payload["artifact_digest"] = artifact_digest
        outcome = None
        last_error: Exception | None = None
        for _attempt in range(_ARTIFACT_PUT_RETRIES):
            _, expected_head = _selected_payload_with_head(self.mg, TASK_ARTIFACT_BINDING)
            try:
                outcome = self.mg.exec(
                    TASK_ARTIFACT_BINDING,
                    "put",
                    scope=self.mg.ground,
                    payload=artifact_payload,
                    expected_head=expected_head,
                    authority=mint_ledger_write_authority(),
                )
                break
            except RuntimeError as exc:
                if "selected head moved" not in str(exc):
                    raise
                last_error = exc
        if outcome is None:
            raise TaskRegistrationError("task-artifact put failed after stale-head retries") from last_error
        if not outcome.oids:
            raise TaskRegistrationError("task-artifact put produced no revision oid")
        ref = TaskArtifactRef(
            schema=_TASK_ARTIFACT_REF_SCHEMA,
            binding=TASK_ARTIFACT_BINDING,
            store_id=TASK_ARTIFACT_STORE_ID,
            resource_id=TASK_ARTIFACT_RESOURCE_ID,
            head=outcome.oids[0],
            artifact_digest=artifact_digest,
        )
        return ref, artifact_digest

    def _publish_payload(self, payload: Mapping[str, object], *, expected_head: str | None) -> str:
        outcome = self.mg.exec(
            TASK_LEDGER_BINDING,
            "publish",
            scope=self.mg.ground,
            payload=dict(payload),
            expected_head=expected_head,
            authority=mint_ledger_write_authority(),
        )
        if not outcome.oids:
            raise TaskRegistrationError("task-ledger publish produced no revision oid")
        return outcome.oids[0]


def _coerce_run_query_input(run_ref: RunSelectorInput) -> tuple[str, bool]:
    """Return ``(run_ref, exact)`` for public run read/query inputs."""
    if isinstance(run_ref, RunRef):
        return coerce_run_ref(run_ref), True
    return coerce_run_selector(run_ref), False


def _coerce_optional_run_query_input(run_ref: RunSelectorInput | None) -> tuple[str | None, bool]:
    """Return ``(run_ref, exact)`` for optional public run read/query inputs."""
    if run_ref is None:
        return None, False
    return _coerce_run_query_input(run_ref)


class RunControlClient:
    """Run-control read and start operations."""

    def __init__(self, workspace: ShepherdWorkspace) -> None:
        self._workspace = workspace

    @property
    def mg(self) -> Any:
        return self._workspace.mg

    def list(
        self,
        *,
        status: str | None = None,
        task_id: str | None = None,
        max_count: int | None = None,
    ) -> tuple[RunSummary,...]:
        return list_runs(self.mg, status=status, task_id=task_id, max_count=max_count)

    def show(self, run_ref: RunSelectorInput) -> RunRecord | None:
        run_ref_id, exact_run_ref = _coerce_run_query_input(run_ref)
        if exact_run_ref:
            return get_run(self.mg, run_ref_id)
        return show_run(self.mg, run_ref_id)

    def trace(self, run_ref: RunSelectorInput, *, events: bool = False) -> Any:
        run_ref_id, exact_run_ref = _coerce_run_query_input(run_ref)
        if exact_run_ref:
            return trace_exact_run(self.mg, run_ref_id, events=events)
        return trace_run(self.mg, run_ref_id, events=events)

    def vcscore(self, run_ref: RunSelectorInput) -> Mapping[str, object] | None:
        run_ref_id, exact_run_ref = _coerce_run_query_input(run_ref)
        if exact_run_ref:
            return run_vcscore_projection_for_exact_run(self.mg, run_ref_id)
        return run_vcscore_projection(self.mg, run_ref_id)

    def output_citations(
        self,
        *,
        run_ref: RunSelectorInput | None = None,
        binding: str | None = None,
    ) -> tuple[RunOutputCitationRef,...]:
        run_ref_id, exact_run_ref = _coerce_optional_run_query_input(run_ref)
        if exact_run_ref:
            assert run_ref_id is not None
            return run_output_citations_for_exact_run(self.mg, run_ref=run_ref_id, binding=binding)
        return run_output_citations(self.mg, run_ref=run_ref_id, binding=binding)

    def outputs(
        self,
        *,
        run_ref: RunSelectorInput | None = None,
        parent: Any = None,
        binding: str | None = None,
        state: str | None = None,
        trace_store: Any = None,
    ) -> tuple[RunOutput,...]:
        owned_store = None
        if trace_store is None:
            from shepherd2.trace_store import SQLiteTraceStore

            owned_store = SQLiteTraceStore(self._workspace.trace_store_path)
            trace_store = owned_store
        try:
            run_ref_id, exact_run_ref = _coerce_optional_run_query_input(run_ref)
            with _seal_and_select_enabled():
                if exact_run_ref:
                    assert run_ref_id is not None
                    refs = outputs_for_exact_run(
                        self.mg,
                        run_ref=run_ref_id,
                        parent=parent,
                        binding=binding,
                        state=state,
                        trace_store=trace_store,
                    )
                else:
                    refs = outputs_for_run(
                        self.mg,
                        run_ref=run_ref_id,
                        parent=parent,
                        binding=binding,
                        state=state,
                        trace_store=trace_store,
                    )
                return tuple(RunOutput(self._workspace, ref) for ref in refs)
        finally:
            if owned_store is not None:
                owned_store.close()

    def changeset(
        self,
        run_ref: RunSelectorInput,
        *,
        output_name: str = "workspace",
        binding: str | None = None,
        state: str | None = None,
        trace_store: Any = None,
    ) -> Changeset:
        """Return a read-only changeset view for one retained run output."""
        if not isinstance(output_name, str) or not output_name:
            raise WorkspaceControlError("run changeset output_name must be a non-empty string")
        outputs = tuple(
            output
            for output in self.outputs(
                run_ref=run_ref,
                binding=binding,
                state=state,
                trace_store=trace_store,
            )
            if output.output_name == output_name
        )
        run_ref_id, _exact_run_ref = _coerce_run_query_input(run_ref)
        if not outputs:
            raise WorkspaceControlError(f"run {run_ref_id!r} has no output named {output_name!r}")
        if len(outputs) > 1:
            raise WorkspaceControlError(f"run {run_ref_id!r} has multiple outputs named {output_name!r}")
        return outputs[0].changeset()

    def output_for_settlement(
        self,
        run_ref: RunRefInput,
        *,
        output_name: str = "workspace",
        binding: str | None = None,
        trace_store: Any = None,
    ) -> RunOutput:
        """Resolve exactly one run-owned output for a mutation/settlement boundary."""
        if not isinstance(output_name, str) or not output_name:
            raise WorkspaceControlError("run output settlement output_name must be a non-empty string")
        try:
            run_ref_id = coerce_exact_run_ref(run_ref)
        except ValueError as exc:
            raise WorkspaceControlError(f"run output settlement requires an exact run identity; {exc}") from exc
        self._require_exact_run_identity_for_output_mutation(run_ref_id, operation="run output settlement")
        outputs = tuple(
            output
            for output in self.outputs(
                run_ref=RunRef(id=run_ref_id),
                binding=binding,
                trace_store=trace_store,
            )
            if output.output_name == output_name
        )
        if not outputs:
            raise WorkspaceControlError(f"run {run_ref_id!r} has no output named {output_name!r}")
        if len(outputs) > 1:
            raise WorkspaceControlError(f"run {run_ref_id!r} has multiple outputs named {output_name!r}")
        return outputs[0]

    def publish_retained_workspace_output(self, run_ref: RunRefInput) -> RunRecord:
        """Publish or repair the retained workspace output for one terminal run."""
        from shepherd_dialect.workspace_control.output_transition import publish_retained_workspace_output

        try:
            run_ref_id = coerce_exact_run_ref(run_ref)
        except ValueError as exc:
            raise WorkspaceControlError(f"run output publication requires an exact run identity; {exc}") from exc
        self._require_exact_run_identity_for_output_mutation(run_ref_id, operation="run output publication")
        with _seal_and_select_enabled():
            return publish_retained_workspace_output(
                self.mg,
                run_ref=run_ref_id,
                trace_store_path=self._workspace.trace_store_path,
            )

    def _require_exact_run_identity_for_output_mutation(self, run_ref: str, *, operation: str) -> None:
        if get_run(self.mg, run_ref) is not None:
            return
        try:
            record = resolve_run_selector(self.mg, run_ref)
        except ValueError as exc:
            raise WorkspaceControlError(
                f"{operation} requires an exact run identity; {run_ref!r} is ambiguous"
            ) from exc
        if record is not None:
            raise WorkspaceControlError(
                f"{operation} requires an exact run identity; {run_ref!r} resolved to {record.run_ref!r}"
            )

    def resolve_task(
        self,
        task_ref: TaskRefInput,
        *,
        reason: str = "dynamic_lookup",
        parent_run_ref: RunRefInput | None = None,
        requester_task_id: str | None = None,
        requester_task_version: str | None = None,
        declared_alias: str | None = None,
        launch_surface: LaunchSurfaceValue = "python",
        metadata: Mapping[str, object] | None = None,
    ) -> TaskResolutionRecord:
        """Resolve a symbolic task ref into an exact artifact lock."""
        task_ref_id = coerce_task_ref(task_ref)
        parent_run_ref_id = coerce_optional_run_ref(parent_run_ref, field_name="parent_run_ref")
        task_payload, task_ledger_head = _selected_task_ledger_payload_with_head(self.mg)
        task = _get_task_from_payload(task_payload, task_ref_id)
        if task is None:
            raise TaskNotFoundError(f"no active task matches {task_ref_id!r}")
        if task.status == "draft":
            raise RunStartError(f"task {task.task_id}@{task.version} is draft; activate it after dependencies resolve")
        resolution = _task_resolution_record(
            task_ref=task_ref_id,
            task=task,
            reason=reason,
            task_ledger_head=task_ledger_head,
            parent_run_ref=parent_run_ref_id,
            requester_task_id=requester_task_id,
            requester_task_version=requester_task_version,
            declared_alias=declared_alias,
            launch_surface=launch_surface,
            metadata=metadata,
        )
        if parent_run_ref_id is not None:
            self._publish_resolution_record(parent_run_ref_id, resolution)
        return resolution

    def start(
        self,
        task_ref: TaskRefInput,
        *,
        args: Mapping[str, Any] | None = None,
        may: str | None = None,
        parent: Any = None,
        launch_surface: LaunchSurfaceValue = "python",
        reason: str | None = None,
        placement: WorkspaceRunPlacement = "auto",
    ) -> RunRecord:
        """Compatibility start surface routed through the retained nucleus spine."""
        if os.environ.get(_FENCED_RUN_START_ENV) != "1":
            raise RunStartError(_FENCED_RUN_START_MESSAGE)
        return self.start_retained_workspace_run(
            task_ref,
            args=args,
            may=may,
            parent=parent,
            launch_surface=launch_surface,
            reason=reason,
            placement=placement,
        )

    def _start_authority_workspace_run(
        self,
        task_ref: TaskRefInput,
        *,
        args: Mapping[str, Any] | None = None,
        may: str | None = None,
        parent: Any = None,
        launch_surface: LaunchSurfaceValue = "python",
        reason: str | None = None,
    ) -> RunRecord:
        """Start a task through direct filesystem authority terminalization.

        This is a private authority-lane evidence hook, not the public
        handle-in launch surface. Public callers should use
        ``ShepherdWorkspace.run(..., repo=...)``.
        """
        task_ref_id = coerce_task_ref(task_ref)
        task_payload, task_ledger_head = _selected_task_ledger_payload_with_head(self.mg)
        task = _get_task_from_payload(task_payload, task_ref_id)
        if task is None:
            raise TaskNotFoundError(f"no active task matches {task_ref_id!r}")
        if task.status == "draft":
            raise RunStartError(f"task {task.task_id}@{task.version} is draft; activate it after dependencies resolve")
        if task.artifact_ref is None:
            raise RunStartError(f"task {task.task_id}@{task.version} has no artifact_ref")
        try:
            resolved_graph = _resolve_task_graph_from_payload(self.mg, task_payload, task)
        except TaskRegistrationError as exc:
            raise RunStartError(f"task dependency resolution failed: {exc}") from exc
        resolved = task.resolved()
        root_resolution = _task_resolution_record(
            task_ref=task_ref_id,
            task=task,
            reason=reason or _default_resolution_reason(launch_surface),
            task_ledger_head=task_ledger_head,
            parent_run_ref=None,
            launch_surface=launch_surface,
        )
        parent_scope = self.mg.ground if parent is None else parent
        try:
            authority_decision = resolve_workspace_authority_decision(
                task_default=resolved.may_default,
                requested=may,
                gitrepo_grant=_workspace_gitrepo_grant_from_signature(resolved.signature_schema),
            )
        except MayProfileError as exc:
            raise RunStartError(str(exc)) from exc
        authority_context = run_authority_context_for_decision(authority_decision)
        may_profile = authority_context.effective_may

        self._workspace.trace_store_path.parent.mkdir(parents=True, exist_ok=True)
        run_ref = f"run-{uuid.uuid4().hex[:12]}"
        started_at = _utc_now()
        run_args = dict(args or {})
        args_payload = build_run_args_payload(
            run_ref=run_ref,
            args=run_args,
            created_at=started_at,
        )
        args_digest = str(args_payload["args_digest"])
        validate_run_artifact_input_refs(self._workspace, args_payload["payload"])
        trace_ref = _workspace_control_trace_ref(run_ref)
        authority_shepherd_context = _workspace_authority_shepherd_context(
            run_ref=run_ref,
            root_resolution=root_resolution,
            may_profile=may_profile,
        )
        filesystem_authority_context = _workspace_filesystem_launch_authority_context(
            authority_decision,
            shepherd_context=authority_shepherd_context,
        )
        launch_context = RunLaunchContext(
            launch_surface=launch_surface,
            may_profile=may_profile,
            handler_env_ref=None,
            settlement_policy={
                "kind": "skeleton.filesystem_authority_terminalization",
                "binding_roots": dict(WORKSPACE_FILESYSTEM_AUTHORITY_BINDING_ROOTS),
                "authority_context": filesystem_authority_context,
            },
        )
        running = RunRecord(
            run_ref=run_ref,
            task_id=resolved.task_id,
            task_version=resolved.version,
            task_schema_digest=resolved.schema_digest,
            task_source_identity=resolved.source_identity,
            args_digest=args_digest,
            args_ref=str(args_payload["args_ref"]),
            may_profile=may_profile,
            authority_context=authority_context,
            provider="shepherd.workspace_control.nucleus-authority.v0",
            status="running",
            terminalization=RunTerminalization(
                body_status="running",
                world_disposition="none",
                output_publication_status="not_applicable",
            ),
            trace_ref=trace_ref,
            operation_refs=RunOperationRefs(),
            input_workspace_world_oid=self.mg.world_oid(parent_scope),
            started_at=started_at,
            parent_run_ref=launch_context.parent_run_ref,
            launch_context=launch_context,
            handler_env_ref=launch_context.handler_env_ref,
            resolved_task_graph=resolved_graph,
            task_resolutions=(root_resolution,),
        )
        running = replace(
            running,
            operation_refs=replace(
                running.operation_refs,
                run_start_revision=self._publish_record(running, args_payload=args_payload),
            ),
        )
        try:
            authority_execution, completed_resolutions, task_executions = self._execute_nucleus_authority_run(
                run_ref=run_ref,
                args=run_args,
                authority_decision=authority_decision,
                parent_scope=parent_scope,
                root_resolution=root_resolution,
                resolved_graph=resolved_graph,
            )
        except _NucleusAuthorityRunExecutionError as exc:
            recovered = self._recover_authority_runtime_failure(
                running=running,
                run_ref=run_ref,
                parent_scope=parent_scope,
                resolved=resolved,
                task_resolutions=exc.task_resolutions,
                task_executions=exc.task_executions,
            )
            if recovered is not None:
                published = self._publish_terminal_record(recovered)
                if recovered.error is not None:
                    raise RunStartError(f"run {run_ref} {recovered.error['message']}") from exc.cause
                return published
            cause = exc.cause
            failed = replace(
                running,
                status="failed",
                finished_at=_utc_now(),
                error=_exception_error_evidence(cause),
                task_resolutions=exc.task_resolutions,
                task_executions=exc.task_executions,
                terminalization=RunTerminalization(
                    body_status="failed",
                    world_disposition="discarded",
                    output_publication_status="not_applicable",
                ),
            )
            self._publish_terminal_record(failed)
            raise RunStartError(f"run {run_ref} failed: {type(cause).__name__}: {cause}") from cause
        except Exception as exc:
            failed = replace(
                running,
                status="failed",
                finished_at=_utc_now(),
                error=_exception_error_evidence(exc),
                terminalization=RunTerminalization(
                    body_status="failed",
                    world_disposition="discarded",
                    output_publication_status="not_applicable",
                ),
            )
            self._publish_terminal_record(failed)
            raise RunStartError(f"run {run_ref} failed: {type(exc).__name__}: {exc}") from exc

        authority_result = authority_execution.authority_result
        authority_allowed = authority_result.outcome == "allowed" and authority_result.settlement == "merged"
        authority_error = None if authority_allowed else _authority_terminalization_error(authority_result)
        terminal_without_trace = replace(
            running,
            status="merged" if authority_allowed else "failed",
            terminal_workspace_world_oid=authority_result.parent_world_after if authority_allowed else None,
            outputs={},
            task_resolutions=completed_resolutions,
            task_executions=task_executions,
            error=authority_error,
            finished_at=_utc_now(),
            terminalization=RunTerminalization(
                body_status="completed",
                world_disposition="merged" if authority_allowed else "discarded",
                output_publication_status="not_applicable",
            ),
        )
        trace_head = self._append_run_trace(
            run_ref=run_ref,
            trace_ref=trace_ref,
            resolved=resolved,
            status=run_trace_terminal_status(terminal_without_trace),
        )
        terminal = replace(
            terminal_without_trace,
            operation_refs=replace(
                running.operation_refs,
                runtime_operation=_runtime_operation_id_for_driver_result(authority_execution.driver_result),
                authority_operation=authority_result.authority_operation_id,
                authority_settlement_operation=authority_result.settlement_operation_id,
                trace_head=trace_head,
            ),
        )
        published = self._publish_terminal_record(terminal)
        if authority_error is not None:
            raise RunStartError(f"run {run_ref} {_authority_terminalization_message(authority_result)}")
        return published

    def _recover_authority_runtime_failure(
        self,
        *,
        running: RunRecord,
        run_ref: str,
        parent_scope: Any,
        resolved: ResolvedTask,
        task_resolutions: tuple[TaskResolutionRecord,...],
        task_executions: tuple[TaskExecutionRecord,...],
    ) -> RunRecord | None:
        pending = _pending_filesystem_authority_settlement_for_run(self.mg, run_ref)
        if pending is None:
            return None
        settlement_operation_id = _required_pending_authority_field(pending, "settlement_operation_id")
        self.mg.recover_authority_settlements()
        settlement_record = _authority_settlement_for_operation(
            self.mg,
            parent_scope=parent_scope,
            settlement_operation_id=settlement_operation_id,
        )
        authority_allowed = (
            settlement_record.get("outcome") == "allowed" and settlement_record.get("settlement") == "merged"
        )
        authority_error = (
            None if authority_allowed else _authority_terminalization_error_from_pending(settlement_record)
        )
        terminal_without_trace = replace(
            running,
            status="merged" if authority_allowed else "failed",
            terminal_workspace_world_oid=(
                _required_pending_authority_field(settlement_record, "parent_world_after")
                if authority_allowed
                else None
            ),
            outputs={},
            task_resolutions=task_resolutions,
            task_executions=task_executions,
            error=authority_error,
            finished_at=_utc_now(),
            terminalization=RunTerminalization(
                body_status="completed",
                world_disposition="merged" if authority_allowed else "discarded",
                output_publication_status="not_applicable",
            ),
        )
        trace_head = self._append_run_trace(
            run_ref=run_ref,
            trace_ref=running.trace_ref,
            resolved=resolved,
            status=run_trace_terminal_status(terminal_without_trace),
        )
        return replace(
            terminal_without_trace,
            operation_refs=replace(
                running.operation_refs,
                runtime_operation=_pending_runtime_operation_id(settlement_record),
                authority_operation=_required_pending_authority_field(settlement_record, "authority_operation_id"),
                authority_settlement_operation=settlement_operation_id,
                trace_head=trace_head,
            ),
        )

    def start_retained_workspace_run(
        self,
        task_ref: TaskRefInput,
        *,
        args: Mapping[str, Any] | None = None,
        may: str | None = None,
        runtime: Mapping[str, object] | RuntimeOptions | None = None,
        parent: Any = None,
        launch_surface: LaunchSurfaceValue = "python",
        reason: str | None = None,
        placement: WorkspaceRunPlacement = "auto",
    ) -> RunRecord:
        """Start a registered task through the nucleus/vcs-core retained-output path."""
        return self._start_retained_workspace_run(
            task_ref,
            args=args,
            may=may,
            runtime=runtime,
            parent=parent,
            launch_surface=launch_surface,
            reason=reason,
            placement=placement,
            flow_context=None,
        )

    def _start_retained_workspace_run(
        self,
        task_ref: TaskRefInput,
        *,
        args: Mapping[str, Any] | None = None,
        may: str | None = None,
        runtime: Mapping[str, object] | RuntimeOptions | None = None,
        parent: Any = None,
        launch_surface: LaunchSurfaceValue = "python",
        reason: str | None = None,
        placement: WorkspaceRunPlacement = "auto",
        flow_context: FlowRunContext | None = None,
    ) -> RunRecord:
        """Start a retained workspace run with optional internal flow metadata."""
        task_ref_id = coerce_task_ref(task_ref)
        task_payload, task_ledger_head = _selected_task_ledger_payload_with_head(self.mg)
        task = _get_task_from_payload(task_payload, task_ref_id)
        if task is None:
            raise TaskNotFoundError(f"no active task matches {task_ref_id!r}")
        if task.status == "draft":
            raise RunStartError(f"task {task.task_id}@{task.version} is draft; activate it after dependencies resolve")
        if task.artifact_ref is None:
            raise RunStartError(f"task {task.task_id}@{task.version} has no artifact_ref")
        try:
            resolved_graph = _resolve_task_graph_from_payload(self.mg, task_payload, task)
        except TaskRegistrationError as exc:
            raise RunStartError(f"task dependency resolution failed: {exc}") from exc
        resolved = task.resolved()
        root_resolution = _task_resolution_record(
            task_ref=task_ref_id,
            task=task,
            reason=reason or _default_resolution_reason(launch_surface),
            task_ledger_head=task_ledger_head,
            parent_run_ref=None,
            launch_surface=launch_surface,
        )
        parent_scope = self.mg.ground if parent is None else parent
        try:
            authority_decision = resolve_workspace_authority_decision(
                task_default=resolved.may_default,
                requested=may,
                gitrepo_grant=_workspace_gitrepo_grant_from_signature(resolved.signature_schema),
            )
        except MayProfileError as exc:
            raise RunStartError(str(exc)) from exc
        authority_context = run_authority_context_for_decision(authority_decision)
        may_profile = authority_context.effective_may
        runtime_plan = _workspace_run_runtime_plan(runtime)
        placement_decision = _workspace_run_placement_decision(
            self._workspace,
            placement,
            authority_decision=authority_decision,
        )
        _validate_workspace_runtime_plan_for_placement(runtime_plan, placement_decision)

        self._workspace.trace_store_path.parent.mkdir(parents=True, exist_ok=True)
        run_ref = f"run-{uuid.uuid4().hex[:12]}"
        started_at = _utc_now()
        run_args = dict(args or {})
        args_payload = build_run_args_payload(
            run_ref=run_ref,
            args=run_args,
            created_at=started_at,
        )
        args_digest = str(args_payload["args_digest"])
        validate_run_artifact_input_refs(self._workspace, args_payload["payload"])
        flow_run_payload = (
            None
            if flow_context is None
            else flow_context.to_record(
                run_ref=run_ref,
                created_at=started_at,
            )
        )
        trace_ref = _workspace_control_trace_ref(run_ref)
        authority_shepherd_context = _workspace_authority_shepherd_context(
            run_ref=run_ref,
            root_resolution=root_resolution,
            may_profile=may_profile,
        )
        retained_execution = _retained_execution_plan_for_decision(
            authority_decision,
            placement_decision=placement_decision,
            runtime_plan=runtime_plan,
        )
        placement_decision = replace(
            placement_decision,
            execution_descriptor=_run_execution_descriptor_for_plan(retained_execution),
        )
        retained_authority_provider = retained_output_authority_provider_for_context(
            authority_context,
            shepherd_context=authority_shepherd_context,
        )
        retained_authority_context = retained_authority_provider.authority_context
        if retained_authority_context is None:
            raise RunStartError("retained workspace run authority context projection failed")
        settlement_policy: JsonObject = {
            "kind": "skeleton.retained_output_selection",
            "authority_context": retained_authority_context,
            "execution_enforcement": retained_execution.to_descriptor(),
        }
        if (runtime_policy:= runtime_plan.policy_payload()) is not None:
            settlement_policy["runtime"] = runtime_policy
        launch_context = RunLaunchContext(
            launch_surface=launch_surface,
            may_profile=may_profile,
            handler_env_ref=None,
            settlement_policy=settlement_policy,
        )
        running = RunRecord(
            run_ref=run_ref,
            task_id=resolved.task_id,
            task_version=resolved.version,
            task_schema_digest=resolved.schema_digest,
            task_source_identity=resolved.source_identity,
            args_digest=args_digest,
            args_ref=str(args_payload["args_ref"]),
            may_profile=may_profile,
            authority_context=authority_context,
            provider="shepherd.workspace_control.nucleus.v0",
            enforcement="advisory",
            execution_evidence=placement_decision.evidence(),
            status="running",
            terminalization=RunTerminalization(
                body_status="running",
                world_disposition="none",
                output_publication_status="not_applicable",
            ),
            trace_ref=trace_ref,
            operation_refs=RunOperationRefs(),
            input_workspace_world_oid=self.mg.world_oid(parent_scope),
            started_at=started_at,
            parent_run_ref=launch_context.parent_run_ref,
            launch_context=launch_context,
            handler_env_ref=launch_context.handler_env_ref,
            resolved_task_graph=resolved_graph,
            task_resolutions=(root_resolution,),
        )
        with _seal_and_select_enabled():
            running = replace(
                running,
                operation_refs=replace(
                    running.operation_refs,
                    run_start_revision=self._publish_record(
                        running,
                        args_payload=args_payload,
                        flow_run_payload=flow_run_payload,
                    ),
                ),
            )
            try:
                sealed_execution, completed_resolutions, task_executions = self._execute_nucleus_retained_run(
                    run_ref=run_ref,
                    args=run_args,
                    authority_decision=authority_decision,
                    execution_plan=retained_execution,
                    parent_scope=parent_scope,
                    root_resolution=root_resolution,
                    resolved_graph=resolved_graph,
                    placement_decision=placement_decision,
                    runtime_plan=runtime_plan,
                )
            except _NucleusRetainedRunExecutionError as exc:
                self._publish_failed_retained_workspace_run(
                    running,
                    exc.cause,
                    task_resolutions=exc.task_resolutions,
                    task_executions=exc.task_executions,
                )
            except Exception as exc: # noqa: BLE001 - terminalize arbitrary task/runtime failures.
                self._publish_failed_retained_workspace_run(running, exc)

            return self._publish_successful_retained_workspace_run(
                running,
                sealed_execution=sealed_execution,
                completed_resolutions=completed_resolutions,
                task_executions=task_executions,
                trace_ref=trace_ref,
                resolved=resolved,
            )

    def _publish_failed_retained_workspace_run(
        self,
        running: RunRecord,
        cause: BaseException,
        *,
        task_resolutions: tuple[TaskResolutionRecord,...] | None = None,
        task_executions: tuple[TaskExecutionRecord,...] | None = None,
    ) -> NoReturn:
        failed = replace(
            running,
            status="failed",
            launch_context=_terminal_launch_context_with_execution_evidence(
                running.launch_context,
                cause=cause,
            ),
            finished_at=_utc_now(),
            error=_exception_error_evidence(cause),
            enforcement=_run_enforcement_for_task_executions(
                () if task_executions is None else task_executions,
                fallback=running.enforcement,
            ),
            execution_evidence=_run_execution_evidence_for_task_executions(
                running.execution_evidence,
                () if task_executions is None else task_executions,
            ),
            task_resolutions=running.task_resolutions if task_resolutions is None else task_resolutions,
            task_executions=running.task_executions if task_executions is None else task_executions,
            terminalization=RunTerminalization(
                body_status="failed",
                world_disposition="discarded",
                output_publication_status="not_applicable",
            ),
        )
        self._publish_terminal_record(failed)
        raise RunStartError(f"run {running.run_ref} failed: {type(cause).__name__}: {cause}") from cause

    def _publish_successful_retained_workspace_run(
        self,
        running: RunRecord,
        *,
        sealed_execution: Any,
        completed_resolutions: tuple[TaskResolutionRecord,...],
        task_executions: tuple[TaskExecutionRecord,...],
        trace_ref: TraceRef,
        resolved: ResolvedTask,
    ) -> RunRecord:
        retained_custody = RunRetainedCustody.from_seal_handoff(sealed_execution.handoff)
        output_citations: dict[str, RunOutputCitationRef] = {}
        publication_error: JsonObject | None = None
        try:
            output_citations = _nucleus_output_citations_for_sealed_execution(
                self._workspace,
                trace_ref=trace_ref,
                sealed_execution=sealed_execution,
            )
        except Exception as exc: # noqa: BLE001 - custody exists; record diagnosable terminal state.
            publication_error = _output_publication_error(exc, sealed_execution=sealed_execution)

        terminal_without_trace = replace(
            running,
            status="retained",
            launch_context=_terminal_launch_context_with_execution_evidence(running.launch_context),
            trace_ref=trace_ref,
            terminal_workspace_world_oid=sealed_execution.handoff.output_world_oid,
            outputs=output_citations,
            enforcement=_run_enforcement_for_task_executions(task_executions, fallback=running.enforcement),
            execution_evidence=_run_execution_evidence_for_task_executions(
                running.execution_evidence,
                task_executions,
            ),
            task_resolutions=completed_resolutions,
            task_executions=task_executions,
            finished_at=_utc_now(),
            terminalization=RunTerminalization(
                body_status="completed",
                world_disposition="retained",
                output_publication_status="published" if output_citations else "failed",
                retained_custody=retained_custody,
                publication_error=publication_error,
            ),
        )
        trace_head = self._append_run_trace(
            run_ref=running.run_ref,
            trace_ref=trace_ref,
            resolved=resolved,
            status=run_trace_terminal_status(terminal_without_trace),
        )
        runtime_operation = _runtime_operation_id_for_sealed_execution(sealed_execution)
        terminal = replace(
            terminal_without_trace,
            operation_refs=replace(
                running.operation_refs,
                runtime_operation=runtime_operation,
                trace_head=trace_head,
            ),
        )
        published = self._publish_terminal_record(terminal)
        if publication_error is not None:
            raise RunStartError(
                f"run {running.run_ref} retained output publication failed: "
                f"{publication_error['type']}: {publication_error['message']}"
            )
        return published

    def _execute_nucleus_retained_run(
        self,
        *,
        run_ref: str,
        args: Mapping[str, Any],
        authority_decision: WorkspaceAuthorityDecision,
        execution_plan: RetainedExecutionPlan,
        parent_scope: Any,
        root_resolution: TaskResolutionRecord,
        resolved_graph: ResolvedTaskGraph,
        placement_decision: _WorkspaceRunPlacementDecision,
        runtime_plan: WorkspaceRunRuntimePlan,
    ) -> tuple[Any, tuple[TaskResolutionRecord,...], tuple[TaskExecutionRecord,...]]:
        """Execute a workspace-control task through vcs-core's retained runtime command."""
        from vcs_core.runtime_api import CommandExecutionOptions
        from vcs_core.types import SealedExecutionOutcome

        task_execution_metadata = placement_decision.task_execution_metadata()
        execution_provider = self._retained_execution_provider(
            authority_decision=authority_decision,
            execution_plan=execution_plan,
            root_resolution=root_resolution,
            resolved_graph=resolved_graph,
            args=args,
            placement_decision=placement_decision,
            task_execution_metadata=task_execution_metadata,
            runtime_plan=runtime_plan,
        )
        executor_descriptor = (
            RuntimeProviderTaskExecutorDescriptor(execution_plan.executor_kind)
            if runtime_plan.uses_execution_provider
            else None
        )
        recorded_value, task_resolutions, task_executions = self._execute_nucleus_runtime_run(
            run_ref=run_ref,
            args=args,
            authority_decision=authority_decision,
            parent_scope=parent_scope,
            root_resolution=root_resolution,
            resolved_graph=resolved_graph,
            execution_options=CommandExecutionOptions(success_disposition="seal"),
            execution_provider=execution_provider,
            executor_descriptor=executor_descriptor,
            task_execution_metadata=task_execution_metadata,
            error_cls=_NucleusRetainedRunExecutionError,
        )
        if not isinstance(recorded_value, SealedExecutionOutcome):
            raise RunStartError("nucleus retained workspace run did not return a sealed execution outcome")
        return recorded_value, task_resolutions, task_executions

    def _retained_execution_provider(
        self,
        *,
        authority_decision: WorkspaceAuthorityDecision,
        execution_plan: RetainedExecutionPlan,
        root_resolution: TaskResolutionRecord,
        resolved_graph: ResolvedTaskGraph,
        args: Mapping[str, Any],
        placement_decision: _WorkspaceRunPlacementDecision,
        task_execution_metadata: dict[str, object],
        runtime_plan: WorkspaceRunRuntimePlan,
    ) -> Any | None:
        """Return the execution-bound provider for retained runs that require syscall enforcement."""
        if runtime_plan.provider_kind == "static":
            if resolved_graph.dependencies:
                raise RunStartError("static runtime workspace runs do not yet support linked task dependencies")
            task_execution_metadata["launch_confined_attempted"] = False
            task_execution_metadata["runtime_provider"] = "static"
            if runtime_plan.model_name is not None:
                task_execution_metadata["runtime_model"] = runtime_plan.model_name
            return StaticWorkspaceRuntimeProvider(
                task_lock=root_resolution.task_lock,
                kwargs=dict(args),
                model_name=runtime_plan.model_name,
                enforce_with_launch_confined=placement_decision.resolved == "jail",
                launch_metadata=task_execution_metadata,
            )
        if runtime_plan.provider_kind == "claude":
            if resolved_graph.dependencies:
                raise RunStartError("Claude runtime workspace runs do not yet support linked task dependencies")
            if placement_decision.resolved != "jail":
                raise RunStartError("Claude runtime workspace runs require native jail placement")
            artifact_payload = _read_task_artifact(self.mg, root_resolution.task_lock.artifact_ref)
            task_execution_metadata["launch_confined_attempted"] = False
            task_execution_metadata["runtime_provider"] = "claude"
            task_execution_metadata["runtime_provider_transport"] = "claude-headless"
            task_execution_metadata["network_credential_posture"] = "advisory"
            if runtime_plan.model_name is not None:
                task_execution_metadata["runtime_model"] = runtime_plan.model_name
            return ClaudeWorkspaceRuntimeProvider(
                task_lock=root_resolution.task_lock,
                artifact_payload=artifact_payload,
                kwargs=dict(args),
                model_name=runtime_plan.model_name,
                input_artifacts=_workspace_runtime_input_artifacts(self._workspace, args),
                launch_metadata=task_execution_metadata,
            )
        if execution_plan.mode != "confined_process" or placement_decision.resolved != "jail":
            return None
        if resolved_graph.dependencies:
            raise RunStartError("confined retained workspace runs do not yet support linked task dependencies")
        artifact_payload = _read_task_artifact(self.mg, root_resolution.task_lock.artifact_ref)
        task_execution_metadata["launch_confined_attempted"] = False
        return ConfinedRootTaskProvider(
            artifact_payload=artifact_payload,
            kwargs=dict(args),
            repo_authority=authority_decision.repo_authority,
            launch_metadata=task_execution_metadata,
        )

    def _execute_nucleus_authority_run(
        self,
        *,
        run_ref: str,
        args: Mapping[str, Any],
        authority_decision: WorkspaceAuthorityDecision,
        parent_scope: Any,
        root_resolution: TaskResolutionRecord,
        resolved_graph: ResolvedTaskGraph,
    ) -> tuple[Any, tuple[TaskResolutionRecord,...], tuple[TaskExecutionRecord,...]]:
        """Execute a workspace-control task through authority terminalization."""
        from vcs_core.runtime_api import AuthorityExecutionOutcome

        from shepherd_dialect.workspace_control._filesystem_authority import (
            filesystem_authority_execution_options_for_clamp,
        )

        execution_options = filesystem_authority_execution_options_for_clamp(
            grant_clamp=workspace_filesystem_authority_grant_clamp(authority_decision),
            binding_roots=WORKSPACE_FILESYSTEM_AUTHORITY_BINDING_ROOTS,
            shepherd_context={
                "run_ref": run_ref,
                "task_id": root_resolution.task_lock.task_id,
                "task_version": root_resolution.task_lock.version,
                "may_profile": authority_decision.may_profile_name,
                "launch_surface": root_resolution.launch_surface,
            },
        )
        recorded_value, task_resolutions, task_executions = self._execute_nucleus_runtime_run(
            run_ref=run_ref,
            args=args,
            authority_decision=authority_decision,
            parent_scope=parent_scope,
            root_resolution=root_resolution,
            resolved_graph=resolved_graph,
            execution_options=execution_options,
            error_cls=_NucleusAuthorityRunExecutionError,
        )
        if not isinstance(recorded_value, AuthorityExecutionOutcome):
            raise RunStartError("nucleus authority workspace run did not return an authority execution outcome")
        return recorded_value, task_resolutions, task_executions

    def _execute_nucleus_runtime_run(
        self,
        *,
        run_ref: str,
        args: Mapping[str, Any],
        authority_decision: WorkspaceAuthorityDecision,
        parent_scope: Any,
        root_resolution: TaskResolutionRecord,
        resolved_graph: ResolvedTaskGraph,
        execution_options: Any,
        error_cls: type[_NucleusRunExecutionError],
        execution_provider: Any | None = None,
        executor_descriptor: TaskExecutor | None = None,
        task_execution_metadata: Mapping[str, object] | None = None,
    ) -> tuple[Any, tuple[TaskResolutionRecord,...], tuple[TaskExecutionRecord,...]]:
        """Execute a workspace-control task through vcs-core's runtime command."""
        if execution_provider is not None:
            return self._execute_nucleus_confined_root_runtime_run(
                run_ref=run_ref,
                args=args,
                authority_decision=authority_decision,
                parent_scope=parent_scope,
                root_resolution=root_resolution,
                execution_options=execution_options,
                execution_provider=execution_provider,
                executor_descriptor=executor_descriptor,
                task_execution_metadata=task_execution_metadata,
                error_cls=error_cls,
            )
        task_executions: list[TaskExecutionRecord] = []
        runtime_ref: TaskRuntimeContext | None = None

        def task_body(_stack: Any, *, working_path: str) -> object:
            nonlocal runtime_ref
            repo = _WorkspaceControlCarrierGitRepo(
                root=Path(working_path),
                authority=authority_decision.repo_authority,
            )
            runtime = TaskRuntimeContext(
                workspace=self._workspace,
                run_ref=run_ref,
                graph=resolved_graph,
                repo=repo,
                root_resolution=root_resolution,
                task_execution_metadata=task_execution_metadata,
            )
            runtime_ref = runtime
            token = _CURRENT_TASK_RUNTIME.set(runtime)
            request = TaskExecutionRequest(
                run_ref=run_ref,
                task_lock=root_resolution.task_lock,
                repo=repo,
                kwargs=dict(args),
                call_kind="root_run",
                resolution_id=root_resolution.resolution_id,
                metadata=dict(task_execution_metadata or {}),
            )
            started = _started_task_execution_record(self._workspace.task_executor, request)
            try:
                result = self._workspace.task_executor.execute(self._workspace, request)
            except Exception as exc:
                failed_execution = _failed_task_execution_record(started, exc)
                task_executions.append(failed_execution)
                raise
            else:
                task_executions.append(_completed_task_execution_record(started))
                return _portable_runtime_result(result)
            finally:
                _CURRENT_TASK_RUNTIME.reset(token)

        task_body.__module__ = root_resolution.task_lock.task_id.rpartition(".")[0] or task_body.__module__
        task_body.__qualname__ = root_resolution.task_lock.task_id
        task_body.__name__ = root_resolution.task_lock.task_id.rsplit(".", 1)[-1]
        try:
            recorded = self.mg.execute_recorded(
                "runtime",
                "run",
                scope=parent_scope,
                task_body=task_body,
                may=_runtime_may_for_workspace_authority(authority_decision),
                execution_options=execution_options,
            )
        except Exception as exc:
            runtime = runtime_ref
            task_runtime_executions: tuple[TaskExecutionRecord,...] = (
                () if runtime is None else runtime.task_executions
            )
            raise error_cls(
                exc,
                task_resolutions=(root_resolution,) if runtime is None else runtime.task_resolutions,
                task_executions=(*task_runtime_executions, *task_executions),
            ) from exc
        runtime = runtime_ref
        if runtime is None:
            raise RunStartError("nucleus workspace run did not enter task runtime")
        return recorded.value, runtime.task_resolutions, (*runtime.task_executions, *task_executions)

    def _execute_nucleus_confined_root_runtime_run(
        self,
        *,
        run_ref: str,
        args: Mapping[str, Any],
        authority_decision: WorkspaceAuthorityDecision,
        parent_scope: Any,
        root_resolution: TaskResolutionRecord,
        execution_options: Any,
        execution_provider: Any,
        executor_descriptor: TaskExecutor | None,
        task_execution_metadata: Mapping[str, object] | None,
        error_cls: type[_NucleusRunExecutionError],
    ) -> tuple[Any, tuple[TaskResolutionRecord,...], tuple[TaskExecutionRecord,...]]:
        """Execute a root workspace task via an execution-bound confined provider."""
        executor = executor_descriptor or ConfinedProcessTaskExecutorDescriptor()
        execution_metadata = (
            task_execution_metadata
            if isinstance(task_execution_metadata, dict)
            else dict(task_execution_metadata or {})
        )
        request = TaskExecutionRequest(
            run_ref=run_ref,
            task_lock=root_resolution.task_lock,
            repo={"binding": "workspace", "authority": authority_decision.repo_authority},
            kwargs=dict(args),
            call_kind="root_run",
            resolution_id=root_resolution.resolution_id,
            metadata=execution_metadata,
        )
        started = _started_task_execution_record(executor, request)

        def task_body(_stack: Any) -> object:
            return None

        task_body.__module__ = root_resolution.task_lock.task_id.rpartition(".")[0] or task_body.__module__
        task_body.__qualname__ = root_resolution.task_lock.task_id
        task_body.__name__ = root_resolution.task_lock.task_id.rsplit(".", 1)[-1]
        try:
            recorded = self.mg.execute_recorded(
                "runtime",
                "run",
                scope=parent_scope,
                task_body=task_body,
                may=_runtime_may_for_workspace_authority(authority_decision),
                provider=execution_provider,
                execution_options=execution_options,
            )
        except Exception as exc:
            failed_started = replace(started, metadata=dict(execution_metadata))
            raise error_cls(
                exc,
                task_resolutions=(root_resolution,),
                task_executions=(_failed_task_execution_record(failed_started, exc),),
            ) from exc
        completed_started = replace(started, metadata=dict(execution_metadata))
        return recorded.value, (root_resolution,), (_completed_task_execution_record(completed_started),)

    def _append_run_trace(
        self,
        *,
        run_ref: str,
        trace_ref: TraceRef,
        resolved: ResolvedTask,
        status: str,
    ) -> str | None:
        payload: JsonObject = {
            "trace_runtime": "shepherd.workspace_control.trace.v1",
            "trace_owner_id": f"task:{resolved.task_id}@{resolved.version}:{run_ref}",
            "frontier_id": trace_ref.frontier_id,
            "run_ref": run_ref,
            "identity_domain": "vcscore.canonical.v2",
            "events": [
                {
                    "id": f"{run_ref}:terminal",
                    "kind": "run.lifecycle",
                    "transition": status,
                    "terminal_status": status,
                    "task_id": resolved.task_id,
                    "task_version": resolved.version,
                    "artifact_digest": resolved.artifact_digest,
                }
            ],
            "causal_edges": [],
            "owner_paths": {f"task:{resolved.task_id}@{resolved.version}:{run_ref}": [f"{run_ref}:terminal"]},
        }
        outcome = self.mg.exec("trace", "append", scope=self.mg.ground, payload=payload)
        return outcome.oids[0] if outcome.oids else None

    def _publish_record(
        self,
        record: RunRecord,
        *,
        args_payload: Mapping[str, object] | None = None,
        flow_run_payload: Mapping[str, object] | None = None,
    ) -> str:
        return publish_run_record(
            self.mg,
            record,
            args_payload=args_payload,
            flow_run_payload=flow_run_payload,
        )

    def _publish_resolution_record(self, run_ref: str, resolution: TaskResolutionRecord) -> str:
        try:
            return append_resolution(self.mg, run_ref, resolution)
        except RunLedgerPublishError as exc:
            raise RunStartError(str(exc)) from exc

    def _publish_terminal_record(self, record: RunRecord) -> RunRecord:
        return publish_terminal_run_record(self.mg, record)

    def _merge_current_run_state(self, record: RunRecord) -> RunRecord:
        from shepherd_dialect.workspace_control.run_ledger import merge_current_run_state

        return merge_current_run_state(self.mg, record)


@dataclass(frozen=True)
class _TaskSource:
    import_path: str
    module_name: str
    qualname: str
    file_path: Path | None
    source_text: str
    callable: Callable[..., Any] | None
    signature_schema: JsonObject | None = None
    provenance_kind: str = "imported_source"


@dataclass(frozen=True)
class _TaskLibraryMutation:
    kind: TaskLibraryMutationKind
    task_id: str
    source: _TaskSource
    may_default: str
    declared_dependencies: Mapping[str, DeclaredTaskDependency]
    metadata: JsonObject
    base_version: str | None
    produced_by_run: str | None
    derived_from: tuple[str,...]
    source_identity: str | None


@dataclass(frozen=True)
class _SourceIdentity:
    world_oid: str
    path: str


def _validate_run_produced_source_identity(
    mg: Any,
    produced_by_run: str,
    source_identity: str | None,
) -> None:
    if source_identity is None:
        raise TaskRegistrationError("tasks.update requires source_identity for task definitions produced by a run")
    identity = _parse_source_identity(source_identity)
    record = get_run(mg, produced_by_run)
    if record is None:
        raise TaskRegistrationError(f"source_identity cites missing produced_by_run {produced_by_run!r}")
    if not run_has_published_workspace_output(record):
        raise TaskRegistrationError(
            f"source_identity produced_by_run {produced_by_run!r} has no published workspace output"
        )
    if not run_can_produce_source_identity(record, identity.world_oid):
        raise TaskRegistrationError("source_identity world does not match the produced_by_run workspace output world")


def _parse_source_identity(value: str) -> _SourceIdentity:
    if not isinstance(value, str) or not value:
        raise TaskRegistrationError("source_identity must be a non-empty string")
    prefix = "world:"
    marker = ":path:"
    if not value.startswith(prefix) or marker not in value:
        raise TaskRegistrationError("source_identity must be shaped as world:<world_oid>:path:<relative_path>")
    world_oid, path = value[len(prefix):].split(marker, 1)
    if not world_oid:
        raise TaskRegistrationError("source_identity world oid must be non-empty")
    _validate_source_identity_path(path)
    return _SourceIdentity(world_oid=world_oid, path=path)


def _validate_source_identity_path(path: str) -> None:
    if not path:
        raise TaskRegistrationError("source_identity path must be non-empty")
    parsed = PurePosixPath(path)
    if path in {".", ".."} or parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise TaskRegistrationError("source_identity path must be a relative workspace path")


def _resolve_task_source(source: str | Callable[..., Any]) -> _TaskSource:
    if isinstance(source, str):
        task_body = resolve_task_id(source)
        import_path = _canonical_import_path(source)
        return _task_source_from_callable(import_path, task_body)
    if callable(source):
        import_path = _import_path_for_callable(source)
        task_body = resolve_task_id(import_path)
        if task_body is not source:
            raise TaskRegistrationError(f"callable {source!r} does not resolve stably from {import_path!r}")
        return _task_source_from_callable(import_path, source)
    raise TaskRegistrationError("task source must be an import path string or callable")


def _task_source_from_callable(import_path: str, task_body: Callable[..., Any]) -> _TaskSource:
    module_name, _, qualname = import_path.partition(":")
    if not module_name or not qualname:
        raise TaskRegistrationError(f"task source {import_path!r} is not a canonical import path")
    source_file = inspect.getsourcefile(task_body)
    if source_file is None:
        raise TaskRegistrationError(f"task source {import_path!r} has no readable source file")
    file_path = Path(source_file)
    try:
        source_text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TaskRegistrationError(f"task source file {source_file!r} is not readable") from exc
    task_source = _TaskSource(
        import_path=import_path,
        module_name=module_name,
        qualname=qualname,
        file_path=file_path,
        source_text=source_text,
        callable=task_body,
        provenance_kind="imported_source",
    )
    _validate_single_file_capture_imports(task_source)
    return task_source


def _task_source_from_source_text(
    *,
    module_name: str,
    qualname: str,
    source_text: str,
) -> _TaskSource:
    _require_non_empty_task_source_field(module_name, "module")
    _require_non_empty_task_source_field(qualname, "entrypoint")
    _require_non_empty_task_source_field(source_text, "source_text")
    try:
        tree = ast.parse(source_text, filename=f"<shepherd-generated:{module_name}>")
    except SyntaxError as exc:
        raise TaskRegistrationError(f"generated task source {module_name!r} is not valid Python") from exc
    signature_schema = _signature_schema_from_ast(tree, module_name=module_name, qualname=qualname)
    task_source = _TaskSource(
        import_path=f"{module_name}:{qualname}",
        module_name=module_name,
        qualname=qualname,
        file_path=None,
        source_text=source_text,
        callable=None,
        signature_schema=signature_schema,
        provenance_kind="generated_source",
    )
    _validate_single_file_capture_imports(task_source)
    return task_source


def _require_non_empty_task_source_field(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise TaskRegistrationError(f"generated task {field_name} must be a non-empty string")


def _canonical_import_path(source: str) -> str:
    module_name, sep, attr_name = source.partition(":")
    if sep:
        return f"{module_name}:{attr_name}"
    module_name, dot, attr_name = source.rpartition(".")
    if not dot:
        raise TaskRegistrationError(
            f"task source {source!r} is not a fully-qualified import path ('pkg.module:attr' or 'pkg.module.attr')"
        )
    return f"{module_name}:{attr_name}"


def _validate_single_file_capture_imports(source: _TaskSource) -> None:
    try:
        tree = ast.parse(source.source_text, filename=_source_filename(source))
    except SyntaxError as exc:
        raise TaskRegistrationError(f"task source {source.import_path!r} is not valid Python") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _reject_local_import(source, alias.name, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise TaskRegistrationError(
                    f"task source {source.import_path!r} uses relative import at line {node.lineno}; "
                    "register an explicit task bundle instead"
                )
            if node.module is not None:
                _reject_local_import(source, node.module, node.lineno)


def _reject_local_import(source: _TaskSource, module_name: str, line: int) -> None:
    top_level = module_name.split(".", 1)[0]
    current_top_level = source.module_name.split(".", 1)[0]
    if top_level == current_top_level:
        raise TaskRegistrationError(
            f"task source {source.import_path!r} imports same-package module {module_name!r} at line {line}; "
            "register an explicit task bundle instead"
        )
    if source.file_path is None:
        return
    source_dir = source.file_path.parent
    if (source_dir / f"{top_level}.py").exists() or (source_dir / top_level / "__init__.py").exists():
        raise TaskRegistrationError(
            f"task source {source.import_path!r} imports local module {top_level!r} at line {line}; "
            "register an explicit task bundle instead"
        )


def _source_filename(source: _TaskSource) -> str:
    if source.file_path is None:
        return f"<shepherd-generated:{source.module_name}>"
    return str(source.file_path)


def _import_path_for_callable(task_body: Callable[..., Any]) -> str:
    module_name = getattr(task_body, "__module__", "")
    qualname = getattr(task_body, "__qualname__", getattr(task_body, "__name__", ""))
    if not module_name or not qualname or "<locals>" in qualname:
        raise TaskRegistrationError(f"callable {task_body!r} does not have a stable import path")
    return f"{module_name}:{qualname}"


def _default_task_id(import_path: str) -> str:
    return import_path.replace(":", ".")


def _resolve_task_may_default(explicit: str | None, source: _TaskSource) -> str:
    raw_may = explicit if explicit is not None else _task_source_may_default(source)
    try:
        return canonical_may_profile_name(raw_may)
    except MayProfileError as exc:
        raise TaskRegistrationError(str(exc)) from exc


def _task_source_may_default(source: _TaskSource) -> str:
    if source.callable is None:
        return DEFAULT_WORKSPACE_MAY_PROFILE
    return _callable_may_default(source.callable)


def _callable_may_default(task_body: Callable[..., Any]) -> str:
    may = getattr(task_body, "may_default", None)
    if may is None:
        may = getattr(task_body, "__shepherd_may_default__", None)
    return may if isinstance(may, str) and may else DEFAULT_WORKSPACE_MAY_PROFILE


@dataclass(frozen=True)
class _WorkspaceControlCarrierGitRepo:
    """Compatibility repo facade over the runtime command's carrier path."""

    root: Path
    authority: str
    binding: str = "workspace"

    def write(self, path: str, content: bytes, *, mode: int = 0o100644) -> _WorkspaceControlCarrierGitRepo:
        _validate_workspace_relative_path(path, field_name="workspace repo write path")
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        if self.binding != "workspace":
            raise WorkspaceControlError("workspace-control carrier GitRepo only supports workspace binding")
        if self.authority != "readwrite":
            raise PermissionError(f"GitRepoHandle.write is not permitted under authority={self.authority!r}")
        if not isinstance(mode, int):
            raise TypeError("mode must be an int")
        target = self.root / PurePosixPath(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(mode)
        return self


def _validate_workspace_relative_path(path: str, *, field_name: str) -> None:
    if not isinstance(path, str):
        raise WorkspaceControlError(f"{field_name} must be a relative POSIX path")
    parsed = PurePosixPath(path)
    if path in {"", ".", ".."} or parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise WorkspaceControlError(f"{field_name} must be a relative POSIX path")


def _portable_runtime_result(value: object) -> object:
    if isinstance(value, _WorkspaceControlCarrierGitRepo):
        return {
            "kind": "shepherd.workspace_control.carrier_git_repo_result.v1",
            "binding": value.binding,
            "authority": value.authority,
        }
    if isinstance(value, tuple):
        return tuple(_portable_runtime_result(item) for item in value)
    if isinstance(value, list):
        return [_portable_runtime_result(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _portable_runtime_result(item) for key, item in value.items()}
    return value


def _runtime_may_for_workspace_authority(decision: WorkspaceAuthorityDecision) -> str:
    return "ReadOnly" if decision.repo_authority == "readonly" else "Permissive"


def _workspace_run_placement_decision(
    workspace: ShepherdWorkspace,
    placement: WorkspaceRunPlacement,
    *,
    authority_decision: WorkspaceAuthorityDecision,
) -> _WorkspaceRunPlacementDecision:
    del workspace
    requested = _resolve_workspace_run_placement(placement)
    if requested == "advisory":
        if authority_decision.repo_authority == "readonly":
            raise RunStartError("placement='advisory' cannot satisfy effective ReadOnly GitRepo authority")
        return _WorkspaceRunPlacementDecision(
            requested=requested,
            resolved="advisory",
            execution_descriptor=_run_execution_descriptor(authority_decision, resolved="advisory"),
            initial_enforcement_basis="explicit_advisory",
        )
    if requested == "jail":
        return _WorkspaceRunPlacementDecision(
            requested=requested,
            resolved="jail",
            execution_descriptor=_run_execution_descriptor(authority_decision, resolved="jail"),
            initial_enforcement_basis="required_jail",
        )
    if authority_decision.repo_authority == "readonly" or native_jail_available():
        return _WorkspaceRunPlacementDecision(
            requested=requested,
            resolved="jail",
            execution_descriptor=_run_execution_descriptor(authority_decision, resolved="jail"),
            initial_enforcement_basis="auto_jail",
        )
    return _WorkspaceRunPlacementDecision(
        requested=requested,
        resolved="advisory",
        execution_descriptor=_run_execution_descriptor(authority_decision, resolved="advisory"),
        initial_enforcement_basis="auto_advisory",
    )


def _resolve_workspace_run_placement(placement: str) -> WorkspaceRunPlacement:
    if placement in {"auto", "advisory", "jail"}:
        return placement # type: ignore[return-value]
    raise RunStartError("workspace run placement must be one of: 'auto', 'advisory', 'jail'")


def _run_execution_descriptor(
    decision: WorkspaceAuthorityDecision,
    *,
    resolved: Literal["advisory", "jail"],
) -> JsonObject:
    if resolved == "jail":
        return {
            "mode": "confined_process",
            "enforcement": "syscall_jail",
            "profile": _runtime_may_for_workspace_authority(decision),
            "provider": "workspace-control-confined-task",
        }
    return {
        "mode": "in_process",
        "enforcement": "advisory",
        "profile": _runtime_may_for_workspace_authority(decision),
        "provider": "in-process",
    }


def _run_execution_descriptor_for_plan(plan: RetainedExecutionPlan) -> JsonObject:
    return {
        "mode": plan.mode,
        "enforcement": "syscall_jail" if plan.mode == "confined_process" else "advisory",
        "profile": plan.profile,
        "provider": plan.provider,
    }


def _retained_execution_plan_for_decision(
    decision: WorkspaceAuthorityDecision,
    *,
    placement_decision: _WorkspaceRunPlacementDecision,
    runtime_plan: WorkspaceRunRuntimePlan | None = None,
) -> RetainedExecutionPlan:
    provider = runtime_plan.provider_id if runtime_plan is not None and runtime_plan.provider_id is not None else None
    if placement_decision.resolved == "jail":
        return RetainedExecutionPlan(
            mode="confined_process",
            provider=provider or "workspace-control-confined-task",
            executor_kind="confined_process",
            profile=_runtime_may_for_workspace_authority(decision),
            authority_basis=(
                "effective_gitrepo_readonly" if decision.repo_authority == "readonly" else "workspace_run_placement"
            ),
            requested_monitor="syscall_jail",
            monitor_required=True,
        )
    return RetainedExecutionPlan(
        mode="in_process",
        provider=provider or "in-process",
        executor_kind="in_process",
        profile=_runtime_may_for_workspace_authority(decision),
        authority_basis="runtime_provider" if provider is not None else "effective_gitrepo_readwrite",
        requested_monitor=None,
        monitor_required=False,
    )


def _workspace_run_runtime_plan(value: Mapping[str, object] | RuntimeOptions | None) -> WorkspaceRunRuntimePlan:
    try:
        return resolve_workspace_run_runtime_plan(value)
    except WorkspaceRuntimePlanError as exc:
        raise RunStartError(str(exc)) from exc


def _validate_workspace_runtime_plan_for_placement(
    runtime_plan: WorkspaceRunRuntimePlan,
    placement_decision: _WorkspaceRunPlacementDecision,
) -> None:
    if runtime_plan.provider_kind != "claude":
        return
    if placement_decision.requested == "advisory":
        raise RunStartError("runtime provider 'claude' requires placement='auto' or placement='jail'")
    if not native_jail_available():
        raise RunStartError("runtime provider 'claude' requires native jail support")
    if placement_decision.resolved != "jail":
        raise RunStartError("runtime provider 'claude' requires native jail placement")


def _workspace_runtime_input_artifacts(
    workspace: ShepherdWorkspace,
    args: Mapping[str, object],
) -> tuple[WorkspaceRuntimeInputArtifact,...]:
    return tuple(
        _workspace_runtime_input_artifact(workspace, ref, index=index)
        for index, ref in enumerate(iter_run_artifact_input_refs(args), start=1)
    )


def _workspace_runtime_input_artifact(
    workspace: ShepherdWorkspace,
    ref: RunArtifactInputRef,
    *,
    index: int,
) -> WorkspaceRuntimeInputArtifact:
    matches = [
        output
        for output in workspace.runs.outputs(run_ref=ref.run_ref, binding=ref.binding)
        if output.output_id == ref.output_id
    ]
    if not matches:
        raise WorkspaceControlError(f"run artifact input ref cannot resolve output {ref.output_id!r}")
    output = matches[0]
    data = output.read_file(ref.path)
    if data is None:
        raise WorkspaceControlError(f"run artifact input ref path is not present: {ref.path!r}")
    label = _runtime_input_label(ref)
    materialized_path = f"{CLAUDE_WORKSPACE_INPUT_DIR}/{index:02d}-{label}/{PurePosixPath(ref.path).as_posix()}"
    return WorkspaceRuntimeInputArtifact(
        source_run_ref=ref.run_ref,
        source_output_id=ref.output_id,
        source_output_name=ref.output_name,
        source_binding=ref.binding,
        source_path=ref.path,
        materialized_path=materialized_path,
        content=data[0],
        label=ref.label,
        content_digest=ref.content_digest,
    )


def _runtime_input_label(ref: RunArtifactInputRef) -> str:
    raw = ref.label or ref.output_id
    label = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in raw.lower()).strip("-")
    return label[:48] or "artifact"


def _terminal_launch_context_with_execution_evidence(
    launch_context: RunLaunchContext,
    *,
    cause: BaseException | None = None,
) -> RunLaunchContext:
    policy = launch_context.settlement_policy
    if policy is None:
        return launch_context
    raw = policy.get("execution_enforcement")
    if not isinstance(raw, Mapping) or raw.get("mode") != "confined_process":
        return launch_context
    updated = dict(raw)
    for key in ("monitor_refusal", "prelaunch_refusal", "body_refusal"):
        updated[key] = None
    if cause is None:
        updated["established_monitor"] = raw.get("requested_monitor")
    elif (confined_failure:= _confined_task_execution_failure(cause)) is not None:
        updated["established_monitor"] = raw.get("requested_monitor") if confined_failure.monitor_established else None
        updated[_confined_task_failure_evidence_key(confined_failure)] = confined_failure.evidence()
    elif _is_monitor_refusal(cause):
        updated["established_monitor"] = None
        updated["monitor_refusal"] = {"type": _monitor_refusal_type(cause), "message": str(cause)}
    else:
        updated["established_monitor"] = None
        updated["prelaunch_refusal"] = {"type": type(cause).__name__, "message": str(cause)}
    return replace(launch_context, settlement_policy={**policy, "execution_enforcement": updated})


def _confined_task_execution_failure(cause: BaseException) -> ConfinedTaskExecutionError | None:
    current: BaseException | None = cause
    while current is not None:
        if isinstance(current, ConfinedTaskExecutionError):
            return current
        current = current.__cause__ if current.__cause__ is not current else None
    return None


def _confined_task_failure_evidence_key(failure: ConfinedTaskExecutionError) -> str:
    return {
        "prelaunch_refused": "prelaunch_refusal",
        "monitor_refused": "monitor_refusal",
        "body_refused": "body_refusal",
    }[failure.phase]


def _is_monitor_refusal(cause: BaseException) -> bool:
    message = str(cause)
    return type(cause).__name__ == "JailNotEstablished" or "no jail-capable" in message


def _monitor_refusal_type(cause: BaseException) -> str:
    cause_type = type(cause).__name__
    if cause_type == "JailNotEstablished" or "no jail-capable" not in str(cause):
        return cause_type
    return "JailNotEstablished"


def _workspace_authority_shepherd_context(
    *,
    run_ref: str,
    root_resolution: TaskResolutionRecord,
    may_profile: str,
) -> JsonObject:
    return {
        "run_ref": run_ref,
        "task_id": root_resolution.task_lock.task_id,
        "task_version": root_resolution.task_lock.version,
        "may_profile": may_profile,
        "launch_surface": root_resolution.launch_surface,
    }


def _workspace_authority_shepherd_context_for_record(record: RunRecord) -> JsonObject:
    return {
        "run_ref": record.run_ref,
        "task_id": record.task_id,
        "task_version": record.task_version,
        "may_profile": record.may_profile,
        "launch_surface": record.launch_context.launch_surface,
    }


def _workspace_filesystem_launch_authority_context(
    decision: WorkspaceAuthorityDecision,
    *,
    shepherd_context: Mapping[str, object],
) -> JsonObject:
    from shepherd_dialect.workspace_control._filesystem_authority import filesystem_authority_merge_provider_for_clamp

    provider = filesystem_authority_merge_provider_for_clamp(
        grant_clamp=workspace_filesystem_authority_grant_clamp(decision),
        binding_roots=WORKSPACE_FILESYSTEM_AUTHORITY_BINDING_ROOTS,
        shepherd_context=shepherd_context,
    )
    return dict(provider.authority_context)


def _nucleus_output_citations_for_sealed_execution(
    workspace: ShepherdWorkspace,
    *,
    trace_ref: TraceRef,
    sealed_execution: Any,
) -> dict[str, RunOutputCitationRef]:
    from shepherd_dialect.workspace_control.output_publication import publish_run_output_descriptor
    from shepherd_dialect.workspace_control.outputs import run_output_publication_from_seal_handoff

    draft = run_output_publication_from_seal_handoff(
        sealed_execution.handoff,
        parent=sealed_execution.seal_result.parent,
        trace_ref=trace_ref,
    )
    return {draft.output_name: publish_run_output_descriptor(workspace.trace_store_path, draft)}


def _output_publication_error(exc: BaseException, *, sealed_execution: Any) -> JsonObject:
    handoff = sealed_execution.handoff
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "stage": "output_publication",
        "phase": "run_output_descriptor",
        "retained_custody_ref": handoff.handoff_ref,
        "retained_output_world_oid": handoff.output_world_oid,
    }


def _authority_terminalization_error(authority_result: Any) -> JsonObject:
    return {
        "type": _authority_terminalization_error_type(authority_result),
        "message": _authority_terminalization_message(authority_result),
        "stage": "authority_terminalization",
        "authority_operation_id": authority_result.authority_operation_id,
        "authority_settlement_operation_id": authority_result.settlement_operation_id,
        "cohort_id": authority_result.cohort_id,
        "candidate_digest": authority_result.candidate_digest,
        "outcome": authority_result.outcome,
        "settlement": authority_result.settlement,
    }


def _authority_terminalization_error_from_pending(pending: Any) -> JsonObject:
    outcome = _required_pending_authority_field(pending, "outcome")
    settlement = _required_pending_authority_field(pending, "settlement")
    return {
        "type": _authority_terminalization_error_type_for(outcome, settlement),
        "message": _authority_terminalization_message_for(
            outcome,
            settlement,
            pending.get("reason_code") if isinstance(pending, Mapping) else None,
        ),
        "stage": "authority_terminalization",
        "authority_operation_id": _required_pending_authority_field(pending, "authority_operation_id"),
        "authority_settlement_operation_id": _required_pending_authority_field(pending, "settlement_operation_id"),
        "cohort_id": _required_pending_authority_field(pending, "cohort_id"),
        "candidate_digest": _required_pending_authority_field(pending, "candidate_digest"),
        "outcome": outcome,
        "settlement": settlement,
    }


def _authority_terminalization_error_type(authority_result: Any) -> str:
    return _authority_terminalization_error_type_for(authority_result.outcome, authority_result.settlement)


def _authority_terminalization_error_type_for(outcome: str, settlement: str) -> str:
    del settlement
    if outcome == "denied":
        return "AuthorityDenied"
    if outcome == "refused":
        return "AuthorityRefused"
    return "AuthoritySettlementMismatch"


def _authority_terminalization_message(authority_result: Any) -> str:
    reason_code = None
    decisions = getattr(authority_result, "decisions", ())
    if decisions:
        reason_code = getattr(decisions[-1], "reason_code", None)
    return _authority_terminalization_message_for(authority_result.outcome, authority_result.settlement, reason_code)


def _authority_terminalization_message_for(outcome: str, settlement: str, reason_code: object) -> str:
    reason = f": {reason_code}" if isinstance(reason_code, str) and reason_code else ""
    return f"authority {outcome} ({settlement}){reason}"


def _pending_filesystem_authority_settlement_for_run(mg: Any, run_ref: str) -> Any | None:
    for pending in mg.authority_settlement_pending_records():
        if pending.get("transaction_kind", "filesystem_merge") != "filesystem_merge":
            continue
        authority_context = pending.get("authority_context")
        if not isinstance(authority_context, Mapping):
            continue
        shepherd = authority_context.get("shepherd")
        if isinstance(shepherd, Mapping) and shepherd.get("run_ref") == run_ref:
            return pending
    return None


def _authority_settlement_for_operation(
    mg: Any,
    *,
    parent_scope: Any,
    settlement_operation_id: str,
) -> dict[str, object]:
    try:
        history = mg.resolve_operation_history(settlement_operation_id, scope=parent_scope)
    except Exception as exc:
        raise RunStartError(
            f"could not read recovered authority settlement {settlement_operation_id!r}: {exc}"
        ) from exc
    for commit in history.commits:
        metadata = getattr(commit, "metadata", None)
        if isinstance(metadata, Mapping) and metadata.get("type") == "AuthoritySettlement":
            return {**metadata, "settlement_operation_id": settlement_operation_id}
    raise RunStartError(f"recovered authority settlement {settlement_operation_id!r} has no settlement effect")


def _required_pending_authority_field(pending: Any, field_name: str) -> str:
    if not isinstance(pending, Mapping):
        raise RunStartError("authority pending settlement record must be an object")
    value = pending.get(field_name)
    if not isinstance(value, str) or not value:
        raise RunStartError(f"authority pending settlement record missing {field_name!r}")
    return value


def _pending_runtime_operation_id(pending: Any) -> str | None:
    if not isinstance(pending, Mapping):
        return None
    authority_context = pending.get("authority_context")
    if not isinstance(authority_context, Mapping):
        return None
    value = authority_context.get("runtime_operation_id")
    if isinstance(value, str) and value:
        return value
    runtime = authority_context.get("runtime")
    if isinstance(runtime, Mapping):
        value = runtime.get("operation_id")
        if isinstance(value, str) and value:
            return value
    return None


def _runtime_operation_id_for_sealed_execution(sealed_execution: Any) -> str | None:
    return _runtime_operation_id_for_driver_result(getattr(sealed_execution, "driver_result", None))


def _runtime_operation_id_for_driver_result(driver_result: Any) -> str | None:
    transitions = getattr(driver_result, "transitions", ())
    try:
        first_transition = transitions[0]
    except (IndexError, TypeError):
        return None
    payload = getattr(first_transition, "payload", None)
    if not isinstance(payload, Mapping):
        return None
    portable_core = payload.get("portable_core")
    if not isinstance(portable_core, Mapping):
        return None
    operation_id = portable_core.get("operation_id")
    return operation_id if isinstance(operation_id, str) and operation_id else None


def _retained_output_selection_authority_provider(mg: Any, output: Any) -> Callable[[Any], AuthorityDecision]:
    owner = getattr(output, "owner", None)
    if getattr(owner, "kind", None) != "run" or getattr(owner, "run_id", None) is None:
        raise WorkspaceControlError("run-output selection authority requires a run-owned output")
    record = get_run(mg, owner.run_id)
    if record is None:
        raise WorkspaceControlError(f"run-output selection authority cannot resolve run {owner.run_id!r}")
    if record.authority_context is None:
        raise WorkspaceControlError("run-output selection authority requires a recorded run authority context")
    try:
        return retained_output_authority_provider_for_context(
            record.authority_context,
            shepherd_context=_workspace_authority_shepherd_context_for_record(record),
        )
    except (TypeError, ValueError, MayProfileError) as exc:
        raise WorkspaceControlError(str(exc)) from exc


def _task_source_signature_schema(source: _TaskSource) -> JsonObject:
    if source.signature_schema is not None:
        return dict(source.signature_schema)
    if source.callable is None:
        raise TaskRegistrationError(f"task source {source.import_path!r} has no signature metadata")
    return _signature_schema(source.callable)


def _signature_schema_from_ast(tree: ast.Module, *, module_name: str, qualname: str) -> JsonObject:
    parts = qualname.split(".")
    if not parts or any(not part for part in parts):
        raise TaskRegistrationError(f"generated task entrypoint {module_name}:{qualname} is not valid")
    if len(parts) != 1:
        raise TaskRegistrationError(
            f"generated task entrypoint {module_name}:{qualname} must be a top-level function or class"
        )
    entrypoint = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and node.name == parts[0]
        ),
        None,
    )
    if entrypoint is None:
        raise TaskRegistrationError(f"generated task source {module_name}:{qualname} has no matching entrypoint")
    if isinstance(entrypoint, ast.ClassDef):
        init_node = next(
            (node for node in entrypoint.body if isinstance(node, ast.FunctionDef) and node.name == "__init__"),
            None,
        )
        if init_node is None:
            return {"parameters": [], "return": entrypoint.name}
        return _signature_schema_from_ast_arguments(
            init_node.args,
            returns=ast.Name(id=entrypoint.name),
            drop_first=True,
        )
    return _signature_schema_from_ast_arguments(entrypoint.args, returns=entrypoint.returns, drop_first=False)


def _signature_schema_from_ast_arguments(
    args: ast.arguments,
    *,
    returns: ast.expr | None,
    drop_first: bool,
) -> JsonObject:
    positional = [*args.posonlyargs, *args.args]
    if drop_first and positional:
        positional = positional[1:]
    default_offset = len([*args.posonlyargs, *args.args]) - len(args.defaults)
    default_by_name = {
        arg.arg: default
        for arg, default in zip([*args.posonlyargs, *args.args][default_offset:], args.defaults, strict=False)
    }
    parameters: list[JsonObject] = []
    posonly_count = len(args.posonlyargs)
    for index, arg in enumerate(positional):
        default = default_by_name.get(arg.arg)
        parameters.append(
            _ast_parameter_schema(
                arg,
                kind="POSITIONAL_ONLY" if index < posonly_count else "POSITIONAL_OR_KEYWORD",
                default=default,
            )
        )
    if args.vararg is not None:
        parameters.append(_ast_parameter_schema(args.vararg, kind="variadic positional", default=None))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        parameters.append(_ast_parameter_schema(arg, kind="KEYWORD_ONLY", default=default))
    if args.kwarg is not None:
        parameters.append(_ast_parameter_schema(args.kwarg, kind="variadic keyword", default=None))
    return {
        "parameters": parameters,
        "return": None if returns is None else ast.unparse(returns),
    }


def _ast_parameter_schema(arg: ast.arg, *, kind: str, default: ast.expr | None) -> JsonObject:
    parameter_schema: JsonObject = {
        "name": arg.arg,
        "kind": kind,
        "required": default is None,
        "annotation": None if arg.annotation is None else ast.unparse(arg.annotation),
        "default": None if default is None else ast.unparse(default),
    }
    try:
        gitrepo_grant = compile_gitrepo_grant_from_ast_annotation(arg.annotation, parameter_name=arg.arg)
    except AuthorityDeclarationError as exc:
        raise TaskRegistrationError(str(exc)) from exc
    if gitrepo_grant is not None:
        parameter_schema["gitrepo_grant"] = gitrepo_grant.to_descriptor()
    return parameter_schema


def _signature_schema(task_body: Callable[..., Any]) -> JsonObject:
    signature = inspect.signature(task_body)
    try:
        hints = get_type_hints(task_body, include_extras=True)
    except Exception as exc:
        unresolved_authority = [
            name
            for name, parameter in signature.parameters.items()
            if raw_annotation_looks_like_authority(parameter.annotation)
        ]
        if raw_annotation_looks_like_authority(signature.return_annotation):
            unresolved_authority.append("return")
        if unresolved_authority:
            names = ", ".join(unresolved_authority)
            task_label = getattr(task_body, "__qualname__", repr(task_body))
            raise TaskRegistrationError(f"could not resolve authority annotations for {task_label}: {names}") from exc
        hints = {}
    parameters: list[JsonObject] = []
    for name, parameter in signature.parameters.items():
        if parameter.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
            kind = parameter.kind.description
        else:
            kind = parameter.kind.name
        parameter_schema: JsonObject = {
            "name": name,
            "kind": kind,
            "required": parameter.default is inspect.Signature.empty,
            "annotation": _annotation_repr(parameter.annotation),
            "default": None if parameter.default is inspect.Signature.empty else repr(parameter.default),
        }
        annotation = hints.get(name, parameter.annotation)
        gitrepo_grant = _gitrepo_grant_from_annotation(annotation, parameter_name=name)
        if gitrepo_grant is not None:
            parameter_schema["gitrepo_grant"] = gitrepo_grant.to_descriptor()
        parameters.append(parameter_schema)
    return {
        "parameters": parameters,
        "return": _annotation_repr(signature.return_annotation),
    }


def _annotation_repr(annotation: object) -> str | None:
    if annotation is inspect.Signature.empty:
        return None
    return getattr(annotation, "__qualname__", None) or getattr(annotation, "__name__", None) or repr(annotation)


def _gitrepo_grant_from_annotation(annotation: object, *, parameter_name: str) -> Any | None:
    try:
        descriptor = compile_gitrepo_grant_from_annotation(annotation, parameter_name=parameter_name)
    except AuthorityDeclarationError as exc:
        raise TaskRegistrationError(str(exc)) from exc
    return descriptor


def _workspace_gitrepo_grant_from_signature(signature_schema: Mapping[str, object]) -> Any | None:
    from shepherd_dialect.workspace_control.authority import GitRepoGrantDescriptor

    raw_parameters = signature_schema.get("parameters")
    if not isinstance(raw_parameters, list | tuple):
        return None
    grants = []
    for raw_parameter in raw_parameters:
        if not isinstance(raw_parameter, Mapping):
            continue
        raw_grant = raw_parameter.get("gitrepo_grant")
        if raw_grant is not None:
            grants.append(GitRepoGrantDescriptor.from_descriptor(raw_grant))
    if not grants:
        return None
    if len(grants) != 1:
        raise RunStartError("workspace-control GitRepo grant v0 supports exactly one repo grant")
    return grants[0]


def _task_schema_digest(
    *,
    import_path: str,
    signature_schema: Mapping[str, object],
    may_default: str,
    artifact_digest: str | None = None,
) -> str:
    return _canonical_digest(
        {
            "import_path": import_path,
            "signature_schema": dict(signature_schema),
            "may_default": may_default,
            "artifact_digest": artifact_digest,
        }
    )


def _selected_payload_with_head(mg: Any, binding: str) -> tuple[Mapping[str, object] | None, str | None]:
    reader = getattr(mg, "read_selected_binding_revision_with_head", None)
    if callable(reader):
        selected = reader(binding)
        if selected is None:
            return None, None
        return selected.payload, selected.head
    payload = mg.read_selected_binding_revision(binding)
    return payload, None


def _selected_task_ledger_payload_with_head(mg: Any) -> tuple[JsonObject, str | None]:
    payload, head = _selected_payload_with_head(mg, TASK_LEDGER_BINDING)
    return _task_ledger_payload(payload), head


def _get_task_from_payload(payload: Mapping[str, object], task_ref: str) -> TaskDefinitionVersion | None:
    task_id, requested_version = _split_task_ref(task_ref)
    versions = _task_versions_for_payload(payload, task_id)
    if requested_version is not None:
        return next((version for version in versions if version.version == requested_version), None)
    active = [version for version in versions if version.status == "active"]
    if len(active) > 1:
        raise ValueError(f"task {task_id!r} has multiple active versions")
    return active[0] if active else None


def _coerce_declared_dependencies(
    value: Mapping[str, DeclaredDependencyInput] | None,
) -> dict[str, DeclaredTaskDependency]:
    if value is None:
        return {}
    out: dict[str, DeclaredTaskDependency] = {}
    for alias, raw in value.items():
        if not isinstance(alias, str) or not alias:
            raise TaskRegistrationError("declared dependency aliases must be non-empty strings")
        if isinstance(raw, DeclaredTaskDependency):
            dependency = raw
        elif isinstance(raw, str):
            dependency = DeclaredTaskDependency(task_id=raw)
        elif isinstance(raw, Mapping):
            task_id = raw.get("task_id")
            selector = raw.get("selector", "active")
            if not isinstance(task_id, str) or not task_id:
                raise TaskRegistrationError(f"declared dependency {alias!r} requires a non-empty task_id")
            if not isinstance(selector, str) or not selector:
                raise TaskRegistrationError(f"declared dependency {alias!r} requires a non-empty selector")
            dependency = DeclaredTaskDependency(task_id=task_id, selector=selector)
        else:
            raise TaskRegistrationError("declared dependency values must be strings, objects, or dependencies")
        out[alias] = dependency
    return out


def _task_artifact_payload(
    *,
    source: _TaskSource,
    declared_dependencies: Mapping[str, DeclaredTaskDependency],
    source_identity: str | None,
    produced_by_run: str | None,
) -> JsonObject:
    artifact_path = _module_artifact_path(source)
    content_digest = _canonical_digest({"path": artifact_path, "content": source.source_text})
    return {
        "schema": TASK_ARTIFACT_SCHEMA,
        "format": "python.package.v1",
        "entrypoint": {
            "module": source.module_name,
            "qualname": source.qualname,
        },
        "files": [
            {
                "path": artifact_path,
                "content_encoding": "utf-8",
                "content": source.source_text,
                "content_digest": content_digest,
                "mode": "100644",
            }
        ],
        "declared_dependencies": {
            alias: dependency.to_json() for alias, dependency in sorted(declared_dependencies.items())
        },
        "requires_python": ">=3.11",
        "metadata": {},
        "provenance": {
            "kind": "workspace_source" if source_identity is not None else source.provenance_kind,
            "source_identity": source_identity,
            "produced_by_run": produced_by_run,
            "source_file": None if source.file_path is None else str(source.file_path),
        },
        "created_at": _utc_now(),
    }


def _module_artifact_path(source: _TaskSource) -> str:
    if source.file_path is not None and source.file_path.name == "__init__.py":
        path = f"{source.module_name.replace('.', '/')}/__init__.py"
    else:
        path = f"{source.module_name.replace('.', '/')}.py"
    _validate_artifact_relative_path(path)
    return path


def _artifact_digest_from_payload(payload: Mapping[str, object]) -> str:
    files = payload.get("files")
    if not isinstance(files, list | tuple) or not files:
        raise TaskRegistrationError("task artifact payload requires at least one file")
    digest_payload = {
        "schema": payload.get("schema"),
        "format": payload.get("format"),
        "entrypoint": payload.get("entrypoint"),
        "files": [
            {
                "path": _required_artifact_file_str(file, "path"),
                "content_encoding": _required_artifact_file_str(file, "content_encoding"),
                "content": _required_artifact_file_str(file, "content"),
                "content_digest": _required_artifact_file_str(file, "content_digest"),
                "mode": _required_artifact_file_str(file, "mode"),
            }
            for file in files
            if isinstance(file, Mapping)
        ],
        "declared_dependencies": payload.get("declared_dependencies", {}),
        "requires_python": payload.get("requires_python"),
        "metadata": payload.get("metadata", {}),
    }
    if len(digest_payload["files"]) != len(files): # type: ignore[arg-type]
        raise TaskRegistrationError("task artifact files must be objects")
    return _canonical_digest(digest_payload)


def _required_artifact_file_str(value: Mapping[str, object], field_name: str) -> str:
    raw = value.get(field_name)
    if not isinstance(raw, str) or not raw:
        raise TaskRegistrationError(f"task artifact file {field_name} must be a non-empty string")
    if field_name == "path":
        _validate_artifact_relative_path(raw)
    return raw


def _read_task_artifact(mg: Any, ref: TaskArtifactRef) -> Mapping[str, object]:
    if ref.binding != TASK_ARTIFACT_BINDING:
        raise RunStartError(f"unsupported task artifact binding {ref.binding!r}")
    reader = getattr(mg, "read_binding_revision", None)
    if not callable(reader):
        raise RunStartError("VcsCore.read_binding_revision is required for artifact-backed task execution")
    payload = reader(
        ref.binding,
        ref.head,
        store_id=ref.store_id,
        resource_id=ref.resource_id,
    )
    if payload.get("schema") != TASK_ARTIFACT_SCHEMA:
        raise RunStartError(f"task artifact expected schema {TASK_ARTIFACT_SCHEMA!r}, got {payload.get('schema')!r}")
    artifact_digest = _artifact_digest_from_payload(payload)
    if artifact_digest != ref.artifact_digest or payload.get("artifact_digest") != ref.artifact_digest:
        raise RunStartError("task artifact digest does not match artifact ref")
    return payload


def _task_artifact_description(payload: Mapping[str, object]) -> JsonObject:
    entrypoint = payload.get("entrypoint")
    entrypoint_payload = dict(entrypoint) if isinstance(entrypoint, Mapping) else {}
    module_name = entrypoint_payload.get("module")
    qualname = entrypoint_payload.get("qualname")
    source = _entrypoint_source_text(payload, module_name if isinstance(module_name, str) else None)
    return {
        "format": payload.get("format"),
        "entrypoint": entrypoint_payload,
        "files": [
            {
                "path": _required_artifact_file_str(file, "path"),
                "mode": _required_artifact_file_str(file, "mode"),
                "content_digest": _required_artifact_file_str(file, "content_digest"),
            }
            for file in _artifact_files(payload)
        ],
        "docstring": _entrypoint_docstring(source, qualname if isinstance(qualname, str) else None),
        "source_excerpt": _source_excerpt(source),
    }


def _entrypoint_source_text(payload: Mapping[str, object], module_name: str | None) -> str | None:
    if module_name is None:
        return None
    expected_path = f"{module_name.replace('.', '/')}.py"
    init_path = f"{module_name.replace('.', '/')}/__init__.py"
    for file in _artifact_files(payload):
        path = _required_artifact_file_str(file, "path")
        if path not in {expected_path, init_path}:
            continue
        if _required_artifact_file_str(file, "content_encoding") != "utf-8":
            return None
        return _required_artifact_file_str(file, "content")
    return None


def _entrypoint_docstring(source: str | None, qualname: str | None) -> str | None:
    if source is None or qualname is None:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    node: ast.AST | None = None
    body: list[ast.stmt] = list(tree.body)
    for part in qualname.split("."):
        node = next(
            (
                candidate
                for candidate in body
                if isinstance(candidate, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                and candidate.name == part
            ),
            None,
        )
        if node is None:
            return None
        body = list(getattr(node, "body", ()))
    return ast.get_docstring(node)


def _source_excerpt(source: str | None, *, max_lines: int = 40) -> str | None:
    if source is None:
        return None
    lines = source.strip().splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join([*lines[:max_lines], "..."])


def _declared_dependencies_from_artifact(
    mg: Any,
    task: TaskDefinitionVersion,
) -> Mapping[str, DeclaredTaskDependency]:
    artifact_ref = _required_task_artifact_ref(task)
    payload = _read_task_artifact(mg, artifact_ref)
    raw_dependencies = payload.get("declared_dependencies", {})
    if not isinstance(raw_dependencies, Mapping):
        raise TaskRegistrationError("task artifact declared_dependencies must be an object")
    dependencies: dict[str, DeclaredTaskDependency] = {}
    for alias, raw_dependency in raw_dependencies.items():
        if not isinstance(alias, str) or not alias:
            raise TaskRegistrationError("task artifact declared dependency aliases must be non-empty strings")
        if not isinstance(raw_dependency, Mapping):
            raise TaskRegistrationError(f"task artifact declared dependency {alias!r} must be an object")
        dependencies[alias] = DeclaredTaskDependency.from_json(raw_dependency)
    if _declared_dependency_payload(dependencies) != _declared_dependency_payload(task.declared_dependencies):
        raise TaskRegistrationError(
            f"task {task.task_id}@{task.version} dependency cache disagrees with artifact metadata"
        )
    return dependencies


def _declared_dependency_payload(
    dependencies: Mapping[str, DeclaredTaskDependency],
) -> dict[str, JsonObject]:
    return {alias: dependency.to_json() for alias, dependency in sorted(dependencies.items())}


@contextmanager
def _loaded_task_callable(mg: Any, ref: TaskArtifactRef) -> Any:
    payload = _read_task_artifact(mg, ref)
    entrypoint = payload.get("entrypoint")
    if not isinstance(entrypoint, Mapping):
        raise RunStartError("task artifact entrypoint must be an object")
    module_name = entrypoint.get("module")
    qualname = entrypoint.get("qualname")
    if not isinstance(module_name, str) or not module_name:
        raise RunStartError("task artifact entrypoint.module must be a non-empty string")
    if not isinstance(qualname, str) or not qualname:
        raise RunStartError("task artifact entrypoint.qualname must be a non-empty string")
    module_names = _module_chain(module_name)
    prior_modules = {name: sys.modules.get(name) for name in module_names}
    missing_modules = {name for name in module_names if name not in sys.modules}
    prior_sys_path = list(sys.path)
    source_parent = _artifact_source_parent(payload)
    with tempfile.TemporaryDirectory(prefix="shepherd-task-artifact-") as root:
        root_path = Path(root)
        for raw_file in _artifact_files(payload):
            path = _required_artifact_file_str(raw_file, "path")
            content = _required_artifact_file_str(raw_file, "content")
            if _required_artifact_file_str(raw_file, "content_encoding") != "utf-8":
                raise RunStartError("only utf-8 task artifact files are supported")
            destination = root_path / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
        sys.path[:] = [root, *[_path for _path in prior_sys_path if not _is_same_path(_path, source_parent)]]
        for name in module_names:
            sys.modules.pop(name, None)
        try:
            module = importlib.import_module(module_name)
            task_body = _resolve_qualname(module, qualname)
            if not callable(task_body):
                raise RunStartError(f"task artifact entrypoint {module_name}:{qualname} is not callable")
            yield task_body
        finally:
            sys.path[:] = prior_sys_path
            for name in module_names:
                sys.modules.pop(name, None)
            for name, module in prior_modules.items():
                if name not in missing_modules:
                    sys.modules[name] = module


def _artifact_files(payload: Mapping[str, object]) -> tuple[Mapping[str, object],...]:
    raw_files = payload.get("files")
    if not isinstance(raw_files, list | tuple):
        raise RunStartError("task artifact files must be a list")
    files: list[Mapping[str, object]] = []
    for raw_file in raw_files:
        if not isinstance(raw_file, Mapping):
            raise RunStartError("task artifact file entries must be objects")
        files.append(raw_file)
    return tuple(files)


def _artifact_source_parent(payload: Mapping[str, object]) -> Path | None:
    provenance = payload.get("provenance")
    if not isinstance(provenance, Mapping):
        return None
    source_file = provenance.get("source_file")
    if not isinstance(source_file, str) or not source_file:
        return None
    return Path(source_file).resolve().parent


def _is_same_path(path: str, other: Path | None) -> bool:
    if other is None or not path:
        return False
    try:
        return Path(path).resolve() == other
    except OSError:
        return False


def _module_chain(module_name: str) -> tuple[str,...]:
    parts = module_name.split(".")
    return tuple(".".join(parts[:index]) for index in range(1, len(parts) + 1))


def _resolve_qualname(module: Any, qualname: str) -> Any:
    value = module
    for part in qualname.split("."):
        if part == "<locals>":
            raise RunStartError("task artifact entrypoint cannot reference a local function")
        value = getattr(value, part)
    return value


def _validate_artifact_relative_path(path: str) -> None:
    parsed = PurePosixPath(path)
    if path in {"", ".", ".."} or parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise TaskRegistrationError("task artifact file paths must be relative POSIX paths")


def _task_dependencies_resolve(mg: Any, payload: Mapping[str, object], task: TaskDefinitionVersion) -> bool:
    try:
        _resolve_task_graph_from_payload(mg, payload, task)
    except TaskRegistrationError:
        return False
    return True


def _resolve_task_graph_from_payload(
    mg: Any,
    payload: Mapping[str, object],
    root: TaskDefinitionVersion,
) -> ResolvedTaskGraph:
    if root.artifact_ref is None or root.artifact_digest is None:
        raise TaskRegistrationError(f"task {root.task_id}@{root.version} has no artifact ref")
    dependencies: dict[str, TaskDependencyLock] = {}
    visiting: list[tuple[str, str]] = []

    def visit(task: TaskDefinitionVersion, alias_prefix: str | None = None) -> None:
        key = (task.task_id, task.version)
        if key in visiting:
            cycle = " -> ".join(f"{task_id}@{version}" for task_id, version in (*visiting, key))
            raise TaskRegistrationError(f"task dependency cycle detected: {cycle}")
        if task.status == "draft" and task is not root:
            raise TaskRegistrationError(f"task dependency {task.task_id}@{task.version} is draft")
        if task.artifact_ref is None or task.artifact_digest is None:
            raise TaskRegistrationError(f"task {task.task_id}@{task.version} has no artifact ref")
        visiting.append(key)
        try:
            for alias, dependency in _declared_dependencies_from_artifact(mg, task).items():
                child = _resolve_dependency_selector(payload, dependency)
                child_alias = alias if alias_prefix is None else f"{alias_prefix}.{alias}"
                dependencies[child_alias] = TaskDependencyLock(
                    alias=child_alias,
                    task_id=child.task_id,
                    selector=dependency.selector,
                    version=child.version,
                    artifact_ref=_required_task_artifact_ref(child),
                    artifact_digest=child.artifact_digest or "",
                    schema_digest=child.schema_digest,
                )
                visit(child, child_alias)
        finally:
            visiting.pop()

    visit(root)
    return ResolvedTaskGraph(
        root=TaskArtifactLock(
            task_id=root.task_id,
            version=root.version,
            artifact_ref=root.artifact_ref,
            artifact_digest=root.artifact_digest,
            schema_digest=root.schema_digest,
        ),
        dependencies=dependencies,
    )


def _task_resolution_record(
    *,
    task_ref: str,
    task: TaskDefinitionVersion,
    reason: str,
    task_ledger_head: str | None,
    parent_run_ref: str | None = None,
    requester_task_id: str | None = None,
    requester_task_version: str | None = None,
    declared_alias: str | None = None,
    launch_surface: LaunchSurfaceValue = "python",
    metadata: Mapping[str, object] | None = None,
) -> TaskResolutionRecord:
    if task_ledger_head is None:
        raise RunStartError("cannot resolve a task without a selected task-ledger head")
    return TaskResolutionRecord(
        resolution_id=f"task-resolution-{uuid.uuid4().hex[:12]}",
        reason=reason,
        requested_ref=task_ref,
        task_ledger_head=task_ledger_head,
        task_lock=_task_artifact_lock(task),
        parent_run_ref=parent_run_ref,
        requester_task_id=requester_task_id,
        requester_task_version=requester_task_version,
        declared_alias=declared_alias,
        launch_surface=launch_surface,
        resolved_at=_utc_now(),
        metadata=dict(metadata or {}),
    )


def _task_artifact_lock(task: TaskDefinitionVersion) -> TaskArtifactLock:
    if task.artifact_ref is None or task.artifact_digest is None:
        raise RunStartError(f"task {task.task_id}@{task.version} has no artifact ref")
    return TaskArtifactLock(
        task_id=task.task_id,
        version=task.version,
        artifact_ref=task.artifact_ref,
        artifact_digest=task.artifact_digest,
        schema_digest=task.schema_digest,
    )


def _default_resolution_reason(launch_surface: str) -> str:
    if launch_surface in {"cli", "sdk"}:
        return launch_surface
    return "run_start"


def _started_task_execution_record(
    executor: TaskExecutor,
    request: TaskExecutionRequest,
) -> TaskExecutionRecord:
    metadata: JsonObject = dict(request.metadata or {})
    if request.alias_path is not None:
        metadata["alias_path"] = request.alias_path
    return TaskExecutionRecord(
        execution_id=f"task-execution-{uuid.uuid4().hex[:12]}",
        run_ref=request.run_ref,
        executor_kind=executor.executor_kind,
        executor_id=executor.executor_id,
        executor_policy=executor.executor_policy,
        call_kind=request.call_kind,
        status="started",
        task_lock=request.task_lock,
        started_at=_utc_now(),
        resolution_id=request.resolution_id,
        metadata=metadata,
    )


def _completed_task_execution_record(record: TaskExecutionRecord) -> TaskExecutionRecord:
    return replace(record, status="completed", finished_at=_utc_now())


def _failed_task_execution_record(record: TaskExecutionRecord, exc: BaseException) -> TaskExecutionRecord:
    return replace(
        record,
        status="failed",
        finished_at=_utc_now(),
        error=_exception_error_evidence(exc),
    )


def _exception_error_evidence(exc: BaseException) -> JsonObject:
    if (confined_failure:= _confined_task_execution_failure(exc)) is not None:
        return confined_failure.evidence()
    return {"type": type(exc).__name__, "message": str(exc)}


def _run_enforcement_for_task_executions(
    executions: tuple[TaskExecutionRecord,...],
    *,
    fallback: RunEnforcement,
) -> RunEnforcement:
    if any(execution.metadata.get("launch_confined_attempted") is True for execution in executions):
        return "jail"
    return fallback


def _run_execution_evidence_for_task_executions(
    evidence: RunExecutionEvidence,
    executions: tuple[TaskExecutionRecord,...],
) -> RunExecutionEvidence:
    if any(execution.metadata.get("launch_confined_attempted") is True for execution in executions):
        return replace(evidence, enforcement_basis="launch_confined_attempted")
    if evidence.resolved_placement == "jail":
        return replace(evidence, enforcement_basis="prelaunch_advisory")
    return evidence


def _required_task_artifact_ref(task: TaskDefinitionVersion) -> TaskArtifactRef:
    if task.artifact_ref is None:
        raise TaskRegistrationError(f"task {task.task_id}@{task.version} has no artifact ref")
    return task.artifact_ref


def _resolve_dependency_selector(
    payload: Mapping[str, object],
    dependency: DeclaredTaskDependency,
) -> TaskDefinitionVersion:
    versions = _task_versions_for_payload(payload, dependency.task_id)
    if dependency.selector == "active":
        active = [version for version in versions if version.status == "active"]
        if len(active) > 1:
            raise TaskRegistrationError(f"task dependency {dependency.task_id!r} has multiple active versions")
        if not active:
            raise TaskRegistrationError(f"task dependency {dependency.task_id!r} has no active version")
        return active[0]
    if dependency.selector.startswith("v"):
        match = next((version for version in versions if version.version == dependency.selector), None)
        if match is None:
            raise TaskRegistrationError(
                f"task dependency {dependency.task_id!r} has no version {dependency.selector!r}"
            )
        if match.status == "draft":
            raise TaskRegistrationError(f"task dependency {dependency.task_id}@{dependency.selector} is draft")
        return match
    raise TaskRegistrationError(f"unsupported task dependency selector {dependency.selector!r}")


def _canonical_digest(value: object) -> str:
    return canonical_digest(value)


def _task_ledger_payload(payload: Mapping[str, object] | None) -> JsonObject:
    if payload is None:
        return {"schema": TASK_LEDGER_SCHEMA, "tasks": {}}
    if payload.get("schema") != TASK_LEDGER_SCHEMA:
        raise TaskRegistrationError(f"unsupported task ledger schema: {payload.get('schema')!r}")
    tasks = payload.get("tasks", {})
    if not isinstance(tasks, Mapping):
        raise TaskRegistrationError("task ledger payload field 'tasks' must be an object")
    return {"schema": TASK_LEDGER_SCHEMA, "tasks": {str(key): list(value) for key, value in tasks.items()}}


def _run_ledger_payload(payload: Mapping[str, object] | None) -> JsonObject:
    try:
        return run_ledger_payload(payload)
    except Exception as exc:
        raise RunStartError(str(exc)) from exc


def _task_versions_for_payload(payload: Mapping[str, object], task_id: str) -> tuple[TaskDefinitionVersion,...]:
    tasks = payload.get("tasks", {})
    if not isinstance(tasks, Mapping):
        raise TaskRegistrationError("task ledger payload field 'tasks' must be an object")
    raw_versions = tasks.get(task_id, ())
    if not isinstance(raw_versions, list | tuple):
        raise TaskRegistrationError(f"task ledger versions for {task_id!r} must be a list")
    return tuple(TaskDefinitionVersion.from_json(raw) for raw in raw_versions if isinstance(raw, Mapping))


def _split_task_ref(task_ref: str) -> tuple[str, str | None]:
    if not isinstance(task_ref, str) or not task_ref:
        raise ValueError("task_ref must be a non-empty string")
    if "@" not in task_ref:
        return task_ref, None
    task_id, version = task_ref.rsplit("@", 1)
    if not task_id or not version:
        raise ValueError("task_ref must be shaped as task_id@version")
    return task_id, version


def _next_version(existing_versions: tuple[TaskDefinitionVersion,...]) -> str:
    max_version = 0
    for version in existing_versions:
        if version.version.startswith("v") and version.version[1:].isdigit():
            max_version = max(max_version, int(version.version[1:]))
    return f"v{max_version + 1}"


def _workspace_control_trace_ref(run_ref: str) -> TraceRef:
    return TraceRef(
        run_id=run_ref,
        execution_id=execution_id_for(f"workspace-control:{run_ref}:create"),
        frontier_id=f"frontier:workspace-control:{run_ref}:terminal",
    )


def _default_trace_store_path(workspace_path: Path | None) -> Path:
    workspace = workspace_path if workspace_path is not None else Path.cwd()
    return Path(workspace) / ".vcscore" / "shepherd" / "trace.sqlite"


def _utc_now() -> str:
    return utc_now()
