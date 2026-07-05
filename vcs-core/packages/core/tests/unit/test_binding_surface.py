"""Tests for the metadata-first binding surface read model."""

from __future__ import annotations

import sys
import types

import pytest
from vcs_core import discovery
from vcs_core._binding_contracts import BindingContractError, BindingContractResolver
from vcs_core._binding_surface import BindingSurface
from vcs_core.config import VcsCoreConfig
from vcs_core.manifest import SubstrateManifest
from vcs_core.spi import CapabilitySet, CommandRequest, DriverSchema
from vcs_core.store import Store
from vcs_core.types import BoundSubstrate
from vcs_core.vcscore import VcsCore

from ..support.drivers import PlainCommandDriver


class _LegacyEchoSubstrate:
    name = "legacy_echo"

    def activate(self) -> None:
        pass

    def deactivate(self) -> None:
        pass

    def authority(self):
        return {}


def test_collect_binding_specs_lists_driver_kind_without_importing_implementation(
    tmp_path,
    monkeypatch,
) -> None:
    impl_module_name = "_lazy_plain_driver_impl"
    sys.modules.pop(impl_module_name, None)
    real_discover = discovery.discover_plugin_registrations

    def patched_discover(*, strict: bool = True):
        available = dict(real_discover(strict=strict))
        available["test.plain_driver"] = discovery.DiscoveredSubstrate(
            name="test.plain_driver",
            module_name=impl_module_name,
            class_name="PlainCommandDriver",
            source="plugin",
            manifest=SubstrateManifest(name="test.plain_driver"),
            entry_point_name="test.plain_driver",
            implementation_kind="driver",
        )
        return available

    monkeypatch.setattr(discovery, "discover_plugin_registrations", patched_discover)

    specs = discovery.collect_binding_specs(
        VcsCoreConfig(bindings={"runtime": {"type": "test.plain_driver"}}),
        tmp_path,
    )

    runtime = next(spec for spec in specs if spec.binding_name == "runtime")
    assert runtime.substrate_type == "test.plain_driver"
    assert runtime.implementation_kind == "driver"
    assert runtime.binding_source == "configured"
    assert runtime.configured is True
    assert runtime.module_name == impl_module_name
    assert impl_module_name not in sys.modules


def test_binding_surface_records_metadata_and_resolver_loads_live_driver_schema() -> None:
    driver = PlainCommandDriver()
    specs = (
        discovery.BindingSpec(
            binding_name="runtime",
            substrate_type=driver.driver_id,
            config={},
            binding_source="configured",
            configured=True,
            manifest=SubstrateManifest(name=driver.driver_id),
            implementation_kind="driver",
            registration_source="plugin",
            module_name="tests.support.drivers",
            class_name="PlainCommandDriver",
        ),
    )
    surface = BindingSurface(
        specs=specs,
        live_bindings=[
            BoundSubstrate(binding_name="runtime", substrate_type=driver.driver_id, instance=driver),
        ],
    )

    assert surface.names() == ("runtime",)
    record = surface.get("runtime")
    assert record.implementation_kind == "driver"
    assert record.live is True
    assert record.configured is True
    assert not hasattr(surface, "exec")
    assert not hasattr(surface, "execute")
    assert not hasattr(surface, "schema")

    resolver = BindingContractResolver(
        specs=specs,
        live_bindings=[
            BoundSubstrate(binding_name="runtime", substrate_type=driver.driver_id, instance=driver),
        ],
    )
    schema = resolver.schema("runtime")
    assert schema.driver_id == driver.driver_id
    assert tuple(schema.commands) == ("echo",)
    assert resolver.schema("runtime") is schema


@pytest.mark.xfail(
    reason="caching regression under always-on carrier — see #11",
    strict=True,
)
def test_vcscore_exec_uses_cached_resolved_binding_contract_without_runtime_describe(tmp_path) -> None:
    class CountingDriver(PlainCommandDriver):
        def __init__(self) -> None:
            self.describe_calls = 0

        def describe(self) -> DriverSchema:
            self.describe_calls += 1
            return super().describe()

    driver = CountingDriver()
    vcscore = VcsCore(str(tmp_path), substrates=[driver])
    vcscore.activate()
    try:
        task = vcscore.fork(vcscore.ground, "runtime-contract-cache")

        first = vcscore.exec("plain", "echo", scope=task, message="first")
        second = vcscore.exec("plain", "echo", scope=task, message="second")

        assert first.value.diagnostics[0].message == "first"
        assert second.value.diagnostics[0].message == "second"
        assert driver.describe_calls == 1
    finally:
        vcscore.deactivate()


def test_c1_c4_binding_surface_seam_contract(tmp_path, monkeypatch) -> None:
    impl_module_name = "_test_c1_c4_seam_driver"
    driver_id = "test.c1_c4_seam_driver"
    sys.modules.pop(impl_module_name, None)
    real_discover = discovery.discover_plugin_registrations

    def patched_discover(*, strict: bool = True):
        available = dict(real_discover(strict=strict))
        available[driver_id] = discovery.DiscoveredSubstrate(
            name=driver_id,
            module_name=impl_module_name,
            class_name="SeamDriver",
            source="plugin",
            manifest=SubstrateManifest(name=driver_id),
            entry_point_name=driver_id,
            implementation_kind="driver",
        )
        return available

    monkeypatch.setattr(discovery, "discover_plugin_registrations", patched_discover)
    config = VcsCoreConfig(bindings={"runtime": {"type": driver_id}})

    specs = discovery.collect_binding_specs(config, tmp_path)
    metadata_surface = BindingSurface(specs=specs)
    runtime = metadata_surface.get("runtime")

    assert runtime.implementation_kind == "driver"
    assert runtime.substrate_type == driver_id
    assert impl_module_name not in sys.modules
    assert not hasattr(metadata_surface, "exec")
    assert not hasattr(metadata_surface, "execute")
    assert not hasattr(metadata_surface, "schema")

    driver_module = types.ModuleType(impl_module_name)

    class SeamDriver(PlainCommandDriver):
        pass

    SeamDriver.driver_id = driver_id
    monkeypatch.setitem(sys.modules, impl_module_name, driver_module)
    driver_module.SeamDriver = SeamDriver  # type: ignore[attr-defined]

    store = Store(str(tmp_path / ".vcscore"))
    bindings = discovery.resolve_bindings(config, tmp_path, store)
    vcscore = VcsCore(str(tmp_path), bindings=bindings, store=store)

    assert "runtime" in vcscore.binding_surface.names()
    assert all(getattr(substrate, "driver_id", None) != driver_id for substrate in vcscore.lifecycle_substrates)
    assert vcscore.binding_contracts.schema("runtime").commands["echo"].params["message"].type == "str"


def test_binding_surface_rejects_live_non_driver_schema() -> None:
    legacy = _LegacyEchoSubstrate()
    resolver = BindingContractResolver(
        live_bindings=[
            BoundSubstrate(binding_name="legacy", substrate_type=legacy.name, instance=legacy),
        ],
    )

    with pytest.raises(BindingContractError, match="does not implement SubstrateDriver"):
        resolver.schema("legacy")


def test_vcscore_binding_surface_infers_live_driver_kind(tmp_path) -> None:
    driver = PlainCommandDriver()
    vcscore = VcsCore(str(tmp_path), substrates=[driver])

    surface = vcscore.binding_surface

    assert surface.names() == ("plain",)
    record = surface.get("plain")
    assert record.implementation_kind == "driver"
    assert record.binding_source == "live"
    assert record.registration_source == "live"
    assert vcscore.binding_contracts.schema("plain").commands["echo"].params["message"].type == "str"


def test_direct_driver_invalid_schema_fails_at_binding_resolution(tmp_path) -> None:
    class BadSchemaDriver(PlainCommandDriver):
        def describe(self) -> DriverSchema:
            schema = super().describe()
            return DriverSchema(
                driver_id=schema.driver_id,
                driver_version=schema.driver_version,
                capabilities=schema.capabilities,
                commands={"echo": {"description": "not a CommandSpec"}},  # type: ignore[dict-item]
            )

    vcscore = VcsCore(str(tmp_path), substrates=[BadSchemaDriver()])

    with pytest.raises(BindingContractError, match="invalid driver schema"):
        vcscore.binding_contracts.schema("plain")


def test_direct_driver_schema_driver_id_mismatch_fails_before_dispatch(tmp_path) -> None:
    class MismatchedSchemaDriver(PlainCommandDriver):
        def describe(self) -> DriverSchema:
            schema = super().describe()
            return DriverSchema(
                driver_id="test.other_driver",
                driver_version=schema.driver_version,
                capabilities=schema.capabilities,
                commands=schema.commands,
            )

    vcscore = VcsCore(str(tmp_path), substrates=[MismatchedSchemaDriver()])

    with pytest.raises(BindingContractError, match="schema driver_id must match substrate type"):
        vcscore.binding_contracts.schema("plain")


def test_direct_driver_live_driver_id_mismatch_fails_before_dispatch() -> None:
    class MismatchedLiveDriverId(PlainCommandDriver):
        driver_id = "test.live_driver"

        def describe(self) -> DriverSchema:
            schema = super().describe()
            return DriverSchema(
                driver_id="test.bound_driver",
                driver_version=schema.driver_version,
                capabilities=schema.capabilities,
                commands=schema.commands,
            )

    driver = MismatchedLiveDriverId()
    resolver = BindingContractResolver(
        live_bindings=[
            BoundSubstrate(binding_name="runtime", substrate_type="test.bound_driver", instance=driver),
        ],
    )

    with pytest.raises(BindingContractError, match="driver_id must match substrate type"):
        resolver.schema("runtime")


def test_direct_driver_schema_version_mismatch_fails_before_dispatch(tmp_path) -> None:
    class MismatchedSchemaVersionDriver(PlainCommandDriver):
        def describe(self) -> DriverSchema:
            schema = super().describe()
            return DriverSchema(
                driver_id=schema.driver_id,
                driver_version="v2",
                capabilities=schema.capabilities,
                commands=schema.commands,
            )

    vcscore = VcsCore(str(tmp_path), substrates=[MismatchedSchemaVersionDriver()])

    with pytest.raises(BindingContractError, match="schema driver_version must match live driver driver_version"):
        vcscore.binding_contracts.schema("plain")


def test_direct_driver_schema_capabilities_mismatch_fails_before_dispatch(tmp_path) -> None:
    class MismatchedSchemaCapabilitiesDriver(PlainCommandDriver):
        @property
        def capabilities(self) -> CapabilitySet:
            return CapabilitySet(accepts=frozenset({CommandRequest}), selectable=True)

        def describe(self) -> DriverSchema:
            schema = super().describe()
            return DriverSchema(
                driver_id=schema.driver_id,
                driver_version=schema.driver_version,
                capabilities=CapabilitySet(accepts=frozenset({CommandRequest}), selectable=False),
                commands=schema.commands,
            )

    vcscore = VcsCore(str(tmp_path), substrates=[MismatchedSchemaCapabilitiesDriver()])

    with pytest.raises(BindingContractError, match="schema capabilities must match live driver capabilities"):
        vcscore.binding_contracts.schema("plain")


def test_vcscore_exec_uses_resolved_schema_capabilities_after_resolution(tmp_path) -> None:
    class MutableCapabilitiesDriver(PlainCommandDriver):
        def __init__(self) -> None:
            self.live_capabilities = CapabilitySet(accepts=frozenset({CommandRequest}), selectable=False)

        @property
        def capabilities(self) -> CapabilitySet:
            return self.live_capabilities

    driver = MutableCapabilitiesDriver()
    vcscore = VcsCore(str(tmp_path), substrates=[driver])
    vcscore.activate()
    try:
        task = vcscore.fork(vcscore.ground, "resolved-capability-cache")
        vcscore.binding_contracts.schema("plain")
        driver.live_capabilities = CapabilitySet(accepts=frozenset(), selectable=True)

        outcome = vcscore.exec("plain", "echo", scope=task, message="cached")

        assert outcome.value.diagnostics[0].message == "cached"
    finally:
        vcscore.deactivate()
