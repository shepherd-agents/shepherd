"""Executable defunctionalized abstract machine for the v3 core fragment."""

from __future__ import annotations

from contextlib import contextmanager
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
    KernelEvent,
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
from shepherd_kernel_v3_reference.kernel.runtime_services import EvidenceMode, _KernelRuntimeServices
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
    from collections.abc import Callable, Iterator

    from shepherd_kernel_v3_reference.kernel.continuation_objects import (
        ContinuationObjectBuilder,
    )
    from shepherd_kernel_v3_reference.kernel.continuations import (
        ContinuationImage,
    )
    from shepherd_kernel_v3_reference.kernel.program_admission import KernelProgramInput
    from shepherd_kernel_v3_reference.source.effects import EffectRegistry


@dataclass(frozen=True)
class _ExternalContinuationContext:
    branch_ref: Ref
    branch_scope_ref: Ref | None
    terminal_resume_contexts: tuple[_TerminalResumeContext, ...] = ()


@dataclass(frozen=True)
class _TerminalResumeContext:
    resume_ref: Ref
    source_ref: Ref
    source_record_type: Literal["ContinuationPending", "ForkBranch"]
    selection_path_ref: Ref
    branch_ref: Ref
    branch_scope_ref: Ref | None


class RecursiveKernelEvaluator(_KernelRuntimeServices):
    """Recursive oracle evaluator for a `KernelProgram`.

    This class still owns shared runtime/evidence helpers while the step
    evaluator extraction is in progress. New trace routing should go through
    `StepKernelEvaluator`; use this class when an explicit recursive oracle is
    needed for parity or debugging.
    """

    def __init__(
        self,
        program: KernelProgramInput,
        registry: EffectRegistry | None = None,
        event_sink: Callable[[KernelEvent], None] | None = None,
        *,
        continuation_builder: ContinuationObjectBuilder | None = None,
        evidence_mode: EvidenceMode | str = EvidenceMode.TRACE,
    ):
        super().__init__(
            program,
            registry=registry,
            event_sink=event_sink,
            continuation_builder=continuation_builder,
            evidence_mode=evidence_mode,
        )
        self._terminal_resume_contexts: tuple[_TerminalResumeContext, ...] = ()

    def _external_continuation(self, fn: Callable[[Any], SourceOutcome]) -> Continuation:
        context = _ExternalContinuationContext(
            branch_ref=self._state.branch_ref,
            branch_scope_ref=self._state.branch_scope_ref,
            terminal_resume_contexts=self._terminal_resume_contexts,
        )

        def apply(value: Any) -> SourceOutcome:
            previous_terminal_contexts = self._terminal_resume_contexts
            with self._state.scoped_branch(context.branch_ref, context.branch_scope_ref):
                self._terminal_resume_contexts = context.terminal_resume_contexts
                try:
                    outcome = fn(value)
                    if isinstance(outcome, Completed):
                        for terminal_context in reversed(context.terminal_resume_contexts):
                            self._emit_terminal_resume_result(terminal_context, outcome.value)
                    return outcome
                finally:
                    self._terminal_resume_contexts = previous_terminal_contexts

        return Continuation(apply)

    @contextmanager
    def _scoped_terminal_resume_context(self, context: _TerminalResumeContext) -> Iterator[None]:
        previous = self._terminal_resume_contexts
        self._terminal_resume_contexts = previous + (context,)
        try:
            yield
        finally:
            self._terminal_resume_contexts = previous

    def _emit_terminal_resume_result(self, context: _TerminalResumeContext, value: Any) -> None:
        self._emit(
            TerminalResumeResult(
                ref=self._fresh_ref("terminal-result"),
                resume_ref=context.resume_ref,
                source_ref=context.source_ref,
                source_record_type=context.source_record_type,
                selection_path_ref=context.selection_path_ref,
                branch_ref=context.branch_ref,
                value=value,
                branch_scope_ref=context.branch_scope_ref,
            )
        )

    def run(self, env: Env | None = None) -> SourceOutcome:
        root_env = env or Env()
        root_context = ExecutionContext().with_binding_env_ref(self._env_ref(root_env))
        return self._eval(self.program.root, root_env, self._empty_kont_state(), root_context)

    def _resume_value_from_image(
        self,
        image: ContinuationImage,
        value: Any,
        *,
        operation_result_schema_ref: Ref | None,
        source_label: str,
    ) -> SourceOutcome:
        """Internal Phase-3 spike: resume value-position control from an image.

        This intentionally does not perform source admission, one-shot
        consumption, or carrier provenance checks. Phase 8 replay will own
        those lifecycle rules.
        """

        program_ref = self._program_ref()
        if image.program_ref != program_ref:
            raise RuntimeError("ContinuationImage program_ref does not match this KernelProgram")
        if image.position != "value":
            raise RuntimeError(f"unsupported ContinuationImage.position: {image.position!r}")
        self._check_schema(
            operation_result_schema_ref,
            value,
            context=source_label,
        )
        kont = self._kont_state_from_frames(self._kont_from_image(image.frames))
        context = self._context_from_payload(image.execution_context)
        with self._state.scoped_branch(image.branch_ref, image.branch_scope_ref):
            return self._continue_value(value, kont, context)

    def _eval(
        self,
        control: KComputation,
        env: Env,
        kont: KontState,
        context: ExecutionContext,
    ) -> SourceOutcome:
        while True:
            match control:
                case KPure(expr):
                    return self._continue_value(eval_expr(expr, env), kont, context)

                case KBind(bound=bound, binder_id=binder_id):
                    kont = self._push_kont_frame(BindFrame(binder_id, env, context), kont)
                    control = bound
                    continue

                case KPerform(effect_kind=effect_kind, payload=payload_expr):
                    payload = eval_expr(payload_expr, env)
                    self._check_schema(
                        control.payload_schema_ref,
                        payload,
                        context=f"perform({effect_kind!r}) payload",
                    )
                    return self._perform(control, payload, env, kont, context)

                case KHandle(body=body, handler_env_ref=handler_env_ref, region_ref=region_ref):
                    entry_context = context.with_region_ref(region_ref).with_binding_env_ref(self._env_ref(env))
                    kont = self._push_kont_frame(
                        HandlerFrame(
                            handler_env_ref,
                            env,
                            region_ref,
                            entry_context,
                            context,
                        ),
                        kont,
                    )
                    context = entry_context
                    control = body
                    continue

                case KResumeWith(value=value_expr):
                    return self._resume(eval_expr(value_expr, env), kont, context)

                case KAbort(value=value_expr):
                    return self._abort(eval_expr(value_expr, env), kont)

                case KForward():
                    return self._forward(kont)

                case KTerminalDelay(reason=reason_expr):
                    return self._terminal_delay(eval_expr(reason_expr, env), kont)

                case KTerminalFork(branches=branches):
                    return self._terminal_fork(
                        tuple((branch_ref, eval_expr(value_expr, env)) for branch_ref, value_expr in branches),
                        kont,
                    )

                case _:
                    raise TypeError(f"unknown kernel control: {control!r}")

    def _continue_value(
        self,
        value: Any,
        kont: KontState,
        context: ExecutionContext,
    ) -> SourceOutcome:
        head_ref = kont.head()
        if head_ref is None:
            return Completed(value)

        head, _head_frame_ref = head_ref
        tail = self._kont_tail(kont)

        if isinstance(head, BindFrame):
            binder = self.program.binders[head.binder_id]
            next_env = head.env.extend(binder.param_name, value)
            return self._eval(
                binder.body,
                next_env,
                tail,
                head.context.with_binding_env_ref(self._env_ref(next_env)),
            )

        if isinstance(head, HandlerFrame):
            return self._continue_value(value, tail, head.outer_context)

        if isinstance(head, ResumeReturnFrame):
            if (
                head.resume_ref is not None
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
                        value=value,
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
            return self._continue_value(value, next_kont, head.handler_context)

        if isinstance(head, HandlerReturnFrame):
            self._check_schema(
                head.install.handled_result_schema_ref,
                value,
                context=f"handler({head.install.handler_id!r}) answer",
            )
            if head.selection_ref is not None and head.selection_path_ref is not None:
                capture_ref = self._emit_capture(
                    head,
                    action_kind="return",
                    action_payload=value,
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
            return self._continue_value(value, tail, head.outer_context)

        raise TypeError(f"unknown continuation frame: {head!r}")

    def _perform(
        self,
        op: KPerform,
        payload: Any,
        env: Env,
        kont: KontState,
        context: ExecutionContext,
    ) -> SourceOutcome:
        declaration_ref = self._fresh_ref("declaration")
        full_continuation_ref = self._kont_ref(
            kont,
            continuation_kind="full",
            context=context,
            result_schema_ref=op.operation_result_schema_ref,
        )
        self._emit(
            EffectDeclared(
                ref=declaration_ref,
                program_ref=self._program_ref(),
                effect_kind=op.effect_kind,
                payload=payload,
                full_continuation_ref=full_continuation_ref,
                branch_ref=self._state.branch_ref,
                payload_schema_ref=op.payload_schema_ref,
                operation_result_schema_ref=op.operation_result_schema_ref,
                execution_context_ref=self._context_ref(context),
                branch_scope_ref=self._state.branch_scope_ref,
            )
        )
        split = self._find_handler(op.effect_kind, kont)
        if split is None:

            def cont(value: Any) -> SourceOutcome:
                self._check_schema(
                    op.operation_result_schema_ref,
                    value,
                    context=f"resume({op.effect_kind!r})",
                )
                return self._continue_value(value, kont, context)

            return Suspended(op.effect_kind, payload, self._external_continuation(cont))

        captured, handler_frame, handler_frame_ref, outer, install = split
        return self._select_handler(
            declaration_ref=declaration_ref,
            op=op,
            payload=payload,
            captured=captured,
            handler_frame=handler_frame,
            handler_frame_ref=handler_frame_ref,
            outer=outer,
            install=install,
            worker_context=context,
        )

    def _select_handler(
        self,
        *,
        declaration_ref: Ref,
        op: KPerform,
        payload: Any,
        captured: KontState,
        handler_frame: HandlerFrame,
        handler_frame_ref: Ref,
        outer: KontState,
        install: HandlerInstallDef,
        worker_context: ExecutionContext,
    ) -> SourceOutcome:
        selection_ref = self._fresh_ref("selection")
        captured_ref = self._kont_ref(
            captured,
            continuation_kind="captured-worker",
            context=worker_context,
            result_schema_ref=op.operation_result_schema_ref,
        )
        captured_control_ref = self._kont_control_ref(captured)
        outer_ref = self._kont_ref(
            outer,
            continuation_kind="outer",
            context=handler_frame.outer_context,
            result_schema_ref=install.handled_result_schema_ref,
        )
        outer_control_ref = self._kont_control_ref(outer)
        handler_env = handler_frame.env.extend(install.payload_name, payload)
        handler_context = handler_frame.entry_context.with_binding_env_ref(self._env_ref(handler_env))
        self._emit(
            HandlerSelected(
                ref=selection_ref,
                declaration_ref=declaration_ref,
                selected_binding_ref=install.install_ref,
                handler_id=install.handler_id,
                handler_frame_ref=handler_frame.handler_env_ref,
                captured_continuation_ref=captured_ref,
                outer_continuation_ref=outer_ref,
                captured_continuation_control_ref=captured_control_ref,
                outer_continuation_control_ref=outer_control_ref,
                handled_result_schema_ref=install.handled_result_schema_ref,
                worker_context_ref=self._context_ref(worker_context),
                handler_context_ref=self._context_ref(handler_context),
                outer_context_ref=self._context_ref(handler_frame.outer_context),
                branch_scope_ref=self._state.branch_scope_ref,
            )
        )
        resumption_handle_ref = self._fresh_ref("resumption")
        self._emit(
            ResumptionCreated(
                ref=resumption_handle_ref,
                declaration_ref=declaration_ref,
                selection_ref=selection_ref,
                continuation_ref=captured_ref,
                operation_result_schema_ref=op.operation_result_schema_ref,
                handled_result_schema_ref=install.handled_result_schema_ref,
                branch_scope_ref=self._state.branch_scope_ref,
            )
        )
        selection_path_ref = self._source_path_ref(selection_ref, resumption_handle_ref)
        handler_return = HandlerReturnFrame(
            install=install,
            selected_handler_frame=handler_frame,
            selected_handler_frame_ref=handler_frame_ref,
            handler_env=handler_env,
            captured_state=captured,
            captured_stack_ref=captured.cursor.stack_ref,
            outer_state=outer,
            outer_stack_ref=outer.cursor.stack_ref,
            worker_context=worker_context,
            handler_context=handler_context,
            outer_context=handler_frame.outer_context,
            declaration_ref=declaration_ref,
            selection_ref=selection_ref,
            resumption_handle_ref=resumption_handle_ref,
            selection_path_ref=selection_path_ref,
            captured_continuation_ref=captured_ref,
            outer_continuation_ref=outer_ref,
            captured_continuation_control_ref=captured_control_ref,
            outer_continuation_control_ref=outer_control_ref,
            operation_result_schema_ref=op.operation_result_schema_ref,
            handled_result_schema_ref=install.handled_result_schema_ref,
        )
        return self._eval(install.body, handler_env, self._push_kont_frame(handler_return, outer), handler_context)

    def _resume(
        self,
        value: Any,
        kont: KontState,
        context: ExecutionContext,
    ) -> SourceOutcome:
        split = self._split_at_handler_return(kont)
        if split is None:
            raise RuntimeError("Resume(value) used outside any handler body")

        handler_continuation, handler_return, handler_return_frame_ref, handler_dynamic_tail = split
        if handler_return.selection_path_ref is not None and not (
            self._state.consume_source_path(handler_return.selection_path_ref)
        ):
            raise ResumptionUsed(f"resumption for handler {handler_return.install.handler_id!r} already used")

        self._check_schema(
            handler_return.operation_result_schema_ref,
            value,
            context=f"resume({handler_return.install.effect_kind!r})",
        )

        resume_ref = self._fresh_ref("resume")
        handler_continuation_ref = self._kont_ref(
            handler_continuation,
            continuation_kind="handler-continuation",
            context=context,
            result_schema_ref=handler_return.operation_result_schema_ref,
        )
        handler_dynamic_tail_ref = self._kont_ref(
            handler_dynamic_tail,
            continuation_kind="handler-dynamic-tail",
            context=handler_return.outer_context,
            result_schema_ref=handler_return.handled_result_schema_ref,
        )
        if (
            handler_return.resumption_handle_ref is not None
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
                    value=value,
                    returns_to_handler=True,
                    worker_context_ref=self._context_ref(handler_return.worker_context),
                    handler_context_ref=self._context_ref(context),
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
            handler_context=context,
        )
        worker_tail = self._push_kont_frame(resume_return, handler_dynamic_tail)
        worker_tail = self._push_kont_frame(handler_return.selected_handler_frame, worker_tail)
        worker_kont = self._concat_kont_states(
            self._handler_return_captured_state(handler_return),
            worker_tail,
        )
        return self._continue_value(value, worker_kont, handler_return.worker_context)

    def _abort(self, value: Any, kont: KontState) -> SourceOutcome:
        split = self._split_at_handler_return(kont)
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
            value,
            context=f"handler({handler_return.install.handler_id!r}) answer",
        )
        if handler_return.selection_ref is not None and handler_return.selection_path_ref is not None:
            capture_ref = self._emit_capture(
                handler_return,
                action_kind="abort",
                action_payload=value,
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
        return self._continue_value(value, handler_dynamic_tail, handler_return.outer_context)

    def _forward(self, kont: KontState) -> SourceOutcome:
        split = self._split_at_handler_return(kont)
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
        self._emit(
            SelectionClosed(
                ref=self._fresh_ref("selection-closed"),
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

            def cont(value: Any) -> SourceOutcome:
                self._check_schema(
                    op.operation_result_schema_ref,
                    value,
                    context=f"resume({op.effect_kind!r})",
                )
                return self._continue_value(value, forwarded_kont, handler_return.worker_context)

            return Suspended(op.effect_kind, payload, self._external_continuation(cont))

        prefix, outer_handler_frame, outer_handler_frame_ref, outer_tail, outer_install = split_outer
        selected_prefix_tail = self._push_kont_frame(handler_return.selected_handler_frame, prefix)
        captured = self._concat_kont_states(
            self._handler_return_captured_state(handler_return),
            selected_prefix_tail,
        )
        return self._select_handler(
            declaration_ref=handler_return.declaration_ref,
            op=op,
            payload=payload,
            captured=captured,
            handler_frame=outer_handler_frame,
            handler_frame_ref=outer_handler_frame_ref,
            outer=outer_tail,
            install=outer_install,
            worker_context=handler_return.worker_context,
        )

    def _terminal_delay(self, reason: Any, kont: KontState) -> SourceOutcome:
        split = self._split_at_handler_return(kont)
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
        self._emit(
            ContinuationPending(
                ref=pending_ref,
                declaration_ref=handler_return.declaration_ref,
                selection_ref=handler_return.selection_ref,
                selection_path_ref=handler_return.selection_path_ref,
                continuation_ref=handler_return.captured_continuation_ref,
                operation_result_schema_ref=handler_return.operation_result_schema_ref,
                branch_ref=self._state.branch_ref,
                reason=reason,
                worker_context_ref=self._context_ref(handler_return.worker_context),
                branch_scope_ref=self._state.branch_scope_ref,
            )
        )
        self._emit(
            ContinuationDelay(
                ref=self._fresh_ref("delay"),
                pending_ref=pending_ref,
                reason=reason,
                branch_scope_ref=self._state.branch_scope_ref,
            )
        )

        pending_path_ref = self._source_path_ref(handler_return.selection_ref, pending_ref)
        pending_branch_ref = self._state.branch_ref
        pending_branch_scope_ref = self._state.branch_scope_ref
        selected_tail = self._push_kont_frame(handler_return.selected_handler_frame, handler_dynamic_tail)
        terminal_kont = self._concat_kont_states(
            self._handler_return_captured_state(handler_return),
            selected_tail,
        )

        def cont(value: Any) -> SourceOutcome:
            if not self._state.consume_source_path(pending_path_ref):
                raise ResumptionUsed(f"pending continuation {pending_ref!r} already used")
            self._check_schema(
                handler_return.operation_result_schema_ref,
                value,
                context=f"resume({handler_return.install.effect_kind!r})",
            )
            with self._state.scoped_branch(pending_branch_ref, pending_branch_scope_ref):
                resume_ref = self._fresh_ref("resume")
                terminal_context = _TerminalResumeContext(
                    resume_ref=resume_ref,
                    source_ref=pending_ref,
                    source_record_type="ContinuationPending",
                    selection_path_ref=pending_path_ref,
                    branch_ref=pending_branch_ref,
                    branch_scope_ref=pending_branch_scope_ref,
                )
                empty_kont_ref = self._kont_ref(
                    self._empty_kont_state(),
                    continuation_kind="empty-terminal",
                    context=handler_return.outer_context,
                    result_schema_ref=None,
                )
                self._emit(
                    ContinuationResumed(
                        ref=resume_ref,
                        source_ref=pending_ref,
                        source_record_type="ContinuationPending",
                        declaration_ref=_require_ref(
                            handler_return.declaration_ref, "HandlerReturnFrame.declaration_ref"
                        ),
                        selection_ref=_require_ref(handler_return.selection_ref, "HandlerReturnFrame.selection_ref"),
                        selection_path_ref=pending_path_ref,
                        branch_ref=self._state.branch_ref,
                        continuation_ref=_require_ref(
                            handler_return.captured_continuation_ref,
                            "HandlerReturnFrame.captured_continuation_ref",
                        ),
                        handler_continuation_ref=empty_kont_ref,
                        handler_dynamic_tail_ref=empty_kont_ref,
                        value=value,
                        returns_to_handler=False,
                        worker_context_ref=self._context_ref(handler_return.worker_context),
                        handler_context_ref=None,
                        branch_scope_ref=self._state.branch_scope_ref,
                    )
                )
                with self._scoped_terminal_resume_context(terminal_context):
                    outcome = self._continue_value(
                        value,
                        terminal_kont,
                        handler_return.worker_context,
                    )
                if isinstance(outcome, Completed):
                    self._emit_terminal_resume_result(terminal_context, outcome.value)
            return outcome

        return Delayed(reason, self._external_continuation(cont))

    def _terminal_fork(
        self,
        branch_values: tuple[tuple[Ref, Any], ...],
        kont: KontState,
    ) -> SourceOutcome:
        split = self._split_at_handler_return(kont)
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

        branch_refs = tuple(branch_ref for branch_ref, _ in branch_values)
        if len(branch_refs) != len(set(branch_refs)):
            raise RuntimeError("TerminalFork branch refs must be unique")

        fork_ref = self._fresh_ref("fork")
        self._state.mark_terminal_path(handler_return.selection_path_ref)
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
        outcomes: dict[str, SourceOutcome] = {}
        for branch_ref, value in branch_values:
            self._check_schema(
                handler_return.operation_result_schema_ref,
                value,
                context=f"resume({handler_return.install.effect_kind!r})",
            )
            fork_branch_ref = self._fresh_ref("fork-branch")
            resume_ref = self._fresh_ref("resume")
            with self._state.scoped_branch(branch_ref, resume_ref):
                terminal_continuation_ref = self._kont_ref(
                    terminal_kont,
                    continuation_kind="full",
                    context=handler_return.worker_context,
                    result_schema_ref=handler_return.operation_result_schema_ref,
                )
            self._emit(
                ForkBranch(
                    ref=fork_branch_ref,
                    fork_ref=fork_ref,
                    declaration_ref=handler_return.declaration_ref,
                    selection_ref=handler_return.selection_ref,
                    selection_path_ref=handler_return.selection_path_ref,
                    branch_ref=branch_ref,
                    continuation_ref=handler_return.captured_continuation_ref,
                    terminal_continuation_ref=terminal_continuation_ref,
                    value=value,
                    branch_scope_ref=self._state.branch_scope_ref,
                )
            )
            branch_path_ref = f"path:{handler_return.selection_ref}/{fork_branch_ref}/{branch_ref}"
            if not self._state.consume_source_path(branch_path_ref):
                raise ResumptionUsed(f"fork branch source {fork_branch_ref!r} already used")
            with self._state.scoped_branch(branch_ref, resume_ref):
                terminal_context = _TerminalResumeContext(
                    resume_ref=resume_ref,
                    source_ref=fork_branch_ref,
                    source_record_type="ForkBranch",
                    selection_path_ref=branch_path_ref,
                    branch_ref=branch_ref,
                    branch_scope_ref=resume_ref,
                )
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
                        declaration_ref=handler_return.declaration_ref,
                        selection_ref=handler_return.selection_ref,
                        selection_path_ref=branch_path_ref,
                        continuation_ref=handler_return.captured_continuation_ref,
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
                with self._scoped_terminal_resume_context(terminal_context):
                    outcome = self._continue_value(
                        value,
                        terminal_kont,
                        handler_return.worker_context,
                    )
                outcomes[branch_ref] = outcome
                if isinstance(outcome, Completed):
                    self._emit_terminal_resume_result(terminal_context, outcome.value)

        return Forked(outcomes)
