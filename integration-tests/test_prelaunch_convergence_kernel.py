"""Promoted pre-launch convergence proof over root commons-vcs."""


from __future__ import annotations

import importlib
import importlib.util
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pytest
from commons_vcs import Object, Repo
from commons_vcs.backends import Backend, MemoryBackend
from shepherd_core.effects import ToolCallStarted
from shepherd_core.effects.commons_vcs import project_effect_layer, shepherd_effect_profile
from shepherd_core.scope.stream import EffectLayer
from vcs_core._identity import read_ground_world_id
from vcs_core.profiles.commons_vcs import profile as vcscore_profile
from vcs_core.profiles.projection import project_commit_object, project_scope_object
from vcs_core.recording import RecordingPipeline
from vcs_core.store import Store
from vcs_core.types import EffectRecord, ScopeInfo

if TYPE_CHECKING:
    from pathlib import Path

KERNEL0_AVAILABLE = importlib.util.find_spec("kernel0") is not None
pytestmark = pytest.mark.skipif(
    not KERNEL0_AVAILABLE,
    reason="kernel0 is not importable; set PYTHONPATH=../../sgc/packages/kernel0/src to run this gate",
)

if KERNEL0_AVAILABLE:
    kernel0_commons = importlib.import_module("kernel0.commons_vcs")
    kernel0_hashes = importlib.import_module("kernel0.hashes")
    kernel0_model = importlib.import_module("kernel0.model")

    SGC_EVIDENCE_ROLE = kernel0_commons.SGC_EVIDENCE_ROLE
    SGC_GOVERNED_STATE_ROLE = kernel0_commons.SGC_GOVERNED_STATE_ROLE
    SGC_STATE_OBJECT_ROLE = kernel0_commons.SGC_STATE_OBJECT_ROLE
    project_governed_state_ref = kernel0_commons.project_governed_state_ref
    project_transition_receipt = kernel0_commons.project_transition_receipt
    sgc_receipt_profile = kernel0_commons.sgc_receipt_profile
    sha256_digest = kernel0_hashes.sha256_digest
    TransitionReceipt = kernel0_model.TransitionReceipt


@dataclass(frozen=True)
class ConvergenceGraph:
    """Object ids for one cross-project convergence graph."""

    repo: Repo
    shepherd_effect_id: str
    shepherd_event_id: str
    vcscore_scope_id: str
    vcscore_commit_id: str
    sgc_governed_state_id: str
    sgc_receipt_id: str
    receipt_hash: str


def _make_memory_backend(_tmp_path: Path) -> Backend:
    return MemoryBackend()


def _make_git_backend(tmp_path: Path) -> Backend:
    pytest.importorskip("pygit2")
    from commons_vcs.backends.git import GitBackend

    return GitBackend.init(tmp_path / "commons-git")


@pytest.fixture(params=[pytest.param(_make_memory_backend, id="memory"), pytest.param(_make_git_backend, id="git")])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Backend:
    """Return each supported commons-vcs backend for the harness."""
    factory = request.param
    return factory(tmp_path)


def _edge_targets(obj: Object, role: str) -> list[str]:
    return [edge.target for edge in obj.edges if edge.role == role]


def _ground_scope(store: Store) -> ScopeInfo:
    return ScopeInfo(
        name="ground",
        ref=Store.GROUND_REF,
        instance_id="ground-prelaunch-convergence",
        creation_oid="",
        world_id=read_ground_world_id(store.repo_path),
    )


def _rehashed_receipt(receipt: TransitionReceipt) -> TransitionReceipt:
    unsigned = replace(receipt, receipt_hash="")
    return replace(unsigned, receipt_hash=sha256_digest(unsigned.binding_dict()))


def _build_convergence_graph(tmp_path: Path, backend: Backend) -> ConvergenceGraph:
    repo = Repo(
        profiles=[shepherd_effect_profile, vcscore_profile, sgc_receipt_profile],
        backend=backend,
    )

    projected = project_effect_layer(
        EffectLayer(
            effect=ToolCallStarted(
                tool_call_id="tool-call-1",
                tool_name="Edit",
                params={"path": "README.md", "replacement": "converged"},
            ),
            sequence=0,
        ),
        stream_id="agent-run-1",
    )
    shepherd_effect_id = repo.append(projected.effect)
    shepherd_event_id = repo.append(projected.event)

    store = Store(str(tmp_path / "vcscore-store"))
    store.create_root_commit()
    ground = _ground_scope(store)
    pipeline = RecordingPipeline(store)
    pipeline.set_scope(ground)
    carrier_oid = pipeline.record_one(
        EffectRecord(
            "ShepherdEvidenceAccepted",
            {"shepherd_event": shepherd_event_id},
        ),
        substrate="prelaunch-convergence",
    )

    vcscore_scope_id = repo.append(project_scope_object(ground))
    carrier_commit = store._repo[carrier_oid]
    vcscore_commit_id = repo.append(
        project_commit_object(
            store._repo,
            carrier_commit,
            effect_id=shepherd_event_id,
            scope_id=vcscore_scope_id,
        )
    )

    receipt = _rehashed_receipt(
        TransitionReceipt(
            lineage_id="lineage://prelaunch-convergence",
            step_id="step://prelaunch-convergence",
            step_nonce="nonce://prelaunch-convergence",
            governing_state_ref=f"commons:{vcscore_commit_id}",
            spec_hash="sha256:" + "1" * 64,
            bundle_hash="sha256:" + "2" * 64,
            candidate_id="sha256:" + "3" * 64,
            decision_ref="decision://prelaunch-convergence",
            authorized=True,
            receipt_hash="",
            prior_receipt_hash=None,
            created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            successor_state_ref="git:after-prelaunch-convergence",
            prior_authorized_receipt_hash=None,
        )
    )
    sgc_governed_state_id = repo.append(
        project_governed_state_ref(
            receipt.governing_state_ref,
            lineage_id=receipt.lineage_id,
            step_id=receipt.step_id,
            state_object=vcscore_commit_id,
        )
    )
    sgc_receipt_id = repo.append(
        project_transition_receipt(
            receipt,
            governed_state=sgc_governed_state_id,
            evidence=(shepherd_event_id,),
        )
    )

    return ConvergenceGraph(
        repo=repo,
        shepherd_effect_id=shepherd_effect_id,
        shepherd_event_id=shepherd_event_id,
        vcscore_scope_id=vcscore_scope_id,
        vcscore_commit_id=vcscore_commit_id,
        sgc_governed_state_id=sgc_governed_state_id,
        sgc_receipt_id=sgc_receipt_id,
        receipt_hash=receipt.receipt_hash,
    )


def test_receipt_walks_to_shepherd_evidence_and_vcscore_state(tmp_path: Path, backend: Backend) -> None:
    """Verify an SGC receipt can reach Shepherd evidence and vcs-core state."""
    graph = _build_convergence_graph(tmp_path, backend)

    shepherd_event = graph.repo.get(graph.shepherd_event_id)
    vcscore_commit = graph.repo.get(graph.vcscore_commit_id)
    sgc_governed_state = graph.repo.get(graph.sgc_governed_state_id)
    sgc_receipt = graph.repo.get(graph.sgc_receipt_id)
    assert shepherd_event is not None
    assert vcscore_commit is not None
    assert sgc_governed_state is not None
    assert sgc_receipt is not None

    assert _edge_targets(shepherd_event, "shepherd.effect") == [graph.shepherd_effect_id]
    assert _edge_targets(vcscore_commit, "effect") == [graph.shepherd_event_id]
    assert _edge_targets(vcscore_commit, "scope") == [graph.vcscore_scope_id]
    assert _edge_targets(sgc_governed_state, SGC_STATE_OBJECT_ROLE) == [graph.vcscore_commit_id]
    assert sgc_governed_state.body["state_ref"] == f"commons:{graph.vcscore_commit_id}"
    assert _edge_targets(sgc_receipt, SGC_GOVERNED_STATE_ROLE) == [graph.sgc_governed_state_id]
    assert _edge_targets(sgc_receipt, SGC_EVIDENCE_ROLE) == [graph.shepherd_event_id]
    assert sgc_receipt.body["receipt"]["receipt_hash"] == graph.receipt_hash

    assert graph.repo.verify(graph.sgc_receipt_id, graph.shepherd_effect_id).outcome == "ok.verified"
    assert graph.repo.verify(graph.sgc_receipt_id, graph.shepherd_event_id).outcome == "ok.verified"
    assert graph.repo.verify(graph.sgc_receipt_id, graph.vcscore_scope_id).outcome == "ok.verified"
    assert graph.repo.verify(graph.sgc_receipt_id, graph.vcscore_commit_id).outcome == "ok.verified"


def test_inverse_citations_answer_cross_project_questions(tmp_path: Path, backend: Backend) -> None:
    """Verify inverse citations remain useful as graph facts."""
    graph = _build_convergence_graph(tmp_path, backend)

    assert graph.repo.cited_by(graph.shepherd_effect_id, "shepherd.effect") == [graph.shepherd_event_id]
    assert graph.repo.cited_by(graph.shepherd_event_id, "effect") == [graph.vcscore_commit_id]
    assert graph.repo.cited_by(graph.vcscore_scope_id, "scope") == [graph.vcscore_commit_id]
    assert graph.repo.cited_by(graph.vcscore_commit_id, SGC_STATE_OBJECT_ROLE) == [graph.sgc_governed_state_id]
    assert graph.repo.cited_by(graph.shepherd_event_id, SGC_EVIDENCE_ROLE) == [graph.sgc_receipt_id]
    assert graph.repo.cited_by(graph.sgc_governed_state_id, SGC_GOVERNED_STATE_ROLE) == [graph.sgc_receipt_id]
