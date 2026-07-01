from __future__ import annotations

import pytest
from vcs_core._command_envelope import (
    AuthorityMergeControl,
    CommandEnvelopeError,
    CommandExecutionOptions,
    command_execution_options_from_mapping,
    command_execution_options_to_mapping,
    validate_command_execution_options,
)
from vcs_core._permission_plan_evidence import permission_plan_digest

_EFFECTIVE_MATCH_DIGEST = "test-effective-match"
_AUTHORITY_SURFACE_PLAN_DIGEST = "test-authority-surface-plan"
_PERMISSION_PLAN_DESCRIPTOR = {
    "schema": "shepherd.permission-plan.v1",
    "fallback": "enforce",
    "assignments": [
        {
            "monitor": "carrier_check_at_commit",
            "timing": "commit",
            "route": "carrier_diff",
            "completeness_basis": "test carrier diff",
            "tamper_basis": "test coordinator",
            "confinement": None,
            "evidence": {
                "effective_match_digest": _EFFECTIVE_MATCH_DIGEST,
                "authority_surface_plan_digest": _AUTHORITY_SURFACE_PLAN_DIGEST,
            },
        }
    ],
}


def _authority_control(**overrides: object) -> AuthorityMergeControl:
    payload = {
        "binding_roots": {"workspace": ""},
        "decide": lambda request: "allowed",
        "effective_match_digest": _EFFECTIVE_MATCH_DIGEST,
        "authority_surface_plan_digest": _AUTHORITY_SURFACE_PLAN_DIGEST,
        "permission_plan_digest": permission_plan_digest(_PERMISSION_PLAN_DESCRIPTOR),
        "permission_plan_descriptor": _PERMISSION_PLAN_DESCRIPTOR,
    }
    payload.update(overrides)
    return AuthorityMergeControl(**payload)


def test_seal_success_disposition_is_deliberately_not_session_transportable() -> None:
    with pytest.raises(CommandEnvelopeError, match="success disposition 'seal' is not supported"):
        command_execution_options_to_mapping(CommandExecutionOptions(success_disposition="seal"))


def test_authority_merge_success_disposition_is_deliberately_not_session_transportable() -> None:
    options = CommandExecutionOptions(
        success_disposition="authority_merge",
        authority_merge=_authority_control(),
    )

    with pytest.raises(CommandEnvelopeError, match="success disposition 'authority_merge' is not supported"):
        command_execution_options_to_mapping(options)


def test_session_transport_rejects_unknown_seal_success_disposition_option() -> None:
    with pytest.raises(CommandEnvelopeError, match="Unknown command execution option"):
        command_execution_options_from_mapping({"success_disposition": "seal"})


def test_authority_merge_requires_control() -> None:
    options = CommandExecutionOptions(success_disposition="authority_merge")

    with pytest.raises(CommandEnvelopeError, match="requires 'authority_merge'"):
        validate_command_execution_options(options)


def test_authority_merge_control_is_only_valid_for_authority_merge() -> None:
    options = CommandExecutionOptions(
        authority_merge=_authority_control(),
    )

    with pytest.raises(CommandEnvelopeError, match="only valid with authority_merge"):
        validate_command_execution_options(options)


def test_authority_merge_control_rejects_invalid_permission_plan_evidence() -> None:
    with pytest.raises(CommandEnvelopeError, match="PermissionPlan evidence invalid"):
        _authority_control(permission_plan_digest="not-the-real-digest")
