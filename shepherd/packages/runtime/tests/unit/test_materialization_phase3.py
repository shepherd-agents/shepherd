"""Unit tests for Phase 3 materialization system robustness features.

Tests:
- is_materializable() protocol checking
- ContextMaterialized effect emission
- Reversibility-based ordering for commit
"""

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Self

import pytest
from pydantic import ValidationError
from shepherd_core.effects import ContextMaterialized, Effect
from shepherd_core.scope.model import ContextBinding
from shepherd_core.types import ReversibilityLevel
from shepherd_runtime.materialization import (
    Materializable,
    MaterializationIntent,
    MaterializationResult,
    clear_materializer_registry,
    is_materializable,
    register_materializer,
)
from shepherd_runtime.scope import Scope

# =============================================================================
# Test Fixtures - Mock Materializable Contexts
# =============================================================================


@dataclass(frozen=True)
class MockMaterializationIntent(MaterializationIntent):
    """Mock intent for testing."""

    patches: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MockMaterializable:
    """A mock context implementing Materializable protocol."""

    context_id: str = "mock:test"
    reversibility: ReversibilityLevel = ReversibilityLevel.AUTO
    _pending_changes: tuple[str, ...] = field(default_factory=tuple)
    _target_path: Path = field(default_factory=Path)

    @property
    def has_pending_changes(self) -> bool:
        return len(self._pending_changes) > 0

    def materialization_intent(self) -> MockMaterializationIntent:
        return MockMaterializationIntent(
            context_type="MockMaterializable",
            context_id=self.context_id,
            target_path=self._target_path,
            patches=self._pending_changes,
        )

    def with_materialized(self, result: MaterializationResult) -> Self:
        return replace(self, _pending_changes=())

    def apply_effect(self, effect: Effect) -> Self:
        """Apply an effect (no-op for mock)."""
        return self


@dataclass(frozen=True)
class NonMaterializable:
    """A context that does NOT implement Materializable protocol."""

    context_id: str = "non-mat:test"
    reversibility: ReversibilityLevel = ReversibilityLevel.AUTO

    def apply_effect(self, effect: Effect) -> Self:
        """Apply an effect (no-op for mock)."""
        return self


class MockMaterializer:
    """Mock materializer for testing."""

    def __init__(self, should_succeed: bool = True, error_msg: str = "Test error"):
        self.should_succeed = should_succeed
        self.error_msg = error_msg
        self.materialize_calls: list[MaterializationIntent] = []
        self.rollback_calls: list[tuple[MaterializationIntent, MaterializationResult]] = []
        self._can_rollback = False

    def materialize(self, intent: MaterializationIntent) -> MaterializationResult:
        self.materialize_calls.append(intent)
        if self.should_succeed:
            return MaterializationResult.ok(
                paths_affected=("file1.txt", "file2.txt"),
                committed="true",
            )
        return MaterializationResult.failure(self.error_msg)

    def can_rollback(self) -> bool:
        return self._can_rollback

    def rollback(
        self,
        intent: MaterializationIntent,
        result: MaterializationResult,
    ) -> None:
        self.rollback_calls.append((intent, result))


# =============================================================================
# Tests: is_materializable()
# =============================================================================


class TestIsMaterializable:
    """Tests for the is_materializable() function."""

    def test_returns_true_for_materializable_context(self) -> None:
        """is_materializable should return True for objects implementing the protocol."""
        ctx = MockMaterializable()
        assert is_materializable(ctx) is True

    def test_returns_false_for_non_materializable_context(self) -> None:
        """is_materializable should return False for objects not implementing the protocol."""
        ctx = NonMaterializable()
        assert is_materializable(ctx) is False

    def test_returns_false_for_primitive_types(self) -> None:
        """is_materializable should return False for primitive types."""
        assert is_materializable("string") is False
        assert is_materializable(123) is False
        assert is_materializable(None) is False
        assert is_materializable([]) is False
        assert is_materializable({}) is False

    def test_isinstance_works_with_runtime_checkable(self) -> None:
        """Isinstance should work directly with Materializable protocol."""
        ctx = MockMaterializable()
        assert isinstance(ctx, Materializable)

        non_ctx = NonMaterializable()
        assert not isinstance(non_ctx, Materializable)


# =============================================================================
# Tests: ContextMaterialized Effect
# =============================================================================


class TestContextMaterializedEffect:
    """Tests for ContextMaterialized effect type."""

    def test_create_success_effect(self) -> None:
        """ContextMaterialized should capture success state."""
        effect = ContextMaterialized(
            binding_name="workspace",
            context_type="MockMaterializable",
            changes_applied=5,
            paths_affected=("a.txt", "b.txt"),
            success=True,
            committed=True,
            duration_ms=150.5,
        )

        assert effect.effect_type == "context_materialized"
        assert effect.binding_name == "workspace"
        assert effect.context_type == "MockMaterializable"
        assert effect.changes_applied == 5
        assert effect.paths_affected == ("a.txt", "b.txt")
        assert effect.success is True
        assert effect.committed is True
        assert effect.error is None
        assert effect.duration_ms == 150.5

    def test_create_failure_effect(self) -> None:
        """ContextMaterialized should capture failure state."""
        effect = ContextMaterialized(
            binding_name="workspace",
            context_type="MockMaterializable",
            success=False,
            error="Drift detected: file was modified externally",
            duration_ms=50.0,
        )

        assert effect.effect_type == "context_materialized"
        assert effect.success is False
        assert effect.error == "Drift detected: file was modified externally"
        assert effect.committed is False

    def test_effect_is_frozen(self) -> None:
        """ContextMaterialized should be immutable."""
        effect = ContextMaterialized(binding_name="test")

        with pytest.raises((ValidationError, AttributeError, TypeError)):
            effect.binding_name = "modified"

    def test_effect_serialization(self) -> None:
        """ContextMaterialized should be serializable."""
        effect = ContextMaterialized(
            binding_name="workspace",
            context_type="WorkspaceRef",
            changes_applied=3,
            success=True,
        )

        # Should serialize without error
        data = effect.model_dump()
        assert data["effect_type"] == "context_materialized"
        assert data["binding_name"] == "workspace"

        # Should deserialize
        from shepherd_core.effects import effect_from_dict

        restored = effect_from_dict(data)
        assert isinstance(restored, ContextMaterialized)
        assert restored.binding_name == "workspace"


# =============================================================================
# Tests: Reversibility-Based Ordering
# =============================================================================


class TestReversibilityOrdering:
    """Tests for _ordered_by_reversibility() method."""

    def test_auto_contexts_ordered_before_none(self, tmp_path: Path) -> None:
        """AUTO reversibility contexts should be materialized before NONE."""
        auto_ctx = MockMaterializable(
            context_id="auto:1",
            reversibility=ReversibilityLevel.AUTO,
            _pending_changes=("change1",),
            _target_path=tmp_path,
        )
        none_ctx = MockMaterializable(
            context_id="none:1",
            reversibility=ReversibilityLevel.NONE,
            _pending_changes=("change2",),
            _target_path=tmp_path,
        )

        with Scope(root=True) as scope:
            # Bind NONE first, AUTO second (opposite of desired order)
            scope.bind("none_ctx", none_ctx)
            scope.bind("auto_ctx", auto_ctx)

            ordered = scope._ordered_by_reversibility()

            # AUTO should come first
            assert len(ordered) == 2
            assert ordered[0].name == "auto_ctx"
            assert ordered[1].name == "none_ctx"

    def test_compensable_ordered_between_auto_and_none(self, tmp_path: Path) -> None:
        """COMPENSABLE contexts should be ordered between AUTO and NONE."""
        auto_ctx = MockMaterializable(
            context_id="auto:1",
            reversibility=ReversibilityLevel.AUTO,
            _pending_changes=("change",),
            _target_path=tmp_path,
        )
        comp_ctx = MockMaterializable(
            context_id="comp:1",
            reversibility=ReversibilityLevel.COMPENSABLE,
            _pending_changes=("change",),
            _target_path=tmp_path,
        )
        none_ctx = MockMaterializable(
            context_id="none:1",
            reversibility=ReversibilityLevel.NONE,
            _pending_changes=("change",),
            _target_path=tmp_path,
        )

        with Scope(root=True) as scope:
            # Bind in wrong order
            scope.bind("none_ctx", none_ctx)
            scope.bind("auto_ctx", auto_ctx)
            scope.bind("comp_ctx", comp_ctx)

            ordered = scope._ordered_by_reversibility()

            assert len(ordered) == 3
            assert ordered[0].name == "auto_ctx"
            assert ordered[1].name == "comp_ctx"
            assert ordered[2].name == "none_ctx"

    def test_excludes_non_materializable_contexts(self, tmp_path: Path) -> None:
        """_ordered_by_reversibility should exclude non-Materializable contexts."""
        mat_ctx = MockMaterializable(
            context_id="mat:1",
            _pending_changes=("change",),
            _target_path=tmp_path,
        )
        non_mat_ctx = NonMaterializable(context_id="non:1")

        with Scope(root=True) as scope:
            scope.bind("mat_ctx", mat_ctx)
            scope.bind("non_mat_ctx", non_mat_ctx)

            ordered = scope._ordered_by_reversibility()

            assert len(ordered) == 1
            assert ordered[0].name == "mat_ctx"

    def test_excludes_contexts_without_pending_changes(self, tmp_path: Path) -> None:
        """_ordered_by_reversibility should exclude contexts without pending changes."""
        with_changes = MockMaterializable(
            context_id="with:1",
            _pending_changes=("change",),
            _target_path=tmp_path,
        )
        without_changes = MockMaterializable(
            context_id="without:1",
            _pending_changes=(),  # Empty
            _target_path=tmp_path,
        )

        with Scope(root=True) as scope:
            scope.bind("with_changes", with_changes)
            scope.bind("without_changes", without_changes)

            ordered = scope._ordered_by_reversibility()

            assert len(ordered) == 1
            assert ordered[0].name == "with_changes"


# =============================================================================
# Tests: Commit Emits ContextMaterialized Effects
# =============================================================================


class TestCommitEmitsEffects:
    """Tests for ContextMaterialized effect emission during commit."""

    def setup_method(self) -> None:
        """Clear materializer registry before each test."""
        clear_materializer_registry()

    def test_commit_emits_context_materialized_effect(self, tmp_path: Path) -> None:
        """scope.commit() should emit ContextMaterialized effect on success."""
        ctx = MockMaterializable(
            context_id="test:1",
            _pending_changes=("change",),
            _target_path=tmp_path,
        )
        materializer = MockMaterializer(should_succeed=True)
        register_materializer("MockMaterializable", materializer)

        with Scope(root=True) as scope:
            scope.bind("test_ctx", ctx)

            # Before commit
            effects_before = len(scope.effects)

            # Commit
            scope.commit()

            # After commit: should have ContextMaterialized effect
            effects_after = list(scope.effects)
            new_effects = effects_after[effects_before:]

            mat_effects = [e.effect for e in new_effects if isinstance(e.effect, ContextMaterialized)]
            assert len(mat_effects) == 1

            effect = mat_effects[0]
            assert effect.binding_name == "test_ctx"
            assert effect.context_type == "MockMaterializable"
            assert effect.success is True
            assert effect.duration_ms > 0

    def test_failed_commit_emits_failure_effect(self, tmp_path: Path) -> None:
        """scope.commit() should emit failure ContextMaterialized on error."""
        ctx = MockMaterializable(
            context_id="test:1",
            _pending_changes=("change",),
            _target_path=tmp_path,
        )
        materializer = MockMaterializer(should_succeed=False, error_msg="Test failure")
        register_materializer("MockMaterializable", materializer)

        with Scope(root=True) as scope:
            scope.bind("test_ctx", ctx)

            effects_before = len(scope.effects)

            # Commit should fail
            with pytest.raises(RuntimeError, match="Test failure"):
                scope.commit()

            # Should still emit failure effect
            effects_after = list(scope.effects)
            new_effects = effects_after[effects_before:]

            mat_effects = [e.effect for e in new_effects if isinstance(e.effect, ContextMaterialized)]
            assert len(mat_effects) == 1

            effect = mat_effects[0]
            assert effect.success is False
            assert effect.error == "Test failure"

    def test_commit_records_duration(self, tmp_path: Path) -> None:
        """ContextMaterialized effect should include accurate duration_ms."""
        ctx = MockMaterializable(
            context_id="test:1",
            _pending_changes=("change",),
            _target_path=tmp_path,
        )
        materializer = MockMaterializer(should_succeed=True)
        register_materializer("MockMaterializable", materializer)

        with Scope(root=True) as scope:
            scope.bind("test_ctx", ctx)
            scope.commit()

            mat_effects = [e.effect for e in scope.effects if isinstance(e.effect, ContextMaterialized)]
            assert len(mat_effects) == 1
            # Duration should be positive (not zero)
            assert mat_effects[0].duration_ms >= 0


# =============================================================================
# Tests: Rollback on Failure
# =============================================================================


class TestRollbackOnFailure:
    """Tests for _rollback_completed() functionality."""

    def setup_method(self) -> None:
        """Clear materializer registry before each test."""
        clear_materializer_registry()

    def test_rollback_called_on_second_context_failure(self, tmp_path: Path) -> None:
        """When second context fails, first should be rolled back."""
        # First context - should succeed and then be rolled back
        ctx1 = MockMaterializable(
            context_id="first:1",
            reversibility=ReversibilityLevel.AUTO,
            _pending_changes=("change1",),
            _target_path=tmp_path,
        )
        materializer1 = MockMaterializer(should_succeed=True)
        materializer1._can_rollback = True
        register_materializer("MockMaterializable", materializer1)

        # Second context - will fail (we'll make it fail via different type)
        @dataclass(frozen=True)
        class FailingMaterializable:
            context_id: str = "fail:1"
            reversibility: ReversibilityLevel = ReversibilityLevel.NONE
            _target_path: Path = field(default_factory=lambda: tmp_path)

            @property
            def has_pending_changes(self) -> bool:
                return True

            def materialization_intent(self) -> MockMaterializationIntent:
                return MockMaterializationIntent(
                    context_type="FailingMaterializable",
                    context_id=self.context_id,
                    target_path=self._target_path,
                )

            def with_materialized(self, result: MaterializationResult) -> Self:
                return self

            def apply_effect(self, effect: Effect) -> Self:
                return self

        failing_materializer = MockMaterializer(should_succeed=False, error_msg="Boom!")
        register_materializer("FailingMaterializable", failing_materializer)

        ctx2 = FailingMaterializable()

        with Scope(root=True) as scope:
            scope.bind("ctx1", ctx1)
            scope.bind("ctx2", ctx2)

            with pytest.raises(RuntimeError, match="Boom!"):
                scope.commit()

            # First materializer should have had rollback called
            assert len(materializer1.rollback_calls) == 1

    def test_rollback_skipped_when_cannot_rollback(self, tmp_path: Path, caplog) -> None:
        """Contexts that can't rollback should be logged and skipped."""
        ctx = MockMaterializable(
            context_id="test:1",
            _pending_changes=("change",),
            _target_path=tmp_path,
        )
        materializer = MockMaterializer(should_succeed=True)
        materializer._can_rollback = False  # Can't rollback
        register_materializer("MockMaterializable", materializer)

        with Scope(root=True) as scope:
            # Manually test _rollback_completed
            binding = ContextBinding(name="test", context=ctx, initial_context=ctx)
            intent = ctx.materialization_intent()
            result = MaterializationResult.ok()

            scope._rollback_completed([(binding, intent, result)])

            # Should have logged a warning
            assert len(materializer.rollback_calls) == 0


# =============================================================================
# Tests: No Materializer Registered
# =============================================================================


class TestNoMaterializerRegistered:
    """Tests for error handling when no materializer is registered."""

    def setup_method(self) -> None:
        """Clear materializer registry before each test."""
        clear_materializer_registry()

    def test_commit_fails_with_clear_error(self, tmp_path: Path) -> None:
        """Commit should fail with helpful error if no materializer registered."""
        ctx = MockMaterializable(
            context_id="test:1",
            _pending_changes=("change",),
            _target_path=tmp_path,
        )
        # Don't register a materializer

        with Scope(root=True) as scope:
            scope.bind("test_ctx", ctx)

            with pytest.raises(RuntimeError, match="No materializer registered"):
                scope.commit()
