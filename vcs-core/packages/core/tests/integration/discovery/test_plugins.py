"""Plugin and protocol-validation discovery tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from vcs_core._substrate_runtime import BuiltInSubstrateContext
from vcs_core.config import VcsCoreConfig
from vcs_core.discovery import (
    SubstrateResolutionError,
    discover_manifests,
    discover_plugin_registrations,
    instantiate_substrate_class,
    resolve_bindings,
)
from vcs_core.manifest import SubstrateManifest, SubstratePlugin
from vcs_core.spi import (
    BaseSubstrateDriver,
    CapabilitySet,
    CommandRequest,
    CommandSpec,
    Diagnostic,
    DriverIngressResult,
    DriverSchema,
    ParamSpec,
)
from vcs_core.store import Store
from vcs_core.vcscore import VcsCore

from ...support.drivers import PlainCommandDriver

if TYPE_CHECKING:
    from pathlib import Path


def test_instantiate_substrate_class_uses_split_public_builtin_and_driver_construction(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / ".vcscore"
    repo_path.mkdir()
    store = Store(str(repo_path))
    store.create_root_commit()

    seen_routes: list[str] = []

    class BuiltInSubstrate:
        def __init__(self, ctx) -> None:  # type: ignore[no-untyped-def]
            seen_routes.append(type(ctx).__name__)
            assert isinstance(ctx, BuiltInSubstrateContext)
            assert ctx.store is store

    class DriverSubstrate:
        def __init__(self, *args: object) -> None:
            seen_routes.append("driver-zero-arg")
            assert args == ()

    instantiate_substrate_class(
        BuiltInSubstrate,
        source="built-in",
        implementation_kind="driver",
        workspace=tmp_path,
        store=store,
        config={"built_in": True},
    )
    instantiate_substrate_class(
        DriverSubstrate,
        source="plugin",
        implementation_kind="driver",
        workspace=tmp_path,
        store=store,
        config={},
    )

    assert seen_routes == ["BuiltInSubstrateContext", "driver-zero-arg"]


def test_driver_kind_construction_rejects_binding_config(tmp_path: Path) -> None:
    class DriverSubstrate:
        def __init__(self) -> None:
            pass

    with pytest.raises(ValueError, match="driver-kind substrates are stateless"):
        instantiate_substrate_class(
            DriverSubstrate,
            source="plugin",
            implementation_kind="driver",
            workspace=tmp_path,
            store=object(),
            config={"unexpected": True},
        )


def test_resolve_rejects_non_protocol_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    from vcs_core import discovery

    fake_module = types.ModuleType("_test_bad_substrate")

    class NotASubstrate:
        def __init__(self) -> None:
            pass

    fake_module.NotASubstrate = NotASubstrate  # type: ignore[attr-defined]
    sys.modules["_test_bad_substrate"] = fake_module

    real_discover = discovery.discover_plugin_registrations

    def patched_discover(*, strict: bool = True):
        del strict
        available = dict(real_discover())
        available["bad-plugin"] = discovery.DiscoveredSubstrate(
            name="bad-plugin",
            module_name="_test_bad_substrate",
            class_name="NotASubstrate",
            source="plugin",
            manifest=SubstrateManifest(name="bad-plugin"),
        )
        return available

    monkeypatch.setattr(discovery, "discover_plugin_registrations", patched_discover)

    repo_path = tmp_path / ".vcscore"
    repo_path.mkdir()
    store = Store(str(repo_path))
    store.create_root_commit()

    config = VcsCoreConfig(bindings={"bad-plugin": {"type": "bad-plugin"}})

    with pytest.raises(SubstrateResolutionError, match="does not implement"):
        resolve_bindings(config, tmp_path, store)


def test_resolve_catches_constructor_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core import discovery

    real_discover = discovery.discover_plugin_registrations

    import sys
    import types

    failing_module = types.ModuleType("_test_failing_substrate")

    class FailingInit:
        def __init__(self) -> None:
            raise RuntimeError("constructor exploded")

    failing_module.FailingInit = FailingInit  # type: ignore[attr-defined]
    sys.modules["_test_failing_substrate"] = failing_module

    def patched_discover(*, strict: bool = True):
        del strict
        available = dict(real_discover())
        available["exploding"] = discovery.DiscoveredSubstrate(
            name="exploding",
            module_name="_test_failing_substrate",
            class_name="FailingInit",
            source="plugin",
            manifest=SubstrateManifest(name="exploding"),
        )
        return available

    monkeypatch.setattr(discovery, "discover_plugin_registrations", patched_discover)

    repo_path = tmp_path / ".vcscore"
    repo_path.mkdir()
    store = Store(str(repo_path))
    store.create_root_commit()

    config = VcsCoreConfig(bindings={"exploding": {"type": "exploding"}})
    with pytest.raises(SubstrateResolutionError, match="constructor exploded"):
        resolve_bindings(config, tmp_path, store)


def test_resolve_rejects_plugin_with_invalid_command_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core import discovery

    real_discover = discovery.discover_plugin_registrations

    import sys
    import types

    bad_schema_module = types.ModuleType("_test_bad_command_schema")

    class BadCommandSchema(PlainCommandDriver):
        driver_id = "bad-command-schema"

        def describe(self) -> DriverSchema:
            return DriverSchema(
                driver_id=self.driver_id,
                driver_version=self.driver_version,
                capabilities=self.capabilities,
                commands={"run": {"description": "not-a-CommandSpec"}},  # type: ignore[dict-item]
            )

    bad_schema_module.BadCommandSchema = BadCommandSchema  # type: ignore[attr-defined]
    sys.modules["_test_bad_command_schema"] = bad_schema_module

    def patched_discover(*, strict: bool = True):
        del strict
        available = dict(real_discover())
        available["bad-command-schema"] = discovery.DiscoveredSubstrate(
            name="bad-command-schema",
            module_name="_test_bad_command_schema",
            class_name="BadCommandSchema",
            source="plugin",
            manifest=SubstrateManifest(name="bad-command-schema"),
        )
        return available

    monkeypatch.setattr(discovery, "discover_plugin_registrations", patched_discover)

    repo_path = tmp_path / ".vcscore"
    repo_path.mkdir()
    store = Store(str(repo_path))
    store.create_root_commit()

    config = VcsCoreConfig(bindings={"bad-command-schema": {"type": "bad-command-schema"}})
    with pytest.raises(SubstrateResolutionError, match="invalid driver schema"):
        resolve_bindings(config, tmp_path, store)


def test_resolve_rejects_plugin_with_invalid_driver_param_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vcs_core import discovery

    real_discover = discovery.discover_plugin_registrations

    import sys
    import types

    bad_schema_module = types.ModuleType("_test_bad_param_schema")

    class BadParamSchema(PlainCommandDriver):
        driver_id = "bad-param-schema"

        def describe(self) -> DriverSchema:
            return DriverSchema(
                driver_id=self.driver_id,
                driver_version=self.driver_version,
                capabilities=self.capabilities,
                commands={"run": CommandSpec(description="run", params={"bad": ParamSpec(type="str??")})},
            )

    bad_schema_module.BadParamSchema = BadParamSchema  # type: ignore[attr-defined]
    sys.modules["_test_bad_param_schema"] = bad_schema_module

    def patched_discover(*, strict: bool = True):
        del strict
        available = dict(real_discover())
        available["bad-param-schema"] = discovery.DiscoveredSubstrate(
            name="bad-param-schema",
            module_name="_test_bad_param_schema",
            class_name="BadParamSchema",
            source="plugin",
            manifest=SubstrateManifest(name="bad-param-schema"),
        )
        return available

    monkeypatch.setattr(discovery, "discover_plugin_registrations", patched_discover)

    repo_path = tmp_path / ".vcscore"
    repo_path.mkdir()
    store = Store(str(repo_path))
    store.create_root_commit()

    config = VcsCoreConfig(bindings={"bad-param-schema": {"type": "bad-param-schema"}})
    with pytest.raises(SubstrateResolutionError, match="invalid driver schema"):
        resolve_bindings(config, tmp_path, store)


def test_resolve_accepts_plugin_with_valid_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core import discovery

    real_discover = discovery.discover_plugin_registrations

    import sys
    import types

    valid_module = types.ModuleType("_test_valid_schema")

    class ValidSchemaDriver(PlainCommandDriver):
        driver_id = "valid-schema"

    valid_module.ValidSchemaDriver = ValidSchemaDriver  # type: ignore[attr-defined]
    sys.modules["_test_valid_schema"] = valid_module

    def patched_discover(*, strict: bool = True):
        del strict
        available = dict(real_discover())
        available["valid-schema"] = discovery.DiscoveredSubstrate(
            name="valid-schema",
            module_name="_test_valid_schema",
            class_name="ValidSchemaDriver",
            source="plugin",
            manifest=SubstrateManifest(name="valid-schema"),
        )
        return available

    monkeypatch.setattr(discovery, "discover_plugin_registrations", patched_discover)

    repo_path = tmp_path / ".vcscore"
    repo_path.mkdir()
    store = Store(str(repo_path))
    store.create_root_commit()

    config = VcsCoreConfig(bindings={"valid-schema": {"type": "valid-schema"}})
    bindings = resolve_bindings(config, tmp_path, store)

    assert any(binding.binding_name == "valid-schema" for binding in bindings)


def test_discover_plugin_registration_bundles_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    from vcs_core import discovery

    plugin_module = types.ModuleType("_test_bundled_plugin")
    plugin_module.plugin = SubstratePlugin(
        name="bundled-plugin",
        substrate=("_test_valid_schema", "ValidSchemaDriver"),
        manifest=SubstrateManifest(
            name="bundled-plugin",
            tier="explicit",
            description="Bundled plugin manifest",
            depends_on=("marker",),
        ),
    )
    sys.modules["_test_bundled_plugin"] = plugin_module

    class _FakeEntryPoint:
        def __init__(self, name: str, value: str, loaded: object) -> None:
            self.name = name
            self.value = value
            self._loaded = loaded

        def load(self) -> object:
            return self._loaded

    def fake_entry_points(*, group: str):  # type: ignore[no-untyped-def]
        if group == discovery.PLUGIN_ENTRY_POINT_GROUP:
            return [_FakeEntryPoint("bundled-plugin", "_test_bundled_plugin:plugin", plugin_module.plugin)]
        return []

    monkeypatch.setattr(discovery, "entry_points", fake_entry_points)

    registrations = discover_plugin_registrations()
    manifests = discover_manifests()

    assert registrations["bundled-plugin"].source == "plugin"
    assert registrations["bundled-plugin"].entry_point_name == "bundled-plugin"
    assert registrations["bundled-plugin"].implementation_kind == "driver"
    assert manifests["bundled-plugin"].name == "bundled-plugin"
    assert manifests["bundled-plugin"].depends_on == ("marker",)


def test_discover_plugin_registration_carries_driver_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    from vcs_core import discovery

    plugin_module = types.ModuleType("_test_driver_kind_plugin")
    plugin_module.plugin = SubstratePlugin(
        name="driver-kind-plugin",
        substrate=("_test_driver_kind_plugin_impl", "DriverKind"),
        manifest=SubstrateManifest(name="driver-kind-plugin"),
        implementation_kind="driver",
    )
    sys.modules["_test_driver_kind_plugin"] = plugin_module

    class _FakeEntryPoint:
        def __init__(self, name: str, loaded: object) -> None:
            self.name = name
            self._loaded = loaded

        def load(self) -> object:
            return self._loaded

    def fake_entry_points(*, group: str):  # type: ignore[no-untyped-def]
        if group == discovery.PLUGIN_ENTRY_POINT_GROUP:
            return [_FakeEntryPoint("driver-kind-plugin", plugin_module.plugin)]
        return []

    monkeypatch.setattr(discovery, "entry_points", fake_entry_points)

    registrations = discover_plugin_registrations()

    assert registrations["driver-kind-plugin"].implementation_kind == "driver"


def test_substrate_plugin_rejects_mismatched_manifest_name() -> None:
    with pytest.raises(ValueError, match=r"SubstratePlugin name must match manifest.name"):
        SubstratePlugin(
            name="plugin-name",
            substrate=("_test_plugin_name_mismatch", "Mismatch"),
            manifest=SubstrateManifest(name="manifest-name"),
        )


def test_substrate_plugin_rejects_unknown_implementation_kind() -> None:
    with pytest.raises(ValueError, match="implementation_kind"):
        SubstratePlugin(
            name="plugin-name",
            substrate=("_test_plugin_unknown_kind", "UnknownKind"),
            manifest=SubstrateManifest(name="plugin-name"),
            implementation_kind="unknown",  # type: ignore[arg-type]
        )


def test_resolve_rejects_driver_kind_plugin_with_mismatched_driver_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vcs_core import discovery

    real_discover = discovery.discover_plugin_registrations

    import sys
    import types

    driver_module = types.ModuleType("_test_wrong_driver_id")

    @dataclass(frozen=True)
    class WrongDriverId(BaseSubstrateDriver):
        driver_id: str = "actual-driver"
        driver_version: str = "0"

        @property
        def capabilities(self) -> CapabilitySet:
            return CapabilitySet(accepts=frozenset())

        def describe(self) -> DriverSchema:
            return DriverSchema(
                driver_id=self.driver_id,
                driver_version=self.driver_version,
                capabilities=self.capabilities,
            )

        def prepare(self, context: Any, request: Any) -> DriverIngressResult:
            del context, request
            return DriverIngressResult()

    driver_module.WrongDriverId = WrongDriverId  # type: ignore[attr-defined]
    sys.modules["_test_wrong_driver_id"] = driver_module

    def patched_discover(*, strict: bool = True):
        del strict
        available = dict(real_discover())
        available["expected-driver"] = discovery.DiscoveredSubstrate(
            name="expected-driver",
            module_name="_test_wrong_driver_id",
            class_name="WrongDriverId",
            source="plugin",
            manifest=SubstrateManifest(name="expected-driver"),
            implementation_kind="driver",
        )
        return available

    monkeypatch.setattr(discovery, "discover_plugin_registrations", patched_discover)

    repo_path = tmp_path / ".vcscore"
    repo_path.mkdir()
    store = Store(str(repo_path))
    store.create_root_commit()

    config = VcsCoreConfig(bindings={"expected-driver": {"type": "expected-driver"}})
    with pytest.raises(SubstrateResolutionError, match="driver_id='expected-driver'"):
        resolve_bindings(config, tmp_path, store)


def test_resolve_admits_driver_kind_binding_and_exec_reaches_spi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vcs_core import discovery

    real_discover = discovery.discover_plugin_registrations

    import sys
    import types

    driver_module = types.ModuleType("_test_configured_driver")

    @dataclass(frozen=True)
    class ConfiguredDriver(BaseSubstrateDriver):
        driver_id: str = "test.configured_driver"
        driver_version: str = "0"
        store_id: str = "store_runtime"
        binding: str = "runtime"
        role: str = "test.ConfiguredDriver"
        materialization_class: str = "noop"

        @property
        def capabilities(self) -> CapabilitySet:
            return CapabilitySet(accepts=frozenset({CommandRequest}), selectable=False, journal_only=True)

        def describe(self) -> DriverSchema:
            return DriverSchema(
                driver_id=self.driver_id,
                driver_version=self.driver_version,
                capabilities=self.capabilities,
                commands={
                    "echo": CommandSpec(
                        description="Echo one value through the SPI dispatch arm.",
                        params={"message": ParamSpec(type="str")},
                    )
                },
            )

        def prepare(self, context: Any, request: Any) -> DriverIngressResult:
            return DriverIngressResult(
                diagnostics=(
                    Diagnostic(
                        code="test.configured_driver.echo",
                        message=request.params["message"],
                        detail={"binding": context.binding},
                    ),
                )
            )

    driver_module.ConfiguredDriver = ConfiguredDriver  # type: ignore[attr-defined]
    sys.modules["_test_configured_driver"] = driver_module

    def patched_discover(*, strict: bool = True):
        del strict
        available = dict(real_discover())
        available["test.configured_driver"] = discovery.DiscoveredSubstrate(
            name="test.configured_driver",
            module_name="_test_configured_driver",
            class_name="ConfiguredDriver",
            source="plugin",
            manifest=SubstrateManifest(name="test.configured_driver"),
            implementation_kind="driver",
        )
        return available

    monkeypatch.setattr(discovery, "discover_plugin_registrations", patched_discover)

    repo_path = tmp_path / ".vcscore"
    repo_path.mkdir()
    store = Store(str(repo_path))
    store.create_root_commit()

    config = VcsCoreConfig(bindings={"runtime": {"type": "test.configured_driver"}})
    bindings = resolve_bindings(config, tmp_path, store)
    runtime_binding = next(binding for binding in bindings if binding.binding_name == "runtime")
    assert runtime_binding.substrate_type == "test.configured_driver"
    assert isinstance(runtime_binding.instance, ConfiguredDriver)

    vcscore = VcsCore(str(tmp_path), bindings=bindings, store=store)
    vcscore.activate()
    try:
        outcome = vcscore.exec("runtime", "echo", scope=vcscore.ground, message="hello")
    finally:
        vcscore.deactivate()

    assert isinstance(outcome.value, DriverIngressResult)
    assert outcome.value.diagnostics == (
        Diagnostic(code="test.configured_driver.echo", message="hello", detail={"binding": "runtime"}),
    )


def test_resolve_rejects_plugin_with_non_schema_describe_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vcs_core import discovery

    real_discover = discovery.discover_plugin_registrations

    import sys
    import types

    bad_schema_module = types.ModuleType("_test_non_schema_describe")

    class NonSchemaDescribeDriver(PlainCommandDriver):
        driver_id = "non-schema-describe"

        def describe(self) -> DriverSchema:
            return {"driver_id": self.driver_id}  # type: ignore[return-value]

    bad_schema_module.NonSchemaDescribeDriver = NonSchemaDescribeDriver  # type: ignore[attr-defined]
    sys.modules["_test_non_schema_describe"] = bad_schema_module

    def patched_discover(*, strict: bool = True):
        del strict
        available = dict(real_discover())
        available["non-schema-describe"] = discovery.DiscoveredSubstrate(
            name="non-schema-describe",
            module_name="_test_non_schema_describe",
            class_name="NonSchemaDescribeDriver",
            source="plugin",
            manifest=SubstrateManifest(name="non-schema-describe"),
        )
        return available

    monkeypatch.setattr(discovery, "discover_plugin_registrations", patched_discover)

    repo_path = tmp_path / ".vcscore"
    repo_path.mkdir()
    store = Store(str(repo_path))
    store.create_root_commit()

    config = VcsCoreConfig(bindings={"non-schema-describe": {"type": "non-schema-describe"}})

    with pytest.raises(SubstrateResolutionError, match="invalid driver schema"):
        resolve_bindings(config, tmp_path, store)


def test_resolve_rejects_plugin_with_mismatched_schema_driver_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vcs_core import discovery

    real_discover = discovery.discover_plugin_registrations

    import sys
    import types

    mismatched_schema_module = types.ModuleType("_test_mismatched_schema_driver_id")

    class MismatchedSchemaDriverId(PlainCommandDriver):
        driver_id = "mismatched-schema"

        def describe(self) -> DriverSchema:
            return DriverSchema(
                driver_id="someone-else",
                driver_version=self.driver_version,
                capabilities=self.capabilities,
                commands=self.describe_commands(),
            )

        def describe_commands(self) -> dict[str, CommandSpec]:
            return PlainCommandDriver.describe(self).commands

    mismatched_schema_module.MismatchedSchemaDriverId = MismatchedSchemaDriverId  # type: ignore[attr-defined]
    sys.modules["_test_mismatched_schema_driver_id"] = mismatched_schema_module

    def patched_discover(*, strict: bool = True):
        del strict
        available = dict(real_discover())
        available["mismatched-schema"] = discovery.DiscoveredSubstrate(
            name="mismatched-schema",
            module_name="_test_mismatched_schema_driver_id",
            class_name="MismatchedSchemaDriverId",
            source="plugin",
            manifest=SubstrateManifest(name="mismatched-schema"),
        )
        return available

    monkeypatch.setattr(discovery, "discover_plugin_registrations", patched_discover)

    repo_path = tmp_path / ".vcscore"
    repo_path.mkdir()
    store = Store(str(repo_path))
    store.create_root_commit()

    config = VcsCoreConfig(bindings={"mismatched-schema": {"type": "mismatched-schema"}})

    with pytest.raises(SubstrateResolutionError, match="Driver schema must report driver_id='mismatched-schema'"):
        resolve_bindings(config, tmp_path, store)


def test_resolve_rejects_plugin_with_mismatched_driver_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core import discovery

    real_discover = discovery.discover_plugin_registrations

    import sys
    import types

    wrong_name_module = types.ModuleType("_test_mismatched_driver_id")

    class WrongDriverId(PlainCommandDriver):
        driver_id = "actual-name"

    wrong_name_module.WrongDriverId = WrongDriverId  # type: ignore[attr-defined]
    sys.modules["_test_mismatched_driver_id"] = wrong_name_module

    def patched_discover(*, strict: bool = True):
        del strict
        available = dict(real_discover())
        available["expected-name"] = discovery.DiscoveredSubstrate(
            name="expected-name",
            module_name="_test_mismatched_driver_id",
            class_name="WrongDriverId",
            source="plugin",
            manifest=SubstrateManifest(name="expected-name"),
        )
        return available

    monkeypatch.setattr(discovery, "discover_plugin_registrations", patched_discover)

    repo_path = tmp_path / ".vcscore"
    repo_path.mkdir()
    store = Store(str(repo_path))
    store.create_root_commit()

    config = VcsCoreConfig(bindings={"expected-name": {"type": "expected-name"}})

    with pytest.raises(SubstrateResolutionError, match="driver_id='expected-name'"):
        resolve_bindings(config, tmp_path, store)


def test_lenient_discovery_skips_broken_plugin_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core import discovery

    class _BrokenEntryPoint:
        def __init__(self, name: str) -> None:
            self.name = name

        def load(self) -> object:
            raise RuntimeError("broken plugin import")

    def fake_entry_points(*, group: str):  # type: ignore[no-untyped-def]
        if group == discovery.PLUGIN_ENTRY_POINT_GROUP:
            return [_BrokenEntryPoint("broken-plugin")]
        return []

    monkeypatch.setattr(discovery, "entry_points", fake_entry_points)

    registrations = discover_plugin_registrations(strict=False)
    manifests = discover_manifests(strict=False)

    assert "broken-plugin" not in registrations
    assert "filesystem" in manifests
    assert "git" in manifests


def test_strict_discovery_reports_broken_plugin_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core import discovery

    class _BrokenEntryPoint:
        def __init__(self, name: str) -> None:
            self.name = name

        def load(self) -> object:
            raise RuntimeError("broken plugin import")

    def fake_entry_points(*, group: str):  # type: ignore[no-untyped-def]
        if group == discovery.PLUGIN_ENTRY_POINT_GROUP:
            return [_BrokenEntryPoint("broken-plugin")]
        return []

    monkeypatch.setattr(discovery, "entry_points", fake_entry_points)

    with pytest.raises(SubstrateResolutionError, match="broken plugin import"):
        discover_plugin_registrations(strict=True)


def test_lenient_discovery_skips_plugin_manifest_identity_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core import discovery

    class _BrokenEntryPoint:
        name = "broken-plugin"

        def load(self) -> object:
            return SubstratePlugin(
                name="plugin-name",
                substrate=("_test_plugin_identity", "Plugin"),
                manifest=SubstrateManifest(name="manifest-name"),
            )

    def fake_entry_points(*, group: str):  # type: ignore[no-untyped-def]
        if group == discovery.PLUGIN_ENTRY_POINT_GROUP:
            return [_BrokenEntryPoint()]
        return []

    monkeypatch.setattr(discovery, "entry_points", fake_entry_points)

    registrations = discover_plugin_registrations(strict=False)
    manifests = discover_manifests(strict=False)

    assert "plugin-name" not in registrations
    assert "plugin-name" not in manifests
    assert "manifest-name" not in manifests


def test_strict_discovery_reports_plugin_manifest_identity_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core import discovery

    class _BrokenEntryPoint:
        name = "broken-plugin"

        def load(self) -> object:
            return SubstratePlugin(
                name="plugin-name",
                substrate=("_test_plugin_identity", "Plugin"),
                manifest=SubstrateManifest(name="manifest-name"),
            )

    def fake_entry_points(*, group: str):  # type: ignore[no-untyped-def]
        if group == discovery.PLUGIN_ENTRY_POINT_GROUP:
            return [_BrokenEntryPoint()]
        return []

    monkeypatch.setattr(discovery, "entry_points", fake_entry_points)

    with pytest.raises(SubstrateResolutionError, match=r"SubstratePlugin name must match manifest.name"):
        discover_plugin_registrations(strict=True)


def test_resolve_uses_bundled_plugin_manifest_dependencies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    from vcs_core import discovery

    valid_module = types.ModuleType("_test_manifest_dep_valid")

    class ManifestDependentDriver(PlainCommandDriver):
        driver_id = "manifest-dependent"

    valid_module.ManifestDependentDriver = ManifestDependentDriver  # type: ignore[attr-defined]
    sys.modules["_test_manifest_dep_valid"] = valid_module

    plugin = SubstratePlugin(
        name="manifest-dependent",
        substrate=("_test_manifest_dep_valid", "ManifestDependentDriver"),
        manifest=SubstrateManifest(
            name="manifest-dependent",
            depends_on=("missing-helper",),
        ),
    )

    class _FakeEntryPoint:
        def __init__(self, name: str, value: str, loaded: object) -> None:
            self.name = name
            self.value = value
            self._loaded = loaded

        def load(self) -> object:
            return self._loaded

    def fake_entry_points(*, group: str):  # type: ignore[no-untyped-def]
        if group == discovery.PLUGIN_ENTRY_POINT_GROUP:
            return [_FakeEntryPoint("manifest-dependent", "_test_manifest_dep_valid:plugin", plugin)]
        return []

    monkeypatch.setattr(discovery, "entry_points", fake_entry_points)

    repo_path = tmp_path / ".vcscore"
    repo_path.mkdir()
    store = Store(str(repo_path))
    store.create_root_commit()

    config = VcsCoreConfig(bindings={"manifest-dependent": {"type": "manifest-dependent"}})
    with pytest.raises(
        SubstrateResolutionError,
        match=r"depends on substrate type 'missing-helper' which is not active",
    ):
        resolve_bindings(config, tmp_path, store)


def test_strict_discovery_rejects_duplicate_builtin_registration_names(monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core import discovery

    plugin = SubstratePlugin(
        name="filesystem",
        substrate=("_test_duplicate_builtin", "FilesystemDuplicate"),
        manifest=SubstrateManifest(name="filesystem"),
    )

    class _FakeEntryPoint:
        def __init__(self, name: str, loaded: object) -> None:
            self.name = name
            self._loaded = loaded

        def load(self) -> object:
            return self._loaded

    def fake_entry_points(*, group: str):  # type: ignore[no-untyped-def]
        if group == discovery.PLUGIN_ENTRY_POINT_GROUP:
            return [_FakeEntryPoint("filesystem-shadow", plugin)]
        return []

    monkeypatch.setattr(discovery, "entry_points", fake_entry_points)

    with pytest.raises(SubstrateResolutionError, match=r"Duplicate substrate registration 'filesystem'"):
        discover_plugin_registrations(strict=True)


def test_strict_discovery_rejects_duplicate_plugin_registration_names(monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core import discovery

    first = SubstratePlugin(
        name="duplicate-plugin",
        substrate=("_test_duplicate_plugin_a", "DuplicateA"),
        manifest=SubstrateManifest(name="duplicate-plugin"),
    )
    second = SubstratePlugin(
        name="duplicate-plugin",
        substrate=("_test_duplicate_plugin_b", "DuplicateB"),
        manifest=SubstrateManifest(name="duplicate-plugin"),
    )

    class _FakeEntryPoint:
        def __init__(self, name: str, loaded: object) -> None:
            self.name = name
            self._loaded = loaded

        def load(self) -> object:
            return self._loaded

    def fake_entry_points(*, group: str):  # type: ignore[no-untyped-def]
        if group == discovery.PLUGIN_ENTRY_POINT_GROUP:
            return [
                _FakeEntryPoint("duplicate-plugin-a", first),
                _FakeEntryPoint("duplicate-plugin-b", second),
            ]
        return []

    monkeypatch.setattr(discovery, "entry_points", fake_entry_points)

    with pytest.raises(SubstrateResolutionError, match=r"Duplicate substrate registration 'duplicate-plugin'"):
        discover_plugin_registrations(strict=True)


def test_resolve_reports_plugin_module_import_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core import discovery

    real_discover = discovery.discover_plugin_registrations

    def patched_discover(*, strict: bool = True):
        del strict
        available = dict(real_discover())
        available["missing-module"] = discovery.DiscoveredSubstrate(
            name="missing-module",
            module_name="_test_missing_substrate_module",
            class_name="MissingSubstrate",
            source="plugin",
            manifest=SubstrateManifest(name="missing-module"),
            entry_point_name="missing-module-plugin",
        )
        return available

    monkeypatch.setattr(discovery, "discover_plugin_registrations", patched_discover)

    repo_path = tmp_path / ".vcscore"
    repo_path.mkdir()
    store = Store(str(repo_path))
    store.create_root_commit()

    config = VcsCoreConfig(bindings={"missing-module": {"type": "missing-module"}})
    with pytest.raises(
        SubstrateResolutionError,
        match=r"failed to import substrate module '_test_missing_substrate_module'",
    ):
        resolve_bindings(config, tmp_path, store)


def test_resolve_reports_missing_plugin_classes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    from vcs_core import discovery

    real_discover = discovery.discover_plugin_registrations
    empty_module = types.ModuleType("_test_missing_substrate_class")
    sys.modules["_test_missing_substrate_class"] = empty_module

    def patched_discover(*, strict: bool = True):
        del strict
        available = dict(real_discover())
        available["missing-class"] = discovery.DiscoveredSubstrate(
            name="missing-class",
            module_name="_test_missing_substrate_class",
            class_name="MissingSubstrate",
            source="plugin",
            manifest=SubstrateManifest(name="missing-class"),
            entry_point_name="missing-class-plugin",
        )
        return available

    monkeypatch.setattr(discovery, "discover_plugin_registrations", patched_discover)

    repo_path = tmp_path / ".vcscore"
    repo_path.mkdir()
    store = Store(str(repo_path))
    store.create_root_commit()

    config = VcsCoreConfig(bindings={"missing-class": {"type": "missing-class"}})
    with pytest.raises(
        SubstrateResolutionError,
        match=r"could not resolve substrate class 'MissingSubstrate'",
    ):
        resolve_bindings(config, tmp_path, store)
