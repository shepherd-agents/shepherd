from __future__ import annotations

import pygit2
import pytest
from vcs_core._authority import _authority_settlement_pending_path
from vcs_core._dirty_flag import write_dirty_flag
from vcs_core._errors import OrphanedOperationsError
from vcs_core._materialization_run import MaterializationRun, write_materialization_run
from vcs_core._query_readiness import (
    ReadinessOperationAuthority,
    ReadinessRequest,
    ReadinessTarget,
    RuntimeAdmissionContext,
    evaluate_readiness,
    readiness_command_metadata,
)
from vcs_core._readiness_admission import require_readiness_allowed
from vcs_core._runtime_types import OperationRefInfo
from vcs_core._scope_world_inventory import RequiredBinding, probe_authority_ref, probe_scope, probe_selected_world
from vcs_core._sibling_groups import (
    CarrierLeaseRecord,
    SiblingGroupRecord,
    SiblingHandleRecord,
    sibling_machine_scope_name,
)
from vcs_core._workspace_authority import WorkspaceAuthorityPending, write_pending_workspace_authority
from vcs_core._world_operation_journal import OPERATION_JOURNAL_PATH
from vcs_core._world_refs import operation_journal_ref, world_open_operation_journal_index_ref
from vcs_core._world_storage_installation import (
    default_world_storage_root,
    open_existing_default_world_storage,
    open_or_init_default_world_storage,
)
from vcs_core.git_store import create_commit_with_recovery, insert_tree_entry
from vcs_core.recording import NestedParentAuthorization
from vcs_core.store import Store
from vcs_core.vcscore import VcsCore


def test_probe_scope_reports_ground_without_activation_handle(mg: VcsCore) -> None:
    item = probe_scope(mg._repo_path, "ground")

    assert item.domain == "scope"
    assert item.health.status == "present_valid"
    assert item.fields["scope_ref"] == "refs/vcscore/ground"
    assert item.source_identity["ref_target_oid"]


def test_readiness_reports_missing_selected_world_before_v2_authority_exists(mg: VcsCore) -> None:
    result = evaluate_readiness(
        mg._repo_path,
        ReadinessRequest.create(command="shepherd.run", requested_freshness="revalidated", allow_best_effort=False),
        owner=mg,
        force_freshness="revalidated",
    )
    payload = result.to_json()

    assert payload["schema"] == "vcscore/shepherd-query-readiness/v1"
    assert payload["readiness"]["allowed"] is False
    assert payload["readiness"]["state"] == "blocked"
    assert any(item["domain"] == "authority_ref" for item in payload["items"])
    assert any(item["health"]["status"] == "absent" for item in payload["items"])
    assert payload["mutation_precondition"] is None


def test_readiness_splits_materialize_from_push_status_policy(mg: VcsCore) -> None:
    status = mg.query_readiness(ReadinessRequest.create(command="shepherd.status"))
    materialize = mg.query_readiness(
        ReadinessRequest.create(command="vcscore.materialize", requested_freshness="locked", allow_best_effort=False)
    )
    push_alias = mg.query_readiness(
        ReadinessRequest.create(command="push", requested_freshness="locked", allow_best_effort=False)
    )

    assert status.allowed is True
    assert status.request.required_bindings == ()
    assert materialize.allowed is False
    assert push_alias.allowed is True
    assert materialize.request.command == "vcscore.materialize"
    assert push_alias.request.command == "vcscore.push-status"
    assert [binding.binding for binding in materialize.request.required_bindings] == ["workspace"]
    assert {blocker.kind for blocker in materialize.blockers} >= {"authority_ref", "world"}
    assert push_alias.request.required_bindings == ()
    assert {item.domain for item in push_alias.snapshot.items}.isdisjoint({"authority_ref", "world"})


def test_readiness_command_metadata_keeps_shepherd_and_private_policy_ids_separate() -> None:
    assert readiness_command_metadata("push") == {
        "command": "vcscore.push-status",
        "mutates": True,
        "shepherd_public": False,
        "default_freshness": "locked",
        "default_allow_best_effort": False,
        "observed_domains": [
            "authority_settlement",
            "operation_journal",
            "recovery",
            "scope",
            "workspace_authority",
        ],
        "blocking_domains": [
            "authority_settlement",
            "operation_journal",
            "recovery",
            "scope",
            "workspace_authority",
        ],
        "health_domains": [
            "authority_settlement",
            "operation_journal",
            "recovery",
            "scope",
            "workspace_authority",
        ],
        "consumed_domains": ["scope"],
        "precondition_domains": ["scope"],
        "blocking_recovery_kinds": [
            "dirty_push",
            "materialization_run",
            "orphaned_operation_ref",
            "orphaned_scope_ref",
            "scope_registry_mismatch",
            "sibling_group_blocker",
        ],
    }
    assert readiness_command_metadata("materialize")["command"] == "vcscore.materialize"
    assert readiness_command_metadata("materialize")["shepherd_public"] is True
    assert readiness_command_metadata("vcscore.lifecycle")["shepherd_public"] is False
    assert readiness_command_metadata("vcscore.lifecycle")["blocking_recovery_kinds"] == [
        "orphaned_operation_ref",
        "scope_registry_mismatch",
        "sibling_group_blocker",
    ]
    assert readiness_command_metadata("vcscore.reset-materialized")["blocking_recovery_kinds"] == [
        "sibling_group_blocker"
    ]
    retained_selection = readiness_command_metadata("retained-output-selection")
    assert retained_selection["command"] == "vcscore.retained-output-selection"
    assert retained_selection["mutates"] is True
    assert retained_selection["shepherd_public"] is False
    assert retained_selection["blocking_recovery_kinds"] == [
        "orphaned_operation_ref",
        "scope_registry_mismatch",
        "sibling_group_blocker",
    ]
    assert {"authority_ref", "world"}.issubset(retained_selection["observed_domains"])


@pytest.mark.parametrize(
    "command",
    [
        "shepherd.run",
        "vcscore.materialize",
        "vcscore.lifecycle",
        "vcscore.runtime",
        "vcscore.push-status",
        "vcscore.reset-materialized",
        "vcscore.retained-output-selection",
    ],
)
def test_mutating_readiness_requests_default_to_authoritative_policy(command: str) -> None:
    request = ReadinessRequest.create(command=command)

    assert request.requested_freshness == "locked"
    assert request.allow_best_effort is False


def test_retained_output_selection_readiness_is_parent_world_mutation(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")

    result = mg.query_readiness(
        ReadinessRequest.create(
            command="vcscore.retained-output-selection",
            scope=mg.ground.ref,
            requested_freshness="locked",
            allow_best_effort=False,
        )
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is True
    assert payload["readiness"]["command"] == "vcscore.retained-output-selection"
    assert result.request.required_bindings == ()
    assert {"authority_ref", "world"}.issubset({item["domain"] for item in payload["items"]})


@pytest.mark.parametrize(
    "command",
    ["vcscore.lifecycle", "vcscore.runtime", "vcscore.push-status", "vcscore.reset-materialized"],
)
def test_vcscore_control_policies_do_not_require_shepherd_workspace_binding(mg: VcsCore, command: str) -> None:
    result = mg.query_readiness(
        ReadinessRequest.create(command=command, requested_freshness="locked", allow_best_effort=False)
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is True
    assert payload["readiness"]["command"] == command
    assert payload["mutation_precondition"]["item_ids"]
    assert result.request.required_bindings == ()
    assert {item["domain"] for item in payload["items"]}.isdisjoint({"authority_ref", "world"})


def test_vcscore_lifecycle_policy_ignores_unconsumed_selected_world_binding_issues(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")

    result = mg.query_readiness(
        ReadinessRequest.create(
            command="vcscore.lifecycle",
            required_bindings=(RequiredBinding(binding="workspace", head_kind="filesystem", role="shepherd.Other"),),
            requested_freshness="locked",
            allow_best_effort=False,
        )
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is True
    assert payload["system_health"]["state"] == "healthy"
    assert {item["domain"] for item in payload["items"]}.isdisjoint({"authority_ref", "world"})
    assert all(item_id.startswith("scope:") for item_id in payload["mutation_precondition"]["item_ids"])


@pytest.mark.parametrize(
    "command",
    ["vcscore.lifecycle", "vcscore.runtime", "vcscore.push-status", "vcscore.reset-materialized"],
)
def test_vcscore_control_policies_block_workspace_authority(mg: VcsCore, command: str) -> None:
    _write_workspace_authority_pending(mg, f"wv-{command.rsplit('.', 1)[-1]}")

    result = mg.query_readiness(
        ReadinessRequest.create(command=command, requested_freshness="locked", allow_best_effort=False)
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is False
    assert payload["system_health"]["state"] == "needs_recovery"
    assert any(blocker["kind"] == "workspace_authority" for blocker in payload["blockers"])


@pytest.mark.parametrize(
    "command",
    ["vcscore.lifecycle", "vcscore.runtime", "vcscore.push-status", "vcscore.reset-materialized"],
)
def test_vcscore_control_policies_block_operation_journal(mg: VcsCore, command: str) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")
    manager = open_existing_default_world_storage(mg._repo_path)
    manager.open_operation_journal(
        operation_id=f"op-{command.rsplit('.', 1)[-1]}",
        operation_kind="shepherd.task",
        target_ref=mg.ground.ref,
        input_world_oid=None,
    )

    result = mg.query_readiness(
        ReadinessRequest.create(command=command, requested_freshness="locked", allow_best_effort=False)
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is False
    assert any(blocker["kind"] == "operation_journal" for blocker in payload["blockers"])


@pytest.mark.parametrize("command", ["vcscore.lifecycle", "vcscore.runtime", "vcscore.push-status"])
def test_vcscore_control_policies_block_live_owner_orphaned_operations(mg: VcsCore, command: str) -> None:
    mg._orphaned_operations = [
        OperationRefInfo(
            handle_id="op-readiness-orphaned",
            ref="refs/vcscore/ops/op-readiness-orphaned",
            kind="marker.runtime",
            scope_ref=mg.ground.ref,
            scope_instance_id=mg.ground.instance_id,
            parent_op_ref=None,
            base_oid=mg.ground.creation_oid,
            operation_id="op-readiness-orphaned",
            operation_label="op-readiness-orphaned",
            world_id=mg.ground.world_id,
        )
    ]

    result = mg.query_readiness(
        ReadinessRequest.create(command=command, requested_freshness="locked", allow_best_effort=False)
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is False
    assert any(blocker["kind"] == "recovery" for blocker in payload["blockers"])
    assert any(item["kind"] == "orphaned_operation_ref" for item in payload["items"])


def test_runtime_readiness_allows_active_operation_with_precondition(mg: VcsCore) -> None:
    operation = mg._pipeline.begin_operation(handle_id="active-runtime", kind="marker.runtime", scope=mg.ground)
    try:
        result = require_readiness_allowed(mg, command="vcscore.runtime", attempted="record")
        payload = result.to_json()
    finally:
        if mg._pipeline.current_operation() is not None:
            mg._pipeline.abort_operation(handle_id=operation.handle_id)

    assert payload["readiness"]["allowed"] is True
    assert payload["readiness"]["freshness"] == "locked"
    assert payload["readiness"]["recovery"]["required"] is False
    assert payload["system_health"]["state"] == "healthy"
    assert payload["mutation_precondition"] is not None
    assert payload["mutation_precondition"]["mode"] == "locked"
    assert any(item_id.startswith("scope:") for item_id in payload["mutation_precondition"]["item_ids"])
    assert any(
        item_id.startswith("operation:authorized:active-runtime")
        for item_id in payload["mutation_precondition"]["item_ids"]
    )


def test_runtime_readiness_precondition_revalidates_active_operation(mg: VcsCore) -> None:
    operation = mg._pipeline.begin_operation(handle_id="active-runtime", kind="marker.runtime", scope=mg.ground)
    request = ReadinessRequest.create(command="vcscore.runtime", requested_freshness="locked", allow_best_effort=False)
    try:
        result = mg.query_readiness(request)
        assert result.mutation_precondition is not None

        revalidated = mg.revalidate_readiness_precondition(request, result.mutation_precondition).to_json()
    finally:
        if mg._pipeline.current_operation() is not None:
            mg._pipeline.abort_operation(handle_id=operation.handle_id)

    assert revalidated["readiness"]["allowed"] is True
    assert revalidated["readiness"]["freshness"] == "revalidated"
    assert any(
        item_id.startswith("operation:authorized:active-runtime")
        for item_id in revalidated["mutation_precondition"]["item_ids"]
    )


def test_runtime_readiness_precondition_rejects_closed_active_operation(mg: VcsCore) -> None:
    operation = mg._pipeline.begin_operation(handle_id="active-runtime", kind="marker.runtime", scope=mg.ground)
    request = ReadinessRequest.create(command="vcscore.runtime", requested_freshness="locked", allow_best_effort=False)
    result = mg.query_readiness(request)
    assert result.mutation_precondition is not None
    mg._pipeline.abort_operation(handle_id=operation.handle_id)

    revalidated = mg.revalidate_readiness_precondition(request, result.mutation_precondition).to_json()

    assert revalidated["readiness"]["allowed"] is False
    assert revalidated["readiness"]["freshness"] == "revalidated"
    assert revalidated["mutation_precondition"] is None
    assert any(issue["code"] == "readiness_mutation_precondition_stale" for issue in revalidated["issues"])


def test_runtime_readiness_default_scope_rejects_implicit_cross_scope_operation(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-runtime-default-scope")
    operation = mg._pipeline.begin_operation(handle_id="task-runtime", kind="marker.runtime", scope=task)
    try:
        result = mg.query_readiness(
            ReadinessRequest.create(command="vcscore.runtime", requested_freshness="locked", allow_best_effort=False)
        )
        payload = result.to_json()
    finally:
        if mg._pipeline.current_operation() is not None:
            mg._pipeline.abort_operation(handle_id=operation.handle_id)
        mg.discard(task)

    assert payload["readiness"]["allowed"] is False
    assert payload["mutation_precondition"] is None
    assert any(issue["code"] == "readiness_operation_scope_mismatch" for issue in payload["issues"])
    assert any(blocker["kind"] == "operation" for blocker in payload["blockers"])


def test_runtime_readiness_admission_infers_active_operation_scope(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-runtime-inferred-scope")
    operation = mg._pipeline.begin_operation(handle_id="task-runtime", kind="marker.runtime", scope=task)
    try:
        result = require_readiness_allowed(mg, command="vcscore.runtime", attempted="record")
        payload = result.to_json()
    finally:
        if mg._pipeline.current_operation() is not None:
            mg._pipeline.abort_operation(handle_id=operation.handle_id)
        mg.discard(task)

    assert payload["readiness"]["allowed"] is True
    assert payload["scope"]["ref"] == task.ref
    assert any(
        item_id.startswith("operation:authorized:task-runtime")
        for item_id in payload["mutation_precondition"]["item_ids"]
    )


def test_runtime_readiness_allows_matching_explicit_operation_authority(mg: VcsCore) -> None:
    assert mg.ground.world_id is not None
    operation = mg.store.begin_operation(
        mg.ground.ref,
        handle_id="shell-lease",
        kind="vcs_core.session_shell",
        world_id=mg.ground.world_id,
        scope_instance_id=mg.ground.instance_id,
        operation_id="shell-lease",
        operation_label="session shell --capture: ground",
        session_id=mg._session_id,
    )
    request = ReadinessRequest.create(
        command="vcscore.runtime",
        requested_freshness="locked",
        allow_best_effort=False,
        authorized_operations=(
            ReadinessOperationAuthority(
                operation_id="shell-lease",
                kind="vcs_core.session_shell",
                scope_ref=mg.ground.ref,
                scope_instance_id=mg.ground.instance_id,
                session_id=mg._session_id,
            ),
        ),
    )
    try:
        payload = mg.query_readiness(request).to_json()
    finally:
        mg.store.abort_operation(operation)

    assert payload["readiness"]["allowed"] is True
    assert any(
        item["domain"] == "operation" and item["kind"] == "authorized_open_operation" for item in payload["items"]
    )
    assert any(
        item_id == "operation:authorized:shell-lease" for item_id in payload["mutation_precondition"]["item_ids"]
    )
    assert not any("shell-lease" in blocker["item_id"] for blocker in payload["blockers"])


def test_runtime_readiness_blocks_explicit_cross_scope_operation_authority(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-runtime-explicit-scope")
    assert task.world_id is not None
    operation = mg.store.begin_operation(
        task.ref,
        handle_id="shell-lease",
        kind="vcs_core.session_shell",
        world_id=task.world_id,
        scope_instance_id=task.instance_id,
        operation_id="shell-lease",
        operation_label="session shell --capture: task",
        session_id=mg._session_id,
    )
    request = ReadinessRequest.create(
        command="vcscore.runtime",
        requested_freshness="locked",
        allow_best_effort=False,
        authorized_operations=(
            ReadinessOperationAuthority(
                operation_id="shell-lease",
                kind="vcs_core.session_shell",
                scope_ref=task.ref,
                scope_instance_id=task.instance_id,
                session_id=mg._session_id,
            ),
        ),
    )
    try:
        payload = mg.query_readiness(request).to_json()
    finally:
        mg.store.abort_operation(operation)
        mg.discard(task)

    assert payload["readiness"]["allowed"] is False
    assert payload["mutation_precondition"] is None
    assert any(issue["code"] == "readiness_operation_scope_mismatch" for issue in payload["issues"])
    assert any(
        blocker["kind"] == "operation" and "shell-lease" in blocker["item_id"] for blocker in payload["blockers"]
    )


def test_nested_descendant_readiness_requires_owner_derived_authority(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    task = mg.fork(mg.ground, "task-runtime-nested-descendant")
    operation = mg._pipeline.begin_operation(
        handle_id="parent-runtime",
        kind="marker.runtime",
        scope=mg.ground,
        session_id=mg._session_id,
    )
    request = ReadinessRequest.create(
        command="vcscore.runtime",
        scope=task.ref,
        requested_freshness="locked",
        allow_best_effort=False,
        authorized_operations=(
            ReadinessOperationAuthority(
                operation_id=operation.durable_id,
                operation_ref=operation.ref,
                kind=operation.kind,
                scope_ref=mg.ground.ref,
                scope_instance_id=mg.ground.instance_id,
                session_id=mg._session_id,
            ),
        ),
    )
    try:
        owner_payload = mg.query_readiness(request).to_json()
        request_only_payload = evaluate_readiness(
            mg._repo_path, request, owner=None, force_freshness="locked"
        ).to_json()
    finally:
        if mg._pipeline.current_operation() is not None:
            mg._pipeline.abort_operation(handle_id=operation.handle_id)
        mg.discard(task)

    assert owner_payload["readiness"]["allowed"] is True
    assert any(
        item["kind"] == "authorized_open_operation" and item["fields"]["scope_ref"] == mg.ground.ref
        for item in owner_payload["items"]
    )
    assert request_only_payload["readiness"]["allowed"] is False
    assert any(blocker["kind"] == "operation" for blocker in request_only_payload["blockers"])


def test_nested_parent_target_blocks_until_child_quiescent(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    task = mg.fork(mg.ground, "task-runtime-parent-quiescence")
    parent_operation = mg._pipeline.begin_operation(
        handle_id="parent-runtime",
        kind="marker.runtime",
        scope=mg.ground,
        session_id=mg._session_id,
    )
    nested = NestedParentAuthorization(
        parent_scope_ref=mg.ground.ref,
        child_scope_ref=task.ref,
        ancestry_chain=(mg.ground.ref,),
    )
    child_operation = mg._pipeline.begin_operation(
        handle_id="child-runtime",
        kind="marker.runtime",
        scope=task,
        nested_parent=nested,
        world_disposition="adopt",
        session_id=mg._session_id,
    )
    try:
        payload = mg.query_readiness(
            ReadinessRequest.create(
                command="vcscore.runtime",
                scope=mg.ground.ref,
                requested_freshness="locked",
                allow_best_effort=False,
            )
        ).to_json()
    finally:
        if mg._pipeline.current_operation() is not None:
            mg._pipeline.abort_operation(handle_id=child_operation.handle_id)
        if mg._pipeline.current_operation() is not None:
            mg._pipeline.abort_operation(handle_id=parent_operation.handle_id)
        mg._pipeline.reset()
        mg.discard(task)

    assert payload["readiness"]["allowed"] is False
    assert any(item["kind"] == "nested_child_quiescence" for item in payload["items"])
    assert any(issue["code"] == "readiness_nested_child_quiescence" for issue in payload["issues"])


def test_nested_parent_target_trace_sidecar_exempts_with_source_identity(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    task = mg.fork(mg.ground, "task-runtime-trace-quiescence")
    parent_operation = mg._pipeline.begin_operation(
        handle_id="parent-runtime",
        kind="marker.runtime",
        scope=mg.ground,
        session_id=mg._session_id,
    )
    nested = NestedParentAuthorization(
        parent_scope_ref=mg.ground.ref,
        child_scope_ref=task.ref,
        ancestry_chain=(mg.ground.ref,),
    )
    child_operation = mg._pipeline.begin_operation(
        handle_id="child-runtime",
        kind="marker.runtime",
        scope=task,
        nested_parent=nested,
        world_disposition="adopt",
        session_id=mg._session_id,
    )
    try:
        request = ReadinessRequest.create(
            command="vcscore.runtime",
            scope=mg.ground.ref,
            requested_freshness="locked",
            allow_best_effort=False,
        )
        runtime_admission_context = RuntimeAdmissionContext(record_class="trace_evidence")
        result = mg._query_readiness_for_runtime(
            request,
            runtime_admission_context=runtime_admission_context,
        )
        assert result.mutation_precondition is not None
        runtime_revalidated = mg._revalidate_readiness_precondition_for_runtime(
            request,
            result.mutation_precondition,
            runtime_admission_context=runtime_admission_context,
        ).to_json()
        public_revalidated = mg.revalidate_readiness_precondition(
            request,
            result.mutation_precondition,
        ).to_json()
        public_revalidated_codes = tuple(
            issue["code"] for issue in public_revalidated["issues"] if isinstance(issue.get("code"), str)
        )
        payload = result.to_json()
    finally:
        if mg._pipeline.current_operation() is not None:
            mg._pipeline.abort_operation(handle_id=child_operation.handle_id)
        if mg._pipeline.current_operation() is not None:
            mg._pipeline.abort_operation(handle_id=parent_operation.handle_id)
        mg._pipeline.reset()
        mg.discard(task)

    assert payload["readiness"]["allowed"] is True
    item = next(item for item in payload["items"] if item["kind"] == "nested_child_quiescence_exempt")
    assert item["source_identity"]["operation_id"] == "child-runtime"
    assert item["fields"]["record_class"] == "trace_evidence"
    assert item["id"] in payload["mutation_precondition"]["item_ids"]
    assert runtime_revalidated["readiness"]["allowed"] is True
    assert runtime_revalidated["readiness"]["freshness"] == "revalidated"
    assert public_revalidated["readiness"]["allowed"] is False
    assert "readiness_nested_child_quiescence" in public_revalidated_codes
    assert "readiness_mutation_precondition_stale" in public_revalidated_codes


def test_public_query_readiness_does_not_accept_runtime_admission_context(mg: VcsCore) -> None:
    query_readiness = mg.query_readiness
    with pytest.raises(TypeError, match="runtime_admission_context"):
        query_readiness(
            ReadinessRequest.create(command="vcscore.runtime"),
            runtime_admission_context=RuntimeAdmissionContext(record_class="trace_evidence"),
        )


def test_readiness_request_has_no_record_class_input() -> None:
    request = ReadinessRequest.from_json(
        {
            "command": "vcscore.runtime",
            "record_class": "trace_evidence",
            "scope": "ground",
        }
    )

    assert not hasattr(request, "record_class")


def test_runtime_readiness_rejects_request_field_parent_authority_for_descendant_scope(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    parent = mg.fork(mg.ground, "task-runtime-nested-parent")
    child = mg.fork(parent, "task-runtime-nested-child")
    assert parent.world_id is not None
    operation = mg.store.begin_operation(
        parent.ref,
        handle_id="parent-run",
        kind="test.parent_run",
        world_id=parent.world_id,
        scope_instance_id=parent.instance_id,
        operation_id="parent-run",
        operation_label="parent run",
        session_id=mg._session_id,
    )
    request = ReadinessRequest.create(
        command="vcscore.runtime",
        scope=child.ref,
        requested_freshness="locked",
        allow_best_effort=False,
        authorized_operations=(
            ReadinessOperationAuthority(
                operation_id="parent-run",
                kind="test.parent_run",
                scope_ref=parent.ref,
                scope_instance_id=parent.instance_id,
                session_id=mg._session_id,
            ),
        ),
    )
    try:
        payload = mg.query_readiness(request).to_json()
    finally:
        mg.store.abort_operation(operation)
        mg.discard(child)
        mg.discard(parent)

    assert payload["readiness"]["allowed"] is False
    assert payload["scope"]["ref"] == child.ref
    assert any(issue["code"] == "readiness_operation_scope_mismatch" for issue in payload["issues"])


def test_runtime_readiness_allows_explicit_operation_authority_for_matching_requested_scope(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-runtime-explicit-matching-scope")
    assert task.world_id is not None
    operation = mg.store.begin_operation(
        task.ref,
        handle_id="shell-lease",
        kind="vcs_core.session_shell",
        world_id=task.world_id,
        scope_instance_id=task.instance_id,
        operation_id="shell-lease",
        operation_label="session shell --capture: task",
        session_id=mg._session_id,
    )
    request = ReadinessRequest.create(
        command="vcscore.runtime",
        scope=task.ref,
        requested_freshness="locked",
        allow_best_effort=False,
        authorized_operations=(
            ReadinessOperationAuthority(
                operation_id="shell-lease",
                kind="vcs_core.session_shell",
                scope_ref=task.ref,
                scope_instance_id=task.instance_id,
                session_id=mg._session_id,
            ),
        ),
    )
    try:
        payload = mg.query_readiness(request).to_json()
    finally:
        mg.store.abort_operation(operation)
        mg.discard(task)

    assert payload["readiness"]["allowed"] is True
    assert payload["scope"]["ref"] == task.ref
    assert any(
        item_id == "operation:authorized:shell-lease" for item_id in payload["mutation_precondition"]["item_ids"]
    )


def test_runtime_readiness_merges_partial_explicit_authority_with_implicit_stack(mg: VcsCore) -> None:
    operation = mg._pipeline.begin_operation(
        handle_id="active-runtime",
        kind="marker.runtime",
        scope=mg.ground,
        session_id=mg._session_id,
    )
    request = ReadinessRequest.create(
        command="vcscore.runtime",
        requested_freshness="locked",
        allow_best_effort=False,
        authorized_operations=(ReadinessOperationAuthority(operation_id=operation.durable_id),),
    )
    try:
        result = mg.query_readiness(request)
        assert result.mutation_precondition is not None
        payload = result.to_json()
        revalidated = mg.revalidate_readiness_precondition(request, result.mutation_precondition).to_json()
    finally:
        if mg._pipeline.current_operation() is not None:
            mg._pipeline.abort_operation(handle_id=operation.handle_id)

    item_id = f"operation:authorized:{operation.durable_id}"
    operation_items = [
        item for item in payload["items"] if item["id"] == item_id and item["kind"] == "authorized_open_operation"
    ]

    assert payload["readiness"]["allowed"] is True
    assert len(operation_items) == 1
    assert operation_items[0]["fields"]["operation_ref"] == operation.ref
    assert payload["mutation_precondition"]["item_ids"].count(item_id) == 1
    assert revalidated["readiness"]["allowed"] is True
    assert revalidated["mutation_precondition"]["item_ids"].count(item_id) == 1


def test_runtime_readiness_preserves_wrong_explicit_ref_with_implicit_stack(mg: VcsCore) -> None:
    operation = mg._pipeline.begin_operation(
        handle_id="active-runtime",
        kind="marker.runtime",
        scope=mg.ground,
        session_id=mg._session_id,
    )
    request = ReadinessRequest.create(
        command="vcscore.runtime",
        requested_freshness="locked",
        allow_best_effort=False,
        authorized_operations=(
            ReadinessOperationAuthority(
                operation_id=operation.durable_id,
                operation_ref="refs/vcscore/ops/not-the-active-runtime",
            ),
        ),
    )
    try:
        payload = mg.query_readiness(request).to_json()
    finally:
        if mg._pipeline.current_operation() is not None:
            mg._pipeline.abort_operation(handle_id=operation.handle_id)

    item_id = f"operation:authorized:{operation.durable_id}"
    operation_items = [
        item for item in payload["items"] if item["id"] == item_id and item["kind"] == "authorized_open_operation"
    ]

    assert payload["readiness"]["allowed"] is False
    assert payload["mutation_precondition"] is None
    assert len(operation_items) == 1
    assert operation_items[0]["source_identity"]["operation_ref"] == operation.ref
    assert any(issue["code"] == "readiness_operation_authority_mismatch" for issue in payload["issues"])


def test_runtime_readiness_preserves_wrong_explicit_scope_with_implicit_stack(mg: VcsCore) -> None:
    operation = mg._pipeline.begin_operation(
        handle_id="active-runtime",
        kind="marker.runtime",
        scope=mg.ground,
        session_id=mg._session_id,
    )
    request = ReadinessRequest.create(
        command="vcscore.runtime",
        requested_freshness="locked",
        allow_best_effort=False,
        authorized_operations=(
            ReadinessOperationAuthority(
                operation_id=operation.durable_id,
                scope_ref="refs/vcscore/scopes/not-ground",
            ),
        ),
    )
    try:
        payload = mg.query_readiness(request).to_json()
    finally:
        if mg._pipeline.current_operation() is not None:
            mg._pipeline.abort_operation(handle_id=operation.handle_id)

    item_id = f"operation:authorized:{operation.durable_id}"
    operation_items = [
        item for item in payload["items"] if item["id"] == item_id and item["kind"] == "authorized_open_operation"
    ]

    assert payload["readiness"]["allowed"] is False
    assert payload["mutation_precondition"] is None
    assert len(operation_items) == 1
    assert operation_items[0]["source_identity"]["operation_ref"] == operation.ref
    assert any(issue["code"] == "readiness_operation_authority_mismatch" for issue in payload["issues"])


def test_runtime_readiness_blocks_mismatched_explicit_operation_authority(mg: VcsCore) -> None:
    assert mg.ground.world_id is not None
    operation = mg.store.begin_operation(
        mg.ground.ref,
        handle_id="shell-lease",
        kind="vcs_core.session_shell",
        world_id=mg.ground.world_id,
        scope_instance_id=mg.ground.instance_id,
        operation_id="shell-lease",
        operation_label="session shell --capture: ground",
        session_id=mg._session_id,
    )
    request = ReadinessRequest.create(
        command="vcscore.runtime",
        requested_freshness="locked",
        allow_best_effort=False,
        authorized_operations=(
            ReadinessOperationAuthority(
                operation_id="shell-lease",
                kind="vcs_core.session_shell",
                scope_ref="refs/vcscore/scopes/other",
                scope_instance_id=mg.ground.instance_id,
                session_id=mg._session_id,
            ),
        ),
    )
    try:
        payload = mg.query_readiness(request).to_json()
    finally:
        mg.store.abort_operation(operation)

    assert payload["readiness"]["allowed"] is False
    assert payload["mutation_precondition"] is None
    assert any(issue["code"] == "readiness_operation_authority_mismatch" for issue in payload["issues"])


def test_runtime_readiness_keeps_unrelated_orphaned_operation_blocker(mg: VcsCore) -> None:
    operation = mg._pipeline.begin_operation(handle_id="active-runtime", kind="marker.runtime", scope=mg.ground)
    mg._orphaned_operations = [
        OperationRefInfo(
            handle_id="unrelated-runtime-orphan",
            ref="refs/vcscore/ops/unrelated-runtime-orphan",
            kind="marker.runtime",
            scope_ref=mg.ground.ref,
            scope_instance_id=mg.ground.instance_id,
            parent_op_ref=None,
            base_oid=mg.ground.creation_oid,
            operation_id="unrelated-runtime-orphan",
            operation_label="unrelated-runtime-orphan",
            world_id=mg.ground.world_id,
        )
    ]
    try:
        with pytest.raises(OrphanedOperationsError) as excinfo:
            require_readiness_allowed(mg, command="vcscore.runtime", attempted="record")
        result = excinfo.value._vcscore_readiness_result
        payload = result.to_json()
    finally:
        mg._orphaned_operations = []
        if mg._pipeline.current_operation() is not None:
            mg._pipeline.abort_operation(handle_id=operation.handle_id)

    assert payload["readiness"]["allowed"] is False
    assert payload["mutation_precondition"] is None
    assert any("unrelated-runtime-orphan" in blocker["item_id"] for blocker in payload["blockers"])


def test_reset_materialized_policy_does_not_inherit_orphaned_operation_blocker(mg: VcsCore) -> None:
    mg._orphaned_operations = [
        OperationRefInfo(
            handle_id="op-reset-orphaned",
            ref="refs/vcscore/ops/op-reset-orphaned",
            kind="marker.runtime",
            scope_ref=mg.ground.ref,
            scope_instance_id=mg.ground.instance_id,
            parent_op_ref=None,
            base_oid=mg.ground.creation_oid,
            operation_id="op-reset-orphaned",
            operation_label="op-reset-orphaned",
            world_id=mg.ground.world_id,
        )
    ]

    result = mg.query_readiness(
        ReadinessRequest.create(
            command="vcscore.reset-materialized",
            requested_freshness="locked",
            allow_best_effort=False,
        )
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is True
    assert payload["system_health"]["state"] == "healthy"
    assert payload["readiness"]["recovery"]["required"] is False
    assert any(item["kind"] == "orphaned_operation_ref" for item in payload["items"])
    assert not payload["blockers"]


@pytest.mark.parametrize("command", ["vcscore.lifecycle", "vcscore.runtime", "vcscore.reset-materialized"])
def test_vcscore_control_policies_do_not_inherit_dirty_push_blocker(mg: VcsCore, command: str) -> None:
    write_dirty_flag(mg._repo_path, "crashed-session")

    result = mg.query_readiness(
        ReadinessRequest.create(command=command, requested_freshness="locked", allow_best_effort=False)
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is True
    assert payload["system_health"]["state"] == "healthy"
    assert any(item["kind"] == "dirty_push" for item in payload["items"])
    assert not payload["blockers"]


@pytest.mark.parametrize("command", ["vcscore.lifecycle", "vcscore.runtime", "vcscore.reset-materialized"])
def test_vcscore_control_policies_do_not_inherit_materialization_run_blocker(mg: VcsCore, command: str) -> None:
    write_materialization_run(
        mg._repo_path,
        MaterializationRun(
            session_id="crashed-session",
            run_id="run-1",
            timestamp=1.0,
            planned_unit_ids=("unit-1",),
        ),
    )

    result = mg.query_readiness(
        ReadinessRequest.create(command=command, requested_freshness="locked", allow_best_effort=False)
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is True
    assert payload["system_health"]["state"] == "healthy"
    assert any(item["kind"] == "materialization_run" for item in payload["items"])
    assert not payload["blockers"]


@pytest.mark.parametrize("fact", ["dirty_push", "materialization_run"])
def test_push_status_policy_blocks_push_recovery_facts(mg: VcsCore, fact: str) -> None:
    if fact == "dirty_push":
        write_dirty_flag(mg._repo_path, "crashed-session")
    else:
        write_materialization_run(
            mg._repo_path,
            MaterializationRun(
                session_id="crashed-session",
                run_id="run-1",
                timestamp=1.0,
                planned_unit_ids=("unit-1",),
            ),
        )

    result = mg.query_readiness(
        ReadinessRequest.create(command="vcscore.push-status", requested_freshness="locked", allow_best_effort=False)
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is False
    assert payload["system_health"]["state"] == "needs_recovery"
    assert any(item["kind"] == fact for item in payload["items"])
    assert any(blocker["kind"] == "recovery" and fact in blocker["item_id"] for blocker in payload["blockers"])


@pytest.mark.parametrize(
    "command",
    ["vcscore.lifecycle", "vcscore.runtime", "vcscore.push-status", "vcscore.reset-materialized"],
)
def test_vcscore_control_policies_block_sibling_groups(mg: VcsCore, command: str) -> None:
    assert mg.store._publish_sibling_group_for_recovery_test(
        _sibling_group(mg, group_id="sg-111111111111"), expected_head_oid=None
    )

    result = mg.query_readiness(
        ReadinessRequest.create(command=command, requested_freshness="locked", allow_best_effort=False)
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is False
    assert any(blocker["kind"] == "recovery" for blocker in payload["blockers"])
    assert any(item["kind"] == "sibling_group_blocker" for item in payload["items"])


def test_authority_ref_probe_reports_unreadable_world_storage_as_invalid(workspace) -> None: # type: ignore[no-untyped-def]
    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    storage_root = default_world_storage_root(store._repo_path)
    storage_root.mkdir(parents=True)
    (storage_root / "world-stores.json").write_text("not json")

    item = probe_authority_ref(store._repo_path, "refs/vcscore/ground")

    assert item.health.presence == "present"
    assert item.health.validity == "invalid"
    assert item.health.issue_codes == ("authority_ref_unreadable",)


def test_readiness_blocks_unreadable_world_storage_as_invalid(workspace) -> None: # type: ignore[no-untyped-def]
    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    storage_root = default_world_storage_root(store._repo_path)
    storage_root.mkdir(parents=True)
    (storage_root / "world-stores.json").write_text("not json")

    result = evaluate_readiness(
        store._repo_path,
        ReadinessRequest.create(command="shepherd.run", requested_freshness="revalidated", allow_best_effort=False),
        force_freshness="revalidated",
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is False
    assert payload["readiness"]["recovery"]["required"] is True
    assert payload["system_health"]["state"] == "needs_recovery"
    assert any(
        item["domain"] == "authority_ref" and item["health"]["validity"] == "invalid" for item in payload["items"]
    )


def test_readiness_request_round_trips_recovery_targets() -> None:
    request = ReadinessRequest.create(
        command="vcscore.recover",
        requested_freshness="locked",
        allow_best_effort=False,
        targets=(ReadinessTarget(domain="workspace_authority", operation_id="wv-target"),),
    )

    parsed = ReadinessRequest.from_json(request.to_json())

    assert parsed.targets == (ReadinessTarget(domain="workspace_authority", operation_id="wv-target"),)


def test_readiness_request_round_trips_recovery_target_kind() -> None:
    request = ReadinessRequest.create(
        command="vcscore.recover",
        requested_freshness="locked",
        allow_best_effort=False,
        targets=(
            ReadinessTarget(
                domain="recovery",
                kind="orphaned_operation_ref",
                item_id="recovery:orphaned_operation:refs/vcscore/ops/op-1",
            ),
        ),
    )

    parsed = ReadinessRequest.from_json(request.to_json())

    assert parsed.targets == (
        ReadinessTarget(
            domain="recovery",
            kind="orphaned_operation_ref",
            item_id="recovery:orphaned_operation:refs/vcscore/ops/op-1",
        ),
    )


def test_readiness_request_round_trips_authorized_operations() -> None:
    authority = ReadinessOperationAuthority(
        operation_id="shell-lease",
        operation_ref="refs/vcscore/ops/shell-lease",
        kind="vcs_core.session_shell",
        scope_ref="refs/vcscore/scopes/task",
        scope_instance_id="scope-instance",
        session_id="session-id",
    )
    request = ReadinessRequest.create(
        command="vcscore.runtime",
        requested_freshness="locked",
        allow_best_effort=False,
        authorized_operations=(authority,),
    )

    parsed = ReadinessRequest.from_json(request.to_json())

    assert parsed.authorized_operations == (authority,)


def test_readiness_request_rejects_authorized_operations_for_non_runtime_command() -> None:
    with pytest.raises(ValueError, match="authorized operation authorities"):
        ReadinessRequest.create(
            command="shepherd.run",
            authorized_operations=(ReadinessOperationAuthority(operation_id="shell-lease"),),
        )


def test_readiness_request_rejects_duplicate_explicit_operation_authorities() -> None:
    with pytest.raises(ValueError, match="unique by operation_id"):
        ReadinessRequest.create(
            command="vcscore.runtime",
            requested_freshness="locked",
            allow_best_effort=False,
            authorized_operations=(
                ReadinessOperationAuthority(
                    operation_id="shell-lease",
                    operation_ref="refs/vcscore/ops/stale-shell-lease",
                ),
                ReadinessOperationAuthority(
                    operation_id="shell-lease",
                    operation_ref="refs/vcscore/ops/current-shell-lease",
                ),
            ),
        )


def test_readiness_request_from_json_rejects_duplicate_explicit_operation_authorities() -> None:
    with pytest.raises(ValueError, match="unique by operation_id"):
        ReadinessRequest.from_json(
            {
                "command": "vcscore.runtime",
                "requested_freshness": "locked",
                "allow_best_effort": False,
                "authorized_operations": [
                    {
                        "operation_id": "shell-lease",
                        "operation_ref": "refs/vcscore/ops/stale-shell-lease",
                    },
                    {
                        "operation_id": "shell-lease",
                        "operation_ref": "refs/vcscore/ops/current-shell-lease",
                    },
                ],
            }
        )


def test_readiness_rejects_broad_recovery_domain_target_for_mutating_recovery() -> None:
    with pytest.raises(ValueError, match="targets must include kind"):
        ReadinessRequest.create(
            command="vcscore.recover",
            requested_freshness="locked",
            allow_best_effort=False,
            targets=(ReadinessTarget(domain="recovery", item_id="recovery:dirty_push:session"),),
        )

    with pytest.raises(ValueError, match="targets must include item_id"):
        ReadinessRequest.create(
            command="vcscore.recover",
            requested_freshness="locked",
            allow_best_effort=False,
            targets=(ReadinessTarget(domain="recovery", kind="dirty_push"),),
        )


def test_readiness_rejects_broad_recovery_domain_target_for_direct_request_construction() -> None:
    with pytest.raises(ValueError, match="targets must include kind"):
        ReadinessRequest(
            command="vcscore.recover",
            requested_freshness="locked",
            allow_best_effort=False,
            targets=(ReadinessTarget(domain="recovery", item_id="recovery:dirty_push:session"),),
        )


def test_readiness_request_dto_defaults_do_not_smuggle_shepherd_bindings() -> None:
    request = ReadinessRequest()

    assert request.command == "shepherd.status"
    assert request.required_bindings == ()


def test_direct_readiness_request_construction_applies_command_policy_defaults() -> None:
    request = ReadinessRequest(command="shepherd.run")

    assert request.requested_freshness == "locked"
    assert request.allow_best_effort is False
    assert [binding.binding for binding in request.required_bindings] == ["workspace"]


def test_readiness_applies_policy_workspace_binding_for_shepherd_run(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")
    request = ReadinessRequest.create(
        command="shepherd.run",
        required_bindings=(),
        requested_freshness="revalidated",
        allow_best_effort=False,
    )

    assert request.required_bindings == (
        RequiredBinding(binding="workspace", head_kind="filesystem", role="shepherd.WorkspaceRef"),
    )

    result = evaluate_readiness(
        mg._repo_path,
        request,
        owner=mg,
        force_freshness="revalidated",
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is True
    assert any(
        item["kind"] == "selected_binding" and item["fields"]["binding"] == "workspace" for item in payload["items"]
    )


def test_readiness_merges_extra_bindings_with_mutating_baseline() -> None:
    extra = RequiredBinding(binding="trace", head_kind="json", role="shepherd.TraceRef")
    request = ReadinessRequest.create(command="shepherd.run", required_bindings=(extra,))

    assert request.required_bindings == (
        RequiredBinding(binding="workspace", head_kind="filesystem", role="shepherd.WorkspaceRef"),
        extra,
    )


def test_readiness_allows_best_effort_status_without_authorizing_mutation(mg: VcsCore) -> None:
    result = evaluate_readiness(
        mg._repo_path,
        ReadinessRequest.create(command="shepherd.status", requested_freshness="best_effort"),
        owner=mg,
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is True
    assert payload["readiness"]["state"] == "safe_to_run"
    assert payload["readiness"]["admission_authoritative"] is True


def test_readiness_allows_revalidated_run_after_workspace_selection(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")

    result = evaluate_readiness(
        mg._repo_path,
        ReadinessRequest.create(command="shepherd.run", requested_freshness="revalidated", allow_best_effort=False),
        owner=mg,
        force_freshness="revalidated",
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is True
    assert payload["readiness"]["state"] == "safe_to_run"
    assert payload["readiness"]["admission_authoritative"] is True
    assert payload["mutation_precondition"]["mode"] == "revalidated"
    assert payload["snapshot"]["consistency"] == "locked"
    assert any(
        item["kind"] == "selected_binding" and item["fields"]["binding"] == "workspace" for item in payload["items"]
    )


def test_revalidate_readiness_precondition_accepts_unchanged_state(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")
    request = ReadinessRequest.create(command="shepherd.run", requested_freshness="revalidated", allow_best_effort=False)
    result = mg.query_readiness(request)
    payload = result.to_json()

    revalidated = mg.revalidate_readiness_precondition(request, payload["mutation_precondition"])

    assert revalidated.allowed is True
    assert revalidated.mutation_precondition is not None
    assert revalidated.freshness == "revalidated"


def test_revalidate_readiness_precondition_rejects_changed_source_identity(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")
    request = ReadinessRequest.create(command="shepherd.run", requested_freshness="revalidated", allow_best_effort=False)
    payload = mg.query_readiness(request).to_json()

    mg.exec("filesystem", "write", scope=mg.ground, path="changed.txt", content=b"changed")

    revalidated = mg.revalidate_readiness_precondition(request, payload["mutation_precondition"]).to_json()

    assert revalidated["readiness"]["allowed"] is False
    assert revalidated["readiness"]["state"] == "blocked"
    assert revalidated["readiness"]["freshness"] == "revalidated"
    assert revalidated["mutation_precondition"] is None
    assert any(issue["code"] == "readiness_mutation_precondition_stale" for issue in revalidated["issues"])


def test_revalidate_readiness_precondition_rejects_command_mismatch(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")
    request = ReadinessRequest.create(command="shepherd.run", requested_freshness="revalidated", allow_best_effort=False)
    payload = mg.query_readiness(request).to_json()
    recover_request = ReadinessRequest.create(
        command="shepherd.recover",
        requested_freshness="revalidated",
        allow_best_effort=False,
    )

    with pytest.raises(ValueError, match="command"):
        mg.revalidate_readiness_precondition(recover_request, payload["mutation_precondition"])


def test_vcscore_query_readiness_uses_locked_policy_path(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")

    result = mg.query_readiness(
        ReadinessRequest.create(command="shepherd.run", requested_freshness="locked", allow_best_effort=False)
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is True
    assert payload["readiness"]["freshness"] == "locked"
    assert payload["mutation_precondition"]["mode"] == "locked"
    assert payload["snapshot"]["consistency"] == "locked"
    item_ids = payload["mutation_precondition"]["item_ids"]
    assert any(item_id.startswith("scope:") for item_id in item_ids)
    assert any(item_id.startswith("authority_ref:") for item_id in item_ids)
    assert any(item_id.startswith("world:") for item_id in item_ids)
    assert any(item_id.startswith("world_binding:") for item_id in item_ids)
    assert not any(
        item_id.startswith(("operation_journal:", "recovery:", "workspace_authority:")) for item_id in item_ids
    )


def test_vcscore_query_readiness_blocks_live_owner_orphaned_operation(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")
    mg._orphaned_operations = [
        OperationRefInfo(
            handle_id="orphan-op",
            kind="marker.runtime",
            ref="refs/vcscore/ops/orphan-op",
            scope_ref=mg.ground.ref,
            scope_instance_id=mg.ground.instance_id,
            parent_op_ref=None,
            base_oid=mg.ground.creation_oid,
            operation_id="op_orphan",
            operation_label="orphan-op",
            world_id=mg.ground.world_id,
        )
    ]

    result = mg.query_readiness(
        ReadinessRequest.create(command="shepherd.run", requested_freshness="locked", allow_best_effort=False)
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is False
    assert payload["readiness"]["recovery"]["required"] is True
    assert payload["system_health"]["state"] == "needs_recovery"
    assert payload["mutation_precondition"] is None
    assert any(item["kind"] == "orphaned_operation_ref" for item in payload["items"])
    assert any(
        blocker["kind"] == "recovery" and "orphaned_operation" in blocker["item_id"] for blocker in payload["blockers"]
    )


def test_readiness_best_effort_run_with_selected_world_is_observed_clear(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")

    result = evaluate_readiness(
        mg._repo_path,
        ReadinessRequest.create(command="shepherd.run", requested_freshness="best_effort", allow_best_effort=True),
        owner=mg,
    )
    payload = result.to_json()

    # Non-authoritative class (a mutation admitted at the chokepoint): JSON `allowed`
    # is null, not false, so a consumer cannot misread a non-answer as "blocked".
    # The internal bool is unchanged (still False); only the published surface is null.
    assert payload["readiness"]["allowed"] is None
    assert result.allowed is False
    assert payload["readiness"]["state"] == "observed_clear"
    assert payload["readiness"]["admission_authoritative"] is False
    assert payload["mutation_precondition"] is None


def test_readiness_blocks_shepherd_run_on_open_operation_journal(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")
    manager = open_existing_default_world_storage(mg._repo_path)
    manager.open_operation_journal(
        operation_id="op-readiness-open",
        operation_kind="shepherd.task",
        target_ref=mg.ground.ref,
        input_world_oid=None,
    )

    result = evaluate_readiness(
        mg._repo_path,
        ReadinessRequest.create(command="shepherd.run", requested_freshness="revalidated", allow_best_effort=False),
        owner=mg,
        force_freshness="revalidated",
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is False
    assert payload["readiness"]["recovery"]["required"] is True
    assert any(blocker["kind"] == "operation_journal" for blocker in payload["blockers"])


def test_readiness_recover_command_does_not_block_on_open_operation_journal(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")
    manager = open_existing_default_world_storage(mg._repo_path)
    manager.open_operation_journal(
        operation_id="op-readiness-recover",
        operation_kind="shepherd.task",
        target_ref=mg.ground.ref,
        input_world_oid=None,
    )

    result = evaluate_readiness(
        mg._repo_path,
        ReadinessRequest.create(command="shepherd.recover", requested_freshness="revalidated", allow_best_effort=False),
        owner=mg,
        force_freshness="revalidated",
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is True
    assert not any(blocker["kind"] == "operation_journal" for blocker in payload["blockers"])


def test_recover_readiness_requires_operation_journal_domain_for_journal_target(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")
    manager = open_existing_default_world_storage(mg._repo_path)
    manager.open_operation_journal(
        operation_id="op-readiness-cross-domain",
        operation_kind="shepherd.task",
        target_ref=mg.ground.ref,
        input_world_oid=None,
    )

    result = evaluate_readiness(
        mg._repo_path,
        ReadinessRequest.create(
            command="vcscore.recover",
            requested_freshness="locked",
            allow_best_effort=False,
            targets=(
                ReadinessTarget(
                    domain="recovery",
                    kind="orphaned_operation_ref",
                    operation_id="op-readiness-cross-domain",
                ),
            ),
        ),
        owner=mg,
        force_freshness="locked",
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is False
    assert any(blocker["kind"] == "operation_journal" for blocker in payload["blockers"])
    assert any(issue["code"] == "readiness_recovery_target_missing" for issue in payload["issues"])


def test_recover_readiness_allows_targeted_operation_journal(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")
    manager = open_existing_default_world_storage(mg._repo_path)
    manager.open_operation_journal(
        operation_id="op-readiness-journal-target",
        operation_kind="shepherd.task",
        target_ref=mg.ground.ref,
        input_world_oid=None,
    )

    result = evaluate_readiness(
        mg._repo_path,
        ReadinessRequest.create(
            command="vcscore.recover",
            requested_freshness="locked",
            allow_best_effort=False,
            targets=(
                ReadinessTarget(
                    domain="operation_journal",
                    kind="v2_world_operation_journal",
                    operation_id="op-readiness-journal-target",
                ),
            ),
        ),
        owner=mg,
        force_freshness="locked",
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is True
    assert not payload["blockers"]
    assert any(item_id.startswith("operation_journal:") for item_id in payload["mutation_precondition"]["item_ids"])


def test_recover_readiness_allows_targeted_workspace_authority(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")
    _write_workspace_authority_pending(mg, "wv-target")

    result = evaluate_readiness(
        mg._repo_path,
        ReadinessRequest.create(
            command="vcscore.recover",
            requested_freshness="locked",
            allow_best_effort=False,
            targets=(ReadinessTarget(domain="workspace_authority", operation_id="wv-target"),),
        ),
        owner=mg,
        force_freshness="locked",
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is True
    assert payload["readiness"]["recovery"]["required"] is True
    assert payload["mutation_precondition"]["mode"] == "locked"
    assert any(
        item_id.startswith("workspace_authority_pending:") for item_id in payload["mutation_precondition"]["item_ids"]
    )


def test_recover_readiness_requires_all_target_selectors_to_match(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")
    _write_workspace_authority_pending(mg, "wv-target")
    status_payload = evaluate_readiness(
        mg._repo_path,
        ReadinessRequest.create(command="shepherd.status"),
        owner=mg,
    ).to_json()
    item_id = next(item["id"] for item in status_payload["items"] if item["domain"] == "workspace_authority")

    result = evaluate_readiness(
        mg._repo_path,
        ReadinessRequest.create(
            command="vcscore.recover",
            requested_freshness="locked",
            allow_best_effort=False,
            targets=(
                ReadinessTarget(
                    domain="workspace_authority",
                    item_id=item_id,
                    operation_id="wv-other",
                ),
            ),
        ),
        owner=mg,
        force_freshness="locked",
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is False
    assert any(issue["code"] == "readiness_recovery_target_missing" for issue in payload["issues"])


def test_recover_readiness_blocks_unrelated_workspace_authority_target(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")
    _write_workspace_authority_pending(mg, "wv-target")
    _write_workspace_authority_pending(mg, "wv-unrelated")

    result = evaluate_readiness(
        mg._repo_path,
        ReadinessRequest.create(
            command="vcscore.recover",
            requested_freshness="locked",
            allow_best_effort=False,
            targets=(ReadinessTarget(domain="workspace_authority", operation_id="wv-target"),),
        ),
        owner=mg,
        force_freshness="locked",
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is False
    assert any(blocker["kind"] == "workspace_authority" for blocker in payload["blockers"])


def test_recover_readiness_blocks_missing_explicit_target(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")

    result = evaluate_readiness(
        mg._repo_path,
        ReadinessRequest.create(
            command="vcscore.recover",
            requested_freshness="locked",
            allow_best_effort=False,
            targets=(ReadinessTarget(domain="workspace_authority", operation_id="wv-missing"),),
        ),
        owner=mg,
        force_freshness="locked",
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is False
    assert any(issue["code"] == "readiness_recovery_target_missing" for issue in payload["issues"])


def test_recover_readiness_blocks_non_recoverable_explicit_target(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")
    status_payload = evaluate_readiness(
        mg._repo_path,
        ReadinessRequest.create(command="shepherd.status"),
        owner=mg,
    ).to_json()
    item_id = next(item["id"] for item in status_payload["items"] if item["domain"] == "scope")

    result = evaluate_readiness(
        mg._repo_path,
        ReadinessRequest.create(
            command="vcscore.recover",
            requested_freshness="locked",
            allow_best_effort=False,
            targets=(ReadinessTarget(domain="scope", item_id=item_id),),
        ),
        owner=mg,
        force_freshness="locked",
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is False
    assert payload["mutation_precondition"] is None
    assert any(issue["code"] == "readiness_recovery_target_not_recoverable" for issue in payload["issues"])


def test_readiness_marks_corrupt_operation_journal_as_recovery_required(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")
    _write_corrupt_operation_journal(mg, "op-corrupt-readiness")

    result = evaluate_readiness(
        mg._repo_path,
        ReadinessRequest.create(command="shepherd.run", requested_freshness="locked", allow_best_effort=False),
        owner=mg,
        force_freshness="locked",
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is False
    assert payload["readiness"]["recovery"]["required"] is True
    assert payload["system_health"]["state"] == "needs_recovery"
    assert any(blocker["kind"] == "operation_journal" for blocker in payload["blockers"])


def test_readiness_marks_corrupt_authority_settlement_as_recovery_required(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")
    path = _authority_settlement_pending_path(mg._repo_path, "op-corrupt-settlement")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json")

    result = evaluate_readiness(
        mg._repo_path,
        ReadinessRequest.create(command="shepherd.run", requested_freshness="locked", allow_best_effort=False),
        owner=mg,
        force_freshness="locked",
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is False
    assert payload["readiness"]["recovery"]["required"] is True
    assert payload["system_health"]["state"] == "needs_recovery"
    assert any(
        item["domain"] == "authority_settlement" and item["health"]["validity"] == "invalid"
        for item in payload["items"]
    )


def test_selected_world_probe_reports_binding_role_mismatch(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")

    items = probe_selected_world(
        mg._repo_path,
        mg.ground.ref,
        required_bindings=(RequiredBinding(binding="workspace", head_kind="filesystem", role="shepherd.Other"),),
    )

    assert any(item.kind == "selected_binding" and item.health.validity == "invalid" for item in items)


def test_readiness_marks_invalid_selected_binding_as_recovery_required(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")

    result = evaluate_readiness(
        mg._repo_path,
        ReadinessRequest.create(
            command="shepherd.run",
            required_bindings=(RequiredBinding(binding="workspace", head_kind="filesystem", role="shepherd.Other"),),
            requested_freshness="locked",
            allow_best_effort=False,
        ),
        owner=mg,
        force_freshness="locked",
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is False
    assert payload["readiness"]["recovery"]["required"] is True
    assert payload["system_health"]["state"] == "needs_recovery"
    assert any(issue["code"] == "world_binding_invalid" for issue in payload["issues"])


def test_readiness_keeps_missing_selected_binding_as_admission_only(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")

    result = evaluate_readiness(
        mg._repo_path,
        ReadinessRequest.create(
            command="shepherd.run",
            required_bindings=(RequiredBinding(binding="trace", head_kind="json", role="shepherd.TraceRef"),),
            requested_freshness="locked",
            allow_best_effort=False,
        ),
        owner=mg,
        force_freshness="locked",
    )
    payload = result.to_json()

    assert payload["readiness"]["allowed"] is False
    assert payload["readiness"]["recovery"]["required"] is False
    assert payload["system_health"]["state"] == "healthy"
    assert any(issue["code"] == "world_binding_missing" for issue in payload["issues"])


def test_authority_ref_probe_uses_world_store_identity_in_item_id(mg: VcsCore) -> None:
    mg.exec("filesystem", "write", scope=mg.ground, path="ready.txt", content=b"ready")

    item = probe_authority_ref(mg._repo_path, mg.ground.ref)

    assert item.health.status == "present_valid"
    assert item.id.startswith("authority_ref:world-store:store_world_main:")


def _write_workspace_authority_pending(mg: VcsCore, operation_id: str) -> None:
    source_commit = mg.store.resolve_to_commit(mg.ground.ref)
    write_pending_workspace_authority(
        mg._repo_path,
        WorkspaceAuthorityPending(
            operation_id=operation_id,
            source_operation_id=f"source-{operation_id}",
            driver_command="scan",
            scope_name=mg.ground.name,
            scope_ref=mg.ground.ref,
            scope_instance_id=mg.ground.instance_id,
            scope_world_id=mg.ground.world_id,
            expected_input_world_oid=mg._current_v2_world_oid(mg._world_storage(), mg.ground.ref),
            scalar_source_commit=str(source_commit.id) if source_commit is not None else None,
        ).with_update(phase="scalar_committed"),
    )


def test_corrupt_index_recovery_not_deadlocked_by_unrelated_workspace_authority_pending(mg: VcsCore) -> None:
    """Review fix (HIGH deadlock): a corrupt open-journal index + an UNRELATED workspace-authority
    pending fact previously blocked each other's recovery — recover_open_operation_journal_index was
    gated through vcscore.recover readiness (blocked by the WA-pending fact) while recover_workspace_
    authority was blocked by the corrupt-index disposition="blocking" fact — leaving no valid first
    step. The open-journal index rebuild is now a PROJECTION-ONLY repair (reads authority, writes a
    derived view), so it is ungated and always available as the first recovery step."""
    manager = open_or_init_default_world_storage(mg._repo_path)
    manager.open_operation_journal(
        operation_id="op-a", operation_kind="shepherd.task", target_ref="refs/vcscore/ground", input_world_oid=None
    )
    repo = manager.world_store.repo
    sig = pygit2.Signature("t", "t@e.invalid")
    corrupt = repo.create_commit(None, sig, sig, "corrupt", repo.TreeBuilder().write(), [])
    repo.references.create(world_open_operation_journal_index_ref(manager.world_store.world_store_id), corrupt, force=True)
    _write_workspace_authority_pending(mg, "op-wa") # the unrelated blocker

    # NOT deadlocked: the projection repair runs despite the unrelated WA-pending fact (before the fix
    # this raised WorkspaceAuthorityRecoveryRequiredError from the recovery-admission gate).
    assert mg.recover_open_operation_journal_index() is True
    assert manager.open_operation_journal_index_corruption() is None # the index is repaired


def _sibling_group(mg: VcsCore, *, group_id: str) -> SiblingGroupRecord:
    parent_oid = mg.store.log(ref=Store.GROUND_REF, max_count=1)[0].oid
    siblings = tuple(_sibling_handle(group_id=group_id, ordinal=ordinal, parent_oid=parent_oid) for ordinal in (0, 1))
    return SiblingGroupRecord(
        group_id=group_id,
        parent_ref=Store.GROUND_REF,
        parent_world_id="ground-world",
        admitted_parent_oid=parent_oid,
        status="admitted",
        siblings=siblings,
        leases=(
            CarrierLeaseRecord(
                lease_id=f"{group_id}-lease-0",
                world_id=siblings[0].world_id,
                substrate="filesystem",
                target_id="workspace",
                mode="writable_carrier",
                resource_key="workspace",
                state="planned",
                carrier_ref=siblings[0].scope_ref,
            ),
        ),
        created_at=1.0,
        updated_at=2.0,
    )


def _sibling_handle(*, group_id: str, ordinal: int, parent_oid: str) -> SiblingHandleRecord:
    machine_scope_name = sibling_machine_scope_name(group_id, ordinal)
    return SiblingHandleRecord(
        world_id=f"{group_id}-world-{ordinal}",
        machine_scope_name=machine_scope_name,
        display_label=f"attempt-{ordinal}",
        scope_ref=f"refs/vcscore/scopes/{machine_scope_name}",
        parent_ref=Store.GROUND_REF,
        creation_oid=parent_oid,
        state="admitted",
        instance_id=f"{group_id}-inst-{ordinal}",
    )


def _write_corrupt_operation_journal(mg: VcsCore, operation_id: str) -> None:
    manager = open_existing_default_world_storage(mg._repo_path)
    # Co-write a VALID open journal first, so the bounded admission index legitimately knows the ref
    # (an open journal IS co-written into the index — the realistic precondition). Then corrupt the
    # commit it points at: payload corruption on a *known* open ref, which bounded admission still
    # probes and blocks on. (A manual open ref that bypassed the co-write would be out-of-model stale
    # drift, invisible to bounded admission until fsck/recovery rebuilds the index.)
    manager.open_operation_journal(
        operation_id=operation_id, operation_kind="shepherd.task", target_ref="refs/vcscore/ground", input_world_oid=None
    )
    repo = manager.world_store.repo
    meta_builder = repo.TreeBuilder()
    insert_tree_entry(
        repo,
        meta_builder,
        OPERATION_JOURNAL_PATH.split("/")[-1],
        repo.create_blob(b"not json"),
        pygit2.GIT_FILEMODE_BLOB,
    )
    root_builder = repo.TreeBuilder()
    insert_tree_entry(repo, root_builder, "meta", meta_builder.write(), pygit2.GIT_FILEMODE_TREE)
    signature = pygit2.Signature("vcs-core operation journal", "vcs-core@example.invalid")
    oid = create_commit_with_recovery(
        repo, None, signature, signature, "manual corrupt journal", root_builder.write(), []
    )
    repo.references.create(operation_journal_ref("open", operation_id), oid, force=True)
