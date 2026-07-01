"""Focused tests for the emission collaborator."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any, Literal

import pytest
from pydantic import BaseModel
from shepherd_core.context.kernel import ExecutionContextDefaults
from shepherd_core.effects import ContextPrepared, Effect
from shepherd_core.types import ProviderBinding, ReversibilityLevel
from shepherd_runtime._scope._emission import EmissionEngine
from shepherd_runtime.scope import Scope, ScopeProxy

if TYPE_CHECKING:
    from shepherd_core.scope.stream import EffectLayer


class IncrementEffect(Effect):
    effect_type: Literal["increment"] = "increment"
    delta: int = 1


class CounterContext(BaseModel, ExecutionContextDefaults):
    name: str
    value: int = 0
    seen_deltas: tuple[int, ...] = ()

    @property
    def context_id(self) -> str:
        return f"counter:{self.name}"

    @property
    def reversibility(self) -> ReversibilityLevel:
        return ReversibilityLevel.AUTO

    def configure(self) -> ProviderBinding:
        return ProviderBinding()

    def apply_effect(self, effect: Effect) -> CounterContext:
        if isinstance(effect, IncrementEffect):
            return self.model_copy(
                update={
                    "value": self.value + effect.delta,
                    "seen_deltas": (*self.seen_deltas, effect.delta),
                }
            )
        if isinstance(effect, ContextPrepared):
            return self.model_copy(
                update={
                    "value": self.value + 1,
                    "seen_deltas": (*self.seen_deltas, 1),
                }
            )
        return self


def _bind_counter(scope: ScopeProxy, name: str) -> None:
    scope.bind(name, CounterContext(name=name))


class ScopeHostAdapter:
    def __init__(self, scope: ScopeProxy, parent: ScopeHostAdapter | None = None) -> None:
        self._scope = scope
        self._parent = parent
        self.persisted_layers: list[EffectLayer] = []
        self.engine = EmissionEngine(self)

    @property
    def emission_lock(self) -> Any:
        return self._scope._emit_lock

    @property
    def emission_scope_id(self) -> str:
        return self._scope.id

    @property
    def emission_depth(self) -> int:
        return self._scope._depth

    def emission_snapshot(self) -> Any:
        return self._scope._scope

    def replace_emission_snapshot(self, scope: Any) -> None:
        self._scope._scope = scope

    def persist_emitted_layer(self, layer: EffectLayer) -> None:
        self.persisted_layers.append(layer)

    def propagate_emitted_layer(self, layer: EffectLayer) -> None:
        if self._parent is not None:
            self._parent.receive_layer(layer)

    def receive_layer(self, layer: EffectLayer) -> None:
        self.engine.receive_layer(layer)


class PersistFailingScopeHostAdapter(ScopeHostAdapter):
    def __init__(self, scope: ScopeProxy, parent: ScopeHostAdapter | None = None, *, error: RuntimeError) -> None:
        super().__init__(scope, parent=parent)
        self._error = error

    def persist_emitted_layer(self, layer: EffectLayer) -> None:
        raise self._error


class TestEmissionEngine:
    def test_engine_preserves_scope_metadata_and_parent_propagation(self) -> None:
        with Scope(root=True) as parent:
            _bind_counter(parent, "workspace")
            child = parent.child()

            parent_host = ScopeHostAdapter(parent)
            child_host = ScopeHostAdapter(child, parent=parent_host)

            emitted = child_host.engine.emit(IncrementEffect(binding_name="workspace", delta=2))

            assert emitted.sequence == 0
            assert emitted.scope_id == child.id
            assert emitted.scope_depth == 1
            assert child_host.persisted_layers[0].scope_id == child.id
            assert parent_host.persisted_layers[0].scope_id == child.id
            assert parent.get_context("workspace").value == 2
            assert child.get_context("workspace").value == 2

    def test_engine_host_interface_is_sufficient_for_concurrent_sequence_assignment(self) -> None:
        with Scope(root=True) as scope:
            _bind_counter(scope, "counter")
            host = ScopeHostAdapter(scope)

            def worker() -> int:
                layer = host.engine.emit(IncrementEffect(binding_name="counter", delta=1))
                return layer.sequence

            sequences: list[int] = []
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(worker) for _ in range(12)]
                for future in as_completed(futures):
                    sequences.append(future.result())

            assert sorted(sequences) == list(range(12))
            assert len(scope.effects) == 12
            assert scope.get_context("counter").value == 12

    def test_engine_failure_boundaries_match_current_local_then_persist_ordering(self) -> None:
        with Scope(root=True) as parent:
            _bind_counter(parent, "workspace")
            child = parent.child()

            parent_host = ScopeHostAdapter(parent)
            child_host = PersistFailingScopeHostAdapter(
                child,
                parent=parent_host,
                error=RuntimeError("child persist failed"),
            )

            with pytest.raises(RuntimeError, match="child persist failed"):
                child_host.engine.emit(IncrementEffect(binding_name="workspace", delta=4))

            assert child.effects[0].effect.delta == 4
            assert len(child.effects) == 1
            assert parent.get_context("workspace").value == 0
            assert child.get_context("workspace").value == 0
            assert len(parent.effects) == 0
            assert child_host.persisted_layers == []
            assert parent_host.persisted_layers == []

        with Scope(root=True) as parent:
            _bind_counter(parent, "workspace")
            child = parent.child()

            parent_host = PersistFailingScopeHostAdapter(parent, error=RuntimeError("parent persist failed"))
            child_host = ScopeHostAdapter(child, parent=parent_host)

            with pytest.raises(RuntimeError, match="parent persist failed"):
                child_host.engine.emit(IncrementEffect(binding_name="workspace", delta=5))

            assert child.get_context("workspace").value == 5
            assert parent.get_context("workspace").value == 5
            assert len(child.effects) == 1
            assert len(parent.effects) == 1
            assert [layer.effect.delta for layer in child_host.persisted_layers] == [5]
            assert parent_host.persisted_layers == []

    def test_child_emission_updates_inherited_parent_binding_exactly_once(self) -> None:
        with Scope(root=True) as parent:
            _bind_counter(parent, "workspace")
            child = parent.child()

            parent_host = ScopeHostAdapter(parent)
            child_host = ScopeHostAdapter(child, parent=parent_host)

            child_host.engine.emit(IncrementEffect(binding_name="workspace", delta=3))
            child_host.engine.emit(IncrementEffect(binding_name="workspace", delta=4))

            assert parent.get_context("workspace").seen_deltas == (3, 4)
            assert child.get_context("workspace").seen_deltas == (3, 4)
            assert parent.get_context("workspace").value == 7
            assert child.get_context("workspace").value == 7
            assert len(parent.effects) == 2
