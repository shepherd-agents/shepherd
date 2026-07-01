"""Validation for normalized core trace records.

Two executable boundaries are exposed:

- Core-0 is the ordinary answer-producing handler fragment: a selected handler
  may answer directly or may invoke its callable resumption once and receive a
  `ResumeReturn` before `EffectCapture(return, completed)`.
- Core-A extends Core-0 with answer-position `Abort` and `SelectionClosed`
  records for selected paths skipped or abandoned by a dynamically nested
  handler answer.
- Runtime validation extends Core-A with production-normalized same-path
  closure evidence for runtime failures and cancellation. This profile is
  trace-valid evidence, not a Core-A proof claim.

`validate_core_trace` is the default executable-core validator and aliases
Core-A.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypeVar, get_args

from shepherd_kernel_v3_reference.kernel.continuation_objects import (
    BindFramePayload,
    ContinuationControlIdentity,
    ContinuationEmptyStack,
    ContinuationEnvEmpty,
    ContinuationEnvNode,
    ContinuationFrameNode,
    ContinuationFramePayload,
    ContinuationFrameSummary,
    ContinuationObject,
    ContinuationRoot,
    ContinuationStackConcat,
    ContinuationStackNode,
    ContinuationStackSummary,
    HandlerFramePayload,
    HandlerReturnFramePayload,
    ResumeReturnFramePayload,
    TerminalResultFramePayload,
    continuation_control_identity_ref,
    continuation_frame_payload_child_roles,
    continuation_object_ref,
)
from shepherd_kernel_v3_reference.kernel.program_admission import KernelProgramInput, ensure_prepared_kernel_program
from shepherd_kernel_v3_reference.kernel.refs import content_ref
from shepherd_kernel_v3_reference.paths import source_path_ref
from shepherd_kernel_v3_reference.profiles import PUBLICATION_EXPERIMENTAL
from shepherd_kernel_v3_reference.source.outcomes import Completed, Forked, SourceOutcome
from shepherd_kernel_v3_reference.trace.machine import TraceEvaluatorEngine, run_trace
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
    from collections.abc import Container, Mapping

    from shepherd_kernel_v3_reference.kernel.ir import Ref
    from shepherd_kernel_v3_reference.source.effects import EffectRegistry
    from shepherd_kernel_v3_reference.source.values import Env

_RUNTIME_OPERATIONAL_CLOSURE_REASONS = frozenset({"runtime_failure", "cancelled"})
TRACE_EVIDENCE_BUNDLE_SCHEMA_VERSION = "shepherd_kernel_v3_reference.trace-evidence-bundle.v2"
TraceEvidenceValidationProfile = Literal["lifecycle-only", "runtime-with-continuations"]
_T = TypeVar("_T")
_PUBLICATION_EVIDENCE_UNSUPPORTED = (
    "publication-experimental continuation evidence artifacts are not supported; "
    "validate publication trace lifecycle with validate_publication_experimental_trace(...)"
)

_ContinuationStackObject = ContinuationEmptyStack | ContinuationStackNode | ContinuationStackConcat
_ContinuationEnvObject = ContinuationEnvEmpty | ContinuationEnvNode
_ContinuationRole = Literal["stack", "frame", "env"]
_PUBLICATION_EXPERIMENTAL_RECORD_TYPES = (
    ContinuationDelay,
    ContinuationPending,
    ForkBranch,
    ForkSummary,
    HandlerForward,
    TerminalResumeResult,
)


def _trace_record_types() -> tuple[type[Any], ...]:
    record_types: list[type[Any]] = []
    for arg in get_args(TraceRecord):
        if not isinstance(arg, type):
            raise TypeError(f"TraceRecord contains a non-runtime type: {arg!r}")
        record_types.append(arg)
    return tuple(record_types)


_TRACE_RECORD_TYPES = _trace_record_types()

_CONTINUATION_REF_FIELDS_BY_RECORD_TYPE: dict[type[TraceRecord], tuple[str, ...]] = {
    EffectDeclaration: ("full_continuation_ref",),
    HandlerSelection: ("captured_continuation_ref", "outer_continuation_ref"),
    ResumptionHandle: ("continuation_ref",),
    ContinuationResume: (
        "continuation_ref",
        "handler_continuation_ref",
        "handler_dynamic_tail_ref",
    ),
    ResumeReturn: ("handler_continuation_ref", "handler_dynamic_tail_ref"),
    ContinuationPending: ("continuation_ref",),
    ForkBranch: ("continuation_ref", "terminal_continuation_ref"),
}


class TraceValidationError(ValueError):
    """Raised when a normalized trace violates core lifecycle constraints."""


@dataclass(frozen=True)
class TraceEvidenceBundle:
    bundle_schema_version: str
    trace: Any
    continuation_root_refs: tuple[Ref, ...]
    continuation_objects: dict[Ref, ContinuationObject]
    validation_profile: TraceEvidenceValidationProfile
    continuation_ref_map: Mapping[Ref, Ref] = field(default_factory=dict)
    continuation_control_ref_map: Mapping[Ref, Ref] = field(default_factory=dict)
    context_ref_map: Mapping[Ref, Ref] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.bundle_schema_version != TRACE_EVIDENCE_BUNDLE_SCHEMA_VERSION:
            raise TraceValidationError(
                f"TraceEvidenceBundle.bundle_schema_version must be {TRACE_EVIDENCE_BUNDLE_SCHEMA_VERSION!r}"
            )
        if self.validation_profile not in ("lifecycle-only", "runtime-with-continuations"):
            raise TraceValidationError(f"unknown TraceEvidenceBundle.validation_profile: {self.validation_profile!r}")
        object.__setattr__(self, "continuation_root_refs", tuple(self.continuation_root_refs))
        object.__setattr__(self, "continuation_objects", dict(self.continuation_objects))
        object.__setattr__(self, "continuation_ref_map", dict(self.continuation_ref_map))
        object.__setattr__(self, "continuation_control_ref_map", dict(self.continuation_control_ref_map))
        object.__setattr__(self, "context_ref_map", dict(self.context_ref_map))


_UNSPECIFIED = object()


@dataclass(frozen=True)
class _TraceState:
    declarations: dict[str, EffectDeclaration]
    selections: dict[str, HandlerSelection]
    handles: dict[str, ResumptionHandle]
    selected_declarations: set[str]
    handles_by_selection: Counter[str]
    resumes: dict[str, ContinuationResume]
    returns: Counter[tuple[str, str]]
    captures: Counter[tuple[str, str]]
    closures: Counter[tuple[str, str]]
    open_paths: set[str]
    program_ref: str | None


@dataclass(frozen=True)
class _ContinuationRootExpectation:
    ref: Ref
    continuation_kind: str
    program_ref: Ref
    branch_ref: Ref
    branch_scope_ref: Ref | None
    execution_context_ref: Ref
    result_schema_ref: Ref | None
    context: str


@dataclass
class _SelectedPathState:
    path_ref: str
    selection_ref: str
    source_ref: str
    branch_ref: str
    opened_at: int
    branch_scope_ref: str | None = None
    terminal_record_type: str | None = None
    terminal_ref: str | None = None


@dataclass(frozen=True)
class _BranchScopeState:
    scope_ref: str
    branch_ref: str
    resume_ref: str
    source_ref: str


class _LifecycleLedger:
    """Resource-state ledger for common trace lifecycle facts."""

    def __init__(self) -> None:
        self.declarations: dict[str, EffectDeclaration] = {}
        self.selections: dict[str, HandlerSelection] = {}
        self.selected_declarations: set[str] = set()
        self.selections_by_declaration: Counter[object] = Counter()
        self.handles: dict[str, ResumptionHandle] = {}
        self.captures_by_ref: dict[str, tuple[EffectCapture, int]] = {}
        self.handles_by_selection: Counter[str] = Counter()
        self.handle_ref_by_selection: dict[str, str] = {}
        self.resumes: dict[str, ContinuationResume] = {}
        self.resumes_by_source_path: Counter[tuple[str, str]] = Counter()
        self.returns: Counter[tuple[str, str]] = Counter()
        self.captures: Counter[tuple[str, str]] = Counter()
        self.closures: Counter[tuple[str, str]] = Counter()
        self.all_refs: set[str] = set()
        self.paths: dict[str, _SelectedPathState] = {}
        self.open_path_refs_by_branch_scope: dict[str | None, set[str]] = {}
        self.open_path_refs_by_selection: dict[str, set[str]] = {}
        self.open_selection_refs_by_control_ref: dict[str, set[str]] = {}
        self.selection_parents: dict[str, set[str]] = {}
        self.active_resumed_paths: set[tuple[str, str]] = set()
        self.active_resumed_selection_counts: Counter[str] = Counter()
        self.open_callable_resume_refs_by_path: dict[tuple[str, str], set[str]] = {}
        self.open_callable_resume_refs_by_branch_scope: dict[str | None, set[str]] = {}
        self.program_ref: str | None = None

    def reject_record_ref(self, record: TraceRecord) -> None:
        _reject_ref(self.all_refs, record.ref, type(record).__name__)

    def declare_effect(self, record: EffectDeclaration) -> None:
        _reject_duplicate(self.declarations, record.ref, "declaration")
        _require_present(record.program_ref, "EffectDeclaration.program_ref")
        if self.program_ref is None:
            self.program_ref = record.program_ref
        else:
            _require(
                record.program_ref == self.program_ref,
                "all declarations in a trace must cite the same program",
            )
        _require_present(
            record.execution_context_ref,
            "EffectDeclaration.execution_context_ref",
        )
        self.declarations[record.ref] = record

    def select_handler(
        self,
        record: HandlerSelection,
        idx: int,
        *,
        duplicate_key: object | None = None,
    ) -> None:
        declaration = _require_lookup(self.declarations, record.declaration_ref, "selection cites missing declaration")
        _require_present(
            record.worker_context_ref,
            "HandlerSelection.worker_context_ref",
        )
        _require_present(
            record.handler_context_ref,
            "HandlerSelection.handler_context_ref",
        )
        _require_present(
            record.outer_context_ref,
            "HandlerSelection.outer_context_ref",
        )
        _require(
            record.worker_context_ref == declaration.execution_context_ref,
            "selection worker context must match declaration execution context",
        )
        _reject_duplicate(self.selections, record.ref, "selection")
        self.selections[record.ref] = record
        parents = set(self.open_selection_refs_by_control_ref.get(record.outer_continuation_control_ref, ()))
        parents.update(self.active_resumed_selection_counts)
        parents.discard(record.ref)
        self.selection_parents[record.ref] = parents
        key = duplicate_key if duplicate_key is not None else record.declaration_ref
        self.selections_by_declaration[key] += 1
        _require(
            self.selections_by_declaration[key] == 1,
            f"declaration {record.declaration_ref!r} has duplicate handler selections",
        )
        self.selected_declarations.add(record.declaration_ref)

    def open_resumption_handle_path(
        self,
        record: ResumptionHandle,
        *,
        branch_ref: str,
        branch_scope_ref: str | None,
        idx: int,
    ) -> None:
        selection = _require_lookup(self.selections, record.selection_ref, "resumption handle cites missing selection")
        declaration = _require_lookup(
            self.declarations,
            record.declaration_ref,
            "resumption handle cites missing declaration",
        )
        _require(
            record.declaration_ref == selection.declaration_ref,
            "resumption handle declaration disagrees with selection",
        )
        _require(
            record.continuation_ref == selection.captured_continuation_ref,
            "resumption handle must name selected captured continuation",
        )
        _require(
            record.operation_result_schema_ref == declaration.operation_result_schema_ref,
            "resumption handle operation-result schema disagrees with declaration",
        )
        _require(
            record.handled_result_schema_ref == selection.handled_result_schema_ref,
            "resumption handle handled-result schema disagrees with selection",
        )
        _reject_duplicate(self.handles, record.ref, "resumption handle")
        self.handles[record.ref] = record
        self.handles_by_selection[record.selection_ref] += 1
        _require(
            self.handles_by_selection[record.selection_ref] == 1,
            f"selection {record.selection_ref!r} has duplicate resumption handles",
        )
        self.handle_ref_by_selection[record.selection_ref] = record.ref
        path_ref = source_path_ref(record.selection_ref, record.ref, branch_ref)
        self.open_path(
            path_ref,
            selection_ref=record.selection_ref,
            source_ref=record.ref,
            branch_ref=branch_ref,
            branch_scope_ref=branch_scope_ref,
            idx=idx,
            context="selected",
        )

    def handle_ref_for_selection(self, selection_ref: str) -> str:
        return _require_lookup(
            self.handle_ref_by_selection,
            selection_ref,
            f"selection {selection_ref!r} must have one handle",
        )

    def open_path(
        self,
        path_ref: str,
        *,
        selection_ref: str,
        source_ref: str,
        branch_ref: str,
        branch_scope_ref: str | None,
        idx: int,
        context: str,
    ) -> None:
        _require(path_ref not in self.paths, f"duplicate {context} path ref: {path_ref!r}")
        path = _SelectedPathState(
            path_ref=path_ref,
            selection_ref=selection_ref,
            source_ref=source_ref,
            branch_ref=branch_ref,
            branch_scope_ref=branch_scope_ref,
            opened_at=idx,
        )
        self.paths[path_ref] = path
        self._index_open_path(path)

    def _index_open_path(self, path: _SelectedPathState) -> None:
        self.open_path_refs_by_branch_scope.setdefault(path.branch_scope_ref, set()).add(path.path_ref)
        self.open_path_refs_by_selection.setdefault(path.selection_ref, set()).add(path.path_ref)
        selection = self.selections.get(path.selection_ref)
        if selection is not None:
            self.open_selection_refs_by_control_ref.setdefault(
                selection.captured_continuation_control_ref,
                set(),
            ).add(path.selection_ref)

    def terminalize_path(
        self,
        path: _SelectedPathState,
        terminal_record_type: str,
        terminal_ref: str,
    ) -> None:
        _terminalize_path(path, terminal_record_type, terminal_ref)
        branch_paths = self.open_path_refs_by_branch_scope.get(path.branch_scope_ref)
        if branch_paths is not None:
            branch_paths.discard(path.path_ref)
            if not branch_paths:
                del self.open_path_refs_by_branch_scope[path.branch_scope_ref]

        selection_paths = self.open_path_refs_by_selection.get(path.selection_ref)
        if selection_paths is not None:
            selection_paths.discard(path.path_ref)
            if not selection_paths:
                del self.open_path_refs_by_selection[path.selection_ref]
                selection = self.selections.get(path.selection_ref)
                if selection is not None:
                    selection_refs = self.open_selection_refs_by_control_ref.get(
                        selection.captured_continuation_control_ref
                    )
                    if selection_refs is not None:
                        selection_refs.discard(path.selection_ref)
                        if not selection_refs:
                            del self.open_selection_refs_by_control_ref[selection.captured_continuation_control_ref]

        active_key = (path.selection_ref, path.path_ref)
        if active_key in self.active_resumed_paths:
            self.active_resumed_paths.discard(active_key)
            self.active_resumed_selection_counts[path.selection_ref] -= 1
            if self.active_resumed_selection_counts[path.selection_ref] <= 0:
                del self.active_resumed_selection_counts[path.selection_ref]

        self._discard_open_callable_resumes_for_path(path.selection_ref, path.path_ref)

    def open_paths_for_branch_scope(self, branch_scope_ref: str | None) -> set[str]:
        return set(self.open_path_refs_by_branch_scope.get(branch_scope_ref, ()))

    def open_callable_resume_refs_for_branch_scope(self, branch_scope_ref: str | None) -> set[str]:
        return set(self.open_callable_resume_refs_by_branch_scope.get(branch_scope_ref, ()))

    def _track_open_callable_resume(self, resume: ContinuationResume) -> None:
        key = (resume.selection_ref, resume.selection_path_ref)
        self.open_callable_resume_refs_by_path.setdefault(key, set()).add(resume.ref)
        self.open_callable_resume_refs_by_branch_scope.setdefault(resume.branch_scope_ref, set()).add(resume.ref)

    def _discard_open_callable_resume(self, resume: ContinuationResume) -> None:
        key = (resume.selection_ref, resume.selection_path_ref)
        path_resumes = self.open_callable_resume_refs_by_path.get(key)
        if path_resumes is not None:
            path_resumes.discard(resume.ref)
            if not path_resumes:
                del self.open_callable_resume_refs_by_path[key]

        branch_resumes = self.open_callable_resume_refs_by_branch_scope.get(resume.branch_scope_ref)
        if branch_resumes is not None:
            branch_resumes.discard(resume.ref)
            if not branch_resumes:
                del self.open_callable_resume_refs_by_branch_scope[resume.branch_scope_ref]

    def _discard_open_callable_resumes_for_path(self, selection_ref: str, path_ref: str) -> None:
        resume_refs = self.open_callable_resume_refs_by_path.pop((selection_ref, path_ref), set())
        for resume_ref in resume_refs:
            resume = self.resumes.get(resume_ref)
            if resume is None:
                continue
            branch_resumes = self.open_callable_resume_refs_by_branch_scope.get(resume.branch_scope_ref)
            if branch_resumes is not None:
                branch_resumes.discard(resume_ref)
                if not branch_resumes:
                    del self.open_callable_resume_refs_by_branch_scope[resume.branch_scope_ref]

    def resume_callable_source(
        self,
        record: ContinuationResume,
        *,
        one_shot_message: str,
    ) -> None:
        handle = _require_lookup(self.handles, record.source_ref, "resume cites missing resumption handle")
        _require(record.source_record_type == "ResumptionHandle", "unsupported resume source")
        _require(record.returns_to_handler, "core callable resume must return to handler")
        _require(record.selection_ref == handle.selection_ref, "resume selection mismatch")
        _require(
            record.declaration_ref == handle.declaration_ref,
            "resume declaration mismatch",
        )
        _require(
            record.continuation_ref == handle.continuation_ref,
            "resume continuation mismatch",
        )
        selection = self.selections[record.selection_ref]
        _require_present(
            record.worker_context_ref,
            "ContinuationResume.worker_context_ref",
        )
        _require_present(
            record.handler_context_ref,
            "ContinuationResume.handler_context_ref",
        )
        _require(
            record.worker_context_ref == selection.worker_context_ref,
            "resume worker context must match selection worker context",
        )
        _require(
            record.selection_path_ref == source_path_ref(record.selection_ref, record.source_ref, record.branch_ref),
            "resume selected path mismatch",
        )
        path = _require_path(self.paths, record.selection_path_ref, "resume")
        _require_path_matches(
            path,
            selection_ref=record.selection_ref,
            source_ref=record.source_ref,
            branch_ref=record.branch_ref,
            branch_scope_ref=record.branch_scope_ref,
            context="resume",
        )
        _require_path_open(path, "resume")
        _reject_duplicate(self.resumes, record.ref, "resume")
        self.resumes_by_source_path[(record.source_ref, record.selection_path_ref)] += 1
        _require(
            self.resumes_by_source_path[(record.source_ref, record.selection_path_ref)] == 1,
            one_shot_message,
        )
        self.resumes[record.ref] = record
        self.active_resumed_paths.add((record.selection_ref, record.selection_path_ref))
        self.active_resumed_selection_counts[record.selection_ref] += 1
        self._track_open_callable_resume(record)

    def record_resume_return(self, record: ResumeReturn) -> None:
        resume = _require_lookup(self.resumes, record.resume_ref, "resume return cites missing resume")
        _require(record.selection_ref == resume.selection_ref, "resume return selection mismatch")
        _require(
            record.selection_path_ref == resume.selection_path_ref,
            "resume return selected path mismatch",
        )
        _require(record.branch_ref == resume.branch_ref, "resume return branch mismatch")
        _require(
            record.handler_continuation_ref == resume.handler_continuation_ref,
            "resume return handler continuation mismatch",
        )
        _require(
            record.handler_dynamic_tail_ref == resume.handler_dynamic_tail_ref,
            "resume return dynamic tail mismatch",
        )
        _require_present(
            record.handler_context_ref,
            "ResumeReturn.handler_context_ref",
        )
        _require(
            record.handler_context_ref == resume.handler_context_ref,
            "resume return handler context must match resume handler context",
        )
        path = _require_path(self.paths, record.selection_path_ref, "resume return")
        _require_path_matches(
            path,
            selection_ref=record.selection_ref,
            source_ref=resume.source_ref,
            branch_ref=record.branch_ref,
            branch_scope_ref=record.branch_scope_ref,
            context="resume return",
        )
        _require_path_open(path, "resume return")
        self.returns[(record.resume_ref, record.selection_path_ref)] += 1
        _require(
            self.returns[(record.resume_ref, record.selection_path_ref)] == 1,
            f"resume {record.resume_ref!r} path {record.selection_path_ref!r} has duplicate returns",
        )
        active_key = (record.selection_ref, record.selection_path_ref)
        if active_key in self.active_resumed_paths:
            self.active_resumed_paths.discard(active_key)
            self.active_resumed_selection_counts[record.selection_ref] -= 1
            if self.active_resumed_selection_counts[record.selection_ref] <= 0:
                del self.active_resumed_selection_counts[record.selection_ref]
        self._discard_open_callable_resume(resume)

    def record_effect_capture(
        self,
        record: EffectCapture,
        *,
        boundary: str | None,
        idx: int,
    ) -> None:
        selection = _require_lookup(self.selections, record.selection_ref, "capture cites missing selection")
        _require_capture_action_matches_disposition(record)
        if boundary == "core0":
            _require(
                record.action_kind == "return" and record.continuation_disposition == "completed",
                "Core-0 admits only return/completed captures",
            )
        _require_present(record.outer_context_ref, "EffectCapture.outer_context_ref")
        _require(
            record.outer_context_ref == selection.outer_context_ref,
            "capture outer context must match selection outer context",
        )
        path = _require_path(self.paths, record.selection_path_ref, "capture")
        _require_path_matches(
            path,
            selection_ref=record.selection_ref,
            branch_ref=record.branch_ref,
            branch_scope_ref=record.branch_scope_ref,
            context="capture",
        )
        _require_path_open(path, "capture")
        open_callable_resumes = self.open_callable_resume_refs_by_path.get(
            (record.selection_ref, record.selection_path_ref),
            set(),
        )
        _require(
            not open_callable_resumes
            or (
                boundary == "runtime" and record.action_kind == "abort" and record.continuation_disposition == "aborted"
            ),
            "capture cannot precede matching ResumeReturn",
        )
        self.captures[(record.selection_ref, record.selection_path_ref)] += 1
        self.captures_by_ref[record.ref] = (record, idx)
        self.terminalize_path(path, "EffectCapture", record.ref)

    def record_selection_closed_by_capture(self, record: SelectionClosed, *, boundary: str | None) -> None:
        _require(record.selection_ref in self.selections, "selection closure cites missing selection")
        _require(
            record.caused_by_record_type == "EffectCapture",
            "selection closure cites unsupported cause record type",
        )
        path = _require_path(self.paths, record.selection_path_ref, "selection closure")
        _require_lookup(
            self.selections,
            record.closed_by_selection_ref,
            "selection closure cites missing closing selection",
        )
        closed_by_path = _require_path(
            self.paths,
            record.closed_by_selection_path_ref,
            "selection closure closing path",
        )
        cause_info = _require_lookup(
            self.captures_by_ref, record.caused_by_ref, "selection closure cites missing cause"
        )
        cause, cause_idx = cause_info
        if record.reason in _RUNTIME_OPERATIONAL_CLOSURE_REASONS:
            self.record_runtime_selection_closed_by_capture(
                record,
                cause=cause,
                cause_idx=cause_idx,
                path=path,
                closed_by_path=closed_by_path,
                boundary=boundary,
            )
            return
        _require(
            cause.selection_ref == record.closed_by_selection_ref
            and cause.selection_path_ref == record.closed_by_selection_path_ref,
            "selection closure cause must be the closing selected path terminal capture",
        )
        _require(
            record.closed_by_selection_ref != record.selection_ref
            or record.closed_by_selection_path_ref != record.selection_path_ref,
            "selection closure cannot close its own selected path",
        )
        _require(
            cause_idx > path.opened_at,
            "selection closure cause must occur after closed path opens",
        )
        _require_path_matches(
            closed_by_path,
            selection_ref=record.closed_by_selection_ref,
            branch_ref=record.branch_ref,
            branch_scope_ref=record.branch_scope_ref,
            context="selection closure closing path",
        )
        if record.reason == "skipped_by_outer_abort":
            _require(
                cause.action_kind == "abort" and cause.continuation_disposition == "aborted",
                "selection closure abort reason disagrees with cause",
            )
        elif record.reason == "abandoned":
            _require(
                cause.action_kind == "return" and cause.continuation_disposition == "completed",
                "selection closure abandoned reason disagrees with cause",
            )
        else:
            raise TraceValidationError(f"unsupported selection closure reason: {record.reason!r}")
        _require(
            _is_selection_ancestor(
                record.selection_ref,
                record.closed_by_selection_ref,
                self.selection_parents,
            ),
            "selection closure closing selection is not dynamically nested under closed selection",
        )
        self.close_selection_path(record)

    def record_runtime_selection_closed_by_capture(
        self,
        record: SelectionClosed,
        *,
        cause: EffectCapture,
        cause_idx: int,
        path: _SelectedPathState,
        closed_by_path: _SelectedPathState,
        boundary: str | None,
    ) -> None:
        _require(
            boundary == "runtime",
            "Core-A does not admit runtime-operational selection closures",
        )
        _require(
            record.closed_by_selection_ref == record.selection_ref
            and record.closed_by_selection_path_ref == record.selection_path_ref,
            "runtime-operational selection closure must close its own selected path",
        )
        _require(
            cause.selection_ref == record.selection_ref and cause.selection_path_ref == record.selection_path_ref,
            "runtime-operational selection closure cause must be the same selected path abort capture",
        )
        _require(
            cause.action_kind == "abort" and cause.continuation_disposition == "aborted",
            "runtime-operational selection closure reason disagrees with cause",
        )
        _require(
            path.terminal_record_type == "EffectCapture" and path.terminal_ref == cause.ref,
            "runtime-operational selection closure must follow the same selected path abort capture",
        )
        _require(
            cause_idx > path.opened_at,
            "selection closure cause must occur after closed path opens",
        )
        _require_path_matches(
            path,
            selection_ref=record.selection_ref,
            branch_ref=record.branch_ref,
            branch_scope_ref=record.branch_scope_ref,
            context="runtime-operational selection closure",
        )
        _require_path_matches(
            closed_by_path,
            selection_ref=record.selection_ref,
            branch_ref=record.branch_ref,
            branch_scope_ref=record.branch_scope_ref,
            context="runtime-operational selection closure closing path",
        )
        self.closures[(record.selection_ref, record.selection_path_ref)] += 1
        _require(
            self.closures[(record.selection_ref, record.selection_path_ref)] == 1,
            "runtime-operational selected path closed more than once",
        )

    def close_selection_path(self, record: SelectionClosed) -> None:
        path = _require_path(self.paths, record.selection_path_ref, "selection closure")
        _require_path_matches(
            path,
            selection_ref=record.selection_ref,
            branch_ref=record.branch_ref,
            branch_scope_ref=record.branch_scope_ref,
            context="selection closure",
        )
        _require_path_open(path, "selection closure")
        self.closures[(record.selection_ref, record.selection_path_ref)] += 1
        self.terminalize_path(path, "SelectionClosed", record.ref)

    def validate_capture_counts(self) -> None:
        for (selection_ref, path_ref), count in self.captures.items():
            _require(
                count == 1,
                f"selection {selection_ref!r} path {path_ref!r} has duplicate captures",
            )

    def open_paths(self) -> set[str]:
        return {path_ref for path_ref, path in self.paths.items() if path.terminal_record_type is None}

    def to_trace_state(self) -> _TraceState:
        return _TraceState(
            declarations=self.declarations,
            selections=self.selections,
            handles=self.handles,
            selected_declarations=self.selected_declarations,
            handles_by_selection=self.handles_by_selection,
            resumes=self.resumes,
            returns=self.returns,
            captures=self.captures,
            closures=self.closures,
            open_paths=self.open_paths(),
            program_ref=self.program_ref,
        )


def validate_core0_trace_prefix(trace: tuple[TraceRecord, ...] | list[TraceRecord]) -> None:
    """Validate a Core-0 trace prefix.

    Prefix validation admits open selected paths for suspended executions, but
    still rejects Core-A-only records such as abort captures and selection
    closures.
    """

    _validate_core_trace(trace, boundary="core0")


def validate_core0_trace(trace: tuple[TraceRecord, ...] | list[TraceRecord]) -> None:
    """Validate a completed Core-0 trace."""

    state = _validate_core_trace(trace, boundary="core0")
    _validate_completed_state(state)


def validate_core_a_trace_prefix(trace: tuple[TraceRecord, ...] | list[TraceRecord]) -> None:
    """Validate a Core-A trace prefix.

    Core-A is the current executable core: Core-0 plus answer-position abort and
    selection closure accounting.
    """

    _validate_core_trace(trace, boundary="core_a")


def validate_core_a_trace(trace: tuple[TraceRecord, ...] | list[TraceRecord]) -> None:
    """Validate a completed Core-A trace.

    Unlike `validate_core_trace_prefix`, this requires every callable resume to
    be completed by either `ResumeReturn` or terminal `SelectionClosed`.
    """

    state = _validate_core_trace(trace, boundary="core_a")
    _validate_completed_state(state)


def validate_core_trace_prefix(trace: tuple[TraceRecord, ...] | list[TraceRecord]) -> None:
    """Validate a current executable-core prefix.

    Compatibility alias for :func:`validate_core_a_trace_prefix`.
    """

    validate_core_a_trace_prefix(trace)


def validate_core_trace(trace: tuple[TraceRecord, ...] | list[TraceRecord]) -> None:
    """Validate a completed current executable-core trace.

    Compatibility alias for :func:`validate_core_a_trace`.
    """

    validate_core_a_trace(trace)


def validate_runtime_trace_prefix(trace: tuple[TraceRecord, ...] | list[TraceRecord]) -> None:
    """Validate a runtime-normalized trace prefix.

    Runtime validation admits Core-A traces plus D18 same-path operational
    closure pairs for runtime failures and cancellation.
    """

    _validate_core_trace(trace, boundary="runtime")


def validate_runtime_trace(trace: tuple[TraceRecord, ...] | list[TraceRecord]) -> None:
    """Validate a completed runtime-normalized trace."""

    state = _validate_core_trace(trace, boundary="runtime")
    _validate_completed_state(state)


def validate_trace_evidence(
    bundle: TraceEvidenceBundle,
    /,
) -> None:
    """Validate runtime trace lifecycle plus continuation-object evidence."""

    trace = _trace_records_from_bundle(bundle.trace)
    if _has_publication_experimental_records(trace):
        raise TraceValidationError(_PUBLICATION_EVIDENCE_UNSUPPORTED)
    state = _validate_core_trace(trace, boundary="runtime")
    _validate_completed_state(state)
    if bundle.validation_profile == "lifecycle-only":
        _require(not bundle.continuation_root_refs, "lifecycle-only evidence must not carry continuation_root_refs")
        _require(not bundle.continuation_objects, "lifecycle-only evidence must not carry continuation_objects")
        _require(not bundle.continuation_ref_map, "lifecycle-only evidence must not carry continuation_ref_map")
        _require(
            not bundle.continuation_control_ref_map,
            "lifecycle-only evidence must not carry continuation_control_ref_map",
        )
        _require(not bundle.context_ref_map, "lifecycle-only evidence must not carry context_ref_map")
        return

    trace_refs = _continuation_refs_from_trace_records(trace)
    continuation_ref_map = _evidence_ref_map(
        bundle.continuation_ref_map,
        trace_refs,
        context="TraceEvidenceBundle.continuation_ref_map",
    )
    root_refs = set(bundle.continuation_root_refs)
    _require(
        root_refs == set(continuation_ref_map.values()),
        "TraceEvidenceBundle.continuation_root_refs must match mapped trace continuation refs",
    )
    control_ref_map = _evidence_ref_map(
        bundle.continuation_control_ref_map,
        _control_refs_from_trace_records(trace),
        context="TraceEvidenceBundle.continuation_control_ref_map",
    )
    context_ref_map = _evidence_ref_map(
        bundle.context_ref_map,
        _context_refs_from_trace_records(trace),
        context="TraceEvidenceBundle.context_ref_map",
    )

    catalog = _validated_continuation_object_catalog(bundle.continuation_objects)
    for trace_ref, evidence_ref in sorted(continuation_ref_map.items()):
        obj = catalog.get(evidence_ref)
        _require(obj is not None, f"continuation ref {trace_ref!r} maps to missing object {evidence_ref!r}")
        _require(
            isinstance(obj, ContinuationRoot),
            f"trace continuation ref {trace_ref!r} must map to ContinuationRoot",
        )

    validator = _ContinuationEvidenceValidator(
        catalog,
        continuation_ref_map=continuation_ref_map,
        continuation_control_ref_map=control_ref_map,
        context_ref_map=context_ref_map,
    )
    for ref in sorted(root_refs):
        validator.validate_root(ref)
    validator.validate_trace_control_refs(trace)
    _validate_runtime_record_root_coherence(trace, state, validator)


def _has_publication_experimental_records(trace: tuple[TraceRecord, ...]) -> bool:
    return any(isinstance(record, _PUBLICATION_EXPERIMENTAL_RECORD_TYPES) for record in trace)


def validate_publication_experimental_trace_prefix(
    trace: tuple[TraceRecord, ...] | list[TraceRecord],
) -> None:
    """Validate the currently implemented publication-control trace prefix."""

    _validate_publication_experimental_trace(trace, completed=False)


def validate_publication_experimental_trace(
    trace: tuple[TraceRecord, ...] | list[TraceRecord],
) -> None:
    """Validate a completed publication-experimental trace.

    This admits the quarantined `Forward`, terminal delay, and terminal fork
    records. Replay/admission validation for externally resumed prefixes
    remains separate from this lifecycle check.
    """

    _validate_publication_experimental_trace(trace, completed=True)


def validate_generated_trace_against_program(
    program: KernelProgramInput,
    trace: tuple[TraceRecord, ...] | list[TraceRecord],
    *,
    env: Env | None = None,
    registry: EffectRegistry | None = None,
    completed: bool = True,
    engine: TraceEvaluatorEngine = "auto",
    include_debug_evidence: bool = False,
) -> None:
    """Validate that `trace` is exactly generated by `program`.

    This is stronger than lifecycle validation, but it is intentionally an
    executable-oracle check rather than an independent semantic verifier. It
    reruns the current kernel/trace machine for the supplied static
    `KernelProgram` and requires exact record equality.

    The scope is intentionally narrow. It validates the initial deterministic
    run represented by `run_trace(...)`; traces extended by later external
    continuation applications need a replay API that supplies those external
    resume values.
    """

    prepared = ensure_prepared_kernel_program(program)
    normalized_trace = tuple(trace)
    generated_result = run_trace(
        prepared,
        env=env,
        registry=registry,
        engine=engine,
        include_debug_evidence=include_debug_evidence,
    )
    if completed and not _outcome_complete_for_profile(
        generated_result.outcome,
        profile=prepared.program.profile,
    ):
        raise TraceValidationError("program-generated execution did not complete")

    if prepared.program.profile == PUBLICATION_EXPERIMENTAL:
        if completed:
            validate_publication_experimental_trace(normalized_trace)
        else:
            validate_publication_experimental_trace_prefix(normalized_trace)
    elif completed:
        validate_core_a_trace(normalized_trace)
    else:
        validate_core_a_trace_prefix(normalized_trace)

    generated = generated_result.trace
    if normalized_trace != generated:
        raise TraceValidationError("trace does not match program-generated execution")


def _validate_completed_state(state: _TraceState) -> None:
    unselected = sorted(set(state.declarations) - state.selected_declarations)
    _require(
        not unselected,
        f"completed trace has unselected declarations: {unselected!r}",
    )
    for selection_ref in state.selections:
        _require(
            state.handles_by_selection[selection_ref] == 1,
            f"completed selection {selection_ref!r} must have exactly one ResumptionHandle",
        )
    for resume in state.resumes.values():
        key = (resume.ref, resume.selection_path_ref)
        _require(
            state.returns[key] == 1 or state.closures[(resume.selection_ref, resume.selection_path_ref)] == 1,
            f"completed callable resume {resume.ref!r} must have exactly one ResumeReturn "
            "or a terminal SelectionClosed",
        )
    _require(
        not state.open_paths,
        f"completed trace has open selected paths: {sorted(state.open_paths)!r}",
    )


def _trace_records_from_bundle(trace: Any) -> tuple[TraceRecord, ...]:
    if hasattr(trace, "kernel"):
        trace = trace.kernel
    records: list[TraceRecord] = []
    for idx, record in enumerate(trace):
        if not isinstance(record, _TRACE_RECORD_TYPES):
            raise TraceValidationError(f"TraceEvidenceBundle.trace[{idx}] is not a TraceRecord")
        records.append(record)
    return tuple(records)


def _validated_continuation_object_catalog(
    objects: Mapping[Ref, ContinuationObject],
) -> dict[Ref, ContinuationObject]:
    catalog: dict[Ref, ContinuationObject] = {}
    for ref, obj in objects.items():
        _require(isinstance(ref, str), f"continuation object map key must be a ref string, got {ref!r}")
        try:
            actual_ref = continuation_object_ref(obj)
        except (TypeError, ValueError) as exc:
            raise TraceValidationError(f"malformed continuation object at {ref!r}") from exc
        _require(
            ref == actual_ref,
            f"continuation object map key {ref!r} does not match content ref {actual_ref!r}",
        )
        _require(ref not in catalog, f"duplicate continuation object ref: {ref!r}")
        catalog[ref] = obj
    return catalog


def _continuation_refs_from_trace_records(records: tuple[TraceRecord, ...]) -> set[Ref]:
    refs: set[Ref] = set()
    for record in records:
        for field_name in _CONTINUATION_REF_FIELDS_BY_RECORD_TYPE.get(type(record), ()):
            value = getattr(record, field_name)
            if isinstance(value, str):
                refs.add(value)
    return refs


def _control_refs_from_trace_records(records: tuple[TraceRecord, ...]) -> set[Ref]:
    refs: set[Ref] = set()
    for record in records:
        if isinstance(record, HandlerSelection):
            refs.add(record.captured_continuation_control_ref)
            refs.add(record.outer_continuation_control_ref)
    return refs


def _context_refs_from_trace_records(records: tuple[TraceRecord, ...]) -> set[Ref]:
    refs: set[Ref] = set()
    for record in records:
        for field_name in (
            "execution_context_ref",
            "worker_context_ref",
            "handler_context_ref",
            "outer_context_ref",
        ):
            value = getattr(record, field_name, None)
            if isinstance(value, str):
                refs.add(value)
    return refs


def _evidence_ref_map(mapping: Mapping[Ref, Ref], trace_refs: set[Ref], *, context: str) -> dict[Ref, Ref]:
    if not mapping:
        return {ref: ref for ref in trace_refs}
    normalized = dict(mapping)
    for trace_ref, evidence_ref in normalized.items():
        _require(isinstance(trace_ref, str), f"{context} key must be a ref string")
        _require(isinstance(evidence_ref, str), f"{context} value must be a ref string")
    _require(
        set(normalized) == trace_refs,
        f"{context} keys must match trace refs",
    )
    return normalized


def _validate_runtime_record_root_coherence(
    records: tuple[TraceRecord, ...],
    state: _TraceState,
    validator: _ContinuationEvidenceValidator,
) -> None:
    for record in records:
        if isinstance(record, EffectDeclaration):
            validator.validate_root_matches(
                _ContinuationRootExpectation(
                    ref=record.full_continuation_ref,
                    continuation_kind="full",
                    program_ref=_required_ref(record.program_ref, "EffectDeclaration.program_ref"),
                    branch_ref=record.branch_ref,
                    branch_scope_ref=record.branch_scope_ref,
                    execution_context_ref=_required_ref(
                        record.execution_context_ref,
                        "EffectDeclaration.execution_context_ref",
                    ),
                    result_schema_ref=record.operation_result_schema_ref,
                    context=f"EffectDeclaration {record.ref!r} full_continuation_ref",
                )
            )
            continue

        if isinstance(record, HandlerSelection):
            declaration = state.declarations[record.declaration_ref]
            validator.validate_root_matches(
                _ContinuationRootExpectation(
                    ref=record.captured_continuation_ref,
                    continuation_kind="captured-worker",
                    program_ref=_required_ref(declaration.program_ref, "EffectDeclaration.program_ref"),
                    branch_ref=declaration.branch_ref,
                    branch_scope_ref=record.branch_scope_ref,
                    execution_context_ref=_required_ref(
                        record.worker_context_ref,
                        "HandlerSelection.worker_context_ref",
                    ),
                    result_schema_ref=declaration.operation_result_schema_ref,
                    context=f"HandlerSelection {record.ref!r} captured_continuation_ref",
                )
            )
            validator.validate_root_matches(
                _ContinuationRootExpectation(
                    ref=record.outer_continuation_ref,
                    continuation_kind="outer",
                    program_ref=_required_ref(declaration.program_ref, "EffectDeclaration.program_ref"),
                    branch_ref=declaration.branch_ref,
                    branch_scope_ref=record.branch_scope_ref,
                    execution_context_ref=_required_ref(
                        record.outer_context_ref,
                        "HandlerSelection.outer_context_ref",
                    ),
                    result_schema_ref=record.handled_result_schema_ref,
                    context=f"HandlerSelection {record.ref!r} outer_continuation_ref",
                )
            )
            continue

        if isinstance(record, ResumptionHandle):
            declaration = state.declarations[record.declaration_ref]
            selection = state.selections[record.selection_ref]
            validator.validate_root_matches(
                _ContinuationRootExpectation(
                    ref=record.continuation_ref,
                    continuation_kind="captured-worker",
                    program_ref=_required_ref(declaration.program_ref, "EffectDeclaration.program_ref"),
                    branch_ref=declaration.branch_ref,
                    branch_scope_ref=record.branch_scope_ref,
                    execution_context_ref=_required_ref(
                        selection.worker_context_ref,
                        "HandlerSelection.worker_context_ref",
                    ),
                    result_schema_ref=record.operation_result_schema_ref,
                    context=f"ResumptionHandle {record.ref!r} continuation_ref",
                )
            )
            continue

        if isinstance(record, ContinuationResume):
            declaration = state.declarations[record.declaration_ref]
            selection = state.selections[record.selection_ref]
            validator.validate_root_matches(
                _ContinuationRootExpectation(
                    ref=record.continuation_ref,
                    continuation_kind="captured-worker",
                    program_ref=_required_ref(declaration.program_ref, "EffectDeclaration.program_ref"),
                    branch_ref=record.branch_ref,
                    branch_scope_ref=record.branch_scope_ref,
                    execution_context_ref=_required_ref(
                        record.worker_context_ref,
                        "ContinuationResume.worker_context_ref",
                    ),
                    result_schema_ref=declaration.operation_result_schema_ref,
                    context=f"ContinuationResume {record.ref!r} continuation_ref",
                )
            )
            if record.returns_to_handler:
                handler_context_ref = _required_ref(
                    record.handler_context_ref,
                    "ContinuationResume.handler_context_ref",
                )
                validator.validate_root_matches(
                    _ContinuationRootExpectation(
                        ref=record.handler_continuation_ref,
                        continuation_kind="handler-continuation",
                        program_ref=_required_ref(declaration.program_ref, "EffectDeclaration.program_ref"),
                        branch_ref=record.branch_ref,
                        branch_scope_ref=record.branch_scope_ref,
                        execution_context_ref=handler_context_ref,
                        result_schema_ref=declaration.operation_result_schema_ref,
                        context=f"ContinuationResume {record.ref!r} handler_continuation_ref",
                    )
                )
                validator.validate_root_matches(
                    _ContinuationRootExpectation(
                        ref=record.handler_dynamic_tail_ref,
                        continuation_kind="handler-dynamic-tail",
                        program_ref=_required_ref(declaration.program_ref, "EffectDeclaration.program_ref"),
                        branch_ref=record.branch_ref,
                        branch_scope_ref=record.branch_scope_ref,
                        execution_context_ref=_required_ref(
                            selection.outer_context_ref,
                            "HandlerSelection.outer_context_ref",
                        ),
                        result_schema_ref=selection.handled_result_schema_ref,
                        context=f"ContinuationResume {record.ref!r} handler_dynamic_tail_ref",
                    )
                )
            else:
                outer_context_ref = _required_ref(
                    selection.outer_context_ref,
                    "HandlerSelection.outer_context_ref",
                )
                for field_name in ("handler_continuation_ref", "handler_dynamic_tail_ref"):
                    validator.validate_root_matches(
                        _ContinuationRootExpectation(
                            ref=getattr(record, field_name),
                            continuation_kind="empty-terminal",
                            program_ref=_required_ref(declaration.program_ref, "EffectDeclaration.program_ref"),
                            branch_ref=record.branch_ref,
                            branch_scope_ref=record.branch_scope_ref,
                            execution_context_ref=outer_context_ref,
                            result_schema_ref=None,
                            context=f"ContinuationResume {record.ref!r} {field_name}",
                        )
                    )
            continue

        if isinstance(record, ResumeReturn):
            resume = state.resumes[record.resume_ref]
            declaration = state.declarations[resume.declaration_ref]
            selection = state.selections[record.selection_ref]
            validator.validate_root_matches(
                _ContinuationRootExpectation(
                    ref=record.handler_continuation_ref,
                    continuation_kind="handler-continuation",
                    program_ref=_required_ref(declaration.program_ref, "EffectDeclaration.program_ref"),
                    branch_ref=record.branch_ref,
                    branch_scope_ref=record.branch_scope_ref,
                    execution_context_ref=_required_ref(
                        record.handler_context_ref,
                        "ResumeReturn.handler_context_ref",
                    ),
                    result_schema_ref=declaration.operation_result_schema_ref,
                    context=f"ResumeReturn {record.ref!r} handler_continuation_ref",
                )
            )
            validator.validate_root_matches(
                _ContinuationRootExpectation(
                    ref=record.handler_dynamic_tail_ref,
                    continuation_kind="handler-dynamic-tail",
                    program_ref=_required_ref(declaration.program_ref, "EffectDeclaration.program_ref"),
                    branch_ref=record.branch_ref,
                    branch_scope_ref=record.branch_scope_ref,
                    execution_context_ref=_required_ref(
                        selection.outer_context_ref,
                        "HandlerSelection.outer_context_ref",
                    ),
                    result_schema_ref=selection.handled_result_schema_ref,
                    context=f"ResumeReturn {record.ref!r} handler_dynamic_tail_ref",
                )
            )
            continue


def _required_ref(value: Ref | None, field_name: str) -> Ref:
    if value is None:
        raise TraceValidationError(f"{field_name} is required")
    if not isinstance(value, str):
        raise TraceValidationError(f"{field_name} must be a ref string")
    return value


def _binding_env_ref(context: Mapping[str, Any]) -> Ref:
    value = context["binding_env_ref"]
    if not isinstance(value, str):
        raise TraceValidationError("execution context binding_env_ref must be a ref string")
    return value


class _ContinuationEvidenceValidator:
    def __init__(
        self,
        catalog: Mapping[Ref, ContinuationObject],
        *,
        continuation_ref_map: Mapping[Ref, Ref] | None = None,
        continuation_control_ref_map: Mapping[Ref, Ref] | None = None,
        context_ref_map: Mapping[Ref, Ref] | None = None,
    ) -> None:
        self.catalog = catalog
        self.continuation_ref_map = dict(continuation_ref_map or {})
        self.continuation_control_ref_map = dict(continuation_control_ref_map or {})
        self.context_ref_map = dict(context_ref_map or {})
        self._validated_structure: set[Ref] = set()
        self._visiting: set[Ref] = set()
        self._validated_roots: dict[Ref, ContinuationRoot] = {}
        self._validated_frame_controls: set[tuple[Ref, Ref, Ref, Ref | None]] = set()
        self._validated_frame_control_stacks: set[tuple[Ref, Ref, Ref, Ref | None]] = set()
        self._validated_frame_control_nodes: set[tuple[Ref, Ref, Ref, Ref | None]] = set()

    def validate_root(self, ref: Ref) -> ContinuationRoot:
        cached = self._validated_roots.get(ref)
        if cached is not None:
            return cached
        obj = self._require_root_object(ref)
        self._validate_root_payload_refs(ref, obj)
        self._validate_structure_iterative(obj.stack_ref, expected_role="stack")
        self._validate_structure_iterative(_binding_env_ref(obj.execution_context), expected_role="env")
        self._validate_frame_controls_for_root(obj.stack_ref, root=obj)
        self._validated_roots[ref] = obj
        return obj

    def validate_root_matches(self, expected: _ContinuationRootExpectation) -> None:
        evidence_ref = self._evidence_continuation_ref(expected.ref)
        root = self.validate_root(evidence_ref)
        evidence_context_ref = self._evidence_context_ref(expected.execution_context_ref)
        checks = (
            ("program_ref", root.program_ref, expected.program_ref),
            ("branch_ref", root.branch_ref, expected.branch_ref),
            ("branch_scope_ref", root.branch_scope_ref, expected.branch_scope_ref),
            ("continuation_kind", root.continuation_kind, expected.continuation_kind),
            ("execution_context_ref", root.execution_context_ref, evidence_context_ref),
            ("result_schema_ref", root.result_schema_ref, expected.result_schema_ref),
        )
        for field_name, actual, wanted in checks:
            _require(
                actual == wanted,
                f"{expected.context} root {field_name} mismatch: expected {wanted!r}, got {actual!r}",
            )

    def validate_trace_control_refs(self, records: tuple[TraceRecord, ...]) -> None:
        for record in records:
            if isinstance(record, HandlerSelection):
                self._validate_control_ref(
                    record.captured_continuation_ref,
                    record.captured_continuation_control_ref,
                    context=f"HandlerSelection {record.ref!r} captured_continuation_control_ref",
                )
                self._validate_control_ref(
                    record.outer_continuation_ref,
                    record.outer_continuation_control_ref,
                    context=f"HandlerSelection {record.ref!r} outer_continuation_control_ref",
                )

    def _validate_control_ref(self, root_ref: Ref, actual_ref: Ref, *, context: str) -> None:
        root = self.validate_root(self._evidence_continuation_ref(root_ref))
        expected_ref = continuation_control_identity_ref(
            ContinuationControlIdentity(
                program_ref=root.program_ref,
                branch_ref=root.branch_ref,
                branch_scope_ref=root.branch_scope_ref,
                position="value",
                stack_ref=root.stack_ref,
            )
        )
        _require(
            self._evidence_control_ref(actual_ref) == expected_ref,
            f"{context} does not match cited continuation stack",
        )

    def _evidence_continuation_ref(self, ref: Ref) -> Ref:
        return self.continuation_ref_map.get(ref, ref)

    def _evidence_control_ref(self, ref: Ref) -> Ref:
        return self.continuation_control_ref_map.get(ref, ref)

    def _evidence_context_ref(self, ref: Ref) -> Ref:
        return self.context_ref_map.get(ref, ref)

    def _validate_structure_iterative(
        self,
        start_ref: Ref,
        *,
        expected_role: _ContinuationRole,
    ) -> None:
        work: list[tuple[Ref, _ContinuationRole, bool]] = [(start_ref, expected_role, False)]
        while work:
            ref, role, ready = work.pop()
            self._require_role_for_ref(ref, role)
            if ready:
                if role == "stack":
                    self._validate_stack_summary(
                        ref,
                        self._require_stack_object(
                            ref,
                            f"continuation child {ref!r} must resolve to a stack object",
                        ),
                    )
                elif role == "frame":
                    self._validate_frame_summary(
                        ref,
                        self._require_frame_object(
                            ref,
                            f"continuation child {ref!r} must resolve to a frame object",
                        ),
                    )
                else:
                    self._validate_env_object(
                        ref,
                        self._require_env_object(
                            ref,
                            f"continuation child {ref!r} must resolve to an env object",
                        ),
                    )
                self._leave(ref)
                continue

            if ref in self._validated_structure:
                continue
            self._enter(ref)
            work.append((ref, role, True))
            for child_ref, child_role in self._child_roles(ref, role):
                work.append((child_ref, child_role, False))

    def _validate_frame_controls_for_root(self, start_ref: Ref, *, root: ContinuationRoot) -> None:
        key = (start_ref, root.program_ref, root.branch_ref, root.branch_scope_ref)
        if key in self._validated_frame_control_stacks:
            return
        self._validated_frame_control_stacks.add(key)
        work = [start_ref]
        seen: set[Ref] = set()
        while work:
            ref = work.pop()
            if ref in seen:
                continue
            seen.add(ref)
            node_key = (ref, root.program_ref, root.branch_ref, root.branch_scope_ref)
            if node_key in self._validated_frame_control_nodes:
                continue
            obj = self._require_object(ref)
            self._validated_frame_control_nodes.add(node_key)
            if isinstance(obj, ContinuationStackNode):
                work.append(obj.head_frame_ref)
                work.append(obj.tail_stack_ref)
            elif isinstance(obj, ContinuationStackConcat):
                work.append(obj.prefix_stack_ref)
                work.append(obj.tail_stack_ref)
            elif isinstance(obj, ContinuationFrameNode):
                self._validate_frame_controls(ref, obj.payload, root=root)
                for child_ref, _child_role in continuation_frame_payload_child_roles(obj.payload):
                    work.append(child_ref)
            elif isinstance(obj, ContinuationEmptyStack | ContinuationEnvEmpty | ContinuationEnvNode):
                continue
            else:
                raise TraceValidationError(f"continuation child {ref!r} cannot be a root")

    def _child_roles(
        self,
        ref: Ref,
        role: _ContinuationRole,
    ) -> tuple[tuple[Ref, _ContinuationRole], ...]:
        if role == "stack":
            stack_obj = self._require_stack_object(ref, f"continuation child {ref!r} has wrong object role")
            if isinstance(stack_obj, ContinuationEmptyStack):
                return ()
            if isinstance(stack_obj, ContinuationStackNode):
                return ((stack_obj.head_frame_ref, "frame"), (stack_obj.tail_stack_ref, "stack"))
            if isinstance(stack_obj, ContinuationStackConcat):
                return ((stack_obj.prefix_stack_ref, "stack"), (stack_obj.tail_stack_ref, "stack"))
        if role == "frame":
            frame_obj = self._require_frame_object(ref, f"continuation child {ref!r} has wrong object role")
            roles = continuation_frame_payload_child_roles(frame_obj.payload)
            for child_ref, child_role in roles:
                child = self._require_object(child_ref)
                self._require_role(child_ref, child, child_role)
            return roles
        if role == "env":
            env_obj = self._require_env_object(ref, f"continuation child {ref!r} has wrong object role")
            if isinstance(env_obj, ContinuationEnvEmpty):
                return ()
            if isinstance(env_obj, ContinuationEnvNode):
                return ((env_obj.parent_env_ref, "env"),)
        raise TraceValidationError(f"continuation child {ref!r} has wrong object role")

    def _require_role_for_ref(self, ref: Ref, role: _ContinuationRole) -> None:
        if role == "stack":
            self._require_stack_object(ref, f"continuation child {ref!r} must resolve to a stack object")
        elif role == "frame":
            self._require_frame_object(ref, f"continuation child {ref!r} must resolve to a frame object")
        else:
            self._require_env_object(ref, f"continuation child {ref!r} must resolve to an env object")

    def _require_role(
        self,
        ref: Ref,
        obj: ContinuationObject,
        role: _ContinuationRole,
    ) -> None:
        if role == "stack":
            _require(
                isinstance(obj, ContinuationEmptyStack | ContinuationStackNode | ContinuationStackConcat),
                f"continuation child {ref!r} must resolve to a stack object",
            )
        elif role == "frame":
            _require(
                isinstance(obj, ContinuationFrameNode),
                f"continuation child {ref!r} must resolve to a frame object",
            )
        else:
            _require(
                isinstance(obj, ContinuationEnvEmpty | ContinuationEnvNode),
                f"continuation child {ref!r} must resolve to an env object",
            )

    def _require_root_object(self, ref: Ref) -> ContinuationRoot:
        obj = self._require_object(ref)
        if not isinstance(obj, ContinuationRoot):
            raise TraceValidationError(f"continuation root {ref!r} must resolve to ContinuationRoot")
        return obj

    def _require_stack_object(self, ref: Ref, message: str) -> _ContinuationStackObject:
        obj = self._require_object(ref)
        if not isinstance(obj, ContinuationEmptyStack | ContinuationStackNode | ContinuationStackConcat):
            raise TraceValidationError(message)
        return obj

    def _require_frame_object(self, ref: Ref, message: str) -> ContinuationFrameNode:
        obj = self._require_object(ref)
        if not isinstance(obj, ContinuationFrameNode):
            raise TraceValidationError(message)
        return obj

    def _require_env_object(self, ref: Ref, message: str) -> _ContinuationEnvObject:
        obj = self._require_object(ref)
        if not isinstance(obj, ContinuationEnvEmpty | ContinuationEnvNode):
            raise TraceValidationError(message)
        return obj

    def _validate_stack_summary(self, ref: Ref, obj: _ContinuationStackObject) -> ContinuationStackSummary:
        if isinstance(obj, ContinuationEmptyStack):
            _require(obj.summary == ContinuationStackSummary(), f"empty stack {ref!r} has non-empty summary")
            return obj.summary
        if isinstance(obj, ContinuationStackConcat):
            prefix = self._require_stack_object(
                obj.prefix_stack_ref,
                f"stack concat {ref!r} prefix stack {obj.prefix_stack_ref!r} has wrong object role",
            )
            tail = self._require_stack_object(
                obj.tail_stack_ref,
                f"stack concat {ref!r} tail stack {obj.tail_stack_ref!r} has wrong object role",
            )
            expected = ContinuationStackSummary(
                depth=prefix.summary.depth + tail.summary.depth,
            )
            _require(obj.summary == expected, f"stack concat {ref!r} summary does not match prefix/tail summaries")
            return obj.summary
        self._require_frame_object(
            obj.head_frame_ref,
            f"stack node {ref!r} head frame {obj.head_frame_ref!r} has wrong object role",
        )
        tail = self._require_stack_object(
            obj.tail_stack_ref,
            f"stack node {ref!r} tail stack {obj.tail_stack_ref!r} has wrong object role",
        )
        expected = ContinuationStackSummary(
            depth=tail.summary.depth + 1,
        )
        _require(obj.summary == expected, f"stack node {ref!r} summary does not match head/tail summaries")
        return obj.summary

    def _validate_frame_summary(self, ref: Ref, obj: ContinuationFrameNode) -> ContinuationFrameSummary:
        _require(
            obj.frame_kind == obj.payload.frame_kind,
            f"frame node {ref!r} frame_kind disagrees with payload",
        )
        self._validate_frame_payload_refs(ref, obj.payload)
        expected = self._frame_summary(obj.payload)
        _require(obj.summary == expected, f"frame node {ref!r} summary does not match payload children")
        return obj.summary

    def _validate_env_object(self, ref: Ref, obj: _ContinuationEnvObject) -> None:
        if isinstance(obj, ContinuationEnvEmpty):
            _require(obj.depth == 0, f"env empty {ref!r} depth must be 0")
            return
        parent = self._require_env_object(
            obj.parent_env_ref,
            f"env node {ref!r} parent {obj.parent_env_ref!r} has wrong object role",
        )
        _require(obj.depth > 0, f"env node {ref!r} depth must be positive")
        _require(
            obj.depth == parent.depth + 1,
            f"env node {ref!r} depth does not match parent depth",
        )

    def _validate_frame_controls(
        self,
        ref: Ref,
        payload: ContinuationFramePayload,
        *,
        root: ContinuationRoot,
    ) -> None:
        key = (ref, root.program_ref, root.branch_ref, root.branch_scope_ref)
        if key in self._validated_frame_controls:
            return
        self._validated_frame_controls.add(key)
        if isinstance(payload, HandlerReturnFramePayload):
            self._require_control_identity_for_stack(
                payload.captured_stack_ref,
                payload.captured_continuation_control_ref,
                root=root,
                context=f"handler-return frame {ref!r} captured_continuation_control_ref",
            )
            self._require_control_identity_for_stack(
                payload.outer_stack_ref,
                payload.outer_continuation_control_ref,
                root=root,
                context=f"handler-return frame {ref!r} outer_continuation_control_ref",
            )

    def _require_control_identity_for_stack(
        self,
        stack_ref: Ref,
        actual_ref: Ref,
        *,
        root: ContinuationRoot,
        context: str,
    ) -> None:
        expected_ref = continuation_control_identity_ref(
            ContinuationControlIdentity(
                program_ref=root.program_ref,
                branch_ref=root.branch_ref,
                branch_scope_ref=root.branch_scope_ref,
                position="value",
                stack_ref=stack_ref,
            )
        )
        _require(actual_ref == expected_ref, f"{context} does not match cited stack")

    def _validate_root_payload_refs(self, ref: Ref, root: ContinuationRoot) -> None:
        self._require_content_ref(
            "ctx",
            root.execution_context,
            root.execution_context_ref,
            context=f"continuation root {ref!r} execution_context_ref",
        )

    def _validate_frame_payload_refs(self, ref: Ref, payload: ContinuationFramePayload) -> None:
        if isinstance(payload, BindFramePayload):
            self._require_env_ref(payload.env_ref, context=f"bind frame {ref!r} env_ref")
            self._require_env_ref(
                _binding_env_ref(payload.context), context=f"bind frame {ref!r} context binding_env_ref"
            )
            self._require_content_ref(
                "ctx",
                payload.context,
                payload.context_ref,
                context=f"bind frame {ref!r} context_ref",
            )
        elif isinstance(payload, HandlerFramePayload):
            self._require_env_ref(payload.env_ref, context=f"handler frame {ref!r} env_ref")
            self._require_env_ref(
                _binding_env_ref(payload.entry_context),
                context=f"handler frame {ref!r} entry_context binding_env_ref",
            )
            self._require_env_ref(
                _binding_env_ref(payload.outer_context),
                context=f"handler frame {ref!r} outer_context binding_env_ref",
            )
            self._require_content_ref(
                "ctx",
                payload.entry_context,
                payload.entry_context_ref,
                context=f"handler frame {ref!r} entry_context_ref",
            )
            self._require_content_ref(
                "ctx",
                payload.outer_context,
                payload.outer_context_ref,
                context=f"handler frame {ref!r} outer_context_ref",
            )

    def _require_content_ref(self, kind: str, payload: Any, actual_ref: Ref, *, context: str) -> None:
        try:
            expected_ref = content_ref(kind, payload)
        except TypeError as exc:
            raise TraceValidationError(f"{context} payload is not content-addressable") from exc
        _require(
            actual_ref == expected_ref,
            f"{context} does not match content ref {expected_ref!r}",
        )

    def _require_env_ref(self, ref: Ref, *, context: str) -> None:
        self._require_env_object(ref, f"{context} must resolve to an env object")

    def _frame_summary(self, payload: ContinuationFramePayload) -> ContinuationFrameSummary:
        required_schema_refs: set[Ref] = set()
        code_identity_refs: set[Ref] = set()
        if isinstance(payload, BindFramePayload):
            code_identity_refs.add(payload.binder_ref)
        elif isinstance(payload, HandlerFramePayload):
            code_identity_refs.add(payload.handler_env_def_ref)
        elif isinstance(payload, HandlerReturnFramePayload):
            code_identity_refs.add(payload.install_def_ref)
            if payload.operation_result_schema_ref is not None:
                required_schema_refs.add(payload.operation_result_schema_ref)
            required_schema_refs.add(payload.handled_result_schema_ref)
        elif isinstance(payload, ResumeReturnFramePayload | TerminalResultFramePayload):
            pass
        else:
            raise TraceValidationError(f"unknown continuation frame payload: {payload!r}")

        for child_ref, _child_role in continuation_frame_payload_child_roles(payload):
            child = self._require_object(child_ref)
            if isinstance(
                child,
                ContinuationEmptyStack
                | ContinuationStackNode
                | ContinuationStackConcat
                | ContinuationFrameNode
                | ContinuationEnvEmpty
                | ContinuationEnvNode,
            ):
                continue
            raise TraceValidationError(f"frame payload child {child_ref!r} cannot be a root")
        return ContinuationFrameSummary(
            required_schema_refs=tuple(required_schema_refs),
            code_identity_refs=tuple(code_identity_refs),
        )

    def _require_object(self, ref: Ref) -> ContinuationObject:
        obj = self.catalog.get(ref)
        if obj is None:
            raise TraceValidationError(f"continuation object ref {ref!r} is missing")
        return obj

    def _enter(self, ref: Ref) -> None:
        _require(ref not in self._visiting, f"continuation object graph contains a cycle at {ref!r}")
        self._visiting.add(ref)

    def _leave(self, ref: Ref) -> None:
        self._visiting.remove(ref)
        self._validated_structure.add(ref)


def _validate_core_trace(
    trace: tuple[TraceRecord, ...] | list[TraceRecord],
    *,
    boundary: str,
) -> _TraceState:
    ledger = _LifecycleLedger()

    for idx, record in enumerate(trace):
        ledger.reject_record_ref(record)
        _require(
            getattr(record, "branch_scope_ref", None) is None,
            "single-root core admits only root branch scope",
        )

        if isinstance(record, EffectDeclaration):
            _require_root_branch(record.branch_ref)
            ledger.declare_effect(record)
            continue

        if isinstance(record, HandlerSelection):
            ledger.select_handler(record, idx)
            continue

        if isinstance(record, ResumptionHandle):
            ledger.open_resumption_handle_path(
                record,
                branch_ref="branch:root",
                branch_scope_ref=None,
                idx=idx,
            )
            continue

        if isinstance(record, ContinuationResume):
            _require_root_branch(record.branch_ref)
            ledger.resume_callable_source(
                record,
                one_shot_message=("one-shot core resumption handle resumed more than once on selected path"),
            )
            continue

        if isinstance(record, ResumeReturn):
            _require_root_branch(record.branch_ref)
            ledger.record_resume_return(record)
            continue

        if isinstance(record, EffectCapture):
            _require_root_branch(record.branch_ref)
            ledger.record_effect_capture(record, boundary=boundary, idx=idx)
            continue

        if isinstance(record, SelectionClosed):
            _require_root_branch(record.branch_ref)
            _require(
                boundary != "core0",
                "Core-0 does not admit SelectionClosed records",
            )
            ledger.record_selection_closed_by_capture(record, boundary=boundary)
            continue

        raise TraceValidationError(f"unknown trace record: {record!r}")

    ledger.validate_capture_counts()
    return ledger.to_trace_state()


def _validate_publication_experimental_trace(
    trace: tuple[TraceRecord, ...] | list[TraceRecord],
    *,
    completed: bool,
) -> None:
    ledger = _LifecycleLedger()
    declarations = ledger.declarations
    selections = ledger.selections
    paths = ledger.paths
    pending_sources: dict[str, ContinuationPending] = {}
    fork_summaries: dict[str, ForkSummary] = {}
    fork_branches: dict[str, ForkBranch] = {}
    forwards_by_ref: dict[str, HandlerForward] = {}
    forward_paths: Counter[str] = Counter()
    closed_forward_paths: Counter[str] = Counter()
    pending_delays: Counter[str] = Counter()
    fork_branch_materializations: Counter[tuple[str, str]] = Counter()
    callable_resumes: set[str] = set()
    returned_resumes: set[str] = set()
    terminal_resume_sources: set[str] = set()
    terminal_resume_results: set[str] = set()
    latest_selection_by_declaration: dict[str, HandlerSelection] = {}
    forward_closed_selections: set[str] = set()
    branch_stack: list[_BranchScopeState] = []
    open_branch_scopes: dict[str, _BranchScopeState] = {}

    def require_branch_context(
        branch_ref: str,
        branch_scope_ref: str | None,
        context: str,
    ) -> None:
        if branch_ref == "branch:root":
            _require(branch_scope_ref is None, f"{context} root branch has branch scope")
            _require(not branch_stack, f"{context} root branch inside active branch scope")
            return
        if not branch_stack:
            raise TraceValidationError(f"{context} branch {branch_ref!r} has no active branch scope")
        current = branch_stack[-1]
        _require(
            current.branch_ref == branch_ref,
            f"{context} branch {branch_ref!r} does not match active branch scope",
        )
        _require(
            branch_scope_ref == current.scope_ref,
            f"{context} branch scope mismatch",
        )

    def open_branch_scope(resume: ContinuationResume) -> None:
        scope_ref = resume.branch_scope_ref
        if scope_ref is None or scope_ref != resume.ref:
            raise TraceValidationError("fork branch resume scope must be its resume ref")
        _require(scope_ref not in open_branch_scopes, "branch scope is already open")
        state = _BranchScopeState(
            scope_ref=scope_ref,
            branch_ref=resume.branch_ref,
            resume_ref=resume.ref,
            source_ref=resume.source_ref,
        )
        open_branch_scopes[scope_ref] = state
        branch_stack.append(state)

    def close_branch_scope(record: TerminalResumeResult) -> None:
        branch_scope_ref = record.branch_scope_ref
        if branch_scope_ref is None:
            raise TraceValidationError("terminal result missing branch scope")
        if not branch_stack:
            raise TraceValidationError("terminal result has no active branch scope")
        current = branch_stack[-1]
        _require(
            current.scope_ref == branch_scope_ref
            and current.resume_ref == record.resume_ref
            and current.branch_ref == record.branch_ref
            and current.source_ref == record.source_ref,
            "terminal result branch scope mismatch",
        )
        open_paths = sorted(ledger.open_paths_for_branch_scope(current.scope_ref))
        _require(
            not open_paths,
            f"terminal result closes branch scope with open selected paths: {open_paths!r}",
        )
        open_callable_resumes = sorted(ledger.open_callable_resume_refs_for_branch_scope(current.scope_ref))
        _require(
            not open_callable_resumes,
            f"terminal result closes branch scope with open callable resumes: {open_callable_resumes!r}",
        )
        branch_stack.pop()
        del open_branch_scopes[current.scope_ref]

    for idx, record in enumerate(trace):
        ledger.reject_record_ref(record)

        if isinstance(record, EffectDeclaration):
            require_branch_context(
                record.branch_ref,
                record.branch_scope_ref,
                "declaration",
            )
            ledger.declare_effect(record)
            continue

        if isinstance(record, HandlerSelection):
            declaration = _require_lookup(declarations, record.declaration_ref, "selection cites missing declaration")
            require_branch_context(
                declaration.branch_ref,
                record.branch_scope_ref,
                "selection",
            )
            _require(
                record.branch_scope_ref == declaration.branch_scope_ref,
                "selection branch scope mismatch",
            )
            previous_selection = latest_selection_by_declaration.get(record.declaration_ref)
            _require(
                previous_selection is None or previous_selection.ref in forward_closed_selections,
                f"declaration {record.declaration_ref!r} previous selection was not forwarded",
            )
            ledger.select_handler(
                record,
                idx,
                duplicate_key=(record.declaration_ref, record.selected_binding_ref),
            )
            latest_selection_by_declaration[record.declaration_ref] = record
            continue

        if isinstance(record, ResumptionHandle):
            declaration = _require_lookup(
                declarations,
                record.declaration_ref,
                "resumption handle cites missing declaration",
            )
            require_branch_context(
                declaration.branch_ref,
                record.branch_scope_ref,
                "resumption handle",
            )
            _require(
                record.branch_scope_ref == declaration.branch_scope_ref,
                "resumption handle branch scope mismatch",
            )
            ledger.open_resumption_handle_path(
                record,
                branch_ref=declaration.branch_ref,
                branch_scope_ref=record.branch_scope_ref,
                idx=idx,
            )
            continue

        if isinstance(record, HandlerForward):
            require_branch_context(record.branch_ref, record.branch_scope_ref, "forward")
            skipped_selection = _require_lookup(
                selections, record.skipped_selection_ref, "forward cites missing selection"
            )
            _require(
                record.declaration_ref == skipped_selection.declaration_ref,
                "forward declaration mismatch",
            )
            _require(
                record.skipped_binding_ref == skipped_selection.selected_binding_ref,
                "forward binding mismatch",
            )
            _require(
                record.skipped_selection_path_ref
                == source_path_ref(
                    record.skipped_selection_ref,
                    ledger.handle_ref_for_selection(record.skipped_selection_ref),
                    record.branch_ref,
                ),
                "forward selected path mismatch",
            )
            path = _require_path(paths, record.skipped_selection_path_ref, "forward")
            _require_path_matches(
                path,
                selection_ref=record.skipped_selection_ref,
                branch_ref=record.branch_ref,
                branch_scope_ref=record.branch_scope_ref,
                context="forward",
            )
            _require_path_open(path, "forward")
            forward_paths[record.skipped_selection_path_ref] += 1
            _require(
                forward_paths[record.skipped_selection_path_ref] == 1,
                "selected path forwarded more than once",
            )
            forwards_by_ref[record.ref] = record
            continue

        if isinstance(record, ContinuationPending):
            require_branch_context(
                record.branch_ref,
                record.branch_scope_ref,
                "pending source",
            )
            handle_ref = ledger.handle_ref_for_selection(record.selection_ref)
            selection = _require_lookup(selections, record.selection_ref, "pending source cites missing selection")
            _require(
                record.declaration_ref == selection.declaration_ref,
                "pending source declaration mismatch",
            )
            _require(
                record.selection_path_ref == source_path_ref(record.selection_ref, handle_ref, record.branch_ref),
                "pending source selected path mismatch",
            )
            _require(
                record.continuation_ref == selection.captured_continuation_ref,
                "pending source continuation mismatch",
            )
            selected_path = _require_path(
                paths,
                record.selection_path_ref,
                "pending source",
            )
            _require_path_matches(
                selected_path,
                selection_ref=record.selection_ref,
                source_ref=handle_ref,
                branch_ref=record.branch_ref,
                branch_scope_ref=record.branch_scope_ref,
                context="pending source",
            )
            ledger.terminalize_path(selected_path, "ContinuationPending", record.ref)
            pending_sources[record.ref] = record
            pending_path_ref = source_path_ref(
                record.selection_ref,
                record.ref,
                record.branch_ref,
            )
            _require(
                pending_path_ref not in paths,
                f"duplicate pending source path ref: {pending_path_ref!r}",
            )
            ledger.open_path(
                pending_path_ref,
                selection_ref=record.selection_ref,
                source_ref=record.ref,
                branch_ref=record.branch_ref,
                branch_scope_ref=record.branch_scope_ref,
                idx=idx,
                context="pending source",
            )
            continue

        if isinstance(record, ContinuationDelay):
            pending = _require_lookup(pending_sources, record.pending_ref, "delay cites missing pending source")
            _require(
                record.branch_scope_ref == pending.branch_scope_ref,
                "delay branch scope mismatch",
            )
            _require(record.reason == pending.reason, "delay reason mismatch")
            _require(
                record.pending_ref not in terminal_resume_sources,
                "delay cannot occur after pending source resume",
            )
            pending_delays[record.pending_ref] += 1
            _require(
                pending_delays[record.pending_ref] == 1,
                "pending source has duplicate delay records",
            )
            continue

        if isinstance(record, ForkSummary):
            require_branch_context(record.branch_ref, record.branch_scope_ref, "fork")
            handle_ref = ledger.handle_ref_for_selection(record.selection_ref)
            _require(record.selection_ref in selections, "fork cites missing selection")
            _require(
                len(record.branch_refs) == len(set(record.branch_refs)),
                "fork branch refs must be unique",
            )
            _require(
                record.selection_path_ref == source_path_ref(record.selection_ref, handle_ref, record.branch_ref),
                "fork selected path mismatch",
            )
            selected_path = _require_path(paths, record.selection_path_ref, "fork")
            _require_path_matches(
                selected_path,
                selection_ref=record.selection_ref,
                source_ref=handle_ref,
                branch_ref=record.branch_ref,
                branch_scope_ref=record.branch_scope_ref,
                context="fork",
            )
            ledger.terminalize_path(selected_path, "ForkSummary", record.ref)
            fork_summaries[record.ref] = record
            continue

        if isinstance(record, ForkBranch):
            fork = _require_lookup(fork_summaries, record.fork_ref, "fork branch cites missing fork summary")
            require_branch_context(fork.branch_ref, record.branch_scope_ref, "fork branch")
            _require(
                record.branch_scope_ref == fork.branch_scope_ref,
                "fork branch parent scope mismatch",
            )
            _require(
                record.branch_ref in fork.branch_refs,
                "fork branch ref is not declared by fork summary",
            )
            fork_branch_materializations[(record.fork_ref, record.branch_ref)] += 1
            _require(
                fork_branch_materializations[(record.fork_ref, record.branch_ref)] == 1,
                "fork branch materialized more than once",
            )
            _require(
                record.selection_ref == fork.selection_ref and record.declaration_ref == fork.declaration_ref,
                "fork branch selection/declaration mismatch",
            )
            selection = selections[record.selection_ref]
            _require(
                record.continuation_ref == selection.captured_continuation_ref,
                "fork branch continuation mismatch",
            )
            _require(
                record.selection_path_ref == fork.selection_path_ref,
                "fork branch selected path mismatch",
            )
            fork_branches[record.ref] = record
            branch_path_ref = source_path_ref(
                record.selection_ref,
                record.ref,
                record.branch_ref,
            )
            _require(
                branch_path_ref not in paths,
                f"duplicate fork branch source path ref: {branch_path_ref!r}",
            )
            ledger.open_path(
                branch_path_ref,
                selection_ref=record.selection_ref,
                source_ref=record.ref,
                branch_ref=record.branch_ref,
                branch_scope_ref=record.branch_scope_ref,
                idx=idx,
                context="fork branch source",
            )
            continue

        if isinstance(record, ContinuationResume):
            _require(record.source_ref not in terminal_resume_sources, "source resumed twice")
            if record.source_record_type == "ResumptionHandle":
                require_branch_context(record.branch_ref, record.branch_scope_ref, "resume")
                ledger.resume_callable_source(
                    record,
                    one_shot_message=("one-shot callable resumption handle resumed more than once"),
                )
                callable_resumes.add(record.ref)
            elif record.source_record_type == "ContinuationPending":
                require_branch_context(
                    record.branch_ref,
                    record.branch_scope_ref,
                    "pending resume",
                )
                pending = _require_lookup(pending_sources, record.source_ref, "resume cites missing pending source")
                _require(
                    pending_delays[record.source_ref] == 1,
                    "pending source resume requires ContinuationDelay",
                )
                _require(not record.returns_to_handler, "pending resume is terminal")
                _require(record.handler_context_ref is None, "terminal resume has no handler context")
                _require(
                    record.selection_ref == pending.selection_ref and record.declaration_ref == pending.declaration_ref,
                    "pending resume selection/declaration mismatch",
                )
                _require(
                    record.continuation_ref == pending.continuation_ref,
                    "pending resume continuation mismatch",
                )
                _require_present(
                    record.worker_context_ref,
                    "ContinuationResume.worker_context_ref",
                )
                _require(
                    record.worker_context_ref == pending.worker_context_ref,
                    "pending resume worker context mismatch",
                )
                _require(
                    record.selection_path_ref
                    == source_path_ref(record.selection_ref, record.source_ref, record.branch_ref),
                    "pending resume selected path mismatch",
                )
                _require(record.branch_ref == pending.branch_ref, "pending resume branch mismatch")
                _require(
                    record.branch_scope_ref == pending.branch_scope_ref,
                    "pending resume branch scope mismatch",
                )
                path = _require_path(paths, record.selection_path_ref, "pending resume")
                _require_path_matches(
                    path,
                    selection_ref=record.selection_ref,
                    source_ref=record.source_ref,
                    branch_ref=record.branch_ref,
                    branch_scope_ref=record.branch_scope_ref,
                    context="pending resume",
                )
                _require_path_open(path, "pending resume")
                terminal_resume_sources.add(record.source_ref)
            elif record.source_record_type == "ForkBranch":
                branch = _require_lookup(fork_branches, record.source_ref, "resume cites missing fork branch")
                if branch.branch_scope_ref is None:
                    _require(
                        not branch_stack,
                        "fork branch resume parent scope mismatch",
                    )
                elif not branch_stack or branch_stack[-1].scope_ref != branch.branch_scope_ref:
                    raise TraceValidationError("fork branch resume parent scope mismatch")
                _require(not record.returns_to_handler, "fork branch resume is terminal")
                _require(record.handler_context_ref is None, "terminal resume has no handler context")
                _require(
                    record.selection_ref == branch.selection_ref and record.declaration_ref == branch.declaration_ref,
                    "fork resume selection/declaration mismatch",
                )
                _require(
                    record.continuation_ref == branch.continuation_ref,
                    "fork resume continuation mismatch",
                )
                selection = selections[record.selection_ref]
                _require_present(
                    record.worker_context_ref,
                    "ContinuationResume.worker_context_ref",
                )
                _require(
                    record.worker_context_ref == selection.worker_context_ref,
                    "fork resume worker context mismatch",
                )
                _require(
                    record.selection_path_ref
                    == source_path_ref(record.selection_ref, record.source_ref, record.branch_ref),
                    "fork resume selected path mismatch",
                )
                _require(record.branch_ref == branch.branch_ref, "fork resume branch mismatch")
                path = _require_path(paths, record.selection_path_ref, "fork resume")
                _require(
                    record.branch_scope_ref == record.ref,
                    "fork branch resume scope must be its resume ref",
                )
                path.branch_scope_ref = record.branch_scope_ref
                _require_path_matches(
                    path,
                    selection_ref=record.selection_ref,
                    source_ref=record.source_ref,
                    branch_ref=record.branch_ref,
                    branch_scope_ref=record.branch_scope_ref,
                    context="fork resume",
                )
                _require_path_open(path, "fork resume")
                open_branch_scope(record)
                terminal_resume_sources.add(record.source_ref)
            else:
                raise TraceValidationError(f"unsupported resume source type: {record.source_record_type!r}")
            ledger.resumes[record.ref] = record
            continue

        if isinstance(record, ResumeReturn):
            require_branch_context(
                record.branch_ref,
                record.branch_scope_ref,
                "resume return",
            )
            resume = _require_lookup(ledger.resumes, record.resume_ref, "resume return cites missing resume")
            _require(record.resume_ref in callable_resumes, "ResumeReturn must cite a callable resume")
            _require(
                record.branch_scope_ref == resume.branch_scope_ref,
                "resume return branch scope mismatch",
            )
            ledger.record_resume_return(record)
            returned_resumes.add(record.resume_ref)
            continue

        if isinstance(record, TerminalResumeResult):
            _require(
                record.source_record_type in {"ContinuationPending", "ForkBranch"},
                "TerminalResumeResult must cite a terminal resume source",
            )
            resume = _require_lookup(ledger.resumes, record.resume_ref, "terminal result cites missing resume")
            require_branch_context(
                record.branch_ref,
                record.branch_scope_ref,
                "terminal result",
            )
            _require(
                resume.source_record_type == record.source_record_type and resume.source_ref == record.source_ref,
                "terminal result source mismatch",
            )
            _require(
                resume.selection_path_ref == record.selection_path_ref and resume.branch_ref == record.branch_ref,
                "terminal result selected path mismatch",
            )
            _require(
                resume.branch_scope_ref == record.branch_scope_ref,
                "terminal result branch scope mismatch",
            )
            path = _require_path(paths, record.selection_path_ref, "terminal result")
            _require_path_matches(
                path,
                selection_ref=resume.selection_ref,
                source_ref=resume.source_ref,
                branch_ref=record.branch_ref,
                branch_scope_ref=record.branch_scope_ref,
                context="terminal result",
            )
            ledger.terminalize_path(path, "TerminalResumeResult", record.ref)
            terminal_resume_results.add(record.resume_ref)
            if record.source_record_type == "ForkBranch":
                close_branch_scope(record)
            continue

        if isinstance(record, EffectCapture):
            require_branch_context(record.branch_ref, record.branch_scope_ref, "capture")
            ledger.record_effect_capture(record, boundary=None, idx=idx)
            continue

        if isinstance(record, SelectionClosed):
            require_branch_context(
                record.branch_ref,
                record.branch_scope_ref,
                "selection closure",
            )
            _require(record.selection_ref in selections, "selection closure cites missing selection")
            if record.caused_by_record_type == "HandlerForward":
                _require(record.reason == "forwarded", "forward closure reason mismatch")
                cause = _require_lookup(forwards_by_ref, record.caused_by_ref, "forward closure cites missing cause")
                _require(
                    cause.skipped_selection_ref == record.selection_ref
                    and cause.skipped_selection_path_ref == record.selection_path_ref,
                    "forward closure cause must be the forwarded selected path",
                )
                _require(
                    record.closed_by_selection_ref == record.selection_ref
                    and record.closed_by_selection_path_ref == record.selection_path_ref,
                    "forward closure must close the forwarded selected path",
                )
                _require(
                    cause.branch_ref == record.branch_ref,
                    "forward closure branch mismatch",
                )
                _require(
                    cause.branch_scope_ref == record.branch_scope_ref,
                    "forward closure branch scope mismatch",
                )
                closed_forward_paths[record.selection_path_ref] += 1
                _require(
                    closed_forward_paths[record.selection_path_ref] == 1,
                    "forwarded selected path closed more than once",
                )
                ledger.close_selection_path(record)
                forward_closed_selections.add(record.selection_ref)
            else:
                ledger.record_selection_closed_by_capture(record, boundary=None)
            continue

        raise TraceValidationError(f"unknown trace record: {record!r}")

    for resume_ref in callable_resumes:
        if completed:
            resume = ledger.resumes[resume_ref]
            _require(
                resume_ref in returned_resumes
                or ledger.closures[(resume.selection_ref, resume.selection_path_ref)] == 1,
                f"callable resume {resume_ref!r} has no ResumeReturn or SelectionClosed",
            )
    for resume in ledger.resumes.values():
        if resume.source_record_type in {"ContinuationPending", "ForkBranch"}:
            _require(
                resume.ref in terminal_resume_results or not completed,
                f"terminal resume {resume.ref!r} has no TerminalResumeResult",
            )
    if completed:
        for fork in fork_summaries.values():
            for branch_ref in fork.branch_refs:
                _require(
                    fork_branch_materializations[(fork.ref, branch_ref)] == 1,
                    f"fork branch {branch_ref!r} was not materialized",
                )
        for path_ref, count in forward_paths.items():
            _require(
                closed_forward_paths[path_ref] == count,
                f"forwarded selected path {path_ref!r} has no SelectionClosed",
            )
        for pending_ref in pending_sources:
            _require(
                pending_delays[pending_ref] == 1,
                f"pending source {pending_ref!r} must have exactly one ContinuationDelay",
            )
        _require(
            not open_branch_scopes,
            f"completed trace has open branch scopes: {sorted(open_branch_scopes)!r}",
        )
        _require(
            not branch_stack,
            f"completed trace has active branch scopes: {[s.scope_ref for s in branch_stack]!r}",
        )
        for selection_ref in selections:
            _require(
                ledger.handles_by_selection[selection_ref] == 1,
                f"completed selection {selection_ref!r} must have exactly one ResumptionHandle",
            )
        unselected = sorted(set(declarations) - ledger.selected_declarations)
        _require(
            not unselected,
            f"completed trace has unselected declarations: {unselected!r}",
        )
        open_paths = sorted(ledger.open_paths())
        _require(
            not open_paths,
            f"completed trace has open selected paths: {open_paths!r}",
        )


def _is_selection_ancestor(
    ancestor_ref: str,
    descendant_ref: str,
    selection_parents: dict[str, set[str]],
) -> bool:
    pending = list(selection_parents.get(descendant_ref, ()))
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == ancestor_ref:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(selection_parents.get(current, ()))
    return False


def _require_path(
    paths: dict[str, _SelectedPathState],
    path_ref: str,
    context: str,
) -> _SelectedPathState:
    path = paths.get(path_ref)
    if path is None:
        raise TraceValidationError(f"{context} selected path mismatch: missing selected path")
    return path


def _require_path_matches(
    path: _SelectedPathState,
    *,
    selection_ref: str,
    branch_ref: str,
    context: str,
    source_ref: str | None = None,
    branch_scope_ref: str | None | object = _UNSPECIFIED,
) -> None:
    _require(path.selection_ref == selection_ref, f"{context} selected path mismatch")
    _require(path.branch_ref == branch_ref, f"{context} selected path mismatch")
    if source_ref is not None:
        _require(path.source_ref == source_ref, f"{context} selected path mismatch")
    if branch_scope_ref is not _UNSPECIFIED:
        _require(
            path.branch_scope_ref == branch_scope_ref,
            f"{context} selected path scope mismatch",
        )


def _require_path_open(path: _SelectedPathState, context: str) -> None:
    _require(
        path.terminal_record_type is None,
        f"{context} path is closed by {path.terminal_record_type} {path.terminal_ref!r}",
    )


def _terminalize_path(
    path: _SelectedPathState,
    terminal_record_type: str,
    terminal_ref: str,
) -> None:
    _require_path_open(path, terminal_record_type)
    path.terminal_record_type = terminal_record_type
    path.terminal_ref = terminal_ref


def _reject_ref(seen: set[str], ref: str, kind: str) -> None:
    _require(ref not in seen, f"duplicate trace record ref: {kind} {ref!r}")
    seen.add(ref)


def _reject_duplicate(records: Container[str], ref: str, kind: str) -> None:
    _require(ref not in records, f"duplicate {kind} ref: {ref}")


def _require_lookup(records: Mapping[str, _T], ref: str, message: str) -> _T:
    value = records.get(ref)
    if value is None:
        raise TraceValidationError(message)
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TraceValidationError(message)


def _require_present(value: object | None, field_name: str) -> None:
    _require(value is not None, f"{field_name} is required")


def _require_root_branch(branch_ref: str) -> None:
    _require(
        branch_ref == "branch:root",
        f"single-root core admits only branch:root, got {branch_ref!r}",
    )


def _require_capture_action_matches_disposition(record: EffectCapture) -> None:
    if record.action_kind == "return":
        _require(
            record.continuation_disposition == "completed",
            "return capture must have completed disposition",
        )
        return
    if record.action_kind == "abort":
        _require(
            record.continuation_disposition == "aborted",
            "abort capture must have aborted disposition",
        )
        return
    raise TraceValidationError(f"unsupported capture action: {record.action_kind!r}")


def _selection_path(selection_ref: str, source_ref: str, branch_ref: str) -> str:
    return source_path_ref(selection_ref, source_ref, branch_ref)


def _outcome_complete_for_profile(
    outcome: SourceOutcome,
    *,
    profile: object,
) -> bool:
    if isinstance(outcome, Completed):
        return True
    if profile == PUBLICATION_EXPERIMENTAL and isinstance(outcome, Forked):
        return all(isinstance(branch_outcome, Completed) for branch_outcome in outcome.branches.values())
    return False
