"""Linux Landlock containment backend — the native syscall-deny tier (no container).

The Linux member of the `ContainmentBackend` family (`_containment.py`): unprivileged
Landlock LSM (kernel 5.13+) restricts filesystem writes to a writable root via raw syscalls
(444/445/446 + ``PR_SET_NO_NEW_PRIVS``). Unlike Seatbelt's external ``sandbox-exec -p <profile>``,
Landlock is **self-restriction-before-exec**: `launch` runs a confine-then-exec runner (this
module's ``__main__``) that applies the ruleset to itself and then ``execvp``s the command. The
backend's "profile" is therefore a backend-private string — the writable root (empty = none).

Lifted from the proven spike ``spikes/sandbox-jail/linux_landlock_fuse.py`` (8/8 in the
vcs-core-test container, ABI 7). Internal runtime surface — not part of the frozen consumer SPI.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vcs_core._containment import JailNotEstablished

if TYPE_CHECKING:
    from collections.abc import Sequence

# Landlock raw syscalls (same numbers on x86_64 and aarch64); glibc lacks wrappers pre-2.36.
_NR_CREATE_RULESET, _NR_ADD_RULE, _NR_RESTRICT_SELF = 444, 445, 446
_PR_SET_NO_NEW_PRIVS = 38
_LL_CREATE_RULESET_VERSION = 1
_LL_RULE_PATH_BENEATH = 1
# v1 FS write-access bits (exclude v2 REFER / v3 TRUNCATE so a v1 kernel accepts the mask).
# Bit i = (1 << i): 1=write_file, 4=remove_dir, 5=remove_file, 6..12 = make_{char,dir,reg,sock,fifo,block,sym}.
# Reads/exec (bits 0,2,3) are intentionally NOT governed.
_WRITE_ACCESS = (
    (1 << 1) | (1 << 4) | (1 << 5) | (1 << 6) | (1 << 7) | (1 << 8) | (1 << 9) | (1 << 10) | (1 << 11) | (1 << 12)
)
# Pairing parity with Seatbelt, which unconditionally allows file-write* under
# /dev ("/dev/null etc. — not the workspace"): without a /dev grant the Linux
# pairing is stricter than the contract, and a body that opens /dev/null for
# writing fails only on Linux (found live: hermes oneshot, S3 evidence run).
# Deliberately narrower than Seatbelt — existing-file writes only, no
# create/remove/make bits, so nothing can be *created* under /dev (/dev/shm).
_DEV_WRITE_ACCESS = 1 << 1

_RUNNER_SENTINEL = "__landlock_confine_exec__"
_CONFINE_FAILED_RC = 3  # runner exit code when confinement could not be established (touch never uses 3)

_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.syscall.restype = ctypes.c_long


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]  # ABI v1: single field


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1  # packed: u64 + s32 = 12 bytes (a classic Landlock gotcha)
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _syscall(*args: Any) -> int:
    conv = [a if not isinstance(a, int) else ctypes.c_long(a) for a in args]
    return int(_LIBC.syscall(*conv))


def landlock_abi() -> int:
    """Landlock ABI version: >=1 supported; -38 (ENOSYS) no kernel support; -1 (EPERM) blocked."""
    rv = _syscall(_NR_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint(_LL_CREATE_RULESET_VERSION))
    return int(rv) if rv >= 0 else -ctypes.get_errno()


def landlock_confine(writable_dirs: Sequence[str]) -> None:
    """Restrict this thread (and its children) so writes are allowed only beneath the roots.

    Reads/exec are not governed. An empty ``writable_dirs`` confines with NO writable root
    (every write denied — the ReadOnly case); each root adds one PATH_BENEATH rule, so a
    proper subset of the workspace yields per-binding grants. Raises OSError on any
    setup failure so the caller can fail closed.
    """
    attr = _RulesetAttr(handled_access_fs=_WRITE_ACCESS)
    fd = _syscall(_NR_CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint(0))
    if fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
    fd = int(fd)
    for writable_dir in writable_dirs:
        dirfd = os.open(writable_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            rule = _PathBeneathAttr(allowed_access=_WRITE_ACCESS, parent_fd=dirfd)
            if (
                _syscall(
                    _NR_ADD_RULE,
                    ctypes.c_int(fd),
                    ctypes.c_int(_LL_RULE_PATH_BENEATH),
                    ctypes.byref(rule),
                    ctypes.c_uint(0),
                )
                < 0
            ):
                raise OSError(ctypes.get_errno(), "landlock_add_rule")
        finally:
            os.close(dirfd)
    # The unconditional /dev grant (see _DEV_WRITE_ACCESS). Fail-open is safe
    # here in the strict direction only: a missing rule makes the jail
    # STRICTER (deny stands), never looser — so an absent /dev (unusual mount
    # namespace) skips the rule instead of failing the confinement.
    with suppress(OSError):
        devfd = os.open("/dev", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            dev_rule = _PathBeneathAttr(allowed_access=_DEV_WRITE_ACCESS, parent_fd=devfd)
            _syscall(
                _NR_ADD_RULE,
                ctypes.c_int(fd),
                ctypes.c_int(_LL_RULE_PATH_BENEATH),
                ctypes.byref(dev_rule),
                ctypes.c_uint(0),
            )
        finally:
            os.close(devfd)
    if _LIBC.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(NO_NEW_PRIVS)")
    if _syscall(_NR_RESTRICT_SELF, ctypes.c_int(fd), ctypes.c_uint(0)) < 0:
        raise OSError(ctypes.get_errno(), "landlock_restrict_self")


class LandlockContainmentBackend:
    """Linux Landlock jail (unprivileged LSM). Native syscall-deny, no container."""

    name = "landlock"
    enforcement_tier = "native-syscall-deny"

    def available(self) -> tuple[bool, str]:
        if sys.platform != "linux":
            return (False, "Landlock is Linux-only")
        abi = landlock_abi()
        if abi >= 1:
            return (True, f"Landlock ABI {abi}")
        if abi == -38:
            return (False, "Landlock unsupported by this kernel (ENOSYS)")
        return (False, f"Landlock unavailable (errno {-abi}; e.g. blocked by seccomp)")

    def profile_for(self, writable_roots: Sequence[str], *, allow_network: bool) -> str:
        """Backend-private profile: a JSON array of the canonicalized writable roots.

        Empty array = ReadOnly (no writable root -> every write denied); one entry ==
        realpath(WORKDIR) = Permissive; a proper subset = per-binding grants. Landlock is
        filesystem-only, so ``allow_network`` is not enforced here (network confinement is
        the Seatbelt/egress-broker axis) — accepted for Protocol parity.
        """
        del allow_network  # Landlock governs filesystem writes only; network is a separate axis.
        return json.dumps([os.path.realpath(str(root)) for root in writable_roots])

    def launch(
        self, profile: str, working_root: Any, command: list[str], *, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run command under a Landlock confine-then-exec runner (this module's ``__main__``)."""
        return subprocess.run(
            [sys.executable, "-m", "vcs_core._landlock_containment", _RUNNER_SENTINEL, profile, *command],
            cwd=os.path.realpath(str(working_root)),
            env=env if env is not None else dict(os.environ),
            capture_output=True,
            text=True,
            check=False,
        )

    def _write_result(self, profile: str, working_root: Any, target: Path) -> int:
        """Confined ``touch target``; return the runner rc: 0=allowed, 3=confine-failed, else denied.

        Cleanup is parent-side (unconfined), so it is reliable regardless of the outcome.
        """
        with suppress(FileNotFoundError):
            target.unlink()
        rc = self.launch(profile, working_root, ["/usr/bin/touch", "--", str(target)]).returncode
        with suppress(FileNotFoundError):
            target.unlink()
        return rc

    def probe(self, profile: str, working_root: Any, *, writable_roots: Sequence[str]) -> None:
        """Fail-closed (§6): prove the jail is BOTH live AND grant-conformant before the body.

        Deny-closed and per-root, mirroring the Seatbelt probe: (1) an out-of-WORKSPACE write
        is denied; (2) a write beneath each declared writable root is allowed; (3) if WORKDIR
        is not itself a writable root, an in-WORKDIR write outside every root is denied.

        Exit-code-aware (unlike a presence-only check): a confine FAILURE (rc==3) is treated
        as "no jail" rather than "write denied", so a broken Landlock can never pass as a
        working jail.
        """
        wd = Path(os.path.realpath(str(working_root)))
        roots = [Path(os.path.realpath(str(root))) for root in writable_roots]

        def _denied(target: Path) -> bool:
            rc = self._write_result(profile, working_root, target)
            if rc == _CONFINE_FAILED_RC:
                raise JailNotEstablished("fail-closed: Landlock confinement could not be established")
            return rc != 0

        # (1) liveness: a write OUTSIDE the workspace must be denied.
        if not _denied(wd.parent / ".jail-probe"):
            raise JailNotEstablished("fail-closed: out-of-WORKDIR write was NOT denied — no jail established")

        # (2) per-root: each declared writable root must accept writes.
        for root in roots:
            if _denied(root / ".jail-probe-canary"):
                raise JailNotEstablished(
                    f"fail-closed: writable root {root} DENIES writes (profile too strict) — "
                    "a legit body's writes would spuriously fail"
                )

        # (3) deny-closed: an in-WORKDIR path outside every writable root must be denied.
        wd_is_writable = any(wd == root or wd.is_relative_to(root) for root in roots)
        if not wd_is_writable and not _denied(wd / ".jail-probe-denied"):
            raise JailNotEstablished(
                "fail-closed: an in-WORKDIR path outside every writable root was PERMITTED — would silently escalate"
            )


def _run_confined() -> int:
    """Runner entry: argv = [SENTINEL, roots_json, *command]. Confine self, then exec command.

    ``roots_json`` is the backend-private profile: a JSON array of writable roots (``[]`` =
    ReadOnly). JSON keeps the whole set in one argv element and is safe for arbitrary paths.
    """
    writable_roots = json.loads(sys.argv[2]) if sys.argv[2] else []
    command = sys.argv[3:]
    try:
        landlock_confine(writable_roots)
    except OSError:
        return _CONFINE_FAILED_RC  # fail-closed: never run the body if confinement failed
    if not command:
        return 0
    os.execvp(command[0], command)  # noqa: S606  intentional shell-free exec (the confined runner)
    return 127  # type: ignore[unreachable]  # os.execvp replaces the process or raises; never returns


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == _RUNNER_SENTINEL:
        raise SystemExit(_run_confined())
