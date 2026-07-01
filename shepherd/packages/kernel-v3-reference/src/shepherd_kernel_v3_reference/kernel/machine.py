"""Public runner entry points for the v3 kernel machine."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.kernel.program_admission import KernelProgramInput
    from shepherd_kernel_v3_reference.source.outcomes import SourceOutcome
    from shepherd_kernel_v3_reference.source.values import Env


def run_kernel(program: KernelProgramInput, env: Env | None = None) -> SourceOutcome:
    from shepherd_kernel_v3_reference.kernel.step_machine import StepKernelEvaluator

    return StepKernelEvaluator(program, evidence_mode="none").run(env)
