"""Tests for scope binding inheritance semantics.

Key behaviors:
- Inherited binding: changes persist to parent scope after child exits
- Shadowed binding: changes are isolated to child scope, lost on exit
"""

from __future__ import annotations

import pytest
from shepherd_runtime.scope import Scope

from .conftest import MockContext, MockProvider


@pytest.mark.integration
class TestScopeBindingInheritance:
    """Test that context bindings follow lexical scoping semantics."""

    async def test_inherited_binding_updates_parent(self) -> None:
        """Test that inherited bindings update the parent scope.

        When a child scope inherits a binding (doesn't shadow it), changes
        made during execution should persist in the parent after the child exits.
        """
        provider = MockProvider(name="test")
        original_ctx = MockContext(name="inherited")

        with Scope() as parent:
            parent.register_provider("default", provider, default=True)
            parent.bind("ctx", original_ctx)

            # Verify initial state
            assert not parent.get_context("ctx")._captured

            with parent.child() as child:
                # Child inherits provider from parent
                # Child does NOT bind "ctx" - it inherits from parent

                # Execute in child scope - should update parent's binding
                result, outputs = await child.execute("Test inherited binding")
                assert result.success

                # The updated context should be in outputs
                updated_ctx = outputs.get("ctx")
                assert updated_ctx is not None
                assert updated_ctx._captured

            # After child exits, parent's binding should be updated
            parent_ctx = parent.get_context("ctx")
            assert parent_ctx._captured, "Parent binding should have captured state after child execution"

    async def test_shadowed_binding_isolated_to_child(self) -> None:
        """Test that shadowed bindings are isolated to the child scope.

        When a child scope shadows a binding (binds the same name), changes
        should be isolated to the child and NOT affect the parent.
        """
        provider = MockProvider(name="test")
        parent_ctx = MockContext(name="parent-ctx")
        child_ctx = MockContext(name="child-ctx")

        with Scope() as parent:
            parent.register_provider("default", provider, default=True)
            parent.bind("ctx", parent_ctx)

            # Verify initial state
            assert not parent.get_context("ctx")._captured

            with parent.child() as child:
                # Child SHADOWS the binding with its own context
                child.bind("ctx", child_ctx)

                # Execute in child scope - should only update child's binding
                result, outputs = await child.execute("Test shadowed binding")
                assert result.success

                # Child's context should be captured
                child_updated = outputs.get("ctx")
                assert child_updated is not None
                assert child_updated._captured
                assert child_updated.name == "child-ctx"

            # After child exits, parent's binding should be UNCHANGED
            parent_ctx_after = parent.get_context("ctx")
            assert not parent_ctx_after._captured, "Parent binding should NOT be affected by child's shadow"
            assert parent_ctx_after.name == "parent-ctx"

    async def test_effects_propagate_regardless_of_binding_scope(self) -> None:
        """Test that effects always propagate to parent, even with shadowed bindings.

        Even when a binding is shadowed, the effects from execution should
        still propagate to the parent's stream for observability.
        """
        provider = MockProvider(name="test")
        parent_ctx = MockContext(name="parent")
        child_ctx = MockContext(name="child")

        with Scope() as parent:
            parent.register_provider("default", provider, default=True)
            parent.bind("ctx", parent_ctx)

            parent_effects_before = len(parent.effects)

            with parent.child() as child:
                child.bind("ctx", child_ctx)  # Shadow the binding

                await child.execute("Task in child with shadow")
                child_effects = len(child.effects)

            # Parent should have received ALL effects from child
            parent_effects_after = len(parent.effects)
            assert parent_effects_after > parent_effects_before
            # Parent's stream should have at least as many effects as child's
            assert parent_effects_after >= parent_effects_before + child_effects

    async def test_multiple_inherited_bindings_update_correctly(self) -> None:
        """Test that multiple inherited bindings all update in parent."""
        provider = MockProvider(name="test")
        ctx1 = MockContext(name="ctx1")
        ctx2 = MockContext(name="ctx2")

        with Scope() as parent:
            parent.register_provider("default", provider, default=True)
            parent.bind("first", ctx1)
            parent.bind("second", ctx2)

            with parent.child() as child:
                # Child inherits both bindings (no shadows)
                result, _outputs = await child.execute("Multi-context inherited")
                assert result.success

            # Both parent bindings should be updated
            assert parent.get_context("first")._captured
            assert parent.get_context("second")._captured

    async def test_mixed_inherited_and_shadowed(self) -> None:
        """Test mixed scenario: one inherited, one shadowed."""
        provider = MockProvider(name="test")
        parent_ctx1 = MockContext(name="inherited")
        parent_ctx2 = MockContext(name="parent-shadowed")
        child_ctx2 = MockContext(name="child-shadow")

        with Scope() as parent:
            parent.register_provider("default", provider, default=True)
            parent.bind("inherited", parent_ctx1)
            parent.bind("shadowed", parent_ctx2)

            with parent.child() as child:
                # Shadow only one binding
                child.bind("shadowed", child_ctx2)

                result, _outputs = await child.execute("Mixed bindings test")
                assert result.success

            # Inherited binding should be updated in parent
            assert parent.get_context("inherited")._captured

            # Shadowed binding should NOT be updated in parent
            assert not parent.get_context("shadowed")._captured
            assert parent.get_context("shadowed").name == "parent-shadowed"
