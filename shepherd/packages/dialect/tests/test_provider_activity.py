from __future__ import annotations

import pytest

from shepherd_dialect.provider_activity import (
    ProviderActivityError,
    ProviderActivityLedger,
    validate_activity_stream,
)


def _project(message: object, parse_state: str) -> dict[str, object]:
    raw = message if isinstance(message, dict) else {}
    return {
        "category": "notification" if raw.get("method") else "malformed",
        "kind": "notification.fixture" if raw.get("method") else f"transport.{parse_state}",
        "method": raw.get("method") if isinstance(raw.get("method"), str) else None,
        "payload_digest": "sha256:" + "0" * 64,
    }


def test_activity_ledger_accounts_raw_frames_without_retaining_payload() -> None:
    ledger = ProviderActivityLedger(
        provider_id="fixture",
        invocation_id="fixture:1",
        source="fixture.transport",
        projector=_project,
    )
    ledger.append_ingress('{"method":"future/event","params":{"apiKey":"sk-do-not-retain"}}\n')
    ledger.append_control(kind="approval.declined", payload={"decision": "decline"})
    ledger.append_ingress("not-json\n")
    manifest = ledger.manifest(terminal_kind="completed", terminal_seen=True)

    activities = validate_activity_stream(ledger.activities, manifest)
    assert [activity.sequence for activity in activities] == [0, 1, 2]
    assert activities[1].category == "control"
    assert activities[2].previous_record_digest == activities[1].record_digest
    serialized = str([activity.as_wire_record() for activity in activities])
    assert "sk-do-not-retain" not in serialized
    assert manifest.ingress_count == 2


def test_activity_validation_rejects_a_chain_break() -> None:
    ledger = ProviderActivityLedger(
        provider_id="fixture",
        invocation_id="fixture:1",
        source="fixture.transport",
        projector=_project,
    )
    ledger.append_ingress('{"method":"one"}\n')
    ledger.append_ingress('{"method":"two"}\n')
    manifest = ledger.manifest(terminal_kind="completed", terminal_seen=True)
    with pytest.raises(ProviderActivityError, match=r"sequence gap|chain break"):
        validate_activity_stream(tuple(reversed(ledger.activities)), manifest)


def test_activity_validation_recomputes_digest_after_payload_mutation() -> None:
    ledger = ProviderActivityLedger(
        provider_id="fixture",
        invocation_id="fixture:1",
        source="fixture.transport",
        projector=_project,
    )
    activity = ledger.append_ingress('{"method":"one"}\n')
    manifest = ledger.manifest(terminal_kind="completed", terminal_seen=True)
    assert isinstance(activity.payload, dict)
    activity.payload["tampered"] = True

    with pytest.raises(ProviderActivityError, match="record digest mismatch"):
        validate_activity_stream(ledger.activities, manifest)


def test_activity_payload_rejects_secret_bearing_field_names() -> None:
    def unsafe(_message: object, _parse_state: str) -> dict[str, object]:
        return {
            "category": "notification",
            "kind": "notification.unsafe",
            "api_key": "sk-never",
        }

    ledger = ProviderActivityLedger(
        provider_id="fixture",
        invocation_id="fixture:1",
        source="fixture.transport",
        projector=unsafe,
    )
    with pytest.raises(ProviderActivityError, match="unsafe activity payload key"):
        ledger.append_ingress('{"method":"unsafe"}\n')


@pytest.mark.parametrize(
    "credential",
    [
        "sk-proj-synthetic-never-retain",
        "Bearer synthetic-token-value",
        "https://user:password@proxy.example",
    ],
)
def test_activity_payload_rejects_credential_shaped_values(credential: str) -> None:
    ledger = ProviderActivityLedger(
        provider_id="fixture",
        invocation_id="fixture:1",
        source="fixture.transport",
        projector=lambda _message, _state: {
            "category": "notification",
            "kind": "notification.unsafe",
            "note": credential,
        },
    )

    with pytest.raises(ProviderActivityError, match="credential-shaped"):
        ledger.append_ingress('{"method":"unsafe"}\n')


def test_wire_activity_rejects_unknown_same_version_fields() -> None:
    ledger = ProviderActivityLedger(
        provider_id="fixture",
        invocation_id="fixture:1",
        source="fixture.transport",
        projector=_project,
    )
    activity = ledger.append_ingress('{"method":"one"}\n')
    wire = {**activity.as_wire_record(), "future_unvalidated_field": True}

    with pytest.raises(ProviderActivityError, match="unsupported fields"):
        type(activity).from_wire_record(wire)
