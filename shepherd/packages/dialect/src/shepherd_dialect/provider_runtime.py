"""Dialect-native provider invocation evidence.

This module is intentionally provider-SDK-free. It defines the durable event
shape used by provider-backed ``ExecutionProvider`` implementations and the
pure projections into vcs-core observations and shepherd2 trace facts.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vcs_core.spi import ObservationDraft

if TYPE_CHECKING:
    from shepherd_dialect.provider_activity import ProviderActivity, ProviderActivityManifest

PROVIDER_INVOCATION_STARTED = "provider.invocation.started"
PROVIDER_INVOCATION_COMPLETED = "provider.invocation.completed"
PROVIDER_INVOCATION_FAILED = "provider.invocation.failed"
MODEL_CALL = "model.call"
MODEL_TURN = "model.turn"
TOOL_CALL_STARTED = "tool.call.started"
TOOL_CALL_COMPLETED = "tool.call.completed"
TOOL_CALL_REJECTED = "tool.call.rejected"

PROVIDER_EVENT_KINDS = frozenset(
    {
        PROVIDER_INVOCATION_STARTED,
        PROVIDER_INVOCATION_COMPLETED,
        PROVIDER_INVOCATION_FAILED,
        MODEL_CALL,
        MODEL_TURN,
        TOOL_CALL_STARTED,
        TOOL_CALL_COMPLETED,
        TOOL_CALL_REJECTED,
    }
)

PROVIDER_INVOCATION_MECHANISM = "shepherd.provider_invocation"
PROVIDER_EVIDENCE_KIND_STARTED = f"{PROVIDER_INVOCATION_MECHANISM}:started"
PROVIDER_EVIDENCE_KIND_COMPLETED = f"{PROVIDER_INVOCATION_MECHANISM}:completed"
PROVIDER_EVIDENCE_KIND_FAILED = f"{PROVIDER_INVOCATION_MECHANISM}:failed"
PROVIDER_EVIDENCE_KIND_MODEL_CALL = f"{PROVIDER_INVOCATION_MECHANISM}:model-call"
PROVIDER_EVIDENCE_KIND_MODEL_TURN = f"{PROVIDER_INVOCATION_MECHANISM}:model-turn"
PROVIDER_EVIDENCE_KIND_TOOL_CALL = f"{PROVIDER_INVOCATION_MECHANISM}:tool-call"
PROVIDER_EVIDENCE_KIND_TOOL_RESULT = f"{PROVIDER_INVOCATION_MECHANISM}:tool-result"
PROVIDER_EVIDENCE_KIND_TOOL_REJECTED = f"{PROVIDER_INVOCATION_MECHANISM}:tool-rejected"

PROVIDER_EVIDENCE_KIND_BY_EVENT_KIND = {
    PROVIDER_INVOCATION_STARTED: PROVIDER_EVIDENCE_KIND_STARTED,
    PROVIDER_INVOCATION_COMPLETED: PROVIDER_EVIDENCE_KIND_COMPLETED,
    PROVIDER_INVOCATION_FAILED: PROVIDER_EVIDENCE_KIND_FAILED,
    MODEL_CALL: PROVIDER_EVIDENCE_KIND_MODEL_CALL,
    MODEL_TURN: PROVIDER_EVIDENCE_KIND_MODEL_TURN,
    TOOL_CALL_STARTED: PROVIDER_EVIDENCE_KIND_TOOL_CALL,
    TOOL_CALL_COMPLETED: PROVIDER_EVIDENCE_KIND_TOOL_RESULT,
    TOOL_CALL_REJECTED: PROVIDER_EVIDENCE_KIND_TOOL_REJECTED,
}

PROVIDER_EVIDENCE_KINDS = frozenset(PROVIDER_EVIDENCE_KIND_BY_EVENT_KIND.values())

PROVIDER_INVOCATION_OUTCOME_SCHEMA = "shepherd/provider_invocation_outcome/v1"
CALL_SURFACE_EXECUTION_PROVIDER = "execution_provider"
DEFAULT_TEXT_EXCERPT_LIMIT = 10_000

PROVIDER_INVOCATION_STARTED_SCHEMA = "shepherd.provider.invocation.started.v1"
PROVIDER_INVOCATION_COMPLETED_SCHEMA = "shepherd.provider.invocation.completed.v1"
PROVIDER_INVOCATION_FAILED_SCHEMA = "shepherd.provider.invocation.failed.v1"
MODEL_CALL_SCHEMA = "shepherd.model.call.v1"
MODEL_TURN_SCHEMA = "shepherd.model.turn.v1"
TOOL_CALL_SCHEMA = "shepherd.tool.call.v1"
TOOL_RESULT_SCHEMA = "shepherd.tool.result.v1"

_FACT_SCHEMA_BY_EVENT_KIND = {
    PROVIDER_INVOCATION_STARTED: PROVIDER_INVOCATION_STARTED_SCHEMA,
    PROVIDER_INVOCATION_COMPLETED: PROVIDER_INVOCATION_COMPLETED_SCHEMA,
    PROVIDER_INVOCATION_FAILED: PROVIDER_INVOCATION_FAILED_SCHEMA,
    MODEL_CALL: MODEL_CALL_SCHEMA,
    MODEL_TURN: MODEL_TURN_SCHEMA,
    TOOL_CALL_STARTED: TOOL_CALL_SCHEMA,
    TOOL_CALL_COMPLETED: TOOL_RESULT_SCHEMA,
    TOOL_CALL_REJECTED: TOOL_RESULT_SCHEMA,
}

_FACT_KIND_LABEL_BY_EVENT_KIND = {
    PROVIDER_INVOCATION_STARTED: PROVIDER_INVOCATION_STARTED,
    PROVIDER_INVOCATION_COMPLETED: PROVIDER_INVOCATION_COMPLETED,
    PROVIDER_INVOCATION_FAILED: PROVIDER_INVOCATION_FAILED,
    MODEL_CALL: MODEL_CALL,
    MODEL_TURN: MODEL_TURN,
    TOOL_CALL_STARTED: "tool.call",
    TOOL_CALL_COMPLETED: "tool.result",
    TOOL_CALL_REJECTED: "tool.result",
}

_RESERVED_PAYLOAD_KEYS = frozenset(
    {
        "authority_ref",
        "candidate_ref",
        "candidate_refs",
        "custody_ref",
        "evidence_kind",
        "evidence_ref",
        "handoff_ref",
        "kind",
        "observation_id",
        "output_oid",
        "output_world_oid",
        "provider",
        "provider_id",
        "retained_output",
        "retention_ref",
        "settlement_ref",
        "trace_owner",
        "trace_owner_id",
        "workspace_output_world_oid",
    }
)

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


class ProviderEventError(ValueError):
    """Raised when a provider event violates the native evidence contract."""


@dataclass(frozen=True)
class ProviderEvent:
    """One redaction-safe provider invocation event."""

    kind: str
    provider_id: str
    invocation_id: str
    sequence: int
    event_id: str
    call_surface: str = CALL_SURFACE_EXECUTION_PROVIDER
    model: str | None = None
    tool_call_id: str | None = None
    caused_by_event_ids: tuple[str, ...] = ()
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in PROVIDER_EVENT_KINDS:
            raise ProviderEventError(f"unsupported provider event kind: {self.kind!r}")
        for field_name, value in (
            ("provider_id", self.provider_id),
            ("invocation_id", self.invocation_id),
            ("event_id", self.event_id),
            ("call_surface", self.call_surface),
        ):
            if not isinstance(value, str) or not value:
                raise ProviderEventError(f"{field_name} must be a non-empty string")
        if not isinstance(self.sequence, int) or self.sequence < 0:
            raise ProviderEventError("sequence must be a non-negative integer")
        if self.kind.startswith("tool.call.") and not self.tool_call_id:
            raise ProviderEventError(f"{self.kind} requires tool_call_id")
        if not isinstance(self.payload, Mapping):
            raise ProviderEventError("payload must be a mapping")
        reserved = sorted(_RESERVED_PAYLOAD_KEYS & set(self.payload))
        if reserved:
            raise ProviderEventError("provider event payload uses projection-owned fields: " + ", ".join(reserved))

    def stable_payload(self) -> dict[str, object]:
        """Return the stable event payload used by projections."""
        payload: dict[str, object] = {
            "kind": self.kind,
            "provider_id": self.provider_id,
            "invocation_id": self.invocation_id,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "call_surface": self.call_surface,
            "payload": dict(self.payload),
        }
        if self.model is not None:
            payload["model"] = self.model
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        if self.caused_by_event_ids:
            payload["caused_by_event_ids"] = list(self.caused_by_event_ids)
        return payload


@dataclass(frozen=True)
class ProviderInvocationResult:
    """Provider-level invocation result."""

    output_text: str = ""
    structured_output: Mapping[str, object] = field(default_factory=dict)
    session_id: str | None = None
    usage: Mapping[str, object] = field(default_factory=dict)
    events: tuple[ProviderEvent, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionProviderResult:
    """Dialect-native run-provider result understood by ShepherdRunDriver."""

    outcome: Mapping[str, object]
    provider_events: tuple[ProviderEvent, ...] = ()
    provider_activities: tuple[ProviderActivity, ...] = ()
    activity_manifest: ProviderActivityManifest | None = None


class ProviderInvocationError(RuntimeError):
    """Raised when a provider invocation fails after producing provider events."""

    def __init__(
        self,
        message: str,
        *,
        provider_events: tuple[ProviderEvent, ...] = (),
        provider_activities: tuple[ProviderActivity, ...] = (),
        activity_manifest: ProviderActivityManifest | None = None,
        outcome: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_events = provider_events
        self.provider_activities = provider_activities
        self.activity_manifest = activity_manifest
        self.outcome = dict(outcome or {})
        self.runtime_operation_id: str | None = None

    @property
    def driver_observations(self) -> tuple[ObservationDraft, ...]:
        """Provider events projected for the VcsCore operation log."""
        return observations_from_provider_events(self.provider_events)


def digest_text(value: str | bytes | None) -> str | None:
    """Return a stable digest for potentially sensitive text."""
    if value is None:
        return None
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def bounded_excerpt(value: str | bytes | None, *, limit: int = DEFAULT_TEXT_EXCERPT_LIMIT) -> str | None:
    """Return a bounded suffix excerpt for diagnostic output."""
    if value is None:
        return None
    if limit < 0:
        raise ValueError("limit must be non-negative")
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    return text[-limit:] if limit else ""


def redacted_text_payload(
    value: str | bytes | None,
    *,
    field: str,
    excerpt_limit: int = DEFAULT_TEXT_EXCERPT_LIMIT,
) -> dict[str, object]:
    """Return digest/length + a **bounded verbatim suffix excerpt** of text.

    "redacted" here means *bounded*, not secret-scrubbed: the excerpt is a raw
    tail slice (up to ``excerpt_limit`` chars) with no token pattern matching, so
    a secret a provider echoes to stdout/stderr within that window rides forward
    into the durable trace — a channel the scratch scrub does not touch. Callers
    seeding host credentials must not rely on this to keep a leaked token out of
    the trace; bound the excerpt and treat provider output as untrusted.
    """
    if value is None:
        return {
            f"{field}_digest": None,
            f"{field}_length": 0,
            f"{field}_excerpt": None,
        }
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    return {
        f"{field}_digest": digest_text(text),
        f"{field}_length": len(text),
        f"{field}_excerpt": bounded_excerpt(text, limit=excerpt_limit),
    }


def digest_jsonable(value: object) -> str:
    """Return a stable digest for a JSON-like object."""
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=repr)
    except TypeError:
        raw = repr(value)
    return f"sha256:{hashlib.sha256(raw.encode('utf-8', errors='replace')).hexdigest()}"


def provider_invocation_outcome(
    result: ProviderInvocationResult,
    *,
    provider_id: str,
    invocation_id: str,
    terminal: str = "success",
    output_excerpt_limit: int = DEFAULT_TEXT_EXCERPT_LIMIT,
) -> dict[str, object]:
    """Return the canonical provider-backed run outcome mapping."""
    return {
        "schema": PROVIDER_INVOCATION_OUTCOME_SCHEMA,
        "provider_id": provider_id,
        "terminal": terminal,
        "invocation_id": invocation_id,
        "event_count": len(result.events),
        "output_text_digest": digest_text(result.output_text) if result.output_text else None,
        "output_text_excerpt": bounded_excerpt(result.output_text, limit=output_excerpt_limit)
        if result.output_text
        else None,
        "structured_output": dict(result.structured_output) if result.structured_output else None,
        "session_id": result.session_id,
        "usage": dict(result.usage),
        "metadata": dict(result.metadata),
    }


def provider_events_from_execution_result(value: object) -> tuple[ProviderEvent, ...]:
    """Extract native provider events from a supported provider result object."""
    if isinstance(value, ExecutionProviderResult):
        return value.provider_events
    raw = getattr(value, "provider_events", ())
    if not isinstance(raw, tuple):
        return ()
    return tuple(event for event in raw if isinstance(event, ProviderEvent))


def outcome_mapping_from_execution_result(value: object) -> dict[str, object]:
    """Return the mapping stored in `portable_core.outcome`."""
    if isinstance(value, ExecutionProviderResult):
        return dict(value.outcome)
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"provider returned {type(value).__name__}, expected mapping or ExecutionProviderResult")


def observation_from_provider_event(event: ProviderEvent) -> ObservationDraft:
    """Project one provider event to a vcs-core observation."""
    evidence_kind = PROVIDER_EVIDENCE_KIND_BY_EVENT_KIND[event.kind]
    return ObservationDraft(
        observation_id=_observation_id(event),
        evidence_kind=evidence_kind,
        stable_observation=event.stable_payload(),
        mechanism=PROVIDER_INVOCATION_MECHANISM,
        correlation_id=event.invocation_id,
        metadata={"source": "shepherd_dialect.provider_runtime"},
    )


def observations_from_provider_events(events: tuple[ProviderEvent, ...]) -> tuple[ObservationDraft, ...]:
    """Project provider events to vcs-core observations."""
    return tuple(observation_from_provider_event(event) for event in events)


def provider_events_from_observations(observations: tuple[ObservationDraft, ...]) -> tuple[ProviderEvent, ...]:
    """Rehydrate provider events from provider observations emitted by ShepherdRunDriver."""
    events: list[ProviderEvent] = []
    for observation in observations:
        if observation.mechanism != PROVIDER_INVOCATION_MECHANISM:
            continue
        payload = observation.stable_observation
        raw_payload = payload.get("payload", {})
        caused_by = payload.get("caused_by_event_ids", ())
        events.append(
            ProviderEvent(
                kind=str(payload["kind"]),
                provider_id=str(payload["provider_id"]),
                invocation_id=str(payload["invocation_id"]),
                event_id=str(payload["event_id"]),
                sequence=int(payload["sequence"]),
                call_surface=str(payload.get("call_surface") or CALL_SURFACE_EXECUTION_PROVIDER),
                model=payload.get("model") if isinstance(payload.get("model"), str) else None,
                tool_call_id=payload.get("tool_call_id") if isinstance(payload.get("tool_call_id"), str) else None,
                caused_by_event_ids=tuple(str(item) for item in caused_by)
                if isinstance(caused_by, list | tuple)
                else (),
                payload=dict(raw_payload) if isinstance(raw_payload, Mapping) else {},
            )
        )
    return tuple(events)


def fact_from_provider_event(event: ProviderEvent, *, append_local_id: str | None = None) -> Any:
    """Project one provider event to a shepherd2 FactDraft.

    The returned draft does not include a trace owner; callers supply owner
    identity when they append the fact to a trace store.
    """
    from shepherd2 import FactDraft

    payload = event.stable_payload()
    if event.kind == TOOL_CALL_REJECTED:
        payload["status"] = "rejected"
    elif event.kind == TOOL_CALL_COMPLETED:
        event_payload = event.payload
        success = event_payload.get("success", True) if isinstance(event_payload, Mapping) else True
        payload.setdefault("status", "ok" if success else "error")
    return FactDraft(
        mode="capture",
        schema_ref=_FACT_SCHEMA_BY_EVENT_KIND[event.kind],
        kind_label=_FACT_KIND_LABEL_BY_EVENT_KIND[event.kind],
        payload=payload,
        append_local_id=append_local_id,
    )


def facts_from_provider_events(
    events: tuple[ProviderEvent, ...],
    *,
    append_local_id_prefix: str = "provider-event",
) -> tuple[Any, ...]:
    """Project provider events to shepherd2 FactDrafts."""
    return tuple(
        fact_from_provider_event(event, append_local_id=f"{append_local_id_prefix}-{index}")
        for index, event in enumerate(events, start=1)
    )


def _observation_id(event: ProviderEvent) -> str:
    safe_invocation = _SAFE_ID_RE.sub("-", event.invocation_id)
    return f"provider-{safe_invocation}-{event.sequence}"


__all__ = [
    "CALL_SURFACE_EXECUTION_PROVIDER",
    "DEFAULT_TEXT_EXCERPT_LIMIT",
    "MODEL_CALL",
    "MODEL_CALL_SCHEMA",
    "MODEL_TURN",
    "MODEL_TURN_SCHEMA",
    "PROVIDER_EVENT_KINDS",
    "PROVIDER_EVIDENCE_KINDS",
    "PROVIDER_EVIDENCE_KIND_BY_EVENT_KIND",
    "PROVIDER_EVIDENCE_KIND_COMPLETED",
    "PROVIDER_EVIDENCE_KIND_FAILED",
    "PROVIDER_EVIDENCE_KIND_MODEL_CALL",
    "PROVIDER_EVIDENCE_KIND_MODEL_TURN",
    "PROVIDER_EVIDENCE_KIND_STARTED",
    "PROVIDER_EVIDENCE_KIND_TOOL_CALL",
    "PROVIDER_EVIDENCE_KIND_TOOL_REJECTED",
    "PROVIDER_EVIDENCE_KIND_TOOL_RESULT",
    "PROVIDER_INVOCATION_COMPLETED",
    "PROVIDER_INVOCATION_COMPLETED_SCHEMA",
    "PROVIDER_INVOCATION_FAILED",
    "PROVIDER_INVOCATION_FAILED_SCHEMA",
    "PROVIDER_INVOCATION_MECHANISM",
    "PROVIDER_INVOCATION_OUTCOME_SCHEMA",
    "PROVIDER_INVOCATION_STARTED",
    "PROVIDER_INVOCATION_STARTED_SCHEMA",
    "TOOL_CALL_COMPLETED",
    "TOOL_CALL_REJECTED",
    "TOOL_CALL_SCHEMA",
    "TOOL_CALL_STARTED",
    "TOOL_RESULT_SCHEMA",
    "ExecutionProviderResult",
    "ProviderEvent",
    "ProviderEventError",
    "ProviderInvocationError",
    "ProviderInvocationResult",
    "bounded_excerpt",
    "digest_jsonable",
    "digest_text",
    "fact_from_provider_event",
    "facts_from_provider_events",
    "observation_from_provider_event",
    "observations_from_provider_events",
    "outcome_mapping_from_execution_result",
    "provider_events_from_execution_result",
    "provider_events_from_observations",
    "provider_invocation_outcome",
    "redacted_text_payload",
]
