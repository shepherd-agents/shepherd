"""Projection of Shepherd effect layers into commons-vcs objects.

The profile validator is structural. It checks encoded effect payloads,
stream-local event ordering, and caused-by edge consistency, but it does not
claim global stream-head admission. That belongs to a future recorder.
"""

from __future__ import annotations

import base64
import json
import math
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, is_dataclass
from typing import TYPE_CHECKING, Any, overload

from commons_vcs import Edge, Failure, Object, Profile, Repo, Resolver

if TYPE_CHECKING:
    from shepherd_core.scope.stream import EffectLayer

SHEPHERD_EFFECT_SCHEMA = "shepherd/effect/v1"
SHEPHERD_EVENT_SCHEMA = "shepherd/event/v1"
SHEPHERD_EFFECT_PROJECTION_VERSION = 1
SHEPHERD_EFFECT_ROLE = "shepherd.effect"
SHEPHERD_PREVIOUS_ROLE = "shepherd.previous"
SHEPHERD_CAUSED_BY_ROLE = "shepherd.caused_by"

_EVENT_EDGE_ROLES = frozenset(
    {
        SHEPHERD_EFFECT_ROLE,
        SHEPHERD_PREVIOUS_ROLE,
        SHEPHERD_CAUSED_BY_ROLE,
    }
)
_MISSING = object()


@dataclass(frozen=True)
class ProjectedEffectLayer:
    """The commons objects emitted for one Shepherd effect layer."""

    effect: Object
    event: Object

    @property
    def objects(self) -> tuple[Object, Object]:
        """Append order for this layer."""
        return (self.effect, self.event)


@dataclass(frozen=True)
class ProjectedEffectStream:
    """The commons objects emitted for an ordered Shepherd effect stream."""

    layers: tuple[ProjectedEffectLayer, ...]

    @property
    def effects(self) -> tuple[Object, ...]:
        return tuple(layer.effect for layer in self.layers)

    @property
    def events(self) -> tuple[Object, ...]:
        return tuple(layer.event for layer in self.layers)

    @property
    def objects(self) -> tuple[Object, ...]:
        return tuple(obj for layer in self.layers for obj in layer.objects)

    def __iter__(self) -> Iterator[Object]:
        return iter(self.objects)

    def __len__(self) -> int:
        return len(self.objects)

    @overload
    def __getitem__(self, index: int) -> Object: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[Object, ...]: ...

    def __getitem__(self, index: int | slice) -> Object | tuple[Object, ...]:
        return self.objects[index]


@dataclass(frozen=True)
class RecordedEffectLayer:
    """Result of appending one projected Shepherd layer to a commons stream."""

    stream_id: str
    previous_head: str | None
    new_head: str
    effect_id: str
    event_id: str
    head_ref: str


@dataclass(frozen=True)
class _PendingStreamAppend:
    stream_id: str
    expected_head: str | None
    effect_id: str
    event_id: str
    sequence: int


class ShepherdStreamConflictError(RuntimeError):
    """Raised when a Shepherd commons stream head changed concurrently."""


class ShepherdStreamRecoveryError(RuntimeError):
    """Raised when a pending Shepherd commons append cannot be recovered safely."""


class ShepherdCommonsRecorder:
    """Durably append Shepherd projected events to a commons-vcs stream head."""

    def __init__(self, repo: Repo) -> None:
        self._repo = repo
        self._backend = repo.backend

    @property
    def repo(self) -> Repo:
        """The commons-vcs repo used by this recorder."""
        return self._repo

    def append_layer(
        self,
        layer: EffectLayer,
        *,
        stream_id: str,
        expected_head: str | None | object = _MISSING,
        caused_by_index: Mapping[str, str] | None = None,
    ) -> RecordedEffectLayer:
        """Append one Shepherd effect layer and publish the stream head with CAS."""
        if not stream_id:
            raise ValueError("stream_id must be non-empty")
        self.recover_stream(stream_id)
        head_ref = self.stream_head_ref(stream_id)
        current_head = self._backend.get_ref(head_ref)
        previous_head = current_head if expected_head is _MISSING else _expect_optional_str(expected_head)
        if current_head != previous_head:
            raise ShepherdStreamConflictError(
                f"stream {stream_id!r} head is {current_head!r}, expected {previous_head!r}"
            )
        self._validate_layer_sequence(layer, stream_id=stream_id, previous_head=previous_head)
        projected = project_effect_layer(
            layer,
            stream_id=stream_id,
            previous_event_id=previous_head,
            caused_by_index=caused_by_index,
        )

        pending = _PendingStreamAppend(
            stream_id=stream_id,
            expected_head=previous_head,
            effect_id=projected.effect.id,
            event_id=projected.event.id,
            sequence=layer.sequence,
        )
        pending_ref = self.pending_append_ref(stream_id, projected.event.id)
        pending_json = self._encode_pending(pending)
        if not self._backend.compare_and_swap_ref(pending_ref, None, pending_json):
            raise ShepherdStreamRecoveryError(f"pending append already exists for {projected.event.id}")

        self._repo.append(projected.effect)
        self._repo.append(projected.event)
        if not self._backend.compare_and_swap_ref(head_ref, previous_head, projected.event.id):
            raise ShepherdStreamConflictError(f"stream {stream_id!r} head changed while appending {projected.event.id}")
        self._delete_pending_if_unchanged(pending_ref, pending_json)
        return RecordedEffectLayer(
            stream_id=stream_id,
            previous_head=previous_head,
            new_head=projected.event.id,
            effect_id=projected.effect.id,
            event_id=projected.event.id,
            head_ref=head_ref,
        )

    def recover_stream(self, stream_id: str) -> str | None:
        """Recover one stream's single pending append when the outcome is provable."""
        pending_refs = list(self._backend.list_refs(self.pending_append_prefix(stream_id)))
        if not pending_refs:
            return self._backend.get_ref(self.stream_head_ref(stream_id))
        if len(pending_refs) > 1:
            raise ShepherdStreamRecoveryError(f"multiple pending appends for stream {stream_id!r}")
        pending_ref = pending_refs[0]
        pending_json = self._backend.get_ref(pending_ref)
        if pending_json is None:
            return self._backend.get_ref(self.stream_head_ref(stream_id))
        pending = self._decode_pending(pending_json)
        if pending.stream_id != stream_id:
            raise ShepherdStreamRecoveryError("pending stream_id does not match recovery stream")
        head_ref = self.stream_head_ref(stream_id)
        current_head = self._backend.get_ref(head_ref)
        if current_head == pending.event_id:
            self._validate_pending_objects(pending)
            self._delete_pending_if_unchanged(pending_ref, pending_json)
            return current_head
        if current_head == pending.expected_head:
            self._validate_pending_objects(pending)
            if not self._backend.compare_and_swap_ref(head_ref, pending.expected_head, pending.event_id):
                raise ShepherdStreamRecoveryError(f"cannot recover stream head for {stream_id!r}")
            self._delete_pending_if_unchanged(pending_ref, pending_json)
            return pending.event_id
        if current_head is not None and self._is_admitted_ancestor(
            pending.event_id,
            current_head,
            stream_id=stream_id,
        ):
            self._validate_pending_objects(pending)
            self._delete_pending_if_unchanged(pending_ref, pending_json)
            return current_head
        raise ShepherdStreamRecoveryError(
            f"cannot recover pending append {pending.event_id}: "
            f"head={current_head!r}, expected={pending.expected_head!r}"
        )

    @staticmethod
    def stream_head_ref(stream_id: str) -> str:
        return f"shepherd/streams/{_ref_segment(stream_id)}/head"

    @staticmethod
    def pending_append_prefix(stream_id: str) -> str:
        return f"shepherd/streams/{_ref_segment(stream_id)}/pending/"

    @staticmethod
    def pending_append_ref(stream_id: str, event_id: str) -> str:
        return f"{ShepherdCommonsRecorder.pending_append_prefix(stream_id)}{_ref_segment(event_id)}"

    def _validate_layer_sequence(
        self,
        layer: EffectLayer,
        *,
        stream_id: str,
        previous_head: str | None,
    ) -> None:
        if previous_head is None:
            if layer.sequence != 0:
                raise ValueError("first stream append must have sequence 0")
            return
        previous = self._repo.get(previous_head)
        if previous is None or previous.schema_ref != SHEPHERD_EVENT_SCHEMA:
            raise ShepherdStreamConflictError("previous stream head is missing or is not an shepherd event")
        if previous.body.get("stream_id") != stream_id:
            raise ShepherdStreamConflictError("previous stream head belongs to a different stream")
        expected_sequence = previous.body.get("sequence")
        if not isinstance(expected_sequence, int):
            raise ShepherdStreamConflictError("previous stream head has an invalid sequence")
        if layer.sequence != expected_sequence + 1:
            raise ValueError(f"next stream append must have sequence {expected_sequence + 1}")

    def _encode_pending(self, pending: _PendingStreamAppend) -> str:
        return json.dumps(
            {
                "version": 1,
                "stream_id": pending.stream_id,
                "expected_head": pending.expected_head,
                "effect_id": pending.effect_id,
                "event_id": pending.event_id,
                "sequence": pending.sequence,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def _decode_pending(self, payload: str) -> _PendingStreamAppend:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ShepherdStreamRecoveryError(f"pending append is not JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ShepherdStreamRecoveryError("pending append must be a JSON object")
        allowed = {"version", "stream_id", "expected_head", "effect_id", "event_id", "sequence"}
        if set(data) != allowed:
            raise ShepherdStreamRecoveryError("pending append has invalid fields")
        version = data.get("version")
        if version != 1 or isinstance(version, bool):
            raise ShepherdStreamRecoveryError("pending append has unsupported version")
        stream_id = data.get("stream_id")
        expected_head = data.get("expected_head")
        effect_id = data.get("effect_id")
        event_id = data.get("event_id")
        sequence = data.get("sequence")
        if not isinstance(stream_id, str) or not stream_id:
            raise ShepherdStreamRecoveryError("pending stream_id must be a non-empty string")
        if expected_head is not None and not isinstance(expected_head, str):
            raise ShepherdStreamRecoveryError("pending expected_head must be null or a string")
        if not isinstance(effect_id, str) or not effect_id:
            raise ShepherdStreamRecoveryError("pending effect_id must be a non-empty string")
        if not isinstance(event_id, str) or not event_id:
            raise ShepherdStreamRecoveryError("pending event_id must be a non-empty string")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise ShepherdStreamRecoveryError("pending sequence must be a non-negative integer")
        pending = _PendingStreamAppend(
            stream_id=stream_id,
            expected_head=expected_head,
            effect_id=effect_id,
            event_id=event_id,
            sequence=sequence,
        )
        if self._encode_pending(pending) != payload:
            raise ShepherdStreamRecoveryError("pending append JSON is not canonical")
        return pending

    def _delete_pending_if_unchanged(self, pending_ref: str, pending_json: str) -> None:
        if not self._backend.compare_and_delete_ref(pending_ref, pending_json):
            raise ShepherdStreamRecoveryError(f"pending append changed before cleanup: {pending_ref}")

    def _load_valid_effect(self, effect_id: str, *, context: str) -> Object:
        effect = self._repo.get(effect_id)
        if effect is None:
            raise ShepherdStreamRecoveryError(f"{context} is missing effect object")
        if effect.schema_ref != SHEPHERD_EFFECT_SCHEMA:
            raise ShepherdStreamRecoveryError(f"{context} effect object is not an shepherd/effect/v1")
        effect_failure = validate_shepherd_effect_v1(effect, Resolver(obj=effect, _backend=self._backend))
        if effect_failure is not None:
            raise ShepherdStreamRecoveryError(
                f"{context} effect object is invalid: {effect_failure.reason_kind}: {effect_failure.reason}"
            )
        return effect

    def _load_valid_event(self, event_id: str, *, stream_id: str, sequence: int | None, context: str) -> Object:
        event = self._repo.get(event_id)
        if event is None:
            raise ShepherdStreamRecoveryError(f"{context} is missing event object")
        if event.schema_ref != SHEPHERD_EVENT_SCHEMA:
            raise ShepherdStreamRecoveryError(f"{context} event object is not an shepherd/event/v1")
        event_failure = validate_shepherd_event_v1(event, Resolver(obj=event, _backend=self._backend))
        if event_failure is not None:
            raise ShepherdStreamRecoveryError(
                f"{context} event object is invalid: {event_failure.reason_kind}: {event_failure.reason}"
            )
        if event.body.get("stream_id") != stream_id:
            raise ShepherdStreamRecoveryError(f"{context} event belongs to a different stream")
        event_sequence = event.body.get("sequence")
        if sequence is not None and event_sequence != sequence:
            raise ShepherdStreamRecoveryError(f"{context} event sequence does not match expected sequence")
        effect_targets = [edge.target for edge in event.edges if edge.role == SHEPHERD_EFFECT_ROLE]
        if len(effect_targets) != 1:
            raise ShepherdStreamRecoveryError(f"{context} event must have exactly one effect edge")
        self._load_valid_effect(effect_targets[0], context=context)
        return event

    def _validate_pending_objects(self, pending: _PendingStreamAppend) -> Object:
        self._load_valid_effect(pending.effect_id, context="pending append")
        event = self._load_valid_event(
            pending.event_id,
            stream_id=pending.stream_id,
            sequence=pending.sequence,
            context="pending append",
        )
        effect_targets = [edge.target for edge in event.edges if edge.role == SHEPHERD_EFFECT_ROLE]
        if effect_targets != [pending.effect_id]:
            raise ShepherdStreamRecoveryError("pending event effect edge does not match pending record")
        previous_targets = [edge.target for edge in event.edges if edge.role == SHEPHERD_PREVIOUS_ROLE]
        if pending.sequence == 0:
            if pending.expected_head is not None:
                raise ShepherdStreamRecoveryError("sequence 0 pending append must not expect a previous head")
            if previous_targets:
                raise ShepherdStreamRecoveryError("sequence 0 pending event must not have a previous edge")
        else:
            if pending.expected_head is None:
                raise ShepherdStreamRecoveryError("sequence > 0 pending append must expect a previous head")
            if previous_targets != [pending.expected_head]:
                raise ShepherdStreamRecoveryError("pending event previous edge does not match expected head")
        return event

    def _is_admitted_ancestor(self, ancestor: str, head: str, *, stream_id: str) -> bool:
        cursor: str | None = head
        previous_sequence: int | None = None
        seen: set[str] = set()
        while cursor is not None:
            if cursor == ancestor:
                return True
            if cursor in seen:
                return False
            seen.add(cursor)
            try:
                event = self._load_valid_event(
                    cursor,
                    stream_id=stream_id,
                    sequence=previous_sequence - 1 if previous_sequence is not None else None,
                    context="admitted path",
                )
            except ShepherdStreamRecoveryError:
                return False
            sequence = event.body.get("sequence")
            if not isinstance(sequence, int):
                return False
            previous_targets = [edge.target for edge in event.edges if edge.role == SHEPHERD_PREVIOUS_ROLE]
            if sequence == 0:
                return False
            if len(previous_targets) != 1:
                return False
            previous_sequence = sequence
            cursor = previous_targets[0]
        return False


def normalize_commons_value(value: Any, *, path: str = "$") -> Any:
    """Project Shepherd values into an injective commons canonical value tree."""
    if value is None:
        return {"kind": "null"}
    if isinstance(value, str):
        return {"kind": "string", "value": value}
    if isinstance(value, bool):
        return {"kind": "bool", "value": value}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"kind": "int", "value": value}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"non-finite floats are not supported at {path}")
        return {
            "kind": "float64",
            "repr": format(value, ".17g"),
        }
    if isinstance(value, Mapping):
        items: list[list[Any]] = []
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError(f"commons object keys must be strings at {path}")
            if not key:
                raise TypeError(f"commons object keys must be non-empty at {path}")
            items.append([key, normalize_commons_value(child, path=f"{path}.{key}")])
        items.sort(key=lambda item: item[0])
        return {"kind": "object", "items": items}
    if isinstance(value, list | tuple):
        return {
            "kind": "list",
            "items": [normalize_commons_value(child, path=f"{path}[{index}]") for index, child in enumerate(value)],
        }
    if isinstance(value, bytes | bytearray | memoryview):
        raise TypeError(f"bytes are not supported at {path}; use a schema-declared encoding")
    if isinstance(value, set | frozenset):
        raise TypeError(f"sets are not supported at {path}; use a sorted list")
    if is_dataclass(value) and not isinstance(value, type):
        raise TypeError(f"dataclasses are not supported at {path}; project to primitives first")
    raise TypeError(f"{type(value).__name__} is not supported at {path}")


def validate_shepherd_effect_v1(obj: Object, _resolver: Resolver) -> Failure | None:
    """Validate an ``shepherd/effect/v1`` content object."""
    body = obj.body
    if set(body) != {"projection_version", "effect_type", "payload"}:
        return Failure("schema", "shepherd/effect/v1 body must contain only projection_version, effect_type, payload")
    if body.get("projection_version") != SHEPHERD_EFFECT_PROJECTION_VERSION:
        return Failure("schema", "shepherd/effect/v1 projection_version must be 1")
    if not isinstance(body.get("effect_type"), str) or not body["effect_type"]:
        return Failure("schema", "shepherd/effect/v1 effect_type must be a non-empty string")
    failure = _validate_typed_value(body.get("payload"), path="payload")
    if failure is not None:
        return failure
    if _typed_kind(body.get("payload")) != "object":
        return Failure("schema", "shepherd/effect/v1 payload must be an encoded object")
    payload_effect_type = _typed_string_field(body["payload"], "effect_type")
    if payload_effect_type != body["effect_type"]:
        return Failure("schema", "shepherd/effect/v1 effect_type must match payload.effect_type")
    if obj.edges:
        return Failure("schema", "shepherd/effect/v1 must not have edges")
    return None


def validate_shepherd_event_v1(obj: Object, resolver: Resolver) -> Failure | None:
    """Validate an ``shepherd/event/v1`` stream occurrence object."""
    body = obj.body
    allowed_body_keys = {"projection_version", "stream_id", "sequence", "scope_depth", "source_context"}
    if not set(body).issubset(allowed_body_keys):
        return Failure("schema", "shepherd/event/v1 body contains unsupported fields")
    if {"projection_version", "stream_id", "sequence", "scope_depth"} - set(body):
        return Failure("schema", "shepherd/event/v1 body is missing required fields")
    if body.get("projection_version") != SHEPHERD_EFFECT_PROJECTION_VERSION:
        return Failure("schema", "shepherd/event/v1 projection_version must be 1")
    if not isinstance(body.get("stream_id"), str) or not body["stream_id"]:
        return Failure("schema", "shepherd/event/v1 stream_id must be a non-empty string")
    if not isinstance(body.get("sequence"), int) or body["sequence"] < 0:
        return Failure("schema", "shepherd/event/v1 sequence must be a non-negative integer")
    if not isinstance(body.get("scope_depth"), int) or body["scope_depth"] < 0:
        return Failure("schema", "shepherd/event/v1 scope_depth must be a non-negative integer")
    if "source_context" in body and (not isinstance(body["source_context"], str) or not body["source_context"]):
        return Failure("schema", "shepherd/event/v1 source_context must be a non-empty string when present")

    for edge in obj.edges:
        if edge.role not in _EVENT_EDGE_ROLES:
            return Failure("schema", f"unsupported shepherd/event/v1 edge role {edge.role!r}")
    effect_edges = [edge for edge in obj.edges if edge.role == SHEPHERD_EFFECT_ROLE]
    previous_edges = [edge for edge in obj.edges if edge.role == SHEPHERD_PREVIOUS_ROLE]
    caused_by_edges = [edge for edge in obj.edges if edge.role == SHEPHERD_CAUSED_BY_ROLE]

    if len(effect_edges) != 1:
        return Failure("schema", "shepherd/event/v1 requires exactly one shepherd.effect edge")
    effect = resolver.by_digest(effect_edges[0].target)
    failure = _validate_shepherd_effect_target(effect, role=SHEPHERD_EFFECT_ROLE)
    if failure is not None:
        return failure
    assert effect is not None

    sequence = body["sequence"]
    stream_id = body["stream_id"]
    failure = _validate_previous_event_edges(
        previous_edges,
        resolver=resolver,
        sequence=sequence,
        stream_id=stream_id,
    )
    if failure is not None:
        return failure

    caused_by = _effect_caused_by(effect)
    if caused_by is _MISSING or caused_by is None:
        if caused_by_edges:
            return Failure("schema", "shepherd.caused_by edge requires effect.payload.caused_by")
        return None
    if not isinstance(caused_by, str) or not caused_by:
        return Failure("schema", "effect.payload.caused_by must be null or a non-empty string")
    if len(caused_by_edges) != 1:
        return Failure("schema", "effect.payload.caused_by requires exactly one shepherd.caused_by edge")
    cause_event = resolver.by_digest(caused_by_edges[0].target)
    failure = _validate_shepherd_event_target(cause_event, role=SHEPHERD_CAUSED_BY_ROLE)
    if failure is not None:
        return failure
    assert cause_event is not None
    if cause_event.body.get("stream_id") != stream_id:
        return Failure("schema", "shepherd.caused_by must target the same stream_id")
    if cause_event.body.get("sequence", sequence) >= sequence:
        return Failure("schema", "shepherd.caused_by must target an earlier sequence")
    cause_effect = _event_effect(cause_event, resolver)
    if cause_effect is None:
        return Failure("missing_target", "shepherd.caused_by target event is missing its effect")
    if _intent_anchor(cause_effect) != caused_by:
        return Failure("schema", "shepherd.caused_by target intent anchor must match effect.payload.caused_by")
    return None


def _validate_previous_event_edges(
    previous_edges: list[Edge],
    *,
    resolver: Resolver,
    sequence: int,
    stream_id: str,
) -> Failure | None:
    if sequence == 0:
        if previous_edges:
            return Failure("schema", "shepherd/event/v1 sequence 0 must not have an shepherd.previous edge")
        return None
    if len(previous_edges) != 1:
        return Failure("schema", "shepherd/event/v1 sequence > 0 requires exactly one shepherd.previous edge")
    previous = resolver.by_digest(previous_edges[0].target)
    failure = _validate_shepherd_event_target(previous, role=SHEPHERD_PREVIOUS_ROLE)
    if failure is not None:
        return failure
    assert previous is not None
    if previous.body.get("stream_id") != stream_id:
        return Failure("schema", "shepherd.previous must target the same stream_id")
    if previous.body.get("sequence") != sequence - 1:
        return Failure("schema", "shepherd.previous must target the immediately previous sequence")
    return None


def _typed_kind(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    kind = value.get("kind")
    return kind if isinstance(kind, str) else None


def _typed_object_field(value: object, field: str) -> object:
    if _typed_kind(value) != "object" or not isinstance(value, Mapping):
        return _MISSING
    items = value.get("items")
    if not isinstance(items, list | tuple):
        return _MISSING
    for item in items:
        if isinstance(item, list | tuple) and len(item) == 2 and item[0] == field:
            return item[1]
    return _MISSING


def _typed_string_field(value: object, field: str) -> str | None:
    child = _typed_object_field(value, field)
    if _typed_kind(child) != "string" or not isinstance(child, Mapping):
        return None
    string_value = child.get("value")
    if not isinstance(string_value, str) or not string_value:
        return None
    return string_value


def _validate_typed_value(value: object, *, path: str) -> Failure | None:
    if not isinstance(value, Mapping):
        return Failure("schema", f"{path} must be an encoded value object")
    kind = value.get("kind")
    if kind == "null":
        if set(value) != {"kind"}:
            return Failure("schema", f"{path} null value must contain only kind")
        return None
    if kind == "string":
        if set(value) != {"kind", "value"} or not isinstance(value.get("value"), str):
            return Failure("schema", f"{path} string value must contain a string value")
        return None
    if kind == "bool":
        if set(value) != {"kind", "value"} or not isinstance(value.get("value"), bool):
            return Failure("schema", f"{path} bool value must contain a boolean value")
        return None
    if kind == "int":
        int_value = value.get("value")
        if set(value) != {"kind", "value"} or not isinstance(int_value, int) or isinstance(int_value, bool):
            return Failure("schema", f"{path} int value must contain an integer value")
        return None
    if kind == "float64":
        repr_value = value.get("repr")
        if set(value) != {"kind", "repr"} or not isinstance(repr_value, str) or not repr_value:
            return Failure("schema", f"{path} float64 value must contain a repr string")
        try:
            parsed = float(repr_value)
        except ValueError:
            return Failure("schema", f"{path} float64 repr must parse as a finite float")
        if not math.isfinite(parsed) or format(parsed, ".17g") != repr_value:
            return Failure("schema", f"{path} float64 repr must be finite canonical .17g form")
        return None
    if kind == "list":
        items = value.get("items")
        if set(value) != {"kind", "items"} or not isinstance(items, list | tuple):
            return Failure("schema", f"{path} list value must contain an items list")
        for index, child in enumerate(items):
            failure = _validate_typed_value(child, path=f"{path}[{index}]")
            if failure is not None:
                return failure
        return None
    if kind == "object":
        items = value.get("items")
        if set(value) != {"kind", "items"} or not isinstance(items, list | tuple):
            return Failure("schema", f"{path} object value must contain an items list")
        previous_key: str | None = None
        for index, item in enumerate(items):
            if not isinstance(item, list | tuple) or len(item) != 2 or not isinstance(item[0], str):
                return Failure("schema", f"{path}.items[{index}] must be a [key, value] pair")
            key = item[0]
            if not key:
                return Failure("schema", f"{path}.items[{index}] key must be non-empty")
            if previous_key is not None and key <= previous_key:
                return Failure("schema", f"{path} object keys must be unique and sorted")
            previous_key = key
            failure = _validate_typed_value(item[1], path=f"{path}.{key}")
            if failure is not None:
                return failure
        return None
    return Failure("schema", f"{path} has unsupported encoded value kind {kind!r}")


def _validate_shepherd_effect_target(obj: Object | None, *, role: str) -> Failure | None:
    if obj is None:
        return Failure("missing_target", f"missing edge target for {role}")
    if obj.schema_ref != SHEPHERD_EFFECT_SCHEMA:
        return Failure("schema", f"{role} must target shepherd/effect/v1")
    return None


def _validate_shepherd_event_target(obj: Object | None, *, role: str) -> Failure | None:
    if obj is None:
        return Failure("missing_target", f"missing edge target for {role}")
    if obj.schema_ref != SHEPHERD_EVENT_SCHEMA:
        return Failure("schema", f"{role} must target shepherd/event/v1")
    return None


def _event_effect(event: Object, resolver: Resolver) -> Object | None:
    effect_edges = [edge for edge in event.edges if edge.role == SHEPHERD_EFFECT_ROLE]
    if len(effect_edges) != 1:
        return None
    effect = resolver.by_digest(effect_edges[0].target)
    if effect is None or effect.schema_ref != SHEPHERD_EFFECT_SCHEMA:
        return None
    return effect


def _effect_caused_by(effect: Object) -> object:
    payload = effect.body.get("payload")
    caused_by = _typed_object_field(payload, "caused_by")
    if caused_by is _MISSING:
        return _MISSING
    if _typed_kind(caused_by) == "null":
        return None
    if _typed_kind(caused_by) == "string" and isinstance(caused_by, Mapping):
        return caused_by.get("value")
    return caused_by


def _intent_anchor(effect: Object) -> str | None:
    if effect.schema_ref != SHEPHERD_EFFECT_SCHEMA:
        return None
    effect_type = effect.body.get("effect_type")
    payload = effect.body.get("payload")
    if effect_type == "tool_call_started":
        return _typed_string_field(payload, "tool_call_id")
    if effect_type == "tool_call_batch":
        return _typed_string_field(payload, "batch_id")
    return None


shepherd_effect_profile = Profile(
    name="shepherd",
    validators={
        SHEPHERD_EFFECT_SCHEMA: validate_shepherd_effect_v1,
        SHEPHERD_EVENT_SCHEMA: validate_shepherd_event_v1,
    },
)


def project_effect_object(effect: Any) -> Object:
    """Project one Shepherd effect payload into an ``shepherd/effect/v1`` object."""
    effect_payload = normalize_commons_value(effect.model_dump(mode="json"), path="effect")
    if _typed_kind(effect_payload) != "object":
        raise TypeError("effect projection must produce an encoded object")
    effect_type = _typed_string_field(effect_payload, "effect_type")
    if effect_type is None:
        raise TypeError("effect.effect_type must be a non-empty string")
    return Object(
        schema_ref=SHEPHERD_EFFECT_SCHEMA,
        body={
            "projection_version": SHEPHERD_EFFECT_PROJECTION_VERSION,
            "effect_type": effect_type,
            "payload": effect_payload,
        },
    )


def project_event_layer(
    layer: EffectLayer,
    *,
    stream_id: str,
    effect: Object,
    previous_event_id: str | None = None,
    caused_by_index: Mapping[str, str] | None = None,
) -> Object:
    """Project one Shepherd effect layer into an ``shepherd/event/v1`` object."""
    if not stream_id:
        raise ValueError("stream_id must be non-empty")
    if layer.sequence < 0:
        raise ValueError("layer.sequence must be non-negative")
    if layer.scope_depth < 0:
        raise ValueError("layer.scope_depth must be non-negative")
    if layer.sequence == 0 and previous_event_id is not None:
        raise ValueError("sequence 0 layers must not have a previous_event_id")
    if layer.sequence > 0 and previous_event_id is None:
        raise ValueError("sequence > 0 layers require previous_event_id")
    if effect.schema_ref != SHEPHERD_EFFECT_SCHEMA:
        raise ValueError("effect must be an shepherd/effect/v1 object")

    body: dict[str, Any] = {
        "projection_version": SHEPHERD_EFFECT_PROJECTION_VERSION,
        "stream_id": stream_id,
        "sequence": layer.sequence,
        "scope_depth": layer.scope_depth,
    }
    if layer.source_context is not None:
        if not layer.source_context:
            raise ValueError("layer.source_context must be non-empty when present")
        body["source_context"] = layer.source_context

    edges: list[Edge] = [Edge(role=SHEPHERD_EFFECT_ROLE, target=effect.id)]
    if previous_event_id is not None:
        edges.append(Edge(role=SHEPHERD_PREVIOUS_ROLE, target=previous_event_id))
    caused_by = _effect_caused_by(effect)
    if caused_by is _MISSING or caused_by is None:
        return Object(schema_ref=SHEPHERD_EVENT_SCHEMA, body=body, edges=tuple(edges))
    if not isinstance(caused_by, str) or not caused_by:
        raise TypeError("effect.caused_by must be null or a non-empty string when present")
    if caused_by_index is None:
        raise ValueError("caused_by_index is required when effect.caused_by is present")
    try:
        edges.append(Edge(role=SHEPHERD_CAUSED_BY_ROLE, target=caused_by_index[caused_by]))
    except KeyError as exc:
        raise ValueError(f"unknown caused_by intent anchor {caused_by!r}") from exc
    return Object(schema_ref=SHEPHERD_EVENT_SCHEMA, body=body, edges=tuple(edges))


def project_effect_layer(
    layer: EffectLayer,
    *,
    stream_id: str,
    previous_event_id: str | None = None,
    caused_by_index: Mapping[str, str] | None = None,
) -> ProjectedEffectLayer:
    """Project one effect layer into content and occurrence objects."""
    effect = project_effect_object(layer.effect)
    event = project_event_layer(
        layer,
        stream_id=stream_id,
        effect=effect,
        previous_event_id=previous_event_id,
        caused_by_index=caused_by_index,
    )
    return ProjectedEffectLayer(effect=effect, event=event)


def project_effect_stream(layers: Iterable[EffectLayer], *, stream_id: str) -> ProjectedEffectStream:
    """Project an ordered effect stream into append-ordered commons objects."""
    projected_layers: list[ProjectedEffectLayer] = []
    previous_event_id: str | None = None
    intent_index: dict[str, str] = {}

    for expected_sequence, layer in enumerate(layers):
        if layer.sequence != expected_sequence:
            raise ValueError(
                f"effect stream must be contiguous from sequence 0; expected {expected_sequence}, got {layer.sequence}"
            )
        projected = project_effect_layer(
            layer,
            stream_id=stream_id,
            previous_event_id=previous_event_id,
            caused_by_index=intent_index,
        )
        projected_layers.append(projected)
        previous_event_id = projected.event.id

        anchor = _intent_anchor(projected.effect)
        if anchor is not None:
            if anchor in intent_index:
                raise ValueError(f"duplicate intent anchor {anchor!r}")
            intent_index[anchor] = projected.event.id

    return ProjectedEffectStream(layers=tuple(projected_layers))


def _ref_segment(value: str) -> str:
    if not value:
        raise ValueError("ref segment value must be non-empty")
    encoded = base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")
    return f"b64-{encoded}"


def _expect_optional_str(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise TypeError("expected_head must be a string or None")


__all__ = [
    "SHEPHERD_CAUSED_BY_ROLE",
    "SHEPHERD_EFFECT_PROJECTION_VERSION",
    "SHEPHERD_EFFECT_ROLE",
    "SHEPHERD_EFFECT_SCHEMA",
    "SHEPHERD_EVENT_SCHEMA",
    "SHEPHERD_PREVIOUS_ROLE",
    "ShepherdCommonsRecorder",
    "ShepherdStreamConflictError",
    "ShepherdStreamRecoveryError",
    "ProjectedEffectLayer",
    "ProjectedEffectStream",
    "RecordedEffectLayer",
    "shepherd_effect_profile",
    "normalize_commons_value",
    "project_effect_layer",
    "project_effect_object",
    "project_effect_stream",
    "project_event_layer",
    "validate_shepherd_effect_v1",
    "validate_shepherd_event_v1",
]
