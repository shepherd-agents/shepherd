"""B2a — the core `ContainmentBackend` (Seatbelt) lifted move-not-build from the skeleton.

Backend-level unit coverage of the macOS native syscall-deny jail (the device-level
wiring is B3c). macOS-gated; the Linux Landlock member is `_landlock_containment.py`
(B2b). Mirrors the skeleton's proven coverage (spikes/sandbox-jail 8/8 + may=->lowering
15/15): lowering shape, real syscall denial, and the hardened fail-closed conformance probe.
"""

from __future__ import annotations

import os
import sys

import pytest
from vcs_core._containment import ContainmentBackend, JailNotEstablished
from vcs_core._seatbelt_containment import SeatbeltContainmentBackend, lower_to_seatbelt

_macos = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Seatbelt is macOS-only; the Linux Landlock member is covered by _landlock_containment (B2b)",
)


@_macos
def test_seatbelt_available_and_protocol_conformant() -> None:
    backend = SeatbeltContainmentBackend()
    assert isinstance(backend, ContainmentBackend)  # structurally satisfies the protocol
    ok, why = backend.available()
    assert ok, why
    assert backend.name == "seatbelt"
    assert backend.enforcement_tier == "native-syscall-deny"


def _permissive(root) -> tuple[str, ...]:
    return (str(root),)


def test_lower_to_seatbelt_readonly_vs_permissive(tmp_path) -> None:
    """The config-coupling seam: Permissive's writable root == realpath(WORKDIR);
    ReadOnly has NO writable root and denies outbound network. (Not macOS-gated — pure
    string lowering.)"""
    wd = os.path.realpath(str(tmp_path))
    permissive = lower_to_seatbelt(_permissive(tmp_path), allow_network=True)
    assert f'(allow file-write* (subpath "{wd}"))' in permissive
    assert "(deny network-outbound)" not in permissive  # Permissive keeps egress open

    readonly = lower_to_seatbelt((), allow_network=False)
    assert "(deny network-outbound)" in readonly
    assert f'subpath "{wd}"' not in readonly  # ReadOnly: no writable root at all


def test_lower_to_seatbelt_multi_root_is_deny_closed(tmp_path) -> None:
    """Per-binding lowering: one allow-subpath rule per writable root, deny-closed default.
    A path under a granted root is allowed; a sibling under no grant has no allow rule."""
    backend_dir = os.path.realpath(str(tmp_path / "backend"))
    profile = lower_to_seatbelt((backend_dir,), allow_network=False)
    assert f'(allow file-write* (subpath "{backend_dir}"))' in profile
    # deny-closed: the whole workspace is NOT re-allowed, only the granted root.
    assert f'(allow file-write* (subpath "{os.path.realpath(str(tmp_path))}"))' not in profile


@_macos
def test_seatbelt_launch_denies_out_of_workdir_write(tmp_path) -> None:
    """Permissive jail: an in-WORKDIR write lands; an out-of-WORKDIR write is refused at
    the syscall (the byte never reaches disk)."""
    backend = SeatbeltContainmentBackend()
    profile = backend.profile_for(_permissive(tmp_path), allow_network=True)

    inside = tmp_path / "ok.txt"
    backend.launch(profile, tmp_path, ["/usr/bin/touch", str(inside)])
    assert inside.exists()  # in-WORKDIR write allowed

    outside = tmp_path.parent / "escape.txt"
    outside.unlink(missing_ok=True)
    backend.launch(profile, tmp_path, ["/usr/bin/touch", str(outside)])
    assert not outside.exists()  # out-of-WORKDIR write denied at the syscall


@_macos
def test_seatbelt_readonly_denies_in_workdir_write(tmp_path) -> None:
    """ReadOnly jail: even an in-WORKDIR write is refused (no writable root)."""
    backend = SeatbeltContainmentBackend()
    profile = backend.profile_for((), allow_network=False)
    target = tmp_path / "nope.txt"
    backend.launch(profile, tmp_path, ["/usr/bin/touch", str(target)])
    assert not target.exists()


@_macos
def test_seatbelt_per_binding_writable_root_is_deny_closed(tmp_path) -> None:
    """v0.2 §5-item-1 backend gate: a single ReadWrite root (``backend/``) alongside an
    ungranted sibling (``docs/``). A write under the granted root lands; a write to the
    ungranted sibling AND to an unbound in-workspace path are both refused at the syscall —
    the deny-closed guarantee the per-binding soundness argument rests on."""
    backend = SeatbeltContainmentBackend()
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    roots = (str(backend_dir),)
    profile = backend.profile_for(roots, allow_network=False)

    # positive: a write beneath the ReadWrite-granted root lands.
    ok = backend_dir / "candidate.txt"
    backend.launch(profile, tmp_path, ["/usr/bin/touch", "--", str(ok)])
    assert ok.exists()

    # deny-closed: a write to the ungranted sibling root is refused at the syscall.
    denied = docs_dir / "nope.txt"
    backend.launch(profile, tmp_path, ["/usr/bin/touch", "--", str(denied)])
    assert not denied.exists()

    # deny-closed: a write to an in-workspace path under no root is refused at the syscall.
    unbound = tmp_path / "stray.txt"
    backend.launch(profile, tmp_path, ["/usr/bin/touch", "--", str(unbound)])
    assert not unbound.exists()

    # the generalized probe accepts this per-binding profile (fail-closed pre-flight).
    backend.probe(profile, tmp_path, writable_roots=roots)


@_macos
def test_seatbelt_fail_closed_probe_raises_when_not_jailed(tmp_path) -> None:
    """Fail-closed (§6): a non-jailing profile (allow-all) must be rejected by the probe —
    the run refuses rather than proceeding unconfined."""
    backend = SeatbeltContainmentBackend()
    non_jailing = "(version 1)\n(allow default)\n"
    with pytest.raises(JailNotEstablished):
        backend.probe(non_jailing, tmp_path, writable_roots=_permissive(tmp_path))


@_macos
def test_seatbelt_probe_catches_readonly_mislowered_to_writable(tmp_path) -> None:
    """Hardened fail-closed = conformance, not just liveness: a ReadOnly run whose profile
    was mis-lowered to a writable WORKDIR (the copy-paste-the-writable-root bug) passes a
    liveness-only probe yet would silently escalate. The deny-closed canary catches it."""
    backend = SeatbeltContainmentBackend()
    mislowered = lower_to_seatbelt(_permissive(tmp_path), allow_network=True)  # BUG: writable WORKDIR
    with pytest.raises(JailNotEstablished):
        backend.probe(mislowered, tmp_path, writable_roots=())  # claims ReadOnly (no writable root)


@_macos
def test_seatbelt_probe_passes_for_conformant_profiles(tmp_path) -> None:
    """The positive case: correctly-lowered Permissive and ReadOnly profiles pass the probe."""
    backend = SeatbeltContainmentBackend()
    backend.probe(
        backend.profile_for(_permissive(tmp_path), allow_network=True), tmp_path, writable_roots=_permissive(tmp_path)
    )
    backend.probe(backend.profile_for((), allow_network=False), tmp_path, writable_roots=())


@_macos
def test_seatbelt_handles_workspace_path_with_shell_and_sbpl_metacharacters(tmp_path) -> None:
    """Security-hygiene regression: a legitimate workspace path containing spaces, single
    quotes, and double quotes must not malform the SBPL profile or the probe write command.
    Before hardening, such a path made the Permissive probe spuriously fail as 'too strict'
    — fail-closed, but legit work refused."""
    weird = tmp_path / "ws a'b\"c"
    weird.mkdir()
    backend = SeatbeltContainmentBackend()
    backend.probe(backend.profile_for(_permissive(weird), allow_network=True), weird, writable_roots=_permissive(weird))
    backend.probe(backend.profile_for((), allow_network=False), weird, writable_roots=())
    inside = weird / "ok.txt"
    backend.launch(backend.profile_for(_permissive(weird), allow_network=True), weird, ["/usr/bin/touch", str(inside)])
    assert inside.exists()  # in-WORKDIR write lands even with metacharacters in the path


@_macos
def test_seatbelt_escapes_backslash_in_workspace_path(tmp_path) -> None:
    """SBPL escaping regression (backslash): a legitimate workspace path containing a literal
    backslash must be escaped inside the double-quoted SBPL subpath literal (``\\`` -> ``\\\\``),
    so the profile stays well-formed and the Permissive probe does not spuriously fail."""
    weird = tmp_path / "ws\\back\\slash"
    weird.mkdir()
    # Direct escaping assertion: the raw backslash path must appear only in escaped form.
    profile = lower_to_seatbelt(_permissive(weird), allow_network=True)
    raw = str(weird.resolve())
    assert raw not in profile
    assert raw.replace("\\", "\\\\") in profile
    # And end-to-end: probe + launch still work with the backslash path.
    backend = SeatbeltContainmentBackend()
    backend.probe(backend.profile_for(_permissive(weird), allow_network=True), weird, writable_roots=_permissive(weird))
    backend.probe(backend.profile_for((), allow_network=False), weird, writable_roots=())
    inside = weird / "ok.txt"
    backend.launch(backend.profile_for(_permissive(weird), allow_network=True), weird, ["/usr/bin/touch", str(inside)])
    assert inside.exists()
