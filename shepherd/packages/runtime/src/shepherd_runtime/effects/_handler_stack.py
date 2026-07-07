"""Dynamic handler stack for the Plan 04 effect surface."""

from __future__ import annotations

import inspect
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from shepherd_runtime.effects.effect_kind import effect_key_for_event, parse_matcher_kind_sugar
from shepherd_runtime.effects.shape_detection import HandlerShape, HandlerSignatureError, detect_handler_shape

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class HandlerBinding:
    """One dynamically installed pure-response handler binding."""

    key: str | type
    fn: Callable[..., Any]
    shape: HandlerShape
    handler_id: str
    binding_ref: str


_handler_stack: ContextVar[tuple[HandlerBinding, ...]] = ContextVar(
    "shepherd_effect_handler_stack",
    default=(),
)


def make_bindings(items: tuple[tuple[Any, Callable[..., Any]], ...]) -> tuple[HandlerBinding, ...]:
    """Validate and normalize user-facing ``handle(...)`` bindings."""
    bindings: list[HandlerBinding] = []
    for raw_key, fn in items:
        key = _normalize_key(raw_key)
        shape = detect_handler_shape(fn)
        bindings.append(
            HandlerBinding(
                key=key,
                fn=fn,
                shape=shape,
                handler_id=_handler_id(key, fn),
                binding_ref=_binding_ref(key),
            )
        )
    return tuple(bindings)


def push_handlers(bindings: tuple[HandlerBinding, ...]) -> Token[tuple[HandlerBinding, ...]]:
    """Push handler bindings into the current dynamic context."""
    return _handler_stack.set((*_handler_stack.get(), *bindings))


def pop_handlers(token: Token[tuple[HandlerBinding, ...]]) -> None:
    """Restore the handler stack to a previous token."""
    _handler_stack.reset(token)


def resolve_handler(effect_or_key: object) -> HandlerBinding | None:
    """Return the nearest handler matching an effect instance or string key."""
    for binding in reversed(_handler_stack.get()):
        if _matches(binding.key, effect_or_key):
            return binding
    return None


async def invoke_handler(binding: HandlerBinding, payload: object) -> Any:
    """Invoke a pure-response handler, awaiting async results."""
    if binding.shape != "pure_response":
        raise HandlerSignatureError(
            f"{binding.handler_id}: supervisor handlers are not supported by the Phase 1 pure-response dispatch path"
        )
    result = binding.fn(payload)
    if inspect.isawaitable(result):
        return await result
    return result


# Dual-key compatibility shim (Bug 1, 2132 W0.1): the spec and curriculum teach
# handle("model.call.requested", ...) while dispatch resolves "model.call", so the
# documented mock idiom was silently ignored and a reachable provider would take
# the call instead. Installation normalizes the taught spelling onto the dispatch
# key: both spellings resolve, innermost-wins is preserved across mixed keys, and
# the *recorded* effect-kind string stays "model.call" — flipping the recorded
# kind is a durable-vocabulary decision (arch-notes #2 / D-3), deliberately not
# taken here.
_KEY_ALIASES: dict[str, str] = {"model.call.requested": "model.call"}


def _normalize_key(key: object) -> str | type:
    if isinstance(key, str):
        mode, kind = parse_matcher_kind_sugar(key)
        if mode != "exact":
            raise HandlerSignatureError(
                "handle(...) wildcard and Match-backed handler activation is deferred in this cut; "
                f"use an exact effect-kind string or effect class, got {key!r}"
            )
        return _KEY_ALIASES.get(kind, kind)
    if isinstance(key, type):
        return key
    raise HandlerSignatureError(
        f"handle(...) effect key must be an effect class or effect-kind string; got {type(key).__name__}"
    )


def _matches(key: str | type, effect_or_key: object) -> bool:
    if isinstance(effect_or_key, str):
        return isinstance(key, str) and key == effect_or_key
    if isinstance(key, str):
        return effect_key_for_event(effect_or_key) == key
    return isinstance(effect_or_key, key)


def _handler_id(key: str | type, fn: Callable[..., Any]) -> str:
    if isinstance(key, str):
        label = key
    else:
        label = f"{key.__module__}.{key.__qualname__}"
    fn_label = getattr(fn, "__qualname__", getattr(fn, "__name__", "handler"))
    return f"local.{label}:{fn_label}"


def _binding_ref(key: str | type) -> str:
    if isinstance(key, str):
        label = key
    else:
        label = f"{key.__module__}.{key.__qualname__}"
    return f"binding:{label}"


__all__ = [
    "HandlerBinding",
    "invoke_handler",
    "make_bindings",
    "pop_handlers",
    "push_handlers",
    "resolve_handler",
]
