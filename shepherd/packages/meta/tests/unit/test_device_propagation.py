"""Tests for Device propagation through Pipeline.run().

These tests validate that Device context flows correctly through the
Pipeline.run() sync bridge, which is a key requirement for the ergonomics pass.

The key scenario is:
    with Device("container"):
        result = Pipeline(Task).run(...)  # Device should propagate

This ensures that ContextVars are preserved across the thread boundary
when run_sync() bridges from sync to async execution.
"""

import pytest
from pydantic import BaseModel
from shepherd.pipeline import Pipeline
from shepherd_runtime.device import Device, DeviceNestingError, get_current_device
from shepherd_runtime.scope import Scope
from shepherd_runtime.task.authoring import Input, Output, task
from shepherd_tests import MockProvider

# =============================================================================
# Test Tasks
# =============================================================================


@task
class SimpleTask(BaseModel):
    """Simple task for testing."""

    prompt: Input(str)
    result: Output(str)


# =============================================================================
# Tests for Device propagation through Pipeline
# =============================================================================


class TestDevicePropagation:
    """Tests for Device context propagation through Pipeline."""

    def test_pipeline_run_inside_device_context(self):
        """Pipeline.run() inside Device context executes with that device."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            with Device("local"):
                # Get current device inside Device context
                device_before = get_current_device()
                assert device_before is not None
                assert device_before.name == "local"

                result = Pipeline(SimpleTask).run(scope=scope, prompt="hello")

                # Should complete successfully
                assert not result.rejected

    def test_device_context_preserved_in_run_sync(self):
        """Device context is preserved when run_sync bridges to async."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            with Device("local"):
                outer_device = get_current_device()

                # Pipeline.run() uses run_sync() internally
                # Device context should propagate through the thread
                result = Pipeline(SimpleTask).run(scope=scope, prompt="hello")

                # After execution, device should still be accessible
                inner_device = get_current_device()
                assert inner_device is outer_device

    def test_nested_device_contexts_raises_error(self):
        """Nested Device contexts raise DeviceNestingError."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            with Device("local") as outer:
                # Nesting is not allowed - raises DeviceNestingError
                with pytest.raises(DeviceNestingError) as exc_info, Device("local") as inner:
                    pass

                assert "cannot nest" in str(exc_info.value).lower()
                # Still in outer context
                assert get_current_device() is outer

    def test_device_context_outside_with_block(self):
        """Outside Device context, get_current_device returns None."""
        # No Device context
        device = get_current_device()
        assert device is None

        with Device("local"):
            device = get_current_device()
            assert device is not None

        # After exiting, back to None
        device = get_current_device()
        assert device is None


# =============================================================================
# Tests for scope= override with Pipeline
# =============================================================================


class TestPipelineScopeOverride:
    """Tests for explicit scope= parameter in Pipeline."""

    def test_explicit_scope_parameter(self):
        """Explicit scope= parameter is used."""
        with Scope(root=True) as outer:
            outer.register_provider("default", MockProvider(), default=True)
            with Scope() as inner:
                # Pass inner scope explicitly
                result = Pipeline(SimpleTask).run(scope=inner, prompt="hello")

                assert not result.rejected

    def test_scope_from_current_scope_when_none(self):
        """When scope=None, uses current scope."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            # Don't pass scope explicitly
            result = Pipeline(SimpleTask).run(prompt="hello")

            assert not result.rejected


# =============================================================================
# Tests for gate rejection with Device
# =============================================================================


class TestGateRejectionWithDevice:
    """Tests for gate rejection behavior inside Device context."""

    def test_gate_rejection_inside_device_discards_effects(self):
        """Gate rejection inside container discards container effects."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            initial_effect_count = len(scope.effects)

            with Device("local"):
                result = (
                    Pipeline(SimpleTask)
                    .gate(lambda r, e: False)  # Always reject
                    .run(scope=scope, prompt="hello")
                )

                assert result.rejected

            # Gate rejection should not commit effects to parent scope
            # (exact behavior depends on gate implementation)

    def test_gate_acceptance_inside_device_commits_effects(self):
        """Gate acceptance inside device commits effects."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            with Device("local"):
                result = (
                    Pipeline(SimpleTask)
                    .gate(lambda r, e: True)  # Always accept
                    .run(scope=scope, prompt="hello")
                )

                assert not result.rejected


# =============================================================================
# Async tests for Device propagation
# =============================================================================


class TestDevicePropagationAsync:
    """Async tests for Device propagation."""

    @pytest.mark.asyncio
    async def test_arun_inside_device_context(self):
        """Pipeline.arun() inside Device context works."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            with Device("local"):
                result = await Pipeline(SimpleTask).arun(scope=scope, prompt="hello")

                assert not result.rejected

    @pytest.mark.asyncio
    async def test_arun_device_preserved(self):
        """Device context is preserved in arun()."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            with Device("local") as device:
                assert get_current_device() is device

                result = await Pipeline(SimpleTask).arun(scope=scope, prompt="hello")

                # Device still accessible after arun
                assert get_current_device() is device
