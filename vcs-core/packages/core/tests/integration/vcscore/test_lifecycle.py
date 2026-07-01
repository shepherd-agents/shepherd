"""VcsCore lifecycle and coordination integration tests."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import pygit2
import pytest
import vcs_core.vcscore as vcscore_module
from vcs_core._errors import (
    InvalidIdentityError,
    InvalidRepositoryStateError,
    OpenScopeError,
    ScopeAdmissionError,
    UnknownForkHintError,
    WorkspaceAuthorityRecoveryRequiredError,
)
from vcs_core._workspace_adoption import adopt_workspace_baseline
from vcs_core._world_operation_runner import WorldOperationResult, WorldOperationRunner
from vcs_core.scope_stack import ScopeStack
from vcs_core.vcscore import VcsCore

from ...support.builders import make_marker_filesystem_vcscore, make_store
from ...support.overlays import MockOverlayBackend


def _workspace_revision_payload(mg: VcsCore, head: str) -> dict[str, object]:
    manager = mg._world_storage()
    repo = manager.store("store_workspace").repo
    commit = repo[head]
    blob = repo[commit.tree["revision.json"].id]
    payload = json.loads(bytes(blob.data).decode("utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_fork_merge_lifecycle(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-test")
    assert task.name == "task-test"

    result = mg.merge(task, mg.ground)
    assert result == "task-test"


def test_fork_accepts_dict_hints_for_compat(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-dict-hints", hints={"isolated": False})
    assert task.name == "task-dict-hints"
    mg.discard(task)


def test_fork_rejects_misspelled_hint_key(mg: VcsCore) -> None:
    with pytest.raises(UnknownForkHintError) as excinfo:
        mg.fork(mg.ground, "task-typo", hints={"isoalted": True})
    assert "isoalted" in str(excinfo.value)
    assert "isolated" in str(excinfo.value)


def test_fork_rejects_restore_dunder_from_public_mapping(mg: VcsCore) -> None:
    with pytest.raises(UnknownForkHintError):
        mg.fork(mg.ground, "task-dunder", hints={"isolated": True, "__restore__": True})


def test_branch_rejects_unknown_hint_keys_at_substrate_boundary(mg: VcsCore) -> None:
    filesystem = next(s for s in mg.lifecycle_substrates if getattr(s, "name", None) == "filesystem")
    with pytest.raises(UnknownForkHintError) as excinfo:
        filesystem.branch("scope-bad-hint", parent_scope=mg.ground, hints={"isolated": True, "mount": "/tmp"})
    message = str(excinfo.value)
    assert "mount" in message
    assert "isolated" in message
    assert "__restore__" in message


def test_fork_discard_lifecycle(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-discard")
    result = mg.discard(task)
    assert result == "task-discard"


def test_nested_fork_merge(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-nested")
    tool = mg.fork(task, "tool-0")
    mg.merge(tool, task)
    mg.merge(task, mg.ground)

    log = mg.log()
    assert len(log) > 2


def test_filesystem_command_selects_workspace_capture_reduction_candidate(mg: VcsCore) -> None:
    # T2c: ``mg.exec("filesystem", "write", ...)`` now produces a
    # ``workspace-capture-reduction`` candidate with
    # ``ingress_kind="reduce"``. Pre-T2c this was misclassified as
    # ``workspace-scan`` with ``ingress_kind="command"`` because the
    # runtime layer hard-coded ``driver_command="scan"`` regardless of
    # whether the writes were observed (capture) or declared (command).
    task = mg.fork(mg.ground, "task-workspace-capture")

    mg.exec("filesystem", "write", scope=task, path="scan.txt", content=b"scan")

    manager = mg._world_storage()
    selected_world = manager.read_world(task.ref)
    selected_head = selected_world.snapshot.head_for("workspace").head
    provenance = manager.store("store_workspace").validate_prepared_candidate(
        selected_head,
        evidence_resolver=manager.world_store.resolve_evidence_ref,
    )
    payload = _workspace_revision_payload(mg, selected_head)
    assert provenance.transition.semantic_op == "workspace-capture-reduction"
    assert provenance.transition.ingress_kind == "reduce"
    assert payload["state_manifest"]["entries"] == [
        {
            "path": "scan.txt",
            "state": "present",
            "mode": 0o100644,
            "content_digest": f"sha256:{hashlib.sha256(b'scan').hexdigest()}",
        }
    ]

    mg.discard(task)


def test_filesystem_command_v2_selection_failure_requires_workspace_authority_recovery(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = mg.fork(mg.ground, "task-workspace-scan-recovery")
    manager = mg._world_storage()
    original_advance_publication = manager.advance_publication
    failed_once = False

    def fail_once_advance(prepared):
        nonlocal failed_once
        if prepared.plan.authority_ref == task.ref and not failed_once:
            failed_once = True
            return False
        return original_advance_publication(prepared)

    monkeypatch.setattr(manager, "advance_publication", fail_once_advance)

    with pytest.raises(InvalidRepositoryStateError, match="workspace selection"):
        mg.exec("filesystem", "write", scope=task, path="scan.txt", content=b"scan")

    assert mg.store.read_workspace_file(task.ref, "scan.txt") == b"scan"
    assert mg.list_workspace_authority_pending()
    assert task.ref not in manager.world_store.repo.references

    with pytest.raises(WorkspaceAuthorityRecoveryRequiredError) as runtime_exc:
        mg.exec("filesystem", "write", scope=task, path="blocked.txt", content=b"blocked")
    runtime_readiness = runtime_exc.value._vcscore_readiness_result  # type: ignore[attr-defined]
    assert runtime_readiness.request.command == "vcscore.runtime"
    assert any(
        "readiness_workspace_authority_pending" in issue_id
        for issue_id in runtime_exc.value._vcscore_readiness_issue_ids  # type: ignore[attr-defined]
    )

    with pytest.raises(WorkspaceAuthorityRecoveryRequiredError) as lifecycle_exc:
        mg.fork(task, "task-workspace-scan-recovery-blocked-child")
    lifecycle_readiness = lifecycle_exc.value._vcscore_readiness_result  # type: ignore[attr-defined]
    assert lifecycle_readiness.request.command == "vcscore.lifecycle"

    monkeypatch.setattr(manager, "advance_publication", original_advance_publication)
    readiness_requests = []
    original_query_readiness = mg.query_readiness

    def record_query_readiness(request=None):
        if request is not None and request.command == "vcscore.recover":
            readiness_requests.append(request)
        return original_query_readiness(request)

    monkeypatch.setattr(mg, "query_readiness", record_query_readiness)
    recovered = mg.recover_workspace_authority()

    assert readiness_requests
    assert readiness_requests[-1].targets
    target_domains = {target.domain for target in readiness_requests[-1].targets}
    assert "workspace_authority" in target_domains
    assert "operation_journal" in target_domains
    assert recovered
    assert mg.list_workspace_authority_pending() == ()
    selected_world = manager.read_world(task.ref)
    selected_head = selected_world.snapshot.head_for("workspace").head
    provenance = manager.store("store_workspace").validate_prepared_candidate(
        selected_head,
        evidence_resolver=manager.world_store.resolve_evidence_ref,
    )
    assert provenance.transition.semantic_op == "workspace-scan"

    mg.discard(task)


def test_filesystem_command_post_publication_journal_failure_clears_workspace_authority(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # T2c: Python-tier filesystem writes now route through the
    # python-runtime-capture flow (prior prefix was "wv_scan_"); the
    # workspace-driver operation id prefix encodes the driver command,
    # which is now "python-runtime-capture" for these effects. The
    # selector helper transforms the command via "-".replace("_") so
    # the prefix is "wv_python_runtime_capture_".
    task = mg.fork(mg.ground, "task-workspace-python-runtime-journal-recovery")
    manager = mg._world_storage()
    original_record_published = manager.record_operation_published
    failed_once = False
    failed_operation_id: str | None = None

    def fail_once_record_published(operation_id: str, *, world_oid: str):
        nonlocal failed_once, failed_operation_id
        if operation_id.startswith("wv_python_runtime_capture_") and not failed_once:
            failed_once = True
            failed_operation_id = operation_id
            raise RuntimeError("synthetic post-publication journal failure")
        return original_record_published(operation_id, world_oid=world_oid)

    monkeypatch.setattr(manager, "record_operation_published", fail_once_record_published)

    mg.exec("filesystem", "write", scope=task, path="scan.txt", content=b"scan")

    assert failed_operation_id is not None
    assert mg.list_workspace_authority_pending() == ()
    selected_world = manager.read_world(task.ref)
    selected_head = selected_world.snapshot.head_for("workspace").head
    payload = _workspace_revision_payload(mg, selected_head)
    assert payload["state_manifest"]["entries"] == [
        {
            "path": "scan.txt",
            "state": "present",
            "mode": 0o100644,
            "content_digest": f"sha256:{hashlib.sha256(b'scan').hexdigest()}",
        }
    ]
    assert manager.read_operation_journal(failed_operation_id, family="closed").tip.payload["status"] == "closed"

    mg.discard(task)


def test_workspace_authority_recovery_clears_lingering_published_pending_record(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = mg.fork(mg.ground, "task-workspace-published-pending")
    original_clear = vcscore_module.clear_pending_workspace_authority

    def suppress_clear(repo_path: str, operation_id: str) -> None:
        del repo_path, operation_id

    monkeypatch.setattr(vcscore_module, "clear_pending_workspace_authority", suppress_clear)
    mg._select_workspace_state_from_store_required(
        scope=task,
        operation_id="wv_scan_lingering_published",
        source_operation_id="op_lingering_published",
        driver_command="scan",
        message="workspace scan: lingering published",
    )
    assert mg.list_workspace_authority_pending() == ("wv_scan_lingering_published",)

    monkeypatch.setattr(vcscore_module, "clear_pending_workspace_authority", original_clear)
    recovered = mg.recover_workspace_authority()

    assert recovered == ("wv_scan_lingering_published",)
    assert mg.list_workspace_authority_pending() == ()
    mg.discard(task)


def test_overlay_merge_selects_workspace_overlay_candidate(tmp_path: Path) -> None:
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
    )
    try:
        task = mg.fork(mg.ground, "task-workspace-overlay", hints={"isolated": True})
        backend.write_file(task.name, "overlay.txt", b"overlay")

        mg.merge(task, mg.ground)

        manager = mg._world_storage()
        selected_world = manager.read_world(mg.ground.ref)
        selected_head = selected_world.snapshot.head_for("workspace").head
        provenance = manager.store("store_workspace").validate_prepared_candidate(
            selected_head,
            evidence_resolver=manager.world_store.resolve_evidence_ref,
        )
        payload = _workspace_revision_payload(mg, selected_head)
        assert provenance.transition.semantic_op == "workspace-overlay-merge"
        assert provenance.transition.ingress_kind == "merge"
        assert payload["state_manifest"]["entries"] == [
            {
                "path": "overlay.txt",
                "state": "present",
                "mode": 0o100644,
                "content_digest": f"sha256:{hashlib.sha256(b'overlay').hexdigest()}",
            }
        ]
    finally:
        mg.deactivate()


def test_workspace_adoption_v2_selection_failure_defers_materialized_until_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "base.txt").write_bytes(b"base")
    store = make_store(tmp_path)
    store.create_root_commit()
    original_publish = WorldOperationRunner.publish_prepared_world
    failed_once = False

    def fail_once_publish(self, prepared):
        nonlocal failed_once
        finalized = prepared.finalize()
        if finalized.operation_kind == "workspace-adoption-selection" and not failed_once:
            failed_once = True
            return WorldOperationResult(
                operation_id=finalized.operation_id,
                status="failed",
                world_oid=None,
                published=False,
                journal_family="open",
                error="synthetic adoption selection failure",
            )
        return original_publish(self, prepared)

    monkeypatch.setattr(WorldOperationRunner, "publish_prepared_world", fail_once_publish)

    with pytest.raises(InvalidRepositoryStateError, match="synthetic adoption selection failure"):
        adopt_workspace_baseline(store, tmp_path, source="worktree")

    assert store.read_workspace_file("refs/vcscore/ground", "base.txt") == b"base"
    assert store.status().local_changes == 1

    mg = VcsCore(str(tmp_path), store=store)
    recovered = mg.recover_workspace_authority()

    assert recovered
    assert mg.list_workspace_authority_pending() == ()
    assert store.status().local_changes == 0


def test_fork_rejects_second_live_child_for_parent(mg: VcsCore) -> None:
    first = mg.fork(mg.ground, "task-one")

    with pytest.raises(ScopeAdmissionError, match="already has live child scope 'task-one'"):
        mg.fork(mg.ground, "task-two")

    mg.discard(first)
    second = mg.fork(mg.ground, "task-two")
    mg.discard(second)


def test_on_merge_callback(mg: VcsCore) -> None:
    merged_names: list[str] = []
    mg.on_merge(lambda name: merged_names.append(name))

    task = mg.fork(mg.ground, "task-cb")
    mg.merge(task, mg.ground)

    assert merged_names == ["task-cb"]


def test_on_discard_callback(mg: VcsCore) -> None:
    discarded_names: list[str] = []
    mg.on_discard(lambda name: discarded_names.append(name))

    task = mg.fork(mg.ground, "task-dcb")
    mg.discard(task)

    assert discarded_names == ["task-dcb"]


def test_merge_completes_even_if_substrate_post_merge_notification_raises(mg: VcsCore, monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    fs = mg.lifecycle_substrates[1]

    def _boom(scope_name: str, parent_scope_name: str) -> None:
        del scope_name, parent_scope_name
        raise RuntimeError("merge notification boom")

    monkeypatch.setattr(fs, "on_scope_merged", _boom, raising=False)
    task = mg.fork(mg.ground, "task-notify-merge")

    with caplog.at_level(logging.WARNING):
        result = mg.merge(task, mg.ground)

    assert result == "task-notify-merge"
    assert task.name not in mg._active_scopes
    assert mg._pipeline.context.world is None
    assert "post-merge notification" in caplog.text


def test_discard_completes_even_if_substrate_post_discard_notification_raises(mg: VcsCore, monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    fs = mg.lifecycle_substrates[1]

    def _boom(scope_name: str) -> None:
        del scope_name
        raise RuntimeError("discard notification boom")

    monkeypatch.setattr(fs, "on_scope_discarded", _boom, raising=False)
    task = mg.fork(mg.ground, "task-notify-discard")

    with caplog.at_level(logging.WARNING):
        result = mg.discard(task)

    assert result == "task-notify-discard"
    assert task.name not in mg._active_scopes
    assert mg._pipeline.context.world is None
    assert "post-discard notification" in caplog.text


def test_on_merge_callback_runs_under_patch_manager_guard(mg: VcsCore, workspace: Path) -> None:
    task = mg.fork(mg.ground, "task-cb-guard-merge")
    victim = workspace / "merge-callback-victim.txt"
    victim.write_text("x")
    mg.on_merge(lambda _name: victim.unlink())
    mg.merge(task, mg.ground)

    assert not victim.exists()


def test_on_discard_callback_runs_under_patch_manager_guard(mg: VcsCore, workspace: Path) -> None:
    task = mg.fork(mg.ground, "task-cb-guard-discard")
    victim = workspace / "discard-callback-victim.txt"
    victim.write_text("x")
    mg.on_discard(lambda _name: victim.unlink())
    mg.discard(task)

    assert not victim.exists()


# ---------------------------------------------------------------------------
# Runtime execution-context lifecycle invariant
# ---------------------------------------------------------------------------


def test_pipeline_scope_cleared_after_outermost_merge(mg: VcsCore) -> None:
    assert mg._pipeline.context.world is None, "fresh activate: no ambient scope"
    task = mg.fork(mg.ground, "task-merge-clear")
    assert mg._pipeline.context.world is task
    mg.merge(task, mg.ground)
    assert mg._pipeline.context.world is None, "outermost merge pops back to None"


def test_pipeline_scope_cleared_after_outermost_discard(mg: VcsCore) -> None:
    assert mg._pipeline.context.world is None
    task = mg.fork(mg.ground, "task-discard-clear")
    mg.discard(task)
    assert mg._pipeline.context.world is None, "outermost discard pops back to None"


def test_pipeline_scope_nested_fork_merge_stack(mg: VcsCore) -> None:
    # Context follows explicit parentage instead of a hidden restoration stack.
    assert mg._pipeline.context.world is None
    task = mg.fork(mg.ground, "task-nest")
    assert mg._pipeline.context.world is task
    tool = mg.fork(task, "tool-nest")
    assert mg._pipeline.context.world is tool
    mg.merge(tool, task)
    assert mg._pipeline.context.world is task, "inner merge pops to the outer scope"
    mg.merge(task, mg.ground)
    assert mg._pipeline.context.world is None, "outermost merge clears the stack"


def test_pipeline_scope_fork_failure_restores_previous(mg: VcsCore) -> None:
    # If fork fails, the explicit runtime context must be restored.
    # Duplicate name triggers the failure path.
    first = mg.fork(mg.ground, "task-dup")
    # We're now inside "task-dup". Attempt a second fork with the same name.
    previous = mg._pipeline.context.world
    with pytest.raises(Exception):
        mg.fork(mg.ground, "task-dup")
    assert mg._pipeline.context.world is previous, "failed fork must restore stack"
    mg.merge(first, mg.ground)


def test_push_rejects_with_open_scope(mg: VcsCore) -> None:
    mg.fork(mg.ground, "task-open")
    with pytest.raises(OpenScopeError):
        mg.push()


def test_push_up_to_raises_not_implemented(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-upto")
    mg.merge(task, mg.ground)
    with pytest.raises(NotImplementedError, match="Phase-gated push"):
        mg.push(up_to="auto")


def test_push_dry_run(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-push")
    fs = mg.lifecycle_substrates[1]
    fs.record_changes([("test.py", b"hello")])  # type: ignore[attr-defined]
    mg.merge(task, mg.ground)

    plan = mg.push(dry_run=True)
    assert plan.total_operations > 0
    assert mg.status().commits_ahead > 0


def test_push_advances_materialized(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-push2")
    mg.merge(task, mg.ground)

    assert mg.status().commits_ahead > 0
    mg.push()
    assert mg.status().commits_ahead == 0


def test_status_delegates_to_store(mg: VcsCore) -> None:
    status = mg.status()
    assert status.commits_ahead == 0
    assert status.local_changes == 0


def test_ground_property(mg: VcsCore) -> None:
    assert mg.ground.name == "ground"
    assert mg.ground.ref == "refs/vcscore/ground"
    assert mg.ground.world_id is not None


def test_ground_world_id_is_repo_stable_across_sessions(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    try:
        ground1 = m1.ground
    finally:
        m1.deactivate()

    m2 = VcsCore(str(workspace))
    m2.activate()
    try:
        ground2 = m2.ground
    finally:
        m2.deactivate()

    assert ground1.instance_id != ground2.instance_id
    assert ground1.world_id is not None
    assert ground1.world_id == ground2.world_id


def test_activate_rejects_malformed_identity_json(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    try:
        identity_path = workspace / ".vcscore" / "identity.json"
        original = identity_path.read_text()
    finally:
        m1.deactivate()

    identity_path.write_text("{not-json")

    m2 = VcsCore(str(workspace))
    with pytest.raises(InvalidIdentityError, match="activation refused to preserve durable identity"):
        m2.activate()

    assert identity_path.read_text() == "{not-json"
    assert original != "{not-json"


def test_activate_rejects_unsupported_identity_version(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    try:
        identity_path = workspace / ".vcscore" / "identity.json"
    finally:
        m1.deactivate()

    identity_path.write_text('{"version": 999, "ground_world_id": "world_existing"}')

    m2 = VcsCore(str(workspace))
    with pytest.raises(InvalidIdentityError, match="unsupported identity version"):
        m2.activate()

    assert identity_path.read_text() == '{"version": 999, "ground_world_id": "world_existing"}'


def test_activate_rejects_missing_execution_history_epoch(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    try:
        identity_path = workspace / ".vcscore" / "identity.json"
    finally:
        m1.deactivate()

    identity_path.write_text('{"version": 1, "ground_world_id": "world_existing"}')

    m2 = VcsCore(str(workspace))
    with pytest.raises(InvalidIdentityError, match="unsupported execution history epoch"):
        m2.activate()

    assert identity_path.read_text() == '{"version": 1, "ground_world_id": "world_existing"}'


def test_pure_open_rejects_missing_identity_without_recreating_it(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    try:
        identity_path = workspace / ".vcscore" / "identity.json"
    finally:
        m1.deactivate()

    identity_path.unlink()
    assert not identity_path.exists()

    m2 = VcsCore.from_config(str(workspace))
    with pytest.raises(InvalidIdentityError, match="is missing; activation refused to preserve durable identity"):
        m2.activate()

    assert not identity_path.exists()


def test_pure_open_rejects_incompatible_control_plane_epoch(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    try:
        identity_path = workspace / ".vcscore" / "identity.json"
        payload = json.loads(identity_path.read_text())
    finally:
        m1.deactivate()

    payload["control_plane_epoch"] = 999
    identity_path.write_text(json.dumps(payload))

    m2 = VcsCore.from_config(str(workspace))
    with pytest.raises(InvalidIdentityError, match="unsupported control plane epoch"):
        m2.activate()

    assert json.loads(identity_path.read_text())["control_plane_epoch"] == 999


def test_activate_rejects_missing_scope_registry_projection(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    try:
        m1.store._repo.references.delete("refs/vcscore/projections/scope-registry/current")
    finally:
        m1.deactivate()

    m2 = VcsCore.from_config(str(workspace))
    with pytest.raises(InvalidRepositoryStateError, match="Scope registry projection is missing"):
        m2.activate()


def test_activate_rejects_pre_cutover_open_operation_ref(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    task = m1.fork(m1.ground, "task-legacy-open-op")
    base_oid = m1.log(ref=task.ref, max_count=1)[0].oid
    legacy_ref = "refs/vcscore/ops/legacy-open-op"
    m1.store._repo.references.create(legacy_ref, m1.store._repo.references[task.ref].peel(pygit2.Commit).id)
    m1.store._emit_effect_to_ref(
        legacy_ref,
        scope_name=task.name,
        effect_type="OperationStarted",
        metadata={
            "op_id": "legacy-open-op",
            "operation_id": "legacy-open-op-id",
            "kind": "marker.runtime",
            "scope_ref": task.ref,
            "scope_instance_id": task.instance_id,
            "base_oid": base_oid,
        },
        substrate="vcscore",
        author_name=task.name,
    )
    m1.deactivate()

    m2 = VcsCore(str(workspace))
    with pytest.raises(InvalidRepositoryStateError, match="Unsupported pre-cutover execution history"):
        m2.activate()


def test_ground_not_activated() -> None:
    m = VcsCore("/tmp/nonexistent")
    with pytest.raises(RuntimeError, match="not activated"):
        _ = m.ground


def test_scope_stack_convenience(mg: VcsCore) -> None:
    ss = ScopeStack(mg)
    assert ss.depth == 0

    ss.begin_scope("task-ss")
    assert ss.depth == 1
    assert ss.current.name == "task-ss"

    ss.begin_scope("tool-0")
    assert ss.depth == 2

    ss.commit_scope()
    assert ss.depth == 1

    ss.commit_scope()
    assert ss.depth == 0
    assert ss.current == mg.ground


def test_scope_stack_rollback(mg: VcsCore) -> None:
    ss = ScopeStack(mg)
    ss.begin_scope("task-rb")
    ss.begin_scope("tool-fail")
    ss.rollback_scope()
    assert ss.depth == 1

    ss.begin_scope("tool-retry")
    ss.commit_scope()
    ss.commit_scope()
    assert ss.depth == 0


def test_deactivate(mg: VcsCore) -> None:
    mg.deactivate()
    with pytest.raises(RuntimeError):
        _ = mg.ground
    assert mg._pipeline.context.world is None


def test_deactivate_resets_open_operation_span(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-open-op-deactivate")
    with mg._lock, mg._scoped(task):
        op = mg._pipeline.begin_operation(handle_id="open-op", kind="test.operation", scope=task)

    assert mg._pipeline.current_operation() == op

    mg.deactivate()

    with pytest.raises(RuntimeError):
        _ = mg.ground
    assert mg._pipeline.context.world is None
    assert mg._pipeline.current_operation() is None


def test_deactivate_clears_pipeline_scope_stack_and_allows_name_reuse(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-reuse-after-deactivate")
    assert mg._pipeline.execution_context is not None
    assert mg._pipeline.execution_context.scope_name == task.name

    mg.deactivate()

    assert mg._pipeline.execution_context is None
    mg.activate()

    assert mg.archive_orphaned_scopes() == [task.name]
    retried = mg.fork(mg.ground, task.name)
    assert retried.name == task.name
    mg.discard(retried)


def test_clear_restored_scope_state_forgets_rehydrated_scopes(workspace: Path, caplog) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    task = m1.fork(m1.ground, "task-restored")
    m1.deactivate()

    m2 = VcsCore(str(workspace))
    m2.activate(defer_orphan_detection=True)
    restored = m2.restore_scope(
        name=task.name,
        ref=task.ref,
        instance_id=task.instance_id,
        creation_oid=task.creation_oid,
        world_id=task.world_id,
        parent=m2.ground,
    )

    assert restored.name in m2._active_scopes
    assert restored.name in m2._restored_scopes

    m2.clear_restored_scope_state()

    assert restored.name not in m2._active_scopes
    assert restored.name not in m2._scope_parents
    assert restored.name not in m2._restored_scopes
    # Clearing restored state leaves no ambient execution context. Ground is
    # not parked as a default.
    assert m2._pipeline.context.world is None
    assert m2.store.ref_exists(task.ref)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        m2.deactivate()

    assert not any("open scope" in record.message.lower() for record in caplog.records)


def test_restore_scope_rejects_missing_world_id_for_non_ground(workspace: Path) -> None:
    m1 = VcsCore(str(workspace))
    m1.activate()
    task = m1.fork(m1.ground, "task-missing-world-id")
    m1.deactivate()

    m2 = VcsCore(str(workspace))
    m2.activate(defer_orphan_detection=True)
    try:
        with pytest.raises(ValueError, match="missing durable world_id"):
            m2.restore_scope(
                name=task.name,
                ref=task.ref,
                instance_id=task.instance_id,
                creation_oid=task.creation_oid,
                parent=m2.ground,
            )
    finally:
        m2.deactivate()


def test_recording_only_scope(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-rec")
    tool = mg.fork(task, "tool-rec", hints={"isolated": False})

    marker = mg.lifecycle_substrates[0]
    marker.mark("ToolCallStarted", {"tool": "search"})  # type: ignore[attr-defined]

    mg.merge(tool, task)
    mg.merge(task, mg.ground)

    results = mg.filter_effects(effect_type="Marker")
    assert len(results) >= 1
    assert results[0].metadata.get("label") == "ToolCallStarted"


def test_substrate_effect_interleaving_workspace_tree_lag(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-interleave")
    marker = mg.lifecycle_substrates[0]
    fs = mg.lifecycle_substrates[1]

    marker.mark("ToolCallStarted", {"tool": "edit"})  # type: ignore[attr-defined]
    fs.record_changes([("interleave.py", b"content")])  # type: ignore[attr-defined]
    marker.mark("ToolCallCompleted", {"tool": "edit"})  # type: ignore[attr-defined]

    mg.merge(task, mg.ground)

    log = mg.log(max_count=20)
    effect_types = [e.metadata.get("type") for e in log]
    assert "Marker" in effect_types
    assert "FileCreate" in effect_types or "FilePatch" in effect_types
    assert "ScopeMerge" in effect_types

    diff = mg.diff()
    assert any(f.path == "interleave.py" for f in diff.files)


def test_store_file_exists_in_workspace(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-exists")
    fs = mg.lifecycle_substrates[1]

    assert not mg.store.file_exists_in_workspace(task.ref, "new_file.py")

    fs.record_changes([("new_file.py", b"content")])  # type: ignore[attr-defined]
    assert mg.store.file_exists_in_workspace(task.ref, "new_file.py")

    fs.record_changes([("src/nested/deep.py", b"deep")])  # type: ignore[attr-defined]
    assert mg.store.file_exists_in_workspace(task.ref, "src/nested/deep.py")
    assert not mg.store.file_exists_in_workspace(task.ref, "src/nested/missing.py")

    mg.merge(task, mg.ground)


def test_hierarchical_branch_zoom(mg: VcsCore) -> None:
    marker = mg.lifecycle_substrates[0]
    fs = mg.lifecycle_substrates[1]

    task = mg.fork(mg.ground, "task-zoom")
    marker.mark("TaskStarted", {"task": "zoom"})  # type: ignore[attr-defined]

    step = mg.fork(task, "step-analyze")
    marker.mark("StepStarted", {"step": "analyze"})  # type: ignore[attr-defined]

    tool = mg.fork(step, "tool-edit")
    marker.mark("ToolCallStarted", {"tool": "edit"})  # type: ignore[attr-defined]
    fs.record_changes([("zoom.py", b"content")])  # type: ignore[attr-defined]
    marker.mark("ToolCallCompleted", {"tool": "edit"})  # type: ignore[attr-defined]
    mg.merge(tool, step)

    marker.mark("StepCompleted", {"step": "analyze"})  # type: ignore[attr-defined]
    mg.merge(step, task)

    marker.mark("TaskCompleted", {"task": "zoom"})  # type: ignore[attr-defined]
    mg.merge(task, mg.ground)

    all_effects = mg.log(max_count=100)
    labels = [e.metadata.get("label") for e in all_effects if e.metadata.get("type") == "Marker"]
    assert "TaskStarted" in labels
    assert "StepStarted" in labels
    assert "ToolCallStarted" in labels
    assert "ToolCallCompleted" in labels
    assert "StepCompleted" in labels
    assert "TaskCompleted" in labels

    file_effects = mg.filter_effects(substrate="filesystem")
    assert any(e.metadata.get("path") == "zoom.py" for e in file_effects)

    tool_effects = mg.filter_effects(scope="tool-edit")
    tool_labels = [e.metadata.get("label") for e in tool_effects if e.metadata.get("type") == "Marker"]
    assert "ToolCallStarted" in tool_labels
    assert "ToolCallCompleted" in tool_labels
    assert "TaskStarted" not in tool_labels
    assert "StepStarted" not in tool_labels

    merges = mg.filter_effects(effect_type="ScopeMerge")
    assert len(merges) >= 3


def test_per_file_substrate_decomposition(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task-decomp")
    fs = mg.lifecycle_substrates[1]

    oids = fs.record_changes(
        [  # type: ignore[attr-defined]
            ("src/a.py", b"aaa"),
            ("src/b.py", b"bbb"),
            ("src/c.py", b"ccc"),
        ]
    )
    assert len(oids) == 3

    mg.merge(task, mg.ground)

    file_creates = mg.filter_effects(effect_type="FileCreate")
    created_paths = {e.metadata.get("path") for e in file_creates}
    assert "src/a.py" in created_paths
    assert "src/b.py" in created_paths
    assert "src/c.py" in created_paths

    for e in file_creates:
        if e.metadata.get("path") in created_paths:
            assert e.metadata.get("substrate") == "filesystem"

    merges = mg.filter_effects(effect_type="ScopeMerge")
    assert any(e.metadata.get("merged_into") == "ground" for e in merges)

    log = mg.log(max_count=100)
    assert len(log) >= 5


def test_context_manager_activates_and_deactivates(workspace: Path) -> None:
    m = VcsCore(str(workspace))
    with m as mg:
        assert mg.ground.name == "ground"
        task = mg.fork(mg.ground, "task-ctx")
        mg.merge(task, mg.ground)
    with pytest.raises(RuntimeError, match="not activated"):
        _ = m.ground


def test_context_manager_deactivates_on_exception(workspace: Path) -> None:
    m = VcsCore(str(workspace))
    with pytest.raises(ValueError, match="intentional"), m:
        raise ValueError("intentional")
    with pytest.raises(RuntimeError, match="not activated"):
        _ = m.ground
