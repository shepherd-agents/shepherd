"""Tests for run() / run_sync() convenience functions."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field
from shepherd_core.run import run, run_sync
from shepherd_runtime.scope import Scope
from shepherd_runtime.task.authoring import Input, Output, task
from shepherd_tests import MockProvider


@task
class GreetTask(BaseModel):
    """Greet someone by name."""

    name: Input(str) = Field(description="Name to greet")
    greeting: Output(str) = Field(default="")

    def execute(self) -> None:
        self.greeting = f"Hello, {self.name}!"


class TestRun:
    """Tests for the async run() function."""

    @pytest.mark.asyncio
    async def test_run_creates_scope_and_executes(self) -> None:
        provider = MockProvider()
        result = await run(GreetTask, provider=provider, name="World")
        assert result.greeting == "Hello, World!"

    @pytest.mark.asyncio
    async def test_run_with_existing_scope(self) -> None:
        provider = MockProvider()
        async with Scope(root=True) as scope:
            scope.register_provider("default", provider, default=True)
            result = await run(GreetTask, provider=provider, scope=scope, name="Scope")
            assert result.greeting == "Hello, Scope!"


class TestRunSync:
    """Tests for the sync run_sync() function."""

    def test_run_sync_creates_scope_and_executes(self) -> None:
        provider = MockProvider()
        result = run_sync(GreetTask, provider=provider, name="Sync")
        assert result.greeting == "Hello, Sync!"
