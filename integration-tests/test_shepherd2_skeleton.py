"""skeleton cross-package integration tests."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pygit2
import pytest
import vcs_core._retained_output_selection as selection_module
from shepherd2.kernel.facts import TRUSTED_APPEND_CONTEXT, TRUSTED_READ_CONTEXT, AppendBatch, AppendGroup, FactDraft
from shepherd2.schemas.execution import (
    create_execution_batch,
    execution_completed,
    project_execution_from_store,
    publish_execution_frontier,
)
from shepherd2.schemas.run_outputs import (
    RUN_OUTPUT_DESCRIPTOR_SCHEMA,
    RunOutputDescriptorLocator,
    project_run_output_descriptor_payloads,
    project_run_output_descriptors,
    run_output_descriptor_fact,
    run_output_descriptor_locator_from_payload,
    run_output_descriptor_locator_payload,
)
from shepherd2.trace_store import SQLiteTraceStore, TraceStoreError
from shepherd2.vnext import skeleton
from shepherd_dialect.workspace_control import RunOutputCitationRef, RunOutputResolver
from shepherd_dialect.workspace_control import TraceRef as WorkspaceTraceRef
from vcs_core import Store, VcsCore, build_builtin_substrate_context
from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._world_substrate_adapters import TaskTraceSubstrateDriver
from vcs_core.git_store import create_or_update_reference
from vcs_core.substrates import FilesystemSubstrate, MarkerSubstrate

if TYPE_CHECKING:
    from pathlib import Path


def _make_mg(root: Path, *, activate: bool = True) -> VcsCore:
    root.mkdir(parents=True, exist_ok=True)
    store = Store(str(root / ".vcscore"))
    context = build_builtin_substrate_context(store, workspace=root, config={})
    mg = VcsCore(
        str(root),
        substrates=[
            MarkerSubstrate(context),
            FilesystemSubstrate(context),
            TaskTraceSubstrateDriver(),
        ],
        store=store,
    )
    if activate:
        mg.activate()
    return mg


def _manual_workspace_output_payload(
    mg: VcsCore,
    session: skeleton.Session,
    *,
    run_id: str,
    child_name: str,
    content: bytes,
    parent: Any | None = None,
) -> tuple[skeleton.TraceRef, dict[str, object]]:
    parent_scope = mg.ground if parent is None else parent
    child = mg.fork(parent_scope, child_name)
    mg.exec("filesystem", "write", scope=child, path="candidate.txt", content=content)
    seal_result = mg.seal(child, output_binding="workspace")
    retained_handle = mg.retained_workspace_handle(child.name)
    trace_ids = skeleton.Session._trace_ids(run_id)
    return trace_ids, skeleton._output_payload(
        parent=parent_scope,
        child=child,
        handoff=seal_result.handoff,
        retained_handle=retained_handle,
        trace_ref=trace_ids,
    )


def _append_manual_execution_create(session: skeleton.Session, trace_ids: skeleton.TraceRef) -> str:
    receipt = session.trace_store.append(
        TRUSTED_APPEND_CONTEXT,
        create_execution_batch(
            append_intent_id=f"manual:{trace_ids.run_id}:create",
            execution_id=trace_ids.execution_id,
            task_ref="integration-tests.manual",
            inputs={},
        ),
    )
    return receipt.fact_ids[-1]


def _publish_manual_execution_frontier(
    session: skeleton.Session,
    trace_ids: skeleton.TraceRef,
    *,
    through_fact_id: str,
) -> None:
    publish_execution_frontier(
        session.trace_store,
        TRUSTED_APPEND_CONTEXT,
        frontier_id=trace_ids.frontier_id,
        target_execution_id=trace_ids.execution_id,
        through_fact_id=through_fact_id,
    )


def _workspace_control_citation(output: skeleton.RunOutput) -> RunOutputCitationRef:
    citation = output.citation
    locator = citation.descriptor_locator
    owner = citation.owner
    if locator is None or owner.kind != "run":
        raise AssertionError("workspace-control citation fixture requires a run-owned descriptor locator")
    assert owner.run_id is not None
    assert owner.execution_id is not None
    assert owner.frontier_id is not None
    return RunOutputCitationRef(
        output_name=citation.identity.output_name,
        output_id=citation.identity.output_id,
        trace_ref=WorkspaceTraceRef(
            run_id=owner.run_id,
            execution_id=owner.execution_id,
            frontier_id=owner.frontier_id,
        ),
        descriptor_locator=run_output_descriptor_locator_payload(locator),
        binding=citation.identity.binding,
        store_id=citation.store_id,
        resource_id=citation.resource_id,
        materialization_kind=citation.descriptor.materialization_kind,
        custody_ref=citation.identity.handoff_ref,
        output_world_oid=citation.identity.output_world_oid,
        parent_basis_world_oid=citation.parent_basis_world_oid,
    )


def _fix_bug(repo: skeleton.GitRepoHandle, issue: str) -> skeleton.GitRepoHandle:
    return repo.write("candidate.txt", f"selected candidate: {issue}\n".encode())


def _trace_payload(frontier: str, *, run_ref: str = "run_trace_output") -> dict[str, object]:
    return {
        "trace_runtime": "shepherd.trace.provider-neutral.v1",
        "trace_owner_id": f"task:{run_ref}",
        "frontier_id": frontier,
        "run_ref": run_ref,
        "identity_domain": "vcscore.canonical.v2",
        "events": [{"id": "e1", "kind": "run.lifecycle", "transition": "finished"}],
        "causal_edges": [],
        "owner_paths": {f"task:{run_ref}": ["e1"]},
    }


def _return_stale_handle(repo: skeleton.GitRepoHandle) -> skeleton.GitRepoHandle:
    repo.write("candidate.txt", b"written but stale handle returned\n")
    return repo


def _raise_task_error(repo: skeleton.GitRepoHandle) -> skeleton.GitRepoHandle:
    raise RuntimeError("simulated task failure")


def _return_invalid_output(repo: skeleton.GitRepoHandle) -> object:
    return "not a repo handle"


def _workspace_head(mg: VcsCore, world_oid: str) -> str:
    return mg._world_storage().read_world(world_oid).snapshot.head_for("workspace").head


def _execution_status(session: skeleton.Session, run_id: str) -> str:
    cutoff = session.trace_store.read_owner_cutoff(f"frontier:skeleton:{run_id}:terminal")
    execution = project_execution_from_store(session.trace_store, TRUSTED_READ_CONTEXT, cutoff)
    return execution.status


def _scope_status(mg: VcsCore, scope_name: str) -> str:
    entry = mg.store.scope_registry_entry(scope_name)
    assert entry is not None
    return entry.status


class _AppendFailingTraceStore:
    def __init__(self, inner: SQLiteTraceStore, *, fail_on_append: int) -> None:
        self._inner = inner
        self._fail_on_append = fail_on_append
        self.append_count = 0

    def append(self, *args: Any, **kwargs: Any) -> Any:
        self.append_count += 1
        if self.append_count == self._fail_on_append:
            raise RuntimeError("simulated trace append failure")
        return self._inner.append(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.fixture(autouse=True)
def _enable_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(skeleton.SKELETON_ENV, "1")
    monkeypatch.setenv(skeleton.SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv(skeleton.NESTED_OPERATIONS_ENV, "1")


def test_skeleton_v0_selects_one_retained_workspace_output(tmp_path: Path) -> None:
    """V0 selects one retained workspace output without scalar checkout mutation."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "parent")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(parent)

        run = session.run_child(
            parent=parent,
            task=_fix_bug,
            args=(repo, "v0"),
            run_id="v0",
            child_name="child",
        )
        out = run.outputs["workspace"]
        parent_scalar_before = mg.store.read_workspace_file(parent.ref, "candidate.txt")

        assert out.read_file("candidate.txt") == (b"selected candidate: v0\n", 0o100644)
        assert run.trace_ref.frontier_id == "frontier:skeleton:v0:terminal"
        assert run.execution.status == "succeeded"
        assert run.execution.outputs["workspace"]["handoff_ref"] == out.handoff_ref
        assert out.identity.binding == "workspace"
        assert out.identity.parent_scope_name == parent.name
        assert out.identity.parent_scope_instance_id == parent.instance_id
        assert out.identity.handoff_ref == out.handoff_ref
        assert out.owner.kind == "run"
        assert out.owner.run_id == "v0"
        assert out.owner.frontier_id == run.trace_ref.frontier_id
        assert out.ref.identity == out.identity
        assert out.ref.owner == out.owner
        assert out.ref.state == "unconsumed"
        assert out.ref.changed_paths == ("candidate.txt",)
        trace_slice = session.trace_store.resolve_frontier(TRUSTED_READ_CONTEXT, run.trace_ref.frontier_id)
        descriptor_records = project_run_output_descriptors(
            trace_slice,
            run.trace_ref.execution_id,
            frontier_id=run.trace_ref.frontier_id,
        )
        descriptors = project_run_output_descriptor_payloads(trace_slice, run.trace_ref.execution_id)
        descriptor_facts = [
            fact
            for fact in trace_slice.visible_facts_by_id.values()
            if getattr(fact.envelope, "schema_ref", None) == RUN_OUTPUT_DESCRIPTOR_SCHEMA
        ]
        assert tuple(descriptors) == ("workspace",)
        assert descriptors["workspace"] == run.execution.outputs["workspace"]
        assert descriptor_records["workspace"].locator == out.citation.descriptor_locator
        assert out.ref.descriptor_locator == out.citation.descriptor_locator
        assert out.citation.descriptor_locator is not None
        assert out.citation.descriptor_locator.frontier_id == run.trace_ref.frontier_id
        assert len(descriptor_facts) == 1
        locator_payload = run_output_descriptor_locator_payload(out.citation.descriptor_locator)
        resolved_out = session.resolve_run_output(run_output_descriptor_locator_from_payload(locator_payload))
        assert resolved_out.handoff_ref == out.handoff_ref
        assert resolved_out.identity == out.identity
        assert resolved_out.owner == out.owner
        assert resolved_out.citation.descriptor_locator == out.citation.descriptor_locator
        assert resolved_out.ref == out.ref
        loaded_out = session.load_run(run.trace_ref).outputs["workspace"]
        assert loaded_out.handoff_ref == out.handoff_ref
        assert loaded_out.identity == out.identity
        assert loaded_out.owner == out.owner
        assert loaded_out.citation.descriptor_locator == out.citation.descriptor_locator
        assert loaded_out.ref == out.ref

        selection = out.select()

        assert selection.settlement.action == "selected"
        assert selection.parent_world_after == mg.world_oid(parent)
        assert _workspace_head(mg, selection.parent_world_after) == selection.settlement.candidate_head
        assert mg.store.read_workspace_file(parent.ref, "candidate.txt") == parent_scalar_before
        assert mg.store.scope_registry_entry("child", status="retained") is not None
        assert out.ref.state == "selected"
        assert out.ref.settlement_ref == selection.settlement.settlement_ref

        with pytest.raises(InvalidRepositoryStateError, match="already settled"):
            out.select()
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_public_boundary_manual_best_of_three(tmp_path: Path) -> None:
    """A product-shaped boundary can manually settle a best-of-3 without cohort policy."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent_world_before = mg.world_oid(mg.ground)
        assert parent_world_before is not None
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(mg.ground)
        workspace = session.workspace(mg.ground)
        runs = [
            session.run_child(
                parent=mg.ground,
                task=_fix_bug,
                args=(repo, f"multi-{index}"),
                run_id=f"multi-{index}",
                child_name=f"multi-child-{index}",
            )
            for index in range(3)
        ]
        outputs = [run.outputs["workspace"] for run in runs]

        before = {row.output.scope_name: row for row in session.list_retained_outputs(parent=mg.ground)}
        assert {row.state for row in before.values()} == {"unconsumed"}
        assert before["multi-child-1"].output.read_file("candidate.txt") == (
            b"selected candidate: multi-1\n",
            0o100644,
        )
        assert [output.owner.run_id for output in outputs] == ["multi-0", "multi-1", "multi-2"]
        assert len({output.identity.output_id for output in outputs}) == 3
        assert {output.identity.parent_ref for output in outputs} == {mg.ground.ref}
        assert {output.identity.binding for output in outputs} == {"workspace"}
        assert {output.ref.state for output in outputs} == {"unconsumed"}

        selected = workspace.select(outputs[1])
        released = workspace.release(outputs[0])
        discarded = workspace.discard(outputs[2])

        assert selected.settlement.action == "selected"
        assert released.settlement.action == "released"
        assert discarded.settlement.action == "discarded"
        assert selected.parent_world_before == parent_world_before
        assert selected.parent_world_after == mg.world_oid(mg.ground)
        assert _workspace_head(mg, selected.parent_world_after) == selected.settlement.candidate_head
        assert runs[0].outputs["workspace"].read_file("candidate.txt") == (
            b"selected candidate: multi-0\n",
            0o100644,
        )
        assert runs[2].outputs["workspace"].read_file("candidate.txt") == (
            b"selected candidate: multi-2\n",
            0o100644,
        )

        after = {row.output.scope_name: row for row in session.list_retained_outputs(parent=mg.ground)}
        assert after["multi-child-0"].state == "released"
        assert after["multi-child-1"].state == "selected"
        assert after["multi-child-2"].state == "discarded"
        assert after["multi-child-0"].ref.state == "released"
        assert after["multi-child-1"].ref.state == "selected"
        assert after["multi-child-2"].ref.state == "discarded"
        assert after["multi-child-0"].settlement == released.settlement
        assert after["multi-child-1"].settlement == selected.settlement
        assert after["multi-child-2"].settlement == discarded.settlement
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_workspace_boundary_apply_selects_full_workspace_output(tmp_path: Path) -> None:
    """Workspace ``apply`` is a product spelling over the selected receipt for now."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(mg.ground)
        run = session.run_child(
            parent=mg.ground,
            task=_fix_bug,
            args=(repo, "apply"),
            run_id="apply",
            child_name="apply-child",
        )

        applied = session.workspace(mg.ground).apply(run.outputs["workspace"])

        assert applied.settlement.action == "selected"
        assert _workspace_head(mg, applied.parent_world_after) == applied.settlement.candidate_head
        with pytest.raises(InvalidRepositoryStateError, match="already settled"):
            session.select(run.outputs["workspace"])
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_rejects_stale_returned_handle_and_discards_child(tmp_path: Path) -> None:
    """A write-returning handle must be the value returned by the child task."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "stale-parent")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(parent)

        with pytest.raises(TypeError, match="stale GitRepoHandle"):
            session.run_child(
                parent=parent,
                task=_return_stale_handle,
                args=(repo,),
                run_id="stale",
                child_name="stale-child",
            )

        assert _execution_status(session, "stale") == "failed"
        assert _scope_status(mg, "stale-child") == "discarded"
        assert mg.lookup_scope("stale-child") is None
        assert mg.store.scope_registry_entry("stale-child", status="retained") is None
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_task_exception_discards_child_and_records_failure(tmp_path: Path) -> None:
    """A task body exception should not leave an unsealed live child behind."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "error-parent")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(parent)

        with pytest.raises(RuntimeError, match="simulated task failure"):
            session.run_child(
                parent=parent,
                task=_raise_task_error,
                args=(repo,),
                run_id="task-error",
                child_name="error-child",
            )

        assert _execution_status(session, "task-error") == "failed"
        assert _scope_status(mg, "error-child") == "discarded"
        assert mg.lookup_scope("error-child") is None
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_invalid_task_output_discards_child_and_records_failure(tmp_path: Path) -> None:
    """Validation failures before seal clean up the child scope."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "invalid-parent")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(parent)

        with pytest.raises(TypeError, match="must return a GitRepoHandle"):
            session.run_child(
                parent=parent,
                task=_return_invalid_output,
                args=(repo,),
                run_id="invalid-output",
                child_name="invalid-child",
            )

        assert _execution_status(session, "invalid-output") == "failed"
        assert _scope_status(mg, "invalid-child") == "discarded"
        assert mg.lookup_scope("invalid-child") is None
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_rejects_stale_input_handle_and_discards_child(tmp_path: Path) -> None:
    """Input handle lowering must not grant write authority from a stale parent basis."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "stale-input-parent")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(parent)
        mg.exec("filesystem", "write", scope=parent, path="parent.txt", content=b"parent\n")

        with pytest.raises(skeleton.SkeletonUnavailableError, match="stale basis"):
            session.run_child(
                parent=parent,
                task=_fix_bug,
                args=(repo, "stale-input"),
                run_id="stale-input",
                child_name="stale-input-child",
            )

        assert _scope_status(mg, "stale-input-child") == "discarded"
        assert mg.lookup_scope("stale-input-child") is None
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_rejects_forged_input_handle_and_discards_child(tmp_path: Path) -> None:
    """Input handle lowering must verify the full parent scope identity."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "forged-input-parent")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(parent)
        forged = replace(repo, scope_instance_id="forged-instance")

        with pytest.raises(skeleton.SkeletonUnavailableError, match="identity mismatch"):
            session.run_child(
                parent=parent,
                task=_fix_bug,
                args=(forged, "forged-input"),
                run_id="forged-input",
                child_name="forged-input-child",
            )

        assert _scope_status(mg, "forged-input-child") == "discarded"
        assert mg.lookup_scope("forged-input-child") is None
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_trace_create_failure_discards_child(tmp_path: Path) -> None:
    """A trace-store failure after child fork must not strand a live child."""
    mg = _make_mg(tmp_path)
    trace_store = _AppendFailingTraceStore(SQLiteTraceStore(tmp_path / "trace.sqlite"), fail_on_append=1)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "trace-failure-parent")
        session = skeleton.Session(mg, trace_store=trace_store)  # type: ignore[arg-type]
        repo = session.workspace_repo(parent)

        with pytest.raises(RuntimeError, match="simulated trace append failure"):
            session.run_child(
                parent=parent,
                task=_fix_bug,
                args=(repo, "trace-failure"),
                run_id="trace-failure",
                child_name="trace-failure-child",
            )

        assert _scope_status(mg, "trace-failure-child") == "discarded"
        assert mg.lookup_scope("trace-failure-child") is None
    finally:
        trace_store.close()
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_post_seal_trace_failure_leaves_auditable_retained_orphan(tmp_path: Path) -> None:
    """If trace completion fails after seal, custody remains in vcs-core but no CompletedRun is exposed."""
    mg = _make_mg(tmp_path)
    trace_store = _AppendFailingTraceStore(SQLiteTraceStore(tmp_path / "trace.sqlite"), fail_on_append=2)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "post-seal-trace-parent")
        session = skeleton.Session(mg, trace_store=trace_store)  # type: ignore[arg-type]
        repo = session.workspace_repo(parent)

        with pytest.raises(RuntimeError, match="simulated trace append failure"):
            session.run_child(
                parent=parent,
                task=_fix_bug,
                args=(repo, "post-seal-trace"),
                run_id="post-seal-trace",
                child_name="post-seal-trace-child",
            )

        assert _execution_status(session, "post-seal-trace") == "failed"
        assert _scope_status(mg, "post-seal-trace-child") == "retained"
        assert mg.read_retained_workspace_file("post-seal-trace-child", "candidate.txt") == (
            b"selected candidate: post-seal-trace\n",
            0o100644,
        )
        trace_ids = skeleton.Session._trace_ids("post-seal-trace")
        trace_slice = session.trace_store.resolve_frontier(TRUSTED_READ_CONTEXT, trace_ids.frontier_id)
        assert project_run_output_descriptor_payloads(trace_slice, trace_ids.execution_id) == {}
        retained_outputs = session.list_run_outputs(parent=parent, binding="workspace")
        assert len(retained_outputs) == 1
        assert retained_outputs[0].state == "unconsumed"
        assert retained_outputs[0].output.owner.kind == "retained-query"
        assert retained_outputs[0].output.citation.descriptor_locator is None
        with pytest.raises(skeleton.SkeletonUnavailableError, match="not completed successfully: failed"):
            session.load_run("post-seal-trace")
    finally:
        trace_store.close()
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_frontier_publish_failure_leaves_descriptor_trace_without_completed_frontier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If frontier publication fails after terminal trace append, no CompletedRun is exposed by frontier."""
    mg = _make_mg(tmp_path)
    trace_store = SQLiteTraceStore(tmp_path / "trace.sqlite")
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "frontier-failure-parent")
        session = skeleton.Session(mg, trace_store=trace_store)
        repo = session.workspace_repo(parent)

        def fail_publish_frontier(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("simulated frontier publish failure")

        monkeypatch.setattr(trace_store, "publish_frontier", fail_publish_frontier)

        with pytest.raises(RuntimeError, match="simulated frontier publish failure"):
            session.run_child(
                parent=parent,
                task=_fix_bug,
                args=(repo, "frontier-failure"),
                run_id="frontier-failure",
                child_name="frontier-failure-child",
            )

        trace_ids = skeleton.Session._trace_ids("frontier-failure")
        owner_prefix = trace_store.read_owner_prefix(TRUSTED_READ_CONTEXT, trace_ids.execution_id, 99)
        descriptors = project_run_output_descriptor_payloads(owner_prefix, trace_ids.execution_id)
        retained_outputs = session.list_run_outputs(parent=parent, binding="workspace")

        assert tuple(descriptors) == ("workspace",)
        assert len(retained_outputs) == 1
        assert retained_outputs[0].state == "unconsumed"
        with pytest.raises(TraceStoreError, match="unknown frontier id"):
            session.load_run(trace_ids)
    finally:
        trace_store.close()
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_parent_workspace_drift_fails_closed(tmp_path: Path) -> None:
    """Same-binding parent drift refuses selection through the skeleton facade."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "drift-parent")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(parent)
        run = session.run_child(
            parent=parent,
            task=_fix_bug,
            args=(repo, "drift"),
            run_id="drift",
            child_name="drift-child",
        )
        mg.exec("filesystem", "write", scope=parent, path="parent.txt", content=b"parent\n")
        parent_world_after_drift = mg.world_oid(parent)

        with pytest.raises(InvalidRepositoryStateError, match="binding 'workspace' advanced"):
            run.outputs["workspace"].select()

        assert mg.world_oid(parent) == parent_world_after_drift
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_v1a_rehydrates_after_reactivation(tmp_path: Path) -> None:
    """V1a reloads a CompletedRun from durable trace and retained custody."""
    trace_path = tmp_path / "trace.sqlite"
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=trace_path)
        repo = session.workspace_repo(mg.ground)
        run = session.run_child(
            parent=mg.ground,
            task=_fix_bug,
            args=(repo, "rehydrate"),
            run_id="rehydrate",
            child_name="rehydrate-child",
        )
        trace_ref = run.trace_ref
    finally:
        mg.deactivate(warn_on_open_scopes=False)

    fresh = _make_mg(tmp_path)
    try:
        session = skeleton.Session(fresh, trace_path=trace_path)
        loaded = session.load_run(trace_ref)

        assert loaded.outputs["workspace"].read_file("candidate.txt") == (
            b"selected candidate: rehydrate\n",
            0o100644,
        )
        selection = loaded.outputs["workspace"].select()

        assert selection.settlement.action == "selected"
        assert _workspace_head(fresh, selection.parent_world_after) == selection.settlement.candidate_head
    finally:
        fresh.deactivate(warn_on_open_scopes=False)


def test_skeleton_run_output_resolves_through_workspace_control_after_reactivation(tmp_path: Path) -> None:
    """Workspace-control RunOutput reconstruction shares 's durable ground identity."""
    trace_path = tmp_path / "trace.sqlite"
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=trace_path)
        repo = session.workspace_repo(mg.ground)
        run = session.run_child(
            parent=mg.ground,
            task=_fix_bug,
            args=(repo, "workspace-control"),
            run_id="workspace-control",
            child_name="workspace-control-child",
        )
        wc_citation = _workspace_control_citation(run.outputs["workspace"])
        resolved = RunOutputResolver(
            mg,
            descriptor_resolver=session._resolve_trace_output_descriptor,
        ).resolve((wc_citation,))[0]

        assert run.outputs["workspace"].identity.parent_scope_instance_id is None
        assert resolved == run.outputs["workspace"].ref
        trace_ref = run.trace_ref
    finally:
        mg.deactivate(warn_on_open_scopes=False)

    fresh = _make_mg(tmp_path)
    try:
        session = skeleton.Session(fresh, trace_path=trace_path)
        loaded_output = session.load_run(trace_ref).outputs["workspace"]
        resolved = RunOutputResolver(
            fresh,
            descriptor_resolver=session._resolve_trace_output_descriptor,
        ).resolve((wc_citation,))[0]

        assert loaded_output.identity.parent_scope_instance_id is None
        assert resolved == loaded_output.ref
    finally:
        fresh.deactivate(warn_on_open_scopes=False)


def test_skeleton_v1a_rehydrates_non_ground_parent_after_reactivation(tmp_path: Path) -> None:
    """V1a restores a live non-ground parent before selecting a retained output."""
    trace_path = tmp_path / "trace.sqlite"
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "nonground-parent")
        session = skeleton.Session(mg, trace_path=trace_path)
        repo = session.workspace_repo(parent)
        run = session.run_child(
            parent=parent,
            task=_fix_bug,
            args=(repo, "non-ground"),
            run_id="non-ground",
            child_name="nonground-child",
        )
        wc_citation = _workspace_control_citation(run.outputs["workspace"])
        parent_instance_id = parent.instance_id
        trace_ref = run.trace_ref
    finally:
        mg.deactivate(warn_on_open_scopes=False)

    fresh = _make_mg(tmp_path)
    try:
        session = skeleton.Session(fresh, trace_path=trace_path)
        loaded = session.load_run(trace_ref)
        resolved = RunOutputResolver(
            fresh,
            descriptor_resolver=session._resolve_trace_output_descriptor,
        ).resolve((wc_citation,))[0]

        assert fresh.lookup_scope("nonground-parent") is None
        assert loaded.outputs["workspace"].identity.parent_scope_instance_id == parent_instance_id
        assert resolved == loaded.outputs["workspace"].ref
        selection = loaded.outputs["workspace"].select()

        restored_parent = fresh.lookup_scope("nonground-parent")
        assert restored_parent is not None
        assert selection.settlement.action == "selected"
        assert selection.parent_world_after == fresh.world_oid(restored_parent)
        assert _workspace_head(fresh, selection.parent_world_after) == selection.settlement.candidate_head
    finally:
        fresh.deactivate(warn_on_open_scopes=False)


@pytest.mark.parametrize(
    ("method_name", "action", "blocked_method"),
    [
        ("release", "released", "discard"),
        ("discard", "discarded", "release"),
    ],
)
def test_skeleton_v1a_rehydrated_output_can_release_or_discard(
    tmp_path: Path,
    method_name: str,
    action: str,
    blocked_method: str,
) -> None:
    """Rehydrated RunOutput wrappers delegate release/discard to vcs-core settlement receipts."""
    trace_path = tmp_path / "trace.sqlite"
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=trace_path)
        repo = session.workspace_repo(mg.ground)
        run = session.run_child(
            parent=mg.ground,
            task=_fix_bug,
            args=(repo, method_name),
            run_id=f"{method_name}-rehydrate",
            child_name=f"{method_name}-child",
        )
        trace_ref = run.trace_ref
    finally:
        mg.deactivate(warn_on_open_scopes=False)

    fresh = _make_mg(tmp_path)
    try:
        session = skeleton.Session(fresh, trace_path=trace_path)
        output = session.load_run(trace_ref).outputs["workspace"]
        parent_world_before = fresh.world_oid(fresh.ground)

        settlement_result = getattr(output, method_name)()

        assert settlement_result.settlement.action == action
        assert settlement_result.parent_world_before == parent_world_before
        assert settlement_result.parent_world_after == parent_world_before
        assert output.read_file("candidate.txt") == (
            f"selected candidate: {method_name}\n".encode(),
            0o100644,
        )
        with pytest.raises(InvalidRepositoryStateError, match="already settled"):
            output.select()
        with pytest.raises(InvalidRepositoryStateError, match="already settled"):
            getattr(output, blocked_method)()
    finally:
        fresh.deactivate(warn_on_open_scopes=False)


def test_skeleton_v1b_query_rehydrates_run_output_from_custody(tmp_path: Path) -> None:
    """The provisional query facade wraps VcsCore-retained custody, not trace-owned state."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(mg.ground)
        run = session.run_child(
            parent=mg.ground,
            task=_fix_bug,
            args=(repo, "query"),
            run_id="query",
            child_name="query-child",
        )

        rows = session.list_run_outputs(parent=mg.ground)
        retained_rows = session.list_retained_outputs(parent=mg.ground)

        assert len(rows) == 1
        assert [row.ref for row in retained_rows] == [row.ref for row in rows]
        assert rows[0].state == "unconsumed"
        assert rows[0].ref.state == "unconsumed"
        assert rows[0].settlement is None
        assert rows[0].output.handoff_ref == run.outputs["workspace"].handoff_ref
        assert rows[0].output.identity == run.outputs["workspace"].identity
        assert rows[0].output.owner == skeleton.RunOutputOwner(kind="retained-query")
        assert rows[0].output.ref == rows[0].ref
        assert rows[0].output.citation.descriptor_locator is None
        assert rows[0].ref.descriptor_locator is None
        assert run.outputs["workspace"].citation.descriptor_locator is not None
        assert rows[0].output.citation.owner != run.outputs["workspace"].citation.owner
        assert rows[0].output.read_file("candidate.txt") == (b"selected candidate: query\n", 0o100644)
        assert session.list_run_outputs(parent=mg.ground, binding="backend") == ()

        selection = rows[0].output.select()
        selected = session.list_run_outputs(parent=mg.ground, state="selected")

        assert len(selected) == 1
        assert selected[0].state == "selected"
        assert selected[0].ref.state == "selected"
        assert selected[0].settlement == selection.settlement
        assert selected[0].output.handoff_ref == run.outputs["workspace"].handoff_ref
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_v1c_repo_handles_reject_non_workspace_bindings(tmp_path: Path) -> None:
    """GitRepoHandle writes are tree-backed workspace outputs, not arbitrary binding producers."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")

        with pytest.raises(skeleton.SkeletonUnavailableError, match="only supports binding='workspace'"):
            session.repo(mg.ground, binding="trace")
        with pytest.raises(skeleton.SkeletonUnavailableError, match="only supports binding='workspace'"):
            session.workspace_repo(mg.ground, binding="backend")
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_vcs_core_child_filesystem_capture_cannot_publish_non_workspace_binding(tmp_path: Path) -> None:
    """The lower bridge must not treat workspace bytes as arbitrary binding output."""
    mg = _make_mg(tmp_path)
    try:
        parent = mg.fork(mg.ground, "nonworkspace-output-parent")
        child = mg.fork(parent, "nonworkspace-output-child")

        with (
            mg.runtime_activity(
                scope=parent,
                operation_label="nonworkspace-output-parent",
                operation_kind="shepherd2.skeleton.parent",
                operation_id="nonworkspace-output-parent",
            ),
            pytest.raises(InvalidRepositoryStateError, match="filesystem runtime effects"),
        ):
            mg._execute_recorded_in_child_operation(
                "filesystem",
                "write",
                scope=child,
                operation_id="nonworkspace-output-child-write",
                operation_kind="shepherd2.skeleton.binding_write",
                workspace_output_binding="trace",
                path="candidate.txt",
                content=b"not trace output\n",
            )

        assert mg.store.read_workspace_file(child.ref, "candidate.txt") is None
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_v1c_query_and_settle_true_trace_output(tmp_path: Path) -> None:
    """The facade can query and settle a retained trace output produced by the trace substrate."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        child = mg.fork(mg.ground, "true-trace-child")
        trace_outcome = mg.exec(
            "trace",
            "append",
            scope=child,
            payload=_trace_payload("frontier:true-trace-child"),
        )
        seal_result = mg.seal(child, output_binding="trace")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")

        rows = session.list_run_outputs(parent=mg.ground, binding="trace")
        assert len(rows) == 1
        row = rows[0]
        out = row.output
        assert row.state == "unconsumed"
        assert out.binding == "trace"
        assert out.identity.output_name == "trace"
        assert out.descriptor.output_name == "trace"
        assert out.descriptor.world_binding == "trace"
        assert out.descriptor.store_id == "store_trace"
        assert out.descriptor.materialization_kind == "external"
        assert out.candidate_head == trace_outcome.oids[0]
        assert out.candidate_head == seal_result.handoff.candidate_head
        assert out.owner == skeleton.RunOutputOwner(kind="retained-query")
        assert out.citation.descriptor_locator is None
        assert row.ref.descriptor_locator is None
        assert out.read_file("candidate.txt") is None
        assert session.list_run_outputs(parent=mg.ground, binding="workspace") == ()

        selected = out.select()

        assert selected.settlement.binding == "trace"
        assert selected.settlement.store_id == "store_trace"
        assert selected.settlement.candidate_head == trace_outcome.oids[0]
        assert session.list_run_outputs(parent=mg.ground, binding="trace", state="selected")[0].state == "selected"
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_v1c_trace_output_selection_fails_when_trace_binding_advanced(tmp_path: Path) -> None:
    """Trace retained-output settlement uses the same target-binding freshness rule."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "trace-parent")
        child = mg.fork(parent, "stale-trace-child")
        mg.exec(
            "trace",
            "append",
            scope=child,
            payload=_trace_payload("frontier:stale-trace-child", run_ref="run_stale_trace_child"),
        )
        mg.seal(child, output_binding="trace")
        mg.exec(
            "trace",
            "append",
            scope=parent,
            payload=_trace_payload("frontier:trace-parent-advanced", run_ref="run_trace_parent"),
        )
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        out = session.list_run_outputs(parent=parent, binding="trace")[0].output

        with pytest.raises(InvalidRepositoryStateError, match="parent binding 'trace' advanced"):
            out.select()
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_v1b_query_fails_closed_on_invalid_retained_custody(tmp_path: Path) -> None:
    """The skeleton query must not skip or paper over lower-layer invalid rows."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(mg.ground)
        run = session.run_child(
            parent=mg.ground,
            task=_fix_bug,
            args=(repo, "invalid-query"),
            run_id="invalid-query",
            child_name="invalid-query-child",
        )
        mg.store._repo.references[run.outputs["workspace"].handoff_ref].delete()

        with pytest.raises(skeleton.SkeletonUnavailableError, match="invalid custody"):
            session.list_retained_outputs(parent=mg.ground)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_v1a_fails_closed_when_parent_is_no_longer_live(tmp_path: Path) -> None:
    """V1a must not expose a RunOutput when retained custody can no longer validate."""
    trace_path = tmp_path / "trace.sqlite"
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        parent = mg.fork(mg.ground, "discarded-parent")
        session = skeleton.Session(mg, trace_path=trace_path)
        repo = session.workspace_repo(parent)
        run = session.run_child(
            parent=parent,
            task=_fix_bug,
            args=(repo, "discarded-parent"),
            run_id="discarded-parent",
            child_name="discarded-parent-child",
        )
        trace_ref = run.trace_ref
        mg.discard(parent)
    finally:
        mg.deactivate(warn_on_open_scopes=False)

    fresh = _make_mg(tmp_path)
    try:
        session = skeleton.Session(fresh, trace_path=trace_path)

        with pytest.raises(skeleton.SkeletonUnavailableError, match="retained custody"):
            session.load_run(trace_ref)

        assert fresh.lookup_scope("discarded-parent") is None
    finally:
        fresh.deactivate(warn_on_open_scopes=False)


@pytest.mark.parametrize(
    ("field_name", "forged_value"),
    [
        ("materialization_kind", "external"),
        ("scope_instance_id", "forged-instance"),
        ("candidate_head", "forged-candidate-head"),
    ],
)
def test_skeleton_revalidates_trace_payload_against_retained_custody(
    tmp_path: Path,
    field_name: str,
    forged_value: str,
) -> None:
    """RunOutput rehydration treats shepherd2 trace metadata as a citation, not custody truth."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(mg.ground)
        run = session.run_child(
            parent=mg.ground,
            task=_fix_bug,
            args=(repo, "custody"),
            run_id=f"custody-{field_name}",
            child_name=f"custody-{field_name}",
        )
        forged_payload = dict(run.outputs["workspace"].metadata)
        forged_payload[field_name] = forged_value
        forged_output = skeleton.RunOutput(session, forged_payload)

        with pytest.raises(skeleton.SkeletonUnavailableError, match=field_name):
            _ = forged_output.metadata
        with pytest.raises(skeleton.SkeletonUnavailableError, match=field_name):
            forged_output.read_file("candidate.txt")
        with pytest.raises(skeleton.SkeletonUnavailableError, match=field_name):
            forged_output.select()
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_rejects_public_output_alias_until_enabled(tmp_path: Path) -> None:
    """The schema can represent aliases, but the public skeleton still exposes binding-named outputs only."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(mg.ground)
        run = session.run_child(
            parent=mg.ground,
            task=_fix_bug,
            args=(repo, "alias"),
            run_id="alias-disabled",
            child_name="alias-disabled-child",
        )
        forged_payload = dict(run.execution.outputs["workspace"])
        forged_payload["output_name"] = "patch"
        forged_execution = replace(run.execution, outputs={"patch": forged_payload})

        with pytest.raises(skeleton.SkeletonUnavailableError, match="output aliases are not enabled"):
            session._completed_run_from_execution(run.run_id, run.trace_ref, forged_execution)
        with pytest.raises(skeleton.SkeletonUnavailableError, match="output aliases are not enabled"):
            _ = skeleton.RunOutput(session, forged_payload).metadata
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_does_not_echo_forged_ground_parent_instance_metadata(tmp_path: Path) -> None:
    """Ground parent instance ids are activation-local, not durable custody truth."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(mg.ground)
        run = session.run_child(
            parent=mg.ground,
            task=_fix_bug,
            args=(repo, "ground-parent"),
            run_id="ground-parent",
            child_name="ground-parent-child",
        )
        forged_payload = dict(run.execution.outputs["workspace"])
        forged_payload["parent_scope_instance_id"] = "forged-ground-instance"
        forged_output = skeleton.RunOutput(session, forged_payload)

        assert "parent_scope_instance_id" not in forged_output.metadata
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_trace_descriptor_must_match_execution_output_mirror(tmp_path: Path) -> None:
    """CompletedRun construction joins trace-owned descriptors to their execution output mirror."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(mg.ground)
        run = session.run_child(
            parent=mg.ground,
            task=_fix_bug,
            args=(repo, "mirror"),
            run_id="mirror",
            child_name="mirror-child",
        )
        trace_slice = session.trace_store.resolve_frontier(TRUSTED_READ_CONTEXT, run.trace_ref.frontier_id)
        forged_payload = dict(run.execution.outputs["workspace"])
        forged_payload["candidate_ref"] = "refs/vcscore/candidates/forged"
        forged_execution = replace(run.execution, outputs={"workspace": forged_payload})

        with pytest.raises(
            skeleton.SkeletonUnavailableError, match="descriptor disagrees with execution output mirror"
        ):
            session._completed_run_from_execution(
                run.run_id,
                run.trace_ref,
                forged_execution,
                trace_slice=trace_slice,
            )
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_trace_descriptor_reconstructs_without_execution_output_mirror(tmp_path: Path) -> None:
    """Trace-owned descriptors are the primary source for CompletedRun outputs."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(mg.ground)
        run = session.run_child(
            parent=mg.ground,
            task=_fix_bug,
            args=(repo, "descriptor-primary"),
            run_id="descriptor-primary",
            child_name="descriptor-primary-child",
        )
        trace_slice = session.trace_store.resolve_frontier(TRUSTED_READ_CONTEXT, run.trace_ref.frontier_id)
        mirrorless_execution = replace(run.execution, outputs={})

        reconstructed = session._completed_run_from_execution(
            run.run_id,
            run.trace_ref,
            mirrorless_execution,
            trace_slice=trace_slice,
        )

        reconstructed_out = reconstructed.outputs["workspace"]
        assert reconstructed_out.handoff_ref == run.outputs["workspace"].handoff_ref
        assert reconstructed_out.citation.descriptor_locator == run.outputs["workspace"].citation.descriptor_locator
        assert reconstructed_out.ref.descriptor_locator == run.outputs["workspace"].ref.descriptor_locator
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_trace_descriptor_reconstructs_when_completion_mirror_is_empty(tmp_path: Path) -> None:
    """Trace-owned descriptor facts can reconstruct outputs even when execution completion mirrors no outputs."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        trace_ids, output_payload = _manual_workspace_output_payload(
            mg,
            session,
            run_id="descriptor-empty-mirror",
            child_name="descriptor-empty-mirror-child",
            content=b"selected candidate: descriptor-empty-mirror\n",
        )
        causal_tail = _append_manual_execution_create(session, trace_ids)
        terminal_receipt = session.trace_store.append(
            TRUSTED_APPEND_CONTEXT,
            AppendBatch(
                append_intent_id="manual:descriptor-empty-mirror:complete",
                groups=(
                    AppendGroup(
                        trace_owner_id=trace_ids.execution_id,
                        causal_parents=(causal_tail,),
                        fact_drafts=(
                            run_output_descriptor_fact(
                                execution_id=trace_ids.execution_id,
                                output_name="workspace",
                                world_binding="workspace",
                                citation=dict(output_payload),
                            ),
                            execution_completed(execution_id=trace_ids.execution_id, outputs={}),
                        ),
                    ),
                ),
            ),
        )
        _publish_manual_execution_frontier(session, trace_ids, through_fact_id=terminal_receipt.fact_ids[-1])

        loaded = session.load_run(trace_ids)
        out = loaded.outputs["workspace"]

        assert loaded.execution.outputs == {}
        assert out.read_file("candidate.txt") == (b"selected candidate: descriptor-empty-mirror\n", 0o100644)
        assert out.citation.descriptor_locator is not None
        assert out.citation.descriptor_locator.frontier_id == trace_ids.frontier_id
        resolved = session.resolve_run_output(out.citation.descriptor_locator)
        assert resolved.identity == out.identity
        assert resolved.handoff_ref == out.handoff_ref
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_execution_output_mirror_cannot_add_uncited_output(tmp_path: Path) -> None:
    """Execution.outputs may mirror descriptors, but cannot mint outputs on its own."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(mg.ground)
        run = session.run_child(
            parent=mg.ground,
            task=_fix_bug,
            args=(repo, "extra-mirror"),
            run_id="extra-mirror",
            child_name="extra-mirror-child",
        )
        trace_slice = session.trace_store.resolve_frontier(TRUSTED_READ_CONTEXT, run.trace_ref.frontier_id)
        forged_execution = replace(
            run.execution,
            outputs={
                "workspace": run.execution.outputs["workspace"],
                "uncited": run.execution.outputs["workspace"],
            },
        )

        with pytest.raises(skeleton.SkeletonUnavailableError, match="without trace-owned descriptors"):
            session._completed_run_from_execution(
                run.run_id,
                run.trace_ref,
                forged_execution,
                trace_slice=trace_slice,
            )
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_malformed_trace_descriptor_does_not_fall_back_to_execution_mirror(tmp_path: Path) -> None:
    """A bad trace-owned descriptor blocks CompletedRun construction even when Execution.outputs is valid."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        trace_ids, output_payload = _manual_workspace_output_payload(
            mg,
            session,
            run_id="malformed-descriptor",
            child_name="malformed-descriptor-child",
            content=b"selected candidate: malformed-descriptor\n",
        )
        causal_tail = _append_manual_execution_create(session, trace_ids)
        forged_citation = dict(output_payload)
        forged_citation["binding"] = "backend"
        terminal_receipt = session.trace_store.append(
            TRUSTED_APPEND_CONTEXT,
            AppendBatch(
                append_intent_id="manual:malformed-descriptor:complete",
                groups=(
                    AppendGroup(
                        trace_owner_id=trace_ids.execution_id,
                        causal_parents=(causal_tail,),
                        fact_drafts=(
                            FactDraft(
                                mode="capture",
                                schema_ref=RUN_OUTPUT_DESCRIPTOR_SCHEMA,
                                kind_label="run_output_descriptor",
                                payload={
                                    "execution_id": trace_ids.execution_id,
                                    "output_name": "workspace",
                                    "world_binding": "workspace",
                                    "citation": forged_citation,
                                },
                            ),
                            execution_completed(
                                execution_id=trace_ids.execution_id,
                                outputs={"workspace": output_payload},
                            ),
                        ),
                    ),
                ),
            ),
        )
        _publish_manual_execution_frontier(session, trace_ids, through_fact_id=terminal_receipt.fact_ids[-1])

        with pytest.raises(skeleton.SkeletonUnavailableError, match="descriptor projection failed"):
            session.load_run(trace_ids)
        with pytest.raises(skeleton.SkeletonUnavailableError, match="locator resolution failed"):
            session.resolve_run_output(
                RunOutputDescriptorLocator(
                    execution_id=trace_ids.execution_id,
                    output_name="workspace",
                    frontier_id=trace_ids.frontier_id,
                    descriptor_fact_id=terminal_receipt.fact_ids[0],
                )
            )
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_resolve_run_output_rejects_wrong_locator_before_custody(tmp_path: Path) -> None:
    """A RunOutput locator must resolve the exact trace-owned descriptor fact."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(mg.ground)
        run = session.run_child(
            parent=mg.ground,
            task=_fix_bug,
            args=(repo, "wrong-locator"),
            run_id="wrong-locator",
            child_name="wrong-locator-child",
        )
        locator = run.outputs["workspace"].citation.descriptor_locator
        assert locator is not None

        with pytest.raises(skeleton.SkeletonUnavailableError, match="locator resolution failed"):
            session.resolve_run_output(
                RunOutputDescriptorLocator(
                    execution_id=locator.execution_id,
                    output_name="patch",
                    frontier_id=locator.frontier_id,
                    descriptor_fact_id=locator.descriptor_fact_id,
                )
            )
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_duplicate_trace_descriptors_do_not_fall_back_to_execution_mirror(tmp_path: Path) -> None:
    """Duplicate trace-owned descriptors for one output fail closed even with a valid mirror."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        trace_ids, output_payload = _manual_workspace_output_payload(
            mg,
            session,
            run_id="duplicate-descriptor",
            child_name="duplicate-descriptor-child",
            content=b"selected candidate: duplicate-descriptor\n",
        )
        causal_tail = _append_manual_execution_create(session, trace_ids)
        descriptor = run_output_descriptor_fact(
            execution_id=trace_ids.execution_id,
            output_name="workspace",
            world_binding="workspace",
            citation=dict(output_payload),
        )
        terminal_receipt = session.trace_store.append(
            TRUSTED_APPEND_CONTEXT,
            AppendBatch(
                append_intent_id="manual:duplicate-descriptor:complete",
                groups=(
                    AppendGroup(
                        trace_owner_id=trace_ids.execution_id,
                        causal_parents=(causal_tail,),
                        fact_drafts=(
                            descriptor,
                            descriptor,
                            execution_completed(
                                execution_id=trace_ids.execution_id,
                                outputs={"workspace": output_payload},
                            ),
                        ),
                    ),
                ),
            ),
        )
        _publish_manual_execution_frontier(session, trace_ids, through_fact_id=terminal_receipt.fact_ids[-1])

        with pytest.raises(skeleton.SkeletonUnavailableError, match="descriptor projection failed"):
            session.load_run(trace_ids)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_load_run_fails_closed_when_retained_custody_is_missing(tmp_path: Path) -> None:
    """A trace descriptor citation is not enough to expose a RunOutput without retained custody."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(mg.ground)
        run = session.run_child(
            parent=mg.ground,
            task=_fix_bug,
            args=(repo, "missing-custody"),
            run_id="missing-custody",
            child_name="missing-custody-child",
        )
        mg.store._repo.references[run.outputs["workspace"].handoff_ref].delete()

        with pytest.raises(skeleton.SkeletonUnavailableError, match="retained custody"):
            session.load_run(run.trace_ref)
        locator = run.outputs["workspace"].citation.descriptor_locator
        assert locator is not None
        with pytest.raises(skeleton.SkeletonUnavailableError, match="retained custody"):
            session.resolve_run_output(locator)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_completed_run_construction_revalidates_trace_payload(tmp_path: Path) -> None:
    """CompletedRun construction must not expose a RunOutput backed by forged trace metadata."""
    mg = _make_mg(tmp_path)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(mg.ground)
        run = session.run_child(
            parent=mg.ground,
            task=_fix_bug,
            args=(repo, "construction"),
            run_id="construction",
            child_name="construction-child",
        )
        forged_payload = dict(run.execution.outputs["workspace"])
        forged_payload["candidate_ref"] = "refs/vcscore/candidates/forged"
        forged_execution = replace(run.execution, outputs={"workspace": forged_payload})

        with pytest.raises(skeleton.SkeletonUnavailableError, match="candidate_ref"):
            session._completed_run_from_execution(run.run_id, run.trace_ref, forged_execution)
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_v1a_recovers_missing_settlement_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """V1a exposes vcs-core's post-publication settlement recovery."""
    mg = _make_mg(tmp_path)
    original_write = selection_module.write_retained_output_settlement
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(mg.ground)
        run = session.run_child(
            parent=mg.ground,
            task=_fix_bug,
            args=(repo, "crash"),
            run_id="crash",
            child_name="crash-child",
        )
        failed = False

        def fail_once_write(*args: Any, **kwargs: Any) -> object:
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("simulated crash after retained output publication")
            return original_write(*args, **kwargs)

        monkeypatch.setattr(selection_module, "write_retained_output_settlement", fail_once_write)
        with pytest.raises(RuntimeError, match="simulated crash after retained output publication"):
            run.outputs["workspace"].select()
        parent_world_after_publication = mg.world_oid(mg.ground)
        assert parent_world_after_publication is not None

        monkeypatch.setattr(selection_module, "write_retained_output_settlement", original_write)
        recovered = session.load_run(run.trace_ref).outputs["workspace"].select()

        assert recovered.parent_world_after == parent_world_after_publication
        assert recovered.settlement.action == "selected"
        assert _workspace_head(mg, recovered.parent_world_after) == recovered.settlement.candidate_head
    finally:
        monkeypatch.setattr(selection_module, "write_retained_output_settlement", original_write)
        mg.deactivate(warn_on_open_scopes=False)


def test_skeleton_v1a_recovery_requires_parent_authority_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The facade must expose vcs-core's authority-protected recovery refusal."""
    mg = _make_mg(tmp_path)
    original_write = selection_module.write_retained_output_settlement
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
        session = skeleton.Session(mg, trace_path=tmp_path / "trace.sqlite")
        repo = session.workspace_repo(mg.ground)
        run = session.run_child(
            parent=mg.ground,
            task=_fix_bug,
            args=(repo, "authority"),
            run_id="authority",
            child_name="authority-child",
        )
        parent_world_before_publication = mg.world_oid(mg.ground)
        assert parent_world_before_publication is not None
        failed = False

        def fail_once_write(*args: Any, **kwargs: Any) -> object:
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("simulated crash after retained output publication")
            return original_write(*args, **kwargs)

        monkeypatch.setattr(selection_module, "write_retained_output_settlement", fail_once_write)
        with pytest.raises(RuntimeError, match="simulated crash after retained output publication"):
            run.outputs["workspace"].select()
        parent_world_after_publication = mg.world_oid(mg.ground)
        assert parent_world_after_publication is not None
        assert parent_world_after_publication != parent_world_before_publication

        manager = mg._world_storage()
        create_or_update_reference(
            manager.world_store.repo,
            mg.ground.ref,
            pygit2.Oid(hex=parent_world_before_publication),
            force=True,
        )
        monkeypatch.setattr(selection_module, "write_retained_output_settlement", original_write)

        with pytest.raises(InvalidRepositoryStateError, match="not protected by target ref"):
            session.load_run(run.trace_ref).outputs["workspace"].select()

        assert mg.world_oid(mg.ground) == parent_world_before_publication
    finally:
        monkeypatch.setattr(selection_module, "write_retained_output_settlement", original_write)
        mg.deactivate(warn_on_open_scopes=False)
