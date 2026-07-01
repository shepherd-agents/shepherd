"""Private readiness vocabulary for staged kernel-program gates."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from shepherd_kernel_v3_reference.kernel.program_admission import PreparedKernelProgram, prepare_kernel_program
from shepherd_kernel_v3_reference.kernel.program_identity import project_program_identity
from shepherd_kernel_v3_reference.profiles import CORE_A

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.kernel.elaborate import KernelProgram
    from shepherd_kernel_v3_reference.kernel.ir import Ref


class KernelProgramReadinessError(RuntimeError):
    """Raised when a program does not satisfy a requested readiness tier."""


class KernelProgramReadinessTier(StrEnum):
    """Named gates used before routing a program through kernel runtime paths."""

    ADMITTED = "admitted"
    IDENTITY_READY = "identity_ready"
    EXECUTION_READY = "execution_ready"
    ARTIFACT_READY = "artifact_ready"


@dataclass(frozen=True)
class KernelProgramReadiness:
    tier: KernelProgramReadinessTier
    prepared: PreparedKernelProgram
    program_ref: Ref | None = None


def require_kernel_program_readiness(
    program: KernelProgram,
    tier: KernelProgramReadinessTier,
) -> KernelProgramReadiness:
    """Check the structural readiness tiers that can be decided for one program.

    Execution and artifact readiness are row-level gates because they depend on
    evaluator behavior and emitted evidence, not only the program graph.
    """

    prepared = prepare_kernel_program(program, profile=CORE_A)
    if tier == KernelProgramReadinessTier.ADMITTED:
        return KernelProgramReadiness(tier=tier, prepared=prepared)

    if tier == KernelProgramReadinessTier.IDENTITY_READY:
        try:
            identity = project_program_identity(prepared)
        except RecursionError as exc:
            raise KernelProgramReadinessError(
                "kernel program is admitted but not identity_ready under the current stack-safety target"
            ) from exc
        return KernelProgramReadiness(
            tier=tier,
            prepared=prepared,
            program_ref=identity.program_ref,
        )

    raise KernelProgramReadinessError(
        f"{tier.value} readiness is an evaluator/evidence gate, not a structural preflight"
    )
