from dataclasses import fields

from shepherd_kernel_v3_reference.kernel import (
    elaborate,
    elaborate_publication_experimental,
)
from shepherd_kernel_v3_reference.kernel.continuation_objects import (
    CONTINUATION_OBJECT_SCHEMA_VERSION,
    ContinuationRoot,
)
from shepherd_kernel_v3_reference.schemas import AnySchema
from shepherd_kernel_v3_reference.source.experimental import TerminalDelay, TerminalFork
from shepherd_kernel_v3_reference.source.handlers import HandlerEnv, StaticHandlerInstall
from shepherd_kernel_v3_reference.source.outcomes import Completed, Delayed
from shepherd_kernel_v3_reference.source.syntax import Handle, Let, Lit, Perform, Resume, Return, Var
from shepherd_kernel_v3_reference.trace.machine import TraceDebugEvidence, TraceResult, TraceSession, run_trace
from shepherd_kernel_v3_reference.trace.records import TraceRecord


def install(effect_kind: str, body, handler_id: str = "h.v1") -> StaticHandlerInstall:
    return StaticHandlerInstall(
        effect_kind=effect_kind,
        handler_id=handler_id,
        handled_result_schema=AnySchema(),
        body=body,
        payload_name="_payload",
    )


def test_core_trace_has_objects_for_every_emitted_continuation_ref() -> None:
    term = Handle(
        Let("y", Perform("eff.a", Lit(None)), Return(Var("y"))),
        HandlerEnv((install("eff.a", Let("r", Resume(Lit("value")), Return(Var("r")))),)),
    )

    result = run_trace(elaborate(term), include_debug_evidence=True)

    assert result.outcome == Completed("value")
    _assert_all_trace_continuations_have_objects(result)
    assert {_root(result, ref).continuation_kind for ref in result.require_debug_evidence().continuation_root_refs} >= {
        "full",
        "captured-worker",
        "handler-continuation",
    }


def test_publication_delay_trace_has_objects_for_later_terminal_resume() -> None:
    term = Handle(
        Perform("eff.a", Lit(None)),
        HandlerEnv((install("eff.a", TerminalDelay(Lit("later"))),)),
    )
    session = TraceSession(elaborate_publication_experimental(term), include_debug_evidence=True)
    initial = session.run()
    assert isinstance(initial.outcome, Delayed)

    resumed = initial.outcome.pending.apply("done")

    assert resumed == Completed("done")
    debug_evidence = session.debug_evidence
    assert debug_evidence is not None
    result = TraceResult(
        outcome=resumed,
        trace=session.trace,
        debug_evidence=TraceDebugEvidence(
            continuation_root_refs=debug_evidence.continuation_root_refs,
            continuation_objects=debug_evidence.continuation_objects,
            program_ref=debug_evidence.program_ref,
            continuation_ref_map=debug_evidence.continuation_ref_map,
            continuation_control_ref_map=debug_evidence.continuation_control_ref_map,
            context_ref_map=debug_evidence.context_ref_map,
        ),
    )
    _assert_all_trace_continuations_have_objects(result)
    assert {_root(result, ref).continuation_kind for ref in result.require_debug_evidence().continuation_root_refs} >= {
        "captured-worker",
        "outer",
        "empty-terminal",
    }


def test_publication_fork_trace_has_branch_scoped_objects() -> None:
    term = Handle(
        Perform("eff.a", Lit(None)),
        HandlerEnv((install("eff.a", TerminalFork((("branch:A", Lit("A")),))),)),
    )

    result = run_trace(elaborate_publication_experimental(term), include_debug_evidence=True)

    _assert_all_trace_continuations_have_objects(result)
    assert any(
        _root(result, ref).branch_scope_ref is not None
        for ref in result.require_debug_evidence().continuation_root_refs
    )


def _assert_all_trace_continuations_have_objects(result: TraceResult) -> None:
    refs = _trace_continuation_refs(result.trace)
    evidence = result.require_debug_evidence()
    assert refs
    assert refs <= set(evidence.continuation_ref_map)
    assert set(evidence.continuation_ref_map.values()) <= set(evidence.continuation_objects)
    for ref in refs:
        root = _root(result, ref)
        assert root.object_schema_version == CONTINUATION_OBJECT_SCHEMA_VERSION
        assert root.program_ref.startswith("program:sha256:")
        assert root.stack_ref in evidence.continuation_objects


def _root(result: TraceResult, ref: str) -> ContinuationRoot:
    obj = result.require_debug_evidence().get_continuation_object(ref)
    assert isinstance(obj, ContinuationRoot)
    return obj


def _trace_continuation_refs(trace: tuple[TraceRecord, ...]) -> set[str]:
    refs: set[str] = set()
    for record in trace:
        for field in fields(record):
            if field.name.endswith("continuation_ref"):
                value = getattr(record, field.name)
                if isinstance(value, str):
                    refs.add(value)
    return refs
