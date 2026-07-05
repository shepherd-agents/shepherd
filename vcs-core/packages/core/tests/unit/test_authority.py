"""Tests for runtime substrate authority reporting."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from vcs_core import VcsCore
from vcs_core._substrate_runtime import build_builtin_substrate_context
from vcs_core.authority import (
    AuthorityAspect,
    AuthorityValidationError,
    SubstrateAuthority,
    derive_authority_level,
    make_authority_aspect,
    validate_authority_report,
)
from vcs_core.cli import main
from vcs_core.store import Store
from vcs_core.substrates import FilesystemSubstrate, MarkerSubstrate
from vcs_core.types import BoundSubstrate

from ..support.overlays import NoOpOverlayBackend


def test_derive_authority_level() -> None:
    assert derive_authority_level(regime="complete", access_gated=True) == "authoritative"
    assert derive_authority_level(regime="partial", access_gated=False) == "best-effort"
    assert derive_authority_level(regime="none", access_gated=False) == "cooperative"


def test_marker_reports_cooperative_authority(store: Store) -> None:  # type: ignore[no-untyped-def]
    marker = MarkerSubstrate(build_builtin_substrate_context(store))

    assert marker.authority() == SubstrateAuthority(
        substrate="marker",
        containment=AuthorityAspect(
            regime="none",
            access_gated=False,
            tier="recording",
            reason="Markers do not gate or isolate external state changes.",
        ),
        provenance=AuthorityAspect(
            regime="none",
            access_gated=False,
            tier="recording",
            reason="Marker effects exist only when explicitly emitted by the caller.",
        ),
        reason="Marker effects are recorded only when explicitly emitted by the caller.",
    )


def test_filesystem_store_mode_reports_best_effort_authority(store: Store) -> None:  # type: ignore[no-untyped-def]
    fs = FilesystemSubstrate(build_builtin_substrate_context(store))

    assert fs.authority() == SubstrateAuthority(
        substrate="filesystem",
        containment=AuthorityAspect(
            regime="none",
            access_gated=False,
            tier="python",
            reason="Python interception does not gate or isolate filesystem access.",
        ),
        provenance=AuthorityAspect(
            regime="partial",
            access_gated=False,
            tier="python",
            reason="Filesystem capture relies on Python interception and can be bypassed by non-Python writes.",
        ),
        reason="Filesystem substrate provides partial provenance without authoritative containment.",
    )


def test_filesystem_overlay_mode_reports_split_authority(store: Store) -> None:  # type: ignore[no-untyped-def]
    fs = FilesystemSubstrate(build_builtin_substrate_context(store), backend=NoOpOverlayBackend())

    assert fs.authority() == SubstrateAuthority(
        substrate="filesystem",
        containment=AuthorityAspect(
            regime="complete",
            access_gated=True,
            tier="container",
            reason="Overlay-backed sessions gate filesystem writes and preserve authoritative final state before materialization.",
        ),
        provenance=AuthorityAspect(
            regime="partial",
            access_gated=True,
            tier="container",
            reason="Overlay-backed sessions preserve final state, but canonical low-level filesystem history remains partial until direct capture covers all mutation paths.",
        ),
        reason="Filesystem substrate provides authoritative containment with partial low-level provenance.",
    )


def test_vcscore_coverage_returns_runtime_reports(tmp_path: Path) -> None:
    Store(str(tmp_path / ".vcscore")).create_root_commit()
    mg = VcsCore.from_config(str(tmp_path))
    mg.activate()
    try:
        reports = {report.substrate: report for report in mg.coverage()}
    finally:
        mg.deactivate()

    # PR#4 made the portable copy carrier a universal backend floor, so the
    # default filesystem substrate now always has containment (was "none" when
    # no carrier was auto-resolved).
    assert reports["filesystem"].containment.regime == "complete"
    assert reports["filesystem"].provenance.regime == "partial"
    assert reports["marker"].containment.regime == "none"
    assert reports["marker"].provenance.regime == "none"


def test_cli_coverage_reports_runtime_state(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = CliRunner()
    result = runner.invoke(main, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(main, ["coverage"])

    assert result.exit_code == 0, result.output
    assert "Contain" in result.output
    assert "Gated" in result.output
    assert "Prov" in result.output
    assert "filesystem" in result.output
    assert "partial" in result.output
    assert "marker" in result.output
    assert "without authoritative containment" in result.output


def test_validate_authority_report_rejects_mismatched_substrate_name() -> None:
    report = SubstrateAuthority(
        substrate="other",
        containment=make_authority_aspect(
            regime="partial",
            access_gated=False,
            tier="recording",
            reason="Containment reason.",
        ),
        provenance=make_authority_aspect(
            regime="partial",
            access_gated=False,
            tier="recording",
            reason="Provenance reason.",
        ),
        reason="Summary reason.",
    )

    with pytest.raises(AuthorityValidationError, match="must report substrate='marker'"):
        validate_authority_report("marker", report)


def test_authority_aspect_derives_level() -> None:
    aspect = AuthorityAspect(
        regime="complete",
        access_gated=True,
        tier="container",
        reason="Containment reason.",
    )

    assert aspect.level == "authoritative"


def test_authority_aspect_rejects_invalid_regime() -> None:
    with pytest.raises(AuthorityValidationError, match="regime must be one of"):
        AuthorityAspect(
            regime="bogus",  # type: ignore[arg-type]
            access_gated=True,
            tier="container",
            reason="Containment reason.",
        )


def test_authority_aspect_rejects_non_bool_access_gated() -> None:
    with pytest.raises(AuthorityValidationError, match="access_gated must be bool"):
        AuthorityAspect(
            regime="partial",
            access_gated="yes",  # type: ignore[arg-type]
            tier="recording",
            reason="Containment reason.",
        )


def test_authority_aspect_rejects_invalid_tier() -> None:
    with pytest.raises(AuthorityValidationError, match="tier must be one of"):
        AuthorityAspect(
            regime="partial",
            access_gated=False,
            tier="bogus",  # type: ignore[arg-type]
            reason="Containment reason.",
        )


def test_vcscore_coverage_rejects_invalid_runtime_report(tmp_path: Path) -> None:
    class InvalidAuthoritySubstrate:
        name = "invalid-authority"
        commands = {}
        effects = {}

        def activate(self) -> None:
            pass

        def deactivate(self) -> None:
            pass

        def authority(self):
            return None

    mg = VcsCore(str(tmp_path), substrates=[InvalidAuthoritySubstrate()])  # type: ignore[list-item]
    mg.activate()
    try:
        with pytest.raises(AuthorityValidationError, match="must return SubstrateAuthority, got NoneType"):
            mg.coverage()
    finally:
        mg.deactivate()


def test_vcscore_coverage_validates_against_bound_substrate_type(tmp_path: Path) -> None:
    class WrongNameSubstrate:
        name = "actual-name"
        commands = {}
        effects = {}

        def activate(self) -> None:
            pass

        def deactivate(self) -> None:
            pass

        def authority(self):
            return SubstrateAuthority(
                substrate="actual-name",
                containment=make_authority_aspect(
                    regime="partial",
                    access_gated=False,
                    tier="recording",
                    reason="Containment reason.",
                ),
                provenance=make_authority_aspect(
                    regime="partial",
                    access_gated=False,
                    tier="recording",
                    reason="Provenance reason.",
                ),
                reason="Summary reason.",
            )

    binding = BoundSubstrate(
        binding_name="expected-name",
        substrate_type="expected-name",
        instance=WrongNameSubstrate(),
    )
    mg = VcsCore(str(tmp_path), bindings=[binding])
    mg.activate()
    try:
        with pytest.raises(AuthorityValidationError, match="must report substrate='expected-name'"):
            mg.coverage()
    finally:
        mg.deactivate()
