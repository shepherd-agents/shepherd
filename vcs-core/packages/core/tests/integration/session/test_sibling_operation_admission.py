"""Session operation-start admission while sibling groups require recovery."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from vcs_core._capture_reducer import CAPTURE_DIAGNOSTIC_KIND, CAPTURE_REDUCTION_KIND, reduction_operation_id
from vcs_core._errors import OrphanedOperationsError, SiblingGroupRecoveryRequiredError
from vcs_core._fs_capture import FsCaptureEvent
from vcs_core._operation_start_authority import begin_executable_operation
from vcs_core._session_dispatch import SessionCommandDispatcher
from vcs_core._sibling_groups import (
    CarrierLeaseRecord,
    SiblingGroupRecord,
    SiblingHandleRecord,
    sibling_machine_scope_name,
)
from vcs_core.store import Store
from vcs_core.types import ScopeInfo
from vcs_core.vcscore import VcsCore

GROUP_ID = "sg-bbbb00000000"


def _parent_oid(store: Store) -> str:
    return store.log(ref=Store.GROUND_REF, max_count=1)[0].oid


def _sibling(store: Store, *, group_id: str, ordinal: int) -> SiblingHandleRecord:
    machine_scope_name = sibling_machine_scope_name(group_id, ordinal)
    return SiblingHandleRecord(
        world_id=f"{group_id}-world-{ordinal}",
        machine_scope_name=machine_scope_name,
        display_label=f"attempt-{ordinal}",
        scope_ref=f"refs/vcscore/scopes/{machine_scope_name}",
        parent_ref=Store.GROUND_REF,
        creation_oid=_parent_oid(store),
        state="admitted",
        instance_id=f"inst-{ordinal}",
    )


def _group_record(store: Store) -> SiblingGroupRecord:
    siblings = (_sibling(store, group_id=GROUP_ID, ordinal=0), _sibling(store, group_id=GROUP_ID, ordinal=1))
    return SiblingGroupRecord(
        group_id=GROUP_ID,
        parent_ref=Store.GROUND_REF,
        parent_world_id="ground-world",
        admitted_parent_oid=_parent_oid(store),
        status="admitted",
        siblings=siblings,
        leases=(
            CarrierLeaseRecord(
                lease_id=f"{GROUP_ID}-lease-0",
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


def _publish_blocker(mg: VcsCore) -> None:
    assert mg.store._publish_sibling_group_for_recovery_test(_group_record(mg.store), expected_head_oid=None)


def _capture_event(scope: ScopeInfo, path: str = "captured.txt") -> FsCaptureEvent:
    return FsCaptureEvent(
        op="write_close",
        scope=scope.name,
        scope_instance_id=scope.instance_id,
        path=path,
        pid=1234,
        proc_seq=1,
    )


def _dispatcher(mg: VcsCore, scope: ScopeInfo) -> SessionCommandDispatcher:
    daemon = SimpleNamespace(
        _lock=threading.RLock(),
        _mg=mg,
        _current_scope_name=scope.name,
        _daemon_instance_id="daemon-current",
    )
    return SessionCommandDispatcher(daemon)


def _begin_direct_session_exec(mg: VcsCore, scope: ScopeInfo, operation_id: str) -> object:
    return mg.store.begin_operation(
        scope.ref,
        handle_id=operation_id,
        kind="vcs_core.session_exec",
        world_id=scope.world_id or mg._scope_world_id(scope),
        scope_instance_id=scope.instance_id,
        operation_id=operation_id,
        operation_label=operation_id,
        session_id=mg._session_id,
        metadata={"command": {"scope": scope.name}},
    )


def test_session_exec_start_blocks_before_open_operation(mg: VcsCore, workspace: Path) -> None:
    task = mg.fork(mg.ground, "task-session-exec-blocked")
    _publish_blocker(mg)
    dispatcher = _dispatcher(mg, task)

    with pytest.raises(SiblingGroupRecoveryRequiredError, match=GROUP_ID):
        dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["python", "-c", "print('blocked')"],
                "cwd": str(workspace),
                "scope": task.name,
                "capture_requested": False,
                "managed": True,
                "started_at": 1.0,
                "client_pid": 123,
            },
        )

    assert not any(operation.kind == "vcs_core.session_exec" for operation in mg.store.list_open_operations())


def test_shell_capture_lease_start_blocks_before_open_operation(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-shell-lease-blocked")
    _publish_blocker(mg)
    dispatcher = _dispatcher(mg, task)

    with pytest.raises(SiblingGroupRecoveryRequiredError, match=GROUP_ID):
        dispatcher.dispatch(
            "shell_capture_lease_begin",
            {
                "lease_id": "shl_blocked",
                "scope": task.name,
                "capture_requested": True,
                "shell_pid": 456,
                "daemon_instance_id": "daemon-current",
                "started_at": 1.0,
                "client_pid": 456,
            },
        )

    assert not any(operation.kind == "vcs_core.session_shell" for operation in mg.store.list_open_operations())


def test_executable_operation_authority_blocks_same_scope_open_operation(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-direct-authority-blocked")
    existing = _begin_direct_session_exec(mg, task, "same-scope-open")
    try:
        with pytest.raises(OrphanedOperationsError, match="same-scope-open"):
            begin_executable_operation(
                mg,
                task,
                attempted="open same-scope direct operation",
                handle_id="same-scope-second",
                kind="vcs_core.session_exec",
                world_id=task.world_id or mg._scope_world_id(task),
                scope_instance_id=task.instance_id,
                operation_id="same-scope-second",
                operation_label="same-scope-second",
                session_id=mg._session_id,
                metadata={"command": {"scope": task.name}},
            )
    finally:
        if mg.store.ref_exists(existing.ref):
            mg.store.abort_operation(existing, metadata={"cleanup": True})

    assert not any(operation.durable_id == "same-scope-second" for operation in mg.store.list_open_operations())


def test_executable_operation_authority_allows_unrelated_scope_open_operation(mg: VcsCore) -> None:
    target = mg.fork(mg.ground, "task-direct-authority-target")
    unrelated = mg.fork(target, "task-direct-authority-unrelated")
    existing = _begin_direct_session_exec(mg, unrelated, "unrelated-scope-open")
    opened = None
    try:
        opened = begin_executable_operation(
            mg,
            target,
            attempted="open target-scope direct operation",
            handle_id="target-scope-open",
            kind="vcs_core.session_exec",
            world_id=target.world_id or mg._scope_world_id(target),
            scope_instance_id=target.instance_id,
            operation_id="target-scope-open",
            operation_label="target-scope-open",
            session_id=mg._session_id,
            metadata={"command": {"scope": target.name}},
        )

        assert opened.scope_ref == target.ref
    finally:
        for operation in (opened, existing):
            if operation is not None and mg.store.ref_exists(operation.ref):
                mg.store.abort_operation(operation, metadata={"cleanup": True})
        mg.discard(unrelated)
        mg.discard(target)


def test_not_admitted_shell_command_remains_allowlisted_diagnostic(mg: VcsCore, workspace: Path) -> None:
    task = mg.fork(mg.ground, "task-not-admitted-shell")
    _publish_blocker(mg)
    dispatcher = _dispatcher(mg, task)

    result = dispatcher.dispatch(
        "shell_command_not_admitted",
        {
            "cwd": str(workspace),
            "scope": task.name,
            "submitted_text": "echo blocked",
            "shell_pid": 456,
            "daemon_instance_id": "daemon-current",
            "started_at": 1.0,
            "ended_at": 2.0,
            "admission_error": "blocked by sibling group",
        },
    )

    history = mg.resolve_operation_history(str(result["operation_id"]), scope=task)
    assert history.summary.kind == "vcs_core.session_exec"
    assert history.summary.visibility == "archived"
    assert history.summary.status == "error"
    assert not any(operation.durable_id == result["operation_id"] for operation in mg.store.list_open_operations())


def test_capture_diagnostic_remains_allowlisted_under_sibling_blocker(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-capture-diagnostic")
    _publish_blocker(mg)

    result = mg._record_capture_diagnostic(
        "filesystem",
        _capture_event(task),
        command_operation_id="cmd-missing",
        global_seq=1,
        event_seq=1,
        capture_mechanism="preload",
        reason="uncorrelated_capture_event",
    )

    assert result is not None
    history = mg.resolve_operation_history("diag_cmd-missing_1", scope=task)
    assert history.summary.kind == CAPTURE_DIAGNOSTIC_KIND
    assert history.summary.status == "ok"
    assert not any(operation.durable_id == "diag_cmd-missing_1" for operation in mg.store.list_open_operations())


def test_capture_reduction_remains_allowlisted_under_sibling_blocker(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-capture-reduction")
    operation_id = "cmd-reduce"
    operation = mg.store.begin_operation(
        task.ref,
        handle_id=operation_id,
        kind="vcs_core.session_exec",
        world_id=task.world_id or mg._scope_world_id(task),
        scope_instance_id=task.instance_id,
        operation_id=operation_id,
        operation_label="captured command",
        session_id=mg._session_id,
        metadata={"command": {"scope": task.name, "capture_requested": True}},
    )
    assert operation.durable_id == operation_id
    mg._record_capture_event(
        "filesystem",
        _capture_event(task),
        command_operation_id=operation_id,
        global_seq=1,
        event_seq=1,
        capture_mechanism="preload",
    )
    _publish_blocker(mg)

    result = mg._reduce_capture_for_command_operation(operation_id, command_metadata={"status": "success"})

    reducer_id = reduction_operation_id(operation_id)
    assert result is not None
    history = mg.resolve_operation_history(reducer_id, scope=task)
    assert history.summary.kind == CAPTURE_REDUCTION_KIND
    assert history.summary.status == "ok"
    assert not any(operation.durable_id == reducer_id for operation in mg.store.list_open_operations())
