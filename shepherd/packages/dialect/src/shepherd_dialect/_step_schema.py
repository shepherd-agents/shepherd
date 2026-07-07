"""Shared JSON Schema utilities for task and step output schemas (W2 port).

Ported verbatim from the legacy ``shepherd_core._shared.schema`` (authoring
re-pin W2 — the dialect carries no legacy imports); the single source of truth
for type-to-schema conversion via Pydantic's TypeAdapter.
"""

from __future__ import annotations

import warnings
from typing import Any, get_args

from pydantic import PydanticSchemaGenerationError, TypeAdapter


class SchemaGenerationError(Exception):
    """Error generating JSON schema for task/step outputs (legacy observable)."""

    def __init__(self, message: str, conflicting_key: str | None = None, field_name: str | None = None):
        self.conflicting_key = conflicting_key
        self.field_name = field_name
        super().__init__(message)


# =============================================================================
# Constants
# =============================================================================

# Key used for single-value outputs in step schemas.
# Used by _return_type_to_output_schema() and _parse_single_output().
SINGLE_OUTPUT_KEY = "result"


# =============================================================================
# Handle-return-slot fence (ported from shepherd_core._shared.schema — this
# module is a deliberate no-core-imports port; a parity test asserts the two
# stacks refuse identically)
# =============================================================================


class HandleReturnSlotUnsupported(TypeError):  # noqa: N818 — pinned name, parity across both schema stacks
    """A provider-facing output schema was requested for a substrate-handle slot."""


HANDLE_RETURN_SLOT_MESSAGE = (
    "return slot declares the substrate handle {noun}: handle-typed return slots "
    "are not lowered to provider output schemas, because a provider-authored handle "
    "would be a fabricated custody claim. Returned handles arrive with the projector "
    "(P-030 phase iii); until then, return ordinary values and consume world output "
    "through RunOutput/Changeset settlement."
)


def find_handle_annotation(annotation: Any) -> Any | None:
    """Return the first substrate-handle noun inside ``annotation``, if any.

    Detects the ``__shepherd_handle_noun__`` class marker directly and inside
    generic containers (``tuple[GitRepo, str]``, ``Annotated``/``May`` wrappers,
    ``Optional``, lists, dicts, ...). Returns ``None`` when the annotation is
    handle-free.
    """
    seen: set[int] = set()

    def _walk(candidate: Any) -> Any | None:
        if candidate is None or id(candidate) in seen:
            return None
        seen.add(id(candidate))
        if getattr(candidate, "__shepherd_handle_noun__", False) is True:
            return candidate
        for arg in get_args(candidate):
            found = _walk(arg)
            if found is not None:
                return found
        return None

    return _walk(annotation)


def refuse_handle_return_slot(annotation: Any) -> None:
    """Raise :class:`HandleReturnSlotUnsupported` if ``annotation`` carries a handle."""
    noun = find_handle_annotation(annotation)
    if noun is not None:
        name = getattr(noun, "__name__", None) or repr(noun)
        raise HandleReturnSlotUnsupported(HANDLE_RETURN_SLOT_MESSAGE.format(noun=repr(name)))


# =============================================================================
# JSON Schema Generation
# =============================================================================


def type_to_json_schema(type_annotation: Any) -> dict[str, Any]:
    """Convert Python type annotation to JSON Schema using Pydantic's TypeAdapter.

    This is the single source of truth for type-to-schema conversion.
    Handles all types Pydantic supports: primitives, generics, unions,
    Literal, Enum, datetime, UUID, Pydantic models, etc.

    Note: TypeAdapter.json_schema() returns a new dict each call, so callers
    may safely mutate the result (e.g., popping $defs for hoisting).

    Args:
        type_annotation: Any Python type annotation

    Returns:
        JSON Schema dict representing the type. Falls back to {"type": "string"}
        with a warning for unsupported types.
    """
    # Handle None/NoneType explicitly (TypeAdapter doesn't like bare None)
    if type_annotation is None or type_annotation is type(None):
        return {"type": "null"}

    # Handle Any - no constraints (empty schema allows anything)
    if type_annotation is Any:
        return {}

    try:
        ta = TypeAdapter(type_annotation)
        schema = ta.json_schema()

        # Strip Pydantic-added title (noise for LLM providers)
        schema.pop("title", None)

        return schema
    except (PydanticSchemaGenerationError, TypeError) as e:
        # PydanticSchemaGenerationError: Pydantic can't generate schema
        # TypeError: Exotic types that fail during introspection
        warnings.warn(
            f"Cannot generate JSON schema for {type_annotation!r}: {e}. "
            f"Falling back to string type. Consider using a supported type.",
            stacklevel=2,
        )
        return {"type": "string"}
    except Exception as e:  # noqa: BLE001
        # Unexpected failure — still fall back, but include exception class for debugging
        warnings.warn(
            f"Unexpected error generating JSON schema for {type_annotation!r} "
            f"({type(e).__name__}: {e}). Falling back to string type.",
            stacklevel=2,
        )
        return {"type": "string"}


# Deprecated alias for backward compatibility
# (internal function, but kept for safety during transition)
python_type_to_json_schema = type_to_json_schema


# =============================================================================
# Schema Helper Functions
# =============================================================================


def merge_schema_defs(
    schema: dict[str, Any],
    all_defs: dict[str, Any],
    *,
    field_name: str | None = None,
    context: str = "output fields",
) -> None:
    """Extract and merge $defs from schema into all_defs.

    Mutates both arguments:
    - schema: $defs key removed if present
    - all_defs: merged with definitions from this schema's $defs

    Args:
        schema: Schema dict that may contain $defs
        all_defs: Accumulator dict for all $defs
        field_name: Optional field name for error attribution
        context: Description of context for error message (e.g., "output fields", "tuple return type")

    Raises:
        SchemaGenerationError: If same $def name has different structure
    """
    if "$defs" not in schema:
        return

    for key, value in schema.pop("$defs").items():
        if key in all_defs and all_defs[key] != value:
            raise SchemaGenerationError(
                f"Conflicting $defs for '{key}' in {context}. "
                f"Two types define nested classes with the same name "
                f"but different structures. Consider renaming one of the "
                f"nested classes to be unique.",
                conflicting_key=key,
                field_name=field_name,
            )
        all_defs[key] = value


def wrap_as_json_schema(
    properties: dict[str, Any],
    required: list[str] | None = None,
    all_defs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap properties dict into JSON schema format for LLM providers.

    Args:
        properties: Dict of property name -> schema
        required: List of required property names (defaults to all properties)
        all_defs: Optional $defs to include at top level

    Returns:
        {"type": "json_schema", "schema": {"type": "object", ...}}
    """
    result_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties.keys()),
        "additionalProperties": False,
    }
    if all_defs:
        result_schema["$defs"] = all_defs
    return {"type": "json_schema", "schema": result_schema}
