"""Task runtime option parsing and resolved runtime-view payloads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

JsonObject = dict[str, object]

_SUPPORTED_RUNTIME_FIELDS = frozenset({"trace", "provider", "model"})
_RESERVED_RUNTIME_FIELDS = frozenset(
    {
        "session",
        "budget",
        "budget_seconds",
        "timeout",
        "device",
        "plan",
        "world",
        "tools",
        "max_turns",
        "provider_options",
    }
)
_SUPPORTED_TRACE_FIELDS = frozenset({"label", "tags"})
_SUPPORTED_PROVIDER_FIELDS = frozenset({"id", "profile", "mode"})
_SUPPORTED_MODEL_FIELDS = frozenset({"name"})


class RuntimeOptionsError(ValueError):
    """Raised when a runtime envelope is malformed or uses reserved fields."""


@dataclass(frozen=True)
class TraceRuntimeOptions:
    """Trace-facing runtime options requested by the caller."""

    label: str | None = None
    tags: tuple[str, ...] = ()

    def to_payload(self) -> JsonObject:
        payload: JsonObject = {}
        if self.label is not None:
            payload["label"] = self.label
        if self.tags:
            payload["tags"] = list(self.tags)
        return payload


@dataclass(frozen=True)
class ProviderRuntimeOptions:
    """Provider requested by the caller for this run."""

    id: str
    profile: str | None = None
    mode: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise RuntimeOptionsError("runtime.provider.id must be a non-empty string")
        if self.profile is not None and (not isinstance(self.profile, str) or not self.profile.strip()):
            raise RuntimeOptionsError("runtime.provider.profile must be null or a non-empty string")
        if self.mode is not None and self.mode not in {"chatgpt", "api_key"}:
            raise RuntimeOptionsError("runtime.provider.mode must be 'chatgpt' or 'api_key'")

    def to_payload(self) -> JsonObject:
        payload: JsonObject = {"id": self.id}
        if self.profile is not None:
            payload["profile"] = self.profile
        if self.mode is not None:
            payload["mode"] = self.mode
        return payload


@dataclass(frozen=True)
class ModelRuntimeOptions:
    """Model requested by the caller for this run."""

    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise RuntimeOptionsError("runtime.model.name must be a non-empty string")

    def to_payload(self) -> JsonObject:
        return {"name": self.name}


@dataclass(frozen=True)
class RuntimeOptions:
    """Sparse runtime envelope.

    ``trace is None`` means the caller did not request the trace sub-envelope.
    ``TraceRuntimeOptions()`` means the caller supplied ``{"trace": {}}``.
    """

    trace: TraceRuntimeOptions | None = None
    provider: ProviderRuntimeOptions | None = None
    model: ModelRuntimeOptions | None = None

    def to_payload(self) -> JsonObject:
        payload: JsonObject = {}
        if self.trace is not None:
            payload["trace"] = self.trace.to_payload()
        if self.provider is not None:
            payload["provider"] = self.provider.to_payload()
        if self.model is not None:
            payload["model"] = self.model.to_payload()
        return payload


@dataclass(frozen=True)
class ResolvedRuntimeView:
    """Requested runtime options plus execution facts resolved by the runtime."""

    requested: RuntimeOptions
    operation_id: str | None
    scope_ref: str | None
    scope_name: str | None
    scope_instance_id: str | None
    world_id: str | None
    session_id: str | None
    working_path: str | None
    isolation: str | None
    provider: str | None

    def to_payload(self) -> JsonObject:
        payload: JsonObject = {
            "trace": {} if self.requested.trace is None else self.requested.trace.to_payload(),
            "execution": {
                "operation_id": self.operation_id,
                "scope_ref": self.scope_ref,
                "scope_name": self.scope_name,
                "scope_instance_id": self.scope_instance_id,
                "world_id": self.world_id,
                "session_id": self.session_id,
                "working_path": self.working_path,
                "isolation": self.isolation,
                "provider": self.provider,
            },
        }
        if self.requested.provider is not None:
            payload["provider"] = self.requested.provider.to_payload()
        if self.requested.model is not None:
            payload["model"] = self.requested.model.to_payload()
        return payload


def parse_runtime_options(value: object | None) -> RuntimeOptions:
    """Parse the sparse ``runtime`` envelope.

    ``runtime.trace``, ``runtime.provider``, and ``runtime.model`` are
    supported in this tranche. Future fields fail closed so callers cannot
    accidentally rely on unimplemented semantics.
    """
    if value is None:
        return RuntimeOptions()
    if isinstance(value, RuntimeOptions):
        return value
    if not isinstance(value, Mapping):
        raise RuntimeOptionsError(f"runtime must be an object, got {type(value).__name__}")
    fields = set(value)
    reserved = sorted(fields & _RESERVED_RUNTIME_FIELDS)
    if reserved:
        raise RuntimeOptionsError(f"runtime field(s) reserved for future use: {', '.join(reserved)}")
    unknown = sorted(fields - _SUPPORTED_RUNTIME_FIELDS)
    if unknown:
        raise RuntimeOptionsError(f"unknown runtime field(s): {', '.join(unknown)}")
    return RuntimeOptions(
        trace=_parse_trace_runtime_options(value["trace"]) if "trace" in value else None,
        provider=_parse_provider_runtime_options(value["provider"]) if "provider" in value else None,
        model=_parse_model_runtime_options(value["model"]) if "model" in value else None,
    )


def merge_runtime_options(base: RuntimeOptions | None, override: RuntimeOptions | None) -> RuntimeOptions | None:
    """Merge two sparse runtime envelopes for chained facade options."""
    if base is None:
        return override
    if override is None:
        return base
    return RuntimeOptions(
        trace=_merge_trace_options(base.trace, override.trace),
        provider=override.provider if override.provider is not None else base.provider,
        model=override.model if override.model is not None else base.model,
    )


def resolve_runtime_view(
    requested: RuntimeOptions,
    *,
    operation_id: str | None,
    scope_ref: str | None,
    scope_name: str | None = None,
    scope_instance_id: str | None = None,
    world_id: str | None = None,
    session_id: str | None = None,
    working_path: str | None,
    isolation: str | None,
    provider: str | None,
) -> ResolvedRuntimeView:
    """Bind requested runtime options to execution facts observed at runtime."""
    return ResolvedRuntimeView(
        requested=requested,
        operation_id=operation_id,
        scope_ref=scope_ref,
        scope_name=scope_name,
        scope_instance_id=scope_instance_id,
        world_id=world_id,
        session_id=session_id,
        working_path=working_path,
        isolation=isolation,
        provider=provider,
    )


def runtime_requested_payload(options: RuntimeOptions | None) -> JsonObject:
    """Return the sparse requested-runtime JSON payload."""
    return (options or RuntimeOptions()).to_payload()


def runtime_resolved_payload(view: ResolvedRuntimeView | Mapping[str, object] | None) -> JsonObject:
    """Return the resolved-runtime JSON payload."""
    if view is None:
        return {}
    if isinstance(view, ResolvedRuntimeView):
        return view.to_payload()
    return dict(view)


def _parse_trace_runtime_options(value: object) -> TraceRuntimeOptions:
    if not isinstance(value, Mapping):
        raise RuntimeOptionsError(f"runtime.trace must be an object, got {type(value).__name__}")
    fields = set(value)
    unknown = sorted(fields - _SUPPORTED_TRACE_FIELDS)
    if unknown:
        raise RuntimeOptionsError(f"unknown runtime.trace field(s): {', '.join(unknown)}")
    label = _parse_label(value.get("label"))
    tags = _parse_tags(value.get("tags", ()))
    return TraceRuntimeOptions(label=label, tags=tags)


def _parse_provider_runtime_options(value: object) -> ProviderRuntimeOptions:
    if isinstance(value, str):
        return ProviderRuntimeOptions(id=value)
    if not isinstance(value, Mapping):
        raise RuntimeOptionsError(f"runtime.provider must be an object or string, got {type(value).__name__}")
    fields = set(value)
    unknown = sorted(fields - _SUPPORTED_PROVIDER_FIELDS)
    if unknown:
        raise RuntimeOptionsError(f"unknown runtime.provider field(s): {', '.join(unknown)}")
    provider_id = value.get("id")
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise RuntimeOptionsError("runtime.provider.id must be a non-empty string")
    profile = value.get("profile")
    if profile is not None and (not isinstance(profile, str) or not profile.strip()):
        raise RuntimeOptionsError("runtime.provider.profile must be null or a non-empty string")
    mode = value.get("mode")
    if mode is not None and mode not in {"chatgpt", "api_key"}:
        raise RuntimeOptionsError("runtime.provider.mode must be 'chatgpt' or 'api_key'")
    return ProviderRuntimeOptions(id=provider_id, profile=profile, mode=mode)


def _parse_model_runtime_options(value: object) -> ModelRuntimeOptions:
    if isinstance(value, str):
        return ModelRuntimeOptions(name=value)
    if not isinstance(value, Mapping):
        raise RuntimeOptionsError(f"runtime.model must be an object or string, got {type(value).__name__}")
    fields = set(value)
    unknown = sorted(fields - _SUPPORTED_MODEL_FIELDS)
    if unknown:
        raise RuntimeOptionsError(f"unknown runtime.model field(s): {', '.join(unknown)}")
    model_name = value.get("name")
    if not isinstance(model_name, str) or not model_name.strip():
        raise RuntimeOptionsError("runtime.model.name must be a non-empty string")
    return ModelRuntimeOptions(name=model_name)


def _merge_trace_options(
    base: TraceRuntimeOptions | None,
    override: TraceRuntimeOptions | None,
) -> TraceRuntimeOptions | None:
    if override is None:
        return base
    if base is None:
        return override
    return TraceRuntimeOptions(
        label=override.label if override.label is not None else base.label,
        tags=override.tags or base.tags,
    )


def _parse_label(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeOptionsError("runtime.trace.label must be a non-empty string")
    return value


def _parse_tags(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise RuntimeOptionsError("runtime.trace.tags must be a list or tuple of strings")
    tags: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item:
            raise RuntimeOptionsError("runtime.trace.tags entries must be non-empty strings")
        if item in seen:
            raise RuntimeOptionsError(f"duplicate runtime.trace tag: {item!r}")
        seen.add(item)
        tags.append(item)
    return tuple(tags)
