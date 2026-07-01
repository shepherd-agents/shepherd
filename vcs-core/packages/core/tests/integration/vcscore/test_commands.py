"""VcsCore command and schema validation integration tests."""

from __future__ import annotations

import threading
import types
from pathlib import Path

import pytest
from vcs_core._binding_contracts import BindingContractError
from vcs_core._errors import StaleScopeError
from vcs_core._schema_errors import SchemaValidationError
from vcs_core._substrate_runtime import build_builtin_substrate_context
from vcs_core.spi import (
    CapabilitySet,
    CommandRequest,
    CommandSpec,
    DriverContext,
    DriverIngressResult,
    DriverSchema,
    IngressRequest,
    UnsupportedRequestError,
)
from vcs_core.types import BoundSubstrate, EffectRecord, ScopeInfo
from vcs_core.vcscore import VcsCore


def _operation_id(entry) -> object:  # type: ignore[no-untyped-def]
    return entry.metadata["mg"]["operation"]["id"]


class _CommandDriverFixture:
    driver_id = "fixture"
    driver_version = "test"
    commands: dict[str, CommandSpec] = {}

    @property
    def name(self) -> str:
        return self.driver_id

    @property
    def binding(self) -> str:
        return self.driver_id

    @property
    def role(self) -> str:
        return self.driver_id

    @property
    def capabilities(self) -> CapabilitySet:
        return CapabilitySet(accepts=frozenset({CommandRequest}), selectable=False)

    def describe(self) -> DriverSchema:
        return DriverSchema(
            driver_id=self.driver_id,
            driver_version=self.driver_version,
            capabilities=self.capabilities,
            commands=self.commands,
        )

    def prepare(self, context: DriverContext, request: IngressRequest) -> DriverIngressResult:
        del context
        if not isinstance(request, CommandRequest):
            raise UnsupportedRequestError(driver_id=self.driver_id, request_type=type(request))
        return self.run_command(request.command, dict(request.params))

    def run_command(self, command: str, params: dict[str, object]) -> DriverIngressResult:
        del command, params
        return DriverIngressResult()

    def capture_adapters(self, context: DriverContext) -> tuple[object, ...]:
        del context
        return ()

    def validate_result(self, request: IngressRequest, result: DriverIngressResult) -> None:
        del request, result


def test_filter_effects_delegation(workspace: Path) -> None:
    from vcs_core.store import Store
    from vcs_core.substrates import MarkerSubstrate

    store = Store(str(workspace / ".vcscore"))
    marker = MarkerSubstrate(build_builtin_substrate_context(store))
    m = VcsCore(str(workspace), substrates=[marker])
    m.activate()

    task = m.fork(m.ground, "task-fe")
    marker.mark("checkpoint", {"phase": "a"})
    m.merge(task, m.ground)

    results = m.filter_effects(effect_type="Marker")
    assert len(results) >= 1
    assert results[0].metadata["type"] == "Marker"


def test_vcscore_exec_records_effect_for_explicit_scope(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-exec")

    outcome = mg.exec("filesystem", "write", scope=task, path="exec.py", content=b"print('hi')")

    assert len(outcome.oids) == 1
    assert outcome.value is None
    types = [entry.metadata["type"] for entry in mg.log(ref=task.ref, max_count=3)]
    assert types == ["OperationCompleted", "FileCreate", "OperationStarted"]
    mg.merge(task, mg.ground)
    effects = mg.filter_effects(effect_type="FileCreate")
    assert any(e.metadata.get("path") == "exec.py" for e in effects)


def test_vcscore_exec_stamps_session_and_execution_identity_metadata(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-exec-metadata")

    mg.exec("filesystem", "write", scope=task, path="meta.py", content=b"print('meta')")

    entries = mg.log(ref=task.ref, max_count=3)
    started = next(entry for entry in entries if entry.metadata.get("type") == "OperationStarted")
    file_entry = next(entry for entry in entries if entry.metadata.get("type") == "FileCreate")
    completed = next(entry for entry in entries if entry.metadata.get("type") == "OperationCompleted")

    assert started.metadata["mg"]["session_id"] == mg._session_id
    assert str(_operation_id(started)).startswith("op_")
    assert started.metadata["mg"]["operation"]["label"] == "filesystem-write"
    assert file_entry.metadata["mg"]["world"]["id"] == task.world_id
    assert file_entry.metadata["mg"]["world"]["ref"] == task.ref
    assert _operation_id(file_entry) == _operation_id(started)
    assert _operation_id(completed) == _operation_id(started)


def test_vcscore_exec_returns_command_value(workspace: Path) -> None:
    class ValueSubstrate(_CommandDriverFixture):
        driver_id = "value"
        commands = {"inspect": CommandSpec(description="Inspect", params={})}

        def run_command(self, command: str, params: dict[str, object]) -> DriverIngressResult:
            del command, params
            return DriverIngressResult(
                effects=(EffectRecord(effect_type="Marker", metadata={"label": "value"}),),
                value={"answer": 42},
            )

    m = VcsCore(str(workspace), substrates=[ValueSubstrate()])
    m.activate()
    try:
        task = m.fork(m.ground, "task-value")
        outcome = m.exec("value", "inspect", scope=task)

        assert len(outcome.oids) == 1
        assert outcome.value == {"answer": 42}
    finally:
        m.deactivate()


def test_vcscore_exec_rejects_non_driver_substrate_before_recording(workspace: Path) -> None:
    class NonDriverSubstrate:
        name = "non-driver"

        def activate(self) -> None:
            pass

        def deactivate(self) -> None:
            pass

        def authority(self):
            return None

    m = VcsCore(str(workspace), substrates=[NonDriverSubstrate()])  # type: ignore[list-item]
    m.activate()
    try:
        task = m.fork(m.ground, "task-non-driver")

        with pytest.raises(BindingContractError, match="does not implement SubstrateDriver"):
            m.exec("non-driver", "inspect", scope=task)

        assert m.store.filter_effects(substrate="non-driver", ref=task.ref) == []
    finally:
        m.deactivate()


def test_runtime_activity_appends_helper_effects_into_one_operation(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-runtime-activity")
    marker = mg._resolve_binding("marker").instance
    filesystem = mg._resolve_binding("filesystem").instance

    with mg.runtime_activity(scope=task, operation_label="script-run", operation_kind="python.run"):
        marker.mark("checkpoint")  # type: ignore[attr-defined]
        filesystem.record_changes([("activity.py", b"print('activity')")])  # type: ignore[attr-defined]

    entries = mg.log(ref=task.ref, max_count=4)
    started = next(entry for entry in entries if entry.metadata.get("type") == "OperationStarted")
    marker_entry = next(entry for entry in entries if entry.metadata.get("type") == "Marker")
    file_entry = next(entry for entry in entries if entry.metadata.get("type") == "FileCreate")
    completed = next(entry for entry in entries if entry.metadata.get("type") == "OperationCompleted")

    assert started.metadata["mg"]["operation"]["kind"] == "python.run"
    assert started.metadata["mg"]["operation"]["label"] == "script-run"
    assert _operation_id(marker_entry) == _operation_id(started)
    assert _operation_id(file_entry) == _operation_id(started)
    assert _operation_id(completed) == _operation_id(started)


def test_child_workspace_write_requires_nested_operations_profile(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VCS_CORE_NESTED_OPERATIONS", raising=False)
    parent = mg.fork(mg.ground, "child-write-profile-parent")
    child = mg.fork(parent, "child-write-profile-child")

    with (
        mg.runtime_activity(scope=parent, operation_label="parent", operation_kind="test.parent"),
        pytest.raises(RuntimeError, match="VCS_CORE_NESTED_OPERATIONS"),
    ):
        mg.record_child_workspace_write(
            scope=child,
            path="child.txt",
            content=b"child\n",
            operation_id="child-write-profile",
            operation_kind="test.child_write",
        )


def test_child_workspace_write_requires_active_parent_activity(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    parent = mg.fork(mg.ground, "child-write-parent")
    child = mg.fork(parent, "child-write-child")

    with pytest.raises(RuntimeError, match="active parent runtime activity"):
        mg.record_child_workspace_write(
            scope=child,
            path="child.txt",
            content=b"child\n",
            operation_id="child-write-without-parent",
            operation_kind="test.child_write",
        )

    with (
        mg.runtime_activity(scope=parent, operation_label="parent", operation_kind="test.parent"),
        pytest.raises(RuntimeError, match="child scope distinct"),
    ):
        mg.record_child_workspace_write(
            scope=parent,
            path="same-scope.txt",
            content=b"same\n",
            operation_id="child-write-same-scope",
            operation_kind="test.child_write",
        )


def test_child_workspace_write_records_under_active_parent_activity(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    parent = mg.fork(mg.ground, "child-write-ok-parent")
    child = mg.fork(parent, "child-write-ok-child")

    with mg.runtime_activity(scope=parent, operation_label="parent", operation_kind="test.parent"):
        outcome = mg.record_child_workspace_write(
            scope=child,
            path="child.txt",
            content=b"child\n",
            operation_id="child-write-ok",
            operation_kind="test.child_write",
        )

    assert len(outcome.oids) == 1


def test_child_workspace_write_rejects_non_descendant_scope(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    parent = mg.fork(mg.ground, "child-write-nondescendant-parent")
    child = mg.fork(parent, "child-write-nondescendant-child")

    with (
        mg.runtime_activity(scope=child, operation_label="child", operation_kind="test.child"),
        pytest.raises(RuntimeError, match="descended from the active parent"),
    ):
        mg.record_child_workspace_write(
            scope=parent,
            path="parent.txt",
            content=b"parent\n",
            operation_id="child-write-nondescendant",
            operation_kind="test.child_write",
        )


def test_runtime_activity_allows_threaded_patch_recording_inside_parent_operation(workspace: Path) -> None:
    from vcs_core.store import Store
    from vcs_core.substrates import DeclarativeFilesystemSubstrate

    store = Store(str(workspace / ".vcscore"))
    filesystem = DeclarativeFilesystemSubstrate(build_builtin_substrate_context(store))
    m = VcsCore(str(workspace), substrates=[filesystem])
    m.activate()
    try:
        task = m.fork(m.ground, "task-threaded-runtime")
        thread_error: list[BaseException] = []

        def worker() -> None:
            try:
                (workspace / "threaded.txt").write_text("threaded")
            except BaseException as exc:
                thread_error.append(exc)

        with m.runtime_activity(scope=task, operation_label="threaded-run", operation_kind="python.run"):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=1.0)
            assert not thread.is_alive()

        assert thread_error == []

        entries = m.log(ref=task.ref, max_count=4)
        assert [entry.metadata["type"] for entry in entries] == [
            "OperationCompleted",
            "FileCreate",
            "OperationStarted",
            "Init",
        ]
        started = entries[2]
        file_entry = entries[1]
        completed = entries[0]
        assert started.metadata["mg"]["operation"]["label"] == "threaded-run"
        assert file_entry.metadata["path"] == "threaded.txt"
        assert _operation_id(file_entry) == _operation_id(started)
        assert _operation_id(completed) == _operation_id(started)
    finally:
        m.deactivate()


def test_runtime_activity_rejects_complete_error_inside_parent_operation(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-runtime-complete-error")

    with mg._lock, mg._scoped(task):
        parent = mg._pipeline.begin_operation(handle_id="parent", kind="git.commit", scope=task)

    with (
        pytest.raises(RuntimeError, match="requires a root runtime activity"),
        mg.runtime_activity(
            scope=task,
            operation_label="nested-run",
            operation_kind="python.run",
            failure_policy="complete_error",
        ),
    ):
        pass

    assert mg._pipeline.current_operation() is not None
    assert mg._pipeline.current_operation().ref == parent.ref

    with mg._lock, mg._scoped(task):
        mg._pipeline.abort_operation(handle_id="parent")


def test_vcscore_exec_routes_by_binding_name_but_records_substrate_type(workspace: Path) -> None:
    class ValueSubstrate(_CommandDriverFixture):
        driver_id = "value"
        commands = {"inspect": CommandSpec(description="Inspect", params={})}

        def run_command(self, command: str, params: dict[str, object]) -> DriverIngressResult:
            del command, params
            return DriverIngressResult(
                effects=(EffectRecord(effect_type="Marker", metadata={"label": "binding-routed"}),),
                value={"answer": 7},
            )

    m = VcsCore(
        str(workspace),
        bindings=[
            BoundSubstrate(
                binding_name="analytics",
                substrate_type="value",
                instance=ValueSubstrate(),
            )
        ],
    )
    m.activate()
    try:
        task = m.fork(m.ground, "task-alias")
        outcome = m.exec("analytics", "inspect", scope=task)

        assert len(outcome.oids) == 1
        assert outcome.value == {"answer": 7}

        m.merge(task, m.ground)
        effects = m.filter_effects(effect_type="Marker", substrate="value")
        assert any(e.metadata.get("label") == "binding-routed" for e in effects)
    finally:
        m.deactivate()


def test_vcscore_exec_records_marker_effect_for_explicit_scope(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-record")

    outcome = mg.exec(
        "marker",
        "mark",
        scope=task,
        label="recorded-directly",
    )

    assert len(outcome.oids) == 1
    types = [entry.metadata["type"] for entry in mg.log(ref=task.ref, max_count=3)]
    assert types == ["OperationCompleted", "Marker", "OperationStarted"]
    mg.merge(task, mg.ground)
    effects = mg.filter_effects(effect_type="Marker")
    assert any(e.metadata.get("label") == "recorded-directly" for e in effects)


def test_vcscore_exec_failure_archives_operation_without_advancing_scope(workspace: Path) -> None:
    class FailingSubstrate(_CommandDriverFixture):
        driver_id = "failing"
        commands = {"explode": CommandSpec(description="Explode", params={})}

        def run_command(self, command: str, params: dict[str, object]) -> DriverIngressResult:
            del command, params
            raise RuntimeError("boom")

    m = VcsCore(str(workspace), substrates=[FailingSubstrate()])
    m.activate()
    try:
        task = m.fork(m.ground, "task-fail-exec")
        original_tip = m.log(ref=task.ref, max_count=1)[0].oid

        with pytest.raises(RuntimeError, match="boom"):
            m.exec("failing", "explode", scope=task)

        assert m.log(ref=task.ref, max_count=1)[0].oid == original_tip
        archive_refs = m.store.list_operation_archive_refs(world_id=task.world_id)
        assert len(archive_refs) == 1
        assert [entry.metadata["type"] for entry in m.log(ref=archive_refs[0], max_count=2)] == [
            "OperationAborted",
            "OperationStarted",
        ]
        assert m.store.list_open_operations(scope_ref=task.ref) == []
    finally:
        m.deactivate()


def test_vcscore_child_operation_failure_archives_without_advancing_parent(workspace: Path) -> None:
    class FailingSubstrate(_CommandDriverFixture):
        driver_id = "failing"
        commands = {"explode": CommandSpec(description="Explode", params={})}

        def run_command(self, command: str, params: dict[str, object]) -> DriverIngressResult:
            del command, params
            raise RuntimeError("hook boom")

    m = VcsCore(str(workspace), substrates=[FailingSubstrate()])
    m.activate()
    try:
        task = m.fork(m.ground, "task-fail-child")
        with m._lock, m._scoped(task):
            parent = m._pipeline.begin_operation(handle_id="parent", kind="git.commit", scope=task)
            m._pipeline.record_one(
                EffectRecord(
                    effect_type="Marker",
                    metadata={"label": "parent"},
                ),
                substrate="marker",
                scope=task,
            )
            parent_tip_before = m.log(ref=parent.ref, max_count=1)[0].oid

        with pytest.raises(RuntimeError, match="hook boom"):
            m._execute_recorded_in_child_operation(
                "failing",
                "explode",
                scope=task,
                operation_id="hook-op",
                operation_kind="git.hook",
                operation_metadata={"hook_kind": "path_wrapper"},
            )

        assert m._pipeline.current_operation() is not None
        assert m._pipeline.current_operation().ref == parent.ref
        assert m.log(ref=parent.ref, max_count=1)[0].oid == parent_tip_before
        archive_refs = [ref for ref in m.store.list_archive_refs() if ref == "refs/vcscore/archive/ops/hook-op"]
        assert len(archive_refs) == 1
        assert [entry.metadata["type"] for entry in m.log(ref=archive_refs[0], max_count=2)] == [
            "OperationAborted",
            "OperationStarted",
        ]
        with m._lock, m._scoped(task):
            m._pipeline.abort_operation(handle_id="parent")
    finally:
        m.deactivate()


def test_vcscore_exec_reentrant_patch_callback_does_not_deadlock(workspace: Path) -> None:
    from vcs_core.store import Store
    from vcs_core.substrates import FilesystemSubstrate

    class WritingSubstrate(_CommandDriverFixture):
        driver_id = "writer"
        commands = {"touch": CommandSpec(description="Touch", params={})}

        def __init__(self, workspace: Path) -> None:
            self._workspace = workspace

        def run_command(self, command: str, params: dict[str, object]) -> DriverIngressResult:
            del command, params
            (self._workspace / "captured.txt").write_text("captured")
            return DriverIngressResult()

    store = Store(str(workspace / ".vcscore"))
    filesystem = FilesystemSubstrate(build_builtin_substrate_context(store))
    m = VcsCore(str(workspace), substrates=[filesystem, WritingSubstrate(workspace)], store=store)
    m.activate()
    try:
        task = m.fork(m.ground, "task-reentrant")
        original = m._patch_manager.record_performed_event
        probes: list[bool] = []

        def wrapped_record_performed_event(self, substrate, event, params, *, scope=None, boundary_policy=None):
            acquired = m._lock.acquire(blocking=False)
            probes.append(acquired)
            if acquired:
                m._lock.release()
            return original(substrate, event, params, scope=scope, boundary_policy=boundary_policy)

        m._patch_manager.record_performed_event = types.MethodType(  # type: ignore[assignment]
            wrapped_record_performed_event,
            m._patch_manager,
        )

        outcome = m.exec("writer", "touch", scope=task)

        assert outcome.oids == ()
        assert probes == [True]
        effect_types = [entry.metadata["type"] for entry in m.log(ref=task.ref, max_count=5)]
        assert "FileCreate" in effect_types
    finally:
        m.deactivate()


def test_vcscore_exec_rejects_stale_scope(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-stale-exec")
    mg.merge(task, mg.ground)

    with pytest.raises(StaleScopeError, match="not a live scope"):
        mg.exec("filesystem", "write", scope=task, path="stale.py", content=b"x")


def test_vcscore_exec_rejects_foreign_or_stale_scope_handle(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-stale-record")
    stale = ScopeInfo(
        name=task.name,
        ref=task.ref,
        instance_id="foreign-instance",
        creation_oid=task.creation_oid,
    )

    with pytest.raises(StaleScopeError, match="stale or belongs to another session"):
        mg.exec("marker", "mark", scope=stale, label="bad")


def test_vcscore_exec_validates_binding_name(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-bad-substrate")

    with pytest.raises(ValueError, match="Unknown binding"):
        mg.exec("unknown", "mark", scope=task, label="x")


def test_vcscore_exec_rejects_unknown_command(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-bad-command")

    with pytest.raises(ValueError, match="Unknown filesystem command"):
        mg.exec("filesystem", "rename", scope=task, path="old.py", new_path="new.py")


def test_vcscore_exec_rejects_missing_required_params(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-missing-param")

    with pytest.raises(SchemaValidationError, match="missing required parameter 'path'"):
        mg.exec("filesystem", "read", scope=task)


def test_vcscore_exec_rejects_unknown_params(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-unknown-param")

    with pytest.raises(SchemaValidationError, match="unknown parameter"):
        mg.exec("filesystem", "read", scope=task, path="known.py", extra=True)


def test_vcscore_exec_rejects_wrong_param_type(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-bad-type")

    with pytest.raises(SchemaValidationError, match="expected bytes"):
        mg.exec("filesystem", "write", scope=task, path="bad.py", content=123)
