"""Public schema utilities for core-owned type conversion helpers."""

from __future__ import annotations

from shepherd_core._shared.schema import (
    HANDLE_RETURN_SLOT_MESSAGE,
    SINGLE_OUTPUT_KEY,
    HandleReturnSlotUnsupported,
    find_handle_annotation,
    merge_schema_defs,
    refuse_handle_return_slot,
    type_to_json_schema,
    wrap_as_json_schema,
)

__all__ = [
    "HANDLE_RETURN_SLOT_MESSAGE",
    "SINGLE_OUTPUT_KEY",
    "HandleReturnSlotUnsupported",
    "find_handle_annotation",
    "merge_schema_defs",
    "refuse_handle_return_slot",
    "type_to_json_schema",
    "wrap_as_json_schema",
]
