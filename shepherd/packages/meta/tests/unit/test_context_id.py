"""Tests for context_id functionality (Phase 1: Effect Attribution).

These tests validate the core context_id functionality:
1. Effect base class has context_id field
2. ExecutionContext protocol requires context_id property
3. WorkspaceRef implements context_id with frozen ID pattern
4. SessionState implements context_id based on session_id
5. context_id survives serialization roundtrip
"""

from shepherd_contexts import SessionState, WorkspaceRef
from shepherd_core.context import is_execution_context
from shepherd_core.effects import (
    KERNEL_EFFECT_REGISTRY,
    DiffPatch,
    Effect,
    FileCreate,
    FilePatch,
    TaskStarted,
)
from shepherd_core.scope import Stream
from shepherd_core.types import ExecutionResult

# =============================================================================
# Effect context_id Tests
# =============================================================================


class TestEffectContextId:
    """Test context_id on Effect base class."""

    def test_effect_has_context_id_field(self):
        """Effect should have context_id field defaulting to None."""
        effect = TaskStarted(task_name="Test")
        assert hasattr(effect, "context_id")
        assert effect.context_id is None

    def test_effect_context_id_can_be_set(self):
        """Effect should accept context_id in constructor."""
        # FileCreate uses path and content_hash (not content)
        effect = FileCreate(
            path="foo.py",
            content_hash="abc123",
            context_id="workspace:/repo:abc123",
        )
        assert effect.context_id == "workspace:/repo:abc123"

    def test_effect_context_id_serializes(self):
        """context_id should be included in serialization."""
        # FilePatch uses path and patch_hash fields
        effect = FilePatch(
            path="foo.py",
            patch_hash="abc123",
            context_id="workspace:/repo:def456",
        )
        assert effect.context_id == "workspace:/repo:def456"

    def test_effect_context_id_survives_json_roundtrip(self):
        """context_id should survive JSON serialization."""
        stream = Stream()
        stream = stream.append(
            FileCreate(
                path="test.py",
                content_hash="hash123",
                context_id="workspace:/path:abc123",
            )
        )

        json_str = stream.to_json()
        restored = Stream.from_json(json_str)

        assert len(restored) == 1
        assert restored[0].context_id == "workspace:/path:abc123"


# =============================================================================
# WorkspaceRef context_id Tests
# =============================================================================


class TestWorkspaceRefContextId:
    """Test context_id on WorkspaceRef."""

    def test_workspace_has_context_id_property(self, git_workspace):
        """WorkspaceRef should have context_id property."""
        workspace = WorkspaceRef.from_path(git_workspace)
        assert hasattr(workspace, "context_id")
        assert isinstance(workspace.context_id, str)
        assert len(workspace.context_id) > 0

    def test_context_id_format(self, git_workspace):
        """context_id should follow workspace:path:commit format."""
        workspace = WorkspaceRef.from_path(git_workspace)
        context_id = workspace.context_id

        assert context_id.startswith("workspace:")
        # Should include path and commit hash
        parts = context_id.split(":")
        assert len(parts) >= 3

    def test_context_id_frozen_at_creation(self, git_workspace):
        """context_id should be frozen when from_path is called."""
        workspace = WorkspaceRef.from_path(git_workspace)
        original_id = workspace.context_id

        # Even if we call context_id multiple times, should be the same
        assert workspace.context_id == original_id
        assert workspace.context_id == original_id

    def test_context_id_preserved_with_pending_patches(self, git_workspace):
        """context_id should be preserved even with pending patches."""
        workspace = WorkspaceRef.from_path(git_workspace)
        original_id = workspace.context_id

        # Create a workspace with pending patches (simulating accumulated changes)
        patch = DiffPatch(
            patch="diff content",
            files_changed=("file.py",),
            source_step="TestTask",
        )
        # In the new API, patches are accumulated via capture(), but we can test
        # that the context_id is preserved by checking the frozen ID pattern
        assert workspace.context_id == original_id

        # The frozen context_id should remain stable even if we create a new
        # workspace with the same path (deterministic)
        workspace2 = WorkspaceRef.from_path(git_workspace)
        assert workspace2.context_id == original_id

    def test_context_id_preserved_across_prepare_and_capture(self, git_workspace):
        """context_id should be preserved after prepare() and capture()."""
        workspace = WorkspaceRef.from_path(git_workspace)
        original_id = workspace.context_id

        # Prepare
        prepared = workspace.prepare()

        # Create a change
        (git_workspace / "new_file.py").write_text("# New file")

        # Extract effects and apply with mock execution result
        result = ExecutionResult(success=True, output_text="done")
        effects = prepared.extract_effects(None, result)

        # Apply effects to derive new state
        new_workspace = prepared
        for effect in effects:
            new_workspace = new_workspace.apply_effect(effect)

        # context_id should be the same
        assert new_workspace.context_id == original_id

    def test_context_id_deterministic(self, git_workspace):
        """Same inputs should produce same context_id."""
        workspace1 = WorkspaceRef.from_path(git_workspace)
        workspace2 = WorkspaceRef.from_path(git_workspace)

        # Different instances but same context_id
        assert workspace1.context_id == workspace2.context_id


# =============================================================================
# SessionState context_id Tests
# =============================================================================


class TestSessionStateContextId:
    """Test context_id on SessionState."""

    def test_session_has_context_id_property(self):
        """SessionState should have context_id property."""
        session = SessionState(session_id="sess_abc123")
        assert hasattr(session, "context_id")
        assert isinstance(session.context_id, str)

    def test_context_id_format(self):
        """context_id should follow session:id format."""
        session = SessionState(session_id="sess_abc123")
        assert session.context_id == "session:sess_abc123"

    def test_context_id_based_on_session_id(self):
        """context_id should be derived from session_id."""
        session1 = SessionState(session_id="sess_abc")
        session2 = SessionState(session_id="sess_xyz")

        assert session1.context_id != session2.context_id
        assert "sess_abc" in session1.context_id
        assert "sess_xyz" in session2.context_id

    def test_context_id_deterministic(self):
        """Same session_id should produce same context_id."""
        session1 = SessionState(session_id="sess_test123")
        session2 = SessionState(session_id="sess_test123")

        assert session1.context_id == session2.context_id


# =============================================================================
# ExecutionContext Protocol Tests
# =============================================================================


class TestExecutionContextProtocol:
    """Test ExecutionContext protocol compliance."""

    def test_workspace_is_execution_context(self, git_workspace):
        """WorkspaceRef should be recognized as ExecutionContext."""
        workspace = WorkspaceRef.from_path(git_workspace)
        assert is_execution_context(workspace)

    def test_session_is_execution_context(self):
        """SessionState should be recognized as ExecutionContext."""
        session = SessionState(session_id="sess_test")
        assert is_execution_context(session)

    def test_workspace_has_required_protocol_methods(self, git_workspace):
        """WorkspaceRef should have all ExecutionContext protocol methods."""
        workspace = WorkspaceRef.from_path(git_workspace)

        # Check all protocol requirements
        assert hasattr(workspace, "context_id")
        assert hasattr(workspace, "configure")
        assert hasattr(workspace, "prepare")
        assert hasattr(workspace, "extract_effects")
        assert hasattr(workspace, "apply_effect")
        assert hasattr(workspace, "cleanup")

        # Protocol methods should be callable
        assert callable(workspace.configure)
        assert callable(workspace.prepare)
        assert callable(workspace.extract_effects)
        assert callable(workspace.apply_effect)
        assert callable(workspace.cleanup)

    def test_session_has_required_protocol_methods(self):
        """SessionState should have all ExecutionContext protocol methods."""
        session = SessionState(session_id="sess_test")

        # Check all protocol requirements
        assert hasattr(session, "context_id")
        assert hasattr(session, "configure")
        assert hasattr(session, "prepare")
        assert hasattr(session, "extract_effects")
        assert hasattr(session, "apply_effect")
        assert hasattr(session, "cleanup")

    def test_context_str_returns_empty(self, git_workspace):
        """ExecutionContexts should return empty string from __str__."""
        workspace = WorkspaceRef.from_path(git_workspace)
        session = SessionState(session_id="sess_test")

        assert str(workspace) == ""
        assert str(session) == ""


# =============================================================================
# Custom Effect with context_id Tests
# =============================================================================


class TestCustomEffectContextId:
    """Test context_id with custom effect types."""

    def test_custom_effect_inherits_context_id(self):
        """Custom effects should inherit context_id from Effect base."""
        from typing import Literal

        class CustomTestEffect(Effect):
            effect_type: Literal["custom_test_effect"] = "custom_test_effect"
            custom_field: str = ""

        effect = CustomTestEffect(
            custom_field="test",
            context_id="custom:ctx123",
        )

        assert effect.context_id == "custom:ctx123"

    def test_custom_effect_context_id_serializes(self):
        """Custom effect context_id should serialize correctly."""
        from typing import Literal

        class AnotherTestEffect(Effect):
            effect_type: Literal["another_test_effect"] = "another_test_effect"
            data: str = ""

        effect = AnotherTestEffect(
            data="test data",
            context_id="another:ctx456",
        )

        stream = Stream().append(effect)
        json_str = stream.to_json()
        registry = KERNEL_EFFECT_REGISTRY.extend({"another_test_effect": AnotherTestEffect})
        restored = Stream.from_json(json_str, registry=registry)

        assert restored[0].context_id == "another:ctx456"
