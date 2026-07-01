"""Stable JSON-compatible serialization for normalized trace records."""

from __future__ import annotations

import json
from dataclasses import MISSING, asdict, fields
from typing import TYPE_CHECKING, Any

from shepherd_kernel_v3_reference.trace.records import (
    ContinuationDelay,
    ContinuationPending,
    ContinuationResume,
    EffectCapture,
    EffectDeclaration,
    ForkBranch,
    ForkSummary,
    HandlerForward,
    HandlerSelection,
    ResumeReturn,
    ResumptionHandle,
    SelectionClosed,
    TerminalResumeResult,
    TraceRecord,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_RECORD_TYPES = {
    cls.__name__: cls
    for cls in (
        ContinuationDelay,
        ContinuationPending,
        EffectDeclaration,
        ForkBranch,
        ForkSummary,
        HandlerForward,
        HandlerSelection,
        ResumptionHandle,
        ContinuationResume,
        TerminalResumeResult,
        ResumeReturn,
        EffectCapture,
        SelectionClosed,
    )
}


class TraceSerializationError(ValueError):
    """Raised when JSON trace data cannot be decoded as trace records."""


def trace_record_to_json(record: TraceRecord) -> dict[str, Any]:
    """Return a JSON-compatible mapping for one trace record."""

    data = asdict(record)
    if data.get("branch_scope_ref") is None:
        data.pop("branch_scope_ref", None)
    data["record_type"] = type(record).__name__
    return {"record_type": data.pop("record_type"), **data}


def trace_record_from_json(data: Mapping[str, Any]) -> TraceRecord:
    """Decode one JSON-compatible trace record mapping."""

    record_type = data.get("record_type")
    if not isinstance(record_type, str):
        raise TraceSerializationError("trace record is missing string record_type")
    record_cls = _RECORD_TYPES.get(record_type)
    if record_cls is None:
        raise TraceSerializationError(f"unknown trace record_type: {record_type!r}")

    field_names = {field.name for field in fields(record_cls)}
    kwargs = {name: data[name] for name in field_names if name in data}
    if record_cls is ForkSummary and isinstance(kwargs.get("branch_refs"), list):
        kwargs["branch_refs"] = tuple(kwargs["branch_refs"])
    missing = [
        field.name
        for field in fields(record_cls)
        if field.name not in kwargs and field.default is MISSING and field.default_factory is MISSING
    ]
    if missing:
        raise TraceSerializationError(f"{record_type} is missing required fields: {missing!r}")
    try:
        return record_cls(**kwargs)
    except TypeError as exc:
        raise TraceSerializationError(str(exc)) from exc


def trace_to_json(trace: tuple[TraceRecord, ...] | list[TraceRecord]) -> list[dict[str, Any]]:
    """Encode a trace as a JSON-compatible list."""

    return [trace_record_to_json(record) for record in trace]


def trace_from_json(data: list[Mapping[str, Any]]) -> tuple[TraceRecord, ...]:
    """Decode a JSON-compatible trace list."""

    return tuple(trace_record_from_json(record) for record in data)


def dumps_trace(trace: tuple[TraceRecord, ...] | list[TraceRecord]) -> str:
    """Serialize a trace to stable JSON text."""

    return json.dumps(
        trace_to_json(trace),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def loads_trace(data: str) -> tuple[TraceRecord, ...]:
    """Deserialize trace JSON text."""

    decoded = json.loads(data)
    if not isinstance(decoded, list):
        raise TraceSerializationError("trace JSON must decode to a list")
    return trace_from_json(decoded)
