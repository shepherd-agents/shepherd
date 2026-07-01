"""VcsCore pipeline and compatibility integration tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from vcs_core.commons_recording import CommonsShadowUnsupportedError
from vcs_core.types import EffectRecord, ScopeInfo
from vcs_core.vcscore import VcsCore

if TYPE_CHECKING:
    from pathlib import Path


def test_fork_cleanup_discards_branched_substrates(workspace: Path) -> None:
    class GoodSubstrate:
        name = "good"
        commands = {}
        effects = {}

        def __init__(self) -> None:
            self.branched: list[str] = []
            self.discarded: list[str] = []

        def bind_pipeline(self, pipeline, *, scope_queries=None) -> None:
            del pipeline, scope_queries

        def activate(self) -> None:
            pass

        def deactivate(self) -> None:
            pass

        def push(self, scope_id: str | None = None) -> None:
            pass

        def authority(self):
            return None

        def python_patches(self) -> tuple[object, ...]:
            return ()

        def branch(self, scope_id: str, *, parent_scope: ScopeInfo, hints: dict | None = None) -> None:
            del parent_scope, hints
            self.branched.append(scope_id)

        def prepare_merge(self, scope: ScopeInfo, parent: ScopeInfo):
            del scope, parent
            return []

        def commit_merge(self, scope_id: str, *, parent_scope: ScopeInfo) -> None:
            del scope_id, parent_scope

        def discard(self, scope_id: str) -> None:
            self.discarded.append(scope_id)

    class FailingSubstrate:
        name = "failing"
        commands = {}
        effects = {}

        def bind_pipeline(self, pipeline, *, scope_queries=None) -> None:
            del pipeline, scope_queries

        def activate(self) -> None:
            pass

        def deactivate(self) -> None:
            pass

        def push(self, scope_id: str | None = None) -> None:
            pass

        def authority(self):
            return None

        def python_patches(self) -> tuple[object, ...]:
            return ()

        def branch(self, scope_id: str, *, parent_scope: ScopeInfo, hints: dict | None = None) -> None:
            del scope_id, parent_scope, hints
            raise RuntimeError("substrate failure")

        def prepare_merge(self, scope: ScopeInfo, parent: ScopeInfo):
            del scope, parent
            return []

        def commit_merge(self, scope_id: str, *, parent_scope: ScopeInfo) -> None:
            del scope_id, parent_scope

        def discard(self, scope_id: str) -> None:
            pass

    good = GoodSubstrate()
    failing = FailingSubstrate()
    m = VcsCore(str(workspace), substrates=[good, failing])  # type: ignore[list-item]
    m.activate()

    with pytest.raises(RuntimeError, match="substrate failure"):
        m.fork(m.ground, "task-fail")

    assert "task-fail" in good.branched
    assert "task-fail" in good.discarded


def test_recording_pipeline_records_single_effect(workspace: Path) -> None:
    from vcs_core.recording import RecordingPipeline
    from vcs_core.store import Store

    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    pipeline = RecordingPipeline(store)

    task = store.fork(Store.GROUND_REF, "task-pipe")
    pipeline.set_scope(task)

    effect = EffectRecord(effect_type="TestEffect", metadata={"key": "val"})
    oid = pipeline.record_one(effect, substrate="test")
    assert oid

    log = store.log(ref=task.ref)
    assert any(e.metadata.get("type") == "TestEffect" for e in log)


def _operation_id(entry) -> object:  # type: ignore[no-untyped-def]
    mg = entry.metadata.get("mg")
    if not isinstance(mg, dict):
        return None
    operation = mg.get("operation")
    if not isinstance(operation, dict):
        return None
    return operation.get("id")


def test_recording_pipeline_stamps_scope_instance_id(workspace: Path) -> None:
    from vcs_core.recording import RecordingPipeline
    from vcs_core.store import Store

    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    pipeline = RecordingPipeline(store)

    task = store.fork(Store.GROUND_REF, "task-stamped")
    pipeline.set_scope(task)

    pipeline.record_one(EffectRecord(effect_type="Stamped", metadata={"key": "val"}), substrate="test")

    log = store.log(ref=task.ref, max_count=1)
    assert log[0].metadata["world_id"] == task.world_id
    assert log[0].metadata["scope_instance_id"] == task.instance_id


def test_recording_pipeline_records_multiple_effects(workspace: Path) -> None:
    from vcs_core.recording import RecordingPipeline
    from vcs_core.store import Store

    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    pipeline = RecordingPipeline(store)

    task = store.fork(Store.GROUND_REF, "task-multi")
    pipeline.set_scope(task)

    effects = [
        EffectRecord(effect_type="A", metadata={"i": 1}),
        EffectRecord(effect_type="B", metadata={"i": 2}),
    ]
    oids = pipeline.record(effects, substrate="test")
    assert len(oids) == 2


def test_recording_pipeline_explicit_scope_overrides_ambient_scope(workspace: Path) -> None:
    from vcs_core.recording import RecordingPipeline
    from vcs_core.store import Store

    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    pipeline = RecordingPipeline(store)

    first = store.fork(Store.GROUND_REF, "task-first")
    second = store.fork(Store.GROUND_REF, "task-second")
    pipeline.set_scope(first)

    pipeline.record_one(
        EffectRecord(effect_type="ExplicitScope", metadata={"target": "second"}),
        substrate="test",
        scope=second,
    )

    first_log = store.log(ref=first.ref)
    second_log = store.log(ref=second.ref)
    assert not any(e.metadata.get("type") == "ExplicitScope" for e in first_log)
    assert any(e.metadata.get("type") == "ExplicitScope" for e in second_log)


def test_recording_pipeline_explicit_operation_scope_refreshes_execution_context(workspace: Path) -> None:
    from vcs_core.recording import RecordingPipeline
    from vcs_core.store import Store

    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    pipeline = RecordingPipeline(store)

    first = store.fork(Store.GROUND_REF, "task-op-context-first")
    second = store.fork(Store.GROUND_REF, "task-op-context-second")
    pipeline.set_scope(first)

    op = pipeline.begin_operation(handle_id="op-second", kind="test.operation", scope=second)

    assert pipeline.context.world == second
    assert pipeline.execution_context is not None
    assert pipeline.execution_context.scope_ref == second.ref
    assert pipeline.execution_context.scope_instance_id == second.instance_id
    assert pipeline.current_operation() == op
    assert op.scope_ref == second.ref

    pipeline.abort_operation(handle_id=op.handle_id)


def test_recording_pipeline_no_scope_raises(workspace: Path) -> None:
    from vcs_core.recording import RecordingPipeline
    from vcs_core.store import Store

    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    pipeline = RecordingPipeline(store)

    effect = EffectRecord(effect_type="Test", metadata={})
    with pytest.raises(RuntimeError, match="No execution context"):
        pipeline.record_one(effect, substrate="test")


def test_recording_pipeline_with_workspace_changes(workspace: Path) -> None:
    from vcs_core.recording import RecordingPipeline
    from vcs_core.store import Store

    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    pipeline = RecordingPipeline(store)

    task = store.fork(Store.GROUND_REF, "task-ws")
    pipeline.set_scope(task)

    effect = EffectRecord(
        effect_type="FileCreate",
        metadata={"path": "hello.py"},
        workspace_changes=(("hello.py", b"content"),),
    )
    pipeline.record_one(effect, substrate="filesystem")

    assert store.file_exists_in_workspace(task.ref, "hello.py")


def test_recording_pipeline_context_world_tracking(workspace: Path) -> None:
    from vcs_core.recording import RecordingPipeline
    from vcs_core.store import Store

    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    pipeline = RecordingPipeline(store)

    assert pipeline.context.world is None
    assert pipeline.context.span is None

    task = store.fork(Store.GROUND_REF, "task-track")
    pipeline.set_scope(task)
    assert pipeline.context.world is not None
    assert pipeline.context.world.name == "task-track"
    assert pipeline.context.world == task
    assert pipeline.context.span is None

    pipeline.set_scope(None)
    assert pipeline.context.world is None


def test_recording_pipeline_runtime_effect_hook(workspace: Path) -> None:
    from vcs_core.recording import RecordingPipeline
    from vcs_core.store import Store

    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    pipeline = RecordingPipeline(store)
    task = store.fork(Store.GROUND_REF, "task-runtime-hook")
    calls: list[tuple[str, ScopeInfo | None, tuple[str, ...]]] = []

    def recorder(effects, *, substrate: str, scope: ScopeInfo | None = None, **kwargs):
        assert kwargs["boundary_policy"] == "append_or_root"
        calls.append((substrate, scope, tuple(effect.effect_type for effect in effects)))
        return ["hooked-oid"]

    pipeline.set_runtime_effect_recorder(recorder)

    result = pipeline.record_runtime_effects(
        [EffectRecord(effect_type="Hooked", metadata={"k": "v"})],
        substrate="test",
        scope=task,
    )

    assert result == ["hooked-oid"]
    assert calls == [("test", task, ("Hooked",))]


def test_recording_pipeline_root_operation_routes_effects_to_operation_ref_until_finalize(workspace: Path) -> None:
    from vcs_core.recording import RecordingPipeline
    from vcs_core.store import Store

    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    pipeline = RecordingPipeline(store)

    task = store.fork(Store.GROUND_REF, "task-root-op")
    pipeline.set_scope(task)
    op = pipeline.begin_operation(handle_id="op-root", kind="git.commit", session_id="sess-root")

    assert pipeline.current_operation() == op
    assert pipeline.current_write_ref() == op.ref

    pipeline.record_one(
        EffectRecord(
            effect_type="FileCreate",
            metadata={"path": "inside-op.txt"},
            workspace_changes=(("inside-op.txt", b"inside op"),),
        ),
        substrate="filesystem",
    )

    assert store.read_workspace_file(task.ref, "inside-op.txt") is None
    assert store.read_workspace_file(op.ref, "inside-op.txt") == b"inside op"

    with pytest.raises(RuntimeError, match="Active operation handle is 'op-root', not 'wrong-handle'"):
        pipeline.end_operation(handle_id="wrong-handle")

    tip_oid = pipeline.end_operation(handle_id="op-root")

    assert pipeline.current_operation() is None
    assert pipeline.current_write_ref() == task.ref
    assert store.read_workspace_file(task.ref, "inside-op.txt") == b"inside op"
    assert store.log(ref=task.ref, max_count=1)[0].oid == tip_oid


def test_recording_pipeline_explicit_scope_bypasses_unrelated_active_operation(workspace: Path) -> None:
    from vcs_core.recording import RecordingPipeline
    from vcs_core.store import Store

    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    pipeline = RecordingPipeline(store)

    first = store.fork(Store.GROUND_REF, "task-op-first")
    second = store.fork(Store.GROUND_REF, "task-op-second")
    pipeline.set_scope(first)
    pipeline.begin_operation(handle_id="op-first", kind="git.commit")

    pipeline.record_one(
        EffectRecord(effect_type="ExplicitScope", metadata={"target": "second"}),
        substrate="test",
        scope=second,
    )

    assert store.log(ref=second.ref, max_count=1)[0].metadata["type"] == "ExplicitScope"
    assert all(
        entry.metadata["type"] != "ExplicitScope" for entry in store.log(ref=pipeline.current_write_ref(), max_count=10)
    )


def test_recording_pipeline_nested_operation_stack_merges_child_into_parent(workspace: Path) -> None:
    from vcs_core.recording import RecordingPipeline
    from vcs_core.store import Store

    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    pipeline = RecordingPipeline(store)

    task = store.fork(Store.GROUND_REF, "task-pipe-nested")
    pipeline.set_scope(task)

    parent = pipeline.begin_operation(handle_id="parent", kind="git.commit")
    pipeline.record_one(
        EffectRecord(
            effect_type="FileCreate",
            metadata={"path": "parent.txt"},
            workspace_changes=(("parent.txt", b"parent"),),
        ),
        substrate="filesystem",
    )

    child = pipeline.begin_operation(handle_id="child", kind="git.hook")
    assert child.parent_op_ref == parent.ref
    assert store.read_workspace_file(child.ref, "parent.txt") == b"parent"

    pipeline.record_one(
        EffectRecord(
            effect_type="FileCreate",
            metadata={"path": "child.txt"},
            workspace_changes=(("child.txt", b"child"),),
        ),
        substrate="filesystem",
    )

    pipeline.end_operation(handle_id="child")
    assert pipeline.current_operation() is not None
    assert pipeline.current_operation().ref == parent.ref
    assert pipeline.current_write_ref() == parent.ref
    assert store.read_workspace_file(parent.ref, "child.txt") == b"child"
    open_operations = store.list_open_operations(scope_ref=task.ref)
    assert [op.handle_id for op in open_operations] == ["parent"]

    pipeline.end_operation(handle_id="parent")
    assert pipeline.current_operation() is None
    assert store.read_workspace_file(task.ref, "parent.txt") == b"parent"
    assert store.read_workspace_file(task.ref, "child.txt") == b"child"


def test_recording_pipeline_child_finalize_restores_parent_without_repo_scan(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vcs_core.recording import RecordingPipeline
    from vcs_core.store import Store

    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    pipeline = RecordingPipeline(store)

    task = store.fork(Store.GROUND_REF, "task-pipe-stack-finalize")
    pipeline.set_scope(task)

    parent = pipeline.begin_operation(handle_id="parent", kind="git.commit")
    child = pipeline.begin_operation(handle_id="child", kind="git.hook")

    def _unexpected_repo_scan(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("child finalize should restore parent from runtime stack, not repository scans")

    monkeypatch.setattr(store, "list_open_operations", _unexpected_repo_scan)

    pipeline.end_operation(handle_id=child.handle_id)

    assert pipeline.current_operation() is not None
    assert pipeline.current_operation().ref == parent.ref


def test_recording_pipeline_scope_switch_requires_reset_when_operation_open(workspace: Path) -> None:
    from vcs_core.recording import RecordingPipeline
    from vcs_core.store import Store

    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    pipeline = RecordingPipeline(store)

    first = store.fork(Store.GROUND_REF, "task-first")
    second = store.fork(Store.GROUND_REF, "task-second")
    pipeline.set_scope(first)
    pipeline.begin_operation(handle_id="op-open", kind="git.commit")

    with pytest.raises(RuntimeError, match="operation span is open"):
        pipeline.set_scope(second)
    with pytest.raises(RuntimeError, match="operation span is open"):
        pipeline.set_scope(None)

    pipeline.reset()
    pipeline.set_scope(second)
    assert pipeline.context.world == second


def test_vcscore_fork_rejects_with_open_operation(mg: VcsCore) -> None:
    with mg._lock:
        mg._pipeline.set_scope(mg.ground)
        mg._pipeline.begin_operation(handle_id="open-ground", kind="test.operation", scope=mg.ground)

    with pytest.raises(RuntimeError, match="Cannot fork while operation"):
        mg.fork(mg.ground, "blocked-child")

    assert mg.lookup_scope("blocked-child") is None
    assert not mg.store.ref_exists("refs/vcscore/scopes/blocked-child")

    with mg._lock:
        mg._pipeline.abort_operation(handle_id="open-ground")
        mg._pipeline.set_scope(None)


def test_vcscore_merge_rejects_with_open_operation(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-open-merge")
    with mg._lock, mg._scoped(task):
        mg._pipeline.begin_operation(handle_id="open-merge", kind="test.operation", scope=task)

    before_tip = mg.log(ref=task.ref, max_count=1)[0].oid
    with pytest.raises(RuntimeError, match="Cannot merge while operation"):
        mg.merge(task, mg.ground)

    assert mg.store.ref_exists(task.ref)
    assert mg.log(ref=task.ref, max_count=1)[0].oid == before_tip

    with mg._lock, mg._scoped(task):
        mg._pipeline.abort_operation(handle_id="open-merge")
    mg.discard(task)


def test_vcscore_discard_rejects_with_open_operation(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-open-discard")
    with mg._lock, mg._scoped(task):
        mg._pipeline.begin_operation(handle_id="open-discard", kind="test.operation", scope=task)

    before_tip = mg.log(ref=task.ref, max_count=1)[0].oid
    with pytest.raises(RuntimeError, match="Cannot discard while operation"):
        mg.discard(task)

    assert mg.store.ref_exists(task.ref)
    assert mg.log(ref=task.ref, max_count=1)[0].oid == before_tip

    with mg._lock, mg._scoped(task):
        mg._pipeline.abort_operation(handle_id="open-discard")
    mg.discard(task)


def test_vcscore_push_rejects_with_open_operation(mg: VcsCore) -> None:
    with mg._lock:
        mg._pipeline.set_scope(mg.ground)
        mg._pipeline.begin_operation(handle_id="open-push", kind="test.operation", scope=mg.ground)

    before_tip = mg.log(ref=mg.ground.ref, max_count=1)[0].oid
    with pytest.raises(RuntimeError, match="Cannot push while operation"):
        mg.push()

    assert mg.log(ref=mg.ground.ref, max_count=1)[0].oid == before_tip

    with mg._lock:
        mg._pipeline.abort_operation(handle_id="open-push")
        mg._pipeline.set_scope(None)


def test_runtime_activity_refuses_operation_when_commons_shadow_enabled(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCS_CORE_COMMONS_SHADOW", "1")
    mg = VcsCore(str(workspace))
    mg.activate()
    task = mg.fork(mg.ground, "task-shadow-runtime-boundary")
    operation_id = "op-shadow-runtime"

    with (
        pytest.raises(CommonsShadowUnsupportedError, match="operation spans"),
        mg.runtime_activity(
            scope=task,
            operation_label="shadow-runtime",
            operation_kind="test.operation",
            operation_id=operation_id,
        ),
    ):
        pass

    assert mg.store.operation_ref(operation_id) not in mg.store._repo.references


def test_recording_pipeline_abort_root_operation_archives_without_advancing_scope(workspace: Path) -> None:
    from vcs_core.recording import RecordingPipeline
    from vcs_core.store import Store

    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    pipeline = RecordingPipeline(store)

    task = store.fork(Store.GROUND_REF, "task-abort-root")
    pipeline.set_scope(task)
    original_tip = store.log(ref=task.ref, max_count=1)[0].oid
    op = pipeline.begin_operation(handle_id="op-abort-root", kind="git.commit")

    pipeline.record_one(
        EffectRecord(
            effect_type="FileCreate",
            metadata={"path": "aborted.txt"},
            workspace_changes=(("aborted.txt", b"nope"),),
        ),
        substrate="filesystem",
    )

    archive_ref = pipeline.abort_operation(handle_id=op.handle_id, metadata={"reason": "command failed"})

    assert pipeline.current_operation() is None
    assert pipeline.current_write_ref() == task.ref
    assert archive_ref in store._repo.references
    assert store.log(ref=task.ref, max_count=1)[0].oid == original_tip
    assert store.read_workspace_file(task.ref, "aborted.txt") is None
    assert [entry.metadata["type"] for entry in store.log(ref=archive_ref, max_count=3)] == [
        "OperationAborted",
        "FileCreate",
        "OperationStarted",
    ]


def test_recording_pipeline_abort_child_operation_restores_parent_without_advancing_parent(workspace: Path) -> None:
    from vcs_core.recording import RecordingPipeline
    from vcs_core.store import Store

    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    pipeline = RecordingPipeline(store)

    task = store.fork(Store.GROUND_REF, "task-abort-child")
    pipeline.set_scope(task)
    parent = pipeline.begin_operation(handle_id="parent", kind="git.commit")
    pipeline.record_one(
        EffectRecord(
            effect_type="FileCreate",
            metadata={"path": "parent.txt"},
            workspace_changes=(("parent.txt", b"parent"),),
        ),
        substrate="filesystem",
    )
    parent_tip_before_child = store.log(ref=parent.ref, max_count=1)[0].oid

    child = pipeline.begin_operation(handle_id="child", kind="git.hook")
    pipeline.record_one(
        EffectRecord(
            effect_type="FileCreate",
            metadata={"path": "child.txt"},
            workspace_changes=(("child.txt", b"child"),),
        ),
        substrate="filesystem",
    )

    archive_ref = pipeline.abort_operation(handle_id=child.handle_id, metadata={"reason": "hook failed"})

    assert pipeline.current_operation() is not None
    assert pipeline.current_operation().ref == parent.ref
    assert pipeline.current_write_ref() == parent.ref
    assert archive_ref in store._repo.references
    assert store.log(ref=parent.ref, max_count=1)[0].oid == parent_tip_before_child
    assert store.read_workspace_file(parent.ref, "child.txt") is None
    assert store.read_workspace_file(parent.ref, "parent.txt") == b"parent"
    assert [entry.metadata["type"] for entry in store.log(ref=archive_ref, max_count=3)] == [
        "OperationAborted",
        "FileCreate",
        "OperationStarted",
    ]


def test_recording_pipeline_child_abort_restores_parent_without_repo_scan(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vcs_core.recording import RecordingPipeline
    from vcs_core.store import Store

    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    pipeline = RecordingPipeline(store)

    task = store.fork(Store.GROUND_REF, "task-pipe-stack-abort")
    pipeline.set_scope(task)

    parent = pipeline.begin_operation(handle_id="parent", kind="git.commit")
    child = pipeline.begin_operation(handle_id="child", kind="git.hook")

    def _unexpected_repo_scan(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("child abort should restore parent from runtime stack, not repository scans")

    monkeypatch.setattr(store, "list_open_operations", _unexpected_repo_scan)

    pipeline.abort_operation(handle_id=child.handle_id)

    assert pipeline.current_operation() is not None
    assert pipeline.current_operation().ref == parent.ref


def test_recording_pipeline_stamps_pointer_linked_operation_metadata(workspace: Path) -> None:
    from vcs_core.recording import RecordingPipeline
    from vcs_core.store import Store

    store = Store(str(workspace / ".vcscore"))
    store.create_root_commit()
    pipeline = RecordingPipeline(store)

    task = store.fork(Store.GROUND_REF, "task-pipe-pointer-metadata")
    pipeline.set_scope(task)

    pipeline.begin_operation(
        handle_id="parent",
        kind="git.commit",
        operation_id="parent-op",
        operation_label="parent",
    )
    pipeline.record_one(
        EffectRecord(effect_type="Marker", metadata={"label": "parent-1"}),
        substrate="marker",
    )

    pipeline.begin_operation(
        handle_id="child",
        kind="git.hook",
        operation_id="child-op",
        operation_label="child",
    )
    pipeline.record_one(
        EffectRecord(effect_type="Marker", metadata={"label": "child-1"}),
        substrate="marker",
    )
    pipeline.end_operation(handle_id="child")

    pipeline.record_one(
        EffectRecord(effect_type="Marker", metadata={"label": "parent-2"}),
        substrate="marker",
    )
    pipeline.end_operation(handle_id="parent")

    entries = store.log(ref=task.ref, max_count=20)
    by_label = {
        (entry.metadata.get("type"), entry.metadata.get("label"), _operation_id(entry)): entry for entry in entries
    }
    by_type_and_operation = {(entry.metadata.get("type"), _operation_id(entry)): entry for entry in entries}

    parent_started = by_type_and_operation[("OperationStarted", "parent-op")]
    parent_effect_1 = by_label[("Marker", "parent-1", "parent-op")]
    parent_effect_2 = by_label[("Marker", "parent-2", "parent-op")]
    parent_completed = by_type_and_operation[("OperationCompleted", "parent-op")]
    child_started = by_type_and_operation[("OperationStarted", "child-op")]
    child_effect = by_label[("Marker", "child-1", "child-op")]
    child_completed = by_type_and_operation[("OperationCompleted", "child-op")]

    parent_start_mg = parent_started.metadata["mg"]
    parent_effect_2_mg = parent_effect_2.metadata["mg"]
    child_start_mg = child_started.metadata["mg"]
    child_completed_mg = child_completed.metadata["mg"]

    assert parent_start_mg["operation"]["id"] == "parent-op"
    assert parent_start_mg["operation"]["phase"] == "started"
    assert parent_start_mg["operation"]["seq"] == 0
    assert parent_start_mg["operation"]["prev_oid"] is None

    assert child_start_mg["operation"]["parent_id"] == "parent-op"
    assert child_start_mg["operation"]["phase"] == "started"
    assert child_start_mg["operation"]["seq"] == 0

    assert parent_effect_2_mg["operation"]["prev_oid"] == parent_effect_1.oid
    assert parent_effect_2_mg["operation"]["seq"] == 2
    assert parent_effect_2_mg["operation"]["effect_count"] == 2

    assert child_completed_mg["operation"]["prev_oid"] == child_effect.oid
    assert child_completed_mg["operation"]["effect_count"] == 1
    assert parent_completed.metadata["mg"]["operation"]["prev_oid"] == parent_effect_2.oid


def test_marker_substrate_uses_pipeline(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-marker-pipe")
    marker = mg.lifecycle_substrates[0]
    marker.mark("test-pipeline")  # type: ignore[attr-defined]
    mg.merge(task, mg.ground)

    effects = mg.filter_effects(effect_type="Marker")
    assert any(e.metadata.get("label") == "test-pipeline" for e in effects)


def test_declarative_filesystem_uses_pipeline(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-fs-pipe")
    fs = mg.lifecycle_substrates[1]
    fs.record_changes([("pipe_test.py", b"hello")])  # type: ignore[attr-defined]
    mg.merge(task, mg.ground)

    effects = mg.filter_effects(effect_type="FileCreate")
    assert any(e.metadata.get("path") == "pipe_test.py" for e in effects)


def test_vcscore_merge_uses_pipeline(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-merge-pipe")
    mg.merge(task, mg.ground)

    effects = mg.filter_effects(effect_type="ScopeMerge")
    effect = next(e for e in effects if e.metadata.get("merged_into") == "ground")
    assert effect.metadata["world_id"] == task.world_id
    assert effect.metadata["parent_world_id"] == mg.ground.world_id


def test_vcscore_discard_uses_pipeline(mg: VcsCore) -> None:
    import pygit2
    from vcs_core.git_store import read_effect_json

    task = mg.fork(mg.ground, "task-discard-pipe")
    mg.discard(task)

    repo = pygit2.Repository(mg.store._repo_path)
    archive_refs = [r for r in repo.references if "task-discard-pipe" in r]
    assert len(archive_refs) == 1

    tip = repo.references[archive_refs[0]].peel(pygit2.Commit)
    found = False
    for commit in repo.walk(tip.id, pygit2.GIT_SORT_TOPOLOGICAL):
        meta = read_effect_json(repo, commit)
        if meta.get("type") == "DiscardSnapshot":
            assert meta.get("discarded_scope") == "task-discard-pipe"
            assert meta.get("world_id") == task.world_id
            assert meta.get("parent_world_id") == mg.ground.world_id
            found = True
            break
    assert found


def test_backward_compat_store_construction(workspace: Path) -> None:
    from vcs_core._substrate_runtime import build_builtin_substrate_context
    from vcs_core.store import Store
    from vcs_core.substrates import MarkerSubstrate

    store = Store(str(workspace / ".vcscore"))
    marker = MarkerSubstrate(build_builtin_substrate_context(store))

    m = VcsCore(str(workspace), substrates=[marker])
    m.activate()

    task = m.fork(m.ground, "task-compat")
    marker.mark("compat-test")
    m.merge(task, m.ground)

    effects = m.filter_effects(effect_type="Marker")
    assert any(e.metadata.get("label") == "compat-test" for e in effects)
    m.deactivate()


def test_single_pipeline_invariant(workspace: Path) -> None:
    from vcs_core._substrate_runtime import build_builtin_substrate_context
    from vcs_core.store import Store
    from vcs_core.substrates import DeclarativeFilesystemSubstrate, MarkerSubstrate

    store = Store(str(workspace / ".vcscore"))
    context = build_builtin_substrate_context(store)
    marker = MarkerSubstrate(context)
    fs = DeclarativeFilesystemSubstrate(context)
    m = VcsCore(str(workspace), substrates=[marker, fs])

    assert marker._pipeline is m._pipeline
    assert fs._pipeline is m._pipeline


def test_vcscore_binds_internal_runtime_to_runtime_bound_substrates(workspace: Path) -> None:
    captured = {}

    class TrackingSubstrate:
        name = "tracking-bind"
        commands = {}
        effects = {}

        def bind_runtime(self, runtime) -> None:
            captured["pipeline"] = runtime.pipeline
            captured["runtime"] = runtime

        def activate(self) -> None:
            pass

        def deactivate(self) -> None:
            pass

        def push(self, scope_id: str | None = None) -> None:
            del scope_id

        def authority(self):
            return None

        def python_patches(self) -> tuple[object, ...]:
            return ()

    m = VcsCore(str(workspace), substrates=[TrackingSubstrate()])  # type: ignore[list-item]

    assert captured["pipeline"] is m._pipeline
    assert captured["runtime"] is not None
