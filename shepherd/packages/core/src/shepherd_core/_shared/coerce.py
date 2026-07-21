"""Value coercion utilities for step outputs.

This module handles type coercion of raw LLM outputs to match expected
Python types, including Literal types, Union types, Enums, Pydantic models,
and primitive types.

This is a shared module used by both step.py and _output_handler.py.
"""

from __future__ import annotations

import json
import logging
import types
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import (
    Any,
    Literal,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from pydantic import BaseModel

from ..errors import StepOutputError
from .schema import SINGLE_OUTPUT_KEY

logger = logging.getLogger(__name__)


# =============================================================================
# Output Parsing
# =============================================================================


def _parse_step_output(raw_output: Any, return_type: type | None, step_name: str = "") -> Any:
    """Parse raw output from LLM to match expected return type.

    Args:
        raw_output: The raw output from the LLM
        return_type: The expected return type
        step_name: Name of the step (for error messages)
    """
    if return_type is None or return_type is type(None):
        return None

    # If already correct type, return as-is
    if isinstance(raw_output, return_type) if isinstance(return_type, type) else False:
        return raw_output

    return _coerce_step_value(raw_output, return_type, step_name, "")


def _parse_single_output(raw_output: dict[str, Any] | Any, return_type: type, step_name: str = "") -> Any:
    """Parse a single output value from a dict or direct value.

    Args:
        raw_output: The raw output (dict with result key, or direct value)
        return_type: The expected return type
        step_name: Name of the step (for error messages)
    """
    # Handle dict output
    if isinstance(raw_output, dict):
        if not raw_output:
            raise StepOutputError(
                step_name=step_name,
                expected_type=return_type,
                received=raw_output,
                reason="Empty response from step",
            )

        # Check for result key
        if SINGLE_OUTPUT_KEY in raw_output:
            value = raw_output[SINGLE_OUTPUT_KEY]
        elif len(raw_output) == 1:
            # Single key - use its value
            value = next(iter(raw_output.values()))
        else:
            raise StepOutputError(
                step_name=step_name,
                expected_type=return_type,
                received=raw_output,
                reason=f"Multiple keys in response but no '{SINGLE_OUTPUT_KEY}' key",
            )
    else:
        value = raw_output

    return _coerce_step_value(value, return_type, step_name, "")


def _parse_tuple_output(
    raw_output: dict[str, Any] | Any, return_types: tuple[type | types.EllipsisType, ...], step_name: str = ""
) -> tuple[Any, ...]:
    """Parse output as a tuple.

    Args:
        raw_output: The raw output (dict with output_N keys)
        return_types: Tuple of expected types for each element
        step_name: Name of the step (for error messages)
    """
    if not return_types:
        return (raw_output,)

    if isinstance(raw_output, dict):
        # Expect output_0, output_1, etc. keys
        values = []
        required_keys = [f"output_{i}" for i in range(len(return_types))]
        missing = [k for k in required_keys if k not in raw_output]
        if missing:
            raise StepOutputError(
                step_name=step_name,
                expected_type=tuple,
                received=raw_output,
                reason=f"Missing required keys: {missing}",
            )

        for i, t in enumerate(return_types):
            if t is not ...:
                values.append(_coerce_step_value(raw_output[f"output_{i}"], t, step_name, f"output_{i}"))

        return tuple(values)

    if isinstance(raw_output, (list, tuple)):
        return tuple(
            _coerce_step_value(v, t, step_name, f"output_{i}")
            for i, (v, t) in enumerate(zip(raw_output, return_types, strict=False))
            if t is not ...
        )

    return (raw_output,)


# =============================================================================
# Value Coercion
# =============================================================================


def _coerce_step_value(
    value: Any,
    target_type: type,
    step_name: str = "",
    field_name: str = "",
) -> Any:
    """Coerce a value to the target type.

    Args:
        value: The value to coerce
        target_type: The type to coerce to
        step_name: Name of the step (for error messages)
        field_name: Name of the field (for error messages)
    """
    origin = get_origin(target_type)
    args = get_args(target_type)

    # Handle None value
    if value is None:
        # Check if None is allowed (Optional types)
        if origin is Union and type(None) in args:
            return None
        raise StepOutputError(
            step_name=step_name,
            expected_type=target_type,
            received=value,
            reason=f"Field '{field_name}' doesn't allow None",
        )

    # Handle Literal
    if origin is Literal:
        # First, try exact match
        if value in args:
            return value

        # For string values, try case-insensitive match against string literals only
        if isinstance(value, str):
            str_value = value.lower()
            for arg in args:
                if isinstance(arg, str) and arg.lower() == str_value:
                    return arg

        # For numeric values, try type coercion to match numeric literals
        if isinstance(value, (int, float, str)):
            for arg in args:
                if isinstance(arg, (int, float)):
                    try:
                        coerced = type(arg)(value)
                        if coerced == arg:
                            return arg
                    except (ValueError, TypeError):
                        continue

        raise StepOutputError(
            step_name=step_name,
            expected_type=target_type,
            received=value,
            reason=f"Value {value!r} not in allowed literals {args}",
        )

    # Handle Union (including Optional and Python 3.10+ X | Y syntax)
    is_union = origin is Union or (hasattr(types, "UnionType") and isinstance(target_type, types.UnionType))
    if is_union:
        non_none_args = [a for a in args if a is not type(None)]
        # Try each type in order, use first that works
        for arg_type in non_none_args:
            try:
                return _coerce_step_value(value, arg_type, step_name, field_name)
            except (StepOutputError, ValueError, TypeError):
                continue
        # If we get here and there are no non-None args, just return value
        if not non_none_args:
            return value
        # None of the union types worked
        raise StepOutputError(
            step_name=step_name,
            expected_type=target_type,
            received=value,
            reason=f"Value doesn't match any type in Union{non_none_args}",
        )

    # Handle list
    if origin is list:
        return _coerce_to_list(value, (args[0],) if args else (Any,), step_name)

    # Handle bool (must be before int due to bool being subclass of int)
    if target_type is bool:
        return _coerce_to_bool(value)

    # Handle Enum
    if isinstance(target_type, type) and issubclass(target_type, Enum):
        return _coerce_to_enum(value, target_type, step_name)

    # Handle Pydantic models
    if isinstance(target_type, type) and issubclass(target_type, BaseModel):
        # Already correct type - return as-is
        if isinstance(value, target_type):
            return value
        if isinstance(value, dict):
            return target_type(**value)
        if isinstance(value, str):
            try:
                return target_type.model_validate_json(value)
            except Exception as e:  # noqa: BLE001
                logger.debug("Pydantic JSON validation failed for %s: %s", target_type.__name__, e)
        # Cannot coerce to Pydantic model - raise explicit error
        raise StepOutputError(
            step_name=step_name,
            expected_type=target_type,
            received=value,
            reason=f"Cannot coerce {type(value).__name__} to {target_type.__name__}. "
            f"Expected dict, JSON string, or {target_type.__name__} instance.",
        )

    # Handle dataclass result models.
    if isinstance(target_type, type) and is_dataclass(target_type):
        if isinstance(value, target_type):
            return value
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as e:
                logger.debug("Dataclass JSON validation failed for %s: %s", target_type.__name__, e)
        if isinstance(value, dict):
            hints = get_type_hints(target_type)
            coerced_fields: dict[str, Any] = {}
            for field in fields(target_type):
                if field.name in value:
                    field_type = hints.get(field.name, field.type)
                    coerced_fields[field.name] = _coerce_step_value(
                        value[field.name], field_type, step_name, field.name
                    )
            return target_type(**coerced_fields)
        raise StepOutputError(
            step_name=step_name,
            expected_type=target_type,
            received=value,
            reason=f"Cannot coerce {type(value).__name__} to {target_type.__name__}. "
            f"Expected dict, JSON string, or {target_type.__name__} instance.",
        )

    # Handle primitives
    if target_type is str:
        return str(value)
    if target_type is int:
        return int(value) if not isinstance(value, bool) else value
    if target_type is float:
        return float(value)

    return value


def _coerce_to_bool(value: Any) -> bool:
    """Coerce value to boolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lower = value.lower().strip()
        if lower in ("true", "yes", "1", "y"):
            return True
        if lower in ("false", "no", "0", "n"):
            return False
    return bool(value)


def _coerce_to_enum(value: Any, enum_type: type[Enum], step_name: str = "") -> Enum:
    """Coerce value to an enum member."""
    # Try direct value match
    for member in enum_type:
        if member.value == value:
            return member
        if member.name == value:
            return member

    # Try case-insensitive match
    str_value = str(value).lower()
    for member in enum_type:
        if str(member.value).lower() == str_value:
            return member
        if member.name.lower() == str_value:
            return member

    # Raise error instead of warning for invalid values
    raise StepOutputError(
        step_name=step_name,
        expected_type=enum_type,
        received=value,
        reason=f"Not a valid enum member for {enum_type.__name__}",
    )


def _coerce_to_list(value: Any, item_types: tuple[type, ...], step_name: str = "") -> list[Any]:
    """Coerce value to a list with items of the specified type.

    Args:
        value: The value to coerce
        item_types: Tuple of (item_type,) for list items
        step_name: Name of the step (for error messages)
    """
    if value is None:
        return []

    item_type = item_types[0] if item_types else Any

    if isinstance(value, list):
        return [_coerce_step_value(item, item_type, step_name, "") for item in value]
    if isinstance(value, str):
        # Try JSON parsing
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [_coerce_step_value(item, item_type, step_name, "") for item in parsed]
        except json.JSONDecodeError as e:
            logger.debug("JSON parse failed for list coercion, trying delimiter split: %s", e)
        # Split by common delimiters
        if "," in value:
            return [_coerce_step_value(item.strip(), item_type, step_name, "") for item in value.split(",")]
    return [value]


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "_coerce_step_value",
    "_coerce_to_bool",
    "_coerce_to_enum",
    "_coerce_to_list",
    "_parse_single_output",
    "_parse_step_output",
    "_parse_tuple_output",
]
