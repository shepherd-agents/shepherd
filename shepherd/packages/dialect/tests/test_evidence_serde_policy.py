"""Persisted-evidence serde policy conformance (evidence-record-schema-policy.md §2.6).

The policy's sixth clause: "the posture is checkable" — a unit test enumerates the §3 register and
asserts each family's declared posture matches its implementation. The register spans two packages,
so conformance lives in two homes, each testing the families it owns with in-package imports only
(no cross-package private coupling, per the test-private-import ratchet):

- **this file** covers the dialect run-ledger families (`RunExecutionEvidence`, and the run-record
  sub-object settlement-policy validators);
- `vcs-core/.../tests/.../test_evidence_serde_policy.py` covers the vcs-core families
  (`RetainedOutputSettlement`, `PendingAuthoritySettlement`, workspace-state manifest).

Each family's row asserts its *declared* posture: the unknown-field-rejecting families are
enumerated in `_STRICT_FAMILIES`; a family that regresses to lenient serde fails here loudly. The
T1 D3 reference case is `RunExecutionEvidence`: it restored `effective_feature_flags` as a
legacy-optional field AND became strict, so old records round-trip while unknown fields are
rejected rather than silently dropped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from shepherd_dialect.workspace_control.schemas import (
    RunExecutionEvidence,
    _validate_execution_enforcement_policy,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


def test_run_execution_evidence_round_trips_legacy_feature_flags() -> None:
    """A 0.2.0-era record carrying effective_feature_flags parses and renders (D3 disposition a)."""
    legacy = {
        "requested_placement": "advisory",
        "resolved_placement": "advisory",
        "enforcement_basis": "legacy_advisory",
        "execution_descriptor": None,
        "effective_feature_flags": {"seal_and_select": True},
    }
    restored = RunExecutionEvidence.from_json(legacy)
    assert restored.effective_feature_flags == {"seal_and_select": True}
    assert restored.to_json()["effective_feature_flags"] == {"seal_and_select": True}


def test_run_execution_evidence_new_records_omit_feature_flags() -> None:
    """New code never populates the retired field, so fresh records stay clean."""
    fresh = RunExecutionEvidence()
    assert fresh.effective_feature_flags is None
    assert "effective_feature_flags" not in fresh.to_json()


def test_run_execution_evidence_rejects_unknown_field() -> None:
    """Strict serde: an unknown field fails closed instead of being silently dropped."""
    payload = {
        "requested_placement": "advisory",
        "resolved_placement": "advisory",
        "enforcement_basis": "legacy_advisory",
        "execution_descriptor": None,
        "some_future_field": "surprise",
    }
    with pytest.raises(ValueError, match="unsupported field"):
        RunExecutionEvidence.from_json(payload)


# §3 record-family register (dialect half): family name -> (reject_callable, minimal valid payload).
# Each listed family must reject an injected unknown field — the mechanism that makes "strict" a
# checked property, not a claimed one, so a family that regresses to lenient serde fails here loudly.
# `reject_callable` parses/validates a mapping and raises on an unknown field.
_STRICT_FAMILIES: dict[str, tuple[Callable[[Mapping[str, object]], object], dict[str, object]]] = {
    "RunExecutionEvidence": (
        RunExecutionEvidence.from_json,
        {
            "requested_placement": "advisory",
            "resolved_placement": "advisory",
            "enforcement_basis": "legacy_advisory",
            "execution_descriptor": None,
        },
    ),
    # A run-record sub-object: the settlement-policy validators route unknown fields through
    # `_reject_unknown_fields`. Exercising one (execution-enforcement) covers that shared mechanism.
    "execution_enforcement_policy": (
        _validate_execution_enforcement_policy,
        {
            "mode": "in_process",
            "executor_kind": "in_process",
            "provider": "deterministic-fake",
            "profile": "Permissive",
            "authority_basis": "task_default",
            "monitor_required": False,
        },
    ),
}


@pytest.mark.parametrize("family", sorted(_STRICT_FAMILIES))
def test_registered_strict_family_rejects_unknown_field(family: str) -> None:
    reject, valid_payload = _STRICT_FAMILIES[family]
    # Baseline: the valid payload parses/validates.
    reject(valid_payload)
    # Injected unknown field is rejected (both call sites use `_reject_unknown_fields`, whose message
    # contains "unsupported field(s)").
    with pytest.raises(ValueError, match="unsupported field"):
        reject({**valid_payload, "__unregistered_field__": "x"})
