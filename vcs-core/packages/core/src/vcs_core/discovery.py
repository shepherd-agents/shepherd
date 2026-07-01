"""Substrate discovery and instantiation.

Combines built-in substrates + entry point discovery + config-driven
resolution into the activation sequence.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, Literal

from vcs_core._driver_schema_validation import DriverSchemaValidationError, validate_driver_schema
from vcs_core._substrate_runtime import build_builtin_substrate_context
from vcs_core.config import SecretRef, VcsCoreConfig, _deep_merge
from vcs_core.manifest import BUILT_IN_PLUGINS, MANIFESTS, ImplementationKind, SubstrateManifest, SubstratePlugin
from vcs_core.spi import SubstrateDriver
from vcs_core.types import BoundSubstrate

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


PLUGIN_ENTRY_POINT_GROUP = "vcscore.substrate_plugins"


@dataclass(frozen=True)
class DiscoveredSubstrate:
    """Resolved plugin registration used by discovery and CLI surfaces."""

    name: str
    module_name: str
    class_name: str
    source: Literal["built-in", "plugin"]
    manifest: SubstrateManifest
    entry_point_name: str | None = None
    implementation_kind: ImplementationKind = "driver"


@dataclass(frozen=True)
class BindingSpec:
    """Metadata-first active binding description, before implementation import."""

    binding_name: str
    substrate_type: str
    config: dict[str, Any]
    binding_source: Literal["implicit-always", "implicit-auto-detect", "configured", "implicit-configured"]
    configured: bool
    manifest: SubstrateManifest | None
    implementation_kind: ImplementationKind
    registration_source: Literal["built-in", "plugin"] | None
    module_name: str | None = None
    class_name: str | None = None
    entry_point_name: str | None = None


class SubstrateResolutionError(Exception):
    """One or more substrates failed validation during resolution."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


def _validate_plugin_registration_identity(entry_point_name: str, plugin: SubstratePlugin) -> None:
    if not plugin.name:
        raise ValueError(f"Plugin entry point '{entry_point_name}' must declare a non-empty SubstratePlugin.name.")
    if plugin.manifest.name != plugin.name:
        raise ValueError(
            f"Plugin entry point '{entry_point_name}' must keep SubstratePlugin.name and manifest.name aligned; "
            f"got {plugin.name!r} vs {plugin.manifest.name!r}."
        )


def instantiate_substrate_class(
    cls: type[Any],
    *,
    source: Literal["built-in", "plugin"],
    implementation_kind: ImplementationKind = "driver",
    workspace: Path,
    store: Any,
    config: dict[str, Any],
) -> object:
    """Instantiate one substrate class through the correct boundary."""
    if source == "built-in":
        return cls(build_builtin_substrate_context(store=store, workspace=workspace, config=config))
    if implementation_kind == "driver":
        if config:
            raise ValueError("driver-kind substrates are stateless in v1 and do not accept binding config.")
        return cls()
    raise ValueError(f"unsupported substrate implementation_kind {implementation_kind!r}; expected 'driver'.")


def _coerce_plugin_spec(name: str, loaded: object) -> SubstratePlugin:
    if isinstance(loaded, SubstratePlugin):
        return loaded
    if callable(loaded):
        created = loaded()
        if isinstance(created, SubstratePlugin):
            return created
    raise TypeError(f"Plugin entry point '{name}' must load a SubstratePlugin or a zero-arg factory returning one.")


def _registration_origin(registration: DiscoveredSubstrate) -> str:
    if registration.source == "built-in":
        return "built-in registration"
    if registration.entry_point_name is not None:
        return f"plugin entry point '{registration.entry_point_name}'"
    return "plugin registration"


def _validate_driver_identity(expected_name: str, instance: SubstrateDriver) -> None:
    driver_id = instance.driver_id
    if isinstance(driver_id, str) and driver_id == expected_name:
        return
    raise ValueError(f"resolved driver instance must report driver_id='{expected_name}', got {driver_id!r}.")


def _duplicate_registration_error(
    *,
    name: str,
    existing: DiscoveredSubstrate,
    requested_entry_point_name: str,
    requested_plugin: SubstratePlugin,
) -> str:
    return (
        f"Duplicate substrate registration '{name}': "
        f"{_registration_origin(existing)} already provides "
        f"{existing.module_name}:{existing.class_name}; "
        f"plugin entry point '{requested_entry_point_name}' also provides "
        f"{requested_plugin.substrate[0]}:{requested_plugin.substrate[1]}."
    )


def _discover_plugin_registrations() -> tuple[dict[str, DiscoveredSubstrate], list[str]]:
    """Discover substrate registrations and collect plugin load errors."""
    discovered: dict[str, DiscoveredSubstrate] = {
        name: DiscoveredSubstrate(
            name=name,
            module_name=plugin.substrate[0],
            class_name=plugin.substrate[1],
            source="built-in",
            manifest=plugin.manifest,
            implementation_kind=plugin.implementation_kind,
        )
        for name, plugin in BUILT_IN_PLUGINS.items()
    }
    errors: list[str] = []

    for ep in entry_points(group=PLUGIN_ENTRY_POINT_GROUP):
        try:
            plugin = _coerce_plugin_spec(ep.name, ep.load())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Failed to load substrate plugin entry point '{ep.name}': {exc}")
            continue
        try:
            _validate_plugin_registration_identity(ep.name, plugin)
        except ValueError as exc:
            errors.append(f"Failed to load substrate plugin entry point '{ep.name}': {exc}")
            continue
        plugin_name = plugin.name or ep.name
        existing = discovered.get(plugin_name)
        if existing is not None:
            errors.append(
                _duplicate_registration_error(
                    name=plugin_name,
                    existing=existing,
                    requested_entry_point_name=ep.name,
                    requested_plugin=plugin,
                )
            )
            continue
        discovered[plugin_name] = DiscoveredSubstrate(
            name=plugin_name,
            module_name=plugin.substrate[0],
            class_name=plugin.substrate[1],
            source="plugin",
            manifest=plugin.manifest,
            entry_point_name=ep.name,
            implementation_kind=plugin.implementation_kind,
        )
    return discovered, errors


def discover_plugin_registrations(*, strict: bool = True) -> dict[str, DiscoveredSubstrate]:
    """Discover all substrate registrations.

    In strict mode, broken plugin entry points raise SubstrateResolutionError.
    In lenient mode, broken plugins are skipped and logged.
    """
    discovered, errors = _discover_plugin_registrations()
    if errors:
        if strict:
            raise SubstrateResolutionError(errors)
        for error in errors:
            logger.warning(error)
    return discovered


def discover_substrates(*, strict: bool = True) -> dict[str, tuple[str, str]]:
    """Discover all available substrates as name -> (module, class)."""
    return {
        name: (registration.module_name, registration.class_name)
        for name, registration in discover_plugin_registrations(strict=strict).items()
    }


def discover_manifests(*, strict: bool = False) -> dict[str, SubstrateManifest]:
    """Discover manifest metadata, preserving the built-in catalog.

    The built-in MANIFESTS table is the catalog of known built-ins,
    including planned ones. Bundled plugin registrations may extend that
    catalog with installable implementations; duplicate names are rejected
    in strict mode and ignored in lenient mode.
    """
    manifests = dict(MANIFESTS)
    for name, registration in discover_plugin_registrations(strict=strict).items():
        manifests[name] = registration.manifest
    return manifests


def collect_binding_specs(config: VcsCoreConfig, workspace: Path) -> tuple[BindingSpec, ...]:
    """Collect active binding metadata without importing implementation modules."""
    registrations = discover_plugin_registrations(strict=True)
    manifests = dict(MANIFESTS)
    manifests.update({name: registration.manifest for name, registration in registrations.items()})
    binding_specs, active_types, errors = _collect_binding_specs(config, workspace, registrations, manifests)
    if errors:
        raise SubstrateResolutionError(errors)
    return tuple(_ordered_binding_specs(binding_specs, active_types, manifests))


def _binding_spec(
    *,
    binding_name: str,
    substrate_type: str,
    config: dict[str, Any],
    binding_source: Literal["implicit-always", "implicit-auto-detect", "configured", "implicit-configured"],
    configured: bool,
    registrations: dict[str, DiscoveredSubstrate],
    manifests: dict[str, SubstrateManifest],
) -> BindingSpec:
    registration = registrations.get(substrate_type)
    manifest = manifests.get(substrate_type)
    return BindingSpec(
        binding_name=binding_name,
        substrate_type=substrate_type,
        config=dict(config),
        binding_source=binding_source,
        configured=configured,
        manifest=manifest,
        implementation_kind=registration.implementation_kind if registration is not None else "driver",
        registration_source=registration.source if registration is not None else None,
        module_name=registration.module_name if registration is not None else None,
        class_name=registration.class_name if registration is not None else None,
        entry_point_name=registration.entry_point_name if registration is not None else None,
    )


def _collect_binding_specs(
    config: VcsCoreConfig,
    workspace: Path,
    registrations: dict[str, DiscoveredSubstrate],
    manifests: dict[str, SubstrateManifest],
) -> tuple[dict[str, BindingSpec], set[str], list[str]]:
    binding_specs: dict[str, BindingSpec] = {}
    errors: list[str] = []

    # 1. Always-active implicit bindings (skip planned substrates with no implementation)
    for substrate_type, manifest in manifests.items():
        if manifest.status == "planned":
            continue
        if manifest.tier == "always" and substrate_type in registrations:
            binding_specs[substrate_type] = _binding_spec(
                binding_name=substrate_type,
                substrate_type=substrate_type,
                config={},
                binding_source="implicit-always",
                configured=False,
                registrations=registrations,
                manifests=manifests,
            )

    # 2. Auto-detected implicit bindings (skip planned substrates with no implementation)
    for substrate_type, manifest in manifests.items():
        if manifest.status == "planned":
            continue
        if (
            manifest.tier == "auto-detect"
            and manifest.auto_detect is not None
            and manifest.auto_detect(workspace)
            and substrate_type in registrations
        ):
            binding_specs[substrate_type] = _binding_spec(
                binding_name=substrate_type,
                substrate_type=substrate_type,
                config={},
                binding_source="implicit-auto-detect",
                configured=False,
                registrations=registrations,
                manifests=manifests,
            )

    # 3. Explicitly configured bindings
    for binding_name, binding in config.bindings.items():
        substrate_type = binding.type
        binding_config = binding.binding_options()
        existing = binding_specs.get(binding_name)
        if existing is not None:
            if existing.substrate_type != substrate_type:
                errors.append(
                    "Binding alias "
                    f"{binding_name!r} collides with implicit binding for substrate type "
                    f"{existing.substrate_type!r}."
                )
                continue
            merged_config = dict(existing.config)
            _deep_merge(merged_config, binding_config)
            binding_specs[binding_name] = _binding_spec(
                binding_name=binding_name,
                substrate_type=substrate_type,
                config=merged_config,
                binding_source="implicit-configured",
                configured=True,
                registrations=registrations,
                manifests=manifests,
            )
            continue
        binding_specs[binding_name] = _binding_spec(
            binding_name=binding_name,
            substrate_type=substrate_type,
            config=binding_config,
            binding_source="configured",
            configured=True,
            registrations=registrations,
            manifests=manifests,
        )

    active_types = {spec.substrate_type for spec in binding_specs.values()}

    # 4. Validate installed substrate types
    for substrate_type in active_types:
        if substrate_type in registrations:
            continue
        manifest_for_type = manifests.get(substrate_type)
        if manifest_for_type is None:
            errors.append(
                f"Substrate type '{substrate_type}' is configured but not installed. Install it or remove the binding."
            )
        elif manifest_for_type.status == "planned":
            errors.append(f"Substrate type '{substrate_type}' is known but not yet implemented (status=planned).")
        else:
            errors.append(
                "Substrate type "
                f"'{substrate_type}' is known but no implementation is installed. Install its plugin package or remove the binding."
            )

    # 5. Validate dependencies by substrate type
    for binding_name, spec in binding_specs.items():
        manifest_for_binding = manifests.get(spec.substrate_type)
        if manifest_for_binding is not None:
            for dep in manifest_for_binding.depends_on:
                if dep not in active_types:
                    errors.append(
                        f"Binding '{binding_name}' ({spec.substrate_type}) depends on substrate type '{dep}' which is not active."
                    )

    return binding_specs, active_types, errors


def _ordered_binding_specs(
    binding_specs: dict[str, BindingSpec],
    active_types: set[str],
    manifests: dict[str, SubstrateManifest],
) -> list[BindingSpec]:
    ordered: list[BindingSpec] = []
    bindings_by_type: dict[str, list[BindingSpec]] = {}
    for spec in binding_specs.values():
        bindings_by_type.setdefault(spec.substrate_type, []).append(spec)

    for substrate_type in _topological_sort(active_types, manifests=manifests):
        ordered.extend(sorted(bindings_by_type.get(substrate_type, ()), key=lambda item: item.binding_name))
    return ordered


def resolve_bindings(
    config: VcsCoreConfig,
    workspace: Path,
    store: Any,
) -> list[BoundSubstrate]:
    """Resolve the active binding set from config + auto-detection.

    The 8-step activation sequence from the proposal:
    1. Collect implicit always-active bindings
    2. Add implicit auto-detected bindings
    3. Merge explicitly configured bindings
    4. Validate all configured substrate types are installed
    5. Validate dependencies satisfied
    6. Validate daemon requirements
    7. Resolve secrets
    8. Topologically sort and instantiate
    """
    registrations = discover_plugin_registrations(strict=True)
    manifests = dict(MANIFESTS)
    manifests.update({name: registration.manifest for name, registration in registrations.items()})
    binding_specs, active_types, errors = _collect_binding_specs(config, workspace, registrations, manifests)

    # 6. Validate daemon requirements (skip for now -- daemon is R1b)

    # 7. Resolve secrets
    for binding_name, spec in binding_specs.items():
        for key, value in spec.config.items():
            if isinstance(value, dict) and "env" in value:
                try:
                    SecretRef(**value).resolve()
                except Exception as e:  # noqa: BLE001
                    errors.append(f"Binding '{binding_name}', key '{key}': {e}")

    if errors:
        raise SubstrateResolutionError(errors)

    # 8. Topologically sort active substrate types and instantiate bindings
    sorted_types = _topological_sort(active_types, manifests=manifests)
    bindings: list[BoundSubstrate] = []
    bindings_by_type: dict[str, list[BindingSpec]] = {}
    for spec in binding_specs.values():
        bindings_by_type.setdefault(spec.substrate_type, []).append(spec)

    for substrate_type in sorted_types:
        registration = registrations.get(substrate_type)
        if registration is None:
            continue

        specs = sorted(bindings_by_type.get(substrate_type, ()), key=lambda item: item.binding_name)
        try:
            module = importlib.import_module(registration.module_name)
        except Exception as e:  # noqa: BLE001
            for spec in specs:
                errors.append(
                    f"Binding '{spec.binding_name}' ({spec.substrate_type}) failed to import substrate module "
                    f"'{registration.module_name}' from {_registration_origin(registration)}: {e}"
                )
            continue

        try:
            cls = getattr(module, registration.class_name)
        except AttributeError as e:
            for spec in specs:
                errors.append(
                    f"Binding '{spec.binding_name}' ({spec.substrate_type}) could not resolve substrate class "
                    f"'{registration.class_name}' in module '{registration.module_name}' from "
                    f"{_registration_origin(registration)}: {e}"
                )
            continue

        class_ref = f"{registration.module_name}:{registration.class_name}"
        for spec in specs:
            resolved_config: dict[str, Any] = {}
            for k, v in spec.config.items():
                if isinstance(v, dict) and "env" in v:
                    resolved_config[k] = SecretRef(**v).resolve()
                else:
                    resolved_config[k] = v

            try:
                instance = instantiate_substrate_class(
                    cls,
                    source=registration.source,
                    implementation_kind=registration.implementation_kind,
                    workspace=workspace,
                    store=store,
                    config=resolved_config,
                )
            except Exception as e:  # noqa: BLE001
                errors.append(
                    f"Binding '{spec.binding_name}' ({spec.substrate_type}/{class_ref}) failed to instantiate: {e}"
                )
                continue

            if not isinstance(instance, SubstrateDriver):
                errors.append(
                    f"Binding '{spec.binding_name}' ({spec.substrate_type}/{class_ref}) does not implement the "
                    "SubstrateDriver protocol (missing required methods)."
                )
                continue

            try:
                _validate_driver_identity(spec.substrate_type, instance)
            except ValueError as e:
                errors.append(
                    f"Binding '{spec.binding_name}' ({spec.substrate_type}/{class_ref}) failed identity validation: {e}"
                )
                continue
            try:
                schema = instance.describe()
                validate_driver_schema(schema)
                if schema.driver_id != spec.substrate_type:
                    raise DriverSchemaValidationError(
                        f"Driver schema must report driver_id='{spec.substrate_type}', got {schema.driver_id!r}."
                    )
            except DriverSchemaValidationError as e:
                errors.append(
                    f"Binding '{spec.binding_name}' ({spec.substrate_type}/{class_ref}) has an invalid driver schema: {e}"
                )
                continue

            bindings.append(
                BoundSubstrate(
                    binding_name=spec.binding_name,
                    substrate_type=spec.substrate_type,
                    instance=instance,
                    config=resolved_config,
                )
            )

    if errors:
        raise SubstrateResolutionError(errors)

    return bindings


def _topological_sort_with_manifests(
    names: set[str],
    *,
    manifests: dict[str, SubstrateManifest],
) -> list[str]:
    """Topologically sort substrate names by dependency (Kahn's algorithm)."""
    # Build adjacency
    in_degree: dict[str, int] = dict.fromkeys(names, 0)
    dependents: dict[str, list[str]] = {name: [] for name in names}

    for name in names:
        manifest = manifests.get(name)
        if manifest:
            for dep in manifest.depends_on:
                if dep in names:
                    in_degree[name] += 1
                    dependents[dep].append(name)

    # Kahn's algorithm
    queue = sorted(name for name, deg in in_degree.items() if deg == 0)
    result: list[str] = []
    while queue:
        node = queue.pop(0)
        result.append(node)
        for dependent in sorted(dependents[node]):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    if len(result) != len(names):
        unsorted = sorted(names - set(result))
        raise SubstrateResolutionError([f"Dependency cycle detected among substrates: {', '.join(unsorted)}"])

    return result


def _topological_sort(names: set[str], manifests: dict[str, SubstrateManifest] | None = None) -> list[str]:
    """Topologically sort substrate names by dependency (Kahn's algorithm)."""
    return _topological_sort_with_manifests(names, manifests=MANIFESTS if manifests is None else manifests)


def check_substrate(
    name: str,
    config: VcsCoreConfig,
    workspace: Path,
) -> dict[str, str]:
    """Validate a substrate's configuration and prerequisites.

    Returns a dict of check_name -> status ("ok" or error message).
    """
    results: dict[str, str] = {}

    manifests = discover_manifests()

    # Config validation
    matching_bindings = {
        binding_name: binding
        for binding_name, binding in config.bindings.items()
        if name in {binding_name, binding.type}
    }
    results["config"] = "valid" if name in manifests or matching_bindings else "not configured"
    checked_types = {binding.type for binding in matching_bindings.values()}
    if not checked_types and name in manifests:
        checked_types.add(name)

    # Secret resolution
    for binding_name, binding in matching_bindings.items():
        for key, value in binding.binding_options().items():
            if isinstance(value, dict) and "env" in value:
                label = f"secret:{key}" if len(matching_bindings) == 1 else f"binding:{binding_name}.secret:{key}"
                try:
                    SecretRef(**value).resolve()
                    results[label] = "resolved"
                except Exception as e:  # noqa: BLE001
                    results[label] = f"FAILED: {e}"

    # Dependency check
    active_types = {binding.type for binding in config.bindings.values()}
    for checked_type in sorted(checked_types):
        manifest = manifests.get(checked_type)
        if manifest is None:
            continue
        for dep in manifest.depends_on:
            if dep in active_types or manifests.get(dep, SubstrateManifest(name=dep)).tier == "always":
                results[f"dependency:{dep}"] = "satisfied"
            else:
                results[f"dependency:{dep}"] = f"MISSING: add a binding with type {dep}"

    return results
