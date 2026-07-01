"""Store operation-ref integration tests."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pygit2
import pytest
import vcs_core._store_operation_queries as store_operation_queries
from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._projection_store import (
    ARCHIVED_OPERATIONS_BY_ID_CURRENT_REF,
    ARCHIVED_OPERATIONS_BY_ID_FAMILY,
    ARCHIVED_OPERATIONS_BY_ID_VERSION,
    archived_operation_projection_digest,
    archived_operation_projection_frontier,
    archived_operation_projection_is_fresh,
    load_archived_operations_by_id_snapshot,
)
from vcs_core.git_store import build_dual_tree, build_effect_meta_tree, build_tree, create_signature
from vcs_core.store import Store


def _pointer_metadata(
    task,
    *,
    operation_id: str,
    phase: str,
    seq: int,
    prev_oid: str | None,
    effect_count: int,
    handle_id: str,
    label: str,
    kind: str = "marker.runtime",
    started_at: float = 100.0,
    closed_at: float | None = None,
    result: str | None = None,
    parent_operation_id: str | None = None,
    world_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, object]:
    operation: dict[str, object] = {
        "id": operation_id,
        "phase": phase,
        "seq": seq,
        "prev_oid": prev_oid,
        "kind": kind,
        "label": label,
        "effect_count": effect_count,
        "started_at": started_at,
    }
    if parent_operation_id is not None:
        operation["parent_id"] = parent_operation_id
    if closed_at is not None:
        operation["closed_at"] = closed_at
    if result is not None:
        operation["result"] = result

    mg: dict[str, object] = {
        "version": 1,
        "world": {
            "id": world_id or task.world_id,
            "ref": task.ref,
            "instance_id": task.instance_id,
        },
        "operation": operation,
    }
    if session_id is not None:
        mg["session_id"] = session_id

    return {"mg": mg}


def _operation_id(entry) -> object:  # type: ignore[no-untyped-def]
    return entry.metadata["mg"]["operation"]["id"]


def _begin_operation(
    store: Store,
    task,
    *,
    handle_id: str,
    kind: str,
    parent_op_ref: str | None = None,
    operation_id: str | None = None,
    operation_label: str | None = None,
    session_id: str | None = None,
    metadata: dict[str, object] | None = None,
):
    assert task.world_id is not None
    return store.begin_operation(
        task.ref,
        handle_id=handle_id,
        kind=kind,
        world_id=task.world_id,
        scope_instance_id=task.instance_id,
        parent_op_ref=parent_op_ref,
        operation_id=operation_id,
        operation_label=operation_label,
        session_id=session_id,
        metadata=metadata,
    )


def test_concurrent_operation_effect_appends_retain_all_events(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-concurrent-capture")
    op = _begin_operation(store, task, handle_id="cmd-concurrent", kind="vcs_core.session_exec")

    event_count = 64

    def append_event(index: int) -> None:
        store.append_operation_effect(
            op,
            "CaptureEvent",
            {"index": index},
            substrate="filesystem",
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(append_event, range(event_count)))

    history = store.read_operation_history(op.ref)
    capture_events = [commit for commit in history.commits if commit.metadata.get("type") == "CaptureEvent"]

    assert len(capture_events) == event_count
    assert sorted(commit.metadata["index"] for commit in capture_events) == list(range(event_count))
    assert sorted(commit.metadata["mg"]["operation"]["seq"] for commit in capture_events) == list(
        range(1, event_count + 1)
    )


def test_concurrent_operation_effect_appends_across_store_instances_retain_all_events(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-concurrent-capture-multi-store")
    op = _begin_operation(store, task, handle_id="cmd-concurrent-multi-store", kind="vcs_core.session_exec")
    sibling_store = Store.open_existing(store.repo_path)
    stores = (store, sibling_store)

    event_count = 64

    def append_event(index: int) -> None:
        stores[index % len(stores)].append_operation_effect(
            op,
            "CaptureEvent",
            {"index": index},
            substrate="filesystem",
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(append_event, range(event_count)))

    history = store.read_operation_history(op.ref)
    capture_events = [commit for commit in history.commits if commit.metadata.get("type") == "CaptureEvent"]

    assert len(capture_events) == event_count
    assert sorted(commit.metadata["index"] for commit in capture_events) == list(range(event_count))
    assert sorted(commit.metadata["mg"]["operation"]["seq"] for commit in capture_events) == list(
        range(1, event_count + 1)
    )


def _drop_nested_metadata_path(metadata: dict[str, object], path: tuple[str, ...]) -> None:
    target: object = metadata
    for segment in path[:-1]:
        assert isinstance(target, dict)
        target = target[segment]
    assert isinstance(target, dict)
    target.pop(path[-1])


def _set_nested_metadata_path(metadata: dict[str, object], path: tuple[str, ...], value: object) -> None:
    target: object = metadata
    for segment in path[:-1]:
        assert isinstance(target, dict)
        target = target[segment]
    assert isinstance(target, dict)
    target[path[-1]] = value


def _write_projected_operation_history(
    store: Store,
    task,
    *,
    operation_id: str,
    final_phase: str,
    missing_path: tuple[str, ...] | None = None,
    invalid_path: tuple[str, ...] | None = None,
    invalid_value: object | None = None,
) -> str:
    ref = store.operation_ref(operation_id)
    base_oid = store.log(ref=task.ref, max_count=1)[0].oid
    store._repo.references.create(ref, pygit2.Oid(hex=base_oid))

    started = _pointer_metadata(
        task,
        operation_id=operation_id,
        phase="started",
        seq=0,
        prev_oid=None,
        effect_count=0,
        handle_id=operation_id,
        label=operation_id,
    )
    if missing_path is not None:
        _drop_nested_metadata_path(started, missing_path)
    if invalid_path is not None:
        _set_nested_metadata_path(started, invalid_path, invalid_value)
    start_oid = store._emit_effect_to_ref(
        ref,
        scope_name=task.name,
        effect_type="OperationStarted",
        metadata=started,
        substrate="vcscore",
        author_name=task.name,
    )

    completed = _pointer_metadata(
        task,
        operation_id=operation_id,
        phase=final_phase,
        seq=1,
        prev_oid=start_oid,
        effect_count=0,
        handle_id=operation_id,
        label=operation_id,
        closed_at=101.0,
        result="ok" if final_phase == "completed" else "error",
    )
    if missing_path is not None:
        _drop_nested_metadata_path(completed, missing_path)
    if invalid_path is not None:
        _set_nested_metadata_path(completed, invalid_path, invalid_value)
    store._emit_effect_to_ref(
        ref,
        scope_name=task.name,
        effect_type="OperationCompleted" if final_phase == "completed" else "OperationAborted",
        metadata=completed,
        substrate="vcscore",
        author_name=task.name,
    )

    tip = store._repo.references[ref].peel(pygit2.Commit).id
    if final_phase == "completed":
        store._repo.references.create(task.ref, tip, force=True)
        store._repo.references.delete(ref)
        return task.ref

    archive_ref = f"refs/vcscore/archive/ops/{operation_id}"
    store._repo.references.create(archive_ref, tip)
    store._repo.references.delete(ref)
    return archive_ref


def _publish_projection_snapshot(
    store: Store,
    *,
    entries: list[dict[str, object]] | None = None,
) -> None:
    frontier = archived_operation_projection_frontier(store._repo)
    manifest = {
        "family": ARCHIVED_OPERATIONS_BY_ID_FAMILY,
        "version": ARCHIVED_OPERATIONS_BY_ID_VERSION,
        "built_at": 0,
        "completeness": "complete",
        "source": [
            {"ref": carrier.ref, "tip_oid": carrier.tip_oid, "carrier_kind": carrier.carrier_kind}
            for carrier in frontier
        ],
        "source_digest": archived_operation_projection_digest(frontier),
    }

    changes: list[tuple[str, bytes]] = [("meta/projection.json", json.dumps(manifest, sort_keys=True).encode("utf-8"))]
    shard_entries: dict[str, list[dict[str, object]]] = {}
    for entry in entries or []:
        operation_id = entry["operation_id"]
        assert isinstance(operation_id, str)
        shard_entries.setdefault(operation_id[:2], []).append(entry)
    for shard, payload in sorted(shard_entries.items()):
        changes.append((f"data/shards/{shard}.json", json.dumps(payload, sort_keys=True).encode("utf-8")))

    tree_oid = build_tree(store._repo, None, changes)
    sig = create_signature("projection-test")
    commit_oid = store._repo.create_commit(None, sig, sig, "projection:test", tree_oid, [])
    store._repo.references.create(ARCHIVED_OPERATIONS_BY_ID_CURRENT_REF, commit_oid, force=True)


def _write_open_operation_start_commit_without_parent(
    store: Store,
    task,
    *,
    operation_id: str,
) -> str:
    ref = store.operation_ref(operation_id)
    root_commit = store._repo.references[Store.GROUND_REF].peel(pygit2.Commit)
    metadata = _pointer_metadata(
        task,
        operation_id=operation_id,
        phase="started",
        seq=0,
        prev_oid=None,
        effect_count=0,
        handle_id=operation_id,
        label=operation_id,
    )
    effect_meta = {
        **metadata,
        "type": "OperationStarted",
        "substrate": "vcscore",
        "scope": task.name,
        "timestamp": 100.0,
    }
    meta_tree_oid = build_effect_meta_tree(store._repo, effect_meta)
    root_tree_oid = build_dual_tree(store._repo, root_commit.tree["workspace"].id, meta_tree_oid)
    message = f"effect:OperationStarted scope:{task.name}\n\nMeta-Effect: {json.dumps(effect_meta)}\n"
    sig = create_signature(task.name)
    commit_oid = store._repo.create_commit(None, sig, sig, message, root_tree_oid, [])
    store._repo.references.create(ref, commit_oid)
    return ref


def test_abort_root_operation_archives_ref_without_advancing_scope(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-root-abort")
    original_tip = store.log(ref=task.ref, max_count=1)[0].oid

    op = _begin_operation(store, task, handle_id="op-root", kind="git.commit")
    store.append_operation_effect(
        op,
        "FileCreate",
        {"path": "aborted.txt"},
        workspace_changes=[("aborted.txt", b"payload")],
        substrate="filesystem",
    )

    archive_ref = store.abort_operation(op, metadata={"reason": "command failed"})

    assert op.ref not in store._repo.references
    assert archive_ref in store._repo.references
    assert store.log(ref=task.ref, max_count=1)[0].oid == original_tip
    assert store.read_workspace_file(task.ref, "aborted.txt") is None

    archive_log = store.log(ref=archive_ref, max_count=3)
    assert [entry.metadata["type"] for entry in archive_log] == [
        "OperationAborted",
        "FileCreate",
        "OperationStarted",
    ]
    assert archive_log[0].metadata["mg"]["operation"]["result"] == "error"
    assert archive_log[0].metadata["reason"] == "command failed"


def test_abort_child_operation_archives_ref_without_advancing_parent(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-child-abort")
    parent = _begin_operation(store, task, handle_id="parent", kind="git.commit")
    store.append_operation_effect(
        parent,
        "FileCreate",
        {"path": "parent.txt"},
        workspace_changes=[("parent.txt", b"parent")],
        substrate="filesystem",
    )
    parent_tip_before_child = store.log(ref=parent.ref, max_count=1)[0].oid

    child = _begin_operation(
        store,
        task,
        handle_id="child",
        kind="git.hook",
        parent_op_ref=parent.ref,
    )
    store.append_operation_effect(
        child,
        "FileCreate",
        {"path": "child.txt"},
        workspace_changes=[("child.txt", b"child")],
        substrate="filesystem",
    )

    archive_ref = store.abort_operation(child, metadata={"reason": "hook failed"})

    assert child.ref not in store._repo.references
    assert archive_ref in store._repo.references
    assert store.log(ref=parent.ref, max_count=1)[0].oid == parent_tip_before_child
    assert store.read_workspace_file(parent.ref, "child.txt") is None
    assert store.read_workspace_file(parent.ref, "parent.txt") == b"parent"

    open_operations = store.list_open_operations(scope_ref=task.ref)
    assert [op.handle_id for op in open_operations] == ["parent"]

    archive_log = store.log(ref=archive_ref, max_count=3)
    assert [entry.metadata["type"] for entry in archive_log] == [
        "OperationAborted",
        "FileCreate",
        "OperationStarted",
    ]
    assert archive_log[0].metadata["mg"]["operation"]["parent_id"] == parent.durable_id


def test_operation_identity_metadata_propagates_through_lifecycle(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-operation-identity")

    op = _begin_operation(
        store,
        task,
        handle_id="label-op",
        kind="git.commit",
        operation_id="stable-op-123",
        operation_label="label-op",
        session_id="sess-123",
    )
    store.append_operation_effect(
        op,
        "FileCreate",
        {"path": "hello.txt"},
        workspace_changes=[("hello.txt", b"hello")],
        substrate="filesystem",
    )

    store.finalize_operation(op, scope=task)

    entries = store.log(ref=task.ref, max_count=3)
    started = next(entry for entry in entries if entry.metadata["type"] == "OperationStarted")
    file_entry = next(entry for entry in entries if entry.metadata["type"] == "FileCreate")
    completed = next(entry for entry in entries if entry.metadata["type"] == "OperationCompleted")

    assert _operation_id(started) == "stable-op-123"
    assert started.metadata["mg"]["operation"]["label"] == "label-op"
    assert _operation_id(file_entry) == "stable-op-123"
    assert _operation_id(completed) == "stable-op-123"
    assert completed.metadata["mg"]["session_id"] == "sess-123"


def test_begin_operation_accepts_explicit_durable_metadata(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-pointer-start")

    op = _begin_operation(
        store,
        task,
        handle_id="pointer-op",
        kind="marker.runtime",
        operation_id="pointer-op-id",
        operation_label="pointer-op",
        session_id="sess-pointer",
    )

    history = store.read_operation_history(op.ref)

    assert history.summary.operation_id == "pointer-op-id"
    assert history.commits[-1].metadata["mg"]["session_id"] == "sess-pointer"


def test_begin_operation_uses_explicit_world_id(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-pointer-no-aliases")
    op = store.begin_operation(
        task.ref,
        handle_id="pointer-op",
        kind="marker.runtime",
        world_id="world_pointer",
        scope_instance_id=task.instance_id,
        operation_id="pointer-op-id",
        operation_label="pointer-op",
    )

    history = store.read_operation_history(op.ref)

    assert op.operation_id == "pointer-op-id"
    assert op.operation_label == "pointer-op"
    assert op.world_id == "world_pointer"
    assert history.summary.operation_id == "pointer-op-id"
    assert history.summary.world_id == "world_pointer"


def test_begin_operation_accepts_explicit_session_id(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-pointer-session-only")
    op = _begin_operation(
        store,
        task,
        handle_id="pointer-op",
        kind="marker.runtime",
        operation_id="pointer-op-id",
        operation_label="pointer-op",
        session_id="sess-pointer",
    )

    history = store.read_operation_history(op.ref)

    assert op.session_id == "sess-pointer"
    assert history.commits[-1].metadata["mg"]["session_id"] == "sess-pointer"


def test_list_open_operations_derives_handle_fields_from_mg_and_topology(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-open-derived")
    base_oid = store.log(ref=task.ref, max_count=1)[0].oid

    parent_ref = store.operation_ref("parent-op-id")
    store._repo.references.create(parent_ref, pygit2.Oid(hex=base_oid))
    store._emit_effect_to_ref(
        parent_ref,
        scope_name=task.name,
        effect_type="OperationStarted",
        metadata={
            "mg": {
                "version": 1,
                "world": {
                    "id": "world_parent",
                    "ref": task.ref,
                    "instance_id": task.instance_id,
                },
                "operation": {
                    "id": "parent-op-id",
                    "phase": "started",
                    "seq": 0,
                    "prev_oid": None,
                    "kind": "marker.runtime",
                    "label": "parent-label",
                    "effect_count": 0,
                    "started_at": 100.0,
                },
                "session_id": "sess-derived",
            }
        },
        substrate="vcscore",
        author_name=task.name,
    )

    child_ref = store.operation_ref("child-op-id")
    store._repo.references.create(child_ref, pygit2.Oid(hex=base_oid))
    store._emit_effect_to_ref(
        child_ref,
        scope_name=task.name,
        effect_type="OperationStarted",
        metadata={
            "mg": {
                "version": 1,
                "world": {
                    "id": "world_child",
                    "ref": task.ref,
                    "instance_id": task.instance_id,
                },
                "operation": {
                    "id": "child-op-id",
                    "parent_id": "parent-op-id",
                    "phase": "started",
                    "seq": 0,
                    "prev_oid": None,
                    "kind": "marker.runtime",
                    "label": "child-label",
                    "effect_count": 0,
                    "started_at": 101.0,
                },
                "session_id": "sess-derived",
            }
        },
        substrate="vcscore",
        author_name=task.name,
    )

    operations = {
        op.operation_id: op for op in store.list_open_operations(scope_ref=task.ref, session_id="sess-derived")
    }

    assert set(operations) == {"parent-op-id", "child-op-id"}
    assert operations["parent-op-id"].handle_id == "parent-op-id"
    assert operations["parent-op-id"].scope_ref == task.ref
    assert operations["parent-op-id"].scope_instance_id == task.instance_id
    assert operations["parent-op-id"].base_oid == base_oid
    assert operations["parent-op-id"].kind == "marker.runtime"
    assert operations["parent-op-id"].operation_label == "parent-label"
    assert operations["parent-op-id"].parent_op_ref is None
    assert operations["child-op-id"].handle_id == "child-op-id"
    assert operations["child-op-id"].durable_id == "child-op-id"
    assert operations["child-op-id"].world_id == "world_child"
    assert operations["child-op-id"].base_oid == base_oid
    assert operations["child-op-id"].parent_operation_id == "parent-op-id"
    assert operations["child-op-id"].parent_op_ref == parent_ref


def test_begin_operation_derives_parent_operation_id_from_parent_ref(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-pointer-parent-only")
    parent = _begin_operation(
        store,
        task,
        handle_id="parent-op",
        kind="marker.runtime",
        operation_id="parent-op-id",
        operation_label="parent-op",
    )
    child = _begin_operation(
        store,
        task,
        handle_id="child-op",
        kind="marker.runtime",
        parent_op_ref=parent.ref,
        operation_id="child-op-id",
        operation_label="child-op",
    )

    history = store.read_operation_history(child.ref)

    assert child.parent_operation_id == "parent-op-id"
    assert history.commits[-1].metadata["mg"]["operation"]["parent_id"] == "parent-op-id"


def test_begin_operation_rejects_reserved_mg_metadata(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-pointer-world-ref-mismatch")
    metadata = _pointer_metadata(
        task,
        operation_id="pointer-op-id",
        phase="started",
        seq=0,
        prev_oid=None,
        effect_count=0,
        handle_id="pointer-op",
        label="pointer-op",
    )
    with pytest.raises(ValueError, match="Reserved mg lifecycle metadata"):
        _begin_operation(
            store,
            task,
            handle_id="pointer-op",
            kind="marker.runtime",
            operation_id="pointer-op-id",
            operation_label="pointer-op",
            metadata=metadata,
        )


def test_begin_operation_rejects_missing_world_id(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-pointer-kind-mismatch")
    with pytest.raises(ValueError, match="world_id is required"):
        store.begin_operation(
            task.ref,
            handle_id="pointer-op",
            kind="marker.runtime",
            world_id="",
            scope_instance_id=task.instance_id,
        )


def test_begin_operation_rejects_top_level_operation_id_alias(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-pointer-alias-id-mismatch")
    metadata = _pointer_metadata(
        task,
        operation_id="pointer-op-id",
        phase="started",
        seq=0,
        prev_oid=None,
        effect_count=0,
        handle_id="pointer-op",
        label="pointer-op",
    )
    metadata["operation_id"] = "pointer-op-id"

    with pytest.raises(ValueError, match="Legacy top-level lifecycle metadata"):
        store.begin_operation(
            task.ref,
            handle_id="pointer-op",
            kind="marker.runtime",
            world_id=task.world_id or "",
            scope_instance_id=task.instance_id,
            metadata=metadata,
        )


def test_begin_operation_rejects_top_level_world_id_alias(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-pointer-alias-world-mismatch")
    metadata = _pointer_metadata(
        task,
        operation_id="pointer-op-id",
        phase="started",
        seq=0,
        prev_oid=None,
        effect_count=0,
        handle_id="pointer-op",
        label="pointer-op",
        world_id="world_pointer",
    )
    metadata["world_id"] = "world_pointer"

    with pytest.raises(ValueError, match="Legacy top-level lifecycle metadata"):
        store.begin_operation(
            task.ref,
            handle_id="pointer-op",
            kind="marker.runtime",
            world_id=task.world_id or "",
            scope_instance_id=task.instance_id,
            metadata=metadata,
        )


def test_begin_operation_rejects_top_level_session_id_alias(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-pointer-alias-session-mismatch")
    metadata = _pointer_metadata(
        task,
        operation_id="pointer-op-id",
        phase="started",
        seq=0,
        prev_oid=None,
        effect_count=0,
        handle_id="pointer-op",
        label="pointer-op",
        session_id="sess-pointer",
    )
    metadata["session_id"] = "sess-pointer"

    with pytest.raises(ValueError, match="Legacy top-level lifecycle metadata"):
        store.begin_operation(
            task.ref,
            handle_id="pointer-op",
            kind="marker.runtime",
            world_id=task.world_id or "",
            scope_instance_id=task.instance_id,
            metadata=metadata,
        )


def test_begin_operation_rejects_top_level_parent_operation_id_alias(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-pointer-alias-parent-mismatch")
    parent = _begin_operation(
        store,
        task,
        handle_id="parent-op",
        kind="marker.runtime",
        operation_id="parent-op-id",
        operation_label="parent-op",
    )
    metadata = _pointer_metadata(
        task,
        operation_id="child-op-id",
        phase="started",
        seq=0,
        prev_oid=None,
        effect_count=0,
        handle_id="child-op",
        label="child-op",
        parent_operation_id="parent-op-id",
    )
    metadata["parent_operation_id"] = "parent-op-id"

    with pytest.raises(ValueError, match="Legacy top-level lifecycle metadata"):
        store.begin_operation(
            task.ref,
            handle_id="child-op",
            kind="marker.runtime",
            world_id=task.world_id or "",
            scope_instance_id=task.instance_id,
            parent_op_ref=parent.ref,
            metadata=metadata,
        )


def test_append_operation_effect_allows_generic_payload_kind_and_status(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-generic-effect-kind")
    op = _begin_operation(
        store,
        task,
        handle_id="payload-op",
        kind="sqlite.execute",
        operation_id="payload-op-id",
        operation_label="payload-op",
    )

    effect_oid = store.append_operation_effect(
        op,
        "SqlStatementRecorded",
        {"kind": "CREATE", "status": "buffered", "sql": "CREATE TABLE items (name TEXT)"},
        substrate="sqlite",
    )
    commit = next(entry for entry in store.log(ref=op.ref, max_count=5) if entry.oid == effect_oid)

    assert commit.metadata["kind"] == "CREATE"
    assert commit.metadata["status"] == "buffered"
    assert commit.metadata["mg"]["operation"]["kind"] == "sqlite.execute"


def test_abort_operations_with_distinct_durable_ids_use_distinct_archive_refs(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-repeat-abort")

    first = _begin_operation(
        store,
        task,
        handle_id="repeat",
        kind="git.commit",
        operation_id="repeat-one",
        operation_label="repeat",
    )
    store.append_operation_effect(
        first,
        "FileCreate",
        {"path": "first.txt"},
        workspace_changes=[("first.txt", b"first")],
        substrate="filesystem",
    )
    first_archive = store.abort_operation(first, metadata={"reason": "first failure"})

    second = _begin_operation(
        store,
        task,
        handle_id="repeat",
        kind="git.commit",
        operation_id="repeat-two",
        operation_label="repeat",
    )
    store.append_operation_effect(
        second,
        "FileCreate",
        {"path": "second.txt"},
        workspace_changes=[("second.txt", b"second")],
        substrate="filesystem",
    )
    second_archive = store.abort_operation(second, metadata={"reason": "second failure"})

    assert first_archive == "refs/vcscore/archive/ops/repeat-one"
    assert second_archive == "refs/vcscore/archive/ops/repeat-two"
    assert first_archive in store._repo.references
    assert second_archive in store._repo.references
    assert store.read_workspace_file(first_archive, "first.txt") == b"first"
    assert store.read_workspace_file(second_archive, "second.txt") == b"second"


def test_visible_operations_exclude_staged_history_until_finalize(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-visible-ops")

    op = _begin_operation(
        store,
        task,
        handle_id="visible-op",
        kind="marker.mark",
        operation_id="visible-op-id",
        operation_label="visible-op",
    )
    store.append_operation_effect(
        op,
        "Marker",
        {"label": "inside"},
        substrate="marker",
    )

    assert store.visible_operations(ref=task.ref) == []

    open_summaries = store.open_operations(scope_ref=task.ref)
    assert len(open_summaries) == 1
    assert open_summaries[0].visibility == "staged"
    assert open_summaries[0].status == "open"
    assert open_summaries[0].effect_count == 1
    assert open_summaries[0].operation_id == "visible-op-id"

    store.finalize_operation(op, scope=task)

    visible = store.visible_operations(ref=task.ref)
    assert len(visible) == 1
    assert visible[0].visibility == "visible"
    assert visible[0].status == "ok"
    assert visible[0].effect_count == 1
    assert visible[0].operation_id == "visible-op-id"
    assert visible[0].label == "visible-op"


def test_visible_operations_reject_duplicate_visible_operation_id(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-duplicate-visible-op-id")

    first = _begin_operation(
        store,
        task,
        handle_id="duplicate-op",
        kind="marker.mark",
        operation_id="shared-operation-id",
        operation_label="duplicate-op",
    )
    store.append_operation_effect(first, "Marker", {"label": "first"}, substrate="marker")
    store.finalize_operation(first, scope=task)

    second = _begin_operation(
        store,
        task,
        handle_id="duplicate-op",
        kind="marker.mark",
        operation_id="shared-operation-id",
        operation_label="duplicate-op",
    )
    store.append_operation_effect(second, "Marker", {"label": "second"}, substrate="marker")
    store.finalize_operation(second, scope=task)

    with pytest.raises(
        RuntimeError, match="multiple visible operations share durable operation_id 'shared-operation-id'"
    ):
        store.visible_operations(ref=task.ref)


def test_visible_operations_reject_pre_cutover_execution_history(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-legacy-visible-history")
    base_oid = store.log(ref=task.ref, max_count=1)[0].oid
    legacy_ref = "refs/vcscore/ops/legacy-visible-op"

    store._repo.references.create(legacy_ref, store._repo.references[task.ref].peel(pygit2.Commit).id)
    store._emit_effect_to_ref(
        legacy_ref,
        scope_name=task.name,
        effect_type="OperationStarted",
        metadata={
            "op_id": "legacy-visible-op",
            "operation_id": "legacy-visible-op-id",
            "operation_label": "legacy-visible-op",
            "kind": "marker.runtime",
            "scope_ref": task.ref,
            "scope_instance_id": task.instance_id,
            "base_oid": base_oid,
        },
        substrate="vcscore",
        author_name=task.name,
    )
    store._emit_effect_to_ref(
        legacy_ref,
        scope_name=task.name,
        effect_type="Marker",
        metadata={
            "op_id": "legacy-visible-op",
            "operation_id": "legacy-visible-op-id",
            "operation_label": "legacy-visible-op",
            "kind": "marker.runtime",
            "scope_ref": task.ref,
            "scope_instance_id": task.instance_id,
            "base_oid": base_oid,
            "label": "legacy",
        },
        substrate="marker",
        author_name=task.name,
    )
    store._emit_effect_to_ref(
        legacy_ref,
        scope_name=task.name,
        effect_type="OperationCompleted",
        metadata={
            "op_id": "legacy-visible-op",
            "operation_id": "legacy-visible-op-id",
            "operation_label": "legacy-visible-op",
            "kind": "marker.runtime",
            "scope_ref": task.ref,
            "scope_instance_id": task.instance_id,
            "base_oid": base_oid,
            "status": "ok",
        },
        substrate="vcscore",
        author_name=task.name,
    )

    store._repo.references.create(
        task.ref,
        store._repo.references[legacy_ref].peel(pygit2.Commit).id,
        force=True,
    )
    store._repo.references.delete(legacy_ref)

    with pytest.raises(InvalidRepositoryStateError, match="Unsupported pre-cutover execution history"):
        store.visible_operations(ref=task.ref)


def test_visible_operations_reject_missing_projected_world_ref_instead_of_falling_back_to_ground(
    store: Store,
) -> None:
    task = store.fork(Store.GROUND_REF, "task-visible-missing-world-ref")
    _write_projected_operation_history(
        store,
        task,
        operation_id="missing-world-ref-visible",
        final_phase="completed",
        missing_path=("mg", "world", "ref"),
    )

    with pytest.raises(InvalidRepositoryStateError, match=r"mg\.world\.ref"):
        store.visible_operations(ref=task.ref)


@pytest.mark.parametrize(
    ("path", "match"),
    [
        (("mg", "world", "id"), "mg.world.id"),
        (("mg", "world", "ref"), "mg.world.ref"),
        (("mg", "world", "instance_id"), "mg.world.instance_id"),
        (("mg", "operation", "kind"), "mg.operation.kind"),
    ],
)
def test_visible_operations_rejects_missing_projected_world_fields(
    store: Store,
    path: tuple[str, ...],
    match: str,
) -> None:
    task = store.fork(Store.GROUND_REF, f"task-visible-missing-{'-'.join(path[1:])}")
    _write_projected_operation_history(
        store,
        task,
        operation_id=f"visible-{'-'.join(path[1:])}",
        final_phase="completed",
        missing_path=path,
    )

    with pytest.raises(InvalidRepositoryStateError, match=match):
        store.visible_operations(ref=task.ref)


def test_visible_operations_reject_invalid_projected_world_ref_as_repository_state(
    store: Store,
) -> None:
    task = store.fork(Store.GROUND_REF, "task-visible-invalid-world-ref")
    _write_projected_operation_history(
        store,
        task,
        operation_id="visible-invalid-world-ref",
        final_phase="completed",
        invalid_path=("mg", "world", "ref"),
        invalid_value="bogus-ref",
    )

    with pytest.raises(InvalidRepositoryStateError, match=r"invalid mg\.world\.ref 'bogus-ref'"):
        store.visible_operations(ref=task.ref)


def test_list_open_operations_rejects_pre_cutover_operation_ref(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-legacy-open-history")
    base_oid = store.log(ref=task.ref, max_count=1)[0].oid
    legacy_ref = "refs/vcscore/ops/legacy-open-op"

    store._repo.references.create(legacy_ref, pygit2.Oid(hex=base_oid))
    store._emit_effect_to_ref(
        legacy_ref,
        scope_name=task.name,
        effect_type="OperationStarted",
        metadata={
            "op_id": "legacy-open-op",
            "operation_id": "legacy-open-op-id",
            "operation_label": "legacy-open-op",
            "kind": "marker.runtime",
            "scope_ref": task.ref,
            "scope_instance_id": task.instance_id,
            "base_oid": base_oid,
        },
        substrate="vcscore",
        author_name=task.name,
    )

    with pytest.raises(InvalidRepositoryStateError, match="Unsupported pre-cutover execution history"):
        store.list_open_operations(scope_ref=task.ref)


@pytest.mark.parametrize(
    ("path", "match"),
    [
        (("mg", "operation", "id"), "Unsupported pre-cutover execution history"),
        (("mg", "world", "id"), "mg.world.id"),
        (("mg", "world", "ref"), "mg.world.ref"),
        (("mg", "world", "instance_id"), "mg.world.instance_id"),
        (("mg", "operation", "kind"), "mg.operation.kind"),
    ],
)
def test_list_open_operations_rejects_missing_required_mg_fields(
    store: Store,
    path: tuple[str, ...],
    match: str,
) -> None:
    task = store.fork(Store.GROUND_REF, f"task-open-missing-{'-'.join(path[1:])}")
    base_oid = store.log(ref=task.ref, max_count=1)[0].oid
    ref = store.operation_ref("missing-field-op")
    store._repo.references.create(ref, pygit2.Oid(hex=base_oid))
    payload = _pointer_metadata(
        task,
        operation_id="missing-field-op",
        phase="started",
        seq=0,
        prev_oid=None,
        effect_count=0,
        handle_id="missing-field",
        label="missing-field",
    )
    target = payload
    for segment in path[:-1]:
        target = target[segment]  # type: ignore[index]
    target.pop(path[-1])  # type: ignore[union-attr]

    store._emit_effect_to_ref(
        ref,
        scope_name=task.name,
        effect_type="OperationStarted",
        metadata=payload,
        substrate="vcscore",
        author_name=task.name,
    )

    with pytest.raises(InvalidRepositoryStateError, match=match):
        store.list_open_operations(scope_ref=task.ref)


def test_list_open_operations_rejects_invalid_world_ref_as_repository_state(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-open-invalid-world-ref")
    base_oid = store.log(ref=task.ref, max_count=1)[0].oid
    ref = store.operation_ref("invalid-world-ref-open")
    store._repo.references.create(ref, pygit2.Oid(hex=base_oid))
    payload = _pointer_metadata(
        task,
        operation_id="invalid-world-ref-open",
        phase="started",
        seq=0,
        prev_oid=None,
        effect_count=0,
        handle_id="invalid-world-ref-open",
        label="invalid-world-ref-open",
    )
    _set_nested_metadata_path(payload, ("mg", "world", "ref"), "bogus-ref")

    store._emit_effect_to_ref(
        ref,
        scope_name=task.name,
        effect_type="OperationStarted",
        metadata=payload,
        substrate="vcscore",
        author_name=task.name,
    )

    with pytest.raises(InvalidRepositoryStateError, match=r"invalid mg\.world\.ref 'bogus-ref'"):
        store.list_open_operations(scope_ref=task.ref)


def test_list_open_operations_rejects_parentless_start_commit(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-open-missing-base-parent")
    _write_open_operation_start_commit_without_parent(
        store,
        task,
        operation_id="missing-base-parent-open",
    )

    with pytest.raises(InvalidRepositoryStateError, match="start commit is missing a base parent"):
        store.list_open_operations(scope_ref=task.ref)


def test_read_operation_history_returns_staged_commits_for_open_ref(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-open-history")

    op = _begin_operation(store, task, handle_id="open-history", kind="filesystem.write")
    store.append_operation_effect(
        op,
        "FileCreate",
        {"path": "draft.txt"},
        workspace_changes=[("draft.txt", b"draft")],
        substrate="filesystem",
    )

    history = store.read_operation_history(op.ref)

    assert history.summary.visibility == "staged"
    assert history.summary.status == "open"
    assert history.summary.effect_count == 1
    assert [entry.metadata["type"] for entry in history.commits] == [
        "FileCreate",
        "OperationStarted",
    ]


def test_append_operation_effect_rejects_reserved_mg_metadata(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-append-phase-mismatch")
    op = _begin_operation(
        store,
        task,
        handle_id="phase-op",
        kind="marker.runtime",
        operation_id="phase-op-id",
        operation_label="phase-op",
    )
    start_oid = store.log(ref=op.ref, max_count=1)[0].oid

    with pytest.raises(ValueError, match="Reserved mg lifecycle metadata"):
        store.append_operation_effect(
            op,
            "Marker",
            _pointer_metadata(
                task,
                operation_id="phase-op-id",
                phase="completed",
                seq=1,
                prev_oid=start_oid,
                effect_count=1,
                handle_id="phase-op",
                label="phase-op",
            ),
            substrate="marker",
        )


def test_append_operation_effect_rejects_top_level_operation_id_alias(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-append-payload-operation-id")
    op = _begin_operation(
        store,
        task,
        handle_id="phase-op",
        kind="marker.runtime",
        operation_id="phase-op-id",
        operation_label="phase-op",
    )
    metadata = {"label": "inside"}
    metadata["operation_id"] = "payload-op-id"

    with pytest.raises(ValueError, match="Legacy top-level lifecycle metadata"):
        store.append_operation_effect(
            op,
            "Marker",
            metadata,
            substrate="marker",
        )


def test_append_operation_effect_preserves_non_lifecycle_payload_alias_keys(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-append-preserves-payload-aliases")
    op = _begin_operation(
        store,
        task,
        handle_id="payload-op",
        kind="marker.runtime",
        operation_id="payload-op-id",
        operation_label="payload-op",
    )

    store.append_operation_effect(
        op,
        "Marker",
        {
            "label": "inside",
            "status_text": "custom-status",
            "operation_hint": "payload-operation-label",
            "parent_hint": "payload-parent-id",
        },
        substrate="marker",
    )

    marker = next(entry for entry in store.log(ref=op.ref, max_count=5) if entry.metadata["type"] == "Marker")

    assert marker.metadata["status_text"] == "custom-status"
    assert marker.metadata["operation_hint"] == "payload-operation-label"
    assert marker.metadata["parent_hint"] == "payload-parent-id"


def test_finalize_operation_rejects_reserved_mg_metadata(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-finalize-phase-mismatch")
    op = _begin_operation(
        store,
        task,
        handle_id="phase-op",
        kind="marker.runtime",
        operation_id="phase-op-id",
        operation_label="phase-op",
    )
    store.append_operation_effect(op, "Marker", {"label": "inside"}, substrate="marker")
    effect_oid = store.log(ref=op.ref, max_count=1)[0].oid

    with pytest.raises(ValueError, match="Reserved mg lifecycle metadata"):
        store.finalize_operation(
            op,
            scope=task,
            metadata=_pointer_metadata(
                task,
                operation_id="phase-op-id",
                phase="effect",
                seq=2,
                prev_oid=effect_oid,
                effect_count=1,
                handle_id="phase-op",
                label="phase-op",
                closed_at=200.0,
                result="ok",
            ),
        )


def test_finalize_operation_rejects_top_level_world_id_alias(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-finalize-alias-world-mismatch")
    op = _begin_operation(
        store,
        task,
        handle_id="phase-op",
        kind="marker.runtime",
        operation_id="phase-op-id",
        operation_label="phase-op",
    )
    store.append_operation_effect(op, "Marker", {"label": "inside"}, substrate="marker")
    effect_oid = store.log(ref=op.ref, max_count=1)[0].oid
    metadata = _pointer_metadata(
        task,
        operation_id="phase-op-id",
        phase="completed",
        seq=2,
        prev_oid=effect_oid,
        effect_count=1,
        handle_id="phase-op",
        label="phase-op",
        closed_at=200.0,
        result="ok",
    )
    metadata["world_id"] = task.instance_id

    with pytest.raises(ValueError, match="Legacy top-level lifecycle metadata"):
        store.finalize_operation(
            op,
            scope=task,
            metadata=metadata,
        )


def test_abort_operation_rejects_reserved_mg_metadata(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-abort-phase-mismatch")
    op = _begin_operation(
        store,
        task,
        handle_id="phase-op",
        kind="marker.runtime",
        operation_id="phase-op-id",
        operation_label="phase-op",
    )
    store.append_operation_effect(op, "Marker", {"label": "inside"}, substrate="marker")
    effect_oid = store.log(ref=op.ref, max_count=1)[0].oid

    with pytest.raises(ValueError, match="Reserved mg lifecycle metadata"):
        store.abort_operation(
            op,
            metadata=_pointer_metadata(
                task,
                operation_id="phase-op-id",
                phase="completed",
                seq=2,
                prev_oid=effect_oid,
                effect_count=1,
                handle_id="phase-op",
                label="phase-op",
                closed_at=200.0,
                result="error",
            ),
        )


def test_abort_operation_rejects_top_level_session_id_alias(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-abort-alias-session-mismatch")
    op = _begin_operation(
        store,
        task,
        handle_id="phase-op",
        kind="marker.runtime",
        operation_id="phase-op-id",
        operation_label="phase-op",
        session_id="sess-123",
    )
    store.append_operation_effect(op, "Marker", {"label": "inside"}, substrate="marker")
    effect_oid = store.log(ref=op.ref, max_count=1)[0].oid
    metadata = _pointer_metadata(
        task,
        operation_id="phase-op-id",
        phase="aborted",
        seq=2,
        prev_oid=effect_oid,
        effect_count=1,
        handle_id="phase-op",
        label="phase-op",
        closed_at=200.0,
        result="error",
        session_id="sess-123",
    )
    metadata["session_id"] = "sess-123"

    with pytest.raises(ValueError, match="Legacy top-level lifecycle metadata"):
        store.abort_operation(
            op,
            metadata=metadata,
        )


def test_archived_operations_summarize_failed_history(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-archived-summary")

    op = _begin_operation(
        store,
        task,
        handle_id="archived-op",
        kind="filesystem.write",
        operation_id="archived-op-id",
        operation_label="archived-op",
    )
    store.append_operation_effect(
        op,
        "FileCreate",
        {"path": "failed.txt"},
        workspace_changes=[("failed.txt", b"failed")],
        substrate="filesystem",
    )
    archive_ref = store.abort_operation(op, metadata={"reason": "boom"})

    summaries = store.archived_operations(world_id=task.world_id)
    assert len(summaries) == 1
    assert summaries[0].visibility == "archived"
    assert summaries[0].status == "error"
    assert summaries[0].effect_count == 1
    assert summaries[0].operation_id == "archived-op-id"
    assert summaries[0].carrier_ref == archive_ref
    assert summaries[0].archived_via == "operation_ref"

    projected_hit = store.archived_operations(operation_id="archived-op-id")
    assert len(projected_hit) == 1
    assert projected_hit[0].carrier_ref == archive_ref
    assert projected_hit[0].archived_via == "operation_ref"

    history = store.read_operation_history(archive_ref)
    assert [entry.metadata["type"] for entry in history.commits] == [
        "OperationAborted",
        "FileCreate",
        "OperationStarted",
    ]


def test_archived_operations_include_discarded_world_visible_history(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-discarded-world-summary")

    op = _begin_operation(
        store,
        task,
        handle_id="discarded-world-op",
        kind="filesystem.write",
        operation_id="discarded-world-op-id",
        operation_label="discarded-world-op",
    )
    store.append_operation_effect(
        op,
        "FileCreate",
        {"path": "discarded.txt"},
        workspace_changes=[("discarded.txt", b"discarded")],
        substrate="filesystem",
    )
    store.finalize_operation(op, scope=task)
    archive_ref = store.discard(task)

    summaries = store.archived_operations(world_id=task.world_id)
    assert len(summaries) == 1
    assert summaries[0].visibility == "archived"
    assert summaries[0].status == "ok"
    assert summaries[0].effect_count == 1
    assert summaries[0].operation_id == "discarded-world-op-id"
    assert summaries[0].carrier_ref == archive_ref
    assert summaries[0].archived_via == "discarded_world_ref"
    assert store.operation_id_exists("discarded-world-op-id")

    projected_hit = store.archived_operations(operation_id="discarded-world-op-id")
    assert len(projected_hit) == 1
    assert projected_hit[0].carrier_ref == archive_ref
    assert projected_hit[0].archived_via == "discarded_world_ref"

    snapshot = load_archived_operations_by_id_snapshot(store._repo)
    assert snapshot is not None
    candidate = snapshot.entries_by_id["discarded-world-op-id"]
    assert candidate.carrier_ref == archive_ref
    assert candidate.carrier_kind == "discarded_world_ref"


def test_archived_operations_fall_back_when_fresh_projection_omits_real_entry(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-discarded-world-projection-omission")

    op = _begin_operation(
        store,
        task,
        handle_id="discarded-world-omission-op",
        kind="filesystem.write",
        operation_id="discarded-world-omission-op-id",
        operation_label="discarded-world-omission-op",
    )
    store.finalize_operation(op, scope=task)
    archive_ref = store.discard(task)

    _publish_projection_snapshot(store, entries=[])

    summaries = store.archived_operations(operation_id="discarded-world-omission-op-id")

    assert len(summaries) == 1
    assert summaries[0].carrier_ref == archive_ref
    assert summaries[0].archived_via == "discarded_world_ref"
    assert store.operation_id_exists("discarded-world-omission-op-id")


def test_archived_operations_fall_back_when_projection_missing(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-discarded-world-projection-missing")

    op = _begin_operation(
        store,
        task,
        handle_id="discarded-world-missing-op",
        kind="filesystem.write",
        operation_id="discarded-world-missing-op-id",
        operation_label="discarded-world-missing-op",
    )
    store.finalize_operation(op, scope=task)
    archive_ref = store.discard(task)

    store._repo.references.delete(ARCHIVED_OPERATIONS_BY_ID_CURRENT_REF)

    reloaded = Store(store.repo_path)
    summaries = reloaded.archived_operations(world_id=task.world_id)

    assert len(summaries) == 1
    assert summaries[0].operation_id == "discarded-world-missing-op-id"
    assert summaries[0].carrier_ref == archive_ref
    assert ARCHIVED_OPERATIONS_BY_ID_CURRENT_REF not in reloaded._repo.references


def test_archived_operations_fall_back_when_projection_is_stale(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-discarded-world-projection-stale")

    op = _begin_operation(
        store,
        task,
        handle_id="discarded-world-stale-op",
        kind="filesystem.write",
        operation_id="discarded-world-stale-op-id",
        operation_label="discarded-world-stale-op",
    )
    store.finalize_operation(op, scope=task)
    archive_ref = store.discard(task)

    snapshot = load_archived_operations_by_id_snapshot(store._repo)
    assert snapshot is not None
    assert archived_operation_projection_is_fresh(store._repo, snapshot)

    ground_tip = store._repo.references[Store.GROUND_REF].peel(pygit2.Commit).id
    store._repo.references.create("refs/vcscore/archive/stale-projection-sentinel", ground_tip)

    stale_snapshot = load_archived_operations_by_id_snapshot(store._repo)
    assert stale_snapshot is not None
    assert not archived_operation_projection_is_fresh(store._repo, stale_snapshot)

    summaries = store.archived_operations(operation_id="discarded-world-stale-op-id")

    assert len(summaries) == 1
    assert summaries[0].carrier_ref == archive_ref
    assert summaries[0].archived_via == "discarded_world_ref"


def test_archived_operations_fall_back_when_projection_is_corrupt(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-discarded-world-projection-corrupt")

    op = _begin_operation(
        store,
        task,
        handle_id="discarded-world-corrupt-op",
        kind="filesystem.write",
        operation_id="discarded-world-corrupt-op-id",
        operation_label="discarded-world-corrupt-op",
    )
    store.finalize_operation(op, scope=task)
    archive_ref = store.discard(task)

    tree_oid = store._repo.TreeBuilder().write()
    sig = create_signature("projection-test")
    corrupt_commit = store._repo.create_commit(None, sig, sig, "projection:corrupt", tree_oid, [])
    store._repo.references.create(ARCHIVED_OPERATIONS_BY_ID_CURRENT_REF, corrupt_commit, force=True)

    assert load_archived_operations_by_id_snapshot(store._repo) is None

    summaries = store.archived_operations(operation_id="discarded-world-corrupt-op-id")

    assert len(summaries) == 1
    assert summaries[0].carrier_ref == archive_ref
    assert summaries[0].archived_via == "discarded_world_ref"


def test_projection_with_bogus_nonexistent_carrier_does_not_reserve_operation_id(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-projection-bogus-nonexistent-carrier")

    op = _begin_operation(
        store,
        task,
        handle_id="discarded-world-bogus-op",
        kind="filesystem.write",
        operation_id="discarded-world-bogus-op-id",
        operation_label="discarded-world-bogus-op",
    )
    store.finalize_operation(op, scope=task)
    store.discard(task)

    _publish_projection_snapshot(
        store,
        entries=[
            {
                "operation_id": "bogus-op-id",
                "carrier_ref": "refs/vcscore/archive/not-real",
                "carrier_tip_oid": "deadbeef",
                "carrier_kind": "discarded_world_ref",
            }
        ],
    )

    assert load_archived_operations_by_id_snapshot(store._repo) is None
    assert not store.operation_id_exists("bogus-op-id")


def test_projection_with_bogus_mapped_candidate_falls_back_instead_of_raising(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-projection-bogus-mapped-candidate")

    op = _begin_operation(
        store,
        task,
        handle_id="discarded-world-routed-op",
        kind="filesystem.write",
        operation_id="discarded-world-routed-op-id",
        operation_label="discarded-world-routed-op",
    )
    store.finalize_operation(op, scope=task)
    archive_ref = store.discard(task)
    carrier_tip = str(store._repo.references[archive_ref].peel(pygit2.Commit).id)

    _publish_projection_snapshot(
        store,
        entries=[
            {
                "operation_id": "bogus-routed-op-id",
                "carrier_ref": archive_ref,
                "carrier_tip_oid": carrier_tip,
                "carrier_kind": "discarded_world_ref",
            }
        ],
    )

    assert store.archived_operations(operation_id="bogus-routed-op-id") == []
    assert not store.operation_id_exists("bogus-routed-op-id")


def test_projection_index_is_not_archived_operation_authority(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-projection-index-not-authority")

    op = _begin_operation(
        store,
        task,
        handle_id="projection-index-not-authority-op",
        kind="filesystem.write",
        operation_id="projection-index-not-authority-op-id",
        operation_label="projection-index-not-authority-op",
    )
    store.finalize_operation(op, scope=task)
    archive_ref = store.discard(task)
    carrier_tip = str(store._repo.references[archive_ref].peel(pygit2.Commit).id)

    _publish_projection_snapshot(
        store,
        entries=[
            {
                "operation_id": "projection-index-bogus-op-id",
                "carrier_ref": archive_ref,
                "carrier_tip_oid": carrier_tip,
                "carrier_kind": "discarded_world_ref",
            }
        ],
    )

    assert store.archived_operations(operation_id="projection-index-bogus-op-id") == []
    assert not store.operation_id_exists("projection-index-bogus-op-id")

    summaries = store.archived_operations(operation_id="projection-index-not-authority-op-id")

    assert len(summaries) == 1
    assert summaries[0].carrier_ref == archive_ref
    assert summaries[0].archived_via == "discarded_world_ref"
    assert store.operation_id_exists("projection-index-not-authority-op-id")


def test_projection_with_manifest_mismatched_candidate_falls_back_canonically(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-projection-manifest-mismatch")

    op = _begin_operation(
        store,
        task,
        handle_id="discarded-world-mismatch-op",
        kind="filesystem.write",
        operation_id="discarded-world-mismatch-op-id",
        operation_label="discarded-world-mismatch-op",
    )
    store.finalize_operation(op, scope=task)
    archive_ref = store.discard(task)

    _publish_projection_snapshot(
        store,
        entries=[
            {
                "operation_id": "discarded-world-mismatch-op-id",
                "carrier_ref": archive_ref,
                "carrier_tip_oid": "deadbeef",
                "carrier_kind": "discarded_world_ref",
            }
        ],
    )

    assert load_archived_operations_by_id_snapshot(store._repo) is None
    summaries = store.archived_operations(operation_id="discarded-world-mismatch-op-id")
    assert len(summaries) == 1
    assert summaries[0].carrier_ref == archive_ref
    assert store.operation_id_exists("discarded-world-mismatch-op-id")


def test_discard_keeps_canonical_archive_when_projection_publication_fails(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = store.fork(Store.GROUND_REF, "task-discarded-world-projection-failure")

    op = _begin_operation(
        store,
        task,
        handle_id="discarded-world-failure-op",
        kind="filesystem.write",
        operation_id="discarded-world-failure-op-id",
        operation_label="discarded-world-failure-op",
    )
    store.finalize_operation(op, scope=task)

    def _raise_projection_failure() -> bool:
        raise pygit2.GitError("projection boom")

    monkeypatch.setattr(store, "_publish_archived_operation_projection", _raise_projection_failure)

    archive_ref = store.discard(task)

    assert archive_ref in store._repo.references
    assert task.ref not in store._repo.references
    summaries = store.archived_operations(operation_id="discarded-world-failure-op-id")
    assert len(summaries) == 1
    assert summaries[0].carrier_ref == archive_ref


def test_operation_id_exists_reuses_canonical_archived_membership_cache_for_repeated_negatives(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = store.fork(Store.GROUND_REF, "task-archived-membership-cache-repeated-negatives")

    op = _begin_operation(
        store,
        task,
        handle_id="discarded-world-cache-op",
        kind="filesystem.write",
        operation_id="discarded-world-cache-op-id",
        operation_label="discarded-world-cache-op",
    )
    store.finalize_operation(op, scope=task)
    store.discard(task)

    monkeypatch.setattr(
        store,
        "_validated_projected_archived_operation_summary",
        lambda *, operation_id, world_id: None,
    )

    build_calls = {"count": 0}
    original_build = store._build_archived_operation_membership_cache

    def wrapped_build() -> object:
        build_calls["count"] += 1
        return original_build()

    monkeypatch.setattr(store, "_build_archived_operation_membership_cache", wrapped_build)

    assert not store.operation_id_exists("missing-archived-op-one")
    assert not store.operation_id_exists("missing-archived-op-two")
    assert not store.operation_id_exists("missing-archived-op-three")
    assert build_calls["count"] == 1


def test_discard_updates_archived_membership_cache_without_rebuild(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        store,
        "_validated_projected_archived_operation_summary",
        lambda *, operation_id, world_id: None,
    )

    first = store.fork(Store.GROUND_REF, "task-archived-membership-cache-first")
    first_op = _begin_operation(
        store,
        first,
        handle_id="discarded-world-cache-first",
        kind="filesystem.write",
        operation_id="discarded-world-cache-first-id",
        operation_label="discarded-world-cache-first",
    )
    store.finalize_operation(first_op, scope=first)
    store.discard(first)

    assert not store.operation_id_exists("missing-before-discard-update")

    second = store.fork(Store.GROUND_REF, "task-archived-membership-cache-second")
    second_op = _begin_operation(
        store,
        second,
        handle_id="discarded-world-cache-second",
        kind="filesystem.write",
        operation_id="discarded-world-cache-second-id",
        operation_label="discarded-world-cache-second",
    )
    store.finalize_operation(second_op, scope=second)
    store.discard(second)

    build_calls = {"count": 0}
    original_build = store._build_archived_operation_membership_cache

    def wrapped_build() -> object:
        build_calls["count"] += 1
        return original_build()

    monkeypatch.setattr(store, "_build_archived_operation_membership_cache", wrapped_build)

    assert store.operation_id_exists("discarded-world-cache-second-id")
    assert build_calls["count"] == 0


def test_discard_updates_archived_projection_without_full_rebuild(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = store.fork(Store.GROUND_REF, "task-archived-projection-first")
    first_op = _begin_operation(
        store,
        first,
        handle_id="discarded-world-projection-first",
        kind="filesystem.write",
        operation_id="discarded-world-projection-first-id",
        operation_label="discarded-world-projection-first",
    )
    store.finalize_operation(first_op, scope=first)
    store.discard(first)

    def fail_if_full_projection_rebuild() -> tuple[object, ...]:
        raise AssertionError("fresh archived projection append must not rebuild all entries")

    monkeypatch.setattr(store, "_build_archived_operation_projection_entries", fail_if_full_projection_rebuild)

    def fail_if_full_snapshot_publish(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("fresh archived projection append must not rewrite the full projection snapshot")

    monkeypatch.setattr(
        store_operation_queries,
        "publish_archived_operations_by_id_snapshot",
        fail_if_full_snapshot_publish,
    )

    second = store.fork(Store.GROUND_REF, "task-archived-projection-second")
    second_op = _begin_operation(
        store,
        second,
        handle_id="discarded-world-projection-second",
        kind="filesystem.write",
        operation_id="discarded-world-projection-second-id",
        operation_label="discarded-world-projection-second",
    )
    store.finalize_operation(second_op, scope=second)
    archive_ref = store.discard(second)

    summaries = store.archived_operations(operation_id="discarded-world-projection-second-id")
    assert len(summaries) == 1
    assert summaries[0].carrier_ref == archive_ref


def test_archive_operation_updates_archived_projection_without_full_rebuild(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = store.fork(Store.GROUND_REF, "task-archived-projection-abort")
    first = _begin_operation(
        store,
        task,
        handle_id="archived-projection-abort-first",
        kind="filesystem.write",
        operation_id="archived-projection-abort-first-id",
        operation_label="archived-projection-abort-first",
    )
    store.abort_operation(first, metadata={"reason": "first"})

    def fail_if_full_projection_rebuild() -> tuple[object, ...]:
        raise AssertionError("fresh archived projection append must not rebuild all entries")

    monkeypatch.setattr(store, "_build_archived_operation_projection_entries", fail_if_full_projection_rebuild)

    def fail_if_full_snapshot_publish(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("fresh archived projection append must not rewrite the full projection snapshot")

    monkeypatch.setattr(
        store_operation_queries,
        "publish_archived_operations_by_id_snapshot",
        fail_if_full_snapshot_publish,
    )

    second = _begin_operation(
        store,
        task,
        handle_id="archived-projection-abort-second",
        kind="filesystem.write",
        operation_id="archived-projection-abort-second-id",
        operation_label="archived-projection-abort-second",
    )
    archive_ref = store.abort_operation(second, metadata={"reason": "second"})

    summaries = store.archived_operations(operation_id="archived-projection-abort-second-id")
    assert len(summaries) == 1
    assert summaries[0].carrier_ref == archive_ref


def test_discard_appends_multi_operation_projection_shards(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = store.fork(Store.GROUND_REF, "task-archived-projection-multishard-first")
    first_op = _begin_operation(
        store,
        first,
        handle_id="discarded-world-projection-multishard-first",
        kind="filesystem.write",
        operation_id="mm-discarded-world-projection-first-id",
        operation_label="discarded-world-projection-multishard-first",
    )
    store.finalize_operation(first_op, scope=first)
    store.discard(first)

    def fail_if_full_snapshot_publish(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("fresh archived projection append must not rewrite the full projection snapshot")

    monkeypatch.setattr(
        store_operation_queries,
        "publish_archived_operations_by_id_snapshot",
        fail_if_full_snapshot_publish,
    )

    second = store.fork(Store.GROUND_REF, "task-archived-projection-multishard-second")
    first_second_op = _begin_operation(
        store,
        second,
        handle_id="discarded-world-projection-aa",
        kind="filesystem.write",
        operation_id="aa-discarded-world-projection-id",
        operation_label="discarded-world-projection-aa",
    )
    store.finalize_operation(first_second_op, scope=second)
    second_second_op = _begin_operation(
        store,
        second,
        handle_id="discarded-world-projection-zz",
        kind="filesystem.write",
        operation_id="zz-discarded-world-projection-id",
        operation_label="discarded-world-projection-zz",
    )
    store.finalize_operation(second_second_op, scope=second)
    archive_ref = store.discard(second)

    snapshot = load_archived_operations_by_id_snapshot(store._repo)
    assert snapshot is not None
    assert snapshot.entries_by_id["aa-discarded-world-projection-id"].carrier_ref == archive_ref
    assert snapshot.entries_by_id["zz-discarded-world-projection-id"].carrier_ref == archive_ref


def test_archived_operation_lookup_handles_crowded_discarded_archives_without_rebuild(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = store.fork(Store.GROUND_REF, "task-crowded-discarded-history-target")
    target_op = _begin_operation(
        store,
        target,
        handle_id="old-discarded-op",
        kind="marker.runtime",
        operation_id="old-discarded-op-id",
        operation_label="old-discarded-op",
    )
    store.finalize_operation(target_op, scope=target)
    target_archive_ref = store.discard(target)

    def fail_if_full_projection_rebuild() -> tuple[object, ...]:
        raise AssertionError("crowded discarded archive updates must not rebuild all archived projection entries")

    def fail_if_full_snapshot_publish(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("crowded discarded archive updates must not rewrite the full projection snapshot")

    monkeypatch.setattr(store, "_build_archived_operation_projection_entries", fail_if_full_projection_rebuild)
    monkeypatch.setattr(
        store_operation_queries,
        "publish_archived_operations_by_id_snapshot",
        fail_if_full_snapshot_publish,
    )

    for idx in range(205):
        task = store.fork(Store.GROUND_REF, f"task-crowded-discarded-history-{idx}")
        op = _begin_operation(
            store,
            task,
            handle_id=f"newer-discarded-op-{idx}",
            kind="marker.runtime",
            operation_id=f"newer-discarded-op-{idx}",
            operation_label=f"newer-discarded-op-{idx}",
        )
        store.finalize_operation(op, scope=task)
        store.discard(task)

    summaries = store.archived_operations(operation_id="old-discarded-op-id")

    assert len(summaries) == 1
    assert summaries[0].operation_id == "old-discarded-op-id"
    assert summaries[0].carrier_ref == target_archive_ref
    assert summaries[0].archived_via == "discarded_world_ref"


def test_discard_skips_membership_scan_when_cache_absent(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = store.fork(Store.GROUND_REF, "task-archived-membership-cache-skip-scan")

    op = _begin_operation(
        store,
        task,
        handle_id="discarded-world-cache-skip-scan",
        kind="filesystem.write",
        operation_id="discarded-world-cache-skip-scan-id",
        operation_label="discarded-world-cache-skip-scan",
    )
    store.finalize_operation(op, scope=task)

    called = {"count": 0}
    original_exact_ids = store._exact_operation_ids_on_committed_carrier

    def wrapped_exact_ids(ref: str) -> frozenset[str]:
        called["count"] += 1
        return original_exact_ids(ref)

    monkeypatch.setattr(store, "_exact_operation_ids_on_committed_carrier", wrapped_exact_ids)

    store.discard(task)

    assert called["count"] == 0


def test_prune_archives_invalidates_archived_membership_cache(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = store.fork(Store.GROUND_REF, "task-archived-membership-cache-prune")

    op = _begin_operation(
        store,
        task,
        handle_id="discarded-world-cache-prune",
        kind="filesystem.write",
        operation_id="discarded-world-cache-prune-id",
        operation_label="discarded-world-cache-prune",
    )
    store.finalize_operation(op, scope=task)
    store.discard(task)

    monkeypatch.setattr(
        store,
        "_validated_projected_archived_operation_summary",
        lambda *, operation_id, world_id: None,
    )

    assert not store.operation_id_exists("missing-before-prune")

    store.prune_archives(keep_recent=0)

    build_calls = {"count": 0}
    original_build = store._build_archived_operation_membership_cache

    def wrapped_build() -> object:
        build_calls["count"] += 1
        return original_build()

    monkeypatch.setattr(store, "_build_archived_operation_membership_cache", wrapped_build)

    assert not store.operation_id_exists("missing-after-prune")
    assert build_calls["count"] == 1


def test_build_archived_membership_cache_uses_exact_carrier_enumeration(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = store.fork(Store.GROUND_REF, "task-archived-membership-cache-exact-build")

    op = _begin_operation(
        store,
        task,
        handle_id="discarded-world-cache-exact-build",
        kind="filesystem.write",
        operation_id="discarded-world-cache-exact-build-id",
        operation_label="discarded-world-cache-exact-build",
    )
    store.finalize_operation(op, scope=task)
    store.discard(task)

    exact_calls = {"count": 0}
    original_exact_ids = store._exact_operation_ids_on_committed_carrier

    def wrapped_exact_ids(ref: str) -> frozenset[str]:
        exact_calls["count"] += 1
        return original_exact_ids(ref)

    def fail_if_capped_archived_summary(*args, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("visibility") == "archived":
            raise AssertionError("archived membership cache build must not use capped archived summaries")
        return original_summaries(*args, **kwargs)

    original_summaries = store._summaries_from_committed_carrier
    monkeypatch.setattr(store, "_exact_operation_ids_on_committed_carrier", wrapped_exact_ids)
    monkeypatch.setattr(store, "_summaries_from_committed_carrier", fail_if_capped_archived_summary)

    cache = store._build_archived_operation_membership_cache()

    assert "discarded-world-cache-exact-build-id" in cache.archived_operation_ids
    assert exact_calls["count"] >= 1


def test_archived_recovery_operations_ignore_discarded_world_history_before_cap(store: Store) -> None:
    failed_task = store.fork(Store.GROUND_REF, "task-archived-recovery")
    failed = _begin_operation(
        store,
        failed_task,
        handle_id="archived-recovery-op",
        kind="filesystem.write",
        operation_id="archived-recovery-op-id",
        operation_label="archived-recovery-op",
    )
    store.append_operation_effect(
        failed,
        "FileCreate",
        {"path": "failed.txt"},
        workspace_changes=[("failed.txt", b"failed")],
        substrate="filesystem",
    )
    store.abort_operation(failed, metadata={"reason": "boom"})

    for idx in range(60):
        task = store.fork(Store.GROUND_REF, f"task-clean-discard-{idx}")
        op = _begin_operation(
            store,
            task,
            handle_id=f"clean-discard-op-{idx}",
            kind="filesystem.write",
            operation_id=f"clean-discard-op-id-{idx}",
            operation_label=f"clean-discard-op-{idx}",
        )
        store.finalize_operation(op, scope=task)
        store.discard(task)

    summaries = store.archived_recovery_operations(max_count=20)

    assert len(summaries) == 1
    assert summaries[0].operation_id == "archived-recovery-op-id"
    assert summaries[0].archived_via == "operation_ref"


@pytest.mark.parametrize(
    ("path", "match"),
    [
        (("mg", "world", "id"), "mg.world.id"),
        (("mg", "world", "ref"), "mg.world.ref"),
        (("mg", "world", "instance_id"), "mg.world.instance_id"),
        (("mg", "operation", "kind"), "mg.operation.kind"),
    ],
)
def test_archived_operations_reject_missing_projected_world_fields(
    store: Store,
    path: tuple[str, ...],
    match: str,
) -> None:
    task = store.fork(Store.GROUND_REF, f"task-archived-missing-{'-'.join(path[1:])}")
    archive_ref = _write_projected_operation_history(
        store,
        task,
        operation_id=f"archived-{'-'.join(path[1:])}",
        final_phase="aborted",
        missing_path=path,
    )

    with pytest.raises(InvalidRepositoryStateError, match=match):
        store.read_operation_history(archive_ref)
    with pytest.raises(InvalidRepositoryStateError, match=match):
        store.archived_operations()


def test_archived_operations_reject_invalid_projected_world_ref_as_repository_state(
    store: Store,
) -> None:
    task = store.fork(Store.GROUND_REF, "task-archived-invalid-world-ref")
    archive_ref = _write_projected_operation_history(
        store,
        task,
        operation_id="archived-invalid-world-ref",
        final_phase="aborted",
        invalid_path=("mg", "world", "ref"),
        invalid_value="bogus-ref",
    )

    with pytest.raises(InvalidRepositoryStateError, match=r"invalid mg\.world\.ref 'bogus-ref'"):
        store.read_operation_history(archive_ref)
    with pytest.raises(InvalidRepositoryStateError, match=r"invalid mg\.world\.ref 'bogus-ref'"):
        store.archived_operations()


def test_visible_parent_history_excludes_promoted_child_commits(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-visible-nested")

    parent = _begin_operation(
        store,
        task,
        handle_id="parent",
        kind="marker.runtime",
        operation_id="parent-op",
        operation_label="parent",
    )
    store.append_operation_effect(parent, "Marker", {"label": "parent-1"}, substrate="marker")

    child = _begin_operation(
        store,
        task,
        handle_id="child",
        kind="marker.runtime",
        parent_op_ref=parent.ref,
        operation_id="child-op",
        operation_label="child",
    )
    store.append_operation_effect(child, "Marker", {"label": "child-1"}, substrate="marker")
    store.finalize_operation(child)

    store.append_operation_effect(parent, "Marker", {"label": "parent-2"}, substrate="marker")
    store.finalize_operation(parent, scope=task)

    parent_history = store.read_visible_operation_history(task.ref, operation_id="parent-op")
    child_history = store.read_visible_operation_history(task.ref, operation_id="child-op")

    assert [(entry.metadata["type"], entry.metadata.get("label")) for entry in parent_history.commits] == [
        ("OperationCompleted", None),
        ("Marker", "parent-2"),
        ("Marker", "parent-1"),
        ("OperationStarted", None),
    ]
    assert [(entry.metadata["type"], entry.metadata.get("label")) for entry in child_history.commits] == [
        ("OperationCompleted", None),
        ("Marker", "child-1"),
        ("OperationStarted", None),
    ]


def test_open_parent_history_excludes_promoted_child_commits(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-open-nested")

    parent = _begin_operation(
        store,
        task,
        handle_id="parent",
        kind="marker.runtime",
        operation_id="parent-op",
        operation_label="parent",
    )
    store.append_operation_effect(parent, "Marker", {"label": "parent-1"}, substrate="marker")

    child = _begin_operation(
        store,
        task,
        handle_id="child",
        kind="marker.runtime",
        parent_op_ref=parent.ref,
        operation_id="child-op",
        operation_label="child",
    )
    store.append_operation_effect(child, "Marker", {"label": "child-1"}, substrate="marker")
    store.finalize_operation(child)

    parent_history = store.read_operation_history(parent.ref)

    assert [(entry.metadata["type"], entry.metadata.get("label")) for entry in parent_history.commits] == [
        ("Marker", "parent-1"),
        ("OperationStarted", None),
    ]


def test_read_operation_history_rejects_pointer_metadata_with_bad_seq(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-pointer-bad-seq")
    op = _begin_operation(
        store,
        task,
        handle_id="pointer-bad-seq",
        kind="marker.runtime",
        operation_id="pointer-bad-seq",
        operation_label="pointer-bad-seq",
    )
    start_oid = store.log(ref=op.ref, max_count=1)[0].oid
    store._emit_effect_to_ref(
        op.ref,
        scope_name=task.name,
        effect_type="Marker",
        metadata={
            **_pointer_metadata(
                task,
                operation_id="pointer-bad-seq",
                phase="effect",
                seq=3,
                prev_oid=start_oid,
                effect_count=1,
                handle_id="pointer-bad-seq",
                label="pointer-bad-seq",
            ),
        },
        substrate="marker",
        author_name=task.name,
    )

    with pytest.raises(InvalidRepositoryStateError, match="non-contiguous seq"):
        store.read_operation_history(op.ref)


def test_visible_operations_derive_status_from_pointer_phase_and_result(store: Store) -> None:
    task = store.fork(Store.GROUND_REF, "task-pointer-derived-status")
    operation_id = "pointer-derived-status"
    handle_id = "pointer-status"
    label = "Pointer Status"
    op = _begin_operation(
        store,
        task,
        handle_id=handle_id,
        kind="marker.runtime",
        operation_id=operation_id,
        operation_label=label,
    )
    store.append_operation_effect(
        op,
        "Marker",
        {"label": label},
        substrate="marker",
    )
    store.finalize_operation(
        op,
        scope=task,
        status="error",
        metadata={},
    )

    visible = store.visible_operations(ref=task.ref)
    assert len(visible) == 1
    assert visible[0].status == "error"

    history = store.read_visible_operation_history(task.ref, operation_id=operation_id)
    assert history.summary.status == "error"
