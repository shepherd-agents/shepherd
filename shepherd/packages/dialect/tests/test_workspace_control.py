from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import pytest
from shepherd2 import AppendBatch, AppendContext, AppendGroup, OwnerCutoffSpec, ReadContext, SQLiteTraceStore
from shepherd2.schemas.run_outputs import (
    RUN_OUTPUT_SCHEMA,
    ProjectedRunOutputDescriptor,
    RunOutputDescriptorLocator,
    run_output_descriptor_locator_from_payload,
    run_output_id_for,
)
from shepherd_kernel_v3_reference.proof_envelope import ProofEnvelope, ProofProfile, ProofStrength
from shepherd_kernel_v3_reference.vcscore_certificate import (
    VCSCORE_RUN_EXTENSION_NAME,
    VCSCORE_RUN_THEOREM_IDS,
    vcscore_run_certificate_from_run_record,
    vcscore_run_proof_envelope,
)
from vcs_core.keyed_json_tree import KeyedJsonTreeStore
from vcs_core.types import RetainedOutputIdentity, RetainedOutputQueryResult, ScopeInfo, SealCandidateHandoff

from shepherd_dialect.workspace_control import (
    RUN_LEDGER_BINDING,
    RUN_LEDGER_SCHEMA,
    TASK_LEDGER_BINDING,
    TASK_LEDGER_SCHEMA,
    PendingEffectRef,
    RetainedWorkspaceOutputPublicationError,
    RunAuthorityContext,
    RunExecutionEvidence,
    RunLaunchContext,
    RunOperationRefs,
    RunOutputCitationRef,
    RunOutputResolutionError,
    RunOutputResolver,
    RunRecord,
    RunRetainedCustody,
    RunSummary,
    RunTerminalization,
    ShepherdWorkspace,
    TaskArtifactLock,
    TaskArtifactRef,
    TaskDefinitionVersion,
    TaskExecutionRecord,
    TaskResolutionRecord,
    TaskSummary,
    TraceDescriptorNotResolvedError,
    TraceNotMaterializedError,
    TraceRef,
    get_task,
    list_runs,
    list_tasks,
    outputs_for_run,
    read_run_ledger_payload,
    read_task_ledger_payload,
    resolve_task,
    run_has_published_workspace_output,
    run_output_citations,
    run_output_publication_from_seal_handoff,
    run_vcscore_projection,
    run_workspace_output_world_oid,
    show_run,
    trace_run,
)
from shepherd_dialect.workspace_control.flows import FlowControlClient
from shepherd_dialect.workspace_control.input_refs import build_run_args_payload
from shepherd_dialect.workspace_control.output_publication import publish_run_output_descriptor
from shepherd_dialect.workspace_control.output_transition import publish_retained_workspace_output
from shepherd_dialect.workspace_control.outputs import run_output_publication_from_retained_row
from shepherd_dialect.workspace_control.run_ledger import RunLedgerPublishError, RunLedgerStore

if TYPE_CHECKING:
    from vcs_core.spi import KeyedJsonTreeDraft

TRACE_APPEND_CONTEXT = AppendContext(
    actor_ref="runtime:test",
    presented_witness_refs=("trusted:internal",),
    schema_version_set="shepherd2-slice-a",
    trust_mode="internal",
)
TRACE_READ_CONTEXT = ReadContext(actor_ref="reader")


@dataclass(frozen=True)
class FakeSelectedRevision:
    payload: dict[str, object]
    head: str


@dataclass(frozen=True)
class FakeExecOutcome:
    oids: tuple[str, ...]


class FakeVcsCore:
    def __init__(
        self,
        payloads: dict[str, dict[str, object]],
        traces: dict[str, dict[str, object]] | None = None,
        retained_outputs: tuple[RetainedOutputQueryResult, ...] = (),
    ) -> None:
        self.ground = object()
        self.payloads = payloads
        self.traces = traces or {}
        self.retained_outputs = retained_outputs
        self.reads: list[tuple[str, object]] = []
        self.direct_retained_reads: list[RetainedOutputIdentity] = []
        self.retained_reads: list[tuple[object, str | None, str | None]] = []
        self.exec_calls: list[tuple[str, str, object, str | None]] = []
        self.published_put_paths: list[tuple[str, ...]] = []
        self.published_delete_paths: list[tuple[str, ...]] = []
        self.entries: dict[str, dict[str, dict[str, object]]] = {}
        self._head_index = 0

    def read_selected_binding_revision(self, binding: str, *, scope: object = None) -> dict[str, object] | None:
        self.reads.append((binding, scope))
        return self.payloads.get(binding)

    def read_selected_binding_revision_with_head(
        self,
        binding: str,
        *,
        scope: object = None,
    ) -> FakeSelectedRevision | None:
        self.reads.append((binding, scope))
        payload = self.payloads.get(binding)
        if payload is None:
            return None
        return FakeSelectedRevision(payload=payload, head=f"head-{binding}-{self._head_index}")

    def read_trace_revision(self, head: str | None = None, *, scope: object = None) -> dict[str, object] | None:
        del scope
        return None if head is None else self.traces.get(head)

    def read_selected_binding_json_entry(
        self,
        binding: str,
        path: str,
        *,
        scope: object = None,
    ) -> dict[str, object] | None:
        self.reads.append((binding, scope))
        value = self.entries.get(binding, {}).get(path)
        return None if value is None else dict(value)

    def read_selected_binding_json_entries(
        self,
        binding: str,
        prefix: str,
        *,
        scope: object = None,
    ) -> tuple[tuple[str, dict[str, object]], ...]:
        self.reads.append((binding, scope))
        rows = [
            (path, dict(value))
            for path, value in self.entries.get(binding, {}).items()
            if path == prefix or path.startswith(f"{prefix}/")
        ]
        return tuple(sorted(rows, key=lambda item: item[0]))

    def exec(
        self,
        binding: str,
        command: str,
        *,
        scope: object = None,
        payload: dict[str, object] | None = None,
        content: KeyedJsonTreeDraft | None = None,
        expected_head: str | None = None,
        authority: object = None,
    ) -> FakeExecOutcome:
        del authority
        self.exec_calls.append((binding, command, scope, expected_head))
        if binding != RUN_LEDGER_BINDING or command != "publish" or payload is None:
            raise AssertionError(f"unexpected fake exec: {binding}.{command}")
        self._head_index += 1
        oid = f"run-ledger-head-{self._head_index}"
        if content is not None:
            self.published_put_paths.append(tuple(f"{content.content_root}/{item.path}" for item in content.puts))
            self.published_delete_paths.append(tuple(f"{content.content_root}/{path}" for path in content.deletes))
            binding_entries = self.entries.setdefault(binding, {})
            for item in content.puts:
                binding_entries[f"{content.content_root}/{item.path}"] = dict(item.payload)
            for path in content.deletes:
                binding_entries.pop(f"{content.content_root}/{path}", None)
        else:
            self.published_put_paths.append(())
            self.published_delete_paths.append(())
        self.payloads[binding] = payload
        return FakeExecOutcome(oids=(oid,))

    def list_retained_outputs(
        self,
        *,
        parent: object = None,
        binding: str | None = None,
        state: str | None = None,
    ) -> tuple[RetainedOutputQueryResult, ...]:
        self.retained_reads.append((parent, binding, state))
        return tuple(
            row
            for row in self.retained_outputs
            if (binding is None or row.binding == binding) and (state is None or row.state == state)
        )

    def get_retained_output(self, identity: RetainedOutputIdentity) -> RetainedOutputQueryResult | None:
        self.direct_retained_reads.append(identity)
        for row in self.retained_outputs:
            if _retained_identity_matches_row(identity, row):
                return row
        return None


class FailingPublishVcsCore(FakeVcsCore):
    def __init__(
        self,
        payloads: dict[str, dict[str, object]],
        traces: dict[str, dict[str, object]] | None = None,
        retained_outputs: tuple[RetainedOutputQueryResult, ...] = (),
    ) -> None:
        super().__init__(payloads, traces=traces, retained_outputs=retained_outputs)
        self.fail_next_publish = True

    def exec(
        self,
        binding: str,
        command: str,
        *,
        scope: object = None,
        payload: dict[str, object] | None = None,
        content: KeyedJsonTreeDraft | None = None,
        expected_head: str | None = None,
        authority: object = None,
    ) -> FakeExecOutcome:
        if self.fail_next_publish:
            self.fail_next_publish = False
            raise RuntimeError("run ledger unavailable")
        return super().exec(
            binding,
            command,
            scope=scope,
            payload=payload,
            content=content,
            expected_head=expected_head,
            authority=authority,
        )


def task_version(
    task_id: str = "tasks.fix_bug",
    version: str = "v1",
    *,
    status: str = "active",
) -> TaskDefinitionVersion:
    return TaskDefinitionVersion(
        task_id=task_id,
        version=version,
        import_path="pkg.tasks:fix_bug",
        schema_digest=f"sha256:{version}",
        may_default="ReadOnly",
        status=status,  # type: ignore[arg-type]
        source_identity="world:w1:path:pkg/tasks.py",
        signature_schema={"type": "object"},
        metadata={"owner": "tests"},
        derived_from=("trace:run-1",),
        created_at="2026-06-20T00:00:00Z",
    )


def task_artifact_lock(
    task_id: str = "tasks.fix_bug",
    version: str = "v1",
) -> TaskArtifactLock:
    ref = TaskArtifactRef(
        binding="shepherd.tasks.artifacts",
        store_id="shepherd.workspace_control.task_artifacts",
        resource_id="task_artifacts",
        head=f"artifact-head-{version}",
        artifact_digest=f"sha256:artifact:{version}",
    )
    return TaskArtifactLock(
        task_id=task_id,
        version=version,
        artifact_ref=ref,
        artifact_digest=ref.artifact_digest,
        schema_digest=f"sha256:schema:{version}",
    )


def retained_output_row(*, state: str = "unconsumed") -> RetainedOutputQueryResult:
    return RetainedOutputQueryResult(
        scope_name="child",
        scope_ref="scope-child",
        scope_instance_id="scope-instance",
        parent_ref="refs/vcscore/ground",
        parent_scope_name="ground",
        parent_scope_instance_id=None,
        state=state,  # type: ignore[arg-type]
        binding="workspace",
        output_world_oid="w-out",
        handoff_ref="handoff-1",
        parent_basis_world_oid="w-in",
        store_id="store_workspace",
        resource_id="workspace",
        candidate_id="candidate-1",
        candidate_ref="refs/vcscore/candidates/1",
        candidate_head="candidate-head-1",
        changed_paths=("src/app.py",),
        settlement_ref=None,
    )


def _retained_identity_matches_row(identity: RetainedOutputIdentity, row: RetainedOutputQueryResult) -> bool:
    return (
        row.scope_name == identity.scope_name
        and row.scope_ref == identity.scope_ref
        and row.scope_instance_id == identity.scope_instance_id
        and row.parent_ref == identity.parent_ref
        and row.parent_scope_name == identity.parent_scope_name
        and row.parent_scope_instance_id == identity.parent_scope_instance_id
        and row.binding == identity.binding
        and row.output_world_oid == identity.output_world_oid
        and row.handoff_ref == identity.handoff_ref
        and row.parent_basis_world_oid == identity.parent_basis_world_oid
        and row.store_id == identity.store_id
        and row.resource_id == identity.resource_id
        and row.candidate_id == identity.candidate_id
        and row.candidate_ref == identity.candidate_ref
        and row.candidate_head == identity.candidate_head
    )


def retained_custody(
    row: RetainedOutputQueryResult | None = None,
    **overrides: str,
) -> RunRetainedCustody:
    row = row or retained_output_row()
    values = RunRetainedCustody.from_retained_output(row).to_json()
    values.update(overrides)
    return RunRetainedCustody(**values)


def retained_custody_from_citation(citation: RunOutputCitationRef) -> RunRetainedCustody:
    return RunRetainedCustody.from_output_citation(citation)


def output_citation(
    row: RetainedOutputQueryResult | None = None,
    *,
    trace_ref: TraceRef | None = None,
) -> RunOutputCitationRef:
    trace_ref = trace_ref or TraceRef(run_id="run-1", execution_id="exec-1", frontier_id="frontier-1")
    row = row or retained_output_row()
    output_id = run_output_id_for(
        output_name="workspace",
        binding="workspace",
        parent_ref=row.parent_ref,
        scope_ref=row.scope_ref,
        scope_instance_id=row.scope_instance_id,
        candidate_id=row.candidate_id or "",
        candidate_head=row.candidate_head or "",
        handoff_ref=row.handoff_ref or "",
        output_world_oid=row.output_world_oid or "",
    )
    return RunOutputCitationRef(
        output_name="workspace",
        output_id=output_id,
        trace_ref=trace_ref,
        descriptor_locator={
            "schema": "shepherd2.skeleton.run_output_descriptor_locator.v1",
            "execution_id": trace_ref.execution_id,
            "frontier_id": trace_ref.frontier_id,
            "output_name": "workspace",
            "descriptor_fact_id": "fact-1",
            "schema_ref": "shepherd2.skeleton.run_output_descriptor.v1",
        },
        binding="workspace",
        store_id="store_workspace",
        resource_id="workspace",
        materialization_kind="tree",
        custody_ref=row.handoff_ref or "",
        output_world_oid=row.output_world_oid,
        parent_basis_world_oid=row.parent_basis_world_oid,
    )


def named_output_citation(
    output_name: str,
    *,
    row: RetainedOutputQueryResult | None = None,
    trace_ref: TraceRef | None = None,
) -> RunOutputCitationRef:
    citation = output_citation(row, trace_ref=trace_ref)
    descriptor_locator = dict(citation.descriptor_locator)
    descriptor_locator["output_name"] = output_name
    return replace(
        citation,
        output_name=output_name,
        output_id=f"{citation.output_id}:{output_name}",
        descriptor_locator=descriptor_locator,
    )


def output_descriptor_record(
    citation: RunOutputCitationRef | None = None,
    row: RetainedOutputQueryResult | None = None,
    **overrides: object,
) -> ProjectedRunOutputDescriptor:
    row = row or retained_output_row()
    citation = citation or output_citation(row)
    payload: dict[str, object] = {
        "schema": RUN_OUTPUT_SCHEMA,
        "output_name": citation.output_name,
        "parent_scope_name": row.parent_scope_name,
        "parent_ref": row.parent_ref,
        "scope_name": row.scope_name,
        "scope_ref": row.scope_ref,
        "scope_instance_id": row.scope_instance_id,
        "binding": citation.binding,
        "output_world_oid": row.output_world_oid,
        "handoff_ref": row.handoff_ref,
        "candidate_id": row.candidate_id,
        "candidate_head": row.candidate_head,
        "candidate_ref": row.candidate_ref,
        "parent_basis_world_oid": row.parent_basis_world_oid,
        "store_id": row.store_id,
        "resource_id": row.resource_id,
        "materialization_kind": citation.materialization_kind,
        "retained_handle_head": row.candidate_head,
        "changed_paths": list(row.changed_paths),
        "trace_run_id": citation.trace_ref.run_id,
        "trace_execution_id": citation.trace_ref.execution_id,
        "trace_frontier_id": citation.trace_ref.frontier_id,
    }
    payload.update(overrides)
    if row.parent_scope_instance_id is not None:
        payload["parent_scope_instance_id"] = row.parent_scope_instance_id
    return ProjectedRunOutputDescriptor(
        locator=run_output_descriptor_locator_from_payload(dict(citation.descriptor_locator)),
        citation_payload=payload,
    )


def seal_parent_scope() -> ScopeInfo:
    return ScopeInfo(
        name="ground",
        ref="refs/vcscore/ground",
        instance_id="ground-instance",
        creation_oid="ground-oid",
        world_id="w-in",
    )


def seal_handoff(parent: ScopeInfo | None = None) -> SealCandidateHandoff:
    parent = parent or seal_parent_scope()
    return SealCandidateHandoff(
        seal_operation_id="op-seal-1",
        producer_operation_id="op-run-1",
        scope_name="child",
        scope_ref="scope-child",
        scope_instance_id="scope-instance",
        scope_world_id="scope-world",
        parent_ref=parent.ref,
        parent_basis_world_oid="w-in",
        output_world_oid="w-out",
        binding="workspace",
        store_id="store_workspace",
        resource_id="workspace",
        candidate_id="candidate-1",
        candidate_ref="refs/vcscore/candidates/1",
        candidate_head="candidate-head-1",
        candidate_tuple_digest="candidate-tuple-digest",
        handoff_ref="handoff-1",
        changed_paths=("src/app.py",),
    )


def retained_output_row_for_handoff(
    handoff: SealCandidateHandoff,
    *,
    parent: ScopeInfo,
    state: str = "unconsumed",
    changed_paths: tuple[str, ...] | None = None,
) -> RetainedOutputQueryResult:
    return RetainedOutputQueryResult(
        scope_name=handoff.scope_name,
        scope_ref=handoff.scope_ref,
        scope_instance_id=handoff.scope_instance_id,
        parent_ref=parent.ref,
        parent_scope_name=parent.name,
        parent_scope_instance_id=None if parent.ref == "refs/vcscore/ground" else parent.instance_id,
        state=state,  # type: ignore[arg-type]
        binding=handoff.binding,
        output_world_oid=handoff.output_world_oid,
        handoff_ref=handoff.handoff_ref,
        parent_basis_world_oid=handoff.parent_basis_world_oid,
        store_id=handoff.store_id,
        resource_id=handoff.resource_id,
        candidate_id=handoff.candidate_id,
        candidate_ref=handoff.candidate_ref,
        candidate_head=handoff.candidate_head,
        changed_paths=handoff.changed_paths if changed_paths is None else changed_paths,
        settlement_ref=None,
    )


def test_run_retained_custody_constructors_match_retained_output() -> None:
    row = retained_output_row()
    citation = output_citation(row)
    custody = RunRetainedCustody.from_retained_output(row)

    assert custody == retained_custody(row)
    assert RunRetainedCustody.from_output_citation(citation) == custody
    assert RunRetainedCustody.from_seal_handoff(seal_handoff()) == custody
    assert custody.matches_retained_output(row)
    assert not custody.matches_retained_output(replace(row, resource_id="other-resource"))


def published_output_citation(
    draft: object,
) -> tuple[SQLiteTraceStore, RunOutputCitationRef]:
    store = SQLiteTraceStore()
    descriptor = draft.descriptor_fact()
    receipt = store.append(
        TRACE_APPEND_CONTEXT,
        AppendBatch(
            append_intent_id="intent:run-output-descriptor",
            groups=(
                AppendGroup(
                    trace_owner_id=draft.trace_ref.execution_id,
                    fact_drafts=(descriptor,),
                ),
            ),
        ),
    )
    store.publish_frontier(
        TRACE_APPEND_CONTEXT,
        OwnerCutoffSpec(
            frontier_id=draft.trace_ref.frontier_id,
            target_trace_owner_id=draft.trace_ref.execution_id,
            through_fact_id=receipt.fact_ids[0],
        ),
    )
    return store, draft.citation_ref(descriptor_fact_id=receipt.fact_ids[0])


def descriptor_resolver_for(record: ProjectedRunOutputDescriptor):
    def _resolve(locator: RunOutputDescriptorLocator) -> ProjectedRunOutputDescriptor:
        assert locator == record.locator
        return record

    return _resolve


def run_record(
    run_ref: str = "run-1",
    *,
    task_id: str = "tasks.fix_bug",
    status: str = "merged",
    trace_head: str | None = "trace-head-1",
) -> RunRecord:
    trace_ref = TraceRef(run_id=run_ref, execution_id="exec-1", frontier_id="frontier-1")
    citation = output_citation(trace_ref=trace_ref)
    outputs = {"workspace": citation} if status == "retained" else {}
    terminal_workspace_world_oid = "w-out" if status in {"merged", "retained"} else None
    return RunRecord(
        run_ref=run_ref,
        task_id=task_id,
        task_version="v1",
        task_schema_digest="sha256:v1",
        task_source_identity="world:w1:path:pkg/tasks.py",
        args_digest="sha256:args",
        may_profile="ReadOnly",
        provider="in-process",
        status=status,  # type: ignore[arg-type]
        terminalization=run_terminalization(status=status, citation=citation),
        trace_ref=trace_ref,
        operation_refs=RunOperationRefs(
            run_start_revision="runs:start:1",
            runtime_operation="op-runtime",
            authority_operation="op-authority",
            authority_settlement_operation="op-authority-settlement",
            runtime_value_ref="value-runtime",
            trace_head=trace_head,
        ),
        input_workspace_world_oid="w-in",
        terminal_workspace_world_oid=terminal_workspace_world_oid,
        outputs=outputs,
        started_at="2026-06-20T00:00:01Z",
        finished_at="2026-06-20T00:00:02Z",
        parent_run_ref="run-parent",
        caused_by="event:parent",
        task_executions=(
            TaskExecutionRecord(
                execution_id="task-execution-1",
                run_ref=run_ref,
                executor_kind="in_process",
                executor_id="shepherd.workspace_control.executor.in_process.v0",
                executor_policy="trusted_bridge",
                call_kind="root_run",
                status="completed",
                task_lock=task_artifact_lock(task_id=task_id),
                started_at="2026-06-20T00:00:01Z",
                finished_at="2026-06-20T00:00:02Z",
                resolution_id="task-resolution-1",
            ),
        ),
        pending_effects=(
            PendingEffectRef(
                effect_ref="effect:1",
                run_ref=run_ref,
                effect_type="NeedsDecision",
                trace_ref=trace_ref,
                state="unsupported",
            ),
        ),
    )


def run_terminalization(*, status: str, citation: RunOutputCitationRef) -> RunTerminalization:
    if status == "pending":
        return RunTerminalization(
            body_status="pending",
            world_disposition="none",
            output_publication_status="not_applicable",
        )
    if status == "running":
        return RunTerminalization(
            body_status="running",
            world_disposition="none",
            output_publication_status="not_applicable",
        )
    if status == "merged":
        return RunTerminalization(
            body_status="completed",
            world_disposition="merged",
            output_publication_status="not_applicable",
        )
    if status == "retained":
        return RunTerminalization(
            body_status="completed",
            world_disposition="retained",
            output_publication_status="published",
            retained_custody=retained_custody_from_citation(citation),
        )
    if status == "cancelled":
        body_status = "stopped"
    else:
        body_status = "failed"
    return RunTerminalization(
        body_status=body_status,
        world_disposition="discarded",
        output_publication_status="not_applicable",
    )


def retained_unpublished_run_record(
    run_ref: str = "run-1",
    *,
    row: RetainedOutputQueryResult | None = None,
    publication_status: str = "failed",
) -> RunRecord:
    row = row or retained_output_row()
    record = run_record(run_ref, status="retained")
    publication_error = None
    if publication_status == "failed":
        publication_error = {
            "type": "RuntimeError",
            "message": "descriptor publication failed",
            "stage": "output_publication",
            "retained_custody_ref": row.handoff_ref,
            "retained_output_world_oid": row.output_world_oid,
        }
    return replace(
        record,
        outputs={},
        terminal_workspace_world_oid=row.output_world_oid,
        terminalization=RunTerminalization(
            body_status="completed",
            world_disposition="retained",
            output_publication_status=publication_status,  # type: ignore[arg-type]
            retained_custody=retained_custody(row),
            publication_error=publication_error,
        ),
    )


def retained_published_run_record(
    *,
    row: RetainedOutputQueryResult,
    trace_store_path: object,
) -> RunRecord:
    record = retained_unpublished_run_record(row=row, publication_status="failed")
    assert record.trace_ref is not None
    draft = run_output_publication_from_retained_row(row, trace_ref=record.trace_ref, output_name="workspace")
    citation = publish_run_output_descriptor(trace_store_path, draft)
    return replace(
        record,
        outputs={"workspace": citation},
        terminal_workspace_world_oid=citation.output_world_oid,
        terminalization=RunTerminalization(
            body_status="completed",
            world_disposition="retained",
            output_publication_status="published",
            retained_custody=retained_custody_from_citation(citation),
        ),
        error=None,
    )


def task_payload(*versions: TaskDefinitionVersion) -> dict[str, object]:
    tasks: dict[str, list[dict[str, object]]] = {}
    for version in versions:
        tasks.setdefault(version.task_id, []).append(version.to_json())
    return {"schema": TASK_LEDGER_SCHEMA, "tasks": tasks}


def run_payload(*records: RunRecord) -> dict[str, object]:
    return {"schema": RUN_LEDGER_SCHEMA, "runs": [record.to_json() for record in records]}


def _run_record_path(run_ref: str) -> str:
    return f"data/runs/by-ref/{run_ref[:2]}/{run_ref}.json"


def _run_args_path(args_ref: str) -> str:
    return f"data/args/by-ref/{args_ref[:2]}/{args_ref}.json"


def _flow_path(flow_id: str) -> str:
    return f"data/flows/by-id/{flow_id[:2]}/{flow_id}.json"


def _flow_run_path(run_ref: str) -> str:
    return f"data/flow-runs/by-run/{run_ref[:2]}/{run_ref}.json"


def fake_mg(
    *,
    tasks: dict[str, object] | None = None,
    runs: dict[str, object] | None = None,
    traces: dict[str, dict[str, object]] | None = None,
    retained_outputs: tuple[RetainedOutputQueryResult, ...] = (),
) -> FakeVcsCore:
    payloads: dict[str, dict[str, object]] = {}
    if tasks is not None:
        payloads[TASK_LEDGER_BINDING] = tasks
    if runs is not None:
        payloads[RUN_LEDGER_BINDING] = runs
    return FakeVcsCore(payloads, traces=traces, retained_outputs=retained_outputs)


def test_task_version_round_trips_as_json() -> None:
    version = task_version()

    restored = TaskDefinitionVersion.from_json(version.to_json())

    assert restored == version
    assert restored.resolved().to_json()["import_path"] == "pkg.tasks:fix_bug"


def test_run_record_round_trips_as_json_with_pending_effects() -> None:
    record = run_record()

    restored = RunRecord.from_json(record.to_json())

    assert restored == record
    assert restored.terminalization == RunTerminalization(
        body_status="completed",
        world_disposition="merged",
        output_publication_status="not_applicable",
    )
    assert restored.enforcement == "advisory"
    assert restored.execution_evidence == RunExecutionEvidence(
        requested_placement="advisory",
        resolved_placement="advisory",
        enforcement_basis="legacy_advisory",
    )
    assert restored.launch_context.launch_surface == "operator"
    assert restored.operation_refs.authority_operation == "op-authority"
    assert restored.operation_refs.authority_settlement_operation == "op-authority-settlement"
    assert restored.task_executions[0].executor_policy == "trusted_bridge"
    assert restored.task_executions[0].task_lock.task_id == record.task_id
    assert restored.proof.profile is ProofProfile.RUNTIME_ONLY
    assert restored.proof.strength is ProofStrength.RUNTIME_ONLY
    assert not restored.proof.proof_backed
    assert restored.outputs == {}
    assert restored.pending_effects[0].state == "unsupported"


def test_run_record_defaults_legacy_records_to_runtime_only_proof() -> None:
    payload = run_record().to_json()
    payload.pop("proof")

    restored = RunRecord.from_json(payload)

    assert restored.proof.profile is ProofProfile.RUNTIME_ONLY
    assert restored.proof.strength is ProofStrength.RUNTIME_ONLY
    assert not restored.proof.proof_backed


def test_run_record_rejects_non_runtime_only_proof_status() -> None:
    with pytest.raises(ValueError, match="runtime_only or matching VcsCore certificate proof status"):
        replace(
            run_record(),
            proof=ProofEnvelope(
                profile=ProofProfile.REFERENCE_CORE_A,
                strength=ProofStrength.REFERENCE_VALIDATED,
                evidence_id=f"proof-evidence:sha256:{'a' * 64}",
                program_ref=f"program:sha256:{'b' * 64}",
                trace_ref=f"trace:sha256:{'c' * 64}",
            ),
        )


def test_run_record_rejects_forged_vcscore_certificate_proof_status() -> None:
    forged = ProofEnvelope(
        profile=ProofProfile.EXTENSION,
        strength=ProofStrength.SEMANTIC_ADEQUACY,
        evidence_id=f"proof-evidence:sha256:{'a' * 64}",
        program_ref=f"program:sha256:{'b' * 64}",
        trace_ref=f"trace:sha256:{'c' * 64}",
        theorem_ids=VCSCORE_RUN_THEOREM_IDS,
        metadata={"extension_name": VCSCORE_RUN_EXTENSION_NAME},
    )

    with pytest.raises(ValueError, match="matching VcsCore certificate proof status"):
        replace(run_record(), proof=forged)


def test_run_record_accepts_vcscore_certificate_proof_status() -> None:
    record = run_record()
    proof = vcscore_run_proof_envelope(vcscore_run_certificate_from_run_record(record.to_json()))

    proven = replace(record, proof=proof)

    assert proven.proof == proof
    assert RunRecord.from_json(proven.to_json()) == proven
    assert proven.proof.profile is ProofProfile.EXTENSION
    assert proven.proof.strength is ProofStrength.SEMANTIC_ADEQUACY


def test_run_record_enforcement_defaults_legacy_records_to_advisory() -> None:
    payload = run_record().to_json()
    payload.pop("enforcement")
    payload.pop("execution_evidence")

    restored = RunRecord.from_json(payload)

    assert restored.enforcement == "advisory"
    assert restored.execution_evidence == RunExecutionEvidence()


def test_run_execution_evidence_rejects_impossible_requested_resolved_pair() -> None:
    with pytest.raises(ValueError, match="cannot resolve advisory placement to jail"):
        RunExecutionEvidence(
            requested_placement="advisory",
            resolved_placement="jail",
            enforcement_basis="launch_confined_attempted",
        )


def test_run_record_rejects_jail_enforcement_without_launch_evidence() -> None:
    with pytest.raises(ValueError, match="jail requires launch_confined evidence"):
        replace(
            run_record(),
            enforcement="jail",
            execution_evidence=RunExecutionEvidence(
                requested_placement="jail",
                resolved_placement="jail",
                enforcement_basis="required_jail",
            ),
        )


def test_run_record_rejects_launch_evidence_with_advisory_enforcement() -> None:
    with pytest.raises(ValueError, match="must be jail when launch_confined was attempted"):
        replace(
            run_record(),
            execution_evidence=RunExecutionEvidence(
                requested_placement="jail",
                resolved_placement="jail",
                enforcement_basis="launch_confined_attempted",
            ),
        )


def test_run_record_rejects_jail_enforcement_for_prelaunch_advisory() -> None:
    with pytest.raises(ValueError, match="prelaunch advisory"):
        replace(
            run_record(),
            enforcement="jail",
            execution_evidence=RunExecutionEvidence(
                requested_placement="jail",
                resolved_placement="jail",
                enforcement_basis="prelaunch_advisory",
            ),
        )


def retained_runtime_settlement_policy(
    *,
    runtime: dict[str, object] | None = None,
    execution_enforcement: dict[str, object] | None = None,
) -> dict[str, object]:
    policy: dict[str, object] = {
        "kind": "skeleton.retained_output_selection",
        "authority_context": {},
        "execution_enforcement": execution_enforcement
        or {
            "mode": "in_process",
            "provider": "static",
            "executor_kind": "in_process",
            "profile": "ReadWrite",
            "authority_basis": "runtime_provider",
            "requested_monitor": None,
            "monitor_required": False,
            "established_monitor": None,
            "monitor_refusal": None,
            "prelaunch_refusal": None,
            "body_refusal": None,
        },
    }
    if runtime is not None:
        policy["runtime"] = runtime
    return policy


def test_launch_context_rejects_runtime_policy_authority_shaped_fields() -> None:
    with pytest.raises(ValueError, match="reserved for future use: tools"):
        RunLaunchContext(
            settlement_policy=retained_runtime_settlement_policy(
                runtime={
                    "requested": {"provider": {"id": "static"}, "tools": ["Write"]},
                    "resolved": {"provider": "static"},
                }
            )
        )


def test_launch_context_rejects_deferred_runtime_policy_provider() -> None:
    with pytest.raises(ValueError, match=r"not supported in v0\.1\.1"):
        RunLaunchContext(
            settlement_policy=retained_runtime_settlement_policy(
                runtime={
                    "requested": {"provider": {"id": "hermes"}},
                    "resolved": {"provider": "hermes"},
                }
            )
        )


def test_launch_context_accepts_codex_profile_and_auth_mode() -> None:
    context = RunLaunchContext(
        settlement_policy=retained_runtime_settlement_policy(
            runtime={
                "requested": {
                    "provider": {"id": "codex", "profile": "release", "mode": "chatgpt"},
                    "model": {"name": "gpt-5.4"},
                },
                "resolved": {
                    "provider": "codex",
                    "profile": "release",
                    "mode": "chatgpt",
                    "model": "gpt-5.4",
                },
            },
            execution_enforcement={
                "mode": "confined_process",
                "provider": "codex",
                "executor_kind": "confined_process",
                "profile": "ReadWrite",
                "authority_basis": "runtime_provider",
                "requested_monitor": "provider_tool_sandbox",
                "monitor_required": True,
                "established_monitor": "provider_tool_sandbox",
                "monitor_refusal": None,
                "prelaunch_refusal": None,
                "body_refusal": None,
            },
        )
    )
    assert context.settlement_policy["runtime"]["resolved"]["provider"] == "codex"


def test_launch_context_accepts_claude_runtime_policy_provider() -> None:
    context = RunLaunchContext(
        settlement_policy=retained_runtime_settlement_policy(
            runtime={
                "requested": {"provider": {"id": "claude"}, "model": {"name": "sonnet"}},
                "resolved": {"provider": "claude", "model": "sonnet"},
            },
            execution_enforcement={
                "mode": "confined_process",
                "provider": "claude",
                "executor_kind": "confined_process",
                "profile": "ReadWrite",
                "authority_basis": "workspace_run_placement",
                "requested_monitor": "syscall_jail",
                "monitor_required": True,
                "established_monitor": None,
                "monitor_refusal": None,
                "prelaunch_refusal": None,
                "body_refusal": None,
            },
        )
    )

    assert context.settlement_policy is not None
    assert context.settlement_policy["runtime"]["resolved"]["provider"] == "claude"


def test_launch_context_rejects_resolved_runtime_model_without_provider() -> None:
    with pytest.raises(ValueError, match=r"resolved\.model requires resolved\.provider"):
        RunLaunchContext(
            settlement_policy=retained_runtime_settlement_policy(
                runtime={
                    "requested": {"trace": {}},
                    "resolved": {"model": "fixture-v1"},
                }
            )
        )


def test_launch_context_rejects_runtime_model_resolution_without_request() -> None:
    with pytest.raises(ValueError, match="cannot resolve a model that was not requested"):
        RunLaunchContext(
            settlement_policy=retained_runtime_settlement_policy(
                runtime={
                    "requested": {"provider": {"id": "static"}},
                    "resolved": {"provider": "static", "model": "fixture-v1"},
                }
            )
        )


def test_launch_context_rejects_runtime_model_resolution_mismatch() -> None:
    with pytest.raises(ValueError, match="requested model must match resolved model"):
        RunLaunchContext(
            settlement_policy=retained_runtime_settlement_policy(
                runtime={
                    "requested": {"provider": {"id": "static"}, "model": {"name": "fixture-v1"}},
                    "resolved": {"provider": "static", "model": "fixture-v2"},
                }
            )
        )


def test_launch_context_rejects_in_process_monitor_claim() -> None:
    execution = {
        "mode": "in_process",
        "provider": "static",
        "executor_kind": "in_process",
        "profile": "ReadWrite",
        "authority_basis": "runtime_provider",
        "requested_monitor": "syscall_jail",
        "monitor_required": True,
        "established_monitor": None,
        "monitor_refusal": None,
        "prelaunch_refusal": None,
        "body_refusal": None,
    }

    with pytest.raises(ValueError, match="in-process execution enforcement cannot require a monitor"):
        RunLaunchContext(settlement_policy=retained_runtime_settlement_policy(execution_enforcement=execution))


def test_run_record_rejects_unknown_enforcement() -> None:
    payload = run_record().to_json()
    payload["enforcement"] = "carrier"

    with pytest.raises(ValueError, match="enforcement must be one of"):
        RunRecord.from_json(payload)


def test_run_operation_refs_accept_legacy_finish_revision_but_do_not_reemit_it() -> None:
    refs = RunOperationRefs.from_json(
        {
            "run_start_revision": "runs:start:1",
            "run_finish_revision": "runs:finish:legacy",
        }
    )

    assert refs.run_finish_revision == "runs:finish:legacy"
    assert refs.to_json() == {
        "run_start_revision": "runs:start:1",
        "runtime_operation": None,
        "authority_operation": None,
        "authority_settlement_operation": None,
        "runtime_value_ref": None,
        "trace_head": None,
    }


def test_run_record_json_uses_terminal_workspace_world_key() -> None:
    record = run_record("run-retained", status="retained")
    payload = record.to_json()

    assert payload["terminal_workspace_world_oid"] == "w-out"
    assert "workspace_output_world_oid" not in payload

    payload["workspace_output_world_oid"] = payload.pop("terminal_workspace_world_oid")
    with pytest.raises(ValueError, match="workspace_output_world_oid is unsupported"):
        RunRecord.from_json(payload)


def test_run_workspace_output_world_oid_is_product_visible_only() -> None:
    published = run_record("run-retained", status="retained")
    merged = run_record("run-merged", status="merged")
    unpublished = replace(
        published,
        outputs={},
        terminalization=RunTerminalization(
            body_status="completed",
            world_disposition="retained",
            output_publication_status="failed",
            retained_custody=retained_custody(),
            publication_error={
                "type": "RuntimeError",
                "message": "descriptor publication failed",
                "stage": "output_publication",
                "retained_custody_ref": "handoff-1",
                "retained_output_world_oid": "w-out",
            },
        ),
    )

    assert published.terminal_workspace_world_oid == "w-out"
    assert run_has_published_workspace_output(published)
    assert run_workspace_output_world_oid(published) == "w-out"

    assert merged.terminal_workspace_world_oid == "w-out"
    assert not run_has_published_workspace_output(merged)
    assert run_workspace_output_world_oid(merged) is None

    assert unpublished.terminal_workspace_world_oid == "w-out"
    assert not run_has_published_workspace_output(unpublished)
    assert run_workspace_output_world_oid(unpublished) is None


def test_retained_run_status_round_trips_and_lists_as_terminal() -> None:
    retained = run_record("run-retained", status="retained")
    merged = run_record("run-merged", status="merged")
    discarded = run_record("run-discarded", status="discarded")
    failed = run_record("run-failed", status="failed")
    mg = fake_mg(runs=run_payload(merged, retained, discarded, failed))

    restored = RunRecord.from_json(retained.to_json())

    assert restored.status == "retained"
    assert restored.terminalization is not None
    assert restored.terminalization.world_disposition == "retained"
    assert restored.terminalization.output_publication_status == "published"
    assert restored.summary() == RunSummary(
        run_ref="run-retained",
        task_id=retained.task_id,
        task_version=retained.task_version,
        status="retained",
        started_at=retained.started_at,
        finished_at=retained.finished_at,
        parent_run_ref=retained.parent_run_ref,
    )
    assert restored.status not in {"merged", "discarded", "failed"}
    assert list_runs(mg, status="retained") == (retained.summary(),)
    assert show_run(mg, "run-retained") == retained


def test_queries_read_selected_payloads_through_public_vcscore_api() -> None:
    scope = object()
    mg = fake_mg(tasks=task_payload(task_version()), runs=run_payload(run_record()))

    assert read_task_ledger_payload(mg, scope=scope)["schema"] == TASK_LEDGER_SCHEMA
    assert read_run_ledger_payload(mg, scope=scope)["schema"] == RUN_LEDGER_SCHEMA

    assert mg.reads == [(TASK_LEDGER_BINDING, scope), (RUN_LEDGER_BINDING, scope)]


def test_exact_run_query_uses_addressable_keyed_record_without_whole_payload_read() -> None:
    record = run_record("run-1")
    mg = fake_mg()
    mg.entries[RUN_LEDGER_BINDING] = {"data/runs/by-ref/ru/run-1.json": record.to_json()}

    def _fail_whole_payload_read(*args: object, **kwargs: object) -> object:
        raise AssertionError("exact run lookup must not read the whole run ledger")

    mg.read_selected_binding_revision = _fail_whole_payload_read  # type: ignore[method-assign]
    mg.read_selected_binding_revision_with_head = _fail_whole_payload_read  # type: ignore[method-assign]

    assert show_run(mg, "run-1") == record


def test_run_ledger_auxiliary_reads_use_addressable_keyed_records_without_whole_payload_read() -> None:
    mg = fake_mg()
    mg.entries[RUN_LEDGER_BINDING] = {
        "data/flows/by-id/fl/flow-1.json": {
            "schema": "shepherd.workspace_control.flow.v1",
            "flow_id": "flow-1",
            "name": "release",
            "metadata": {"track": "skeleton"},
        },
        "data/flow-runs/by-run/ru/run-1.json": {
            "schema": "shepherd.workspace_control.flow_run.v1",
            "flow_id": "flow-1",
            "run_ref": "run-1",
            "name": "candidate",
            "sequence": 0,
            "created_at": "2026-06-30T00:00:00Z",
        },
        "data/flow-runs/by-run/ru/run-2.json": {
            "schema": "shepherd.workspace_control.flow_run.v1",
            "flow_id": "flow-2",
            "run_ref": "run-2",
            "name": "other",
            "sequence": 0,
            "created_at": "2026-06-30T00:00:01Z",
        },
    }

    def _fail_whole_payload_read(*args: object, **kwargs: object) -> object:
        raise AssertionError("auxiliary lookup must not read the whole run ledger")

    mg.read_selected_binding_revision = _fail_whole_payload_read  # type: ignore[method-assign]
    mg.read_selected_binding_revision_with_head = _fail_whole_payload_read  # type: ignore[method-assign]

    store = RunLedgerStore(mg)

    assert store.get_flow("flow-1") == {
        "schema": "shepherd.workspace_control.flow.v1",
        "flow_id": "flow-1",
        "name": "release",
        "metadata": {"track": "skeleton"},
    }
    assert [row["run_ref"] for row in store.list_flow_runs(flow_id="flow-1")] == ["run-1"]


def test_all_run_output_citations_use_keyed_records_without_whole_payload_read() -> None:
    record = run_record("run-1", status="retained")
    mg = fake_mg()
    mg.entries[RUN_LEDGER_BINDING] = {"data/runs/by-ref/ru/run-1.json": record.to_json()}

    def _fail_whole_payload_read(*args: object, **kwargs: object) -> object:
        raise AssertionError("all-run citation lookup must not synthesize the whole run ledger")

    mg.read_selected_binding_revision = _fail_whole_payload_read  # type: ignore[method-assign]
    mg.read_selected_binding_revision_with_head = _fail_whole_payload_read  # type: ignore[method-assign]

    citations = run_output_citations(mg)

    assert len(citations) == 1
    assert citations[0].output_name == "workspace"
    assert citations[0].binding == "workspace"


def test_run_ledger_put_current_emits_only_changed_keyed_records() -> None:
    mg = fake_mg()
    store = RunLedgerStore(mg)
    args_payload = build_run_args_payload(
        run_ref="run-1",
        args={"issue": "parser"},
        created_at="2026-06-30T00:00:00Z",
    )
    args_ref = args_payload["args_ref"]
    assert isinstance(args_ref, str)
    flow_run = {
        "schema": "shepherd.workspace_control.flow_run.v1",
        "flow_id": "flow-1",
        "run_ref": "run-1",
        "name": "candidate",
        "sequence": 0,
        "created_at": "2026-06-30T00:00:00Z",
    }

    record = replace(run_record("run-1"), args_digest=str(args_payload["args_digest"]), args_ref=args_ref)

    store.put_current(record, args_payload=args_payload, flow_run_payload=flow_run)

    assert set(mg.published_put_paths[-1]) == {
        _run_args_path(args_ref),
        _flow_run_path("run-1"),
        _run_record_path("run-1"),
    }
    assert mg.published_delete_paths[-1] == ()

    store.put_current(run_record("run-2"))

    assert mg.published_put_paths[-1] == (_run_record_path("run-2"),)
    assert mg.published_delete_paths[-1] == ()


def test_run_ledger_row_updates_emit_only_changed_run_record() -> None:
    mg = fake_mg()
    store = RunLedgerStore(mg)
    store.put_current(run_record("run-1"))
    mg.published_put_paths.clear()
    mg.published_delete_paths.clear()

    execution = TaskExecutionRecord(
        execution_id="task-execution-2",
        run_ref="run-1",
        executor_kind="in_process",
        executor_id="shepherd.workspace_control.executor.in_process.v0",
        executor_policy="trusted_bridge",
        call_kind="root_run",
        status="completed",
        task_lock=task_artifact_lock(),
        started_at="2026-06-20T00:00:03Z",
        finished_at="2026-06-20T00:00:04Z",
        resolution_id="task-resolution-2",
    )
    resolution = TaskResolutionRecord(
        resolution_id="task-resolution-2",
        reason="dependency",
        requested_ref="tasks.fix_bug",
        task_ledger_head="head-shepherd.tasks",
        task_lock=task_artifact_lock(),
        parent_run_ref="run-1",
        requester_task_id="tasks.fix_bug",
        requester_task_version="v1",
        resolved_at="2026-06-20T00:00:03Z",
    )

    store.append_execution("run-1", execution)
    store.append_resolution("run-1", resolution)

    assert mg.published_put_paths == [
        (_run_record_path("run-1"),),
        (_run_record_path("run-1"),),
    ]
    assert mg.published_delete_paths == [(), ()]


def test_run_ledger_typed_auxiliary_writers_publish_closed_record_families() -> None:
    mg = fake_mg()
    store = RunLedgerStore(mg)
    flow = {
        "schema": "shepherd.workspace_control.flow.v1",
        "flow_id": "flow-1",
        "name": "release",
        "metadata": {"track": "skeleton"},
    }
    flow_run = {
        "schema": "shepherd.workspace_control.flow_run.v1",
        "flow_id": "flow-1",
        "run_ref": "run-1",
        "name": "candidate",
        "sequence": 0,
        "after": [],
        "metadata": {},
        "created_at": "2026-06-30T00:00:00Z",
    }
    args_payload = build_run_args_payload(
        run_ref="run-1",
        args={"issue": "parser"},
        created_at="2026-06-30T00:00:00Z",
    )
    args_ref = args_payload["args_ref"]
    assert isinstance(args_ref, str)

    store.put_flow("flow-1", flow)
    store.put_flow_run("run-1", flow_run)
    store.put_args(args_ref, args_payload)

    assert store.get_flow("flow-1") == flow
    assert store.list_flow_runs(flow_id="flow-1") == (flow_run,)
    assert store.get_args(args_ref) == args_payload
    assert set(mg.entries[RUN_LEDGER_BINDING]) == {
        _flow_path("flow-1"),
        _flow_run_path("run-1"),
        _run_args_path(args_ref),
    }
    assert mg.published_put_paths == [
        (_flow_path("flow-1"),),
        (_flow_run_path("run-1"),),
        (_run_args_path(args_ref),),
    ]
    assert mg.published_delete_paths == [(), (), ()]
    with pytest.raises(RunLedgerPublishError, match="flow_id disagrees"):
        store.put_flow("other-flow", flow)
    with pytest.raises(RunLedgerPublishError, match="run_ref disagrees"):
        store.put_flow_run("other-run", flow_run)
    with pytest.raises(RunLedgerPublishError, match="args_ref disagrees"):
        store.put_args("other-args", args_payload)


def test_run_ledger_auxiliary_surface_rejects_unknown_record_families() -> None:
    mg = fake_mg()
    store = RunLedgerStore(mg)
    unknown_store = KeyedJsonTreeStore("custom/by-id")

    assert not hasattr(store, "put_auxiliary")
    assert not hasattr(store, "get_auxiliary")
    assert not hasattr(store, "list_auxiliary")
    with pytest.raises(ValueError, match="unsupported run-ledger auxiliary store"):
        store._get_auxiliary(unknown_store, "custom-1")
    with pytest.raises(ValueError, match="unsupported run-ledger auxiliary store"):
        store._list_auxiliary(unknown_store)
    with pytest.raises(ValueError, match="unsupported run-ledger auxiliary store"):
        store._put_auxiliary(unknown_store, "custom-1", {"custom_id": "custom-1"})
    assert mg.reads == []
    assert mg.exec_calls == []


def test_flow_facade_lists_keyed_flow_records_without_whole_payload_read() -> None:
    mg = fake_mg()
    mg.entries[RUN_LEDGER_BINDING] = {
        "data/flows/by-id/fl/flow-1.json": {
            "schema": "shepherd.workspace_control.flow.v1",
            "flow_id": "flow-1",
            "name": "release",
            "metadata": {"track": "skeleton"},
        }
    }

    def _fail_whole_payload_read(*args: object, **kwargs: object) -> object:
        raise AssertionError("flow facade must not read the whole run ledger")

    mg.read_selected_binding_revision = _fail_whole_payload_read  # type: ignore[method-assign]
    mg.read_selected_binding_revision_with_head = _fail_whole_payload_read  # type: ignore[method-assign]

    workspace = type("FakeWorkspace", (), {"mg": mg})()
    client = FlowControlClient(workspace)  # type: ignore[arg-type]

    assert [flow.flow_id for flow in client.list()] == ["flow-1"]
    flow = client.get("flow-1")
    assert flow is not None
    assert flow.name == "release"
    assert flow.metadata == {"track": "skeleton"}


def test_task_queries_resolve_active_and_explicit_versions() -> None:
    v1 = task_version(version="v1", status="superseded")
    v2 = task_version(version="v2", status="active")
    mg = fake_mg(tasks=task_payload(v1, v2))

    assert list_tasks(mg) == (v1.summary(), v2.summary())
    assert list_tasks(mg, status="active") == (v2.summary(),)
    assert get_task(mg, "tasks.fix_bug") == v2
    assert get_task(mg, "tasks.fix_bug@v1") == v1
    assert resolve_task(mg, "tasks.fix_bug").version == "v2"


def test_task_query_rejects_multiple_active_versions() -> None:
    mg = fake_mg(tasks=task_payload(task_version(version="v1"), task_version(version="v2")))

    with pytest.raises(ValueError, match="multiple active versions"):
        get_task(mg, "tasks.fix_bug")


def test_run_queries_resolve_latest_short_refs_trace_and_outputs() -> None:
    first = run_record("run-111", status="failed")
    second = run_record("run-222", task_id="tasks.other", status="retained")
    trace_payload = {"run_ref": "run-222", "events": [{"kind": "run.lifecycle"}]}
    retained = retained_output_row()
    mg = fake_mg(
        runs=run_payload(first, second),
        traces={"trace-head-1": trace_payload},
        retained_outputs=(retained,),
    )

    assert list_runs(mg, status="retained") == (second.summary(),)
    assert list_runs(mg, max_count=1) == (second.summary(),)
    assert list_runs(mg, max_count=0) == ()
    assert show_run(mg, "@latest") == second
    assert show_run(mg, "run-11") == first
    assert trace_run(mg, "run-222").payload == trace_payload
    assert run_output_citations(mg, run_ref="run-222")[0].output_name == "workspace"
    citation = second.outputs["workspace"]
    output = outputs_for_run(
        mg,
        run_ref="run-222",
        descriptor_resolver=descriptor_resolver_for(output_descriptor_record(citation=citation, row=retained)),
    )[0]
    assert output.identity.output_name == "workspace"
    assert output.identity.parent_scope_name == "ground"
    assert output.identity.parent_scope_instance_id is None
    assert output.state == "unconsumed"
    assert output.changed_paths == ("src/app.py",)
    assert len(mg.direct_retained_reads) == 1
    assert mg.retained_reads == []


def test_run_vcscore_projection_cites_existing_run_record_identities() -> None:
    record = run_record("run-222")
    retained = run_record("run-retained", status="retained")
    mg = fake_mg(runs=run_payload(record, retained))

    projection = run_vcscore_projection(mg, "run-222")
    retained_projection = run_vcscore_projection(mg, "run-retained")

    assert projection == {
        "schema": "shepherd.workspace_control.run_vcscore_projection.v2",
        "run_ref": "run-222",
        "task_id": "tasks.fix_bug",
        "status": "merged",
        "provider": "in-process",
        "enforcement": "advisory",
        "execution_evidence": {
            "requested_placement": "advisory",
            "resolved_placement": "advisory",
            "enforcement_basis": "legacy_advisory",
            "execution_descriptor": None,
        },
        "runtime_operation": "op-runtime",
        "authority_operation": "op-authority",
        "authority_settlement_operation": "op-authority-settlement",
        "operation_show": ("vcs-core", "operation", "show", "op-runtime"),
        "trace_head": "trace-head-1",
        "trace_show": ("shepherd", "run", "trace", "run-222"),
        "run_start_revision": "runs:start:1",
        "input_workspace_world_oid": "w-in",
        "terminal_workspace_world_oid": "w-out",
        "published_workspace_output_world_oid": None,
    }
    assert retained_projection is not None
    assert retained_projection["terminal_workspace_world_oid"] == "w-out"
    assert retained_projection["published_workspace_output_world_oid"] == "w-out"
    assert run_vcscore_projection(mg, "run-missing") is None


def test_workspace_control_cli_lists_tasks_runs_and_raw_output_citations(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from shepherd_dialect import cli

    mg = fake_mg(tasks=task_payload(task_version()), runs=run_payload(run_record(status="retained")))
    workspace = _fake_workspace(mg)
    monkeypatch.setattr(cli, "_open_workspace", lambda **kwargs: workspace)
    runner = CliRunner()

    task_result = runner.invoke(cli.main, ["task", "list", "--json"])
    assert task_result.exit_code == 0, task_result.output
    assert json.loads(task_result.output)[0]["task_id"] == "tasks.fix_bug"

    run_result = runner.invoke(cli.main, ["run", "list", "--status", "retained", "--json"])
    assert run_result.exit_code == 0, run_result.output
    assert json.loads(run_result.output)[0]["run_ref"] == "run-1"

    show_result = runner.invoke(cli.main, ["run", "show", "@latest", "--json"])
    assert show_result.exit_code == 0, show_result.output
    assert json.loads(show_result.output)["outputs"]["workspace"]["output_world_oid"] == "w-out"

    citation_result = runner.invoke(
        cli.main,
        ["run", "output-citations", "@latest", "--binding", "workspace", "--json"],
    )
    assert citation_result.exit_code == 0, citation_result.output
    assert json.loads(citation_result.output)[0]["parent_basis_world_oid"] == "w-in"

    vcscore_result = runner.invoke(cli.main, ["run", "vcscore", "@latest", "--json"])
    assert vcscore_result.exit_code == 0, vcscore_result.output
    vcscore_json = json.loads(vcscore_result.output)
    assert vcscore_json["runtime_operation"] == "op-runtime"
    assert vcscore_json["operation_show"] == ["vcs-core", "operation", "show", "op-runtime"]


def test_workspace_control_cli_run_show_renders_enforcement_line(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from shepherd_dialect import cli

    mg = fake_mg(tasks=task_payload(task_version()), runs=run_payload(run_record(status="retained")))
    workspace = _fake_workspace(mg)
    monkeypatch.setattr(cli, "_open_workspace", lambda **kwargs: workspace)

    result = CliRunner().invoke(cli.main, ["run", "show", "@latest"])
    assert result.exit_code == 0, result.output
    # Device honesty is surfaced at the CLI, not only in the durable record: the human `run show`
    # render carries the enforcement mode and its basis. (P-030 v0.2 device-honesty gate, CLI layer.)
    assert "enforcement:  advisory (legacy_advisory)" in result.output


def test_workspace_control_cli_task_show_renders_signature_and_json(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from shepherd_dialect import cli

    mg = fake_mg(tasks=task_payload(task_version()))
    workspace = _fake_workspace(mg)
    monkeypatch.setattr(cli, "_open_workspace", lambda **kwargs: workspace)
    runner = CliRunner()

    human = runner.invoke(cli.main, ["task", "show", "tasks.fix_bug"])
    assert human.exit_code == 0, human.output
    assert "Task tasks.fix_bug@v1" in human.output
    assert "Fix one bug" in human.output
    assert "ReadOnly" in human.output

    raw = runner.invoke(cli.main, ["task", "show", "tasks.fix_bug", "--json"])
    assert raw.exit_code == 0, raw.output
    payload = json.loads(raw.output)
    assert payload["task"]["task_id"] == "tasks.fix_bug"
    assert payload["artifact"]["docstring"] == "Fix one bug in the selected workspace."


# --- 0.2.0 teaching beat: task show leads with the per-binding grant summary ----------------

# Runtime import: `_signature_schema` resolves these stringized annotations via
# `get_type_hints`, which needs them in the module globals (not a TYPE_CHECKING block).
from shepherd_runtime.nucleus import GitRepo  # noqa: TC002

from shepherd_dialect.workspace_control import May, ReadOnly, ReadWrite
from shepherd_dialect.workspace_control.workspace import _signature_schema


def _lane_c_docs_backend_task(docs: May[GitRepo, ReadOnly], backend: May[GitRepo, ReadWrite]) -> None:
    """Multi-binding Lane C source used to pin the teaching-shape grant summary."""


def _single_repo_task(repo: May[GitRepo, ReadOnly]) -> None:
    """Single injected-repo grant source."""


def _task_show_human_output(monkeypatch: pytest.MonkeyPatch, version: TaskDefinitionVersion) -> str:
    from click.testing import CliRunner

    from shepherd_dialect import cli

    mg = fake_mg(tasks=task_payload(version))
    workspace = _fake_workspace(mg)
    monkeypatch.setattr(cli, "_open_workspace", lambda **kwargs: workspace)
    result = CliRunner().invoke(cli.main, ["task", "show", version.task_id])
    assert result.exit_code == 0, result.output
    return result.output


def test_task_show_leads_with_multi_binding_grant_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    version = replace(task_version(), signature_schema=_signature_schema(_lane_c_docs_backend_task))

    output = _task_show_human_output(monkeypatch, version)

    # The permission surface leads: the exact teaching shape is the first rendered line.
    assert output.splitlines()[0] == "docs read-only / backend read-write"
    # Fuller detail (including the raw signature JSON) is retained after the summary.
    assert "Task tasks.fix_bug@v1" in output
    assert "signature:" in output
    assert "gitrepo_grant" in output


def test_task_show_leads_with_single_repo_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    version = replace(task_version(), signature_schema=_signature_schema(_single_repo_task))

    output = _task_show_human_output(monkeypatch, version)

    assert output.splitlines()[0] == "repo read-only"


def test_task_show_falls_back_to_may_profile_without_per_binding_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Whole-run may only: signature carries no per-binding GitRepo grants.
    version = replace(task_version(), signature_schema={"type": "object"}, may_default="ReadOnly")

    output = _task_show_human_output(monkeypatch, version)

    assert output.splitlines()[0] == "may: ReadOnly"
    assert " read-only" not in output.splitlines()[0]
    assert "read-write" not in output.splitlines()[0]


def test_workspace_control_cli_trace_reads_run_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from shepherd_dialect import cli

    trace_payload = {
        "run_ref": "run-1",
        "events": [
            {"kind": "run.lifecycle", "terminal_status": "merged"},
            {"kind": "task.invocation", "record_digest": "sha256:invocation"},
        ],
    }
    mg = fake_mg(runs=run_payload(run_record()), traces={"trace-head-1": trace_payload})
    workspace = _fake_workspace(mg)
    monkeypatch.setattr(cli, "_open_workspace", lambda **kwargs: workspace)
    runner = CliRunner()

    summary_result = runner.invoke(cli.main, ["run", "trace", "@latest", "--json"])
    assert summary_result.exit_code == 0, summary_result.output
    summary_json = json.loads(summary_result.output)
    assert summary_json["run_ref"] == "run-1"
    assert summary_json["terminal_status"] == "merged"
    assert summary_json["kinds"] == {"run.lifecycle": 1, "task.invocation": 1}
    assert "events" not in summary_json

    events_result = runner.invoke(cli.main, ["run", "trace", "@latest", "--events", "--json"])
    assert events_result.exit_code == 0, events_result.output
    events_json = json.loads(events_result.output)
    assert events_json["summary"]["run_ref"] == "run-1"
    assert events_json["events"] == trace_payload["events"]


def test_workspace_control_cli_trace_fails_closed_for_unmaterialized_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from shepherd_dialect import cli

    mg = fake_mg(runs=run_payload(run_record(trace_head=None)))
    workspace = _fake_workspace(mg)
    monkeypatch.setattr(cli, "_open_workspace", lambda **kwargs: workspace)

    result = CliRunner().invoke(cli.main, ["run", "trace", "@latest"])

    assert result.exit_code == 1
    assert "no materialized trace_head" in result.output


def test_workspace_control_cli_outputs_uses_trace_store_and_query_layer(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from shepherd_dialect import cli

    class FakeTraceStore:
        closed = False

        def close(self) -> None:
            self.closed = True

    trace_store = FakeTraceStore()
    trace_path = tmp_path / "trace.sqlite"
    trace_path.write_text("")
    calls: dict[str, object] = {}

    class FakeRuns:
        def outputs(self, **kwargs: object) -> tuple[dict[str, object], ...]:
            calls.update(kwargs)
            return ({"output_name": "workspace", "state": "selected"},)

    class FakeWorkspace:
        runs = FakeRuns()

    workspace = FakeWorkspace()

    def fake_open_workspace(**kwargs: object) -> FakeWorkspace:
        calls["open_workspace_kwargs"] = kwargs
        return workspace

    def fake_open_trace_store(path: str) -> FakeTraceStore:
        calls["trace_path"] = path
        return trace_store

    monkeypatch.setattr(cli, "_open_workspace", fake_open_workspace)
    monkeypatch.setattr(cli, "_open_trace_store", fake_open_trace_store)

    result = CliRunner().invoke(
        cli.main,
        [
            "run",
            "outputs",
            "@latest",
            "--trace-store",
            str(trace_path),
            "--binding",
            "workspace",
            "--state",
            "selected",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [{"output_name": "workspace", "state": "selected"}]
    assert calls["open_workspace_kwargs"] == {"activate": False}
    assert calls["trace_path"] == str(trace_path)
    assert calls["run_ref"] == "@latest"
    assert calls["binding"] == "workspace"
    assert calls["state"] == "selected"
    assert calls["trace_store"] is trace_store
    assert trace_store.closed


def test_workspace_control_cli_publishes_retained_workspace_output_through_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from shepherd_dialect import cli

    calls: dict[str, object] = {}

    class FakeRuns:
        def publish_retained_workspace_output(self, run_ref: str) -> RunRecord:
            calls["run_ref"] = run_ref
            return run_record(run_ref=run_ref, status="retained")

    class FakeWorkspace:
        runs = FakeRuns()

        def close(self) -> None:
            calls["closed"] = True

    def fake_open_workspace(**kwargs: object) -> FakeWorkspace:
        calls["open_workspace_kwargs"] = kwargs
        return FakeWorkspace()

    monkeypatch.setattr(cli, "_open_workspace", fake_open_workspace)

    result = CliRunner().invoke(cli.main, ["run", "publish-retained-workspace-output", "run-cli"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["run_ref"] == "run-cli"
    assert calls["open_workspace_kwargs"] == {"activate": True}
    assert calls["run_ref"] == "run-cli"
    assert calls["closed"] is True


def test_workspace_control_cli_registers_task_through_workspace_facade(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from shepherd_dialect import cli

    calls: dict[str, object] = {}

    class FakeTasks:
        def register(self, source: str, **kwargs: object) -> TaskDefinitionVersion:
            calls["source"] = source
            calls.update(kwargs)
            return task_version(task_id=str(kwargs["task_id"]), version="v1")

    class FakeWorkspace:
        tasks = FakeTasks()

        def close(self) -> None:
            calls["closed"] = True

    def fake_open_workspace(**kwargs: object) -> FakeWorkspace:
        calls["open_workspace_kwargs"] = kwargs
        return FakeWorkspace()

    monkeypatch.setattr(cli, "_open_workspace", fake_open_workspace)

    result = CliRunner().invoke(
        cli.main,
        [
            "task",
            "register",
            "pkg.tasks:fix_bug",
            "--task-id",
            "tasks.fix_bug",
            "--may-default",
            "ReadWrite",
            "--metadata",
            '{"owner":"cli"}',
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["task_id"] == "tasks.fix_bug"
    assert calls["open_workspace_kwargs"] == {"activate": True}
    assert calls["source"] == "pkg.tasks:fix_bug"
    assert calls["task_id"] == "tasks.fix_bug"
    assert calls["may_default"] == "ReadWrite"
    assert calls["metadata"] == {"owner": "cli"}
    assert calls["closed"] is True


def test_workspace_control_cli_starts_run_through_workspace_facade(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from shepherd_dialect import cli

    calls: dict[str, object] = {}

    class FakeRuns:
        def start(self, task_ref: str, **kwargs: object) -> RunRecord:
            calls["task_ref"] = task_ref
            calls.update(kwargs)
            return run_record(run_ref="run-cli")

    class FakeWorkspace:
        runs = FakeRuns()

        def close(self) -> None:
            calls["closed"] = True

    def fake_open_workspace(**kwargs: object) -> FakeWorkspace:
        calls["open_workspace_kwargs"] = kwargs
        return FakeWorkspace()

    monkeypatch.setattr(cli, "_open_workspace", fake_open_workspace)

    result = CliRunner().invoke(
        cli.main,
        [
            "run",
            "start",
            "tasks.fix_bug",
            "--args",
            '{"issue":"parser"}',
            "--may",
            "ReadWrite",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["run_ref"] == "run-cli"
    assert calls["open_workspace_kwargs"] == {"activate": True}
    assert calls["task_ref"] == "tasks.fix_bug"
    assert calls["args"] == {"issue": "parser"}
    assert calls["may"] == "ReadWrite"
    assert calls["launch_surface"] == "cli"
    assert calls["closed"] is True


def _fake_workspace(mg: FakeVcsCore) -> object:
    class FakeTasks:
        def list(self, *, status: str | None = None, prefix: str | None = None) -> tuple[TaskSummary, ...]:
            return list_tasks(mg, status=status, prefix=prefix)

        def describe(self, task_ref: str) -> dict[str, object] | None:
            task = get_task(mg, task_ref)
            if task is None:
                return None
            return {
                "task": task.to_json(),
                "artifact": {
                    "docstring": "Fix one bug in the selected workspace.",
                    "entrypoint": {"module": "pkg.tasks", "qualname": "fix_bug"},
                    "files": [],
                    "source_excerpt": "def fix_bug(repo): ...",
                },
                "artifact_error": None,
            }

    class FakeRuns:
        def list(
            self,
            *,
            status: str | None = None,
            task_id: str | None = None,
            max_count: int | None = None,
        ) -> tuple[RunSummary, ...]:
            return list_runs(mg, status=status, task_id=task_id, max_count=max_count)

        def show(self, run_ref: str) -> RunRecord | None:
            return show_run(mg, run_ref)

        def output_citations(
            self,
            *,
            run_ref: str | None = None,
            binding: str | None = None,
        ) -> tuple[RunOutputCitationRef, ...]:
            return run_output_citations(mg, run_ref=run_ref, binding=binding)

        def vcscore(self, run_ref: str) -> dict[str, object] | None:
            projection = run_vcscore_projection(mg, run_ref)
            return None if projection is None else dict(projection)

        def trace(self, run_ref: str, *, events: bool = False) -> object | None:
            return trace_run(mg, run_ref, events=events)

    class FakeWorkspace:
        tasks = FakeTasks()
        runs = FakeRuns()

    return FakeWorkspace()


def test_run_query_rejects_ambiguous_short_refs() -> None:
    mg = fake_mg(runs=run_payload(run_record("run-111"), run_record("run-112")))

    with pytest.raises(ValueError, match="ambiguous"):
        show_run(mg, "run-11")


def test_run_query_rejects_empty_run_ref() -> None:
    mg = fake_mg(runs=run_payload(run_record("run-111")))

    with pytest.raises(ValueError, match="non-empty"):
        show_run(mg, "")


def test_trace_query_rejects_unmaterialized_provider_neutral_trace() -> None:
    mg = fake_mg(runs=run_payload(run_record("run-111", trace_head=None)))

    with pytest.raises(TraceNotMaterializedError, match="no materialized trace_head"):
        trace_run(mg, "run-111")


def test_product_output_query_requires_matching_retained_custody() -> None:
    mg = fake_mg(runs=run_payload(run_record(status="retained")))

    with pytest.raises(ValueError, match="no retained-output custody row"):
        outputs_for_run(mg, run_ref="run-1", descriptor_resolver=descriptor_resolver_for(output_descriptor_record()))


def test_product_output_query_filters_after_retained_custody_validation() -> None:
    retained = retained_output_row(state="selected")
    mg = fake_mg(runs=run_payload(run_record(status="retained")), retained_outputs=(retained,))

    assert (
        outputs_for_run(
            mg,
            run_ref="run-1",
            state="unconsumed",
            descriptor_resolver=descriptor_resolver_for(output_descriptor_record()),
        )
        == ()
    )
    assert len(mg.direct_retained_reads) == 1
    assert mg.retained_reads == []


def test_direct_output_resolver_binding_filter_skips_non_matching_citations() -> None:
    citation = output_citation()
    mg = fake_mg()

    assert RunOutputResolver(mg, binding="other").resolve((citation,)) == ()
    assert mg.retained_reads == []


def test_product_output_query_requires_trace_descriptor_authority() -> None:
    retained = retained_output_row()
    mg = fake_mg(runs=run_payload(run_record(status="retained")), retained_outputs=(retained,))

    with pytest.raises(TraceDescriptorNotResolvedError, match="trace_store or descriptor_resolver"):
        outputs_for_run(mg, run_ref="run-1")


def test_seal_handoff_publication_hydrates_through_run_output_resolver() -> None:
    parent = seal_parent_scope()
    handoff = seal_handoff(parent)
    trace_ref = TraceRef(run_id="run-1", execution_id="exec-1", frontier_id="frontier-1")

    draft = run_output_publication_from_seal_handoff(handoff, parent=parent, trace_ref=trace_ref)

    assert draft.citation_payload["schema"] == RUN_OUTPUT_SCHEMA
    assert draft.citation_payload["changed_paths"] == ["src/app.py"]
    assert "parent_scope_instance_id" not in draft.citation_payload
    descriptor = draft.descriptor_fact()
    assert descriptor.payload["citation"] == draft.citation_payload

    trace_store, citation = published_output_citation(draft)
    retained = retained_output_row_for_handoff(handoff, parent=parent)
    mg = fake_mg(retained_outputs=(retained,))

    refs = RunOutputResolver(mg, trace_store=trace_store, read_context=TRACE_READ_CONTEXT).resolve((citation,))

    assert len(refs) == 1
    ref = refs[0]
    assert ref.state == "unconsumed"
    assert ref.identity.output_id == citation.output_id
    assert ref.identity.handoff_ref == handoff.handoff_ref
    assert ref.descriptor.output_name == "workspace"
    assert ref.descriptor_locator == run_output_descriptor_locator_from_payload(dict(citation.descriptor_locator))
    assert ref.changed_paths == ("src/app.py",)


def test_publish_retained_workspace_output_failure_publishes_hydratable_output(tmp_path) -> None:
    retained = retained_output_row()
    record = retained_unpublished_run_record(row=retained)
    trace_payload = {"run_ref": record.run_ref, "events": [{"kind": "run.lifecycle", "terminal_status": "retained"}]}
    mg = fake_mg(
        runs=run_payload(record),
        traces={"trace-head-1": trace_payload},
        retained_outputs=(retained,),
    )
    trace_store_path = tmp_path / "trace.sqlite"

    assert show_run(mg, record.run_ref) == record
    assert trace_run(mg, record.run_ref).payload == trace_payload
    assert run_output_citations(mg, run_ref=record.run_ref) == ()
    trace_store = SQLiteTraceStore(trace_store_path)
    try:
        assert outputs_for_run(mg, run_ref=record.run_ref, trace_store=trace_store) == ()
    finally:
        trace_store.close()

    updated = publish_retained_workspace_output(
        mg,
        run_ref=record.run_ref,
        trace_store_path=trace_store_path,
    )

    assert updated == show_run(mg, record.run_ref)
    assert updated.status == "retained"
    assert updated.error is None
    assert updated.terminalization.output_publication_status == "published"
    assert updated.terminalization.publication_error is None
    assert set(updated.outputs) == {"workspace"}
    assert updated.terminal_workspace_world_oid == retained.output_world_oid
    assert updated.outputs["workspace"].custody_ref == retained.handoff_ref
    assert updated.operation_refs.run_finish_revision == record.operation_refs.run_finish_revision
    assert len([call for call in mg.exec_calls if call[0] == RUN_LEDGER_BINDING]) == 1

    trace_store = SQLiteTraceStore(trace_store_path)
    try:
        refs = outputs_for_run(mg, run_ref=record.run_ref, trace_store=trace_store, read_context=TRACE_READ_CONTEXT)
    finally:
        trace_store.close()

    assert len(refs) == 1
    assert refs[0].identity.output_id == updated.outputs["workspace"].output_id
    assert refs[0].changed_paths == retained.changed_paths


def test_publish_retained_workspace_output_rejects_selector_prefix(tmp_path) -> None:
    retained = retained_output_row()
    record = retained_unpublished_run_record(row=retained)
    mg = fake_mg(
        runs=run_payload(record),
        traces={"trace-head-1": {"run_ref": record.run_ref, "events": []}},
        retained_outputs=(retained,),
    )

    assert show_run(mg, "run") == record
    with pytest.raises(RetainedWorkspaceOutputPublicationError, match="missing run"):
        publish_retained_workspace_output(
            mg,
            run_ref="run",
            trace_store_path=tmp_path / "trace.sqlite",
        )

    assert run_output_citations(mg, run_ref=record.run_ref) == ()
    assert not mg.exec_calls


def test_workspace_runs_publish_retained_workspace_output_uses_workspace_trace_path(tmp_path) -> None:
    retained = retained_output_row()
    record = retained_unpublished_run_record(row=retained)
    trace_store_path = tmp_path / "workspace" / "trace.sqlite"
    mg = fake_mg(runs=run_payload(record), retained_outputs=(retained,))
    workspace = ShepherdWorkspace(mg, trace_store_path=trace_store_path)

    updated = workspace.runs.publish_retained_workspace_output(record.run_ref)

    assert updated == show_run(mg, record.run_ref)
    assert updated.terminalization.output_publication_status == "published"
    assert updated.operation_refs.run_finish_revision == record.operation_refs.run_finish_revision
    assert trace_store_path.exists()
    refs = workspace.runs.outputs(run_ref=record.run_ref)
    assert len(refs) == 1
    assert refs[0].identity.output_id == updated.outputs["workspace"].output_id


def test_publish_retained_workspace_output_pending_publishes_output(tmp_path) -> None:
    retained = retained_output_row()
    record = retained_unpublished_run_record(row=retained, publication_status="pending")
    mg = fake_mg(runs=run_payload(record), retained_outputs=(retained,))

    updated = publish_retained_workspace_output(
        mg,
        run_ref=record.run_ref,
        trace_store_path=tmp_path / "trace.sqlite",
    )

    assert updated.terminalization.output_publication_status == "published"
    assert set(updated.outputs) == {"workspace"}


def test_publish_retained_workspace_output_is_idempotent_after_success(tmp_path) -> None:
    retained = retained_output_row()
    record = retained_unpublished_run_record(row=retained)
    mg = fake_mg(runs=run_payload(record), retained_outputs=(retained,))
    trace_store_path = tmp_path / "trace.sqlite"

    first = publish_retained_workspace_output(mg, run_ref=record.run_ref, trace_store_path=trace_store_path)
    second = publish_retained_workspace_output(mg, run_ref=record.run_ref, trace_store_path=trace_store_path)

    assert second == first
    assert len(run_output_citations(mg, run_ref=record.run_ref)) == 1
    assert len([call for call in mg.exec_calls if call[0] == RUN_LEDGER_BINDING]) == 1


def test_publish_retained_workspace_output_noops_for_published_record(tmp_path) -> None:
    retained = retained_output_row()
    trace_store_path = tmp_path / "trace.sqlite"
    record = retained_published_run_record(row=retained, trace_store_path=trace_store_path)
    mg = fake_mg(runs=run_payload(record), retained_outputs=(retained,))

    updated = publish_retained_workspace_output(
        mg,
        run_ref=record.run_ref,
        trace_store_path=trace_store_path,
    )

    assert updated == record
    assert mg.exec_calls == []


def test_publish_retained_workspace_output_repairs_missing_trace_descriptor_for_published_record(tmp_path) -> None:
    retained = retained_output_row()
    record = run_record(status="retained")
    mg = fake_mg(runs=run_payload(record), retained_outputs=(retained,))
    trace_store_path = tmp_path / "trace.sqlite"

    updated = publish_retained_workspace_output(
        mg,
        run_ref=record.run_ref,
        trace_store_path=trace_store_path,
    )

    assert updated.terminalization.output_publication_status == "published"
    assert updated.outputs["workspace"].output_id == record.outputs["workspace"].output_id
    assert updated.outputs["workspace"].descriptor_locator != record.outputs["workspace"].descriptor_locator
    assert updated.operation_refs.run_finish_revision == record.operation_refs.run_finish_revision
    assert len([call for call in mg.exec_calls if call[0] == RUN_LEDGER_BINDING]) == 1

    trace_store = SQLiteTraceStore(trace_store_path)
    try:
        refs = outputs_for_run(mg, run_ref=record.run_ref, trace_store=trace_store, read_context=TRACE_READ_CONTEXT)
    finally:
        trace_store.close()

    assert len(refs) == 1
    assert refs[0].identity.output_id == updated.outputs["workspace"].output_id


def test_publish_retained_workspace_output_published_record_requires_retained_custody(tmp_path) -> None:
    retained = retained_output_row()
    trace_store_path = tmp_path / "trace.sqlite"
    record = retained_published_run_record(row=retained, trace_store_path=trace_store_path)
    mg = fake_mg(runs=run_payload(record), retained_outputs=())

    with pytest.raises(RetainedWorkspaceOutputPublicationError, match="custody row is missing"):
        publish_retained_workspace_output(mg, run_ref=record.run_ref, trace_store_path=trace_store_path)

    assert show_run(mg, record.run_ref) == record
    assert mg.exec_calls == []


def test_publish_retained_workspace_output_published_record_rejects_invalid_custody(tmp_path) -> None:
    retained = retained_output_row()
    invalid = replace(retained, state="invalid", invalid_reason="settled elsewhere")
    trace_store_path = tmp_path / "trace.sqlite"
    record = retained_published_run_record(row=retained, trace_store_path=trace_store_path)
    mg = fake_mg(runs=run_payload(record), retained_outputs=(invalid,))

    with pytest.raises(RetainedWorkspaceOutputPublicationError, match="custody row is invalid: settled elsewhere"):
        publish_retained_workspace_output(mg, run_ref=record.run_ref, trace_store_path=trace_store_path)

    assert show_run(mg, record.run_ref) == record
    assert mg.exec_calls == []


def test_publish_retained_workspace_output_published_repair_rejects_custody_drift(tmp_path) -> None:
    retained = retained_output_row()
    drifted = replace(retained, resource_id="workspace-other")
    record = run_record(status="retained")
    mg = fake_mg(runs=run_payload(record), retained_outputs=(drifted,))

    with pytest.raises(RetainedWorkspaceOutputPublicationError, match="custody row is missing"):
        publish_retained_workspace_output(mg, run_ref=record.run_ref, trace_store_path=tmp_path / "trace.sqlite")

    assert show_run(mg, record.run_ref) == record
    assert mg.exec_calls == []


def test_publish_retained_workspace_output_missing_custody_does_not_mutate(tmp_path) -> None:
    record = retained_unpublished_run_record()
    mg = fake_mg(runs=run_payload(record), retained_outputs=())

    with pytest.raises(RetainedWorkspaceOutputPublicationError, match="custody row is missing"):
        publish_retained_workspace_output(mg, run_ref=record.run_ref, trace_store_path=tmp_path / "trace.sqlite")

    assert show_run(mg, record.run_ref) == record
    assert mg.exec_calls == []


def test_publish_retained_workspace_output_custody_mismatch_does_not_mutate(tmp_path) -> None:
    retained = retained_output_row()
    mismatched = replace(retained, handoff_ref="handoff-other")
    record = retained_unpublished_run_record(row=retained)
    mg = fake_mg(runs=run_payload(record), retained_outputs=(mismatched,))

    with pytest.raises(RetainedWorkspaceOutputPublicationError, match="custody row is missing"):
        publish_retained_workspace_output(mg, run_ref=record.run_ref, trace_store_path=tmp_path / "trace.sqlite")

    assert show_run(mg, record.run_ref) == record
    assert mg.exec_calls == []


def test_publish_retained_workspace_output_allows_run_input_world_to_differ_from_binding_basis(tmp_path) -> None:
    retained = retained_output_row()
    record = replace(retained_unpublished_run_record(row=retained), input_workspace_world_oid="w-other")
    mg = fake_mg(runs=run_payload(record), retained_outputs=(retained,))

    updated = publish_retained_workspace_output(
        mg,
        run_ref=record.run_ref,
        trace_store_path=tmp_path / "trace.sqlite",
    )

    assert updated.input_workspace_world_oid == "w-other"
    assert updated.outputs["workspace"].parent_basis_world_oid == retained.parent_basis_world_oid
    assert show_run(mg, record.run_ref) == updated
    assert len(mg.exec_calls) == 1


def test_publish_retained_workspace_output_descriptor_failure_does_not_mutate(tmp_path, monkeypatch) -> None:
    retained = retained_output_row()
    record = retained_unpublished_run_record(row=retained)
    mg = fake_mg(runs=run_payload(record), retained_outputs=(retained,))

    def fail_publish(*args: object, **kwargs: object) -> object:
        raise RuntimeError("trace store unavailable")

    monkeypatch.setattr(
        "shepherd_dialect.workspace_control.output_transition.publish_run_output_descriptor",
        fail_publish,
    )

    with pytest.raises(RuntimeError, match="trace store unavailable"):
        publish_retained_workspace_output(mg, run_ref=record.run_ref, trace_store_path=tmp_path / "trace.sqlite")

    assert show_run(mg, record.run_ref) == record
    assert mg.exec_calls == []


def test_publish_retained_workspace_output_retries_after_ledger_failure(tmp_path) -> None:
    retained = retained_output_row()
    record = retained_unpublished_run_record(row=retained)
    mg = FailingPublishVcsCore(
        {RUN_LEDGER_BINDING: run_payload(record)},
        retained_outputs=(retained,),
    )
    trace_store_path = tmp_path / "trace.sqlite"

    with pytest.raises(RuntimeError, match="run ledger unavailable"):
        publish_retained_workspace_output(mg, run_ref=record.run_ref, trace_store_path=trace_store_path)
    assert show_run(mg, record.run_ref) == record

    updated = publish_retained_workspace_output(mg, run_ref=record.run_ref, trace_store_path=trace_store_path)

    assert updated.terminalization.output_publication_status == "published"
    assert len(run_output_citations(mg, run_ref=record.run_ref)) == 1


def test_seal_handoff_publication_output_id_mismatch_fails_closed() -> None:
    parent = seal_parent_scope()
    handoff = seal_handoff(parent)
    draft = run_output_publication_from_seal_handoff(
        handoff,
        parent=parent,
        trace_ref=TraceRef(run_id="run-1", execution_id="exec-1", frontier_id="frontier-1"),
    )
    trace_store, citation = published_output_citation(draft)
    forged = replace(citation, output_id="run-output:forged")
    retained = retained_output_row_for_handoff(handoff, parent=parent)
    mg = fake_mg(retained_outputs=(retained,))

    with pytest.raises(RunOutputResolutionError, match="output_id disagrees"):
        RunOutputResolver(mg, trace_store=trace_store, read_context=TRACE_READ_CONTEXT).resolve((forged,))


def test_seal_handoff_publication_changed_paths_mismatch_fails_closed() -> None:
    parent = seal_parent_scope()
    handoff = seal_handoff(parent)
    draft = run_output_publication_from_seal_handoff(
        handoff,
        parent=parent,
        trace_ref=TraceRef(run_id="run-1", execution_id="exec-1", frontier_id="frontier-1"),
    )
    trace_store, citation = published_output_citation(draft)
    retained = retained_output_row_for_handoff(handoff, parent=parent, changed_paths=("other.py",))
    mg = fake_mg(retained_outputs=(retained,))

    with pytest.raises(RunOutputResolutionError, match="changed_paths disagree"):
        RunOutputResolver(mg, trace_store=trace_store, read_context=TRACE_READ_CONTEXT).resolve((citation,))


def test_product_output_query_rejects_custody_tuple_mismatch() -> None:
    retained = retained_output_row()
    mismatched = RetainedOutputQueryResult(
        scope_name=retained.scope_name,
        scope_ref=retained.scope_ref,
        scope_instance_id=retained.scope_instance_id,
        parent_ref=retained.parent_ref,
        parent_scope_name=retained.parent_scope_name,
        parent_scope_instance_id=retained.parent_scope_instance_id,
        state=retained.state,
        binding=retained.binding,
        output_world_oid="other-world",
        handoff_ref=retained.handoff_ref,
        parent_basis_world_oid=retained.parent_basis_world_oid,
        store_id=retained.store_id,
        resource_id=retained.resource_id,
        candidate_id=retained.candidate_id,
        candidate_ref=retained.candidate_ref,
        candidate_head=retained.candidate_head,
        changed_paths=retained.changed_paths,
    )
    mg = fake_mg(runs=run_payload(run_record(status="retained")), retained_outputs=(mismatched,))

    with pytest.raises(ValueError, match="no retained-output custody row"):
        outputs_for_run(mg, run_ref="run-1", descriptor_resolver=descriptor_resolver_for(output_descriptor_record()))


def test_product_output_query_rejects_trace_descriptor_mismatch() -> None:
    retained = retained_output_row()
    mg = fake_mg(runs=run_payload(run_record(status="retained")), retained_outputs=(retained,))

    with pytest.raises(RunOutputResolutionError, match="store_id"):
        outputs_for_run(
            mg,
            run_ref="run-1",
            descriptor_resolver=descriptor_resolver_for(output_descriptor_record(row=retained, store_id="other-store")),
        )


def test_run_output_citation_rejects_malformed_descriptor_locator() -> None:
    trace_ref = TraceRef(run_id="run-1", execution_id="exec-1", frontier_id="frontier-1")

    with pytest.raises(ValueError, match="unsupported schema"):
        RunOutputCitationRef(
            output_name="workspace",
            output_id="out-1",
            trace_ref=trace_ref,
            descriptor_locator={"schema": "other"},
            binding="workspace",
            store_id="store_workspace",
            resource_id="workspace",
            materialization_kind="tree",
            custody_ref="handoff-1",
            output_world_oid="w-out",
            parent_basis_world_oid="w-in",
        )


def test_run_output_citation_locator_must_match_citation_identity() -> None:
    trace_ref = TraceRef(run_id="run-1", execution_id="exec-1", frontier_id="frontier-1")

    with pytest.raises(ValueError, match="output_name disagrees"):
        RunOutputCitationRef(
            output_name="workspace",
            output_id="out-1",
            trace_ref=trace_ref,
            descriptor_locator={
                "schema": "shepherd2.skeleton.run_output_descriptor_locator.v1",
                "execution_id": "exec-1",
                "frontier_id": "frontier-1",
                "output_name": "other",
                "descriptor_fact_id": "fact-1",
                "schema_ref": "shepherd2.skeleton.run_output_descriptor.v1",
            },
            binding="workspace",
            store_id="store_workspace",
            resource_id="workspace",
            materialization_kind="tree",
            custody_ref="handoff-1",
            output_world_oid="w-out",
            parent_basis_world_oid="w-in",
        )


def test_run_output_citation_requires_product_custody_worlds() -> None:
    citation = output_citation()
    payload = citation.to_json()
    del payload["output_world_oid"]

    with pytest.raises(ValueError, match="output_world_oid"):
        RunOutputCitationRef.from_json(payload)

    payload = citation.to_json()
    del payload["parent_basis_world_oid"]

    with pytest.raises(ValueError, match="parent_basis_world_oid"):
        RunOutputCitationRef.from_json(payload)


def test_run_record_constructor_rejects_invalid_optional_strings() -> None:
    with pytest.raises(ValueError, match=r"run\.task_source_identity"):
        replace(run_record("run-1"), task_source_identity="")


def test_run_record_constructor_rejects_invalid_launch_context() -> None:
    with pytest.raises(TypeError, match=r"run\.launch_context"):
        replace(run_record("run-1"), launch_context={"launch_surface": "cli"})  # type: ignore[arg-type]


def test_workspace_control_run_record_requires_authority_context() -> None:
    with pytest.raises(ValueError, match=r"authority_context"):
        replace(run_record("run-1"), provider="shepherd.workspace_control.nucleus.v0")


def test_run_record_authority_context_must_match_may_profile() -> None:
    context = RunAuthorityContext(
        task_default_may="ReadWrite",
        requested_may="ReadOnly",
        effective_may="ReadOnly",
        repo_authority="readonly",
        workspace_selection_can_mutate=False,
        grant_clamp={"schema": "test.clamp"},
        effective_grant={"schema": "test.grant"},
        effective_grant_digest="digest:grant",
        effective_match_digest="digest:match",
        authority_surface_plan_digest="digest:plan",
        classifier_policy={"schema": "test.policy"},
    )

    with pytest.raises(ValueError, match=r"may_profile"):
        replace(run_record("run-1"), may_profile="ReadWrite", authority_context=context)


def test_run_record_rejects_output_map_key_that_disagrees_with_citation_name() -> None:
    record = run_record(status="retained")
    payload = record.to_json()
    payload["outputs"] = {"wrong": record.outputs["workspace"].to_json()}

    with pytest.raises(ValueError, match="outputs keys"):
        RunRecord.from_json(payload)
    with pytest.raises(ValueError, match="outputs keys"):
        replace(record, outputs={"wrong": record.outputs["workspace"]})


def test_run_record_rejects_outputs_on_non_retained_states() -> None:
    retained = run_record("run-source", status="retained")
    failed = run_record("run-failed", status="failed")
    merged = run_record("run-merged", status="merged")

    failed_citation = output_citation(trace_ref=failed.trace_ref)
    merged_citation = output_citation(trace_ref=merged.trace_ref)

    with pytest.raises(ValueError, match="terminal workspace world oid requires merged or retained"):
        replace(
            failed,
            outputs={"workspace": failed_citation},
            terminal_workspace_world_oid=failed_citation.output_world_oid,
        )
    with pytest.raises(ValueError, match="outputs require retained published terminalization"):
        replace(merged, outputs={"workspace": merged_citation})

    payload = failed.to_json()
    payload["outputs"] = {"workspace": failed_citation.to_json()}
    payload["terminal_workspace_world_oid"] = failed_citation.output_world_oid
    with pytest.raises(ValueError, match="terminal workspace world oid requires merged or retained"):
        RunRecord.from_json(payload)

    assert retained.outputs["workspace"].output_name == "workspace"


def test_run_record_rejects_retained_published_non_workspace_output() -> None:
    record = run_record("run-retained", status="retained")
    patch = named_output_citation("patch", trace_ref=record.trace_ref)

    with pytest.raises(ValueError, match="exactly one workspace output citation"):
        replace(record, outputs={"patch": patch})


def test_run_record_rejects_retained_published_extra_output() -> None:
    record = run_record("run-retained", status="retained")
    patch = named_output_citation("patch", trace_ref=record.trace_ref)

    with pytest.raises(ValueError, match="exactly one workspace output citation"):
        replace(record, outputs={**record.outputs, "patch": patch})


def test_run_record_rejects_retained_published_missing_output() -> None:
    record = run_record("run-retained", status="retained")

    with pytest.raises(ValueError, match="exactly one workspace output citation"):
        replace(record, outputs={})


def test_run_record_rejects_retained_unpublished_outputs() -> None:
    record = run_record("run-retained", status="retained")

    with pytest.raises(ValueError, match="unpublished retained output terminalization"):
        replace(
            record,
            terminalization=RunTerminalization(
                body_status="completed",
                world_disposition="retained",
                output_publication_status="failed",
                retained_custody=retained_custody(),
                publication_error={
                    "type": "RuntimeError",
                    "message": "boom",
                    "stage": "output_publication",
                    "retained_custody_ref": "handoff-1",
                    "retained_output_world_oid": "w-out",
                },
            ),
        )


def test_run_record_rejects_retained_published_citation_custody_drift() -> None:
    record = run_record("run-retained", status="retained")

    with pytest.raises(ValueError, match="retained custody ref disagrees"):
        replace(
            record,
            terminalization=RunTerminalization(
                body_status="completed",
                world_disposition="retained",
                output_publication_status="published",
                retained_custody=retained_custody(custody_ref="handoff-other"),
            ),
        )
    with pytest.raises(ValueError, match=r"world.*disagrees"):
        replace(
            record,
            terminalization=RunTerminalization(
                body_status="completed",
                world_disposition="retained",
                output_publication_status="published",
                retained_custody=retained_custody(output_world_oid="w-other"),
            ),
            terminal_workspace_world_oid=None,
        )


def test_run_record_rejects_output_citation_from_other_run() -> None:
    record = run_record("run-2", status="retained")

    with pytest.raises(ValueError, match="output citation trace_ref"):
        replace(record, outputs={"workspace": output_citation()})

    payload = record.to_json()
    payload["outputs"] = {"workspace": output_citation().to_json()}
    with pytest.raises(ValueError, match="output citation trace_ref"):
        RunRecord.from_json(payload)


def test_run_record_rejects_trace_ref_from_other_run() -> None:
    record = run_record("run-2")

    with pytest.raises(ValueError, match="trace_ref run_id"):
        replace(record, trace_ref=TraceRef(run_id="run-1", execution_id="exec-1", frontier_id="frontier-1"))


def test_run_record_rejects_task_execution_from_other_run() -> None:
    record = run_record("run-1")
    execution = replace(record.task_executions[0], run_ref="run-other")

    with pytest.raises(ValueError, match="task execution run_ref"):
        replace(record, task_executions=(execution,))


def test_run_record_rejects_pending_effect_from_other_run() -> None:
    record = run_record("run-1")
    effect = replace(record.pending_effects[0], run_ref="run-other")

    with pytest.raises(ValueError, match="pending effect run_ref"):
        replace(record, pending_effects=(effect,))


def test_run_record_rejects_pending_effect_trace_ref_from_other_run() -> None:
    record = run_record("run-1")
    effect = replace(
        record.pending_effects[0],
        trace_ref=TraceRef(run_id="run-other", execution_id="exec-1", frontier_id="frontier-1"),
    )

    with pytest.raises(ValueError, match="pending effect trace_ref"):
        replace(record, pending_effects=(effect,))


def test_run_record_rejects_retained_publication_failure_without_custody_refs() -> None:
    with pytest.raises(ValueError, match="retained_custody"):
        RunTerminalization(
            body_status="completed",
            world_disposition="retained",
            output_publication_status="failed",
            publication_error={"type": "RuntimeError", "message": "boom"},
        )


def test_run_terminalization_rejects_publication_error_custody_drift() -> None:
    with pytest.raises(ValueError, match="publication_error retained_custody_ref"):
        RunTerminalization(
            body_status="completed",
            world_disposition="retained",
            output_publication_status="failed",
            retained_custody=retained_custody(),
            publication_error={
                "type": "RuntimeError",
                "message": "boom",
                "stage": "output_publication",
                "retained_custody_ref": "handoff-other",
                "retained_output_world_oid": "w-out",
            },
        )

    with pytest.raises(ValueError, match="publication_error retained_output_world_oid"):
        RunTerminalization(
            body_status="completed",
            world_disposition="retained",
            output_publication_status="failed",
            retained_custody=retained_custody(),
            publication_error={
                "type": "RuntimeError",
                "message": "boom",
                "stage": "output_publication",
                "retained_custody_ref": "handoff-1",
                "retained_output_world_oid": "w-other",
            },
        )


def test_run_record_rejects_retained_unpublished_workspace_world_drift() -> None:
    record = run_record("run-retained", status="retained")

    with pytest.raises(ValueError, match="terminal workspace world oid"):
        replace(
            record,
            outputs={},
            terminal_workspace_world_oid="w-other",
            terminalization=RunTerminalization(
                body_status="completed",
                world_disposition="retained",
                output_publication_status="failed",
                retained_custody=retained_custody(),
                publication_error={
                    "type": "RuntimeError",
                    "message": "boom",
                    "stage": "output_publication",
                    "retained_custody_ref": "handoff-1",
                    "retained_output_world_oid": "w-out",
                },
            ),
        )


def test_run_record_rejects_terminal_workspace_world_on_non_world_disposition() -> None:
    for status in ("failed", "discarded", "cancelled"):
        with pytest.raises(ValueError, match="terminal workspace world oid requires merged or retained"):
            replace(run_record(f"run-{status}", status=status), terminal_workspace_world_oid="w-out")


def test_run_terminalization_rejects_retained_or_merged_world_without_completed_body() -> None:
    with pytest.raises(ValueError, match="merged/retained worlds require completed body"):
        RunTerminalization(
            body_status="failed",
            world_disposition="merged",
            output_publication_status="not_applicable",
        )

    with pytest.raises(ValueError, match="merged/retained worlds require completed body"):
        RunTerminalization(
            body_status="failed",
            world_disposition="retained",
            output_publication_status="failed",
            retained_custody=retained_custody(),
            publication_error={
                "type": "RuntimeError",
                "message": "boom",
                "stage": "output_publication",
                "retained_custody_ref": "handoff-1",
                "retained_output_world_oid": "w-out",
            },
        )


def test_run_terminalization_rejects_pending_or_running_body_with_world_disposition() -> None:
    with pytest.raises(ValueError, match="pending/running bodies require no world disposition"):
        RunTerminalization(
            body_status="pending",
            world_disposition="discarded",
            output_publication_status="not_applicable",
        )

    with pytest.raises(ValueError, match="pending/running bodies require no world disposition"):
        RunTerminalization(
            body_status="running",
            world_disposition="discarded",
            output_publication_status="not_applicable",
        )


def test_run_record_rejects_top_level_error_on_completed_records() -> None:
    retained = run_record("run-retained", status="retained")
    merged = run_record("run-merged", status="merged")

    with pytest.raises(ValueError, match=r"run.error requires non-completed body status"):
        replace(retained, error={"type": "RuntimeError", "message": "body failed"})
    with pytest.raises(ValueError, match=r"run.error requires non-completed body status"):
        replace(merged, error={"type": "RuntimeError", "message": "body failed"})


def test_run_record_rejects_top_level_error_on_non_failure_statuses() -> None:
    running = run_record("run-running", status="running")

    with pytest.raises(ValueError, match=r"run.error requires failed, discarded, or cancelled run status"):
        replace(running, error={"type": "RuntimeError", "message": "still running"})


def test_run_record_allows_top_level_error_on_failed_non_completed_record() -> None:
    record = replace(run_record("run-failed", status="failed"), error={"type": "RuntimeError", "message": "boom"})

    assert record.error == {"type": "RuntimeError", "message": "boom"}


def test_run_record_from_json_rejects_retained_world_with_failed_body() -> None:
    record = run_record("run-retained", status="retained")
    payload = record.to_json()
    raw_terminalization = payload["terminalization"]
    assert isinstance(raw_terminalization, dict)
    terminalization = dict(raw_terminalization)
    terminalization["body_status"] = "failed"
    payload["terminalization"] = terminalization

    with pytest.raises(ValueError, match="merged/retained worlds require completed body"):
        RunRecord.from_json(payload)


def test_run_record_rejects_top_level_publication_error_metadata() -> None:
    record = run_record("run-old-publication-failure", status="retained")

    with pytest.raises(ValueError, match="publication errors belong"):
        replace(
            record,
            outputs={},
            error={
                "type": "RuntimeError",
                "message": "descriptor store down",
                "stage": "output_publication",
                "phase": "run_output_descriptor",
                "retained_custody_ref": "handoff-1",
                "retained_output_world_oid": "w-out",
            },
        )


def test_run_record_from_json_requires_terminalization() -> None:
    record = run_record("run-published-publication-error", status="retained")
    payload = record.to_json()
    del payload["terminalization"]

    with pytest.raises(TypeError, match="run terminalization must be an object"):
        RunRecord.from_json(payload)


def test_old_failed_publication_shape_is_rejected() -> None:
    record = run_record("run-retained", status="retained")
    payload = record.to_json()
    payload["status"] = "failed"
    payload["outputs"] = {}
    payload["error"] = {
        "type": "RuntimeError",
        "message": "descriptor store down",
        "stage": "output_publication",
        "phase": "run_output_descriptor",
        "retained_custody_ref": "handoff-1",
        "retained_output_world_oid": "w-out",
    }

    with pytest.raises(ValueError, match="publication errors belong"):
        RunRecord.from_json(payload)


def test_queries_reject_unexpected_ledger_schema() -> None:
    mg = fake_mg(tasks={"schema": "other", "tasks": {}})

    with pytest.raises(ValueError, match="unsupported ledger schema"):
        list_tasks(mg)


def test_run_queries_reject_v1_run_ledger_schema() -> None:
    mg = fake_mg(runs={"schema": "shepherd.workspace_control.runs.v1", "runs": []})

    with pytest.raises(ValueError, match="unsupported ledger schema"):
        list_runs(mg)
