"""Property-based tests for Scope fork/merge/discard invariants.

Tests core invariants of the Scope containment system:
1. Fork independence: Parent effects unchanged by child operations
2. Merge completeness: All child effects appear in parent after merge
3. Discard containment: Discarded scope's effects never leak
4. Mutual exclusivity: Either merge XOR discard, never both
5. Binding snapshot: Fork captures bindings at fork time
"""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from shepherd_core.context.kernel import ExecutionContext
from shepherd_core.effects import (
    Effect,
    TaskCompleted,
    TaskStarted,
    ToolCallStarted,
)
from shepherd_core.foundation.errors import ScopeError
from shepherd_runtime.scope import Scope

# =============================================================================
# Test Context
# =============================================================================


class PropertyTestContext(ExecutionContext):
    """Simple context for property testing."""

    def __init__(self, name: str = "test"):
        self._name = name
        self._effect_count = 0

    @property
    def context_id(self) -> str:
        return f"property_test:{self._name}"

    def apply_effect(self, effect: Effect) -> "PropertyTestContext":
        """Return new context with incremented effect count."""
        new_ctx = PropertyTestContext(self._name)
        new_ctx._effect_count = self._effect_count + 1
        return new_ctx

    @property
    def effect_count(self) -> int:
        return self._effect_count


# =============================================================================
# Hypothesis Strategies
# =============================================================================


@st.composite
def effect_strategy(draw: st.DrawFn) -> Effect:
    """Generate a random effect."""
    effect_type = draw(st.sampled_from(["task_started", "task_completed", "tool_call_started"]))
    task_name = draw(st.text(min_size=1, max_size=15, alphabet=st.characters(whitelist_categories=("L", "N"))))

    if effect_type == "task_started":
        return TaskStarted(task_name=task_name, inputs={})
    if effect_type == "task_completed":
        return TaskCompleted(task_name=task_name, outputs={}, duration_ms=0.0)
    return ToolCallStarted(
        task_name=task_name,
        tool_call_id=draw(st.text(min_size=1, max_size=10)),
        tool_name=draw(st.text(min_size=1, max_size=10)),
        params={},
    )


@st.composite
def effect_list_strategy(draw: st.DrawFn, min_size: int = 1, max_size: int = 10) -> list[Effect]:
    """Generate a list of effects."""
    return draw(st.lists(effect_strategy(), min_size=min_size, max_size=max_size))


# =============================================================================
# Property Tests: Fork Independence
# =============================================================================


class TestForkIndependence:
    """Tests for fork independence invariant.

    Parent's effects should be unchanged by any operations on forked child.
    """

    @given(parent_effects=effect_list_strategy(min_size=1, max_size=5))
    @settings(max_examples=50)
    def test_fork_does_not_share_stream(self, parent_effects: list[Effect]) -> None:
        """Forked child has independent stream from parent."""
        with Scope() as parent:
            # Add effects to parent
            for effect in parent_effects:
                parent.emit(effect)

            parent_len_before = len(parent.effects)

            # Fork
            child = parent.fork()

            # Parent unchanged after fork
            assert len(parent.effects) == parent_len_before

            # Child starts with empty stream
            assert len(child.effects) == 0

    @given(
        parent_effects=effect_list_strategy(min_size=1, max_size=3),
        child_effects=effect_list_strategy(min_size=1, max_size=5),
    )
    @settings(max_examples=50)
    def test_child_effects_do_not_affect_parent(
        self, parent_effects: list[Effect], child_effects: list[Effect]
    ) -> None:
        """Effects emitted to child do not appear in parent (until merge)."""
        with Scope() as parent:
            for effect in parent_effects:
                parent.emit(effect)

            parent_len_before = len(parent.effects)
            parent_effects_copy = [layer.effect for layer in parent.effects]

            child = parent.fork()

            # Add effects to child
            for effect in child_effects:
                child.emit(effect)

            # Parent unchanged
            assert len(parent.effects) == parent_len_before
            for i, layer in enumerate(parent.effects):
                assert layer.effect == parent_effects_copy[i]

    @given(effects=effect_list_strategy(min_size=1, max_size=5))
    @settings(max_examples=30)
    def test_multiple_forks_are_independent(self, effects: list[Effect]) -> None:
        """Multiple forks from same parent are independent of each other."""
        with Scope() as parent:
            fork1 = parent.fork()
            fork2 = parent.fork()

            # Add different effects to each fork
            for i, effect in enumerate(effects):
                if i % 2 == 0:
                    fork1.emit(effect)
                else:
                    fork2.emit(effect)

            # Forks should have independent effect counts
            fork1_count = len(fork1.effects)
            fork2_count = len(fork2.effects)

            # Total should be sum, not shared
            assert fork1_count + fork2_count == len(effects)

            # Parent should be empty (no automatic propagation)
            assert len(parent.effects) == 0


# =============================================================================
# Property Tests: Merge Completeness
# =============================================================================


class TestMergeCompleteness:
    """Tests for merge completeness invariant.

    All child effects should appear in parent after merge, in order.
    """

    @given(child_effects=effect_list_strategy(min_size=1, max_size=10))
    @settings(max_examples=50)
    def test_merge_transfers_all_effects(self, child_effects: list[Effect]) -> None:
        """All child effects appear in parent after merge."""
        with Scope() as parent:
            child = parent.fork()

            for effect in child_effects:
                child.emit(effect)

            parent.merge(child)

            # All child effects should be in parent
            assert len(parent.effects) == len(child_effects)

    @given(child_effects=effect_list_strategy(min_size=2, max_size=10))
    @settings(max_examples=50)
    def test_merge_preserves_order(self, child_effects: list[Effect]) -> None:
        """Effects appear in parent in same order as emitted to child."""
        with Scope() as parent:
            child = parent.fork()

            for effect in child_effects:
                child.emit(effect)

            parent.merge(child)

            # Order should be preserved
            for i, effect in enumerate(child_effects):
                assert parent.effects[i].effect.effect_type == effect.effect_type
                assert parent.effects[i].effect.task_name == effect.task_name

    @given(
        parent_effects=effect_list_strategy(min_size=1, max_size=3),
        child_effects=effect_list_strategy(min_size=1, max_size=5),
    )
    @settings(max_examples=50)
    def test_merge_appends_to_existing(self, parent_effects: list[Effect], child_effects: list[Effect]) -> None:
        """Merged effects are appended to parent's existing effects."""
        with Scope() as parent:
            for effect in parent_effects:
                parent.emit(effect)

            child = parent.fork()
            for effect in child_effects:
                child.emit(effect)

            parent.merge(child)

            # Parent should have both sets
            assert len(parent.effects) == len(parent_effects) + len(child_effects)

            # Original effects still at front
            for i, effect in enumerate(parent_effects):
                assert parent.effects[i].effect.effect_type == effect.effect_type

            # Child effects at end
            for i, effect in enumerate(child_effects):
                assert parent.effects[len(parent_effects) + i].effect.effect_type == effect.effect_type


# =============================================================================
# Property Tests: Discard Containment
# =============================================================================


class TestDiscardContainment:
    """Tests for discard containment invariant.

    Discarded scope's effects should never leak to parent.
    """

    @given(child_effects=effect_list_strategy(min_size=1, max_size=10))
    @settings(max_examples=50)
    def test_discard_prevents_leak(self, child_effects: list[Effect]) -> None:
        """Discarded effects never appear in parent."""
        with Scope() as parent:
            child = parent.fork()

            for effect in child_effects:
                child.emit(effect)

            child.discard()

            # Parent should have no effects
            assert len(parent.effects) == 0

    @given(
        parent_effects=effect_list_strategy(min_size=1, max_size=3),
        child_effects=effect_list_strategy(min_size=1, max_size=5),
    )
    @settings(max_examples=50)
    def test_discard_preserves_parent_state(self, parent_effects: list[Effect], child_effects: list[Effect]) -> None:
        """Parent state unchanged after child discard."""
        with Scope() as parent:
            for effect in parent_effects:
                parent.emit(effect)

            parent_len_before = len(parent.effects)

            child = parent.fork()
            for effect in child_effects:
                child.emit(effect)

            child.discard()

            # Parent unchanged
            assert len(parent.effects) == parent_len_before

    @given(effects=effect_list_strategy(min_size=1, max_size=5))
    @settings(max_examples=30)
    def test_discard_is_idempotent(self, effects: list[Effect]) -> None:
        """Multiple discards are safe (idempotent)."""
        with Scope() as parent:
            child = parent.fork()

            for effect in effects:
                child.emit(effect)

            # Multiple discards should not raise
            child.discard()
            child.discard()
            child.discard()

            assert child.is_discarded


# =============================================================================
# Property Tests: Mutual Exclusivity
# =============================================================================


class TestMutualExclusivity:
    """Tests for merge/discard mutual exclusivity.

    Either merge XOR discard, never both.
    """

    @given(effects=effect_list_strategy(min_size=1, max_size=5))
    @settings(max_examples=50)
    def test_cannot_merge_after_discard(self, effects: list[Effect]) -> None:
        """Cannot merge a discarded child."""
        with Scope() as parent:
            child = parent.fork()

            for effect in effects:
                child.emit(effect)

            child.discard()

            with pytest.raises(ScopeError, match="discarded"):
                parent.merge(child)

    @given(effects=effect_list_strategy(min_size=1, max_size=5))
    @settings(max_examples=30)
    def test_discard_after_merge_is_no_op(self, effects: list[Effect]) -> None:
        """Discard after merge doesn't undo the merge."""
        with Scope() as parent:
            child = parent.fork()

            for effect in effects:
                child.emit(effect)

            parent.merge(child)
            parent_len_after_merge = len(parent.effects)

            # Discard after merge - effects already in parent
            child.discard()

            # Parent still has effects (merge already happened)
            assert len(parent.effects) == parent_len_after_merge


# =============================================================================
# Property Tests: Binding Snapshot
# =============================================================================


class TestBindingSnapshot:
    """Tests for binding snapshot on fork.

    Fork captures bindings at fork time; later parent bindings don't affect child.
    """

    def test_fork_copies_existing_bindings(self) -> None:
        """Forked child has access to parent's bindings at fork time."""
        with Scope() as parent:
            ctx = PropertyTestContext("shared")
            parent.bind("ctx", ctx)

            child = parent.fork()

            # Child should have the binding
            child_binding = child.get_context("ctx")
            assert child_binding is not None
            assert child_binding.context_id == "property_test:shared"

    def test_parent_binding_after_fork_not_in_child(self) -> None:
        """Bindings added to parent after fork are not in child."""
        with Scope() as parent:
            child = parent.fork()

            # Add binding to parent AFTER fork
            ctx = PropertyTestContext("after_fork")
            parent.bind("late_ctx", ctx)

            # Child should NOT have this binding
            with pytest.raises(KeyError):
                child.get_context("late_ctx")

    def test_child_binding_not_in_parent(self) -> None:
        """Bindings added to child are not in parent."""
        with Scope() as parent:
            child = parent.fork()

            ctx = PropertyTestContext("child_only")
            child.bind("child_ctx", ctx)

            # Parent should NOT have this binding
            with pytest.raises(KeyError):
                parent.get_context("child_ctx")


# =============================================================================
# Property Tests: Stream State Consistency
# =============================================================================


class TestStreamStateConsistency:
    """Tests for stream state consistency through fork/merge/discard."""

    @given(effects=effect_list_strategy(min_size=1, max_size=10))
    @settings(max_examples=30)
    def test_effect_count_consistency(self, effects: list[Effect]) -> None:
        """Stream length equals number of effects emitted."""
        with Scope() as scope:
            for effect in effects:
                scope.emit(effect)

            assert len(scope.effects) == len(effects)

    @given(
        batch1=effect_list_strategy(min_size=1, max_size=5),
        batch2=effect_list_strategy(min_size=1, max_size=5),
    )
    @settings(max_examples=30)
    def test_sequential_merges_accumulate(self, batch1: list[Effect], batch2: list[Effect]) -> None:
        """Sequential merges accumulate effects correctly."""
        with Scope() as parent:
            # First fork/merge
            child1 = parent.fork()
            for effect in batch1:
                child1.emit(effect)
            parent.merge(child1)

            # Second fork/merge
            child2 = parent.fork()
            for effect in batch2:
                child2.emit(effect)
            parent.merge(child2)

            # Parent should have all effects
            assert len(parent.effects) == len(batch1) + len(batch2)

    @given(
        keep=effect_list_strategy(min_size=1, max_size=3),
        discard=effect_list_strategy(min_size=1, max_size=3),
    )
    @settings(max_examples=30)
    def test_selective_merge_and_discard(self, keep: list[Effect], discard: list[Effect]) -> None:
        """Can selectively merge some forks and discard others."""
        with Scope() as parent:
            # Fork to keep
            keep_child = parent.fork()
            for effect in keep:
                keep_child.emit(effect)

            # Fork to discard
            discard_child = parent.fork()
            for effect in discard:
                discard_child.emit(effect)

            # Discard one, merge other
            discard_child.discard()
            parent.merge(keep_child)

            # Only kept effects should be in parent
            assert len(parent.effects) == len(keep)
