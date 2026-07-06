"""Multi-coordinator exclusion via a filesystem lock.

Reclaim is **process-identity authoritative**, not time-based. A held lock is stolen
only when its holder is provably gone: the PID is dead, or the PID is alive but its
recorded process start time no longer matches the process now at that PID (the PID was
recycled by an unrelated process). A genuinely live holder is *never* reclaimed, however
long it has held the lock.

There is deliberately no age timeout. An age-based "stale lock" reclaim would steal the
lock of a run that simply takes longer than the timeout — and, with run-start
auto-recovery, the stealer would then archive that still-live run's operations. The
identity check makes the age timeout unnecessary: every dead-holder case (crash, kill,
power-loss, PID reuse) is reclaimed at once, and the one case that must not be reclaimed
— a live long run — is exactly the one the identity check protects.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import NamedTuple

from vcs_core._errors import ActivationError

try:  # psutil is not a declared dependency; the lock degrades safely without it.
    import psutil
except ImportError:  # pragma: no cover - exercised only where psutil is absent
    psutil = None


class _LockHolder(NamedTuple):
    pid: int
    start_time: str | None  # recorded process create-time; None for a legacy/psutil-less writer
    age: float


def acquire_session_lock(repo_path: str, session_id: str) -> None:
    """Acquire the cross-process lock on the ``.vcscore/`` repository.

    Uses ``O_CREAT|O_EXCL`` for atomic creation. The lock records the holder's session
    id, PID, timestamp, and (best-effort) process start time. A pre-existing lock is
    reclaimed only if its holder is provably gone (see the module docstring); otherwise
    activation fails closed rather than stealing a live session's lock.
    """
    lock_path = str(Path(repo_path) / "session.lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, _lock_payload(session_id))
        os.close(fd)
    except FileExistsError:
        holder = _read_lock(lock_path)
        if _holder_is_gone(holder):
            Path(lock_path).unlink()
            acquire_session_lock(repo_path, session_id)
            return
        raise ActivationError(
            f"Repository locked by session (PID {holder.pid}, held {holder.age:.0f}s). "
            f"Another session is active. If you are certain that process is gone, remove {lock_path}."
        ) from None


def release_session_lock(repo_path: str, session_id: str) -> None:
    """Release this session's lock if this session still owns it."""
    lock = Path(repo_path) / "session.lock"
    with contextlib.suppress(FileNotFoundError):
        held_session_id = lock.read_text().split("\n", 1)[0]
        if held_session_id == session_id:
            lock.unlink()


def _lock_payload(session_id: str) -> bytes:
    """Serialize the lock as ``session_id / pid / timestamp / start_time`` (one per line).

    The start time is a trailing (optional) line, so a reader that only wants the pid /
    timestamp — and any legacy 3-line lock — parses unchanged.
    """
    start = _process_start_time(os.getpid()) or ""
    return f"{session_id}\n{os.getpid()}\n{time.time()}\n{start}\n".encode()


def _read_lock(lock_path: str) -> _LockHolder:
    """Read a lock file into a holder record (tolerant of the legacy 3-line format)."""
    lines = Path(lock_path).read_text().strip().split("\n")
    pid = int(lines[1])
    age = time.time() - float(lines[2])
    start_time = lines[3] if len(lines) > 3 and lines[3] else None
    return _LockHolder(pid=pid, start_time=start_time, age=age)


def _holder_is_gone(holder: _LockHolder) -> bool:
    """Whether the lock's holder is provably no longer running.

    Dead PID -> gone. Live PID whose recorded start time no longer matches the process
    now at that PID -> the PID was recycled, so the original holder is gone. A live PID
    with a matching — or unverifiable — start time is treated as a live holder and never
    stolen.
    """
    if not _pid_alive(holder.pid):
        return True
    if holder.start_time is None:
        return False  # no recorded identity to check against -> assume live (never steal)
    current = _process_start_time(holder.pid)
    if current is None:
        return False  # cannot verify the identity now -> assume live
    return current != holder.start_time  # start-time mismatch -> PID reused -> holder gone


def _process_start_time(pid: int) -> str | None:
    """Best-effort, stable process creation-time string; ``None`` if unknowable."""
    if psutil is None:
        return None
    try:
        return repr(psutil.Process(pid).create_time())
    except Exception:  # noqa: BLE001 - a process we cannot inspect -> identity unknowable
        return None


def _pid_alive(pid: int) -> bool:
    """Check if a process is alive via ``kill(0)``."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Process exists but we can't signal it
    return True
