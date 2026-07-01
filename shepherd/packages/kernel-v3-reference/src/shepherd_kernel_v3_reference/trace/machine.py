"""Trace-machine entry points for the core fragment."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from shepherd_kernel_v3_reference.kernel.events import (
    ContinuationDelay as ContinuationDelayEvent,
)
from shepherd_kernel_v3_reference.kernel.events import (
    ContinuationPending as ContinuationPendingEvent,
)
from shepherd_kernel_v3_reference.kernel.events import (
    ContinuationResumed,
    EffectDeclared,
    HandlerCaptured,
    HandlerSelected,
    KernelEvent,
    ResumptionCreated,
    WorkerReturned,
)
from shepherd_kernel_v3_reference.kernel.events import (
    ForkBranch as ForkBranchEvent,
)
from shepherd_kernel_v3_reference.kernel.events import (
    ForkSummary as ForkSummaryEvent,
)
from shepherd_kernel_v3_reference.kernel.events import (
    HandlerForward as HandlerForwardEvent,
)
from shepherd_kernel_v3_reference.kernel.events import (
    SelectionClosed as SelectionClosedEvent,
)
from shepherd_kernel_v3_reference.kernel.events import (
    TerminalResumeResult as TerminalResumeResultEvent,
)
from shepherd_kernel_v3_reference.kernel.recursive_machine import RecursiveKernelEvaluator
from shepherd_kernel_v3_reference.kernel.step_machine import StepKernelEvaluator
from shepherd_kernel_v3_reference.trace.records import (
    ContinuationDelay,
    ContinuationPending,
    ContinuationResume,
    EffectCapture,
    EffectDeclaration,
    ForkBranch,
    ForkSummary,
    HandlerForward,
    HandlerSelection,
    ResumeReturn,
    ResumptionHandle,
    SelectionClosed,
    TerminalResumeResult,
    TraceRecord,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from shepherd_kernel_v3_reference.kernel.continuation_objects import ContinuationObject
    from shepherd_kernel_v3_reference.kernel.program_admission import KernelProgramInput
    from shepherd_kernel_v3_reference.source.effects import EffectRegistry
    from shepherd_kernel_v3_reference.source.outcomes import SourceOutcome
    from shepherd_kernel_v3_reference.source.values import Env

TraceEvaluatorEngine = Literal["auto", "step", "recursive"]


@dataclass(frozen=True)
class TraceDebugEvidence:
    """Opt-in continuation evidence emitted for debugging and conformance export."""

    continuation_root_refs: tuple[str, ...]
    continuation_objects: Mapping[str, ContinuationObject]
    program_ref: str
    continuation_ref_map: Mapping[str, str] = field(default_factory=dict)
    continuation_control_ref_map: Mapping[str, str] = field(default_factory=dict)
    context_ref_map: Mapping[str, str] = field(default_factory=dict)

    def get_continuation_object(self, ref: str) -> ContinuationObject:
        return self.continuation_objects[self.continuation_ref_map.get(ref, ref)]

    def list_continuation_objects(self) -> tuple[ContinuationObject, ...]:
        return tuple(self.continuation_objects.values())


@dataclass(frozen=True)
class TraceResult:
    outcome: SourceOutcome
    trace: tuple[TraceRecord, ...]
    debug_evidence: TraceDebugEvidence | None = None

    def require_debug_evidence(self) -> TraceDebugEvidence:
        if self.debug_evidence is None:
            raise RuntimeError("trace debug evidence is unavailable; call run_trace(..., include_debug_evidence=True)")
        return self.debug_evidence


class TraceSession:
    """Live traced execution.

    `run_trace(...)` returns a snapshot. Use `TraceSession` when a suspended
    outcome may be resumed and callers need the trace emitted by that later
    continuation application.
    """

    def __init__(
        self,
        program: KernelProgramInput,
        env: Env | None = None,
        registry: EffectRegistry | None = None,
        *,
        engine: TraceEvaluatorEngine = "auto",
        include_debug_evidence: bool = False,
    ) -> None:
        self._env = env
        self._include_debug_evidence = include_debug_evidence
        self._records: list[TraceRecord] = []
        self._evaluator = _new_trace_evaluator(
            program,
            registry=registry,
            event_sink=lambda event: self._records.append(record_from_event(event)),
            engine=engine,
            include_debug_evidence=include_debug_evidence,
        )
        self._started = False

    @property
    def trace(self) -> tuple[TraceRecord, ...]:
        return tuple(self._records)

    @property
    def debug_evidence(self) -> TraceDebugEvidence | None:
        if not self._include_debug_evidence:
            return None
        return TraceDebugEvidence(
            continuation_root_refs=self._evaluator.continuation_root_refs,
            continuation_objects=self._evaluator.continuation_objects,
            program_ref=self._evaluator.program_ref,
            continuation_ref_map=self._evaluator.continuation_ref_map,
            continuation_control_ref_map=self._evaluator.continuation_control_ref_map,
            context_ref_map=self._evaluator.context_ref_map,
        )

    def run(self) -> TraceResult:
        if self._started:
            raise RuntimeError("TraceSession.run() may be called only once")
        self._started = True
        outcome = self._evaluator.run(self._env)
        return TraceResult(
            outcome=outcome,
            trace=self.trace,
            debug_evidence=self.debug_evidence,
        )


def run_trace(
    program: KernelProgramInput,
    env: Env | None = None,
    registry: EffectRegistry | None = None,
    *,
    engine: TraceEvaluatorEngine = "auto",
    include_debug_evidence: bool = False,
) -> TraceResult:
    return TraceSession(
        program,
        env=env,
        registry=registry,
        engine=engine,
        include_debug_evidence=include_debug_evidence,
    ).run()


def _new_trace_evaluator(
    program: KernelProgramInput,
    *,
    registry: EffectRegistry | None,
    event_sink: Callable[[KernelEvent], None],
    engine: TraceEvaluatorEngine,
    include_debug_evidence: bool,
) -> StepKernelEvaluator | RecursiveKernelEvaluator:
    evidence_mode = "sidecar" if include_debug_evidence else "none"
    if engine in ("auto", "step"):
        return StepKernelEvaluator(program, registry=registry, event_sink=event_sink, evidence_mode=evidence_mode)
    if engine == "recursive":
        return RecursiveKernelEvaluator(program, registry=registry, event_sink=event_sink, evidence_mode=evidence_mode)
    raise ValueError(f"unknown trace evaluator engine: {engine!r}")


def record_from_event(event: KernelEvent) -> TraceRecord:
    if isinstance(event, ContinuationDelayEvent):
        return ContinuationDelay(
            ref=event.ref,
            pending_ref=event.pending_ref,
            reason=event.reason,
            branch_scope_ref=event.branch_scope_ref,
        )

    if isinstance(event, ContinuationPendingEvent):
        return ContinuationPending(
            ref=event.ref,
            declaration_ref=event.declaration_ref,
            selection_ref=event.selection_ref,
            selection_path_ref=event.selection_path_ref,
            continuation_ref=event.continuation_ref,
            operation_result_schema_ref=event.operation_result_schema_ref,
            branch_ref=event.branch_ref,
            reason=event.reason,
            worker_context_ref=event.worker_context_ref,
            branch_scope_ref=event.branch_scope_ref,
        )

    if isinstance(event, EffectDeclared):
        return EffectDeclaration(
            ref=event.ref,
            program_ref=event.program_ref,
            effect_kind=event.effect_kind,
            payload=event.payload,
            full_continuation_ref=event.full_continuation_ref,
            branch_ref=event.branch_ref,
            payload_schema_ref=event.payload_schema_ref,
            operation_result_schema_ref=event.operation_result_schema_ref,
            execution_context_ref=event.execution_context_ref,
            branch_scope_ref=event.branch_scope_ref,
        )

    if isinstance(event, ForkBranchEvent):
        return ForkBranch(
            ref=event.ref,
            fork_ref=event.fork_ref,
            declaration_ref=event.declaration_ref,
            selection_ref=event.selection_ref,
            selection_path_ref=event.selection_path_ref,
            branch_ref=event.branch_ref,
            continuation_ref=event.continuation_ref,
            terminal_continuation_ref=event.terminal_continuation_ref,
            value=event.value,
            branch_scope_ref=event.branch_scope_ref,
        )

    if isinstance(event, ForkSummaryEvent):
        return ForkSummary(
            ref=event.ref,
            declaration_ref=event.declaration_ref,
            selection_ref=event.selection_ref,
            selection_path_ref=event.selection_path_ref,
            branch_ref=event.branch_ref,
            branch_refs=event.branch_refs,
            branch_scope_ref=event.branch_scope_ref,
        )

    if isinstance(event, HandlerForwardEvent):
        return HandlerForward(
            ref=event.ref,
            declaration_ref=event.declaration_ref,
            skipped_selection_ref=event.skipped_selection_ref,
            skipped_binding_ref=event.skipped_binding_ref,
            skipped_selection_path_ref=event.skipped_selection_path_ref,
            branch_ref=event.branch_ref,
            branch_scope_ref=event.branch_scope_ref,
        )

    if isinstance(event, HandlerSelected):
        return HandlerSelection(
            ref=event.ref,
            declaration_ref=event.declaration_ref,
            selected_binding_ref=event.selected_binding_ref,
            handler_id=event.handler_id,
            handler_frame_ref=event.handler_frame_ref,
            captured_continuation_ref=event.captured_continuation_ref,
            outer_continuation_ref=event.outer_continuation_ref,
            captured_continuation_control_ref=event.captured_continuation_control_ref,
            outer_continuation_control_ref=event.outer_continuation_control_ref,
            handled_result_schema_ref=event.handled_result_schema_ref,
            worker_context_ref=event.worker_context_ref,
            handler_context_ref=event.handler_context_ref,
            outer_context_ref=event.outer_context_ref,
            branch_scope_ref=event.branch_scope_ref,
        )

    if isinstance(event, ResumptionCreated):
        return ResumptionHandle(
            ref=event.ref,
            declaration_ref=event.declaration_ref,
            selection_ref=event.selection_ref,
            continuation_ref=event.continuation_ref,
            operation_result_schema_ref=event.operation_result_schema_ref,
            handled_result_schema_ref=event.handled_result_schema_ref,
            branch_scope_ref=event.branch_scope_ref,
        )

    if isinstance(event, TerminalResumeResultEvent):
        return TerminalResumeResult(
            ref=event.ref,
            resume_ref=event.resume_ref,
            source_ref=event.source_ref,
            source_record_type=event.source_record_type,
            selection_path_ref=event.selection_path_ref,
            branch_ref=event.branch_ref,
            value=event.value,
            branch_scope_ref=event.branch_scope_ref,
        )

    if isinstance(event, ContinuationResumed):
        return ContinuationResume(
            ref=event.ref,
            source_ref=event.source_ref,
            source_record_type=event.source_record_type,
            declaration_ref=event.declaration_ref,
            selection_ref=event.selection_ref,
            selection_path_ref=event.selection_path_ref,
            continuation_ref=event.continuation_ref,
            handler_continuation_ref=event.handler_continuation_ref,
            handler_dynamic_tail_ref=event.handler_dynamic_tail_ref,
            branch_ref=event.branch_ref,
            value=event.value,
            returns_to_handler=event.returns_to_handler,
            worker_context_ref=event.worker_context_ref,
            handler_context_ref=event.handler_context_ref,
            branch_scope_ref=event.branch_scope_ref,
        )

    if isinstance(event, WorkerReturned):
        return ResumeReturn(
            ref=event.ref,
            resume_ref=event.resume_ref,
            selection_ref=event.selection_ref,
            selection_path_ref=event.selection_path_ref,
            branch_ref=event.branch_ref,
            handler_continuation_ref=event.handler_continuation_ref,
            handler_dynamic_tail_ref=event.handler_dynamic_tail_ref,
            value=event.value,
            handler_context_ref=event.handler_context_ref,
            branch_scope_ref=event.branch_scope_ref,
        )

    if isinstance(event, HandlerCaptured):
        return EffectCapture(
            ref=event.ref,
            selection_ref=event.selection_ref,
            selection_path_ref=event.selection_path_ref,
            branch_ref=event.branch_ref,
            action_kind=event.action_kind,
            action_payload=event.action_payload,
            continuation_disposition=event.continuation_disposition,
            outer_context_ref=event.outer_context_ref,
            branch_scope_ref=event.branch_scope_ref,
        )

    if isinstance(event, SelectionClosedEvent):
        return SelectionClosed(
            ref=event.ref,
            selection_ref=event.selection_ref,
            selection_path_ref=event.selection_path_ref,
            branch_ref=event.branch_ref,
            reason=event.reason,
            caused_by_ref=event.caused_by_ref,
            caused_by_record_type=event.caused_by_record_type,
            closed_by_selection_ref=event.closed_by_selection_ref,
            closed_by_selection_path_ref=event.closed_by_selection_path_ref,
            branch_scope_ref=event.branch_scope_ref,
        )

    raise TypeError(f"unknown kernel event: {event!r}")
