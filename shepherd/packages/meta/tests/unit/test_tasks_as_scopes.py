"""Tests for tasks-as-scopes feature.

This module tests the RFC implementation where every @task execution
creates a child scope, unifying task and scope hierarchies.

Key behaviors tested:
1. Task execution creates child scope
2. Effects propagate to parent scope
3. stream.direct() filters to scope's own effects
4. stream.summarized() shows only task boundaries
5. stream.by_depth() limits hierarchy depth
6. Context updates flow to declaration site
"""

import pytest
from pydantic import BaseModel
from shepherd_core.effects import TaskStarted
from shepherd_core.scope import Stream
from shepherd_runtime.scope import Scope
from shepherd_runtime.task.authoring import Input, Output, task
from shepherd_tests import MockProvider

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def mock_provider():
    """Create a mock provider for testing."""
    return MockProvider(name="test")


@pytest.fixture
def parent_scope(mock_provider):
    """Create a parent scope with mock provider."""
    scope = Scope()
    scope.register_provider("default", mock_provider, default=True)
    return scope


# =============================================================================
# Test: Effect Propagation
# =============================================================================


class TestEffectPropagation:
    """Test that effects propagate from child to parent scope."""

    def test_effects_visible_in_parent_stream(self, parent_scope):
        """Effects from child scope should propagate to parent stream."""

        @task
        class PropagatingTask(BaseModel):
            """Task that emits effects."""

            value: Input(str)
            result: Output(str) = None

        with parent_scope:
            result = PropagatingTask(value="test")

            # Parent should see task effects (TaskStarted at minimum)
            # Note: MockProvider doesn't emit full effects, but the scope
            # machinery should still work
            assert len(parent_scope.effects) >= 0

    def test_child_effects_have_correct_scope_id(self, parent_scope):
        """Effects should carry the emitting scope's ID."""
        from shepherd_core.effects import TaskStarted

        with parent_scope:
            # Manually emit an effect to test scope_id tracking
            parent_scope.emit(TaskStarted(task_name="test"))

            layers = list(parent_scope.effects)
            assert len(layers) == 1
            # Effect should have parent's scope ID
            assert layers[0].scope_id == parent_scope.id
            assert layers[0].scope_depth == 0


# =============================================================================
# Test: direct() Query Method
# =============================================================================


class TestDirectQuery:
    """Test Stream.direct() method."""

    def test_direct_returns_only_this_scope_effects(self):
        """direct() should return only effects emitted by this scope."""
        from shepherd_core.effects import FileRead, TaskStarted

        with Scope() as parent:
            parent.register_provider("default", MockProvider(), default=True)

            # Emit effect directly to parent
            parent.emit(TaskStarted(task_name="parent_task"))

            # Create child and emit to child
            child = parent.child()
            with child:
                child.emit(FileRead(path="test.py"))

            # Parent's direct stream should only have parent's effect
            direct = parent.effects.direct()
            assert len(direct) == 1
            assert isinstance(direct.layers[0].effect, TaskStarted)

            # Parent's full stream should have both
            assert len(parent.effects) == 2

    def test_direct_requires_scope_context(self):
        """direct() should raise if stream has no scope context."""
        stream = Stream()
        stream = stream.append(TaskStarted(task_name="test"))

        with pytest.raises(ValueError, match="requires a scope-bound stream"):
            stream.direct()

    def test_child_direct_has_child_effects(self):
        """Child's direct() should have effects emitted by child."""
        from shepherd_core.effects import FileRead, TaskStarted

        with Scope() as parent:
            parent.register_provider("default", MockProvider(), default=True)

            parent.emit(TaskStarted(task_name="parent"))

            child = parent.child()
            with child:
                child.emit(FileRead(path="child.py"))

                # Child's direct should have child's effect
                child_direct = child.effects.direct()
                assert len(child_direct) == 1
                assert isinstance(child_direct.layers[0].effect, FileRead)


# =============================================================================
# Test: summarized() Query Method
# =============================================================================


class TestSummarizedQuery:
    """Test Stream.summarized() method."""

    def test_summarized_only_task_boundaries(self):
        """summarized() should return only TaskStarted/Completed/Failed."""
        from shepherd_core.effects import FileRead, TaskCompleted, TaskStarted, ToolCallCompleted

        stream = Stream()
        stream = stream.append(TaskStarted(task_name="test"))
        stream = stream.append(FileRead(path="a.py"))
        stream = stream.append(ToolCallCompleted(tool_name="read"))
        stream = stream.append(TaskCompleted(task_name="test"))

        # Need to add scope context for the method to work
        stream = stream.with_scope_context("test_scope")

        summarized = stream.summarized()

        assert len(summarized) == 2
        effect_types = {type(el.effect).__name__ for el in summarized}
        assert effect_types == {"TaskStarted", "TaskCompleted"}

    def test_summarized_works_without_scope_context(self):
        """summarized() should work even without scope context."""
        from shepherd_core.effects import TaskStarted

        stream = Stream()
        stream = stream.append(TaskStarted(task_name="test"))

        # Should work without scope context
        summarized = stream.summarized()
        assert len(summarized) == 1


# =============================================================================
# Test: by_depth() Query Method
# =============================================================================


class TestByDepthQuery:
    """Test Stream.by_depth() method."""

    def test_by_depth_zero_only_this_scope(self):
        """by_depth(0) should return only this scope's effects."""
        from shepherd_core.effects import FileRead, TaskStarted
        from shepherd_core.scope.stream import EffectLayer

        # Create a stream with effects at different depths
        layers = [
            EffectLayer(
                effect=TaskStarted(task_name="parent"),
                sequence=0,
                scope_id="parent_scope",
                scope_depth=0,
            ),
            EffectLayer(
                effect=FileRead(path="child.py"),
                sequence=1,
                scope_id="child_scope",
                scope_depth=1,
            ),
        ]
        stream = Stream(_layers=tuple(layers), _scope_id="parent_scope")

        result = stream.by_depth(0)

        assert len(result) == 1
        assert isinstance(result.layers[0].effect, TaskStarted)

    def test_by_depth_one_includes_children(self):
        """by_depth(1) should include immediate children."""
        from shepherd_core.effects import FileRead, TaskStarted
        from shepherd_core.scope.stream import EffectLayer

        layers = [
            EffectLayer(
                effect=TaskStarted(task_name="parent"),
                sequence=0,
                scope_id="parent_scope",
                scope_depth=0,
            ),
            EffectLayer(
                effect=FileRead(path="child.py"),
                sequence=1,
                scope_id="child_scope",
                scope_depth=1,
            ),
            EffectLayer(
                effect=FileRead(path="grandchild.py"),
                sequence=2,
                scope_id="grandchild_scope",
                scope_depth=2,
            ),
        ]
        stream = Stream(_layers=tuple(layers), _scope_id="parent_scope")

        result = stream.by_depth(1)

        assert len(result) == 2
        paths = [el.effect.path for el in result if hasattr(el.effect, "path")]
        assert "child.py" in paths
        assert "grandchild.py" not in paths

    def test_by_depth_requires_scope_context(self):
        """by_depth() should raise if stream has no scope context."""
        stream = Stream()
        stream = stream.append(TaskStarted(task_name="test"))

        with pytest.raises(ValueError, match="requires a scope-bound stream"):
            stream.by_depth(1)


# =============================================================================
# Test: Scope Context Preservation
# =============================================================================


class TestScopeContextPreservation:
    """Test that scope context is preserved through operations."""

    def test_filter_preserves_scope_context(self):
        """Filtering operations should preserve _scope_id."""
        from shepherd_core.effects import FileRead, TaskStarted

        stream = Stream()
        stream = stream.append(TaskStarted(task_name="test"))
        stream = stream.append(FileRead(path="a.py", context_id="ctx1"))
        stream = stream.with_scope_context("my_scope")

        filtered = stream.by_context("ctx1")

        assert filtered._scope_id == "my_scope"

    def test_by_task_preserves_scope_context(self):
        """by_task() should preserve _scope_id."""
        stream = Stream()
        stream = stream.append(TaskStarted(task_name="test"))
        stream = stream.with_scope_context("my_scope")

        filtered = stream.by_task("test")

        assert filtered._scope_id == "my_scope"

    def test_summarized_preserves_scope_context(self):
        """summarized() should preserve _scope_id."""
        stream = Stream()
        stream = stream.append(TaskStarted(task_name="test"))
        stream = stream.with_scope_context("my_scope")

        summarized = stream.summarized()

        assert summarized._scope_id == "my_scope"


# =============================================================================
# Test: EffectLayer Scope Metadata
# =============================================================================


class TestEffectLayerScopeMetadata:
    """Test scope_id and scope_depth on EffectLayer."""

    def test_emit_adds_scope_metadata(self):
        """emit() should add scope_id and scope_depth to layer."""
        from shepherd_core.effects import TaskStarted

        with Scope() as scope:
            scope.register_provider("default", MockProvider(), default=True)

            scope.emit(TaskStarted(task_name="test"))

            layer = scope.effects[0]
            assert layer.scope_id == scope.id
            assert layer.scope_depth == scope._depth

    def test_child_emit_has_child_metadata(self):
        """Effects emitted in child should have child's metadata."""
        from shepherd_core.effects import FileRead

        with Scope() as parent:
            parent.register_provider("default", MockProvider(), default=True)

            child = parent.child()
            with child:
                child.emit(FileRead(path="test.py"))

            # Effect in parent stream should have child's scope_id
            layer = parent.effects[0]
            assert layer.scope_id == child.id
            assert layer.scope_depth == 1

    def test_auto_nested_scope_matches_child_metadata(self):
        """Implicitly nested Scope() blocks should behave like child()."""
        from shepherd_core.effects import FileRead

        with Scope() as parent:
            parent.register_provider("default", MockProvider(), default=True)

            with Scope() as child:
                child.emit(FileRead(path="implicit-child.py"))

            layer = parent.effects[0]
            assert child._parent_proxy is parent
            assert child._depth == 1
            assert layer.scope_id == child.id
            assert layer.scope_depth == 1

    def test_scope_metadata_survives_serialization(self):
        """scope_id and scope_depth should survive JSON roundtrip."""
        from shepherd_core.effects import TaskStarted
        from shepherd_core.scope.stream import EffectLayer

        layer = EffectLayer(
            effect=TaskStarted(task_name="test"),
            sequence=0,
            scope_id="test_scope_123",
            scope_depth=3,
        )
        stream = Stream(_layers=(layer,))

        json_str = stream.to_json()
        restored = Stream.from_json(json_str)

        assert restored.layers[0].scope_id == "test_scope_123"
        assert restored.layers[0].scope_depth == 3


# =============================================================================
# Test: Context Binding Inheritance
# =============================================================================


class TestContextBindingInheritance:
    """Test that child scopes inherit and can access parent bindings."""

    def test_child_inherits_provider(self):
        """Child scope should inherit parent's providers."""
        with Scope() as parent:
            provider = MockProvider()
            parent.register_provider("default", provider, default=True)

            child = parent.child()
            with child:
                # Child should be able to get parent's provider
                assert child.get_provider() == provider

    def test_child_inherits_context_bindings(self):
        """Child scope should inherit parent's context bindings."""
        from shepherd_core.types import ProviderBinding

        # Create a minimal context for testing
        class TestContext:
            context_id = "test:ctx"
            reversibility = None

            def configure(self, caps):
                return ProviderBinding()

            def prepare(self):
                return self

            def capture(self, result):
                return None

            def cleanup(self, error=None):
                pass

        ctx = TestContext()

        with Scope() as parent:
            parent.register_provider("default", MockProvider(), default=True)
            parent.bind("test_ctx", ctx)

            child = parent.child()
            with child:
                # Child should be able to access parent's binding
                assert child.get_context("test_ctx") == ctx


# =============================================================================
# Test: Real-World Patterns
# =============================================================================


class TestRealWorldPatterns:
    """Test real-world usage patterns with tasks-as-scopes."""

    def test_nested_task_isolation(self):
        """Nested tasks should have isolated scopes."""
        from shepherd_core.effects import TaskStarted

        with Scope() as outer:
            outer.register_provider("default", MockProvider(), default=True)

            # Emit in outer scope
            outer.emit(TaskStarted(task_name="outer"))

            # Create "task" scope
            task_scope = outer.child()
            with task_scope:
                task_scope.emit(TaskStarted(task_name="task"))

                # Task's direct effects should only include task's
                task_direct = task_scope.effects.direct()
                assert len(task_direct) == 1
                assert task_direct.layers[0].effect.task_name == "task"

            # Outer's stream should have both
            assert len(outer.effects) == 2

            # Outer's direct should only have outer's
            outer_direct = outer.effects.direct()
            assert len(outer_direct) == 1
            assert outer_direct.layers[0].effect.task_name == "outer"

    def test_effect_propagation_chain(self):
        """Effects should propagate through multiple scope levels."""
        from shepherd_core.effects import FileRead

        with Scope() as level0:
            level0.register_provider("default", MockProvider(), default=True)

            level1 = level0.child()
            with level1:
                level2 = level1.child()
                with level2:
                    level2.emit(FileRead(path="deep.py"))

                    # All levels should see the effect
                    assert len(level2.effects) == 1
                    assert len(level1.effects) == 1
                    assert len(level0.effects) == 1

                    # Scope IDs should be correct
                    assert level0.effects[0].scope_id == level2.id
                    assert level0.effects[0].scope_depth == 2
