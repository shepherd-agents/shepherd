"""Tests for ExecutionContext protocol compliance across all context types.

These parametrized tests ensure all context implementations follow the
ExecutionContext protocol consistently. This validates that the pattern
is truly domain-agnostic.

Tested contexts:
- WorkspaceRef (coding domain)
- SessionState (conversation domain)
- KVStoreContext (generic state domain)

Protocol Methods (v2 API):
- configure(capabilities) -> ProviderBinding
- prepare() -> Self
- extract_effects(sandbox, result) -> Sequence[Effect]
- apply_effect(effect) -> Self
- cleanup(error: Exception | None) -> None
"""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from shepherd_contexts import KVStoreContext, SessionState, WorkspaceRef
from shepherd_core.context import compute_composite_reversibility, is_execution_context
from shepherd_core.effects import Effect
from shepherd_core.types import ExecutionResult, ProviderBinding, ReversibilityLevel
from shepherd_runtime.context.sandbox import GITPYTHON_AVAILABLE

if GITPYTHON_AVAILABLE:
    from git import Repo

pytestmark = pytest.mark.skipif(not GITPYTHON_AVAILABLE, reason="GitPython not installed")

# =============================================================================
# Fixtures for creating context instances
# =============================================================================


@pytest.fixture
def git_workspace(tmp_path: Path) -> Path:
    """Create a temporary git repository."""
    repo = Repo.init(tmp_path)
    (tmp_path / "README.md").write_text("# Test Repo")
    repo.index.add(["README.md"])
    repo.index.commit("Initial commit")
    return tmp_path


@pytest.fixture
def workspace_factory(git_workspace: Path) -> Callable[[], WorkspaceRef]:
    """Factory for creating WorkspaceRef instances."""

    def factory() -> WorkspaceRef:
        return WorkspaceRef.from_path(git_workspace)

    return factory


@pytest.fixture
def session_factory() -> Callable[[], SessionState]:
    """Factory for creating SessionState instances."""

    def factory() -> SessionState:
        return SessionState(session_id="sess_test123")

    return factory


@pytest.fixture
def kvstore_factory() -> Callable[[], KVStoreContext]:
    """Factory for creating KVStoreContext instances."""

    def factory() -> KVStoreContext:
        return KVStoreContext.create({"key": "value"})

    return factory


@pytest.fixture
def mock_execution_result() -> ExecutionResult:
    """Create a mock ExecutionResult for capture tests."""
    return ExecutionResult(
        success=True,
        output_text="Mock execution output",
    )


# =============================================================================
# Parametrized Protocol Compliance Tests
# =============================================================================


class TestProtocolCompliance:
    """Parametrized tests for ExecutionContext protocol compliance."""

    @pytest.fixture(params=["workspace", "session", "kvstore"])
    def context_info(
        self,
        request: pytest.FixtureRequest,
        workspace_factory: Callable[[], WorkspaceRef],
        session_factory: Callable[[], SessionState],
        kvstore_factory: Callable[[], KVStoreContext],
    ) -> tuple[str, Callable[[], Any]]:
        """Parametrized fixture providing context name and factory."""
        factories = {
            "workspace": workspace_factory,
            "session": session_factory,
            "kvstore": kvstore_factory,
        }
        name = request.param
        return (name, factories[name])

    def test_is_execution_context(self, context_info: tuple[str, Callable]):
        """All contexts should be recognized as ExecutionContext."""
        name, factory = context_info
        ctx = factory()
        assert is_execution_context(ctx), f"{name} should be ExecutionContext"

    def test_has_context_id_property(self, context_info: tuple[str, Callable]):
        """All contexts should have context_id property."""
        name, factory = context_info
        ctx = factory()

        assert hasattr(ctx, "context_id"), f"{name} missing context_id"
        assert isinstance(ctx.context_id, str), f"{name} context_id should be string"
        assert len(ctx.context_id) > 0, f"{name} context_id should not be empty"

    def test_has_reversibility_property(self, context_info: tuple[str, Callable]):
        """All contexts should have reversibility property."""
        name, factory = context_info
        ctx = factory()

        assert hasattr(ctx, "reversibility"), f"{name} missing reversibility"
        assert isinstance(ctx.reversibility, ReversibilityLevel), f"{name} reversibility should be ReversibilityLevel"

    def test_has_configure_method(self, context_info: tuple[str, Callable]):
        """All contexts should have configure() method."""
        name, factory = context_info
        ctx = factory()

        assert hasattr(ctx, "configure"), f"{name} missing configure"
        assert callable(ctx.configure), f"{name} configure not callable"

    def test_has_prepare_method(self, context_info: tuple[str, Callable]):
        """All contexts should have prepare() method."""
        name, factory = context_info
        ctx = factory()

        assert hasattr(ctx, "prepare"), f"{name} missing prepare"
        assert callable(ctx.prepare), f"{name} prepare not callable"

    def test_has_extract_effects_method(self, context_info: tuple[str, Callable]):
        """All contexts should have extract_effects() method."""
        name, factory = context_info
        ctx = factory()

        assert hasattr(ctx, "extract_effects"), f"{name} missing extract_effects"
        assert callable(ctx.extract_effects), f"{name} extract_effects not callable"

    def test_has_apply_effect_method(self, context_info: tuple[str, Callable]):
        """All contexts should have apply_effect() method."""
        name, factory = context_info
        ctx = factory()

        assert hasattr(ctx, "apply_effect"), f"{name} missing apply_effect"
        assert callable(ctx.apply_effect), f"{name} apply_effect not callable"

    def test_has_cleanup_method(self, context_info: tuple[str, Callable]):
        """All contexts should have cleanup() method."""
        name, factory = context_info
        ctx = factory()

        assert hasattr(ctx, "cleanup"), f"{name} missing cleanup"
        assert callable(ctx.cleanup), f"{name} cleanup not callable"

    def test_context_id_deterministic(self, context_info: tuple[str, Callable]):
        """context_id should be deterministic (same on multiple accesses)."""
        name, factory = context_info
        ctx = factory()

        id1 = ctx.context_id
        id2 = ctx.context_id
        id3 = ctx.context_id

        assert id1 == id2 == id3, f"{name} context_id should be stable"

    def test_configure_returns_provider_binding(self, context_info: tuple[str, Callable]):
        """configure() should return a ProviderBinding."""
        name, factory = context_info
        ctx = factory()

        # configure() takes ProviderCapabilities | None, not frozenset
        binding = ctx.configure(None)

        assert binding is not None, f"{name} configure should return something"
        assert isinstance(binding, ProviderBinding), f"{name} configure should return ProviderBinding"

    def test_prepare_returns_context(
        self,
        context_info: tuple[str, Callable],
    ):
        """prepare() should return a context (self or new)."""
        name, factory = context_info
        ctx = factory()

        prepared = ctx.prepare()

        assert prepared is not None, f"{name} prepare should return something"
        assert is_execution_context(prepared), f"{name} prepare should return context"

    def test_extract_effects_returns_sequence(
        self,
        context_info: tuple[str, Callable],
        mock_execution_result: ExecutionResult,
    ):
        """extract_effects() should return a sequence of effects."""
        name, factory = context_info
        ctx = factory()

        prepared = ctx.prepare()
        effects = prepared.extract_effects(None, mock_execution_result)

        assert effects is not None, f"{name} extract_effects should return something"
        # Should be a sequence (list, tuple, etc.)
        assert hasattr(effects, "__iter__"), f"{name} extract_effects should return sequence"
        # All items should be Effect instances
        for effect in effects:
            assert isinstance(effect, Effect), f"{name} extract_effects should return Effect instances"

    def test_apply_effect_returns_context(
        self,
        context_info: tuple[str, Callable],
        mock_execution_result: ExecutionResult,
    ):
        """apply_effect() should return a context."""
        name, factory = context_info
        ctx = factory()

        prepared = ctx.prepare()
        effects = prepared.extract_effects(None, mock_execution_result)

        # Apply each effect and verify result is a context
        new_ctx = prepared
        for effect in effects:
            new_ctx = new_ctx.apply_effect(effect)
            assert is_execution_context(new_ctx), f"{name} apply_effect should return context"

    def test_cleanup_accepts_none_error(
        self,
        context_info: tuple[str, Callable],
    ):
        """cleanup() should accept None for success case."""
        _name, factory = context_info
        ctx = factory()

        prepared = ctx.prepare()
        # Should not raise
        prepared.cleanup(error=None)

    def test_cleanup_accepts_exception(
        self,
        context_info: tuple[str, Callable],
    ):
        """cleanup() should accept Exception for failure case."""
        _name, factory = context_info
        ctx = factory()

        prepared = ctx.prepare()
        # Should not raise
        prepared.cleanup(error=Exception("test error"))

    def test_cleanup_idempotent(
        self,
        context_info: tuple[str, Callable],
    ):
        """cleanup() should be idempotent."""
        _name, factory = context_info
        ctx = factory()

        prepared = ctx.prepare()
        # Multiple calls should not raise
        prepared.cleanup(None)
        prepared.cleanup(None)
        prepared.cleanup(error=Exception("test"))


# =============================================================================
# Context ID Format Tests
# =============================================================================


class TestContextIdFormats:
    """Test that each context type uses appropriate context_id format."""

    def test_workspace_context_id_format(self, workspace_factory: Callable[[], WorkspaceRef]):
        """WorkspaceRef should use workspace:{path}:{commit} format."""
        ctx = workspace_factory()
        assert ctx.context_id.startswith("workspace:")

    def test_session_context_id_format(self, session_factory: Callable[[], SessionState]):
        """SessionState should use session:{id} format."""
        ctx = session_factory()
        assert ctx.context_id.startswith("session:")
        assert "sess_test123" in ctx.context_id

    def test_kvstore_context_id_format(self, kvstore_factory: Callable[[], KVStoreContext]):
        """KVStoreContext should use kvstore:{hash} format."""
        ctx = kvstore_factory()
        assert ctx.context_id.startswith("kvstore:")


# =============================================================================
# Reversibility Composition Tests
# =============================================================================


class TestReversibilityComposition:
    """Test reversibility composition across context types."""

    def test_all_builtin_contexts_are_auto(
        self,
        workspace_factory: Callable[[], WorkspaceRef],
        session_factory: Callable[[], SessionState],
        kvstore_factory: Callable[[], KVStoreContext],
    ):
        """All built-in contexts should be AUTO reversible."""
        workspace = workspace_factory()
        session = session_factory()
        kvstore = kvstore_factory()

        assert workspace.reversibility == ReversibilityLevel.AUTO
        assert session.reversibility == ReversibilityLevel.AUTO
        assert kvstore.reversibility == ReversibilityLevel.AUTO

    def test_composite_all_auto(
        self,
        workspace_factory: Callable[[], WorkspaceRef],
        session_factory: Callable[[], SessionState],
        kvstore_factory: Callable[[], KVStoreContext],
    ):
        """Composite of all AUTO contexts should be AUTO."""
        contexts = [
            workspace_factory(),
            session_factory(),
            kvstore_factory(),
        ]

        composite = compute_composite_reversibility(contexts)
        assert composite == ReversibilityLevel.AUTO

    def test_composite_with_mock_compensable(
        self,
        workspace_factory: Callable[[], WorkspaceRef],
    ):
        """Composite with COMPENSABLE context should be COMPENSABLE."""

        class MockCompensableContext:
            @property
            def context_id(self) -> str:
                return "mock:compensable"

            @property
            def reversibility(self) -> ReversibilityLevel:
                return ReversibilityLevel.COMPENSABLE

            def configure(self, capabilities: frozenset[str]) -> ProviderBinding:
                return ProviderBinding(context_id=self.context_id)

            def prepare(self):
                return self

            def extract_effects(self, sandbox, result: ExecutionResult) -> Sequence[Effect]:
                return []

            def apply_effect(self, effect: Effect):
                return self

            def cleanup(self, error=None):
                pass

        workspace = workspace_factory()
        mock = MockCompensableContext()

        composite = compute_composite_reversibility([workspace, mock])
        assert composite == ReversibilityLevel.COMPENSABLE

    def test_composite_with_mock_none(
        self,
        workspace_factory: Callable[[], WorkspaceRef],
        kvstore_factory: Callable[[], KVStoreContext],
    ):
        """Composite with NONE context should be NONE."""

        class MockIrreversibleContext:
            @property
            def context_id(self) -> str:
                return "mock:irreversible"

            @property
            def reversibility(self) -> ReversibilityLevel:
                return ReversibilityLevel.NONE

            def configure(self, capabilities: frozenset[str]) -> ProviderBinding:
                return ProviderBinding(context_id=self.context_id)

            def prepare(self):
                return self

            def extract_effects(self, sandbox, result: ExecutionResult) -> Sequence[Effect]:
                return []

            def apply_effect(self, effect: Effect):
                return self

            def cleanup(self, error=None):
                pass

        workspace = workspace_factory()
        kvstore = kvstore_factory()
        mock = MockIrreversibleContext()

        composite = compute_composite_reversibility([workspace, kvstore, mock])
        assert composite == ReversibilityLevel.NONE


# =============================================================================
# Context ID Stability Tests
# =============================================================================


class TestContextIdStability:
    """Test context_id stability across lifecycle operations."""

    def test_workspace_context_id_stable_across_effects(
        self,
        workspace_factory: Callable[[], WorkspaceRef],
        git_workspace: Path,
        mock_execution_result: ExecutionResult,
    ):
        """WorkspaceRef context_id should be stable across extract_effects/apply_effect."""
        ctx = workspace_factory()
        original_id = ctx.context_id

        prepared = ctx.prepare()

        # Make a change
        (git_workspace / "new_file.txt").write_text("content")

        effects = prepared.extract_effects(None, mock_execution_result)
        new_ctx = prepared
        for effect in effects:
            new_ctx = new_ctx.apply_effect(effect)

        assert new_ctx.context_id == original_id

    def test_kvstore_context_id_stable_across_effects(
        self,
        kvstore_factory: Callable[[], KVStoreContext],
        mock_execution_result: ExecutionResult,
    ):
        """KVStoreContext context_id should be stable across extract_effects/apply_effect."""
        ctx = kvstore_factory()
        original_id = ctx.context_id

        prepared = ctx.prepare()
        prepared.set("key", "new_value")
        effects = prepared.extract_effects(None, mock_execution_result)
        new_ctx = prepared
        for effect in effects:
            new_ctx = new_ctx.apply_effect(effect)

        assert new_ctx.context_id == original_id

    def test_session_context_id_based_on_session_id(
        self,
        session_factory: Callable[[], SessionState],
    ):
        """SessionState context_id should be based on session_id."""
        ctx = session_factory()

        # session_id is immutable, so context_id is inherently stable
        assert ctx.session_id in ctx.context_id


# =============================================================================
# v2 API Tests: extract_effects() and apply_effect()
# =============================================================================


class TestV2APICompliance:
    """Test v2 API compliance: extract_effects() and apply_effect()."""

    @pytest.fixture(params=["workspace", "session", "kvstore"])
    def context_info(
        self,
        request: pytest.FixtureRequest,
        workspace_factory: Callable[[], WorkspaceRef],
        session_factory: Callable[[], SessionState],
        kvstore_factory: Callable[[], KVStoreContext],
    ) -> tuple[str, Callable[[], Any]]:
        """Parametrized fixture providing context name and factory."""
        factories = {
            "workspace": workspace_factory,
            "session": session_factory,
            "kvstore": kvstore_factory,
        }
        name = request.param
        return (name, factories[name])

    def test_has_extract_effects_method(self, context_info: tuple[str, Callable]):
        """All v2 contexts should have extract_effects() method."""
        name, factory = context_info
        ctx = factory()

        assert hasattr(ctx, "extract_effects"), f"{name} missing extract_effects"
        assert callable(ctx.extract_effects), f"{name} extract_effects not callable"

    def test_has_apply_effect_method(self, context_info: tuple[str, Callable]):
        """All v2 contexts should have apply_effect() method."""
        name, factory = context_info
        ctx = factory()

        assert hasattr(ctx, "apply_effect"), f"{name} missing apply_effect"
        assert callable(ctx.apply_effect), f"{name} apply_effect not callable"

    def test_extract_effects_returns_sequence(
        self,
        context_info: tuple[str, Callable],
        mock_execution_result: ExecutionResult,
    ):
        """extract_effects() should return a sequence of Effects."""
        name, factory = context_info
        ctx = factory()

        effects = ctx.extract_effects(None, mock_execution_result)

        # Should be a sequence (list, tuple, etc.)
        assert hasattr(effects, "__iter__"), f"{name} extract_effects should return iterable"
        # All items should be Effects
        for effect in effects:
            assert isinstance(effect, Effect), f"{name} should return Effect instances"

    def test_apply_effect_returns_same_type(
        self,
        context_info: tuple[str, Callable],
    ):
        """apply_effect() should return same type as self."""
        name, factory = context_info
        ctx = factory()

        # Apply a base effect (should be no-op)
        result = ctx.apply_effect(Effect())

        assert type(result) == type(ctx), f"{name} apply_effect should return same type"


# Note: TestV1V2Equivalence class removed - v1 API (capture()) has been retired
# All contexts now use v2 API (extract_effects + apply_effect)
