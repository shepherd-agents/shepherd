"""Thin workflow facade over the public workspace-control run spine."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from shepherd_dialect.workspace_control.errors import WorkspaceControlError
from shepherd_dialect.workspace_control.flow_context import (
    FLOW_SCHEMA,
    FLOW_TRACE_SCHEMA,
    FlowRunContext,
)
from shepherd_dialect.workspace_control.identities import RunRef, TaskRefInput, coerce_run_ref
from shepherd_dialect.workspace_control.input_refs import iter_run_artifact_input_refs
from shepherd_dialect.workspace_control.queries import get_run_args
from shepherd_dialect.workspace_control.run_ledger import RunLedgerStore, publish_flow_record, utc_now

if TYPE_CHECKING:
    from shepherd_runtime.nucleus import GitRepo

    from shepherd_dialect.runtime_options import RuntimeOptions
    from shepherd_dialect.workspace_control.run_handles import WorkspaceRun
    from shepherd_dialect.workspace_control.workspace import ShepherdWorkspace

JsonObject = dict[str, object]


class FlowControlClient:
    """Workspace-scoped workflow metadata operations."""

    def __init__(self, workspace: ShepherdWorkspace) -> None:
        self._workspace = workspace

    def open(self, *, name: str, metadata: Mapping[str, object] | None = None) -> Flow:
        """Open a durable workflow metadata record."""
        _require_name(name, field_name="flow name")
        flow_id = f"flow-{uuid.uuid4().hex[:12]}"
        record: JsonObject = {
            "schema": FLOW_SCHEMA,
            "flow_id": flow_id,
            "name": name,
            "metadata": dict(metadata or {}),
            "created_at": utc_now(),
        }
        publish_flow_record(self._workspace.mg, record)
        return Flow(self._workspace, flow_id=flow_id, name=name, metadata=dict(metadata or {}))

    def get(self, flow_id: str) -> Flow | None:
        """Return one workflow facade from persisted metadata."""
        record = _flow_record(self._workspace, flow_id)
        if record is None:
            return None
        return Flow(
            self._workspace,
            flow_id=_required_str(record, "flow_id"),
            name=_required_str(record, "name"),
            metadata=dict(_optional_mapping(record, "metadata")),
        )

    def list(self) -> tuple[Flow, ...]:
        """Return all durable workflow facades visible in the selected run ledger."""
        raw_flows = RunLedgerStore(self._workspace.mg).list_flows()
        flows = []
        for raw in raw_flows:
            if not isinstance(raw, Mapping):
                raise WorkspaceControlError("flow records must be objects")
            flows.append(
                Flow(
                    self._workspace,
                    flow_id=_required_str(raw, "flow_id"),
                    name=_required_str(raw, "name"),
                    metadata=dict(_optional_mapping(raw, "metadata")),
                )
            )
        return tuple(flows)


@dataclass(frozen=True, eq=False)
class Flow:
    """Notebook-friendly workflow metadata facade over ``workspace.run(...)``."""

    _workspace: ShepherdWorkspace = field(repr=False, compare=False)
    flow_id: str
    name: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def fork(
        self,
        task_ref: TaskRefInput,
        *,
        repo: GitRepo,
        name: str,
        args: Mapping[str, Any] | None = None,
        after: Sequence[WorkspaceRun | RunRef | str] = (),
        runtime: Mapping[str, object] | RuntimeOptions | None = None,
        placement: Literal["auto", "advisory", "jail"] = "auto",
        may: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> WorkspaceRun:
        """Run one task through the workspace-control spine and record flow metadata."""
        _require_name(name, field_name="flow run name")
        after_refs = tuple(_coerce_after_ref(item) for item in after)
        flow_context = FlowRunContext(
            flow_id=self.flow_id,
            name=name,
            sequence=len(_flow_run_records(self._workspace, self.flow_id)),
            after=after_refs,
            metadata=dict(metadata or {}),
        )
        run = self._workspace._run_with_flow_context(
            task_ref,
            repo=repo,
            flow_context=flow_context,
            args=args,
            may=may,
            placement=placement,
            runtime=runtime,
        )
        return run.refresh()

    def runs(self) -> tuple[WorkspaceRun, ...]:
        """Return the current run wrappers attached to this flow."""
        from shepherd_dialect.workspace_control.run_handles import WorkspaceRun

        runs: list[WorkspaceRun] = []
        for flow_run in _flow_run_records(self._workspace, self.flow_id):
            run_ref = _required_str(flow_run, "run_ref")
            record = self._workspace.runs.show(RunRef(id=run_ref))
            if record is not None:
                runs.append(WorkspaceRun(self._workspace, record))
        return tuple(runs)

    def trace(self) -> JsonObject:
        """Return a read-only workflow trace projection from durable records."""
        flow_record = _flow_record(self._workspace, self.flow_id)
        if flow_record is None:
            raise WorkspaceControlError(f"flow {self.flow_id!r} is no longer visible")
        flow_runs = _flow_run_records(self._workspace, self.flow_id)
        events: list[JsonObject] = [
            {
                "id": f"{self.flow_id}:opened",
                "kind": "flow.opened",
                "flow_id": self.flow_id,
                "name": self.name,
                "metadata": dict(self.metadata),
            }
        ]
        edges: list[JsonObject] = []
        for flow_run in flow_runs:
            run_ref = _required_str(flow_run, "run_ref")
            run_record = self._workspace.runs.show(RunRef(id=run_ref))
            flow_run_metadata = dict(_optional_mapping(flow_run, "metadata"))
            events.append(
                {
                    "id": f"{run_ref}:flow-fork",
                    "kind": "flow.fork.requested",
                    "flow_id": self.flow_id,
                    "run_ref": run_ref,
                    "name": _required_str(flow_run, "name"),
                    "after": list(_optional_sequence(flow_run, "after")),
                    "metadata": flow_run_metadata,
                }
            )
            _append_flow_label_events(events, flow_id=self.flow_id, run_ref=run_ref, metadata=flow_run_metadata)
            if run_record is not None:
                events.append(
                    {
                        "id": f"{run_ref}:lifecycle",
                        "kind": "run.lifecycle",
                        "run_ref": run_ref,
                        "task_id": run_record.task_id,
                        "task_version": run_record.task_version,
                        "status": run_record.status,
                        "enforcement": run_record.enforcement,
                    }
                )
                for execution in run_record.task_executions:
                    if not _append_provider_events_from_execution_metadata(
                        events,
                        run_ref=run_ref,
                        execution_id=execution.execution_id,
                        metadata=execution.metadata,
                        status=execution.status,
                    ):
                        provider_id = execution.metadata.get("runtime_provider")
                        if not isinstance(provider_id, str) or not provider_id:
                            continue
                        event: JsonObject = {
                            "id": f"{execution.execution_id}:provider",
                            "kind": "provider.invocation",
                            "run_ref": run_ref,
                            "execution_id": execution.execution_id,
                            "provider_id": provider_id,
                            "status": execution.status,
                            "launched_confined": execution.metadata.get("launch_confined_attempted"),
                            "source": "task_execution.metadata.runtime_provider",
                            "evidence_role": "provider_provenance",
                        }
                        model = execution.metadata.get("runtime_model")
                        if isinstance(model, str) and model:
                            event["model"] = model
                        events.append(event)
                for output_name, citation in run_record.outputs.items():
                    events.append(
                        {
                            "id": f"{run_ref}:output:{output_name}:published",
                            "kind": "run.output.published",
                            "run_ref": run_ref,
                            "output_name": output_name,
                            "output_id": citation.output_id,
                            "binding": citation.binding,
                            "output_world_oid": citation.output_world_oid,
                        }
                    )
                try:
                    current_outputs = self._workspace.runs.outputs(run_ref=RunRef(id=run_ref))
                except WorkspaceControlError:
                    current_outputs = ()
                for output in current_outputs:
                    if output.state == "unconsumed":
                        continue
                    events.append(
                        {
                            "id": f"{run_ref}:output:{output.output_name}:settled",
                            "kind": "run.output.settled",
                            "run_ref": run_ref,
                            "output_name": output.output_name,
                            "output_id": output.output_id,
                            "state": output.state,
                            "settlement_ref": output.settlement_ref,
                        }
                    )
                if run_record.args_ref is not None:
                    args_payload = get_run_args(self._workspace.mg, run_record.args_ref)
                    if args_payload is not None:
                        for index, ref in enumerate(iter_run_artifact_input_refs(args_payload.get("payload", {}))):
                            input_event_id = f"{run_ref}:input:{index}"
                            events.append(
                                {
                                    "id": input_event_id,
                                    "kind": "run.output.input",
                                    "run_ref": run_ref,
                                    "source_run_ref": ref.run_ref,
                                    "source_output_id": ref.output_id,
                                    "path": ref.path,
                                    "label": ref.label,
                                    "content_digest": ref.content_digest,
                                }
                            )
                            edges.append(
                                {
                                    "id": f"input:{ref.output_id}:{index}->{run_ref}",
                                    "kind": "data_dependency",
                                    "source": ref.output_id,
                                    "target": run_ref,
                                    "label": ref.label,
                                }
                            )
            for parent_ref in _optional_sequence(flow_run, "after"):
                edges.append(
                    {
                        "id": f"after:{parent_ref}->{run_ref}",
                        "kind": "causal_after",
                        "source": parent_ref,
                        "target": run_ref,
                    }
                )
        return {
            "schema": FLOW_TRACE_SCHEMA,
            "flow_id": self.flow_id,
            "name": self.name,
            "events": events,
            "edges": edges,
        }

    def to_json(self) -> JsonObject:
        """Return a compact JSON-shaped flow projection."""
        return {
            "schema": FLOW_SCHEMA,
            "flow_id": self.flow_id,
            "name": self.name,
            "metadata": dict(self.metadata),
            "runs": [run.to_json() for run in self.runs()],
        }


def _flow_record(workspace: ShepherdWorkspace, flow_id: str) -> Mapping[str, object] | None:
    _require_name(flow_id, field_name="flow_id")
    raw = RunLedgerStore(workspace.mg).get_flow(flow_id)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise WorkspaceControlError(f"flow record {flow_id!r} must be an object")
    return raw


def _flow_run_records(workspace: ShepherdWorkspace, flow_id: str) -> tuple[Mapping[str, object], ...]:
    records = []
    for raw in RunLedgerStore(workspace.mg).list_flow_runs(flow_id=flow_id):
        if not isinstance(raw, Mapping):
            raise WorkspaceControlError("flow run records must be objects")
        records.append(raw)
    return tuple(sorted(records, key=_flow_run_sort_key))


def _flow_run_sort_key(value: Mapping[str, object]) -> tuple[int, str]:
    raw_sequence = value.get("sequence", 0)
    sequence = raw_sequence if isinstance(raw_sequence, int) else 0
    raw_created_at = value.get("created_at", "")
    created_at = raw_created_at if isinstance(raw_created_at, str) else ""
    return sequence, created_at


def _append_flow_label_events(
    events: list[JsonObject],
    *,
    flow_id: str,
    run_ref: str,
    metadata: Mapping[str, object],
) -> None:
    logical_boundary = _metadata_label(metadata, "logical_boundary")
    if logical_boundary is not None:
        events.append(
            {
                "id": f"{run_ref}:logical-boundary",
                "kind": "flow.logical_boundary",
                "flow_id": flow_id,
                "run_ref": run_ref,
                "label": logical_boundary,
            }
        )
    failed_run = _metadata_label(metadata, "failed_run")
    if failed_run is not None:
        events.append(
            {
                "id": f"{run_ref}:failed-run",
                "kind": "flow.failed_run",
                "flow_id": flow_id,
                "run_ref": run_ref,
                "label": failed_run,
            }
        )
    retry_run = _metadata_label(metadata, "retry_run")
    if retry_run is not None:
        events.append(
            {
                "id": f"{run_ref}:retry-run",
                "kind": "flow.retry_run",
                "flow_id": flow_id,
                "run_ref": run_ref,
                "label": retry_run,
            }
        )


def _metadata_label(metadata: Mapping[str, object], key: str) -> str | None:
    raw = metadata.get(key)
    if raw is True:
        return key
    if isinstance(raw, str) and raw:
        return raw
    return None


def _append_provider_events_from_execution_metadata(
    events: list[JsonObject],
    *,
    run_ref: str,
    execution_id: str,
    metadata: Mapping[str, object],
    status: str,
) -> bool:
    raw_events = metadata.get("provider_events")
    if not isinstance(raw_events, Sequence) or isinstance(raw_events, str | bytes | bytearray):
        return False
    appended = False
    for index, raw_event in enumerate(raw_events):
        if not isinstance(raw_event, Mapping):
            continue
        provider_event_kind = raw_event.get("kind")
        provider_id = raw_event.get("provider_id")
        invocation_id = raw_event.get("invocation_id")
        event_id = raw_event.get("event_id")
        if not all(isinstance(value, str) and value for value in (provider_event_kind, provider_id, invocation_id)):
            continue
        event: JsonObject = {
            "id": f"{execution_id}:provider:{index}",
            "kind": _workflow_provider_event_kind(provider_event_kind),
            "run_ref": run_ref,
            "execution_id": execution_id,
            "provider_id": provider_id,
            "invocation_id": invocation_id,
            "provider_event_kind": provider_event_kind,
            "status": _provider_event_status(provider_event_kind, fallback=status),
            "source": "task_execution.metadata.provider_events",
            "evidence_role": "provider_provenance",
        }
        if isinstance(event_id, str) and event_id:
            event["event_id"] = event_id
        sequence = raw_event.get("sequence")
        if isinstance(sequence, int):
            event["sequence"] = sequence
        model = raw_event.get("model")
        if isinstance(model, str) and model:
            event["model"] = model
        tool_call_id = raw_event.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            event["tool_call_id"] = tool_call_id
        payload = raw_event.get("payload")
        if isinstance(payload, Mapping):
            event["payload"] = dict(payload)
        events.append(event)
        appended = True
    return appended


def _workflow_provider_event_kind(provider_event_kind: object) -> str:
    if provider_event_kind in {"model.call", "model.turn"}:
        return "provider.model"
    if provider_event_kind in {"tool.call.started", "tool.call.completed", "tool.call.rejected"}:
        return "provider.tool_call"
    return "provider.invocation"


def _provider_event_status(provider_event_kind: object, *, fallback: str) -> str:
    if provider_event_kind == "provider.invocation.started":
        return "started"
    if provider_event_kind == "provider.invocation.completed":
        return "completed"
    if provider_event_kind == "provider.invocation.failed":
        return "failed"
    if provider_event_kind == "tool.call.rejected":
        return "rejected"
    if provider_event_kind == "tool.call.completed":
        return "completed"
    if provider_event_kind == "tool.call.started":
        return "started"
    return fallback


def _coerce_after_ref(value: WorkspaceRun | RunRef | str) -> str:
    if hasattr(value, "run_ref"):
        return str(value.run_ref)
    return coerce_run_ref(value)


def _require_name(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _required_str(value: Mapping[str, object], field_name: str) -> str:
    raw = value.get(field_name)
    if not isinstance(raw, str) or not raw:
        raise WorkspaceControlError(f"{field_name} must be a non-empty string")
    return raw


def _optional_mapping(value: Mapping[str, object], field_name: str) -> Mapping[str, object]:
    raw = value.get(field_name, {})
    if not isinstance(raw, Mapping):
        raise WorkspaceControlError(f"{field_name} must be an object")
    return raw


def _optional_sequence(value: Mapping[str, object], field_name: str) -> tuple[str, ...]:
    raw = value.get(field_name, ())
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes | bytearray):
        raise WorkspaceControlError(f"{field_name} must be a list")
    result = []
    for item in raw:
        if not isinstance(item, str) or not item:
            raise WorkspaceControlError(f"{field_name} values must be non-empty strings")
        result.append(item)
    return tuple(result)
