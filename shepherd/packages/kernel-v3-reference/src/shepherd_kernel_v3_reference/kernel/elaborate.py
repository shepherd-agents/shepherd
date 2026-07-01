"""Elaboration from source syntax to kernel IR."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count
from typing import TYPE_CHECKING, TypeAlias, cast

from shepherd_kernel_v3_reference.kernel.ir import (
    BinderDef,
    HandlerEnvDef,
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
    SchemaDef,
)
from shepherd_kernel_v3_reference.profiles import CORE_A, PUBLICATION_EXPERIMENTAL, SemanticProfile
from shepherd_kernel_v3_reference.source.effects import EffectRegistry
from shepherd_kernel_v3_reference.source.experimental import Forward, TerminalDelay, TerminalFork
from shepherd_kernel_v3_reference.source.handlers import (
    DynamicHandlerInstall,
    HandlerEnv,
    HandlerInstall,
    StaticHandlerInstall,
)
from shepherd_kernel_v3_reference.source.syntax import (
    Abort,
    Computation,
    Handle,
    Let,
    Perform,
    Resume,
    Return,
)
from shepherd_kernel_v3_reference.source.wellformed import (
    SourceFormError,
    validate_core_handler_body,
    validate_core_program,
    validate_publication_experimental_handler_body,
    validate_publication_experimental_program,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from shepherd_kernel_v3_reference.schemas import Schema


@dataclass(frozen=True)
class KernelProgram:
    root: KComputation
    binders: Mapping[Ref, BinderDef]
    handler_envs: Mapping[Ref, HandlerEnvDef]
    schemas: Mapping[Ref, SchemaDef]
    profile: SemanticProfile = CORE_A


@dataclass(frozen=True)
class _ElaborateTerm:
    term: object


@dataclass(frozen=True)
class _FinishLet:
    name: str


@dataclass(frozen=True)
class _BuildHandle:
    handler_env: HandlerEnv


@dataclass(frozen=True)
class _FinishHandle:
    body_ir: KComputation


@dataclass(frozen=True)
class _ElaborateHandlerEnv:
    handler_env: HandlerEnv


@dataclass(frozen=True)
class _ContinueHandlerEnv:
    handler_env_ref: Ref
    installs: tuple[HandlerInstall, ...]
    index: int
    bindings: tuple[HandlerInstallDef, ...]


@dataclass(frozen=True)
class _FinishInstall:
    handler_env_ref: Ref
    installs: tuple[HandlerInstall, ...]
    index: int
    bindings: tuple[HandlerInstallDef, ...]
    install: StaticHandlerInstall
    install_ref: Ref
    handled_schema_ref: Ref


_ElaborationFrame = (
    _ElaborateTerm
    | _FinishLet
    | _BuildHandle
    | _FinishHandle
    | _ElaborateHandlerEnv
    | _ContinueHandlerEnv
    | _FinishInstall
)
_ElaborationResult: TypeAlias = KComputation | Ref


@dataclass
class Elaborator:
    registry: EffectRegistry
    profile: SemanticProfile = CORE_A
    region_ref: Ref = "region:root"
    _fresh: Iterator[int] = field(default_factory=count)
    binders: dict[Ref, BinderDef] = field(default_factory=dict)
    handler_envs: dict[Ref, HandlerEnvDef] = field(default_factory=dict)
    schemas: dict[Ref, SchemaDef] = field(default_factory=dict)

    def elaborate_program(self, term: object) -> KernelProgram:
        self._validate_program(term)
        root = self.elaborate_computation(term)
        return KernelProgram(
            root=root,
            binders=dict(self.binders),
            handler_envs=dict(self.handler_envs),
            schemas=dict(self.schemas),
            profile=self.profile,
        )

    def elaborate_handler_body(self, term: object) -> KComputation:
        self._validate_handler_body(term)
        return self.elaborate_computation(term)

    def elaborate_computation(self, term: object) -> KComputation:
        frames: list[_ElaborationFrame] = [_ElaborateTerm(term)]
        results: list[_ElaborationResult] = []

        while frames:
            frame = frames.pop()
            if isinstance(frame, _ElaborateTerm):
                self._schedule_term(frame.term, frames, results)
                continue

            if isinstance(frame, _FinishLet):
                body_ir = cast("KComputation", results.pop())
                bound_ir = cast("KComputation", results.pop())
                binder_id = self._ref("binder")
                env_ref = self._ref("env")
                self.binders[binder_id] = BinderDef(
                    binder_id=binder_id,
                    param_name=frame.name,
                    body=body_ir,
                    binder_env_ref=env_ref,
                )
                results.append(KBind(bound_ir, binder_id, env_ref))
                continue

            if isinstance(frame, _BuildHandle):
                body_ir = cast("KComputation", results.pop())
                frames.append(_FinishHandle(body_ir))
                frames.append(_ElaborateHandlerEnv(frame.handler_env))
                continue

            if isinstance(frame, _FinishHandle):
                handler_env_ref = cast("Ref", results.pop())
                results.append(
                    KHandle(
                        body=frame.body_ir,
                        handler_env_ref=handler_env_ref,
                        region_ref=self.region_ref,
                    )
                )
                continue

            if isinstance(frame, _ElaborateHandlerEnv):
                handler_env_ref = self._ref("handler-env")
                frames.append(_ContinueHandlerEnv(handler_env_ref, frame.handler_env.bindings, 0, ()))
                continue

            if isinstance(frame, _ContinueHandlerEnv):
                if frame.index >= len(frame.installs):
                    self.handler_envs[frame.handler_env_ref] = HandlerEnvDef(
                        frame.handler_env_ref,
                        frame.bindings,
                    )
                    results.append(frame.handler_env_ref)
                    continue

                install = frame.installs[frame.index]
                if isinstance(install, DynamicHandlerInstall):
                    raise SourceFormError(
                        "kernel elaboration requires static handler bodies; "
                        f"handler {install.handler_id!r} uses a Python builder"
                    )

                install_ref = self._ref("install")
                handled_schema_ref = self._schema_ref(
                    "handled-result",
                    install.handler_id,
                    install.handled_result_schema,
                )
                self._validate_handler_body(install.body)
                frames.append(
                    _FinishInstall(
                        handler_env_ref=frame.handler_env_ref,
                        installs=frame.installs,
                        index=frame.index,
                        bindings=frame.bindings,
                        install=install,
                        install_ref=install_ref,
                        handled_schema_ref=handled_schema_ref,
                    )
                )
                frames.append(_ElaborateTerm(install.body))
                continue

            if isinstance(frame, _FinishInstall):
                body_ir = cast("KComputation", results.pop())
                install_def = HandlerInstallDef(
                    install_ref=frame.install_ref,
                    effect_kind=frame.install.effect_kind,
                    handler_id=frame.install.handler_id,
                    handled_result_schema_ref=frame.handled_schema_ref,
                    payload_name=frame.install.payload_name,
                    body=body_ir,
                )
                frames.append(
                    _ContinueHandlerEnv(
                        frame.handler_env_ref,
                        frame.installs,
                        frame.index + 1,
                        (*frame.bindings, install_def),
                    )
                )
                continue

        if len(results) != 1:
            raise RuntimeError("internal elaboration stack produced an invalid result count")
        return cast("KComputation", results[0])

    def _schedule_term(
        self,
        term: object,
        frames: list[_ElaborationFrame],
        results: list[_ElaborationResult],
    ) -> None:
        match term:
            case Return(expr):
                results.append(KPure(expr))

            case Let(name=name, bound=bound, body=body):
                frames.append(_FinishLet(name))
                frames.append(_ElaborateTerm(body))
                frames.append(_ElaborateTerm(bound))

            case Perform(effect_kind=effect_kind, payload=payload):
                payload_schema_ref: Ref | None = None
                operation_result_schema_ref: Ref | None = None
                if effect_kind in self.registry:
                    sig = self.registry.lookup(effect_kind)
                    payload_schema_ref = self._schema_ref("payload", effect_kind, sig.payload_schema)
                    operation_result_schema_ref = self._schema_ref(
                        "operation-result",
                        effect_kind,
                        sig.operation_result_schema,
                    )
                results.append(
                    KPerform(
                        effect_kind=effect_kind,
                        payload=payload,
                        payload_schema_ref=payload_schema_ref,
                        operation_result_schema_ref=operation_result_schema_ref,
                    )
                )

            case Handle(body=body, handler_env=handler_env):
                frames.append(_BuildHandle(handler_env))
                frames.append(_ElaborateTerm(body))

            case Resume(value=value):
                results.append(KResumeWith(value))

            case Abort(value=value):
                results.append(KAbort(value))

            case Forward() if self.profile == PUBLICATION_EXPERIMENTAL:
                results.append(KForward())

            case TerminalDelay(reason=reason) if self.profile == PUBLICATION_EXPERIMENTAL:
                results.append(KTerminalDelay(reason))

            case TerminalFork(branches=branches) if self.profile == PUBLICATION_EXPERIMENTAL:
                results.append(KTerminalFork(branches))

            case _:
                raise TypeError(f"unknown computation form: {term!r}")

    def _schema_ref(self, role: str, name: str, schema: Schema) -> Ref:
        ref = f"schema:{role}:{name}"
        existing = self.schemas.get(ref)
        if existing is None:
            self.schemas[ref] = SchemaDef(ref, schema)
        elif existing.schema != schema:
            ref = self._ref(f"schema:{role}:{name}")
            self.schemas[ref] = SchemaDef(ref, schema)
        return ref

    def _ref(self, prefix: str) -> Ref:
        return f"{prefix}:{next(self._fresh)}"

    def _validate_program(self, term: object) -> None:
        if self.profile == PUBLICATION_EXPERIMENTAL:
            validate_publication_experimental_program(term)
            return
        validate_core_program(cast("Computation", term))

    def _validate_handler_body(self, term: object) -> None:
        if self.profile == PUBLICATION_EXPERIMENTAL:
            validate_publication_experimental_handler_body(term)
            return
        validate_core_handler_body(cast("Computation", term))


def elaborate(
    term: Computation,
    *,
    registry: EffectRegistry | None = None,
) -> KernelProgram:
    return Elaborator(registry=registry or EffectRegistry()).elaborate_program(term)


def elaborate_publication_experimental(
    term: object,
    *,
    registry: EffectRegistry | None = None,
) -> KernelProgram:
    return Elaborator(
        registry=registry or EffectRegistry(),
        profile=PUBLICATION_EXPERIMENTAL,
    ).elaborate_program(term)
