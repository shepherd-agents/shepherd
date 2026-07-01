import functools
from collections import Counter
from typing import TYPE_CHECKING, Any

import pytest

import shepherd_kernel_v3_reference.kernel as kernel_api
import shepherd_kernel_v3_reference.kernel.machine as kernel_machine
from shepherd_kernel_v3_reference.kernel import (
    continuation_objects,
    continuations,
    elaborate,
    elaborate_publication_experimental,
    program_identity,
    refs,
    run_kernel,
    runtime_services,
)
from shepherd_kernel_v3_reference.kernel.continuation_objects import ContinuationObjectBuilder
from shepherd_kernel_v3_reference.kernel.recursive_machine import RecursiveKernelEvaluator
from shepherd_kernel_v3_reference.kernel.runtime_services import EvidenceMode, EvidenceUnavailableError
from shepherd_kernel_v3_reference.kernel.step_machine import StepKernelEvaluator
from shepherd_kernel_v3_reference.schemas import AnySchema
from shepherd_kernel_v3_reference.source.experimental import TerminalDelay, TerminalFork
from shepherd_kernel_v3_reference.source.handlers import HandlerEnv, StaticHandlerInstall
from shepherd_kernel_v3_reference.source.outcomes import (
    Completed,
    Delayed,
    Forked,
    ResumptionUsed,
    SourceOutcome,
    Suspended,
)
from shepherd_kernel_v3_reference.source.syntax import Handle, Let, Lit, Perform, Resume, Return, Var
from shepherd_kernel_v3_reference.source.values import Env
from shepherd_kernel_v3_reference.trace.machine import TraceDebugEvidence, TraceResult, record_from_event, run_trace
from shepherd_kernel_v3_reference.trace.validate import (
    TRACE_EVIDENCE_BUNDLE_SCHEMA_VERSION,
    TraceEvidenceBundle,
    validate_trace_evidence,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def test_stable_identity_cache_preserves_trace_evidence_and_bounds_computes() -> None:
    program = _sequential_handled_effect_program(50)
    result, evaluator, _builder = _run_with_evaluator(program)

    validate_trace_evidence(_bundle(result))
    assert result.require_debug_evidence().program_ref == RecursiveKernelEvaluator(program).program_ref
    for record in result.trace:
        program_ref = getattr(record, "program_ref", None)
        if program_ref is not None:
            assert program_ref == result.require_debug_evidence().program_ref

    stats = evaluator._identity_stats
    install_count = sum(len(handler_env.bindings) for handler_env in evaluator.program.handler_envs.values())
    assert stats.program_ref_computes == 1
    assert stats.binder_ref_computes <= len(evaluator.program.binders)
    assert stats.binder_fingerprint_computes <= len(evaluator.program.binders)
    assert stats.handler_env_ref_computes <= len(evaluator.program.handler_envs)
    assert stats.handler_env_fingerprint_computes <= len(evaluator.program.handler_envs)
    assert stats.install_ref_computes <= install_count
    assert stats.install_fingerprint_computes <= install_count
    assert stats.schema_fingerprint_computes == 1
    assert stats.schema_ref_fingerprint_computes <= len(evaluator.program.schemas) + 1
    assert stats.control_fingerprint_computes <= (len(evaluator.program.binders) * 3) + 5
    assert stats.kont_state_from_frame_refs_rebuilds == 0
    assert stats.kont_state_from_frame_refs_replayed_frame_refs == 0


def test_program_identity_projection_handles_k500_under_default_recursion_limit() -> None:
    program = _sequential_handled_effect_program(500)
    evaluator = RecursiveKernelEvaluator(program)

    assert evaluator.program_ref.startswith("program:sha256:")
    assert evaluator._identity_stats.program_ref_computes == 1


def test_evaluator_uses_construction_time_program_mapping_snapshot() -> None:
    program = _sequential_handled_effect_program(2)
    evaluator = RecursiveKernelEvaluator(program)
    program_ref = evaluator.program_ref

    assert evaluator.program.binders is not program.binders
    assert evaluator.program.handler_envs is not program.handler_envs
    assert evaluator.program.schemas is not program.schemas

    program.binders.clear()
    program.handler_envs.clear()
    program.schemas.clear()

    assert evaluator.program_ref == program_ref
    assert evaluator.run() == Completed("value")


def test_run_kernel_routes_to_step_evaluator(monkeypatch: pytest.MonkeyPatch) -> None:
    program = elaborate(Return(Lit("done")))

    def blocked_recursive_run(_self: RecursiveKernelEvaluator, _env: object | None = None) -> SourceOutcome:
        raise AssertionError("run_kernel used the recursive evaluator")

    monkeypatch.setattr(RecursiveKernelEvaluator, "run", blocked_recursive_run)

    assert run_kernel(program) == Completed("done")


def test_run_kernel_uses_execution_only_evidence_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    program = elaborate(Return(Lit("done")))
    seen_modes = []
    original_init = StepKernelEvaluator.__init__

    def tracking_init(self: StepKernelEvaluator, *args: object, **kwargs: object) -> None:
        seen_modes.append(kwargs.get("evidence_mode"))
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(StepKernelEvaluator, "__init__", tracking_init)

    assert run_kernel(program) == Completed("done")
    assert seen_modes == ["none"]


def test_direct_step_evaluator_defaults_to_trace_evidence_mode() -> None:
    evaluator = StepKernelEvaluator(elaborate(Return(Lit("done"))))

    assert evaluator.evidence_mode is EvidenceMode.TRACE


def test_no_evidence_accessors_raise_documented_misuse_errors() -> None:
    evaluator = StepKernelEvaluator(elaborate(Return(Lit("done"))), evidence_mode=EvidenceMode.NONE)

    assert evaluator.run() == Completed("done")
    with pytest.raises(EvidenceUnavailableError):
        _ = evaluator.program_ref
    with pytest.raises(EvidenceUnavailableError):
        _ = evaluator.continuation_root_refs
    with pytest.raises(EvidenceUnavailableError):
        _ = evaluator.continuation_objects


def test_completed_run_kernel_rows_do_not_project_content_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    counts = _install_content_ref_spy(monkeypatch)
    term: object = Return(Lit("done"))
    for idx in reversed(range(100)):
        term = Let(f"x{idx}", Return(Lit(idx)), term)

    assert run_kernel(elaborate(term)) == Completed("done")
    assert counts == Counter()

    counts.clear()
    assert run_kernel(_sequential_handled_effect_program(25)) == Completed("value")
    assert counts == Counter()


def test_completed_run_kernel_rows_do_not_materialize_env_bindings(monkeypatch: pytest.MonkeyPatch) -> None:
    counts: Counter[str] = Counter()
    original_bindings = Env.bindings.fget
    assert original_bindings is not None

    def counted_bindings(self: Env) -> tuple[tuple[str, Any], ...]:
        counts["env.bindings"] += 1
        return original_bindings(self)

    monkeypatch.setattr(Env, "bindings", property(counted_bindings))

    assert run_kernel(_sequential_handled_effect_program(25)) == Completed("value")
    assert counts == Counter()


def test_run_trace_env_evidence_does_not_materialize_env_bindings(monkeypatch: pytest.MonkeyPatch) -> None:
    counts: Counter[str] = Counter()
    original_bindings = Env.bindings.fget
    assert original_bindings is not None

    def counted_bindings(self: Env) -> tuple[tuple[str, Any], ...]:
        counts["env.bindings"] += 1
        return original_bindings(self)

    monkeypatch.setattr(Env, "bindings", property(counted_bindings))

    result = run_trace(_sequential_handled_effect_program(50), include_debug_evidence=True)

    assert result.outcome == Completed("value")
    validate_trace_evidence(_bundle(result))
    assert counts == Counter()


def test_sequential_handled_step_evaluator_replay_counter_stays_flat() -> None:
    for effect_count in (10, 50, 100, 250):
        no_evidence_evaluator = StepKernelEvaluator(
            _sequential_handled_effect_program(effect_count),
            evidence_mode=EvidenceMode.NONE,
        )
        trace_evaluator = StepKernelEvaluator(
            _sequential_handled_effect_program(effect_count),
            evidence_mode=EvidenceMode.TRACE,
        )

        assert no_evidence_evaluator.run() == Completed("value")
        assert trace_evaluator.run() == Completed("value")
        assert no_evidence_evaluator._identity_stats.kont_state_from_frame_refs_replayed_frame_refs == 0
        assert trace_evaluator._identity_stats.kont_state_from_frame_refs_replayed_frame_refs == 0


def test_no_evidence_suspended_continuation_preserves_one_shot_semantics() -> None:
    outcome = run_kernel(elaborate(Let("x", Perform("eff.unhandled", Lit("payload")), Return(Var("x")))))

    assert isinstance(outcome, Suspended)
    assert outcome.effect_kind == "eff.unhandled"
    assert outcome.payload == "payload"
    assert outcome.continuation.apply("resumed") == Completed("resumed")
    with pytest.raises(ResumptionUsed):
        outcome.continuation.apply("again")


def test_no_evidence_terminal_delay_continuation_preserves_one_shot_semantics() -> None:
    program = elaborate_publication_experimental(
        Handle(
            Let("y", Perform("eff.a", Lit("payload")), Return(Var("y"))),
            HandlerEnv((_install("eff.a", TerminalDelay(Lit("waiting")), "h.delay"),)),
        )
    )

    outcome = run_kernel(program)

    assert isinstance(outcome, Delayed)
    assert outcome.reason == "waiting"
    assert outcome.pending.apply("resumed") == Completed("resumed")
    with pytest.raises(ResumptionUsed):
        outcome.pending.apply("again")


def test_no_evidence_terminal_fork_preserves_branch_semantics() -> None:
    program = elaborate_publication_experimental(
        Handle(
            Let("y", Perform("eff.a", Lit("payload")), Return(Var("y"))),
            HandlerEnv(
                (
                    _install(
                        "eff.a",
                        TerminalFork(
                            (
                                ("branch:A", Lit("value-A")),
                                ("branch:B", Lit("value-B")),
                            )
                        ),
                        "h.fork",
                    ),
                )
            ),
        )
    )

    outcome = run_kernel(program)

    assert isinstance(outcome, Forked)
    assert outcome.branches == {
        "branch:A": Completed("value-A"),
        "branch:B": Completed("value-B"),
    }


def test_step_evaluator_is_split_from_recursive_oracle() -> None:
    assert not issubclass(StepKernelEvaluator, RecursiveKernelEvaluator)
    for method_name in (
        "_eval",
        "_continue_value",
        "_perform",
        "_resume",
        "_terminal_delay",
        "_terminal_fork",
    ):
        assert not hasattr(StepKernelEvaluator, method_name)


def test_ambiguous_kernel_evaluator_alias_is_removed() -> None:
    assert "KernelEvaluator" not in kernel_api.__all__
    assert not hasattr(kernel_api, "KernelEvaluator")
    assert not hasattr(kernel_machine, "KernelEvaluator")


def test_stack_cursor_cache_hits_when_rebuilding_sequential_continuations() -> None:
    program = _sequential_handled_effect_program(8)
    result, _evaluator, builder = _run_with_evaluator(program)

    validate_trace_evidence(_bundle(result))
    assert builder._diagnostics.stack_cursor_cache_hits > 0
    assert builder._diagnostics.stack_cursor_cache_misses == len(builder._stack_cursor_cache)


def _run_with_evaluator(program):
    builder = ContinuationObjectBuilder()
    records = []
    evaluator = RecursiveKernelEvaluator(
        program,
        event_sink=lambda event: records.append(record_from_event(event)),
        continuation_builder=builder,
    )
    outcome = evaluator.run()
    result = TraceResult(
        outcome=outcome,
        trace=tuple(records),
        debug_evidence=TraceDebugEvidence(
            continuation_root_refs=evaluator.continuation_root_refs,
            continuation_objects=evaluator.continuation_objects,
            program_ref=evaluator.program_ref,
        ),
    )
    assert outcome == Completed("value")
    return result, evaluator, builder


def _bundle(result: TraceResult) -> TraceEvidenceBundle:
    evidence = result.require_debug_evidence()
    return TraceEvidenceBundle(
        bundle_schema_version=TRACE_EVIDENCE_BUNDLE_SCHEMA_VERSION,
        trace=result.trace,
        continuation_root_refs=evidence.continuation_root_refs,
        continuation_objects=evidence.continuation_objects,
        validation_profile="runtime-with-continuations",
        continuation_ref_map=evidence.continuation_ref_map,
        continuation_control_ref_map=evidence.continuation_control_ref_map,
        context_ref_map=evidence.context_ref_map,
    )


def _install_content_ref_spy(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    counts: Counter[str] = Counter()
    original_content_ref: Callable[[str, Any], str] = refs.content_ref

    def counted_content_ref(kind: str, payload: Any) -> str:
        counts[f"content:{kind}"] += 1
        return original_content_ref(kind, payload)

    def blocked_builder_init(self: ContinuationObjectBuilder, *args: object, **kwargs: object) -> None:
        counts["builder:init"] += 1
        original_builder_init(self, *args, **kwargs)

    original_builder_init = ContinuationObjectBuilder.__init__
    for module in (refs, runtime_services, continuation_objects, program_identity, continuations):
        monkeypatch.setattr(module, "content_ref", counted_content_ref)
    monkeypatch.setattr(ContinuationObjectBuilder, "__init__", blocked_builder_init)
    return counts


def _install(effect_kind: str, body: object, handler_id: str) -> StaticHandlerInstall:
    return StaticHandlerInstall(
        effect_kind=effect_kind,
        handler_id=handler_id,
        handled_result_schema=AnySchema(),
        payload_name="_payload",
        body=body,
    )


def _sequential_handled_effect_program(effect_count: int):
    term = functools.reduce(
        lambda body, idx: Let(f"y{idx}", Perform("eff.a", Lit({"i": idx})), body),
        reversed(range(effect_count)),
        Return(Var(f"y{effect_count - 1}")) if effect_count else Return(Lit(None)),
    )
    return elaborate(
        Handle(
            term,
            HandlerEnv(
                (
                    StaticHandlerInstall(
                        effect_kind="eff.a",
                        handler_id="machine-hardening-test.handler.v1",
                        handled_result_schema=AnySchema(),
                        payload_name="_payload",
                        body=Let("r", Resume(Lit("value")), Return(Var("r"))),
                    ),
                )
            ),
        )
    )
