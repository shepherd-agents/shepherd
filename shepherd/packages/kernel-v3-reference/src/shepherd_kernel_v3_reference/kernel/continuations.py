"""Durable continuation images for the storage-free semantic core."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import TYPE_CHECKING, Any, Literal

from shepherd_kernel_v3_reference.kernel.refs import content_ref

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.kernel.ir import Ref

CONTINUATION_IMAGE_SCHEMA_VERSION = "shepherd_kernel_v3_reference.continuation-image.v1"
CONTINUATION_CONTROL_SCHEMA_VERSION = "shepherd_kernel_v3_reference.continuation-control.v1"

ContinuationImagePosition = Literal["value"]
ContinuationImageKind = Literal[
    "full",
    "captured-worker",
    "outer",
    "handler-continuation",
    "handler-dynamic-tail",
    "empty-terminal",
]

CONTINUATION_IMAGE_POSITIONS = frozenset(("value",))
CONTINUATION_IMAGE_KINDS = frozenset(
    (
        "full",
        "captured-worker",
        "outer",
        "handler-continuation",
        "handler-dynamic-tail",
        "empty-terminal",
    )
)


class _FrozenJsonDict(dict[str, Any]):
    """Dict-shaped immutable mapping so JSON encoders still see an object."""

    __slots__ = ()

    def _blocked(self, *args: object, **kwargs: object) -> None:
        raise TypeError("ContinuationImage JSON mappings are immutable")

    __setitem__ = _blocked
    __delitem__ = _blocked
    clear = _blocked
    pop = _blocked
    popitem = _blocked  # type: ignore[assignment]
    setdefault = _blocked
    update = _blocked
    __ior__ = _blocked  # type: ignore[assignment]


@dataclass(frozen=True)
class ContinuationImage:
    """JSON-compatible image of a defunctionalized continuation.

    The image is not a storage record. It is the semantic payload that a
    storage backend can retain under `ref` if it wants to make continuation
    refs restartable outside the current Python process.
    """

    program_ref: Ref
    branch_ref: Ref
    branch_scope_ref: Ref | None
    position: ContinuationImagePosition
    continuation_kind: ContinuationImageKind
    execution_context_ref: Ref
    execution_context: Mapping[str, Any]
    frames: tuple[Mapping[str, Any], ...]
    required_schema_refs: tuple[Ref, ...] = ()
    code_identity_refs: tuple[Ref, ...] = ()
    image_schema_version: str = CONTINUATION_IMAGE_SCHEMA_VERSION
    ref: Ref | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "execution_context",
            _freeze_mapping_value(
                self.execution_context,
                context="ContinuationImage.execution_context",
            ),
        )
        object.__setattr__(
            self,
            "frames",
            _freeze_mapping_tuple_value(
                self.frames,
                context="ContinuationImage.frames",
            ),
        )
        object.__setattr__(
            self,
            "required_schema_refs",
            _freeze_str_tuple_value(
                self.required_schema_refs,
                context="ContinuationImage.required_schema_refs",
            ),
        )
        object.__setattr__(
            self,
            "code_identity_refs",
            _freeze_str_tuple_value(
                self.code_identity_refs,
                context="ContinuationImage.code_identity_refs",
            ),
        )
        payload = continuation_image_payload(self)
        expected_ref = continuation_image_ref(payload)
        if self.ref is not None and self.ref != expected_ref:
            raise ValueError("ContinuationImage.ref does not match its content-addressed payload")
        object.__setattr__(self, "ref", expected_ref)


def continuation_image_ref(payload: Mapping[str, Any]) -> Ref:
    """Return the content-addressed ref for an image's control identity."""

    if "ref" in payload:
        raise ValueError("ContinuationImage ref is excluded from image identity")
    return content_ref("continuation-image", payload)


def continuation_control_ref(payload: Mapping[str, Any]) -> Ref:
    """Return the content-addressed ref for role-independent control identity."""

    if "ref" in payload:
        raise ValueError("Continuation control ref is excluded from control identity")
    return content_ref("continuation-control", payload)


def continuation_control_payload(
    *,
    program_ref: Ref,
    branch_ref: Ref,
    branch_scope_ref: Ref | None,
    position: ContinuationImagePosition,
    frames: tuple[Mapping[str, Any], ...],
    control_schema_version: str = CONTINUATION_CONTROL_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Return the canonical payload for dynamic continuation ancestry."""

    if control_schema_version != CONTINUATION_CONTROL_SCHEMA_VERSION:
        raise ValueError(f"Continuation control schema version must be {CONTINUATION_CONTROL_SCHEMA_VERSION!r}")
    if position not in CONTINUATION_IMAGE_POSITIONS:
        raise ValueError(f"unknown continuation control position: {position!r}")
    payload = {
        "control_schema_version": control_schema_version,
        "program_ref": program_ref,
        "branch_ref": branch_ref,
        "branch_scope_ref": branch_scope_ref,
        "position": position,
        "frames": frames,
    }
    _require_json_compatible(payload, context="ContinuationControl")
    return payload


def continuation_image_payload(image: ContinuationImage) -> dict[str, Any]:
    """Return the canonical image payload, excluding its content-addressed ref."""

    if image.image_schema_version != CONTINUATION_IMAGE_SCHEMA_VERSION:
        raise ValueError(f"ContinuationImage.image_schema_version must be {CONTINUATION_IMAGE_SCHEMA_VERSION!r}")
    if image.position not in CONTINUATION_IMAGE_POSITIONS:
        raise ValueError(f"unknown ContinuationImage.position: {image.position!r}")
    if image.continuation_kind not in CONTINUATION_IMAGE_KINDS:
        raise ValueError(f"unknown ContinuationImage.continuation_kind: {image.continuation_kind!r}")
    payload = {
        "image_schema_version": image.image_schema_version,
        "program_ref": image.program_ref,
        "branch_ref": image.branch_ref,
        "branch_scope_ref": image.branch_scope_ref,
        "position": image.position,
        "continuation_kind": image.continuation_kind,
        "execution_context_ref": image.execution_context_ref,
        "execution_context": image.execution_context,
        "frames": image.frames,
        "required_schema_refs": image.required_schema_refs,
        "code_identity_refs": image.code_identity_refs,
    }
    _require_json_compatible(payload, context="ContinuationImage")
    return payload


def continuation_image_to_json(image: ContinuationImage) -> dict[str, Any]:
    return {"ref": image.ref, **_jsonify(continuation_image_payload(image))}


def continuation_image_from_json(data: Mapping[str, Any]) -> ContinuationImage:
    schema_version = _require_str(data, "image_schema_version")
    if schema_version != CONTINUATION_IMAGE_SCHEMA_VERSION:
        raise ValueError(f"unsupported ContinuationImage.image_schema_version: {schema_version!r}")
    position = _require_str(data, "position")
    if position not in CONTINUATION_IMAGE_POSITIONS:
        raise ValueError(f"unknown ContinuationImage.position: {position!r}")
    continuation_kind = _require_str(data, "continuation_kind")
    if continuation_kind not in CONTINUATION_IMAGE_KINDS:
        raise ValueError(f"unknown ContinuationImage.continuation_kind: {continuation_kind!r}")

    return ContinuationImage(
        ref=_require_str(data, "ref"),
        program_ref=_require_str(data, "program_ref"),
        branch_ref=_require_str(data, "branch_ref"),
        branch_scope_ref=_require_optional_str(data, "branch_scope_ref"),
        position=position,  # type: ignore[arg-type]
        continuation_kind=continuation_kind,  # type: ignore[arg-type]
        execution_context_ref=_require_str(data, "execution_context_ref"),
        execution_context=_require_mapping(data, "execution_context"),
        frames=_require_mapping_tuple(data, "frames"),
        required_schema_refs=_require_str_tuple(data, "required_schema_refs"),
        code_identity_refs=_require_str_tuple(data, "code_identity_refs"),
        image_schema_version=schema_version,
    )


def _require_str(data: Mapping[str, Any], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise TypeError(f"ContinuationImage.{key} must be a string")
    return value


def _require_optional_str(data: Mapping[str, Any], key: str) -> str | None:
    value = data[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"ContinuationImage.{key} must be a string or null")
    return value


def _require_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data[key]
    if not isinstance(value, Mapping):
        raise TypeError(f"ContinuationImage.{key} must be a mapping")
    _require_json_compatible(value, context=f"ContinuationImage.{key}")
    return dict(value)


def _require_mapping_tuple(
    data: Mapping[str, Any],
    key: str,
) -> tuple[Mapping[str, Any], ...]:
    value = data[key]
    if not isinstance(value, tuple | list):
        raise TypeError(f"ContinuationImage.{key} must be a sequence")
    frames: list[Mapping[str, Any]] = []
    for idx, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise TypeError(f"ContinuationImage.{key}[{idx}] must be a mapping")
        _require_json_compatible(item, context=f"ContinuationImage.{key}[{idx}]")
        frames.append(dict(item))
    return tuple(frames)


def _require_str_tuple(data: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = data[key]
    if not isinstance(value, tuple | list):
        raise TypeError(f"ContinuationImage.{key} must be a sequence")
    refs: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str):
            raise TypeError(f"ContinuationImage.{key}[{idx}] must be a string")
        refs.append(item)
    return tuple(refs)


def _jsonify(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_jsonify(item) for item in value]
    if isinstance(value, list):
        return [_jsonify(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _jsonify(item) for key, item in value.items()}
    return value


def _freeze_mapping_value(value: Any, *, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{context} must be a mapping")
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError(f"{context} contains a non-string mapping key")
        frozen[key] = _freeze_json_compatible(item, context=f"{context}.{key}")
    return _FrozenJsonDict(frozen)


def _freeze_mapping_tuple_value(
    value: Any,
    *,
    context: str,
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, tuple | list):
        raise TypeError(f"{context} must be a sequence")
    frames: list[Mapping[str, Any]] = []
    for idx, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise TypeError(f"{context}[{idx}] must be a mapping")
        frames.append(_freeze_mapping_value(item, context=f"{context}[{idx}]"))
    return tuple(frames)


def _freeze_str_tuple_value(value: Any, *, context: str) -> tuple[str, ...]:
    if not isinstance(value, tuple | list):
        raise TypeError(f"{context} must be a sequence")
    refs: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str):
            raise TypeError(f"{context}[{idx}] must be a string")
        refs.append(item)
    return tuple(refs)


def _freeze_json_compatible(value: Any, *, context: str) -> Any:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise TypeError(f"{context} contains a non-finite float")
        return value
    if isinstance(value, tuple | list):
        return tuple(_freeze_json_compatible(item, context=f"{context}[{idx}]") for idx, item in enumerate(value))
    if isinstance(value, Mapping):
        return _freeze_mapping_value(value, context=context)
    raise TypeError(f"{context} contains a non-JSON-compatible value: {value!r}")


def _require_json_compatible(value: Any, *, context: str) -> None:
    if value is None or isinstance(value, bool | int | str):
        return
    if isinstance(value, float):
        if not isfinite(value):
            raise TypeError(f"{context} contains a non-finite float")
        return
    if isinstance(value, tuple | list):
        for idx, item in enumerate(value):
            _require_json_compatible(item, context=f"{context}[{idx}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{context} contains a non-string mapping key")
            _require_json_compatible(item, context=f"{context}.{key}")
        return
    raise TypeError(f"{context} contains a non-JSON-compatible value: {value!r}")
