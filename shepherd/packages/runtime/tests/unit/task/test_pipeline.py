"""Tests for pipeline task primitives: run_stage, OnError, stage effects, stages registry."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel
from shepherd_core.context.kernel import ExecutionContextDefaults
from shepherd_core.effects.effects import (
    StageCompleted,
    StageFailed,
    StageSkipped,
    StageStarted,
)
from shepherd_core.errors import TaskExecutionError
from shepherd_core.types import ReversibilityLevel
from shepherd_runtime.scope import Scope
from shepherd_runtime.task.authoring import Context, Input, Output, task
from shepherd_runtime.task.pipeline import OnError, Stage

# =============================================================================
# Test Contexts
# =============================================================================


@dataclass
class SimpleContext(ExecutionContextDefaults):
    value: str

    @property
    def context_id(self) -> str:
        return f"simple:{self.value[:20]}"

    @property
    def reversibility(self) -> ReversibilityLevel:
        return ReversibilityLevel.AUTO


# =============================================================================
# Test Tasks
# =============================================================================

_attempt_counters: dict[str, int] = {}


@task
class SuccessTask(BaseModel):
    """Always succeeds."""

    value: Input(str) = "ok"
    result: Output(str) = None

    def execute(self) -> None:
        self.result = f"success-{self.value}"


@task
class FailTask(BaseModel):
    """Always fails."""

    message: Input(str) = "fail"
    result: Output(str) = None

    def execute(self) -> None:
        raise RuntimeError(f"Deliberate failure: {self.message}")


@task
class FailThenSucceed(BaseModel):
    """Fails N times, then succeeds."""

    fail_count: Input(int) = 1
    counter_key: Input(str) = "default"
    result: Output(str) = None

    def execute(self) -> None:
        key = self.counter_key
        _attempt_counters.setdefault(key, 0)
        _attempt_counters[key] += 1
        if _attempt_counters[key] <= self.fail_count:
            raise RuntimeError(f"Attempt {_attempt_counters[key]}: not yet")
        self.result = f"ok-on-{_attempt_counters[key]}"


@task
class BindingReader(BaseModel):
    """Resolves a binding from scope."""

    result: Output(str) = None

    def execute(self) -> None:
        binding = self.scope.get_binding("ctx")
        self.result = binding.context.value


@task
class ContextReader(BaseModel):
    """Reads a context via Context() field in a programmatic execute() task."""

    ctx: Context[SimpleContext]

    result: Output(str) = None

    def execute(self) -> None:
        self.result = self.ctx.value


@task
class AsyncContextReader(BaseModel):
    """Reads a context via Context() field in an async execute() task."""

    ctx: Context[SimpleContext]

    result: Output(str) = None

    async def execute(self) -> None:
        self.result = self.ctx.value


# =============================================================================
# Pipeline Tasks
# =============================================================================


@task
class SimplePipeline(BaseModel):
    """Two-stage pipeline."""

    input_val: Input(str)
    output_val: Output(str) = None

    async def execute(self) -> None:
        a = await self.run_stage("stage_a", SuccessTask, value=self.input_val)
        b = await self.run_stage("stage_b", SuccessTask, value=a.result)
        self.output_val = b.result


@task
class BindingPipeline(BaseModel):
    """Pipeline with inter-stage binding."""

    output_val: Output(str) = None

    async def execute(self) -> None:
        a = await self.run_stage("produce", SuccessTask, value="hello")
        self.scope.bind("ctx", SimpleContext(value=a.result))
        b = await self.run_stage("consume", BindingReader)
        self.output_val = b.result


@task
class ErrorPolicyPipeline(BaseModel):
    """Pipeline exercising all error policies."""

    skipped_result: Output(Any) = None
    defaulted_result: Output(Any) = None
    continued_result: Output(Any) = None

    async def execute(self) -> None:
        self.skipped_result = await self.run_stage(
            "skip_stage",
            FailTask,
            message="skip-me",
            on_error=OnError.skip,
        )
        self.defaulted_result = await self.run_stage(
            "default_stage",
            FailTask,
            message="default-me",
            on_error=OnError.default(category="unknown", level=0),
        )
        self.continued_result = await self.run_stage(
            "continue_stage",
            FailTask,
            message="continue-me",
            on_error=OnError.continue_with(posted=False),
        )


@task
class StageDeviceProbe(BaseModel):
    """Reads the active stage device from the runtime owner path."""

    device_name: Output(str) = None

    def execute(self) -> None:
        from shepherd_runtime.device import get_current_device

        device = get_current_device()
        self.device_name = device.name if device else "none"


@task
class StageDevicePipeline(BaseModel):
    """Pipeline that validates per-stage device scoping."""

    before: Output(str) = None
    during: Output(str) = None
    after: Output(str) = None

    async def execute(self) -> None:
        result = await self.run_stage("before", StageDeviceProbe)
        self.before = result.device_name
        result = await self.run_stage("during", StageDeviceProbe, device="local")
        self.during = result.device_name
        result = await self.run_stage("after", StageDeviceProbe)
        self.after = result.device_name


# =============================================================================
# Tests: run_stage basics
# =============================================================================


class TestRunStageBasics:
    def test_two_stage_pipeline(self) -> None:
        result = asyncio.run(self._run())
        assert result.output_val == "success-success-hello"

    async def _run(self) -> Any:
        async with Scope(root=True) as scope:
            return await SimplePipeline.arun(scope=scope, input_val="hello")

    def test_stages_registry_populated(self) -> None:
        result = asyncio.run(self._run_stages())
        assert "stage_a" in result.stages
        assert "stage_b" in result.stages
        assert result.stages["stage_a"].result == "success-hello"

    async def _run_stages(self) -> Any:
        async with Scope(root=True) as scope:
            return await SimplePipeline.arun(scope=scope, input_val="hello")

    def test_stage_effects_emitted(self) -> None:
        result = asyncio.run(self._run_effects())
        effects = result.effects
        started = [ly for ly in effects.layers if isinstance(ly.effect, StageStarted)]
        completed = [ly for ly in effects.layers if isinstance(ly.effect, StageCompleted)]
        assert len(started) == 2
        assert len(completed) == 2
        assert started[0].effect.stage_name == "stage_a"
        assert started[1].effect.stage_name == "stage_b"
        assert completed[0].effect.duration_ms >= 0
        assert completed[1].effect.duration_ms >= 0

    async def _run_effects(self) -> Any:
        async with Scope(root=True) as scope:
            return await SimplePipeline.arun(scope=scope, input_val="hello")

    def test_duplicate_stage_name_raises(self) -> None:
        @task
        class DupPipeline(BaseModel):
            async def execute(self) -> None:
                await self.run_stage("same", SuccessTask, value="a")
                await self.run_stage("same", SuccessTask, value="b")

        with pytest.raises((ValueError, TaskExecutionError), match="Duplicate stage name 'same'"):
            asyncio.run(self._run_dup(DupPipeline))

    async def _run_dup(self, cls: type) -> Any:
        async with Scope(root=True) as scope:
            return await cls.arun(scope=scope)

    def test_scope_property_available(self) -> None:
        @task
        class ScopePipeline(BaseModel):
            scope_id: Output(str) = None

            async def execute(self) -> None:
                self.scope_id = self.scope.id

        result = asyncio.run(self._run_scope(ScopePipeline))
        assert result.scope_id is not None
        assert result.scope_id.startswith("scope_")

    async def _run_scope(self, cls: type) -> Any:
        async with Scope(root=True) as scope:
            return await cls.arun(scope=scope)


# =============================================================================
# Tests: binding propagation
# =============================================================================


class TestBindingPropagation:
    def test_binding_across_stages(self) -> None:
        result = asyncio.run(self._run())
        assert result.output_val == "success-hello"

    async def _run(self) -> Any:
        async with Scope(root=True) as scope:
            return await BindingPipeline.arun(scope=scope)


# =============================================================================
# Tests: OnError policies
# =============================================================================


class TestOnErrorPolicies:
    def test_fatal_raises(self) -> None:
        @task
        class FatalPipeline(BaseModel):
            async def execute(self) -> None:
                await self.run_stage("fail", FailTask, on_error=OnError.fatal, message="fail")

        with pytest.raises((RuntimeError, TaskExecutionError), match="Deliberate failure"):
            asyncio.run(self._run(FatalPipeline))

    async def _run(self, cls: type) -> Any:
        async with Scope(root=True) as scope:
            return await cls.arun(scope=scope)

    def test_fatal_discards_effects_on_raise(self) -> None:
        """When a fatal stage raises, the pipeline's fork is discarded by arun().
        No effects from the failed pipeline escape to the parent scope."""

        @task
        class FatalPipeline(BaseModel):
            async def execute(self) -> None:
                await self.run_stage("fail", FailTask, on_error=OnError.fatal, message="fail")

        async def run() -> Any:
            async with Scope(root=True) as scope:
                with contextlib.suppress(RuntimeError, TaskExecutionError):
                    await FatalPipeline.arun(scope=scope)
                return scope.effects

        effects = asyncio.run(run())
        # Pipeline fork was discarded — no effects escape to parent
        stage_effects = [ly for ly in effects.layers if isinstance(ly.effect, (StageStarted, StageFailed))]
        assert len(stage_effects) == 0

    def test_skip_returns_none(self) -> None:
        result = asyncio.run(self._run_policies())
        assert result.skipped_result is None

    def test_skip_emits_stage_skipped(self) -> None:
        result = asyncio.run(self._run_policies())
        skipped = [ly for ly in result.effects.layers if isinstance(ly.effect, StageSkipped)]
        assert any(s.effect.stage_name == "skip_stage" for s in skipped)

    def test_default_returns_stub(self) -> None:
        result = asyncio.run(self._run_policies())
        assert result.defaulted_result is not None
        assert result.defaulted_result.category == "unknown"
        assert result.defaulted_result.level == 0

    def test_default_emits_defaulted(self) -> None:
        result = asyncio.run(self._run_policies())
        completed = [
            ly for ly in result.effects.layers if isinstance(ly.effect, StageCompleted) and ly.effect.defaulted
        ]
        assert any(c.effect.stage_name == "default_stage" for c in completed)

    def test_continue_with_returns_stub(self) -> None:
        result = asyncio.run(self._run_policies())
        assert result.continued_result is not None
        assert result.continued_result.posted is False

    def test_continue_with_emits_partial(self) -> None:
        result = asyncio.run(self._run_policies())
        completed = [ly for ly in result.effects.layers if isinstance(ly.effect, StageCompleted) and ly.effect.partial]
        assert any(c.effect.stage_name == "continue_stage" for c in completed)

    async def _run_policies(self) -> Any:
        async with Scope(root=True) as scope:
            return await ErrorPolicyPipeline.arun(scope=scope)


# =============================================================================
# Tests: retry
# =============================================================================


class TestRetry:
    def test_retry_succeeds_after_failures(self) -> None:
        _attempt_counters.clear()

        @task
        class RetryPipeline(BaseModel):
            result: Output(str) = None

            async def execute(self) -> None:
                r = await self.run_stage(
                    "flaky",
                    FailThenSucceed,
                    fail_count=2,
                    counter_key="test_retry",
                    retry=2,
                )
                self.result = r.result

        result = asyncio.run(self._run(RetryPipeline))
        assert result.result == "ok-on-3"

    def test_retry_exhausted_applies_policy(self) -> None:
        _attempt_counters.clear()

        @task
        class RetryDefaultPipeline(BaseModel):
            result: Output(Any) = None

            async def execute(self) -> None:
                self.result = await self.run_stage(
                    "always_fail",
                    FailTask,
                    message="nope",
                    retry=1,
                    on_error=OnError.default(fallback=True),
                )

        result = asyncio.run(self._run(RetryDefaultPipeline))
        assert result.result.fallback is True

        # Should have StageFailed for each failed attempt (before exhaustion)
        failed = [ly for ly in result.effects.layers if isinstance(ly.effect, StageFailed)]
        assert len(failed) == 1  # Only the non-final attempt emits StageFailed

    async def _run(self, cls: type) -> Any:
        async with Scope(root=True) as scope:
            return await cls.arun(scope=scope)


# =============================================================================
# Tests: device routing
# =============================================================================


class TestDeviceRouting:
    def test_per_stage_device(self) -> None:
        result = asyncio.run(self._run(StageDevicePipeline))
        assert result.before == "none"
        assert result.during == "local"
        assert result.after == "none"

    async def _run(self, cls: type) -> Any:
        async with Scope(root=True) as scope:
            return await cls.arun(scope=scope)


# =============================================================================
# Tests: stages registry
# =============================================================================


class TestStagesRegistry:
    def test_stages_empty_for_non_pipeline(self) -> None:
        result = asyncio.run(self._run_non_pipeline())
        assert dict(result.stages) == {}

    async def _run_non_pipeline(self) -> Any:
        async with Scope(root=True) as scope:
            return await SuccessTask.arun(scope=scope, value="test")

    def test_stages_includes_skipped(self) -> None:
        result = asyncio.run(self._run_policies())
        assert result.stages["skip_stage"] is None
        assert result.stages["default_stage"] is not None
        assert result.stages["continue_stage"] is not None

    async def _run_policies(self) -> Any:
        async with Scope(root=True) as scope:
            return await ErrorPolicyPipeline.arun(scope=scope)


# =============================================================================
# Tests: RestrictedPython async def
# =============================================================================


class TestAsyncRestriction:
    def test_secure_reconstruct_allows_async_def(self) -> None:
        from shepherd_runtime.task.secure import secure_reconstruct_task_class

        source = """\
from pydantic import BaseModel
from shepherd_runtime.task.authoring import Input, Output, task

@task
class AsyncTask(BaseModel):
    value: Input(int)
    result: Output(str) = None

    async def execute(self) -> None:
        self.result = f"async-{self.value}"
"""
        reconstructed = secure_reconstruct_task_class(
            source,
            allowed_imports=frozenset({"shepherd_runtime", "pydantic"}),
        )
        assert hasattr(reconstructed, "_task_meta")
        assert asyncio.iscoroutinefunction(reconstructed.execute)


# =============================================================================
# Tests: Context resolution for execute() tasks
# =============================================================================


class TestExecuteContextResolution:
    """Context() fields should be resolved from scope for programmatic tasks.

    Previously, Context() fields were only resolved for LLM tasks (via
    _execute_async). Programmatic tasks with execute() bypassed context
    resolution entirely, leaving Context() fields as None.
    """

    def test_sync_execute_resolves_context_via_arun(self) -> None:
        """Context() field is populated before sync execute() runs."""

        async def _run() -> None:
            async with Scope(root=True) as scope:
                scope.bind("ctx", SimpleContext(value="hello-sync"))
                result = await ContextReader.arun(scope=scope)
                assert result.result == "hello-sync"
                assert result.ctx is not None
                assert result.ctx.value == "hello-sync"

        asyncio.run(_run())

    def test_async_execute_resolves_context_via_arun(self) -> None:
        """Context() field is populated before async execute() runs."""

        async def _run() -> None:
            async with Scope(root=True) as scope:
                scope.bind("ctx", SimpleContext(value="hello-async"))
                result = await AsyncContextReader.arun(scope=scope)
                assert result.result == "hello-async"
                assert result.ctx is not None

        asyncio.run(_run())

    def test_context_resolved_by_type_when_name_differs(self) -> None:
        """Context resolves by type match when binding name differs from field name."""

        async def _run() -> None:
            async with Scope(root=True) as scope:
                # Bind under a different name than the field ("ctx")
                scope.bind("my_context", SimpleContext(value="type-match"))
                result = await ContextReader.arun(scope=scope)
                assert result.result == "type-match"

        asyncio.run(_run())

    def test_context_in_pipeline_stage(self) -> None:
        """Context() fields work for execute() tasks called via run_stage."""

        @task
        class ContextPipeline(BaseModel):
            output_val: Output(str) = None

            async def execute(self) -> None:
                self.scope.bind("ctx", SimpleContext(value="from-pipeline"))
                reader = await self.run_stage("read", ContextReader)
                self.output_val = reader.result

        async def _run() -> None:
            async with Scope(root=True) as scope:
                result = await ContextPipeline.arun(scope=scope)
                assert result.output_val == "from-pipeline"

        asyncio.run(_run())

    def test_explicit_context_takes_precedence(self) -> None:
        """Explicitly set Context() field is used over scope binding."""

        async def _run() -> None:
            async with Scope(root=True) as scope:
                scope.bind("ctx", SimpleContext(value="from-scope"))
                result = await ContextReader.arun(
                    scope=scope,
                    ctx=SimpleContext(value="explicit"),
                )
                assert result.result == "explicit"

        asyncio.run(_run())


# =============================================================================
# Tests: run_stage_sync
# =============================================================================


class TestRunStageSync:
    def test_sync_pipeline_two_stages(self) -> None:
        @task
        class SyncPipeline(BaseModel):
            input_val: Input(str)
            output_val: Output(str) = None

            def execute(self) -> None:
                a = self.run_stage_sync("stage_a", SuccessTask, value=self.input_val)
                b = self.run_stage_sync("stage_b", SuccessTask, value=a.result)
                self.output_val = b.result

        result = asyncio.run(self._run(SyncPipeline, input_val="hello"))
        assert result.output_val == "success-success-hello"

    def test_sync_stage_effects_emitted(self) -> None:
        @task
        class SyncEffectsPipeline(BaseModel):
            def execute(self) -> None:
                self.run_stage_sync("step1", SuccessTask, value="a")
                self.run_stage_sync("step2", SuccessTask, value="b")

        result = asyncio.run(self._run(SyncEffectsPipeline))
        started = [ly for ly in result.effects.layers if isinstance(ly.effect, StageStarted)]
        completed = [ly for ly in result.effects.layers if isinstance(ly.effect, StageCompleted)]
        assert len(started) == 2
        assert len(completed) == 2

    def test_sync_on_error_skip(self) -> None:
        @task
        class SyncSkipPipeline(BaseModel):
            result: Output(Any) = "not_set"

            def execute(self) -> None:
                self.result = self.run_stage_sync(
                    "fail",
                    FailTask,
                    message="skip-me",
                    on_error=OnError.skip,
                )

        result = asyncio.run(self._run(SyncSkipPipeline))
        assert result.result is None

    def test_sync_stages_registry(self) -> None:
        @task
        class SyncRegistryPipeline(BaseModel):
            def execute(self) -> None:
                self.run_stage_sync("a", SuccessTask, value="x")
                self.run_stage_sync("b", SuccessTask, value="y")

        result = asyncio.run(self._run(SyncRegistryPipeline))
        assert "a" in result.stages
        assert "b" in result.stages

    async def _run(self, cls: type, **kwargs: Any) -> Any:
        async with Scope(root=True) as scope:
            return await cls.arun(scope=scope, **kwargs)


# =============================================================================
# Tests: run_stages_parallel
# =============================================================================


class TestRunStagesParallel:
    def test_parallel_stages_all_succeed(self) -> None:
        @task
        class ParallelPipeline(BaseModel):
            results: Output(list) = None

            async def execute(self) -> None:
                self.results = await self.run_stages_parallel(
                    Stage("a", SuccessTask, {"value": "x"}),
                    Stage("b", SuccessTask, {"value": "y"}),
                    Stage("c", SuccessTask, {"value": "z"}),
                )

        result = asyncio.run(self._run(ParallelPipeline))
        assert len(result.results) == 3
        assert result.results[0].result == "success-x"
        assert result.results[1].result == "success-y"
        assert result.results[2].result == "success-z"

    def test_parallel_stages_with_failure_skip(self) -> None:
        @task
        class ParallelSkipPipeline(BaseModel):
            results: Output(list) = None

            async def execute(self) -> None:
                self.results = await self.run_stages_parallel(
                    Stage("ok", SuccessTask, {"value": "a"}),
                    Stage("fail", FailTask, {"message": "boom"}, on_error=OnError.skip),
                    Stage("ok2", SuccessTask, {"value": "b"}),
                )

        result = asyncio.run(self._run(ParallelSkipPipeline))
        assert result.results[0].result == "success-a"
        assert result.results[1] is None
        assert result.results[2].result == "success-b"

    def test_parallel_stages_with_default_policy(self) -> None:
        @task
        class ParallelDefaultPipeline(BaseModel):
            results: Output(list) = None

            async def execute(self) -> None:
                self.results = await self.run_stages_parallel(
                    Stage("fail", FailTask, {"message": "x"}, on_error=OnError.default(fallback="yes")),
                )

        result = asyncio.run(self._run(ParallelDefaultPipeline))
        assert result.results[0].fallback == "yes"

    def test_parallel_stages_emits_effects(self) -> None:
        @task
        class ParallelEffectsPipeline(BaseModel):
            async def execute(self) -> None:
                await self.run_stages_parallel(
                    Stage("a", SuccessTask, {"value": "1"}),
                    Stage("b", SuccessTask, {"value": "2"}),
                )

        result = asyncio.run(self._run(ParallelEffectsPipeline))
        started = [ly for ly in result.effects.layers if isinstance(ly.effect, StageStarted)]
        completed = [ly for ly in result.effects.layers if isinstance(ly.effect, StageCompleted)]
        assert len(started) == 2
        assert len(completed) == 2

    def test_parallel_stages_registry(self) -> None:
        @task
        class ParallelRegistryPipeline(BaseModel):
            async def execute(self) -> None:
                await self.run_stages_parallel(
                    Stage("x", SuccessTask, {"value": "1"}),
                    Stage("y", SuccessTask, {"value": "2"}),
                )

        result = asyncio.run(self._run(ParallelRegistryPipeline))
        assert "x" in result.stages
        assert "y" in result.stages

    def test_parallel_stages_max_concurrency(self) -> None:
        @task
        class BatchedPipeline(BaseModel):
            results: Output(list) = None

            async def execute(self) -> None:
                self.results = await self.run_stages_parallel(
                    Stage("a", SuccessTask, {"value": "1"}),
                    Stage("b", SuccessTask, {"value": "2"}),
                    Stage("c", SuccessTask, {"value": "3"}),
                    Stage("d", SuccessTask, {"value": "4"}),
                    max_concurrency=2,
                )

        result = asyncio.run(self._run(BatchedPipeline))
        assert len(result.results) == 4
        assert all(r is not None for r in result.results)

    def test_parallel_duplicate_name_raises(self) -> None:
        @task
        class DupParallelPipeline(BaseModel):
            async def execute(self) -> None:
                await self.run_stages_parallel(
                    Stage("same", SuccessTask, {"value": "1"}),
                    Stage("same", SuccessTask, {"value": "2"}),
                )

        with pytest.raises((ValueError, TaskExecutionError), match="Duplicate stage name"):
            asyncio.run(self._run(DupParallelPipeline))

    def test_parallel_fatal_raises(self) -> None:
        @task
        class FatalParallelPipeline(BaseModel):
            async def execute(self) -> None:
                await self.run_stages_parallel(
                    Stage("ok", SuccessTask, {"value": "a"}),
                    Stage("fail", FailTask, {"message": "fatal"}, on_error=OnError.fatal),
                )

        with pytest.raises((RuntimeError, TaskExecutionError), match="Deliberate failure"):
            asyncio.run(self._run(FatalParallelPipeline))

    async def _run(self, cls: type, **kwargs: Any) -> Any:
        async with Scope(root=True) as scope:
            return await cls.arun(scope=scope, **kwargs)
