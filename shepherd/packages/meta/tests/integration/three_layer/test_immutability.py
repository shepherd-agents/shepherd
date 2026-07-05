"""Tests for v2 architecture immutability and effect-driven state derivation.

Tests the core invariant: state(t) = fold(apply_effect, effects[0:t], initial_state)
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from typing import Self

import pytest
from shepherd_core.effects import Effect, TaskStarted
from shepherd_core.scope import ContextBinding, ImmutableScope
from shepherd_runtime.scope import Scope

from .conftest import MockContext

# =============================================================================
# Immutability Tests
# =============================================================================


class TestScopeImmutability:
    """Test that ImmutableScope is truly immutable."""

    async def test_immutable_scope_is_frozen(self) -> None:
        """Test that ImmutableScope cannot be mutated directly."""
        scope = ImmutableScope()

        # Attempting to mutate should raise FrozenInstanceError
        with pytest.raises(FrozenInstanceError):
            scope._id = "new_id"

        with pytest.raises(FrozenInstanceError):
            scope._bindings = ()

        with pytest.raises(FrozenInstanceError):
            scope._stream = scope._stream

    async def test_context_binding_is_frozen(self) -> None:
        """Test that ContextBinding cannot be mutated directly."""
        ctx = MockContext("test")
        binding = ContextBinding(name="test", context=ctx)

        # Attempting to mutate should raise FrozenInstanceError
        with pytest.raises(FrozenInstanceError):
            binding.name = "new_name"

        with pytest.raises(FrozenInstanceError):
            binding.context = ctx

    async def test_immutable_scope_with_binding_returns_new_instance(self) -> None:
        """Test that with_binding returns a NEW ImmutableScope."""
        ctx = MockContext("test")
        scope1 = ImmutableScope()
        scope2 = scope1.with_binding("test", ctx)

        # Should be different instances
        assert scope1 is not scope2

        # Original should be unchanged
        assert len(scope1._bindings) == 0

        # New should have the binding
        assert len(scope2._bindings) == 1
        assert scope2._bindings[0].name == "test"

    async def test_immutable_scope_with_effect_returns_new_instance(self) -> None:
        """Test that with_effect returns a NEW ImmutableScope."""
        effect = TaskStarted(task_name="test")
        scope1 = ImmutableScope()
        scope2 = scope1.with_effect(effect)

        # Should be different instances
        assert scope1 is not scope2

        # Original should be unchanged
        assert len(scope1._stream) == 0

        # New should have the effect
        assert len(scope2._stream) == 1

    async def test_scope_snapshot_returns_immutable_state(self) -> None:
        """Test that snapshot() returns the immutable scope."""
        with Scope(root=True) as scope:
            scope.bind("test", MockContext("test"))
            scope.register_provider("default", MockContext("provider"), default=True)

            # Get snapshot
            snapshot = scope.snapshot()

            # Should be an ImmutableScope
            assert isinstance(snapshot, ImmutableScope)

            # Snapshot should have current state
            assert len(snapshot._bindings) == 1
            assert snapshot._bindings[0].name == "test"

    async def test_scope_snapshot_is_frozen_in_time(self) -> None:
        """Test that snapshot doesn't change when scope is updated."""
        with Scope(root=True) as scope:
            scope.bind("test1", MockContext("test1"))

            # Take snapshot
            snapshot1 = scope.snapshot()
            assert len(snapshot1._bindings) == 1

            # Update scope
            scope.bind("test2", MockContext("test2"))

            # Snapshot should NOT have changed
            assert len(snapshot1._bindings) == 1

            # But current scope state should have
            snapshot2 = scope.snapshot()
            assert len(snapshot2._bindings) == 2

    async def test_binding_with_context_returns_new_instance(self) -> None:
        """Test that ContextBinding.with_context returns new binding."""
        ctx1 = MockContext("original")
        ctx2 = MockContext("updated")

        binding1 = ContextBinding(name="test", context=ctx1)
        binding2 = binding1.with_context(ctx2)

        # Should be different instances
        assert binding1 is not binding2

        # Original should be unchanged
        assert binding1.context.name == "original"

        # New should have updated context
        assert binding2.context.name == "updated"
        assert binding2.name == "test"  # Name preserved


# =============================================================================
# Effect-Driven State Derivation
# =============================================================================


class TestEffectDrivenStateDerivation:
    """Test that effects drive state derivation correctly."""

    async def test_emit_applies_effect_to_contexts(self) -> None:
        """Test that emit() calls apply_effect on contexts."""

        # Create a context that tracks apply_effect calls
        @dataclass
        class StatefulContext(MockContext):
            value: int = 0

            def apply_effect(self, effect: Effect) -> Self:
                # Increment value for any effect
                return StatefulContext(
                    name=self.name,
                    value=self.value + 1,
                    _prepared=self._prepared,
                    _captured=self._captured,
                    _cleaned_up=self._cleaned_up,
                )

        with Scope(root=True) as scope:
            ctx = StatefulContext("counter")
            scope.bind("counter", ctx)

            # Initial value
            assert scope.get_context("counter").value == 0

            # Emit effect with matching context_id
            effect = TaskStarted(task_name="test")
            # Add context_id to effect (normally done by lifecycle)
            effect = effect.with_context(ctx.context_id)
            scope.emit(effect)

            # Context should have been updated via apply_effect
            updated_ctx = scope.get_context("counter")
            assert updated_ctx.value == 1

    async def test_immutable_scope_apply_effect_returns_new_scope(self) -> None:
        """Test that apply_effect returns new scope with derived state."""

        @dataclass
        class CountingContext(MockContext):
            count: int = 0

            def apply_effect(self, effect: Effect) -> Self:
                return CountingContext(
                    name=self.name,
                    count=self.count + 1,
                    _prepared=self._prepared,
                    _captured=self._captured,
                    _cleaned_up=self._cleaned_up,
                )

        ctx = CountingContext("test")
        binding = ContextBinding(name="test", context=ctx)
        scope1 = ImmutableScope(_bindings=(binding,))

        # Apply effect
        effect = TaskStarted(task_name="test")
        effect = effect.with_context(ctx.context_id)
        scope2 = scope1.apply_effect(effect)

        # Original unchanged
        assert scope1._bindings[0].context.count == 0

        # New scope has derived state
        assert scope2._bindings[0].context.count == 1

    async def test_core_invariant_holds(self) -> None:
        """Test the core invariant: state(t) = fold(apply_effect, effects[0:t], initial).

        This is the fundamental property of the v2 architecture.
        """

        @dataclass
        class AccumulatingContext(MockContext):
            total: int = 0

            def apply_effect(self, effect: Effect) -> Self:
                # Each effect adds its sequence number (if available)
                increment = getattr(effect, "sequence", 1)
                return AccumulatingContext(
                    name=self.name,
                    total=self.total + increment,
                    _prepared=self._prepared,
                    _captured=self._captured,
                    _cleaned_up=self._cleaned_up,
                )

        ctx = AccumulatingContext("acc")
        binding = ContextBinding(name="acc", context=ctx)
        initial_scope = ImmutableScope(_bindings=(binding,))

        # Create a sequence of effects
        effects = [TaskStarted(task_name=f"task{i}").with_context(ctx.context_id) for i in range(5)]

        # Apply effects one by one
        scope = initial_scope
        for effect in effects:
            scope = scope.with_effect(effect).apply_effect(effect)

        # Final state should be the fold of all effects
        final_ctx = scope._bindings[0].context
        assert final_ctx.total == 5  # 5 effects, each adding 1

        # We can also "replay" to any intermediate state
        scope_t2 = initial_scope
        for effect in effects[:2]:
            scope_t2 = scope_t2.with_effect(effect).apply_effect(effect)

        ctx_t2 = scope_t2._bindings[0].context
        assert ctx_t2.total == 2  # Only 2 effects applied
