"""Integration tests for Python-tier capture under SPI v0.1 (T2c).

T2c rewired Python-tier capture from the pre-T2 scalar-``EffectRecord``
+ ``driver_command="scan"`` dispatch to a proper
``PythonRuntimeCaptureAdapter`` -> coordinator persist -> typed
``ReduceRequest`` -> ``workspace-capture-reduction`` flow. These tests
walk the production ``mg.exec("filesystem", "write", ...)`` path
end-to-end and assert the new classification.

Parent context:
``vcs-core/design/spikes/260515-world-vectors/260523-python-tier-push-admission/``
identified the push-admission failure as the visible symptom of the
pre-T2 mis-classification. T2c fixes the classification (this test
suite verifies); T4 fixes admission against the substrate-aware
reference set (separate test suite).
"""

from __future__ import annotations

import hashlib
import json

import pytest
from vcs_core._active_surface_profiles import permissive_active_surface, read_only_filesystem_surface
from vcs_core._substrate_driver import SurfacePolicyError
from vcs_core.types import ScopeInfo
from vcs_core.vcscore import VcsCore


def _content_digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _workspace_revision_payload(mg: VcsCore, head: str) -> dict[str, object]:
    """Read the workspace substrate revision payload at ``head``."""
    manager = mg._world_storage()
    repo = manager.store("store_workspace").repo
    commit = repo[head]
    blob = repo[commit.tree["revision.json"].id]
    payload = json.loads(bytes(blob.data).decode("utf-8"))
    assert isinstance(payload, dict)
    return payload


def _selected_workspace_head_or_none(mg: VcsCore, scope: ScopeInfo) -> str | None:
    manager = mg._world_storage()
    try:
        selected_world = manager.read_world(scope.ref)
        return selected_world.snapshot.head_for("workspace").head
    except KeyError:
        return None


def _world_store_refs(mg: VcsCore) -> set[str]:
    return set(mg._world_storage().world_store.repo.references)


def test_python_tier_write_classifies_as_capture_reduction(mg: VcsCore) -> None:
    """A single Python-tier write produces a workspace-capture-reduction candidate.

    Pre-T2: ``mg.exec("filesystem", "write", ...)`` produced a
    ``workspace-scan`` candidate with ``ingress_kind="command"``. T2c
    rewired this through PythonRuntimeCaptureAdapter so the candidate
    correctly classifies as ``workspace-capture-reduction`` with
    ``ingress_kind="reduce"``.
    """
    task = mg.fork(mg.ground, "task-py-capture")
    mg.exec("filesystem", "write", scope=task, path="src/app.py", content=b"print('hi')\n")

    manager = mg._world_storage()
    selected_world = manager.read_world(task.ref)
    selected_head = selected_world.snapshot.head_for("workspace").head
    provenance = manager.store("store_workspace").validate_prepared_candidate(
        selected_head,
        evidence_resolver=manager.world_store.resolve_evidence_ref,
    )

    assert provenance.transition.semantic_op == "workspace-capture-reduction"
    assert provenance.transition.ingress_kind == "reduce"
    assert provenance.transition.driver == "shepherd.workspace_ref"


def test_python_tier_write_read_only_active_surface_denies_before_evidence(mg: VcsCore) -> None:
    """A read-only ActiveSurface refuses Python-runtime writes before evidence persistence."""
    task = mg.fork(mg.ground, "task-py-capture-read-only-denied")
    before_head = _selected_workspace_head_or_none(mg, task)
    before_refs = _world_store_refs(mg)

    with (
        pytest.raises(SurfacePolicyError, match="python-runtime:write"),
        mg._use_active_surface(read_only_filesystem_surface()),
    ):
        mg.exec("filesystem", "write", scope=task, path="blocked.txt", content=b"blocked")

    after_head = _selected_workspace_head_or_none(mg, task)
    new_refs = _world_store_refs(mg) - before_refs

    assert after_head == before_head
    assert not any(ref.startswith("refs/vcscore/evidence/") for ref in new_refs)
    assert not any(ref.startswith("refs/vcscore/evidence-only/") for ref in new_refs)


def test_python_tier_read_read_only_active_surface_does_not_create_workspace_candidate(mg: VcsCore) -> None:
    """Read-only ActiveSurface still permits read provenance with no workspace mutation."""
    task = mg.fork(mg.ground, "task-py-capture-read-only-read")
    before_head = _selected_workspace_head_or_none(mg, task)
    before_refs = _world_store_refs(mg)

    with mg._use_active_surface(read_only_filesystem_surface()):
        mg.exec("filesystem", "read", scope=task, path="missing.txt")

    assert _selected_workspace_head_or_none(mg, task) == before_head
    assert not (_world_store_refs(mg) - before_refs)


def test_python_tier_write_permissive_active_surface_allows_capture(mg: VcsCore) -> None:
    """An explicit permissive ActiveSurface keeps the existing write path open."""
    task = mg.fork(mg.ground, "task-py-capture-permissive")

    with mg._use_active_surface(permissive_active_surface()):
        mg.exec("filesystem", "write", scope=task, path="allowed.txt", content=b"allowed")

    manager = mg._world_storage()
    selected_world = manager.read_world(task.ref)
    selected_head = selected_world.snapshot.head_for("workspace").head
    provenance = manager.store("store_workspace").validate_prepared_candidate(
        selected_head,
        evidence_resolver=manager.world_store.resolve_evidence_ref,
    )
    evidence_records = tuple(
        manager.world_store.resolve_evidence_ref(ref) for ref in provenance.preparation.evidence_refs
    )

    assert provenance.transition.semantic_op == "workspace-capture-reduction"
    assert any(record.evidence_kind == "python-runtime:write" for record in evidence_records)


def test_python_tier_writes_carry_tree_backed_byte_authority(mg: VcsCore) -> None:
    """Tree-backed byte authority is preserved through the typed reduce flow.

    The reduction_payload carries workspace_tree_oid into the typed
    ReduceRequest; the workspace driver's reduce handler picks it up
    and emits a ``byte_authority="tree-backed"`` candidate when the
    scalar workspace tree exists.
    """
    task = mg.fork(mg.ground, "task-py-capture-tree-backed")
    mg.exec("filesystem", "write", scope=task, path="hello.txt", content=b"hello\n")

    manager = mg._world_storage()
    selected_world = manager.read_world(task.ref)
    selected_head = selected_world.snapshot.head_for("workspace").head
    substrate = manager.store("store_workspace")
    metadata = substrate.read_revision_metadata(selected_head)

    assert metadata.byte_authority == "tree-backed"
    assert metadata.git_tree_oid is not None
    assert len(metadata.git_tree_oid) == 40


def test_python_tier_evidence_records_carry_python_runtime_mechanism(mg: VcsCore) -> None:
    """Persisted evidence records bear the python-runtime mechanism marker.

    The PythonRuntimeCaptureAdapter emits ``ObservationDraft`` values
    with ``mechanism=Mechanism.PYTHON_RUNTIME`` and
    ``evidence_kind=EvidenceKind.PYTHON_RUNTIME_WRITE``. After
    coordinator persistence, the EvidenceRecord retains these markers
    — so the query plane and supervisor sinks can discriminate
    Python-tier observations from overlay-captured ones by mechanism
    or evidence_kind.
    """
    task = mg.fork(mg.ground, "task-py-capture-evidence")
    mg.exec("filesystem", "write", scope=task, path="evidence.txt", content=b"e")

    manager = mg._world_storage()
    selected_world = manager.read_world(task.ref)
    selected_head = selected_world.snapshot.head_for("workspace").head
    provenance = manager.store("store_workspace").validate_prepared_candidate(
        selected_head,
        evidence_resolver=manager.world_store.resolve_evidence_ref,
    )
    evidence_records = tuple(
        manager.world_store.resolve_evidence_ref(ref) for ref in provenance.preparation.evidence_refs
    )
    # Find the python-runtime observation evidence (the reduced-state-proof
    # is the workspace driver's own proof observation alongside).
    runtime_records = [record for record in evidence_records if record.evidence_kind.startswith("python-runtime:")]
    assert runtime_records, "expected at least one python-runtime evidence record"
    assert all(record.ingress_kind == "capture" for record in runtime_records)
    assert all(record.evidence_kind == "python-runtime:write" for record in runtime_records)


def test_python_tier_delete_emits_delete_evidence_kind(mg: VcsCore) -> None:
    """``mg.exec("filesystem", "remove", ...)`` emits a python-runtime:delete observation.

    Verifies the adapter's op-token branch handles delete operations
    distinctly from writes (each gets its own evidence_kind).
    """
    task = mg.fork(mg.ground, "task-py-capture-delete")
    mg.exec("filesystem", "write", scope=task, path="to-delete.txt", content=b"x")
    mg.exec("filesystem", "delete", scope=task, path="to-delete.txt")

    manager = mg._world_storage()
    selected_world = manager.read_world(task.ref)
    selected_head = selected_world.snapshot.head_for("workspace").head
    provenance = manager.store("store_workspace").validate_prepared_candidate(
        selected_head,
        evidence_resolver=manager.world_store.resolve_evidence_ref,
    )
    evidence_records = tuple(
        manager.world_store.resolve_evidence_ref(ref) for ref in provenance.preparation.evidence_refs
    )
    delete_records = [record for record in evidence_records if record.evidence_kind == "python-runtime:delete"]
    assert delete_records, "expected a python-runtime:delete evidence record"


def test_python_tier_multiple_writes_reduce_to_single_candidate(mg: VcsCore) -> None:
    """Multiple writes within one operation reduce to a single workspace candidate.

    The scope's selected workspace head reflects the final reduced
    state. Each write contributes an observation to the evidence
    set; the reduce handler emits one TransitionDraft regardless.
    """
    task = mg.fork(mg.ground, "task-py-capture-multi")
    mg.exec("filesystem", "write", scope=task, path="a.txt", content=b"alpha")
    mg.exec("filesystem", "write", scope=task, path="b.txt", content=b"beta")
    mg.exec("filesystem", "write", scope=task, path="a.txt", content=b"alpha-2")

    manager = mg._world_storage()
    selected_world = manager.read_world(task.ref)
    selected_head = selected_world.snapshot.head_for("workspace").head
    provenance = manager.store("store_workspace").validate_prepared_candidate(
        selected_head,
        evidence_resolver=manager.world_store.resolve_evidence_ref,
    )
    # The selected substrate revision represents the final reduced state
    # (the latest exec call's output): a.txt=alpha-2, b.txt=beta.
    payload = _workspace_revision_payload(mg, selected_head)
    entries = {entry["path"]: entry for entry in payload["state_manifest"]["entries"]}
    assert set(entries.keys()) == {"a.txt", "b.txt"}
    assert entries["a.txt"]["content_digest"] == _content_digest(b"alpha-2")
    assert entries["b.txt"]["content_digest"] == _content_digest(b"beta")
    assert provenance.transition.semantic_op == "workspace-capture-reduction"


def test_python_tier_uses_registry_owned_adapter_not_driver_default(mg: VcsCore) -> None:
    """PythonRuntimeCaptureAdapter is registry-owned, not workspace-driver-owned.

    Per SPI v0.1 §Q2 Discovery boundary criterion, the python-runtime
    adapter's lifetime is owned by the patch manager (a cross-cutting
    installation component), so it lives in ``CaptureAdapterRegistry``.
    The workspace driver's ``capture_adapters(context)`` returns ONLY
    its driver-default adapter (overlay).
    """
    from vcs_core._overlay_capture_adapter import OverlayCaptureAdapter
    from vcs_core._python_runtime_capture_adapter import PythonRuntimeCaptureAdapter
    from vcs_core._substrate_evidence_kinds import Mechanism

    # The patch manager registered the python-runtime adapter at construction.
    registry_adapter = mg._capture_adapter_by_mechanism(Mechanism.PYTHON_RUNTIME)
    assert isinstance(registry_adapter, PythonRuntimeCaptureAdapter)

    # The workspace driver's default adapters do NOT include the python-runtime one.
    from vcs_core._world_substrate_adapters import WorkspaceSubstrateDriver

    workspace_driver = WorkspaceSubstrateDriver()
    # Build a minimal context to pass to capture_adapters.
    from vcs_core._substrate_driver import DriverContext
    from vcs_core._world_types import SubstrateStoreIdentity

    ctx = DriverContext(
        operation_id="op-test",
        binding=workspace_driver.binding,
        role=workspace_driver.role,
        store_identity=SubstrateStoreIdentity(
            store_id=workspace_driver.store_id,
            kind="filesystem",
            resource_id="ws:test",
        ),
    )
    driver_defaults = workspace_driver.capture_adapters(ctx)
    assert all(isinstance(a, OverlayCaptureAdapter) for a in driver_defaults)
    assert not any(isinstance(a, PythonRuntimeCaptureAdapter) for a in driver_defaults), (
        "PythonRuntimeCaptureAdapter must be registry-owned, not driver-default"
    )
