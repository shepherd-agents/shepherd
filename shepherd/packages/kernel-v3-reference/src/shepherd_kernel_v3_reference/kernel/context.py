"""Execution context identities for the kernel fragment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.kernel.ir import Ref


@dataclass(frozen=True)
class ExecutionContext:
    binding_env_ref: Ref = "env:root"
    region_ref: Ref = "region:root"
    authority_ref: Ref = "authority:root"

    def with_binding_env_ref(self, binding_env_ref: Ref) -> ExecutionContext:
        return ExecutionContext(
            binding_env_ref=binding_env_ref,
            region_ref=self.region_ref,
            authority_ref=self.authority_ref,
        )

    def with_region_ref(self, region_ref: Ref) -> ExecutionContext:
        return ExecutionContext(
            binding_env_ref=self.binding_env_ref,
            region_ref=region_ref,
            authority_ref=self.authority_ref,
        )

    def with_authority_ref(self, authority_ref: Ref) -> ExecutionContext:
        return ExecutionContext(
            binding_env_ref=self.binding_env_ref,
            region_ref=self.region_ref,
            authority_ref=authority_ref,
        )
