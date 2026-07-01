"""Normalized core trace records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Union

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.kernel.ir import Ref


@dataclass(frozen=True)
class EffectDeclaration:
    ref: Ref
    program_ref: Ref | None
    effect_kind: str
    payload: Any
    full_continuation_ref: Ref
    branch_ref: Ref
    payload_schema_ref: Ref | None
    operation_result_schema_ref: Ref | None
    execution_context_ref: Ref | None = None
    branch_scope_ref: Ref | None = None


@dataclass(frozen=True)
class HandlerSelection:
    ref: Ref
    declaration_ref: Ref
    selected_binding_ref: Ref
    handler_id: str
    handler_frame_ref: Ref
    captured_continuation_ref: Ref
    outer_continuation_ref: Ref
    captured_continuation_control_ref: Ref
    outer_continuation_control_ref: Ref
    handled_result_schema_ref: Ref
    worker_context_ref: Ref | None = None
    handler_context_ref: Ref | None = None
    outer_context_ref: Ref | None = None
    branch_scope_ref: Ref | None = None


@dataclass(frozen=True)
class ResumptionHandle:
    ref: Ref
    declaration_ref: Ref
    selection_ref: Ref
    continuation_ref: Ref
    operation_result_schema_ref: Ref | None
    handled_result_schema_ref: Ref
    branch_scope_ref: Ref | None = None


@dataclass(frozen=True)
class ContinuationResume:
    ref: Ref
    source_ref: Ref
    source_record_type: Literal["ResumptionHandle", "ContinuationPending", "ForkBranch"]
    declaration_ref: Ref
    selection_ref: Ref
    selection_path_ref: Ref
    continuation_ref: Ref
    handler_continuation_ref: Ref
    handler_dynamic_tail_ref: Ref
    branch_ref: Ref
    value: Any
    returns_to_handler: bool
    worker_context_ref: Ref | None = None
    handler_context_ref: Ref | None = None
    branch_scope_ref: Ref | None = None


@dataclass(frozen=True)
class ResumeReturn:
    ref: Ref
    resume_ref: Ref
    selection_ref: Ref
    selection_path_ref: Ref
    branch_ref: Ref
    handler_continuation_ref: Ref
    handler_dynamic_tail_ref: Ref
    value: Any
    handler_context_ref: Ref | None = None
    branch_scope_ref: Ref | None = None


@dataclass(frozen=True)
class EffectCapture:
    ref: Ref
    selection_ref: Ref
    selection_path_ref: Ref
    branch_ref: Ref
    action_kind: Literal["return", "abort"]
    action_payload: Any
    continuation_disposition: Literal["completed", "aborted"]
    outer_context_ref: Ref | None = None
    branch_scope_ref: Ref | None = None


@dataclass(frozen=True)
class SelectionClosed:
    ref: Ref
    selection_ref: Ref
    selection_path_ref: Ref
    branch_ref: Ref
    reason: Literal[
        "skipped_by_outer_abort",
        "abandoned",
        "runtime_failure",
        "cancelled",
        "forwarded",
    ]
    caused_by_ref: Ref
    caused_by_record_type: Literal["EffectCapture", "RuntimeFailure", "HandlerForward"]
    closed_by_selection_ref: Ref
    closed_by_selection_path_ref: Ref
    branch_scope_ref: Ref | None = None


@dataclass(frozen=True)
class HandlerForward:
    ref: Ref
    declaration_ref: Ref
    skipped_selection_ref: Ref
    skipped_binding_ref: Ref
    skipped_selection_path_ref: Ref
    branch_ref: Ref
    branch_scope_ref: Ref | None = None


@dataclass(frozen=True)
class ContinuationPending:
    ref: Ref
    declaration_ref: Ref
    selection_ref: Ref
    selection_path_ref: Ref
    continuation_ref: Ref
    operation_result_schema_ref: Ref | None
    branch_ref: Ref
    reason: Any
    worker_context_ref: Ref | None = None
    branch_scope_ref: Ref | None = None


@dataclass(frozen=True)
class ContinuationDelay:
    ref: Ref
    pending_ref: Ref
    reason: Any
    branch_scope_ref: Ref | None = None


@dataclass(frozen=True)
class ForkSummary:
    ref: Ref
    declaration_ref: Ref
    selection_ref: Ref
    selection_path_ref: Ref
    branch_ref: Ref
    branch_refs: tuple[Ref, ...]
    branch_scope_ref: Ref | None = None


@dataclass(frozen=True)
class ForkBranch:
    ref: Ref
    fork_ref: Ref
    declaration_ref: Ref
    selection_ref: Ref
    selection_path_ref: Ref
    branch_ref: Ref
    continuation_ref: Ref
    value: Any
    terminal_continuation_ref: Ref | None = None
    branch_scope_ref: Ref | None = None


@dataclass(frozen=True)
class TerminalResumeResult:
    ref: Ref
    resume_ref: Ref
    source_ref: Ref
    source_record_type: Literal["ContinuationPending", "ForkBranch"]
    selection_path_ref: Ref
    branch_ref: Ref
    value: Any
    branch_scope_ref: Ref | None = None


TraceRecord = Union[
    ContinuationDelay,
    ContinuationPending,
    EffectDeclaration,
    ForkBranch,
    ForkSummary,
    HandlerForward,
    HandlerSelection,
    ResumptionHandle,
    ContinuationResume,
    TerminalResumeResult,
    ResumeReturn,
    EffectCapture,
    SelectionClosed,
]
