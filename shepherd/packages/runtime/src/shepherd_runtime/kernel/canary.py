"""Canary routing for prepared kernel-v3 program execution."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import create_model
from shepherd_kernel_v3_reference.kernel import (
    ExternalEffectRequestDescriptor,
    HostCompleted,
    KernelReplayRejected,
    KernelReplaySession,
    KernelReplayState,
    PreparedKernelProgram,
    ReplayableCompleted,
    host_completed_from_json,
    host_completed_to_json,
    prepare_kernel_program,
    run_kernel,
)
from shepherd_kernel_v3_reference.source.outcomes import Completed

if TYPE_CHECKING:
    from collections.abc import Generator

    from shepherd_kernel_v3_reference.kernel.program_admission import KernelProgramInput
    from shepherd_kernel_v3_reference.source.effects import EffectRegistry
    from shepherd_kernel_v3_reference.source.outcomes import SourceOutcome
    from shepherd_kernel_v3_reference.source.values import Env


class KernelV3CanaryMode(str, Enum):
    """Runtime rollout modes for kernel-v3 canary execution."""

    OFF = "off"
    SHADOW = "shadow"
    CANARY = "canary"


@dataclass(frozen=True)
class KernelV3CanaryPolicy:
    """Context-local rollout policy for kernel-v3 canary execution."""

    mode: KernelV3CanaryMode = KernelV3CanaryMode.OFF
    raise_on_shadow_mismatch: bool = False


@dataclass(frozen=True)
class KernelV3CanarySpec:
    """Explicit task opt-in for prepared kernel-v3 execution.

    By default canaries use the execution-only `run_kernel(...)` path. Supplying
    `host_observation_adapter` opts the task into the replay boundary, where
    unhandled external requests are handed to the host and resumed with
    `HostCompleted` observations.
    """

    program_factory: Callable[[Any], KernelProgramInput]
    env_factory: Callable[[Any], Env | None] | None = None
    output_adapter: Callable[[SourceOutcome, Any], Mapping[str, Any]] | None = None
    host_observation_adapter: Callable[
        [ExternalEffectRequestDescriptor, Any],
        HostCompleted | Awaitable[HostCompleted],
    ] | None = None
    effect_registry: EffectRegistry | None = None
    cache_key: str | None = None
    shadow_safe: bool = False
    max_replay_transitions: int = 32


@dataclass(frozen=True)
class KernelV3CanaryReport:
    """Cheap runtime report for one canary decision."""

    mode: KernelV3CanaryMode
    authoritative: Literal["existing", "v3", "none"]
    v3_ran: bool
    fallback_ran: bool
    prepared_cache_hit: bool = False
    fallback_reason: str | None = None
    mismatch_reason: str | None = None
    v3_boundary: Literal["none", "execution", "replay"] = "none"
    v3_replay_transition_refs: tuple[str, ...] = ()
    v3_replay_open_source_keys: tuple[str, ...] = ()
    v3_replay_consumed_source_keys: tuple[str, ...] = ()
    v3_duration_ms: float = 0.0
    fallback_duration_ms: float = 0.0

    def to_metadata(self) -> dict[str, Any]:
        """Return a JSON-compatible runtime diagnostics summary."""
        metadata: dict[str, Any] = {
            "mode": self.mode.value,
            "authoritative": self.authoritative,
            "v3_ran": self.v3_ran,
            "fallback_ran": self.fallback_ran,
            "prepared_cache_hit": self.prepared_cache_hit,
            "fallback_reason": self.fallback_reason,
            "mismatch_reason": self.mismatch_reason,
            "v3_boundary": self.v3_boundary,
            "v3_duration_ms": self.v3_duration_ms,
            "fallback_duration_ms": self.fallback_duration_ms,
        }
        if self.v3_boundary == "replay" or self.v3_replay_transition_refs:
            metadata["v3_replay"] = {
                "transition_refs": list(self.v3_replay_transition_refs),
                "open_source_keys": list(self.v3_replay_open_source_keys),
                "consumed_source_keys": list(self.v3_replay_consumed_source_keys),
            }
        return metadata


class KernelV3CanaryMismatchError(RuntimeError):
    """Raised only when the active policy asks shadow mismatches to fail."""


@dataclass(frozen=True)
class _PreparedCacheEntry:
    prepared: PreparedKernelProgram
    program_factory: Callable[[Any], KernelProgramInput]


@dataclass(frozen=True)
class _ReplayMetadata:
    transition_refs: tuple[str, ...] = ()
    open_source_keys: tuple[str, ...] = ()
    consumed_source_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class _V3RunResult:
    outcome: SourceOutcome
    prepared_cache_hit: bool
    boundary: Literal["execution", "replay"]
    replay_metadata: _ReplayMetadata = _ReplayMetadata()


_DEFAULT_POLICY = KernelV3CanaryPolicy()
_POLICY: ContextVar[KernelV3CanaryPolicy | None] = ContextVar(
    "kernel_v3_canary_policy",
    default=None,
)
_PREPARED_CACHE: dict[str, _PreparedCacheEntry] = {}


def get_kernel_v3_canary_policy() -> KernelV3CanaryPolicy:
    """Return the active context-local canary policy."""
    return _POLICY.get() or _DEFAULT_POLICY


@contextmanager
def kernel_v3_canary_policy(
    mode: KernelV3CanaryMode | str,
    *,
    raise_on_shadow_mismatch: bool = False,
) -> Generator[KernelV3CanaryPolicy]:
    """Temporarily install a kernel-v3 canary rollout policy."""
    normalized = mode if isinstance(mode, KernelV3CanaryMode) else KernelV3CanaryMode(mode)
    policy = KernelV3CanaryPolicy(
        mode=normalized,
        raise_on_shadow_mismatch=raise_on_shadow_mismatch,
    )
    token = _POLICY.set(policy)
    try:
        yield policy
    finally:
        _POLICY.reset(token)


def clear_kernel_v3_canary_cache() -> None:
    """Clear the process-local prepared-program canary cache."""
    _PREPARED_CACHE.clear()


def kernel_v3_canary(spec: KernelV3CanarySpec | None = None, **kwargs: Any) -> Callable[[type], type]:
    """Attach a kernel-v3 canary spec to a task class."""
    resolved = spec or KernelV3CanarySpec(**kwargs)

    def decorate(cls: type) -> type:
        cls._kernel_v3_canary_spec = resolved  # type: ignore[attr-defined]
        return cls

    return decorate


async def run_kernel_v3_canary(
    *,
    target: Any,
    executor: Callable[[], Any],
    output_fields: Mapping[str, Any],
    spec: KernelV3CanarySpec,
) -> KernelV3CanaryReport:
    """Run one canary decision for a programmatic task executor."""
    policy = get_kernel_v3_canary_policy()
    if policy.mode == KernelV3CanaryMode.OFF:
        start = time.perf_counter()
        await _call_executor(executor)
        return KernelV3CanaryReport(
            mode=policy.mode,
            authoritative="existing",
            v3_ran=False,
            fallback_ran=True,
            fallback_duration_ms=_elapsed_ms(start),
        )

    if policy.mode == KernelV3CanaryMode.SHADOW:
        return await _run_shadow(
            target=target,
            executor=executor,
            output_fields=output_fields,
            spec=spec,
            policy=policy,
        )

    return await _run_canary(
        target=target,
        executor=executor,
        output_fields=output_fields,
        spec=spec,
    )


async def _run_shadow(
    *,
    target: Any,
    executor: Callable[[], Any],
    output_fields: Mapping[str, Any],
    spec: KernelV3CanarySpec,
    policy: KernelV3CanaryPolicy,
) -> KernelV3CanaryReport:
    start_existing = time.perf_counter()
    await _call_executor(executor)
    existing_duration_ms = _elapsed_ms(start_existing)
    existing_outputs = _read_outputs(target, output_fields)

    if not spec.shadow_safe:
        return KernelV3CanaryReport(
            mode=KernelV3CanaryMode.SHADOW,
            authoritative="existing",
            v3_ran=False,
            fallback_ran=True,
            fallback_reason="shadow_not_safe",
            fallback_duration_ms=existing_duration_ms,
        )

    prepared_cache_hit = False
    v3_boundary: Literal["none", "execution", "replay"] = (
        "replay" if spec.host_observation_adapter is not None else "execution"
    )
    start_v3 = time.perf_counter()
    try:
        v3_result = await _run_v3(target, spec)
        prepared_cache_hit = v3_result.prepared_cache_hit
        v3_boundary = v3_result.boundary
        replay_metadata = v3_result.replay_metadata
        v3_outputs = _adapt_outputs(v3_result.outcome, target, output_fields, spec)
        mismatch_reason = None if v3_outputs == existing_outputs else "output_mismatch"
    except Exception as exc:  # noqa: BLE001
        replay_metadata = _replay_metadata_from_exception(exc)
        mismatch_reason = f"v3_failed:{type(exc).__name__}:{exc}"
    v3_duration_ms = _elapsed_ms(start_v3)

    if mismatch_reason is not None and policy.raise_on_shadow_mismatch:
        raise KernelV3CanaryMismatchError(mismatch_reason)

    return KernelV3CanaryReport(
        mode=KernelV3CanaryMode.SHADOW,
        authoritative="existing",
        v3_ran=True,
        fallback_ran=True,
        prepared_cache_hit=prepared_cache_hit,
        mismatch_reason=mismatch_reason,
        v3_boundary=v3_boundary,
        v3_replay_transition_refs=replay_metadata.transition_refs,
        v3_replay_open_source_keys=replay_metadata.open_source_keys,
        v3_replay_consumed_source_keys=replay_metadata.consumed_source_keys,
        v3_duration_ms=v3_duration_ms,
        fallback_duration_ms=existing_duration_ms,
    )


async def _run_canary(
    *,
    target: Any,
    executor: Callable[[], Any],
    output_fields: Mapping[str, Any],
    spec: KernelV3CanarySpec,
) -> KernelV3CanaryReport:
    prepared_cache_hit = False
    v3_boundary: Literal["none", "execution", "replay"] = (
        "replay" if spec.host_observation_adapter is not None else "execution"
    )
    start_v3 = time.perf_counter()
    try:
        v3_result = await _run_v3(target, spec)
        prepared_cache_hit = v3_result.prepared_cache_hit
        v3_boundary = v3_result.boundary
        replay_metadata = v3_result.replay_metadata
        outputs = _adapt_outputs(v3_result.outcome, target, output_fields, spec)
    except Exception as exc:  # noqa: BLE001
        v3_duration_ms = _elapsed_ms(start_v3)
        replay_metadata = _replay_metadata_from_exception(exc)
        start_fallback = time.perf_counter()
        await _call_executor(executor)
        return KernelV3CanaryReport(
            mode=KernelV3CanaryMode.CANARY,
            authoritative="existing",
            v3_ran=True,
            fallback_ran=True,
            prepared_cache_hit=prepared_cache_hit,
            fallback_reason=f"v3_failed:{type(exc).__name__}:{exc}",
            v3_boundary=v3_boundary,
            v3_replay_transition_refs=replay_metadata.transition_refs,
            v3_replay_open_source_keys=replay_metadata.open_source_keys,
            v3_replay_consumed_source_keys=replay_metadata.consumed_source_keys,
            v3_duration_ms=v3_duration_ms,
            fallback_duration_ms=_elapsed_ms(start_fallback),
        )

    _write_outputs(target, outputs)
    return KernelV3CanaryReport(
        mode=KernelV3CanaryMode.CANARY,
        authoritative="v3",
        v3_ran=True,
        fallback_ran=False,
        prepared_cache_hit=prepared_cache_hit,
        v3_boundary=v3_boundary,
        v3_replay_transition_refs=replay_metadata.transition_refs,
        v3_replay_open_source_keys=replay_metadata.open_source_keys,
        v3_replay_consumed_source_keys=replay_metadata.consumed_source_keys,
        v3_duration_ms=_elapsed_ms(start_v3),
    )


async def _call_executor(executor: Callable[[], Any]) -> None:
    if asyncio.iscoroutinefunction(executor):
        await executor()
        return
    await asyncio.to_thread(executor)


async def _run_v3(
    target: Any,
    spec: KernelV3CanarySpec,
) -> _V3RunResult:
    prepared, cache_hit = _prepare(target, spec)
    env = spec.env_factory(target) if spec.env_factory is not None else None
    if spec.host_observation_adapter is None:
        return _V3RunResult(
            outcome=run_kernel(prepared, env=env),
            prepared_cache_hit=cache_hit,
            boundary="execution",
        )
    outcome, state = await _run_v3_replay(prepared, env, target, spec)
    return _V3RunResult(
        outcome=outcome,
        prepared_cache_hit=cache_hit,
        boundary="replay",
        replay_metadata=_replay_metadata_from_state(state),
    )


async def _run_v3_replay(
    prepared: PreparedKernelProgram,
    env: Env | None,
    target: Any,
    spec: KernelV3CanarySpec,
) -> tuple[SourceOutcome, KernelReplayState]:
    if spec.max_replay_transitions < 0:
        raise ValueError("kernel-v3 canary max_replay_transitions must be non-negative")
    session, transition = KernelReplaySession.start(prepared, env=env, registry=spec.effect_registry)
    for _index in range(spec.max_replay_transitions + 1):
        if isinstance(transition.payload, ReplayableCompleted):
            return transition.payload.outcome, session.state
        request = session.current_request_descriptor()
        if request is None:
            raise TypeError(f"unsupported kernel-v3 replay payload: {type(transition.payload).__name__}")
        if _index >= spec.max_replay_transitions:
            raise RuntimeError("kernel-v3 replay canary exceeded max_replay_transitions")
        observation = await _host_observation_from_adapter(spec, request, target)
        observation = host_completed_from_json(json.loads(json.dumps(host_completed_to_json(observation))))
        transition = session.resume_current(observation, registry=spec.effect_registry)
    raise RuntimeError("kernel-v3 replay canary exceeded max_replay_transitions")


def _replay_metadata_from_state(state: KernelReplayState) -> _ReplayMetadata:
    return _ReplayMetadata(
        transition_refs=state.transition_refs,
        open_source_keys=state.open_source_keys,
        consumed_source_keys=state.consumed_source_keys,
    )


def _replay_metadata_from_exception(exc: Exception) -> _ReplayMetadata:
    if isinstance(exc, KernelReplayRejected):
        return _replay_metadata_from_state(exc.state)
    return _ReplayMetadata()


async def _host_observation_from_adapter(
    spec: KernelV3CanarySpec,
    request: ExternalEffectRequestDescriptor,
    target: Any,
) -> HostCompleted:
    if spec.host_observation_adapter is None:
        raise RuntimeError("kernel-v3 replay canary requires host_observation_adapter")
    observation = spec.host_observation_adapter(request, target)
    if inspect.isawaitable(observation):
        observation = await observation
    if not isinstance(observation, HostCompleted):
        raise TypeError("kernel-v3 replay canary host_observation_adapter must return HostCompleted")
    return observation


def _prepare(target: Any, spec: KernelV3CanarySpec) -> tuple[PreparedKernelProgram, bool]:
    if spec.cache_key is not None:
        cached = _PREPARED_CACHE.get(spec.cache_key)
        if cached is not None:
            if cached.program_factory is not spec.program_factory:
                raise RuntimeError(
                    "kernel-v3 canary cache_key collision: "
                    f"{spec.cache_key!r} is already bound to a different program factory"
                )
            return cached.prepared, True

    program = spec.program_factory(target)
    # Default to CORE_A for runtime canary parity with the operational corpus
    # per 2026-05-23 SD "Profile-attachment migration"; strict -lite admission
    # is a separate code path that callers opt into.
    from shepherd_kernel_v3_reference.profiles import CORE_A
    prepared = (
        program if isinstance(program, PreparedKernelProgram)
        else prepare_kernel_program(program, profile=CORE_A)
    )
    if spec.cache_key is not None:
        _PREPARED_CACHE[spec.cache_key] = _PreparedCacheEntry(
            prepared=prepared,
            program_factory=spec.program_factory,
        )
    return prepared, False


def _adapt_outputs(
    outcome: SourceOutcome,
    target: Any,
    output_fields: Mapping[str, Any],
    spec: KernelV3CanarySpec,
) -> dict[str, Any]:
    if spec.output_adapter is not None:
        raw_outputs = dict(spec.output_adapter(outcome, target))
    else:
        raw_outputs = _default_outputs(outcome, output_fields)
    return _validate_outputs(raw_outputs, output_fields)


def _default_outputs(outcome: SourceOutcome, output_fields: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(outcome, Completed):
        raise TypeError(f"kernel-v3 canary expected Completed outcome, got {type(outcome).__name__}")
    if not output_fields:
        return {}
    if len(output_fields) == 1:
        name = next(iter(output_fields))
        return {name: outcome.value}
    if isinstance(outcome.value, Mapping):
        return {name: outcome.value[name] for name in output_fields}
    raise RuntimeError("kernel-v3 canary multi-output tasks require a mapping Completed value")


def _validate_outputs(outputs: Mapping[str, Any], output_fields: Mapping[str, Any]) -> dict[str, Any]:
    if not output_fields:
        return {}
    missing = sorted(set(output_fields) - set(outputs))
    if missing:
        raise RuntimeError(f"kernel-v3 canary outputs missing fields: {missing}")
    wrapper_fields: dict[str, Any] = {name: (field.inner_type, ...) for name, field in output_fields.items()}
    wrapper = create_model("_KernelV3CanaryOutputs", **wrapper_fields)
    parsed = wrapper.model_validate({name: outputs[name] for name in output_fields})
    return {name: getattr(parsed, name) for name in output_fields}


def _read_outputs(target: Any, output_fields: Mapping[str, Any]) -> dict[str, Any]:
    return {name: getattr(target, name, None) for name in output_fields}


def _write_outputs(target: Any, outputs: Mapping[str, Any]) -> None:
    for name, value in outputs.items():
        setattr(target, name, value)


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000


__all__ = [
    "KernelV3CanaryMismatchError",
    "KernelV3CanaryMode",
    "KernelV3CanaryPolicy",
    "KernelV3CanaryReport",
    "KernelV3CanarySpec",
    "clear_kernel_v3_canary_cache",
    "get_kernel_v3_canary_policy",
    "kernel_v3_canary",
    "kernel_v3_canary_policy",
    "run_kernel_v3_canary",
]
