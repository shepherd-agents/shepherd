"""Tests for the runtime kernel-v3 canary boundary."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel
from shepherd_kernel_v3_reference.kernel import elaborate
from shepherd_kernel_v3_reference.source.syntax import Lit, Return
from shepherd_runtime.kernel import (
    KernelV3CanaryMode,
    KernelV3CanaryReport,
    clear_kernel_v3_canary_cache,
    kernel_v3_canary,
    kernel_v3_canary_policy,
)
from shepherd_runtime.scope import Scope
from shepherd_runtime.task.authoring import Input, Output, task


@pytest.fixture(autouse=True)
def clear_canary_cache() -> None:
    clear_kernel_v3_canary_cache()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_kernel_v3_canary_report_metadata_is_json_compatible() -> None:
    report = KernelV3CanaryReport(
        mode=KernelV3CanaryMode.CANARY,
        authoritative="v3",
        v3_ran=True,
        fallback_ran=False,
        prepared_cache_hit=True,
        v3_duration_ms=1.25,
    )

    assert report.to_metadata() == {
        "mode": "canary",
        "authoritative": "v3",
        "v3_ran": True,
        "fallback_ran": False,
        "prepared_cache_hit": True,
        "fallback_reason": None,
        "mismatch_reason": None,
        "v3_boundary": "none",
        "v3_duration_ms": 1.25,
        "fallback_duration_ms": 0.0,
    }


async def _run_task(task_cls: type, **kwargs: Any) -> Any:
    async with Scope(root=True) as scope:
        return await task_cls.arun(scope=scope, **kwargs)


def test_kernel_v3_canary_off_uses_existing_executor_without_building_program() -> None:
    def blocked_program_factory(task_instance: Any) -> Any:
        raise AssertionError("v3 program should not be built when canary is off")

    @kernel_v3_canary(program_factory=blocked_program_factory)
    @task
    class ExistingPathTask(BaseModel):
        value: Input(int) = 0
        doubled: Output(int) = None

        def execute(self) -> None:
            self.doubled = self.value * 2

    result = _run(_run_task(ExistingPathTask, value=7))

    assert result.doubled == 14
    assert result.kernel_v3_canary_report.mode == KernelV3CanaryMode.OFF
    assert result.kernel_v3_canary_report.v3_ran is False
    assert result.kernel_v3_canary_report.fallback_ran is True


def test_kernel_v3_canary_shadow_keeps_existing_output_authoritative() -> None:
    @kernel_v3_canary(
        program_factory=lambda task_instance: elaborate(Return(Lit(task_instance.value * 3))),
        shadow_safe=True,
    )
    @task
    class ShadowTask(BaseModel):
        value: Input(int) = 0
        doubled: Output(int) = None

        def execute(self) -> None:
            self.doubled = self.value * 2

    with kernel_v3_canary_policy("shadow"):
        result = _run(_run_task(ShadowTask, value=5))

    assert result.doubled == 10
    assert result.kernel_v3_canary_report.mode == KernelV3CanaryMode.SHADOW
    assert result.kernel_v3_canary_report.authoritative == "existing"
    assert result.kernel_v3_canary_report.v3_ran is True
    assert result.kernel_v3_canary_report.mismatch_reason == "output_mismatch"


def test_kernel_v3_canary_shadow_can_attach_spec_before_task_decorator() -> None:
    @task
    @kernel_v3_canary(
        program_factory=lambda task_instance: elaborate(Return(Lit(task_instance.value * 2))),
        shadow_safe=True,
    )
    class PreDecoratedShadowTask(BaseModel):
        value: Input(int) = 0
        doubled: Output(int) = None

        def execute(self) -> None:
            self.doubled = self.value * 2

    with kernel_v3_canary_policy("shadow"):
        result = _run(_run_task(PreDecoratedShadowTask, value=6))

    assert result.doubled == 12
    assert result.kernel_v3_canary_report.v3_ran is True
    assert result.kernel_v3_canary_report.mismatch_reason is None


def test_kernel_v3_canary_mode_uses_v3_without_existing_executor() -> None:
    called_existing = False

    @kernel_v3_canary(program_factory=lambda task_instance: elaborate(Return(Lit(task_instance.value * 4))))
    @task
    class CanaryTask(BaseModel):
        value: Input(int) = 0
        quadrupled: Output(int) = None

        def execute(self) -> None:
            nonlocal called_existing
            called_existing = True
            self.quadrupled = -1

    with kernel_v3_canary_policy("canary"):
        result = _run(_run_task(CanaryTask, value=3))

    assert result.quadrupled == 12
    assert called_existing is False
    assert result.kernel_v3_canary_report.authoritative == "v3"
    assert result.kernel_v3_canary_report.fallback_ran is False


def test_kernel_v3_canary_falls_back_when_v3_fails() -> None:
    def broken_program_factory(task_instance: Any) -> Any:
        raise RuntimeError("not lowerable")

    @kernel_v3_canary(program_factory=broken_program_factory)
    @task
    class FallbackTask(BaseModel):
        value: Input(int) = 0
        doubled: Output(int) = None

        def execute(self) -> None:
            self.doubled = self.value * 2

    with kernel_v3_canary_policy("canary"):
        result = _run(_run_task(FallbackTask, value=8))

    assert result.doubled == 16
    assert result.kernel_v3_canary_report.authoritative == "existing"
    assert result.kernel_v3_canary_report.v3_ran is True
    assert result.kernel_v3_canary_report.fallback_ran is True
    assert "not lowerable" in result.kernel_v3_canary_report.fallback_reason


def test_kernel_v3_canary_reuses_prepared_program_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    from shepherd_runtime.kernel import canary

    prepare_calls = 0
    real_prepare = canary.prepare_kernel_program

    def counting_prepare(program: Any, **kwargs: Any) -> Any:
        nonlocal prepare_calls
        prepare_calls += 1
        return real_prepare(program, **kwargs)

    monkeypatch.setattr(canary, "prepare_kernel_program", counting_prepare)

    @kernel_v3_canary(
        program_factory=lambda task_instance: elaborate(Return(Lit("cached"))),
        cache_key="CachedTask:v1",
    )
    @task
    class CachedTask(BaseModel):
        label: Output(str) = None

        def execute(self) -> None:
            self.label = "existing"

    with kernel_v3_canary_policy("canary"):
        first = _run(_run_task(CachedTask))
        second = _run(_run_task(CachedTask))

    assert first.label == "cached"
    assert second.label == "cached"
    assert prepare_calls == 1
    assert first.kernel_v3_canary_report.prepared_cache_hit is False
    assert second.kernel_v3_canary_report.prepared_cache_hit is True


def test_kernel_v3_canary_hot_path_does_not_call_run_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    from shepherd_kernel_v3_reference.trace import machine as trace_machine

    def blocked_run_trace(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("canary hot path must not call run_trace")

    monkeypatch.setattr(trace_machine, "run_trace", blocked_run_trace)

    @kernel_v3_canary(program_factory=lambda task_instance: elaborate(Return(Lit("v3"))))
    @task
    class HotPathTask(BaseModel):
        label: Output(str) = None

        def execute(self) -> None:
            self.label = "existing"

    with kernel_v3_canary_policy("canary"):
        result = _run(_run_task(HotPathTask))

    assert result.label == "v3"
    assert result.kernel_v3_canary_report.v3_ran is True


def test_task_without_canary_spec_keeps_existing_behavior() -> None:
    @task
    class PlainTask(BaseModel):
        value: Input(int) = 0
        doubled: Output(int) = None

        def execute(self) -> None:
            self.doubled = self.value * 2

    with kernel_v3_canary_policy("canary"):
        result = _run(_run_task(PlainTask, value=4))

    assert result.doubled == 8
    assert result.kernel_v3_canary_report is None


def test_kernel_v3_canary_cache_key_collision_falls_back() -> None:
    @kernel_v3_canary(
        program_factory=lambda task_instance: elaborate(Return(Lit(task_instance.value * 2))),
        cache_key="CollisionTask:v1",
    )
    @task
    class FirstCollisionTask(BaseModel):
        value: Input(int) = 0
        doubled: Output(int) = None

        def execute(self) -> None:
            self.doubled = self.value * 2

    @kernel_v3_canary(
        program_factory=lambda task_instance: elaborate(Return(Lit(task_instance.value * 3))),
        cache_key="CollisionTask:v1",
    )
    @task
    class SecondCollisionTask(BaseModel):
        value: Input(int) = 0
        tripled: Output(int) = None

        def execute(self) -> None:
            self.tripled = self.value * 3

    with kernel_v3_canary_policy("canary"):
        first = _run(_run_task(FirstCollisionTask, value=3))
        second = _run(_run_task(SecondCollisionTask, value=4))

    assert first.doubled == 6
    assert second.tripled == 12
    assert second.kernel_v3_canary_report.authoritative == "existing"
    assert second.kernel_v3_canary_report.fallback_ran is True
    assert "cache_key collision" in second.kernel_v3_canary_report.fallback_reason
