"""Fixture loader for `core-reference-v0-lite` differential corpus.

Reconstructs source-AST from the JSON DSL and validates fixtures against
the expected outcome. Per `260524-post-72-design-pass.md` §"Item B".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from shepherd_kernel_v3_reference.schemas import (
    IntSchema,
    LiteralSchema,
    NullSchema,
)
from shepherd_kernel_v3_reference.source.effects import EffectRegistry, EffectSignature
from shepherd_kernel_v3_reference.source.handlers import (
    DynamicHandlerInstall,
    HandlerEnv,
    StaticHandlerInstall,
)
from shepherd_kernel_v3_reference.source.syntax import (
    Abort,
    Computation,
    Expr,
    Handle,
    Let,
    Lit,
    Perform,
    RecordExpr,
    Resume,
    Return,
    Var,
)

if TYPE_CHECKING:
    from pathlib import Path

FIXTURE_SCHEMA_VERSION = "shepherd_kernel_v3_reference.v0_lite_fixture.v1"

VALID_KINDS = frozenset({
    "positive",
    "negative-profile-admission",
    "negative-kernel-admission",
    "negative-runtime-rejection",
    "negative-observation-admission",
    "negative-ref-map",
})


@dataclass(frozen=True)
class Fixture:
    """Loaded fixture; `program` is the reconstructed source AST.

    `observations` carries raw JSON specs (e.g. ``{"value": 42}`` or
    ``{"reuse_index": 0}``); the test runner constructs full
    ``AdmittedObservation`` bundles from these specs against the live
    replay state at each suspend point. ``registry`` is reconstructed
    from ``input.registry`` (an effect-signature list) when present; it
    flows into ``start_kernel_run(..., registry=...)`` so typed-schema
    fixtures (e.g. step-7 admission rejection) can exercise the relevant
    paths.
    """

    path: Path
    case: str
    kind: str
    description: str
    covers: tuple[str, ...]
    program: Computation
    observations: tuple[Any, ...]  # raw JSON specs; runner builds AdmittedObservations
    expected: dict[str, Any]
    registry: EffectRegistry | None = None
    restart_via_serialized: bool = False  # round-trip each request through JSON before resume


def load_fixture(path: Path) -> Fixture:
    """Read and validate a fixture file; reconstruct the source AST."""

    data = json.loads(path.read_text())
    schema_version = data.get("fixture_schema_version")
    if schema_version != FIXTURE_SCHEMA_VERSION:
        raise ValueError(
            f"{path.name}: fixture_schema_version must be {FIXTURE_SCHEMA_VERSION!r}, "
            f"got {schema_version!r}"
        )
    kind = data["kind"]
    if kind not in VALID_KINDS:
        raise ValueError(
            f"{path.name}: kind must be one of {sorted(VALID_KINDS)!r}, got {kind!r}"
        )
    program_json = data["input"]["program"]
    program = _load_computation(program_json)
    observations = tuple(data["input"].get("observations", ()))
    registry_json = data["input"].get("registry")
    registry = _load_registry(registry_json) if registry_json else None
    restart_via_serialized = bool(data["input"].get("restart_via_serialized_artifact", False))
    return Fixture(
        path=path,
        case=data["case"],
        kind=kind,
        description=data["description"],
        covers=tuple(data.get("covers", ())),
        program=program,
        observations=observations,
        expected=data["expected"],
        registry=registry,
        restart_via_serialized=restart_via_serialized,
    )


def _load_registry(data: Any) -> EffectRegistry:
    """Build an EffectRegistry from a list of effect-signature dicts."""

    if not isinstance(data, list):
        raise ValueError(f"expected registry list, got {type(data).__name__}: {data!r}")
    registry = EffectRegistry()
    for sig in data:
        registry.register(EffectSignature(
            effect_kind=sig["effect_kind"],
            payload_schema=_load_schema(sig["payload_schema"]),
            operation_result_schema=_load_schema(sig["operation_result_schema"]),
        ))
    return registry


def iter_fixtures(root: Path, subdir: str = "") -> list[Fixture]:
    """Yield fixtures under root[/subdir], sorted by filename."""

    base = root / subdir if subdir else root
    return [
        load_fixture(p)
        for p in sorted(base.rglob("*.json"))
        if not p.name.startswith("_")
    ]


# ---------------------------------------------------------------------------
# DSL reconstruction
# ---------------------------------------------------------------------------


def _load_expr(data: Any) -> Expr:
    if not isinstance(data, dict):
        raise ValueError(f"expected expression dict, got {type(data).__name__}: {data!r}")
    node = data.get("node")
    if node == "Lit":
        return Lit(value=data["value"])
    if node == "Var":
        return Var(name=data["name"])
    if node == "RecordExpr":
        return RecordExpr(
            fields=tuple((f["name"], _load_expr(f["value"])) for f in data["fields"]),
        )
    raise ValueError(f"unknown expression node: {node!r}")


def _load_computation(data: Any) -> Computation:
    if not isinstance(data, dict):
        raise ValueError(f"expected computation dict, got {type(data).__name__}: {data!r}")
    node = data.get("node")
    if node == "Return":
        return Return(expr=_load_expr(data["expr"]))
    if node == "Let":
        return Let(
            name=data["name"],
            bound=_load_computation(data["bound"]),
            body=_load_computation(data["body"]),
        )
    if node == "Perform":
        return Perform(
            effect_kind=data["effect_kind"],
            payload=_load_expr(data["payload"]),
        )
    if node == "Handle":
        return Handle(
            body=_load_computation(data["body"]),
            handler_env=_load_handler_env(data["handler_env"]),
        )
    if node == "Resume":
        return Resume(value=_load_expr(data["value"]))
    if node == "Abort":
        return Abort(value=_load_expr(data["value"]))
    # Publication-experimental forms (rejected by validate_profile_admission;
    # admitted at construction for negative-fixture coverage of the
    # rejection paths, per README §"input.program — source-AST DSL").
    if node == "Forward":
        from shepherd_kernel_v3_reference.source.experimental import Forward
        return Forward()  # type: ignore[return-value]
    if node == "TerminalDelay":
        from shepherd_kernel_v3_reference.source.experimental import TerminalDelay
        return TerminalDelay(reason=_load_expr(data["reason"]))  # type: ignore[return-value]
    if node == "TerminalFork":
        from shepherd_kernel_v3_reference.source.experimental import TerminalFork
        branches = tuple(
            (b["name"], _load_expr(b["value"])) for b in data["branches"]
        )
        return TerminalFork(branches=branches)  # type: ignore[return-value]
    raise ValueError(f"unknown computation node: {node!r}")


def _load_handler_env(data: Any) -> HandlerEnv:
    if not isinstance(data, dict) or "bindings" not in data:
        raise ValueError(f"expected HandlerEnv dict with 'bindings', got {data!r}")
    bindings: list[Any] = []
    for b in data["bindings"]:
        node = b.get("node")
        if node == "StaticHandlerInstall":
            bindings.append(StaticHandlerInstall(
                effect_kind=b["effect_kind"],
                handler_id=b["handler_id"],
                handled_result_schema=_load_schema(b["handled_result_schema"]),
                payload_name=b["payload_name"],
                body=_load_computation(b["body"]),
            ))
        elif node == "DynamicHandlerInstall":
            # Used by negative fixtures only; body is constructed as a
            # placeholder Python callable since DynamicHandlerInstall
            # carries a Python closure that has no JSON representation.
            bindings.append(DynamicHandlerInstall(
                effect_kind=b["effect_kind"],
                handler_id=b["handler_id"],
                handled_result_schema=_load_schema(b["handled_result_schema"]),
                body=lambda _payload: Return(Lit(None)),
            ))
        else:
            raise ValueError(f"unknown handler install node: {node!r}")
    return HandlerEnv(bindings=tuple(bindings))


def _load_schema(data: Any) -> Any:
    if not isinstance(data, dict):
        raise ValueError(f"expected schema dict, got {type(data).__name__}: {data!r}")
    node = data.get("node")
    if node == "IntSchema":
        return IntSchema()
    if node == "NullSchema":
        return NullSchema()
    if node == "LiteralSchema":
        return LiteralSchema(value=data["value"])
    # Negative-fixture schemas (used only to exercise profile-admission rejection)
    if node == "AnySchema":
        from shepherd_kernel_v3_reference.schemas import AnySchema
        return AnySchema()
    if node == "TypeSchema":
        from shepherd_kernel_v3_reference.schemas import TypeSchema
        # Lookup the named type from a small whitelist
        type_name = data["type"]
        if type_name == "int":
            return TypeSchema(int)
        if type_name == "str":
            return TypeSchema(str)
        raise ValueError(f"unsupported TypeSchema type: {type_name!r}")
    if node == "TaggedRecordSchema":
        from shepherd_kernel_v3_reference.schemas import TaggedRecordSchema
        return TaggedRecordSchema(tag=data["tag"])
    raise ValueError(f"unknown schema node: {node!r}")


__all__ = [
    "FIXTURE_SCHEMA_VERSION",
    "VALID_KINDS",
    "Fixture",
    "iter_fixtures",
    "load_fixture",
]
