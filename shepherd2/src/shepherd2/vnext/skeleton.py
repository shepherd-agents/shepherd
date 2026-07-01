"""Experimental retained-output walking skeleton.

This module is an opt-in integration harness. Importing it must not import
``vcs_core``; enabled entrypoints load that package lazily after the profile
flags are present.
"""

from __future__ import annotations

import importlib
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from ..kernel.facts import TRUSTED_APPEND_CONTEXT, TRUSTED_READ_CONTEXT, AppendBatch, AppendGroup
from ..schemas.execution import (
    Execution,
    create_execution_batch,
    execution_completed,
    execution_id_for,
    fail_execution_batch,
    project_execution,
    publish_execution_frontier,
)
from ..schemas.run_outputs import (
    RUN_OUTPUT_SCHEMA,
    ProjectedRunOutputDescriptor,
    RunOutputCitation,
    RunOutputDescriptor,
    RunOutputDescriptorLocator,
    RunOutputIdentity,
    RunOutputMaterializationKind,
    RunOutputOwner,
    RunOutputRef,
    RunOutputState,
    project_run_output_descriptors,
    resolve_run_output_descriptor,
    resolve_run_output_descriptor_from_store,
    run_output_descriptor_fact,
    run_output_identity_for,
)
from ..trace_store import SQLiteTraceStore, TraceStoreError

if TYPE_CHECKING:
    from pathlib import Path

    from ..kernel.facts import TraceStore

SKELETON_ENV = "SHEPHERD2_SKELETON"
SEAL_AND_SELECT_ENV = "VCS_CORE_SEAL_AND_SELECT"
NESTED_OPERATIONS_ENV = "VCS_CORE_NESTED_OPERATIONS"


class SkeletonUnavailableError(RuntimeError):
    """Raised when the experimental skeleton profile is unavailable."""


@dataclass(frozen=True)
class TraceRef:
    """Durable shepherd2 citation for one skeleton child run."""

    run_id: str
    execution_id: str
    frontier_id: str


@dataclass(frozen=True)
class GitRepoHandle:
    """Copyable tree-backed repo handle view for the skeleton.

    The handle is a value view over a binding basis. It is not the settlement
    token; only ``RunOutput`` can consume a retained output.
    """

    binding: str
    scope_name: str
    scope_ref: str
    scope_instance_id: str
    basis_world_oid: str | None
    _session: Session = field(compare=False, repr=False)
    _scope: Any = field(compare=False, repr=False)
    authority: str = "readwrite"
    _run_id: str | None = field(default=None, compare=False, repr=False)

    def write(self, path: str, content: bytes, *, mode: int = 0o100644) -> GitRepoHandle:
        """Return a new handle after recording one child binding write."""
        if self._run_id is None:
            raise SkeletonUnavailableError("skeleton writes are only available inside run_child().")
        return self._session._write_child_workspace(self, path, content, mode=mode)

    def trace_payload(self) -> dict[str, object]:
        """Return a JSON-shaped citation for this handle."""
        return {
            "kind": "shepherd2.skeleton.git_repo_handle.v0",
            "binding": self.binding,
            "scope_name": self.scope_name,
            "scope_ref": self.scope_ref,
            "scope_instance_id": self.scope_instance_id,
            "basis_world_oid": self.basis_world_oid,
            "authority": self.authority,
        }


@dataclass(frozen=True)
class RunOutput:
    """Run-owned retained output reference with consume-once settlement rights."""

    _session: Session = field(compare=False, repr=False)
    _payload: Mapping[str, Any]
    _descriptor_locator: RunOutputDescriptorLocator | None = field(default=None, compare=False, repr=False)

    @property
    def identity(self) -> RunOutputIdentity:
        """Return the stable product identity for this output."""
        return _run_output_identity(self._custody_payload())

    @property
    def owner(self) -> RunOutputOwner:
        """Return the run/query owner citation for this settlement right."""
        return _run_output_owner(self._custody_payload())

    @property
    def descriptor(self) -> RunOutputDescriptor:
        """Return output-name, world-binding, and materialization metadata."""
        return _run_output_descriptor(self._custody_payload())

    @property
    def citation(self) -> RunOutputCitation:
        """Return the trace-owned citation for this output."""
        return _run_output_citation(self._payload, descriptor_locator=self._descriptor_locator)

    @property
    def ref(self) -> RunOutputRef:
        """Return the current immutable query value for this output."""
        return self._session._run_output_ref_from_payload(
            self._payload,
            descriptor_locator=self._descriptor_locator,
        )

    @property
    def binding(self) -> str:
        return str(self._custody_payload()["binding"])

    @property
    def parent_scope_name(self) -> str:
        return str(self._custody_payload()["parent_scope_name"])

    @property
    def parent_ref(self) -> str:
        return str(self._custody_payload()["parent_ref"])

    @property
    def parent_scope_instance_id(self) -> str | None:
        value = self._custody_payload().get("parent_scope_instance_id")
        return str(value) if value is not None else None

    @property
    def scope_name(self) -> str:
        return str(self._custody_payload()["scope_name"])

    @property
    def output_world_oid(self) -> str:
        return str(self._custody_payload()["output_world_oid"])

    @property
    def handoff_ref(self) -> str:
        return str(self._custody_payload()["handoff_ref"])

    @property
    def candidate_head(self) -> str:
        return str(self._custody_payload()["candidate_head"])

    @property
    def metadata(self) -> Mapping[str, Any]:
        """Return custody-validated output metadata."""
        return self._custody_payload()

    def _custody_payload(self) -> Mapping[str, Any]:
        validated = self._session._validated_output_payload(self._payload)
        _require_output_alias_disabled(validated)
        return validated

    def read_file(self, path: str) -> tuple[bytes, int] | None:
        """Read one file from the retained tree-backed output."""
        self._session._ensure_available()
        if self.descriptor.materialization_kind != "tree":
            return None
        handle = self._session._retained_handle_from_output(self._payload)
        return cast("tuple[bytes, int] | None", self._session._mg.read_retained_workspace_file(handle.scope_name, path))

    def select(self) -> Any:
        """Select this retained output into its parent binding."""
        return self._session.select(self)

    def apply(self) -> Any:
        """Apply this retained output at the boundary.

        For the current skeleton floor, ``apply`` and ``select`` both advance
        the full binding through the lower-layer selected receipt. Future
        Changeset-slice application can split this spelling.
        """
        return self._session.apply(self)

    def release(self) -> Any:
        """Release this retained output without selecting it."""
        return self._session.release(self)

    def discard(self) -> Any:
        """Discard this retained output without selecting it."""
        return self._session.discard(self)


@dataclass(frozen=True)
class WorkspaceBinding:
    """Product-shaped boundary verbs for one parent binding."""

    _session: Session = field(compare=False, repr=False)
    _parent: Any = field(compare=False, repr=False)
    binding: str = "workspace"

    def select(self, output: RunOutput) -> Any:
        """Select a retained output into this binding."""
        return self._session.select(output, parent=self._parent, binding=self.binding)

    def apply(self, output: RunOutput) -> Any:
        """Apply a retained output into this binding."""
        return self._session.apply(output, parent=self._parent, binding=self.binding)

    def release(self, output: RunOutput) -> Any:
        """Release a retained output without advancing this binding."""
        return self._session.release(output, parent=self._parent, binding=self.binding)

    def discard(self, output: RunOutput) -> Any:
        """Discard a retained output without advancing this binding."""
        return self._session.discard(output, parent=self._parent, binding=self.binding)


@dataclass(frozen=True)
class CompletedRun:
    """Value-shaped completed run facade for the skeleton."""

    run_id: str
    inputs: Mapping[str, Any]
    outputs: Mapping[str, RunOutput]
    trace_ref: TraceRef
    execution: Execution


@dataclass(frozen=True)
class RetainedRunOutput:
    """Experimental query facade over a lower-layer retained-output row."""

    state: str
    output: RunOutput
    ref: RunOutputRef
    settlement: Any | None = None


class Session:
    """Experimental integration owner over one ``VcsCore`` instance."""

    def __init__(
        self,
        vcscore: Any,
        *,
        trace_store: TraceStore | None = None,
        trace_path: str | Path | None = None,
    ) -> None:
        if trace_store is not None and trace_path is not None:
            raise ValueError("Provide trace_store or trace_path, not both.")
        self._mg = vcscore
        self.trace_store = trace_store or SQLiteTraceStore(trace_path or ":memory:")
        self._owns_trace_store = trace_store is None
        self._write_index = 0

    def close(self) -> None:
        """Close the owned trace store, if this session created it."""
        if self._owns_trace_store:
            self.trace_store.close()

    def repo(self, parent: Any, *, binding: str = "workspace", authority: str = "readwrite") -> GitRepoHandle:
        """Return a copyable parent binding handle for use with ``run_child``."""
        self._ensure_available()
        _require_workspace_repo_binding(binding)
        _require_repo_authority(authority)
        parent_scope = self._coerce_live_scope(parent)
        return self._repo_handle(parent_scope, binding=binding, run_id=None, authority=authority)

    def workspace_repo(self, parent: Any, *, binding: str = "workspace") -> GitRepoHandle:
        """Compatibility alias for ``repo``."""
        return self.repo(parent, binding=binding)

    def binding(self, parent: Any, binding: str = "workspace") -> WorkspaceBinding:
        """Return boundary verbs for one parent binding."""
        self._ensure_available()
        _require_binding_name(binding)
        return WorkspaceBinding(self, self._coerce_live_scope(parent), binding=binding)

    def workspace(self, parent: Any, *, binding: str = "workspace") -> WorkspaceBinding:
        """Compatibility alias for ``binding``."""
        return self.binding(parent, binding=binding)

    def select(self, output: RunOutput, *, parent: Any | None = None, binding: str | None = None) -> Any:
        """Select a retained run output into its parent binding."""
        return self._settle_run_output(output, action="select", parent=parent, binding=binding)

    def apply(self, output: RunOutput, *, parent: Any | None = None, binding: str | None = None) -> Any:
        """Apply a retained run output at the boundary.

        The current floor has no partial Changeset application yet, so
        applying a full binding output lowers to the selected receipt.
        """
        return self._settle_run_output(output, action="select", parent=parent, binding=binding)

    def release(self, output: RunOutput, *, parent: Any | None = None, binding: str | None = None) -> Any:
        """Release a retained run output without applying it."""
        return self._settle_run_output(output, action="release", parent=parent, binding=binding)

    def discard(self, output: RunOutput, *, parent: Any | None = None, binding: str | None = None) -> Any:
        """Discard a retained run output without applying it."""
        return self._settle_run_output(output, action="discard", parent=parent, binding=binding)

    def run_child(
        self,
        *,
        parent: Any,
        task: Callable[..., GitRepoHandle],
        args: tuple[Any,...] = (),
        kwargs: Mapping[str, Any] | None = None,
        run_id: str | None = None,
        child_name: str | None = None,
    ) -> CompletedRun:
        """Run one callable in a vcs-core child scope and retain one binding output."""
        self._ensure_available()
        parent_scope = self._coerce_live_scope(parent)
        stable_run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        child_scope = self._mg.fork(parent_scope, child_name or f"{stable_run_id}-child")
        child_sealed = False
        trace_ids = self._trace_ids(stable_run_id)
        call_kwargs = dict(kwargs or {})
        trace_inputs = {
            "args": [_trace_value(item) for item in args],
            "kwargs": {str(key): _trace_value(value) for key, value in call_kwargs.items()},
            "parent": _scope_payload(parent_scope),
        }
        allowed_bindings = _input_handle_bindings(args, call_kwargs)
        causal_tail: str | None = None

        try:
            create_receipt = self.trace_store.append(
                TRUSTED_APPEND_CONTEXT,
                create_execution_batch(
                    append_intent_id=f"skeleton:{stable_run_id}:create",
                    execution_id=trace_ids.execution_id,
                    task_ref=_task_ref(task),
                    inputs=trace_inputs,
                ),
            )
            causal_tail = create_receipt.fact_ids[-1]
            lowered_args = tuple(
                self._lower_task_arg(item, parent_scope=parent_scope, child_scope=child_scope, run_id=stable_run_id)
                for item in args
            )
            lowered_kwargs = {
                key: self._lower_task_arg(
                    value,
                    parent_scope=parent_scope,
                    child_scope=child_scope,
                    run_id=stable_run_id,
                )
                for key, value in call_kwargs.items()
            }
            with self._mg.runtime_activity(
                scope=parent_scope,
                operation_label=f"skeleton-parent-{stable_run_id}",
                operation_kind="shepherd2.skeleton.parent",
                operation_id=_operation_id(stable_run_id, "parent"),
            ):
                result = task(*lowered_args, **lowered_kwargs)
            self._validate_returned_handle(result, child_scope=child_scope, allowed_bindings=allowed_bindings)
            seal_result = self._mg.seal(child_scope, output_binding=result.binding)
            child_sealed = True
            retained_handle = self._mg.retained_workspace_handle(child_scope.name)
            output_payload = _output_payload(
                parent=parent_scope,
                child=child_scope,
                handoff=seal_result.handoff,
                retained_handle=retained_handle,
                trace_ref=trace_ids,
            )
            terminal_receipt = self.trace_store.append(
                TRUSTED_APPEND_CONTEXT,
                _complete_execution_with_run_output_descriptors_batch(
                    append_intent_id=f"skeleton:{stable_run_id}:complete",
                    execution_id=trace_ids.execution_id,
                    outputs={result.binding: output_payload},
                    caused_by=(causal_tail,),
                ),
            )
        except Exception as exc:
            if not child_sealed:
                self._discard_unsealed_child(child_scope, primary_error=exc)
            self._record_failed_run(stable_run_id, trace_ids, primary_error=exc, caused_by=causal_tail)
            raise

        cutoff = self._publish_frontier(trace_ids, terminal_receipt.fact_ids[-1])
        trace_slice = self.trace_store.resolve_frontier(TRUSTED_READ_CONTEXT, cutoff.frontier_id)
        execution = project_execution(trace_slice, trace_ids.execution_id, cutoff=cutoff)
        return self._completed_run_from_execution(stable_run_id, trace_ids, execution, trace_slice=trace_slice)

    def load_run(self, ref: TraceRef | str) -> CompletedRun:
        """Reload a completed skeleton run from durable shepherd2 trace storage."""
        self._ensure_available()
        trace_ids = ref if isinstance(ref, TraceRef) else self._trace_ids(ref)
        cutoff = self.trace_store.read_owner_cutoff(trace_ids.frontier_id)
        trace_slice = self.trace_store.resolve_frontier(TRUSTED_READ_CONTEXT, cutoff.frontier_id)
        execution = project_execution(trace_slice, trace_ids.execution_id, cutoff=cutoff)
        return self._completed_run_from_execution(trace_ids.run_id, trace_ids, execution, trace_slice=trace_slice)

    def resolve_run_output(self, locator: RunOutputDescriptorLocator) -> RunOutput:
        """Resolve one RunOutput from a trace-owned descriptor locator."""
        self._ensure_available()
        if not isinstance(locator, RunOutputDescriptorLocator):
            raise TypeError("skeleton resolve_run_output() requires a RunOutputDescriptorLocator.")
        record = self._resolve_trace_output_descriptor(locator)
        return self._run_output_from_descriptor_record(record.locator.output_name, record)

    def list_retained_outputs(
        self,
        *,
        parent: Any | None = None,
        binding: str = "workspace",
        state: str | None = None,
    ) -> tuple[RetainedRunOutput,...]:
        """Compatibility alias for ``list_run_outputs``."""
        return self.list_run_outputs(parent=parent, binding=binding, state=state)

    def list_run_outputs(
        self,
        *,
        parent: Any | None = None,
        binding: str | None = None,
        state: str | None = None,
    ) -> tuple[RetainedRunOutput,...]:
        """List retained run outputs using lower-layer custody facts."""
        self._ensure_available()
        rows = self._mg.list_retained_outputs(parent=parent, binding=binding, state=state)
        results: list[RetainedRunOutput] = []
        for row in rows:
            if row.state == "invalid":
                raise SkeletonUnavailableError(
                    f"skeleton retained-output query found invalid custody for {row.scope_name!r}: "
                    f"{row.invalid_reason}"
                )
            payload = self._output_payload_from_query_row(row)
            results.append(
                RetainedRunOutput(
                    state=row.state,
                    output=RunOutput(self, payload),
                    ref=self._run_output_ref_from_payload(
                        payload,
                        state=row.state,
                        settlement_ref=row.settlement_ref,
                        invalid_reason=row.invalid_reason,
                    ),
                    settlement=row.settlement,
                )
            )
        return tuple(results)

    def _write_child_workspace(
        self,
        handle: GitRepoHandle,
        path: str,
        content: bytes,
        *,
        mode: int,
    ) -> GitRepoHandle:
        self._ensure_available()
        if not path:
            raise ValueError("path must not be empty")
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        _require_workspace_repo_binding(handle.binding)
        if handle.authority != "readwrite":
            raise PermissionError(f"GitRepoHandle.write is not permitted under authority={handle.authority!r}")
        current_world_oid = self._mg.world_oid(handle._scope)
        if handle.basis_world_oid != current_world_oid:
            raise SkeletonUnavailableError(
                "skeleton GitRepoHandle has a stale basis; use the handle returned by the previous write."
            )
        self._write_index += 1
        assert handle._run_id is not None
        operation_id = _operation_id(handle._run_id, f"{handle.binding}-write-{self._write_index}")
        self._mg.record_child_workspace_write(
            scope=handle._scope,
            path=path,
            content=content,
            mode=mode,
            operation_id=operation_id,
            operation_kind="shepherd2.skeleton.binding_write",
            operation_metadata={"path": path, "binding": handle.binding},
        )
        return self._repo_handle(
            handle._scope,
            binding=handle.binding,
            run_id=handle._run_id,
            authority=handle.authority,
        )

    def _retained_handle_from_output(self, payload: Mapping[str, Any]) -> Any:
        validated = self._validated_output_payload(payload)
        return self._mg.retained_workspace_handle(str(validated["scope_name"]))

    def _output_payload_from_query_row(self, row: Any) -> dict[str, Any]:
        try:
            handle = self._mg.retained_workspace_handle(row.scope_name)
            handoff = self._mg.retained_workspace_handoff(handle)
        except Exception as exc:
            raise SkeletonUnavailableError(
                "skeleton retained-output query row cannot be revalidated against custody."
            ) from exc
        parent_identity = self._parent_identity_from_handoff(handoff)
        payload: dict[str, Any] = {
            "schema": RUN_OUTPUT_SCHEMA,
            "output_name": handoff.binding,
            "parent_scope_name": parent_identity["name"],
            "parent_ref": handoff.parent_ref,
            "scope_name": handoff.scope_name,
            "scope_ref": handoff.scope_ref,
            "scope_instance_id": handoff.scope_instance_id,
            "binding": handoff.binding,
            "output_world_oid": handoff.output_world_oid,
            "handoff_ref": handoff.handoff_ref,
            "candidate_id": handoff.candidate_id,
            "candidate_ref": handoff.candidate_ref,
            "candidate_head": handoff.candidate_head,
            "parent_basis_world_oid": handoff.parent_basis_world_oid,
            "store_id": handoff.store_id,
            "resource_id": handoff.resource_id,
            "materialization_kind": _materialization_kind_for_store(handoff.store_id),
            "retained_handle_head": handle.head,
            "changed_paths": list(handoff.changed_paths),
        }
        if parent_identity["instance_id"] is not None:
            payload["parent_scope_instance_id"] = parent_identity["instance_id"]
        return self._validated_output_payload(payload)

    def _validated_output_payload(
        self,
        payload: Mapping[str, Any],
        *,
        trace_ids: TraceRef | None = None,
    ) -> dict[str, Any]:
        self._ensure_available()
        if payload.get("schema") != RUN_OUTPUT_SCHEMA:
            raise SkeletonUnavailableError("skeleton run has no RunOutput metadata.")
        try:
            scope_name = str(payload["scope_name"])
        except KeyError as exc:
            raise SkeletonUnavailableError("skeleton run output is missing retained custody metadata.") from exc
        try:
            handle = self._mg.retained_workspace_handle(scope_name)
            handoff = self._mg.retained_workspace_handoff(handle)
        except Exception as exc:
            raise SkeletonUnavailableError(
                "skeleton run output cannot be validated against retained custody."
            ) from exc
        parent_identity = self._parent_identity_from_handoff(handoff)
        expected_fields: list[tuple[str, Any]] = [
            ("scope_name", handoff.scope_name),
            ("scope_ref", handoff.scope_ref),
            ("scope_instance_id", handoff.scope_instance_id),
            ("binding", handoff.binding),
            ("output_world_oid", handoff.output_world_oid),
            ("handoff_ref", handoff.handoff_ref),
            ("candidate_id", handoff.candidate_id),
            ("candidate_ref", handoff.candidate_ref),
            ("candidate_head", handoff.candidate_head),
            ("parent_ref", handoff.parent_ref),
            ("parent_basis_world_oid", handoff.parent_basis_world_oid),
            ("parent_scope_name", parent_identity["name"]),
            ("retained_handle_head", handle.head),
            ("store_id", handle.store_id),
            ("resource_id", handle.resource_id),
            ("materialization_kind", _materialization_kind_for_store(handle.store_id)),
        ]
        if parent_identity["instance_id"] is not None:
            expected_fields.append(("parent_scope_instance_id", parent_identity["instance_id"]))
        payload_trace_ids = trace_ids if trace_ids is not None else _payload_trace_ref(payload)
        if payload_trace_ids is not None:
            expected_fields.extend(
                [
                    ("trace_run_id", payload_trace_ids.run_id),
                    ("trace_execution_id", payload_trace_ids.execution_id),
                    ("trace_frontier_id", payload_trace_ids.frontier_id),
                ]
            )
        canonical: dict[str, Any] = {"schema": RUN_OUTPUT_SCHEMA}
        output_name = payload.get("output_name")
        if not isinstance(output_name, str) or not output_name:
            raise SkeletonUnavailableError("skeleton run output has malformed output_name metadata.")
        canonical["output_name"] = output_name
        for field_name, expected in expected_fields:
            actual = payload.get(field_name)
            if actual != expected:
                raise SkeletonUnavailableError(
                    "skeleton run output disagrees with retained custody: "
                    f"{field_name} expected {expected!r}, got {actual!r}."
                )
            canonical[field_name] = expected
        if tuple(payload.get("changed_paths", ())) != tuple(handle.changed_paths):
            raise SkeletonUnavailableError("skeleton run output disagrees with retained custody: changed_paths.")
        canonical["changed_paths"] = list(handle.changed_paths)
        return canonical

    def _run_output_ref_from_payload(
        self,
        payload: Mapping[str, Any],
        *,
        state: str | None = None,
        settlement_ref: str | None = None,
        invalid_reason: str | None = None,
        descriptor_locator: RunOutputDescriptorLocator | None = None,
    ) -> RunOutputRef:
        validated = self._validated_output_payload(payload)
        if state is None:
            row = self._query_row_for_output(validated)
            state = str(row.state)
            settlement_ref = row.settlement_ref
            invalid_reason = row.invalid_reason
        output_state = _run_output_state(state)
        if output_state == "invalid":
            raise SkeletonUnavailableError(
                f"skeleton run output has invalid retained custody: {invalid_reason or 'unknown reason'}"
            )
        citation = _run_output_citation(validated, descriptor_locator=descriptor_locator)
        return RunOutputRef(
            identity=citation.identity,
            owner=citation.owner,
            descriptor=citation.descriptor,
            state=output_state,
            parent_basis_world_oid=citation.parent_basis_world_oid,
            candidate_ref=citation.candidate_ref,
            store_id=citation.store_id,
            resource_id=citation.resource_id,
            changed_paths=citation.changed_paths,
            settlement_ref=settlement_ref,
            invalid_reason=invalid_reason,
            descriptor_locator=descriptor_locator,
        )

    def _query_row_for_output(self, payload: Mapping[str, Any]) -> Any:
        parent = self._parent_scope_from_output(payload)
        binding = str(payload["binding"])
        scope_ref = str(payload["scope_ref"])
        scope_instance_id = str(payload["scope_instance_id"])
        candidate_id = str(payload["candidate_id"])
        for row in self._mg.list_retained_outputs(parent=parent, binding=binding):
            if (
                row.scope_ref == scope_ref
                and row.scope_instance_id == scope_instance_id
                and row.candidate_id == candidate_id
            ):
                if row.state == "invalid":
                    raise SkeletonUnavailableError(
                        f"skeleton retained-output query found invalid custody for {row.scope_name!r}: "
                        f"{row.invalid_reason}"
                    )
                return row
        raise SkeletonUnavailableError("skeleton run output is missing from retained-output custody query.")

    def _settle_run_output(
        self,
        output: RunOutput,
        *,
        action: str,
        parent: Any | None,
        binding: str | None,
    ) -> Any:
        self._ensure_available()
        if not isinstance(output, RunOutput):
            raise TypeError("skeleton boundary verbs require a RunOutput.")
        if output._session is not self:
            raise TypeError("skeleton boundary verbs cannot settle a RunOutput from another Session.")
        payload = output._custody_payload()
        output_binding = str(payload["binding"])
        if binding is not None and binding != output_binding:
            raise SkeletonUnavailableError(
                f"skeleton boundary binding mismatch: expected {output_binding!r}, got {binding!r}."
            )
        parent_scope = self._parent_scope_from_output(payload)
        if parent is not None:
            requested_parent = self._coerce_live_scope(parent)
            self._require_parent_matches_output(requested_parent, payload)
            parent_scope = requested_parent
        handle = self._retained_handle_from_output(payload)
        if action == "select":
            return self._mg.select_retained_output(handle, parent=parent_scope, binding=output_binding)
        if action == "release":
            return self._mg.release_retained_output(handle, parent=parent_scope, binding=output_binding)
        if action == "discard":
            return self._mg.discard_retained_output(handle, parent=parent_scope, binding=output_binding)
        raise ValueError(f"unknown skeleton settlement action: {action!r}")

    def _require_parent_matches_output(self, parent: Any, payload: Mapping[str, Any]) -> None:
        parent_name = str(payload["parent_scope_name"])
        parent_ref = str(payload["parent_ref"])
        if parent_ref == self._mg.ground.ref:
            if parent.name != self._mg.ground.name or parent.ref != self._mg.ground.ref:
                raise SkeletonUnavailableError(
                    "skeleton boundary parent mismatch: "
                    f"expected ground ({self._mg.ground.ref}), got {parent.name!r} ({parent.ref})."
                )
            return
        parent_instance_id = str(payload["parent_scope_instance_id"])
        _require_scope_identity(
            parent,
            name=parent_name,
            ref=parent_ref,
            instance_id=parent_instance_id,
            context="skeleton boundary parent",
        )

    def _parent_identity_from_handoff(self, handoff: Any) -> dict[str, str | None]:
        if handoff.parent_ref == self._mg.ground.ref:
            return {"name": self._mg.ground.name, "instance_id": None}
        snapshot = self._mg.store.require_scope_registry_projection()
        entry = snapshot.entries_by_ref.get(handoff.parent_ref)
        if entry is None:
            raise SkeletonUnavailableError(
                "skeleton run output disagrees with retained custody: "
                f"missing parent registry ref {handoff.parent_ref!r}."
            )
        return {"name": entry.name, "instance_id": entry.instance_id}

    def _ensure_available(self) -> None:
        if os.environ.get(SKELETON_ENV) != "1":
            raise SkeletonUnavailableError(f"skeleton unavailable: set {SKELETON_ENV}=1.")
        if os.environ.get(SEAL_AND_SELECT_ENV) != "1":
            raise SkeletonUnavailableError(f"skeleton unavailable: set {SEAL_AND_SELECT_ENV}=1.")
        if os.environ.get(NESTED_OPERATIONS_ENV) != "1":
            raise SkeletonUnavailableError(f"skeleton unavailable: set {NESTED_OPERATIONS_ENV}=1.")
        try:
            importlib.import_module("vcs_core")
        except ModuleNotFoundError as exc:
            raise SkeletonUnavailableError("skeleton unavailable: vcs_core is not importable.") from exc

    def _coerce_live_scope(self, scope: Any) -> Any:
        if isinstance(scope, str):
            if scope == self._mg.ground.name:
                return self._mg.ground
            resolved = self._mg.lookup_scope(scope)
            if resolved is None:
                raise SkeletonUnavailableError(f"skeleton scope is not live: {scope!r}.")
            return resolved
        return scope

    def _repo_handle(self, scope: Any, *, binding: str, run_id: str | None, authority: str) -> GitRepoHandle:
        _require_repo_authority(authority)
        return GitRepoHandle(
            binding=binding,
            scope_name=scope.name,
            scope_ref=scope.ref,
            scope_instance_id=scope.instance_id,
            basis_world_oid=self._mg.world_oid(scope),
            _session=self,
            _scope=scope,
            authority=authority,
            _run_id=run_id,
        )

    def _validate_returned_handle(
        self,
        result: Any,
        *,
        child_scope: Any,
        allowed_bindings: frozenset[str],
    ) -> None:
        if not isinstance(result, GitRepoHandle):
            raise TypeError("skeleton tasks must return a GitRepoHandle.")
        if result._session is not self:
            raise TypeError("skeleton task returned a handle from another Session.")
        if result.binding not in allowed_bindings:
            raise TypeError("skeleton task returned a handle for a binding that was not provided as input.")
        if (
            result.scope_name != child_scope.name
            or result.scope_ref != child_scope.ref
            or result.scope_instance_id != child_scope.instance_id
        ):
            raise TypeError("skeleton task returned a handle outside the child scope.")
        current_child_world_oid = self._mg.world_oid(child_scope)
        if result.basis_world_oid != current_child_world_oid:
            raise TypeError(
                "skeleton task returned a stale GitRepoHandle; "
                "return the handle produced by the final binding write."
            )

    def _validate_input_handle(self, value: GitRepoHandle, *, parent_scope: Any) -> None:
        if value._session is not self:
            raise TypeError("skeleton cannot lower a GitRepoHandle from another Session.")
        _require_workspace_repo_binding(value.binding)
        if (
            value.scope_name != parent_scope.name
            or value.scope_ref != parent_scope.ref
            or value.scope_instance_id != parent_scope.instance_id
        ):
            raise SkeletonUnavailableError(
                "skeleton input handle identity mismatch: "
                f"expected {parent_scope.name!r} ({parent_scope.ref}, {parent_scope.instance_id}), "
                f"got {value.scope_name!r} ({value.scope_ref}, {value.scope_instance_id})."
            )
        current_parent_world_oid = self._mg.world_oid(parent_scope)
        if value.basis_world_oid != current_parent_world_oid:
            raise SkeletonUnavailableError(
                "skeleton input GitRepoHandle has a stale basis; reacquire the parent binding handle."
            )

    def _discard_unsealed_child(self, child_scope: Any, *, primary_error: BaseException) -> None:
        child = self._mg.lookup_scope(child_scope.name)
        if child is None:
            return
        if child.ref != child_scope.ref or child.instance_id != child_scope.instance_id:
            return
        try:
            self._mg.discard(child)
        except Exception as discard_error: # noqa: BLE001
            primary_error.add_note(
                "skeleton could not discard failed child "
                f"{child_scope.name!r}: {type(discard_error).__name__}: {discard_error}"
            )

    def _record_failed_run(
        self,
        run_id: str,
        trace_ids: TraceRef,
        *,
        primary_error: BaseException,
        caused_by: str | None,
    ) -> None:
        try:
            terminal_receipt = self.trace_store.append(
                TRUSTED_APPEND_CONTEXT,
                fail_execution_batch(
                    append_intent_id=f"skeleton:{run_id}:fail",
                    execution_id=trace_ids.execution_id,
                    error=f"{type(primary_error).__name__}: {primary_error}",
                    caused_by=() if caused_by is None else (caused_by,),
                ),
            )
            self._publish_frontier(trace_ids, terminal_receipt.fact_ids[-1])
        except Exception as trace_error: # noqa: BLE001
            primary_error.add_note(
                "skeleton could not record failure trace "
                f"for run {run_id!r}: {type(trace_error).__name__}: {trace_error}"
            )

    def _lower_task_arg(self, value: Any, *, parent_scope: Any, child_scope: Any, run_id: str) -> Any:
        if not isinstance(value, GitRepoHandle):
            return value
        self._validate_input_handle(value, parent_scope=parent_scope)
        return self._repo_handle(child_scope, binding=value.binding, run_id=run_id, authority=value.authority)

    def _parent_scope_from_output(self, payload: Mapping[str, Any]) -> Any:
        validated = self._validated_output_payload(payload)
        parent_name = str(validated["parent_scope_name"])
        parent_ref = str(validated["parent_ref"])
        if parent_name == self._mg.ground.name or parent_ref == self._mg.ground.ref:
            return self._mg.ground
        parent_instance_id = str(validated["parent_scope_instance_id"])
        parent = self._mg.lookup_scope(parent_name)
        if parent is None:
            snapshot = self._mg.store.require_scope_registry_projection()
            parent = self._scope_from_registry_ref(snapshot, parent_ref, seen=frozenset())
        _require_scope_identity(
            parent,
            name=parent_name,
            ref=parent_ref,
            instance_id=parent_instance_id,
            context="skeleton output parent",
        )
        return parent

    def _scope_from_registry_ref(self, snapshot: Any, ref: str, *, seen: frozenset[str]) -> Any:
        if ref == self._mg.ground.ref:
            return self._mg.ground
        if ref in seen:
            raise SkeletonUnavailableError(f"skeleton scope registry contains a parent cycle at {ref!r}.")
        entry = snapshot.entries_by_ref.get(ref)
        if entry is None:
            raise SkeletonUnavailableError(
                f"skeleton cannot restore parent scope for retained output: missing registry ref {ref!r}."
            )
        existing = self._mg.lookup_scope(entry.name)
        if existing is not None:
            _require_scope_identity(
                existing,
                name=entry.name,
                ref=entry.ref,
                instance_id=entry.instance_id,
                context="skeleton restored parent",
            )
            return existing
        if entry.status != "live":
            raise SkeletonUnavailableError(
                "skeleton cannot select retained output because parent scope "
                f"{entry.name!r} is not live; status={entry.status!r}."
            )
        parent = self._scope_from_registry_ref(snapshot, entry.parent_ref, seen=seen | {ref})
        return self._mg.restore_scope(
            name=entry.name,
            ref=entry.ref,
            instance_id=entry.instance_id,
            creation_oid=entry.creation_oid,
            parent=parent,
            world_id=entry.world_id,
            isolated=entry.isolation_mode == "isolated",
        )

    def _publish_frontier(self, trace_ids: TraceRef, terminal_fact_id: str) -> Any:
        return publish_execution_frontier(
            cast("Any", self.trace_store),
            TRUSTED_APPEND_CONTEXT,
            frontier_id=trace_ids.frontier_id,
            target_execution_id=trace_ids.execution_id,
            through_fact_id=terminal_fact_id,
        )

    def _completed_run_from_execution(
        self,
        run_id: str,
        trace_ids: TraceRef,
        execution: Execution,
        *,
        trace_slice: Any | None = None,
    ) -> CompletedRun:
        if execution.status != "succeeded":
            raise SkeletonUnavailableError(f"skeleton run is not completed successfully: {execution.status}.")
        outputs: dict[str, RunOutput] = {}
        descriptor_records = self._project_trace_output_descriptors(trace_ids, trace_slice)
        if trace_slice is not None and not descriptor_records:
            raise SkeletonUnavailableError("skeleton run has no trace-owned RunOutput descriptors.")
        raw_outputs: dict[str, tuple[Any, RunOutputDescriptorLocator | None]]
        if descriptor_records:
            extra_outputs = set(execution.outputs) - set(descriptor_records)
            if extra_outputs:
                raise SkeletonUnavailableError(
                    "skeleton execution output mirror contains outputs without trace-owned descriptors."
                )
            descriptor_records = {
                output_key: self._resolve_trace_output_descriptor(record.locator, trace_slice=trace_slice)
                for output_key, record in descriptor_records.items()
            }
            raw_outputs = {
                output_key: (record.citation_payload, record.locator)
                for output_key, record in descriptor_records.items()
            }
        else:
            raw_outputs = {output_key: (raw_output, None) for output_key, raw_output in execution.outputs.items()}
        for output_key, (raw_output, descriptor_locator) in raw_outputs.items():
            if not isinstance(raw_output, Mapping) or raw_output.get("schema") != RUN_OUTPUT_SCHEMA:
                raise SkeletonUnavailableError("skeleton run has non-RunOutput output metadata.")
            citation = _run_output_citation(raw_output, descriptor_locator=descriptor_locator)
            if output_key != citation.descriptor.output_name:
                raise SkeletonUnavailableError(
                    "skeleton run output key disagrees with trace citation: "
                    f"{output_key!r} != {citation.descriptor.output_name!r}."
                )
            if output_key in execution.outputs and execution.outputs.get(output_key) != dict(raw_output):
                raise SkeletonUnavailableError(
                    "skeleton trace-owned RunOutput descriptor disagrees with execution output mirror."
                )
            _require_output_alias_disabled(raw_output)
            validated_output = self._validated_output_payload(raw_output, trace_ids=trace_ids)
            outputs[output_key] = RunOutput(self, validated_output, descriptor_locator)
        if not outputs:
            raise SkeletonUnavailableError("skeleton run has no RunOutput metadata.")
        return CompletedRun(
            run_id=run_id,
            inputs=execution.inputs,
            outputs=outputs,
            trace_ref=trace_ids,
            execution=execution,
        )

    def _project_trace_output_descriptors(
        self,
        trace_ids: TraceRef,
        trace_slice: Any | None,
    ) -> dict[str, ProjectedRunOutputDescriptor]:
        if trace_slice is None:
            return {}
        try:
            return project_run_output_descriptors(
                trace_slice,
                trace_ids.execution_id,
                frontier_id=trace_ids.frontier_id,
            )
        except (TypeError, ValueError) as exc:
            raise SkeletonUnavailableError("skeleton RunOutput descriptor projection failed.") from exc

    def _resolve_trace_output_descriptor(
        self,
        locator: RunOutputDescriptorLocator,
        *,
        trace_slice: Any | None = None,
    ) -> ProjectedRunOutputDescriptor:
        try:
            if trace_slice is not None:
                return resolve_run_output_descriptor(trace_slice, locator)
            return resolve_run_output_descriptor_from_store(self.trace_store, TRUSTED_READ_CONTEXT, locator)
        except (TraceStoreError, TypeError, ValueError) as exc:
            raise SkeletonUnavailableError("skeleton RunOutput descriptor locator resolution failed.") from exc

    def _run_output_from_descriptor_record(
        self,
        output_key: str,
        record: ProjectedRunOutputDescriptor,
    ) -> RunOutput:
        raw_output = record.citation_payload
        if raw_output.get("schema") != RUN_OUTPUT_SCHEMA:
            raise SkeletonUnavailableError("skeleton run has non-RunOutput output metadata.")
        try:
            citation = _run_output_citation(raw_output, descriptor_locator=record.locator)
        except ValueError as exc:
            raise SkeletonUnavailableError("skeleton RunOutput descriptor locator disagrees with citation.") from exc
        if output_key != citation.descriptor.output_name:
            raise SkeletonUnavailableError(
                "skeleton run output key disagrees with trace citation: "
                f"{output_key!r} != {citation.descriptor.output_name!r}."
            )
        _require_output_alias_disabled(raw_output)
        validated_output = self._validated_output_payload(raw_output)
        return RunOutput(self, validated_output, record.locator)

    @staticmethod
    def _trace_ids(run_id: str) -> TraceRef:
        execution_id = execution_id_for(f"skeleton:{run_id}:create")
        return TraceRef(
            run_id=run_id,
            execution_id=execution_id,
            frontier_id=f"frontier:skeleton:{run_id}:terminal",
        )


def _task_ref(task: Callable[..., Any]) -> str:
    module = getattr(task, "__module__", "")
    qualname = getattr(task, "__qualname__", getattr(task, "__name__", repr(task)))
    return f"{module}.{qualname}" if module else str(qualname)


def _operation_id(run_id: str, suffix: str) -> str:
    return f"skeleton-{_safe_ref_token(run_id)}-{_safe_ref_token(suffix)}"


def _safe_ref_token(value: str) -> str:
    safe = [char if char.isalnum() or char in "._-" else "-" for char in value]
    token = "".join(safe).strip(".-/")
    return token or "run"


def _scope_payload(scope: Any) -> dict[str, object]:
    return {
        "name": scope.name,
        "ref": scope.ref,
        "instance_id": scope.instance_id,
        "world_id": scope.world_id,
    }


def _require_scope_identity(scope: Any, *, name: str, ref: str, instance_id: str, context: str) -> None:
    if scope.name != name or scope.ref != ref or scope.instance_id != instance_id:
        raise SkeletonUnavailableError(
            f"{context} identity mismatch: expected {name!r} ({ref}, {instance_id}), "
            f"got {scope.name!r} ({scope.ref}, {scope.instance_id})."
        )


def _require_binding_name(binding: str) -> None:
    if not isinstance(binding, str) or not binding:
        raise SkeletonUnavailableError("skeleton binding names must be non-empty strings.")


def _require_workspace_repo_binding(binding: str) -> None:
    _require_binding_name(binding)
    if binding != "workspace":
        raise SkeletonUnavailableError(
            "skeleton GitRepoHandle is tree-backed and currently only supports "
            f"binding='workspace'; got {binding!r}. Use retained-output query/settlement for non-workspace bindings."
        )


def _require_repo_authority(authority: str) -> None:
    if authority not in {"readonly", "readwrite"}:
        raise SkeletonUnavailableError(f"skeleton GitRepoHandle authority is unsupported: {authority!r}")


def _input_handle_bindings(args: tuple[Any,...], kwargs: Mapping[str, Any]) -> frozenset[str]:
    bindings = {item.binding for item in args if isinstance(item, GitRepoHandle)}
    bindings.update(value.binding for value in kwargs.values() if isinstance(value, GitRepoHandle))
    return frozenset(bindings)


def _payload_trace_ref(payload: Mapping[str, Any]) -> TraceRef | None:
    fields = ("trace_run_id", "trace_execution_id", "trace_frontier_id")
    present = [field for field in fields if field in payload]
    if not present:
        return None
    if len(present) != len(fields):
        raise SkeletonUnavailableError("skeleton run output has incomplete trace ownership metadata.")
    run_id = payload["trace_run_id"]
    execution_id = payload["trace_execution_id"]
    frontier_id = payload["trace_frontier_id"]
    if not all(isinstance(value, str) and value for value in (run_id, execution_id, frontier_id)):
        raise SkeletonUnavailableError("skeleton run output has malformed trace ownership metadata.")
    return TraceRef(run_id=run_id, execution_id=execution_id, frontier_id=frontier_id)


def _run_output_state(state: str) -> RunOutputState:
    if state not in {"unconsumed", "selected", "applied", "released", "discarded", "invalid"}:
        raise SkeletonUnavailableError(f"skeleton run output has unsupported state: {state!r}.")
    return cast("RunOutputState", state)


def _run_output_identity(payload: Mapping[str, Any]) -> RunOutputIdentity:
    parent_scope_instance_id = payload.get("parent_scope_instance_id")
    return run_output_identity_for(
        output_name=str(payload["output_name"]),
        binding=str(payload["binding"]),
        parent_scope_name=str(payload["parent_scope_name"]),
        parent_ref=str(payload["parent_ref"]),
        parent_scope_instance_id=str(parent_scope_instance_id) if parent_scope_instance_id is not None else None,
        scope_name=str(payload["scope_name"]),
        scope_ref=str(payload["scope_ref"]),
        scope_instance_id=str(payload["scope_instance_id"]),
        candidate_id=str(payload["candidate_id"]),
        candidate_head=str(payload["candidate_head"]),
        output_world_oid=str(payload["output_world_oid"]),
        handoff_ref=str(payload["handoff_ref"]),
    )


def _run_output_descriptor(payload: Mapping[str, Any]) -> RunOutputDescriptor:
    store_id = str(payload["store_id"])
    return RunOutputDescriptor(
        output_name=str(payload["output_name"]),
        world_binding=str(payload["binding"]),
        store_id=store_id,
        resource_id=str(payload["resource_id"]),
        materialization_kind=_materialization_kind_for_store(store_id),
    )


def _run_output_owner(payload: Mapping[str, Any]) -> RunOutputOwner:
    trace_ref = _payload_trace_ref(payload)
    if trace_ref is None:
        return RunOutputOwner(kind="retained-query")
    return RunOutputOwner(
        kind="run",
        run_id=trace_ref.run_id,
        execution_id=trace_ref.execution_id,
        frontier_id=trace_ref.frontier_id,
    )


def _run_output_citation(
    payload: Mapping[str, Any],
    *,
    descriptor_locator: RunOutputDescriptorLocator | None = None,
) -> RunOutputCitation:
    if payload.get("schema") != RUN_OUTPUT_SCHEMA:
        raise SkeletonUnavailableError("skeleton run output has unsupported metadata schema.")
    return RunOutputCitation(
        identity=_run_output_identity(payload),
        owner=_run_output_owner(payload),
        descriptor=_run_output_descriptor(payload),
        parent_basis_world_oid=str(payload["parent_basis_world_oid"]),
        candidate_ref=str(payload["candidate_ref"]),
        store_id=str(payload["store_id"]),
        resource_id=str(payload["resource_id"]),
        changed_paths=tuple(str(path) for path in payload.get("changed_paths", ())),
        descriptor_locator=descriptor_locator,
    )


def _require_output_alias_disabled(payload: Mapping[str, Any]) -> None:
    output_name = str(payload["output_name"])
    binding = str(payload["binding"])
    if output_name != binding:
        raise SkeletonUnavailableError(
            "skeleton output aliases are not enabled: "
            f"output_name {output_name!r} must equal world binding {binding!r}."
        )


def _materialization_kind_for_store(store_id: str) -> RunOutputMaterializationKind:
    return "tree" if store_id == "store_workspace" else "external"


def _complete_execution_with_run_output_descriptors_batch(
    *,
    append_intent_id: str,
    execution_id: str,
    outputs: dict[str, Any],
    caused_by: tuple[str,...] = (),
) -> AppendBatch:
    fact_drafts = tuple(
        run_output_descriptor_fact(
            execution_id=execution_id,
            output_name=output_name,
            world_binding=str(output_payload["binding"]),
            citation=dict(output_payload),
        )
        for output_name, output_payload in outputs.items()
        if isinstance(output_payload, Mapping)
    )
    if len(fact_drafts) != len(outputs):
        raise SkeletonUnavailableError("skeleton run has non-RunOutput output metadata.")
    return AppendBatch(
        append_intent_id=append_intent_id,
        groups=(
            AppendGroup(
                trace_owner_id=execution_id,
                causal_parents=caused_by,
                fact_drafts=(
                    *fact_drafts,
                    execution_completed(execution_id=execution_id, outputs=outputs),
                ),
            ),
        ),
    )


def _output_payload(
    *,
    parent: Any,
    child: Any,
    handoff: Any,
    retained_handle: Any,
    trace_ref: TraceRef,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": RUN_OUTPUT_SCHEMA,
        "output_name": handoff.binding,
        "parent_scope_name": parent.name,
        "parent_ref": parent.ref,
        "scope_name": child.name,
        "scope_ref": child.ref,
        "scope_instance_id": child.instance_id,
        "binding": handoff.binding,
        "output_world_oid": handoff.output_world_oid,
        "handoff_ref": handoff.handoff_ref,
        "candidate_id": handoff.candidate_id,
        "candidate_head": handoff.candidate_head,
        "candidate_ref": handoff.candidate_ref,
        "parent_basis_world_oid": handoff.parent_basis_world_oid,
        "store_id": handoff.store_id,
        "resource_id": handoff.resource_id,
        "materialization_kind": _materialization_kind_for_store(handoff.store_id),
        "retained_handle_head": retained_handle.head,
        "changed_paths": list(handoff.changed_paths),
        "trace_run_id": trace_ref.run_id,
        "trace_execution_id": trace_ref.execution_id,
        "trace_frontier_id": trace_ref.frontier_id,
    }
    if parent.ref != "refs/vcscore/ground":
        payload["parent_scope_instance_id"] = parent.instance_id
    return payload


def _trace_value(value: Any) -> Any:
    if isinstance(value, GitRepoHandle):
        return value.trace_payload()
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, bytes):
        return {"kind": "bytes", "hex": value.hex()}
    if isinstance(value, Mapping):
        return {str(key): _trace_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_trace_value(item) for item in value]
    return {"kind": type(value).__name__, "repr": repr(value)}


__all__ = [
    "NESTED_OPERATIONS_ENV",
    "SEAL_AND_SELECT_ENV",
    "SKELETON_ENV",
    "CompletedRun",
    "GitRepoHandle",
    "RetainedRunOutput",
    "RunOutput",
    "RunOutputCitation",
    "RunOutputDescriptor",
    "RunOutputDescriptorLocator",
    "RunOutputIdentity",
    "RunOutputMaterializationKind",
    "RunOutputOwner",
    "RunOutputRef",
    "Session",
    "SkeletonUnavailableError",
    "TraceRef",
    "WorkspaceBinding",
]
