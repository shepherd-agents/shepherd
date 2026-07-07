"""``@step`` — the step layer's public surface (authoring re-pin W2).

Sub-increment W2a: the **pure** layer — output parsing/coercion
(`test_step_parsing` re-pins), metadata extraction, and JSON-schema generation
(`test_step_schema` re-pins) — ported from the legacy
``shepherd_core._shared.{coerce,schema}`` + ``shepherd_runtime.step.{metadata,
output}`` with no legacy imports. W2b adds the function-form decorator and the
``step.{started,completed,failed}`` durable trace events (S1 seam 2).
"""

from __future__ import annotations

import functools
import inspect
import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, get_args, get_origin, get_type_hints

from shepherd_dialect._step_coerce import (
    StepOutputError,
    _coerce_step_value,
    _coerce_to_bool,
    _coerce_to_enum,
    _coerce_to_list,
    _parse_single_output,
    _parse_step_output,
    _parse_tuple_output,
)
from shepherd_dialect._step_schema import (
    SINGLE_OUTPUT_KEY,
    SchemaGenerationError,
    merge_schema_defs,
    refuse_handle_return_slot,
    type_to_json_schema,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "DEFAULT_STEP_TIMEOUT",
    "SchemaGenerationError",
    "StepInputInfo",
    "StepMetadata",
    "StepOutputError",
    "coerce_step_value",
    "coerce_to_bool",
    "coerce_to_enum",
    "coerce_to_list",
    "extract_step_metadata",
    "parse_single_output",
    "parse_step_output",
    "parse_tuple_output",
    "return_type_to_output_schema",
    "step",
    "type_to_json_schema",
]

DEFAULT_STEP_TIMEOUT = 120.0


# --- parsing/coercion (the legacy public names) ----------------------------------


def coerce_step_value(value: Any, expected_type: Any, step_name: str, field_name: str) -> Any:
    """Coerce a raw step value into the expected type."""
    return _coerce_step_value(value, expected_type, step_name, field_name)


def coerce_to_bool(value: Any) -> bool:
    """Coerce a raw value to bool."""
    return _coerce_to_bool(value)


def coerce_to_enum(value: Any, enum_type: type, step_name: str) -> Any:
    """Coerce a raw value to an enum member."""
    return _coerce_to_enum(value, enum_type, step_name)


def coerce_to_list(value: Any, list_args: tuple[Any, ...], step_name: str) -> list[Any]:
    """Coerce a raw value to a list."""
    return _coerce_to_list(value, list_args, step_name)


def parse_single_output(result: dict[str, Any], return_type: type, step_name: str) -> Any:
    """Parse the single-output shape returned by a step provider."""
    return _parse_single_output(result, return_type, step_name)


def parse_step_output(result: dict[str, Any], return_type: type | None, step_name: str) -> Any:
    """Parse step output into the declared return type."""
    return _parse_step_output(result, return_type, step_name)


def parse_tuple_output(result: dict[str, Any], tuple_args: tuple[Any, ...], step_name: str) -> tuple[Any, ...]:
    """Parse tuple-typed step output."""
    return _parse_tuple_output(result, tuple_args, step_name)


# --- metadata (ported from shepherd_runtime.step.metadata) ------------------------


@dataclass
class StepInputInfo:
    """Information about a step input parameter."""

    type_annotation: type
    is_required: bool = True
    default: Any = None


@dataclass
class StepMetadata:
    """Metadata extracted from a ``@step``-decorated function."""

    name: str
    docstring: str = ""
    parameters: dict[str, type] = field(default_factory=dict)
    return_type: type | None = None
    timeout: float = DEFAULT_STEP_TIMEOUT
    shepherd: bool = True

    @property
    def step_id(self) -> str:
        return f"step:{self.name}"

    @property
    def inputs(self) -> dict[str, StepInputInfo]:
        return {
            name: StepInputInfo(
                type_annotation=typ,
                is_required=self._param_details.get(name, {}).get("is_required", True),
                default=self._param_details.get(name, {}).get("default"),
            )
            for name, typ in self.parameters.items()
        }

    _param_details: dict[str, dict[str, Any]] = field(default_factory=dict)


def extract_step_metadata(
    func: Callable[..., Any],
    *,
    shepherd: bool = True,
    timeout: float = DEFAULT_STEP_TIMEOUT,
) -> StepMetadata:
    """Extract metadata from a step function (the docstring is the prompt)."""
    sig = inspect.signature(func)
    hints = get_type_hints(func)
    if not func.__doc__:
        warnings.warn(
            f"Step '{func.__name__}' has no docstring. The docstring is used as the LLM prompt.",
            stacklevel=3,
        )
    params: dict[str, type] = {}
    param_details: dict[str, dict[str, Any]] = {}
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        params[name] = hints.get(name, Any)
        has_default = param.default is not inspect.Parameter.empty
        param_details[name] = {
            "is_required": not has_default,
            "default": param.default if has_default else None,
        }
    return StepMetadata(
        name=func.__name__,
        docstring=func.__doc__ or "",
        parameters=params,
        return_type=hints.get("return"),
        shepherd=shepherd,
        timeout=timeout,
        _param_details=param_details,
    )


# --- schema (ported from shepherd_runtime.step.output) ----------------------------


def return_type_to_output_schema(return_type: type | None) -> dict[str, Any]:
    """Convert a return type annotation to JSON schema for structured output.

    Handle-typed return slots refuse fail-closed (handle slots are
    custody-resolved, never provider-authored — the P-030 fabrication fence).
    """
    refuse_handle_return_slot(return_type)
    if return_type is None:
        inner_schema: dict[str, Any] = {"type": "string"}
        all_defs: dict[str, Any] = {}
    else:
        origin = get_origin(return_type)
        if origin is tuple:
            args = get_args(return_type)
            properties: dict[str, Any] = {}
            required: list[str] = []
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


# --- the function-form decorator (W2b) --------------------------------------------


def step(fn: Callable[..., Any] | None = None, *, timeout: float = DEFAULT_STEP_TIMEOUT, shepherd: bool = True) -> Any:
    """Function-form ``@step`` decorator.

    The class-form inline ``self.step[T]`` retires with the spine (tranche D1).
    An shepherd step's docstring is its model prompt: calling it dispatches
    ``model.call`` through the nucleus seam, parses ``structured_output`` into
    the declared return type (``parse_step_output``), and records
    ``step.{started,completed,failed}`` into the current run's durable trace
    (no parallel stream — triage D1 applied). A non-shepherd step runs its body
    with the same lifecycle events. Outside a run, steps still execute (events
    are simply not recorded) so they stay unit-testable.
    """

    def wrap(func: Callable[..., Any]) -> Callable[..., Any]:
        meta = extract_step_metadata(func, shepherd=shepherd, timeout=timeout)

        @functools.wraps(func)
        def runner(*args: Any, **kwargs: Any) -> Any:
            from shepherd_dialect import nucleus
            from shepherd_dialect.provider_boundary import ModelRequest

            nucleus._emit_step_event({"kind": "step.started", "step": meta.name})
            try:
                if not meta.shepherd:
                    result = func(*args, **kwargs)
                else:
                    bound = inspect.signature(func).bind(*args, **kwargs)
                    bound.apply_defaults()
                    response = nucleus._dispatch(
                        "model.call",
                        ModelRequest(goal=meta.docstring.strip(), evidence=(dict(bound.arguments),)),
                    )
                    raw = getattr(response, "structured_output", response)
                    result = (
                        parse_single_output(raw, meta.return_type, meta.name) if meta.return_type is not None else None
                    )
            except Exception as exc:
                nucleus._emit_step_event({"kind": "step.failed", "step": meta.name, "error": str(exc)[:200]})
                raise
            nucleus._emit_step_event({"kind": "step.completed", "step": meta.name})
            return result

        runner.step_metadata = meta  # type: ignore[attr-defined]
        return runner

    return wrap(fn) if fn is not None else wrap
