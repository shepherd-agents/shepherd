"""Transport records for the Shepherd workspace-control plane."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from shepherd2.schemas.run_outputs import (
    run_output_descriptor_locator_from_payload,
    run_output_descriptor_locator_payload,
)
from shepherd_kernel_v3_reference.proof_envelope import (
    ProofEnvelope,
    ProofProfile,
    ProofStrength,
    proof_envelope_from_json,
    runtime_only_envelope,
)
from shepherd_kernel_v3_reference.vcscore_certificate import (
    VcsCoreCertificateError,
    validate_vcscore_run_proof_envelope,
)

TaskVersionStatus = Literal["active", "superseded", "deprecated", "draft"]
RunStatus = Literal["pending", "running", "merged", "retained", "discarded", "failed", "cancelled"]
RunBodyStatus = Literal["pending", "running", "completed", "failed", "stopped", "exhausted", "refused"]
RunWorldDisposition = Literal["none", "merged", "retained", "discarded"]
RunOutputPublicationStatus = Literal["not_applicable", "pending", "published", "failed"]
RunEnforcement = Literal["jail", "advisory"]
RunRequestedPlacement = Literal["auto", "advisory", "jail"]
RunResolvedPlacement = Literal["advisory", "jail"]
RunEnforcementBasis = Literal[
    "legacy_advisory",
    "explicit_advisory",
    "auto_advisory",
    "auto_jail",
    "required_jail",
    "prelaunch_advisory",
    "launch_confined_attempted",
]
PendingEffectState = Literal["recorded", "awaiting_control", "unsupported", "resolved"]
LaunchSurface = Literal["python", "cli", "model_tool", "sdk", "operator"]
TaskExecutionCallKind = Literal["root_run", "linked_call"]
TaskExecutionStatus = Literal["started", "completed", "failed"]
TaskExecutorKind = Literal["in_process", "process", "confined_process"]

_TASK_STATUSES = frozenset({"active", "superseded", "deprecated", "draft"})
_RUN_STATUSES = frozenset({"pending", "running", "merged", "retained", "discarded", "failed", "cancelled"})
_RUN_BODY_STATUSES = frozenset({"pending", "running", "completed", "failed", "stopped", "exhausted", "refused"})
_RUN_WORLD_DISPOSITIONS = frozenset({"none", "merged", "retained", "discarded"})
_RUN_OUTPUT_PUBLICATION_STATUSES = frozenset({"not_applicable", "pending", "published", "failed"})
_RUN_ENFORCEMENTS = frozenset({"jail", "advisory"})
_RUN_REQUESTED_PLACEMENTS = frozenset({"auto", "advisory", "jail"})
_RUN_RESOLVED_PLACEMENTS = frozenset({"advisory", "jail"})
_RUN_ENFORCEMENT_BASES = frozenset(
    {
        "legacy_advisory",
        "explicit_advisory",
        "auto_advisory",
        "auto_jail",
        "required_jail",
        "prelaunch_advisory",
        "launch_confined_attempted",
    }
)
_PENDING_EFFECT_STATES = frozenset({"recorded", "awaiting_control", "unsupported", "resolved"})
_LAUNCH_SURFACES = frozenset({"python", "cli", "model_tool", "sdk", "operator"})
_TASK_EXECUTION_CALL_KINDS = frozenset({"root_run", "linked_call"})
_TASK_EXECUTION_STATUSES = frozenset({"started", "completed", "failed"})
_TASK_EXECUTOR_KINDS = frozenset({"in_process", "process", "confined_process"})
# Durable launch-settlement-policy kind strings. These are SERIALIZED vocabulary
# (they name records in retained-output custody), so they live as named
# constants — a raw literal that diverges from these (e.g. the p030->skeleton
# scrub that overloaded the fenced-run-start guard's needle) is exactly the
# class of bug W1a prevents. The no-raw-literal guard
# (test_workspace_control_vocabulary.py) keeps them here.
FILESYSTEM_AUTHORITY_TERMINALIZATION_KIND = "skeleton.filesystem_authority_terminalization"
RETAINED_OUTPUT_SELECTION_KIND = "skeleton.retained_output_selection"
_LAUNCH_SETTLEMENT_POLICY_KINDS = frozenset(
    {
        FILESYSTEM_AUTHORITY_TERMINALIZATION_KIND,
        RETAINED_OUTPUT_SELECTION_KIND,
    }
)
_RUNTIME_POLICY_FIELDS = frozenset({"requested", "resolved"})
_RUNTIME_REQUESTED_FIELDS = frozenset({"trace", "provider", "model"})
_RUNTIME_RESERVED_FIELDS = frozenset(
    {
        "budget",
        "budget_seconds",
        "device",
        "max_turns",
        "plan",
        "provider_options",
        "session",
        "timeout",
        "tools",
        "world",
    }
)
_RUNTIME_RESOLVED_FIELDS = frozenset({"provider", "model"})
_V011_RUNTIME_PROVIDERS = frozenset({"claude", "static"})
_EXECUTION_ENFORCEMENT_FIELDS = frozenset(
    {
        "authority_basis",
        "body_refusal",
        "established_monitor",
        "executor_kind",
        "mode",
        "monitor_refusal",
        "monitor_required",
        "prelaunch_refusal",
        "profile",
        "provider",
        "requested_monitor",
    }
)
_RUN_AUTHORITY_CONTEXT_SCHEMA = "shepherd.workspace_control.run_authority_context.v1"
_WORKSPACE_REPO_AUTHORITIES = frozenset({"readonly", "readwrite"})

JsonObject = dict[str, object]


@dataclass(frozen=True)
class TaskArtifactRef:
    """Structured citation to one immutable task artifact revision."""

    binding: str
    store_id: str
    resource_id: str
    head: str
    artifact_digest: str
    schema: str = "shepherd.workspace_control.task_artifact_ref.v1"

    def __post_init__(self) -> None:
        _require_non_empty_str(self.schema, "task_artifact_ref.schema")
        for field_name, value in (
            ("binding", self.binding),
            ("store_id", self.store_id),
            ("resource_id", self.resource_id),
            ("head", self.head),
            ("artifact_digest", self.artifact_digest),
        ):
            _require_non_empty_str(value, f"task_artifact_ref.{field_name}")

    def to_json(self) -> JsonObject:
        return {
            "schema": self.schema,
            "binding": self.binding,
            "store_id": self.store_id,
            "resource_id": self.resource_id,
            "head": self.head,
            "artifact_digest": self.artifact_digest,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> TaskArtifactRef:
        return cls(
            schema=_required_str(value, "schema"),
            binding=_required_str(value, "binding"),
            store_id=_required_str(value, "store_id"),
            resource_id=_required_str(value, "resource_id"),
            head=_required_str(value, "head"),
            artifact_digest=_required_str(value, "artifact_digest"),
        )


@dataclass(frozen=True)
class DeclaredTaskDependency:
    """Author-time child-task selector declared by a task artifact."""

    task_id: str
    selector: str = "active"

    def __post_init__(self) -> None:
        _require_non_empty_str(self.task_id, "task_dependency.task_id")
        _require_non_empty_str(self.selector, "task_dependency.selector")

    def to_json(self) -> JsonObject:
        return {
            "task_id": self.task_id,
            "selector": self.selector,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> DeclaredTaskDependency:
        return cls(
            task_id=_required_str(value, "task_id"),
            selector=_required_str(value, "selector"),
        )


@dataclass(frozen=True)
class TaskArtifactLock:
    """Exact executable task-version lock used by a run."""

    task_id: str
    version: str
    artifact_ref: TaskArtifactRef
    artifact_digest: str
    schema_digest: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("task_id", self.task_id),
            ("version", self.version),
            ("artifact_digest", self.artifact_digest),
            ("schema_digest", self.schema_digest),
        ):
            _require_non_empty_str(value, f"task_lock.{field_name}")
        if not isinstance(self.artifact_ref, TaskArtifactRef):
            raise TypeError("task_lock.artifact_ref must be TaskArtifactRef")
        if self.artifact_ref.artifact_digest != self.artifact_digest:
            raise ValueError("task_lock artifact_digest must match artifact_ref")

    def to_json(self) -> JsonObject:
        return {
            "task_id": self.task_id,
            "version": self.version,
            "artifact_ref": self.artifact_ref.to_json(),
            "artifact_digest": self.artifact_digest,
            "schema_digest": self.schema_digest,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> TaskArtifactLock:
        return cls(
            task_id=_required_str(value, "task_id"),
            version=_required_str(value, "version"),
            artifact_ref=TaskArtifactRef.from_json(_required_mapping(value, "artifact_ref")),
            artifact_digest=_required_str(value, "artifact_digest"),
            schema_digest=_required_str(value, "schema_digest"),
        )


@dataclass(frozen=True)
class TaskDependencyLock:
    """Resolved child-task dependency edge for one run."""

    alias: str
    task_id: str
    selector: str
    version: str
    artifact_ref: TaskArtifactRef
    artifact_digest: str
    schema_digest: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("alias", self.alias),
            ("task_id", self.task_id),
            ("selector", self.selector),
            ("version", self.version),
            ("artifact_digest", self.artifact_digest),
            ("schema_digest", self.schema_digest),
        ):
            _require_non_empty_str(value, f"task_dependency_lock.{field_name}")
        if not isinstance(self.artifact_ref, TaskArtifactRef):
            raise TypeError("task_dependency_lock.artifact_ref must be TaskArtifactRef")
        if self.artifact_ref.artifact_digest != self.artifact_digest:
            raise ValueError("task_dependency_lock artifact_digest must match artifact_ref")

    def to_json(self) -> JsonObject:
        return {
            "alias": self.alias,
            "task_id": self.task_id,
            "selector": self.selector,
            "version": self.version,
            "artifact_ref": self.artifact_ref.to_json(),
            "artifact_digest": self.artifact_digest,
            "schema_digest": self.schema_digest,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> TaskDependencyLock:
        return cls(
            alias=_required_str(value, "alias"),
            task_id=_required_str(value, "task_id"),
            selector=_required_str(value, "selector"),
            version=_required_str(value, "version"),
            artifact_ref=TaskArtifactRef.from_json(_required_mapping(value, "artifact_ref")),
            artifact_digest=_required_str(value, "artifact_digest"),
            schema_digest=_required_str(value, "schema_digest"),
        )


@dataclass(frozen=True)
class ResolvedTaskGraph:
    """Preflight root task plus child-task locks observed before execution."""

    root: TaskArtifactLock
    dependencies: Mapping[str, TaskDependencyLock] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.root, TaskArtifactLock):
            raise TypeError("resolved_task_graph.root must be TaskArtifactLock")
        for alias, lock in self.dependencies.items():
            _require_non_empty_str(alias, "resolved_task_graph.dependencies key")
            if not isinstance(lock, TaskDependencyLock):
                raise TypeError("resolved_task_graph.dependencies values must be TaskDependencyLock")
            if alias != lock.alias:
                raise ValueError("resolved_task_graph dependency keys must match lock alias")

    def to_json(self) -> JsonObject:
        return {
            "root": self.root.to_json(),
            "dependencies": {alias: lock.to_json() for alias, lock in self.dependencies.items()},
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ResolvedTaskGraph:
        raw_deps = _optional_mapping(value, "dependencies")
        dependencies: dict[str, TaskDependencyLock] = {}
        for alias, raw_lock in raw_deps.items():
            if not isinstance(alias, str) or not alias:
                raise ValueError("resolved_task_graph dependency aliases must be non-empty strings")
            if not isinstance(raw_lock, Mapping):
                raise TypeError("resolved_task_graph dependency locks must be objects")
            dependencies[alias] = TaskDependencyLock.from_json(raw_lock)
        return cls(
            root=TaskArtifactLock.from_json(_required_mapping(value, "root")),
            dependencies=dependencies,
        )


@dataclass(frozen=True)
class TaskResolutionRecord:
    """Ledgered task link or exact-lock citation."""

    resolution_id: str
    reason: str
    requested_ref: str
    task_ledger_head: str | None
    task_lock: TaskArtifactLock
    parent_run_ref: str | None = None
    requester_task_id: str | None = None
    requester_task_version: str | None = None
    declared_alias: str | None = None
    launch_surface: LaunchSurface = "python"
    resolved_at: str | None = None
    metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name, value in (
            ("resolution_id", self.resolution_id),
            ("reason", self.reason),
            ("requested_ref", self.requested_ref),
        ):
            _require_non_empty_str(value, f"task_resolution.{field_name}")
        if not isinstance(self.task_lock, TaskArtifactLock):
            raise TypeError("task_resolution.task_lock must be TaskArtifactLock")
        for field_name, value in (
            ("task_ledger_head", self.task_ledger_head),
            ("parent_run_ref", self.parent_run_ref),
            ("requester_task_id", self.requester_task_id),
            ("requester_task_version", self.requester_task_version),
            ("declared_alias", self.declared_alias),
            ("resolved_at", self.resolved_at),
        ):
            _require_optional_str(value, f"task_resolution.{field_name}")
        if self.launch_surface not in _LAUNCH_SURFACES:
            raise ValueError(f"unsupported task resolution launch surface: {self.launch_surface!r}")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("task_resolution.metadata must be an object")

    def to_json(self) -> JsonObject:
        return {
            "resolution_id": self.resolution_id,
            "reason": self.reason,
            "requested_ref": self.requested_ref,
            "task_ledger_head": self.task_ledger_head,
            "task_lock": self.task_lock.to_json(),
            "parent_run_ref": self.parent_run_ref,
            "requester_task_id": self.requester_task_id,
            "requester_task_version": self.requester_task_version,
            "declared_alias": self.declared_alias,
            "launch_surface": self.launch_surface,
            "resolved_at": self.resolved_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> TaskResolutionRecord:
        return cls(
            resolution_id=_required_str(value, "resolution_id"),
            reason=_required_str(value, "reason"),
            requested_ref=_required_str(value, "requested_ref"),
            task_ledger_head=_optional_str(value, "task_ledger_head"),
            task_lock=TaskArtifactLock.from_json(_required_mapping(value, "task_lock")),
            parent_run_ref=_optional_str(value, "parent_run_ref"),
            requester_task_id=_optional_str(value, "requester_task_id"),
            requester_task_version=_optional_str(value, "requester_task_version"),
            declared_alias=_optional_str(value, "declared_alias"),
            launch_surface=_required_member(value, "launch_surface", _LAUNCH_SURFACES, default="python"),  # type: ignore[arg-type]
            resolved_at=_optional_str(value, "resolved_at"),
            metadata=dict(_optional_mapping(value, "metadata")),
        )


@dataclass(frozen=True)
class TraceRef:
    """Provider-neutral trace owner/cutoff identity."""

    run_id: str
    execution_id: str
    frontier_id: str

    def __post_init__(self) -> None:
        _require_non_empty_str(self.run_id, "trace_ref.run_id")
        _require_non_empty_str(self.execution_id, "trace_ref.execution_id")
        _require_non_empty_str(self.frontier_id, "trace_ref.frontier_id")

    def to_json(self) -> JsonObject:
        return {
            "run_id": self.run_id,
            "execution_id": self.execution_id,
            "frontier_id": self.frontier_id,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> TraceRef:
        return cls(
            run_id=_required_str(value, "run_id"),
            execution_id=_required_str(value, "execution_id"),
            frontier_id=_required_str(value, "frontier_id"),
        )


@dataclass(frozen=True)
class RunOutputCitationRef:
    """Run-ledger citation to a trace-owned output descriptor."""

    output_name: str
    output_id: str
    trace_ref: TraceRef
    descriptor_locator: JsonObject
    binding: str
    store_id: str
    resource_id: str
    materialization_kind: Literal["tree", "external"]
    custody_ref: str
    output_world_oid: str
    parent_basis_world_oid: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("output_name", self.output_name),
            ("output_id", self.output_id),
            ("binding", self.binding),
            ("store_id", self.store_id),
            ("resource_id", self.resource_id),
            ("materialization_kind", self.materialization_kind),
            ("custody_ref", self.custody_ref),
            ("output_world_oid", self.output_world_oid),
            ("parent_basis_world_oid", self.parent_basis_world_oid),
        ):
            _require_non_empty_str(value, f"run_output.{field_name}")
        if self.materialization_kind not in {"tree", "external"}:
            raise ValueError(f"unsupported run output materialization kind: {self.materialization_kind!r}")
        if not isinstance(self.trace_ref, TraceRef):
            raise TypeError("run_output.trace_ref must be TraceRef")
        object.__setattr__(
            self,
            "descriptor_locator",
            _validated_descriptor_locator_payload(
                self.descriptor_locator,
                output_name=self.output_name,
                trace_ref=self.trace_ref,
            ),
        )

    def to_json(self) -> JsonObject:
        return {
            "output_name": self.output_name,
            "output_id": self.output_id,
            "trace_ref": self.trace_ref.to_json(),
            "descriptor_locator": dict(self.descriptor_locator),
            "binding": self.binding,
            "store_id": self.store_id,
            "resource_id": self.resource_id,
            "materialization_kind": self.materialization_kind,
            "custody_ref": self.custody_ref,
            "output_world_oid": self.output_world_oid,
            "parent_basis_world_oid": self.parent_basis_world_oid,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RunOutputCitationRef:
        trace_value = _required_mapping(value, "trace_ref")
        return cls(
            output_name=_required_str(value, "output_name"),
            output_id=_required_str(value, "output_id"),
            trace_ref=TraceRef.from_json(trace_value),
            descriptor_locator=dict(_required_mapping(value, "descriptor_locator")),
            binding=_required_str(value, "binding"),
            store_id=_required_str(value, "store_id"),
            resource_id=_required_str(value, "resource_id"),
            materialization_kind=_required_member(value, "materialization_kind", {"tree", "external"}),  # type: ignore[arg-type]
            custody_ref=_required_str(value, "custody_ref"),
            output_world_oid=_required_str(value, "output_world_oid"),
            parent_basis_world_oid=_required_str(value, "parent_basis_world_oid"),
        )


@dataclass(frozen=True)
class TaskDefinitionVersion:
    """Durable task-library version record."""

    task_id: str
    version: str
    import_path: str
    schema_digest: str
    may_default: str
    status: TaskVersionStatus
    base_version: str | None = None
    artifact_ref: TaskArtifactRef | None = None
    artifact_digest: str | None = None
    source_identity: str | None = None
    signature_schema: JsonObject = field(default_factory=dict)
    declared_dependencies: Mapping[str, DeclaredTaskDependency] = field(default_factory=dict)
    metadata: JsonObject = field(default_factory=dict)
    produced_by_run: str | None = None
    derived_from: tuple[str, ...] = ()
    created_at: str | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("task_id", self.task_id),
            ("version", self.version),
            ("import_path", self.import_path),
            ("schema_digest", self.schema_digest),
            ("may_default", self.may_default),
        ):
            _require_non_empty_str(value, f"task.{field_name}")
        if self.status not in _TASK_STATUSES:
            raise ValueError(f"unsupported task status: {self.status!r}")
        for field_name, value in (
            ("base_version", self.base_version),
            ("artifact_digest", self.artifact_digest),
            ("source_identity", self.source_identity),
            ("produced_by_run", self.produced_by_run),
            ("created_at", self.created_at),
        ):
            _require_optional_str(value, f"task.{field_name}")
        if self.artifact_ref is not None:
            if not isinstance(self.artifact_ref, TaskArtifactRef):
                raise TypeError("task.artifact_ref must be TaskArtifactRef or None")
            if self.artifact_digest != self.artifact_ref.artifact_digest:
                raise ValueError("task.artifact_digest must match artifact_ref.artifact_digest")
        for alias, dependency in self.declared_dependencies.items():
            _require_non_empty_str(alias, "task.declared_dependencies key")
            if not isinstance(dependency, DeclaredTaskDependency):
                raise TypeError("task.declared_dependencies values must be DeclaredTaskDependency")
        _require_str_tuple(self.derived_from, "task.derived_from")

    def to_json(self) -> JsonObject:
        return {
            "task_id": self.task_id,
            "version": self.version,
            "base_version": self.base_version,
            "import_path": self.import_path,
            "artifact_ref": None if self.artifact_ref is None else self.artifact_ref.to_json(),
            "artifact_digest": self.artifact_digest,
            "source_identity": self.source_identity,
            "schema_digest": self.schema_digest,
            "signature_schema": dict(self.signature_schema),
            "declared_dependencies": {
                alias: dependency.to_json() for alias, dependency in self.declared_dependencies.items()
            },
            "may_default": self.may_default,
            "status": self.status,
            "metadata": dict(self.metadata),
            "produced_by_run": self.produced_by_run,
            "derived_from": list(self.derived_from),
            "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> TaskDefinitionVersion:
        return cls(
            task_id=_required_str(value, "task_id"),
            version=_required_str(value, "version"),
            base_version=_optional_str(value, "base_version"),
            import_path=_required_str(value, "import_path"),
            artifact_ref=_optional_task_artifact_ref(value, "artifact_ref"),
            artifact_digest=_optional_str(value, "artifact_digest"),
            source_identity=_optional_str(value, "source_identity"),
            schema_digest=_required_str(value, "schema_digest"),
            signature_schema=dict(_optional_mapping(value, "signature_schema")),
            declared_dependencies=_declared_dependency_map(value.get("declared_dependencies")),
            may_default=_required_str(value, "may_default"),
            status=_required_member(value, "status", _TASK_STATUSES),  # type: ignore[arg-type]
            metadata=dict(_optional_mapping(value, "metadata")),
            produced_by_run=_optional_str(value, "produced_by_run"),
            derived_from=_str_tuple(value.get("derived_from"), "derived_from"),
            created_at=_optional_str(value, "created_at"),
        )

    def summary(self) -> TaskSummary:
        return TaskSummary(
            task_id=self.task_id,
            version=self.version,
            status=self.status,
            import_path=self.import_path,
            schema_digest=self.schema_digest,
        )

    def resolved(self) -> ResolvedTask:
        return ResolvedTask(
            task_id=self.task_id,
            version=self.version,
            import_path=self.import_path,
            schema_digest=self.schema_digest,
            source_identity=self.source_identity,
            may_default=self.may_default,
            status=self.status,
            artifact_ref=self.artifact_ref,
            artifact_digest=self.artifact_digest,
            signature_schema=dict(self.signature_schema),
            declared_dependencies=dict(self.declared_dependencies),
        )


@dataclass(frozen=True)
class TaskSummary:
    """Compact task row for list views."""

    task_id: str
    version: str
    status: TaskVersionStatus
    import_path: str
    schema_digest: str

    def to_json(self) -> JsonObject:
        return {
            "task_id": self.task_id,
            "version": self.version,
            "status": self.status,
            "import_path": self.import_path,
            "schema_digest": self.schema_digest,
        }


@dataclass(frozen=True)
class ResolvedTask:
    """Transportable task snapshot used before execution."""

    task_id: str
    version: str
    import_path: str
    schema_digest: str
    source_identity: str | None
    may_default: str
    status: TaskVersionStatus = "active"
    artifact_ref: TaskArtifactRef | None = None
    artifact_digest: str | None = None
    signature_schema: JsonObject = field(default_factory=dict)
    declared_dependencies: Mapping[str, DeclaredTaskDependency] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in _TASK_STATUSES:
            raise ValueError(f"unsupported resolved task status: {self.status!r}")
        if self.artifact_ref is not None:
            if not isinstance(self.artifact_ref, TaskArtifactRef):
                raise TypeError("resolved_task.artifact_ref must be TaskArtifactRef or None")
            if self.artifact_digest != self.artifact_ref.artifact_digest:
                raise ValueError("resolved_task artifact_digest must match artifact_ref")

    def to_json(self) -> JsonObject:
        return {
            "task_id": self.task_id,
            "version": self.version,
            "import_path": self.import_path,
            "schema_digest": self.schema_digest,
            "source_identity": self.source_identity,
            "may_default": self.may_default,
            "status": self.status,
            "artifact_ref": None if self.artifact_ref is None else self.artifact_ref.to_json(),
            "artifact_digest": self.artifact_digest,
            "signature_schema": dict(self.signature_schema),
            "declared_dependencies": {
                alias: dependency.to_json() for alias, dependency in self.declared_dependencies.items()
            },
        }


@dataclass(frozen=True)
class RunOperationRefs:
    """Structured durable identities produced during run orchestration."""

    run_start_revision: str | None = None
    runtime_operation: str | None = None
    authority_operation: str | None = None
    authority_settlement_operation: str | None = None
    runtime_value_ref: str | None = None
    trace_head: str | None = None
    run_finish_revision: str | None = None

    def __post_init__(self) -> None:
        for field_name, value in self.to_json().items():
            _require_optional_str(value, f"run_operation_refs.{field_name}")
        _require_optional_str(self.run_finish_revision, "run_operation_refs.run_finish_revision")

    def to_json(self) -> JsonObject:
        return {
            "run_start_revision": self.run_start_revision,
            "runtime_operation": self.runtime_operation,
            "authority_operation": self.authority_operation,
            "authority_settlement_operation": self.authority_settlement_operation,
            "runtime_value_ref": self.runtime_value_ref,
            "trace_head": self.trace_head,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object] | None) -> RunOperationRefs:
        if value is None:
            return cls()
        return cls(
            run_start_revision=_optional_str(value, "run_start_revision"),
            runtime_operation=_optional_str(value, "runtime_operation"),
            authority_operation=_optional_str(value, "authority_operation"),
            authority_settlement_operation=_optional_str(value, "authority_settlement_operation"),
            runtime_value_ref=_optional_str(value, "runtime_value_ref"),
            trace_head=_optional_str(value, "trace_head"),
            run_finish_revision=_optional_str(value, "run_finish_revision"),
        )


@dataclass(frozen=True)
class RunAuthorityContext:
    """Durable authority metadata for one workspace-control run.

    This is evidence and classifier metadata, not custody. Retained-output
    settlement remains in vcs-core, while launch/settlement records describe
    which execution and adoption monitors enforced the authority surface.

    Placement-policy review rule: facts a settlement decision depends on live
    here, behind the fail-closed authority-context validator. RunExecutionEvidence
    is for observability about how the run executed, not a side channel for
    settlement-relevant fields that would otherwise face this validator.
    """

    task_default_may: str
    requested_may: str | None
    effective_may: str
    repo_authority: str
    workspace_selection_can_mutate: bool
    grant_clamp: JsonObject
    effective_grant: JsonObject
    effective_grant_digest: str
    effective_match_digest: str
    authority_surface_plan_digest: str
    classifier_policy: JsonObject
    per_binding_authority: JsonObject | None = None
    schema: str = _RUN_AUTHORITY_CONTEXT_SCHEMA

    def __post_init__(self) -> None:
        for field_name, value in (
            ("schema", self.schema),
            ("task_default_may", self.task_default_may),
            ("effective_may", self.effective_may),
            ("repo_authority", self.repo_authority),
            ("effective_grant_digest", self.effective_grant_digest),
            ("effective_match_digest", self.effective_match_digest),
            ("authority_surface_plan_digest", self.authority_surface_plan_digest),
        ):
            _require_non_empty_str(value, f"run.authority_context.{field_name}")
        if self.schema != _RUN_AUTHORITY_CONTEXT_SCHEMA:
            raise ValueError(f"unsupported run authority context schema: {self.schema!r}")
        _require_optional_str(self.requested_may, "run.authority_context.requested_may")
        if self.repo_authority not in _WORKSPACE_REPO_AUTHORITIES:
            raise ValueError(f"unsupported run authority repo_authority: {self.repo_authority!r}")
        if not isinstance(self.workspace_selection_can_mutate, bool):
            raise TypeError("run.authority_context.workspace_selection_can_mutate must be a boolean")
        for field_name, value in (
            ("grant_clamp", self.grant_clamp),
            ("effective_grant", self.effective_grant),
            ("classifier_policy", self.classifier_policy),
        ):
            if not isinstance(value, Mapping) or not value:
                raise TypeError(f"run.authority_context.{field_name} must be a non-empty object")
        grant_digest = self.effective_grant.get("digest")
        if grant_digest is not None:
            raise ValueError("run.authority_context.effective_grant must not embed a digest field")
        if self.per_binding_authority is not None:
            # Lane C additive per-binding evidence: {name: {"authority": ..., "root": ...}}.
            # Single-binding runs leave it absent, keeping the persisted shape byte-identical.
            if not isinstance(self.per_binding_authority, Mapping) or not self.per_binding_authority:
                raise TypeError("run.authority_context.per_binding_authority must be a non-empty object when present")
            for name, entry in self.per_binding_authority.items():
                if not isinstance(name, str) or not name:
                    raise ValueError("run.authority_context.per_binding_authority names must be non-empty strings")
                if not isinstance(entry, Mapping):
                    raise TypeError("run.authority_context.per_binding_authority entries must be objects")
                if entry.get("authority") not in _WORKSPACE_REPO_AUTHORITIES:
                    raise ValueError(
                        f"run.authority_context.per_binding_authority[{name!r}].authority is unsupported: "
                        f"{entry.get('authority')!r}"
                    )
                if not isinstance(entry.get("root"), str):
                    raise TypeError(f"run.authority_context.per_binding_authority[{name!r}].root must be a string")

    def to_json(self) -> JsonObject:
        return {
            "schema": self.schema,
            "task_default_may": self.task_default_may,
            "requested_may": self.requested_may,
            "effective_may": self.effective_may,
            "repo_authority": self.repo_authority,
            "workspace_selection_can_mutate": self.workspace_selection_can_mutate,
            "grant_clamp": dict(self.grant_clamp),
            "effective_grant": dict(self.effective_grant),
            "effective_grant_digest": self.effective_grant_digest,
            "effective_match_digest": self.effective_match_digest,
            "authority_surface_plan_digest": self.authority_surface_plan_digest,
            "classifier_policy": dict(self.classifier_policy),
            "per_binding_authority": (None if self.per_binding_authority is None else dict(self.per_binding_authority)),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RunAuthorityContext:
        raw_per_binding = value.get("per_binding_authority")
        return cls(
            schema=_required_str(value, "schema"),
            task_default_may=_required_str(value, "task_default_may"),
            requested_may=_optional_str(value, "requested_may"),
            effective_may=_required_str(value, "effective_may"),
            repo_authority=_required_str(value, "repo_authority"),
            workspace_selection_can_mutate=_required_bool(value, "workspace_selection_can_mutate"),
            grant_clamp=dict(_required_mapping(value, "grant_clamp")),
            effective_grant=dict(_required_mapping(value, "effective_grant")),
            effective_grant_digest=_required_str(value, "effective_grant_digest"),
            effective_match_digest=_required_str(value, "effective_match_digest"),
            authority_surface_plan_digest=_required_str(value, "authority_surface_plan_digest"),
            classifier_policy=dict(_required_mapping(value, "classifier_policy")),
            per_binding_authority=None
            if raw_per_binding is None
            else dict(_required_mapping(value, "per_binding_authority")),
        )


@dataclass(frozen=True)
class RunLaunchContext:
    """Durable launch-context summary for parentage and supervision."""

    parent_run_ref: str | None = None
    launched_by: str | None = None
    caused_by_event_ref: str | None = None
    launch_surface: LaunchSurface = "operator"
    authority_ref: str | None = None
    may_profile: str | None = None
    inherited_may: str | None = None
    inherited_plan_ref: str | None = None
    handler_env_ref: str | None = None
    settlement_policy: JsonObject | None = None

    def __post_init__(self) -> None:
        if self.launch_surface not in _LAUNCH_SURFACES:
            raise ValueError(f"unsupported launch surface: {self.launch_surface!r}")
        for field_name, value in (
            ("parent_run_ref", self.parent_run_ref),
            ("launched_by", self.launched_by),
            ("caused_by_event_ref", self.caused_by_event_ref),
            ("authority_ref", self.authority_ref),
            ("may_profile", self.may_profile),
            ("inherited_may", self.inherited_may),
            ("inherited_plan_ref", self.inherited_plan_ref),
            ("handler_env_ref", self.handler_env_ref),
        ):
            _require_optional_str(value, f"launch_context.{field_name}")
        if self.settlement_policy is not None:
            if not isinstance(self.settlement_policy, Mapping):
                raise TypeError("launch_context.settlement_policy must be an object or null")
            _validate_launch_settlement_policy(self.settlement_policy)

    def to_json(self) -> JsonObject:
        return {
            "parent_run_ref": self.parent_run_ref,
            "launched_by": self.launched_by,
            "caused_by_event_ref": self.caused_by_event_ref,
            "launch_surface": self.launch_surface,
            "authority_ref": self.authority_ref,
            "may_profile": self.may_profile,
            "inherited_may": self.inherited_may,
            "inherited_plan_ref": self.inherited_plan_ref,
            "handler_env_ref": self.handler_env_ref,
            "settlement_policy": None if self.settlement_policy is None else dict(self.settlement_policy),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object] | None) -> RunLaunchContext:
        if value is None:
            return cls()
        return cls(
            parent_run_ref=_optional_str(value, "parent_run_ref"),
            launched_by=_optional_str(value, "launched_by"),
            caused_by_event_ref=_optional_str(value, "caused_by_event_ref"),
            launch_surface=_required_member(value, "launch_surface", _LAUNCH_SURFACES, default="operator"),  # type: ignore[arg-type]
            authority_ref=_optional_str(value, "authority_ref"),
            may_profile=_optional_str(value, "may_profile"),
            inherited_may=_optional_str(value, "inherited_may"),
            inherited_plan_ref=_optional_str(value, "inherited_plan_ref"),
            handler_env_ref=_optional_str(value, "handler_env_ref"),
            settlement_policy=_optional_mapping_or_none(value, "settlement_policy"),
        )


def _validate_launch_settlement_policy(policy: Mapping[str, object]) -> None:
    kind = policy.get("kind")
    if kind not in _LAUNCH_SETTLEMENT_POLICY_KINDS:
        raise ValueError(f"unsupported launch settlement policy kind: {kind!r}")
    if kind == RETAINED_OUTPUT_SELECTION_KIND:
        _validate_retained_output_selection_policy(policy)
        return
    if kind == FILESYSTEM_AUTHORITY_TERMINALIZATION_KIND:
        _validate_filesystem_authority_terminalization_policy(policy)
        return


def _validate_retained_output_selection_policy(policy: Mapping[str, object]) -> None:
    _reject_unknown_fields(
        policy,
        {"kind", "authority_context", "execution_enforcement", "runtime"},
        "retained-output settlement policy",
    )
    _required_mapping(policy, "authority_context")
    _validate_execution_enforcement_policy(_required_mapping(policy, "execution_enforcement"))
    runtime = policy.get("runtime")
    if runtime is not None:
        if not isinstance(runtime, Mapping):
            raise TypeError("retained-output settlement policy runtime must be an object")
        _validate_runtime_policy(runtime)


def _validate_filesystem_authority_terminalization_policy(policy: Mapping[str, object]) -> None:
    _reject_unknown_fields(
        policy,
        {"kind", "binding_roots", "authority_context"},
        "filesystem-authority settlement policy",
    )
    _required_mapping(policy, "binding_roots")
    _required_mapping(policy, "authority_context")


def _validate_execution_enforcement_policy(value: Mapping[str, object]) -> None:
    _reject_unknown_fields(value, _EXECUTION_ENFORCEMENT_FIELDS, "execution enforcement policy")
    mode = value.get("mode")
    if mode not in {"in_process", "confined_process"}:
        raise ValueError(f"execution enforcement policy mode is unsupported: {mode!r}")
    executor_kind = value.get("executor_kind")
    if executor_kind not in {"in_process", "confined_process"}:
        raise ValueError(f"execution enforcement policy executor_kind is unsupported: {executor_kind!r}")
    _required_str(value, "provider")
    _required_str(value, "profile")
    _required_str(value, "authority_basis")
    monitor_required = value.get("monitor_required")
    if not isinstance(monitor_required, bool):
        raise TypeError("execution enforcement policy monitor_required must be a boolean")
    for field_name in ("requested_monitor", "established_monitor"):
        _require_optional_str(value.get(field_name), f"execution_enforcement.{field_name}")
    for field_name in ("monitor_refusal", "prelaunch_refusal", "body_refusal"):
        raw = value.get(field_name)
        if raw is not None and not isinstance(raw, Mapping):
            raise TypeError(f"execution enforcement policy {field_name} must be an object or null")
    if mode == "in_process":
        if monitor_required or value.get("requested_monitor") is not None:
            raise ValueError("in-process execution enforcement cannot require a monitor")
    elif not monitor_required or value.get("requested_monitor") is None:
        raise ValueError("confined execution enforcement requires a requested monitor")


def _validate_runtime_policy(value: Mapping[str, object]) -> None:
    _reject_unknown_fields(value, _RUNTIME_POLICY_FIELDS, "runtime policy")
    requested = _required_mapping(value, "requested")
    resolved = _required_mapping(value, "resolved")
    requested_provider, requested_model = _validate_runtime_requested_policy(requested)
    resolved_provider, resolved_model = _validate_runtime_resolved_policy(resolved)
    if requested_provider is None:
        if resolved_provider is not None:
            raise ValueError("runtime policy cannot resolve a provider that was not requested")
        if resolved_model is not None:
            raise ValueError("runtime policy cannot resolve a model that was not requested")
        return
    if resolved_provider != requested_provider:
        raise ValueError("runtime policy requested provider must match resolved provider")
    if requested_model is None:
        if resolved_model is not None:
            raise ValueError("runtime policy cannot resolve a model that was not requested")
    elif resolved_model != requested_model:
        raise ValueError("runtime policy requested model must match resolved model")


def _validate_runtime_requested_policy(value: Mapping[str, object]) -> tuple[str | None, str | None]:
    reserved = sorted(set(value) & _RUNTIME_RESERVED_FIELDS)
    if reserved:
        raise ValueError(f"runtime policy requested field(s) reserved for future use: {', '.join(reserved)}")
    _reject_unknown_fields(value, _RUNTIME_REQUESTED_FIELDS, "runtime policy requested")
    trace = value.get("trace")
    if trace is not None and not isinstance(trace, Mapping):
        raise TypeError("runtime policy requested.trace must be an object")
    provider_id = _runtime_provider_payload_id(value.get("provider"), label="runtime policy requested.provider")
    model_name = _runtime_model_payload_name(value.get("model"), label="runtime policy requested.model")
    if model_name is not None and provider_id is None:
        raise ValueError("runtime policy requested.model requires requested.provider")
    if provider_id is not None and provider_id not in _V011_RUNTIME_PROVIDERS:
        raise ValueError(f"runtime policy provider is not supported in v0.1.1: {provider_id!r}")
    return provider_id, model_name


def _validate_runtime_resolved_policy(value: Mapping[str, object]) -> tuple[str | None, str | None]:
    _reject_unknown_fields(value, _RUNTIME_RESOLVED_FIELDS, "runtime policy resolved")
    provider = value.get("provider")
    if provider is None:
        provider_id = None
    elif isinstance(provider, str) and provider:
        provider_id = provider
    else:
        raise ValueError("runtime policy resolved.provider must be a non-empty string")
    model = value.get("model")
    if model is not None and (not isinstance(model, str) or not model):
        raise ValueError("runtime policy resolved.model must be null or a non-empty string")
    if model is not None and provider_id is None:
        raise ValueError("runtime policy resolved.model requires resolved.provider")
    if provider_id is not None and provider_id not in _V011_RUNTIME_PROVIDERS:
        raise ValueError(f"runtime policy resolved provider is not supported in v0.1.1: {provider_id!r}")
    return provider_id, model if isinstance(model, str) else None


def _runtime_provider_payload_id(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    _reject_unknown_fields(value, {"id"}, label)
    provider_id = _required_str(value, "id").strip().lower()
    if not provider_id:
        raise ValueError(f"{label}.id must be a non-empty string")
    return provider_id


def _runtime_model_payload_name(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    _reject_unknown_fields(value, {"name"}, label)
    return _required_str(value, "name")


def _reject_unknown_fields(value: Mapping[str, object], allowed: set[str] | frozenset[str], label: str) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ValueError(f"{label} has unsupported field(s): {', '.join(unknown)}")


@dataclass(frozen=True)
class RunExecutionEvidence:
    """Durable placement and enforcement evidence for one run.

    Authority context describes what the run was allowed to do. This object
    describes the execution device that attempted to enforce it.
    """

    requested_placement: RunRequestedPlacement = "advisory"
    resolved_placement: RunResolvedPlacement = "advisory"
    enforcement_basis: RunEnforcementBasis = "legacy_advisory"
    execution_descriptor: JsonObject | None = None
    # Additive (P1.2 / finding #5): the resolved state of the flags that alter
    # durable run behavior (e.g. ``seal_and_select``), so two runs under
    # different flag state are distinguishable in the record. ``None`` on
    # records written before this field existed — read as "not recorded", never
    # as "all-false".
    effective_feature_flags: JsonObject | None = None

    def __post_init__(self) -> None:
        if self.requested_placement not in _RUN_REQUESTED_PLACEMENTS:
            raise ValueError(f"unsupported requested placement: {self.requested_placement!r}")
        if self.resolved_placement not in _RUN_RESOLVED_PLACEMENTS:
            raise ValueError(f"unsupported resolved placement: {self.resolved_placement!r}")
        if self.enforcement_basis not in _RUN_ENFORCEMENT_BASES:
            raise ValueError(f"unsupported enforcement basis: {self.enforcement_basis!r}")
        if self.execution_descriptor is not None and not isinstance(self.execution_descriptor, Mapping):
            raise TypeError("run.execution_evidence.execution_descriptor must be an object or null")
        if self.effective_feature_flags is not None and not isinstance(self.effective_feature_flags, Mapping):
            raise TypeError("run.execution_evidence.effective_feature_flags must be an object or null")
        _validate_run_execution_evidence(self)

    def to_json(self) -> JsonObject:
        return {
            "requested_placement": self.requested_placement,
            "resolved_placement": self.resolved_placement,
            "enforcement_basis": self.enforcement_basis,
            "execution_descriptor": None if self.execution_descriptor is None else dict(self.execution_descriptor),
            "effective_feature_flags": (
                None if self.effective_feature_flags is None else dict(self.effective_feature_flags)
            ),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object] | None) -> RunExecutionEvidence:
        if value is None:
            return cls()
        return cls(
            requested_placement=_required_member(
                value,
                "requested_placement",
                _RUN_REQUESTED_PLACEMENTS,
                default="advisory",
            ),  # type: ignore[arg-type]
            resolved_placement=_required_member(
                value,
                "resolved_placement",
                _RUN_RESOLVED_PLACEMENTS,
                default="advisory",
            ),  # type: ignore[arg-type]
            enforcement_basis=_required_member(
                value,
                "enforcement_basis",
                _RUN_ENFORCEMENT_BASES,
                default="legacy_advisory",
            ),  # type: ignore[arg-type]
            execution_descriptor=_optional_mapping_or_none(value, "execution_descriptor"),
            effective_feature_flags=_optional_mapping_or_none(value, "effective_feature_flags"),
        )


def _validate_run_execution_evidence(evidence: RunExecutionEvidence) -> None:
    requested = evidence.requested_placement
    resolved = evidence.resolved_placement
    basis = evidence.enforcement_basis
    if requested == "advisory" and resolved != "advisory":
        raise ValueError("run execution evidence cannot resolve advisory placement to jail")
    if requested == "jail" and resolved != "jail":
        raise ValueError("run execution evidence cannot resolve required jail placement to advisory")

    legal_rows = {
        ("advisory", "advisory", "legacy_advisory"),
        ("advisory", "advisory", "explicit_advisory"),
        ("auto", "advisory", "auto_advisory"),
        ("auto", "jail", "auto_jail"),
        ("auto", "jail", "prelaunch_advisory"),
        ("auto", "jail", "launch_confined_attempted"),
        ("jail", "jail", "required_jail"),
        ("jail", "jail", "prelaunch_advisory"),
        ("jail", "jail", "launch_confined_attempted"),
    }
    if (requested, resolved, basis) not in legal_rows:
        raise ValueError(
            "run execution evidence has inconsistent placement/evidence fields: "
            f"requested={requested!r}, resolved={resolved!r}, basis={basis!r}"
        )


def _validate_run_enforcement_evidence(
    enforcement: RunEnforcement,
    evidence: RunExecutionEvidence,
) -> None:
    basis = evidence.enforcement_basis
    if basis == "launch_confined_attempted" and enforcement != "jail":
        raise ValueError("run enforcement must be jail when launch_confined was attempted")
    if basis == "prelaunch_advisory" and enforcement != "advisory":
        raise ValueError("prelaunch advisory execution evidence requires advisory enforcement")
    if enforcement == "jail" and basis != "launch_confined_attempted":
        raise ValueError("run enforcement jail requires launch_confined evidence")


@dataclass(frozen=True)
class RunRetainedCustody:
    """Canonical retained-output custody handle for one retained run."""

    custody_ref: str
    output_world_oid: str
    binding: str
    store_id: str
    resource_id: str
    parent_basis_world_oid: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("custody_ref", self.custody_ref),
            ("output_world_oid", self.output_world_oid),
            ("binding", self.binding),
            ("store_id", self.store_id),
            ("resource_id", self.resource_id),
            ("parent_basis_world_oid", self.parent_basis_world_oid),
        ):
            _require_non_empty_str(value, f"run.terminalization.retained_custody.{field_name}")

    def to_json(self) -> JsonObject:
        return {
            "custody_ref": self.custody_ref,
            "output_world_oid": self.output_world_oid,
            "binding": self.binding,
            "store_id": self.store_id,
            "resource_id": self.resource_id,
            "parent_basis_world_oid": self.parent_basis_world_oid,
        }

    @classmethod
    def from_retained_output(cls, retained: Any) -> RunRetainedCustody:
        """Build the run-ledger custody handle from vcs-core's retained-output row."""
        return cls(
            custody_ref=_required_attr_str(retained, "handoff_ref", "retained output"),
            output_world_oid=_required_attr_str(retained, "output_world_oid", "retained output"),
            binding=_required_attr_str(retained, "binding", "retained output"),
            store_id=_required_attr_str(retained, "store_id", "retained output"),
            resource_id=_required_attr_str(retained, "resource_id", "retained output"),
            parent_basis_world_oid=_required_attr_str(retained, "parent_basis_world_oid", "retained output"),
        )

    @classmethod
    def from_output_citation(cls, citation: RunOutputCitationRef) -> RunRetainedCustody:
        """Build the custody handle cited by a product-visible run output."""
        return cls(
            custody_ref=citation.custody_ref,
            output_world_oid=citation.output_world_oid,
            binding=citation.binding,
            store_id=citation.store_id,
            resource_id=citation.resource_id,
            parent_basis_world_oid=citation.parent_basis_world_oid,
        )

    @classmethod
    def from_seal_handoff(cls, handoff: Any) -> RunRetainedCustody:
        """Build the custody handle from vcs-core's durable seal handoff."""
        return cls(
            custody_ref=_required_attr_str(handoff, "handoff_ref", "seal handoff"),
            output_world_oid=_required_attr_str(handoff, "output_world_oid", "seal handoff"),
            binding=_required_attr_str(handoff, "binding", "seal handoff"),
            store_id=_required_attr_str(handoff, "store_id", "seal handoff"),
            resource_id=_required_attr_str(handoff, "resource_id", "seal handoff"),
            parent_basis_world_oid=_required_attr_str(handoff, "parent_basis_world_oid", "seal handoff"),
        )

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RunRetainedCustody:
        return cls(
            custody_ref=_required_str(value, "custody_ref"),
            output_world_oid=_required_str(value, "output_world_oid"),
            binding=_required_str(value, "binding"),
            store_id=_required_str(value, "store_id"),
            resource_id=_required_str(value, "resource_id"),
            parent_basis_world_oid=_required_str(value, "parent_basis_world_oid"),
        )

    def matches_retained_output(self, retained: Any) -> bool:
        """Return whether a retained-output row carries this custody handle."""
        return (
            getattr(retained, "handoff_ref", None) == self.custody_ref
            and getattr(retained, "output_world_oid", None) == self.output_world_oid
            and getattr(retained, "binding", None) == self.binding
            and getattr(retained, "store_id", None) == self.store_id
            and getattr(retained, "resource_id", None) == self.resource_id
            and getattr(retained, "parent_basis_world_oid", None) == self.parent_basis_world_oid
        )


@dataclass(frozen=True)
class RunTerminalization:
    """Canonical body/world/publication phase breakdown for one run record."""

    body_status: RunBodyStatus
    world_disposition: RunWorldDisposition
    output_publication_status: RunOutputPublicationStatus
    retained_custody: RunRetainedCustody | None = None
    publication_error: JsonObject | None = None

    def __post_init__(self) -> None:
        if self.body_status not in _RUN_BODY_STATUSES:
            raise ValueError(f"unsupported run body status: {self.body_status!r}")
        if self.world_disposition not in _RUN_WORLD_DISPOSITIONS:
            raise ValueError(f"unsupported run world disposition: {self.world_disposition!r}")
        if self.output_publication_status not in _RUN_OUTPUT_PUBLICATION_STATUSES:
            raise ValueError(f"unsupported run output publication status: {self.output_publication_status!r}")
        if self.retained_custody is not None and not isinstance(self.retained_custody, RunRetainedCustody):
            raise TypeError("run.terminalization.retained_custody must be RunRetainedCustody or None")
        if self.publication_error is not None and not isinstance(self.publication_error, Mapping):
            raise TypeError("run.terminalization.publication_error must be an object or None")
        if self.world_disposition == "retained":
            if self.retained_custody is None:
                raise ValueError("run.terminalization retained_custody is required for retained worlds")
        elif self.retained_custody is not None:
            raise ValueError("run.terminalization retained_custody requires world_disposition='retained'")
        if self.output_publication_status == "failed" and self.publication_error is None:
            raise ValueError("run.terminalization publication_error is required when publication failed")
        if self.output_publication_status != "failed" and self.publication_error is not None:
            raise ValueError("run.terminalization publication_error requires failed publication status")
        if self.output_publication_status == "failed":
            _validate_terminalization_publication_error(self)
        if self.world_disposition in {"merged", "retained"} and self.body_status != "completed":
            raise ValueError("run.terminalization merged/retained worlds require completed body status")
        if self.body_status in {"pending", "running"}:
            if self.world_disposition != "none":
                raise ValueError("run.terminalization pending/running bodies require no world disposition")
            if self.output_publication_status != "not_applicable":
                raise ValueError("run.terminalization pending/running bodies require no output publication")
        if self.output_publication_status in {"published", "failed", "pending"}:
            if self.world_disposition != "retained":
                raise ValueError("run.terminalization output publication status requires retained world disposition")
        elif self.output_publication_status == "not_applicable" and self.world_disposition == "retained":
            raise ValueError("retained run terminalization requires explicit output publication status")

    @property
    def retained_custody_ref(self) -> str | None:
        return None if self.retained_custody is None else self.retained_custody.custody_ref

    @property
    def retained_output_world_oid(self) -> str | None:
        return None if self.retained_custody is None else self.retained_custody.output_world_oid

    def to_json(self) -> JsonObject:
        return {
            "body_status": self.body_status,
            "world_disposition": self.world_disposition,
            "output_publication_status": self.output_publication_status,
            "retained_custody": None if self.retained_custody is None else self.retained_custody.to_json(),
            "publication_error": None if self.publication_error is None else dict(self.publication_error),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RunTerminalization:
        raw_custody = value.get("retained_custody")
        if raw_custody is not None and not isinstance(raw_custody, Mapping):
            raise TypeError("run terminalization retained_custody must be an object or null")
        return cls(
            body_status=_required_member(value, "body_status", _RUN_BODY_STATUSES),  # type: ignore[arg-type]
            world_disposition=_required_member(value, "world_disposition", _RUN_WORLD_DISPOSITIONS),  # type: ignore[arg-type]
            output_publication_status=_required_member(  # type: ignore[arg-type]
                value,
                "output_publication_status",
                _RUN_OUTPUT_PUBLICATION_STATUSES,
            ),
            retained_custody=None if raw_custody is None else RunRetainedCustody.from_json(raw_custody),
            publication_error=_optional_mapping_or_none(value, "publication_error"),
        )


@dataclass(frozen=True)
class TaskExecutionRecord:
    """Executor selection and outcome for one task artifact invocation."""

    execution_id: str
    run_ref: str
    executor_kind: TaskExecutorKind
    executor_id: str
    executor_policy: str
    call_kind: TaskExecutionCallKind
    status: TaskExecutionStatus
    task_lock: TaskArtifactLock
    started_at: str | None = None
    finished_at: str | None = None
    environment_ref: str | None = None
    resolution_id: str | None = None
    error: JsonObject | None = None
    metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name, value in (
            ("execution_id", self.execution_id),
            ("run_ref", self.run_ref),
            ("executor_id", self.executor_id),
            ("executor_policy", self.executor_policy),
        ):
            _require_non_empty_str(value, f"task_execution.{field_name}")
        if self.executor_kind not in _TASK_EXECUTOR_KINDS:
            raise ValueError(f"unsupported task execution executor kind: {self.executor_kind!r}")
        if self.call_kind not in _TASK_EXECUTION_CALL_KINDS:
            raise ValueError(f"unsupported task execution call kind: {self.call_kind!r}")
        if self.status not in _TASK_EXECUTION_STATUSES:
            raise ValueError(f"unsupported task execution status: {self.status!r}")
        if not isinstance(self.task_lock, TaskArtifactLock):
            raise TypeError("task_execution.task_lock must be TaskArtifactLock")
        for field_name, value in (
            ("started_at", self.started_at),
            ("finished_at", self.finished_at),
            ("environment_ref", self.environment_ref),
            ("resolution_id", self.resolution_id),
        ):
            _require_optional_str(value, f"task_execution.{field_name}")
        if self.error is not None and not isinstance(self.error, Mapping):
            raise TypeError("task_execution.error must be an object or None")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("task_execution.metadata must be an object")

    def to_json(self) -> JsonObject:
        return {
            "execution_id": self.execution_id,
            "run_ref": self.run_ref,
            "executor_kind": self.executor_kind,
            "executor_id": self.executor_id,
            "executor_policy": self.executor_policy,
            "call_kind": self.call_kind,
            "status": self.status,
            "task_lock": self.task_lock.to_json(),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "environment_ref": self.environment_ref,
            "resolution_id": self.resolution_id,
            "error": None if self.error is None else dict(self.error),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> TaskExecutionRecord:
        return cls(
            execution_id=_required_str(value, "execution_id"),
            run_ref=_required_str(value, "run_ref"),
            executor_kind=_required_member(value, "executor_kind", _TASK_EXECUTOR_KINDS),  # type: ignore[arg-type]
            executor_id=_required_str(value, "executor_id"),
            executor_policy=_required_str(value, "executor_policy"),
            call_kind=_required_member(value, "call_kind", _TASK_EXECUTION_CALL_KINDS),  # type: ignore[arg-type]
            status=_required_member(value, "status", _TASK_EXECUTION_STATUSES),  # type: ignore[arg-type]
            task_lock=TaskArtifactLock.from_json(_required_mapping(value, "task_lock")),
            started_at=_optional_str(value, "started_at"),
            finished_at=_optional_str(value, "finished_at"),
            environment_ref=_optional_str(value, "environment_ref"),
            resolution_id=_optional_str(value, "resolution_id"),
            error=_optional_mapping_or_none(value, "error"),
            metadata=dict(_optional_mapping(value, "metadata")),
        )


@dataclass(frozen=True)
class PendingEffectRef:
    """Recorded effect that may need later run-control attention."""

    effect_ref: str
    run_ref: str
    effect_type: str
    trace_ref: TraceRef | None = None
    handler_env_ref: str | None = None
    state: PendingEffectState = "recorded"

    def __post_init__(self) -> None:
        _require_non_empty_str(self.effect_ref, "pending_effect.effect_ref")
        _require_non_empty_str(self.run_ref, "pending_effect.run_ref")
        _require_non_empty_str(self.effect_type, "pending_effect.effect_type")
        if self.trace_ref is not None and not isinstance(self.trace_ref, TraceRef):
            raise TypeError("pending_effect.trace_ref must be TraceRef or None")
        _require_optional_str(self.handler_env_ref, "pending_effect.handler_env_ref")
        if self.state not in _PENDING_EFFECT_STATES:
            raise ValueError(f"unsupported pending effect state: {self.state!r}")

    def to_json(self) -> JsonObject:
        return {
            "effect_ref": self.effect_ref,
            "run_ref": self.run_ref,
            "effect_type": self.effect_type,
            "trace_ref": None if self.trace_ref is None else self.trace_ref.to_json(),
            "handler_env_ref": self.handler_env_ref,
            "state": self.state,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> PendingEffectRef:
        raw_trace = value.get("trace_ref")
        if raw_trace is not None and not isinstance(raw_trace, Mapping):
            raise TypeError("pending effect trace_ref must be an object or null")
        return cls(
            effect_ref=_required_str(value, "effect_ref"),
            run_ref=_required_str(value, "run_ref"),
            effect_type=_required_str(value, "effect_type"),
            trace_ref=None if raw_trace is None else TraceRef.from_json(raw_trace),
            handler_env_ref=_optional_str(value, "handler_env_ref"),
            state=_required_member(value, "state", _PENDING_EFFECT_STATES, default="recorded"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class RunRecord:
    """Durable run index/control record."""

    run_ref: str
    task_id: str
    task_version: str
    task_schema_digest: str
    args_digest: str
    may_profile: str
    provider: str
    status: RunStatus
    terminalization: RunTerminalization
    enforcement: RunEnforcement = "advisory"
    operation_refs: RunOperationRefs = field(default_factory=RunOperationRefs)
    authority_context: RunAuthorityContext | None = None
    execution_evidence: RunExecutionEvidence = field(default_factory=RunExecutionEvidence)
    proof: ProofEnvelope = field(default_factory=runtime_only_envelope)
    task_source_identity: str | None = None
    args_ref: str | None = None
    trace_ref: TraceRef | None = None
    input_workspace_world_oid: str | None = None
    # Terminal workspace world, not necessarily product-visible output. Use
    # run_workspace_output_world_oid() when publication visibility matters.
    terminal_workspace_world_oid: str | None = None
    outputs: Mapping[str, RunOutputCitationRef] = field(default_factory=dict)
    started_at: str | None = None
    finished_at: str | None = None
    parent_run_ref: str | None = None
    caused_by: str | None = None
    launch_context: RunLaunchContext = field(default_factory=RunLaunchContext)
    launch_context_ref: str | None = None
    handler_env_ref: str | None = None
    resolved_task_graph: ResolvedTaskGraph | None = None
    task_executions: tuple[TaskExecutionRecord, ...] = ()
    task_resolutions: tuple[TaskResolutionRecord, ...] = ()
    pending_effects: tuple[PendingEffectRef, ...] = ()
    error: JsonObject | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("run_ref", self.run_ref),
            ("task_id", self.task_id),
            ("task_version", self.task_version),
            ("task_schema_digest", self.task_schema_digest),
            ("args_digest", self.args_digest),
            ("may_profile", self.may_profile),
            ("provider", self.provider),
        ):
            _require_non_empty_str(value, f"run.{field_name}")
        if self.status not in _RUN_STATUSES:
            raise ValueError(f"unsupported run status: {self.status!r}")
        if self.enforcement not in _RUN_ENFORCEMENTS:
            raise ValueError(f"unsupported run enforcement: {self.enforcement!r}")
        if not isinstance(self.operation_refs, RunOperationRefs):
            raise TypeError("run.operation_refs must be RunOperationRefs")
        if self.authority_context is not None and not isinstance(self.authority_context, RunAuthorityContext):
            raise TypeError("run.authority_context must be RunAuthorityContext or None")
        if self.authority_context is None and self.provider.startswith("shepherd.workspace_control."):
            raise ValueError("workspace-control runs require run.authority_context")
        if self.authority_context is not None and self.may_profile != self.authority_context.effective_may:
            raise ValueError("run.may_profile must match run.authority_context.effective_may")
        if self.trace_ref is not None and not isinstance(self.trace_ref, TraceRef):
            raise TypeError("run.trace_ref must be TraceRef or None")
        if self.trace_ref is not None and self.trace_ref.run_id != self.run_ref:
            raise ValueError("run.trace_ref run_id must equal run.run_ref")
        if not isinstance(self.launch_context, RunLaunchContext):
            raise TypeError("run.launch_context must be RunLaunchContext")
        if not isinstance(self.execution_evidence, RunExecutionEvidence):
            raise TypeError("run.execution_evidence must be RunExecutionEvidence")
        if not isinstance(self.proof, ProofEnvelope):
            raise TypeError("run.proof must be ProofEnvelope")
        non_runtime_proof = not (
            self.proof.profile is ProofProfile.RUNTIME_ONLY and self.proof.strength is ProofStrength.RUNTIME_ONLY
        )
        if not non_runtime_proof and (
            self.proof.evidence_id is not None or self.proof.program_ref is not None or self.proof.trace_ref is not None
        ):
            raise ValueError("workspace-control runtime_only proof records must not carry proof evidence refs")
        _validate_run_enforcement_evidence(self.enforcement, self.execution_evidence)
        if (
            self.authority_context is not None
            and self.launch_context.may_profile is not None
            and self.launch_context.may_profile != self.authority_context.effective_may
        ):
            raise ValueError("run.launch_context.may_profile must match run.authority_context.effective_may")
        if self.resolved_task_graph is not None and not isinstance(self.resolved_task_graph, ResolvedTaskGraph):
            raise TypeError("run.resolved_task_graph must be ResolvedTaskGraph or None")
        terminalization = self.terminalization
        if not isinstance(terminalization, RunTerminalization):
            raise TypeError("run.terminalization must be RunTerminalization")
        for field_name, value in (
            ("task_source_identity", self.task_source_identity),
            ("args_ref", self.args_ref),
            ("input_workspace_world_oid", self.input_workspace_world_oid),
            ("terminal_workspace_world_oid", self.terminal_workspace_world_oid),
            ("started_at", self.started_at),
            ("finished_at", self.finished_at),
            ("parent_run_ref", self.parent_run_ref),
            ("caused_by", self.caused_by),
            ("launch_context_ref", self.launch_context_ref),
            ("handler_env_ref", self.handler_env_ref),
        ):
            _require_optional_str(value, f"run.{field_name}")
        if self.error is not None and not isinstance(self.error, Mapping):
            raise TypeError("run.error must be an object or None")
        if self.error is not None and self.error.get("stage") == "output_publication":
            raise ValueError("run publication errors belong in run.terminalization.publication_error")
        if (
            self.error is not None
            and self.error.get("stage") == "authority_terminalization"
            and terminalization.world_disposition != "discarded"
        ):
            raise ValueError("run authority terminalization errors require discarded world disposition")
        if (
            self.error is not None
            and terminalization.body_status == "completed"
            and self.error.get("stage") != "authority_terminalization"
        ):
            raise ValueError("run.error requires non-completed body status")
        if self.error is not None and self.status in {"pending", "running", "merged", "retained"}:
            raise ValueError("run.error requires failed, discarded, or cancelled run status")
        seen_output_ids: set[str] = set()
        for key, citation in self.outputs.items():
            _require_non_empty_str(key, "run.outputs key")
            if not isinstance(citation, RunOutputCitationRef):
                raise TypeError("run.outputs values must be RunOutputCitationRef")
            if key != citation.output_name:
                raise ValueError("run.outputs keys must match citation output_name")
            if citation.output_id in seen_output_ids:
                raise ValueError(f"duplicate run output id: {citation.output_id!r}")
            seen_output_ids.add(citation.output_id)
            if self.trace_ref is None:
                raise ValueError("run output citation trace_ref requires run.trace_ref")
            if citation.trace_ref != self.trace_ref:
                raise ValueError("run output citation trace_ref disagrees with run.trace_ref")
        workspace_output = self.outputs.get("workspace")
        if workspace_output is not None and self.terminal_workspace_world_oid != workspace_output.output_world_oid:
            raise ValueError("run.terminal_workspace_world_oid disagrees with workspace output citation")
        if not isinstance(self.task_executions, tuple):
            raise TypeError("run.task_executions must be a tuple")
        if not isinstance(self.task_resolutions, tuple):
            raise TypeError("run.task_resolutions must be a tuple")
        if not isinstance(self.pending_effects, tuple):
            raise TypeError("run.pending_effects must be a tuple")
        seen_execution_ids: set[str] = set()
        for execution in self.task_executions:
            if not isinstance(execution, TaskExecutionRecord):
                raise TypeError("run.task_executions values must be TaskExecutionRecord")
            if execution.run_ref != self.run_ref:
                raise ValueError("run task execution run_ref disagrees with run.run_ref")
            if execution.execution_id in seen_execution_ids:
                raise ValueError(f"duplicate task execution id: {execution.execution_id!r}")
            seen_execution_ids.add(execution.execution_id)
        seen_resolution_ids: set[str] = set()
        for resolution in self.task_resolutions:
            if not isinstance(resolution, TaskResolutionRecord):
                raise TypeError("run.task_resolutions values must be TaskResolutionRecord")
            if resolution.resolution_id in seen_resolution_ids:
                raise ValueError(f"duplicate task resolution id: {resolution.resolution_id!r}")
            seen_resolution_ids.add(resolution.resolution_id)
        for effect in self.pending_effects:
            if not isinstance(effect, PendingEffectRef):
                raise TypeError("run.pending_effects values must be PendingEffectRef")
            if effect.run_ref != self.run_ref:
                raise ValueError("run pending effect run_ref disagrees with run.run_ref")
            if effect.trace_ref is not None and self.trace_ref is None:
                raise ValueError("run pending effect trace_ref requires run.trace_ref")
            if effect.trace_ref is not None and effect.trace_ref != self.trace_ref:
                raise ValueError("run pending effect trace_ref disagrees with run.trace_ref")
        _validate_run_terminalization(
            status=self.status,
            terminalization=terminalization,
            outputs=self.outputs,
            terminal_workspace_world_oid=self.terminal_workspace_world_oid,
        )
        if non_runtime_proof:
            try:
                validate_vcscore_run_proof_envelope(self.to_json(), self.proof)
            except VcsCoreCertificateError as exc:
                raise ValueError(
                    "workspace-control run records must carry runtime_only or matching VcsCore certificate proof status"
                ) from exc

    def to_json(self) -> JsonObject:
        return {
            "run_ref": self.run_ref,
            "task_id": self.task_id,
            "task_version": self.task_version,
            "task_schema_digest": self.task_schema_digest,
            "task_source_identity": self.task_source_identity,
            "args_digest": self.args_digest,
            "args_ref": self.args_ref,
            "may_profile": self.may_profile,
            "authority_context": None if self.authority_context is None else self.authority_context.to_json(),
            "provider": self.provider,
            "enforcement": self.enforcement,
            "execution_evidence": self.execution_evidence.to_json(),
            "proof": self.proof.to_json(),
            "status": self.status,
            "trace_ref": None if self.trace_ref is None else self.trace_ref.to_json(),
            "operation_refs": self.operation_refs.to_json(),
            "input_workspace_world_oid": self.input_workspace_world_oid,
            "terminal_workspace_world_oid": self.terminal_workspace_world_oid,
            "outputs": {name: citation.to_json() for name, citation in self.outputs.items()},
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "parent_run_ref": self.parent_run_ref,
            "caused_by": self.caused_by,
            "launch_context": self.launch_context.to_json(),
            "launch_context_ref": self.launch_context_ref,
            "handler_env_ref": self.handler_env_ref,
            "resolved_task_graph": None if self.resolved_task_graph is None else self.resolved_task_graph.to_json(),
            "task_executions": [execution.to_json() for execution in self.task_executions],
            "task_resolutions": [resolution.to_json() for resolution in self.task_resolutions],
            "pending_effects": [effect.to_json() for effect in self.pending_effects],
            "error": None if self.error is None else dict(self.error),
            "terminalization": self.terminalization.to_json(),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RunRecord:
        raw_trace = value.get("trace_ref")
        if raw_trace is not None and not isinstance(raw_trace, Mapping):
            raise TypeError("run trace_ref must be an object or null")
        raw_terminalization = value.get("terminalization")
        if not isinstance(raw_terminalization, Mapping):
            raise TypeError("run terminalization must be an object")
        if "workspace_output_world_oid" in value:
            raise ValueError("run workspace_output_world_oid is unsupported; use terminal_workspace_world_oid")
        return cls(
            run_ref=_required_str(value, "run_ref"),
            task_id=_required_str(value, "task_id"),
            task_version=_required_str(value, "task_version"),
            task_schema_digest=_required_str(value, "task_schema_digest"),
            task_source_identity=_optional_str(value, "task_source_identity"),
            args_digest=_required_str(value, "args_digest"),
            args_ref=_optional_str(value, "args_ref"),
            may_profile=_required_str(value, "may_profile"),
            authority_context=_optional_run_authority_context(value, "authority_context"),
            provider=_required_str(value, "provider"),
            enforcement=_required_member(value, "enforcement", _RUN_ENFORCEMENTS, default="advisory"),  # type: ignore[arg-type]
            execution_evidence=RunExecutionEvidence.from_json(_optional_mapping_or_none(value, "execution_evidence")),
            proof=_optional_proof_envelope(value, "proof"),
            status=_required_member(value, "status", _RUN_STATUSES),  # type: ignore[arg-type]
            trace_ref=None if raw_trace is None else TraceRef.from_json(raw_trace),
            operation_refs=RunOperationRefs.from_json(_optional_mapping_or_none(value, "operation_refs")),
            input_workspace_world_oid=_optional_str(value, "input_workspace_world_oid"),
            terminal_workspace_world_oid=_optional_str(value, "terminal_workspace_world_oid"),
            outputs=_output_map(value.get("outputs")),
            started_at=_optional_str(value, "started_at"),
            finished_at=_optional_str(value, "finished_at"),
            parent_run_ref=_optional_str(value, "parent_run_ref"),
            caused_by=_optional_str(value, "caused_by"),
            launch_context=RunLaunchContext.from_json(_optional_mapping_or_none(value, "launch_context")),
            launch_context_ref=_optional_str(value, "launch_context_ref"),
            handler_env_ref=_optional_str(value, "handler_env_ref"),
            resolved_task_graph=_optional_resolved_task_graph(value, "resolved_task_graph"),
            task_executions=_task_execution_tuple(value.get("task_executions")),
            task_resolutions=_task_resolution_tuple(value.get("task_resolutions")),
            pending_effects=_pending_effect_tuple(value.get("pending_effects")),
            error=_optional_mapping_or_none(value, "error"),
            terminalization=RunTerminalization.from_json(raw_terminalization),
        )

    def summary(self) -> RunSummary:
        return RunSummary(
            run_ref=self.run_ref,
            task_id=self.task_id,
            task_version=self.task_version,
            status=self.status,
            started_at=self.started_at,
            finished_at=self.finished_at,
            parent_run_ref=self.parent_run_ref,
        )


def _validate_run_terminalization(
    *,
    status: RunStatus,
    terminalization: RunTerminalization,
    outputs: Mapping[str, RunOutputCitationRef],
    terminal_workspace_world_oid: str | None,
) -> None:
    if status == "retained":
        if terminalization.world_disposition != "retained":
            raise ValueError("retained run status requires retained terminalization")
    elif terminalization.world_disposition == "retained":
        raise ValueError("retained terminalization requires run status 'retained'")
    if status == "merged" and terminalization.world_disposition != "merged":
        raise ValueError("merged run status requires merged terminalization")
    if status in {"pending", "running"} and terminalization.world_disposition != "none":
        raise ValueError("non-terminal run status requires no world disposition")
    if status in {"failed", "discarded", "cancelled"} and terminalization.world_disposition == "merged":
        raise ValueError("failed/discarded run status must not carry merged terminalization")
    if status not in {"pending", "running"} and terminalization.body_status in {"pending", "running"}:
        raise ValueError("terminal run status must not carry pending/running body status")
    if status == "pending" and terminalization.body_status != "pending":
        raise ValueError("pending run status requires pending body status")
    if status == "running" and terminalization.body_status != "running":
        raise ValueError("running run status requires running body status")
    if terminal_workspace_world_oid is not None and terminalization.world_disposition not in {"merged", "retained"}:
        raise ValueError("run terminal workspace world oid requires merged or retained world disposition")
    if terminalization.world_disposition != "retained":
        if outputs:
            raise ValueError("run outputs require retained published terminalization")
        return
    if terminalization.retained_custody is None:
        raise ValueError("retained terminalization requires retained custody")
    if (
        terminal_workspace_world_oid is not None
        and terminal_workspace_world_oid != terminalization.retained_output_world_oid
    ):
        raise ValueError("run terminal workspace world oid disagrees with retained terminalization")
    if terminalization.output_publication_status != "published":
        if outputs:
            raise ValueError("unpublished retained output terminalization must not carry output citations")
        return
    _validate_published_retained_workspace_output(
        outputs,
        terminalization=terminalization,
        terminal_workspace_world_oid=terminal_workspace_world_oid,
    )


def _validate_published_retained_workspace_output(
    outputs: Mapping[str, RunOutputCitationRef],
    *,
    terminalization: RunTerminalization,
    terminal_workspace_world_oid: str | None,
) -> None:
    if set(outputs) != {"workspace"}:
        raise ValueError("published retained output terminalization requires exactly one workspace output citation")
    citation = outputs["workspace"]
    custody = terminalization.retained_custody
    if custody is None:
        raise ValueError("published retained output terminalization requires retained custody")
    if citation.binding != "workspace":
        raise ValueError("published retained workspace output citation requires workspace binding")
    if custody.custody_ref != citation.custody_ref:
        raise ValueError("run terminalization retained custody ref disagrees with output citation")
    if custody.output_world_oid != citation.output_world_oid:
        raise ValueError("run terminalization retained output world oid disagrees with output citation")
    if custody.binding != citation.binding:
        raise ValueError("run terminalization retained custody binding disagrees with output citation")
    if custody.store_id != citation.store_id:
        raise ValueError("run terminalization retained custody store_id disagrees with output citation")
    if custody.resource_id != citation.resource_id:
        raise ValueError("run terminalization retained custody resource_id disagrees with output citation")
    if custody.parent_basis_world_oid != citation.parent_basis_world_oid:
        raise ValueError("run terminalization retained custody parent basis disagrees with output citation")
    if terminal_workspace_world_oid != citation.output_world_oid:
        raise ValueError("run terminal workspace world oid disagrees with output citation")


def run_trace_terminal_status(record: RunRecord) -> str:
    """Project one run record to the trace lifecycle terminal string."""
    terminalization = record.terminalization
    if terminalization.world_disposition in {"merged", "retained", "discarded"}:
        return terminalization.world_disposition
    if terminalization.body_status == "refused":
        return "refused"
    if terminalization.body_status in {"pending", "running"}:
        return terminalization.body_status
    if record.status == "cancelled":
        return "cancelled"
    return "failed"


def run_published_workspace_output(record: RunRecord) -> RunOutputCitationRef | None:
    """Return the published workspace output citation for a retained run, if any."""
    terminalization = record.terminalization
    if terminalization.world_disposition != "retained":
        return None
    if terminalization.output_publication_status != "published":
        return None
    return record.outputs.get("workspace")


def run_has_published_workspace_output(record: RunRecord) -> bool:
    """Whether the run exposes a product-visible workspace output citation."""
    return run_published_workspace_output(record) is not None


def run_workspace_output_world_oid(record: RunRecord) -> str | None:
    """Return the product-visible workspace output world id, if publication completed."""
    citation = run_published_workspace_output(record)
    return None if citation is None else citation.output_world_oid


def run_can_produce_source_identity(record: RunRecord, world_oid: str) -> bool:
    """Whether the run can justify a source identity for the given world."""
    if not isinstance(world_oid, str) or not world_oid:
        return False
    return run_workspace_output_world_oid(record) == world_oid


def _error_str(error: Mapping[str, object], field_name: str) -> str:
    value = error.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"run terminalization publication error requires {field_name}")
    return value


def _validate_terminalization_publication_error(terminalization: RunTerminalization) -> None:
    error = terminalization.publication_error
    if error is None:
        raise ValueError("run.terminalization publication_error is required when publication failed")
    if error.get("stage") != "output_publication":
        raise ValueError("run.terminalization publication_error stage must be output_publication")
    if terminalization.retained_custody is None:
        raise ValueError("run.terminalization publication_error requires retained custody")
    _require_publication_error_field(
        error,
        "retained_custody_ref",
        terminalization.retained_custody_ref,
    )
    _require_publication_error_field(
        error,
        "retained_output_world_oid",
        terminalization.retained_output_world_oid,
    )


def _require_publication_error_field(
    error: Mapping[str, object],
    field_name: str,
    expected: str | None,
) -> None:
    actual = _error_str(error, field_name)
    if actual != expected:
        raise ValueError(f"run.terminalization publication_error {field_name} disagrees with retained custody")


@dataclass(frozen=True)
class RunSummary:
    """Compact run row for list views."""

    run_ref: str
    task_id: str
    task_version: str
    status: RunStatus
    started_at: str | None = None
    finished_at: str | None = None
    parent_run_ref: str | None = None

    def to_json(self) -> JsonObject:
        return {
            "run_ref": self.run_ref,
            "task_id": self.task_id,
            "task_version": self.task_version,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "parent_run_ref": self.parent_run_ref,
        }


def _required_str(value: Mapping[str, object], field_name: str) -> str:
    raw = value.get(field_name)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{field_name} must be a non-empty string")
    return raw


def _required_attr_str(value: Any, field_name: str, label: str) -> str:
    raw = getattr(value, field_name, None)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{label} field {field_name!r} must be a non-empty string")
    return raw


def _optional_str(value: Mapping[str, object], field_name: str) -> str | None:
    raw = value.get(field_name)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{field_name} must be null or a non-empty string")
    return raw


def _required_bool(value: Mapping[str, object], field_name: str) -> bool:
    raw = value.get(field_name)
    if not isinstance(raw, bool):
        raise TypeError(f"{field_name} must be a boolean")
    return raw


def _require_non_empty_str(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_optional_str(value: object, field_name: str) -> None:
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(f"{field_name} must be null or a non-empty string")


def _validated_descriptor_locator_payload(
    value: object,
    *,
    output_name: str,
    trace_ref: TraceRef,
) -> JsonObject:
    if not isinstance(value, Mapping) or not value:
        raise ValueError("run_output.descriptor_locator must be a non-empty object")
    locator = run_output_descriptor_locator_from_payload(dict(value))
    if locator.output_name != output_name:
        raise ValueError("run_output.descriptor_locator output_name disagrees with output_name")
    if locator.execution_id != trace_ref.execution_id:
        raise ValueError("run_output.descriptor_locator execution_id disagrees with trace_ref")
    if locator.frontier_id != trace_ref.frontier_id:
        raise ValueError("run_output.descriptor_locator frontier_id disagrees with trace_ref")
    return dict(run_output_descriptor_locator_payload(locator))


def _required_mapping(value: Mapping[str, object], field_name: str) -> Mapping[str, object]:
    raw = value.get(field_name)
    if not isinstance(raw, Mapping):
        raise TypeError(f"{field_name} must be an object")
    return raw


def _optional_mapping(value: Mapping[str, object], field_name: str) -> Mapping[str, object]:
    raw = value.get(field_name, {})
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise TypeError(f"{field_name} must be an object")
    return raw


def _optional_mapping_or_none(value: Mapping[str, object], field_name: str) -> JsonObject | None:
    raw = value.get(field_name)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise TypeError(f"{field_name} must be an object or null")
    return dict(raw)


def _optional_proof_envelope(value: Mapping[str, object], field_name: str) -> ProofEnvelope:
    raw = value.get(field_name)
    if raw is None:
        return runtime_only_envelope()
    if not isinstance(raw, Mapping):
        raise TypeError(f"{field_name} must be an object or null")
    return proof_envelope_from_json(raw)


def _optional_task_artifact_ref(value: Mapping[str, object], field_name: str) -> TaskArtifactRef | None:
    raw = value.get(field_name)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise TypeError(f"{field_name} must be an object or null")
    return TaskArtifactRef.from_json(raw)


def _optional_resolved_task_graph(value: Mapping[str, object], field_name: str) -> ResolvedTaskGraph | None:
    raw = value.get(field_name)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise TypeError(f"{field_name} must be an object or null")
    return ResolvedTaskGraph.from_json(raw)


def _optional_run_authority_context(value: Mapping[str, object], field_name: str) -> RunAuthorityContext | None:
    raw = value.get(field_name)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise TypeError(f"{field_name} must be an object or null")
    return RunAuthorityContext.from_json(raw)


def _declared_dependency_map(value: object) -> dict[str, DeclaredTaskDependency]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("declared_dependencies must be an object")
    out: dict[str, DeclaredTaskDependency] = {}
    for alias, raw in value.items():
        if not isinstance(alias, str) or not alias:
            raise ValueError("declared dependency aliases must be non-empty strings")
        if isinstance(raw, str):
            out[alias] = DeclaredTaskDependency(task_id=raw)
            continue
        if not isinstance(raw, Mapping):
            raise TypeError("declared dependency values must be objects")
        out[alias] = DeclaredTaskDependency.from_json(raw)
    return out


def _required_member(
    value: Mapping[str, object],
    field_name: str,
    allowed: frozenset[str] | set[str],
    *,
    default: str | None = None,
) -> Any:
    raw = value.get(field_name, default)
    if not isinstance(raw, str) or raw not in allowed:
        raise ValueError(f"{field_name} must be one of {sorted(allowed)!r}")
    return raw


def _str_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise TypeError(f"{field_name} must be a list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError(f"{field_name} entries must be non-empty strings")
        out.append(item)
    return tuple(out)


def _require_str_tuple(value: tuple[str, ...], field_name: str) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    for item in value:
        _require_non_empty_str(item, f"{field_name} entry")


def _output_map(value: object) -> dict[str, RunOutputCitationRef]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("outputs must be an object")
    out: dict[str, RunOutputCitationRef] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError("outputs keys must be non-empty strings")
        if not isinstance(raw, Mapping):
            raise TypeError("outputs values must be objects")
        citation = RunOutputCitationRef.from_json(raw)
        if key != citation.output_name:
            raise ValueError("outputs keys must match citation output_name")
        out[key] = citation
    return out


def _pending_effect_tuple(value: object) -> tuple[PendingEffectRef, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise TypeError("pending_effects must be a list")
    out: list[PendingEffectRef] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise TypeError("pending_effects entries must be objects")
        out.append(PendingEffectRef.from_json(raw))
    return tuple(out)


def _task_execution_tuple(value: object) -> tuple[TaskExecutionRecord, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise TypeError("task_executions must be a list")
    out: list[TaskExecutionRecord] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise TypeError("task_executions entries must be objects")
        out.append(TaskExecutionRecord.from_json(raw))
    return tuple(out)


def _task_resolution_tuple(value: object) -> tuple[TaskResolutionRecord, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise TypeError("task_resolutions must be a list")
    out: list[TaskResolutionRecord] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise TypeError("task_resolutions entries must be objects")
        out.append(TaskResolutionRecord.from_json(raw))
    return tuple(out)
