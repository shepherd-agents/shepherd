"""Stack-disciplined stable identity projection for admitted kernel programs."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from shepherd_kernel_v3_reference.kernel.ir import (
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
from shepherd_kernel_v3_reference.kernel.refs import content_ref
from shepherd_kernel_v3_reference.schemas import schema_fingerprint
from shepherd_kernel_v3_reference.source.syntax import Lit, RecordExpr, Var

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.kernel.program_admission import NodeId, PreparedKernelProgram


@dataclass(frozen=True)
class ProgramIdentity:
    program_ref: Ref
    control_fingerprints: MappingProxyType[NodeId, object]
    binder_fingerprints: MappingProxyType[Ref, object]
    binder_refs: MappingProxyType[Ref, Ref]
    handler_env_fingerprints: MappingProxyType[Ref, object]
    handler_env_refs: MappingProxyType[Ref, Ref]
    install_fingerprints_by_node: MappingProxyType[NodeId, object]
    install_refs_by_node: MappingProxyType[NodeId, Ref]
    install_fingerprints_by_object_id: MappingProxyType[int, object]
    install_refs_by_object_id: MappingProxyType[int, Ref]
    schemas_fingerprint: object
    schema_ref_fingerprints: MappingProxyType[Ref | None, object | None]


def project_program_identity(prepared: PreparedKernelProgram) -> ProgramIdentity:
    """Compute byte-for-byte stable program refs from an admitted program index."""

    cached = prepared._identity_cache
    if cached is not None:
        return cached
    identity = _project_program_identity_uncached(prepared)
    object.__setattr__(prepared, "_identity_cache", identity)
    return identity


def _project_program_identity_uncached(prepared: PreparedKernelProgram) -> ProgramIdentity:
    projector = _ProgramIdentityProjector(prepared)
    return projector.project()


class _ProgramIdentityProjector:
    def __init__(self, prepared: PreparedKernelProgram) -> None:
        self.prepared = prepared
        self.program = prepared.program
        self.index = prepared.index
        self.control_fingerprints: dict[NodeId, object] = {}
        self.binder_fingerprints: dict[Ref, object] = {}
        self.binder_refs: dict[Ref, Ref] = {}
        self.handler_env_fingerprints: dict[Ref, object] = {}
        self.handler_env_refs: dict[Ref, Ref] = {}
        self.install_fingerprints_by_node: dict[NodeId, object] = {}
        self.install_refs_by_node: dict[NodeId, Ref] = {}
        self.install_fingerprints_by_object_id: dict[int, object] = {}
        self.install_refs_by_object_id: dict[int, Ref] = {}
        self.schema_ref_fingerprints: dict[Ref | None, object | None] = {None: None}

    def project(self) -> ProgramIdentity:
        for node in self.index.identity_postorder:
            kind = node[0]
            if kind == "schema":
                self._schema_ref_fingerprint(node[1])
            elif kind == "control":
                self._project_control(node)
            elif kind == "binder":
                self._project_binder(node[1])
            elif kind == "handler-env":
                self._project_handler_env(node[1])
            elif kind == "install":
                self._project_install(node)
            else:
                raise RuntimeError(f"unknown program identity node: {node!r}")

        schemas_fingerprint = tuple(
            (schema_ref, schema_fingerprint(schema_def.schema))
            for schema_ref, schema_def in sorted(self.program.schemas.items())
        )
        program_ref = content_ref(
            "program",
            {
                "root": self.control_fingerprints[self.index.root_node],
                "profile": {
                    "name": self.program.profile.name,
                    "version": self.program.profile.version,
                    "validated": self.program.profile.validated,
                },
                "schemas": schemas_fingerprint,
            },
        )
        return ProgramIdentity(
            program_ref=program_ref,
            control_fingerprints=MappingProxyType(dict(self.control_fingerprints)),
            binder_fingerprints=MappingProxyType(dict(self.binder_fingerprints)),
            binder_refs=MappingProxyType(dict(self.binder_refs)),
            handler_env_fingerprints=MappingProxyType(dict(self.handler_env_fingerprints)),
            handler_env_refs=MappingProxyType(dict(self.handler_env_refs)),
            install_fingerprints_by_node=MappingProxyType(dict(self.install_fingerprints_by_node)),
            install_refs_by_node=MappingProxyType(dict(self.install_refs_by_node)),
            install_fingerprints_by_object_id=MappingProxyType(dict(self.install_fingerprints_by_object_id)),
            install_refs_by_object_id=MappingProxyType(dict(self.install_refs_by_object_id)),
            schemas_fingerprint=schemas_fingerprint,
            schema_ref_fingerprints=MappingProxyType(dict(self.schema_ref_fingerprints)),
        )

    def _project_control(self, node: NodeId) -> None:
        control = self.index.control_nodes[node]
        if isinstance(control, KPure):
            payload: object = {"control": "pure", "expr": self._expr_fingerprint(control.expr)}
        elif isinstance(control, KBind):
            payload = {
                "control": "bind",
                "bound": self.control_fingerprints[self._control_node(control.bound)],
                "binder_ref": self.binder_refs[control.binder_id],
            }
        elif isinstance(control, KPerform):
            payload = {
                "control": "perform",
                "effect_kind": control.effect_kind,
                "payload": self._expr_fingerprint(control.payload),
                "payload_schema_ref": control.payload_schema_ref,
                "payload_schema": self._schema_ref_fingerprint(control.payload_schema_ref),
                "operation_result_schema_ref": control.operation_result_schema_ref,
                "operation_result_schema": self._schema_ref_fingerprint(control.operation_result_schema_ref),
            }
        elif isinstance(control, KHandle):
            payload = {
                "control": "handle",
                "body": self.control_fingerprints[self._control_node(control.body)],
                "handler_env_ref": self.handler_env_refs[control.handler_env_ref],
                "region_ref": control.region_ref,
            }
        elif isinstance(control, KResumeWith):
            payload = {"control": "resume-with", "value": self._expr_fingerprint(control.value)}
        elif isinstance(control, KAbort):
            payload = {"control": "abort", "value": self._expr_fingerprint(control.value)}
        elif isinstance(control, KForward):
            payload = {"control": "forward"}
        elif isinstance(control, KTerminalDelay):
            payload = {
                "control": "terminal-delay",
                "reason": self._expr_fingerprint(control.reason),
            }
        elif isinstance(control, KTerminalFork):
            payload = {
                "control": "terminal-fork",
                "branches": tuple(
                    (branch_ref, self._expr_fingerprint(value)) for branch_ref, value in control.branches
                ),
            }
        else:
            raise TypeError(f"unknown kernel control: {control!r}")
        self.control_fingerprints[node] = payload

    def _project_binder(self, binder_ref: Ref) -> None:
        binder = self.program.binders[binder_ref]
        payload = {
            "param_name": binder.param_name,
            "body": self.control_fingerprints[self._control_node(binder.body)],
        }
        self.binder_fingerprints[binder_ref] = payload
        self.binder_refs[binder_ref] = content_ref("binder", payload)

    def _project_handler_env(self, handler_env_ref: Ref) -> None:
        handler_env = self.program.handler_envs[handler_env_ref]
        payload = {
            "bindings": tuple(
                self.install_fingerprints_by_node[self._install_node(handler_env_ref, idx)]
                for idx, _install in enumerate(handler_env.bindings)
            ),
        }
        self.handler_env_fingerprints[handler_env_ref] = payload
        self.handler_env_refs[handler_env_ref] = content_ref("handler-env", payload)

    def _project_install(self, node: NodeId) -> None:
        handler_env_ref = node[1]
        binding_index = node[2]
        if not isinstance(handler_env_ref, str) or not isinstance(binding_index, int):
            raise RuntimeError(f"malformed install node: {node!r}")
        install = self.program.handler_envs[handler_env_ref].bindings[binding_index]
        payload = {
            "effect_kind": install.effect_kind,
            "handler_id": install.handler_id,
            "handled_result_schema_ref": install.handled_result_schema_ref,
            "handled_result_schema": self._schema_ref_fingerprint(install.handled_result_schema_ref),
            "payload_name": install.payload_name,
            "body": self.control_fingerprints[self._control_node(install.body)],
        }
        install_ref = content_ref("install", payload)
        self.install_fingerprints_by_node[node] = payload
        self.install_refs_by_node[node] = install_ref
        self.install_fingerprints_by_object_id[id(install)] = payload
        self.install_refs_by_object_id[id(install)] = install_ref

    def _schema_ref_fingerprint(self, schema_ref: Ref | None) -> object | None:
        if schema_ref in self.schema_ref_fingerprints:
            return self.schema_ref_fingerprints[schema_ref]
        if schema_ref is None:
            self.schema_ref_fingerprints[schema_ref] = None
            return None
        schema_def = self.program.schemas.get(schema_ref)
        if schema_def is None:
            raise RuntimeError(f"kernel schema ref {schema_ref!r} is missing from program.schemas")
        payload: object = schema_fingerprint(schema_def.schema)
        self.schema_ref_fingerprints[schema_ref] = payload
        return payload

    def _control_node(self, control: KComputation) -> NodeId:
        return ("control", id(control))

    def _install_node(self, handler_env_ref: Ref, binding_index: int) -> NodeId:
        return ("install", handler_env_ref, binding_index)

    def _expr_fingerprint(self, expr: object) -> object:
        match expr:
            case Lit(value):
                return {"expr": "lit", "value": value}
            case Var(name):
                return {"expr": "var", "name": name}
            case RecordExpr(fields):
                return {
                    "expr": "record",
                    "fields": tuple((name, self._expr_fingerprint(value)) for name, value in fields),
                }
            case _:
                raise TypeError(f"unknown expression form: {expr!r}")
