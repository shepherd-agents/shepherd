"""Equivalence guard for the lifecycle-provider inventory.

The substrate-CLI foundations plan (`260614-0130-substrate-cli-foundations-plan.md`, A2c)
narrows `mg.lifecycle_substrates` to lifecycle providers so lifecycle / materialization / recovery
paths stop assuming every bound instance exposes scalar command hooks. The review asked
for an *equivalence* gate — the materialized artifact must be byte-identical whether or
not a plain SPI driver is present in the inventory — rather than only a "doesn't crash"
smoke.

This module is that gate:

  * `test_materializer_set_inert_to_plain_driver` — materialization already skips a
    non-`InternalMaterializerProvider` driver (the safe half).
  * `test_push_snapshot_deterministic` — the byte-identical snapshot primitive.
  * `test_prebound_driver_admitted_to_bindings_but_not_lifecycle_substrates` — a pre-bound
    driver enters the all-binding inventory and `BindingSurface`, but not the narrowed
    lifecycle-provider view.
  * `test_mixed_inventory_push_byte_identical` — a driver's presence in the all-binding
    inventory must not perturb lifecycle/materialization output.

Grounded in `spikes/260614-a2c-equivalence/` (FINDINGS.md, spike.py).
"""

from __future__ import annotations

import hashlib
import stat
from typing import TYPE_CHECKING, Any

from vcs_core.materialization import build_materializers
from vcs_core.spi import CapabilitySet, DriverSchema
from vcs_core.vcscore import VcsCore

from ..support.builders import make_marker_filesystem_substrates, make_store
from ..support.drivers import PlainCommandDriver
from ..support.overlays import MockOverlayBackend

if TYPE_CHECKING:
    from pathlib import Path


class _StubBoundJournalDriver:
    """A pre-bound, zero-config, journal-only D0-cone driver (selectable=False).

    A plain duck object, not a ``BaseSubstrateDriver`` subclass: the base defaults
    ``binding=""`` until discovery assigns it, so a raw driver isn't injectable via
    ``substrates=`` without a pre-set ``.binding``. Deliberately carries **none** of the
    lifecycle hooks (``.activate``/``.deactivate``/``.branch``/``.materializers``/
    ``.commit_merge``/``.discard``) — those are exactly what A2c must stop calling on a
    driver. ``.binding`` is the bind-gate discriminator (``vcscore.py:132``).
    """

    binding = "journal_stub"
    driver_id = "test.stub_journal"
    driver_version = "0"
    role = "journal_stub"

    @property
    def capabilities(self) -> CapabilitySet:
        return CapabilitySet(accepts=frozenset(), selectable=False)

    def describe(self) -> DriverSchema:
        return DriverSchema(
            driver_id=self.driver_id,
            driver_version=self.driver_version,
            capabilities=self.capabilities,
        )


class _NamedPlainDriver(PlainCommandDriver):
    """A valid SPI driver with an incidental `.name` attribute."""

    binding = "named_plain"
    driver_id = "test.named_plain_driver"
    name = "lifecycle-looking-name"


def _snapshot(workspace: Path) -> dict[str, tuple[str, str]]:
    """relpath -> (sha256, octal-mode) for every materialized file, excluding the store."""
    out: dict[str, tuple[str, str]] = {}
    for f in sorted(workspace.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(workspace).as_posix()
        if rel.startswith(".vcscore/") or "/.vcscore/" in rel:
            continue
        data = f.read_bytes()
        out[rel] = (hashlib.sha256(data).hexdigest(), oct(stat.S_IMODE(f.stat().st_mode)))
    return out


def _push_workload(workspace: Path, *, extra: tuple[object, ...] = ()) -> dict[str, tuple[str, str]]:
    """Activate → isolated fork → two writes → merge → push; return the materialized snapshot."""
    workspace.mkdir(parents=True, exist_ok=True)
    store = make_store(workspace)
    marker, filesystem = make_marker_filesystem_substrates(store, declarative=False, backend=MockOverlayBackend())
    # Heterogeneous by design: lifecycle providers + (optionally) a plain driver.
    inventory: list[Any] = [marker, filesystem, *extra]
    vcscore = VcsCore(str(workspace), substrates=inventory, store=store)
    vcscore.activate()
    try:
        task = vcscore.fork(vcscore.ground, "task", hints={"isolated": True})
        vcscore.exec("filesystem", "write", scope=task, path="a/hello.txt", content=b"payload-A\n")
        vcscore.exec("filesystem", "write", scope=task, path="b/world.txt", content=b"payload-B\n")
        vcscore.merge(task, vcscore.ground)
        vcscore.push()
        return _snapshot(workspace)
    finally:
        vcscore.deactivate()


def test_materializer_set_inert_to_plain_driver(workspace: Path) -> None:
    """`build_materializers` must yield the same set with or without a plain driver."""
    store = make_store(workspace)
    marker, filesystem = make_marker_filesystem_substrates(store, declarative=True)
    mixed_inventory: list[Any] = [marker, filesystem, _StubBoundJournalDriver()]
    lifecycle = sorted(m.materializer_key for m in build_materializers([marker, filesystem]))
    mixed = sorted(m.materializer_key for m in build_materializers(mixed_inventory))
    assert lifecycle, "expected at least the builtin:filesystem materializer"
    assert mixed == lifecycle


def test_push_snapshot_deterministic(tmp_path: Path) -> None:
    """The byte-identical snapshot primitive is stable across two independent pushes."""
    first = _push_workload(tmp_path / "ws1")
    second = _push_workload(tmp_path / "ws2")
    assert first, "expected a non-empty materialized snapshot"
    assert first == second


def test_prebound_driver_admitted_to_bindings_but_not_lifecycle_substrates(workspace: Path) -> None:
    """A pre-bound `.binding` driver remains all-binding inventory, not lifecycle inventory."""
    store = make_store(workspace)
    marker, filesystem = make_marker_filesystem_substrates(store, declarative=True)
    driver = _StubBoundJournalDriver()
    inventory: list[Any] = [marker, filesystem, driver]
    vcscore = VcsCore(str(workspace), substrates=inventory, store=store)
    assert any(binding.instance is driver for binding in vcscore.bindings)
    assert "journal_stub" in vcscore.binding_surface.names()
    assert all(substrate is not driver for substrate in vcscore.lifecycle_substrates)
    assert all(substrate is not driver for substrate in vcscore.lifecycle_substrates)


def test_named_spi_driver_prefers_binding_and_stays_lifecycle_inert(workspace: Path) -> None:
    """A structural driver with `.name` is still a driver, not a lifecycle substrate."""
    store = make_store(workspace)
    marker, filesystem = make_marker_filesystem_substrates(store, declarative=True)
    driver = _NamedPlainDriver()

    vcscore = VcsCore(str(workspace), substrates=[marker, filesystem, driver], store=store)

    assert any(
        binding.instance is driver
        and binding.binding_name == "named_plain"
        and binding.substrate_type == "test.named_plain_driver"
        for binding in vcscore.bindings
    )
    assert "named_plain" in vcscore.binding_surface.names()
    assert "lifecycle-looking-name" not in vcscore.binding_surface.names()
    assert all(substrate is not driver for substrate in vcscore.lifecycle_substrates)
    assert all(substrate is not driver for substrate in vcscore.lifecycle_substrates)

    vcscore.activate()
    try:
        assert [report.substrate for report in vcscore.coverage()] == ["marker", "filesystem"]
    finally:
        vcscore.deactivate()


def test_mixed_inventory_push_byte_identical(tmp_path: Path) -> None:
    """A driver's PRESENCE in the inventory must not change materialization output by one byte."""
    lifecycle = _push_workload(tmp_path / "lifecycle")
    mixed = _push_workload(tmp_path / "mixed", extra=(_StubBoundJournalDriver(),))
    assert mixed == lifecycle


def test_coverage_reports_lifecycle_authority_only(workspace: Path) -> None:
    """Coverage/authority reporting excludes driver bindings until drivers declare authority."""
    store = make_store(workspace)
    marker, filesystem = make_marker_filesystem_substrates(store, declarative=True)
    driver = _StubBoundJournalDriver()
    vcscore = VcsCore(str(workspace), substrates=[marker, filesystem, driver], store=store)
    vcscore.activate()
    try:
        assert [report.substrate for report in vcscore.coverage()] == ["marker", "filesystem"]
    finally:
        vcscore.deactivate()
