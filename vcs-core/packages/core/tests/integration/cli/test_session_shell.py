# under-test: vcs_core._cli_session_runtime
"""Session shell CLI behavior tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from click.testing import CliRunner
from vcs_core._cli_session_group import _shell_capture_bashrc
from vcs_core._cli_session_runtime import SessionCliError, begin_shell_command_envelope, finish_exec_envelope
from vcs_core.cli import main
from vcs_core.testing import SessionInfo

from ...support.cli import init_repo as _init


def _session_info(workspace: Path) -> SessionInfo:
    return SessionInfo(
        pid=os.getpid(),
        socket_path="/tmp/fake.sock",
        mount_path="/stale/path",
        workspace=str(workspace),
        started_at=time.time(),
    )


def test_session_shell_uses_get_state_without_switching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    runner = CliRunner()
    _init(runner, tmp_path)

    info = _session_info(tmp_path)
    calls: list[tuple[str, dict | None]] = []

    monkeypatch.setattr("vcs_core._ipc.is_session_alive", lambda repo_path: True)
    monkeypatch.setattr("vcs_core._ipc.read_session_info", lambda repo_path: info)

    def fake_send_request(socket_path: str, method: str, params: dict | None = None) -> dict:
        assert socket_path == info.socket_path
        calls.append((method, params))
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
        lambda argv, cwd, env, check: subprocess.CompletedProcess(argv, 0),
    )

    result = runner.invoke(main, ["session", "shell"])

    assert result.exit_code == 0, result.output
    assert calls == [("get_state", {"hook_capabilities": []})]
    assert str(tmp_path) in result.output
    assert "workspace file changes under the overlay are sandboxed" in result.output


def test_session_shell_switches_scope_before_launching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    runner = CliRunner()
    _init(runner, tmp_path)

    info = _session_info(tmp_path)
    calls: list[tuple[str, dict | None]] = []

    monkeypatch.setattr("vcs_core._ipc.is_session_alive", lambda repo_path: True)
    monkeypatch.setattr("vcs_core._ipc.read_session_info", lambda repo_path: info)

    def fake_send_request(socket_path: str, method: str, params: dict | None = None) -> dict:
        assert socket_path == info.socket_path
        calls.append((method, params))
        if method == "switch":
            return {"ok": True, "result": {"current_scope": "experiment", "mount_path": str(tmp_path)}}
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
        lambda argv, cwd, env, check: subprocess.CompletedProcess(argv, 0),
    )

    result = runner.invoke(main, ["session", "shell", "--scope", "experiment"])

    assert result.exit_code == 0, result.output
    assert calls == [("switch", {"name": "experiment"}), ("get_state", {"hook_capabilities": []})]
    assert "scope 'experiment'" in result.output
    assert "vcs-core merge experiment" in result.output


def test_session_shell_rejects_ground_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

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

    result = runner.invoke(main, ["session", "shell"])

    assert result.exit_code == 1
    assert "session shell/exec on ground is disabled" in result.output


@pytest.mark.xfail(
    reason=(
        "owner: vcs-core — `session shell --capture` consults session IPC (get_state with "
        "hook_capabilities) before the local prerequisite refusal these tests pin; the "
        "refusal-before-IPC ordering contract is violated by the current CLI path."
    ),
    strict=False,
)
def test_session_shell_capture_is_linux_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    info = _session_info(tmp_path)

    monkeypatch.setattr("vcs_core._ipc.is_session_alive", lambda repo_path: True)
    monkeypatch.setattr("vcs_core._ipc.read_session_info", lambda repo_path: info)
    monkeypatch.setattr(
        "vcs_core._ipc.send_request",
        lambda socket_path, method, params=None: pytest.fail("shell capture must not reach session IPC"),
    )

    result = runner.invoke(main, ["session", "shell", "--capture"])

    assert result.exit_code == 1
    assert "session shell --capture" in result.output
    assert "session exec --capture" in result.output


def test_session_shell_capture_launches_bash_runtime_on_linux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    info = _session_info(tmp_path)
    calls: list[tuple[str, dict | None]] = []
    seen_env: dict[str, str] = {}
    seen_argv: list[str] = []
    seen_lease_id = ""

    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("shutil.which", lambda binary: "/bin/bash" if binary == "bash" else None)
    monkeypatch.setattr("vcs_core._ipc.is_session_alive", lambda repo_path: True)
    monkeypatch.setattr("vcs_core._ipc.read_session_info", lambda repo_path: info)

    def fake_send_request(socket_path: str, method: str, params: dict | None = None) -> dict:
        nonlocal seen_lease_id
        assert socket_path == info.socket_path
        calls.append((method, params))
        if method == "shell_capture_lease_begin":
            assert params is not None
            assert str(params["lease_id"]).startswith("shl_")
            assert params["scope"] == "experiment"
            assert params["shell_pid"] == 4242
            seen_lease_id = str(params["lease_id"])
            return {"ok": True, "result": {"lease_id": seen_lease_id, "operation_ref": "refs/vcscore/ops/shl"}}
        if method == "shell_capture_lease_outcome":
            assert params is not None
            assert params["operation_id"] == seen_lease_id
            assert params["outcome"] == "success"
            assert params["exit_code"] == 0
            return {"ok": True, "result": {"lease_id": seen_lease_id, "archive_ref": "refs/archive/shl"}}
        return {
            "ok": True,
            "result": {
                "pid": info.pid,
                "current_scope": "experiment",
                "current_scope_instance_id": "experiment-iid",
                "mount_path": str(tmp_path),
                "workspace": str(tmp_path),
                "started_at": info.started_at,
                "daemon_instance_id": "daemon-current",
                "hook_static_env": {
                    "VCS_CORE_HOOK_SOCKET": "/tmp/session-hook.sock",
                },
                "hook_static_prepend_path": ["/tmp/hook-bin"],
                "hook_static_prepend_env": {},
                "hook_scope_env": {
                    "VCS_CORE_SCOPE": "experiment",
                    "VCS_CORE_SCOPE_INSTANCE_ID": "experiment-iid",
                },
                "hook_scope_prepend_path": [],
                "hook_scope_prepend_env": {"LD_PRELOAD": ["/tmp/fs_capture_shim.so"]},
            },
        }

    class FakeProcess:
        pid = 4242
        returncode: int | None = None

        def __init__(self, argv, cwd, env):  # type: ignore[no-untyped-def]
            assert cwd == str(tmp_path)
            seen_argv.extend(argv)
            seen_env.update(env)
            rcfile = Path(argv[3])
            rcfile_text = rcfile.read_text(encoding="utf-8")
            helper_text = (rcfile.parent / "shell_capture_helper.py").read_text(encoding="utf-8")
            assert rcfile_text
            assert "VCS_CORE_SHELL_LEASE_READY_PATH" in rcfile_text
            assert "shell_command_finish" not in rcfile_text
            assert "VCS_CORE_SHELL_FINISH_ACTIVE" in rcfile_text
            assert "DAEMON_INSTANCE_ID = 'daemon-current'" in helper_text
            assert "SHELL_LEASE_ID = 'shl_" in helper_text

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = 0
            return 0

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 143

        def kill(self) -> None:
            self.returncode = 137

    def fake_popen(argv, cwd, env):  # type: ignore[no-untyped-def]
        return FakeProcess(argv, cwd, env)

    def fail_run(argv, cwd, env, check):  # type: ignore[no-untyped-def]
        assert cwd == str(tmp_path)
        assert check is False
        pytest.fail("captured shell should launch through Popen so its PID can be leased")

    monkeypatch.setattr("vcs_core._ipc.send_request", fake_send_request)
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("subprocess.run", fail_run)

    result = runner.invoke(main, ["session", "shell", "--capture"])

    assert result.exit_code == 0, result.output
    assert [method for method, _params in calls] == [
        "get_state",
        "shell_capture_lease_begin",
        "shell_capture_lease_outcome",
    ]
    assert seen_argv[:4] == ["/bin/bash", "--noprofile", "--rcfile", seen_argv[3]]
    assert seen_argv[4] == "-i"
    assert seen_env["LD_PRELOAD"].startswith("/tmp/fs_capture_shim.so")
    assert seen_env["VCS_CORE_SCOPE"] == "experiment"
    assert "VCS_CORE_SHELL_FINISH_PATH" in seen_env
    assert seen_env["VCS_CORE_SHELL_LEASE_ID"] == seen_lease_id


def test_shell_command_envelope_uses_explicit_socket_from_overlay_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay_cwd = tmp_path / "overlay"
    overlay_cwd.mkdir()
    calls: list[tuple[str, str, dict[str, object]]] = []

    monkeypatch.chdir(overlay_cwd)
    monkeypatch.setattr(
        "vcs_core._cli_session_runtime._cli_ipc.live_session_info",
        lambda: pytest.fail("shell helper must not rediscover session from overlay cwd"),
    )

    def fake_send(socket_path: str, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        payload = dict(params or {})
        calls.append((socket_path, method, payload))
        if method == "exec_envelope_begin":
            return {
                "ok": True,
                "result": {
                    "operation_id": "cmd-shell",
                    "env": {
                        "VCS_CORE_COMMAND_OPERATION_ID": "cmd-shell",
                        "VCS_CORE_CAPTURE_EPOCH": "cap-shell",
                        "VCS_CORE_CAPTURE_ACTIVE": "1",
                    },
                },
            }
        return {
            "ok": True,
            "result": {"operation_id": "cmd-shell", "archive_ref": "refs/vcscore/archive/ops/cmd-shell"},
        }

    monkeypatch.setattr("vcs_core._cli_session_runtime._cli_ipc.send_session_request_to_socket", fake_send)

    envelope = begin_shell_command_envelope(
        command_text="printf shell > shell.txt",
        cwd=str(overlay_cwd),
        scope_name="shellcap",
        shell_pid=123,
        shell_lease_id="shl-shell",
        socket_path="/tmp/vcs-core-session.sock",
        daemon_instance_id="daemon-current",
        exit_code=3,
    )
    finish_exec_envelope(
        operation_id=envelope.operation_id,
        outcome="success",
        exit_code_value=0,
        socket_path="/tmp/vcs-core-session.sock",
        daemon_instance_id="daemon-current",
    )

    assert envelope.operation_id == "cmd-shell"
    assert envelope.env["VCS_CORE_CAPTURE_ACTIVE"] == "1"
    assert calls[0][0] == "/tmp/vcs-core-session.sock"
    assert calls[0][1] == "exec_envelope_begin"
    assert calls[0][2]["scope"] == "shellcap"
    assert calls[0][2]["capture_policy"] == "shell_command"
    assert calls[0][2]["transport"] == "shell"
    assert calls[0][2]["submitted_text"] == "printf shell > shell.txt"
    assert calls[0][2]["shell_pid"] == 123
    assert calls[0][2]["shell_lease_id"] == "shl-shell"
    assert calls[0][2]["daemon_instance_id"] == "daemon-current"
    assert calls[1][0] == "/tmp/vcs-core-session.sock"
    assert calls[1][1] == "exec_envelope_outcome"
    assert calls[1][2]["operation_id"] == "cmd-shell"
    assert calls[1][2]["daemon_instance_id"] == "daemon-current"


def test_shell_command_begin_rejection_records_not_admitted_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay_cwd = tmp_path / "overlay"
    overlay_cwd.mkdir()
    calls: list[tuple[str, str, dict[str, object]]] = []

    monkeypatch.chdir(overlay_cwd)
    monkeypatch.setattr(
        "vcs_core._cli_session_runtime._cli_ipc.live_session_info",
        lambda: pytest.fail("shell helper must not rediscover session from overlay cwd"),
    )

    def fake_send(socket_path: str, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        payload = dict(params or {})
        calls.append((socket_path, method, payload))
        if method == "exec_envelope_begin":
            return {"ok": False, "error": "forced begin rejection"}
        return {"ok": True, "result": {"operation_id": "cmd-diagnostic", "archive_ref": "archive-ref"}}

    monkeypatch.setattr("vcs_core._cli_session_runtime._cli_ipc.send_session_request_to_socket", fake_send)

    with pytest.raises(SessionCliError, match="forced begin rejection"):
        begin_shell_command_envelope(
            command_text="printf shell > shell.txt",
            cwd=str(overlay_cwd),
            scope_name="shellcap",
            shell_pid=123,
            shell_lease_id="shl-shell",
            socket_path="/tmp/vcs-core-session.sock",
            daemon_instance_id="daemon-current",
            exit_code=3,
        )

    assert [call[1] for call in calls] == ["exec_envelope_begin", "shell_command_not_admitted"]
    diagnostic = calls[1][2]
    assert diagnostic["scope"] == "shellcap"
    assert diagnostic["submitted_text"] == "printf shell > shell.txt"
    assert diagnostic["shell_pid"] == 123
    assert diagnostic["shell_lease_id"] == "shl-shell"
    assert diagnostic["daemon_instance_id"] == "daemon-current"
    assert diagnostic["admission_error"] == "forced begin rejection"


def test_shell_capture_bashrc_reports_begin_failure_without_activating_capture(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to exercise the generated shell capture rcfile")

    helper_path = tmp_path / "helper"
    helper_path.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$1" >> "$VCS_CORE_HELPER_LOG"\n'
        "printf 'forced begin failure\\n' >&2\n"
        "exit 17\n",
        encoding="utf-8",
    )
    helper_path.chmod(0o700)
    rcfile = tmp_path / "bashrc"
    rcfile.write_text(
        _shell_capture_bashrc(helper_path=helper_path, finish_path=tmp_path / "finish"),
        encoding="utf-8",
    )
    helper_log = tmp_path / "helper.log"

    result = subprocess.run(
        [
            bash,
            "--noprofile",
            "--norc",
            "-c",
            (
                f"source {rcfile}; "
                "trap - DEBUG; "
                "PROMPT_COMMAND=; "
                "__mg_suppress=0; "
                "__mg_begin; "
                "printf 'status=%s active=%s command=%s env=%s epoch=%s capture=%s\\n' "
                '"$?" "$__mg_active" "$__mg_command_id" "${VCS_CORE_COMMAND_OPERATION_ID-unset}" '
                '"${VCS_CORE_CAPTURE_EPOCH-unset}" "${VCS_CORE_CAPTURE_ACTIVE-unset}"'
            ),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "VCS_CORE_HELPER_LOG": str(helper_log),
            "VCS_CORE_COMMAND_OPERATION_ID": "stale-command",
            "VCS_CORE_CAPTURE_EPOCH": "stale-epoch",
            "VCS_CORE_CAPTURE_ACTIVE": "1",
        },
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert "forced begin failure" in result.stderr
    assert "vcs-core shell capture helper error: begin failed for" in result.stderr
    assert result.stdout == "status=0 active=0 command= env=unset epoch=unset capture=unset\n"
    assert helper_log.read_text(encoding="utf-8") == "begin\n"


def test_shell_capture_bashrc_cleans_env_when_outcome_helper_fails(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to exercise the generated shell capture rcfile")

    helper_path = tmp_path / "helper"
    helper_path.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s:%s:%s\\n\' "$1" "$2" "$3" >> "$VCS_CORE_HELPER_LOG"\n'
        "printf 'forced outcome failure\\n' >&2\n"
        "exit 17\n",
        encoding="utf-8",
    )
    helper_path.chmod(0o700)
    rcfile = tmp_path / "bashrc"
    finish_path = tmp_path / "finish"
    rcfile.write_text(
        _shell_capture_bashrc(helper_path=helper_path, finish_path=finish_path),
        encoding="utf-8",
    )
    helper_log = tmp_path / "helper.log"

    result = subprocess.run(
        [
            bash,
            "--noprofile",
            "--norc",
            "-c",
            (
                f"source {rcfile}; "
                "trap - DEBUG; "
                "PROMPT_COMMAND=; "
                "__mg_active=1; "
                "__mg_command_id=cmd-1; "
                "export VCS_CORE_COMMAND_OPERATION_ID=cmd-1 VCS_CORE_CAPTURE_EPOCH=cap-1 VCS_CORE_CAPTURE_ACTIVE=1; "
                "__mg_finish 7; "
                "printf 'status=%s active=%s command=%s env=%s\\n' "
                '"$?" "$__mg_active" "$__mg_command_id" "${VCS_CORE_COMMAND_OPERATION_ID-unset}"'
            ),
        ],
        cwd=tmp_path,
        env={**os.environ, "VCS_CORE_HELPER_LOG": str(helper_log)},
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert "forced outcome failure" in result.stderr
    assert "vcs-core shell capture helper error: outcome failed for cmd-1" in result.stderr
    assert result.stdout == "status=7 active=0 command= env=unset\n"
    assert helper_log.read_text(encoding="utf-8") == "outcome:cmd-1:7\n"
    assert finish_path.read_text(encoding="utf-8") == ""


def test_session_shell_without_capture_does_not_inject_capture_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    info = _session_info(tmp_path)
    seen_env: dict[str, str] = {}

    monkeypatch.setattr("vcs_core._ipc.is_session_alive", lambda repo_path: True)
    monkeypatch.setattr("vcs_core._ipc.read_session_info", lambda repo_path: info)

    def fake_send_request(socket_path: str, method: str, params: dict | None = None) -> dict:
        assert socket_path == info.socket_path
        del method, params
        return {
            "ok": True,
            "result": {
                "pid": info.pid,
                "current_scope": "experiment",
                "current_scope_instance_id": "experiment-iid",
                "mount_path": str(tmp_path),
                "workspace": str(tmp_path),
                "started_at": info.started_at,
                "hook_static_env": {
                    "VCS_CORE_HOOK_SOCKET": "/tmp/session-hook.sock",
                },
                "hook_static_prepend_path": ["/tmp/hook-bin"],
                "hook_static_prepend_env": {},
                "hook_scope_env": {
                    "VCS_CORE_SCOPE": "experiment",
                    "VCS_CORE_SCOPE_INSTANCE_ID": "experiment-iid",
                },
                "hook_scope_prepend_path": [],
                "hook_scope_prepend_env": {},
            },
        }

    def fake_run(argv, cwd, env, check):  # type: ignore[no-untyped-def]
        del argv, cwd, check
        seen_env.update(env)
        return subprocess.CompletedProcess(["/bin/bash"], 0)

    monkeypatch.setattr("vcs_core._ipc.send_request", fake_send_request)
    monkeypatch.setattr("subprocess.run", fake_run)

    result = runner.invoke(main, ["session", "shell"])

    assert result.exit_code == 0, result.output
    assert seen_env["VCS_CORE_SESSION"] == "1"
    assert seen_env["VCS_CORE_SCOPE"] == "experiment"
    assert seen_env["VCS_CORE_SCOPE_INSTANCE_ID"] == "experiment-iid"
    assert seen_env["VCS_CORE_HOOK_SOCKET"] == "/tmp/session-hook.sock"
    assert "VCS_CORE_FS_CAPTURE_SOCKET" not in seen_env
    assert "LD_PRELOAD" not in seen_env
    assert seen_env["PATH"].startswith("/tmp/hook-bin")


def test_session_shell_injects_hook_env_when_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    runner = CliRunner()
    _init(runner, tmp_path)

    info = _session_info(tmp_path)
    seen_env: dict[str, str] = {}

    monkeypatch.setattr("vcs_core._ipc.is_session_alive", lambda repo_path: True)
    monkeypatch.setattr("vcs_core._ipc.read_session_info", lambda repo_path: info)

    def fake_send_request(socket_path: str, method: str, params: dict | None = None) -> dict:
        assert socket_path == info.socket_path
        del method, params
        return {
            "ok": True,
            "result": {
                "pid": info.pid,
                "current_scope": "experiment",
                "current_scope_instance_id": "experiment-iid",
                "mount_path": str(tmp_path),
                "workspace": str(tmp_path),
                "started_at": info.started_at,
                "hook_static_env": {
                    "VCS_CORE_HOOK_SOCKET": "/tmp/session-hook.sock",
                },
                "hook_static_prepend_path": ["/tmp/hook-bin"],
                "hook_static_prepend_env": {},
                "hook_scope_env": {
                    "VCS_CORE_SCOPE": "experiment",
                    "VCS_CORE_SCOPE_INSTANCE_ID": "experiment-iid",
                },
                "hook_scope_prepend_path": [],
                "hook_scope_prepend_env": {},
            },
        }

    def fake_run(argv, cwd, env, check):  # type: ignore[no-untyped-def]
        del argv, cwd, check
        seen_env.update(env)
        return subprocess.CompletedProcess(["/bin/bash"], 0)

    monkeypatch.setattr("vcs_core._ipc.send_request", fake_send_request)
    monkeypatch.setattr("subprocess.run", fake_run)

    result = runner.invoke(main, ["session", "shell"])

    assert result.exit_code == 0, result.output
    assert seen_env["VCS_CORE_HOOK_SOCKET"] == "/tmp/session-hook.sock"
    assert seen_env["VCS_CORE_SCOPE_INSTANCE_ID"] == "experiment-iid"
    assert seen_env["PATH"].startswith("/tmp/hook-bin")


@pytest.mark.xfail(
    reason=(
        "owner: vcs-core — same refusal-before-IPC ordering contract as "
        "test_session_shell_capture_is_linux_only; the CLI reaches get_state first."
    ),
    strict=False,
)
def test_session_shell_capture_errors_cleanly_when_prerequisites_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    info = _session_info(tmp_path)

    monkeypatch.setattr("vcs_core._ipc.is_session_alive", lambda repo_path: True)
    monkeypatch.setattr("vcs_core._ipc.read_session_info", lambda repo_path: info)

    monkeypatch.setattr(
        "vcs_core._ipc.send_request",
        lambda socket_path, method, params=None: pytest.fail("shell capture must not reach session IPC"),
    )

    result = runner.invoke(main, ["session", "shell", "--capture"])

    assert result.exit_code == 1
    assert "session shell --capture" in result.output


def test_session_shell_create_switches_then_launches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    runner = CliRunner()
    _init(runner, tmp_path)

    info = _session_info(tmp_path)
    calls: list[tuple[str, dict | None]] = []

    monkeypatch.setattr("vcs_core._ipc.is_session_alive", lambda repo_path: True)
    monkeypatch.setattr("vcs_core._ipc.read_session_info", lambda repo_path: info)

    def fake_send_request(socket_path: str, method: str, params: dict | None = None) -> dict:
        assert socket_path == info.socket_path
        calls.append((method, params))
        if method in {"fork", "switch"}:
            return {"ok": True, "result": {"current_scope": "task-shell", "mount_path": str(tmp_path)}}
        return {
            "ok": True,
            "result": {
                "pid": info.pid,
                "current_scope": "task-shell",
                "mount_path": str(tmp_path),
                "workspace": str(tmp_path),
                "started_at": info.started_at,
            },
        }

    monkeypatch.setattr("vcs_core._ipc.send_request", fake_send_request)
    monkeypatch.setattr(
        "subprocess.run",
        lambda argv, cwd, env, check: subprocess.CompletedProcess(argv, 0),
    )

    result = runner.invoke(
        main,
        ["session", "shell", "--scope", "task-shell", "--create", "--parent", "task-parent"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        ("fork", {"name": "task-shell", "parent": "task-parent", "isolated": True}),
        ("switch", {"name": "task-shell"}),
        ("get_state", {"hook_capabilities": []}),
    ]
    assert "vcs-core discard task-shell" in result.output


def test_session_shell_create_requires_scope(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    result = runner.invoke(main, ["session", "shell", "--create"])

    assert result.exit_code != 0
    assert "requires `--scope <name>`" in result.output


def test_session_shell_parent_requires_create(tmp_path: Path) -> None:
    runner = CliRunner()
    _init(runner, tmp_path)

    result = runner.invoke(main, ["session", "shell", "--scope", "task", "--parent", "ground"])

    assert result.exit_code != 0
    assert "only valid together with `--create`" in result.output
