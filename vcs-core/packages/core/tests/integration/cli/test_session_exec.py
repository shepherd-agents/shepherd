# under-test: vcs_core._session
"""Session exec CLI behavior tests."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import socket
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import vcs_core.store as store_module
from click.testing import CliRunner
from vcs_core import canonical_digest
from vcs_core._capture_reducer import CAPTURE_REDUCTION_KIND
from vcs_core._cli_session_runtime import run_managed_exec
from vcs_core._fs_capture import FsCaptureEvent
from vcs_core._hook_frontier import HookEventFrontier
from vcs_core._session import SessionDaemon
from vcs_core._session_dispatch import SessionCommandDispatcher
from vcs_core._world_refs import candidate_ref, world_fork_origin_receipt_ref
from vcs_core._world_storage_installation import default_world_storage_root
from vcs_core.cli import main
from vcs_core.testing import HookManager, SessionInfo
from vcs_core.vcscore import VcsCore

from ...support.builders import make_marker_filesystem_vcscore
from ...support.cli import init_repo as _init
from ...support.overlays import MockOverlayBackend

pytestmark = pytest.mark.slow  # full-lifecycle suite: runs in the lifecycle-tests CI job


def _session_info(workspace: Path) -> SessionInfo:
    return SessionInfo(
        pid=os.getpid(),
        socket_path="/tmp/fake.sock",
        mount_path="/stale/path",
        workspace=str(workspace),
        started_at=time.time(),
    )


def _read_substrate_revision_payload(repo: Any, head: str) -> dict[str, object]:
    commit = repo[head]
    blob = repo[commit.tree["revision.json"].id]
    payload = json.loads(bytes(blob.data).decode("utf-8"))
    assert isinstance(payload, dict)
    return payload


def _read_workspace_shadow_candidate(
    mg: VcsCore,
    reducer_id: str,
) -> tuple[dict[str, object], Any, tuple[Any, ...]]:
    world_storage = mg._world_storage()
    workspace_store = world_storage.store("store_workspace")
    head = str(workspace_store.repo.references[candidate_ref(reducer_id, "workspace")].target)
    provenance = workspace_store.validate_prepared_candidate(
        head,
        evidence_resolver=world_storage.world_store.resolve_evidence_ref,
    )
    payload = _read_substrate_revision_payload(workspace_store.repo, head)
    records = tuple(world_storage.world_store.resolve_evidence_ref(ref) for ref in provenance.preparation.evidence_refs)
    return payload, provenance, records


def _workspace_shadow_candidate_exists(mg: VcsCore, reducer_id: str) -> bool:
    world_storage = mg._world_storage()
    workspace_store = world_storage.store("store_workspace")
    return candidate_ref(reducer_id, "workspace") in workspace_store.repo.references


def _daemon_stub(mg: VcsCore, scope_name: str, **attrs: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "_lock": threading.RLock(),
        "_mg": mg,
        "_current_scope_name": scope_name,
        "_hook_frontier": HookEventFrontier(),
        "_hook_accepted_seq": 0,
        "_hook_processed_seq": 0,
    }
    base.update(attrs)
    return SimpleNamespace(**base)


def _record_single_write_capture(
    tmp_path: Path,
    mg: VcsCore,
    backend: MockOverlayBackend,
    task: Any,
    *,
    path: str,
    content: bytes,
) -> str:
    backend.write_file(task.name, path, content)
    daemon = _daemon_stub(mg, task.name)
    dispatcher = SessionCommandDispatcher(daemon)
    begin = dispatcher.dispatch(
        "exec_envelope_begin",
        {
            "argv": ["bash", "-c", f"write {path}"],
            "cwd": str(tmp_path),
            "scope": task.name,
            "capture_requested": True,
            "started_at": 10.0,
            "client_pid": 123,
        },
    )
    operation_id = str(begin["operation_id"])
    mg._record_capture_event(
        "filesystem",
        FsCaptureEvent(
            op="write_close",
            scope=task.name,
            scope_instance_id=task.instance_id,
            path=path,
            pid=123,
            proc_seq=1,
        ),
        command_operation_id=operation_id,
        global_seq=1,
        event_seq=1,
        capture_mechanism="preload",
    )
    dispatcher.dispatch(
        "exec_envelope_outcome",
        {
            "operation_id": operation_id,
            "outcome": "success",
            "ended_at": 11.0,
            "exit_code": 0,
        },
    )
    return operation_id


def _begin_shell_capture_lease(
    dispatcher: SessionCommandDispatcher,
    *,
    scope: str,
    shell_pid: int = 456,
    daemon_instance_id: str = "daemon-current",
) -> str:
    result = dispatcher.dispatch(
        "shell_capture_lease_begin",
        {
            "lease_id": f"shl_{uuid.uuid4().hex}",
            "scope": scope,
            "capture_requested": True,
            "shell_pid": shell_pid,
            "daemon_instance_id": daemon_instance_id,
            "started_at": 9.0,
            "client_pid": shell_pid,
        },
    )
    return str(result["lease_id"])


def _read_exec_frames(sock: socket.socket, *, timeout: float = 5.0) -> list[dict[str, object]]:
    frames: list[dict[str, object]] = []
    buffer = b""
    sock.settimeout(timeout)
    with sock:
        while True:
            try:
                chunk = sock.recv(65536)
            except TimeoutError:
                pytest.fail("timed out waiting for managed exec stream to close")
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                raw_line, buffer = buffer.split(b"\n", 1)
                if raw_line.strip():
                    frame = json.loads(raw_line.decode())
                    assert isinstance(frame, dict)
                    frames.append(frame)
    return frames


def _joined_frame_payload(frames: list[dict[str, object]], frame_type: str) -> bytes:
    return b"".join(base64.b64decode(str(frame["data_b64"])) for frame in frames if frame.get("type") == frame_type)


def _wait_for_process_exit(pid: int, *, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _terminal_operation_phases(history: Any) -> list[str]:
    phases: list[str] = []
    for commit in history.commits:
        metadata = commit.metadata
        mg = metadata.get("mg") if isinstance(metadata, dict) else None
        operation = mg.get("operation") if isinstance(mg, dict) else None
        phase = operation.get("phase") if isinstance(operation, dict) else None
        if phase in {"completed", "aborted"}:
            phases.append(str(phase))
    return phases


class _BrokenPipeSink:
    def write(self, data: bytes) -> int:
        del data
        raise BrokenPipeError

    def flush(self) -> None:
        return None


def _install_managed_exec_overlay_state(
    daemon: SessionDaemon,
    *,
    scope_name: str,
    scope_instance_id: str,
    mount_path: Path,
    hook_static_env: dict[str, str] | None = None,
    hook_scope_prepend_env: dict[str, list[str]] | None = None,
) -> None:
    original_dispatch = daemon._dispatch

    def dispatch(method: str, params: dict[str, object]) -> dict[str, object]:
        if method == "switch":
            daemon._current_scope_name = str(params["name"])
            return {"current_scope": daemon._current_scope_name, "mount_path": str(mount_path)}
        if method == "get_state":
            return {
                "pid": os.getpid(),
                "current_scope": scope_name,
                "current_scope_instance_id": scope_instance_id,
                "current_world_id": None,
                "mount_path": str(mount_path),
                "workspace": str(mount_path),
                "started_at": time.time(),
                "hook_socket": "/tmp/test-session-hook.sock",
                "hook_static_env": hook_static_env or {},
                "hook_static_prepend_path": [],
                "hook_static_prepend_env": {},
                "hook_scope_env": {
                    "VCS_CORE_SCOPE": scope_name,
                    "VCS_CORE_SCOPE_INSTANCE_ID": scope_instance_id,
                    "VCS_CORE_WORKSPACE": str(mount_path),
                },
                "hook_scope_prepend_path": [],
                "hook_scope_prepend_env": hook_scope_prepend_env or {},
            }
        return original_dispatch(method, params)

    daemon._dispatch = dispatch  # type: ignore[method-assign]


def test_session_exec_help_exposes_headless_session_options() -> None:
    runner = CliRunner()

    result = runner.invoke(main, ["session", "exec", "--help"])

    assert result.exit_code == 0, result.output
    assert "--capture-debug" in result.output
    assert "--cwd" in result.output
    assert "non-interactive" in result.output


def test_session_exec_uses_get_state_without_switching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    runner = CliRunner()
    _init(runner, tmp_path)
    monkeypatch.setenv("VCS_CORE_TEST_LEGACY_EXEC", "1")

    info = _session_info(tmp_path)
    calls: list[tuple[str, dict | None]] = []
    seen: dict[str, object] = {}

    monkeypatch.setattr("vcs_core._ipc.is_session_alive", lambda repo_path: True)
    monkeypatch.setattr("vcs_core._ipc.read_session_info", lambda repo_path: info)

    def fake_send_request(socket_path: str, method: str, params: dict | None = None) -> dict:
        assert socket_path == info.socket_path
        calls.append((method, params))
        if method == "exec_envelope_begin":
            return {
                "ok": True,
                "result": {
                    "operation_id": "cmd-test",
                    "env": {"VCS_CORE_COMMAND_OPERATION_ID": "cmd-test"},
                },
            }
        if method == "exec_envelope_outcome":
            return {
                "ok": True,
                "result": {"operation_id": "cmd-test", "archive_ref": "refs/vcscore/archive/ops/cmd-test"},
            }
        return {
            "ok": True,
            "result": {
                "pid": info.pid,
                "current_scope": "experiment",
                "mount_path": str(tmp_path),
                "workspace": str(tmp_path),
                "started_at": info.started_at,
                "hook_static_env": {"VCS_CORE_HOOK_SOCKET": "/tmp/session-hook.sock"},
                "hook_static_prepend_path": ["/tmp/hook-bin"],
                "hook_static_prepend_env": {},
                "hook_scope_env": {
                    "VCS_CORE_SCOPE": "experiment",
                    "VCS_CORE_SCOPE_INSTANCE_ID": "scope-1",
                    "VCS_CORE_WORKSPACE": str(tmp_path),
                },
                "hook_scope_prepend_path": [],
                "hook_scope_prepend_env": {},
            },
        }

    def fake_run(argv, cwd, env, check):  # type: ignore[no-untyped-def]
        del check
        seen["argv"] = argv
        seen["cwd"] = cwd
        seen["env"] = env
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("vcs_core._ipc.send_request", fake_send_request)
    monkeypatch.setattr("subprocess.run", fake_run)

    result = runner.invoke(main, ["session", "exec", "--", "true"])

    assert result.exit_code == 0, result.output
    assert result.output == ""
    assert calls[0] == ("get_state", {"hook_capabilities": []})
    assert calls[1][0] == "exec_envelope_begin"
    assert calls[2][0] == "exec_envelope_outcome"
    assert seen["argv"] == ["true"]
    assert seen["cwd"] == str(tmp_path.resolve())
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["VCS_CORE_SESSION"] == "1"
    assert env["VCS_CORE_SCOPE"] == "experiment"
    assert env["VCS_CORE_HOOK_SOCKET"] == "/tmp/session-hook.sock"
    assert env["VCS_CORE_COMMAND_OPERATION_ID"] == "cmd-test"
    assert env["PATH"].startswith("/tmp/hook-bin")


def test_session_exec_uses_managed_exec_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    info = _session_info(tmp_path)
    seen: dict[str, object] = {}

    monkeypatch.setattr("vcs_core._ipc.is_session_alive", lambda repo_path: True)
    monkeypatch.setattr("vcs_core._ipc.read_session_info", lambda repo_path: info)

    def fake_run_managed_exec(**kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return 7

    monkeypatch.setattr("vcs_core._cli_session_group.run_managed_exec", fake_run_managed_exec)

    result = runner.invoke(
        main,
        ["session", "exec", "--scope", "experiment", "--create", "--cwd", "subdir", "--", "false"],
    )

    assert result.exit_code == 7, result.output
    assert seen["argv"] == ("false",)
    assert seen["scope_name"] == "experiment"
    assert seen["create"] is True
    assert seen["parent"] is None
    assert seen["cwd_subpath"] == "subdir"
    assert seen["capture_requested"] is False
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["VCS_CORE_SESSION"] == "1"


def test_session_exec_capture_uses_managed_exec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    info = _session_info(tmp_path)
    seen: dict[str, object] = {}

    monkeypatch.setattr("vcs_core._ipc.is_session_alive", lambda repo_path: True)
    monkeypatch.setattr("vcs_core._ipc.read_session_info", lambda repo_path: info)

    def fake_run_managed_exec(**kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return 0

    monkeypatch.setattr("vcs_core._cli_session_group.run_managed_exec", fake_run_managed_exec)

    result = runner.invoke(main, ["session", "exec", "--capture", "--", "true"])

    assert result.exit_code == 0, result.output
    assert seen["capture_requested"] is True
    assert seen["argv"] == ("true",)


def test_session_exec_switches_scope_and_propagates_child_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    runner = CliRunner()
    _init(runner, tmp_path)
    monkeypatch.setenv("VCS_CORE_TEST_LEGACY_EXEC", "1")

    info = _session_info(tmp_path)
    calls: list[tuple[str, dict | None]] = []

    monkeypatch.setattr("vcs_core._ipc.is_session_alive", lambda repo_path: True)
    monkeypatch.setattr("vcs_core._ipc.read_session_info", lambda repo_path: info)

    def fake_send_request(socket_path: str, method: str, params: dict | None = None) -> dict:
        assert socket_path == info.socket_path
        calls.append((method, params))
        if method == "switch":
            return {"ok": True, "result": {"current_scope": "experiment", "mount_path": str(tmp_path)}}
        if method == "exec_envelope_begin":
            return {
                "ok": True,
                "result": {
                    "operation_id": "cmd-false",
                    "env": {"VCS_CORE_COMMAND_OPERATION_ID": "cmd-false"},
                },
            }
        if method == "exec_envelope_outcome":
            return {
                "ok": True,
                "result": {"operation_id": "cmd-false", "archive_ref": "refs/vcscore/archive/ops/cmd-false"},
            }
        return {
            "ok": True,
            "result": {
                "pid": info.pid,
                "current_scope": "experiment",
                "mount_path": str(tmp_path),
                "workspace": str(tmp_path),
                "started_at": info.started_at,
            },
        }

    monkeypatch.setattr("vcs_core._ipc.send_request", fake_send_request)
    monkeypatch.setattr(
        "subprocess.run",
        lambda argv, cwd, env, check: subprocess.CompletedProcess(argv, 7),
    )

    result = runner.invoke(main, ["session", "exec", "--scope", "experiment", "--", "false"])

    assert result.exit_code == 7, result.output
    assert result.output == ""
    assert calls[0:2] == [("switch", {"name": "experiment"}), ("get_state", {"hook_capabilities": []})]
    assert calls[2][0] == "exec_envelope_begin"
    outcome_method, outcome_params = calls[3]
    assert outcome_method == "exec_envelope_outcome"
    assert outcome_params is not None
    assert outcome_params["operation_id"] == "cmd-false"
    assert outcome_params["outcome"] == "failed_exit"
    assert outcome_params["exit_code"] == 7
    assert isinstance(outcome_params["ended_at"], float)


def test_session_exec_create_requires_scope(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    result = runner.invoke(main, ["session", "exec", "--create", "--", "true"])

    assert result.exit_code == 2
    assert "requires `--scope <name>`" in result.output


def test_session_exec_requires_command(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    result = runner.invoke(main, ["session", "exec"])

    assert result.exit_code == 2
    assert "requires a command after `--`" in result.output


def test_session_exec_reports_missing_session_as_environment_error(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    result = runner.invoke(main, ["session", "exec", "--", "true"])

    assert result.exit_code == 3
    assert "no session running" in result.output


def test_session_exec_rejects_cwd_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    monkeypatch.setenv("VCS_CORE_TEST_LEGACY_EXEC", "1")

    info = _session_info(tmp_path)

    monkeypatch.setattr("vcs_core._ipc.is_session_alive", lambda repo_path: True)
    monkeypatch.setattr("vcs_core._ipc.read_session_info", lambda repo_path: info)
    monkeypatch.setattr(
        "vcs_core._ipc.send_request",
        lambda socket_path, method, params=None: {
            "ok": True,
            "result": {
                "pid": info.pid,
                "current_scope": "experiment",
                "mount_path": str(tmp_path),
                "workspace": str(tmp_path),
                "started_at": info.started_at,
            },
        },
    )

    result = runner.invoke(main, ["session", "exec", "--cwd", "../outside", "--", "true"])

    assert result.exit_code == 2
    assert "escapes overlay mount" in result.output


def test_session_exec_rejects_ground_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    monkeypatch.setenv("VCS_CORE_TEST_LEGACY_EXEC", "1")

    info = _session_info(tmp_path)

    monkeypatch.setattr("vcs_core._ipc.is_session_alive", lambda repo_path: True)
    monkeypatch.setattr("vcs_core._ipc.read_session_info", lambda repo_path: info)
    monkeypatch.setattr(
        "vcs_core._ipc.send_request",
        lambda socket_path, method, params=None: {
            "ok": True,
            "result": {
                "pid": info.pid,
                "current_scope": "ground",
                "mount_path": str(tmp_path),
                "workspace": str(tmp_path),
                "started_at": info.started_at,
            },
        },
    )

    result = runner.invoke(main, ["session", "exec", "--", "true"])

    assert result.exit_code == 2
    assert "session shell/exec on ground is disabled" in result.output


def test_session_exec_rejects_legacy_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    monkeypatch.setenv("VCS_CORE_TEST_LEGACY_EXEC", "1")

    result = runner.invoke(main, ["session", "exec", "--capture", "--capture-debug", "--", "true"])

    assert result.exit_code == 2
    assert "requires daemon-managed execution" in result.output


def test_session_exec_warns_when_capture_debug_used_without_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    runner = CliRunner()
    _init(runner, tmp_path)
    monkeypatch.setenv("VCS_CORE_TEST_LEGACY_EXEC", "1")

    info = _session_info(tmp_path)
    seen_env: dict[str, str] = {}
    debug_log = tmp_path / "logs" / "shim.log"

    monkeypatch.setattr("vcs_core._ipc.is_session_alive", lambda repo_path: True)
    monkeypatch.setattr("vcs_core._ipc.read_session_info", lambda repo_path: info)

    def fake_send_request(socket_path: str, method: str, params: dict | None = None) -> dict:
        if method == "exec_envelope_begin":
            return {
                "ok": True,
                "result": {
                    "operation_id": "cmd-debug",
                    "env": {"VCS_CORE_COMMAND_OPERATION_ID": "cmd-debug"},
                },
            }
        if method == "exec_envelope_outcome":
            return {
                "ok": True,
                "result": {"operation_id": "cmd-debug", "archive_ref": "refs/vcscore/archive/ops/cmd-debug"},
            }
        return {
            "ok": True,
            "result": {
                "pid": info.pid,
                "current_scope": "experiment",
                "mount_path": str(tmp_path),
                "workspace": str(tmp_path),
                "started_at": info.started_at,
            },
        }

    monkeypatch.setattr("vcs_core._ipc.send_request", fake_send_request)

    def fake_run(argv, cwd, env, check):  # type: ignore[no-untyped-def]
        del argv, cwd, check
        seen_env.update(env)
        return subprocess.CompletedProcess(["true"], 0)

    monkeypatch.setattr("subprocess.run", fake_run)

    result = runner.invoke(main, ["session", "exec", "--capture-debug", str(debug_log), "--", "true"])

    assert result.exit_code == 0, result.output
    assert "Warning: --capture-debug has no effect without --capture." in result.output
    assert seen_env["VCS_CORE_FS_CAPTURE_DEBUG_LOG"] == str(debug_log)
    assert debug_log.parent.is_dir()


def test_capture_debug_path_inside_tracked_workspace_is_detected(tmp_path: Path) -> None:
    from vcs_core._cli_session_runtime import _debug_log_inside_tracked_workspace

    workspace = str(tmp_path)
    # An explicit path inside the tracked workspace must be flagged: `push` would
    # later refuse it as worktree-not-adopted.
    assert _debug_log_inside_tracked_workspace(str(tmp_path / "capture-debug.log"), workspace) is True
    assert _debug_log_inside_tracked_workspace(str(tmp_path / "logs" / "shim.log"), workspace) is True
    # The control plane is exempt — it is excluded from materialization.
    assert (
        _debug_log_inside_tracked_workspace(str(tmp_path / ".vcscore" / "var" / "logs" / "shim.log"), workspace)
        is False
    )
    # Paths outside the workspace are fine.
    assert _debug_log_inside_tracked_workspace(str(tmp_path.parent / "outside.log"), workspace) is False


def test_session_exec_reports_command_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    monkeypatch.setenv("VCS_CORE_TEST_LEGACY_EXEC", "1")

    info = _session_info(tmp_path)

    monkeypatch.setattr("vcs_core._ipc.is_session_alive", lambda repo_path: True)
    monkeypatch.setattr("vcs_core._ipc.read_session_info", lambda repo_path: info)

    def fake_send_request(socket_path: str, method: str, params: dict | None = None) -> dict:
        if method == "exec_envelope_begin":
            return {
                "ok": True,
                "result": {
                    "operation_id": "cmd-missing",
                    "env": {"VCS_CORE_COMMAND_OPERATION_ID": "cmd-missing"},
                },
            }
        if method == "exec_envelope_outcome":
            return {
                "ok": True,
                "result": {"operation_id": "cmd-missing", "archive_ref": "refs/vcscore/archive/ops/cmd-missing"},
            }
        return {
            "ok": True,
            "result": {
                "pid": info.pid,
                "current_scope": "experiment",
                "mount_path": str(tmp_path),
                "workspace": str(tmp_path),
                "started_at": info.started_at,
            },
        }

    monkeypatch.setattr("vcs_core._ipc.send_request", fake_send_request)
    monkeypatch.setattr(
        "subprocess.run",
        lambda argv, cwd, env, check: (_ for _ in ()).throw(FileNotFoundError(argv[0])),
    )

    result = runner.invoke(main, ["session", "exec", "--", "missing-binary"])

    assert result.exit_code == 127
    assert "command not found: missing-binary" in result.output


def test_session_exec_records_generic_launch_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    monkeypatch.setenv("VCS_CORE_TEST_LEGACY_EXEC", "1")

    info = _session_info(tmp_path)
    calls: list[tuple[str, dict | None]] = []

    monkeypatch.setattr("vcs_core._ipc.is_session_alive", lambda repo_path: True)
    monkeypatch.setattr("vcs_core._ipc.read_session_info", lambda repo_path: info)

    def fake_send_request(socket_path: str, method: str, params: dict | None = None) -> dict:
        assert socket_path == info.socket_path
        calls.append((method, params))
        if method == "exec_envelope_begin":
            return {
                "ok": True,
                "result": {
                    "operation_id": "cmd-oserror",
                    "env": {"VCS_CORE_COMMAND_OPERATION_ID": "cmd-oserror"},
                },
            }
        if method == "exec_envelope_outcome":
            return {
                "ok": True,
                "result": {"operation_id": "cmd-oserror", "archive_ref": "refs/vcscore/archive/ops/cmd-oserror"},
            }
        return {
            "ok": True,
            "result": {
                "pid": info.pid,
                "current_scope": "experiment",
                "mount_path": str(tmp_path),
                "workspace": str(tmp_path),
                "started_at": info.started_at,
            },
        }

    monkeypatch.setattr("vcs_core._ipc.send_request", fake_send_request)
    monkeypatch.setattr(
        "subprocess.run",
        lambda argv, cwd, env, check: (_ for _ in ()).throw(OSError(8, "Exec format error")),
    )

    result = runner.invoke(main, ["session", "exec", "--", "bad-script"])

    assert result.exit_code == 126
    assert "failed to launch bad-script: Exec format error" in result.output
    outcome_method, outcome_params = calls[-1]
    assert outcome_method == "exec_envelope_outcome"
    assert outcome_params is not None
    assert outcome_params["operation_id"] == "cmd-oserror"
    assert outcome_params["outcome"] == "launch_error"
    assert outcome_params["exit_code"] == 126
    assert outcome_params["launch_error"] == "failed to launch bad-script: Exec format error"


def test_session_exec_envelope_dispatcher_archives_failed_command_metadata(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-envelope")
        daemon = _daemon_stub(mg, task.name, _daemon_instance_id="daemon-current")
        dispatcher = SessionCommandDispatcher(daemon)

        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["false"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": False,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = begin["operation_id"]
        assert begin["env"] == {"VCS_CORE_COMMAND_OPERATION_ID": operation_id}

        outcome = dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "failed_exit",
                "ended_at": 12.5,
                "exit_code": 7,
            },
        )

        assert outcome["archive_ref"] == f"refs/vcscore/archive/ops/{operation_id}"
        history = mg.resolve_operation_history(str(operation_id), scope=task)
        assert history.summary.visibility == "archived"
        assert history.summary.status == "error"
        assert history.summary.kind == "vcs_core.session_exec"
        start = history.commits[-1].metadata["command"]
        completed = history.commits[0].metadata["command"]
        assert start["argv"] == ["false"]
        assert start["cwd"] == str(tmp_path)
        assert completed["status"] == "failed_exit"
        assert completed["exit_code"] == 7
        assert completed["duration_seconds"] == 2.5
    finally:
        mg.deactivate()


def test_session_exec_managed_stream_archives_command_metadata(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-managed-exec")
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        _install_managed_exec_overlay_state(
            daemon,
            scope_name=task.name,
            scope_instance_id=task.instance_id,
            mount_path=tmp_path,
        )
        client, server = socket.socketpair()

        def run_managed_exec() -> None:
            with server:
                daemon._handle_managed_exec_connection(
                    server,
                    {
                        "argv": [
                            sys.executable,
                            "-c",
                            "import sys; sys.stdout.write('out'); sys.stderr.write('err'); sys.exit(7)",
                        ],
                        "scope": task.name,
                        "cwd_subpath": None,
                        "capture_requested": False,
                        "env": dict(os.environ),
                        "started_at": 10.0,
                        "client_pid": 123,
                    },
                )

        thread = threading.Thread(
            target=run_managed_exec,
            daemon=True,
        )
        thread.start()

        frames = _read_exec_frames(client)
        thread.join(timeout=5.0)

        assert frames[0]["type"] == "started"
        assert frames[-1]["type"] == "exit"
        by_type = {str(frame["type"]): frame for frame in frames[1:-1]}
        assert base64.b64decode(str(by_type["stdout"]["data_b64"])) == b"out"
        assert base64.b64decode(str(by_type["stderr"]["data_b64"])) == b"err"
        assert frames[-1]["exit_code"] == 7
        operation_id = str(frames[0]["operation_id"])
        history = mg.resolve_operation_history(operation_id, scope=task)
        completed = history.commits[0].metadata["command"]
        assert completed["status"] == "failed_exit"
        assert completed["exit_code"] == 7
    finally:
        mg.deactivate()


def test_session_exec_managed_stream_rejects_caller_supplied_cwd(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-managed-cwd-reject")
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        _install_managed_exec_overlay_state(
            daemon,
            scope_name=task.name,
            scope_instance_id=task.instance_id,
            mount_path=tmp_path,
        )
        client, server = socket.socketpair()

        def run_managed_exec() -> None:
            with server:
                daemon._handle_managed_exec_connection(
                    server,
                    {
                        "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                        "cwd": str(tmp_path.parent),
                        "scope": task.name,
                        "capture_requested": False,
                        "env": dict(os.environ),
                        "started_at": 10.0,
                        "client_pid": 123,
                    },
                )

        thread = threading.Thread(target=run_managed_exec, daemon=True)
        thread.start()

        frames = _read_exec_frames(client)
        thread.join(timeout=5.0)

        assert frames[-1] == {"type": "exit", "exit_code": 3}
        errors = [frame for frame in frames if frame.get("type") == "error"]
        assert errors
        assert "Unsupported managed exec parameter(s): cwd" in str(errors[0]["message"])
    finally:
        mg.deactivate()


def test_session_exec_managed_stream_rejects_cwd_escape_as_usage_error(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-managed-cwd-escape")
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        _install_managed_exec_overlay_state(
            daemon,
            scope_name=task.name,
            scope_instance_id=task.instance_id,
            mount_path=tmp_path,
        )
        client, server = socket.socketpair()

        def run_managed_exec() -> None:
            with server:
                daemon._handle_managed_exec_connection(
                    server,
                    {
                        "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                        "scope": task.name,
                        "cwd_subpath": "../outside",
                        "capture_requested": False,
                        "env": dict(os.environ),
                        "started_at": 10.0,
                        "client_pid": 123,
                    },
                )

        thread = threading.Thread(target=run_managed_exec, daemon=True)
        thread.start()

        frames = _read_exec_frames(client)
        thread.join(timeout=5.0)

        assert frames[-1] == {"type": "exit", "exit_code": 2}
        errors = [frame for frame in frames if frame.get("type") == "error"]
        assert errors
        assert "escapes overlay mount" in str(errors[0]["message"])
    finally:
        mg.deactivate()


def test_session_exec_managed_stream_rejects_invalid_scope_as_usage_error(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-managed-invalid-scope")
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        _install_managed_exec_overlay_state(
            daemon,
            scope_name=task.name,
            scope_instance_id=task.instance_id,
            mount_path=tmp_path,
        )
        client, server = socket.socketpair()

        def run_managed_exec() -> None:
            with server:
                daemon._handle_managed_exec_connection(
                    server,
                    {
                        "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                        "scope": "bad/scope",
                        "cwd_subpath": None,
                        "capture_requested": False,
                        "env": dict(os.environ),
                        "started_at": 10.0,
                        "client_pid": 123,
                    },
                )

        thread = threading.Thread(target=run_managed_exec, daemon=True)
        thread.start()

        frames = _read_exec_frames(client)
        thread.join(timeout=5.0)

        assert frames[-1] == {"type": "exit", "exit_code": 2}
        errors = [frame for frame in frames if frame.get("type") == "error"]
        assert errors
        assert "contains '/'" in str(errors[0]["message"])
    finally:
        mg.deactivate()


def test_session_exec_managed_stream_rejects_ground_scope_as_usage_error(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = "ground"
        _install_managed_exec_overlay_state(
            daemon,
            scope_name="ground",
            scope_instance_id=mg.ground.instance_id,
            mount_path=tmp_path,
        )
        client, server = socket.socketpair()

        def run_managed_exec() -> None:
            with server:
                daemon._handle_managed_exec_connection(
                    server,
                    {
                        "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                        "scope": None,
                        "cwd_subpath": None,
                        "capture_requested": False,
                        "env": dict(os.environ),
                        "started_at": 10.0,
                        "client_pid": 123,
                    },
                )

        thread = threading.Thread(target=run_managed_exec, daemon=True)
        thread.start()

        frames = _read_exec_frames(client)
        thread.join(timeout=5.0)

        assert frames[-1] == {"type": "exit", "exit_code": 2}
        errors = [frame for frame in frames if frame.get("type") == "error"]
        assert errors
        assert "session shell/exec on ground is disabled" in str(errors[0]["message"])
    finally:
        mg.deactivate()


def test_session_exec_managed_stream_renders_app_error_details(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        mg.fork(mg.ground, "alpha")
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = "ground"
        client, server = socket.socketpair()

        def run_managed_exec() -> None:
            with server:
                daemon._handle_managed_exec_connection(
                    server,
                    {
                        "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                        "scope": "beta",
                        "create": True,
                        "cwd_subpath": None,
                        "capture_requested": False,
                        "env": dict(os.environ),
                        "started_at": 10.0,
                        "client_pid": 123,
                    },
                )

        thread = threading.Thread(target=run_managed_exec, daemon=True)
        thread.start()

        frames = _read_exec_frames(client)
        thread.join(timeout=5.0)

        assert frames[-1] == {"type": "exit", "exit_code": 3}
        errors = [frame for frame in frames if frame.get("type") == "error"]
        assert errors
        message = str(errors[0]["message"])
        assert message.startswith("Error: cannot branch:")
        assert "AppCommandBlocked" not in message
        assert "parent already has live child scope 'alpha'" in message
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_session_exec_managed_stream_rejects_daemon_owned_child_env(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-managed-env")
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        _install_managed_exec_overlay_state(
            daemon,
            scope_name=task.name,
            scope_instance_id=task.instance_id,
            mount_path=tmp_path,
            hook_static_env={"VCS_CORE_HOOK_SOCKET": "/daemon/hook.sock"},
        )
        client, server = socket.socketpair()
        stale_env = {
            "DYLD_INSERT_LIBRARIES": "/stale/dyld.dylib",
            "USER_VAR": "preserved",
        }

        def run_managed_exec() -> None:
            with server:
                daemon._handle_managed_exec_connection(
                    server,
                    {
                        "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                        "scope": task.name,
                        "cwd_subpath": None,
                        "capture_requested": False,
                        "capture_debug_log": str(tmp_path / "fresh-debug.log"),
                        "env": stale_env,
                        "started_at": 10.0,
                        "client_pid": 123,
                    },
                )

        thread = threading.Thread(target=run_managed_exec, daemon=True)
        thread.start()

        frames = _read_exec_frames(client)
        thread.join(timeout=5.0)

        assert frames[-1] == {"type": "exit", "exit_code": 3}
        errors = [frame for frame in frames if frame.get("type") == "error"]
        assert errors
        assert "daemon-owned key" in str(errors[0]["message"])
        assert "DYLD_INSERT_LIBRARIES" in str(errors[0]["message"])
    finally:
        mg.deactivate()


def test_session_exec_managed_stream_closes_stdin_for_mvp(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-managed-stdin")
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        _install_managed_exec_overlay_state(
            daemon,
            scope_name=task.name,
            scope_instance_id=task.instance_id,
            mount_path=tmp_path,
        )
        client, server = socket.socketpair()

        def run_managed_exec() -> None:
            with server:
                daemon._handle_managed_exec_connection(
                    server,
                    {
                        "argv": [
                            sys.executable,
                            "-c",
                            "import sys; sys.exit(0 if sys.stdin.read() == '' else 9)",
                        ],
                        "scope": task.name,
                        "cwd_subpath": None,
                        "capture_requested": False,
                        "env": dict(os.environ),
                        "started_at": 10.0,
                        "client_pid": 123,
                    },
                )

        thread = threading.Thread(target=run_managed_exec, daemon=True)
        thread.start()

        frames = _read_exec_frames(client)
        thread.join(timeout=5.0)

        assert frames[-1] == {"type": "exit", "exit_code": 0}
    finally:
        mg.deactivate()


def test_session_exec_managed_stream_preserves_large_stdout(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-managed-large-output")
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        _install_managed_exec_overlay_state(
            daemon,
            scope_name=task.name,
            scope_instance_id=task.instance_id,
            mount_path=tmp_path,
        )
        client, server = socket.socketpair()

        def run_managed_exec() -> None:
            with server:
                daemon._handle_managed_exec_connection(
                    server,
                    {
                        "argv": [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1048576)"],
                        "scope": task.name,
                        "cwd_subpath": None,
                        "capture_requested": False,
                        "env": dict(os.environ),
                        "started_at": 10.0,
                        "client_pid": 123,
                    },
                )

        thread = threading.Thread(target=run_managed_exec, daemon=True)
        thread.start()

        frames = _read_exec_frames(client)
        thread.join(timeout=5.0)

        assert frames[-1] == {"type": "exit", "exit_code": 0}
        assert _joined_frame_payload(frames, "stdout") == b"x" * 1048576
    finally:
        mg.deactivate()


def test_session_exec_managed_stream_does_not_wait_for_background_stdout_holder(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-managed-background")
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        _install_managed_exec_overlay_state(
            daemon,
            scope_name=task.name,
            scope_instance_id=task.instance_id,
            mount_path=tmp_path,
        )
        client, server = socket.socketpair()
        script = (
            "import subprocess, sys; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], stdout=sys.stdout); "
            "sys.stdout.write('done'); sys.stdout.flush()"
        )

        def run_managed_exec() -> None:
            with server:
                daemon._handle_managed_exec_connection(
                    server,
                    {
                        "argv": [sys.executable, "-c", script],
                        "scope": task.name,
                        "cwd_subpath": None,
                        "capture_requested": False,
                        "env": dict(os.environ),
                        "started_at": 10.0,
                        "client_pid": 123,
                    },
                )

        thread = threading.Thread(target=run_managed_exec, daemon=True)
        thread.start()

        frames = _read_exec_frames(client, timeout=3.0)
        thread.join(timeout=1.0)

        assert not thread.is_alive()
        assert frames[-1] == {"type": "exit", "exit_code": 0}
        assert _joined_frame_payload(frames, "stdout") == b"done"
    finally:
        mg.deactivate()


def test_session_exec_managed_stream_terminates_background_process_group(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-managed-background-kill")
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        _install_managed_exec_overlay_state(
            daemon,
            scope_name=task.name,
            scope_instance_id=task.instance_id,
            mount_path=tmp_path,
        )
        client, server = socket.socketpair()
        pid_file = tmp_path / "child.pid"
        script = (
            "import os, subprocess, sys; "
            "proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
            f"open({str(pid_file)!r}, 'w').write(str(proc.pid)); "
            "sys.exit(0)"
        )

        def run_managed_exec() -> None:
            with server:
                daemon._handle_managed_exec_connection(
                    server,
                    {
                        "argv": [sys.executable, "-c", script],
                        "scope": task.name,
                        "cwd_subpath": None,
                        "capture_requested": False,
                        "env": dict(os.environ),
                        "started_at": 10.0,
                        "client_pid": 123,
                    },
                )

        thread = threading.Thread(target=run_managed_exec, daemon=True)
        thread.start()

        frames = _read_exec_frames(client)
        thread.join(timeout=5.0)

        assert frames[-1] == {"type": "exit", "exit_code": 0}
        assert pid_file.exists()
        child_pid = int(pid_file.read_text())
        assert _wait_for_process_exit(child_pid)
    finally:
        mg.deactivate()


def test_session_exec_managed_shutdown_terminates_running_command(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-managed-shutdown")
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        _install_managed_exec_overlay_state(
            daemon,
            scope_name=task.name,
            scope_instance_id=task.instance_id,
            mount_path=tmp_path,
        )
        client, server = socket.socketpair()

        def run_managed_exec() -> None:
            with server:
                daemon._handle_managed_exec_connection(
                    server,
                    {
                        "argv": [sys.executable, "-c", "import time; time.sleep(30)"],
                        "scope": task.name,
                        "cwd_subpath": None,
                        "capture_requested": False,
                        "env": dict(os.environ),
                        "started_at": 10.0,
                        "client_pid": 123,
                    },
                )

        thread = threading.Thread(target=run_managed_exec, daemon=True)
        thread.start()
        first_frame = json.loads(client.recv(65536).split(b"\n", 1)[0].decode())
        assert first_frame["type"] == "started"

        daemon._shutdown_managed_execs(timeout_seconds=2.0)
        frames = _read_exec_frames(client)
        thread.join(timeout=5.0)

        assert not thread.is_alive()
        assert frames[-1]["type"] == "exit"
        assert frames[-1]["exit_code"] in {143, 137}
    finally:
        mg.deactivate()


def test_session_exec_managed_downstream_broken_pipe_abandons_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    socket_path = Path(f"/tmp/vcs-core-managed-{os.getpid()}-{uuid.uuid4().hex}.sock")
    try:
        task = mg.fork(mg.ground, "task-managed-broken-pipe")
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        _install_managed_exec_overlay_state(
            daemon,
            scope_name=task.name,
            scope_instance_id=task.instance_id,
            mount_path=tmp_path,
        )
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        server.listen(1)
        server_errors: list[BaseException] = []

        def serve_one() -> None:
            try:
                conn, _ = server.accept()
                with conn:
                    daemon._handle_connection(conn)
            except BaseException as exc:
                server_errors.append(exc)
            finally:
                server.close()

        thread = threading.Thread(target=serve_one, daemon=True)
        thread.start()
        monkeypatch.setattr(
            "vcs_core._cli_ipc.live_session_info",
            lambda: SessionInfo(
                pid=os.getpid(),
                socket_path=str(socket_path),
                mount_path=str(tmp_path),
                workspace=str(tmp_path),
                started_at=time.time(),
            ),
        )

        result_code = run_managed_exec(
            argv=(
                sys.executable,
                "-c",
                (
                    "import sys, time\n"
                    "while True:\n"
                    "    sys.stdout.write('x' * 65536)\n"
                    "    sys.stdout.flush()\n"
                    "    time.sleep(0.001)\n"
                ),
            ),
            scope_name=task.name,
            create=False,
            parent=None,
            cwd_subpath=None,
            capture_requested=False,
            capture_debug=None,
            env=dict(os.environ),
            stdout=_BrokenPipeSink(),  # type: ignore[arg-type]
            stderr=io.BytesIO(),
            exit_code=3,
        )

        thread.join(timeout=5.0)

        assert result_code == 1
        assert not thread.is_alive()
        assert server_errors == []
        archived = [
            summary for summary in mg.archived_operations(max_count=10) if summary.kind == "vcs_core.session_exec"
        ]
        assert len(archived) == 1
        history = mg.resolve_operation_history(archived[0].operation_id, scope=task)
        completed = history.commits[0].metadata["command"]
        assert completed["status"] == "abandoned"
        assert completed["abandoned_reason"] == "client disconnected"
    finally:
        with contextlib.suppress(FileNotFoundError):
            socket_path.unlink()
        mg.deactivate()


def test_session_exec_managed_stream_child_exit_survives_recording_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-managed-recording-failure")
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        _install_managed_exec_overlay_state(
            daemon,
            scope_name=task.name,
            scope_instance_id=task.instance_id,
            mount_path=tmp_path,
        )

        def fail_record_outcome(params: dict[str, object]) -> dict[str, object]:
            del params
            raise RuntimeError("store unavailable")

        monkeypatch.setattr(daemon._managed_execution_service, "record_outcome", fail_record_outcome)
        client, server = socket.socketpair()

        def run_managed_exec() -> None:
            with server:
                daemon._handle_managed_exec_connection(
                    server,
                    {
                        "argv": [sys.executable, "-c", "raise SystemExit(7)"],
                        "scope": task.name,
                        "cwd_subpath": None,
                        "capture_requested": False,
                        "env": dict(os.environ),
                        "started_at": 10.0,
                        "client_pid": 123,
                    },
                )

        thread = threading.Thread(target=run_managed_exec, daemon=True)
        thread.start()

        frames = _read_exec_frames(client)
        thread.join(timeout=5.0)

        assert any(frame.get("type") == "recording_error" for frame in frames)
        assert frames[-1] == {"type": "exit", "exit_code": 7}
    finally:
        mg.deactivate()


def test_session_exec_managed_disconnect_after_child_exit_records_child_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-managed-post-exit-disconnect")
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        _install_managed_exec_overlay_state(
            daemon,
            scope_name=task.name,
            scope_instance_id=task.instance_id,
            mount_path=tmp_path,
        )

        def fake_drain_remaining_streams(*args: object, **kwargs: object) -> object:
            del args, kwargs
            if False:
                yield None
            return False

        monkeypatch.setattr(
            daemon._managed_execution_service,
            "_drain_remaining_streams",
            fake_drain_remaining_streams,
        )
        client, server = socket.socketpair()

        def run_managed_exec() -> None:
            with server:
                daemon._handle_managed_exec_connection(
                    server,
                    {
                        "argv": [sys.executable, "-c", "import time; time.sleep(0.05); raise SystemExit(7)"],
                        "scope": task.name,
                        "cwd_subpath": None,
                        "capture_requested": False,
                        "env": dict(os.environ),
                        "started_at": 10.0,
                        "client_pid": 123,
                    },
                )

        thread = threading.Thread(target=run_managed_exec, daemon=True)
        thread.start()

        frames = _read_exec_frames(client)
        thread.join(timeout=5.0)

        assert frames[0]["type"] == "started"
        assert all(frame.get("type") != "exit" for frame in frames)
        operation_id = str(frames[0]["operation_id"])
        history = mg.resolve_operation_history(operation_id, scope=task)
        completed = history.commits[0].metadata["command"]
        assert completed["status"] == "failed_exit"
        assert completed["exit_code"] == 7
        assert completed["transport_status"] == "client_disconnected"
    finally:
        mg.deactivate()


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"outcome": "success", "ended_at": 11.0, "exit_code": 7}, "requires exit_code=0"),
        ({"outcome": "failed_exit", "ended_at": 11.0}, "requires a positive exit_code"),
        ({"outcome": "signaled", "ended_at": 11.0}, "requires a positive signal"),
        ({"outcome": "launch_error", "ended_at": 11.0}, "requires launch_error"),
        ({"outcome": "abandoned", "ended_at": 11.0}, "requires abandoned_reason"),
        (
            {"outcome": "success", "ended_at": 11.0, "exit_code": 0, "signal": 2},
            "does not accept: signal",
        ),
    ],
)
def test_session_exec_envelope_dispatcher_rejects_invalid_outcomes(
    tmp_path: Path,
    params: dict[str, object],
    message: str,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-envelope-invalid")
        daemon = _daemon_stub(mg, task.name)
        dispatcher = SessionCommandDispatcher(daemon)

        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["false"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": False,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])

        with pytest.raises(ValueError, match=message):
            dispatcher.dispatch("exec_envelope_outcome", {"operation_id": operation_id, **params})

        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "failed_exit",
                "ended_at": 12.0,
                "exit_code": 7,
            },
        )
    finally:
        mg.deactivate()


def test_session_exec_startup_recovery_marks_abandoned_with_duration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-envelope-abandoned")
        daemon = _daemon_stub(mg, task.name)
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["sleep", "10"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])

        session_daemon = SessionDaemon(str(tmp_path))
        session_daemon._mg = mg
        monkeypatch.setattr("vcs_core._session.time.time", lambda: 12.0)

        session_daemon._recover_abandoned_session_exec_envelopes()

        history = mg.resolve_operation_history(operation_id, scope=task)
        assert history.summary.visibility == "archived"
        assert history.summary.status == "error"
        assert history.summary.final_phase == "aborted"
        completed = history.commits[0].metadata["command"]
        assert completed["status"] == "abandoned"
        assert completed["abandoned_reason"] == "session daemon startup recovery"
        assert completed["capture_status"] == "incomplete"
        assert completed["capture_stream_status"] == "incomplete"
        assert completed["capture_incomplete_reason"] == "session_daemon_startup_recovery"
        assert isinstance(completed["capture_epoch"], str)
        assert completed["started_at"] == 10.0
        assert completed["ended_at"] == 12.0
        assert completed["duration_seconds"] == 2.0
    finally:
        mg.deactivate()


def test_session_shell_capture_lease_recovery_marks_abandoned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-shell-lease-abandoned")
        daemon = _daemon_stub(mg, task.name, _daemon_instance_id="daemon-current")
        dispatcher = SessionCommandDispatcher(daemon)
        lease_id = _begin_shell_capture_lease(dispatcher, scope=task.name, shell_pid=456)

        session_daemon = SessionDaemon(str(tmp_path))
        session_daemon._mg = mg
        monkeypatch.setattr("vcs_core._session.time.time", lambda: 12.0)

        session_daemon._recover_abandoned_session_operations()

        history = mg.resolve_operation_history(lease_id, scope=task)
        assert history.summary.kind == "vcs_core.session_shell"
        assert history.summary.visibility == "archived"
        assert history.summary.status == "error"
        assert history.summary.final_phase == "aborted"
        completed = history.commits[0].metadata["shell"]
        assert completed["status"] == "abandoned"
        assert completed["abandoned_reason"] == "session daemon startup recovery"
        assert completed["capture_status"] == "incomplete"
        assert completed["capture_stream_status"] == "incomplete"
        assert completed["capture_incomplete_reason"] == "session_daemon_startup_recovery"
        assert completed["shell_pid"] == 456
        assert completed["started_at"] == 9.0
        assert completed["ended_at"] == 12.0
        assert completed["duration_seconds"] == 3.0
    finally:
        mg.deactivate()


def test_active_daemon_lease_exclusion_discriminates_by_daemon_instance(tmp_path: Path) -> None:
    """M3 + ylppksvk composition (second merge): query_readiness excludes only
    the *current* daemon's shell-capture lease from orphaned-op blockers. A
    different daemon id (e.g. a crashed prior session that left an open lease)
    does NOT match, so that lease correctly stays a blocker. This proves the
    daemon-instance scoping is not over-broad.

    The readiness request is scoped to the lease's own scope. ylppksvk's
    scope-narrowing (second merge) filters off-scope blockers, so the daemon
    discriminator is only observable on a same-scope request — which is also
    the safety-relevant case (a stale lease blocks a write to *its own* scope).
    This is the joint composition test: scope-narrowing keeps the on-scope
    lease, M3's daemon-exclusion removes it only for the owning daemon.
    """
    from vcs_core._query_readiness import ReadinessRequest
    from vcs_core._readiness_admission import _active_daemon_shell_lease_ids

    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-lease-discriminator")
        daemon = _daemon_stub(mg, task.name, _daemon_instance_id="daemon-current")
        dispatcher = SessionCommandDispatcher(daemon)
        lease_id = _begin_shell_capture_lease(dispatcher, scope=task.name)

        # Helper discriminates by daemon id: the owning daemon discovers the
        # lease; a foreign/stale daemon id does not.
        assert _active_daemon_shell_lease_ids(mg.store, "daemon-current") == frozenset({lease_id})
        assert _active_daemon_shell_lease_ids(mg.store, "daemon-stale") == frozenset()

        # Scope the request to the lease's own scope so ylppksvk's scope-narrowing
        # keeps the lease in the blocker set; the daemon discriminator (M3) is then
        # what excludes it for the owning daemon.
        request = ReadinessRequest.create(command="vcscore.runtime", scope=task.name)

        # Current daemon: the lease is excluded (and surfaced for audit).
        mg._active_daemon_instance_id = "daemon-current"
        current = mg.query_readiness(request)
        assert lease_id in current.excluded_daemon_lease_ids

        # Foreign daemon (crashed-prior-session case): the lease is NOT excluded
        # and remains a same-scope blocker (the safety property).
        mg._active_daemon_instance_id = "daemon-stale"
        stale = mg.query_readiness(request)
        assert lease_id not in stale.excluded_daemon_lease_ids
        assert any(
            blocker.item_id == f"recovery:orphaned_operation:{mg.store.operation_ref(lease_id)}"
            for blocker in stale.blockers
        )
    finally:
        mg.deactivate()


def test_session_exec_command_envelope_renders_in_operation_show_and_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-envelope-render")
        daemon = _daemon_stub(mg, task.name)
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["false"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        env = begin["env"]
        assert isinstance(env, dict)
        assert env["VCS_CORE_COMMAND_OPERATION_ID"] == operation_id
        assert isinstance(env["VCS_CORE_CAPTURE_EPOCH"], str)
        assert env["VCS_CORE_CAPTURE_ACTIVE"] == "1"
        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "failed_exit",
                "ended_at": 12.5,
                "exit_code": 7,
            },
        )
    finally:
        mg.deactivate()

    monkeypatch.chdir(tmp_path)
    show = runner.invoke(main, ["operation", "show", operation_id])
    recovery = runner.invoke(main, ["recovery"])

    assert show.exit_code == 0, show.output
    assert f"Operation:    {operation_id}" in show.output
    assert "Command:      false" in show.output
    assert f"CWD:          {tmp_path}" in show.output
    assert "Capture:      true" in show.output
    assert "Cmd Status:   failed_exit" in show.output
    assert "Capture State: complete" in show.output
    assert "Capture Stream: drained" in show.output
    assert "Exit Code:    7" in show.output
    assert "Duration:     2.500s" in show.output

    assert recovery.exit_code == 0, recovery.output
    assert "Archived recovery operations:" in recovery.output
    assert operation_id in recovery.output
    assert "session exec: false" in recovery.output


def test_session_shell_command_envelope_renders_shell_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-shell-envelope-render")
        daemon = _daemon_stub(mg, task.name, _daemon_instance_id="daemon-current")
        dispatcher = SessionCommandDispatcher(daemon)
        shell_lease_id = _begin_shell_capture_lease(dispatcher, scope=task.name)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-lc", "printf shell > shell.txt"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "capture_policy": "shell_command",
                "transport": "shell",
                "submitted_text": "printf shell > shell.txt",
                "shell_pid": 456,
                "shell_lease_id": shell_lease_id,
                "daemon_instance_id": "daemon-current",
                "started_at": 10.0,
                "client_pid": 456,
            },
        )
        operation_id = str(begin["operation_id"])
        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "failed_exit",
                "ended_at": 12.0,
                "exit_code": 7,
                "daemon_instance_id": "daemon-current",
            },
        )
        history = mg.resolve_operation_history(operation_id, scope=task)
        completed = history.commits[0].metadata["command"]
        assert completed["transport"] == "shell"
        assert completed["submitted_text"] == "printf shell > shell.txt"
        assert completed["shell_pid"] == 456
        assert completed["shell_lease_id"] == shell_lease_id
    finally:
        mg.deactivate()

    monkeypatch.chdir(tmp_path)
    show = runner.invoke(main, ["operation", "show", operation_id])
    recovery = runner.invoke(main, ["recovery"])

    assert show.exit_code == 0, show.output
    assert "Shell Command: printf shell > shell.txt" in show.output
    assert "Command:      bash -lc" not in show.output
    assert recovery.exit_code == 0, recovery.output
    assert operation_id in recovery.output
    assert "session shell: printf shell > shell.txt" in recovery.output


def test_shell_command_not_admitted_records_diagnostic_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-shell-not-admitted")
        daemon = _daemon_stub(mg, task.name, _daemon_instance_id="daemon-current")
        dispatcher = SessionCommandDispatcher(daemon)
        shell_lease_id = _begin_shell_capture_lease(dispatcher, scope=task.name)
        result = dispatcher.dispatch(
            "shell_command_not_admitted",
            {
                "cwd": str(tmp_path),
                "scope": task.name,
                "submitted_text": "printf missed > missed.txt",
                "shell_pid": 456,
                "shell_lease_id": shell_lease_id,
                "daemon_instance_id": "daemon-current",
                "started_at": 10.0,
                "ended_at": 10.25,
                "admission_error": "forced admission rejection",
            },
        )
        operation_id = str(result["operation_id"])
        history = mg.resolve_operation_history(operation_id, scope=task)
        completed = history.commits[0].metadata["command"]
        assert completed["transport"] == "shell"
        assert completed["submitted_text"] == "printf missed > missed.txt"
        assert completed["status"] == "abandoned"
        assert completed["abandoned_reason"] == "shell_command_not_admitted"
        assert completed["capture_status"] == "incomplete"
        assert completed["capture_stream_status"] == "not_admitted"
        assert completed["capture_incomplete_reason"] == "shell_command_not_admitted"
        assert completed["admission_error"] == "forced admission rejection"

        dispatcher.dispatch(
            "shell_capture_lease_outcome",
            {
                "operation_id": shell_lease_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
                "daemon_instance_id": "daemon-current",
            },
        )
    finally:
        mg.deactivate()

    monkeypatch.chdir(tmp_path)
    show = runner.invoke(main, ["operation", "show", operation_id])

    assert show.exit_code == 0, show.output
    assert "Shell Command: printf missed > missed.txt" in show.output
    assert "Capture State: incomplete" in show.output
    assert "Capture Stream: not_admitted" in show.output
    assert "Abandoned:    shell_command_not_admitted" in show.output
    assert "Capture Note: shell_command_not_admitted" in show.output


def test_shell_command_envelope_rejects_stale_daemon_instance_on_begin(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-shell-stale-begin")
        daemon = _daemon_stub(mg, task.name, _daemon_instance_id="daemon-current")
        dispatcher = SessionCommandDispatcher(daemon)

        with pytest.raises(ValueError, match="stale shell capture helper for daemon instance"):
            dispatcher.dispatch(
                "exec_envelope_begin",
                {
                    "argv": ["bash", "-lc", "printf shell > shell.txt"],
                    "cwd": str(tmp_path),
                    "scope": task.name,
                    "capture_requested": True,
                    "capture_policy": "shell_command",
                    "transport": "shell",
                    "submitted_text": "printf shell > shell.txt",
                    "shell_pid": 456,
                    "daemon_instance_id": "daemon-stale",
                    "started_at": 10.0,
                    "client_pid": 456,
                },
            )

        assert mg.store.list_open_operations(scope_ref=task.ref) == []
    finally:
        mg.deactivate()


def test_shell_command_envelope_rejects_missing_daemon_instance_on_begin(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-shell-missing-begin")
        daemon = _daemon_stub(mg, task.name, _daemon_instance_id="daemon-current")
        dispatcher = SessionCommandDispatcher(daemon)

        with pytest.raises(ValueError, match="stale shell capture helper for daemon instance"):
            dispatcher.dispatch(
                "exec_envelope_begin",
                {
                    "argv": ["bash", "-lc", "printf shell > shell.txt"],
                    "cwd": str(tmp_path),
                    "scope": task.name,
                    "capture_requested": True,
                    "capture_policy": "shell_command",
                    "transport": "shell",
                    "submitted_text": "printf shell > shell.txt",
                    "shell_pid": 456,
                    "started_at": 10.0,
                    "client_pid": 456,
                },
            )

        assert mg.store.list_open_operations(scope_ref=task.ref) == []
    finally:
        mg.deactivate()


def test_shell_command_envelope_rejects_mismatched_shell_pid_on_begin(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-shell-stale-pid")
        daemon = _daemon_stub(mg, task.name, _daemon_instance_id="daemon-current")
        dispatcher = SessionCommandDispatcher(daemon)
        shell_lease_id = _begin_shell_capture_lease(dispatcher, scope=task.name, shell_pid=456)

        with pytest.raises(ValueError, match="stale shell capture helper for shell pid"):
            dispatcher.dispatch(
                "exec_envelope_begin",
                {
                    "argv": ["bash", "-lc", "printf shell > shell.txt"],
                    "cwd": str(tmp_path),
                    "scope": task.name,
                    "capture_requested": True,
                    "capture_policy": "shell_command",
                    "transport": "shell",
                    "submitted_text": "printf shell > shell.txt",
                    "shell_pid": 789,
                    "shell_lease_id": shell_lease_id,
                    "daemon_instance_id": "daemon-current",
                    "started_at": 10.0,
                    "client_pid": 789,
                },
            )

        assert {operation.durable_id for operation in mg.store.list_open_operations(scope_ref=task.ref)} == {
            shell_lease_id,
        }
    finally:
        mg.deactivate()


def test_shell_command_envelope_rejects_stale_daemon_instance_on_outcome(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-shell-stale-outcome")
        daemon = _daemon_stub(mg, task.name, _daemon_instance_id="daemon-current")
        dispatcher = SessionCommandDispatcher(daemon)
        shell_lease_id = _begin_shell_capture_lease(dispatcher, scope=task.name)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-lc", "printf shell > shell.txt"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "capture_policy": "shell_command",
                "transport": "shell",
                "submitted_text": "printf shell > shell.txt",
                "shell_pid": 456,
                "shell_lease_id": shell_lease_id,
                "daemon_instance_id": "daemon-current",
                "started_at": 10.0,
                "client_pid": 456,
            },
        )
        operation_id = str(begin["operation_id"])

        with pytest.raises(ValueError, match="stale shell capture helper for daemon instance"):
            dispatcher.dispatch(
                "exec_envelope_outcome",
                {
                    "operation_id": operation_id,
                    "outcome": "success",
                    "ended_at": 12.0,
                    "exit_code": 0,
                    "daemon_instance_id": "daemon-stale",
                },
            )

        assert {operation.durable_id for operation in mg.store.list_open_operations(scope_ref=task.ref)} == {
            shell_lease_id,
            operation_id,
        }
        assert mg.archived_operations(operation_id=operation_id) == []
    finally:
        mg.deactivate()


def test_shell_command_envelope_rejects_missing_daemon_instance_on_outcome(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-shell-missing-outcome")
        daemon = _daemon_stub(mg, task.name, _daemon_instance_id="daemon-current")
        dispatcher = SessionCommandDispatcher(daemon)
        shell_lease_id = _begin_shell_capture_lease(dispatcher, scope=task.name)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-lc", "printf shell > shell.txt"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "capture_policy": "shell_command",
                "transport": "shell",
                "submitted_text": "printf shell > shell.txt",
                "shell_pid": 456,
                "shell_lease_id": shell_lease_id,
                "daemon_instance_id": "daemon-current",
                "started_at": 10.0,
                "client_pid": 456,
            },
        )
        operation_id = str(begin["operation_id"])

        with pytest.raises(ValueError, match="stale shell capture helper for daemon instance"):
            dispatcher.dispatch(
                "exec_envelope_outcome",
                {
                    "operation_id": operation_id,
                    "outcome": "success",
                    "ended_at": 12.0,
                    "exit_code": 0,
                },
            )

        assert {operation.durable_id for operation in mg.store.list_open_operations(scope_ref=task.ref)} == {
            shell_lease_id,
            operation_id,
        }
        assert mg.archived_operations(operation_id=operation_id) == []
    finally:
        mg.deactivate()


def test_exec_command_envelope_does_not_require_daemon_instance_id(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-exec-no-daemon-instance")
        daemon = _daemon_stub(mg, task.name, _daemon_instance_id="daemon-current")
        dispatcher = SessionCommandDispatcher(daemon)

        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["true"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": False,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 12.0,
                "exit_code": 0,
            },
        )

        completed = mg.resolve_operation_history(operation_id, scope=task).commits[0].metadata["command"]
        assert completed["argv"] == ["true"]
        assert "daemon_instance_id" not in completed
    finally:
        mg.deactivate()


def test_session_exec_envelope_success_is_archived_but_not_recovery_state(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-envelope-success")
        daemon = _daemon_stub(mg, task.name)
        dispatcher = SessionCommandDispatcher(daemon)

        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["true"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": False,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 10.5,
                "exit_code": 0,
            },
        )

        history = mg.resolve_operation_history(operation_id, scope=task)
        assert history.summary.visibility == "archived"
        assert history.summary.status == "ok"
        assert all(
            summary.operation_id != operation_id for summary in mg.recovery_snapshot().archived_recovery_operations
        )
    finally:
        mg.deactivate()


def test_session_exec_capture_gets_scope_writer_exclusivity(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=MockOverlayBackend(),
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-capture-exclusive", hints={"isolated": True})
        daemon = _daemon_stub(mg, task.name)
        dispatcher = SessionCommandDispatcher(daemon)
        first = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "sleep 1"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        first_operation_id = str(first["operation_id"])

        with pytest.raises(ValueError, match=r"session exec .* is still open"):
            dispatcher.dispatch(
                "exec_envelope_begin",
                {
                    "argv": ["bash", "-c", "printf later > out.txt"],
                    "cwd": str(tmp_path),
                    "scope": task.name,
                    "capture_requested": True,
                    "started_at": 10.5,
                    "client_pid": 124,
                },
            )
        with pytest.raises(ValueError, match=r"session exec .* is still open"):
            dispatcher.dispatch(
                "exec_envelope_begin",
                {
                    "argv": ["bash", "-c", "printf untracked > out.txt"],
                    "cwd": str(tmp_path),
                    "scope": task.name,
                    "capture_requested": False,
                    "started_at": 10.5,
                    "client_pid": 125,
                },
            )

        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": first_operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )
        second = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "true"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": False,
                "started_at": 12.0,
                "client_pid": 126,
            },
        )
        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": str(second["operation_id"]),
                "outcome": "success",
                "ended_at": 13.0,
                "exit_code": 0,
            },
        )
    finally:
        mg.deactivate()


def test_session_shell_capture_lease_blocks_scope_writers_until_released(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=MockOverlayBackend(),
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-shell-lease-exclusive", hints={"isolated": True})
        daemon = _daemon_stub(mg, task.name, _daemon_instance_id="daemon-current")
        dispatcher = SessionCommandDispatcher(daemon)
        lease_id = _begin_shell_capture_lease(dispatcher, scope=task.name)

        with pytest.raises(ValueError, match=r"session shell .* is still open"):
            dispatcher.dispatch(
                "exec_envelope_begin",
                {
                    "argv": ["bash", "-c", "printf plain > out.txt"],
                    "cwd": str(tmp_path),
                    "scope": task.name,
                    "capture_requested": False,
                    "started_at": 10.5,
                    "client_pid": 124,
                },
            )
        with pytest.raises(ValueError, match=r"session shell .* is still open"):
            _begin_shell_capture_lease(dispatcher, scope=task.name, shell_pid=789)

        shell_command = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-lc", "printf shell > out.txt"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "capture_policy": "shell_command",
                "transport": "shell",
                "submitted_text": "printf shell > out.txt",
                "shell_pid": 456,
                "shell_lease_id": lease_id,
                "daemon_instance_id": "daemon-current",
                "started_at": 11.0,
                "client_pid": 456,
            },
        )
        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": str(shell_command["operation_id"]),
                "outcome": "success",
                "ended_at": 11.5,
                "exit_code": 0,
                "daemon_instance_id": "daemon-current",
            },
        )

        with pytest.raises(ValueError, match=r"session shell .* is still open"):
            dispatcher.dispatch(
                "exec_envelope_begin",
                {
                    "argv": ["bash", "-c", "printf still-blocked > out.txt"],
                    "cwd": str(tmp_path),
                    "scope": task.name,
                    "capture_requested": False,
                    "started_at": 12.0,
                    "client_pid": 125,
                },
            )

        dispatcher.dispatch(
            "shell_capture_lease_outcome",
            {
                "operation_id": lease_id,
                "outcome": "success",
                "ended_at": 13.0,
                "exit_code": 0,
                "daemon_instance_id": "daemon-current",
            },
        )
        second = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "true"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": False,
                "started_at": 14.0,
                "client_pid": 126,
            },
        )
        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": str(second["operation_id"]),
                "outcome": "success",
                "ended_at": 15.0,
                "exit_code": 0,
            },
        )
    finally:
        mg.deactivate()


def test_session_shell_capture_lease_blocks_delegated_scope_writers(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=MockOverlayBackend(),
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-shell-lease-delegated", hints={"isolated": True})
        other = mg.fork(task, "task-shell-lease-other", hints={"isolated": True})
        daemon = _daemon_stub(mg, task.name, _daemon_instance_id="daemon-current")
        dispatcher = SessionCommandDispatcher(daemon)
        lease_id = _begin_shell_capture_lease(dispatcher, scope=task.name)

        with pytest.raises(ValueError, match=r"Cannot execute command on scope .*session shell .* is still open"):
            dispatcher.dispatch(
                "exec",
                {
                    "binding": "marker",
                    "command": "mark",
                    "scope": task.name,
                    "params": {"label": "blocked-exec"},
                },
            )
        exec_result = dispatcher.dispatch(
            "exec",
            {
                "binding": "marker",
                "command": "mark",
                "scope": other.name,
                "params": {"label": "other-exec"},
            },
        )
        assert exec_result["oids"]

        dispatcher.dispatch(
            "shell_capture_lease_outcome",
            {
                "operation_id": lease_id,
                "outcome": "success",
                "ended_at": 13.0,
                "exit_code": 0,
                "daemon_instance_id": "daemon-current",
            },
        )
        unblocked = dispatcher.dispatch(
            "exec",
            {
                "binding": "marker",
                "command": "mark",
                "scope": task.name,
                "params": {"label": "unblocked-exec"},
            },
        )
        assert unblocked["oids"]
    finally:
        mg.deactivate()


def test_session_shell_capture_lease_blocks_scope_lifecycle_until_released(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=MockOverlayBackend(),
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-shell-lease-lifecycle", hints={"isolated": True})
        daemon = _daemon_stub(mg, task.name, _daemon_instance_id="daemon-current")
        dispatcher = SessionCommandDispatcher(daemon)
        lease_id = _begin_shell_capture_lease(dispatcher, scope=task.name)

        with pytest.raises(ValueError, match=r"Cannot merge scope .*session shell .* is still open"):
            dispatcher.dispatch("merge", {"name": task.name})
        with pytest.raises(ValueError, match=r"Cannot discard scope .*session shell .* is still open"):
            dispatcher.dispatch("discard", {"name": task.name})

        dispatcher.dispatch(
            "shell_capture_lease_outcome",
            {
                "operation_id": lease_id,
                "outcome": "success",
                "ended_at": 13.0,
                "exit_code": 0,
                "daemon_instance_id": "daemon-current",
            },
        )

        result = dispatcher.dispatch("merge", {"name": task.name})
        assert result == {"merged": task.name, "into": "ground"}
    finally:
        mg.deactivate()


def test_session_exec_envelope_blocks_scope_lifecycle_until_released(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=MockOverlayBackend(),
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-exec-lifecycle", hints={"isolated": True})
        daemon = _daemon_stub(mg, task.name)
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "sleep 1"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": False,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )

        with pytest.raises(ValueError, match=r"Cannot discard scope .*session exec .* is still open"):
            dispatcher.dispatch("discard", {"name": task.name})

        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": str(begin["operation_id"]),
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )

        result = dispatcher.dispatch("discard", {"name": task.name})
        assert result == {"discarded": task.name}
    finally:
        mg.deactivate()


def test_session_exec_capture_rejects_existing_scope_writer(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=MockOverlayBackend(),
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-capture-existing-writer", hints={"isolated": True})
        daemon = _daemon_stub(mg, task.name)
        dispatcher = SessionCommandDispatcher(daemon)
        first = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "sleep 1"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": False,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        with pytest.raises(ValueError, match=r"session exec .* is still open"):
            dispatcher.dispatch(
                "exec_envelope_begin",
                {
                    "argv": ["bash", "-c", "sleep 1"],
                    "cwd": str(tmp_path),
                    "scope": task.name,
                    "capture_requested": False,
                    "started_at": 10.1,
                    "client_pid": 124,
                },
            )

        with pytest.raises(ValueError, match=r"session exec .* is still open"):
            dispatcher.dispatch(
                "exec_envelope_begin",
                {
                    "argv": ["bash", "-c", "printf captured > out.txt"],
                    "cwd": str(tmp_path),
                    "scope": task.name,
                    "capture_requested": True,
                    "started_at": 10.5,
                    "client_pid": 125,
                },
            )

        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": str(first["operation_id"]),
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )
    finally:
        mg.deactivate()


def test_session_exec_capture_reduces_raw_events_into_linked_operation(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-capture-reducer", hints={"isolated": True})
        backend.delete_file(task.name, "transient.txt")
        daemon = _daemon_stub(mg, task.name)
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "create-delete"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        mg._record_capture_event(
            "filesystem",
            FsCaptureEvent(
                op="write_close",
                scope=task.name,
                scope_instance_id=task.instance_id,
                path="transient.txt",
                pid=123,
                proc_seq=1,
            ),
            command_operation_id=operation_id,
            global_seq=1,
            event_seq=1,
            capture_mechanism="preload",
        )
        mg._record_capture_event(
            "filesystem",
            FsCaptureEvent(
                op="unlink",
                scope=task.name,
                scope_instance_id=task.instance_id,
                path="transient.txt",
                pid=123,
                proc_seq=2,
            ),
            command_operation_id=operation_id,
            global_seq=2,
            event_seq=2,
            capture_mechanism="preload",
        )

        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )

        reducer_id = f"red_{operation_id}"
        history = mg.resolve_operation_history(reducer_id, scope=task)
        assert history.summary.kind == "vcs_core.fs_capture_reduction"
        assert history.summary.effect_count == 0
        completed = history.commits[0].metadata["capture"]
        assert completed["covered_paths"] == ["transient.txt"]
        assert completed["capture_stream_status"] == "drained"
        assert completed["reduced_effect_count"] == 0

        command_history = mg.resolve_operation_history(operation_id, scope=task)
        raw_events = [commit for commit in command_history.commits if commit.metadata.get("type") == "CaptureEvent"]
        assert len(raw_events) == 2

        payload, provenance, records = _read_workspace_shadow_candidate(mg, reducer_id)
        assert provenance.transition.semantic_op == "workspace-capture-reduction"
        assert provenance.transition.ingress_kind == "reduce"
        assert provenance.transition.binding == "workspace"
        assert payload["state_manifest"]["entries"] == []
        proof_records = [record for record in records if record.evidence_kind == "reduce:reduced-state-proof"]
        raw_records = [record for record in records if record.evidence_kind == "capture:filesystem-event"]
        assert [record.operation_id for record in proof_records] == [reducer_id]
        assert {record.operation_id for record in raw_records} == {operation_id}
        assert len(raw_records) == 2
        assert proof_records[0].stable_observation["proof"]["covered_paths"] == ["transient.txt"]
        assert proof_records[0].stable_observation["proof"]["reduced_effect_count"] == 0
    finally:
        mg.deactivate()


def test_session_exec_capture_hook_dispatch_retains_concurrent_raw_events(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-hook-stress")
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        daemon._hook_manager = HookManager(
            mg,
            workspace=tmp_path,
            repo_path=tmp_path / ".vcscore",
            socket_path=str(tmp_path / ".vcscore" / "session-hook.sock"),
        )
        daemon._hook_manager.install_bindings(mg.bindings)
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-lc", "stress"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        capture_epoch = str(begin["env"]["VCS_CORE_CAPTURE_EPOCH"])

        events = [
            {
                "binding_name": "filesystem",
                "hook_id": "filesystem-direct",
                "kind": "ld_preload",
                "phase": "point",
                "scope": task.name,
                "scope_instance_id": task.instance_id,
                "pid": pid,
                "proc_seq": proc_seq,
                "timestamp_ns": 42 + proc_seq,
                "command_operation_id": operation_id,
                "capture_epoch": capture_epoch,
                "payload": {
                    "op": "write_close" if proc_seq % 2 else "unlink",
                    "path": "shared.txt",
                    "seq": proc_seq,
                    "capture_mechanism": "preload",
                },
            }
            for proc_seq in range(1, 9)
            for pid in (101, 102, 103, 104)
        ]

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda payload: daemon._handle_hook_line(json.dumps(payload)), events))

        history = mg.store.read_operation_history(mg.store.operation_ref(operation_id))
        raw_events = [commit for commit in history.commits if commit.metadata.get("type") == "CaptureEvent"]

        assert len(raw_events) == len(events)
        assert sorted(commit.metadata["pid"] for commit in raw_events) == sorted(event["pid"] for event in events)
        assert sorted(commit.metadata["proc_seq"] for commit in raw_events) == sorted(
            event["proc_seq"] for event in events
        )
        assert sorted(commit.metadata["global_seq"] for commit in raw_events) == list(range(1, len(events) + 1))
        assert daemon._hook_outcomes["persisted"] == len(events)
        assert daemon._hook_processed_seq == len(events)
    finally:
        mg.deactivate()


def test_session_exec_capture_outcome_waits_for_globally_accepted_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-capture-race", hints={"isolated": True})
        backend.write_file(task.name, "out.txt", b"final")
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "printf final > out.txt"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        with daemon._lock:
            seq = daemon._hook_frontier.accept_next()
            assert seq == 1
            daemon._hook_accepted_seq = daemon._hook_frontier.accepted_seq
            daemon._hook_processed_seq = daemon._hook_frontier.processed_seq

        seen_min_accepted_seq: list[int] = []

        def wait_for_hook_drain(
            *,
            min_accepted_seq: int = 0,
            timeout_seconds: float = 1.0,
            quiet_period_seconds: float = 0.05,
        ) -> bool:
            del timeout_seconds, quiet_period_seconds
            seen_min_accepted_seq.append(min_accepted_seq)
            accepted = daemon._capture_authority.accept_event(operation_id, pid=123, proc_seq=1, global_seq=1)
            assert accepted.accepted
            mg._record_capture_event(
                "filesystem",
                FsCaptureEvent(
                    op="write_close",
                    scope=task.name,
                    scope_instance_id=task.instance_id,
                    path="out.txt",
                    pid=123,
                    proc_seq=1,
                ),
                command_operation_id=operation_id,
                global_seq=1,
                event_seq=1,
                capture_mechanism="preload",
            )
            daemon._capture_authority.mark_processed(operation_id, global_seq=1)
            with daemon._lock:
                daemon._hook_frontier.mark_terminal(1)
                daemon._hook_processed_seq = daemon._hook_frontier.processed_seq
            return True

        monkeypatch.setattr(daemon, "_wait_for_hook_drain", wait_for_hook_drain)
        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )

        assert seen_min_accepted_seq == [1]
        assert daemon._capture_authority.active_count() == 0
        late = daemon._capture_authority.accept_event(operation_id, pid=123, proc_seq=2, global_seq=2)
        assert not late.accepted
        assert late.reason == "capture_complete"
        reducer_id = f"red_{operation_id}"
        history = mg.resolve_operation_history(reducer_id, scope=task)
        assert history.summary.kind == "vcs_core.fs_capture_reduction"
        assert history.summary.effect_count == 1
        completed = history.commits[0].metadata["capture"]
        assert completed["capture_status"] == "complete"
        assert completed["capture_stream_status"] == "drained"
        assert completed["covered_paths"] == ["out.txt"]

        payload, provenance, records = _read_workspace_shadow_candidate(mg, reducer_id)
        manifest = payload["state_manifest"]
        assert manifest["entries"] == [
            {
                "path": "out.txt",
                "state": "present",
                "mode": 0o100644,
                "content_digest": f"sha256:{hashlib.sha256(b'final').hexdigest()}",
            }
        ]
        proof_record = next(record for record in records if record.evidence_kind == "reduce:reduced-state-proof")
        assert proof_record.stable_observation["proof"]["manifest_digest"] == canonical_digest(manifest)
        selected_world = mg._world_storage().read_world(task.ref)
        assert selected_world.snapshot.head_for("workspace").head == provenance.head
        assert selected_world.operation_final["selected"] == {"workspace": provenance.head}
        assert "refs/vcscore/ground" not in mg._world_storage().world_store.repo.references
    finally:
        mg.deactivate()

    monkeypatch.chdir(tmp_path)
    show = runner.invoke(main, ["operation", "show", operation_id])
    assert show.exit_code == 0, show.output
    assert "Capture Shadow: selected" in show.output
    assert "Shadow Manifest: sha256:" in show.output
    assert "Shadow Raw Evidence: 1" in show.output
    assert "Shadow Proof Evidence: 1" in show.output


def test_session_exec_capture_shadow_records_executable_manifest_entry(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-capture-shadow-mode", hints={"isolated": True})
        script = b"#!/bin/sh\n"
        backend.write_file(task.name, "run.sh", script, mode=0o100755)
        daemon = _daemon_stub(mg, task.name)
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "printf '#!/bin/sh\\n' > run.sh; chmod +x run.sh"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        mg._record_capture_event(
            "filesystem",
            FsCaptureEvent(
                op="metadata_change",
                scope=task.name,
                scope_instance_id=task.instance_id,
                path="run.sh",
                pid=123,
                proc_seq=1,
            ),
            command_operation_id=operation_id,
            global_seq=1,
            event_seq=1,
            capture_mechanism="preload",
        )

        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )

        payload, _provenance, _records = _read_workspace_shadow_candidate(mg, f"red_{operation_id}")
        assert payload["state_manifest"]["entries"] == [
            {
                "path": "run.sh",
                "state": "present",
                "mode": 0o100755,
                "content_digest": f"sha256:{hashlib.sha256(script).hexdigest()}",
            }
        ]
    finally:
        mg.deactivate()


def test_session_exec_capture_scoped_selection_discards_without_ground_leak(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-capture-v2-discard", hints={"isolated": True})
        operation_id = _record_single_write_capture(tmp_path, mg, backend, task, path="child.txt", content=b"child")
        payload, provenance, _records = _read_workspace_shadow_candidate(mg, f"red_{operation_id}")
        assert payload["state_manifest"]["entries"][0]["path"] == "child.txt"
        assert mg._world_storage().read_world(task.ref).snapshot.head_for("workspace").head == provenance.head
        assert "refs/vcscore/ground" not in mg._world_storage().world_store.repo.references

        mg.discard(task)

        assert "refs/vcscore/ground" not in mg._world_storage().world_store.repo.references
        assert task.ref not in mg._world_storage().world_store.repo.references
        assert mg.store.read_workspace_file(mg.ground.ref, "child.txt") is None
    finally:
        mg.deactivate()


def test_session_exec_capture_scoped_selection_merges_to_ground(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-capture-v2-merge", hints={"isolated": True})
        operation_id = _record_single_write_capture(tmp_path, mg, backend, task, path="merged.txt", content=b"merged")
        _payload, provenance, _records = _read_workspace_shadow_candidate(mg, f"red_{operation_id}")

        mg.merge(task, mg.ground)

        selected_world = mg._world_storage().read_world(mg.ground.ref)
        assert selected_world.snapshot.head_for("workspace").head == provenance.head
        assert task.ref not in mg._world_storage().world_store.repo.references
        assert mg.store.read_workspace_file(mg.ground.ref, "merged.txt") == b"merged"
    finally:
        mg.deactivate()

    monkeypatch.chdir(tmp_path)
    show = runner.invoke(main, ["operation", "show", operation_id])
    assert show.exit_code == 0, show.output
    assert "Capture Shadow: selected" in show.output


def test_scope_fork_without_v2_parent_does_not_initialize_world_storage(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-no-v2-parent")

        assert not default_world_storage_root(tmp_path / ".vcscore").exists()

        mg.discard(task)
    finally:
        mg.deactivate()


def test_scope_fork_inherits_parent_v2_world_authority(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        first = mg.fork(mg.ground, "task-v2-fork-seed", hints={"isolated": True})
        _record_single_write_capture(tmp_path, mg, backend, first, path="seed.txt", content=b"seed")
        mg.merge(first, mg.ground)

        manager = mg._world_storage()
        assert first.ref not in manager.world_store.repo.references
        ground_world_oid = str(manager.world_store.repo.references[mg.ground.ref].target)
        second = mg.fork(mg.ground, "task-v2-fork-child", hints={"isolated": True})

        assert str(manager.world_store.repo.references[second.ref].target) == ground_world_oid
        assert world_fork_origin_receipt_ref(second.ref) in manager.world_store.repo.references

        mg.discard(second)
    finally:
        mg.deactivate()


def test_scope_merge_removes_unchanged_forked_v2_authority(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        seed = mg.fork(mg.ground, "task-v2-unchanged-seed", hints={"isolated": True})
        _record_single_write_capture(tmp_path, mg, backend, seed, path="seed.txt", content=b"seed")
        mg.merge(seed, mg.ground)
        manager = mg._world_storage()
        ground_world_oid = str(manager.world_store.repo.references[mg.ground.ref].target)

        unchanged = mg.fork(mg.ground, "task-v2-unchanged-child", hints={"isolated": True})
        assert str(manager.world_store.repo.references[unchanged.ref].target) == ground_world_oid

        mg.merge(unchanged, mg.ground)

        assert str(manager.world_store.repo.references[mg.ground.ref].target) == ground_world_oid
        assert unchanged.ref not in manager.world_store.repo.references
    finally:
        mg.deactivate()


def test_session_exec_capture_shadow_records_delete_manifest_entry(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        seed = mg.fork(mg.ground, "task-capture-shadow-delete-seed")
        mg.exec("filesystem", "write", path="old.txt", content=b"before", scope=seed)
        mg.merge(seed, mg.ground)
        task = mg.fork(mg.ground, "task-capture-shadow-delete", hints={"isolated": True})
        backend.delete_file(task.name, "old.txt")
        daemon = _daemon_stub(mg, task.name)
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "rm old.txt"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        mg._record_capture_event(
            "filesystem",
            FsCaptureEvent(
                op="unlink",
                scope=task.name,
                scope_instance_id=task.instance_id,
                path="old.txt",
                pid=123,
                proc_seq=1,
            ),
            command_operation_id=operation_id,
            global_seq=1,
            event_seq=1,
            capture_mechanism="preload",
        )

        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )

        history = mg.resolve_operation_history(f"red_{operation_id}", scope=task)
        assert history.summary.effect_count == 1
        assert any(commit.metadata.get("type") == "FileDelete" for commit in history.commits)
        payload, _provenance, records = _read_workspace_shadow_candidate(mg, f"red_{operation_id}")
        assert payload["state_manifest"]["entries"] == []
        proof_record = next(record for record in records if record.evidence_kind == "reduce:reduced-state-proof")
        assert proof_record.stable_observation["proof"]["deleted_paths"] == ["old.txt"]
    finally:
        mg.deactivate()


def test_session_exec_failed_capture_shadow_preserves_failed_origin(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-capture-shadow-failed-origin", hints={"isolated": True})
        backend.write_file(task.name, "out.txt", b"failed-final")
        daemon = _daemon_stub(mg, task.name)
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "printf failed-final > out.txt; exit 7"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        mg._record_capture_event(
            "filesystem",
            FsCaptureEvent(
                op="write_close",
                scope=task.name,
                scope_instance_id=task.instance_id,
                path="out.txt",
                pid=123,
                proc_seq=1,
            ),
            command_operation_id=operation_id,
            global_seq=1,
            event_seq=1,
            capture_mechanism="preload",
        )

        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "failed_exit",
                "ended_at": 11.0,
                "exit_code": 7,
            },
        )

        _payload, _provenance, records = _read_workspace_shadow_candidate(mg, f"red_{operation_id}")
        proof_record = next(record for record in records if record.evidence_kind == "reduce:reduced-state-proof")
        assert proof_record.stable_observation["proof"]["failed_command_origin"] == {
            "operation_id": operation_id,
            "exit_code": 7,
            "signal": None,
        }
    finally:
        mg.deactivate()


def test_session_exec_capture_shadow_preserves_duplicate_interleaved_raw_evidence(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-capture-shadow-interleaved", hints={"isolated": True})
        backend.write_file(task.name, "a.txt", b"a2")
        backend.write_file(task.name, "b.txt", b"b2")
        daemon = _daemon_stub(mg, task.name)
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "printf a2 > a.txt; printf b2 > b.txt"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        events = (
            ("a.txt", 101, 2, 1),
            ("b.txt", 202, 1, 2),
            ("a.txt", 101, 3, 3),
            ("b.txt", 202, 4, 4),
        )
        for path, pid, proc_seq, global_seq in events:
            mg._record_capture_event(
                "filesystem",
                FsCaptureEvent(
                    op="write_close",
                    scope=task.name,
                    scope_instance_id=task.instance_id,
                    path=path,
                    pid=pid,
                    proc_seq=proc_seq,
                ),
                command_operation_id=operation_id,
                global_seq=global_seq,
                event_seq=global_seq,
                capture_mechanism="preload",
            )

        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )

        _payload, _provenance, records = _read_workspace_shadow_candidate(mg, f"red_{operation_id}")
        raw_records = [record for record in records if record.evidence_kind == "capture:filesystem-event"]
        assert [record.operation_id for record in raw_records] == [operation_id] * 4
        ordered_raw_records = sorted(raw_records, key=lambda record: int(record.stable_observation["global_seq"]))
        assert [record.stable_observation["global_seq"] for record in ordered_raw_records] == [1, 2, 3, 4]
        assert [record.stable_observation["path"] for record in ordered_raw_records] == [
            "a.txt",
            "b.txt",
            "a.txt",
            "b.txt",
        ]
    finally:
        mg.deactivate()


def test_session_exec_capture_shadow_failure_preserves_scalar_reduction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-capture-shadow-fail", hints={"isolated": True})
        backend.write_file(task.name, "out.txt", b"final")
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        dispatcher = SessionCommandDispatcher(daemon)

        def fail_shadow(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise RuntimeError("shadow unavailable")

        monkeypatch.setattr(mg, "_shadow_workspace_capture_reduction", fail_shadow)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "printf final > out.txt"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        mg._record_capture_event(
            "filesystem",
            FsCaptureEvent(
                op="write_close",
                scope=task.name,
                scope_instance_id=task.instance_id,
                path="out.txt",
                pid=123,
                proc_seq=1,
            ),
            command_operation_id=operation_id,
            global_seq=1,
            event_seq=1,
            capture_mechanism="preload",
        )

        outcome = dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )

        assert "recording_error" not in outcome
        reducer_id = f"red_{operation_id}"
        history = mg.resolve_operation_history(reducer_id, scope=task)
        assert history.summary.kind == "vcs_core.fs_capture_reduction"
        assert history.summary.effect_count == 1
        completed = history.commits[0].metadata["capture"]
        assert completed["capture_status"] == "complete"
        assert completed["reduced_effect_count"] == 1
    finally:
        mg.deactivate()


def test_session_exec_incomplete_capture_does_not_publish_shadow_candidate_after_prior_shadow(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        first_task = mg.fork(mg.ground, "task-capture-shadow-prior", hints={"isolated": True})
        backend.write_file(first_task.name, "first.txt", b"first")
        first_daemon = _daemon_stub(mg, first_task.name)
        first_dispatcher = SessionCommandDispatcher(first_daemon)
        first_begin = first_dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "printf first > first.txt"],
                "cwd": str(tmp_path),
                "scope": first_task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        first_operation_id = str(first_begin["operation_id"])
        mg._record_capture_event(
            "filesystem",
            FsCaptureEvent(
                op="write_close",
                scope=first_task.name,
                scope_instance_id=first_task.instance_id,
                path="first.txt",
                pid=123,
                proc_seq=1,
            ),
            command_operation_id=first_operation_id,
            global_seq=1,
            event_seq=1,
            capture_mechanism="preload",
        )
        first_dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": first_operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )
        assert _workspace_shadow_candidate_exists(mg, f"red_{first_operation_id}")
        mg.merge(first_task, mg.ground)

        second_task = mg.fork(mg.ground, "task-capture-shadow-timeout", hints={"isolated": True})
        backend.write_file(second_task.name, "second.txt", b"second")
        second_daemon = _daemon_stub(mg, second_task.name, _wait_for_hook_drain=lambda **_kwargs: False)
        second_dispatcher = SessionCommandDispatcher(second_daemon)
        second_begin = second_dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "printf second > second.txt"],
                "cwd": str(tmp_path),
                "scope": second_task.name,
                "capture_requested": True,
                "started_at": 12.0,
                "client_pid": 123,
            },
        )
        second_operation_id = str(second_begin["operation_id"])
        mg._record_capture_event(
            "filesystem",
            FsCaptureEvent(
                op="write_close",
                scope=second_task.name,
                scope_instance_id=second_task.instance_id,
                path="second.txt",
                pid=123,
                proc_seq=1,
            ),
            command_operation_id=second_operation_id,
            global_seq=2,
            event_seq=1,
            capture_mechanism="preload",
        )

        second_dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": second_operation_id,
                "outcome": "success",
                "ended_at": 13.0,
                "exit_code": 0,
            },
        )

        second_history = mg.resolve_operation_history(second_operation_id, scope=second_task)
        second_completed = second_history.commits[0].metadata["command"]
        assert second_completed["capture_status"] == "incomplete"
        assert second_completed["capture_incomplete_reason"] == "hook_drain_timeout"
        assert not mg.store.operation_id_exists(f"red_{second_operation_id}")
        assert not _workspace_shadow_candidate_exists(mg, f"red_{second_operation_id}")
    finally:
        mg.deactivate()


def test_session_exec_capture_reduction_failure_marks_incomplete_and_finalizes_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-capture-reduce-fail", hints={"isolated": True})
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        dispatcher = SessionCommandDispatcher(daemon)

        def fail_reduce(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise RuntimeError("reducer unavailable")

        monkeypatch.setattr(mg, "_reduce_capture_for_command_operation", fail_reduce)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "printf final > out.txt"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        assert daemon._capture_authority.accept_event(operation_id, pid=123, proc_seq=1, global_seq=1).accepted
        daemon._capture_authority.mark_processed(operation_id, global_seq=1)

        outcome = dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )

        assert outcome["recording_error"] == "capture reduction failed: reducer unavailable"
        assert daemon._capture_authority.active_count() == 0
        late = daemon._capture_authority.accept_event(operation_id, pid=123, proc_seq=2, global_seq=2)
        assert not late.accepted
        assert late.reason == "capture_incomplete"
        history = mg.resolve_operation_history(operation_id, scope=task)
        completed = history.commits[0].metadata["command"]
        assert completed["status"] == "success"
        assert completed["capture_status"] == "incomplete"
        assert completed["capture_incomplete_reason"] == "capture_reduction_failed"
        assert not mg.store.operation_id_exists(f"red_{operation_id}")
        assert not default_world_storage_root(tmp_path / ".vcscore").exists()
    finally:
        mg.deactivate()


def test_session_exec_capture_reduction_finalize_failure_leaves_no_open_reducer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-capture-reduce-finalize-fail", hints={"isolated": True})
        backend.write_file(task.name, "out.txt", b"final")
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "printf final > out.txt"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        assert daemon._capture_authority.accept_event(operation_id, pid=123, proc_seq=1, global_seq=1).accepted
        mg._record_capture_event(
            "filesystem",
            FsCaptureEvent(
                op="write_close",
                scope=task.name,
                scope_instance_id=task.instance_id,
                path="out.txt",
                pid=123,
                proc_seq=1,
            ),
            command_operation_id=operation_id,
            global_seq=1,
            event_seq=1,
            capture_mechanism="preload",
        )
        daemon._capture_authority.mark_processed(operation_id, global_seq=1)

        real_finalize = mg.store.finalize_operation

        def fail_reducer_finalize(*args: object, **kwargs: object) -> str:
            operation = args[0]
            if getattr(operation, "kind", None) == CAPTURE_REDUCTION_KIND:
                raise RuntimeError("finalize failed after reducer begin")
            return real_finalize(*args, **kwargs)

        monkeypatch.setattr(mg.store, "finalize_operation", fail_reducer_finalize)

        outcome = dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )

        assert outcome["recording_error"] == "capture reduction failed: finalize failed after reducer begin"
        open_reducers = [op for op in mg.store.list_open_operations() if op.kind == CAPTURE_REDUCTION_KIND]
        assert open_reducers == []
        history = mg.resolve_operation_history(operation_id, scope=task)
        completed = history.commits[0].metadata["command"]
        assert completed["capture_status"] == "incomplete"
        assert completed["capture_incomplete_reason"] == "capture_reduction_failed"
    finally:
        mg.deactivate()


def test_session_exec_archive_failure_falls_back_to_aborted_terminal_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-archive-fallback")
        daemon = _daemon_stub(mg, task.name)
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["true"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": False,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])

        def fail_complete(*args: object, **kwargs: object) -> str:
            del args, kwargs
            raise RuntimeError("archive ref unavailable")

        monkeypatch.setattr(mg.store, "complete_operation_to_archive", fail_complete)

        outcome = dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )

        assert outcome["recording_error"] == "session exec outcome archive failed: archive ref unavailable"
        history = mg.resolve_operation_history(operation_id, scope=task)
        completed = history.commits[0].metadata["command"]
        assert history.summary.status == "error"
        assert history.summary.final_phase == "aborted"
        assert completed["status"] == "success"
        assert completed["recording_status"] == "failed"
        assert completed["recording_error"] == "session exec outcome archive failed: archive ref unavailable"
        assert mg.store.list_open_operations() == []
    finally:
        mg.deactivate()


def test_session_exec_archive_ref_publish_failure_does_not_double_terminal_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-archive-publish-fail")
        daemon = _daemon_stub(mg, task.name)
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["true"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": False,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        real_create_or_update_reference = store_module.create_or_update_reference
        failed_once = False

        def fail_first_archive_ref_publish(*args: object, **kwargs: object) -> object:
            nonlocal failed_once
            ref = args[1]
            if isinstance(ref, str) and ref == f"refs/vcscore/archive/ops/{operation_id}" and not failed_once:
                failed_once = True
                raise RuntimeError("archive ref publish failed")
            return real_create_or_update_reference(*args, **kwargs)

        monkeypatch.setattr(store_module, "create_or_update_reference", fail_first_archive_ref_publish)

        outcome = dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )

        assert outcome["recording_error"] == "session exec outcome archive failed: archive ref publish failed"
        assert mg.store.list_open_operations() == []
        history = mg.resolve_operation_history(operation_id, scope=task)
        assert history.summary.final_phase == "aborted"
        assert _terminal_operation_phases(history) == ["aborted"]
        completed = history.commits[0].metadata["command"]
        assert completed["status"] == "success"
        assert completed["recording_status"] == "failed"
    finally:
        mg.deactivate()


def test_session_exec_archive_ref_published_then_failure_cleans_open_ref_without_double_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-archive-published-then-fail")
        daemon = _daemon_stub(mg, task.name)
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["true"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": False,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        real_create_or_update_reference = store_module.create_or_update_reference
        real_delete_ref_if_ref_exists = mg.store._delete_ref_if_ref_exists
        deleted_refs: list[tuple[str, str]] = []
        failed_once = False

        def publish_archive_ref_then_fail(*args: object, **kwargs: object) -> object:
            nonlocal failed_once
            ref = args[1]
            result = real_create_or_update_reference(*args, **kwargs)
            if isinstance(ref, str) and ref == f"refs/vcscore/archive/ops/{operation_id}" and not failed_once:
                failed_once = True
                raise RuntimeError("post-publish archive failure")
            return result

        def record_delete_ref_if_ref_exists(*, ref: str, required_ref: str) -> bool:
            deleted_refs.append((ref, required_ref))
            return real_delete_ref_if_ref_exists(ref=ref, required_ref=required_ref)

        monkeypatch.setattr(store_module, "create_or_update_reference", publish_archive_ref_then_fail)
        monkeypatch.setattr(
            mg.store, "_delete_ref_if_exists", lambda ref: pytest.fail("split ref cleanup is not atomic")
        )
        monkeypatch.setattr(mg.store, "_delete_ref_if_ref_exists", record_delete_ref_if_ref_exists)

        outcome = dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )

        assert outcome["recording_error"] == "session exec outcome archive failed: post-publish archive failure"
        assert mg.store.list_open_operations() == []
        assert deleted_refs == [
            (mg.store.operation_ref(operation_id), f"refs/vcscore/archive/ops/{operation_id}"),
        ]
        history = mg.resolve_operation_history(operation_id, scope=task)
        assert history.summary.final_phase == "completed"
        assert history.summary.status == "ok"
        assert _terminal_operation_phases(history) == ["completed"]
        completed = history.commits[0].metadata["command"]
        assert completed["status"] == "success"
    finally:
        mg.deactivate()


def test_session_exec_capture_archive_failure_preserves_finalized_reducer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-capture-archive-fallback", hints={"isolated": True})
        backend.write_file(task.name, "out.txt", b"final")
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "printf final > out.txt"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        assert daemon._capture_authority.accept_event(operation_id, pid=123, proc_seq=1, global_seq=1).accepted
        mg._record_capture_event(
            "filesystem",
            FsCaptureEvent(
                op="write_close",
                scope=task.name,
                scope_instance_id=task.instance_id,
                path="out.txt",
                pid=123,
                proc_seq=1,
            ),
            command_operation_id=operation_id,
            global_seq=1,
            event_seq=1,
            capture_mechanism="preload",
        )
        daemon._capture_authority.mark_processed(operation_id, global_seq=1)

        real_complete = mg.store.complete_operation_to_archive

        def fail_session_exec_archive(*args: object, **kwargs: object) -> str:
            operation = args[0]
            if getattr(operation, "kind", None) == "vcs_core.session_exec":
                raise RuntimeError("archive ref unavailable")
            return real_complete(*args, **kwargs)

        monkeypatch.setattr(mg.store, "complete_operation_to_archive", fail_session_exec_archive)

        outcome = dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )

        assert outcome["recording_error"] == "session exec outcome archive failed: archive ref unavailable"
        assert mg.store.list_open_operations() == []
        reducer_history = mg.resolve_operation_history(f"red_{operation_id}", scope=task)
        assert reducer_history.summary.kind == CAPTURE_REDUCTION_KIND
        assert reducer_history.summary.effect_count == 1
        command_history = mg.resolve_operation_history(operation_id, scope=task)
        assert command_history.summary.status == "error"
        completed = command_history.commits[0].metadata["command"]
        assert completed["status"] == "success"
        assert completed["recording_status"] == "failed"
    finally:
        mg.deactivate()


def test_session_exec_capture_authority_finalize_failure_warns_without_blocking_archive(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate(defer_orphan_detection=True)
    try:
        task = mg.fork(mg.ground, "task-capture-finalize-failure")

        class FailingCaptureAuthority:
            def begin(self, operation_id: str, *, require_lifecycle: bool = False) -> None:
                del operation_id, require_lifecycle

            def finalize(self, operation_id: str) -> None:
                del operation_id
                raise RuntimeError("authority cleanup unavailable")

        daemon = _daemon_stub(mg, task.name, _capture_authority=FailingCaptureAuthority())
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["true"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])

        outcome = dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )

        assert outcome["recording_error"] == "capture authority finalize failed: authority cleanup unavailable"
        history = mg.resolve_operation_history(operation_id, scope=task)
        completed = history.commits[0].metadata["command"]
        assert history.summary.status == "ok"
        assert completed["status"] == "success"
        assert completed["capture_status"] == "complete"
    finally:
        mg.deactivate()


def test_session_exec_capture_timeout_marks_command_incomplete_and_skips_reducer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-capture-timeout", hints={"isolated": True})
        backend.write_file(task.name, "out.txt", b"final")
        backend.write_file(task.name, "unrelated.txt", b"fallback")
        daemon = _daemon_stub(mg, task.name, _wait_for_hook_drain=lambda **_kwargs: False)
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "printf final > out.txt"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        mg._record_capture_event(
            "filesystem",
            FsCaptureEvent(
                op="write_close",
                scope=task.name,
                scope_instance_id=task.instance_id,
                path="out.txt",
                pid=123,
                proc_seq=1,
            ),
            command_operation_id=operation_id,
            global_seq=1,
            event_seq=1,
            capture_mechanism="preload",
        )

        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )

        history = mg.resolve_operation_history(operation_id, scope=task)
        completed = history.commits[0].metadata["command"]
        assert completed["capture_status"] == "incomplete"
        assert completed["capture_stream_status"] == "incomplete"
        assert completed["capture_incomplete_reason"] == "hook_drain_timeout"
        assert not mg.store.operation_id_exists(f"red_{operation_id}")
        assert not default_world_storage_root(tmp_path / ".vcscore").exists()

        mg.merge(task, mg.ground)
        file_creates = [
            effect
            for effect in mg.store.filter_effects(effect_type="FileCreate", substrate="filesystem", ref=mg.ground.ref)
            if effect.metadata.get("path") == "out.txt"
        ]
        assert len(file_creates) == 1
        assert file_creates[0].metadata.get("capture_record") != "reduction"
        assert file_creates[0].metadata["reconcile_reason"] == "capture_incomplete:hook_drain_timeout"
        assert file_creates[0].metadata["reconcile_command_operation_id"] == operation_id
        unrelated_creates = [
            effect
            for effect in mg.store.filter_effects(effect_type="FileCreate", substrate="filesystem", ref=mg.ground.ref)
            if effect.metadata.get("path") == "unrelated.txt"
        ]
        assert len(unrelated_creates) == 1
        assert unrelated_creates[0].metadata["reconcile_reason"] == "missing_direct_create_or_patch"
        assert "reconcile_command_operation_id" not in unrelated_creates[0].metadata
    finally:
        mg.deactivate()

    monkeypatch.chdir(tmp_path)
    show = runner.invoke(main, ["operation", "show", operation_id])
    assert show.exit_code == 0, show.output
    assert "Capture State: incomplete" in show.output
    assert "Capture Stream: incomplete" in show.output
    assert "Capture Note: hook_drain_timeout" in show.output


def test_session_exec_failed_capture_fallback_preserves_failed_origin(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-capture-failed-fallback", hints={"isolated": True})
        backend.write_file(task.name, "out.txt", b"failed-final")
        daemon = _daemon_stub(mg, task.name, _wait_for_hook_drain=lambda **_kwargs: False)
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "printf failed-final > out.txt; exit 7"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        mg._record_capture_event(
            "filesystem",
            FsCaptureEvent(
                op="write_close",
                scope=task.name,
                scope_instance_id=task.instance_id,
                path="out.txt",
                pid=123,
                proc_seq=1,
            ),
            command_operation_id=operation_id,
            global_seq=1,
            event_seq=1,
            capture_mechanism="preload",
        )

        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "failed_exit",
                "ended_at": 11.0,
                "exit_code": 7,
            },
        )

        mg.merge(task, mg.ground)
        file_creates = [
            effect
            for effect in mg.store.filter_effects(effect_type="FileCreate", substrate="filesystem", ref=mg.ground.ref)
            if effect.metadata.get("path") == "out.txt"
        ]
        assert len(file_creates) == 1
        assert file_creates[0].metadata["reconcile_reason"] == "capture_incomplete:hook_drain_timeout"
        assert file_creates[0].metadata["reconcile_command_operation_id"] == operation_id
        assert file_creates[0].metadata["failed_command_origin"] == {
            "operation_id": operation_id,
            "exit_code": 7,
            "signal": None,
        }
    finally:
        mg.deactivate()


def test_session_exec_capture_proc_seq_gap_marks_command_incomplete(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-capture-gap", hints={"isolated": True})
        backend.write_file(task.name, "out.txt", b"final")
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "printf final > out.txt"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        accepted = daemon._capture_authority.accept_event(operation_id, pid=123, proc_seq=2, global_seq=1)
        assert accepted.accepted
        mg._record_capture_event(
            "filesystem",
            FsCaptureEvent(
                op="write_close",
                scope=task.name,
                scope_instance_id=task.instance_id,
                path="out.txt",
                pid=123,
                proc_seq=2,
            ),
            command_operation_id=operation_id,
            global_seq=1,
            event_seq=2,
            capture_mechanism="preload",
        )
        daemon._capture_authority.mark_processed(operation_id, global_seq=1)

        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )

        history = mg.resolve_operation_history(operation_id, scope=task)
        completed = history.commits[0].metadata["command"]
        assert completed["capture_status"] == "incomplete"
        assert completed["capture_stream_status"] == "incomplete"
        assert completed["capture_incomplete_reason"] == "hook_proc_seq_gap"
        assert not mg.store.operation_id_exists(f"red_{operation_id}")
    finally:
        mg.deactivate()


def test_session_shell_capture_child_finish_without_start_marks_incomplete(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-shell-child-missing-start", hints={"isolated": True})
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        daemon._daemon_instance_id = "daemon-current"
        dispatcher = SessionCommandDispatcher(daemon)
        shell_lease_id = _begin_shell_capture_lease(dispatcher, scope=task.name, shell_pid=101)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-lc", "python child.py"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "capture_policy": "shell_command",
                "transport": "shell",
                "submitted_text": "python child.py",
                "shell_pid": 101,
                "shell_lease_id": shell_lease_id,
                "daemon_instance_id": "daemon-current",
                "started_at": 10.0,
                "client_pid": 101,
            },
        )
        operation_id = str(begin["operation_id"])
        assert daemon._capture_authority.accept_event(operation_id, pid=202, proc_seq=1, global_seq=1).accepted
        mg._record_capture_event(
            "filesystem",
            FsCaptureEvent(
                op="write_close",
                scope=task.name,
                scope_instance_id=task.instance_id,
                path="out.txt",
                pid=202,
                proc_seq=1,
            ),
            command_operation_id=operation_id,
            global_seq=1,
            event_seq=1,
            capture_mechanism="preload",
        )
        daemon._capture_authority.mark_processed(operation_id, global_seq=1)
        assert daemon._capture_authority.finish_process(operation_id, pid=202, last_proc_seq=1).accepted
        assert daemon._capture_authority.finish_shell_command(operation_id, pid=101, proc_seq=1).accepted

        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
                "daemon_instance_id": "daemon-current",
            },
        )

        history = mg.resolve_operation_history(operation_id, scope=task)
        completed = history.commits[0].metadata["command"]
        assert completed["capture_status"] == "incomplete"
        assert completed["capture_stream_status"] == "incomplete"
        assert completed["capture_incomplete_reason"] == "missing_process_start"
        assert not mg.store.operation_id_exists(f"red_{operation_id}")
    finally:
        mg.deactivate()


def test_session_shell_capture_fd_context_crossing_marks_incomplete(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-shell-fd-cross", hints={"isolated": True})
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        daemon._daemon_instance_id = "daemon-current"
        dispatcher = SessionCommandDispatcher(daemon)
        shell_lease_id = _begin_shell_capture_lease(dispatcher, scope=task.name, shell_pid=101)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-lc", "printf cross >&4"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "capture_policy": "shell_command",
                "transport": "shell",
                "submitted_text": "printf cross >&4",
                "shell_pid": 101,
                "shell_lease_id": shell_lease_id,
                "daemon_instance_id": "daemon-current",
                "started_at": 10.0,
                "client_pid": 101,
            },
        )
        operation_id = str(begin["operation_id"])
        assert daemon._capture_authority.accept_event(operation_id, pid=101, proc_seq=1, global_seq=1).accepted
        mg._record_capture_event(
            "filesystem",
            FsCaptureEvent(
                op="write_observed",
                scope=task.name,
                scope_instance_id=task.instance_id,
                path="cross-fd.txt",
                pid=101,
                proc_seq=1,
            ),
            command_operation_id=operation_id,
            global_seq=1,
            event_seq=1,
            capture_mechanism="preload",
        )
        daemon._capture_authority.mark_processed(operation_id, global_seq=1)
        assert daemon._capture_authority.finish_shell_command(operation_id, pid=101, proc_seq=2).accepted

        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
                "daemon_instance_id": "daemon-current",
            },
        )

        history = mg.resolve_operation_history(operation_id, scope=task)
        completed = history.commits[0].metadata["command"]
        assert completed["capture_status"] == "incomplete"
        assert completed["capture_incomplete_reason"] == "fd_context_crossed_command"
        assert not mg.store.operation_id_exists(f"red_{operation_id}")
    finally:
        mg.deactivate()


def test_session_shell_capture_dirty_fd_left_open_marks_incomplete(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-shell-fd-open", hints={"isolated": True})
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        daemon._daemon_instance_id = "daemon-current"
        dispatcher = SessionCommandDispatcher(daemon)
        shell_lease_id = _begin_shell_capture_lease(dispatcher, scope=task.name, shell_pid=101)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-lc", "exec 4>cross-fd.txt"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "capture_policy": "shell_command",
                "transport": "shell",
                "submitted_text": "exec 4>cross-fd.txt",
                "shell_pid": 101,
                "shell_lease_id": shell_lease_id,
                "daemon_instance_id": "daemon-current",
                "started_at": 10.0,
                "client_pid": 101,
            },
        )
        operation_id = str(begin["operation_id"])
        assert daemon._capture_authority.accept_event(operation_id, pid=101, proc_seq=1, global_seq=1).accepted
        mg._record_capture_event(
            "filesystem",
            FsCaptureEvent(
                op="write_open",
                scope=task.name,
                scope_instance_id=task.instance_id,
                path="cross-fd.txt",
                pid=101,
                proc_seq=1,
            ),
            command_operation_id=operation_id,
            global_seq=1,
            event_seq=1,
            capture_mechanism="preload",
        )
        daemon._capture_authority.mark_processed(operation_id, global_seq=1)
        assert daemon._capture_authority.finish_shell_command(operation_id, pid=101, proc_seq=2).accepted

        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
                "daemon_instance_id": "daemon-current",
            },
        )

        history = mg.resolve_operation_history(operation_id, scope=task)
        completed = history.commits[0].metadata["command"]
        assert completed["capture_status"] == "incomplete"
        assert completed["capture_incomplete_reason"] == "dirty_fd_left_open"
        assert not mg.store.operation_id_exists(f"red_{operation_id}")
    finally:
        mg.deactivate()


def test_session_shell_capture_duplicate_fd_open_marks_incomplete(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-shell-fd-duplicate-open", hints={"isolated": True})
        daemon = SessionDaemon(str(tmp_path))
        daemon._mg = mg
        daemon._current_scope_name = task.name
        daemon._daemon_instance_id = "daemon-current"
        dispatcher = SessionCommandDispatcher(daemon)
        shell_lease_id = _begin_shell_capture_lease(dispatcher, scope=task.name, shell_pid=101)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-lc", "exec 4>cross-fd.txt 5>cross-fd.txt; printf x >&4"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "capture_policy": "shell_command",
                "transport": "shell",
                "submitted_text": "exec 4>cross-fd.txt 5>cross-fd.txt; printf x >&4",
                "shell_pid": 101,
                "shell_lease_id": shell_lease_id,
                "daemon_instance_id": "daemon-current",
                "started_at": 10.0,
                "client_pid": 101,
            },
        )
        operation_id = str(begin["operation_id"])
        event_ops = ("write_open", "write_open", "write_observed", "write_close")
        for seq, op in enumerate(event_ops, start=1):
            assert daemon._capture_authority.accept_event(operation_id, pid=101, proc_seq=seq, global_seq=seq).accepted
            mg._record_capture_event(
                "filesystem",
                FsCaptureEvent(
                    op=op,
                    scope=task.name,
                    scope_instance_id=task.instance_id,
                    path="cross-fd.txt",
                    pid=101,
                    proc_seq=seq,
                ),
                command_operation_id=operation_id,
                global_seq=seq,
                event_seq=seq,
                capture_mechanism="preload",
            )
            daemon._capture_authority.mark_processed(operation_id, global_seq=seq)
        assert daemon._capture_authority.finish_shell_command(operation_id, pid=101, proc_seq=5).accepted

        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
                "daemon_instance_id": "daemon-current",
            },
        )

        history = mg.resolve_operation_history(operation_id, scope=task)
        completed = history.commits[0].metadata["command"]
        assert completed["capture_status"] == "incomplete"
        assert completed["capture_incomplete_reason"] == "fd_context_crossed_command"
        assert not mg.store.operation_id_exists(f"red_{operation_id}")
    finally:
        mg.deactivate()


def test_session_exec_capture_reducer_prevents_overlay_duplicate_on_merge(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-capture-final", hints={"isolated": True})
        backend.write_file(task.name, "out.txt", b"final")
        daemon = _daemon_stub(mg, task.name)
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "printf final > out.txt"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        mg._record_capture_event(
            "filesystem",
            FsCaptureEvent(
                op="write_close",
                scope=task.name,
                scope_instance_id=task.instance_id,
                path="out.txt",
                pid=123,
                proc_seq=1,
            ),
            command_operation_id=operation_id,
            global_seq=1,
            event_seq=1,
            capture_mechanism="preload",
        )

        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )
        backend.write_file(task.name, "late.txt", b"late")
        mg._record_capture_diagnostic(
            "filesystem",
            FsCaptureEvent(
                op="write_close",
                scope=task.name,
                scope_instance_id=task.instance_id,
                path="late.txt",
                pid=123,
                proc_seq=2,
            ),
            command_operation_id=operation_id,
            global_seq=2,
            event_seq=2,
            capture_mechanism="preload",
            reason="late_event_after_finalization",
        )
        mg.merge(task, mg.ground)

        file_creates = [
            effect
            for effect in mg.store.filter_effects(effect_type="FileCreate", substrate="filesystem", ref=mg.ground.ref)
            if effect.metadata.get("path") == "out.txt"
        ]
        assert len(file_creates) == 1
        assert file_creates[0].metadata["capture_record"] == "reduction"
        late_creates = [
            effect
            for effect in mg.store.filter_effects(effect_type="FileCreate", substrate="filesystem", ref=mg.ground.ref)
            if effect.metadata.get("path") == "late.txt"
        ]
        assert len(late_creates) == 1
        assert late_creates[0].metadata["reconcile_reason"] == "capture_incomplete:late_event_after_finalization"
        assert late_creates[0].metadata["reconcile_command_operation_id"] == operation_id
    finally:
        mg.deactivate()


def test_session_exec_capture_reconciles_same_path_overlay_drift_after_reduction(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)
    store = VcsCore.from_config(str(tmp_path)).store
    backend = MockOverlayBackend()
    mg = make_marker_filesystem_vcscore(
        tmp_path,
        declarative=False,
        backend=backend,
        activate=True,
        store=store,
    )
    try:
        task = mg.fork(mg.ground, "task-capture-drift", hints={"isolated": True})
        backend.write_file(task.name, "out.txt", b"captured")
        daemon = _daemon_stub(mg, task.name)
        dispatcher = SessionCommandDispatcher(daemon)
        begin = dispatcher.dispatch(
            "exec_envelope_begin",
            {
                "argv": ["bash", "-c", "printf captured > out.txt"],
                "cwd": str(tmp_path),
                "scope": task.name,
                "capture_requested": True,
                "started_at": 10.0,
                "client_pid": 123,
            },
        )
        operation_id = str(begin["operation_id"])
        mg._record_capture_event(
            "filesystem",
            FsCaptureEvent(
                op="write_close",
                scope=task.name,
                scope_instance_id=task.instance_id,
                path="out.txt",
                pid=123,
                proc_seq=1,
            ),
            command_operation_id=operation_id,
            global_seq=1,
            event_seq=1,
            capture_mechanism="preload",
        )

        dispatcher.dispatch(
            "exec_envelope_outcome",
            {
                "operation_id": operation_id,
                "outcome": "success",
                "ended_at": 11.0,
                "exit_code": 0,
            },
        )
        payload, _provenance, _records = _read_workspace_shadow_candidate(mg, f"red_{operation_id}")
        assert payload["state_manifest"]["entries"] == [
            {
                "path": "out.txt",
                "state": "present",
                "mode": 0o100644,
                "content_digest": f"sha256:{hashlib.sha256(b'captured').hexdigest()}",
            }
        ]
        backend.write_file(task.name, "out.txt", b"late")
        mg.merge(task, mg.ground)

        assert mg.store.read_workspace_file(mg.ground.ref, "out.txt") == b"late"
        out_effects = [
            effect
            for effect in mg.store.filter_effects(substrate="filesystem", ref=mg.ground.ref)
            if effect.metadata.get("path") == "out.txt"
        ]
        reduction_effects = [effect for effect in out_effects if effect.metadata.get("capture_record") == "reduction"]
        reconciled_effects = [effect for effect in out_effects if effect.metadata.get("capture_mode") == "reconciled"]
        assert len(reduction_effects) == 1
        assert len(reconciled_effects) == 1
        assert reconciled_effects[0].metadata["reconcile_reason"] == "missing_direct_create_or_patch"
    finally:
        mg.deactivate()
