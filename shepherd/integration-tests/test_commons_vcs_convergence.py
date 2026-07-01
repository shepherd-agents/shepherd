"""Cross-project commons-vcs convergence proof."""

from __future__ import annotations

import importlib
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pytest
from commons_vcs import Object, Repo
from shepherd_core.effects import ToolCallStarted
from shepherd_core.effects.commons_vcs import project_effect_layer, shepherd_effect_profile
from shepherd_core.scope.stream import EffectLayer
from vcs_core.profiles.commons_vcs import profile as vcscore_profile
from vcs_core.profiles.projection import project_commit_object, project_scope_object
from vcs_core.recording import RecordingPipeline
from vcs_core.store import Store
from vcs_core.types import EffectRecord, ScopeInfo

if TYPE_CHECKING:
    from pathlib import Path

try:
    kernel0_commons = importlib.import_module("kernel0.commons_vcs")
    kernel0_hashes = importlib.import_module("kernel0.hashes")
    kernel0_model = importlib.import_module("kernel0.model")
except ModuleNotFoundError as exc:
    pytest.skip(
        "The convergence contract gate requires kernel0. "
        "Run it with `make test_convergence` so PYTHONPATH and the required "
        "cross-project dependencies are configured."
        f" Missing import: {exc.name}.",
        allow_module_level=True,
    )

SGC_EVIDENCE_ROLE = kernel0_commons.SGC_EVIDENCE_ROLE
SGC_GOVERNED_STATE_ROLE = kernel0_commons.SGC_GOVERNED_STATE_ROLE
SGC_STATE_OBJECT_ROLE = kernel0_commons.SGC_STATE_OBJECT_ROLE
project_governed_state_ref = kernel0_commons.project_governed_state_ref
project_transition_receipt = kernel0_commons.project_transition_receipt
sgc_receipt_profile = kernel0_commons.sgc_receipt_profile
sha256_digest = kernel0_hashes.sha256_digest
TransitionReceipt = kernel0_model.TransitionReceipt


def _edge_targets(obj: Object, role: str) -> list[str]:
    return [edge.target for edge in obj.edges if edge.role == role]


def _ground_scope(store: Store) -> ScopeInfo:
    return ScopeInfo(
        name="ground",
        ref=Store.GROUND_REF,
        instance_id="ground-convergence",
        creation_oid="",
        world_id="world-convergence",
    )


def _rehashed_receipt(receipt: TransitionReceipt) -> TransitionReceipt:
    unsigned = replace(receipt, receipt_hash="")
    return replace(unsigned, receipt_hash=sha256_digest(unsigned.binding_dict()))


def test_shepherd_event_vcscore_commit_and_sgc_receipt_share_one_graph(tmp_path: Path) -> None:
    """One real object from each project participates in the same commons graph."""
    repo = Repo(profiles=[shepherd_effect_profile, vcscore_profile, sgc_receipt_profile])
    projected = project_effect_layer(
        EffectLayer(
            effect=ToolCallStarted(
                tool_call_id="tc_1",
                tool_name="Edit",
                params={"path": "README.md"},
            ),
            sequence=0,
        ),
        stream_id="agent-run-1",
    )
    shepherd_effect_id = repo.append(projected.effect)
    shepherd_event_id = repo.append(projected.event)

    store = Store(str(tmp_path / ".vcscore"))
    store.create_root_commit()
    ground = _ground_scope(store)
    pipeline = RecordingPipeline(store)
    pipeline.set_scope(ground)
    carrier_oid = pipeline.record_one(
        EffectRecord("ShepherdEvidenceAccepted", {"shepherd_event": shepherd_event_id}),
        substrate="convergence",
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
            lineage_id="lineage://convergence",
            step_id="step://convergence",
            step_nonce="nonce://convergence",
            governing_state_ref=f"commons:{vcscore_commit_id}",
            spec_hash="sha256:" + "1" * 64,
            bundle_hash="sha256:" + "2" * 64,
            candidate_id="sha256:" + "3" * 64,
            decision_ref="decision://convergence",
            authorized=True,
            receipt_hash="",
            prior_receipt_hash=None,
            created_at=datetime(2026, 4, 26, tzinfo=timezone.utc),
            successor_state_ref="git:after-convergence",
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

    event = repo.get(shepherd_event_id)
    vcscore_commit = repo.get(vcscore_commit_id)
    sgc_governed_state = repo.get(sgc_governed_state_id)
    sgc_receipt = repo.get(sgc_receipt_id)
    assert event is not None
    assert vcscore_commit is not None
    assert sgc_governed_state is not None
    assert sgc_receipt is not None

    assert _edge_targets(event, "shepherd.effect") == [shepherd_effect_id]
    assert repo.get(shepherd_effect_id) == projected.effect
    assert _edge_targets(vcscore_commit, "effect") == [shepherd_event_id]
    assert _edge_targets(sgc_governed_state, SGC_STATE_OBJECT_ROLE) == [vcscore_commit_id]
    assert sgc_governed_state.body["state_ref"] == f"commons:{vcscore_commit_id}"
    assert _edge_targets(sgc_receipt, SGC_GOVERNED_STATE_ROLE) == [sgc_governed_state_id]
    assert sgc_receipt.body["receipt"]["governing_state_ref"] == f"commons:{vcscore_commit_id}"
    assert _edge_targets(sgc_receipt, SGC_EVIDENCE_ROLE) == [shepherd_event_id]
    assert sgc_receipt.body["receipt"]["receipt_hash"] == receipt.receipt_hash
    assert repo.cited_by(shepherd_effect_id, "shepherd.effect") == [shepherd_event_id]
    assert repo.cited_by(shepherd_event_id, "effect") == [vcscore_commit_id]
    assert repo.cited_by(vcscore_commit_id, SGC_STATE_OBJECT_ROLE) == [sgc_governed_state_id]
    assert repo.cited_by(shepherd_event_id, SGC_EVIDENCE_ROLE) == [sgc_receipt_id]
    assert repo.cited_by(sgc_governed_state_id, SGC_GOVERNED_STATE_ROLE) == [sgc_receipt_id]
