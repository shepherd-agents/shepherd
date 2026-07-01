"""Runtime-shaped workload coverage for the kernel-v3 canary boundary."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel
from shepherd_core.context.kernel import ExecutionContextDefaults
from shepherd_core.effects import TaskCompleted
from shepherd_core.types import ReversibilityLevel
from shepherd_kernel_v3_reference.kernel import (
    ExternalEffectRequestDescriptor,
    HostCompleted,
    elaborate,
    host_completed_to_json,
)
from shepherd_kernel_v3_reference.schemas import AnySchema, TypeSchema
from shepherd_kernel_v3_reference.source.effects import EffectRegistry, EffectSignature
from shepherd_kernel_v3_reference.source.syntax import Let, Lit, Perform, RecordExpr, Return, Var
from shepherd_kernel_v3_reference.source.values import Env
from shepherd_runtime.kernel import (
    KernelV3CanaryMode,
    clear_kernel_v3_canary_cache,
    kernel_v3_canary,
    kernel_v3_canary_policy,
)
from shepherd_runtime.scope import Scope
from shepherd_runtime.task.authoring import Context, Input, Output, task


@dataclass
class TextPolicyContext(ExecutionContextDefaults):
    prefix: str

    @property
    def context_id(self) -> str:
        return f"text-policy:{self.prefix}"

    @property
    def reversibility(self) -> ReversibilityLevel:
        return ReversibilityLevel.AUTO


_CALLS: dict[str, int] = {
    "program_factory": 0,
    "existing_executor": 0,
}


def _reset_calls() -> None:
    for name in _CALLS:
        _CALLS[name] = 0


def _normalize_text(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _word_count(text: str) -> int:
    return len([word for word in text.split() if word])


def _runtime_workload_program_factory(_task_instance: Any) -> Any:
    _CALLS["program_factory"] += 1
    return elaborate(
        Return(
            RecordExpr(
                (
                    ("normalized", Var("normalized")),
                    ("word_count", Var("word_count")),
                    ("tag", Var("tag")),
                )
            )
        )
    )


def _runtime_workload_env_factory(task_instance: Any) -> Env:
    normalized = _normalize_text(task_instance.text)
    return (
        Env()
        .extend("normalized", normalized)
        .extend("word_count", _word_count(normalized))
        .extend("tag", f"{task_instance.policy.prefix}:{normalized[:8]}")
    )


@kernel_v3_canary(
    program_factory=_runtime_workload_program_factory,
    env_factory=_runtime_workload_env_factory,
    cache_key="RuntimeCanaryNormalize:v1",
    shadow_safe=True,
)
@task
class RuntimeCanaryNormalize(BaseModel):
    text: Input(str) = ""
    policy: Context[TextPolicyContext]
    normalized: Output(str) = None
    word_count: Output(int) = None
    tag: Output(str) = None

    def execute(self) -> None:
        _CALLS["existing_executor"] += 1
        normalized = _normalize_text(self.text)
        self.normalized = normalized
        self.word_count = _word_count(normalized)
        self.tag = f"{self.policy.prefix}:{normalized[:8]}"


@task
class RuntimeCanaryPipeline(BaseModel):
    text: Input(str) = ""
    normalized: Output(str) = None
    tag: Output(str) = None
    child_authoritative: Output(str) = None
    child_cache_hit: Output(bool) = None

    async def execute(self) -> None:
        child = await self.run_stage("normalize", RuntimeCanaryNormalize, text=self.text)
        self.normalized = child.normalized
        self.tag = child.tag
        self.child_authoritative = child.kernel_v3_canary_report.authoritative
        self.child_cache_hit = child.kernel_v3_canary_report.prepared_cache_hit


@pytest.fixture(autouse=True)
def reset_canary_state() -> None:
    clear_kernel_v3_canary_cache()
    _reset_calls()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _run_with_policy(task_cls: type, mode: str, **inputs: Any) -> Any:
    async with Scope(root=True) as scope:
        scope.bind("policy", TextPolicyContext(prefix="docs"))
        with kernel_v3_canary_policy(mode):
            return await task_cls.arun(scope=scope, **inputs)


async def _run_two_canary_inputs() -> tuple[Any, Any]:
    async with Scope(root=True) as scope:
        scope.bind("policy", TextPolicyContext(prefix="docs"))
        with kernel_v3_canary_policy("canary"):
            first = await RuntimeCanaryNormalize.arun(scope=scope, text="  Hello   WORLD  ")
            second = await RuntimeCanaryNormalize.arun(scope=scope, text="Second sample")
            return first, second


async def _run_two_canary_contexts() -> tuple[Any, Any]:
    with kernel_v3_canary_policy("canary"):
        async with Scope(root=True) as first_scope:
            first_scope.bind("policy", TextPolicyContext(prefix="docs"))
            first = await RuntimeCanaryNormalize.arun(first_scope, text="Same Input")
        async with Scope(root=True) as second_scope:
            second_scope.bind("policy", TextPolicyContext(prefix="api"))
            second = await RuntimeCanaryNormalize.arun(second_scope, text="Same Input")
        return first, second


def _task_completed_effects(result: Any) -> list[TaskCompleted]:
    return [layer.effect for layer in result.effects.layers if isinstance(layer.effect, TaskCompleted)]


def test_runtime_workload_canary_reuses_prepared_program_with_per_run_env() -> None:
    first, second = _run(_run_two_canary_inputs())

    assert first.normalized == "hello world"
    assert first.word_count == 2
    assert first.tag == "docs:hello wo"
    assert second.normalized == "second sample"
    assert second.word_count == 2
    assert second.tag == "docs:second s"

    assert _CALLS["program_factory"] == 1
    assert _CALLS["existing_executor"] == 0
    assert first.kernel_v3_canary_report.authoritative == "v3"
    assert first.kernel_v3_canary_report.prepared_cache_hit is False
    assert second.kernel_v3_canary_report.authoritative == "v3"
    assert second.kernel_v3_canary_report.prepared_cache_hit is True


def test_runtime_workload_canary_reuses_prepared_program_with_per_context_env() -> None:
    first, second = _run(_run_two_canary_contexts())

    assert first.normalized == "same input"
    assert first.tag == "docs:same inp"
    assert second.normalized == "same input"
    assert second.tag == "api:same inp"
    assert _CALLS["program_factory"] == 1
    assert first.kernel_v3_canary_report.prepared_cache_hit is False
    assert second.kernel_v3_canary_report.prepared_cache_hit is True


def test_runtime_workload_shadow_has_typed_multi_output_parity() -> None:
    async def run_shadow() -> Any:
        async with Scope(root=True) as scope:
            scope.bind("policy", TextPolicyContext(prefix="docs"))
            with kernel_v3_canary_policy("shadow", raise_on_shadow_mismatch=True):
                return await RuntimeCanaryNormalize.arun(scope=scope, text="  Mixed   CASE  ")

    result = _run(run_shadow())

    assert result.normalized == "mixed case"
    assert result.word_count == 2
    assert result.tag == "docs:mixed ca"
    assert _CALLS["existing_executor"] == 1
    assert result.kernel_v3_canary_report.mode == KernelV3CanaryMode.SHADOW
    assert result.kernel_v3_canary_report.authoritative == "existing"
    assert result.kernel_v3_canary_report.v3_ran is True
    assert result.kernel_v3_canary_report.mismatch_reason is None


def test_runtime_workload_shadow_mismatch_reports_readable_reason() -> None:
    @kernel_v3_canary(
        program_factory=lambda _task_instance: elaborate(Return(Lit("wrong"))),
        cache_key="RuntimeCanaryMismatch:v1",
        shadow_safe=True,
    )
    @task
    class RuntimeCanaryMismatch(BaseModel):
        text: Input(str) = ""
        normalized: Output(str) = None

        def execute(self) -> None:
            self.normalized = _normalize_text(self.text)

    result = _run(_run_with_policy(RuntimeCanaryMismatch, "shadow", text="Expected Output"))

    assert result.normalized == "expected output"
    assert result.kernel_v3_canary_report.authoritative == "existing"
    assert result.kernel_v3_canary_report.mismatch_reason == "output_mismatch"


def test_runtime_workload_survives_run_stage_pipeline_lifecycle() -> None:
    result = _run(_run_with_policy(RuntimeCanaryPipeline, "canary", text="  Pipeline   Input  "))

    assert result.normalized == "pipeline input"
    assert result.tag == "docs:pipeline"
    assert result.child_authoritative == "v3"
    assert result.child_cache_hit is False
    assert result.stages["normalize"].kernel_v3_canary_report.authoritative == "v3"
    assert _CALLS["existing_executor"] == 0


def test_runtime_workload_task_completed_carries_canary_metadata() -> None:
    result = _run(_run_with_policy(RuntimeCanaryNormalize, "canary", text="Metadata Input"))

    completed = _task_completed_effects(result)
    canary_metadata = [
        effect.metadata["kernel_v3_canary"] for effect in completed if "kernel_v3_canary" in effect.metadata
    ]

    assert len(canary_metadata) == 1
    assert canary_metadata[0]["mode"] == "canary"
    assert canary_metadata[0]["authoritative"] == "v3"
    json.dumps(canary_metadata[0])


def test_runtime_workload_replay_canary_handles_external_request() -> None:
    observations: list[dict[str, Any]] = []

    def host_observation_adapter(request: ExternalEffectRequestDescriptor, task_instance: Any) -> HostCompleted:
        observation = HostCompleted(f"{task_instance.prefix}:{request.payload['prompt']}")
        observations.append(
            {
                "request": {
                    "effect_kind": request.effect_kind,
                    "payload": request.payload,
                    "root_ref": request.root_ref,
                },
                "observation": json.loads(json.dumps(host_completed_to_json(observation))),
            }
        )
        return observation

    @kernel_v3_canary(
        program_factory=lambda _task_instance: elaborate(
            Let("draft", Perform("provider.llm.generate", Lit({"prompt": "draft"})), Return(Var("draft")))
        ),
        host_observation_adapter=host_observation_adapter,
        cache_key="RuntimeReplayCanaryProvider:v1",
    )
    @task
    class RuntimeReplayCanaryProvider(BaseModel):
        prefix: Input(str) = ""
        result: Output(str) = None

        def execute(self) -> None:
            _CALLS["existing_executor"] += 1
            self.result = "existing"

    result = _run(_run_with_policy(RuntimeReplayCanaryProvider, "canary", prefix="host"))

    assert result.result == "host:draft"
    assert len(observations) == 1
    assert observations[0]["request"]["effect_kind"] == "provider.llm.generate"
    assert observations[0]["request"]["payload"] == {"prompt": "draft"}
    assert observations[0]["request"]["root_ref"].startswith("continuation-object:sha256:")
    assert observations[0]["observation"]["value"] == "host:draft"
    assert _CALLS["existing_executor"] == 0
    assert result.kernel_v3_canary_report.authoritative == "v3"
    assert result.kernel_v3_canary_report.v3_boundary == "replay"
    assert len(result.kernel_v3_canary_report.v3_replay_transition_refs) == 2
    assert result.kernel_v3_canary_report.v3_replay_open_source_keys == ()
    assert len(result.kernel_v3_canary_report.v3_replay_consumed_source_keys) == 1
    assert result.kernel_v3_canary_report.to_metadata()["v3_boundary"] == "replay"
    assert result.kernel_v3_canary_report.to_metadata()["v3_replay"]["transition_refs"]


def test_runtime_workload_replay_canary_descriptor_payload_is_snapshot() -> None:
    def host_observation_adapter(request: ExternalEffectRequestDescriptor, _task_instance: Any) -> HostCompleted:
        prompt = request.payload["prompt"]
        request.payload["prompt"] = "mutated"
        return HostCompleted(f"{prompt}-result")

    @kernel_v3_canary(
        program_factory=lambda _task_instance: elaborate(
            Let("draft", Perform("provider.llm.generate", Lit({"prompt": "draft"})), Return(Var("draft")))
        ),
        host_observation_adapter=host_observation_adapter,
        cache_key="RuntimeReplayCanarySnapshot:v1",
    )
    @task
    class RuntimeReplayCanarySnapshot(BaseModel):
        result: Output(str) = None

        def execute(self) -> None:
            _CALLS["existing_executor"] += 1
            self.result = "existing"

    result = _run(_run_with_policy(RuntimeReplayCanarySnapshot, "canary"))

    assert result.result == "draft-result"
    assert _CALLS["existing_executor"] == 0
    assert result.kernel_v3_canary_report.authoritative == "v3"


def test_runtime_workload_replay_canary_handles_sequential_external_requests() -> None:
    observed_prompts: list[str] = []

    async def host_observation_adapter(
        request: ExternalEffectRequestDescriptor,
        _task_instance: Any,
    ) -> HostCompleted:
        observed_prompts.append(request.payload["prompt"])
        return HostCompleted(f"{request.payload['prompt']}-result")

    @kernel_v3_canary(
        program_factory=lambda _task_instance: elaborate(
            Let(
                "first",
                Perform("provider.llm.generate", Lit({"prompt": "first"})),
                Let(
                    "second",
                    Perform("provider.llm.generate", Lit({"prompt": "second"})),
                    Return(Var("second")),
                ),
            )
        ),
        host_observation_adapter=host_observation_adapter,
        cache_key="RuntimeReplayCanarySequential:v1",
    )
    @task
    class RuntimeReplayCanarySequential(BaseModel):
        result: Output(str) = None

        def execute(self) -> None:
            _CALLS["existing_executor"] += 1
            self.result = "existing"

    result = _run(_run_with_policy(RuntimeReplayCanarySequential, "canary"))

    assert result.result == "second-result"
    assert observed_prompts == ["first", "second"]
    assert _CALLS["existing_executor"] == 0
    assert result.kernel_v3_canary_report.v3_boundary == "replay"
    assert len(result.kernel_v3_canary_report.v3_replay_transition_refs) == 3
    assert len(result.kernel_v3_canary_report.v3_replay_consumed_source_keys) == 2
    assert result.kernel_v3_canary_report.v3_replay_open_source_keys == ()


def test_runtime_workload_replay_canary_falls_back_on_bad_observation() -> None:
    @kernel_v3_canary(
        program_factory=lambda _task_instance: elaborate(
            Let("draft", Perform("provider.llm.generate", Lit({"prompt": "draft"})), Return(Var("draft")))
        ),
        host_observation_adapter=lambda _request, _task_instance: "not-a-host-observation",
        cache_key="RuntimeReplayCanaryBadObservation:v1",
    )
    @task
    class RuntimeReplayCanaryBadObservation(BaseModel):
        result: Output(str) = None

        def execute(self) -> None:
            _CALLS["existing_executor"] += 1
            self.result = "existing"

    result = _run(_run_with_policy(RuntimeReplayCanaryBadObservation, "canary"))

    assert result.result == "existing"
    assert _CALLS["existing_executor"] == 1
    assert result.kernel_v3_canary_report.authoritative == "existing"
    assert result.kernel_v3_canary_report.fallback_ran is True
    assert result.kernel_v3_canary_report.v3_boundary == "replay"
    assert "HostCompleted" in result.kernel_v3_canary_report.fallback_reason


def test_runtime_workload_replay_canary_rejected_observation_reports_frontier_metadata() -> None:
    registry = EffectRegistry()
    registry.register(EffectSignature("provider.llm.generate", AnySchema(), TypeSchema(int)))

    @kernel_v3_canary(
        program_factory=lambda _task_instance: elaborate(
            Let("draft", Perform("provider.llm.generate", Lit({"prompt": "draft"})), Return(Var("draft"))),
            registry=registry,
        ),
        host_observation_adapter=lambda _request, _task_instance: HostCompleted("not-an-int"),
        effect_registry=registry,
        cache_key="RuntimeReplayCanaryRejectedObservation:v1",
    )
    @task
    class RuntimeReplayCanaryRejectedObservation(BaseModel):
        result: Output(str) = None

        def execute(self) -> None:
            _CALLS["existing_executor"] += 1
            self.result = "existing"

    result = _run(_run_with_policy(RuntimeReplayCanaryRejectedObservation, "canary"))

    assert result.result == "existing"
    assert _CALLS["existing_executor"] == 1
    assert result.kernel_v3_canary_report.authoritative == "existing"
    assert result.kernel_v3_canary_report.fallback_ran is True
    assert result.kernel_v3_canary_report.v3_boundary == "replay"
    assert len(result.kernel_v3_canary_report.v3_replay_transition_refs) == 2
    assert len(result.kernel_v3_canary_report.v3_replay_consumed_source_keys) == 1
    assert result.kernel_v3_canary_report.v3_replay_open_source_keys == ()
    assert "KernelReplayRejected" in result.kernel_v3_canary_report.fallback_reason


def test_plain_runtime_workload_task_completed_has_no_canary_metadata() -> None:
    @task
    class PlainRuntimeWorkload(BaseModel):
        text: Input(str) = ""
        normalized: Output(str) = None

        def execute(self) -> None:
            self.normalized = _normalize_text(self.text)

    result = _run(_run_with_policy(PlainRuntimeWorkload, "canary", text="Plain Input"))

    completed = _task_completed_effects(result)
    assert completed
    assert all("kernel_v3_canary" not in effect.metadata for effect in completed)


def test_runtime_workload_output_validation_failure_falls_back() -> None:
    @kernel_v3_canary(
        program_factory=lambda _task_instance: elaborate(
            Return(
                RecordExpr(
                    (
                        ("normalized", Lit("bad")),
                        ("word_count", Lit("not-an-int")),
                        ("tag", Lit("bad")),
                    )
                )
            )
        ),
        env_factory=_runtime_workload_env_factory,
        cache_key="RuntimeCanaryBadOutputs:v1",
        shadow_safe=True,
    )
    @task
    class RuntimeCanaryBadOutputs(BaseModel):
        text: Input(str) = ""
        policy: Context[TextPolicyContext]
        normalized: Output(str) = None
        word_count: Output(int) = None
        tag: Output(str) = None

        def execute(self) -> None:
            _CALLS["existing_executor"] += 1
            normalized = _normalize_text(self.text)
            self.normalized = normalized
            self.word_count = _word_count(normalized)
            self.tag = f"{self.policy.prefix}:{normalized[:8]}"

    result = _run(_run_with_policy(RuntimeCanaryBadOutputs, "canary", text="Fallback On Validation"))

    assert result.normalized == "fallback on validation"
    assert result.word_count == 3
    assert result.tag == "docs:fallback"
    assert _CALLS["existing_executor"] == 1
    assert result.kernel_v3_canary_report.authoritative == "existing"
    assert result.kernel_v3_canary_report.fallback_ran is True
    assert "ValidationError" in result.kernel_v3_canary_report.fallback_reason


def test_runtime_workload_canary_falls_back_to_existing_executor() -> None:
    def broken_program_factory(_task_instance: Any) -> Any:
        raise RuntimeError("runtime workload not admitted")

    @kernel_v3_canary(
        program_factory=broken_program_factory,
        env_factory=_runtime_workload_env_factory,
        cache_key="BrokenRuntimeCanaryNormalize:v1",
        shadow_safe=True,
    )
    @task
    class BrokenRuntimeCanaryNormalize(BaseModel):
        text: Input(str) = ""
        policy: Context[TextPolicyContext]
        normalized: Output(str) = None
        word_count: Output(int) = None
        tag: Output(str) = None

        def execute(self) -> None:
            _CALLS["existing_executor"] += 1
            normalized = _normalize_text(self.text)
            self.normalized = normalized
            self.word_count = _word_count(normalized)
            self.tag = f"{self.policy.prefix}:{normalized[:8]}"

    result = _run(_run_with_policy(BrokenRuntimeCanaryNormalize, "canary", text="Fallback Input"))

    assert result.normalized == "fallback input"
    assert result.word_count == 2
    assert result.tag == "docs:fallback"
    assert _CALLS["existing_executor"] == 1
    assert result.kernel_v3_canary_report.authoritative == "existing"
    assert result.kernel_v3_canary_report.fallback_ran is True
    assert "runtime workload not admitted" in result.kernel_v3_canary_report.fallback_reason
