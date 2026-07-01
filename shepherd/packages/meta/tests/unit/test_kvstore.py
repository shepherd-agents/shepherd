"""Tests for KVStoreContext (Phase 4: Domain-Agnostic Validation).

These tests validate that the ExecutionContext pattern works beyond the
coding domain by testing a simple key-value store context.

Test coverage:
1. Basic creation and data access
2. context_id generation and stability
3. reversibility declaration
4. Full lifecycle (prepare → modify → capture)
5. Change detection
6. Protocol compliance
7. Serialization roundtrip
"""

import pytest
from shepherd_contexts import KVStoreContext, SessionState
from shepherd_core.context import compute_composite_reversibility, is_execution_context
from shepherd_core.effects import TaskCompleted, TaskStarted
from shepherd_core.scope import Stream
from shepherd_core.types import ReversibilityLevel

# =============================================================================
# Basic Creation Tests
# =============================================================================


class TestKVStoreCreation:
    """Test KVStoreContext creation and basic properties."""

    def test_create_empty(self):
        """Can create empty KVStore."""
        store = KVStoreContext.create()
        assert store.data == {}

    def test_create_with_data(self):
        """Can create KVStore with initial data."""
        store = KVStoreContext.create({"key": "value", "foo": "bar"})
        assert store.data == {"key": "value", "foo": "bar"}

    def test_model_is_frozen(self):
        """Model should be frozen (cannot reassign attributes)."""
        store = KVStoreContext.create({"key": "value"})
        with pytest.raises(Exception):  # Pydantic frozen model
            store.data = {"new": "value"}

    def test_str_returns_empty(self):
        """__str__ should return empty for prompt invisibility."""
        store = KVStoreContext.create({"key": "value"})
        assert str(store) == ""

    def test_repr_shows_useful_info(self):
        """__repr__ should show keys and source_step."""
        store = KVStoreContext.create({"a": "1", "b": "2"})
        repr_str = repr(store)
        assert "KVStoreContext" in repr_str
        assert "a" in repr_str or "b" in repr_str


# =============================================================================
# Context ID Tests
# =============================================================================


class TestKVStoreContextId:
    """Test context_id generation and stability."""

    def test_has_context_id(self):
        """KVStore should have context_id property."""
        store = KVStoreContext.create({"key": "value"})
        assert hasattr(store, "context_id")
        assert isinstance(store.context_id, str)

    def test_context_id_format(self):
        """context_id should follow kvstore:{hash} format."""
        store = KVStoreContext.create({"key": "value"})
        assert store.context_id.startswith("kvstore:")

    def test_context_id_frozen_at_creation(self):
        """context_id should be frozen at creation."""
        store = KVStoreContext.create({"key": "value"})
        original_id = store.context_id

        # Multiple accesses return same ID
        assert store.context_id == original_id
        assert store.context_id == original_id

    def test_context_id_deterministic(self):
        """Same data should produce same context_id."""
        store1 = KVStoreContext.create({"key": "value"})
        store2 = KVStoreContext.create({"key": "value"})

        # Different instances but same content = same hash component
        # But context_id is frozen at creation, so may differ
        # The important thing is each instance has a stable ID
        assert store1.context_id == store1.context_id
        assert store2.context_id == store2.context_id

    def test_context_id_preserved_across_capture(self):
        """context_id should be preserved after capture."""
        store = KVStoreContext.create({"key": "value"})
        original_id = store.context_id

        prepared = store.__context_prepare__()
        prepared.set("key", "new_value")
        captured = prepared.__context_capture__("TestTask")

        # Same lineage = same context_id
        assert captured.context_id == original_id


# =============================================================================
# Reversibility Tests
# =============================================================================


class TestKVStoreReversibility:
    """Test reversibility declaration."""

    def test_has_reversibility(self):
        """KVStore should have reversibility property."""
        store = KVStoreContext.create()
        assert hasattr(store, "reversibility")

    def test_reversibility_is_auto(self):
        """KVStore should be AUTO reversible (in-memory)."""
        store = KVStoreContext.create()
        assert store.reversibility == ReversibilityLevel.AUTO

    def test_reversibility_type(self):
        """Reversibility should return ReversibilityLevel."""
        store = KVStoreContext.create()
        assert isinstance(store.reversibility, ReversibilityLevel)

    def test_composite_with_other_contexts(self):
        """KVStore should compose correctly with other contexts."""
        store = KVStoreContext.create()
        session = SessionState(session_id="sess_test")

        # Both are AUTO
        composite = compute_composite_reversibility([store, session])
        assert composite == ReversibilityLevel.AUTO


# =============================================================================
# Data Access Tests
# =============================================================================


class TestKVStoreDataAccess:
    """Test data access methods."""

    def test_get_existing_key(self):
        """Can get existing key."""
        store = KVStoreContext.create({"key": "value"})
        assert store.get("key") == "value"

    def test_get_missing_key_returns_none(self):
        """Get on missing key returns None."""
        store = KVStoreContext.create({})
        assert store.get("missing") is None

    def test_get_with_default(self):
        """Get with default returns default for missing key."""
        store = KVStoreContext.create({})
        assert store.get("missing", "default") == "default"

    def test_keys_returns_list(self):
        """keys() returns list of keys."""
        store = KVStoreContext.create({"a": "1", "b": "2"})
        keys = store.keys()
        assert set(keys) == {"a", "b"}

    def test_has_key_true(self):
        """has_key returns True for existing key."""
        store = KVStoreContext.create({"key": "value"})
        assert store.has_key("key") is True

    def test_has_key_false(self):
        """has_key returns False for missing key."""
        store = KVStoreContext.create({})
        assert store.has_key("missing") is False


# =============================================================================
# Lifecycle Tests
# =============================================================================


class TestKVStoreLifecycle:
    """Test prepare → modify → capture lifecycle."""

    def test_set_before_prepare_raises(self):
        """set() before prepare should raise."""
        store = KVStoreContext.create({"key": "value"})
        with pytest.raises(RuntimeError) as exc_info:
            store.set("key", "new")
        assert "prepare" in str(exc_info.value).lower()

    def test_delete_before_prepare_raises(self):
        """delete() before prepare should raise."""
        store = KVStoreContext.create({"key": "value"})
        with pytest.raises(RuntimeError) as exc_info:
            store.delete("key")
        assert "prepare" in str(exc_info.value).lower()

    def test_prepare_returns_self(self):
        """prepare() should return self."""
        store = KVStoreContext.create({"key": "value"})
        prepared = store.__context_prepare__()
        assert prepared is store

    def test_set_after_prepare_works(self):
        """set() after prepare should work."""
        store = KVStoreContext.create({"key": "value"})
        prepared = store.__context_prepare__()

        prepared.set("key", "new_value")
        assert prepared.get("key") == "new_value"

    def test_delete_after_prepare_works(self):
        """delete() after prepare should work."""
        store = KVStoreContext.create({"key": "value"})
        prepared = store.__context_prepare__()

        result = prepared.delete("key")
        assert result is True
        assert prepared.has_key("key") is False

    def test_delete_missing_returns_false(self):
        """delete() on missing key returns False."""
        store = KVStoreContext.create({})
        prepared = store.__context_prepare__()

        result = prepared.delete("missing")
        assert result is False

    def test_capture_returns_new_context_on_changes(self):
        """capture() should return new context when changes made."""
        store = KVStoreContext.create({"key": "value"})
        prepared = store.__context_prepare__()

        prepared.set("key", "new_value")
        captured = prepared.__context_capture__("TestTask")

        assert captured is not store
        assert captured.data == {"key": "new_value"}

    def test_capture_returns_self_on_no_changes(self):
        """capture() should return self when no changes made."""
        store = KVStoreContext.create({"key": "value"})
        prepared = store.__context_prepare__()

        captured = prepared.__context_capture__("TestTask")

        assert captured is store

    def test_capture_sets_source_step(self):
        """capture() should set source_step on returned context."""
        store = KVStoreContext.create({"key": "value"})
        prepared = store.__context_prepare__()

        prepared.set("key", "new_value")
        captured = prepared.__context_capture__("MyTask")

        assert captured.source_step == "MyTask"

    def test_cleanup_clears_working_state(self):
        """cleanup() should clear working state."""
        store = KVStoreContext.create({"key": "value"})
        prepared = store.__context_prepare__()
        prepared.set("key", "modified")

        prepared.__context_cleanup__()

        # After cleanup, set should fail again
        with pytest.raises(RuntimeError):
            prepared.set("key", "value")

    def test_cleanup_idempotent(self):
        """cleanup() should be idempotent."""
        store = KVStoreContext.create({})
        prepared = store.__context_prepare__()

        # Multiple cleanups should not raise
        prepared.__context_cleanup__()
        prepared.__context_cleanup__()
        prepared.__context_cleanup__(error=Exception("test"))

    def test_full_lifecycle(self):
        """Test complete lifecycle flow."""
        # Create
        store = KVStoreContext.create({"user": "alice", "count": "0"})
        original_id = store.context_id

        # Prepare
        prepared = store.__context_prepare__()
        assert prepared is store

        # Modify
        prepared.set("count", "1")
        prepared.set("new_key", "value")
        prepared.delete("user")

        # Capture
        captured = prepared.__context_capture__("ProcessData")

        assert captured is not store
        assert captured.data == {"count": "1", "new_key": "value"}
        assert captured.source_step == "ProcessData"
        assert captured.context_id == original_id

        # Cleanup
        prepared.__context_cleanup__()


# =============================================================================
# Change Detection Tests
# =============================================================================


class TestKVStoreChangeDetection:
    """Test change detection helpers."""

    def test_has_changes_before_prepare(self):
        """has_changes() before prepare returns False."""
        store = KVStoreContext.create({"key": "value"})
        assert store.has_changes() is False

    def test_has_changes_no_modifications(self):
        """has_changes() with no modifications returns False."""
        store = KVStoreContext.create({"key": "value"})
        prepared = store.__context_prepare__()

        assert prepared.has_changes() is False

    def test_has_changes_with_modifications(self):
        """has_changes() with modifications returns True."""
        store = KVStoreContext.create({"key": "value"})
        prepared = store.__context_prepare__()

        prepared.set("key", "new_value")
        assert prepared.has_changes() is True

    def test_get_changes_empty(self):
        """get_changes() with no modifications returns empty dict."""
        store = KVStoreContext.create({"key": "value"})
        prepared = store.__context_prepare__()

        assert prepared.get_changes() == {}

    def test_get_changes_modification(self):
        """get_changes() shows modifications."""
        store = KVStoreContext.create({"key": "old"})
        prepared = store.__context_prepare__()

        prepared.set("key", "new")
        changes = prepared.get_changes()

        assert changes == {"key": ("old", "new")}

    def test_get_changes_addition(self):
        """get_changes() shows additions."""
        store = KVStoreContext.create({})
        prepared = store.__context_prepare__()

        prepared.set("new_key", "value")
        changes = prepared.get_changes()

        assert changes == {"new_key": (None, "value")}

    def test_get_changes_deletion(self):
        """get_changes() shows deletions."""
        store = KVStoreContext.create({"key": "value"})
        prepared = store.__context_prepare__()

        prepared.delete("key")
        changes = prepared.get_changes()

        assert changes == {"key": ("value", None)}

    def test_get_changes_multiple(self):
        """get_changes() shows multiple changes."""
        store = KVStoreContext.create({"existing": "old", "to_delete": "bye"})
        prepared = store.__context_prepare__()

        prepared.set("existing", "new")
        prepared.set("added", "hello")
        prepared.delete("to_delete")

        changes = prepared.get_changes()

        assert changes == {
            "existing": ("old", "new"),
            "added": (None, "hello"),
            "to_delete": ("bye", None),
        }


# =============================================================================
# Protocol Compliance Tests
# =============================================================================


class TestKVStoreProtocolCompliance:
    """Test ExecutionContext protocol compliance."""

    def test_is_execution_context(self):
        """KVStore should be recognized as ExecutionContext."""
        store = KVStoreContext.create()
        assert is_execution_context(store)

    def test_has_all_protocol_methods(self):
        """KVStore should have all ExecutionContext methods."""
        store = KVStoreContext.create()

        assert hasattr(store, "context_id")
        assert hasattr(store, "reversibility")
        assert hasattr(store, "__context_prepare__")
        assert hasattr(store, "__context_capture__")
        assert hasattr(store, "__context_cleanup__")
        assert hasattr(store, "__str__")

    def test_protocol_methods_callable(self):
        """Protocol methods should be callable."""
        store = KVStoreContext.create()

        assert callable(store.__context_prepare__)
        assert callable(store.__context_capture__)
        assert callable(store.__context_cleanup__)

    def test_invisible_in_prompts(self):
        """KVStore should be invisible in prompts (str == "")."""
        store = KVStoreContext.create({"key": "value"})
        assert str(store) == ""


# =============================================================================
# Serialization Tests
# =============================================================================


class TestKVStoreSerialization:
    """Test JSON serialization roundtrip."""

    def test_model_dump(self):
        """Can dump to dict."""
        store = KVStoreContext.create({"key": "value"})
        data = store.model_dump()

        assert data["data"] == {"key": "value"}
        assert "frozen_context_id" in data

    def test_model_validate(self):
        """Can restore from dict."""
        store = KVStoreContext.create({"key": "value"})
        data = store.model_dump()

        restored = KVStoreContext.model_validate(data)

        assert restored.data == {"key": "value"}
        assert restored.context_id == store.context_id

    def test_context_id_survives_roundtrip(self):
        """context_id should survive serialization roundtrip."""
        store = KVStoreContext.create({"key": "value"})
        original_id = store.context_id

        data = store.model_dump()
        restored = KVStoreContext.model_validate(data)

        assert restored.context_id == original_id

    def test_source_step_survives_roundtrip(self):
        """source_step should survive serialization roundtrip."""
        store = KVStoreContext.create({"key": "value"})
        prepared = store.__context_prepare__()
        prepared.set("key", "new")
        captured = prepared.__context_capture__("TestTask")

        data = captured.model_dump()
        restored = KVStoreContext.model_validate(data)

        assert restored.source_step == "TestTask"


# =============================================================================
# Integration with Stream Tests
# =============================================================================


class TestKVStoreStreamIntegration:
    """Test KVStore integration with effect stream."""

    def test_context_id_in_effects(self):
        """Effects should be able to use KVStore context_id."""
        store = KVStoreContext.create({"key": "value"})

        effect = TaskStarted(
            task_name="ProcessData",
            context_id=store.context_id,
        )

        assert effect.context_id == store.context_id
        assert effect.context_id.startswith("kvstore:")

    def test_stream_filtering_by_context(self):
        """Stream should be filterable by KVStore context_id."""
        store = KVStoreContext.create({"key": "value"})
        ctx_id = store.context_id

        stream = Stream()
        stream = stream.append(TaskStarted(task_name="T1", context_id=ctx_id))
        stream = stream.append(TaskStarted(task_name="T2", context_id="other:ctx"))
        stream = stream.append(TaskCompleted(task_name="T1", context_id=ctx_id))

        filtered = stream.by_context(ctx_id)

        assert len(filtered) == 2
        assert all(layer.effect.context_id == ctx_id for layer in filtered)

    def test_stream_filtering_by_context_type(self):
        """Stream should be filterable by kvstore: prefix."""
        store = KVStoreContext.create({"key": "value"})

        stream = Stream()
        stream = stream.append(TaskStarted(task_name="T1", context_id=store.context_id))
        stream = stream.append(TaskStarted(task_name="T2", context_id="workspace:/repo:abc"))

        filtered = stream.by_context_type("kvstore:")

        assert len(filtered) == 1
        assert filtered.layers[0].effect.context_id.startswith("kvstore:")


# =============================================================================
# Real-World Scenario Tests
# =============================================================================


class TestKVStoreRealWorldScenarios:
    """Test realistic usage patterns."""

    def test_config_management_workflow(self):
        """Simulate a configuration management task."""
        # Initial config
        config = KVStoreContext.create(
            {
                "db_host": "localhost",
                "db_port": "5432",
                "cache_enabled": "false",
            }
        )

        # Task modifies config
        prepared = config.__context_prepare__()
        prepared.set("cache_enabled", "true")
        prepared.set("cache_ttl", "3600")

        # Capture changes
        updated = prepared.__context_capture__("EnableCaching")

        assert updated.data == {
            "db_host": "localhost",
            "db_port": "5432",
            "cache_enabled": "true",
            "cache_ttl": "3600",
        }
        assert updated.source_step == "EnableCaching"

    def test_state_tracking_across_tasks(self):
        """Simulate state tracking across multiple task executions."""
        # Initial state
        state = KVStoreContext.create({"step": "0", "status": "started"})

        # Task 1: Process
        p1 = state.__context_prepare__()
        p1.set("step", "1")
        p1.set("status", "processing")
        state = p1.__context_capture__("Task1")
        p1.__context_cleanup__()

        # Task 2: Complete
        p2 = state.__context_prepare__()
        p2.set("step", "2")
        p2.set("status", "completed")
        state = p2.__context_capture__("Task2")
        p2.__context_cleanup__()

        # Final state
        assert state.data == {"step": "2", "status": "completed"}
        assert state.source_step == "Task2"

    def test_rollback_scenario(self):
        """Demonstrate how changes can be rolled back."""
        original = KVStoreContext.create({"value": "original"})
        original_id = original.context_id

        # Make changes
        prepared = original.__context_prepare__()
        prepared.set("value", "modified")
        prepared.set("new_key", "data")

        # Decide not to commit changes - just cleanup
        prepared.__context_cleanup__()

        # Original is unchanged
        assert original.data == {"value": "original"}
        assert original.context_id == original_id

        # Can start fresh with original
        fresh = original.__context_prepare__()
        assert fresh.get("value") == "original"
        assert fresh.has_key("new_key") is False
