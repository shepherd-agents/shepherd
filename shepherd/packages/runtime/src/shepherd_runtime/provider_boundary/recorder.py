"""D6 ``InterpositionRecorder`` Protocol plus in-memory recorder.

The recorder is the boundary between provider adapters and the
kernel trace machine. Adapters drive these methods from streaming
SDK callbacks; the recorder owns ref minting, ``ExecutionContext``
stamping, and lifecycle validation.

Nine methods exactly as named (CONTRACTS D6 lifts these verbatim
from Plan 01 SCOPE WP2). All return the minted record's content-
addressed ``Ref`` so the caller can cite it in subsequent calls.

The ``StubRecorder`` records calls in a list, mints counter-based refs, and
enforces the local lifecycle order that offline provider-boundary tests rely
on. It is still not a production content-addressing recorder.

Pinned by `docs/design/proposed/260505-plans/CONTRACTS.md` D6.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from shepherd_runtime.provider_boundary.payloads import ModelRequest
    from shepherd_runtime.trace import Ref

__all__ = [
    "InterpositionRecorder",
    "RecorderLifecycleError",
    "StubRecorder",
    "ToolHandlerNotFoundError",
]


class RecorderLifecycleError(RuntimeError):
    """Recorder method called out of sextet order.

    Recorders enforce the local provider-boundary lifecycle
    (declaration -> selection -> resumption -> resume -> resume-return ->
    capture) for return paths. Abort paths may capture and close a selection
    without a resume-return.
    """


class ToolHandlerNotFoundError(LookupError):
    """``select_tool_handler`` was called for a ``tool.<name>`` with no handler registered.

    Adapters catch this at the recorder boundary and emit
    ``capture(selection_ref, "abort", ...)`` plus
    ``selection_closed(selection_ref, "abandoned")`` on the parent
    ``model.call`` selection to keep the outer sextet well-formed.
    """


class InterpositionRecorder(Protocol):
    """Mediate between provider adapters and the kernel trace machine.

    Adapters drive these methods from streaming SDK callbacks; the
    recorder owns ref minting, ``ExecutionContext`` stamping, and
    lifecycle validation. Adapters do not see ref minting or
    ``ExecutionContext`` directly.

    Calls compose into the standard sextet for one model turn::

        start_model_call -> select_provider_handler -> mint_resumption ->
        resume -> resume_return -> capture

    Tool calls inside the turn add their own nested sextet via
    ``open_tool_call -> select_tool_handler -> mint_resumption ->
    resume -> resume_return -> capture``.
    """

    def start_model_call(self, request: ModelRequest) -> Ref:
        """Mint ``EffectDeclaration("model.call")``; return its ref."""

    def select_provider_handler(self, declaration_ref: Ref, handler_id: str) -> Ref:
        """Mint ``HandlerSelection`` for the model.call; return its ref."""

    def open_tool_call(self, parent_decl_ref: Ref, tool_name: str, payload: dict) -> Ref:
        """Mint nested ``EffectDeclaration("tool.<name>")``; return its ref."""

    def select_tool_handler(self, decl_ref: Ref, handler_id: str) -> Ref:
        """Mint ``HandlerSelection`` for a tool effect; return its ref."""

    def mint_resumption(self, decl_ref: Ref, selection_ref: Ref) -> Ref:
        """Mint ``ResumptionHandle``; return its ref."""

    def resume(self, handle_ref: Ref, value: object) -> Ref:
        """Mint ``ContinuationResume``; return its ref."""

    def resume_return(self, resume_ref: Ref, value: object) -> Ref:
        """Mint ``ResumeReturn``; return its ref."""

    def capture(
        self,
        selection_ref: Ref,
        action_kind: Literal["return", "abort"],
        payload: object,
    ) -> Ref:
        """Mint ``EffectCapture``; return its ref."""

    def selection_closed(self, selection_ref: Ref, reason: str) -> Ref:
        """Mint ``SelectionClosed``; return its ref."""


class StubRecorder:
    """Minimal ``InterpositionRecorder`` for consumer tests.

    Records every call as a tuple in ``self.calls``; mints counter-
    based refs. Consumers asserting trace structure walk
    ``self.calls``; consumers asserting ref content-addressing must
    use the real recorder.

    The stub enforces local lifecycle ordering for declarations, selections,
    resumptions, resumes, resume-returns, captures, and close records. It does
    not perform production content-addressing or kernel-v3 proof validation.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._counter = 0
        self._declarations: set[Ref] = set()
        self._selections: dict[Ref, Ref] = {}
        self._handles: dict[Ref, Ref] = {}
        self._resumes: dict[Ref, Ref] = {}
        self._resume_returns: set[Ref] = set()
        self._captured: set[Ref] = set()
        self._closed: set[Ref] = set()

    def _next_ref(self, kind: str) -> str:
        self._counter += 1
        return f"{kind}:stub:{self._counter}"

    def start_model_call(self, request: ModelRequest) -> Ref:
        ref = self._next_ref("decl")
        self._declarations.add(ref)
        self.calls.append(("start_model_call", request, ref))
        return ref

    def select_provider_handler(self, declaration_ref: Ref, handler_id: str) -> Ref:
        self._require_declaration(declaration_ref)
        ref = self._next_ref("sel")
        self._selections[ref] = declaration_ref
        self.calls.append(("select_provider_handler", declaration_ref, handler_id, ref))
        return ref

    def open_tool_call(self, parent_decl_ref: Ref, tool_name: str, payload: dict) -> Ref:
        self._require_declaration(parent_decl_ref)
        ref = self._next_ref("decl")
        self._declarations.add(ref)
        self.calls.append(("open_tool_call", parent_decl_ref, tool_name, payload, ref))
        return ref

    def select_tool_handler(self, decl_ref: Ref, handler_id: str) -> Ref:
        self._require_declaration(decl_ref)
        ref = self._next_ref("sel")
        self._selections[ref] = decl_ref
        self.calls.append(("select_tool_handler", decl_ref, handler_id, ref))
        return ref

    def mint_resumption(self, decl_ref: Ref, selection_ref: Ref) -> Ref:
        self._require_selection(selection_ref)
        if self._selections[selection_ref] != decl_ref:
            raise RecorderLifecycleError(f"selection {selection_ref!r} does not belong to declaration {decl_ref!r}")
        ref = self._next_ref("handle")
        self._handles[ref] = selection_ref
        self.calls.append(("mint_resumption", decl_ref, selection_ref, ref))
        return ref

    def resume(self, handle_ref: Ref, value: object) -> Ref:
        if handle_ref not in self._handles:
            raise RecorderLifecycleError(f"unknown resumption handle {handle_ref!r}")
        ref = self._next_ref("resume")
        self._resumes[ref] = handle_ref
        self.calls.append(("resume", handle_ref, value, ref))
        return ref

    def resume_return(self, resume_ref: Ref, value: object) -> Ref:
        if resume_ref not in self._resumes:
            raise RecorderLifecycleError(f"unknown resume ref {resume_ref!r}")
        ref = self._next_ref("rret")
        self._resume_returns.add(resume_ref)
        self.calls.append(("resume_return", resume_ref, value, ref))
        return ref

    def capture(
        self,
        selection_ref: Ref,
        action_kind: Literal["return", "abort"],
        payload: object,
    ) -> Ref:
        self._require_selection(selection_ref)
        if selection_ref in self._captured:
            raise RecorderLifecycleError(f"selection {selection_ref!r} is already captured")
        if action_kind == "return":
            selection_handles = [handle for handle, selection in self._handles.items() if selection == selection_ref]
            if selection_handles and not any(
                resume in self._resume_returns
                for resume, handle in self._resumes.items()
                if handle in selection_handles
            ):
                raise RecorderLifecycleError(f"selection {selection_ref!r} cannot capture return before resume_return")
        ref = self._next_ref("cap")
        self._captured.add(selection_ref)
        self.calls.append(("capture", selection_ref, action_kind, payload, ref))
        return ref

    def selection_closed(self, selection_ref: Ref, reason: str) -> Ref:
        self._require_selection(selection_ref)
        if selection_ref in self._closed:
            raise RecorderLifecycleError(f"selection {selection_ref!r} is already closed")
        ref = self._next_ref("close")
        self._closed.add(selection_ref)
        self.calls.append(("selection_closed", selection_ref, reason, ref))
        return ref

    def _require_declaration(self, declaration_ref: Ref) -> None:
        if declaration_ref not in self._declarations:
            raise RecorderLifecycleError(f"unknown declaration ref {declaration_ref!r}")

    def _require_selection(self, selection_ref: Ref) -> None:
        if selection_ref not in self._selections:
            raise RecorderLifecycleError(f"unknown selection ref {selection_ref!r}")
