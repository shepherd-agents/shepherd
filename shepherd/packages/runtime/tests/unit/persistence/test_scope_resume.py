"""Unit tests for scope resume functionality.

Tests:
- Resume loads persisted effects
- Deferred state derivation during bind()
- Effect routing by binding_name and context_id
- Stream continuation after resume
- Edge cases (empty stream, no matching effects)
"""

from pathlib import Path

import pytest
from pydantic import BaseModel
from shepherd_contexts.workspace.effects import WorkspacePatchCaptured
from shepherd_core.context.kernel import ExecutionContextDefaults
from shepherd_core.effects import (
    ContextPrepared,
    DiffPatch,
    Effect,
    TaskCompleted,
    TaskStarted,
)
from shepherd_core.types import ProviderBinding, ReversibilityLevel
from shepherd_runtime.scope import Scope

# =============================================================================
# Test Context Implementation
# =============================================================================


class MockContextState(BaseModel, ExecutionContextDefaults):
    """Simple test context that tracks state via effects."""

    name: str = "test"
    value: int = 0
    patches: list[str] = []

    @property
    def context_id(self) -> str:
        return f"test_context:{self.name}"

    @property
    def reversibility(self) -> ReversibilityLevel:
        return ReversibilityLevel.AUTO

    def configure(self) -> ProviderBinding:
        return ProviderBinding()

    def apply_effect(self, effect: Effect) -> "MockContextState":
        """Apply effects to update state."""
        # Handle workspace patches
        if isinstance(effect, WorkspacePatchCaptured):
            return self.model_copy(update={"patches": [*self.patches, effect.patch.patch]})
        # Handle context prepared (increment value as marker)
        if isinstance(effect, ContextPrepared):
            return self.model_copy(update={"value": self.value + 1})
        return self


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


# =============================================================================
# Tests: Basic Resume
# =============================================================================


class TestBasicResume:
    """Tests for basic resume functionality."""

    def test_resume_loads_effects_into_stream(self, project_path: Path, setup_home: Path) -> None:
        """resume() should load persisted effects into the stream."""
        # Session 1: emit some effects
        with Scope(project_path=project_path) as scope:
            scope.emit(TaskStarted(task_name="Task1", task_fqn="test.Task1"))
            scope.emit(TaskCompleted(task_name="Task1", task_fqn="test.Task1"))

        # Session 2: resume
        resumed = Scope.resume(project_path)
        try:
            assert len(resumed.effects) == 2
            effects = [layer.effect for layer in resumed.effects]
            assert effects[0].task_name == "Task1"
            assert effects[1].task_name == "Task1"
        finally:
            resumed.__exit__(None, None, None)

    def test_resume_with_no_prior_session(self, project_path: Path, setup_home: Path) -> None:
        """resume() with no prior session should return empty scope."""
        resumed = Scope.resume(project_path)
        try:
            assert len(resumed.effects) == 0
        finally:
            resumed.__exit__(None, None, None)

    def test_resume_sets_resumed_layers(self, project_path: Path, setup_home: Path) -> None:
        """resume() should set _resumed_layers for deferred derivation."""
        # Session 1
        with Scope(project_path=project_path) as scope:
            scope.emit(TaskStarted(task_name="Task1", task_fqn="test.Task1"))

        # Session 2
        resumed = Scope.resume(project_path)
        try:
            assert resumed._resumed_layers is not None
            assert len(resumed._resumed_layers) == 1
        finally:
            resumed.__exit__(None, None, None)


# =============================================================================
# Tests: Deferred State Derivation
# =============================================================================


class TestDeferredStateDerivation:
    """Tests for deferred state derivation during bind()."""

    def test_bind_applies_matching_effects_by_binding_name(self, project_path: Path, setup_home: Path) -> None:
        """bind() should apply effects that match by binding_name."""
        # Session 1: emit effect with binding_name
        with Scope(project_path=project_path) as scope:
            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="workspace",
                    patch=DiffPatch(patch="patch1", files_changed=("a.py",)),
                )
            )

        # Session 2: resume and bind
        resumed = Scope.resume(project_path)
        try:
            ctx = MockContextState(name="workspace")
            ref = resumed.bind("workspace", ctx)

            # Effect should have been applied
            assert len(ref.value.patches) == 1
            assert ref.value.patches[0] == "patch1"
        finally:
            resumed.__exit__(None, None, None)

    def test_bind_applies_matching_effects_by_context_id(self, project_path: Path, setup_home: Path) -> None:
        """bind() should apply effects that match by context_id."""
        # Session 1: emit effect with context_id (no binding_name)
        with Scope(project_path=project_path) as scope:
            scope.emit(
                ContextPrepared(
                    context_id="test_context:myctx",
                    context_type="MockContextState",
                )
            )

        # Session 2: resume and bind with matching context_id
        resumed = Scope.resume(project_path)
        try:
            ctx = MockContextState(name="myctx")  # context_id = "test_context:myctx"
            ref = resumed.bind("different_name", ctx)

            # Effect should have been applied (matched by context_id)
            assert ref.value.value == 1  # Incremented by apply_effect
        finally:
            resumed.__exit__(None, None, None)

    def test_bind_ignores_non_matching_effects(self, project_path: Path, setup_home: Path) -> None:
        """bind() should ignore effects that don't match."""
        # Session 1: emit effects for different bindings
        with Scope(project_path=project_path) as scope:
            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="other_workspace",
                    patch=DiffPatch(patch="other_patch", files_changed=("b.py",)),
                )
            )
            scope.emit(TaskStarted(task_name="Task1", task_fqn="test.Task1"))

        # Session 2: resume and bind
        resumed = Scope.resume(project_path)
        try:
            ctx = MockContextState(name="workspace")
            ref = resumed.bind("workspace", ctx)

            # No effects should have been applied
            assert len(ref.value.patches) == 0
        finally:
            resumed.__exit__(None, None, None)

    def test_bind_applies_multiple_effects_in_order(self, project_path: Path, setup_home: Path) -> None:
        """bind() should apply multiple matching effects in order."""
        # Session 1: emit multiple effects
        with Scope(project_path=project_path) as scope:
            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="workspace",
                    patch=DiffPatch(patch="patch1", files_changed=("a.py",)),
                )
            )
            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="workspace",
                    patch=DiffPatch(patch="patch2", files_changed=("b.py",)),
                )
            )
            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="workspace",
                    patch=DiffPatch(patch="patch3", files_changed=("c.py",)),
                )
            )

        # Session 2: resume and bind
        resumed = Scope.resume(project_path)
        try:
            ctx = MockContextState(name="workspace")
            ref = resumed.bind("workspace", ctx)

            # All effects should be applied in order
            assert len(ref.value.patches) == 3
            assert ref.value.patches == ["patch1", "patch2", "patch3"]
        finally:
            resumed.__exit__(None, None, None)

    def test_multiple_bindings_get_correct_effects(self, project_path: Path, setup_home: Path) -> None:
        """Multiple bindings should each get their own matching effects."""
        # Session 1: emit effects for different bindings
        with Scope(project_path=project_path) as scope:
            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="workspace1",
                    patch=DiffPatch(patch="ws1_patch", files_changed=("a.py",)),
                )
            )
            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="workspace2",
                    patch=DiffPatch(patch="ws2_patch", files_changed=("b.py",)),
                )
            )

        # Session 2: resume and bind both
        resumed = Scope.resume(project_path)
        try:
            ctx1 = MockContextState(name="ws1")
            ctx2 = MockContextState(name="ws2")

            ref1 = resumed.bind("workspace1", ctx1)
            ref2 = resumed.bind("workspace2", ctx2)

            # Each should have only its own effects
            assert ref1.value.patches == ["ws1_patch"]
            assert ref2.value.patches == ["ws2_patch"]
        finally:
            resumed.__exit__(None, None, None)


# =============================================================================
# Tests: Stream Continuation
# =============================================================================


class TestStreamContinuation:
    """Tests for stream continuation after resume."""

    def test_new_effects_persisted_after_resume(self, project_path: Path, setup_home: Path) -> None:
        """New effects after resume should be persisted."""
        # Session 1
        with Scope(project_path=project_path) as scope:
            scope.emit(TaskStarted(task_name="Session1", task_fqn="test.Session1"))

        # Session 2: resume and emit more
        with Scope.resume(project_path) as resumed:
            resumed.emit(TaskStarted(task_name="Session2", task_fqn="test.Session2"))

        # Session 3: verify chain
        with Scope.resume(project_path) as final:
            # Should have both effects via stream chain
            from shepherd_runtime.persistence import PersistenceManager, ProjectId

            manager = PersistenceManager(setup_home / ".shepherd", ProjectId.from_path(project_path))
            manager.initialize()
            all_layers = manager.read_stream_chain()

            assert len(all_layers) == 2
            assert all_layers[0].effect.task_name == "Session1"
            assert all_layers[1].effect.task_name == "Session2"

    def test_resume_without_continuation(self, project_path: Path, setup_home: Path) -> None:
        """resume(continues_from=False) should not persist new effects."""
        # Session 1
        with Scope(project_path=project_path) as scope:
            scope.emit(TaskStarted(task_name="Session1", task_fqn="test.Session1"))

        # Session 2: resume without continuation
        resumed = Scope.resume(project_path, continues_from=False)
        try:
            # Stream should have loaded effects
            assert len(resumed.effects) == 1

            # But persistence should be None
            assert resumed._persistence_manager.manager is None

            # Emitting should work but not persist
            resumed.emit(TaskStarted(task_name="Session2", task_fqn="test.Session2"))
            assert len(resumed.effects) == 2
        finally:
            resumed.__exit__(None, None, None)

        # Session 3: verify only original effect persisted
        with Scope.resume(project_path) as final:
            from shepherd_runtime.persistence import PersistenceManager, ProjectId

            manager = PersistenceManager(setup_home / ".shepherd", ProjectId.from_path(project_path))
            manager.initialize()
            all_layers = manager.read_stream_chain()

            # Only original effect (Session2 was not persisted)
            assert len(all_layers) == 1


# =============================================================================
# Tests: Clear Resumed Layers
# =============================================================================


class TestClearResumedLayers:
    """Tests for clearing resumed layers."""

    def test_clear_resumed_layers(self, project_path: Path, setup_home: Path) -> None:
        """clear_resumed_layers() should free memory."""
        # Session 1
        with Scope(project_path=project_path) as scope:
            scope.emit(TaskStarted(task_name="Task1", task_fqn="test.Task1"))

        # Session 2
        resumed = Scope.resume(project_path)
        try:
            assert resumed._resumed_layers is not None

            resumed.clear_resumed_layers()

            assert resumed._resumed_layers is None
        finally:
            resumed.__exit__(None, None, None)

    def test_bind_after_clear_works_without_effects(self, project_path: Path, setup_home: Path) -> None:
        """bind() after clear_resumed_layers() should work but not apply effects."""
        # Session 1
        with Scope(project_path=project_path) as scope:
            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="workspace",
                    patch=DiffPatch(patch="patch1", files_changed=("a.py",)),
                )
            )

        # Session 2: resume, clear, then bind
        resumed = Scope.resume(project_path)
        try:
            resumed.clear_resumed_layers()

            ctx = MockContextState(name="workspace")
            ref = resumed.bind("workspace", ctx)

            # No effects applied because we cleared
            assert len(ref.value.patches) == 0
        finally:
            resumed.__exit__(None, None, None)


# =============================================================================
# Tests: Edge Cases
# =============================================================================


class TestResumeEdgeCases:
    """Tests for edge cases in resume."""

    def test_resume_specific_stream_id(self, project_path: Path, setup_home: Path) -> None:
        """resume() can target a specific stream by ID."""
        # Session 1
        with Scope(project_path=project_path) as scope:
            scope.emit(TaskStarted(task_name="Session1", task_fqn="test.Session1"))
            stream1_id = next(iter(scope._persistence_manager.manager.get_stream_info().keys()))

        # Session 2
        with Scope(project_path=project_path) as scope:
            scope.emit(TaskStarted(task_name="Session2", task_fqn="test.Session2"))

        # Resume from first stream specifically
        resumed = Scope.resume(project_path, stream_id=stream1_id)
        try:
            assert len(resumed.effects) == 1
            assert resumed.effects[0].effect.task_name == "Session1"
        finally:
            resumed.__exit__(None, None, None)

    def test_resume_with_interleaved_effects(self, project_path: Path, setup_home: Path) -> None:
        """Effects targeting different bindings interleaved should route correctly."""
        # Session 1: interleaved effects
        with Scope(project_path=project_path) as scope:
            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="A",
                    patch=DiffPatch(patch="A1", files_changed=("a.py",)),
                )
            )
            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="B",
                    patch=DiffPatch(patch="B1", files_changed=("b.py",)),
                )
            )
            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="A",
                    patch=DiffPatch(patch="A2", files_changed=("a2.py",)),
                )
            )
            scope.emit(
                WorkspacePatchCaptured(
                    binding_name="B",
                    patch=DiffPatch(patch="B2", files_changed=("b2.py",)),
                )
            )

        # Session 2: bind A then B
        resumed = Scope.resume(project_path)
        try:
            ctx_a = MockContextState(name="a")
            ctx_b = MockContextState(name="b")

            ref_a = resumed.bind("A", ctx_a)
            ref_b = resumed.bind("B", ctx_b)

            # Each should have exactly its effects
            assert ref_a.value.patches == ["A1", "A2"]
            assert ref_b.value.patches == ["B1", "B2"]
        finally:
            resumed.__exit__(None, None, None)

    def test_resume_works_as_context_manager(self, project_path: Path, setup_home: Path) -> None:
        """Scope.resume() should work as context manager."""
        # Session 1
        with Scope(project_path=project_path) as scope:
            scope.emit(TaskStarted(task_name="Task1", task_fqn="test.Task1"))

        # Session 2: use as context manager
        with Scope.resume(project_path) as resumed:
            assert len(resumed.effects) == 1
            resumed.emit(TaskStarted(task_name="Task2", task_fqn="test.Task2"))
            assert len(resumed.effects) == 2

        # Stream should be closed after exit
        from shepherd_runtime.persistence import PersistenceManager, ProjectId, StreamIndex

        manager = PersistenceManager(setup_home / ".shepherd", ProjectId.from_path(project_path))
        manager.initialize()
        index = StreamIndex.load(manager.index_path)
        assert index.current_stream_id is None
