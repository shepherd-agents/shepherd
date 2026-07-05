from __future__ import annotations

from dataclasses import dataclass

import pytest

from shepherd_kernel_v3_reference.kernel import elaborate, run_kernel
from shepherd_kernel_v3_reference.schemas import AnySchema, TaggedRecordSchema, TypeSchema, ValidationError
from shepherd_kernel_v3_reference.source.eval_direct import AbortAfterResume, run
from shepherd_kernel_v3_reference.source.handlers import HandlerEnv, StaticHandlerInstall
from shepherd_kernel_v3_reference.source.outcomes import Completed, ResumptionUsed, Suspended
from shepherd_kernel_v3_reference.source.syntax import Abort, Handle, Let, Lit, Perform, Resume, Return, Var
from shepherd_kernel_v3_reference.source.wellformed import SourceFormError
from shepherd_kernel_v3_reference.trace.machine import run_trace
from shepherd_kernel_v3_reference.trace.records import EffectCapture, SelectionClosed
from shepherd_kernel_v3_reference.trace.validate import (
    TraceValidationError,
    validate_core0_trace,
    validate_core0_trace_prefix,
    validate_core_a_trace,
    validate_core_a_trace_prefix,
    validate_generated_trace_against_program,
)


@dataclass(frozen=True)
class CorpusCase:
    name: str
    term: object
    core0: bool


def install(
    effect_kind: str,
    body,
    handler_id: str = "h.v1",
    *,
    handled_result_schema=AnySchema(),
) -> StaticHandlerInstall:
    return StaticHandlerInstall(
        effect_kind=effect_kind,
        handler_id=handler_id,
        handled_result_schema=handled_result_schema,
        payload_name="_payload",
        body=body,
    )


def passthrough(effect_kind: str, value, handler_id: str = "h.v1") -> StaticHandlerInstall:
    return install(
        effect_kind,
        Let("r", Resume(Lit(value)), Return(Var("r"))),
        handler_id,
    )


def static_corpus() -> list[CorpusCase]:
    return [
        CorpusCase(
            "pure-let",
            Let("x", Return(Lit(1)), Return(Var("x"))),
            True,
        ),
        CorpusCase(
            "unhandled-perform",
            Let("x", Perform("eff.unhandled", Lit("payload")), Return(Var("x"))),
            True,
        ),
        CorpusCase(
            "handler-replacement",
            Handle(
                Perform("eff.a", Lit(None)),
                HandlerEnv((install("eff.a", Return(Lit("replaced"))),)),
            ),
            True,
        ),
        CorpusCase(
            "one-resume",
            Handle(
                Let("x", Perform("eff.a", Lit(None)), Return(Var("x"))),
                HandlerEnv((passthrough("eff.a", "value"),)),
            ),
            True,
        ),
        CorpusCase(
            "handler-observes-worker-result",
            Handle(
                Let("x", Perform("eff.a", Lit(None)), Return(Var("x"))),
                HandlerEnv(
                    (
                        install(
                            "eff.a",
                            Let("r", Resume(Lit("worker-value")), Return(Lit("answer"))),
                        ),
                    )
                ),
            ),
            True,
        ),
        CorpusCase(
            "deep-handling-second-perform",
            Handle(
                Let(
                    "x",
                    Perform("eff.a", Lit("first")),
                    Let("y", Perform("eff.a", Lit("second")), Return(Var("y"))),
                ),
                HandlerEnv((passthrough("eff.a", "deep-value"),)),
            ),
            True,
        ),
        CorpusCase(
            "handler-side-effect-before-resume",
            Handle(
                Handle(
                    Perform("eff.inner", Lit(None)),
                    HandlerEnv(
                        (
                            install(
                                "eff.inner",
                                Let(
                                    "approval",
                                    Perform("eff.outer", Lit("ask")),
                                    Let("r", Resume(Var("approval")), Return(Var("r"))),
                                ),
                                "h.inner",
                            ),
                        )
                    ),
                ),
                HandlerEnv((passthrough("eff.outer", "approved", "h.outer"),)),
            ),
            True,
        ),
        CorpusCase(
            "handler-side-effect-after-resume",
            Handle(
                Handle(
                    Let("x", Perform("eff.work", Lit(None)), Return(Var("x"))),
                    HandlerEnv(
                        (
                            install(
                                "eff.work",
                                Let(
                                    "section",
                                    Resume(Lit("draft")),
                                    Let(
                                        "_ack",
                                        Perform("eff.audit", Var("section")),
                                        Return(Var("section")),
                                    ),
                                ),
                                "h.work",
                            ),
                        )
                    ),
                ),
                HandlerEnv((passthrough("eff.audit", "ack", "h.audit"),)),
            ),
            True,
        ),
        CorpusCase(
            "three-level-supervision",
            Handle(
                Handle(
                    Handle(
                        Let("x", Perform("eff.work", Lit(None)), Return(Var("x"))),
                        HandlerEnv(
                            (
                                install(
                                    "eff.work",
                                    Let(
                                        "policy",
                                        Perform("eff.request", Lit("ask-parent")),
                                        Let("r", Resume(Var("policy")), Return(Var("r"))),
                                    ),
                                    "h.supervisor",
                                ),
                            )
                        ),
                    ),
                    HandlerEnv(
                        (
                            install(
                                "eff.request",
                                Let(
                                    "policy",
                                    Perform("eff.escalate", Lit("ask-grandparent")),
                                    Let("r", Resume(Var("policy")), Return(Var("r"))),
                                ),
                                "h.parent",
                            ),
                        )
                    ),
                ),
                HandlerEnv((passthrough("eff.escalate", "policy-x", "h.grandparent"),)),
            ),
            True,
        ),
        CorpusCase(
            "answer-abort",
            Handle(
                Perform("eff.a", Lit(None)),
                HandlerEnv((install("eff.a", Abort(Lit("blocked"))),)),
            ),
            False,
        ),
        CorpusCase(
            "outer-abort-closes-inner",
            Handle(
                Handle(
                    Perform("eff.inner", Lit(None)),
                    HandlerEnv(
                        (
                            install(
                                "eff.inner",
                                Let(
                                    "x",
                                    Perform("eff.outer", Lit("ask")),
                                    Return(Var("x")),
                                ),
                                "h.inner",
                            ),
                        )
                    ),
                ),
                HandlerEnv((install("eff.outer", Abort(Lit("outer-abort")), "h.outer"),)),
            ),
            False,
        ),
        CorpusCase(
            "outer-return-closes-inner",
            Handle(
                Handle(
                    Perform("eff.inner", Lit(None)),
                    HandlerEnv(
                        (
                            install(
                                "eff.inner",
                                Let(
                                    "x",
                                    Perform("eff.outer", Lit("ask")),
                                    Return(Var("x")),
                                ),
                                "h.inner",
                            ),
                        )
                    ),
                ),
                HandlerEnv((install("eff.outer", Return(Lit("outer-return")), "h.outer"),)),
            ),
            False,
        ),
    ]


@pytest.mark.parametrize("case", static_corpus(), ids=lambda case: case.name)
def test_static_fragment_direct_kernel_and_trace_corpus(case: CorpusCase) -> None:
    kernel = elaborate(case.term)
    direct = run(case.term)
    machine = run_kernel(kernel)
    traced = run_trace(kernel)

    _assert_outcomes_agree(direct, machine)
    _assert_outcomes_agree(direct, traced.outcome)

    if case.core0:
        _validate_complete_or_prefix(direct, traced.trace, core0=True)
    else:
        with pytest.raises(TraceValidationError):
            _validate_complete_or_prefix(direct, traced.trace, core0=True)

    _validate_complete_or_prefix(direct, traced.trace, core0=False)
    validate_generated_trace_against_program(
        kernel,
        traced.trace,
        completed=isinstance(direct, Completed),
    )


def _validate_complete_or_prefix(outcome, trace, *, core0: bool) -> None:
    completed = isinstance(outcome, Completed)
    if core0:
        if completed:
            validate_core0_trace(trace)
        else:
            validate_core0_trace_prefix(trace)
        return

    if completed:
        validate_core_a_trace(trace)
    else:
        validate_core_a_trace_prefix(trace)


def _assert_outcomes_agree(left, right) -> None:
    if isinstance(left, Completed):
        assert right == left
        return

    assert isinstance(left, Suspended)
    assert isinstance(right, Suspended)
    assert right.effect_kind == left.effect_kind
    assert right.payload == left.payload


@pytest.mark.parametrize(
    ("name", "term", "error"),
    [
        (
            "resume-twice",
            Handle(
                Perform("eff.a", Lit(None)),
                HandlerEnv(
                    (
                        install(
                            "eff.a",
                            Let(
                                "x",
                                Resume(Lit("first")),
                                Let("y", Resume(Lit("second")), Return(Var("y"))),
                            ),
                        ),
                    )
                ),
            ),
            ResumptionUsed,
        ),
        (
            "abort-after-resume",
            Handle(
                Perform("eff.a", Lit(None)),
                HandlerEnv(
                    (
                        install(
                            "eff.a",
                            Let("x", Resume(Lit("value")), Abort(Lit("blocked"))),
                        ),
                    )
                ),
            ),
            AbortAfterResume,
        ),
        (
            "abort-in-bound-position",
            Handle(
                Perform("eff.a", Lit(None)),
                HandlerEnv(
                    (
                        install(
                            "eff.a",
                            Let("x", Abort(Lit("blocked")), Return(Var("x"))),
                        ),
                    )
                ),
            ),
            SourceFormError,
        ),
        (
            "answer-schema-mismatch",
            Handle(
                Perform("eff.a", Lit(None)),
                HandlerEnv(
                    (
                        install(
                            "eff.a",
                            Return(Lit("not-an-int")),
                            handled_result_schema=TypeSchema(int),
                        ),
                    )
                ),
            ),
            ValidationError,
        ),
        (
            "resume-schema-mismatch",
            Handle(
                Perform("eff.a", Lit(None)),
                HandlerEnv(
                    (
                        install(
                            "eff.a",
                            Let("x", Resume(Lit("not-result")), Return(Var("x"))),
                        ),
                    )
                ),
            ),
            ValidationError,
        ),
    ],
)
def test_static_fragment_negative_corpus(name: str, term, error: type[Exception]) -> None:
    registry = None
    if name == "resume-schema-mismatch":
        from shepherd_kernel_v3_reference.source.effects import EffectRegistry, EffectSignature

        registry = EffectRegistry()
        registry.register(
            EffectSignature("eff.a", AnySchema(), TaggedRecordSchema("Result")),
        )

    with pytest.raises(error):
        run(term, registry=registry)
    if error is SourceFormError:
        with pytest.raises(error):
            elaborate(term, registry=registry)
        return

    kernel = elaborate(term, registry=registry)
    with pytest.raises(error):
        run_kernel(kernel)


def test_core_a_corpus_actually_exercises_extensions() -> None:
    extension_traces = [run_trace(elaborate(case.term)).trace for case in static_corpus() if not case.core0]

    assert any(
        any(
            isinstance(record, EffectCapture)
            and record.action_kind == "abort"
            and record.continuation_disposition == "aborted"
            for record in trace
        )
        for trace in extension_traces
    )
    assert any(any(isinstance(record, SelectionClosed) for record in trace) for trace in extension_traces)
