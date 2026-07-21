"""Loss-detecting native activity evidence for streaming providers.

``ProviderEvent`` is Shepherd's closed semantic vocabulary.  This module is
the complementary open ledger: one record can account for any provider-native
transport frame, including protocol additions Shepherd does not understand
yet.  Activities are redaction-safe summaries; raw provider payloads are
represented only by byte lengths and SHA-256 digests.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

ACTIVITY_SCHEMA_VERSION = "shepherd.provider_activity.v1"
ACTIVITY_MANIFEST_SCHEMA_VERSION = "shepherd.provider_activity_manifest.v1"

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:/-]+$")
_FORBIDDEN_PAYLOAD_KEYS = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|cookie|password|secret|raw|content|text|delta|diff|command|arguments|result)$",
    re.IGNORECASE,
)
_CREDENTIAL_SHAPED_VALUE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}\b|\bbearer\s+[A-Za-z0-9._~+/-]{8,}|://[^/@\s]+:[^/@\s]+@)",
    re.IGNORECASE,
)


class ProviderActivityError(ValueError):
    """Raised when a native activity ledger is malformed or unsafe."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _json_safe(value: object, *, path: str = "payload") -> object:
    """Validate the deliberately small durable activity payload vocabulary."""
    if isinstance(value, str):
        if _CREDENTIAL_SHAPED_VALUE.search(value):
            raise ProviderActivityError(f"{path} contains credential-shaped text")
        return value
    if value is None or isinstance(value, int | float | bool):
        return value
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for raw_key, child in value.items():
            if not isinstance(raw_key, str) or not raw_key:
                raise ProviderActivityError(f"{path} keys must be non-empty strings")
            if _FORBIDDEN_PAYLOAD_KEYS.search(raw_key):
                raise ProviderActivityError(f"unsafe activity payload key: {path}.{raw_key}")
            out[raw_key] = _json_safe(child, path=f"{path}.{raw_key}")
        return out
    if isinstance(value, list | tuple):
        return [_json_safe(child, path=f"{path}[]") for child in value]
    raise ProviderActivityError(f"{path} contains unsupported {type(value).__name__}")


@dataclass(frozen=True)
class ProviderActivity:
    """One immutable, hash-chained provider-native activity."""

    provider_id: str
    invocation_id: str
    sequence: int
    category: str
    kind: str
    event_id: str
    raw_length: int
    raw_digest: str
    record_digest: str
    previous_record_digest: str | None = None
    method: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)
    source: str = "provider.transport"
    schema_version: str = ACTIVITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ACTIVITY_SCHEMA_VERSION:
            raise ProviderActivityError(f"unsupported activity schema: {self.schema_version!r}")
        for identifier_name, identifier_value in (
            ("provider_id", self.provider_id),
            ("invocation_id", self.invocation_id),
            ("category", self.category),
            ("kind", self.kind),
            ("event_id", self.event_id),
            ("source", self.source),
        ):
            if (
                not isinstance(identifier_value, str)
                or not identifier_value
                or not _SAFE_NAME.fullmatch(identifier_value)
            ):
                raise ProviderActivityError(f"{identifier_name} must be a non-empty safe identifier")
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 0:
            raise ProviderActivityError("activity sequence must be a non-negative integer")
        if not isinstance(self.raw_length, int) or isinstance(self.raw_length, bool) or self.raw_length < 0:
            raise ProviderActivityError("activity raw_length must be a non-negative integer")
        for digest_name, digest_value in (("raw_digest", self.raw_digest), ("record_digest", self.record_digest)):
            if not isinstance(digest_value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest_value):
                raise ProviderActivityError(f"{digest_name} must be a SHA-256 digest")
        if self.previous_record_digest is not None and not re.fullmatch(
            r"sha256:[0-9a-f]{64}", self.previous_record_digest
        ):
            raise ProviderActivityError("previous_record_digest must be null or a SHA-256 digest")
        for correlation_name, correlation_value in (
            ("method", self.method),
            ("thread_id", self.thread_id),
            ("turn_id", self.turn_id),
            ("item_id", self.item_id),
        ):
            if correlation_value is not None and (not isinstance(correlation_value, str) or not correlation_value):
                raise ProviderActivityError(f"{correlation_name} must be null or a non-empty string")
        safe_payload = _json_safe(self.payload)
        if not isinstance(safe_payload, dict):
            raise ProviderActivityError("activity payload must be a mapping")
        if self.record_digest != self.expected_record_digest():
            raise ProviderActivityError(f"activity {self.sequence} record digest mismatch")

    def unsigned_wire_record(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "source": self.source,
            "provider_id": self.provider_id,
            "invocation_id": self.invocation_id,
            "sequence": self.sequence,
            "category": self.category,
            "kind": self.kind,
            "event_id": self.event_id,
            "method": self.method,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "item_id": self.item_id,
            "raw_length": self.raw_length,
            "raw_digest": self.raw_digest,
            "previous_record_digest": self.previous_record_digest,
            "payload": dict(self.payload),
        }
        return payload

    def expected_record_digest(self) -> str:
        return _digest_bytes(_canonical_bytes(self.unsigned_wire_record()))

    def as_wire_record(self) -> dict[str, object]:
        return {**self.unsigned_wire_record(), "record_digest": self.record_digest}

    @classmethod
    def from_wire_record(cls, value: Mapping[str, object]) -> ProviderActivity:
        _reject_unknown_fields(
            value,
            {
                "schema_version",
                "source",
                "provider_id",
                "invocation_id",
                "sequence",
                "category",
                "kind",
                "event_id",
                "method",
                "thread_id",
                "turn_id",
                "item_id",
                "raw_length",
                "raw_digest",
                "previous_record_digest",
                "record_digest",
                "payload",
            },
            "activity",
        )
        payload = value.get("payload", {})
        if not isinstance(payload, Mapping):
            raise ProviderActivityError("activity payload must be a mapping")
        return cls(
            schema_version=str(value.get("schema_version") or ""),
            source=str(value.get("source") or ""),
            provider_id=str(value.get("provider_id") or ""),
            invocation_id=str(value.get("invocation_id") or ""),
            sequence=_required_int(value, "sequence"),
            category=str(value.get("category") or ""),
            kind=str(value.get("kind") or ""),
            event_id=str(value.get("event_id") or ""),
            method=_optional_string(value, "method"),
            thread_id=_optional_string(value, "thread_id"),
            turn_id=_optional_string(value, "turn_id"),
            item_id=_optional_string(value, "item_id"),
            raw_length=_required_int(value, "raw_length"),
            raw_digest=str(value.get("raw_digest") or ""),
            previous_record_digest=_optional_string(value, "previous_record_digest"),
            record_digest=str(value.get("record_digest") or ""),
            payload=dict(payload),
        )


@dataclass(frozen=True)
class ProviderActivityManifest:
    """Terminal integrity claim for one activity stream."""

    provider_id: str
    invocation_id: str
    activity_count: int
    ingress_count: int
    last_record_digest: str | None
    terminal_seen: bool
    terminal_kind: str
    category_counts: Mapping[str, int]
    complete: bool
    schema_version: str = ACTIVITY_MANIFEST_SCHEMA_VERSION

    def as_wire_record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider_id": self.provider_id,
            "invocation_id": self.invocation_id,
            "activity_count": self.activity_count,
            "ingress_count": self.ingress_count,
            "last_record_digest": self.last_record_digest,
            "terminal_seen": self.terminal_seen,
            "terminal_kind": self.terminal_kind,
            "category_counts": dict(self.category_counts),
            "complete": self.complete,
        }

    @classmethod
    def from_wire_record(cls, value: Mapping[str, object]) -> ProviderActivityManifest:
        _reject_unknown_fields(
            value,
            {
                "schema_version",
                "provider_id",
                "invocation_id",
                "activity_count",
                "ingress_count",
                "last_record_digest",
                "terminal_seen",
                "terminal_kind",
                "category_counts",
                "complete",
            },
            "activity manifest",
        )
        counts = value.get("category_counts")
        if not isinstance(counts, Mapping) or not all(
            isinstance(key, str) and isinstance(count, int) and not isinstance(count, bool) and count >= 0
            for key, count in counts.items()
        ):
            raise ProviderActivityError("manifest category_counts must be non-negative integer counts")
        manifest = cls(
            schema_version=str(value.get("schema_version") or ""),
            provider_id=str(value.get("provider_id") or ""),
            invocation_id=str(value.get("invocation_id") or ""),
            activity_count=_required_int(value, "activity_count"),
            ingress_count=_required_int(value, "ingress_count"),
            last_record_digest=_optional_string(value, "last_record_digest"),
            terminal_seen=value.get("terminal_seen") is True,
            terminal_kind=str(value.get("terminal_kind") or ""),
            category_counts={str(key): int(count) for key, count in counts.items()},
            complete=value.get("complete") is True,
        )
        if manifest.schema_version != ACTIVITY_MANIFEST_SCHEMA_VERSION:
            raise ProviderActivityError(f"unsupported activity manifest schema: {manifest.schema_version!r}")
        if not manifest.provider_id or not manifest.invocation_id or not manifest.terminal_kind:
            raise ProviderActivityError("manifest identity and terminal_kind must be non-empty")
        return manifest


ActivityProjector = Callable[[Mapping[str, Any] | None, str], Mapping[str, object]]
ActivitySink = Callable[[ProviderActivity], None]


class ProviderActivityLedger:
    """Thread-safe producer ledger that emits before provider routing proceeds."""

    def __init__(
        self,
        *,
        provider_id: str,
        invocation_id: str,
        source: str,
        projector: ActivityProjector,
        on_activity: ActivitySink | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.invocation_id = invocation_id
        self.source = source
        self._projector = projector
        self._on_activity = on_activity
        self._lock = threading.RLock()
        self._activities: list[ProviderActivity] = []

    @property
    def activities(self) -> tuple[ProviderActivity, ...]:
        with self._lock:
            return tuple(self._activities)

    def append_ingress(self, raw_line: str | bytes) -> ProviderActivity:
        raw = raw_line.encode("utf-8", errors="replace") if isinstance(raw_line, str) else raw_line
        try:
            parsed = json.loads(raw)
            message = parsed if isinstance(parsed, dict) else None
            parse_state = "json_object" if message is not None else "json_non_object"
        except json.JSONDecodeError:
            message = None
            parse_state = "malformed_json"
        projected = dict(self._projector(message, parse_state))
        return self._append(projected, raw=raw)

    def append_control(self, *, kind: str, payload: Mapping[str, object]) -> ProviderActivity:
        """Append a safe local decision adjacent to the ingress that caused it."""
        projected = {"category": "control", "kind": kind, **dict(payload)}
        return self._append(projected, raw=b"")

    def _append(self, projected: dict[str, object], *, raw: bytes) -> ProviderActivity:
        with self._lock:
            sequence = len(self._activities)
            previous = self._activities[-1].record_digest if self._activities else None
            unsigned: dict[str, object] = {
                "schema_version": ACTIVITY_SCHEMA_VERSION,
                "source": self.source,
                "provider_id": self.provider_id,
                "invocation_id": self.invocation_id,
                "sequence": sequence,
                "category": str(projected.pop("category")),
                "kind": str(projected.pop("kind")),
                "event_id": f"{self.invocation_id}:activity:{sequence}",
                "method": projected.pop("method", None),
                "thread_id": projected.pop("thread_id", None),
                "turn_id": projected.pop("turn_id", None),
                "item_id": projected.pop("item_id", None),
                "raw_length": len(raw),
                "raw_digest": _digest_bytes(raw),
                "previous_record_digest": previous,
                "payload": projected,
            }
            activity = ProviderActivity(record_digest=_digest_bytes(_canonical_bytes(unsigned)), **unsigned)  # type: ignore[arg-type]
            self._activities.append(activity)
            if self._on_activity is not None:
                self._on_activity(activity)
            return activity

    def manifest(self, *, terminal_kind: str, terminal_seen: bool, complete: bool = True) -> ProviderActivityManifest:
        activities = self.activities
        counts = Counter(activity.category for activity in activities)
        return ProviderActivityManifest(
            provider_id=self.provider_id,
            invocation_id=self.invocation_id,
            activity_count=len(activities),
            ingress_count=sum(1 for activity in activities if activity.category != "control"),
            last_record_digest=activities[-1].record_digest if activities else None,
            terminal_seen=terminal_seen,
            terminal_kind=terminal_kind,
            category_counts=dict(sorted(counts.items())),
            complete=complete,
        )


def validate_activity_stream(
    activities: Iterable[ProviderActivity],
    manifest: ProviderActivityManifest,
    *,
    require_complete: bool = True,
) -> tuple[ProviderActivity, ...]:
    """Verify identity, sequence, chain, counts, and terminal completeness."""
    records = tuple(activities)
    if require_complete:
        if not manifest.complete:
            raise ProviderActivityError("provider activity manifest is incomplete")
        if not manifest.terminal_seen:
            raise ProviderActivityError("provider activity manifest has no provider terminal")
    if len(records) != manifest.activity_count:
        raise ProviderActivityError("provider activity count does not match manifest")
    previous: str | None = None
    for sequence, activity in enumerate(records):
        if activity.record_digest != activity.expected_record_digest():
            raise ProviderActivityError(f"provider activity record digest mismatch at {sequence}")
        if activity.provider_id != manifest.provider_id or activity.invocation_id != manifest.invocation_id:
            raise ProviderActivityError("provider activity identity does not match manifest")
        if activity.sequence != sequence:
            raise ProviderActivityError(f"provider activity sequence gap at {sequence}")
        if activity.previous_record_digest != previous:
            raise ProviderActivityError(f"provider activity chain break at {sequence}")
        previous = activity.record_digest
    if previous != manifest.last_record_digest:
        raise ProviderActivityError("provider activity terminal digest does not match manifest")
    counts = Counter(activity.category for activity in records)
    if dict(sorted(counts.items())) != dict(sorted(manifest.category_counts.items())):
        raise ProviderActivityError("provider activity category counts do not match manifest")
    ingress_count = sum(1 for activity in records if activity.category != "control")
    if ingress_count != manifest.ingress_count:
        raise ProviderActivityError("provider ingress count does not match manifest")
    return records


def _required_int(value: Mapping[str, object], key: str) -> int:
    raw = value.get(key)
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise ProviderActivityError(f"{key} must be a non-negative integer")
    return raw


def _reject_unknown_fields(value: Mapping[str, object], expected: set[str], label: str) -> None:
    unknown = sorted(set(value).difference(expected))
    if unknown:
        raise ProviderActivityError(f"{label} has unsupported fields: {', '.join(unknown)}")


def _optional_string(value: Mapping[str, object], key: str) -> str | None:
    raw = value.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ProviderActivityError(f"{key} must be null or a non-empty string")
    return raw


__all__ = [
    "ACTIVITY_MANIFEST_SCHEMA_VERSION",
    "ACTIVITY_SCHEMA_VERSION",
    "ProviderActivity",
    "ProviderActivityError",
    "ProviderActivityLedger",
    "ProviderActivityManifest",
    "validate_activity_stream",
]
