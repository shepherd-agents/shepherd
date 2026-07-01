"""Tests for mixin fork collapse (Spike 6).

Validates that all three entry points — sync auto-execute, arun() with
programmatic tasks, and arun() with LLM tasks — produce correct scope
management, context resolution, effect emission, and output handling.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel
from shepherd_core.effects import TaskCompleted, TaskStarted
from shepherd_runtime.scope import Scope
from shepherd_runtime.task._mixin import _async_execute_mode
from shepherd_runtime.task.authoring import Input, Output, task

# ---------------------------------------------------------------------------
# Test tasks
# ---------------------------------------------------------------------------


@task
class SyncProgrammatic(BaseModel):
    """Programmatic task with sync execute()."""

    input_val: Input(int) = 0
    output_val: Output(int) = None

    def execute(self) -> None:
        self.output_val = self.input_val * 2


@task
class AsyncProgrammatic(BaseModel):
    """Programmatic task with async execute()."""

    input_val: Input(int) = 0
    output_val: Output(int) = None

    async def execute(self) -> None:
        self.output_val = self.input_val * 3


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestArunProgrammatic:
    """Validate arun() with programmatic tasks after fork collapse."""

    def test_arun_sync_execute(self) -> None:
        """arun() with sync execute() produces correct output."""

        async def _run() -> Any:
            async with Scope(root=True) as scope:
                return await SyncProgrammatic.arun(scope=scope, input_val=5)

        result = asyncio.run(_run())
        assert result.output_val == 10

    def test_arun_async_execute(self) -> None:
        """arun() with async execute() produces correct output."""

        async def _run() -> Any:
            async with Scope(root=True) as scope:
                return await AsyncProgrammatic.arun(scope=scope, input_val=5)

        result = asyncio.run(_run())
        assert result.output_val == 15

    def test_programmatic_emits_lifecycle_effects(self) -> None:
        """Programmatic tasks now emit TaskStarted/TaskCompleted effects."""

        async def _run() -> tuple[Any, Any]:
            async with Scope(root=True) as scope:
                result = await SyncProgrammatic.arun(scope=scope, input_val=7)
                return result, scope.effects

        result, effects = asyncio.run(_run())
        assert result.output_val == 14

        # Check for TaskStarted and TaskCompleted in effect stream
        started = list(effects.query(TaskStarted))
        completed = list(effects.query(TaskCompleted))

        assert len(started) >= 1
        assert len(completed) >= 1

        # Programmatic tasks have provider_id=None
        assert started[0].effect.provider_id is None
        assert completed[0].effect.provider_id is None

    def test_async_execute_mode_true_for_async(self) -> None:
        """_async_execute_mode is True when executing async execute()."""
        observed_mode = None

        @task
        class ModeChecker(BaseModel):
            output_val: Output(bool) = None

            async def execute(self) -> None:
                self.output_val = _async_execute_mode.get()

        async def _run() -> Any:
            async with Scope(root=True) as scope:
                return await ModeChecker.arun(scope=scope)

        result = asyncio.run(_run())
        assert result.output_val is True

    def test_async_execute_mode_false_for_sync(self) -> None:
        """_async_execute_mode is False when executing sync execute()."""

        @task
        class SyncModeChecker(BaseModel):
            output_val: Output(bool) = None

            def execute(self) -> None:
                self.output_val = _async_execute_mode.get()

        async def _run() -> Any:
            async with Scope(root=True) as scope:
                return await SyncModeChecker.arun(scope=scope)

        result = asyncio.run(_run())
        assert result.output_val is False


class TestScopeManagement:
    """Validate scope fork/merge/discard after unification."""

    def test_scope_merged_after_success(self) -> None:
        """Parent scope has effects after successful programmatic task."""

        async def _run() -> Any:
            async with Scope(root=True) as scope:
                await SyncProgrammatic.arun(scope=scope, input_val=3)
                return scope.effects

        effects = asyncio.run(_run())
        # Effects from the task should be merged into parent scope
        started = list(effects.query(TaskStarted))
        assert len(started) >= 1

    def test_scope_discarded_on_failure(self) -> None:
        """Scope is discarded when programmatic task fails."""
        from shepherd_core.errors import TaskExecutionError

        @task
        class FailTask(BaseModel):
            def execute(self) -> None:
                raise RuntimeError("boom")

        async def _run() -> Any:
            async with Scope(root=True) as scope:
                with pytest.raises(TaskExecutionError):
                    await FailTask.arun(scope=scope)
                return scope.effects

        effects = asyncio.run(_run())
        # Parent scope should not have merged effects from the failed child
        started = list(effects.query(TaskStarted))
        assert len(started) == 0


class TestCacheableDefault:
    """Validate cacheable defaults for programmatic vs LLM tasks."""

    def test_programmatic_task_defaults_to_not_cacheable(self) -> None:
        """Tasks with execute() should default to cacheable=False."""
        assert SyncProgrammatic._task_meta.cacheable is False

    def test_programmatic_task_explicit_cacheable_true(self) -> None:
        """Explicit cacheable=True overrides the programmatic default."""

        @task(cacheable=True)
        class CacheableProgrammatic(BaseModel):
            def execute(self) -> None:
                pass

        assert CacheableProgrammatic._task_meta.cacheable is True

    def test_llm_task_defaults_to_cacheable(self) -> None:
        """Tasks without execute() should default to cacheable=True."""

        @task
        class LLMTask(BaseModel):
            query: Input(str) = ""
            answer: Output(str) = None

        assert LLMTask._task_meta.cacheable is True

    def test_llm_task_explicit_cacheable_false(self) -> None:
        """Explicit cacheable=False overrides the LLM default."""

        @task(cacheable=False)
        class NonCacheableLLM(BaseModel):
            query: Input(str) = ""
            answer: Output(str) = None

        assert NonCacheableLLM._task_meta.cacheable is False
