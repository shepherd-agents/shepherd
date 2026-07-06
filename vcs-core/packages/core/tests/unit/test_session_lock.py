"""Session-lock reclaim is process-identity authoritative, not time-based.

The hazard this fixes: an age-based "stale lock" reclaim stole the lock of any run that
outlived the timeout (agent runs routinely do), and — with run-start auto-recovery — the
stealer would then archive that still-live run's operations. Reclaim now happens only
when the holder is provably gone (dead PID, or a PID recycled by another process), so a
live long run is never stolen while every genuinely dead holder is reclaimed at once.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from vcs_core import ActivationError
from vcs_core._lock import (
    _pid_alive,
    _process_start_time,
    acquire_session_lock,
    release_session_lock,
)


def _lock_file(repo: Path) -> Path:
    return repo / "session.lock"


def _plant_lock(repo: Path, *, session_id: str, pid: int, age_seconds: float, start_time: str | None) -> None:
    """Write a session.lock as a prior holder would (start_time=None => legacy 3-line)."""
    payload = f"{session_id}\n{pid}\n{time.time() - age_seconds}\n"
    if start_time is not None:
        payload += f"{start_time}\n"
    _lock_file(repo).write_text(payload)


def _held_by(repo: Path) -> str:
    return _lock_file(repo).read_text().split("\n", 1)[0]


def _reaped_dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_live_long_held_lock_is_never_reclaimed(tmp_path: Path) -> None:
    """THE HAZARD FIX: a live holder whose lock is hours old is not stolen.

    Before, `lock_age > 300s` reclaimed it — so a second run started during a >5-minute
    run would steal the lock and auto-archive the live run's operations.
    """
    _plant_lock(
        tmp_path,
        session_id="live-long-run",
        pid=os.getpid(),  # this process = a genuinely live holder
        age_seconds=86_400,  # a full day old: far past any old age timeout
        start_time=_process_start_time(os.getpid()),  # matching identity => truly live
    )
    with pytest.raises(ActivationError, match="Another session is active"):
        acquire_session_lock(str(tmp_path), "impatient-second-run")
    assert _held_by(tmp_path) == "live-long-run"  # the live holder keeps its lock


def test_dead_holder_lock_is_reclaimed(tmp_path: Path) -> None:
    """A crash/kill/power-loss (dead PID) lock is reclaimed immediately — no timeout wait."""
    dead = _reaped_dead_pid()
    if _pid_alive(dead):
        pytest.skip("the reaped PID was recycled before the test could observe it dead")
    _plant_lock(tmp_path, session_id="crashed", pid=dead, age_seconds=1.0, start_time="123.0")
    acquire_session_lock(str(tmp_path), "next-run")  # must not raise
    assert _held_by(tmp_path) == "next-run"
    release_session_lock(str(tmp_path), "next-run")


def test_reused_pid_lock_is_reclaimed(tmp_path: Path) -> None:
    """A dead session whose PID was recycled auto-heals: the recorded start time no longer
    matches the process now at that PID, so the stale lock is reclaimed rather than blocking."""
    if _process_start_time(os.getpid()) is None:
        pytest.skip("process start time is unavailable (psutil missing); reuse cannot be detected")
    _plant_lock(
        tmp_path,
        session_id="dead-but-pid-recycled",
        pid=os.getpid(),  # PID is alive...
        age_seconds=1.0,
        start_time="0.0",  # ...but recorded as a different process => recycled
    )
    acquire_session_lock(str(tmp_path), "next-run")  # identity mismatch => reclaim, must not raise
    assert _held_by(tmp_path) == "next-run"
    release_session_lock(str(tmp_path), "next-run")


def test_legacy_three_line_lock_with_live_pid_is_refused(tmp_path: Path) -> None:
    """Backward compatibility: a legacy lock with no recorded start time and a live PID is
    treated as live (never stolen) — the safe default when identity can't be verified."""
    _plant_lock(tmp_path, session_id="legacy", pid=os.getpid(), age_seconds=99_999, start_time=None)
    with pytest.raises(ActivationError):
        acquire_session_lock(str(tmp_path), "next-run")


def test_acquire_then_release_round_trip_records_identity(tmp_path: Path) -> None:
    """A freshly acquired lock records this process's identity and releases cleanly."""
    acquire_session_lock(str(tmp_path), "sess-1")
    lines = _lock_file(tmp_path).read_text().strip().split("\n")
    assert lines[0] == "sess-1"
    assert int(lines[1]) == os.getpid()
    # a foreign session cannot release ours; the owner can
    release_session_lock(str(tmp_path), "other")
    assert _lock_file(tmp_path).exists()
    release_session_lock(str(tmp_path), "sess-1")
    assert not _lock_file(tmp_path).exists()
