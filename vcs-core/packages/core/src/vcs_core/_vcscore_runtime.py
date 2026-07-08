"""VcsCore run/runtime execution — FENCED for P3 (see P3-forward-exploration.md).

Deliberately **not** retired into a no-`owner` collaborator (V3.4, reclassified from
extract to fence, D-C rev i). Its run orchestration composes VcsCore's own top-level
public verbs — `owner.fork`/`merge`/`merge_with_authority`/`discard`/`seal`/`world_oid`
(the run loop, ~L900-L1160) — so it is orchestration that belongs at the facade like
`_vcscore_lifecycle`, not a decoupled service. Extracting it would inject ~16 owner
methods for no decoupling gain. A `RunController(owner)` *tidy* (real class holding
`owner`, like `MaterializationController`) is an optional future follow-up; it is not
part of P3's claim.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from vcs_core._command_admission import admit_command_invocation
from vcs_core._command_contract import CommandContract, CommandContractError, normalize_command_params
from vcs_core._command_envelope import CommandEnvelopeError, CommandExecutionOptions, validate_command_execution_options
from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._execution_capability import (
    ExecutionBoundDriver,
    ExecutionCapability,
    detect_containment_backend,
)
from vcs_core._fork_hints import ForkHints
from vcs_core._python_runtime_capture_adapter import (
    PYTHON_RUNTIME_EFFECT_TYPE,
)
from vcs_core._runtime_types import ExecutionContext
from vcs_core._schema_errors import SchemaValidationError
from vcs_core._substrate_driver import CapabilitySet, CommandRequest, DriverContext, DriverSchema, SubstrateDriver
from vcs_core._world_transition_coordinator import dispatch_driver
from vcs_core.recording import NestedParentAuthorization
from vcs_core.types import (
    AuthorityExecutionOutcome,
    BoundSubstrate,
    EffectRecord,
    RecordedCommandOutcome,
    ScopeInfo,
    SealedExecutionOutcome,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from vcs_core._authority import DecisionProvider
    from vcs_core._binding_contracts import ResolvedDriverBinding
    from vcs_core._command_values import CommandValueSource
    from vcs_core._runtime_types import OperationRefInfo, RuntimeContext
    from vcs_core._world_types import SubstrateHead
    from vcs_core.vcscore import VcsCore


def record_runtime_effects(
    owner: VcsCore,
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
    if not effects:
        return []
    if (
        substrate == "filesystem"
        and workspace_output_binding != "workspace"
        and any(effect.workspace_changes for effect in effects)
    ):
        raise InvalidRepositoryStateError(
            f"filesystem runtime effects can only publish the workspace binding; got {workspace_output_binding!r}."
        )
    with owner._lock:
        effective_scope = owner._pipeline.require_world(scope)
        owner._validate_scope(effective_scope)
        effective_scope = owner._live_scope(effective_scope)
        owner._ensure_runtime_mutation_allowed(
            f"record {substrate} runtime effects",
            scope_selector=effective_scope.ref,
        )
        preexisting_operation = owner._pipeline.current_operation()
        selected_operation_id: str | None = None
        nested_parent = _boundary_nested_admission(
            owner,
            scope=effective_scope,
            boundary_policy=boundary_policy,
        )
        with (
            _boundary_scope_context(owner, effective_scope, nested_parent),
            runtime_operation_boundary(
                owner,
                scope=effective_scope,
                boundary_policy=boundary_policy,
                default_label=operation_label or f"{substrate}-runtime",
                default_kind=operation_kind or f"{substrate}.runtime",
                operation_id=operation_id,
                operation_kind=operation_kind,
                operation_label=operation_label,
                operation_metadata=operation_metadata,
                nested_parent=nested_parent,
            ) as operation,
        ):
            oids = owner._pipeline.record(effects, substrate=substrate, scope=effective_scope)
            if operation is not None and preexisting_operation is None:
                selected_operation_id = operation.durable_id
            elif operation is not None:
                owner._queue_workspace_state_for_runtime_effects(
                    effects,
                    substrate=substrate,
                    scope=effective_scope,
                    operation_id=operation.durable_id,
                    driver_command=workspace_driver_command or "python-runtime-capture",
                    workspace_output_binding=workspace_output_binding,
                    workspace_effect_overlay=workspace_effect_overlay,
                )
        if selected_operation_id is not None:
            owner._select_workspace_state_for_runtime_effects(
                effects,
                substrate=substrate,
                scope=effective_scope,
                operation_id=selected_operation_id,
                driver_command=workspace_driver_command or "python-runtime-capture",
                workspace_output_binding=workspace_output_binding,
                workspace_effect_overlay=workspace_effect_overlay,
            )
        return oids


def python_runtime_events_from_effects(
    effects: Sequence[EffectRecord],
    *,
    command_operation_id: str,
    binding_name: str = "workspace",
) -> tuple[dict[str, object], ...]:
    """Translate ``EffectRecord.workspace_changes`` into python-runtime raw event dicts.

    The patch manager intercepts Python ``open()`` / ``os.remove()`` / etc.
    calls and produces ``EffectRecord`` values with
    ``workspace_changes: tuple[WorkspaceChange, ...]``. T2c routes these
    through ``PythonRuntimeCaptureAdapter`` rather than the legacy
    ``driver_command="scan"`` dispatch; this helper builds the raw event
    dicts the adapter consumes.

    One workspace change becomes one event. ``content=None`` is a delete;
    otherwise a write with a sha256 content_digest. Mode defaults to
    0o100644 when not present on the change tuple.
    """
    events: list[dict[str, object]] = []
    global_seq = 0
    for effect in effects:
        for change in effect.workspace_changes:
            global_seq += 1
            path = change[0]
            content = change[1] if len(change) > 1 else None
            mode = change[2] if len(change) > 2 else 0o100644

            if content is None:
                event: dict[str, object] = {
                    "type": PYTHON_RUNTIME_EFFECT_TYPE,
                    "op": "delete",
                    "path": path,
                    "command_operation_id": command_operation_id,
                    "binding_name": binding_name,
                    "global_seq": global_seq,
                }
            else:
                content_digest = "sha256:" + hashlib.sha256(content).hexdigest()
                event = {
                    "type": PYTHON_RUNTIME_EFFECT_TYPE,
                    "op": "write",
                    "path": path,
                    "content_digest": content_digest,
                    "mode": mode,
                    "command_operation_id": command_operation_id,
                    "binding_name": binding_name,
                    "global_seq": global_seq,
                }
            events.append(event)
    return tuple(events)


def build_operation_metadata(
    owner: VcsCore,
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
    parent_operation = owner._pipeline.current_operation()
    if (
        parent_operation is not None
        and parent_operation.scope_ref != scope.ref
        and (
            nested_parent is None
            or not nested_parent.admits(parent_scope_ref=parent_operation.scope_ref, child_scope_ref=scope.ref)
        )
    ):
        msg = (
            f"Active operation handle {parent_operation.handle_id!r} belongs to "
            f"{parent_operation.scope_ref}, not {scope.ref}."
        )
        raise RuntimeError(msg)

    if operation_id is not None and owner._store.operation_id_exists(operation_id):
        raise ValueError(f"Operation id {operation_id!r} is already present in repository history.")

    op_label = operation_label or default_label
    handle_id = operation_id or default_handle_id(op_label)
    durable_operation_id = operation_id or new_operation_id()
    op_kind = operation_kind or default_kind
    op_metadata = dict(operation_metadata or {})
    return handle_id, op_kind, durable_operation_id, owner._session_id, op_metadata


def _consume_world_disposition(
    op_metadata: dict[str, object],
    *,
    nested_parent: NestedParentAuthorization | None,
) -> str | None:
    if "nested" in op_metadata:
        raise ValueError("Reserved nested operation metadata is store-owned; remove 'nested'.")
    raw = op_metadata.pop("world_disposition", None)
    if raw is None:
        return "adopt" if nested_parent is not None else None
    if nested_parent is None:
        raise ValueError("world_disposition is only valid for nested runtime operations.")
    if raw not in {"adopt", "release"}:
        raise ValueError("world_disposition must be 'adopt' or 'release'.")
    return str(raw)


def _nested_operations_enabled() -> bool:
    """Whether nested sub-task operations are enabled (experimental; default OFF).

    Allows a runtime operation to nest on a DESCENDANT scope of an active operation; the
    same-scope invariant holds unless VCS_CORE_NESTED_OPERATIONS is set.
    """
    return os.environ.get("VCS_CORE_NESTED_OPERATIONS", "").strip().lower() in {"1", "true", "yes", "on"}


def _ancestry_chain(owner: VcsCore, ancestor_ref: str, descendant: ScopeInfo) -> tuple[str, ...] | None:
    """The proof chain for nested-operation authorization.

    Scope refs from ``descendant``'s parent up to (and including)
    ``ancestor_ref``, or None if ``ancestor_ref`` is not an ancestor.
    Walks owner._scope_parents; a scope has <=1 live child, so this is a linear chain.
    """
    parents = getattr(owner, "_scope_parents", {})
    seen: set[str] = set()
    chain: list[str] = []
    parent = parents.get(descendant.name)
    while parent is not None and parent.ref not in seen:
        chain.append(parent.ref)
        if parent.ref == ancestor_ref:
            return tuple(chain)
        seen.add(parent.ref)
        parent = parents.get(parent.name)
    return None


def _scope_info_for_ref(owner: VcsCore, scope_ref: str) -> ScopeInfo | None:
    ground = cast("ScopeInfo | None", getattr(owner, "_ground", None))
    if ground is not None and ground.ref == scope_ref:
        return ground
    active_scopes = cast("Mapping[str, ScopeInfo]", getattr(owner, "_active_scopes", {}))
    for scope in active_scopes.values():
        if scope.ref == scope_ref:
            return scope
    scope_parents = cast("Mapping[str, ScopeInfo]", getattr(owner, "_scope_parents", {}))
    for scope in scope_parents.values():
        if scope.ref == scope_ref:
            return scope
    return None


def _nested_admission_authorizations(owner: VcsCore | None, scope_ref: str) -> tuple[NestedParentAuthorization, ...]:
    if owner is None or not _nested_operations_enabled():
        return ()
    scope = _scope_info_for_ref(owner, scope_ref)
    if scope is None:
        return ()
    parents = getattr(owner, "_scope_parents", {})
    authorizations: list[NestedParentAuthorization] = []
    chain: list[str] = []
    parent = parents.get(scope.name)
    seen: set[str] = set()
    while parent is not None and parent.ref not in seen:
        chain.append(parent.ref)
        authorizations.append(
            NestedParentAuthorization(
                parent_scope_ref=parent.ref,
                child_scope_ref=scope.ref,
                ancestry_chain=tuple(chain),
            )
        )
        seen.add(parent.ref)
        parent = parents.get(parent.name)
    return tuple(authorizations)


def _nested_parent_authorization(owner: VcsCore, scope: ScopeInfo) -> NestedParentAuthorization | None:
    """The proof-carrying authorization for nesting an operation on ``scope``, or None.

    Under the experimental flag, a parent operation may nest on a DESCENDANT scope —
    the returned authorization names the live (parent, child) pair plus the ancestry
    chain that proved it, and ``begin_operation`` re-checks the pair. Same-scope or
    flag-off returns None (no behavior change).
    """
    if not _nested_operations_enabled():
        return None
    current = owner._pipeline.current_operation()
    if current is None or current.scope_ref == scope.ref:
        return None
    chain = _ancestry_chain(owner, current.scope_ref, scope)
    if chain is None:
        return None
    return NestedParentAuthorization(
        parent_scope_ref=current.scope_ref,
        child_scope_ref=scope.ref,
        ancestry_chain=chain,
    )


def _runtime_record_class(owner: VcsCore, binding_name: str) -> str | None:
    if not _nested_operations_enabled():
        return None
    try:
        binding = owner._resolve_binding(binding_name)
    except ValueError:
        return None
    driver = binding.instance
    if not isinstance(driver, SubstrateDriver):
        return None
    capabilities = getattr(driver, "capabilities", None)
    selectable = capabilities is not None and getattr(capabilities, "selectable", False)
    if selectable and getattr(driver, "lifecycle_class", None) == "evidence":
        return "trace_evidence"
    return None


def _runtime_admission_context(
    owner: VcsCore,
    *,
    scope_ref: str,
    record_class: str | None = None,
    allowed_blocker_item_ids: tuple[str, ...] = (),
) -> Any | None:
    if not _nested_operations_enabled() and record_class is None and not allowed_blocker_item_ids:
        return None
    from vcs_core._query_readiness import RuntimeAdmissionContext

    return RuntimeAdmissionContext(
        record_class=record_class,
        nested_authorizations=_nested_admission_authorizations(owner, scope_ref),
        allowed_blocker_item_ids=allowed_blocker_item_ids,
    )


def _boundary_refusal(current: OperationRefInfo, scope: ScopeInfo) -> RuntimeError:
    msg = f"Active operation handle {current.handle_id!r} belongs to {current.scope_ref}, not {scope.ref}."
    return RuntimeError(msg)


def _boundary_nested_admission(
    owner: VcsCore,
    *,
    scope: ScopeInfo,
    boundary_policy: str,
    nested_parent: NestedParentAuthorization | None = None,
) -> NestedParentAuthorization | None:
    current = owner._pipeline.current_operation()
    if current is None or current.scope_ref == scope.ref:
        return None
    if boundary_policy == "append_or_root":
        raise _boundary_refusal(current, scope)
    authorization = nested_parent or _nested_parent_authorization(owner, scope)
    if authorization is None or not authorization.admits(
        parent_scope_ref=current.scope_ref,
        child_scope_ref=scope.ref,
    ):
        raise _boundary_refusal(current, scope)
    return authorization


@contextmanager
def _boundary_scope_context(
    owner: VcsCore,
    scope: ScopeInfo,
    nested_parent: NestedParentAuthorization | None,
) -> Iterator[RuntimeContext | None]:
    if nested_parent is None:
        with owner._scoped(scope):
            yield None
        return
    previous_context = owner._pipeline.context
    try:
        yield previous_context
    finally:
        owner._pipeline.set_context(previous_context)


@contextmanager
def opened_runtime_operation(
    owner: VcsCore,
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
    nested_parent = nested_parent or _nested_parent_authorization(owner, scope)
    previous_context = owner._pipeline.context if nested_parent is not None else None
    handle_id, op_kind, durable_operation_id, session_id, op_metadata = build_operation_metadata(
        owner,
        scope=scope,
        default_label=default_label,
        default_kind=default_kind,
        operation_id=operation_id,
        operation_kind=operation_kind,
        operation_label=operation_label,
        operation_metadata=operation_metadata,
        nested_parent=nested_parent,
    )
    world_disposition = _consume_world_disposition(op_metadata, nested_parent=nested_parent)
    operation = owner._pipeline.begin_operation(
        handle_id=handle_id,
        kind=op_kind,
        operation_id=durable_operation_id,
        operation_label=operation_label or default_label,
        session_id=session_id,
        metadata=op_metadata,
        scope=scope,
        nested_parent=nested_parent,
        world_disposition=world_disposition,
    )
    try:
        yield operation
    except BaseException as exc:
        try:
            if failure_policy == "abort_archive":
                owner._pipeline.abort_operation(handle_id=operation.handle_id, metadata=op_metadata)
            elif failure_policy == "complete_error":
                owner._pipeline.end_operation(
                    handle_id=operation.handle_id,
                    scope=scope,
                    metadata={
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                    status="error",
                )
            else:
                raise ValueError(f"Unknown runtime failure policy: {failure_policy!r}") from exc
        except Exception:
            if failure_policy not in {"abort_archive", "complete_error"}:
                raise
        finally:
            if previous_context is not None:
                owner._pipeline.set_context(previous_context)
        raise
    else:
        owner._pipeline.end_operation(handle_id=operation.handle_id, scope=scope, metadata={})
        if previous_context is not None:
            owner._pipeline.set_context(previous_context)
        owner._flush_workspace_state_for_runtime_operation(operation.durable_id)


@contextmanager
def runtime_operation_boundary(
    owner: VcsCore,
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
    current_operation = owner._pipeline.current_operation()
    nested_parent = _boundary_nested_admission(
        owner,
        scope=scope,
        boundary_policy=boundary_policy,
        nested_parent=nested_parent,
    )
    if current_operation is not None and current_operation.scope_ref != scope.ref:
        if nested_parent is not None:
            current_operation = None
        else:
            msg = (
                f"Active operation handle {current_operation.handle_id!r} belongs to "
                f"{current_operation.scope_ref}, not {scope.ref}."
            )
            raise RuntimeError(msg)
    if failure_policy not in {"abort_archive", "complete_error"}:
        raise ValueError(f"Unknown runtime failure policy: {failure_policy!r}")
    if failure_policy == "complete_error" and current_operation is not None:
        raise RuntimeError("failure_policy='complete_error' requires a root runtime activity.")

    if boundary_policy == "append_or_root" and current_operation is not None:
        yield current_operation
        return
    if boundary_policy not in {"append_or_root", "explicit", "forced_child"}:
        raise ValueError(f"Unknown runtime boundary policy: {boundary_policy!r}")

    with opened_runtime_operation(
        owner,
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


def default_handle_id(label: str) -> str:
    safe_label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip(".-") or "operation"
    return f"{safe_label}-{uuid.uuid4().hex[:8]}"


def new_operation_id() -> str:
    return f"op_{uuid.uuid4().hex[:12]}"


@contextmanager
def runtime_activity(
    owner: VcsCore,
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
    boundary = None
    previous_context = None
    operation: OperationRefInfo | None = None
    with owner._lock:
        owner._validate_scope(scope)
        scope = owner._live_scope(scope)
        owner._ensure_runtime_mutation_allowed(
            f"open runtime activity {operation_kind}",
            scope_selector=scope.ref,
            runtime_admission_context=_runtime_admission_context(
                owner,
                scope_ref=scope.ref,
                allowed_blocker_item_ids=allowed_blocker_item_ids,
            ),
        )
        previous_context = owner._pipeline.context
        nested_parent = _boundary_nested_admission(owner, scope=scope, boundary_policy=boundary_policy)
        if nested_parent is None:
            owner._pipeline.set_execution_context(scope, session_id=owner._session_id)
        boundary = runtime_operation_boundary(
            owner,
            scope=scope,
            boundary_policy=boundary_policy,
            default_label=operation_label,
            default_kind=operation_kind,
            failure_policy=failure_policy,
            operation_id=operation_id,
            operation_kind=operation_kind,
            operation_label=operation_label,
            operation_metadata=operation_metadata,
            nested_parent=nested_parent,
        )
        try:
            operation = boundary.__enter__()
        except BaseException:
            owner._pipeline.set_context(previous_context)
            raise
    try:
        yield operation
    except BaseException as exc:
        with owner._lock:
            try:
                assert boundary is not None
                boundary.__exit__(type(exc), exc, exc.__traceback__)
            finally:
                owner._pipeline.set_context(previous_context)
        raise
    else:
        with owner._lock:
            try:
                assert boundary is not None
                boundary.__exit__(None, None, None)
            finally:
                owner._pipeline.set_context(previous_context)


def execute_recorded_in_operation(
    owner: VcsCore,
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
    workspace_output_binding: str = "workspace",
    workspace_effect_overlay: bool = False,
    **params: Any,
) -> RecordedCommandOutcome:
    return _execute_recorded_params(
        owner,
        binding_name,
        command,
        scope=scope,
        boundary_policy=boundary_policy,
        operation_id=operation_id,
        operation_kind=operation_kind,
        operation_label=operation_label,
        operation_metadata=operation_metadata,
        params=params,
        command_param_source="native",
        execution_options=execution_options,
        workspace_output_binding=workspace_output_binding,
        workspace_effect_overlay=workspace_effect_overlay,
    )


def _execute_recorded_params(
    owner: VcsCore,
    binding_name: str,
    command: str,
    *,
    scope: ScopeInfo,
    boundary_policy: str = "explicit",
    operation_id: str | None = None,
    operation_kind: str | None = None,
    operation_label: str | None = None,
    operation_metadata: dict[str, object] | None = None,
    params: Mapping[str, Any],
    command_param_source: CommandValueSource,
    execution_options: CommandExecutionOptions | None = None,
    workspace_output_binding: str = "workspace",
    workspace_effect_overlay: bool = False,
) -> RecordedCommandOutcome:
    if execution_options is None:
        execution_options = CommandExecutionOptions()
    if not isinstance(execution_options, CommandExecutionOptions):
        raise SchemaValidationError(
            f"execution_options must be CommandExecutionOptions, got {type(execution_options).__name__}."
        )
    try:
        validate_command_execution_options(execution_options)
    except CommandEnvelopeError as exc:
        raise SchemaValidationError(str(exc)) from exc
    with owner._lock:
        owner._validate_scope(scope)
        scope = owner._live_scope(scope)
        runtime_admission_context = _runtime_admission_context(
            owner,
            scope_ref=scope.ref,
            record_class=_runtime_record_class(owner, binding_name),
        )
        owner._ensure_runtime_mutation_allowed(
            f"execute {binding_name}.{command}",
            scope_selector=scope.ref,
            runtime_admission_context=runtime_admission_context,
        )
        resolved = owner._binding_contracts.resolve_driver(binding_name)
        # PD3: the `mg exec` -> SPI `prepare` dispatch bridge. SPI drivers
        # dispatch through the validated SPI entry (capability acceptance +
        # ActiveSurface policy + the three-layer ingress validator) inside the
        # same operation boundary.
        return _execute_spi_driver_in_operation(
            owner,
            resolved,
            command,
            scope=scope,
            boundary_policy=boundary_policy,
            operation_id=operation_id,
            operation_kind=operation_kind,
            operation_label=operation_label,
            operation_metadata=operation_metadata,
            params=dict(params),
            command_param_source=command_param_source,
            execution_options=execution_options,
            workspace_output_binding=workspace_output_binding,
            workspace_effect_overlay=workspace_effect_overlay,
        )


def _driver_store_identity(owner: VcsCore, driver: Any, binding: BoundSubstrate) -> Any:
    """Resolve the driver's store identity, nominal when it selects no store.

    Journal-only drivers (the run-driver fingerprint) select no durable store
    state; their identity is the operation journal entry. Drivers whose
    ``store_id`` exists in the world-storage installation get the real one.
    """
    store_id = getattr(driver, "store_id", f"store_{binding.binding_name}")
    try:
        return owner._world_storage().store(store_id).identity
    except KeyError:  # no such store in the installation: nominal identity
        from vcs_core._world_types import SubstrateStoreIdentity

        return SubstrateStoreIdentity(
            store_id=store_id,
            kind="runtime.journal_only",
            resource_id=f"binding:{binding.binding_name}",
        )


def _execute_spi_driver_in_operation(
    owner: VcsCore,
    resolved: ResolvedDriverBinding,
    command: str,
    *,
    scope: ScopeInfo,
    boundary_policy: str,
    operation_id: str | None,
    operation_kind: str | None,
    operation_label: str | None,
    operation_metadata: dict[str, object] | None,
    params: dict[str, Any],
    command_param_source: CommandValueSource,
    execution_options: CommandExecutionOptions,
    workspace_output_binding: str = "workspace",
    workspace_effect_overlay: bool = False,
) -> RecordedCommandOutcome:
    """The PD3 bridge body: route a bound SPI driver through ``prepare``.

    Runs inside the same recorded operation boundary the legacy ``.execute``
    path uses, so a driver dispatch is a journaled operation. Caller holds
    the coordinator lock and has validated scope + mutation admission.
    """
    binding = resolved.bound
    driver = resolved.driver
    selectable = resolved.schema.capabilities.selectable
    if selectable:
        store_id = getattr(driver, "store_id", None)
        if store_id is None or store_id not in owner._world_storage().stores:
            # Fail-closed: a selectable driver without an installed store has no
            # durable home for its revisions — its world selection cannot be
            # computed. (B4b W2 opened the installed-store half; this refusal
            # is the PD3a posture, kept for everything the installation
            # doesn't carry.)
            raise ValueError(
                f"SPI driver dispatch via `mg exec` supports journal-only drivers and selectable "
                f"drivers whose store is installed; {binding.binding_name} declares selectable=True "
                f"but store {store_id!r} is not in the world installation."
            )
    command_contract = resolved.command_contracts.get(command)
    if command_contract is None:
        raise ValueError(f"Unknown {binding.binding_name} command: {command!r}")
    if selectable:
        _reject_execution_options_for_plain_command(binding.binding_name, command, execution_options)
        params = _normalize_and_admit_driver_command_params(
            owner,
            driver,
            command_contract,
            scope=scope,
            params=params,
            command_param_source=command_param_source,
        )
        return _execute_selectable_spi_driver(
            owner,
            binding,
            command,
            scope=scope,
            params=params,
            capabilities=resolved.schema.capabilities,
            schema=resolved.schema,
        )
    if isinstance(driver, ExecutionBoundDriver) and command in driver.execution_commands:
        # PD1/PD2: the driver opted into execution authority for THIS command —
        # dispatch through the reversible wrap, which constructs the per-run
        # capability. A bound driver's non-execution commands (`list`, …) fall
        # through to plain `prepare` below: no capability, no scope fork.
        return _execute_execution_bound_driver(
            owner,
            binding,
            command,
            command_contract=command_contract,
            scope=scope,
            boundary_policy=boundary_policy,
            operation_id=operation_id,
            operation_kind=operation_kind,
            operation_label=operation_label,
            operation_metadata=operation_metadata,
            params=params,
            command_param_source=command_param_source,
            execution_options=execution_options,
            capabilities=resolved.schema.capabilities,
            schema=resolved.schema,
        )
    _reject_execution_options_for_plain_command(binding.binding_name, command, execution_options)
    params = _normalize_and_admit_driver_command_params(
        owner,
        driver,
        command_contract,
        scope=scope,
        params=params,
        command_param_source=command_param_source,
    )

    nested_parent = _boundary_nested_admission(owner, scope=scope, boundary_policy=boundary_policy)
    previous_context = owner._pipeline.context
    try:
        with (
            _boundary_scope_context(owner, scope, nested_parent),
            runtime_operation_boundary(
                owner,
                scope=scope,
                boundary_policy=boundary_policy,
                default_label=f"{binding.binding_name}-{command}",
                default_kind=f"{binding.substrate_type}.{command}",
                operation_id=operation_id,
                operation_kind=operation_kind,
                operation_label=operation_label,
                operation_metadata=operation_metadata,
                nested_parent=nested_parent,
            ) as operation,
        ):
            context = DriverContext(
                operation_id=operation.durable_id if operation is not None else f"unrecorded:{command}",
                binding=binding.binding_name,
                role=getattr(driver, "role", binding.substrate_type),
                store_identity=_driver_store_identity(owner, driver, binding),
                base_heads=(),
            )
            result = dispatch_driver(
                driver,
                context,
                CommandRequest(command=command, params=dict(params)),
                capabilities=resolved.schema.capabilities,
                schema=resolved.schema,
            )
            oids: tuple[str, ...] = ()
            if result.effects:
                oids = tuple(
                    record_runtime_effects(
                        owner,
                        result.effects,
                        substrate=binding.substrate_type,
                        scope=scope,
                        workspace_output_binding=workspace_output_binding,
                        workspace_effect_overlay=workspace_effect_overlay,
                    )
                )
    finally:
        owner._pipeline.restore_execution_context(previous_context)
    value = result.value if result.effects or result.value is not None else result
    return RecordedCommandOutcome(oids=oids, value=value)


def _execute_selectable_spi_driver(
    owner: VcsCore,
    binding: BoundSubstrate,
    command: str,
    *,
    scope: ScopeInfo,
    params: dict[str, Any],
    capabilities: CapabilitySet,
    schema: DriverSchema,
) -> RecordedCommandOutcome:
    """The selectable arm of the dispatch bridge (B4b W2).

    Dispatch → candidate bundle → spelled selection (the new candidate plus an
    explicit ``unchanged`` plan, with a selected-head pin, for every carried
    head — the builder refuses implicit carry) → one journaled, CAS-protected
    world publication via ``WorldOperationRunner``. The shape is S1's probe
    (`spikes/260610-b4b-s1-selectable-route/`), made the route: real
    ``base_heads`` from the binding's currently selected head; an interrupted
    append recovers via the standard operation journal, never a half-published
    selection.
    """
    from vcs_core._transition_kernel_records import RetentionPolicyRequirement
    from vcs_core._world_operation_builder import OperationFinalBuilder, SelectionRequirementPlan
    from vcs_core._world_operation_runner import WorldOperationRunner
    from vcs_core._world_substrate_adapters import _plan_candidate_selection
    from vcs_core._world_types import WORLD_TRANSITION_SCHEMA, WorldSnapshot

    driver = binding.instance
    manager = owner._world_storage()
    role = getattr(driver, "role", binding.substrate_type)
    store_id = driver.store_id
    operation_id = new_operation_id()

    input_world = owner.world_oid(scope)
    carried: tuple[SubstrateHead, ...] = ()
    if input_world is not None:
        carried = tuple(manager.read_world(input_world).snapshot.heads)
    own_previous = next((head for head in carried if head.binding == binding.binding_name), None)
    base_heads = (own_previous.head,) if own_previous is not None else ()

    context = DriverContext(
        operation_id=operation_id,
        binding=binding.binding_name,
        role=role,
        store_identity=manager.store(store_id).identity,
        base_heads=base_heads,
    )
    result = dispatch_driver(
        driver,
        context,
        CommandRequest(command=command, params=dict(params)),
        capabilities=capabilities,
        schema=schema,
    )
    bundle = manager.create_prepared_driver_candidate_bundle(
        store_id,
        operation_id=operation_id,
        binding=binding.binding_name,
        result=result,
        driver_id=driver.driver_id,
        driver_version=driver.driver_version,
        parents=base_heads,
    )
    builder = OperationFinalBuilder(operation_id).select_candidate_plan(
        plan=_plan_candidate_selection(manager, role, bundle)
    )
    kept = tuple(head for head in carried if head.binding != binding.binding_name)
    for head in kept:
        builder = builder.select_unchanged(
            plan=SelectionRequirementPlan(
                operation_id=operation_id,
                binding=head.binding,
                store_id=head.store_id,
                resource_id=head.resource_id,
                selected_head=head.head,
                selection_kind="unchanged",
                retention_policy_requirements=(RetentionPolicyRequirement(kind="selected-head-pin", target=head.head),),
            )
        )
    new_head = manager.substrate_head(store_id, binding=binding.binding_name, head=bundle.candidate.head, role=role)
    transition: dict[str, Any] = {
        "schema": WORLD_TRANSITION_SCHEMA,
        "operation_id": operation_id,
        "parent_worlds": [input_world] if input_world else [],
    }
    if input_world is not None:
        transition["input_world"] = input_world
    prepared = builder.build_prepared(
        operation_kind=f"{binding.substrate_type}.{command}",
        target_ref=scope.ref,
        input_world_oid=input_world,
        parents=(input_world,) if input_world else (),
        snapshot=WorldSnapshot((*kept, new_head)),
        transition=transition,
    )
    outcome = WorldOperationRunner(manager).publish_prepared_world(prepared)
    if not outcome.published:
        # CAS lost or journal failure: fail-closed — the journal carries the
        # diagnosis; nothing was selected.
        raise RuntimeError(
            f"selectable driver publication did not land ({binding.binding_name}.{command}): "
            f"{outcome.error or outcome.status}"
        )
    return RecordedCommandOutcome(oids=(bundle.candidate.head,), value=result)


def _execute_execution_bound_driver(
    owner: VcsCore,
    binding: BoundSubstrate,
    command: str,
    *,
    command_contract: CommandContract,
    scope: ScopeInfo,
    boundary_policy: str,
    operation_id: str | None,
    operation_kind: str | None,
    operation_label: str | None,
    operation_metadata: dict[str, object] | None,
    params: dict[str, Any],
    command_param_source: CommandValueSource,
    execution_options: CommandExecutionOptions,
    capabilities: CapabilitySet,
    schema: DriverSchema,
) -> RecordedCommandOutcome:
    """The reversible-transaction wrap (PD2) around an execution-bound dispatch.

    Reversible by default: the coordinator forks an **isolated** child scope
    (no isolation knob on this path — a carrier that cannot isolate refuses,
    fail-closed), constructs the per-run ``ExecutionCapability`` only *after*
    the isolated fork succeeded (the capability carries the proof), dispatches
    ``prepare_bound`` inside a recorded operation on the run scope, then
    **merges** on success (capture is implicit — the carrier's ``prepare_merge``
    diff at merge IS the capture, flowing through the recording pipeline where
    the Pattern B supervisor seam lives) or **discards** on failure.

    The loud, greppable opt-out
    (``CommandExecutionOptions(non_reversible_run=True)``) runs against the
    dispatch scope's working path with ``isolation="ground"``: deliberately
    the bottom row of the run-mode matrix, an auditable population.
    """
    driver = binding.instance
    non_reversible = execution_options.non_reversible_run
    params = _normalize_and_admit_driver_command_params(
        owner,
        driver,
        command_contract,
        scope=scope,
        params=params,
        command_param_source=command_param_source,
    )

    if non_reversible:
        run_scope = scope
        isolation = "ground"
    else:
        # Isolated, always: ForkHints on this path has no isolation knob to
        # forget. A carrier that cannot isolate raises here and nothing runs.
        # Note: this execution-bound route remains deliberately unwired for A2
        # nested operations. A parent-live invocation would fork and later merge
        # a mid-operation run scope, which A3 routes through composition instead.
        # Top-level dispatch (no live parent op) is the Phase D path and needs no
        # nested-operation flag.
        run_scope = owner.fork(scope, f"run-{uuid.uuid4().hex[:12]}", hints=ForkHints(isolated=True))
        isolation = "isolated"
    operation_metadata = {**(operation_metadata or {}), "reversible_run": not non_reversible}

    capability = ExecutionCapability(
        identity=ExecutionContext.from_scope(run_scope, session_id=owner._session_id),
        working_path=Path(owner.overlay_mount_path_for_scope(run_scope)),
        isolation=isolation,
        _containment=detect_containment_backend(),
    )
    runtime_operation_id: str | None = None
    try:
        with (
            owner._scoped(run_scope),
            runtime_operation_boundary(
                owner,
                scope=run_scope,
                boundary_policy=boundary_policy,
                default_label=f"{binding.binding_name}-{command}",
                default_kind=f"{binding.substrate_type}.{command}",
                operation_id=operation_id,
                operation_kind=operation_kind,
                operation_label=operation_label,
                operation_metadata=operation_metadata,
            ) as operation,
        ):
            runtime_operation_id = operation.durable_id if operation is not None else f"unrecorded:{command}"
            context = DriverContext(
                operation_id=runtime_operation_id,
                binding=binding.binding_name,
                role=getattr(driver, "role", binding.substrate_type),
                store_identity=_driver_store_identity(owner, driver, binding),
                base_heads=(),
            )
            result = dispatch_driver(
                driver,
                context,
                CommandRequest(command=command, params=dict(params)),
                capabilities=capabilities,
                schema=schema,
                execution=capability,
            )
    except BaseException:
        if not non_reversible:
            # Discard, never auto-merge a half-run delta. The body's failure is
            # the error that surfaces; discard problems are logged by lifecycle.
            try:
                owner.discard(run_scope)
            except Exception:  # noqa: BLE001 — surfacing the body's error wins
                logger.warning("Failed to discard run scope %r after a failed run", run_scope.name, exc_info=True)
        raise
    if not non_reversible:
        if execution_options.success_disposition == "seal":
            try:
                seal_result = owner.seal(run_scope, output_binding=execution_options.seal_output_binding)
            except BaseException:
                _discard_execution_run_scope_after_terminalization_failure(owner, run_scope, "seal")
                raise
            return RecordedCommandOutcome(
                oids=(),
                value=SealedExecutionOutcome(driver_result=result, seal_result=seal_result),
            )
        if execution_options.success_disposition == "authority_merge":
            control = execution_options.authority_merge
            if control is None:
                raise AssertionError("validated authority_merge execution options lost their control object")
            try:
                authority_result = owner.merge_with_authority(
                    run_scope,
                    scope,
                    binding_roots=control.binding_roots,
                    decide=cast("DecisionProvider", control.decide),
                    effective_match_digest=control.effective_match_digest,
                    authority_surface_plan_digest=control.authority_surface_plan_digest,
                    permission_plan_digest=control.permission_plan_digest,
                    permission_plan_descriptor=control.permission_plan_descriptor,
                    authority_context=_authority_context_with_runtime_operation(
                        control.authority_context,
                        runtime_operation_id,
                    ),
                )
            except BaseException:
                _discard_execution_run_scope_after_terminalization_failure(owner, run_scope, "authority merge")
                raise
            return RecordedCommandOutcome(
                oids=(),
                value=AuthorityExecutionOutcome(driver_result=result, authority_result=authority_result),
            )
        owner.merge(run_scope, scope)
    return RecordedCommandOutcome(oids=(), value=result)


def _authority_context_with_runtime_operation(
    authority_context: object,
    runtime_operation_id: str | None,
) -> dict[str, object] | None:
    if authority_context is None and runtime_operation_id is None:
        return None
    if authority_context is None:
        context: dict[str, object] = {}
    elif isinstance(authority_context, Mapping):
        context = dict(authority_context)
    else:
        raise TypeError("authority context must be an object")
    if runtime_operation_id is not None:
        context["runtime_operation_id"] = runtime_operation_id
    return context


def _discard_execution_run_scope_after_terminalization_failure(
    owner: VcsCore,
    run_scope: ScopeInfo,
    action: str,
) -> None:
    try:
        owner.discard(run_scope)
    except Exception:  # noqa: BLE001 — surfacing the terminalization error wins
        logger.warning(
            "Failed to discard run scope %r after %s terminalization failed",
            run_scope.name,
            action,
            exc_info=True,
        )


def _reject_execution_options_for_plain_command(
    binding_name: str,
    command: str,
    options: CommandExecutionOptions,
) -> None:
    if options.non_reversible_run:
        raise SchemaValidationError(
            f"Command execution option 'non_reversible_run' is only valid for execution-bound commands; "
            f"{binding_name}.{command} is not execution-bound."
        )
    if options.success_disposition != "merge":
        raise SchemaValidationError(
            f"Command execution option 'success_disposition={options.success_disposition}' is only valid for "
            f"execution-bound commands; {binding_name}.{command} is not execution-bound."
        )


def _normalize_and_admit_driver_command_params(
    owner: VcsCore,
    driver: object,
    command_contract: CommandContract,
    *,
    scope: ScopeInfo,
    params: dict[str, Any],
    command_param_source: CommandValueSource,
) -> dict[str, Any]:
    try:
        invocation = normalize_command_params(command_contract, params, source=command_param_source)
    except CommandContractError as exc:
        raise SchemaValidationError(str(exc)) from exc
    coerced = dict(invocation.params)
    admit_command(owner, driver, command_contract.command_name, scope=scope, params=coerced)
    return coerced


def admit_command(
    owner: VcsCore,
    substrate: object,
    command: str,
    *,
    scope: ScopeInfo,
    params: dict[str, Any],
) -> None:
    with owner._patch_manager.guard():
        for provider in owner._command_admission_providers:
            admit_command_invocation(provider, command, scope, params=params)
        admit_command_invocation(substrate, command, scope, params=params)


def record_effect_in_operation(
    owner: VcsCore,
    binding: BoundSubstrate,
    effect_record: EffectRecord,
    *,
    scope: ScopeInfo,
    boundary_policy: str = "explicit",
    operation_id: str | None = None,
    operation_kind: str | None = None,
    operation_label: str | None = None,
    operation_metadata: dict[str, object] | None = None,
) -> list[str]:
    nested_parent = _boundary_nested_admission(owner, scope=scope, boundary_policy=boundary_policy)
    with (
        _boundary_scope_context(owner, scope, nested_parent),
        runtime_operation_boundary(
            owner,
            scope=scope,
            boundary_policy=boundary_policy,
            default_label=f"{binding.binding_name}-{effect_record.effect_type}",
            default_kind=f"{binding.substrate_type}.{effect_record.effect_type}",
            operation_id=operation_id,
            operation_kind=operation_kind,
            operation_label=operation_label,
            operation_metadata=operation_metadata,
            nested_parent=nested_parent,
        ),
    ):
        return record_runtime_effects(owner, [effect_record], substrate=binding.substrate_type, scope=scope)


def exec_command(
    owner: VcsCore,
    binding_name: str,
    command: str,
    *,
    scope: ScopeInfo,
    execution_options: CommandExecutionOptions | None = None,
    **params: Any,
) -> RecordedCommandOutcome:
    return execute_recorded(owner, binding_name, command, scope=scope, execution_options=execution_options, **params)


def execute_recorded(
    owner: VcsCore,
    binding_name: str,
    command: str,
    *,
    scope: ScopeInfo,
    execution_options: CommandExecutionOptions | None = None,
    **params: Any,
) -> RecordedCommandOutcome:
    return execute_recorded_in_operation(
        owner,
        binding_name,
        command,
        scope=scope,
        boundary_policy="explicit",
        execution_options=execution_options,
        **params,
    )


def execute_recorded_in_child_operation(
    owner: VcsCore,
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
    return execute_recorded_in_operation(
        owner,
        binding_name,
        command,
        scope=scope,
        boundary_policy="forced_child",
        operation_id=operation_id,
        operation_kind=operation_kind,
        operation_label=operation_id,
        operation_metadata=operation_metadata,
        execution_options=execution_options,
        workspace_output_binding=workspace_output_binding,
        workspace_effect_overlay=True,
        **params,
    )


def record_in_child_operation(
    owner: VcsCore,
    binding_name: str,
    effect_record: EffectRecord,
    *,
    scope: ScopeInfo,
    operation_id: str,
    operation_kind: str,
    operation_metadata: dict[str, object] | None = None,
) -> list[str]:
    with owner._lock:
        owner._validate_scope(scope)
        scope = owner._live_scope(scope)
        owner._ensure_runtime_mutation_allowed(
            f"record {binding_name}.{effect_record.effect_type}",
            scope_selector=scope.ref,
        )
        binding = owner._resolve_binding(binding_name)
        return record_effect_in_operation(
            owner,
            binding,
            effect_record,
            scope=scope,
            boundary_policy="forced_child",
            operation_id=operation_id,
            operation_kind=operation_kind,
            operation_label=operation_id,
            operation_metadata=operation_metadata,
        )
