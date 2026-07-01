from __future__ import annotations

from typing import TYPE_CHECKING

from shepherd_kernel_v3_reference.conformance import artifact_from_trace_result, conformance_artifact_to_json
from shepherd_kernel_v3_reference.kernel import elaborate, elaborate_publication_experimental
from shepherd_kernel_v3_reference.kernel.recursive_machine import RecursiveKernelEvaluator
from shepherd_kernel_v3_reference.kernel.step_machine import StepKernelEvaluator
from shepherd_kernel_v3_reference.schemas import AnySchema
from shepherd_kernel_v3_reference.source.experimental import Forward, TerminalDelay, TerminalFork
from shepherd_kernel_v3_reference.source.handlers import HandlerEnv, StaticHandlerInstall
from shepherd_kernel_v3_reference.source.outcomes import Completed, Delayed, Forked, SourceOutcome, Suspended
from shepherd_kernel_v3_reference.source.syntax import Abort, Handle, Let, Lit, Perform, Resume, Return, Var
from shepherd_kernel_v3_reference.trace.machine import (
    TraceDebugEvidence,
    TraceResult,
    TraceSession,
    record_from_event,
    run_trace,
)
from shepherd_kernel_v3_reference.trace.records import TerminalResumeResult

if TYPE_CHECKING:
    import pytest


def test_step_machine_successful_handled_resumption_matches_recursive_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = elaborate(_sequential_handled_effect_program(8))
    recursive = run_trace(program, engine="recursive", include_debug_evidence=True)
    records = []
    evaluator = StepKernelEvaluator(
        program,
        event_sink=lambda event: records.append(record_from_event(event)),
        evidence_mode="sidecar",
    )
    _block_recursive_scheduler(evaluator, monkeypatch)

    outcome = evaluator.run_step()
    stepped = TraceResult(
        outcome=outcome,
        trace=tuple(records),
        debug_evidence=TraceDebugEvidence(
            continuation_root_refs=evaluator.continuation_root_refs,
            continuation_objects=evaluator.continuation_objects,
            program_ref=evaluator.program_ref,
            continuation_ref_map=evaluator.continuation_ref_map,
            continuation_control_ref_map=evaluator.continuation_control_ref_map,
            context_ref_map=evaluator.context_ref_map,
        ),
    )

    assert stepped.outcome == recursive.outcome
    assert stepped.trace == recursive.trace
    assert (
        stepped.require_debug_evidence().continuation_root_refs
        == recursive.require_debug_evidence().continuation_root_refs
    )
    assert (
        stepped.require_debug_evidence().continuation_objects == recursive.require_debug_evidence().continuation_objects
    )
    assert conformance_artifact_to_json(artifact_from_trace_result(stepped)) == conformance_artifact_to_json(
        artifact_from_trace_result(recursive)
    )


def test_step_machine_top_level_unhandled_reentry_matches_recursive() -> None:
    program = elaborate(Let("x", Perform("eff.a", Lit("payload")), Return(Var("x"))))
    recursive_records = []
    recursive_evaluator = RecursiveKernelEvaluator(
        program,
        event_sink=lambda event: recursive_records.append(record_from_event(event)),
        evidence_mode="sidecar",
    )
    stepped, _evaluator = _run_step_trace(program)

    recursive_outcome = recursive_evaluator.run()
    assert isinstance(recursive_outcome, Suspended)
    assert isinstance(stepped.outcome, Suspended)
    assert stepped.outcome.effect_kind == recursive_outcome.effect_kind
    assert stepped.outcome.payload == recursive_outcome.payload
    assert stepped.trace == tuple(recursive_records)
    assert stepped.outcome.continuation.apply("value") == recursive_outcome.continuation.apply("value")


def test_step_machine_k500_successful_handled_resumption_completes() -> None:
    program = elaborate(_sequential_handled_effect_program(500))
    stepped, _evaluator = _run_step_trace(program)

    assert stepped.outcome == Completed("value")
    conformance_artifact_to_json(artifact_from_trace_result(stepped))


def test_step_machine_abort_matches_recursive_artifact() -> None:
    program = elaborate(
        Handle(
            Perform("eff.a", Lit("payload")),
            HandlerEnv((_install("eff.a", Abort(Lit("aborted")), "h.abort"),)),
        )
    )

    _assert_completed_step_matches_recursive(program)


def test_step_machine_forward_matches_recursive_artifact() -> None:
    term = Handle(
        Handle(
            Let("y", Perform("eff.a", Lit("payload")), Return(Var("y"))),
            HandlerEnv((_install("eff.a", Forward(), "h.inner"),)),
        ),
        HandlerEnv(
            (
                _install(
                    "eff.a",
                    Let("r", Resume(Lit("outer-value")), Return(Var("r"))),
                    "h.outer",
                ),
            )
        ),
    )
    program = elaborate_publication_experimental(term)

    _assert_step_trace_matches_recursive(program)


def test_step_machine_terminal_delay_reentry_matches_recursive_live_trace() -> None:
    program = elaborate_publication_experimental(
        Handle(
            Let("y", Perform("eff.a", Lit("payload")), Return(Var("y"))),
            HandlerEnv((_install("eff.a", TerminalDelay(Lit("waiting")), "h.delay"),)),
        )
    )
    recursive_session = TraceSession(program, engine="recursive")
    recursive_initial = recursive_session.run()
    step_records = []
    step_evaluator = StepKernelEvaluator(
        program,
        event_sink=lambda event: step_records.append(record_from_event(event)),
        evidence_mode="none",
    )
    step_initial = step_evaluator.run_step()

    assert isinstance(recursive_initial.outcome, Delayed)
    assert isinstance(step_initial, Delayed)
    assert step_initial.reason == recursive_initial.outcome.reason
    assert tuple(step_records) == recursive_initial.trace

    assert step_initial.pending.apply("resumed-value") == recursive_initial.outcome.pending.apply("resumed-value")
    assert tuple(step_records) == recursive_session.trace


def test_step_machine_terminal_delay_multi_hop_finalizer_matches_recursive_live_trace() -> None:
    program = elaborate_publication_experimental(_delay_suspends_twice_then_handles_downstream_term())
    recursive_session = TraceSession(program, engine="recursive")
    recursive_initial = recursive_session.run()
    step_records = []
    step_evaluator = StepKernelEvaluator(
        program,
        event_sink=lambda event: step_records.append(record_from_event(event)),
        evidence_mode="none",
    )
    step_initial = step_evaluator.run_step()

    assert isinstance(recursive_initial.outcome, Delayed)
    assert isinstance(step_initial, Delayed)
    assert step_initial.reason == recursive_initial.outcome.reason
    assert tuple(step_records) == recursive_session.trace

    step_first = step_initial.pending.apply("delay-resume")
    recursive_first = recursive_initial.outcome.pending.apply("delay-resume")
    assert isinstance(step_first, Suspended)
    assert isinstance(recursive_first, Suspended)
    assert not [record for record in step_records if isinstance(record, TerminalResumeResult)]
    assert tuple(step_records) == recursive_session.trace

    step_second = step_first.continuation.apply("first-resume")
    recursive_second = recursive_first.continuation.apply("first-resume")
    assert isinstance(step_second, Suspended)
    assert isinstance(recursive_second, Suspended)
    assert not [record for record in step_records if isinstance(record, TerminalResumeResult)]
    assert tuple(step_records) == recursive_session.trace

    assert step_second.continuation.apply("second-resume") == recursive_second.continuation.apply("second-resume")
    terminal_results = [record for record in step_records if isinstance(record, TerminalResumeResult)]
    assert len(terminal_results) == 1
    assert terminal_results[0].value == "handled-b"
    assert tuple(step_records) == recursive_session.trace


def test_step_machine_terminal_fork_matches_recursive_artifact() -> None:
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

    recursive = run_trace(program, engine="recursive", include_debug_evidence=True)
    stepped, _evaluator = _run_step_trace(program)
    assert isinstance(stepped.outcome, Forked)
    assert stepped.outcome == recursive.outcome
    assert stepped.trace == recursive.trace
    assert (
        stepped.require_debug_evidence().continuation_root_refs
        == recursive.require_debug_evidence().continuation_root_refs
    )
    assert (
        stepped.require_debug_evidence().continuation_objects == recursive.require_debug_evidence().continuation_objects
    )


def test_step_machine_terminal_fork_escaped_suspension_matches_recursive_live_trace() -> None:
    program = elaborate_publication_experimental(_fork_branch_suspends_then_handles_downstream_term())
    recursive_session = TraceSession(program, engine="recursive")
    recursive_initial = recursive_session.run()
    step_records = []
    step_evaluator = StepKernelEvaluator(
        program,
        event_sink=lambda event: step_records.append(record_from_event(event)),
        evidence_mode="none",
    )
    step_initial = step_evaluator.run_step()

    assert isinstance(recursive_initial.outcome, Forked)
    assert isinstance(step_initial, Forked)
    recursive_branch = recursive_initial.outcome.branches["branch:A"]
    step_branch = step_initial.branches["branch:A"]
    assert isinstance(recursive_branch, Suspended)
    assert isinstance(step_branch, Suspended)

    assert step_branch.continuation.apply("resumed-unhandled") == recursive_branch.continuation.apply(
        "resumed-unhandled"
    )
    assert tuple(step_records) == recursive_session.trace


def test_step_machine_terminal_fork_multi_hop_finalizer_matches_recursive_live_trace() -> None:
    program = elaborate_publication_experimental(_fork_branch_suspends_twice_then_handles_downstream_term())
    recursive_session = TraceSession(program, engine="recursive")
    recursive_initial = recursive_session.run()
    step_records = []
    step_evaluator = StepKernelEvaluator(
        program,
        event_sink=lambda event: step_records.append(record_from_event(event)),
        evidence_mode="none",
    )
    step_initial = step_evaluator.run_step()

    assert isinstance(recursive_initial.outcome, Forked)
    assert isinstance(step_initial, Forked)
    recursive_first = recursive_initial.outcome.branches["branch:A"]
    step_first = step_initial.branches["branch:A"]
    assert isinstance(recursive_first, Suspended)
    assert isinstance(step_first, Suspended)

    step_second = step_first.continuation.apply("first-resume")
    recursive_second = recursive_first.continuation.apply("first-resume")
    assert isinstance(step_second, Suspended)
    assert isinstance(recursive_second, Suspended)
    assert not [record for record in step_records if isinstance(record, TerminalResumeResult)]
    assert tuple(step_records) == recursive_session.trace

    assert step_second.continuation.apply("second-resume") == recursive_second.continuation.apply("second-resume")
    terminal_results = [record for record in step_records if isinstance(record, TerminalResumeResult)]
    assert len(terminal_results) == 1
    assert terminal_results[0].value == "handled-b"
    assert tuple(step_records) == recursive_session.trace


def test_step_machine_terminal_fork_deep_stress_is_stack_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    program = elaborate_publication_experimental(_sequential_terminal_fork_program(1000))
    evaluator = StepKernelEvaluator(program, event_sink=lambda event: None)
    _guard_run_loop_reentry(evaluator, monkeypatch)

    outcome = evaluator.run_step()

    for _ in range(1000):
        assert isinstance(outcome, Forked)
        outcome = outcome.branches["branch:A"]
    assert outcome == Completed("done")


def test_step_machine_covered_rows_do_not_call_recursive_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    for program, resume_value in (
        (elaborate(_sequential_handled_effect_program(3)), None),
        (
            elaborate(
                Handle(
                    Perform("eff.a", Lit("payload")),
                    HandlerEnv((_install("eff.a", Abort(Lit("aborted")), "h.abort"),)),
                )
            ),
            None,
        ),
        (
            elaborate_publication_experimental(
                Handle(
                    Handle(
                        Let("y", Perform("eff.a", Lit("payload")), Return(Var("y"))),
                        HandlerEnv((_install("eff.a", Forward(), "h.inner"),)),
                    ),
                    HandlerEnv((_install("eff.a", Let("r", Resume(Lit("outer-value")), Return(Var("r"))), "h.outer"),)),
                )
            ),
            None,
        ),
        (
            elaborate_publication_experimental(
                Handle(
                    Let("y", Perform("eff.a", Lit("payload")), Return(Var("y"))),
                    HandlerEnv((_install("eff.a", TerminalDelay(Lit("waiting")), "h.delay"),)),
                )
            ),
            "resumed-value",
        ),
        (
            elaborate_publication_experimental(
                Handle(
                    Let("y", Perform("eff.a", Lit("payload")), Return(Var("y"))),
                    HandlerEnv((_install("eff.a", TerminalFork((("branch:A", Lit("value-A")),)), "h.fork"),)),
                )
            ),
            None,
        ),
        (elaborate_publication_experimental(_fork_branch_suspends_then_handles_downstream_term()), "resumed-unhandled"),
    ):
        evaluator = StepKernelEvaluator(program, event_sink=lambda event: None)
        _block_recursive_scheduler(evaluator, monkeypatch)
        _guard_run_loop_reentry(evaluator, monkeypatch)
        outcome = evaluator.run_step()
        if isinstance(outcome, Delayed):
            assert resume_value is not None
            outcome.pending.apply(resume_value)
        if isinstance(outcome, Forked):
            branch = outcome.branches["branch:A"]
            if isinstance(branch, Suspended):
                assert resume_value is not None
                branch.continuation.apply(resume_value)


def _run_step_trace(
    program,
) -> tuple[TraceResult, StepKernelEvaluator]:
    records = []
    step_evaluator = StepKernelEvaluator(
        program,
        event_sink=lambda event: records.append(record_from_event(event)),
        evidence_mode="sidecar",
    )
    outcome = step_evaluator.run_step()
    return (
        TraceResult(
            outcome=outcome,
            trace=tuple(records),
            debug_evidence=TraceDebugEvidence(
                continuation_root_refs=step_evaluator.continuation_root_refs,
                continuation_objects=step_evaluator.continuation_objects,
                program_ref=step_evaluator.program_ref,
                continuation_ref_map=step_evaluator.continuation_ref_map,
                continuation_control_ref_map=step_evaluator.continuation_control_ref_map,
                context_ref_map=step_evaluator.context_ref_map,
            ),
        ),
        step_evaluator,
    )


def _assert_completed_step_matches_recursive(program) -> None:
    recursive = run_trace(program, engine="recursive", include_debug_evidence=True)
    stepped, _evaluator = _run_step_trace(program)

    assert stepped.outcome == recursive.outcome
    assert stepped.trace == recursive.trace
    assert (
        stepped.require_debug_evidence().continuation_root_refs
        == recursive.require_debug_evidence().continuation_root_refs
    )
    assert (
        stepped.require_debug_evidence().continuation_objects == recursive.require_debug_evidence().continuation_objects
    )
    assert conformance_artifact_to_json(artifact_from_trace_result(stepped)) == conformance_artifact_to_json(
        artifact_from_trace_result(recursive)
    )


def _assert_step_trace_matches_recursive(program) -> None:
    recursive = run_trace(program, engine="recursive", include_debug_evidence=True)
    stepped, _evaluator = _run_step_trace(program)

    assert stepped.outcome == recursive.outcome
    assert stepped.trace == recursive.trace
    assert (
        stepped.require_debug_evidence().continuation_root_refs
        == recursive.require_debug_evidence().continuation_root_refs
    )
    assert (
        stepped.require_debug_evidence().continuation_objects == recursive.require_debug_evidence().continuation_objects
    )


def _install(effect_kind: str, body: object, handler_id: str) -> StaticHandlerInstall:
    return StaticHandlerInstall(
        effect_kind=effect_kind,
        handler_id=handler_id,
        handled_result_schema=AnySchema(),
        payload_name="_payload",
        body=body,
    )


def _block_recursive_scheduler(evaluator: StepKernelEvaluator, monkeypatch: pytest.MonkeyPatch) -> None:
    blocked = {
        "_eval",
        "_continue_value",
        "_perform",
        "_select_handler",
        "_resume",
        "_abort",
        "_forward",
        "_terminal_delay",
        "_terminal_fork",
    }
    for name in blocked:
        if hasattr(evaluator, name):
            monkeypatch.setattr(evaluator, name, _blocked_recursive_scheduler(name))


def _blocked_recursive_scheduler(name: str):
    def blocked(*args: object, **kwargs: object) -> SourceOutcome:
        raise AssertionError(f"step machine called recursive scheduler {name}")

    return blocked


def _guard_run_loop_reentry(evaluator: StepKernelEvaluator, monkeypatch: pytest.MonkeyPatch) -> None:
    original_run_loop = evaluator._run_loop
    active = False

    def guarded(*args: object, **kwargs: object) -> SourceOutcome:
        nonlocal active
        if active:
            raise AssertionError("step machine re-entered _run_loop")
        active = True
        try:
            return original_run_loop(*args, **kwargs)
        finally:
            active = False

    monkeypatch.setattr(evaluator, "_run_loop", guarded)


def _sequential_handled_effect_program(count: int) -> object:
    term: object = Return(Lit("value"))
    for idx in reversed(range(count)):
        term = Let(f"x{idx}", Perform("eff.a", Lit(f"payload-{idx}")), term)
    return Handle(
        term,
        HandlerEnv(
            (
                StaticHandlerInstall(
                    effect_kind="eff.a",
                    handler_id="h.resume",
                    handled_result_schema=AnySchema(),
                    payload_name="_payload",
                    body=Let("r", Resume(Lit("value")), Return(Var("r"))),
                ),
            )
        ),
    )


def _sequential_terminal_fork_program(count: int) -> object:
    term: object = Return(Lit("done"))
    for idx in reversed(range(count)):
        term = Let(f"x{idx}", Perform("eff.a", Lit(f"payload-{idx}")), term)
    return Handle(
        term,
        HandlerEnv(
            (
                StaticHandlerInstall(
                    effect_kind="eff.a",
                    handler_id="h.fork",
                    handled_result_schema=AnySchema(),
                    payload_name="_payload",
                    body=TerminalFork((("branch:A", Lit("fork-value")),)),
                ),
            )
        ),
    )


def _fork_branch_suspends_then_handles_downstream_term() -> object:
    return Handle(
        Let(
            "y",
            Perform("eff.a", Lit("payload")),
            Let("z", Perform("eff.unhandled", Var("y")), Perform("eff.b", Var("z"))),
        ),
        HandlerEnv(
            (
                _install("eff.a", TerminalFork((("branch:A", Lit("fork-value")),)), "h.fork"),
                _install("eff.b", Return(Lit("handled-b")), "h.b"),
            )
        ),
    )


def _delay_suspends_twice_then_handles_downstream_term() -> object:
    return Handle(
        Let(
            "y",
            Perform("eff.a", Lit("payload")),
            Let(
                "z",
                Perform("eff.unhandled1", Var("y")),
                Let("w", Perform("eff.unhandled2", Var("z")), Perform("eff.b", Var("w"))),
            ),
        ),
        HandlerEnv(
            (
                _install("eff.a", TerminalDelay(Lit("waiting")), "h.delay"),
                _install("eff.b", Return(Lit("handled-b")), "h.b"),
            )
        ),
    )


def _fork_branch_suspends_twice_then_handles_downstream_term() -> object:
    return Handle(
        Let(
            "y",
            Perform("eff.a", Lit("payload")),
            Let(
                "z",
                Perform("eff.unhandled1", Var("y")),
                Let("w", Perform("eff.unhandled2", Var("z")), Perform("eff.b", Var("w"))),
            ),
        ),
        HandlerEnv(
            (
                _install("eff.a", TerminalFork((("branch:A", Lit("fork-value")),)), "h.fork"),
                _install("eff.b", Return(Lit("handled-b")), "h.b"),
            )
        ),
    )
