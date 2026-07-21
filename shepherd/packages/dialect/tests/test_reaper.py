"""The jailed hard-stop supervisor (§4.6): faithful status propagation + tree reap.

Linux-only (the reaper walks ``/proc``); on macOS the builder keeps the perl
form, so these drive the real ``_reaper.py`` runner as a subprocess. Offline —
no provider, no jail, just process lifecycle. The two budget-alarm tests exercise
real ``signal.alarm`` timing (a few seconds each), so they carry ``slow``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the reaper is the Linux hard stop")

_REAPER = Path(__import__("shepherd_dialect.providers._reaper", fromlist=["__file__"]).__file__)


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(_REAPER), *argv], capture_output=True, text=True, check=False, **kwargs)


def test_reaper_propagates_normal_exit_codes() -> None:
    """The supervisor is transparent on the happy path: the child's rc is the rc."""
    assert _run(["10", "/bin/sh", "-c", "exit 0"]).returncode == 0
    assert _run(["10", "/bin/sh", "-c", "exit 1"]).returncode == 1  # the auth-failure shape
    assert _run(["10", "/bin/sh", "-c", "exit 7"]).returncode == 7


def test_reaper_passes_stdout_and_stderr_through() -> None:
    """The child inherits the pipes — output must reach the launch buffers intact."""
    proc = _run(["10", "/bin/sh", "-c", "echo out; echo err >&2; exit 0"])
    assert proc.stdout.strip() == "out"
    assert "err" in proc.stderr


@pytest.mark.slow
def test_reaper_alarm_returns_minus_14() -> None:
    """The budget stop keeps the -14 signature BudgetExhausted reads. Budget 2s
    (not 1s) clears interpreter startup so the alarm never races the fork/exec."""
    proc = _run(["2", "/bin/sh", "-c", "sleep 30"])
    assert proc.returncode == -14


def test_descendants_survives_an_unreadable_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    """A confinement that masks /proc must not crash the reap: _descendants returns
    empty rather than letting the OSError escape the SIGALRM handler (which would
    kill the reaper with rc 1 and skip the child kill + the -14 signature)."""
    from shepherd_dialect.providers import _reaper

    def _raise(_path: str) -> list[str]:
        raise PermissionError("/proc is masked")

    monkeypatch.setattr(_reaper.os, "listdir", _raise)
    assert _reaper._descendants(os.getpid()) == []


@pytest.mark.slow
def test_reaper_alarm_reaps_the_reparented_daemon(tmp_path: Path) -> None:
    """The reason the reaper exists, in the shape the S3 jail run exposed: an
    agent backgrounds a daemon whose intermediate shell exits at once, so the
    orphan reparents to init before the alarm. A plain PPID walk from the
    command is blind to it; the subreaper walk catches it.

    Timing has margin: the reap fires at 2s, the daemon would write at 6s, and
    we watch until 9s — the marker's absence is the reap, not a slow write.
    """
    marker = tmp_path / f"escaped-{uuid.uuid4().hex[:8]}.txt"
    # cmd1: the intermediate shell backgrounds a detached daemon and returns
    # immediately (the reparent); cmd2: the "agent" keeps running past the budget.
    body = f"bash -c \"nohup bash -c 'sleep 6 && echo escaped > {marker}' >/dev/null 2>&1 &\"; sleep 6"
    proc = _run(["2", "/bin/sh", "-c", body])
    assert proc.returncode == -14
    deadline = time.monotonic() + 7  # past the daemon's 6s write, if it survived
    while time.monotonic() < deadline:
        assert not marker.exists(), "a reparented daemon survived the reap and wrote after the kill"
        time.sleep(0.5)


def test_reaper_no_command_is_a_noop() -> None:
    assert _run(["10"]).returncode == 0
