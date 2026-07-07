"""Smoke tests to verify the v2 package structure and imports work.

These tests verify that:
1. Core modules can be imported
2. Public API exports are accessible
3. Basic types can be instantiated
"""


class TestPackageImports:
    """Test that all public API imports work."""

    def test_import_main_package(self):
        """Main package should be importable."""
        import shepherd

        assert hasattr(shepherd, "__version__")
        assert shepherd.__version__ == "0.3.0"

    def test_top_level_exposes_public_surface(self):
        """The main package exposes the full public `sp.*` surface (WS-A)."""
        import shepherd

        # nucleus spine + handle surface, both reachable through `sp`
        for name in (
            "workspace",
            "task",
            "handle",
            "GitRepo",
            "May",
            "RunOutput",
            "ShepherdWorkspace",
            "open",
        ):
            assert name in shepherd.__all__
            assert getattr(shepherd, name) is not None

    def test_import_layer1_scope(self):
        """Layer 1 (Scope) should be importable from owner paths."""
        from shepherd_core.scope import ContextBinding
        from shepherd_runtime.scope import Scope

        assert Scope is not None
        assert ContextBinding is not None

    def test_import_layer2_lifecycle(self):
        """Layer 2 (ExecutionLifecycle) should be importable from owner paths."""
        from shepherd_core.provider import Provider
        from shepherd_runtime.lifecycle import ExecutionLifecycle, execute

        assert ExecutionLifecycle is not None
        assert Provider is not None
        assert execute is not None

    def test_import_layer3_providers(self):
        """Layer 3 (Providers) should be importable from owner paths."""
        from shepherd_providers import ClaudeProvider, OpenAIProvider

        assert ClaudeProvider is not None
        assert OpenAIProvider is not None

    def test_import_effects(self):
        """Effect types should be importable from owner paths."""
        from shepherd_core.effects import TaskCompleted, TaskStarted

        assert TaskStarted is not None
        assert TaskCompleted is not None

    def test_import_providers_subpackage(self):
        """Providers subpackage should be importable."""
        from shepherd_providers import ClaudeProvider, OpenAIProvider

        assert ClaudeProvider is not None
        assert OpenAIProvider is not None

    def test_import_contexts_subpackage(self):
        """Contexts subpackage should be importable."""
        from shepherd_contexts import (
            SessionState,
            WorkspaceRef,
        )

        assert WorkspaceRef is not None
        assert SessionState is not None


class TestBasicTypeInstantiation:
    """Test that basic types can be instantiated."""

    def test_create_stream(self):
        """Stream should be instantiable."""
        from shepherd_core.scope import Stream

        stream = Stream()
        assert len(stream) == 0

    def test_create_provider_capabilities(self):
        """ProviderCapabilities should be instantiable."""
        from shepherd_core.types import ProviderCapabilities

        caps = ProviderCapabilities(
            provider_type="test",
            supports_streaming=True,
            supports_tools=True,
        )
        assert caps.provider_type == "test"
        assert caps.supports_streaming is True

    def test_create_tool_call(self):
        """ToolCall should be instantiable."""
        from shepherd_core.types import ToolCall

        call = ToolCall(id="tc_1", name="Read", params={"path": "/test.py"})
        assert call.id == "tc_1"
        assert call.name == "Read"

    def test_create_validation_result(self):
        """ValidationResult should be instantiable."""
        from shepherd_core.types import ToolCall, ValidationResult

        call = ToolCall(id="tc_1", name="Read", params={})

        # Test allow
        result = ValidationResult.allow(call)
        assert result.allowed is True

        # Test reject
        result = ValidationResult.reject(call, "Not allowed")
        assert result.allowed is False
        assert result.rejection_reason == "Not allowed"

    def test_create_execution_result(self):
        """ExecutionResult should be instantiable."""
        from shepherd_core.types import ExecutionResult

        result = ExecutionResult(
            success=True,
            output_text="Hello, world!",
            tool_calls=(),
            tool_results=(),
        )
        assert result.success is True
        assert result.output_text == "Hello, world!"

    def test_create_claude_provider(self):
        """ClaudeProvider should be instantiable."""
        from shepherd_providers import ClaudeProvider

        provider = ClaudeProvider(name="test")
        assert provider.name == "test"
        assert "claude" in provider.provider_id

    def test_create_openai_provider(self):
        """OpenAIProvider should be instantiable."""
        from shepherd_providers import OpenAIProvider

        provider = OpenAIProvider(name="test")
        assert provider.name == "test"
        assert "openai" in provider.provider_id


class TestReversibilityLevels:
    """Test reversibility level semantics."""

    def test_reversibility_enum_values(self):
        """Reversibility levels should have expected values."""
        from shepherd_core.types import ReversibilityLevel

        assert ReversibilityLevel.AUTO is not None
        assert ReversibilityLevel.COMPENSABLE is not None
        assert ReversibilityLevel.NONE is not None

    def test_reversibility_comparison(self):
        """Reversibility levels should be comparable."""
        from shepherd_core.types import ReversibilityLevel

        # These should be different enum values
        assert ReversibilityLevel.AUTO != ReversibilityLevel.COMPENSABLE
        assert ReversibilityLevel.COMPENSABLE != ReversibilityLevel.NONE


class TestEffectStream:
    """Test effect stream operations."""

    def test_stream_append(self):
        """Stream should support append (returning new stream)."""
        from shepherd_core.effects import TaskStarted
        from shepherd_core.scope import Stream

        stream = Stream()
        effect = TaskStarted(task_name="test_task")

        new_stream = stream.append(effect)

        # Original unchanged (immutable)
        assert len(stream) == 0
        # New stream has effect
        assert len(new_stream) == 1

    def test_stream_query(self):
        """Stream should support querying by effect type."""
        from shepherd_core.effects import TaskCompleted, TaskStarted
        from shepherd_core.scope import Stream

        stream = Stream()
        stream = stream.append(TaskStarted(task_name="task_1"))
        stream = stream.append(TaskCompleted(task_name="task_1", duration_ms=100.0))
        stream = stream.append(TaskStarted(task_name="task_2"))

        # Query by type
        started = list(stream.query(TaskStarted))
        assert len(started) == 2

        completed = list(stream.query(TaskCompleted))
        assert len(completed) == 1
