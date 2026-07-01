from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from shepherd_kernel_v3_reference.kernel import (
    ExternalEffectRequestRef,
    HostCompleted,
    KernelProgram,
    KernelReplayJournal,
    KernelReplaySession,
    KernelReplayState,
    PreparedKernelProgram,
    ReplayableCompleted,
    ReplayableExternalEffectRequest,
    ReplayableKernelTransition,
    elaborate,
    elaborate_publication_experimental,
    kernel_replay_journal_current_request,
    kernel_replay_journal_current_request_descriptor,
    kernel_replay_journal_from_json,
    kernel_replay_journal_to_json,
    kernel_replay_state_from_journal,
    kernel_replay_state_from_json,
    kernel_replay_state_to_json,
    prepare_kernel_program,
    run_kernel,
)
from shepherd_kernel_v3_reference.kernel.continuation_objects import ContinuationObject, continuation_object_to_json
from shepherd_kernel_v3_reference.schemas import AnySchema
from shepherd_kernel_v3_reference.source.experimental import TerminalFork
from shepherd_kernel_v3_reference.source.handlers import HandlerEnv, StaticHandlerInstall
from shepherd_kernel_v3_reference.source.syntax import Computation, Handle, Let, Lit, Perform, Resume, Return, Var
from shepherd_kernel_v3_reference.source.values import Env
from shepherd_kernel_v3_reference.trace.machine import TraceResult, run_trace
from shepherd_kernel_v3_reference.trace.validate import (
    TRACE_EVIDENCE_BUNDLE_SCHEMA_VERSION,
    TraceEvidenceBundle,
    TraceValidationError,
    validate_publication_experimental_trace,
    validate_publication_experimental_trace_prefix,
    validate_runtime_trace,
    validate_runtime_trace_prefix,
    validate_trace_evidence,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


@dataclass(frozen=True)
class Workload:
    name: str
    build: Callable[[int], KernelProgram]


@dataclass(frozen=True)
class Measurement:
    workload: str
    size: int
    prepare_ms: float
    run_kernel_raw_ms: float
    run_kernel_prepared_ms: float
    run_trace_raw_ms: float
    run_trace_prepared_ms: float
    run_trace_debug_raw_ms: float
    run_trace_debug_prepared_ms: float
    validate_runtime_ms: float
    validate_evidence_ms: float
    validate_evidence_status: str
    trace_records: int
    evidence_objects: int
    evidence_bytes: int
    env_binding_reads: int
    replay_start_ms: float
    replay_loop_ms: float
    replay_journal_closure_ms: float
    replay_journal_closure_object_gets: int
    replay_descriptor_ms: float
    replay_materialize_ms: float
    replay_long_open_descriptor_ms: float
    replay_long_open_materialize_ms: float
    replay_state_json_ms: float
    replay_journal_ms: float
    replay_status: str
    replay_transitions: int
    replay_consumed_sources: int
    replay_transition_bytes: int
    replay_object_catalog_entries: int
    replay_artifact_catalog_bytes: int
    replay_object_catalog_bytes: int
    replay_journal_bytes: int


@dataclass(frozen=True)
class ReplayMeasurement:
    start_ms: float
    loop_ms: float
    journal_closure_ms: float
    journal_closure_object_gets: int
    descriptor_ms: float
    materialize_ms: float
    long_open_descriptor_ms: float
    long_open_materialize_ms: float
    state_json_ms: float
    journal_ms: float
    status: str
    transitions: int
    consumed_sources: int
    transition_bytes: int
    object_catalog_entries: int
    artifact_catalog_bytes: int
    object_catalog_bytes: int
    journal_bytes: int


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark kernel-v3 runtime, trace, and optional evidence paths.")
    parser.add_argument("--sizes", default="25,50,100,200", help="comma-separated workload sizes")
    parser.add_argument("--repeat", type=int, default=5, help="runs per measurement; median is reported")
    parser.add_argument(
        "--workload",
        choices=tuple(workload.name for workload in WORKLOADS),
        action="append",
        help="workload to run; may be repeated; defaults to all workloads",
    )
    parser.add_argument(
        "--check-linear",
        action="store_true",
        help="fail if prepared runtime/trace timings scale superlinearly by a generous local threshold",
    )
    parser.add_argument(
        "--linear-tolerance",
        type=float,
        default=4.0,
        help="allowed multiple over size-normalized linear scaling for --check-linear",
    )
    parser.add_argument(
        "--linear-jitter-ms",
        type=float,
        default=5.0,
        help="absolute local timing slack for --check-linear",
    )
    args = parser.parse_args()

    sizes = tuple(int(size) for size in args.sizes.split(",") if size)
    selected = tuple(workload for workload in WORKLOADS if args.workload is None or workload.name in args.workload)

    measurements: list[Measurement] = []
    sys.stdout.write(
        "workload,size,prepare_ms,run_kernel_raw_ms,run_kernel_prepared_ms,"
        "run_trace_raw_ms,run_trace_prepared_ms,run_trace_debug_raw_ms,run_trace_debug_prepared_ms,"
        "validate_runtime_ms,validate_evidence_ms,validate_evidence_status,"
        "trace_records,evidence_objects,evidence_bytes,env_binding_reads,"
        "replay_start_ms,replay_loop_ms,replay_journal_closure_ms,replay_descriptor_ms,"
        "replay_journal_closure_object_gets,replay_materialize_ms,"
        "replay_long_open_descriptor_ms,replay_long_open_materialize_ms,"
        "replay_state_json_ms,replay_journal_ms,replay_status,"
        "replay_transitions,replay_consumed_sources,replay_transition_bytes,"
        "replay_object_catalog_entries,replay_artifact_catalog_bytes,"
        "replay_object_catalog_bytes,replay_journal_bytes\n"
    )
    for workload in selected:
        for size in sizes:
            measurement = measure(workload, size=size, repeat=args.repeat)
            measurements.append(measurement)
            sys.stdout.write(
                f"{measurement.workload},{measurement.size},"
                f"{measurement.prepare_ms:.3f},"
                f"{measurement.run_kernel_raw_ms:.3f},{measurement.run_kernel_prepared_ms:.3f},"
                f"{measurement.run_trace_raw_ms:.3f},{measurement.run_trace_prepared_ms:.3f},"
                f"{measurement.run_trace_debug_raw_ms:.3f},{measurement.run_trace_debug_prepared_ms:.3f},"
                f"{measurement.validate_runtime_ms:.3f},"
                f"{measurement.validate_evidence_ms:.3f},{measurement.validate_evidence_status},"
                f"{measurement.trace_records},"
                f"{measurement.evidence_objects},{measurement.evidence_bytes},{measurement.env_binding_reads},"
                f"{measurement.replay_start_ms:.3f},{measurement.replay_loop_ms:.3f},"
                f"{measurement.replay_journal_closure_ms:.3f},{measurement.replay_descriptor_ms:.3f},"
                f"{measurement.replay_journal_closure_object_gets},"
                f"{measurement.replay_materialize_ms:.3f},"
                f"{measurement.replay_long_open_descriptor_ms:.3f},"
                f"{measurement.replay_long_open_materialize_ms:.3f},"
                f"{measurement.replay_state_json_ms:.3f},{measurement.replay_journal_ms:.3f},"
                f"{measurement.replay_status},"
                f"{measurement.replay_transitions},{measurement.replay_consumed_sources},"
                f"{measurement.replay_transition_bytes},{measurement.replay_object_catalog_entries},"
                f"{measurement.replay_artifact_catalog_bytes},"
                f"{measurement.replay_object_catalog_bytes},{measurement.replay_journal_bytes}\n"
            )
    if args.check_linear:
        _check_linear(measurements, tolerance=args.linear_tolerance, jitter_ms=args.linear_jitter_ms)


def measure(workload: Workload, *, size: int, repeat: int) -> Measurement:
    program = workload.build(size)
    env_reads: Counter[str] = Counter()

    def timed(fn: Callable[[], Any]) -> float:
        samples = []
        for _ in range(repeat):
            start = time.perf_counter()
            fn()
            samples.append((time.perf_counter() - start) * 1_000)
        return statistics.median(samples)

    original_bindings = Env.bindings.fget
    if original_bindings is None:
        raise RuntimeError("Env.bindings is not a property")

    def counted_bindings(self: Env) -> tuple[tuple[str, Any], ...]:
        env_reads["bindings"] += 1
        return original_bindings(self)

    Env.bindings = property(counted_bindings)  # type: ignore[method-assign]
    try:
        prepare_ms = timed(lambda: prepare_kernel_program(program))
        prepared = prepare_kernel_program(program)
        run_kernel_raw_ms = timed(lambda: run_kernel(program))
        run_kernel_prepared_ms = timed(lambda: run_kernel(prepared))
        run_trace_raw_ms = timed(lambda: run_trace(program))
        run_trace_prepared_ms = timed(lambda: run_trace(prepared))
        run_trace_debug_raw_ms = timed(lambda: run_trace(program, include_debug_evidence=True))
        run_trace_debug_prepared_ms = timed(lambda: run_trace(prepared, include_debug_evidence=True))
        debug_result = run_trace(prepared, include_debug_evidence=True)
        validate_runtime_ms = timed(lambda: _validate_lifecycle(debug_result))
        validate_evidence_ms, validate_evidence_status = _timed_optional(
            lambda: validate_trace_evidence(_bundle(debug_result)),
            repeat=repeat,
        )
        replay_measurement = _measure_replay(prepared, repeat=repeat)
    finally:
        Env.bindings = property(original_bindings)  # type: ignore[method-assign]

    evidence = debug_result.require_debug_evidence()
    return Measurement(
        workload=workload.name,
        size=size,
        prepare_ms=prepare_ms,
        run_kernel_raw_ms=run_kernel_raw_ms,
        run_kernel_prepared_ms=run_kernel_prepared_ms,
        run_trace_raw_ms=run_trace_raw_ms,
        run_trace_prepared_ms=run_trace_prepared_ms,
        run_trace_debug_raw_ms=run_trace_debug_raw_ms,
        run_trace_debug_prepared_ms=run_trace_debug_prepared_ms,
        validate_runtime_ms=validate_runtime_ms,
        validate_evidence_ms=validate_evidence_ms,
        validate_evidence_status=validate_evidence_status,
        trace_records=len(debug_result.trace),
        evidence_objects=len(evidence.continuation_objects),
        evidence_bytes=_evidence_bytes(evidence.continuation_objects.values()),
        env_binding_reads=env_reads["bindings"],
        replay_start_ms=replay_measurement.start_ms,
        replay_loop_ms=replay_measurement.loop_ms,
        replay_journal_closure_ms=replay_measurement.journal_closure_ms,
        replay_journal_closure_object_gets=replay_measurement.journal_closure_object_gets,
        replay_descriptor_ms=replay_measurement.descriptor_ms,
        replay_materialize_ms=replay_measurement.materialize_ms,
        replay_long_open_descriptor_ms=replay_measurement.long_open_descriptor_ms,
        replay_long_open_materialize_ms=replay_measurement.long_open_materialize_ms,
        replay_state_json_ms=replay_measurement.state_json_ms,
        replay_journal_ms=replay_measurement.journal_ms,
        replay_status=replay_measurement.status,
        replay_transitions=replay_measurement.transitions,
        replay_consumed_sources=replay_measurement.consumed_sources,
        replay_transition_bytes=replay_measurement.transition_bytes,
        replay_object_catalog_entries=replay_measurement.object_catalog_entries,
        replay_artifact_catalog_bytes=replay_measurement.artifact_catalog_bytes,
        replay_object_catalog_bytes=replay_measurement.object_catalog_bytes,
        replay_journal_bytes=replay_measurement.journal_bytes,
    )


def _bundle(result: TraceResult) -> TraceEvidenceBundle:
    evidence = result.require_debug_evidence()
    return TraceEvidenceBundle(
        bundle_schema_version=TRACE_EVIDENCE_BUNDLE_SCHEMA_VERSION,
        trace=result.trace,
        continuation_root_refs=evidence.continuation_root_refs,
        continuation_objects=dict(evidence.continuation_objects),
        validation_profile="runtime-with-continuations",
        continuation_ref_map=evidence.continuation_ref_map,
        continuation_control_ref_map=evidence.continuation_control_ref_map,
        context_ref_map=evidence.context_ref_map,
    )


def _validate_lifecycle(result: TraceResult) -> None:
    validators = (
        validate_runtime_trace,
        validate_publication_experimental_trace,
        validate_runtime_trace_prefix,
        validate_publication_experimental_trace_prefix,
    )
    last_error: TraceValidationError | None = None
    for validator in validators:
        try:
            validator(result.trace)
            return
        except TraceValidationError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error


def _timed_optional(fn: Callable[[], Any], *, repeat: int) -> tuple[float, str]:
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        try:
            fn()
        except TraceValidationError:
            return float("nan"), "unsupported"
        samples.append((time.perf_counter() - start) * 1_000)
    return statistics.median(samples), "ok"


def _measure_replay(prepared: PreparedKernelProgram, *, repeat: int) -> ReplayMeasurement:
    try:
        state, _transitions = _run_replay_loop(prepared)
    except Exception as exc:  # noqa: BLE001
        return ReplayMeasurement(
            start_ms=float("nan"),
            loop_ms=float("nan"),
            journal_closure_ms=float("nan"),
            journal_closure_object_gets=0,
            descriptor_ms=float("nan"),
            materialize_ms=float("nan"),
            long_open_descriptor_ms=float("nan"),
            long_open_materialize_ms=float("nan"),
            state_json_ms=float("nan"),
            journal_ms=float("nan"),
            status=f"unsupported:{type(exc).__name__}",
            transitions=0,
            consumed_sources=0,
            transition_bytes=0,
            object_catalog_entries=0,
            artifact_catalog_bytes=0,
            object_catalog_bytes=0,
            journal_bytes=0,
        )

    def timed(fn: Callable[[], Any]) -> float:
        samples = []
        for _ in range(repeat):
            start = time.perf_counter()
            fn()
            samples.append((time.perf_counter() - start) * 1_000)
        return statistics.median(samples)

    session, journal_state, journal_transitions = _run_replay_session(prepared)
    journal = session.to_journal()
    open_session, _open_transition = KernelReplaySession.start(prepared)
    open_journal = open_session.to_journal()
    long_open_journal = _long_open_journal(journal)
    state_json = kernel_replay_state_to_json(state)
    journal_json = kernel_replay_journal_to_json(journal)
    return ReplayMeasurement(
        start_ms=timed(lambda: KernelReplaySession.start(prepared)),
        loop_ms=timed(lambda: _run_replay_loop(prepared)),
        journal_closure_ms=timed(lambda: session.to_journal()),
        journal_closure_object_gets=_journal_closure_object_gets(session),
        descriptor_ms=timed(lambda: _journal_current_request_descriptor(open_journal)),
        materialize_ms=timed(lambda: kernel_replay_journal_current_request(open_journal)),
        long_open_descriptor_ms=_timed_or_nan(
            lambda: _journal_current_request_descriptor(long_open_journal),
            repeat=repeat,
            enabled=long_open_journal is not None,
        ),
        long_open_materialize_ms=_timed_or_nan(
            lambda: kernel_replay_journal_current_request(long_open_journal),
            repeat=repeat,
            enabled=long_open_journal is not None,
        ),
        state_json_ms=timed(lambda: kernel_replay_state_from_json(prepared, json.loads(json.dumps(state_json)))),
        journal_ms=timed(
            lambda: kernel_replay_state_from_journal(
                prepared,
                kernel_replay_journal_from_json(json.loads(json.dumps(journal_json))),
            )
        ),
        status="ok",
        transitions=len(journal_transitions),
        consumed_sources=len(journal_state.consumed_source_keys),
        transition_bytes=len(json.dumps(journal_json["transitions"], sort_keys=True)),
        object_catalog_entries=len(journal.continuation_objects),
        artifact_catalog_bytes=len(json.dumps(journal_json["artifacts"], sort_keys=True)),
        object_catalog_bytes=len(json.dumps(journal_json["continuation_objects"], sort_keys=True)),
        journal_bytes=len(json.dumps(journal_json, sort_keys=True)),
    )


def _timed_or_nan(fn: Callable[[], Any], *, repeat: int, enabled: bool) -> float:
    if not enabled:
        return float("nan")
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1_000)
    return statistics.median(samples)


def _long_open_journal(journal: KernelReplayJournal) -> KernelReplayJournal | None:
    if len(journal.transitions) < 2:
        return None
    terminal = journal.transitions[-1]
    open_transition = journal.transitions[-2]
    if not isinstance(terminal.payload, ReplayableCompleted):
        return journal
    if not isinstance(open_transition.payload, ExternalEffectRequestRef):
        return None
    return KernelReplayJournal(
        program_ref=journal.program_ref,
        transitions=journal.transitions[:-1],
        continuation_objects=journal.continuation_objects,
        artifacts=journal.artifacts,
    )


def _journal_closure_object_gets(session: KernelReplaySession) -> int:
    evaluator = session._evaluator
    original_get = evaluator.get_continuation_object
    object_gets = 0

    def counted_get(ref: str) -> ContinuationObject:
        nonlocal object_gets
        object_gets += 1
        return original_get(ref)

    evaluator.get_continuation_object = counted_get  # type: ignore[method-assign]
    try:
        session.to_journal()
    finally:
        evaluator.get_continuation_object = original_get  # type: ignore[method-assign]
    return object_gets


def _run_replay_loop(
    prepared: PreparedKernelProgram,
) -> tuple[KernelReplayState, tuple[ReplayableKernelTransition, ...]]:
    _session, state, transitions = _run_replay_session(prepared)
    return state, transitions


def _run_replay_session(
    prepared: PreparedKernelProgram,
) -> tuple[KernelReplaySession, KernelReplayState, tuple[ReplayableKernelTransition, ...]]:
    session, transition = KernelReplaySession.start(prepared)
    for index in range(10_000):
        if isinstance(transition.payload, ReplayableCompleted):
            return session, session.state, session.transitions
        request = session.current_request()
        if request is None:
            raise TypeError(f"unsupported replay payload: {type(transition.payload).__name__}")
        transition = session.resume_current(_host_observation_for(request, index))
    raise RuntimeError("replay loop exceeded transition limit")


def _journal_current_request_descriptor(journal: Any) -> Any:
    return kernel_replay_journal_current_request_descriptor(journal)


def _host_observation_for(request: ReplayableExternalEffectRequest, index: int) -> HostCompleted:
    payload = request.payload
    if isinstance(payload, dict) and isinstance(payload.get("i"), int):
        return HostCompleted(f"value:{payload['i']}")
    if isinstance(payload, dict) and "prompt" in payload:
        return HostCompleted(f"{payload['prompt']}:result")
    return HostCompleted(f"value:{index}")


def _check_linear(measurements: Iterable[Measurement], *, tolerance: float, jitter_ms: float) -> None:
    failures: list[str] = []
    by_workload: dict[str, list[Measurement]] = {}
    for measurement in measurements:
        by_workload.setdefault(measurement.workload, []).append(measurement)

    metrics: tuple[tuple[str, Callable[[Measurement], float]], ...] = (
        ("run_kernel_prepared_ms", lambda measurement: measurement.run_kernel_prepared_ms),
        ("run_trace_prepared_ms", lambda measurement: measurement.run_trace_prepared_ms),
        ("run_trace_debug_prepared_ms", lambda measurement: measurement.run_trace_debug_prepared_ms),
        ("validate_runtime_ms", lambda measurement: measurement.validate_runtime_ms),
        ("validate_evidence_ms", lambda measurement: measurement.validate_evidence_ms),
        ("replay_start_ms", lambda measurement: measurement.replay_start_ms),
        ("replay_loop_ms", lambda measurement: measurement.replay_loop_ms),
        ("replay_journal_closure_ms", lambda measurement: measurement.replay_journal_closure_ms),
        ("replay_descriptor_ms", lambda measurement: measurement.replay_descriptor_ms),
        ("replay_materialize_ms", lambda measurement: measurement.replay_materialize_ms),
        ("replay_long_open_descriptor_ms", lambda measurement: measurement.replay_long_open_descriptor_ms),
        ("replay_long_open_materialize_ms", lambda measurement: measurement.replay_long_open_materialize_ms),
        ("replay_state_json_ms", lambda measurement: measurement.replay_state_json_ms),
        ("replay_journal_ms", lambda measurement: measurement.replay_journal_ms),
    )
    for workload, rows in sorted(by_workload.items()):
        ordered = sorted(rows, key=lambda measurement: measurement.size)
        for row in ordered:
            if (
                row.replay_object_catalog_entries
                and row.replay_journal_closure_object_gets > row.replay_object_catalog_entries
            ):
                failures.append(
                    f"{workload} replay_journal_closure_object_gets {row.size}: "
                    f"{row.replay_journal_closure_object_gets} > {row.replay_object_catalog_entries}"
                )
        for previous, current in pairwise(ordered):
            size_ratio = current.size / previous.size
            for metric_name, getter in metrics:
                previous_value = getter(previous)
                current_value = getter(current)
                if not (math.isfinite(previous_value) and math.isfinite(current_value)):
                    continue
                allowed = max(jitter_ms, previous_value * size_ratio * tolerance)
                if current_value > allowed:
                    failures.append(
                        f"{workload} {metric_name} {previous.size}->{current.size}: "
                        f"{current_value:.3f}ms > {allowed:.3f}ms"
                    )
    if failures:
        for failure in failures:
            sys.stderr.write(f"linear check failed: {failure}\n")
        raise SystemExit(1)


def _evidence_bytes(objects: Iterable[ContinuationObject]) -> int:
    return sum(len(json.dumps(continuation_object_to_json(obj), sort_keys=True)) for obj in objects)


def pure_let_program(size: int) -> KernelProgram:
    body: Computation = Return(Lit("done"))
    for idx in reversed(range(size)):
        body = Let(f"x{idx}", Return(Lit(idx)), body)
    return elaborate(body)


def sequential_handled_effect_program(size: int) -> KernelProgram:
    body: Computation = Return(Lit("value"))
    for idx in reversed(range(size)):
        body = Let(f"y{idx}", Perform("eff.a", Lit({"i": idx})), body)
    return elaborate(Handle(body, HandlerEnv((_install("eff.a", Let("r", Resume(Lit("value")), Return(Var("r")))),))))


def sequential_external_effect_program(size: int) -> KernelProgram:
    body: Computation = Return(Lit("done"))
    for idx in reversed(range(size)):
        body = Let(f"external{idx}", Perform("provider.llm.generate", Lit({"i": idx})), body)
    return elaborate(body)


def nested_handler_program(size: int) -> KernelProgram:
    body: Computation = Perform("eff.0", Lit(None))
    for idx in range(size):
        body = Handle(body, HandlerEnv((_install(f"eff.{idx}", Resume(Lit(f"value:{idx}"))),)))
    return elaborate(body)


def publication_fork_program(size: int) -> KernelProgram:
    branch_values = tuple((f"branch:{idx}", Lit(idx)) for idx in range(max(1, size)))
    return elaborate_publication_experimental(
        Handle(Perform("eff.fork", Lit(None)), HandlerEnv((_install("eff.fork", TerminalFork(branch_values)),)))
    )


def _install(effect_kind: str, body: Computation) -> StaticHandlerInstall:
    return StaticHandlerInstall(
        effect_kind=effect_kind,
        handler_id=f"{effect_kind}.handler.v1",
        handled_result_schema=AnySchema(),
        payload_name="_payload",
        body=body,
    )


WORKLOADS = (
    Workload("pure-let", pure_let_program),
    Workload("sequential-handled-effect", sequential_handled_effect_program),
    Workload("sequential-external-effects", sequential_external_effect_program),
    Workload("nested-handlers", nested_handler_program),
    Workload("publication-fork", publication_fork_program),
)


if __name__ == "__main__":
    main()
