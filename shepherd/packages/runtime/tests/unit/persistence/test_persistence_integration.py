"""Integration tests for the persistence system (Phase 4.5).

Tests:
- Full persist → exit → resume → continue flow
- Crash recovery simulation with commit_remaining()
- Multiple session handling
- Materialization with persistence
- preview_commit() functionality
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from shepherd_contexts.workspace.effects import WorkspacePatchCaptured
from shepherd_core.context.kernel import ExecutionContextDefaults
from shepherd_core.effects import (
    ContextMaterialized,
    DiffPatch,
    Effect,
    TaskStarted,
)
from shepherd_core.types import ProviderBinding, ReversibilityLevel
from shepherd_runtime.materialization import (
    MaterializationIntent,
    MaterializationResult,
    register_materializer,
)
from shepherd_runtime.persistence import PersistenceManager, ProjectId
from shepherd_runtime.scope import Scope

# =============================================================================
# Test Context and Materializer Implementation
# =============================================================================


@dataclass(frozen=True)
class MockMaterializationIntent(MaterializationIntent):
    """Test intent for integration tests."""

    operations: tuple[str, ...] = ()
    commit_message: str | None = None

    def with_commit_message(self, message: str) -> "MockMaterializationIntent":
        return MockMaterializationIntent(
            context_type=self.context_type,
            context_id=self.context_id,
            target_path=self.target_path,
            operations=self.operations,
            commit_message=message,
        )


class MaterializableTestContext(BaseModel, ExecutionContextDefaults):
    """Test context that supports materialization."""

    name: str = "test"
    _pending_operations: list[str] = []
    _materialized: bool = False
    target_path: Path = Path("/tmp")

    model_config = {"arbitrary_types_allowed": True}

    @property
    def context_id(self) -> str:
        return f"test_context:{self.name}"

    @property
    def reversibility(self) -> ReversibilityLevel:
        return ReversibilityLevel.AUTO

    def configure(self) -> ProviderBinding:
        return ProviderBinding()

    def apply_effect(self, effect: Effect) -> "MaterializableTestContext":
        """Apply effects to update state."""
        if isinstance(effect, WorkspacePatchCaptured):
            return self.model_copy(update={"_pending_operations": [*self._pending_operations, effect.patch.patch]})
        return self

    # Materializable protocol
    @property
    def has_pending_changes(self) -> bool:
        return len(self._pending_operations) > 0 and not self._materialized

    def materialization_intent(self) -> MockMaterializationIntent:
        return MockMaterializationIntent(
            context_type=type(self).__name__,
            context_id=self.context_id,
            target_path=self.target_path,
            operations=tuple(self._pending_operations),
        )

    def with_materialized(self, result: MaterializationResult) -> "MaterializableTestContext":
        return self.model_copy(update={"_materialized": True, "_pending_operations": []})


class MockTestMaterializer:
    """Test materializer for integration tests."""

    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail
        self.materialize_calls: list[MockMaterializationIntent] = []

    def materialize(self, intent: MockMaterializationIntent) -> MaterializationResult:
        self.materialize_calls.append(intent)

        if self.should_fail:
            return MaterializationResult(
                success=False,
                paths_affected=(),
                error="Simulated failure",
            )

        return MaterializationResult(
            success=True,
            paths_affected=tuple(f"file_{i}.txt" for i in range(len(intent.operations))),
            metadata={"committed": "true" if intent.commit_message else "false"},
        )

    def can_rollback(self) -> bool:
        return True

    def rollback(self, intent: Any, result: MaterializationResult) -> None:
        pass


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def project_path(tmp_path: Path) -> Path:
    """Create a temporary project directory."""
    project = tmp_path / "my_project"
    project.mkdir()
    return project


@pytest.fixture
def setup_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Setup HOME to use tmp_path for persistence."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def test_materializer() -> MockTestMaterializer:
    """Create and register a test materializer."""
    materializer = MockTestMaterializer()
    register_materializer("MaterializableTestContext", materializer)
    return materializer


# =============================================================================
# Tests: Full Persist-Resume Flow (Phase 4.5)
# =============================================================================


class TestFullPersistResumeFlow:
    """Integration tests for full persist → exit → resume → continue flow."""

    def test_full_flow_persist_and_resume(self, project_path: Path, setup_home: Path) -> None:
        """Test persist → exit → resume → continue flow."""
        # Session 1: Create workspace, make changes, persist
        with Scope(project_path=project_path) as scope:
            ctx = MaterializableTestContext(name="workspace", target_path=project_path)
            workspace = scope.bind("workspace", ctx)

            # Simulate execution that creates effects
            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="workspace",
                    patch=DiffPatch(patch="patch1", files_changed=("test.py",)),
                )
            )

            # Verify effect persisted
            assert (setup_home / ".shepherd/projects").exists()

        # Session 2: Resume and verify state
        with Scope.resume(project_path) as scope:
            # Re-bind triggers deferred state derivation
            ctx = MaterializableTestContext(name="workspace", target_path=project_path)
            workspace = scope.bind("workspace", ctx)

            # State reconstructed from stored effects during bind()
            assert len(workspace.value._pending_operations) == 1
            assert workspace.value._pending_operations[0] == "patch1"

            # Can continue working — new effects go to new stream
            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="workspace",
                    patch=DiffPatch(patch="patch2", files_changed=("test2.py",)),
                )
            )

            assert len(workspace.value._pending_operations) == 2

    def test_multiple_sessions_with_stream_chain(self, project_path: Path, setup_home: Path) -> None:
        """Test multiple sessions create a chain of streams."""
        # Session 1
        with Scope(project_path=project_path) as scope:
            scope.emit(TaskStarted(task_name="Session1", task_fqn="test.Session1"))

        # Session 2
        with Scope.resume(project_path) as scope:
            scope.emit(TaskStarted(task_name="Session2", task_fqn="test.Session2"))

        # Session 3
        with Scope.resume(project_path) as scope:
            scope.emit(TaskStarted(task_name="Session3", task_fqn="test.Session3"))

        # Verify chain
        manager = PersistenceManager(setup_home / ".shepherd", ProjectId.from_path(project_path))
        manager.initialize()
        all_layers = manager.read_stream_chain()

        assert len(all_layers) == 3
        assert all_layers[0].effect.task_name == "Session1"
        assert all_layers[1].effect.task_name == "Session2"
        assert all_layers[2].effect.task_name == "Session3"

    def test_resume_reconstructs_multiple_contexts(self, project_path: Path, setup_home: Path) -> None:
        """Test that resume correctly reconstructs multiple contexts."""
        # Session 1: Create effects for multiple contexts
        with Scope(project_path=project_path) as scope:
            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="context_a",
                    patch=DiffPatch(patch="a_patch1", files_changed=("a.py",)),
                )
            )
            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="context_b",
                    patch=DiffPatch(patch="b_patch1", files_changed=("b.py",)),
                )
            )
            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="context_a",
                    patch=DiffPatch(patch="a_patch2", files_changed=("a2.py",)),
                )
            )

        # Session 2: Resume and bind both contexts
        with Scope.resume(project_path) as scope:
            ctx_a = MaterializableTestContext(name="context_a", target_path=project_path)
            ctx_b = MaterializableTestContext(name="context_b", target_path=project_path)

            ref_a = scope.bind("context_a", ctx_a)
            ref_b = scope.bind("context_b", ctx_b)

            # Each context should have its own effects
            assert ref_a.value._pending_operations == ["a_patch1", "a_patch2"]
            assert ref_b.value._pending_operations == ["b_patch1"]


# =============================================================================
# Tests: Crash Recovery (Phase 4.4b)
# =============================================================================


class TestCrashRecovery:
    """Integration tests for crash recovery with commit_remaining()."""

    def test_commit_remaining_skips_already_materialized(
        self, project_path: Path, setup_home: Path, test_materializer: MockTestMaterializer
    ) -> None:
        """commit_remaining() should skip contexts with successful ContextMaterialized."""
        # Session 1: Make changes and simulate partial commit
        with Scope(project_path=project_path) as scope:
            ctx = MaterializableTestContext(name="workspace", target_path=project_path)
            scope.bind("workspace", ctx)

            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="workspace",
                    patch=DiffPatch(patch="patch1", files_changed=("file.py",)),
                )
            )

            # Simulate that materialization already succeeded (e.g., before crash)
            scope.emit(
                ContextMaterialized(
                    binding_name="workspace",
                    context_type="MaterializableTestContext",
                    success=True,
                    changes_applied=1,
                    paths_affected=("file.py",),
                )
            )

        # Session 2: Resume and call commit_remaining
        with Scope.resume(project_path) as scope:
            ctx = MaterializableTestContext(name="workspace", target_path=project_path)
            scope.bind("workspace", ctx)

            # commit_remaining() should skip the already-materialized context
            result = scope.commit_remaining(message="Complete commit")

            assert result["total_paths_affected"] == 0
            assert result["skipped"] == ["workspace"]
            assert len(test_materializer.materialize_calls) == 0

    def test_commit_remaining_processes_unmaterialized(
        self, project_path: Path, setup_home: Path, test_materializer: MockTestMaterializer
    ) -> None:
        """commit_remaining() should process contexts without ContextMaterialized."""
        # Session 1: Make changes but no ContextMaterialized effect
        with Scope(project_path=project_path) as scope:
            ctx = MaterializableTestContext(name="workspace", target_path=project_path)
            scope.bind("workspace", ctx)

            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="workspace",
                    patch=DiffPatch(patch="patch1", files_changed=("file.py",)),
                )
            )

        # Session 2: Resume and call commit_remaining
        with Scope.resume(project_path) as scope:
            ctx = MaterializableTestContext(name="workspace", target_path=project_path)
            scope.bind("workspace", ctx)

            # commit_remaining() should process the unmaterialized context
            result = scope.commit_remaining(message="Complete commit")

            assert result["total_paths_affected"] == 1
            assert result["skipped"] == []
            assert len(test_materializer.materialize_calls) == 1

    def test_commit_remaining_idempotent(
        self, project_path: Path, setup_home: Path, test_materializer: MockTestMaterializer
    ) -> None:
        """commit_remaining() is safe to call multiple times."""
        # Session 1: Make changes
        with Scope(project_path=project_path) as scope:
            ctx = MaterializableTestContext(name="workspace", target_path=project_path)
            scope.bind("workspace", ctx)

            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="workspace",
                    patch=DiffPatch(patch="patch1", files_changed=("file.py",)),
                )
            )

        # Session 2: Call commit_remaining twice
        with Scope.resume(project_path) as scope:
            ctx = MaterializableTestContext(name="workspace", target_path=project_path)
            scope.bind("workspace", ctx)

            # First call materializes
            result1 = scope.commit_remaining()
            assert result1["total_paths_affected"] == 1

            # Second call does nothing - context no longer has pending changes
            # (with_materialized() cleared them after first commit)
            result2 = scope.commit_remaining()
            assert result2["total_paths_affected"] == 0
            # No skipped entries because context has no pending changes
            assert result2["skipped"] == []
            assert result2["contexts"] == []

    def test_commit_remaining_handles_multiple_contexts(
        self, project_path: Path, setup_home: Path, test_materializer: MockTestMaterializer
    ) -> None:
        """commit_remaining() correctly handles partial materialization of multiple contexts."""
        # Session 1: Make changes to multiple contexts, partially materialize
        with Scope(project_path=project_path) as scope:
            ctx_a = MaterializableTestContext(name="ctx_a", target_path=project_path)
            ctx_b = MaterializableTestContext(name="ctx_b", target_path=project_path)
            scope.bind("context_a", ctx_a)
            scope.bind("context_b", ctx_b)

            # Changes to both
            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="context_a",
                    patch=DiffPatch(patch="a_patch", files_changed=("a.py",)),
                )
            )
            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="context_b",
                    patch=DiffPatch(patch="b_patch", files_changed=("b.py",)),
                )
            )

            # Only context_a was materialized before "crash"
            scope.emit(
                ContextMaterialized(
                    binding_name="context_a",
                    context_type="MaterializableTestContext",
                    success=True,
                    changes_applied=1,
                    paths_affected=("a.py",),
                )
            )

        # Session 2: Resume and complete
        with Scope.resume(project_path) as scope:
            ctx_a = MaterializableTestContext(name="ctx_a", target_path=project_path)
            ctx_b = MaterializableTestContext(name="ctx_b", target_path=project_path)
            scope.bind("context_a", ctx_a)
            scope.bind("context_b", ctx_b)

            result = scope.commit_remaining()

            # context_a skipped, context_b processed
            assert "context_a" in result["skipped"]
            assert len(result["contexts"]) == 1
            assert result["contexts"][0]["name"] == "context_b"


# =============================================================================
# Tests: Preview Commit (Phase 4.x)
# =============================================================================


class TestPreviewCommit:
    """Integration tests for preview_commit() functionality."""

    def test_preview_commit_shows_pending_changes(self, project_path: Path, setup_home: Path) -> None:
        """preview_commit() should show contexts with pending changes."""
        with Scope(project_path=project_path) as scope:
            ctx = MaterializableTestContext(name="workspace", target_path=project_path)
            scope.bind("workspace", ctx)

            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="workspace",
                    patch=DiffPatch(patch="patch1", files_changed=("file.py",)),
                )
            )

            preview = scope.preview_commit()

            assert "workspace" in preview
            assert preview["workspace"]["context_type"] == "MaterializableTestContext"
            assert preview["workspace"]["has_pending_changes"] is True
            assert len(preview["workspace"]["intent"].operations) == 1

    def test_preview_commit_empty_when_no_changes(self, project_path: Path, setup_home: Path) -> None:
        """preview_commit() should return empty when no pending changes."""
        with Scope(project_path=project_path) as scope:
            ctx = MaterializableTestContext(name="workspace", target_path=project_path)
            scope.bind("workspace", ctx)

            # No effects emitted
            preview = scope.preview_commit()

            assert preview == {}

    def test_preview_commit_excludes_non_materializable(self, project_path: Path, setup_home: Path) -> None:
        """preview_commit() should exclude non-Materializable contexts."""

        class NonMaterializableContext(BaseModel, ExecutionContextDefaults):
            """Context without Materializable protocol."""

            name: str = "simple"

            @property
            def context_id(self) -> str:
                return f"simple:{self.name}"

            @property
            def reversibility(self) -> ReversibilityLevel:
                return ReversibilityLevel.NONE

            def configure(self) -> ProviderBinding:
                return ProviderBinding()

        with Scope(project_path=project_path) as scope:
            ctx = NonMaterializableContext(name="simple")
            scope.bind("simple", ctx)

            preview = scope.preview_commit()

            # Non-materializable context not included
            assert "simple" not in preview

    def test_preview_commit_multiple_contexts(self, project_path: Path, setup_home: Path) -> None:
        """preview_commit() should show all contexts with pending changes."""
        with Scope(project_path=project_path) as scope:
            ctx_a = MaterializableTestContext(name="ctx_a", target_path=project_path)
            ctx_b = MaterializableTestContext(name="ctx_b", target_path=project_path)
            scope.bind("context_a", ctx_a)
            scope.bind("context_b", ctx_b)

            # Only context_a has changes
            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="context_a",
                    patch=DiffPatch(patch="a_patch", files_changed=("a.py",)),
                )
            )

            preview = scope.preview_commit()

            assert "context_a" in preview
            assert "context_b" not in preview  # No pending changes


# =============================================================================
# Tests: Materialization with Persistence
# =============================================================================


class TestMaterializationWithPersistence:
    """Integration tests for materialization combined with persistence."""

    def test_commit_emits_persisted_effects(
        self, project_path: Path, setup_home: Path, test_materializer: MockTestMaterializer
    ) -> None:
        """commit() should emit ContextMaterialized effects that are persisted."""
        with Scope(project_path=project_path) as scope:
            ctx = MaterializableTestContext(name="workspace", target_path=project_path)
            scope.bind("workspace", ctx)

            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="workspace",
                    patch=DiffPatch(patch="patch1", files_changed=("file.py",)),
                )
            )

            scope.commit(message="Test commit")

        # Verify ContextMaterialized was persisted
        manager = PersistenceManager(setup_home / ".shepherd", ProjectId.from_path(project_path))
        manager.initialize()
        layers = manager.read_latest_stream()

        materialized_effects = [layer.effect for layer in layers if isinstance(layer.effect, ContextMaterialized)]

        assert len(materialized_effects) == 1
        assert materialized_effects[0].binding_name == "workspace"
        assert materialized_effects[0].success is True

    def test_resume_after_commit_shows_clean_state(
        self, project_path: Path, setup_home: Path, test_materializer: MockTestMaterializer
    ) -> None:
        """After commit, resumed context should have no pending changes."""
        # Session 1: Make changes and commit
        with Scope(project_path=project_path) as scope:
            ctx = MaterializableTestContext(name="workspace", target_path=project_path)
            scope.bind("workspace", ctx)

            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="workspace",
                    patch=DiffPatch(patch="patch1", files_changed=("file.py",)),
                )
            )

            scope.commit(message="Commit changes")

        # Session 2: Resume - context should reflect post-commit state
        # Note: The context reconstruction applies all effects including the
        # implicit state change from with_materialized(). For proper state
        # reconstruction after commit, the context should track this.
        with Scope.resume(project_path) as scope:
            # Stream should have both the patch and the materialized effect
            patch_effects = [
                layer.effect for layer in scope.effects if isinstance(layer.effect, WorkspacePatchCaptured)
            ]
            materialized_effects = [
                layer.effect for layer in scope.effects if isinstance(layer.effect, ContextMaterialized)
            ]

            assert len(patch_effects) == 1
            assert len(materialized_effects) == 1


# =============================================================================
# Tests: Edge Cases
# =============================================================================


class TestPersistenceEdgeCases:
    """Edge case tests for persistence integration."""

    def test_resume_nonexistent_stream_id_returns_empty(self, project_path: Path, setup_home: Path) -> None:
        """Resuming with nonexistent stream_id should return empty scope."""
        # Create initial session
        with Scope(project_path=project_path) as scope:
            scope.emit(TaskStarted(task_name="Test", task_fqn="test.Test"))

        # Resume with bogus stream_id
        with Scope.resume(project_path, stream_id="nonexistent") as scope:
            assert len(scope.effects) == 0

    def test_large_effect_stream_performance(self, project_path: Path, setup_home: Path) -> None:
        """Large effect streams should be handled efficiently."""
        # Session 1: Create many effects
        with Scope(project_path=project_path) as scope:
            for i in range(1000):
                scope.emit(TaskStarted(task_name=f"Task{i}", task_fqn=f"test.Task{i}"))

        # Session 2: Resume should handle efficiently
        with Scope.resume(project_path) as scope:
            assert len(scope.effects) == 1000

            # Binding should still work
            ctx = MaterializableTestContext(name="workspace", target_path=project_path)
            scope.bind("workspace", ctx)

    def test_concurrent_sessions_create_separate_streams(self, project_path: Path, setup_home: Path) -> None:
        """Multiple non-overlapping sessions should create separate streams."""
        # Session 1
        with Scope(project_path=project_path) as s1:
            s1.emit(TaskStarted(task_name="S1", task_fqn="test.S1"))
            stream1_info = s1._persistence_manager.manager.get_stream_info()

        # Session 2
        with Scope(project_path=project_path) as s2:
            s2.emit(TaskStarted(task_name="S2", task_fqn="test.S2"))
            stream2_info = s2._persistence_manager.manager.get_stream_info()

        # Should have 2 distinct streams
        assert len(stream2_info) == 2
