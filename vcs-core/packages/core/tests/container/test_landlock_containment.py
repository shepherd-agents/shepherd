# under-test: vcs_core._landlock_containment
"""B2b — Linux Landlock ContainmentBackend (real, container-verified).

Run via ``make test_container`` (privileged Podman, kernel >=5.13). These assert the real
Landlock syscall denial and the exit-code-aware fail-closed probe. Marked ``container`` +
Linux-gated: the default container seccomp profile can block the Landlock syscalls (444-446),
so they need the privileged image; on macOS they skip.
"""

from __future__ import annotations

import json
import os
import sys

import pytest
from vcs_core._containment import ContainmentBackend, JailNotEstablished
from vcs_core._landlock_containment import LandlockContainmentBackend, landlock_abi

pytestmark = [
    pytest.mark.container,
    pytest.mark.skipif(sys.platform != "linux", reason="Landlock is Linux-only"),
]


def test_landlock_available_and_protocol_conformant() -> None:
    backend = LandlockContainmentBackend()
    assert isinstance(backend, ContainmentBackend)
    ok, why = backend.available()
    assert ok, why
    assert backend.name == "landlock"
    assert backend.enforcement_tier == "native-syscall-deny"
    assert landlock_abi() >= 1


def test_landlock_profile_for_readonly_vs_permissive(tmp_path) -> None:
    backend = LandlockContainmentBackend()
    wd = os.path.realpath(str(tmp_path))
    assert backend.profile_for((str(tmp_path),), allow_network=True) == json.dumps([wd])
    assert backend.profile_for((), allow_network=False) == json.dumps([])  # no writable root
    # multi-root: one entry per granted root (Landlock is FS-only; allow_network is ignored).
    backend_dir = os.path.realpath(str(tmp_path / "backend"))
    assert backend.profile_for((str(tmp_path / "backend"),), allow_network=False) == json.dumps([backend_dir])


def test_landlock_permissive_allows_in_workdir_denies_outside(tmp_path) -> None:
    backend = LandlockContainmentBackend()
    profile = backend.profile_for((str(tmp_path),), allow_network=True)

    inside = tmp_path / "ok.txt"
    assert backend.launch(profile, tmp_path, ["/usr/bin/touch", "--", str(inside)]).returncode == 0
    assert inside.exists()  # in-WORKDIR write allowed

    outside = tmp_path.parent / "escape.txt"
    outside.unlink(missing_ok=True)
    assert backend.launch(profile, tmp_path, ["/usr/bin/touch", "--", str(outside)]).returncode != 0
    assert not outside.exists()  # out-of-WORKDIR write denied at the syscall


def test_landlock_readonly_denies_in_workdir(tmp_path) -> None:
    backend = LandlockContainmentBackend()
    profile = backend.profile_for((), allow_network=False)
    target = tmp_path / "nope.txt"
    assert backend.launch(profile, tmp_path, ["/usr/bin/touch", "--", str(target)]).returncode != 0
    assert not target.exists()


def test_landlock_per_binding_writable_root_is_deny_closed(tmp_path) -> None:
    """v0.2: a single ReadWrite root (``backend/``) beside an ungranted sibling
    (``docs/``). A write under the granted root lands; the sibling and any unbound
    in-workspace path are refused at the syscall — the deny-closed guarantee."""
    backend = LandlockContainmentBackend()
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    profile = backend.profile_for((str(backend_dir),), allow_network=False)

    ok = backend_dir / "candidate.txt"
    assert backend.launch(profile, tmp_path, ["/usr/bin/touch", "--", str(ok)]).returncode == 0
    assert ok.exists()

    denied = docs_dir / "nope.txt"
    assert backend.launch(profile, tmp_path, ["/usr/bin/touch", "--", str(denied)]).returncode != 0
    assert not denied.exists()

    unbound = tmp_path / "stray.txt"
    assert backend.launch(profile, tmp_path, ["/usr/bin/touch", "--", str(unbound)]).returncode != 0
    assert not unbound.exists()

    backend.probe(profile, tmp_path, writable_roots=(str(backend_dir),))


def test_landlock_probe_passes_for_conformant_profiles(tmp_path) -> None:
    backend = LandlockContainmentBackend()
    backend.probe(backend.profile_for((str(tmp_path),), allow_network=True), tmp_path, writable_roots=(str(tmp_path),))
    backend.probe(backend.profile_for((), allow_network=False), tmp_path, writable_roots=())


def test_landlock_probe_catches_readonly_mislowered_to_writable(tmp_path) -> None:
    backend = LandlockContainmentBackend()
    # BUG SIM: a ReadOnly run handed a Permissive-style writable-root profile must be caught
    # by the deny-closed canary (in-WORKDIR write would otherwise be permitted).
    mislowered = backend.profile_for((str(tmp_path),), allow_network=True)
    with pytest.raises(JailNotEstablished):
        backend.probe(mislowered, tmp_path, writable_roots=())


def test_landlock_dev_null_writable_even_readonly_but_dev_create_denied(tmp_path) -> None:
    """Seatbelt-pairing parity: /dev/null etc. accept writes under every profile
    (the macOS lowering allows file-write* under /dev unconditionally — a body
    that opens /dev/null for writing must not fail only on Linux; found live
    with the hermes oneshot, S3 evidence). Narrower than Seatbelt: creating
    files under /dev stays denied."""
    backend = LandlockContainmentBackend()
    profile = backend.profile_for((), allow_network=False)  # ReadOnly — the strictest profile

    write_null = ["/bin/sh", "-c", "echo probe > /dev/null"]
    assert backend.launch(profile, tmp_path, write_null).returncode == 0

    create_under_dev = ["/usr/bin/touch", "--", "/dev/shm/.landlock-dev-create-probe"]
    result = backend.launch(profile, tmp_path, create_under_dev)
    assert result.returncode != 0, "creating files under /dev must stay denied (write_file-only grant)"
