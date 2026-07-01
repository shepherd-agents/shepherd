"""Tests for ContextRef - live references to bound contexts."""

import copy

import pytest
from shepherd_contexts import KVStoreContext, SessionState, WorkspaceRef
from shepherd_core.context import ExecutionContextDefaults
from shepherd_core.types import ProviderCapabilities, ReversibilityLevel
from shepherd_runtime.context import Bindable
from shepherd_runtime.scope import Scope


class TestContextRefBasics:
    """Basic ContextRef functionality tests."""

    def test_context_ref_returns_current_state(self):
        """ContextRef.value returns the current context from scope."""
        with Scope() as scope:
            session_ref = scope.bind("session", SessionState())

            # Initial state
            assert session_ref.session_id is None

            # Simulate effect application
            new_session = SessionState(session_id="test-123")
            scope.update_context("session", new_session)

            # Reference reflects update
            assert session_ref.session_id == "test-123"

    def test_context_ref_attribute_delegation(self):
        """ContextRef delegates attribute access to underlying context."""
        with Scope() as scope:
            # Use direct construction with valid 40-char SHA
            workspace = WorkspaceRef(path="/repo", base_commit="a" * 40)
            workspace_ref = scope.bind("workspace", workspace)

            # Direct attribute access
            assert workspace_ref.path == "/repo"
            assert workspace_ref.context_id.startswith("workspace:")

    def test_context_ref_method_delegation(self):
        """ContextRef delegates method calls to underlying context."""
        with Scope() as scope:
            session_ref = scope.bind("session", SessionState())

            # Method call delegation - SessionState has methods we can call
            ctx = session_ref.value
            assert hasattr(ctx, "configure")  # Verify context has a method

            # Call method through ref (configure returns ProviderBinding)
            binding = session_ref.configure(ProviderCapabilities(provider_type="claude"))
            assert binding is not None

    def test_context_ref_explicit_value(self):
        """ContextRef.value returns typed context."""
        with Scope() as scope:
            session_ref = scope.bind("session", SessionState())

            ctx = session_ref.value
            assert isinstance(ctx, SessionState)

    def test_context_ref_binding_name(self):
        """ContextRef.binding_name returns the binding name, not context's name."""
        with Scope() as scope:
            ref = scope.bind("my_binding", SessionState())

            # binding_name is the scope binding name
            assert ref.binding_name == "my_binding"


class TestContextRefEquality:
    """ContextRef equality and hashing tests."""

    def test_context_ref_equality(self):
        """ContextRefs are equal if they reference the same binding."""
        with Scope() as scope:
            # Use distinct session_ids to ensure unique context_ids
            ref1 = scope.bind("session", SessionState(session_id="session-1"))
            ref2 = scope.bind("other", SessionState(session_id="session-2"))

            # Different bindings are not equal (even if contexts are equal)
            assert ref1 != ref2

            # Ref equals itself
            assert ref1 == ref1  # noqa: PLR0124

    def test_context_ref_hash_stable(self):
        """ContextRef hash is stable across context updates."""
        with Scope() as scope:
            ref = scope.bind("session", SessionState())
            hash1 = hash(ref)

            # Update context
            scope.update_context("session", SessionState(session_id="new"))
            hash2 = hash(ref)

            # Hash based on (scope, name), not context value
            assert hash1 == hash2

    def test_context_ref_in_set(self):
        """ContextRef can be used in sets."""
        with Scope() as scope:
            ref1 = scope.bind("session", SessionState())
            # Use direct construction with valid 40-char SHA
            workspace = WorkspaceRef(path="/repo", base_commit="a" * 40)
            ref2 = scope.bind("workspace", workspace)

            refs = {ref1, ref2}
            assert len(refs) == 2
            assert ref1 in refs
            assert ref2 in refs

    def test_context_ref_as_dict_key(self):
        """ContextRef can be used as dict key."""
        with Scope() as scope:
            ref = scope.bind("session", SessionState())

            mapping = {ref: "session context"}
            assert mapping[ref] == "session context"

            # Key still works after context update
            scope.update_context("session", SessionState(session_id="new"))
            assert mapping[ref] == "session context"


class TestContextRefRejections:
    """Tests for operations that should be rejected on ContextRef."""

    def test_context_ref_setattr_rejected(self):
        """Cannot set attributes on ContextRef."""
        with Scope() as scope:
            ref = scope.bind("session", SessionState())

            with pytest.raises(AttributeError, match="Cannot set"):
                ref.session_id = "hacked"

    def test_context_ref_iter_rejected(self):
        """Cannot iterate over ContextRef."""
        with Scope() as scope:
            ref = scope.bind("session", SessionState())

            with pytest.raises(TypeError, match="not iterable"):
                for _ in ref:
                    pass

    def test_context_ref_len_rejected(self):
        """Cannot get len() of ContextRef."""
        with Scope() as scope:
            ref = scope.bind("session", SessionState())

            with pytest.raises(TypeError, match="has no len"):
                len(ref)

    def test_context_ref_getitem_rejected(self):
        """Cannot subscript ContextRef."""
        with Scope() as scope:
            ref = scope.bind("session", SessionState())

            with pytest.raises(TypeError, match="not subscriptable"):
                _ = ref[0]


class TestContextRefClosedScope:
    """Tests for ContextRef behavior when scope is closed."""

    def test_context_ref_closed_scope_raises(self):
        """Accessing ContextRef after scope closes raises RuntimeError."""
        with Scope() as scope:
            ref = scope.bind("session", SessionState())

        # Scope is now closed
        with pytest.raises(RuntimeError, match="scope has been closed"):
            _ = ref.session_id

    def test_context_ref_closed_scope_value_raises(self):
        """ContextRef.value after scope closes raises RuntimeError."""
        with Scope() as scope:
            ref = scope.bind("session", SessionState())

        with pytest.raises(RuntimeError, match="scope has been closed"):
            _ = ref.value

    def test_context_ref_repr_after_close(self):
        """ContextRef repr handles closed scope gracefully."""
        with Scope() as scope:
            ref = scope.bind("session", SessionState())

        # Should not raise, should show "closed"
        repr_str = repr(ref)
        assert "ContextRef" in repr_str
        assert "closed" in repr_str


class TestContextRefRebinding:
    """Tests for rebinding behavior."""

    def test_context_ref_duplicate_bind_raises(self):
        """Binding the same name twice raises ValueError."""
        with Scope() as scope:
            scope.bind("session", SessionState(session_id="first"))

            # Cannot rebind with bind() - use update_context() instead
            with pytest.raises(ValueError, match="already bound"):
                scope.bind("session", SessionState(session_id="second"))

    def test_context_ref_update_context_reflects_in_ref(self):
        """update_context() changes what ref sees."""
        with Scope() as scope:
            ref = scope.bind("session", SessionState(session_id="first"))
            assert ref.session_id == "first"

            # Update via update_context (how ExecutionLifecycle updates)
            scope.update_context("session", SessionState(session_id="second"))

            # Ref sees the updated context
            assert ref.session_id == "second"


class TestContextRefMisc:
    """Miscellaneous ContextRef tests."""

    def test_context_ref_repr(self):
        """ContextRef has useful repr."""
        with Scope() as scope:
            ref = scope.bind("session", SessionState(session_id="abc"))

            repr_str = repr(ref)
            assert "ContextRef" in repr_str
            assert "abc" in repr_str

    def test_context_ref_dir(self):
        """ContextRef.__dir__ includes context attributes."""
        with Scope() as scope:
            ref = scope.bind("session", SessionState())

            attrs = dir(ref)
            assert "value" in attrs
            assert "binding_name" in attrs
            assert "session_id" in attrs  # From SessionState

    def test_context_ref_bool_always_true(self):
        """ContextRef is always truthy."""
        with Scope() as scope:
            ref = scope.bind("session", SessionState())

            # Verify the ref itself is truthy
            assert ref
            assert bool(ref) is True

    def test_context_ref_copy_returns_self(self):
        """Copying a ContextRef returns the same ref."""
        with Scope() as scope:
            ref = scope.bind("session", SessionState())

            # Shallow copy returns same ref
            shallow = copy.copy(ref)
            assert shallow is ref

            # Deep copy also returns same ref
            deep = copy.deepcopy(ref)
            assert deep is ref


class TestContextRefKVStore:
    """Tests for ContextRef with KVStoreContext.

    KVStoreContext is particularly important for testing because it has a
    .get(key) method that would conflict with the old ContextRef.get() API.
    The .value property solves this collision.
    """

    def test_kvstore_get_method_collision_solved(self):
        """Demonstrate that .value solves the .get() method collision.

        This test documents the core problem that motivated changing from
        ContextRef.get() to ContextRef.value:

        - KVStoreContext has .get(key, default) for dict-like access
        - Old API: ContextRef.get() returned the underlying context
        - Problem: config_ref.get("key") would call ContextRef.get(), not KVStore.get()
        - Solution: Use .value property, so config_ref.value.get("key") is unambiguous
        """
        with Scope() as scope:
            config_ref = scope.bind("config", KVStoreContext.create({"env": "dev", "debug": "true"}))

            # The .value property gives us the underlying KVStoreContext
            # where we can call .get(key) without collision
            assert config_ref.value.get("env") == "dev"
            assert config_ref.value.get("debug") == "true"
            assert config_ref.value.get("missing", "default") == "default"

            # Other methods that don't collide work via delegation
            assert config_ref.has_key("env") is True
            assert config_ref.has_key("missing") is False
            assert set(config_ref.keys()) == {"env", "debug"}

            # Direct data access also works via delegation
            assert config_ref.data["env"] == "dev"

    def test_kvstore_ref_returns_current_state(self):
        """ContextRef.value returns the current KVStore from scope."""
        with Scope() as scope:
            config_ref = scope.bind("config", KVStoreContext.create({"env": "dev"}))

            # Initial state
            assert config_ref.data == {"env": "dev"}

            # Simulate effect application (as ExecutionLifecycle would do)
            updated = KVStoreContext.create({"env": "prod", "debug": "false"})
            # Preserve context_id for proper update
            updated = KVStoreContext(
                data=updated.data,
                frozen_context_id=config_ref.value.frozen_context_id,
            )
            scope.update_context("config", updated)

            # Reference reflects update
            assert config_ref.data == {"env": "prod", "debug": "false"}

    def test_kvstore_ref_attribute_delegation(self):
        """ContextRef delegates attribute access to KVStoreContext."""
        with Scope() as scope:
            config_ref = scope.bind("config", KVStoreContext.create({"key": "value"}))

            # Direct attribute access
            assert config_ref.data == {"key": "value"}
            assert config_ref.context_id.startswith("kvstore:")
            assert config_ref.has_key("key") is True

            # Note: access .value.get() for key lookups, or access .data directly
            assert config_ref.value.get("key") == "value"

    def test_kvstore_ref_method_delegation(self):
        """ContextRef delegates method calls to KVStoreContext."""
        with Scope() as scope:
            config_ref = scope.bind("config", KVStoreContext.create({"a": "1", "b": "2"}))

            # Method calls through ref (methods that don't conflict with ContextRef)
            keys = config_ref.keys()
            assert set(keys) == {"a", "b"}

            # Use .value.get() for key lookups, or access .data directly
            assert config_ref.value.get("a") == "1"
            assert config_ref.data["a"] == "1"


class TestFluentBinding:
    """Tests for fluent binding patterns with __binding_name__."""

    def test_context_bind_method(self):
        """Context.bind(scope) uses __binding_name__."""
        with Scope() as scope:
            # SessionState has __binding_name__ = "session"
            session = SessionState()
            ref = session.bind(scope)

            assert ref.binding_name == "session"
            assert ref.value is session

    def test_context_bind_method_with_explicit_name(self):
        """Context.bind(scope, name=...) overrides __binding_name__."""
        with Scope() as scope:
            session = SessionState()
            ref = session.bind(scope, name="custom_session")

            assert ref.binding_name == "custom_session"
            assert ref.value is session

    def test_scope_bind_context_only(self):
        """scope.bind(context) uses __binding_name__."""
        with Scope() as scope:
            session = SessionState()
            ref = scope.bind(session)

            assert ref.binding_name == "session"
            assert ref.value is session

    def test_scope_bind_explicit_name(self):
        """scope.bind(name, context) works as before."""
        with Scope() as scope:
            session = SessionState()
            ref = scope.bind("explicit_name", session)

            assert ref.binding_name == "explicit_name"
            assert ref.value is session

    def test_workspace_binding_name(self):
        """WorkspaceRef has __binding_name__ = 'workspace'."""
        with Scope() as scope:
            # Use direct construction with valid 40-char SHA
            workspace = WorkspaceRef(path="/repo", base_commit="a" * 40)
            ref = scope.bind(workspace)

            assert ref.binding_name == "workspace"

    def test_kvstore_binding_name(self):
        """KVStoreContext has __binding_name__ = 'config'."""
        with Scope() as scope:
            config = KVStoreContext.create({"key": "value"})
            ref = scope.bind(config)

            assert ref.binding_name == "config"

    def test_context_without_binding_name_raises(self):
        """Binding context without __binding_name__ and no explicit name raises."""
        from dataclasses import dataclass

        @dataclass
        class NoBindingNameContext(ExecutionContextDefaults):
            some_value: str = ""

            @property
            def context_id(self) -> str:
                return f"test:{self.some_value}"

            @property
            def reversibility(self):
                return ReversibilityLevel.AUTO

        with Scope() as scope:
            ctx = NoBindingNameContext(some_value="test")

            # scope.bind(context) should raise
            with pytest.raises(ValueError, match="no __binding_name__"):
                scope.bind(ctx)

    def test_context_bind_without_binding_name_raises(self):
        """Calling context.bind(scope) without __binding_name__ raises."""
        from dataclasses import dataclass

        @dataclass
        class NoBindingNameContext(ExecutionContextDefaults, Bindable):
            some_value: str = ""

            @property
            def context_id(self) -> str:
                return f"test:{self.some_value}"

            @property
            def reversibility(self):
                return ReversibilityLevel.AUTO

        with Scope() as scope:
            ctx = NoBindingNameContext(some_value="test")

            # context.bind(scope) without name should raise
            with pytest.raises(ValueError, match="no default binding name"):
                ctx.bind(scope)

            # But explicit name should work
            ref = ctx.bind(scope, name="explicit")
            assert ref.binding_name == "explicit"
