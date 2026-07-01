"""Explicit semantic state carried by the executable kernel machine."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from shepherd_kernel_v3_reference.kernel.context import ExecutionContext
from shepherd_kernel_v3_reference.profiles import CORE_A, SemanticProfile

if TYPE_CHECKING:
    from collections.abc import Iterator

    from shepherd_kernel_v3_reference.kernel.continuations import ContinuationImage
    from shepherd_kernel_v3_reference.kernel.ir import Ref


@dataclass
class MachineState:
    """Mutable execution state for one trace session.

    The evaluator is still implemented recursively, but semantic state is
    centralized here so branch, path, trace-id, and continuation-catalog facts
    stop living as unrelated evaluator globals.
    """

    profile: SemanticProfile = CORE_A
    program_ref: Ref | None = None
    branch_ref: Ref = "branch:root"
    branch_scope_ref: Ref | None = None
    execution_context: ExecutionContext = field(default_factory=ExecutionContext)
    terminal_paths: set[Ref] = field(default_factory=set)
    consumed_source_paths: set[Ref] = field(default_factory=set)
    next_trace_id: int = 0
    continuation_image_catalog: dict[Ref, ContinuationImage] = field(default_factory=dict)

    def fresh_ref(self, prefix: str) -> Ref:
        ref = f"{prefix}:{self.next_trace_id}"
        self.next_trace_id += 1
        return ref

    def consume_source_path(self, source_path_ref: Ref) -> bool:
        if source_path_ref in self.consumed_source_paths:
            return False
        self.consumed_source_paths.add(source_path_ref)
        return True

    def mark_terminal_path(self, selection_path_ref: Ref) -> bool:
        if selection_path_ref in self.terminal_paths:
            return False
        self.terminal_paths.add(selection_path_ref)
        return True

    @contextmanager
    def scoped_branch(
        self,
        branch_ref: Ref,
        branch_scope_ref: Ref | None = None,
    ) -> Iterator[None]:
        previous = self.branch_ref
        previous_scope = self.branch_scope_ref
        self.branch_ref = branch_ref
        self.branch_scope_ref = branch_scope_ref
        try:
            yield
        finally:
            self.branch_ref = previous
            self.branch_scope_ref = previous_scope
