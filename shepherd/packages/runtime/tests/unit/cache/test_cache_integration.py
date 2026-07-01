"""Integration tests for cache flow with task execution."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel
from shepherd_runtime.cache import CacheHit, CacheStored
from shepherd_runtime.scope import Scope
from shepherd_runtime.task.authoring import Input, Output, TaskRef, task
from shepherd_runtime.task.output import TaskRefReconstructionPolicy
from shepherd_tests import MockProvider
from shepherd_transform.source import extract_task_source, reconstruct_task_class

if TYPE_CHECKING:
    from pathlib import Path

# --- Test Tasks ---


@task
class SimpleTask(BaseModel):
    """A simple test task with one input and one output."""

    text: Input(str)
    result: Output(str)


@task(cacheable=False)
class NonCacheableTask(BaseModel):
    """A task that should never be cached."""

    input_val: Input(str)
    output_val: Output(str)


@task
class TransformTask(BaseModel):
    """A cacheable task that emits another task definition."""

    transformed: Output(TaskRef)


@task
class BatchTransformTask(BaseModel):
    """A cacheable task that emits multiple task definitions."""

    transformed: Output(list[TaskRef])


@task
class AnalyzeTask(BaseModel):
    """A cacheable task that consumes a task class."""

    target: Input(TaskRef)
    summary: Output(str)


# --- Tests ---


class TestCacheHitMiss:
    """Test cache hit and miss behavior."""

    def test_cache_miss_on_first_execution(self, tmp_path: Path):
        """First execution should be a cache miss."""
        with Scope(root=True, project_path=tmp_path) as scope:
            scope.register_provider("default", MockProvider(), default=True)

            result = SimpleTask(text="hello")

            # Check for CacheMiss or no CacheHit
            cache_hits = list(scope.effects.query(CacheHit))
            assert len(cache_hits) == 0

    def test_non_cacheable_task_never_cached(self, tmp_path: Path):
        """Tasks with cacheable=False should never be cached."""
        with Scope(root=True, project_path=tmp_path) as scope:
            scope.register_provider("default", MockProvider(), default=True)

            # Initialize cache store
            cache_store = scope._get_cache_store()
            if cache_store:
                initial_count = cache_store.stats().entry_count

                # Execute non-cacheable task
                result = NonCacheableTask(input_val="test")

                # Should not have stored anything
                final_count = cache_store.stats().entry_count
                assert final_count == initial_count


class TestCacheableDecorator:
    """Test the cacheable decorator parameter."""

    def test_cacheable_true_by_default(self):
        """Tasks should be cacheable by default."""

        @task
        class DefaultTask(BaseModel):
            input: Input(str)
            output: Output(str)

        meta = DefaultTask._task_meta
        assert meta.cacheable is True

    def test_cacheable_false_explicit(self):
        """Tasks can explicitly opt out of caching."""

        @task(cacheable=False)
        class NoCacheTask(BaseModel):
            input: Input(str)
            output: Output(str)

        meta = NoCacheTask._task_meta
        assert meta.cacheable is False

    def test_cacheable_with_guidance(self):
        """Cacheable can be combined with guidance."""

        @task(guidance="Be helpful", cacheable=False)
        class GuidedNoCacheTask(BaseModel):
            input: Input(str)
            output: Output(str)

        meta = GuidedNoCacheTask._task_meta
        assert meta.cacheable is False
        assert meta.guidance == "Be helpful"

    def test_explicit_cls_with_kwargs(self):
        """Programmatic decoration with explicit __cls and kwargs should work.

        This tests the edge case where task() is called programmatically with
        both the class and keyword arguments. Previously, kwargs were silently
        ignored when __cls was provided.
        """

        class RawTask(BaseModel):
            input: Input(str)
            output: Output(str)

        # Programmatic decoration with explicit cls (positional) and kwargs
        DecoratedTask = task(RawTask, cacheable=False, guidance="Be concise")

        meta = DecoratedTask._task_meta
        assert meta.cacheable is False, "cacheable=False should not be ignored"
        assert meta.guidance == "Be concise", "guidance should not be ignored"


class TestCacheEffects:
    """Test cache-related effect emission."""

    def test_cache_hit_effect_has_correct_fields(self):
        """CacheHit effect should have all required fields."""
        effect = CacheHit(
            task_name="TestTask",
            execution_key="abc123",
            cache_mode="outputs_only",
            created_at="2024-01-01T00:00:00",
            age_seconds=3600.0,
        )

        assert effect.effect_type == "cache_hit"
        assert effect.task_name == "TestTask"
        assert effect.execution_key == "abc123"
        assert effect.cache_mode == "outputs_only"
        assert effect.age_seconds == 3600.0

    def test_cache_stored_effect_has_correct_fields(self):
        """CacheStored effect should have all required fields."""
        effect = CacheStored(
            task_name="TestTask",
            execution_key="abc123",
            cache_mode="outputs_only",
            size_bytes=1024,
        )

        assert effect.effect_type == "cache_stored"
        assert effect.task_name == "TestTask"
        assert effect.execution_key == "abc123"
        assert effect.size_bytes == 1024


class TestCacheWithScope:
    """Test cache interaction with scope."""

    def test_cache_property_returns_store(self, tmp_path: Path):
        """Scope.cache should return the cache store."""
        with Scope(root=True, project_path=tmp_path) as scope:
            scope.register_provider("default", MockProvider(), default=True)

            # Initialize by getting store
            store = scope._get_cache_store()
            assert store is not None

            # cache property should return same store
            assert scope.cache is store

    def test_cache_none_without_project_path(self):
        """Scope.cache should return None without project_path."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)

            assert scope.cache is None

    def test_child_scope_delegates_to_parent(self, tmp_path: Path):
        """Child scope should delegate cache to parent."""
        with Scope(root=True, project_path=tmp_path) as parent:
            parent.register_provider("default", MockProvider(), default=True)
            parent_cache = parent._get_cache_store()

            child = parent.child()
            child_cache = child._get_cache_store()

            assert child_cache is parent_cache

    def test_cache_invalidation_via_scope(self, tmp_path: Path):
        """Should be able to invalidate cache via scope.cache."""
        with Scope(root=True, project_path=tmp_path) as scope:
            scope.register_provider("default", MockProvider(), default=True)

            cache = scope._get_cache_store()
            if cache:
                # Add some entries
                from shepherd_runtime.cache import CachedOutputs

                cache.put("key1", CachedOutputs(outputs={"r": 1}, task_name="Task"))
                cache.put("key2", CachedOutputs(outputs={"r": 2}, task_name="Task"))

                assert cache.stats().entry_count == 2

                # Invalidate all
                scope.cache.invalidate()

                assert cache.stats().entry_count == 0


class TestCacheIsolation:
    """Test that caches are isolated by project."""

    def test_different_projects_have_different_caches(self, tmp_path: Path):
        """Different project paths should have isolated caches."""
        project1 = tmp_path / "project1"
        project2 = tmp_path / "project2"
        project1.mkdir()
        project2.mkdir()

        with Scope(root=True, project_path=project1) as scope1:
            cache1 = scope1._get_cache_store()
            if cache1:
                from shepherd_runtime.cache import CachedOutputs

                cache1.put("shared_key", CachedOutputs(outputs={"from": "project1"}, task_name="Task"))

        with Scope(root=True, project_path=project2) as scope2:
            cache2 = scope2._get_cache_store()
            if cache2:
                # Should not find the key from project1
                result = cache2.get("shared_key")
                assert result is None


# =============================================================================
# End-to-End Cache Hit Test
# =============================================================================
#
# This test verifies the FULL cache path through task execution:
# 1. Task executes -> CacheStored effect emitted
# 2. Same task executes again -> CacheHit effect emitted
# 3. Output values are identical
#
# This test will FAIL until the following issues are fixed:
# - Bug #3: Wrong import paths in _mixin.py (from .cache -> from shepherd_runtime.cache)
# - Bug #4: Missing _get_cache_config() on ScopeProxy
# - Bug #5: Datetime timezone mismatch in age calculation
#
# =============================================================================


class TestEndToEndCacheHit:
    """End-to-end test for cache hit behavior through full task execution.

    Unlike other tests in this file that test cache components directly,
    this test exercises the complete path through _mixin.py's cache logic.

    NOTE: These tests do NOT use mock_tasks() because mock mode bypasses the
    cache code path in _execute_async(). We use MockProvider for fast execution
    while still exercising the full ExecutionLifecycle including caching.
    """

    def test_second_execution_hits_cache(self, tmp_path: Path):
        """Execute same task twice - second should be a cache hit.

        This is the core user-facing behavior: run a task, run it again
        with the same inputs, and the second run should use cached results.
        """
        with Scope(root=True, project_path=tmp_path) as scope:
            scope.register_provider("default", MockProvider(), default=True)

            # First execution - should be cache miss, should store result
            result1 = SimpleTask(text="hello world")

            # Verify CacheStored was emitted (result was cached)
            cache_stored = list(scope.effects.query(CacheStored))
            assert len(cache_stored) == 1, (
                "First execution should emit CacheStored effect. If this fails, caching is not storing results."
            )
            assert cache_stored[0].task_name == "SimpleTask"

            # Verify no CacheHit on first run
            cache_hits_first = list(scope.effects.query(CacheHit))
            assert len(cache_hits_first) == 0, "First execution should not be a cache hit"

            # Second execution with same inputs - should be cache hit
            result2 = SimpleTask(text="hello world")

            # Verify CacheHit was emitted
            cache_hits = list(scope.effects.query(CacheHit))
            assert len(cache_hits) == 1, (
                "Second execution with same inputs should emit CacheHit effect. "
                "If this fails, the cache lookup in _mixin.py is not working."
            )
            assert cache_hits[0].task_name == "SimpleTask"

            # Verify outputs are the same
            assert result1.result == result2.result, "Cached result should match original result"

    def test_different_inputs_no_cache_hit(self, tmp_path: Path):
        """Different inputs should not hit cache."""
        with Scope(root=True, project_path=tmp_path) as scope:
            scope.register_provider("default", MockProvider(), default=True)

            # First execution
            result1 = SimpleTask(text="input one")

            # Second execution with DIFFERENT inputs
            result2 = SimpleTask(text="input two")

            # Should have two CacheStored (both were misses that got stored)
            cache_stored = list(scope.effects.query(CacheStored))
            assert len(cache_stored) == 2, "Both executions with different inputs should store to cache"

            # Should have zero CacheHit (different inputs = different cache keys)
            cache_hits = list(scope.effects.query(CacheHit))
            assert len(cache_hits) == 0, "Different inputs should not produce cache hit"

    def test_cache_hit_skips_execution(self, tmp_path: Path):
        """Cache hit should skip actual LLM execution.

        We verify this by checking that the task's _cache_hit flag is set.
        Note: _cache_hit is a Pydantic PrivateAttr, accessed via __pydantic_private__.
        """

        def get_cache_hit(task_instance) -> bool:
            """Access _cache_hit private attr from Pydantic model."""
            private = object.__getattribute__(task_instance, "__pydantic_private__")
            return private.get("_cache_hit", False) if private else False

        with Scope(root=True, project_path=tmp_path) as scope:
            scope.register_provider("default", MockProvider(), default=True)

            # First execution
            result1 = SimpleTask(text="test input")
            assert get_cache_hit(result1) is False, "First execution should not be cache hit"

            # Second execution - should hit cache
            result2 = SimpleTask(text="test input")
            assert get_cache_hit(result2) is True, (
                "Second execution should have _cache_hit=True. "
                "If this fails, the cache hit path in _mixin.py is not being taken."
            )

    def test_cache_hit_has_age_seconds(self, tmp_path: Path):
        """CacheHit effect should include age_seconds field."""
        with Scope(root=True, project_path=tmp_path) as scope:
            scope.register_provider("default", MockProvider(), default=True)

            # First execution - stores to cache
            SimpleTask(text="age test")

            # Second execution - hits cache
            SimpleTask(text="age test")

            cache_hits = list(scope.effects.query(CacheHit))
            assert len(cache_hits) == 1

            # age_seconds should be a non-negative number
            # (even if nearly zero for immediate re-execution)
            assert cache_hits[0].age_seconds >= 0, (
                "CacheHit should have valid age_seconds. "
                "If this fails with TypeError, there may be a datetime comparison issue."
            )

    def test_taskref_output_is_cached_as_source_and_rehydrated_on_hit(self, tmp_path: Path):
        """Cached TaskRef outputs should survive cache store and cache hit paths."""

        def get_cache_hit(task_instance) -> bool:
            private = object.__getattribute__(task_instance, "__pydantic_private__")
            return private.get("_cache_hit", False) if private else False

        with Scope(root=True, project_path=tmp_path) as scope:
            scope.register_provider("default", MockProvider(), default=True)

            first = TransformTask()
            second = TransformTask()

            assert get_cache_hit(first) is False
            assert get_cache_hit(second) is True
            assert second.transformed is not None
            assert extract_task_source(first.transformed) == extract_task_source(second.transformed)

            cache_stored = list(scope.effects.query(CacheStored))
            cache_hits = list(scope.effects.query(CacheHit))
            assert len(cache_stored) == 1
            assert len(cache_hits) == 1

    def test_list_taskref_output_is_rehydrated_on_cache_hit(self, tmp_path: Path):
        """Cached list[TaskRef] outputs should rehydrate back to task classes."""

        def get_cache_hit(task_instance) -> bool:
            private = object.__getattribute__(task_instance, "__pydantic_private__")
            return private.get("_cache_hit", False) if private else False

        with Scope(root=True, project_path=tmp_path) as scope:
            scope.register_provider("default", MockProvider(), default=True)

            first = BatchTransformTask()
            second = BatchTransformTask()

            assert get_cache_hit(first) is False
            assert get_cache_hit(second) is True
            assert isinstance(second.transformed, list)
            assert len(second.transformed) == 1
            assert isinstance(second.transformed[0], type)
            assert extract_task_source(first.transformed[0]) == extract_task_source(second.transformed[0])

    def test_reconstructed_taskref_inputs_hash_by_source(self, tmp_path: Path):
        """Equivalent reconstructed task inputs should hit the same cache key."""

        def get_cache_hit(task_instance) -> bool:
            private = object.__getattribute__(task_instance, "__pydantic_private__")
            return private.get("_cache_hit", False) if private else False

        source = extract_task_source(SimpleTask)
        first_target = reconstruct_task_class(source)
        second_target = reconstruct_task_class(source)

        with Scope(root=True, project_path=tmp_path) as scope:
            scope.register_provider("default", MockProvider(), default=True)

            first = AnalyzeTask(target=first_target)
            second = AnalyzeTask(target=second_target)

            assert get_cache_hit(first) is False
            assert get_cache_hit(second) is True

            cache_hits = list(scope.effects.query(CacheHit))
            assert len(cache_hits) == 1

    @pytest.mark.asyncio
    async def test_taskref_output_cache_hit_uses_explicit_allowlisted_policy(self, tmp_path: Path):
        """Allowlisted task execution policy should apply on both fresh extraction and cache hit restore."""
        domain_module = tmp_path / "my_domain.py"
        domain_module.write_text("Alias = str\n", encoding="utf-8")
        source = "from my_domain import Alias\n@task\nclass DomainTask(BaseModel):\n    query: Input(Alias)\n    answer: Output(str)"
        provider = MockProvider(
            mock_responses=[
                {"structured": {"transformed": source}},
            ]
        )

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.syspath_prepend(str(tmp_path))

            with Scope(root=True, project_path=tmp_path) as scope:
                scope.register_provider("default", provider, default=True)

                first = await TransformTask.arun(
                    scope=scope,
                    taskref_policy=TaskRefReconstructionPolicy.allowlisted("my_domain"),
                )
                second = await TransformTask.arun(
                    scope=scope,
                    taskref_policy=TaskRefReconstructionPolicy.allowlisted("my_domain"),
                )

                assert extract_task_source(first.transformed) == source
                assert extract_task_source(second.transformed) == source
                cache_hits = list(scope.effects.query(CacheHit))
                assert len(cache_hits) == 1
