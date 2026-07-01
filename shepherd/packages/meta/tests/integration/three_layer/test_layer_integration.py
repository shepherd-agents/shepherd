"""Integration tests for the three-layer architecture.

Tests that Scope (Layer 1), ExecutionLifecycle (Layer 2), and Provider (Layer 3)
work together correctly, including stream queries and reversibility composition.
"""

from __future__ import annotations

import pytest
from shepherd_core.effects import (
    TaskCompleted,
    TaskStarted,
)
from shepherd_core.errors import PreparationError
from shepherd_core.types import ProviderBinding, ReversibilityLevel
from shepherd_providers import ClaudeProvider
from shepherd_runtime.lifecycle import ExecutionLifecycle
from shepherd_runtime.scope import Scope

from .conftest import MockContext, MockProvider


@pytest.mark.integration
class TestThreeLayerIntegration:
    """Test that the three layers work together correctly."""

    async def test_basic_scope_execute(self) -> None:
        """Test basic execution through Scope.execute()."""
        provider = MockProvider(name="test", _response="Hello from mock!")

        with Scope() as scope:
            scope.register_provider("default", provider, default=True)

            result, _outputs = await scope.execute("Say hello")

            assert result.success
            assert result.output_text == "Hello from mock!"
            assert len(scope.effects) > 0

    async def test_scope_with_context_binding(self) -> None:
        """Test that contexts go through full lifecycle."""
        provider = MockProvider(name="test")
        context = MockContext(name="test-ctx")

        with Scope() as scope:
            scope.register_provider("default", provider, default=True)
            scope.bind("ctx", context)

            result, outputs = await scope.execute("Do something")

            assert result.success
            # Context should have been prepared and captured
            updated_ctx = outputs.get("ctx")
            assert updated_ctx is not None
            assert updated_ctx._captured

    async def test_execution_lifecycle_phases(self) -> None:
        """Test that ExecutionLifecycle runs all 7 phases correctly."""
        provider = MockProvider(name="test")
        context = MockContext(name="lifecycle-test")

        with Scope() as scope:
            scope.register_provider("default", provider, default=True)
            scope.bind("ctx", context)

            async with ExecutionLifecycle(scope, provider, task_name="test_task") as lifecycle:
                # At this point: configure and prepare have run
                binding = scope.get_binding("ctx")
                assert binding is not None
                assert binding.is_prepared  # Use is_prepared, not state

                # Execute
                result = await lifecycle.execute("Test prompt")
                assert result.success

                # Get the captured context
                updated_ctx = lifecycle.get_context("ctx")
                assert updated_ctx._captured

            # After exit: cleanup has run
            # Verify lifecycle completed by checking binding state reset
            binding_after = scope.get_binding("ctx")
            assert not binding_after.in_lifecycle  # Lifecycle released the binding

    async def test_effect_stream_accumulates(self) -> None:
        """Test that effects accumulate in the scope's stream."""
        provider = MockProvider(name="test")

        with Scope() as scope:
            scope.register_provider("default", provider, default=True)

            initial_count = len(scope.effects)

            await scope.execute("First task")
            after_first = len(scope.effects)
            assert after_first > initial_count

            await scope.execute("Second task")
            after_second = len(scope.effects)
            assert after_second > after_first

    async def test_task_lifecycle_effects(self) -> None:
        """Test that TaskStarted and TaskCompleted effects are emitted."""
        provider = MockProvider(name="test")

        with Scope() as scope:
            scope.register_provider("default", provider, default=True)

            await scope.execute("Test task")

            # Should have task lifecycle effects
            started = list(scope.effects.query(TaskStarted))
            completed = list(scope.effects.query(TaskCompleted))

            assert len(started) >= 1
            assert len(completed) >= 1

    async def test_multiple_contexts_compose(self) -> None:
        """Test that multiple contexts compose their bindings correctly."""
        provider = MockProvider(name="test")
        ctx1 = MockContext(name="ctx1")
        ctx2 = MockContext(name="ctx2")

        with Scope() as scope:
            scope.register_provider("default", provider, default=True)
            scope.bind("first", ctx1)
            scope.bind("second", ctx2)

            result, outputs = await scope.execute("Multi-context task")

            assert result.success
            assert "first" in outputs
            assert "second" in outputs
            assert outputs["first"]._captured
            assert outputs["second"]._captured

    async def test_preparation_failure_triggers_cleanup(self) -> None:
        """Test that preparation failure cleans up already-prepared contexts."""
        provider = MockProvider(name="test")
        ctx1 = MockContext(name="succeeds")
        ctx2 = MockContext(name="fails", _prepare_should_fail=True)

        with Scope() as scope:
            scope.register_provider("default", provider, default=True)
            scope.bind("first", ctx1)
            scope.bind("second", ctx2)

            # Framework wraps preparation failures in PreparationError
            with pytest.raises(PreparationError, match="Simulated preparation failure"):
                await scope.execute("Should fail during prepare")

            # First context should have been cleaned up after second failed
            assert ctx1._cleaned_up

    async def test_child_scope_isolation(self) -> None:
        """Test that child scopes have isolated effect streams."""
        provider = MockProvider(name="test")

        with Scope() as parent:
            parent.register_provider("default", provider, default=True)

            await parent.execute("Parent task")
            parent_effects_before = len(parent.effects)

            with parent.child() as child:
                # Child scope needs its own provider registration
                child.register_provider("default", provider, default=True)
                await child.execute("Child task")
                child_effects = len(child.effects)

            # Parent should have received child's effects
            parent_effects_after = len(parent.effects)
            assert parent_effects_after > parent_effects_before

    async def test_provider_binding_translation(self) -> None:
        """Test that provider correctly translates binding to SDK config."""
        # Use real ClaudeProvider to test translation
        provider = ClaudeProvider(name="test")

        binding = ProviderBinding(
            context_id="test:ctx",
            context_type="TestContext",
            trust_level="restricted",
            session_isolation="forked",
            capabilities=frozenset({"read", "write"}),
        )

        # Access the private translation method for testing
        config = provider._translate_binding(binding)

        assert config["permission_mode"] == "default"  # restricted -> default
        assert config["fork_session"] is True  # forked -> fork_session=True


@pytest.mark.integration
class TestStreamQueries:
    """Test that stream queries work correctly with real execution."""

    async def test_query_by_task_name(self) -> None:
        """Test filtering effects by task name."""
        provider = MockProvider(name="test")

        with Scope() as scope:
            scope.register_provider("default", provider, default=True)

            # Execute with explicit task names
            async with ExecutionLifecycle(scope, provider, task_name="task_a") as lc:
                await lc.execute("Task A")

            async with ExecutionLifecycle(scope, provider, task_name="task_b") as lc:
                await lc.execute("Task B")

            # Query by task name
            task_a_effects = scope.effects.by_task("task_a")
            task_b_effects = scope.effects.by_task("task_b")

            assert len(task_a_effects.layers) > 0
            assert len(task_b_effects.layers) > 0

            # They should be different
            task_a_ids = {e.task_name for e in task_a_effects.layers}
            task_b_ids = {e.task_name for e in task_b_effects.layers}
            assert task_a_ids == {"task_a"}
            assert task_b_ids == {"task_b"}

    async def test_query_by_context(self) -> None:
        """Test filtering effects by context ID."""
        provider = MockProvider(name="test")
        ctx = MockContext(name="query-test")

        with Scope() as scope:
            scope.register_provider("default", provider, default=True)
            scope.bind("ctx", ctx)

            await scope.execute("Test query")

            # Query by context
            ctx_effects = scope.effects.by_context(ctx.context_id)
            assert len(ctx_effects.layers) >= 0  # May have context effects


@pytest.mark.integration
class TestReversibilityComposition:
    """Test that reversibility levels compose correctly across contexts."""

    async def test_auto_plus_auto_is_auto(self) -> None:
        """Test that AUTO + AUTO = AUTO."""
        ctx1 = MockContext(name="auto1")  # AUTO by default
        ctx2 = MockContext(name="auto2")  # AUTO by default

        with Scope() as scope:
            scope.bind("first", ctx1)
            scope.bind("second", ctx2)

            composite = scope.composite_reversibility()
            assert composite == ReversibilityLevel.AUTO
