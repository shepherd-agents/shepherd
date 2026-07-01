"""Binding ownership and lifecycle routing for ScopeProxy."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from shepherd_core.errors import BindingNotFoundError

if TYPE_CHECKING:
    from shepherd_core.context.kernel import ExecutionContext

    from ._binding_registry import BindingRegistry, BindingWithState
    from .substrate import ContextBinding, ContextRef, ImmutableScope

__all__ = ["BindingHost", "BindingLookup", "BindingService"]

# Reserved registry prefix for type-keyed bindings (CONTRACTS C5 / DECISIONS D2).
# `_synthetic_type_binding_name` mints names under this prefix; the name-keyed
# branch of `_normalize_bind_arguments` rejects user-supplied names that start
# with it (Tranche 7.5b PR 36).
_TYPE_NAME_PREFIX = "__type__:"


class BindingLookup(Protocol):
    """Parent-facing binding lookup surface."""

    def get_binding(self, name: str) -> BindingWithState: ...

    def get_context(self, name: str) -> ExecutionContext: ...

    def update_context(self, name: str, new_context: ExecutionContext) -> None: ...

    def all_bindings(self) -> list[BindingWithState]: ...

    def mark_binding_lifecycle(
        self,
        name: str,
        *,
        is_prepared: bool | None = None,
        in_lifecycle: bool | None = None,
    ) -> None: ...


class BindingHost(Protocol):
    """Narrow host contract for the binding subsystem."""

    @property
    def _binding_parent(self) -> BindingLookup | None: ...

    def _binding_snapshot(self) -> ImmutableScope: ...

    def _replace_binding_snapshot(self, scope: ImmutableScope) -> None: ...

    def _has_resumed_binding_layers(self) -> bool: ...

    def _apply_resumed_binding_effects(
        self,
        binding_name: str,
        context: ExecutionContext,
    ) -> ExecutionContext: ...

    def _create_context_ref(self, name: str) -> ContextRef[Any]: ...


class BindingService:
    """Owns binding mutations, lookup, and lifecycle-state wrapping."""

    __slots__ = ("_host", "_registry")

    def __init__(self, host: BindingHost, registry: BindingRegistry) -> None:
        self._host = host
        self._registry = registry

    def bind(
        self,
        name_or_context: Any,
        context: Any = None,
    ) -> ContextRef[Any]:
        name, bound_context = self._normalize_bind_arguments(name_or_context, context)

        # Two-phase binding keeps lifecycle state aligned with successful scope updates.
        new_scope = self._host._binding_snapshot().with_binding(name, bound_context)
        self._host._replace_binding_snapshot(new_scope)
        self._registry.on_bind(name)

        if self._host._has_resumed_binding_layers():
            replayed_context = self._host._apply_resumed_binding_effects(name, bound_context)
            self._host._replace_binding_snapshot(
                self._host._binding_snapshot().with_updated_context(name, replayed_context)
            )

        return self._host._create_context_ref(name)

    def get_binding(self, name: str) -> BindingWithState:
        binding = self._local_binding(name)
        if binding is None and self._host._binding_parent is not None:
            return self._host._binding_parent.get_binding(name)
        if binding is None:
            binding = self._host._binding_snapshot().get_binding(name)
        if binding is None:
            raise BindingNotFoundError(name, self._available_local_names())
        return self.wrap_local_binding(binding)

    def get_context(self, name: str) -> ExecutionContext:
        binding = self._local_binding(name)
        if binding is None and self._host._binding_parent is not None:
            return self._host._binding_parent.get_context(name)
        if binding is None:
            binding = self._host._binding_snapshot().get_binding(name)
        if binding is None:
            raise BindingNotFoundError(name, self._available_local_names())
        return binding.context

    def update_context(self, name: str, new_context: ExecutionContext) -> None:
        if self._local_binding(name) is not None:
            self._host._replace_binding_snapshot(self._host._binding_snapshot().with_updated_context(name, new_context))
            return
        if self._host._binding_parent is not None:
            self._host._binding_parent.update_context(name, new_context)
            return
        raise BindingNotFoundError(name, self._available_local_names())

    def all_bindings(self) -> list[BindingWithState]:
        result = self.local_bindings()
        seen_names = {binding.name for binding in result}

        if self._host._binding_parent is not None:
            for parent_binding in self._host._binding_parent.all_bindings():
                if parent_binding.name not in seen_names:
                    result.append(parent_binding)
                    seen_names.add(parent_binding.name)

        return result

    def mark_lifecycle(
        self,
        name: str,
        *,
        is_prepared: bool | None = None,
        in_lifecycle: bool | None = None,
    ) -> None:
        if self._local_binding(name) is not None:
            self._registry.mark_state(name, is_prepared=is_prepared, in_lifecycle=in_lifecycle)
            return
        if self._host._binding_parent is not None:
            self._host._binding_parent.mark_binding_lifecycle(
                name,
                is_prepared=is_prepared,
                in_lifecycle=in_lifecycle,
            )
            return
        raise BindingNotFoundError(name, self._available_local_names())

    def wrap_local_binding(self, binding: ContextBinding) -> BindingWithState:
        return self._registry.wrap_binding(binding)

    def local_bindings(self) -> list[BindingWithState]:
        return self._registry.all_with_state()

    def copy_lifecycle_state_to(self, other: BindingService) -> None:
        self._registry.copy_state_to(other._registry)

    def reset_lifecycle_for_fork(self) -> None:
        self._registry.reset_lifecycle_for_fork()

    def _local_binding(self, name: str) -> ContextBinding | None:
        return self._host._binding_snapshot()._binding_index.get(name)

    def _available_local_names(self) -> list[str]:
        return [binding.name for binding in self._host._binding_snapshot()._bindings]

    def _normalize_bind_arguments(
        self,
        name_or_context: Any,
        context: Any = None,
    ) -> tuple[str, ExecutionContext]:
        name: str
        if context is None:
            if isinstance(name_or_context, str):
                raise TypeError(
                    f"bind() called with string '{name_or_context}' but no context. "
                    f"Use scope.bind('{name_or_context}', context) or scope.bind(context)."
                )
            context = name_or_context
            binding_name: str | None = getattr(context, "__binding_name__", None)
            if binding_name is None:
                raise ValueError(
                    f"{type(context).__name__} has no __binding_name__. Use scope.bind('name', context) to specify one."
                )
            name = binding_name
        elif isinstance(name_or_context, str):
            # name-keyed form: bind("name", context)
            if name_or_context.startswith(_TYPE_NAME_PREFIX):
                raise ValueError(
                    f"binding name {name_or_context!r} starts with "
                    f"reserved prefix {_TYPE_NAME_PREFIX!r}; use "
                    f"scope.bind(T, value) for type-keyed bindings."
                )
            name = name_or_context
        elif isinstance(name_or_context, type):
            # CONTRACTS C5 / DECISIONS D2 type-keyed form: bind(T, value).
            # current_binding(T) ignores the binding name and matches on
            # context type, so a synthetic name is sufficient as a
            # registry slot.
            target_type = name_or_context
            if not isinstance(context, target_type):
                raise TypeError(
                    f"bind({target_type.__name__}, value): value is "
                    f"{type(context).__name__}, not a {target_type.__name__}."
                )
            name = _synthetic_type_binding_name(target_type)
        else:
            raise TypeError(
                f"First argument must be a string name or a type, got "
                f"{type(name_or_context).__name__}. "
                f"Use scope.bind('name', context), scope.bind(T, context), "
                f"or scope.bind(context)."
            )
        return name, context


def _synthetic_type_binding_name(t: type) -> str:
    """Stable, unique name slot for a type-keyed binding.

    The name is opaque to ``current_binding(T)`` (which matches by
    context type, not name) but must not collide with user-chosen
    string names used by the legacy name-keyed form. The
    ``__type__:`` prefix is reserved and enforced — name-keyed
    ``Scope.bind(name, value)`` rejects names starting with this
    prefix.
    """
    qualname = getattr(t, "__qualname__", t.__name__)
    return f"{_TYPE_NAME_PREFIX}{t.__module__}.{qualname}"
