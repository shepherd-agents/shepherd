"""Tests for programmatic device execution.

Validates the device routing, TaskSpec construction, ExecutionSpec backward
compatibility, preflight validation, and scaffold delegation for programmatic
(@task with execute()) tasks running through the ExecutionLifecycle.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from shepherd_core.effects import TaskCompleted, TaskStarted
from shepherd_core.errors import TaskExecutionError
from shepherd_core.foundation.protocols.device import (
    DeviceCapabilities,
    EffectBundle,
    ExecutionResult,
    ExecutionSpec,
    SandboxExecutionError,
    TaskSpec,
)
from shepherd_core.types import ProviderCapabilities
from shepherd_runtime.device.container.preflight import preflight_check_spec
from shepherd_runtime.lifecycle import ExecutionLifecycle
from shepherd_runtime.task._mixin import _async_mode
from shepherd_runtime.task.authoring import Input, Output, task

# ---------------------------------------------------------------------------
# Helpers (matching test_dual_path.py patterns)
# ---------------------------------------------------------------------------


def _make_scope() -> MagicMock:
    """Create a mock scope with effect tracking."""
    scope = MagicMock()
    scope.emit = MagicMock()
    scope.update_context = MagicMock()
    scope.mark_binding_lifecycle = MagicMock()
    scope.all_bindings = MagicMock(return_value=[])
    scope.current_device = None
    scope.effects = MagicMock()
    return scope


def _make_provider(provider_id: str = "test-provider") -> MagicMock:
    """Create a mock provider."""
    provider = MagicMock()
    provider.provider_id = provider_id
    provider.capabilities = ProviderCapabilities(provider_type="test")
    provider.validate_binding = MagicMock()
    provider.formatter = MagicMock()
    provider.execute_sdk = AsyncMock(return_value=MagicMock(output_text="Test output", success=True))
    return provider


def _make_mock_device(
    isolation_level: str = "container",
    effect_capture: str = "overlay",
    task_outputs: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a mock device implementing DeviceProtocol."""
    device = MagicMock()

    # Track calls
    device._calls = []

    # capabilities as a property
    caps = DeviceCapabilities(isolation_level=isolation_level, effect_capture=effect_capture)
    type(device).capabilities = PropertyMock(return_value=caps)

    # Sandbox handle
    sandbox = MagicMock()
    sandbox.sandbox_id = "test-sandbox-001"
    sandbox.device_name = "mock-device"

    device.create_sandbox = AsyncMock(return_value=sandbox)

    # Execute returns result with task_outputs
    outputs = task_outputs or {}
    device.execute = AsyncMock(
        return_value=ExecutionResult(
            success=True,
            output_text="",
            metadata={"task_outputs": outputs},
        )
    )

    # Extract effects returns empty bundle
    device.extract_effects = AsyncMock(return_value=EffectBundle(context_effects={}, lifecycle_effects=[]))

    # Cleanup is a no-op
    device.cleanup = AsyncMock()

    return device


@task
class GreetTask:
    name: Input(str) = "world"
    greeting: Output(str) = None

    def execute(self):
        self.greeting = f"hello {self.name}"


def _make_greet_instance(name: str = "world") -> Any:
    """Create a GreetTask instance without triggering auto-execution."""
    token = _async_mode.set(True)
    try:
        instance = GreetTask.model_validate({"name": name})
    finally:
        _async_mode.reset(token)
    return instance


# ===========================================================================
# 1. ExecutionSpec backward compatibility
# ===========================================================================


class TestExecutionSpecBackwardCompat:
    """ExecutionSpec constructs with or without task_spec."""

    def test_without_task_spec(self) -> None:
        """Construct with only prompt + provider_config: task_spec is None."""
        spec = ExecutionSpec(prompt="hello", provider_config={"model": "gpt-4"})
        assert spec.task_spec is None
        assert spec.prompt == "hello"

    def test_with_task_spec(self) -> None:
        """Construct with task_spec: it is set."""
        ts = TaskSpec(
            task_source="class Foo: pass",
            task_class_name="Foo",
            task_imports=(),
            task_inputs={},
            output_fields=(),
            context_fields={},
        )
        spec = ExecutionSpec(prompt="", provider_config={}, task_spec=ts)
        assert spec.task_spec is ts
        assert spec.task_spec.task_class_name == "Foo"


# ===========================================================================
# 2. TaskSpec construction and serialization
# ===========================================================================


class TestTaskSpecSerialization:
    """TaskSpec round-trips through dict conversion."""

    def test_round_trip(self) -> None:
        """Construct TaskSpec with all fields, convert to dict, verify fields."""
        ts = TaskSpec(
            task_source="@task\nclass Greet:\n    pass",
            task_class_name="Greet",
            task_imports=("import os",),
            task_inputs={"name": "world"},
            output_fields=("greeting",),
            context_fields={"workspace": "workspace"},
            is_async=True,
        )
        from dataclasses import asdict

        d = asdict(ts)
        assert d["task_source"] == "@task\nclass Greet:\n    pass"
        assert d["task_class_name"] == "Greet"
        assert d["task_imports"] == ("import os",)
        assert d["task_inputs"] == {"name": "world"}
        assert d["output_fields"] == ("greeting",)
        assert d["context_fields"] == {"workspace": "workspace"}
        assert d["is_async"] is True

        # Reconstruct from dict
        ts2 = TaskSpec(**d)
        assert ts2 == ts


# ===========================================================================
# 3. run_executor() device routing
# ===========================================================================


class TestRunExecutorDeviceRouting:
    """Device routing in run_executor()."""

    @pytest.mark.asyncio
    async def test_device_routing_triggers(self) -> None:
        """When scope.current_device is set and executor is a bound @task method,
        device.execute() is called."""
        scope = _make_scope()
        device = _make_mock_device(task_outputs={"greeting": "hello"})
        scope.current_device = device

        instance = _make_greet_instance()
        async with ExecutionLifecycle(
            scope=scope,
            provider=None,
            executor=instance.execute,
            task_name="GreetTask",
        ) as lifecycle:
            await lifecycle.run_executor()

        device.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_in_process_fallback_no_device(self) -> None:
        """scope.current_device=None: executor runs in-process."""
        scope = _make_scope()
        scope.current_device = None
        emitted: list[Any] = []
        scope.emit = lambda effect: emitted.append(effect)

        called = False

        def my_executor() -> None:
            nonlocal called
            called = True

        async with ExecutionLifecycle(
            scope=scope,
            provider=None,
            executor=my_executor,
            task_name="InProcessTask",
        ) as lifecycle:
            await lifecycle.run_executor()

        assert called
        completed = [e for e in emitted if isinstance(e, TaskCompleted)]
        assert len(completed) == 1

    @pytest.mark.asyncio
    async def test_in_process_fallback_isolation_none(self) -> None:
        """Device with isolation_level='none': executor runs in-process."""
        scope = _make_scope()
        device = _make_mock_device(isolation_level="none")
        scope.current_device = device
        emitted: list[Any] = []
        scope.emit = lambda effect: emitted.append(effect)

        called = False

        def my_executor() -> None:
            nonlocal called
            called = True

        async with ExecutionLifecycle(
            scope=scope,
            provider=None,
            executor=my_executor,
            task_name="NoIsolationTask",
        ) as lifecycle:
            await lifecycle.run_executor()

        assert called
        device.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_in_process_fallback_no_task_source(self) -> None:
        """Executor's class has _task_source=None: runs in-process."""
        scope = _make_scope()
        device = _make_mock_device()
        scope.current_device = device

        instance = _make_greet_instance()
        # Patch _task_source to None on the class
        original_source = instance.__class__._task_source
        try:
            instance.__class__._task_source = None

            async with ExecutionLifecycle(
                scope=scope,
                provider=None,
                executor=instance.execute,
                task_name="NoSourceTask",
            ) as lifecycle:
                await lifecycle.run_executor()

            device.execute.assert_not_called()
        finally:
            instance.__class__._task_source = original_source

    @pytest.mark.asyncio
    async def test_in_process_fallback_non_bound_executor(self) -> None:
        """Lambda (non-bound method) executor: runs in-process."""
        scope = _make_scope()
        device = _make_mock_device()
        scope.current_device = device
        emitted: list[Any] = []
        scope.emit = lambda effect: emitted.append(effect)

        called = False

        def plain_fn() -> None:
            nonlocal called
            called = True

        async with ExecutionLifecycle(
            scope=scope,
            provider=None,
            executor=plain_fn,
            task_name="LambdaTask",
        ) as lifecycle:
            await lifecycle.run_executor()

        assert called
        device.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_device_outputs_deserialized(self) -> None:
        """After device routing, _device_task_outputs has expected values."""
        scope = _make_scope()
        device = _make_mock_device(task_outputs={"greeting": "hello world"})
        scope.current_device = device

        instance = _make_greet_instance()
        async with ExecutionLifecycle(
            scope=scope,
            provider=None,
            executor=instance.execute,
            task_name="GreetTask",
        ) as lifecycle:
            await lifecycle.run_executor()
            assert lifecycle._device_task_outputs == {"greeting": "hello world"}

    @pytest.mark.asyncio
    async def test_effects_emitted_for_device_path(self) -> None:
        """TaskStarted and TaskCompleted are emitted even on device path."""
        scope = _make_scope()
        device = _make_mock_device(task_outputs={"greeting": "hi"})
        scope.current_device = device
        emitted: list[Any] = []
        scope.emit = lambda effect: emitted.append(effect)

        instance = _make_greet_instance()
        async with ExecutionLifecycle(
            scope=scope,
            provider=None,
            executor=instance.execute,
            task_name="GreetTask",
        ) as lifecycle:
            await lifecycle.run_executor()

        started = [e for e in emitted if isinstance(e, TaskStarted)]
        completed = [e for e in emitted if isinstance(e, TaskCompleted)]
        assert len(started) == 1
        assert started[0].task_name == "GreetTask"
        assert len(completed) == 1

    @pytest.mark.asyncio
    async def test_cleanup_called_after_device_error(self) -> None:
        """When device.execute() raises SandboxExecutionError, cleanup() is still called."""
        scope = _make_scope()
        device = _make_mock_device()
        device.execute = AsyncMock(side_effect=SandboxExecutionError("boom", phase="task_execution"))
        scope.current_device = device

        instance = _make_greet_instance()
        async with ExecutionLifecycle(
            scope=scope,
            provider=None,
            executor=instance.execute,
            task_name="GreetTask",
        ) as lifecycle:
            with pytest.raises(TaskExecutionError):
                await lifecycle.run_executor()

        device.cleanup.assert_called_once()


# ===========================================================================
# 4. _build_programmatic_spec
# ===========================================================================


class TestBuildProgrammaticSpec:
    """_build_programmatic_spec returns correct ExecutionSpec."""

    @pytest.mark.asyncio
    async def test_spec_fields(self) -> None:
        """Returned spec has expected prompt, provider_config, and task_spec."""
        scope = _make_scope()
        # We need a lifecycle instance to call _build_programmatic_spec
        instance = _make_greet_instance("world")

        async with ExecutionLifecycle(
            scope=scope,
            provider=None,
            executor=instance.execute,
            task_name="GreetTask",
        ) as lifecycle:
            build_fn = lifecycle._build_programmatic_spec(instance.execute)
            spec = build_fn({})

        assert spec.prompt == ""
        assert spec.provider_config == {}
        assert spec.task_spec is not None
        assert spec.task_spec.task_class_name == "GreetTask"

    @pytest.mark.asyncio
    async def test_task_spec_source(self) -> None:
        """task_spec.task_source contains class source code."""
        scope = _make_scope()
        instance = _make_greet_instance()

        async with ExecutionLifecycle(
            scope=scope,
            provider=None,
            executor=instance.execute,
            task_name="GreetTask",
        ) as lifecycle:
            build_fn = lifecycle._build_programmatic_spec(instance.execute)
            spec = build_fn({})

        assert spec.task_spec is not None
        # Source should contain the class definition
        assert "GreetTask" in spec.task_spec.task_source
        assert "execute" in spec.task_spec.task_source

    @pytest.mark.asyncio
    async def test_task_spec_inputs(self) -> None:
        """task_spec.task_inputs matches serialized inputs."""
        scope = _make_scope()
        instance = _make_greet_instance("world")

        async with ExecutionLifecycle(
            scope=scope,
            provider=None,
            executor=instance.execute,
            task_name="GreetTask",
        ) as lifecycle:
            build_fn = lifecycle._build_programmatic_spec(instance.execute)
            spec = build_fn({})

        assert spec.task_spec is not None
        assert spec.task_spec.task_inputs == {"name": "world"}

    @pytest.mark.asyncio
    async def test_task_spec_output_fields(self) -> None:
        """task_spec.output_fields contains 'greeting'."""
        scope = _make_scope()
        instance = _make_greet_instance()

        async with ExecutionLifecycle(
            scope=scope,
            provider=None,
            executor=instance.execute,
            task_name="GreetTask",
        ) as lifecycle:
            build_fn = lifecycle._build_programmatic_spec(instance.execute)
            spec = build_fn({})

        assert spec.task_spec is not None
        assert "greeting" in spec.task_spec.output_fields

    @pytest.mark.asyncio
    async def test_task_spec_is_async(self) -> None:
        """task_spec.is_async is False for sync execute."""
        scope = _make_scope()
        instance = _make_greet_instance()

        async with ExecutionLifecycle(
            scope=scope,
            provider=None,
            executor=instance.execute,
            task_name="GreetTask",
        ) as lifecycle:
            build_fn = lifecycle._build_programmatic_spec(instance.execute)
            spec = build_fn({})

        assert spec.task_spec is not None
        assert spec.task_spec.is_async is False


# ===========================================================================
# 5. Preflight validation
# ===========================================================================


class TestPreflightValidation:
    """preflight_check_spec validates ExecutionSpec correctly."""

    def test_llm_spec_empty_prompt_error(self) -> None:
        """LLM spec with empty prompt produces error."""
        spec = ExecutionSpec(prompt="", provider_config={})
        result = preflight_check_spec(spec)
        assert not result.is_ok
        assert any("prompt" in e.lower() for e in result.errors)

    def test_llm_spec_valid(self) -> None:
        """LLM spec with valid prompt + provider_config produces no error."""
        spec = ExecutionSpec(
            prompt="Fix the bug",
            provider_config={"model": "gpt-4"},
        )
        result = preflight_check_spec(spec)
        assert result.is_ok

    def test_programmatic_spec_no_error(self) -> None:
        """Programmatic spec (with task_spec) passes even with empty prompt."""
        ts = TaskSpec(
            task_source="class Foo: pass",
            task_class_name="Foo",
            task_imports=(),
            task_inputs={},
            output_fields=(),
            context_fields={},
        )
        spec = ExecutionSpec(prompt="", provider_config={}, task_spec=ts)
        result = preflight_check_spec(spec)
        assert result.is_ok

    def test_programmatic_spec_empty_source_error(self) -> None:
        """Programmatic spec with empty task_source produces error."""
        ts = TaskSpec(
            task_source="",
            task_class_name="Foo",
            task_imports=(),
            task_inputs={},
            output_fields=(),
            context_fields={},
        )
        spec = ExecutionSpec(prompt="", provider_config={}, task_spec=ts)
        result = preflight_check_spec(spec)
        assert not result.is_ok
        assert any("task_source" in e.lower() for e in result.errors)


# ===========================================================================
# 6. Scaffold refactor regression
# ===========================================================================


class TestScaffoldDelegation:
    """Verify _execute_on_device delegates to _execute_on_device_scaffold."""

    @pytest.mark.asyncio
    async def test_execute_on_device_calls_scaffold(self) -> None:
        """_execute_on_device delegates to device.execute (via scaffold)."""
        scope = _make_scope()
        device = _make_mock_device()
        scope.current_device = device

        provider = _make_provider()

        async with ExecutionLifecycle(
            scope=scope,
            provider=provider,
            task_name="LLMTask",
        ) as lifecycle:
            result = await lifecycle._execute_on_device(device, "Hello")

        # The scaffold should have called device.execute
        device.execute.assert_called_once()
        # And device.cleanup should have been called in finally block
        device.cleanup.assert_called_once()
