from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from shepherd2.trace_store import SQLiteTraceStore

import shepherd_dialect.trace as trace_module
from shepherd_dialect import CHILD_LAUNCH_REFUSED, CHILD_RUN_COMPLETED, CHILD_VALUE_COMPLETED, nucleus, task, workspace
from shepherd_dialect.nucleus import Failed, Finished, reset_workspace_for_tests
from shepherd_dialect.workspace_control import (
    ShepherdWorkspace,
    outputs_for_run,
    read_run_ledger_payload,
    run_vcscore_projection,
    show_run,
    trace_run,
)
from shepherd_dialect.workspace_control.output_transition import publish_retained_workspace_output
from shepherd_dialect.workspace_control.run_ledger import (
    append_execution,
    append_resolution,
    publish_record,
    publish_terminal_run_record,
)
from shepherd_dialect.workspace_control.schemas import (
    RunOperationRefs,
    RunRecord,
    RunRetainedCustody,
    RunTerminalization,
    TaskArtifactLock,
    TaskArtifactRef,
    TaskExecutionRecord,
    TaskResolutionRecord,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _reset_workspace() -> Iterator[None]:
    reset_workspace_for_tests()
    yield
    reset_workspace_for_tests()


def test_nucleus_run_publishes_to_existing_run_ledger(tmp_path) -> None:
    ws = workspace(model=object(), root=str(tmp_path))

    @task(may="Permissive")
    def returns_value(name: str) -> str:
        return f"hello {name}"

    run = returns_value.detailed("ledger")

    assert isinstance(run.outcome, Finished)
    assert run.outcome.value == "hello ledger"

    payload = read_run_ledger_payload(ws._mg)
    assert payload is not None
    assert payload["schema"] == "shepherd.workspace_control.runs.v2"
    assert len(payload["runs"]) == 1
    manifest = ws._mg.read_selected_binding_revision("shepherd.runs")
    assert manifest is not None
    assert manifest["schema"] == "shepherd.workspace_control.runs.v2"
    assert manifest["storage_shape"] == "keyed-json-tree"
    assert manifest["record_count"] == 1
    assert manifest["latest_run_ref"] == run.ref.id
    assert "runs" not in manifest
    keyed_row = ws._mg.read_selected_binding_json_entry(
        "shepherd.runs",
        f"data/runs/by-ref/{run.ref.id[:2]}/{run.ref.id}.json",
    )
    assert keyed_row is not None
    assert keyed_row["run_ref"] == run.ref.id

    exact = show_run(ws._mg, run.ref.id)
    assert exact is not None
    assert exact.run_ref == run.ref.id
    assert exact.task_version == "nucleus"
    assert exact.provider == "shepherd.nucleus.v1"
    assert exact.status == "merged"
    assert exact.operation_refs.trace_head == run._trace_head

    assert show_run(ws._mg, "@latest") == exact
    assert show_run(ws._mg, run.ref.id[:8]) == exact

    ledger_trace = trace_run(ws._mg, run.ref.id)
    assert ledger_trace is not None
    assert run.trace is not None
    assert ledger_trace.payload == run.trace.payload

    assert exact.operation_refs.runtime_operation is not None
    projection = run_vcscore_projection(ws._mg, run.ref.id)
    assert projection is not None
    assert projection["run_ref"] == run.ref.id
    assert projection["provider"] == "shepherd.nucleus.v1"
    assert projection["runtime_operation"] == exact.operation_refs.runtime_operation
    assert projection["operation_show"] == (
        "vcs-core",
        "operation",
        "show",
        exact.operation_refs.runtime_operation,
    )
    assert projection["trace_head"] == run._trace_head
    assert projection["trace_show"] == ("shepherd", "run", "trace", run.ref.id)


def test_internal_nucleus_seal_mode_publishes_resolver_compatible_output(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shepherd2.vnext import skeleton

    def fail_bridge_session(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("nucleus seal mode must not route through the skeleton bridge")

    monkeypatch.setattr(skeleton, "Session", fail_bridge_session)
    ws = workspace(model=object(), root=str(tmp_path))
    ws._mg.exec("filesystem", "write", scope=ws._mg.ground, path="base.txt", content=b"base\n")

    @task(may="Permissive")
    def writes_candidate(*, working_path: str) -> str:
        Path(working_path, "candidate.txt").write_text("candidate\n", encoding="utf-8")
        return "ok"

    run = writes_candidate.detailed_retained()

    assert isinstance(run.outcome, Finished)
    assert run.outcome.value == "ok"
    assert run.trace is not None
    assert run.trace.summary()["terminal_status"] == "retained"

    record = show_run(ws._mg, run.ref.id)
    assert record is not None
    assert record.status == "retained"
    assert record.terminalization == RunTerminalization(
        body_status="completed",
        world_disposition="retained",
        output_publication_status="published",
        retained_custody=RunRetainedCustody.from_output_citation(record.outputs["workspace"]),
    )
    assert record.trace_ref is not None
    assert record.outputs["workspace"].trace_ref == record.trace_ref
    assert record.terminal_workspace_world_oid == record.outputs["workspace"].output_world_oid

    rows = ws._mg.list_retained_outputs(parent=ws._mg.ground, binding="workspace", state="unconsumed")
    assert len(rows) == 1
    assert rows[0].handoff_ref == record.outputs["workspace"].custody_ref
    assert rows[0].changed_paths == ("candidate.txt",)

    trace_store = SQLiteTraceStore(ws.trace_store_path)
    try:
        refs = outputs_for_run(ws._mg, run_ref=run.ref.id, trace_store=trace_store)
    finally:
        trace_store.close()

    assert len(refs) == 1
    output = refs[0]
    assert output.state == "unconsumed"
    assert output.identity.output_id == record.outputs["workspace"].output_id
    assert output.changed_paths == ("candidate.txt",)


def test_internal_nucleus_async_seal_mode_publishes_resolver_compatible_output(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = workspace(model=object(), root=str(tmp_path))
    ws._mg.exec("filesystem", "write", scope=ws._mg.ground, path="base.txt", content=b"base\n")

    @task(may="Permissive")
    async def writes_candidate(*, working_path: str) -> str:
        Path(working_path, "async-candidate.txt").write_text("async candidate\n", encoding="utf-8")
        return "async-ok"

    run = asyncio.run(writes_candidate.detailed_retained())

    assert isinstance(run.outcome, Finished)
    assert run.outcome.value == "async-ok"
    record = show_run(ws._mg, run.ref.id)
    assert record is not None
    assert record.status == "retained"
    trace_store = SQLiteTraceStore(ws.trace_store_path)
    try:
        (output,) = outputs_for_run(ws._mg, run_ref=run.ref.id, trace_store=trace_store)
    finally:
        trace_store.close()
    assert output.state == "unconsumed"
    assert output.changed_paths == ("async-candidate.txt",)


def test_workspace_control_resolves_nucleus_produced_retained_output(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = workspace(model=object(), root=str(tmp_path))
    ws._mg.exec("filesystem", "write", scope=ws._mg.ground, path="base.txt", content=b"base\n")

    @task(may="Permissive")
    def writes_candidate(*, working_path: str) -> str:
        Path(working_path, "candidate.txt").write_text("candidate\n", encoding="utf-8")
        return "ok"

    run = writes_candidate.detailed_retained()
    facade = ShepherdWorkspace(ws._mg, trace_store_path=ws.trace_store_path, workspace_path=tmp_path)
    try:
        (output,) = facade.runs.outputs(run_ref=run.ref.id)
        changeset = facade.runs.changeset(run.ref.id)
    finally:
        facade.close()

    assert output.output_name == "workspace"
    assert output.state == "unconsumed"
    assert output.changed_paths == ("candidate.txt",)
    assert changeset.output_id == output.output_id
    assert changeset.stat().changed_paths == ("candidate.txt",)


def test_internal_nucleus_seal_mode_failure_publishes_no_output(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = workspace(model=object(), root=str(tmp_path))
    ws._mg.exec("filesystem", "write", scope=ws._mg.ground, path="base.txt", content=b"base\n")

    def writes_then_fails(*, working_path: str) -> str:
        Path(working_path, "candidate.txt").write_text("candidate\n", encoding="utf-8")
        raise RuntimeError("boom")

    run = nucleus._execute(
        writes_then_fails,
        (),
        {},
        may="Permissive",
        success_disposition="seal",
    )

    assert isinstance(run.outcome, Failed)
    assert run.trace is not None
    assert run.trace.summary()["terminal_status"] == "discarded"

    record = show_run(ws._mg, run.ref.id)
    assert record is not None
    assert record.status == "failed"
    assert record.terminalization is not None
    assert record.terminalization.world_disposition == "discarded"
    assert record.terminalization.output_publication_status == "not_applicable"
    assert record.outputs == {}
    assert ws._mg.list_retained_outputs(parent=ws._mg.ground, binding="workspace") == ()


def test_internal_nucleus_seal_mode_publication_failure_is_diagnosable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = workspace(model=object(), root=str(tmp_path))
    ws._mg.exec("filesystem", "write", scope=ws._mg.ground, path="base.txt", content=b"base\n")

    def fail_publication(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("descriptor store unavailable")

    monkeypatch.setattr(nucleus, "_publish_nucleus_run_output_descriptors", fail_publication)

    def writes_candidate(*, working_path: str) -> str:
        Path(working_path, "candidate.txt").write_text("candidate\n", encoding="utf-8")
        return "ok"

    run = nucleus._execute(
        writes_candidate,
        (),
        {},
        may="Permissive",
        success_disposition="seal",
    )

    assert isinstance(run.outcome, Failed)
    assert run.outcome.error_type == "RuntimeError"
    assert "descriptor store unavailable" in run.outcome.message

    record = show_run(ws._mg, run.ref.id)
    assert record is not None
    assert record.status == "retained"
    assert record.outputs == {}
    assert record.error is None
    publication_error = record.terminalization.publication_error
    assert publication_error is not None
    assert publication_error["stage"] == "output_publication"
    assert publication_error["phase"] == "run_output_descriptor"
    assert isinstance(publication_error["retained_custody_ref"], str)
    assert publication_error["retained_custody_ref"]
    assert isinstance(publication_error["retained_output_world_oid"], str)
    assert publication_error["retained_output_world_oid"]
    rows = ws._mg.list_retained_outputs(parent=ws._mg.ground, binding="workspace", state="unconsumed")
    assert len(rows) == 1
    assert record.terminalization == RunTerminalization(
        body_status="completed",
        world_disposition="retained",
        output_publication_status="failed",
        retained_custody=RunRetainedCustody.from_retained_output(rows[0]),
        publication_error=publication_error,
    )
    assert run.trace is not None
    assert run.trace.summary()["terminal_status"] == "retained"

    assert rows[0].handoff_ref == publication_error["retained_custody_ref"]
    assert rows[0].changed_paths == ("candidate.txt",)


def test_internal_nucleus_retained_publication_failure_publishes_hydratable_output(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = workspace(model=object(), root=str(tmp_path))
    ws._mg.exec("filesystem", "write", scope=ws._mg.ground, path="base.txt", content=b"base\n")

    publish_nucleus_descriptors = nucleus._publish_nucleus_run_output_descriptors

    def fail_once(*args: Any, **kwargs: Any) -> dict[str, Any]:
        monkeypatch.setattr(nucleus, "_publish_nucleus_run_output_descriptors", publish_nucleus_descriptors)
        raise RuntimeError("descriptor store unavailable")

    monkeypatch.setattr(nucleus, "_publish_nucleus_run_output_descriptors", fail_once)

    def writes_candidate(*, working_path: str) -> str:
        Path(working_path, "candidate.txt").write_text("candidate\n", encoding="utf-8")
        return "ok"

    run = nucleus._execute(
        writes_candidate,
        (),
        {},
        may="Permissive",
        success_disposition="seal",
    )

    assert isinstance(run.outcome, Failed)
    before = show_run(ws._mg, run.ref.id)
    assert before is not None
    assert before.status == "retained"
    assert before.terminalization.output_publication_status == "failed"
    assert before.outputs == {}
    assert outputs_for_run(ws._mg, run_ref=run.ref.id) == ()

    published = publish_retained_workspace_output(
        ws._mg,
        run_ref=run.ref.id,
        trace_store_path=ws.trace_store_path,
    )

    assert published.status == "retained"
    assert published.terminalization.output_publication_status == "published"
    assert published.terminalization.publication_error is None
    assert set(published.outputs) == {"workspace"}
    assert published.terminal_workspace_world_oid == published.outputs["workspace"].output_world_oid

    trace_store = SQLiteTraceStore(ws.trace_store_path)
    try:
        refs = outputs_for_run(ws._mg, run_ref=run.ref.id, trace_store=trace_store)
    finally:
        trace_store.close()

    assert len(refs) == 1
    assert refs[0].state == "unconsumed"
    assert refs[0].changed_paths == ("candidate.txt",)


def test_nested_value_child_does_not_masquerade_as_durable_run(tmp_path) -> None:
    ws = workspace(model=object(), root=str(tmp_path))

    @task(may="Permissive")
    def child() -> str:
        return "child"

    @task(may="Permissive")
    def parent() -> str:
        child_run = child.detailed()
        assert isinstance(child_run.outcome, Finished)
        return child_run.ref.id

    run = parent.detailed()

    assert isinstance(run.outcome, Finished)
    child_ref = run.outcome.value
    assert show_run(ws._mg, child_ref) is None
    assert trace_run(ws._mg, child_ref) is None
    assert run.trace is not None
    assert run.trace.filter(CHILD_RUN_COMPLETED) == ()
    (event,) = run.trace.filter(CHILD_VALUE_COMPLETED)
    assert event["child_run_ref"] == child_ref
    assert event["child_trace_token"].startswith("memory-trace:")
    assert event["evidence_level"] == "same_process_value"
    assert event["trace_materialized"] is False
    assert event["ledger_visible"] is False
    assert event["operation_identity_kind"] == "logical_placeholder"


def test_nested_value_child_never_attempts_durable_child_writes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = workspace(model=object(), root=str(tmp_path))
    appended_run_refs: list[str] = []
    published_run_refs: list[str] = []
    append_run_trace = trace_module.append_run_trace
    publish_nucleus_run_record = nucleus._publish_nucleus_run_record

    def record_append(*args: Any, **kwargs: Any) -> str:
        appended_run_refs.append(args[1]["run_ref"])
        return append_run_trace(*args, **kwargs)

    def record_publish(*args: Any, **kwargs: Any) -> None:
        published_run_refs.append(kwargs["run_ctx"].ref.id)
        return publish_nucleus_run_record(*args, **kwargs)

    monkeypatch.setattr(trace_module, "append_run_trace", record_append)
    monkeypatch.setattr(nucleus, "_publish_nucleus_run_record", record_publish)

    @task(may="Permissive")
    def child() -> str:
        return "child"

    @task(may="Permissive")
    def parent() -> str:
        child_run = child.detailed()
        assert isinstance(child_run.outcome, Finished)
        return child_run.ref.id

    run = parent.detailed()

    assert isinstance(run.outcome, Finished)
    child_ref = run.outcome.value
    assert appended_run_refs == [run.ref.id]
    assert published_run_refs == [run.ref.id]
    assert child_ref not in appended_run_refs
    assert child_ref not in published_run_refs
    assert run.trace is not None
    assert run.trace.filter(CHILD_RUN_COMPLETED) == ()
    (event,) = run.trace.filter(CHILD_VALUE_COMPLETED)
    assert event["child_run_ref"] == child_ref
    assert event["child_trace_token"] == f"memory-trace:{child_ref}"
    assert event["trace_materialized"] is False
    assert event["ledger_visible"] is False


def test_top_level_task_gets_carrier_path_but_logical_child_does_not(tmp_path) -> None:
    workspace(model=object(), root=str(tmp_path))

    @task(may="Permissive")
    def child(*, working_path: str | None = None) -> str | None:
        return working_path

    @task(may="Permissive")
    def parent(*, working_path: str | None = None) -> tuple[bool, str | None]:
        child_run = child.detailed()
        assert isinstance(child_run.outcome, Finished)
        return working_path is not None, child_run.outcome.value

    run = parent.detailed()

    assert isinstance(run.outcome, Finished)
    parent_saw_carrier, child_saw_carrier = run.outcome.value
    assert parent_saw_carrier is True
    assert child_saw_carrier is None
    assert run.trace is not None
    assert run.trace.filter(CHILD_RUN_COMPLETED) == ()
    (event,) = run.trace.filter(CHILD_VALUE_COMPLETED)
    assert event["evidence_level"] == "same_process_value"
    assert event["trace_materialized"] is False
    assert event["ledger_visible"] is False


def test_task_call_cannot_override_runtime_working_path_keyword(tmp_path) -> None:
    workspace(model=object(), root=str(tmp_path))

    @task(may="Permissive")
    def carrier_task(*, working_path: str | None = None) -> str | None:
        return working_path

    run = carrier_task.detailed(working_path="caller-controlled")

    assert isinstance(run.outcome, Failed)
    assert run.outcome.error_type == "ReservedRuntimeParameter"
    assert "runtime-owned" in run.outcome.message
    assert run.trace is not None
    assert run.trace.summary()["terminal_status"] == "discarded"


def test_task_call_cannot_smuggle_runtime_working_path_through_kwargs(tmp_path) -> None:
    workspace(model=object(), root=str(tmp_path))

    @task(may="Permissive")
    def carrier_task(**kwargs: str) -> str | None:
        return kwargs.get("working_path")

    run = carrier_task.detailed(working_path="caller-controlled")

    assert isinstance(run.outcome, Failed)
    assert run.outcome.error_type == "ReservedRuntimeParameter"
    assert "runtime-owned" in run.outcome.message
    assert run.trace is not None
    assert run.trace.summary()["terminal_status"] == "discarded"


def test_task_call_cannot_override_runtime_working_path_positionally(tmp_path) -> None:
    workspace(model=object(), root=str(tmp_path))

    @task(may="Permissive")
    def carrier_task(working_path: str | None = None) -> str | None:
        return working_path

    run = carrier_task.detailed("caller-controlled")

    assert isinstance(run.outcome, Failed)
    assert run.outcome.error_type == "ReservedRuntimeParameter"
    assert "runtime-owned" in run.outcome.message
    assert run.trace is not None
    assert run.trace.summary()["terminal_status"] == "discarded"


def test_readonly_parent_refuses_defaulted_permissive_child_before_body(tmp_path) -> None:
    workspace(model=object(), root=str(tmp_path))
    body_entered = False

    @task
    def defaulted_permissive_child() -> str:
        nonlocal body_entered
        body_entered = True
        return "child"

    @task(may="ReadOnly")
    def parent() -> tuple[str, str | None]:
        child_run = defaulted_permissive_child.detailed()
        return type(child_run.outcome).__name__, child_run.trace.run_ref if child_run.trace is not None else None

    run = parent.detailed()

    assert isinstance(run.outcome, Finished)
    assert run.outcome.value == ("Failed", None)
    assert body_entered is False
    assert run.trace is not None
    assert run.trace.filter(CHILD_RUN_COMPLETED) == ()
    assert run.trace.filter(CHILD_VALUE_COMPLETED) == ()
    (event,) = run.trace.filter(CHILD_LAUNCH_REFUSED)
    assert event["reason"] == "Failed"
    assert event["terminal_status"] == "refused"
    assert event["may_profile"] == "Permissive"


def test_terminal_publish_preserves_append_streams(tmp_path) -> None:
    ws = workspace(model=object(), root=str(tmp_path))
    lock = _task_lock()
    running = RunRecord(
        run_ref="run-append-streams",
        task_id="tests.parent",
        task_version="v1",
        task_schema_digest="sha256:task",
        args_digest="sha256:args",
        may_profile="Permissive",
        provider="shepherd.test",
        status="running",
        terminalization=RunTerminalization(
            body_status="running",
            world_disposition="none",
            output_publication_status="not_applicable",
        ),
        operation_refs=RunOperationRefs(run_start_revision="rev-start"),
    )
    publish_record(ws._mg, running)

    child_resolution = TaskResolutionRecord(
        resolution_id="resolution-child",
        reason="test",
        requested_ref="tests.child",
        task_ledger_head="task-head",
        task_lock=lock,
    )
    append_resolution(ws._mg, running.run_ref, child_resolution)
    append_resolution(ws._mg, running.run_ref, child_resolution)
    child_execution = TaskExecutionRecord(
        execution_id="execution-child",
        run_ref=running.run_ref,
        executor_kind="in_process",
        executor_id="test",
        executor_policy="trusted_bridge",
        call_kind="linked_call",
        status="completed",
        task_lock=lock,
        resolution_id="resolution-child",
    )
    append_execution(
        ws._mg,
        running.run_ref,
        child_execution,
    )
    append_execution(ws._mg, running.run_ref, child_execution)

    terminal = publish_terminal_run_record(
        ws._mg,
        RunRecord(
            run_ref=running.run_ref,
            task_id=running.task_id,
            task_version=running.task_version,
            task_schema_digest=running.task_schema_digest,
            args_digest=running.args_digest,
            may_profile=running.may_profile,
            provider=running.provider,
            status="merged",
            terminalization=RunTerminalization(
                body_status="completed",
                world_disposition="merged",
                output_publication_status="not_applicable",
            ),
            operation_refs=RunOperationRefs(run_start_revision="rev-start", trace_head="trace-head"),
            task_executions=(
                TaskExecutionRecord(
                    execution_id="execution-root",
                    run_ref=running.run_ref,
                    executor_kind="in_process",
                    executor_id="test",
                    executor_policy="trusted_bridge",
                    call_kind="root_run",
                    status="completed",
                    task_lock=lock,
                ),
            ),
        ),
    )

    assert [execution.execution_id for execution in terminal.task_executions] == [
        "execution-child",
        "execution-root",
    ]
    assert [resolution.resolution_id for resolution in terminal.task_resolutions] == ["resolution-child"]


def _task_lock() -> TaskArtifactLock:
    artifact_ref = TaskArtifactRef(
        binding="shepherd.tasks.artifacts",
        store_id="shepherd.workspace_control.task_artifacts",
        resource_id="task_artifacts",
        head="artifact-head",
        artifact_digest="sha256:artifact",
    )
    return TaskArtifactLock(
        task_id="tests.child",
        version="v1",
        artifact_ref=artifact_ref,
        artifact_digest=artifact_ref.artifact_digest,
        schema_digest="sha256:schema",
    )
