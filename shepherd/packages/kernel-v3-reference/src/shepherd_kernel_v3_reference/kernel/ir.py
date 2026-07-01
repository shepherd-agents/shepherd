"""Kernel IR for the §02 source-calculus core.

The IR is intentionally first-order at the sequencing boundary: source `Let`
does not survive as source syntax. It becomes `KBind(bound, binder_id,
binder_env_ref)`, with the binder body stored separately in `BinderDef`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.schemas import Schema
    from shepherd_kernel_v3_reference.source.syntax import Expr

Ref = str


@dataclass(frozen=True)
class KPure:
    expr: Expr


@dataclass(frozen=True)
class KBind:
    bound: KComputation
    binder_id: Ref
    binder_env_ref: Ref


@dataclass(frozen=True)
class KPerform:
    effect_kind: str
    payload: Expr
    payload_schema_ref: Ref | None = None
    operation_result_schema_ref: Ref | None = None


@dataclass(frozen=True)
class KHandle:
    body: KComputation
    handler_env_ref: Ref
    region_ref: Ref = "region:root"


@dataclass(frozen=True)
class KResumeWith:
    value: Expr


@dataclass(frozen=True)
class KAbort:
    value: Expr


@dataclass(frozen=True)
class KForward:
    pass


@dataclass(frozen=True)
class KTerminalDelay:
    reason: Expr


@dataclass(frozen=True)
class KTerminalFork:
    branches: tuple[tuple[Ref, Expr], ...]


KComputation = Union[
    KPure,
    KBind,
    KPerform,
    KHandle,
    KResumeWith,
    KAbort,
    KForward,
    KTerminalDelay,
    KTerminalFork,
]


@dataclass(frozen=True)
class BinderDef:
    binder_id: Ref
    param_name: str
    body: KComputation
    binder_env_ref: Ref


@dataclass(frozen=True)
class HandlerInstallDef:
    install_ref: Ref
    effect_kind: str
    handler_id: str
    handled_result_schema_ref: Ref
    payload_name: str
    body: KComputation


@dataclass(frozen=True)
class HandlerEnvDef:
    handler_env_ref: Ref
    bindings: tuple[HandlerInstallDef, ...]


@dataclass(frozen=True)
class SchemaDef:
    schema_ref: Ref
    schema: Schema
