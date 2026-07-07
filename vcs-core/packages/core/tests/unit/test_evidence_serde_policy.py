"""Persisted-evidence serde policy conformance — vcs-core families (evidence-record-schema-policy.md §2.6).

The §3 register spans two packages; this file owns the vcs-core rows (the dialect rows live in
`shepherd/packages/dialect/tests/test_evidence_serde_policy.py`). Two strictness mechanisms appear:

- **unknown-field rejection** (`RetainedOutputSettlement`, `PendingAuthoritySettlement`): a drifted
  writer's extra field fails closed rather than being silently dropped;
- **canonical-equality** (workspace-state manifest): the payload must equal its own canonical form,
  which is strictly stronger than unknown-field rejection.

A family that regresses to lenient serde fails here loudly — that is the whole point of §2.6.
"""

from __future__ import annotations

import json

import pytest
from vcs_core._authority import PendingAuthoritySettlement
from vcs_core._retained_output_settlement import _settlement_from_json, _settlement_to_json
from vcs_core._world_substrate_adapters import (
    validate_workspace_state_manifest_payload,
    workspace_state_manifest_payload,
)
from vcs_core.types import RetainedOutputSettlement


def _valid_settlement() -> RetainedOutputSettlement:
    return RetainedOutputSettlement(
        scope_name="run-abc",
        scope_ref="refs/vcscore/scopes/run-abc",
        scope_instance_id="inst-1",
        parent_ref="refs/vcscore/scopes/parent",
        handoff_ref="refs/vcscore/handoffs/run-abc",
        output_world_oid="0" * 40,
        binding="workspace",
        store_id="store-1",
        resource_id="resource-1",
        candidate_id="primary",
        candidate_head="1" * 40,
        action="selected",
        operation_id="op-1",
        parent_world_before="2" * 40,
        parent_world_after="3" * 40,
        settlement_ref="refs/vcscore/settlements/run-abc",
    )


def _valid_pending() -> PendingAuthoritySettlement:
    return PendingAuthoritySettlement(
        settlement_operation_id="settle-1",
        authority_operation_id="auth-1",
        scope_name="run-abc",
        scope_ref="refs/vcscore/scopes/run-abc",
        scope_instance_id="inst-1",
        scope_world_id=None,
        parent_scope_name="parent",
        parent_scope_ref="refs/vcscore/scopes/parent",
        parent_scope_instance_id="pinst-1",
        parent_scope_world_id=None,
        cohort_id="cohort-1",
        candidate_digest="deadbeef",
        outcome="allowed",
        settlement="merged",
        commit_outcome="pending",
        decision_ids=("dec-1",),
        reason_code="pending_filesystem_merge",
    )


def test_retained_output_settlement_rejects_unknown_field() -> None:
    """Strict + digested: an unknown field is refused before the digest is even checked."""
    payload = _settlement_to_json(_valid_settlement())
    assert _settlement_from_json(payload).action == "selected"  # baseline round-trip
    with pytest.raises(Exception, match="unexpected retained output settlement fields"):
        _settlement_from_json({**payload, "__unregistered_field__": "x"})


def test_pending_authority_settlement_rejects_unknown_field() -> None:
    """Strict: `from_dict` must reject unknown fields rather than silently dropping them.

    Before the T1 closeout this record's `from_dict` was silently lenient — it read named keys and
    ignored the rest — while the §3 register declared it strict. Hardened to match the declared
    posture; the allowed set derives from the dataclass fields, so D7's additive vocabulary tracks
    automatically.
    """
    # Round-trip through JSON (tuples become lists) to match how records actually reach the store.
    payload = json.loads(json.dumps(_valid_pending().to_dict()))
    assert PendingAuthoritySettlement.from_dict(payload).outcome == "allowed"  # baseline round-trip
    with pytest.raises(ValueError, match="unsupported field"):
        PendingAuthoritySettlement.from_dict({**payload, "__unregistered_field__": "x"})


def test_workspace_state_manifest_rejects_noncanonical_payload() -> None:
    """Canonical-equality: the manifest must equal its own canonical form (stronger than unknown-field).

    An entry carrying an extra key is refused: a present entry's field set is checked exactly, so the
    unknown key fails closed rather than being silently dropped.
    """
    entries = ({"path": "a.txt", "state": "present", "mode": 0o100644, "content_digest": "sha256:" + "0" * 64},)
    canonical = workspace_state_manifest_payload(entries, byte_authority="digest-only")
    assert validate_workspace_state_manifest_payload(canonical)["entries"]  # baseline
    tampered = {**canonical, "entries": [{**canonical["entries"][0], "__unregistered__": "x"}]}
    with pytest.raises(ValueError, match=r"present entries require|canonical"):
        validate_workspace_state_manifest_payload(tampered)
