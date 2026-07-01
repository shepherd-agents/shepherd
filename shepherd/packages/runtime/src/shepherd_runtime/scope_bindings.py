"""Type-keyed binding lookup over the active Scope chain.

CONTRACTS C4 + DECISIONS D2.

Resolution order per D2:

1. Walk the active Scope outward.
2. For each scope, check exact-class match then ``isinstance`` match.
3. If exactly one binding satisfies at the innermost depth, return it.
4. Multiple at same depth -> ``AmbiguousBindingError(T)``.
5. None on the chain -> ``NoBindingForTypeError(T)``.

The ``ContextRef[T]`` proxy delegates attribute access to the current
value and re-resolves on each access so rebinding in the same Scope
updates the reference transparently.

Name-keyed ``scope.bind("name", value)`` is a separate (deletion-
target) form per D5; ``current_binding(T)`` ignores the binding
*name* and matches on context type only.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from shepherd_runtime._scope.scope import current_scope

__all__ = [
    "AmbiguousBindingError",
    "ContextRef",
    "NoBindingForTypeError",
    "TypedContextRef",
    "current_binding",
]


T = TypeVar("T")


class AmbiguousBindingError(LookupError):
    """Two or more bindings at the same innermost depth match ``T``."""

    def __init__(self, target_type: type) -> None:
        super().__init__(
            f"ambiguous binding: multiple values match type {target_type.__name__!r} "
            f"at the same Scope depth"
        )
        self.target_type = target_type


class NoBindingForTypeError(LookupError):
    """No binding on the active Scope chain matches ``T``."""

    def __init__(self, target_type: type) -> None:
        super().__init__(
            f"no binding for type {target_type.__name__!r} in the active Scope chain"
        )
        self.target_type = target_type


class ContextRef(Generic[T]):
    """Type-keyed live reference to a scope binding (CONTRACTS C4).

    Returned by :func:`current_binding`. Delegates attribute access to
    the current value; exposes ``.value`` for code that wants the
    underlying object explicitly. Rebinding in the same Scope updates
    the reference transparently because each access re-resolves through
    ``current_binding``.

    Disambiguation note. There is a sibling class
    ``shepherd_core.scope.context_ref.ContextRef`` returned by
    ``Scope.bind(...)``; that one is *name-keyed* and stores
    ``(accessor, name)`` rather than a target type. They have
    overlapping ``.value`` ergonomics but are distinct types. Code
    that needs to disambiguate should import this class as
    ``TypedContextRef`` (re-exported below).
    """

    def __init__(self, target_type: type[T]) -> None:
        self._target_type = target_type

    @property
    def value(self) -> T:
        return _resolve(self._target_type)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(_resolve(self._target_type), name)

    def __repr__(self) -> str:
        return f"ContextRef[{self._target_type.__name__}]"


# Unambiguous alias for code that needs to distinguish this class from
# the name-keyed ``shepherd_core.scope.context_ref.ContextRef``. The
# original name is retained for CONTRACTS C4 compatibility.
TypedContextRef = ContextRef


def current_binding(target_type: type[T]) -> ContextRef[T]:
    """Return a live ``ContextRef[T]`` for the innermost matching binding.

    Raises ``NoBindingForTypeError`` if no binding matches anywhere on the
    Scope chain. Raises ``AmbiguousBindingError`` if two or more bindings
    match at the same innermost depth.

    The returned ``ContextRef`` re-resolves on every attribute access,
    so subsequent rebinding in the active Scope is visible without
    re-calling ``current_binding``.
    """
    # Resolve once to validate the lookup; the ContextRef will re-resolve
    # lazily on subsequent accesses.
    _resolve(target_type)
    return ContextRef(target_type)


def _resolve(target_type: type[T]) -> T:
    """Walk the scope chain (innermost first) and return the matching binding.

    Implementation detail: ``Scope.all_bindings()`` already includes
    inherited bindings from parent scopes. Per D2, we walk from
    innermost outward; the production semantics are equivalent to
    ``all_bindings()`` returning the innermost-first ordering.
    """
    scope = current_scope()
    if scope is None:
        raise NoBindingForTypeError(target_type)

    bindings = scope.all_bindings()  # innermost-first order from the scope chain
    exact: list[Any] = []
    iso: list[Any] = []
    for b in bindings:
        ctx = b.context
        if type(ctx) is target_type:
            exact.append(ctx)
        elif isinstance(ctx, target_type):
            iso.append(ctx)

    chosen = exact or iso
    if not chosen:
        raise NoBindingForTypeError(target_type)
    if len(chosen) > 1:
        raise AmbiguousBindingError(target_type)
    return chosen[0]
