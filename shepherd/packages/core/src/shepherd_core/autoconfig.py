"""Schema extraction and model construction for config inference.

Provides utilities to extract ``Infer``-annotated fields from Pydantic models
and build filtered models suitable for LLM-driven inference.

Usage::

    from shepherd_core.autoconfig import extract_infer_fields, build_inference_model

    fields = extract_infer_fields(PRReviewConfig)
    # {'guidelines': {type: str, description: '...', ...}, ...}

    FilteredModel = build_inference_model(PRReviewConfig)
    # Pydantic model with only Infer-annotated fields
"""

from __future__ import annotations

from typing import Any, get_args, get_type_hints

from pydantic import BaseModel, Field, create_model
from pydantic.fields import PydanticUndefined  # type: ignore[attr-defined]
from shepherd_runtime.task.markers import InputMarker

from ._infer import _InferMarker


def extract_infer_fields(cls: type[BaseModel]) -> dict[str, dict[str, Any]]:
    """Extract Infer-annotated fields from a BaseModel class.

    Supports two annotation styles:

    - ``Annotated[str, Infer]`` on plain BaseModel config classes
    - ``Input(str, infer=True)`` on @task classes

    Returns:
        Dict of ``{field_name: {type, description, default, has_default_factory}}``.
        Only fields marked as inferable are included.
    """
    hints = get_type_hints(cls, include_extras=True)
    model_fields = cls.model_fields
    result: dict[str, dict[str, Any]] = {}

    for name, hint in hints.items():
        if name.startswith("_"):
            continue

        # Check if Infer is in the Annotated metadata
        metadata = getattr(hint, "__metadata__", ())
        has_infer = any(isinstance(m, _InferMarker) for m in metadata)

        # Also check for InputMarker(infer=True) on @task classes
        if not has_infer:
            has_infer = any(isinstance(m, InputMarker) and m.infer for m in metadata)

        if not has_infer:
            continue

        # Get the inner type (strip Annotated wrapper)
        inner_args = get_args(hint)
        inner_type = inner_args[0] if inner_args else hint

        # Get field info from Pydantic model_fields
        pydantic_field = model_fields.get(name)
        description = ""
        default: Any = ...
        has_default_factory = False

        if pydantic_field is not None:
            description = pydantic_field.description or ""
            if pydantic_field.default_factory is not None:
                has_default_factory = True
                default = pydantic_field.default_factory()  # type: ignore[call-arg]
            elif pydantic_field.default is not PydanticUndefined:
                default = pydantic_field.default
            else:
                default = ...

        result[name] = {
            "type": inner_type,
            "description": description,
            "default": default,
            "has_default_factory": has_default_factory,
        }

    return result


def build_inference_model(cls: type[BaseModel]) -> type[BaseModel]:
    """Build a Pydantic model with only the Infer-annotated fields.

    Uses ``pydantic.create_model()`` for dynamic model construction.
    The resulting model can be used as the output type for ``infer_from_context``.

    Nested model ``$defs`` are preserved when referenced by filtered fields
    (e.g., ``VerifyConfig`` in ``verify: VerifyConfig | None``).

    Args:
        cls: A BaseModel subclass with Infer-annotated fields.

    Returns:
        A new Pydantic BaseModel subclass with only the inferable fields.
    """
    infer_fields = extract_infer_fields(cls)
    field_definitions: dict[str, Any] = {}

    model_fields = cls.model_fields
    for name, info in infer_fields.items():
        pydantic_field = model_fields.get(name)
        if pydantic_field is not None:
            if info["has_default_factory"]:
                field_definitions[name] = (
                    info["type"],
                    Field(
                        default_factory=pydantic_field.default_factory,  # type: ignore[arg-type]
                        description=info["description"],
                    ),
                )
            elif info["default"] is not ...:
                field_definitions[name] = (
                    info["type"],
                    Field(default=info["default"], description=info["description"]),
                )
            else:
                field_definitions[name] = (
                    info["type"],
                    Field(description=info["description"]),
                )
        else:
            field_definitions[name] = (info["type"], ...)

    return create_model(
        f"Infer{cls.__name__}",
        **field_definitions,
    )


__all__ = [
    "build_inference_model",
    "extract_infer_fields",
]
