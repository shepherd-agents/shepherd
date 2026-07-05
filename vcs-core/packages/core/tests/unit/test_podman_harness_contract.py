"""Regression checks for the shared Podman shakeout harness."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = PACKAGE_ROOT.parents[2]
SCRIPTS = [
    REPO_ROOT / "scripts" / "vcs-core-podman.sh",
    REPO_ROOT / "scripts" / "vcs-core-wander-podman.sh",
    REPO_ROOT / "scripts" / "vcs-core-session-shakeout.sh",
    REPO_ROOT / "scripts" / "vcs-core-capture-shakeout.sh",
    REPO_ROOT / "scripts" / "vcs-core-tour-demo.sh",
]


# The public source cut omits the repo-root podman harness scripts these
# contracts read; skip cleanly there (the internal tree always has them).
pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / "scripts" / "vcs-core-podman.sh").exists(),
    reason="repo-root podman harness scripts are not present in this checkout",
)


def test_podman_harness_scripts_have_valid_bash_syntax() -> None:
    subprocess.run(["bash", "-n", *[str(path) for path in SCRIPTS]], check=True, cwd=REPO_ROOT)


def test_shared_podman_harness_exposes_supported_commands() -> None:
    text = (REPO_ROOT / "scripts" / "vcs-core-podman.sh").read_text()

    for token in (
        "check",
        "build",
        "up",
        "exec",
        "shell",
        "demo",
        "session-smoke",
        "capture-smoke",
        "logs",
        "inspect",
        "down",
        "reset",
    ):
        assert token in text

    assert "VCS_CORE_PODMAN_KEEP_RUN" in text
    assert "VCS_CORE_PODMAN_RUN_NAME" in text
    assert "CONTAINER_SCRIPTS_ROOT" in text
    assert ".podman/" in text
    assert "container_path_env" in text
    assert (
        'CONTAINER_BASE_PATH="${CONTAINER_BASE_PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"'
        in text
    )
    assert "${CONTAINER_VENV}/bin:${CONTAINER_BASE_PATH}" in text


def test_podman_logs_can_focus_on_named_retained_run() -> None:
    text = (REPO_ROOT / "scripts" / "vcs-core-podman.sh").read_text()

    assert 'requested_run_name="${VCS_CORE_PODMAN_RUN_NAME:-}"' in text
    assert 'run_name="$(sanitize_name "${requested_run_name}")"' in text
    assert 'retained_root="${HOST_RUNS_ROOT}/${run_name}"' in text
    assert "does not exist under" in text


def test_shared_podman_harness_runs_repo_scripts_from_repo_root() -> None:
    text = (REPO_ROOT / "scripts" / "vcs-core-podman.sh").read_text()

    assert "${CONTAINER_SCRIPTS_ROOT}/vcs-core-tour-demo.sh" in text
    assert "${CONTAINER_SCRIPTS_ROOT}/vcs-core-session-shakeout.sh" in text
    assert "${CONTAINER_SCRIPTS_ROOT}/vcs-core-capture-shakeout.sh" in text
    assert "DEMO_ROOT=" in text
    assert "VCS_CORE_SESSION_SHAKEOUT_ROOT=" in text
    assert "VCS_CORE_CAPTURE_SHAKEOUT_ROOT=" in text
    assert "VCS_CORE_CAPTURE_SHAKEOUT_DEBUG_LOG=" in text


def test_legacy_wander_wrapper_routes_through_the_shared_harness() -> None:
    text = (REPO_ROOT / "scripts" / "vcs-core-wander-podman.sh").read_text()

    assert "vcs-core-podman.sh" in text
    assert "shell" in text
    assert "exec --" in text


def test_session_and_capture_smokes_cover_the_expected_cli_paths() -> None:
    session_text = (REPO_ROOT / "scripts" / "vcs-core-session-shakeout.sh").read_text()
    capture_text = (REPO_ROOT / "scripts" / "vcs-core-capture-shakeout.sh").read_text()

    assert "vcs-core session start" in session_text
    assert "vcs-core session exec" in session_text
    assert "vcs-core session shell" in session_text
    assert "vcs-core merge" in session_text
    assert "vcs-core discard" in session_text

    assert "--capture" in capture_text
    assert "--capture-debug" in capture_text
    assert "vcs-core push" in capture_text
