"""Unit tests for task transform locking."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import BaseModel
from shepherd_runtime.task.authoring import Input, Output, task
from shepherd_transform.transform_lock import (
    LockError,
    TaskTransformLock,
    TransformLock,
    TransformState,
)

# =============================================================================
# Test Task Classes
# =============================================================================


@task
class TestTask(BaseModel):
    """A simple task for testing."""

    __test__ = False

    x: Input(int)
    result: Output(int)


@task
class AnotherTask(BaseModel):
    """Another task for testing."""

    query: Input(str)
    answer: Output(str)


# =============================================================================
# TransformLock Tests
# =============================================================================


class TestTransformLock:
    """Tests for TransformLock dataclass."""

    def test_create_lock(self):
        """Test creating a transform lock."""
        lock = TransformLock(
            task_name="TestTask",
            holder_id="holder_123",
            acquired_at=time.time(),
        )
        assert lock.task_name == "TestTask"
        assert lock.holder_id == "holder_123"
        assert lock.state == TransformState.TRANSFORMING
        assert not lock.is_expired

    def test_lock_expiration(self):
        """Test that locks expire correctly."""
        lock = TransformLock(
            task_name="TestTask",
            holder_id="holder_123",
            acquired_at=time.time() - 100,  # 100 seconds ago
            timeout_seconds=60.0,
        )
        assert lock.is_expired

    def test_lock_not_expired(self):
        """Test that fresh locks are not expired."""
        lock = TransformLock(
            task_name="TestTask",
            holder_id="holder_123",
            acquired_at=time.time(),
            timeout_seconds=60.0,
        )
        assert not lock.is_expired

    def test_held_for_seconds(self):
        """Test held_for_seconds property."""
        lock = TransformLock(
            task_name="TestTask",
            holder_id="holder_123",
            acquired_at=time.time() - 5.0,
        )
        assert 4.9 < lock.held_for_seconds < 6.0


# =============================================================================
# TaskTransformLock Tests
# =============================================================================


class TestTaskTransformLock:
    """Tests for TaskTransformLock registry."""

    def test_register_task(self):
        """Test registering a task."""
        registry = TaskTransformLock()
        registry.register(TestTask, "class TestTask...")

        assert "TestTask" in registry
        assert len(registry) == 1

    def test_register_non_task_raises(self):
        """Test that registering non-task raises ValueError."""
        registry = TaskTransformLock()

        class NotATask:
            pass

        with pytest.raises(ValueError, match="not a @task"):
            registry.register(NotATask, "class NotATask...")

    def test_get_task(self):
        """Test getting a task class."""
        registry = TaskTransformLock()
        registry.register(TestTask, "class TestTask...")

        task_class = registry.get_task("TestTask")
        assert task_class is TestTask

    def test_get_task_not_found(self):
        """Test getting a non-existent task."""
        registry = TaskTransformLock()
        assert registry.get_task("NonExistent") is None

    def test_get_source(self):
        """Test getting task source."""
        registry = TaskTransformLock()
        source = "class TestTask..."
        registry.register(TestTask, source)

        assert registry.get_source("TestTask") == source

    def test_get_snapshot(self):
        """Test getting consistent snapshot."""
        registry = TaskTransformLock()
        source = "class TestTask..."
        registry.register(TestTask, source)

        snapshot = registry.get_snapshot("TestTask")
        assert snapshot is not None
        task_class, task_source = snapshot
        assert task_class is TestTask
        assert task_source == source

    def test_list_tasks(self):
        """Test listing all tasks."""
        registry = TaskTransformLock()
        registry.register(TestTask, "source1")
        registry.register(AnotherTask, "source2")

        tasks = registry.list_tasks()
        assert set(tasks) == {"TestTask", "AnotherTask"}

    def test_unregister(self):
        """Test unregistering a task."""
        registry = TaskTransformLock()
        registry.register(TestTask, "source")

        assert registry.unregister("TestTask")
        assert "TestTask" not in registry
        assert not registry.unregister("TestTask")  # Already removed


class TestTransformLocking:
    """Tests for transform lock operations."""

    def test_acquire_lock(self):
        """Test acquiring a transform lock."""
        registry = TaskTransformLock()
        registry.register(TestTask, "source")

        holder_id = registry.try_acquire_transform_lock("TestTask")
        assert holder_id is not None
        assert registry.is_transforming("TestTask")

    def test_acquire_lock_custom_holder(self):
        """Test acquiring lock with custom holder ID."""
        registry = TaskTransformLock()
        registry.register(TestTask, "source")

        holder_id = registry.try_acquire_transform_lock("TestTask", "my_holder")
        assert holder_id == "my_holder"

    def test_acquire_lock_not_found(self):
        """Test acquiring lock for non-existent task."""
        registry = TaskTransformLock()

        with pytest.raises(LockError, match="not found"):
            registry.try_acquire_transform_lock("NonExistent")

    def test_concurrent_lock_rejected(self):
        """Test that concurrent locks are rejected."""
        registry = TaskTransformLock()
        registry.register(TestTask, "source")

        holder_a = registry.try_acquire_transform_lock("TestTask", "holder_a")

        with pytest.raises(LockError, match="being transformed"):
            registry.try_acquire_transform_lock("TestTask", "holder_b")

        registry.release_transform_lock("TestTask", holder_a)

    def test_release_lock(self):
        """Test releasing a transform lock."""
        registry = TaskTransformLock()
        registry.register(TestTask, "source")

        holder_id = registry.try_acquire_transform_lock("TestTask")
        assert registry.is_transforming("TestTask")

        released = registry.release_transform_lock("TestTask", holder_id)
        assert released
        assert not registry.is_transforming("TestTask")

    def test_release_wrong_holder(self):
        """Test that wrong holder can't release lock."""
        registry = TaskTransformLock()
        registry.register(TestTask, "source")

        registry.try_acquire_transform_lock("TestTask", "holder_a")

        released = registry.release_transform_lock("TestTask", "holder_b")
        assert not released
        assert registry.is_transforming("TestTask")

    def test_commit_transform(self):
        """Test committing a transformation."""
        registry = TaskTransformLock()
        registry.register(TestTask, "original_source")

        holder_id = registry.try_acquire_transform_lock("TestTask")

        committed = registry.commit_transform(
            "TestTask",
            holder_id,
            AnotherTask,  # Using different task class for test
            "new_source",
        )

        assert committed
        assert not registry.is_transforming("TestTask")
        assert registry.get_source("TestTask") == "new_source"

    def test_commit_wrong_holder(self):
        """Test that wrong holder can't commit."""
        registry = TaskTransformLock()
        registry.register(TestTask, "original_source")

        registry.try_acquire_transform_lock("TestTask", "holder_a")

        committed = registry.commit_transform(
            "TestTask",
            "holder_b",
            AnotherTask,
            "new_source",
        )

        assert not committed
        assert registry.get_source("TestTask") == "original_source"

    def test_lock_expiration_recovery(self):
        """Test that expired locks are cleaned up."""
        registry = TaskTransformLock(lock_timeout=0.1)
        registry.register(TestTask, "source")

        registry.try_acquire_transform_lock("TestTask", "crashed_holder")

        # Wait for expiration
        time.sleep(0.2)

        # New holder should be able to acquire
        holder_b = registry.try_acquire_transform_lock("TestTask", "recovery_holder")
        assert holder_b == "recovery_holder"


class TestTransformContext:
    """Tests for transform_context context manager."""

    def test_context_manager_success(self):
        """Test context manager with successful commit."""
        registry = TaskTransformLock()
        registry.register(TestTask, "original")

        with registry.transform_context("TestTask") as holder_id:
            assert registry.is_transforming("TestTask")
            registry.commit_transform("TestTask", holder_id, TestTask, "new")

        assert not registry.is_transforming("TestTask")
        assert registry.get_source("TestTask") == "new"

    def test_context_manager_rollback_on_error(self):
        """Test context manager releases lock on error."""
        registry = TaskTransformLock()
        registry.register(TestTask, "original")

        with pytest.raises(ValueError), registry.transform_context("TestTask"):
            assert registry.is_transforming("TestTask")
            raise ValueError("Simulated error")

        assert not registry.is_transforming("TestTask")
        assert registry.get_source("TestTask") == "original"

    def test_context_manager_rollback_without_commit(self):
        """Test context manager releases lock if not committed."""
        registry = TaskTransformLock()
        registry.register(TestTask, "original")

        with registry.transform_context("TestTask"):
            # Don't commit
            pass

        assert not registry.is_transforming("TestTask")
        assert registry.get_source("TestTask") == "original"


class TestThreadSafety:
    """Tests for thread safety."""

    def test_threaded_contention(self):
        """Test multiple threads competing for lock."""
        registry = TaskTransformLock()
        registry.register(TestTask, "source")

        results = {"acquired": [], "rejected": []}
        lock = threading.Lock()

        def try_transform(thread_id: int):
            try:
                holder_id = registry.try_acquire_transform_lock("TestTask", f"thread_{thread_id}")
                with lock:
                    results["acquired"].append(thread_id)
                time.sleep(0.05)
                registry.release_transform_lock("TestTask", holder_id)
            except LockError:
                with lock:
                    results["rejected"].append(thread_id)

        threads = [threading.Thread(target=try_transform, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results["acquired"]) == 1
        assert len(results["rejected"]) == 4

    def test_executor_pattern(self):
        """Test with ThreadPoolExecutor."""
        registry = TaskTransformLock()
        registry.register(TestTask, "source")

        results = []
        results_lock = threading.Lock()

        def transform_task(transform_id: int):
            try:
                with registry.transform_context("TestTask") as holder_id:
                    time.sleep(0.05)
                    registry.commit_transform("TestTask", holder_id, TestTask, f"source_{transform_id}")
                    with results_lock:
                        results.append(("success", transform_id))
            except LockError:
                with results_lock:
                    results.append(("rejected", transform_id))

        with ThreadPoolExecutor(max_workers=3) as executor:
            list(executor.map(transform_task, range(3)))

        successes = sum(1 for r, _ in results if r == "success")
        rejections = sum(1 for r, _ in results if r == "rejected")

        assert successes == 1
        assert rejections == 2
