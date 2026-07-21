from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("openai_codex", reason="install shepherd-dialect[codex] to run Codex provider tests")

from vcs_core.spi import ConfinementSpec

from shepherd_dialect.provider_runtime import (
    MODEL_TURN,
    PROVIDER_INVOCATION_COMPLETED,
    ProviderInvocationError,
)
from shepherd_dialect.providers.codex import CodexAgentProvider

FAKE_APP_SERVER = Path(__file__).with_name("support") / "fake_codex_app_server.py"


def _profile(root: Path, *, mode: str = "chatgpt") -> None:
    credential = root / "default" / "credential"
    credential.mkdir(parents=True, mode=0o700)
    (credential / "auth.json").write_text('{"tokens":{"access_token":"sk-fixture-secret"}}', encoding="utf-8")
    (root / "default" / "metadata.json").write_text(
        json.dumps(
            {
                "schema": "shepherd.codex_profile.v1",
                "mode": mode,
                "source": "fixture",
                "sdk_version": "0.144.4",
            }
        ),
        encoding="utf-8",
    )


def test_python_provider_streams_every_frame_and_returns_usage_and_subscription_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_root = tmp_path / "profiles"
    _profile(profile_root)
    monkeypatch.setenv("SHEPHERD_CODEX_PROFILE_ROOT", str(profile_root))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    execution = SimpleNamespace(
        working_path=workspace,
        identity=SimpleNamespace(scope_instance_id="fixture-run", scope_name="fixture"),
    )
    provider = CodexAgentProvider(
        prompt="exercise the complete fixture",
        model="gpt-5.4",
        _test_app_server_argv=(sys.executable, "-B", str(FAKE_APP_SERVER)),
        _test_app_server_env={
            "SHEPHERD_CODEX_SPIKE_SCENARIO": "production-all-events",
            "SHEPHERD_CODEX_SPIKE_WRITE_PATH": str(workspace / "result.txt"),
            "SHEPHERD_CODEX_SPIKE_CARRIER_ONLY_PATH": str(workspace / "carrier-only.txt"),
        },
        _allow_fake_runtime=True,
    )

    result = provider.execute(
        None,
        SimpleNamespace(),
        SimpleNamespace(),
        {},
        execution=execution,
        confinement=ConfinementSpec.permissive_for(workspace),
    )

    assert result.activity_manifest is not None
    assert result.activity_manifest.complete is True
    assert result.activity_manifest.activity_count == len(result.provider_activities)
    assert result.activity_manifest.activity_count == 31
    assert result.activity_manifest.ingress_count == 31
    methods = [activity.method for activity in result.provider_activities]
    assert Counter(methods) == {
        "initialize": 1,
        "account/read": 1,
        "command/exec": 9,
        "account/rateLimits/read": 2,
        "thread/start": 1,
        "turn/start": 1,
        "item/started": 4,
        "item/commandExecution/outputDelta": 1,
        "item/completed": 5,
        "item/mcpToolCall/progress": 1,
        "turn/diff/updated": 1,
        "experimental/futureEvent": 1,
        "item/agentMessage/delta": 1,
        "thread/tokenUsage/updated": 1,
        "turn/completed": 1,
    }
    assert "experimental/futureEvent" in methods
    assert "thread/tokenUsage/updated" in methods
    assert "turn/completed" in methods
    assert result.outcome["usage"] == {
        "last": {
            "input_tokens": 120,
            "cached_input_tokens": 20,
            "output_tokens": 30,
            "reasoning_output_tokens": 10,
            "total_tokens": 150,
        },
        "total": {
            "input_tokens": 120,
            "cached_input_tokens": 20,
            "output_tokens": 30,
            "reasoning_output_tokens": 10,
            "total_tokens": 150,
        },
        "model_context_window": 200000,
    }
    metadata = result.outcome["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["sandbox_evidence"] == {
        "adapter_version": "shepherd.codex_protocol.144_4.v1",
        "outside_command_exit_code": 1,
        "outside_write_probe": "passed",
        "parent_environment_secret_probe": "passed",
        "permission_profile": "shepherd_run",
        "provider_state_denial_probe": "passed",
        "provider_state_denied_path_count": 6,
        "workspace_write_probe": "passed",
    }
    assert metadata["cost"]["subscription_credits_consumed"] == "0.5"
    assert metadata["cost"]["currency"] is None
    assert metadata["file_effect_reconciliation"]["classification_counts"] == {
        "carrier_confirmed": 1,
        "provider_only": 0,
        "carrier_only": 1,
    }
    assert any(event.kind == MODEL_TURN for event in result.provider_events)
    assert result.provider_events[-1].kind == PROVIDER_INVOCATION_COMPLETED
    durable = json.dumps(
        {
            "activities": [activity.as_wire_record() for activity in result.provider_activities],
            "events": [event.stable_payload() for event in result.provider_events],
            "outcome": result.outcome,
        },
        sort_keys=True,
    )
    assert "sk-fixture-secret" not in durable
    assert "printf 'do not persist" not in durable
    assert "completed safely sk-spike-secret" not in json.dumps(
        [activity.as_wire_record() for activity in result.provider_activities], sort_keys=True
    )


def test_approval_request_is_recorded_and_explicit_decline_is_hash_chained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_root = tmp_path / "profiles"
    _profile(profile_root)
    monkeypatch.setenv("SHEPHERD_CODEX_PROFILE_ROOT", str(profile_root))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    decision_report = tmp_path / "decision.json"
    provider = CodexAgentProvider(
        prompt="approval fixture",
        _test_app_server_argv=(sys.executable, "-B", str(FAKE_APP_SERVER)),
        _test_app_server_env={
            "SHEPHERD_CODEX_SPIKE_SCENARIO": "approval",
            "SHEPHERD_CODEX_SPIKE_APPROVAL_RESULT": str(decision_report),
        },
        _allow_fake_runtime=True,
    )

    result = provider.execute(
        None,
        SimpleNamespace(),
        SimpleNamespace(),
        {},
        execution=SimpleNamespace(
            working_path=workspace,
            identity=SimpleNamespace(scope_instance_id="approval-run", scope_name="fixture"),
        ),
        confinement=ConfinementSpec.permissive_for(workspace),
    )

    request = next(activity for activity in result.provider_activities if activity.category == "server_request")
    decision = next(activity for activity in result.provider_activities if activity.kind == "approval.declined")
    assert decision.sequence == request.sequence + 1
    assert decision.previous_record_digest == request.record_digest
    assert decision.payload["decision"] == "decline"
    assert json.loads(decision_report.read_text(encoding="utf-8")) == {"decision": "decline"}


def test_api_key_profile_is_supported_offline_without_api_billing_guess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_root = tmp_path / "profiles"
    _profile(profile_root, mode="api_key")
    monkeypatch.setenv("SHEPHERD_CODEX_PROFILE_ROOT", str(profile_root))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = CodexAgentProvider(
        prompt="api-key fixture",
        auth_mode="api_key",
        _test_app_server_argv=(sys.executable, "-B", str(FAKE_APP_SERVER)),
        _test_app_server_env={"SHEPHERD_CODEX_SPIKE_SCENARIO": "api-key-production"},
        _allow_fake_runtime=True,
    )

    result = provider.execute(
        None,
        SimpleNamespace(),
        SimpleNamespace(),
        {},
        execution=SimpleNamespace(
            working_path=workspace,
            identity=SimpleNamespace(scope_instance_id="api-key-run", scope_name="fixture"),
        ),
        confinement=ConfinementSpec.permissive_for(workspace),
    )

    assert result.outcome["metadata"]["cost"] == {
        "basis": "api_billing",
        "currency": None,
        "amount": None,
        "reported": False,
    }


def test_deadline_gracefully_interrupts_turn_and_preserves_partial_activities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_root = tmp_path / "profiles"
    _profile(profile_root)
    monkeypatch.setenv("SHEPHERD_CODEX_PROFILE_ROOT", str(profile_root))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = CodexAgentProvider(
        prompt="interrupt fixture",
        budget_seconds=1,
        _test_app_server_argv=(sys.executable, "-B", str(FAKE_APP_SERVER)),
        _test_app_server_env={"SHEPHERD_CODEX_SPIKE_SCENARIO": "interrupt"},
        _allow_fake_runtime=True,
    )

    with pytest.raises(ProviderInvocationError) as caught:
        provider.execute(
            None,
            SimpleNamespace(),
            SimpleNamespace(),
            {},
            execution=SimpleNamespace(
                working_path=workspace,
                identity=SimpleNamespace(scope_instance_id="interrupt-run", scope_name="fixture"),
            ),
            confinement=ConfinementSpec.permissive_for(workspace),
        )

    assert caught.value.activity_manifest is not None
    assert caught.value.activity_manifest.terminal_seen is True
    assert caught.value.activity_manifest.terminal_kind == "interrupted"
    assert any(activity.kind == "turn.interrupt.requested" for activity in caught.value.provider_activities)
    assert caught.value.outcome["file_effect_reconciliation"]["complete"] is True


def test_malformed_app_server_frame_is_accounted_and_cannot_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_root = tmp_path / "profiles"
    _profile(profile_root)
    monkeypatch.setenv("SHEPHERD_CODEX_PROFILE_ROOT", str(profile_root))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = CodexAgentProvider(
        prompt="malformed fixture",
        budget_seconds=1,
        _test_app_server_argv=(sys.executable, "-B", str(FAKE_APP_SERVER)),
        _test_app_server_env={"SHEPHERD_CODEX_SPIKE_SCENARIO": "malformed"},
        _allow_fake_runtime=True,
    )

    with pytest.raises(ProviderInvocationError) as caught:
        provider.execute(
            None,
            SimpleNamespace(),
            SimpleNamespace(),
            {},
            execution=SimpleNamespace(
                working_path=workspace,
                identity=SimpleNamespace(scope_instance_id="malformed-run", scope_name="fixture"),
            ),
            confinement=ConfinementSpec.permissive_for(workspace),
        )

    assert caught.value.activity_manifest is not None
    assert caught.value.activity_manifest.complete is False
    assert caught.value.activity_manifest.terminal_seen is False
    assert any(activity.category == "malformed" for activity in caught.value.provider_activities)


def test_unknown_server_request_is_recorded_then_refused_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_root = tmp_path / "profiles"
    _profile(profile_root)
    monkeypatch.setenv("SHEPHERD_CODEX_PROFILE_ROOT", str(profile_root))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = CodexAgentProvider(
        prompt="unknown request fixture",
        budget_seconds=1,
        _test_app_server_argv=(sys.executable, "-B", str(FAKE_APP_SERVER)),
        _test_app_server_env={"SHEPHERD_CODEX_SPIKE_SCENARIO": "unknown-request"},
        _allow_fake_runtime=True,
    )

    with pytest.raises(ProviderInvocationError) as caught:
        provider.execute(
            None,
            SimpleNamespace(),
            SimpleNamespace(),
            {},
            execution=SimpleNamespace(
                working_path=workspace,
                identity=SimpleNamespace(scope_instance_id="unknown-request-run", scope_name="fixture"),
            ),
            confinement=ConfinementSpec.permissive_for(workspace),
        )

    request = next(activity for activity in caught.value.provider_activities if activity.category == "server_request")
    refusal = next(
        activity for activity in caught.value.provider_activities if activity.kind == "server_request.refused"
    )
    assert request.method == "experimental/requestApproval"
    assert refusal.sequence == request.sequence + 1
    assert refusal.previous_record_digest == request.record_digest
    assert caught.value.activity_manifest is not None
    assert caught.value.activity_manifest.complete is False
    assert "sk-spike-secret" not in json.dumps(
        [activity.as_wire_record() for activity in caught.value.provider_activities], sort_keys=True
    )


@pytest.mark.parametrize("profile_location", ["profile_root", "auth_target"])
def test_provider_refuses_auth_state_overlapping_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile_location: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_root = workspace / "profiles" if profile_location == "profile_root" else tmp_path / "profiles"
    _profile(profile_root)
    if profile_location == "auth_target":
        target = workspace / "host-auth.json"
        target.write_text("{}", encoding="utf-8")
        auth = profile_root / "default" / "credential" / "auth.json"
        auth.unlink()
        auth.symlink_to(target)
    monkeypatch.setenv("SHEPHERD_CODEX_PROFILE_ROOT", str(profile_root))

    provider = CodexAgentProvider(prompt="must refuse before launch")
    with pytest.raises(ProviderInvocationError) as caught:
        provider.execute(
            None,
            SimpleNamespace(),
            SimpleNamespace(),
            {},
            execution=SimpleNamespace(
                working_path=workspace,
                identity=SimpleNamespace(scope_instance_id="profile-overlap", scope_name="fixture"),
            ),
            confinement=ConfinementSpec.permissive_for(workspace),
        )

    assert type(caught.value.__cause__).__name__ == "CodexProviderError"
    assert not caught.value.provider_activities
