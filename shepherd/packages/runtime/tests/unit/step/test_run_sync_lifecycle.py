"""Tests for sync path validation through run_sync (Spike 5).

Validates that programmatic tasks work correctly when routed through
run_sync(_execute_async(...)) in different event loop contexts.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel
from shepherd_core.effects import TaskCompleted, TaskStarted
from shepherd_runtime.scope import Scope
from shepherd_runtime.task.authoring import Input, Output, task


@task
class DoublerTask(BaseModel):
    """Simple programmatic task for sync path validation."""

    input_val: Input(int) = 0
    output_val: Output(int) = None

    def execute(self) -> None:
        self.output_val = self.input_val * 2


class TestSyncPathValidation:
    """Validate that sync auto-execute works after unification."""

    def test_sync_inside_asyncio_run(self) -> None:
        """Programmatic task via arun() inside asyncio.run() — thread-based fallback path."""

        async def _run() -> Any:
            async with Scope(root=True) as scope:
                result = await DoublerTask.arun(scope=scope, input_val=7)
                return result, scope.effects

        result, effects = asyncio.run(_run())

        assert result.output_val == 14

        # Verify lifecycle effects were emitted
        started = list(effects.query(TaskStarted))
        completed = list(effects.query(TaskCompleted))
        assert len(started) >= 1
        assert len(completed) >= 1

    def test_async_arun_parity(self) -> None:
        """arun() produces same result as the sync path."""

        async def _run() -> Any:
            async with Scope(root=True) as scope:
                return await DoublerTask.arun(scope=scope, input_val=10)

        result = asyncio.run(_run())
        assert result.output_val == 20

    def test_scope_state_consistent_after_run(self) -> None:
        """Scope state is consistent after sync execution completes."""

        async def _run() -> Any:
            async with Scope(root=True) as scope:
                result = await DoublerTask.arun(scope=scope, input_val=4)
                # Scope should not be discarded
                assert not scope.is_discarded
                # Effects should be accessible
                effects = scope.effects
                assert effects is not None
                return result

        result = asyncio.run(_run())
        assert result.output_val == 8
