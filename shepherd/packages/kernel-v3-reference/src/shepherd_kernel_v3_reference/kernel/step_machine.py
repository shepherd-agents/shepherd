"""Experimental explicit-step evaluator for kernel programs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from shepherd_kernel_v3_reference.kernel.context import ExecutionContext
from shepherd_kernel_v3_reference.kernel.events import (
    ContinuationDelay,
    ContinuationPending,
    ContinuationResumed,
    EffectDeclared,
    ForkBranch,
    ForkSummary,
    HandlerForward,
    HandlerSelected,
    ResumptionCreated,
    SelectionClosed,
    TerminalResumeResult,
    WorkerReturned,
)
from shepherd_kernel_v3_reference.kernel.frame_state import (
    BindFrame,
    HandlerFrame,
    HandlerReturnFrame,
    KontState,
    ResumeReturnFrame,
    _require_ref,
)
from shepherd_kernel_v3_reference.kernel.ir import (
    HandlerInstallDef,
    KAbort,
    KBind,
    KComputation,
    KForward,
    KHandle,
    KPerform,
    KPure,
    KResumeWith,
    KTerminalDelay,
    KTerminalFork,
    Ref,
)
from shepherd_kernel_v3_reference.kernel.runtime_services import _KernelRuntimeServices
from shepherd_kernel_v3_reference.source.eval_direct import AbortAfterResume, eval_expr
from shepherd_kernel_v3_reference.source.outcomes import (
    Completed,
    Continuation,
    Delayed,
    Forked,
    ResumptionUsed,
    SourceOutcome,
    Suspended,
)
from shepherd_kernel_v3_reference.source.syntax import Lit
from shepherd_kernel_v3_reference.source.values import Env

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shepherd_kernel_v3_reference.kernel.continuation_objects import ContinuationObject, ContinuationRoot


@dataclass(frozen=True)
class Eval:
    control: KComputation
    env: Env
    kont: KontState
    context: ExecutionContext


@dataclass(frozen=True)
class Continue:
    value: Any
    kont: KontState
    context: ExecutionContext


@dataclass(frozen=True)
class Perform:
    op: KPerform
    payload: Any
    kont: KontState
    context: ExecutionContext


@dataclass(frozen=True)
class SelectHandler:
    declaration_ref: Ref
    op: KPerform
    payload: Any
    captured: KontState
    handler_frame: HandlerFrame
    handler_frame_ref: Ref
    outer: KontState
    install: HandlerInstallDef
    worker_context: ExecutionContext


@dataclass(frozen=True)
class Resume:
    value: Any
    kont: KontState
    context: ExecutionContext


@dataclass(frozen=True)
class Abort:
    value: Any
    kont: KontState


@dataclass(frozen=True)
class Forward:
    kont: KontState


@dataclass(frozen=True)
class TerminalDelay:
    reason: Any
    kont: KontState


@dataclass(frozen=True)
class TerminalFork:
    branch_values: tuple[tuple[Ref, Any], ...]
    kont: KontState


@dataclass(frozen=True)
class ApplySuspendedContinuation:
    value: Any
    op: KPerform
    kont: KontState
    context: ExecutionContext


@dataclass(frozen=True)
class PendingTerminalReentry:
    pending_ref: Ref
    pending_path_ref: Ref
    terminal_kont: KontState
    handler_return: HandlerReturnFrame


@dataclass(frozen=True)
class ApplyPendingTerminalContinuation:
    value: Any
    reentry: PendingTerminalReentry


@dataclass(frozen=True)
class EnterTerminalForkBranch:
    fork_ref: Ref
    remaining_branch_values: tuple[tuple[Ref, Any], ...]
    outcomes: tuple[tuple[Ref, SourceOutcome], ...]
    terminal_kont: KontState
    handler_return: HandlerReturnFrame
    parent_branch_ref: Ref
    parent_branch_scope_ref: Ref | None


@dataclass(frozen=True)
class Done:
    outcome: SourceOutcome


@dataclass(frozen=True)
class _ContinuationReplayState:
    root: ContinuationRoot
    kont: KontState
    context: ExecutionContext


StepState = (
    Eval
    | Continue
    | Perform
    | SelectHandler
    | Resume
    | Abort
    | Forward
    | TerminalDelay
    | TerminalFork
    | ApplySuspendedContinuation
    | ApplyPendingTerminalContinuation
    | EnterTerminalForkBranch
    | Done
)


@dataclass(frozen=True)
class TerminalResumeFinalizer:
    resume_ref: Ref
    source_ref: Ref
    source_record_type: Literal["ContinuationPending", "ForkBranch"]
    selection_path_ref: Ref
    branch_ref: Ref
    branch_scope_ref: Ref | None


@dataclass(frozen=True)
class ForkAccumulator:
    fork_ref: Ref
    remaining_branch_values: tuple[tuple[Ref, Any], ...]
    outcomes: tuple[tuple[Ref, SourceOutcome], ...]
    terminal_kont: KontState
    handler_return: HandlerReturnFrame
    parent_branch_ref: Ref
    parent_branch_scope_ref: Ref | None
    current_branch_ref: Ref


SchedulerFrame = TerminalResumeFinalizer | ForkAccumulator


@dataclass(frozen=True)
class SchedulerSnapshot:
    branch_ref: Ref
    branch_scope_ref: Ref | None
    terminal_finalizers: tuple[TerminalResumeFinalizer, ...]


class StepKernelEvaluator(_KernelRuntimeServices):
    """Primary evaluator whose covered transitions run through an explicit loop."""

    def run(self, env: Env | None = None) -> SourceOutcome:
        return self.run_step(env)

    def run_step(self, env: Env | None = None) -> SourceOutcome:
        root_env = env or Env()
        root_context = ExecutionContext().with_binding_env_ref(self._env_ref(root_env))
        return self._run_loop(Eval(self.program.root, root_env, self._empty_kont_state(), root_context))

    def _resume_value_from_continuation_objects(
        self,
        root_ref: Ref,
        objects: Mapping[Ref, ContinuationObject],
        value: Any,
        *,
        source_label: str,
    ) -> SourceOutcome:
        """Internal Phase-0 replay hook for executable continuation root objects.

        This is intentionally narrower than a public carrier API: callers
        provide the already-selected root ref plus its reachable object
        snapshot, and lifecycle rules such as one-shot source consumption remain
        outside this helper.
        """

        replay_state = self._continuation_replay_state_from_objects(root_ref, objects)
        return self._resume_value_from_continuation_state(replay_state, value, source_label=source_label)

    def _resume_value_from_continuation_root(
        self,
        root: ContinuationRoot,
        value: Any,
        *,
        source_label: str,
    ) -> SourceOutcome:
        replay_state = self._continuation_replay_state_from_root(root)
        return self._resume_value_from_continuation_state(replay_state, value, source_label=source_label)

    def _continuation_replay_state_from_objects(
        self,
        root_ref: Ref,
        objects: Mapping[Ref, ContinuationObject],
    ) -> _ContinuationReplayState:
        root = self._continuation_root_from_objects(root_ref, objects)
        return self._continuation_replay_state_from_root(root)

    def _continuation_replay_state_from_root(self, root: ContinuationRoot) -> _ContinuationReplayState:
        if root.continuation_kind == "empty-terminal":
            raise RuntimeError("ContinuationRoot empty-terminal replay is not supported")
        kont = self._kont_state_from_continuation_object_stack_ref(root.stack_ref)
        context = self._context_from_continuation_payload(
            root.execution_context,
            expected_ref=root.execution_context_ref,
            source="ContinuationRoot.execution_context",
        )
        return _ContinuationReplayState(root=root, kont=kont, context=context)

    def _resume_value_from_continuation_state(
        self,
        replay_state: _ContinuationReplayState,
        value: Any,
        *,
        source_label: str,
    ) -> SourceOutcome:
        root = replay_state.root
        self._check_schema(
            root.result_schema_ref,
            value,
            context=source_label,
        )
        return self._run_loop(
            Continue(value, replay_state.kont, replay_state.context),
            branch_ref=root.branch_ref,
            branch_scope_ref=root.branch_scope_ref,
        )

    def _run_loop(
        self,
        state: StepState,
        *,
        branch_ref: Ref | None = None,
        branch_scope_ref: Ref | None = None,
        scheduler_snapshot: SchedulerSnapshot | None = None,
    ) -> SourceOutcome:
        previous_branch_ref = self._state.branch_ref
        previous_branch_scope_ref = self._state.branch_scope_ref
        previous_scheduler_frames = self._scheduler_frames()
        if scheduler_snapshot is not None:
            next_branch_ref = scheduler_snapshot.branch_ref
            next_branch_scope_ref = scheduler_snapshot.branch_scope_ref
            next_scheduler_frames: tuple[SchedulerFrame, ...] = scheduler_snapshot.terminal_finalizers
        elif branch_ref is not None:
            next_branch_ref = branch_ref
            next_branch_scope_ref = branch_scope_ref
            next_scheduler_frames = previous_scheduler_frames
        else:
            next_branch_ref = previous_branch_ref
            next_branch_scope_ref = previous_branch_scope_ref
            next_scheduler_frames = previous_scheduler_frames

        self._set_branch(next_branch_ref, next_branch_scope_ref)
        self._set_scheduler_frames(next_scheduler_frames)
        try:
            while True:
                while not isinstance(state, Done):
                    state = self._step(state)
                next_state = self._complete_done(state)
                if isinstance(next_state, Done):
                    if self._has_pending_fork_completion():
                        state = next_state
                        continue
                    return next_state.outcome
                state = next_state
        finally:
            self._set_branch(previous_branch_ref, previous_branch_scope_ref)
            self._set_scheduler_frames(previous_scheduler_frames)

    def _step(self, state: StepState) -> StepState:
        if isinstance(state, Eval):
            return self._step_eval(state)
        if isinstance(state, Continue):
            return self._step_continue(state)
        if isinstance(state, Perform):
            return self._step_perform(state)
        if isinstance(state, SelectHandler):
            return self._step_select_handler(state)
        if isinstance(state, Resume):
            return self._step_resume(state)
        if isinstance(state, Abort):
            return self._step_abort(state)
        if isinstance(state, Forward):
            return self._step_forward(state)
        if isinstance(state, TerminalDelay):
            return self._step_terminal_delay(state)
        if isinstance(state, TerminalFork):
            return self._step_terminal_fork(state)
        if isinstance(state, ApplySuspendedContinuation):
            return self._step_apply_suspended_continuation(state)
        if isinstance(state, ApplyPendingTerminalContinuation):
            return self._step_apply_pending_terminal_continuation(state)
        if isinstance(state, EnterTerminalForkBranch):
            return self._step_enter_terminal_fork_branch(state)
        raise TypeError(f"unknown step state: {state!r}")

    def _scheduler_frames(self) -> tuple[SchedulerFrame, ...]:
        return getattr(self, "_step_scheduler_frames", ())

    def _set_scheduler_frames(self, frames: tuple[SchedulerFrame, ...]) -> None:
        self._step_scheduler_frames = frames

    def _push_scheduler_frame(self, frame: SchedulerFrame) -> None:
        self._set_scheduler_frames(self._scheduler_frames() + (frame,))

    def _pop_scheduler_frame(self) -> SchedulerFrame:
        frames = self._scheduler_frames()
        if not frames:
            raise RuntimeError("step scheduler frame stack underflow")
        self._set_scheduler_frames(frames[:-1])
        return frames[-1]

    def _set_branch(self, branch_ref: Ref, branch_scope_ref: Ref | None) -> None:
        self._state.branch_ref = branch_ref
        self._state.branch_scope_ref = branch_scope_ref

    def _capture_scheduler_snapshot(self) -> SchedulerSnapshot:
        return SchedulerSnapshot(
            branch_ref=self._state.branch_ref,
            branch_scope_ref=self._state.branch_scope_ref,
            terminal_finalizers=tuple(
                frame for frame in self._scheduler_frames() if isinstance(frame, TerminalResumeFinalizer)
            ),
        )

    def _has_pending_fork_completion(self) -> bool:
        return any(isinstance(frame, ForkAccumulator) for frame in self._scheduler_frames())

    def _suspended_continuation(
        self,
        *,
        op: KPerform,
        kont: KontState,
        context: ExecutionContext,
    ) -> Continuation:
        snapshot = self._capture_scheduler_snapshot()

        def apply(value: Any) -> SourceOutcome:
            return self._run_loop(
                ApplySuspendedContinuation(value, op, kont, context),
                scheduler_snapshot=snapshot,
            )

        return Continuation(apply)

    def _pending_terminal_continuation(self, reentry: PendingTerminalReentry) -> Continuation:
        snapshot = self._capture_scheduler_snapshot()

        def apply(value: Any) -> SourceOutcome:
            return self._run_loop(
                ApplyPendingTerminalContinuation(value, reentry),
                scheduler_snapshot=snapshot,
            )

        return Continuation(apply)

    def _complete_done(self, state: Done) -> StepState:
        outcome = state.outcome
        if isinstance(outcome, Completed):
            while self._scheduler_frames() and isinstance(self._scheduler_frames()[-1], TerminalResumeFinalizer):
                finalizer = self._pop_scheduler_frame()
                if not isinstance(finalizer, TerminalResumeFinalizer):
                    raise AssertionError("terminal finalizer suffix changed during completion")
                self._emit_scheduler_terminal_resume_result(finalizer, outcome.value)
        else:
            self._drop_unemitted_terminal_finalizers_before_fork()

        frames = self._scheduler_frames()
        if frames and isinstance(frames[-1], ForkAccumulator):
            frame = self._pop_scheduler_frame()
            if not isinstance(frame, ForkAccumulator):
                raise AssertionError("fork accumulator changed during completion")
            return self._complete_terminal_fork_branch(frame, outcome)
        return Done(outcome)

    def _drop_unemitted_terminal_finalizers_before_fork(self) -> None:
        frames = self._scheduler_frames()
        idx = len(frames)
        while idx > 0 and isinstance(frames[idx - 1], TerminalResumeFinalizer):
            idx -= 1
        if idx > 0 and isinstance(frames[idx - 1], ForkAccumulator):
            self._set_scheduler_frames(frames[:idx])

    def _complete_terminal_fork_branch(self, frame: ForkAccumulator, outcome: SourceOutcome) -> StepState:
        self._set_branch(frame.parent_branch_ref, frame.parent_branch_scope_ref)
        outcomes = frame.outcomes + ((frame.current_branch_ref, outcome),)
        if not frame.remaining_branch_values:
            return Done(Forked(dict(outcomes)))
        return EnterTerminalForkBranch(
            fork_ref=frame.fork_ref,
            remaining_branch_values=frame.remaining_branch_values,
            outcomes=outcomes,
            terminal_kont=frame.terminal_kont,
            handler_return=frame.handler_return,
            parent_branch_ref=frame.parent_branch_ref,
            parent_branch_scope_ref=frame.parent_branch_scope_ref,
        )

    def _emit_scheduler_terminal_resume_result(self, finalizer: TerminalResumeFinalizer, value: Any) -> None:
        terminal_result_ref = self._fresh_ref("terminal-result")
        if self._trace_events_enabled:
            self._emit(
                TerminalResumeResult(
                    ref=terminal_result_ref,
                    resume_ref=finalizer.resume_ref,
                    source_ref=finalizer.source_ref,
                    source_record_type=finalizer.source_record_type,
                    selection_path_ref=finalizer.selection_path_ref,
                    branch_ref=finalizer.branch_ref,
                    value=value,
                    branch_scope_ref=finalizer.branch_scope_ref,
                )
            )

    def _step_eval(self, state: Eval) -> StepState:
        control = state.control
        if isinstance(control, KPure):
            return Continue(eval_expr(control.expr, state.env), state.kont, state.context)
        if isinstance(control, KBind):
            return Eval(
                control.bound,
                state.env,
                self._push_kont_frame(BindFrame(control.binder_id, state.env, state.context), state.kont),
                state.context,
            )
        if isinstance(control, KPerform):
            payload = eval_expr(control.payload, state.env)
            self._check_schema(control.payload_schema_ref, payload, context=f"perform({control.effect_kind!r}) payload")
            return Perform(control, payload, state.kont, state.context)
        if isinstance(control, KHandle):
            entry_context = state.context.with_region_ref(control.region_ref).with_binding_env_ref(
                self._env_ref(state.env)
            )
            return Eval(
                control.body,
                state.env,
                self._push_kont_frame(
                    HandlerFrame(
                        control.handler_env_ref,
                        state.env,
                        control.region_ref,
                        entry_context,
                        state.context,
                    ),
                    state.kont,
                ),
                entry_context,
            )
        if isinstance(control, KResumeWith):
            return Resume(eval_expr(control.value, state.env), state.kont, state.context)
        if isinstance(control, KAbort):
            return Abort(eval_expr(control.value, state.env), state.kont)
        if isinstance(control, KForward):
            return Forward(state.kont)
        if isinstance(control, KTerminalDelay):
            return TerminalDelay(eval_expr(control.reason, state.env), state.kont)
        if isinstance(control, KTerminalFork):
            return TerminalFork(
                tuple((branch_ref, eval_expr(value_expr, state.env)) for branch_ref, value_expr in control.branches),
                state.kont,
            )
        raise AssertionError(f"step evaluator does not yet cover control {control!r}")

    def _step_continue(self, state: Continue) -> StepState:
        head_ref = state.kont.head()
        if head_ref is None:
            return Done(Completed(state.value))

        head, _head_frame_ref = head_ref
        tail = self._kont_tail(state.kont)
        if isinstance(head, BindFrame):
            binder = self.program.binders[head.binder_id]
            next_env = head.env.extend(binder.param_name, state.value)
            return Eval(
                binder.body,
                next_env,
                tail,
                head.context.with_binding_env_ref(self._env_ref(next_env)),
            )
        if isinstance(head, HandlerFrame):
            return Continue(state.value, tail, head.outer_context)
        if isinstance(head, ResumeReturnFrame):
            if (
                self._trace_events_enabled
                and head.resume_ref is not None
                and head.selection_path_ref is not None
                and head.handler_return_frame.selection_ref is not None
                and head.handler_continuation_ref is not None
                and head.handler_dynamic_tail_ref is not None
            ):
                self._emit(
                    WorkerReturned(
                        ref=self._fresh_ref("resume-return"),
                        resume_ref=head.resume_ref,
                        selection_ref=head.handler_return_frame.selection_ref,
                        selection_path_ref=head.selection_path_ref,
                        branch_ref=self._state.branch_ref,
                        handler_continuation_ref=head.handler_continuation_ref,
                        handler_dynamic_tail_ref=head.handler_dynamic_tail_ref,
                        value=state.value,
                        handler_context_ref=self._context_ref(head.handler_context),
                        branch_scope_ref=self._state.branch_scope_ref,
                    )
                )
            handler_continuation = self._resume_handler_continuation_state(head)
            handler_dynamic_tail = self._resume_handler_dynamic_tail_state(head)
            handler_return_frame_ref = _require_ref(
                head.handler_return_frame_ref,
                "ResumeReturnFrame.handler_return_frame_ref",
            )
            handler_return_tail = self._concat_kont_states(
                self._kont_state_from_frame_ref(head.handler_return_frame, handler_return_frame_ref),
                handler_dynamic_tail,
            )
            next_kont = self._concat_kont_states(handler_continuation, handler_return_tail)
            return Continue(state.value, next_kont, head.handler_context)
        if isinstance(head, HandlerReturnFrame):
            self._check_schema(
                head.install.handled_result_schema_ref,
                state.value,
                context=f"handler({head.install.handler_id!r}) answer",
            )
            if head.selection_ref is not None and head.selection_path_ref is not None:
                capture_ref = self._emit_capture(
                    head,
                    action_kind="return",
                    action_payload=state.value,
                    continuation_disposition="completed",
                )
                self._close_abandoned_selections(
                    self._handler_return_captured_state(head),
                    reason="abandoned",
                    caused_by_ref=capture_ref,
                    caused_by_record_type="EffectCapture",
                    closed_by_selection_ref=head.selection_ref,
                    closed_by_selection_path_ref=head.selection_path_ref,
                )
            return Continue(state.value, tail, head.outer_context)
        raise TypeError(f"unknown continuation frame: {head!r}")

    def _step_perform(self, state: Perform) -> StepState:
        declaration_ref = self._fresh_ref("declaration")
        full_continuation_ref = self._kont_ref(
            state.kont,
            continuation_kind="full",
            context=state.context,
            result_schema_ref=state.op.operation_result_schema_ref,
        )
        if self._trace_events_enabled:
            self._emit(
                EffectDeclared(
                    ref=declaration_ref,
                    program_ref=self._program_ref(),
                    effect_kind=state.op.effect_kind,
                    payload=state.payload,
                    full_continuation_ref=full_continuation_ref,
                    branch_ref=self._state.branch_ref,
                    payload_schema_ref=state.op.payload_schema_ref,
                    operation_result_schema_ref=state.op.operation_result_schema_ref,
                    execution_context_ref=self._context_ref(state.context),
                    branch_scope_ref=self._state.branch_scope_ref,
                )
            )
        split = self._find_handler(state.op.effect_kind, state.kont)
        if split is None:
            return Done(
                Suspended(
                    state.op.effect_kind,
                    state.payload,
                    self._suspended_continuation(op=state.op, kont=state.kont, context=state.context),
                )
            )

        captured, handler_frame, handler_frame_ref, outer, install = split
        return SelectHandler(
            declaration_ref,
            state.op,
            state.payload,
            captured,
            handler_frame,
            handler_frame_ref,
            outer,
            install,
            state.context,
        )

    def _step_select_handler(self, state: SelectHandler) -> StepState:
        selection_ref = self._fresh_ref("selection")
        captured_ref = self._kont_ref(
            state.captured,
            continuation_kind="captured-worker",
            context=state.worker_context,
            result_schema_ref=state.op.operation_result_schema_ref,
        )
        captured_control_ref = self._kont_control_ref(state.captured)
        outer_ref = self._kont_ref(
            state.outer,
            continuation_kind="outer",
            context=state.handler_frame.outer_context,
            result_schema_ref=state.install.handled_result_schema_ref,
        )
        outer_control_ref = self._kont_control_ref(state.outer)
        handler_env = state.handler_frame.env.extend(state.install.payload_name, state.payload)
        handler_context = state.handler_frame.entry_context.with_binding_env_ref(self._env_ref(handler_env))
        if self._trace_events_enabled:
            self._emit(
                HandlerSelected(
                    ref=selection_ref,
                    declaration_ref=state.declaration_ref,
                    selected_binding_ref=state.install.install_ref,
                    handler_id=state.install.handler_id,
                    handler_frame_ref=state.handler_frame.handler_env_ref,
                    captured_continuation_ref=captured_ref,
                    outer_continuation_ref=outer_ref,
                    captured_continuation_control_ref=captured_control_ref,
                    outer_continuation_control_ref=outer_control_ref,
                    handled_result_schema_ref=state.install.handled_result_schema_ref,
                    worker_context_ref=self._context_ref(state.worker_context),
                    handler_context_ref=self._context_ref(handler_context),
                    outer_context_ref=self._context_ref(state.handler_frame.outer_context),
                    branch_scope_ref=self._state.branch_scope_ref,
                )
            )
        resumption_handle_ref = self._fresh_ref("resumption")
        if self._trace_events_enabled:
            self._emit(
                ResumptionCreated(
                    ref=resumption_handle_ref,
                    declaration_ref=state.declaration_ref,
                    selection_ref=selection_ref,
                    continuation_ref=captured_ref,
                    operation_result_schema_ref=state.op.operation_result_schema_ref,
                    handled_result_schema_ref=state.install.handled_result_schema_ref,
                    branch_scope_ref=self._state.branch_scope_ref,
                )
            )
        selection_path_ref = self._source_path_ref(selection_ref, resumption_handle_ref)
        handler_return = HandlerReturnFrame(
            install=state.install,
            selected_handler_frame=state.handler_frame,
            selected_handler_frame_ref=state.handler_frame_ref,
            handler_env=handler_env,
            captured_state=state.captured,
            captured_stack_ref=state.captured.cursor.stack_ref,
            outer_state=state.outer,
            outer_stack_ref=state.outer.cursor.stack_ref,
            worker_context=state.worker_context,
            handler_context=handler_context,
            outer_context=state.handler_frame.outer_context,
            declaration_ref=state.declaration_ref,
            selection_ref=selection_ref,
            resumption_handle_ref=resumption_handle_ref,
            selection_path_ref=selection_path_ref,
            captured_continuation_ref=captured_ref,
            outer_continuation_ref=outer_ref,
            captured_continuation_control_ref=captured_control_ref,
            outer_continuation_control_ref=outer_control_ref,
            operation_result_schema_ref=state.op.operation_result_schema_ref,
            handled_result_schema_ref=state.install.handled_result_schema_ref,
        )
        return Eval(
            state.install.body,
            handler_env,
            self._push_kont_frame(handler_return, state.outer),
            handler_context,
        )

    def _step_resume(self, state: Resume) -> StepState:
        split = self._split_at_handler_return(state.kont)
        if split is None:
            raise RuntimeError("Resume(value) used outside any handler body")
        handler_continuation, handler_return, handler_return_frame_ref, handler_dynamic_tail = split
        if handler_return.selection_path_ref is not None and not self._state.consume_source_path(
            handler_return.selection_path_ref
        ):
            raise ResumptionUsed(f"resumption for handler {handler_return.install.handler_id!r} already used")
        self._check_schema(
            handler_return.operation_result_schema_ref,
            state.value,
            context=f"resume({handler_return.install.effect_kind!r})",
        )
        resume_ref = self._fresh_ref("resume")
        handler_continuation_ref = self._kont_ref(
            handler_continuation,
            continuation_kind="handler-continuation",
            context=state.context,
            result_schema_ref=handler_return.operation_result_schema_ref,
        )
        handler_dynamic_tail_ref = self._kont_ref(
            handler_dynamic_tail,
            continuation_kind="handler-dynamic-tail",
            context=handler_return.outer_context,
            result_schema_ref=handler_return.handled_result_schema_ref,
        )
        if (
            self._trace_events_enabled
            and handler_return.resumption_handle_ref is not None
            and handler_return.declaration_ref is not None
            and handler_return.selection_ref is not None
            and handler_return.selection_path_ref is not None
            and handler_return.captured_continuation_ref is not None
        ):
            self._emit(
                ContinuationResumed(
                    ref=resume_ref,
                    source_ref=handler_return.resumption_handle_ref,
                    source_record_type="ResumptionHandle",
                    declaration_ref=handler_return.declaration_ref,
                    selection_ref=handler_return.selection_ref,
                    selection_path_ref=handler_return.selection_path_ref,
                    continuation_ref=handler_return.captured_continuation_ref,
                    handler_continuation_ref=handler_continuation_ref,
                    handler_dynamic_tail_ref=handler_dynamic_tail_ref,
                    branch_ref=self._state.branch_ref,
                    value=state.value,
                    returns_to_handler=True,
                    worker_context_ref=self._context_ref(handler_return.worker_context),
                    handler_context_ref=self._context_ref(state.context),
                    branch_scope_ref=self._state.branch_scope_ref,
                )
            )
        resume_return = ResumeReturnFrame(
            resume_ref=resume_ref,
            selection_path_ref=handler_return.selection_path_ref,
            handler_continuation_ref=handler_continuation_ref,
            handler_dynamic_tail_ref=handler_dynamic_tail_ref,
            handler_continuation_state=handler_continuation,
            handler_continuation_stack_ref=handler_continuation.cursor.stack_ref,
            handler_return_frame=handler_return,
            handler_return_frame_ref=handler_return_frame_ref,
            handler_dynamic_tail_state=handler_dynamic_tail,
            handler_dynamic_tail_stack_ref=handler_dynamic_tail.cursor.stack_ref,
            handler_context=state.context,
        )
        worker_tail = self._push_kont_frame(resume_return, handler_dynamic_tail)
        worker_tail = self._push_kont_frame(handler_return.selected_handler_frame, worker_tail)
        worker_kont = self._concat_kont_states(
            self._handler_return_captured_state(handler_return),
            worker_tail,
        )
        return Continue(state.value, worker_kont, handler_return.worker_context)

    def _step_abort(self, state: Abort) -> StepState:
        split = self._split_at_handler_return(state.kont)
        if split is None:
            raise RuntimeError("Abort(value) used outside any handler body")

        handler_continuation, handler_return, _handler_return_frame_ref, handler_dynamic_tail = split
        if handler_continuation:
            raise RuntimeError("Abort(value) is valid only in handler answer position")
        if (
            handler_return.selection_path_ref is not None
            and handler_return.selection_path_ref in self._state.consumed_source_paths
        ):
            raise AbortAfterResume(
                f"handler {handler_return.install.handler_id!r} aborted after invoking "
                "the selected worker resumption; §10 rejects `resume(...); Abort(...)` "
                "in the core"
            )
        self._check_schema(
            handler_return.install.handled_result_schema_ref,
            state.value,
            context=f"handler({handler_return.install.handler_id!r}) answer",
        )
        if handler_return.selection_ref is not None and handler_return.selection_path_ref is not None:
            capture_ref = self._emit_capture(
                handler_return,
                action_kind="abort",
                action_payload=state.value,
                continuation_disposition="aborted",
            )
        else:
            capture_ref = None
        self._close_abandoned_selections(
            self._handler_return_captured_state(handler_return),
            reason="skipped_by_outer_abort",
            caused_by_ref=capture_ref,
            caused_by_record_type="EffectCapture",
            closed_by_selection_ref=handler_return.selection_ref,
            closed_by_selection_path_ref=handler_return.selection_path_ref,
        )
        return Continue(state.value, handler_dynamic_tail, handler_return.outer_context)

    def _step_forward(self, state: Forward) -> StepState:
        split = self._split_at_handler_return(state.kont)
        if split is None:
            raise RuntimeError("Forward() used outside any handler body")

        handler_continuation, handler_return, _handler_return_frame_ref, _handler_dynamic_tail = split
        if handler_continuation:
            raise RuntimeError("Forward() is valid only in handler answer position")
        if (
            handler_return.selection_path_ref is not None
            and handler_return.selection_path_ref in self._state.consumed_source_paths
        ):
            raise ResumptionUsed(
                f"resumption for handler {handler_return.install.handler_id!r} "
                "already used; Forward() is terminal over an unused source"
            )
        if (
            handler_return.declaration_ref is None
            or handler_return.selection_ref is None
            or handler_return.selection_path_ref is None
        ):
            raise RuntimeError("Forward() requires an active selected handler")
        self._state.consume_source_path(handler_return.selection_path_ref)

        forward_ref = self._fresh_ref("forward")
        if self._trace_events_enabled:
            self._emit(
                HandlerForward(
                    ref=forward_ref,
                    declaration_ref=handler_return.declaration_ref,
                    skipped_selection_ref=handler_return.selection_ref,
                    skipped_binding_ref=handler_return.install.install_ref,
                    skipped_selection_path_ref=handler_return.selection_path_ref,
                    branch_ref=self._state.branch_ref,
                    branch_scope_ref=self._state.branch_scope_ref,
                )
            )
        self._state.mark_terminal_path(handler_return.selection_path_ref)
        selection_closed_ref = self._fresh_ref("selection-closed")
        if self._trace_events_enabled:
            self._emit(
                SelectionClosed(
                    ref=selection_closed_ref,
                    selection_ref=handler_return.selection_ref,
                    selection_path_ref=handler_return.selection_path_ref,
                    branch_ref=self._state.branch_ref,
                    reason="forwarded",
                    caused_by_ref=forward_ref,
                    caused_by_record_type="HandlerForward",
                    closed_by_selection_ref=handler_return.selection_ref,
                    closed_by_selection_path_ref=handler_return.selection_path_ref,
                    branch_scope_ref=self._state.branch_scope_ref,
                )
            )

        payload = handler_return.handler_env.lookup(handler_return.install.payload_name)
        op = KPerform(
            effect_kind=handler_return.install.effect_kind,
            payload=Lit(payload),
            payload_schema_ref=None,
            operation_result_schema_ref=handler_return.operation_result_schema_ref,
        )
        outer_state = self._handler_return_outer_state(handler_return)
        split_outer = self._find_handler(op.effect_kind, outer_state)
        selected_outer_tail = self._push_kont_frame(handler_return.selected_handler_frame, outer_state)
        forwarded_kont = self._concat_kont_states(
            self._handler_return_captured_state(handler_return),
            selected_outer_tail,
        )
        if split_outer is None:
            return Done(
                Suspended(
                    op.effect_kind,
                    payload,
                    self._suspended_continuation(op=op, kont=forwarded_kont, context=handler_return.worker_context),
                )
            )

        prefix, outer_handler_frame, outer_handler_frame_ref, outer_tail, outer_install = split_outer
        selected_prefix_tail = self._push_kont_frame(handler_return.selected_handler_frame, prefix)
        captured = self._concat_kont_states(
            self._handler_return_captured_state(handler_return),
            selected_prefix_tail,
        )
        return SelectHandler(
            handler_return.declaration_ref,
            op,
            payload,
            captured,
            outer_handler_frame,
            outer_handler_frame_ref,
            outer_tail,
            outer_install,
            handler_return.worker_context,
        )

    def _step_apply_suspended_continuation(self, state: ApplySuspendedContinuation) -> StepState:
        self._check_schema(
            state.op.operation_result_schema_ref,
            state.value,
            context=f"resume({state.op.effect_kind!r})",
        )
        return Continue(state.value, state.kont, state.context)

    def _step_terminal_delay(self, state: TerminalDelay) -> StepState:
        split = self._split_at_handler_return(state.kont)
        if split is None:
            raise RuntimeError("TerminalDelay(reason) used outside any handler body")

        handler_continuation, handler_return, _handler_return_frame_ref, handler_dynamic_tail = split
        if handler_continuation:
            raise RuntimeError("TerminalDelay(reason) is valid only in handler answer position")
        if (
            handler_return.selection_path_ref is not None
            and handler_return.selection_path_ref in self._state.consumed_source_paths
        ):
            raise ResumptionUsed(
                f"resumption for handler {handler_return.install.handler_id!r} "
                "already used; TerminalDelay is terminal over an unused source"
            )
        if (
            handler_return.declaration_ref is None
            or handler_return.selection_ref is None
            or handler_return.selection_path_ref is None
            or handler_return.captured_continuation_ref is None
        ):
            raise RuntimeError("TerminalDelay(reason) requires an active selected handler")
        self._state.consume_source_path(handler_return.selection_path_ref)

        pending_ref = self._fresh_ref("pending")
        self._state.mark_terminal_path(handler_return.selection_path_ref)
        if self._trace_events_enabled:
            self._emit(
                ContinuationPending(
                    ref=pending_ref,
                    declaration_ref=handler_return.declaration_ref,
                    selection_ref=handler_return.selection_ref,
                    selection_path_ref=handler_return.selection_path_ref,
                    continuation_ref=handler_return.captured_continuation_ref,
                    operation_result_schema_ref=handler_return.operation_result_schema_ref,
                    branch_ref=self._state.branch_ref,
                    reason=state.reason,
                    worker_context_ref=self._context_ref(handler_return.worker_context),
                    branch_scope_ref=self._state.branch_scope_ref,
                )
            )
            self._emit(
                ContinuationDelay(
                    ref=self._fresh_ref("delay"),
                    pending_ref=pending_ref,
                    reason=state.reason,
                    branch_scope_ref=self._state.branch_scope_ref,
                )
            )

        pending_path_ref = self._source_path_ref(handler_return.selection_ref, pending_ref)
        selected_tail = self._push_kont_frame(handler_return.selected_handler_frame, handler_dynamic_tail)
        terminal_kont = self._concat_kont_states(
            self._handler_return_captured_state(handler_return),
            selected_tail,
        )

        return Done(
            Delayed(
                state.reason,
                self._pending_terminal_continuation(
                    PendingTerminalReentry(
                        pending_ref=pending_ref,
                        pending_path_ref=pending_path_ref,
                        terminal_kont=terminal_kont,
                        handler_return=handler_return,
                    )
                ),
            )
        )

    def _step_apply_pending_terminal_continuation(self, state: ApplyPendingTerminalContinuation) -> StepState:
        reentry = state.reentry
        handler_return = reentry.handler_return
        if not self._state.consume_source_path(reentry.pending_path_ref):
            raise ResumptionUsed(f"pending continuation {reentry.pending_ref!r} already used")
        self._check_schema(
            handler_return.operation_result_schema_ref,
            state.value,
            context=f"resume({handler_return.install.effect_kind!r})",
        )
        resume_ref = self._fresh_ref("resume")
        declaration_ref = _require_ref(handler_return.declaration_ref, "HandlerReturnFrame.declaration_ref")
        selection_ref = _require_ref(handler_return.selection_ref, "HandlerReturnFrame.selection_ref")
        captured_continuation_ref = _require_ref(
            handler_return.captured_continuation_ref,
            "HandlerReturnFrame.captured_continuation_ref",
        )
        finalizer = TerminalResumeFinalizer(
            resume_ref=resume_ref,
            source_ref=reentry.pending_ref,
            source_record_type="ContinuationPending",
            selection_path_ref=reentry.pending_path_ref,
            branch_ref=self._state.branch_ref,
            branch_scope_ref=self._state.branch_scope_ref,
        )
        if self._trace_events_enabled:
            empty_kont_ref = self._kont_ref(
                self._empty_kont_state(),
                continuation_kind="empty-terminal",
                context=handler_return.outer_context,
                result_schema_ref=None,
            )
            self._emit(
                ContinuationResumed(
                    ref=resume_ref,
                    source_ref=reentry.pending_ref,
                    source_record_type="ContinuationPending",
                    declaration_ref=declaration_ref,
                    selection_ref=selection_ref,
                    selection_path_ref=reentry.pending_path_ref,
                    branch_ref=self._state.branch_ref,
                    continuation_ref=captured_continuation_ref,
                    handler_continuation_ref=empty_kont_ref,
                    handler_dynamic_tail_ref=empty_kont_ref,
                    value=state.value,
                    returns_to_handler=False,
                    worker_context_ref=self._context_ref(handler_return.worker_context),
                    handler_context_ref=None,
                    branch_scope_ref=self._state.branch_scope_ref,
                )
            )
        self._push_scheduler_frame(finalizer)
        return Continue(state.value, reentry.terminal_kont, handler_return.worker_context)

    def _step_terminal_fork(self, state: TerminalFork) -> StepState:
        split = self._split_at_handler_return(state.kont)
        if split is None:
            raise RuntimeError("TerminalFork(...) used outside any handler body")

        handler_continuation, handler_return, _handler_return_frame_ref, handler_dynamic_tail = split
        if handler_continuation:
            raise RuntimeError("TerminalFork(...) is valid only in handler answer position")
        if (
            handler_return.selection_path_ref is not None
            and handler_return.selection_path_ref in self._state.consumed_source_paths
        ):
            raise ResumptionUsed(
                f"resumption for handler {handler_return.install.handler_id!r} "
                "already used; TerminalFork is terminal over an unused source"
            )
        if (
            handler_return.declaration_ref is None
            or handler_return.selection_ref is None
            or handler_return.selection_path_ref is None
            or handler_return.captured_continuation_ref is None
        ):
            raise RuntimeError("TerminalFork(...) requires an active selected handler")
        self._state.consume_source_path(handler_return.selection_path_ref)

        branch_refs = tuple(branch_ref for branch_ref, _ in state.branch_values)
        if len(branch_refs) != len(set(branch_refs)):
            raise RuntimeError("TerminalFork branch refs must be unique")

        fork_ref = self._fresh_ref("fork")
        self._state.mark_terminal_path(handler_return.selection_path_ref)
        if self._trace_events_enabled:
            self._emit(
                ForkSummary(
                    ref=fork_ref,
                    declaration_ref=handler_return.declaration_ref,
                    selection_ref=handler_return.selection_ref,
                    selection_path_ref=handler_return.selection_path_ref,
                    branch_ref=self._state.branch_ref,
                    branch_refs=branch_refs,
                    branch_scope_ref=self._state.branch_scope_ref,
                )
            )

        selected_tail = self._push_kont_frame(handler_return.selected_handler_frame, handler_dynamic_tail)
        terminal_kont = self._concat_kont_states(
            self._handler_return_captured_state(handler_return),
            selected_tail,
        )
        if not state.branch_values:
            return Done(Forked({}))
        return EnterTerminalForkBranch(
            fork_ref=fork_ref,
            remaining_branch_values=state.branch_values,
            outcomes=(),
            terminal_kont=terminal_kont,
            handler_return=handler_return,
            parent_branch_ref=self._state.branch_ref,
            parent_branch_scope_ref=self._state.branch_scope_ref,
        )

    def _step_enter_terminal_fork_branch(self, state: EnterTerminalForkBranch) -> StepState:
        branch_ref, value = state.remaining_branch_values[0]
        remaining_branch_values = state.remaining_branch_values[1:]
        handler_return = state.handler_return
        self._check_schema(
            handler_return.operation_result_schema_ref,
            value,
            context=f"resume({handler_return.install.effect_kind!r})",
        )
        fork_branch_ref = self._fresh_ref("fork-branch")
        resume_ref = self._fresh_ref("resume")
        self._set_branch(branch_ref, resume_ref)
        try:
            terminal_continuation_ref = self._kont_ref(
                state.terminal_kont,
                continuation_kind="full",
                context=handler_return.worker_context,
                result_schema_ref=handler_return.operation_result_schema_ref,
            )
        finally:
            self._set_branch(state.parent_branch_ref, state.parent_branch_scope_ref)
        declaration_ref = _require_ref(handler_return.declaration_ref, "HandlerReturnFrame.declaration_ref")
        selection_ref = _require_ref(handler_return.selection_ref, "HandlerReturnFrame.selection_ref")
        selection_path_ref = _require_ref(handler_return.selection_path_ref, "HandlerReturnFrame.selection_path_ref")
        captured_continuation_ref = _require_ref(
            handler_return.captured_continuation_ref,
            "HandlerReturnFrame.captured_continuation_ref",
        )
        if self._trace_events_enabled:
            self._emit(
                ForkBranch(
                    ref=fork_branch_ref,
                    fork_ref=state.fork_ref,
                    declaration_ref=declaration_ref,
                    selection_ref=selection_ref,
                    selection_path_ref=selection_path_ref,
                    branch_ref=branch_ref,
                    continuation_ref=captured_continuation_ref,
                    terminal_continuation_ref=terminal_continuation_ref,
                    value=value,
                    branch_scope_ref=self._state.branch_scope_ref,
                )
            )
        branch_path_ref = self._source_path_ref_for_branch(selection_ref, fork_branch_ref, branch_ref)
        if not self._state.consume_source_path(branch_path_ref):
            raise ResumptionUsed(f"fork branch source {fork_branch_ref!r} already used")

        self._set_branch(branch_ref, resume_ref)
        finalizer = TerminalResumeFinalizer(
            resume_ref=resume_ref,
            source_ref=fork_branch_ref,
            source_record_type="ForkBranch",
            selection_path_ref=branch_path_ref,
            branch_ref=branch_ref,
            branch_scope_ref=resume_ref,
        )
        if self._trace_events_enabled:
            empty_kont_ref = self._kont_ref(
                self._empty_kont_state(),
                continuation_kind="empty-terminal",
                context=handler_return.outer_context,
                result_schema_ref=None,
            )
            self._emit(
                ContinuationResumed(
                    ref=resume_ref,
                    source_ref=fork_branch_ref,
                    source_record_type="ForkBranch",
                    declaration_ref=declaration_ref,
                    selection_ref=selection_ref,
                    selection_path_ref=branch_path_ref,
                    continuation_ref=captured_continuation_ref,
                    handler_continuation_ref=empty_kont_ref,
                    handler_dynamic_tail_ref=empty_kont_ref,
                    branch_ref=branch_ref,
                    value=value,
                    returns_to_handler=False,
                    worker_context_ref=self._context_ref(handler_return.worker_context),
                    handler_context_ref=None,
                    branch_scope_ref=self._state.branch_scope_ref,
                )
            )
        self._push_scheduler_frame(
            ForkAccumulator(
                fork_ref=state.fork_ref,
                remaining_branch_values=remaining_branch_values,
                outcomes=state.outcomes,
                terminal_kont=state.terminal_kont,
                handler_return=handler_return,
                parent_branch_ref=state.parent_branch_ref,
                parent_branch_scope_ref=state.parent_branch_scope_ref,
                current_branch_ref=branch_ref,
            )
        )
        self._push_scheduler_frame(finalizer)
        return Continue(value, state.terminal_kont, handler_return.worker_context)
