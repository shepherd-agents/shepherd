"""Public runtime task output helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, get_args, get_origin

from shepherd_core.errors import TaskRefOutputError
from shepherd_core.output import coerce_output_value, generate_mock_value
from shepherd_core.schema import (
    merge_schema_defs,
    refuse_handle_return_slot,
    type_to_json_schema,
    wrap_as_json_schema,
)

from shepherd_runtime.task.source_analysis import SourceExtractionError, extract_task_source
from shepherd_runtime.task.source_validation import SourceValidationError

from .markers import TaskRef
from .metadata import TaskMetadata, strip_none_from_type
from .secure import SecurityError, secure_reconstruct_task_class


@dataclass(frozen=True)
class TaskRefReconstructionPolicy:
    """Caller-owned policy for rehydrating TaskRef source strings."""

    allowed_imports: frozenset[str] = frozenset()

    @property
    def is_allowlisted(self) -> bool:
        """Whether this policy permits additional import modules."""
        return bool(self.allowed_imports)

    @classmethod
    def restricted(cls) -> TaskRefReconstructionPolicy:
        """Construct the default restricted reconstruction policy."""
        return cls()

    @classmethod
    def allowlisted(cls, *allowed_imports: str) -> TaskRefReconstructionPolicy:
        """Construct an allowlisted reconstruction policy."""
        if not allowed_imports:
            raise ValueError("allowlisted TaskRef reconstruction requires at least one allowed import")
        return cls(allowed_imports=frozenset(allowed_imports))


def _task_ref_output_kind(field_type: Any) -> Literal["single", "list"] | None:
    inner_type = strip_none_from_type(field_type)
    if inner_type is TaskRef:
        return "single"

    origin = get_origin(inner_type)
    if origin is list:
        args = get_args(inner_type)
        if args and args[0] is TaskRef:
            return "list"

    return None


def _task_ref_output_schema(kind: Literal["single", "list"]) -> dict[str, Any]:
    item_schema = {
        "type": "string",
        "description": (
            "Raw Python source for exactly one @task class. "
            "Return source only, without Markdown fences or surrounding prose."
        ),
    }
    if kind == "single":
        return dict(item_schema)
    return {"type": "array", "items": item_schema}


def generate_output_schema(meta: TaskMetadata) -> dict[str, Any] | None:
    """Generate a JSON schema for structured output extraction."""
    if not meta.outputs:
        return None

    properties: dict[str, Any] = {}
    all_defs: dict[str, Any] = {}

    for name, field_info in meta.outputs.items():
        refuse_handle_return_slot(field_info.inner_type)
        inner_type = strip_none_from_type(field_info.inner_type)
        task_ref_kind = _task_ref_output_kind(field_info.inner_type)
        if task_ref_kind is not None:
            schema = _task_ref_output_schema(task_ref_kind)
        else:
            schema = type_to_json_schema(inner_type)

        merge_schema_defs(schema, all_defs, field_name=name, context="output fields")

        if field_info.description:
            existing_description = schema.get("description")
            if existing_description:
                schema["description"] = f"{field_info.description} {existing_description}"
            else:
                schema["description"] = field_info.description

        properties[name] = schema

    if not properties:
        return None

    return wrap_as_json_schema(properties, all_defs=all_defs or None)


def extract_outputs(
    meta: TaskMetadata,
    result: Any,
    *,
    taskref_policy: TaskRefReconstructionPolicy | None = None,
) -> dict[str, Any]:
    """Extract output values from execution results."""
    outputs: dict[str, Any] = {}
    structured = result.structured_output or {}
    resolved_policy = taskref_policy or TaskRefReconstructionPolicy()

    for name, field_info in meta.outputs.items():
        if name in structured:
            raw_value = structured[name]
            task_ref_kind = _task_ref_output_kind(field_info.inner_type)
            if task_ref_kind == "single":
                outputs[name] = _reconstruct_task_output(raw_value, name, resolved_policy)
            elif task_ref_kind == "list":
                outputs[name] = _reconstruct_task_output_list(raw_value, name, resolved_policy)
            else:
                outputs[name] = coerce_output_value(raw_value, field_info.inner_type)
        else:
            outputs[name] = None

    return outputs


def serialize_outputs_for_cache(meta: TaskMetadata, outputs: dict[str, Any]) -> dict[str, Any]:
    """Convert runtime outputs into a JSON-friendly cache representation."""
    serialized: dict[str, Any] = {}

    for name, value in outputs.items():
        field_info = meta.outputs.get(name)
        if field_info is None:
            serialized[name] = value
            continue

        task_ref_kind = _task_ref_output_kind(field_info.inner_type)
        if task_ref_kind == "single":
            serialized[name] = _serialize_task_output_for_cache(value, name)
        elif task_ref_kind == "list":
            serialized[name] = _serialize_task_output_list_for_cache(value, name)
        else:
            serialized[name] = value

    return serialized


def rehydrate_cached_outputs(
    meta: TaskMetadata,
    cached_outputs: dict[str, Any],
    *,
    taskref_policy: TaskRefReconstructionPolicy | None = None,
) -> dict[str, Any]:
    """Rehydrate cached transport data back into runtime output values."""
    rehydrated: dict[str, Any] = {}
    resolved_policy = taskref_policy or TaskRefReconstructionPolicy()

    for name, value in cached_outputs.items():
        field_info = meta.outputs.get(name)
        if field_info is None:
            rehydrated[name] = value
            continue

        task_ref_kind = _task_ref_output_kind(field_info.inner_type)
        if task_ref_kind == "single":
            rehydrated[name] = _rehydrate_cached_task_output(value, name, resolved_policy)
        elif task_ref_kind == "list":
            rehydrated[name] = _rehydrate_cached_task_output_list(value, name, resolved_policy)
        else:
            rehydrated[name] = value

    return rehydrated


def rehydrate_task_source(
    source: str,
    *,
    taskref_policy: TaskRefReconstructionPolicy | None = None,
) -> type:
    """Rehydrate raw task source under the current local reconstruction policy."""
    resolved_policy = taskref_policy or TaskRefReconstructionPolicy()

    if resolved_policy.is_allowlisted:
        return secure_reconstruct_task_class(source, allowed_imports=resolved_policy.allowed_imports)
    return secure_reconstruct_task_class(source)


def _reconstruct_task_output(raw_value: Any, field_name: str, taskref_policy: TaskRefReconstructionPolicy) -> type:
    if not isinstance(raw_value, str):
        raise TaskRefOutputError(
            field_name,
            f"expected a raw Python source string, got {type(raw_value).__name__}",
            actual_value=raw_value,
        )

    try:
        return rehydrate_task_source(raw_value, taskref_policy=taskref_policy)
    except (SecurityError, SourceValidationError, SyntaxError, ValueError) as e:
        raise TaskRefOutputError(field_name, str(e), actual_value=raw_value) from e


def _reconstruct_task_output_list(
    raw_value: Any,
    field_name: str,
    taskref_policy: TaskRefReconstructionPolicy,
) -> list[type]:
    if not isinstance(raw_value, list):
        raise TaskRefOutputError(
            field_name,
            f"expected a list of raw Python source strings, got {type(raw_value).__name__}",
            actual_value=raw_value,
        )

    return [
        _reconstruct_task_output(item, f"{field_name}[{index}]", taskref_policy) for index, item in enumerate(raw_value)
    ]


def _serialize_task_output_for_cache(value: Any, field_name: str) -> str | None:
    if value is None:
        return None

    if isinstance(value, str):
        return value

    if isinstance(value, type) and hasattr(value, "_task_meta"):
        try:
            return extract_task_source(value)
        except SourceExtractionError as e:
            raise TaskRefOutputError(field_name, str(e), actual_value=value) from e

    raise TaskRefOutputError(
        field_name,
        f"expected a @task class or raw Python source string, got {type(value).__name__}",
        actual_value=value,
    )


def _serialize_task_output_list_for_cache(value: Any, field_name: str) -> list[str] | None:
    if value is None:
        return None

    if not isinstance(value, list):
        raise TaskRefOutputError(
            field_name,
            f"expected a list of @task classes or raw Python source strings, got {type(value).__name__}",
            actual_value=value,
        )

    return [_serialize_task_output_for_cache(item, f"{field_name}[{index}]") for index, item in enumerate(value)]  # type: ignore[misc]


def _rehydrate_cached_task_output(
    value: Any,
    field_name: str,
    taskref_policy: TaskRefReconstructionPolicy,
) -> type | None:
    if value is None:
        return None

    if isinstance(value, type) and hasattr(value, "_task_meta"):
        return value

    return _reconstruct_task_output(value, field_name, taskref_policy)


def _rehydrate_cached_task_output_list(
    value: Any,
    field_name: str,
    taskref_policy: TaskRefReconstructionPolicy,
) -> list[type] | None:
    if value is None:
        return None

    if not isinstance(value, list):
        raise TaskRefOutputError(
            field_name,
            f"expected a list of cached TaskRef values, got {type(value).__name__}",
            actual_value=value,
        )

    return [
        _rehydrate_cached_task_output(item, f"{field_name}[{index}]", taskref_policy)  # type: ignore[misc]
        for index, item in enumerate(value)
    ]


def populate_mock_outputs(task_instance: Any, meta: TaskMetadata) -> None:
    """Populate output fields with mock values."""
    for name, field_info in meta.outputs.items():
        mock_value = generate_mock_value(field_info.inner_type)
        setattr(task_instance, name, mock_value)


__all__ = [
    "TaskRefReconstructionPolicy",
    "extract_outputs",
    "generate_output_schema",
    "populate_mock_outputs",
    "rehydrate_cached_outputs",
    "rehydrate_task_source",
    "serialize_outputs_for_cache",
]
