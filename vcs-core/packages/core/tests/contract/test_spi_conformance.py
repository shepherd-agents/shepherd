"""Contract tests for the substrate SPI."""

from __future__ import annotations

from pathlib import Path

import pytest
import vcs_core as vcs_core_pkg
from vcs_core import spi
from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._substrate_runtime import ContainmentSubstrate, build_builtin_substrate_context
from vcs_core.spi import (
    SPI_VERSION,
    SUBSTRATE_DRIVER_CONTRACT_REVISION,
    ActiveSurface,
    CapabilitySet,
    CaptureAdapter,
    CommandRequest,
    CommandSpec,
    DriverContext,
    DriverIngressResult,
    DriverSchema,
    IngressRequest,
    ObservationSink,
    ParamSpec,
    ParseResult,
    PayloadDescriptorClaim,
    RelationshipRequirement,
    SubstrateDriver,
    SubstrateStoreIdentity,
    TransitionDraft,
    validate_driver_ingress_result,
)
from vcs_core.substrates import DeclarativeFilesystemSubstrate, FilesystemSubstrate, MarkerSubstrate
from vcs_core.types import FileState

from ..support.spi import (
    BuiltInContainmentScenario,
    DriverCommandScenario,
    assert_built_in_containment_conforms,
    assert_driver_command_effects,
)


class _MemoryOverlayBackend:
    def __init__(self) -> None:
        self.layers: dict[str, dict[str, FileState | None]] = {}
        self.parents: dict[str, str | None] = {}

    def create_layer(self, scope_id: str, *, parent_scope_id: str | None) -> None:
        self.layers.setdefault(scope_id, {})
        self.parents[scope_id] = parent_scope_id

    def has_layer(self, scope_id: str) -> bool:
        return scope_id in self.layers

    def read_file(self, scope_id: str, path: str) -> bytes:
        return self.read_file_state(scope_id, path).content

    def read_file_state(self, scope_id: str, path: str) -> FileState:
        value = self.layers[scope_id][path]
        if value is None:
            raise FileNotFoundError(path)
        return value

    def write_file(self, scope_id: str, path: str, content: bytes, *, mode: int = 0o100644) -> None:
        self.layers.setdefault(scope_id, {})[path] = FileState(content, mode)

    def delete_file(self, scope_id: str, path: str) -> None:
        self.layers.setdefault(scope_id, {})[path] = None

    def diff_layer(self, scope_id: str) -> list[tuple[str, bytes | None, int]]:
        return [
            (p, state.content, state.mode) if state is not None else (p, None, 0)
            for p, state in sorted(self.layers.get(scope_id, {}).items())
        ]

    def commit_layer(self, scope_id: str, *, into_scope_id: str | None) -> None:
        if into_scope_id is None:
            raise RuntimeError("destination required")
        dest = self.layers.setdefault(into_scope_id, {})
        dest.update(self.layers.get(scope_id, {}))
        self.discard_layer(scope_id)

    def discard_layer(self, scope_id: str) -> None:
        self.layers.pop(scope_id, None)
        self.parents.pop(scope_id, None)

    def push_layer(self, scope_id: str | None = None) -> None:
        del scope_id

    def working_path(self, scope_id: str) -> Path:
        return Path("/virtual") / scope_id

    def deactivate(self) -> None:
        self.layers.clear()
        self.parents.clear()


class _MemoryDriver:
    """Minimal SubstrateDriver-conforming test fixture with typed dispatch."""

    driver_id = "test.memory"
    driver_version = "v1"
    capabilities = CapabilitySet(accepts=frozenset({CommandRequest}), selectable=True)

    def describe(self) -> DriverSchema:
        return DriverSchema(
            driver_id=self.driver_id,
            driver_version=self.driver_version,
            capabilities=self.capabilities,
        )

    def prepare(
        self,
        context: DriverContext,
        request: IngressRequest,
    ) -> DriverIngressResult:
        del context
        if isinstance(request, CommandRequest):
            return DriverIngressResult(
                transitions=(
                    TransitionDraft(
                        transition_id="primary",
                        semantic_op=request.command,
                        payload={"schema": "test/memory", "params": dict(request.params)},
                        observation_ids=(),
                    ),
                )
            )
        from vcs_core._substrate_driver import UnsupportedRequestError

        raise UnsupportedRequestError(driver_id=self.driver_id, request_type=type(request))

    def capture_adapters(self, context: DriverContext) -> tuple[()]:
        return ()

    def validate_result(self, request: IngressRequest, result: DriverIngressResult) -> None:
        return None


def test_spi_version_starts_pre_stable() -> None:
    assert SPI_VERSION == 0


def test_spi_schema_types_are_exported_and_constructible() -> None:
    command = CommandSpec(
        description="Write a file",
        params={"path": ParamSpec(type="str", description="Relative path")},
        examples=("vcs-core exec filesystem write --path file.txt",),
    )

    assert command.params["path"].type == "str"
    assert command.examples[0].startswith("vcs-core exec")


def test_spi_driver_contract_is_exported_and_runtime_checkable() -> None:
    driver = _MemoryDriver()

    assert isinstance(driver, SubstrateDriver)
    assert CommandRequest in driver.capabilities.accepts


def test_substrate_driver_contract_revision_pinned() -> None:
    assert SUBSTRATE_DRIVER_CONTRACT_REVISION == "v0.1"


def test_typed_ingress_family_exports_are_dataclasses() -> None:
    from dataclasses import is_dataclass

    from vcs_core.spi import (
        CaptureRequest,
        MergeRequest,
        ReduceRequest,
        ScanRequest,
    )

    for variant in (CommandRequest, ScanRequest, CaptureRequest, ReduceRequest, MergeRequest):
        assert is_dataclass(variant), f"{variant.__name__} must be a frozen dataclass"


def test_capture_adapter_protocol_is_runtime_checkable() -> None:
    class _StubAdapter:
        adapter_id = "stub"
        adapter_version = "v1"
        mechanism = "stub"
        evidence_kinds = ("stub:event",)

        def parse(self, context, raw_events, sink):  # type: ignore[no-untyped-def]
            return ParseResult.skip()

    adapter = _StubAdapter()
    assert isinstance(adapter, CaptureAdapter)


def test_active_surface_supports_dual_allow_deny_polarity() -> None:
    surface = ActiveSurface(
        deny_request_types=frozenset({CommandRequest}),
        deny_evidence_kinds=frozenset({"fs:write"}),
    )
    assert CommandRequest in surface.deny_request_types
    assert "fs:write" in surface.deny_evidence_kinds
    assert surface.allow_request_types is None
    assert surface.allow_evidence_kinds is None


def test_typed_dispatch_drivers_end_match_with_assert_never() -> None:
    """SPI v0.1 §Q1 exhaustiveness discipline: every driver that uses a
    ``match request:`` over ``IngressRequest`` must end with
    ``case _: assert_never(request)``. Without this, a new variant in a
    future v0.x (e.g., ``ReplayRequest``) will silently fall through the
    driver's dispatch instead of producing one mypy error per
    under-implementing driver.

    All four built-in drivers now use ``match`` dispatch (the legacy
    string-dispatch path was removed at T3-final). The same discipline is
    available to out-of-tree drivers as the opt-in
    ``vcs_core.spi.testing.assert_match_dispatch_exhaustive`` (not part of the
    conformance aggregate — ``if`` / ``raise`` dispatch is conformant too).
    """
    import ast
    import inspect

    from vcs_core._world_substrate_adapters import (
        SessionStateSubstrateDriver,
        TaskTraceSubstrateDriver,
        WorkspaceSubstrateDriver,
        WorldRefSubstrateDriver,
    )

    typed_dispatch_drivers = (
        WorkspaceSubstrateDriver,
        TaskTraceSubstrateDriver,
        SessionStateSubstrateDriver,
        WorldRefSubstrateDriver,
    )
    for driver_cls in typed_dispatch_drivers:
        source = inspect.getsource(driver_cls.prepare)
        tree = ast.parse(source.lstrip())
        match_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.Match)]
        assert match_nodes, f"{driver_cls.__name__}.prepare must contain a `match request:` block"
        for match in match_nodes:
            last = match.cases[-1]
            assert isinstance(last.pattern, ast.MatchAs), (
                f"{driver_cls.__name__}.prepare match must end with a wildcard `case _:` arm"
            )
            assert last.pattern.pattern is None, (
                f"{driver_cls.__name__}.prepare match must end with a wildcard `case _:` arm"
            )
            body = last.body
            assert any(
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Name)
                and stmt.value.func.id == "assert_never"
                for stmt in body
            ), f"{driver_cls.__name__}.prepare wildcard arm must call `assert_never(request)`"


def test_observation_sink_protocol_admits_tuple_sink_and_fan_out_sink() -> None:
    from vcs_core.spi import FanOutSink, TupleSink

    tuple_sink = TupleSink()
    fan_out = FanOutSink([tuple_sink])
    assert isinstance(tuple_sink, ObservationSink)
    assert isinstance(fan_out, ObservationSink)


def test_spi_driver_support_types_are_exported_and_constructible() -> None:
    payload = {"schema": "test/memory", "value": 1}
    descriptor = PayloadDescriptorClaim.for_json_payload(payload)
    requirement = RelationshipRequirement(
        binding="memory",
        relation="exact",
        target_binding="workspace",
        target_head="1" * 40,
    )
    identity = SubstrateStoreIdentity(store_id="store_memory", kind="test.memory", resource_id="memory:test")

    assert descriptor.payload_digest
    assert requirement.relation == "exact"
    assert identity.store_id == "store_memory"


def test_spi_driver_validator_rejects_authority_bearing_output() -> None:
    result = DriverIngressResult(
        transitions=(
            TransitionDraft(
                transition_id="primary",
                semantic_op="put",
                payload={"schema": "test/memory"},
                observation_ids=(),
                metadata={"evidence_ref": "refs/vcscore/evidence/op/abc123"},
            ),
        )
    )

    with pytest.raises(InvalidRepositoryStateError, match="reserved authority fields"):
        validate_driver_ingress_result(result)


def test_spi_excludes_internal_runtime_surface() -> None:
    assert not hasattr(spi, "ContainSubstrate")
    assert not hasattr(spi, "MaterializerProvider")
    assert not hasattr(spi, "OverlayBackend")
    assert not hasattr(spi, "CarrierBackend")
    assert not hasattr(spi, "ContainmentBackend")
    assert not hasattr(spi, "PythonPatch")
    assert not hasattr(spi, "BuiltInRuntimeBinding")
    assert not hasattr(spi, "BuiltInSubstrateContext")


def test_package_root_excludes_runtime_pipeline_and_public_init_context() -> None:
    assert not hasattr(vcs_core_pkg, "RecordingPipeline")
    assert not hasattr(vcs_core_pkg, "SubstrateContext")
    assert not hasattr(vcs_core_pkg, "BuiltInRuntimeBinding")
    assert not hasattr(vcs_core_pkg, "BuiltInSubstrateContext")


def test_built_in_substrates_expose_schema_properties(store) -> None:  # type: ignore[no-untyped-def]
    marker = MarkerSubstrate(build_builtin_substrate_context(store))
    filesystem = FilesystemSubstrate(build_builtin_substrate_context(store))

    assert set(marker.describe().commands) == {"mark"}
    assert set(filesystem.describe().commands) == {"write", "read", "delete"}
    assert not hasattr(marker, "effects")
    assert not hasattr(filesystem, "effects")


def test_marker_schema_matches_execute_surface(store) -> None:  # type: ignore[no-untyped-def]
    marker = MarkerSubstrate(build_builtin_substrate_context(store))
    schema = marker.describe()

    assert schema.commands["mark"].params["label"].type == "str"
    assert schema.commands["mark"].params["metadata"].required is False


def test_filesystem_schema_matches_execute_surface(store) -> None:  # type: ignore[no-untyped-def]
    filesystem = FilesystemSubstrate(build_builtin_substrate_context(store))
    schema = filesystem.describe()

    assert schema.commands["write"].params["content"].type == "bytes"
    assert schema.commands["read"].params["path"].type == "str"
    assert schema.commands["delete"].params["path"].type == "str"


def test_marker_substrate_records_driver_command_effects(store) -> None:  # type: ignore[no-untyped-def]
    marker = MarkerSubstrate(build_builtin_substrate_context(store))
    effects = assert_driver_command_effects(
        marker,
        store,
        DriverCommandScenario(
            command="mark",
            params={"label": "spi-checkpoint", "metadata": {"phase": "contract"}},
            expected_effect_types=("Marker",),
        ),
    )
    assert effects[0].metadata["label"] == "spi-checkpoint"
    assert effects[0].metadata["metadata"] == {"phase": "contract"}


def test_filesystem_store_mode_records_driver_command_effects(store) -> None:  # type: ignore[no-untyped-def]
    fs = DeclarativeFilesystemSubstrate(build_builtin_substrate_context(store))
    effects = assert_driver_command_effects(
        fs,
        store,
        DriverCommandScenario(
            command="write",
            params={"path": "spi.txt", "content": b"payload"},
            expected_effect_types=("FileCreate",),
        ),
    )
    assert effects[0].metadata["path"] == "spi.txt"


def test_filesystem_overlay_mode_conforms_to_built_in_containment_runtime(store) -> None:  # type: ignore[no-untyped-def]
    backend = _MemoryOverlayBackend()
    fs = FilesystemSubstrate(build_builtin_substrate_context(store), backend=backend)
    assert isinstance(fs, ContainmentSubstrate)
    effects = assert_built_in_containment_conforms(
        fs,
        store,
        BuiltInContainmentScenario(
            exercise=lambda substrate, scope: substrate.execute(
                "write",
                scope,
                path="overlay.txt",
                content=b"payload",
            ),
            expected_effect_types=("FileCreate",),
            hints={"isolated": True},
        ),
    )
    assert effects[0].metadata["path"] == "overlay.txt"
    assert backend.layers["ground"]["overlay.txt"] == FileState(b"payload")
