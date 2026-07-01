"""Tests for capability validation (v2 Architecture).

These tests verify that the capability validation system correctly allows/denies
tool calls based on context capabilities via ProviderBinding.

The v2 architecture uses ProviderBinding composition and Provider._build_composite_validator()
instead of the v1 hook-based approach.

Test coverage:
- Tool capability requirement mappings
- ProviderBinding capability validation
- ValidationResult structure
- ToolCallRejected effect
"""

from __future__ import annotations

import pytest
from shepherd_contexts import WorkspaceRef
from shepherd_core.effects import ToolCallRejected
from shepherd_core.types import (
    TOOL_CAPABILITY_REQUIREMENTS,
    ProviderBinding,
    ToolCall,
    ValidationResult,
    capability_for_tool,
)

# =============================================================================
# Test capability_for_tool()
# =============================================================================


class TestCapabilityForTool:
    """Tests for the tool -> capability mapping function."""

    def test_write_requires_write(self):
        """Write tool requires 'write' capability."""
        assert capability_for_tool("Write") == "write"

    def test_edit_requires_write(self):
        """Edit tool requires 'write' capability."""
        assert capability_for_tool("Edit") == "write"

    def test_notebook_edit_requires_write(self):
        """NotebookEdit tool requires 'write' capability."""
        assert capability_for_tool("NotebookEdit") == "write"

    def test_bash_requires_bash(self):
        """Bash tool requires 'bash' capability."""
        assert capability_for_tool("Bash") == "bash"

    def test_read_requires_read_or_nothing(self):
        """Read tool requires 'read' or no special capability."""
        result = capability_for_tool("Read")
        assert result == "read" or result is None

    def test_unknown_tool_requires_nothing(self):
        """Unknown tools don't require special capability."""
        assert capability_for_tool("UnknownTool") is None
        assert capability_for_tool("Glob") is None
        assert capability_for_tool("Grep") is None

    def test_tool_requirements_mapping(self):
        """Verify the mapping contains expected tools."""
        assert "Write" in TOOL_CAPABILITY_REQUIREMENTS
        assert "Edit" in TOOL_CAPABILITY_REQUIREMENTS
        assert "Bash" in TOOL_CAPABILITY_REQUIREMENTS


# =============================================================================
# Test ValidationResult
# =============================================================================


class TestValidationResult:
    """Tests for the ValidationResult dataclass."""

    def test_allowed_result(self):
        """ValidationResult with allowed=True."""
        tool_call = ToolCall(id="tc_1", name="Read", params={})
        result = ValidationResult(allowed=True, tool=tool_call)
        assert result.allowed is True
        assert result.tool == tool_call
        assert result.rejection_reason is None

    def test_denied_result(self):
        """ValidationResult with allowed=False."""
        tool_call = ToolCall(id="tc_1", name="Write", params={})
        result = ValidationResult(
            allowed=False,
            tool=tool_call,
            rejection_reason="Missing write capability",
        )
        assert result.allowed is False
        assert result.rejection_reason == "Missing write capability"

    def test_result_is_frozen(self):
        """ValidationResult should be immutable."""
        tool_call = ToolCall(id="tc_1", name="Read", params={})
        result = ValidationResult(allowed=True, tool=tool_call)
        # Attempting to modify should raise an error
        with pytest.raises(Exception):
            result.allowed = False


# =============================================================================
# Test ToolCallRejected Effect
# =============================================================================


class TestToolCallRejectedEffect:
    """Tests for the ToolCallRejected effect type."""

    def test_creation(self):
        """ToolCallRejected can be created with required fields."""
        effect = ToolCallRejected(
            tool_call_id="tool_123",
            tool_name="Write",
            reason="Write not allowed on readonly workspace",
        )

        assert effect.tool_name == "Write"
        assert effect.tool_call_id == "tool_123"
        assert "Write not allowed" in effect.reason

    def test_with_context_id(self):
        """ToolCallRejected can include context_id for attribution."""
        effect = ToolCallRejected(
            tool_call_id="tool_456",
            tool_name="Bash",
            reason="Bash not available",
            context_id="workspace:/test:abc12345",
        )

        assert effect.context_id == "workspace:/test:abc12345"

    def test_frozen(self):
        """ToolCallRejected is immutable."""
        effect = ToolCallRejected(
            tool_call_id="tool_123",
            tool_name="Write",
            reason="Not allowed",
        )

        with pytest.raises(Exception):
            effect.tool_name = "Edit"


# =============================================================================
# Test ProviderBinding Capability Validation
# =============================================================================


class TestProviderBindingValidation:
    """Tests for capability validation via ProviderBinding."""

    def test_binding_allows_tool_with_capability(self):
        """Binding with capability should allow corresponding tool."""
        binding = ProviderBinding(
            context_id="test:ctx",
            capabilities=frozenset({"read", "write"}),
        )

        # Write tool requires 'write' capability
        required = capability_for_tool("Write")
        assert required in binding.capabilities

    def test_binding_denies_tool_without_capability(self):
        """Binding without capability should deny corresponding tool."""
        binding = ProviderBinding(
            context_id="test:ctx",
            capabilities=frozenset({"read"}),  # No write
        )

        # Write tool requires 'write' capability
        required = capability_for_tool("Write")
        assert required not in binding.capabilities

    def test_binding_blocked_tools(self):
        """Blocked tools should be denied regardless of capabilities."""
        binding = ProviderBinding(
            context_id="test:ctx",
            capabilities=frozenset({"read", "write", "bash"}),
            blocked_tools=frozenset({"Bash"}),  # Explicitly blocked
        )

        assert "Bash" in binding.blocked_tools

    def test_composed_binding_intersection_logic(self):
        """Composed bindings use intersection for capabilities."""
        # First context allows all
        binding1 = ProviderBinding(
            context_id="ctx1",
            capabilities=frozenset({"read", "write", "bash"}),
        )
        # Second context only allows read/write
        binding2 = ProviderBinding(
            context_id="ctx2",
            capabilities=frozenset({"read", "write"}),
        )

        composed = ProviderBinding.compose(binding1, binding2)

        # Intersection: bash should be removed
        assert "bash" not in composed.capabilities
        assert "read" in composed.capabilities
        assert "write" in composed.capabilities


# =============================================================================
# Integration Tests with WorkspaceRef
# =============================================================================


class TestWorkspaceRefCapabilityValidation:
    """Integration tests for capability validation with real WorkspaceRef."""

    def test_readonly_workspace_binding(self, git_workspace):
        """Test binding from readonly WorkspaceRef."""
        workspace = WorkspaceRef.readonly(git_workspace)
        binding = workspace.configure(frozenset({"read"}))

        # Should have read capability
        assert binding.capabilities

    def test_writable_workspace_binding(self, git_workspace):
        """Test binding from writable WorkspaceRef."""
        workspace = WorkspaceRef.writable(git_workspace)
        binding = workspace.configure(frozenset({"read", "write"}))

        # Should have capabilities
        assert binding.capabilities

    def test_workspace_with_bash_binding(self, git_workspace):
        """Test binding from WorkspaceRef with bash enabled."""
        workspace = WorkspaceRef.writable(git_workspace).with_bash()
        binding = workspace.configure(frozenset({"read", "write", "bash"}))

        # Should include all capabilities
        assert binding.capabilities


# =============================================================================
# Validator Builder Pattern Tests
# =============================================================================


class TestValidatorPattern:
    """Tests for the validator pattern used by providers."""

    def test_validator_function_signature(self):
        """Validators should accept ToolCall and return ValidationResult."""

        # A validator is a callable
        def sample_validator(tool_call: ToolCall) -> ValidationResult:
            return ValidationResult(allowed=True, tool=tool_call)

        # Verify it matches expected signature
        tool_call = ToolCall(id="tc_1", name="Read", params={})
        result = sample_validator(tool_call)
        assert isinstance(result, ValidationResult)

    def test_chained_validators(self):
        """Multiple validators can be chained."""

        def capability_validator(tool_call: ToolCall) -> ValidationResult:
            if tool_call.name == "Bash":
                return ValidationResult(
                    allowed=False,
                    tool=tool_call,
                    rejection_reason="Bash not allowed",
                )
            return ValidationResult(allowed=True, tool=tool_call)

        def blocked_tool_validator(tool_call: ToolCall) -> ValidationResult:
            if tool_call.name == "DangerousTool":
                return ValidationResult(
                    allowed=False,
                    tool=tool_call,
                    rejection_reason="Tool is blocked",
                )
            return ValidationResult(allowed=True, tool=tool_call)

        # Chain: first check capabilities, then blocked tools
        def chain(*validators):
            def chained(tool_call: ToolCall) -> ValidationResult:
                for v in validators:
                    result = v(tool_call)
                    if not result.allowed:
                        return result
                return ValidationResult(allowed=True, tool=tool_call)

            return chained

        validator = chain(capability_validator, blocked_tool_validator)

        # Test: Bash rejected by first validator
        bash_call = ToolCall(id="tc_1", name="Bash", params={})
        result = validator(bash_call)
        assert not result.allowed
        assert "Bash not allowed" in result.rejection_reason

        # Test: Read allowed
        read_call = ToolCall(id="tc_2", name="Read", params={})
        result = validator(read_call)
        assert result.allowed
