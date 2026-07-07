"""Public runtime step schema helpers."""

from __future__ import annotations

from typing import Any, get_args, get_origin

from shepherd_core.schema import (
    SINGLE_OUTPUT_KEY,
    merge_schema_defs,
    refuse_handle_return_slot,
    type_to_json_schema,
)


def return_type_to_output_schema(return_type: type | None) -> dict[str, Any]:
    """Convert a return type annotation to JSON schema for structured output.

    Handle-typed return slots refuse fail-closed (handle slots are
    custody-resolved, never provider-authored — the P-030 fabrication fence).
    """
    refuse_handle_return_slot(return_type)
    if return_type is None:
        inner_schema = {"type": "string"}
        all_defs: dict[str, Any] = {}
    else:
        origin = get_origin(return_type)

        if origin is tuple:
            args = get_args(return_type)
            properties = {}
            required = []
            all_defs = {}

            for i, arg in enumerate(args):
                if arg is not ...:
                    key = f"output_{i}"
                    schema = type_to_json_schema(arg)
                    merge_schema_defs(schema, all_defs, field_name=key, context="tuple return type")
                    properties[key] = schema
                    required.append(key)

            result_schema: dict[str, Any] = {
                "type": "object",
                "properties": properties,
                "required": required,
            }
            if all_defs:
                result_schema["$defs"] = all_defs

            return {"type": "json_schema", "schema": result_schema}

        inner_schema = type_to_json_schema(return_type)
        all_defs = {}
        merge_schema_defs(inner_schema, all_defs)

    result_schema = {
        "type": "object",
        "properties": {SINGLE_OUTPUT_KEY: inner_schema},
        "required": [SINGLE_OUTPUT_KEY],
    }
    if all_defs:
        result_schema["$defs"] = all_defs

    return {"type": "json_schema", "schema": result_schema}


__all__ = ["return_type_to_output_schema"]
