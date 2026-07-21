# under-test: vcs_core._session
"""Session daemon lifecycle and IPC dispatch tests."""

from __future__ import annotations

import json
import os
import struct
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from vcs_core._ipc import is_session_alive, write_session_info
from vcs_core.runtime_api import CommandExecutionOptions
from vcs_core.store import Store
from vcs_core.testing import SessionInfo
from vcs_core.types import (
    CommitInfo,
    OperationHistory,
    OperationSummary,
    RecordedCommandOutcome,
    RecoverySnapshot,
    ScopeInfo,
)


def _init_vcscore_repo(workspace: Path) -> None:
    Store(str(workspace / ".vcscore")).create_root_commit()


def _expected_operation_summary(**overrides: object) -> dict[str, object]:
    summary = {
        "operation_id": "op-default",
        "label": "Default Op",
        "kind": "test.default",
        "status": "ok",
        "visibility": "visible",
        "world_id": "world_default",
        "world_name": "default",
        "world_ref": "refs/vcscore/scopes/default",
        "carrier_ref": "refs/vcscore/scopes/default",
        "anchor_oid": None,
        "effect_count": 0,
        "parent_operation_id": None,
        "final_phase": None,
        "archived_via": None,
    }
    summary.update(overrides)
    return summary


def _exec_ipc_params(
    binding: str,
    command: str,
    params: dict[str, object] | None = None,
    *,
    scope: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"binding": binding, "command": command, "params": params or {}}
    if scope is not None:
        payload["scope"] = scope
    return payload


class _FakeActiveApp:
    def __init__(
        self,
        scopes: dict[str, ScopeInfo],
        *,
        exec_outcome: RecordedCommandOutcome | None = None,
        record_oids: list[str] | None = None,
    ) -> None:
        self._scopes = scopes
        self._exec_outcome = exec_outcome
        self._record_oids = record_oids
        self.execute_calls: list[tuple[str, str, str, dict[str, object]]] = []
        self.execute_command_sources: list[str] = []
        self.execute_execution_options: list[CommandExecutionOptions | None] = []

    def resolve_scope(self, name: str) -> ScopeInfo:
        return self._scopes[name]

    def retain_restored_scope(self, name: str) -> None:
        del name

    def execute(
        self,
        *,
        binding_name: str,
        command: str,
        scope_name: str,
        params: dict[str, object],
        execution_options: CommandExecutionOptions | None = None,
        command_source: str = "native",
    ) -> RecordedCommandOutcome:
        self.execute_calls.append((binding_name, command, scope_name, params))
        self.execute_execution_options.append(execution_options)
        self.execute_command_sources.append(command_source)
        assert self._exec_outcome is not None
        return self._exec_outcome

    def record(
        self,
        *,
        binding_name: str,
        effect_type: str,
        metadata: dict[str, object],
        scope_name: str,
    ) -> list[str]:
        del binding_name, effect_type, metadata, scope_name
        assert self._record_oids is not None
        return self._record_oids


class _NoOpenOperationsStore:
    def list_open_operations(self):  # type: ignore[no-untyped-def]
        return ()


@contextmanager
def _active_view_for(app: _FakeActiveApp):  # type: ignore[no-untyped-def]
    yield app


def _patch_active_view(monkeypatch: pytest.MonkeyPatch, app: _FakeActiveApp) -> None:
    def fake_active_view(mg: object, *, current_scope: str = "ground"):  # type: ignore[no-untyped-def]
        del mg, current_scope
        return _active_view_for(app)

    monkeypatch.setattr("vcs_core._session_dispatch.VcsCoreApp.active_view", fake_active_view)


def test_daemon_dispatch_unknown_method(workspace: Path) -> None:
    from vcs_core._session import SessionDaemon

    daemon = SessionDaemon(str(workspace))

    with pytest.raises(ValueError, match="Unknown method"):
        daemon._dispatch("nonexistent", {})


def test_daemon_fork_rejects_invalid_scope_name_before_app_dispatch(workspace: Path) -> None:
    from vcs_core._app import AppCommandBlocked
    from vcs_core._session import SessionDaemon

    daemon = SessionDaemon(str(workspace))

    with pytest.raises(AppCommandBlocked) as excinfo:
        daemon._dispatch("fork", {"name": "bad/name"})

    assert excinfo.value.command == "branch"
    assert [blocker.kind for blocker in excinfo.value.blockers] == ["invalid_input"]
    assert "contains '/'" in excinfo.value.blockers[0].detail


def test_daemonize_rejects_double_start(workspace: Path) -> None:
    from vcs_core._session import daemonize

    _init_vcscore_repo(workspace)
    repo_path = workspace / ".vcscore"

    write_session_info(
        str(repo_path),
        SessionInfo(
            pid=os.getpid(),
            socket_path="/tmp/fake.sock",
            mount_path=str(workspace),
            workspace=str(workspace),
            started_at=time.time(),
        ),
    )

    with pytest.raises(RuntimeError, match="Session already running"):
        daemonize(str(workspace))


def test_stop_session_no_session(workspace: Path) -> None:
    from vcs_core._session import stop_session

    (workspace / ".vcscore").mkdir(exist_ok=True)

    with pytest.raises(RuntimeError, match="No session is running"):
        stop_session(str(workspace))


def test_stale_session_info_cleanup(workspace: Path) -> None:
    """A session.json with a dead PID should not block new starts."""
    repo_path = workspace / ".vcscore"
    repo_path.mkdir(exist_ok=True)

    write_session_info(
        str(repo_path),
        SessionInfo(
            pid=999999999,
            socket_path="/tmp/stale.sock",
            mount_path=str(workspace),
            workspace=str(workspace),
            started_at=time.time() - 600,
        ),
    )

    assert not is_session_alive(str(repo_path))


def test_cleanup_deactivates_patches_before_uninstalling_hook_wrappers(workspace: Path) -> None:
    from vcs_core._hooks import HookEffects, HookEvent, SystemHook
    from vcs_core._session import SessionDaemon
    from vcs_core.testing import HookManager
    from vcs_core.types import BoundSubstrate, EffectRecord

    from ...support.builders import make_marker_filesystem_vcscore

    class _FakeGitHookSubstrate:
        name = "git"
        commands = {"status": object()}
        effects = {}

        def bind_pipeline(self, pipeline, *, scope_queries=None) -> None:  # type: ignore[no-untyped-def]
            del pipeline, scope_queries

        def activate(self) -> None:
            return None

        def deactivate(self) -> None:
            return None

        def authority(self):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        def python_patches(self):  # type: ignore[no-untyped-def]
            return ()

        def system_hooks(self):
            return (
                SystemHook(
                    hook_id="git-cli",
                    kind="path_wrapper",
                    config={"binary": "git"},
                    translator=self._translate,
                ),
            )

        def _translate(self, event: HookEvent):
            if event.exit_code != 0:
                return None
            return HookEffects(effects=(EffectRecord(effect_type="GitStatus", metadata={"cwd": event.cwd or "."}),))

    daemon = SessionDaemon(str(workspace))
    mg = make_marker_filesystem_vcscore(workspace, activate=True)
    repo_path = workspace / ".vcscore"
    daemon._mg = mg
    daemon._hook_manager = HookManager(
        mg,
        workspace=workspace,
        repo_path=repo_path,
        socket_path=str(repo_path / "session-hook.sock"),
    )
    daemon._hook_manager.install_bindings(
        (BoundSubstrate(binding_name="git", substrate_type="git", instance=_FakeGitHookSubstrate()),)
    )

    installer = daemon._hook_manager._installers[0]
    session_root = installer._session_root

    assert session_root is not None
    assert session_root.exists()

    daemon._cleanup("/tmp/test-session.sock", "/tmp/test-session-hook.sock")

    assert not session_root.exists()


def test_daemonize_waits_for_ipc_readiness(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core import _session as session_mod

    _init_vcscore_repo(workspace)

    info = SessionInfo(
        pid=12345,
        socket_path="/tmp/fake.sock",
        mount_path=str(workspace),
        workspace=str(workspace),
        started_at=time.time(),
    )
    attempts = {"count": 0}
    alive_checks = {"count": 0}
    clock = {"value": 0.0}

    monkeypatch.setattr(session_mod.SessionDaemon, "start", lambda self, foreground=False: None)
    monkeypatch.setattr(session_mod, "read_session_info", lambda repo_path: info)

    def fake_is_session_alive(repo_path: str) -> bool:
        del repo_path
        alive_checks["count"] += 1
        return alive_checks["count"] > 1

    monkeypatch.setattr(session_mod, "is_session_alive", fake_is_session_alive)

    def fake_send_request(socket_path: str, method: str, params: dict | None = None) -> dict:
        assert socket_path == info.socket_path
        assert method == "get_state"
        assert params == {"hook_capabilities": []}
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ConnectionError("socket not ready")
        return {"ok": True, "result": {"current_scope": "ground"}}

    monkeypatch.setattr(session_mod, "send_request", fake_send_request)
    monkeypatch.setattr(session_mod.time, "sleep", lambda seconds: None)

    def fake_time() -> float:
        clock["value"] += 0.1
        return clock["value"]

    monkeypatch.setattr(session_mod.time, "time", fake_time)

    assert session_mod.daemonize(str(workspace)) == info.pid
    assert attempts["count"] == 2


def test_daemon_child_exits_nonzero_when_run_crashes(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core._session import SessionDaemon

    repo_path = workspace / ".vcscore"
    repo_path.mkdir(exist_ok=True)
    daemon = SessionDaemon(str(workspace))
    fake_stdio = type("FakeStdio", (), {"fileno": lambda self: 0})()

    monkeypatch.setattr("vcs_core._session.os.fork", lambda: 0)
    monkeypatch.setattr("vcs_core._session.os.setsid", lambda: None)
    monkeypatch.setattr("vcs_core._session.os.open", lambda path, flags, mode=0o777: 3)
    monkeypatch.setattr("vcs_core._session.os.dup2", lambda src, dst: None)
    monkeypatch.setattr("vcs_core._session.os.close", lambda fd: None)
    monkeypatch.setattr("vcs_core._session.sys.stdin", fake_stdio)
    monkeypatch.setattr("vcs_core._session.sys.stdout", fake_stdio)
    monkeypatch.setattr("vcs_core._session.sys.stderr", fake_stdio)
    monkeypatch.setattr(daemon, "_run", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(SystemExit) as excinfo:
        daemon._daemonize()

    assert excinfo.value.code == 1


def test_daemon_child_exits_zero_when_run_returns_cleanly(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core._session import SessionDaemon

    repo_path = workspace / ".vcscore"
    repo_path.mkdir(exist_ok=True)
    daemon = SessionDaemon(str(workspace))
    fake_stdio = type("FakeStdio", (), {"fileno": lambda self: 0})()

    monkeypatch.setattr("vcs_core._session.os.fork", lambda: 0)
    monkeypatch.setattr("vcs_core._session.os.setsid", lambda: None)
    monkeypatch.setattr("vcs_core._session.os.open", lambda path, flags, mode=0o777: 3)
    monkeypatch.setattr("vcs_core._session.os.dup2", lambda src, dst: None)
    monkeypatch.setattr("vcs_core._session.os.close", lambda fd: None)
    monkeypatch.setattr("vcs_core._session.sys.stdin", fake_stdio)
    monkeypatch.setattr("vcs_core._session.sys.stdout", fake_stdio)
    monkeypatch.setattr("vcs_core._session.sys.stderr", fake_stdio)
    monkeypatch.setattr(daemon, "_run", lambda: None)

    with pytest.raises(SystemExit) as excinfo:
        daemon._daemonize()

    assert excinfo.value.code == 0


def test_hook_state_exposes_socket_and_watermarks(workspace: Path) -> None:
    from vcs_core._session import SessionDaemon

    daemon = SessionDaemon(str(workspace))
    state = daemon._dispatch("hook_state", {})

    assert state["hook_socket"].endswith("session-hook.sock")
    assert state["accepted_seq"] == 0
    assert state["processed_seq"] == 0
    assert state["persisted_seq"] == 0
    assert state["failed_seq"] == 0
    assert state["outcomes"] == {
        "persisted": 0,
        "ignored_no_effect": 0,
        "ignored_stale_scope": 0,
        "ignored_unsupported": 0,
        "malformed": 0,
        "failed": 0,
    }


def test_daemon_connection_renders_app_errors_for_ipc(workspace: Path) -> None:
    from vcs_core._app import AppBlocker, AppCommandBlocked
    from vcs_core._session import SessionDaemon

    class _FakeConnection:
        def __init__(self) -> None:
            self._chunks = [b'{"method": "fork", "params": {}}\n']
            self.sent = b""

        def recv(self, bufsize: int) -> bytes:
            del bufsize
            if not self._chunks:
                return b""
            return self._chunks.pop(0)

        def sendall(self, data: bytes) -> None:
            self.sent += data

        def getsockopt(self, level: int, optname: int, buflen: int = 0) -> bytes:
            # Present the same-uid SO_PEERCRED a real same-user local
            # connection carries, so _authorize_peer sees an authorized peer.
            del level, optname, buflen
            return struct.pack("3i", os.getpid(), os.getuid(), os.getgid())

    daemon = SessionDaemon(str(workspace))

    def fail_with_app_error(method: str, params: dict[str, object]) -> dict[str, object]:
        del method, params
        raise AppCommandBlocked(
            command="branch",
            blockers=(
                AppBlocker(
                    kind="live_scope",
                    subject="task",
                    detail="Live scope 'task' must be merged or discarded before materialization.",
                ),
            ),
        )

    daemon._dispatch = fail_with_app_error  # type: ignore[method-assign]
    conn = _FakeConnection()

    daemon._handle_connection(conn)

    response = json.loads(conn.sent.decode())
    assert response["ok"] is False
    assert response["error"].startswith("Error: cannot branch:")
    assert "Live scope 'task' must be merged or discarded before materialization." in response["error"]


def test_daemon_merge_restores_stateless_created_scope(workspace: Path) -> None:
    from vcs_core._app import AppOpenMode, VcsCoreApp
    from vcs_core._session import SessionDaemon
    from vcs_core.vcscore import VcsCore

    _init_vcscore_repo(workspace)
    with VcsCoreApp.open_existing(str(workspace), mode=AppOpenMode.CONTROL) as app:
        app.branch(name="task", parent="ground")

    daemon = SessionDaemon(str(workspace))
    mg = VcsCore.from_config(str(workspace))
    mg.activate()
    daemon._mg = mg
    try:
        result = daemon._dispatch("merge", {"name": "task"})
    finally:
        mg.deactivate(warn_on_open_scopes=False)

    assert result == {"merged": "task", "into": "ground"}
    entry = VcsCore.from_config(str(workspace)).store.scope_registry_entry("task")
    assert entry is not None
    assert entry.status == "merged"


def test_daemon_merge_terminal_scope_reports_app_error(workspace: Path) -> None:
    from vcs_core._app import AppOpenMode, AppScopeTerminalState, VcsCoreApp
    from vcs_core._session import SessionDaemon
    from vcs_core.vcscore import VcsCore

    _init_vcscore_repo(workspace)
    with VcsCoreApp.open_existing(str(workspace), mode=AppOpenMode.CONTROL) as app:
        app.branch(name="task", parent="ground")

    daemon = SessionDaemon(str(workspace))
    mg = VcsCore.from_config(str(workspace))
    mg.activate()
    daemon._mg = mg
    try:
        assert daemon._dispatch("merge", {"name": "task"}) == {"merged": "task", "into": "ground"}
        with pytest.raises(AppScopeTerminalState) as excinfo:
            daemon._dispatch("merge", {"name": "task"})
        assert excinfo.value.name == "task"
        assert excinfo.value.status == "merged"
    finally:
        mg.deactivate(warn_on_open_scopes=False)


def test_daemon_runtime_commands_restore_stateless_created_scope(workspace: Path) -> None:
    from click.testing import CliRunner
    from vcs_core._app import AppOpenMode, VcsCoreApp
    from vcs_core._session import SessionDaemon
    from vcs_core.vcscore import VcsCore

    from ...support.cli import init_repo

    runner = CliRunner()
    init_repo(runner, workspace)
    with VcsCoreApp.open_existing(str(workspace), mode=AppOpenMode.CONTROL) as app:
        app.branch(name="task", parent="ground")

    daemon = SessionDaemon(str(workspace))
    mg = VcsCore.from_config(str(workspace))
    mg.activate()
    daemon._mg = mg
    try:
        switch_result = daemon._dispatch("switch", {"name": "task"})
        retained_scope = mg.lookup_scope("task")
        exec_result = daemon._dispatch("exec", _exec_ipc_params("marker", "mark", {"label": "session-exec"}))
    finally:
        mg.deactivate(warn_on_open_scopes=False)

    assert switch_result["current_scope"] == "task"
    assert retained_scope is not None
    assert daemon._current_scope_name == "task"
    assert exec_result["oids"]


def test_session_state_restores_registry_live_current_scope(workspace: Path) -> None:
    from vcs_core._app import AppOpenMode, VcsCoreApp
    from vcs_core._session import SessionDaemon
    from vcs_core.vcscore import VcsCore

    _init_vcscore_repo(workspace)
    with VcsCoreApp.open_existing(str(workspace), mode=AppOpenMode.CONTROL) as app:
        app.branch(name="task", parent="ground")

    daemon = SessionDaemon(str(workspace))
    mg = VcsCore.from_config(str(workspace))
    mg.activate()
    daemon._mg = mg
    daemon._current_scope_name = "task"
    try:
        entry = mg.store.scope_registry_entry("task", status="live")
        assert entry is not None

        state = daemon._dispatch("get_state", {"hook_capabilities": []})
        retained_scope = mg.lookup_scope("task")
    finally:
        mg.deactivate(warn_on_open_scopes=False)

    assert state["current_scope"] == "task"
    assert state["current_scope_instance_id"] == entry.instance_id
    assert state["current_world_id"] == entry.world_id
    assert state["mount_path"]
    assert retained_scope is not None
    assert retained_scope.instance_id == entry.instance_id
    assert retained_scope.world_id == entry.world_id


def test_session_state_reports_active_handle_registry_mismatch(workspace: Path) -> None:
    from vcs_core._app import AppScopeResolutionError
    from vcs_core._session import SessionDaemon
    from vcs_core.vcscore import VcsCore

    _init_vcscore_repo(workspace)
    mg = VcsCore.from_config(str(workspace))
    mg.activate()
    task = mg.fork(mg.ground, "task")
    mg.clear_restored_scope_state()
    mg._active_scopes["task"] = ScopeInfo(
        name="task",
        ref=task.ref,
        instance_id="conflicting-instance",
        creation_oid=task.creation_oid,
        world_id=task.world_id,
    )
    mg._scope_parents["task"] = mg.ground

    daemon = SessionDaemon(str(workspace))
    daemon._mg = mg
    daemon._current_scope_name = "task"
    try:
        with pytest.raises(AppScopeResolutionError) as excinfo:
            daemon._dispatch("get_state", {"hook_capabilities": []})
    finally:
        mg.deactivate(warn_on_open_scopes=False)

    assert excinfo.value.blockers
    assert excinfo.value.blockers[0].kind == "scope_registry_mismatch"


def test_session_startup_mount_path_uses_app_backed_ground(workspace: Path) -> None:
    from vcs_core._session import SessionDaemon
    from vcs_core.vcscore import VcsCore

    _init_vcscore_repo(workspace)
    daemon = SessionDaemon(str(workspace))
    mg = VcsCore.from_config(str(workspace))
    mg.activate()
    daemon._mg = mg
    try:
        mount_path = daemon._current_scope_mount_path()
    finally:
        mg.deactivate(warn_on_open_scopes=False)

    assert mount_path


def test_session_state_uses_split_hook_env_only(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core._session import SessionDaemon
    from vcs_core.types import ScopeInfo

    daemon = SessionDaemon(str(workspace))
    ground = ScopeInfo(name="ground", ref="refs/vcscore/ground", instance_id="ground", creation_oid="")
    _patch_active_view(monkeypatch, _FakeActiveApp({"ground": ground}))
    daemon._mg = type(
        "FakeVcsCore",
        (),
        {
            "_repo_path": str(workspace),
            "substrates": [],
            "overlay_mount_path_for_scope": lambda self, scope: workspace,
        },
    )()

    state = daemon._session_state()

    assert state["hook_socket"].endswith("session-hook.sock")
    assert "hook_env" not in state
    assert "hook_prepend_path" not in state


def test_session_state_rejects_unknown_hook_capability(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core import build_builtin_substrate_context
    from vcs_core._session import SessionDaemon
    from vcs_core.substrates import FilesystemSubstrate
    from vcs_core.testing import HookManager
    from vcs_core.types import BoundSubstrate, ScopeInfo

    daemon = SessionDaemon(str(workspace))
    repo_path = workspace / ".vcscore"
    repo_path.mkdir(exist_ok=True)
    ground = ScopeInfo(name="ground", ref="refs/vcscore/ground", instance_id="ground", creation_oid="")
    _patch_active_view(monkeypatch, _FakeActiveApp({"ground": ground}))

    daemon._mg = type(
        "FakeVcsCore",
        (),
        {
            "_repo_path": str(workspace),
            "substrates": [],
            "bindings": (
                BoundSubstrate(
                    binding_name="filesystem",
                    substrate_type="filesystem",
                    instance=FilesystemSubstrate(
                        build_builtin_substrate_context(
                            type("Store", (), {"repo_path": str(repo_path)})(),
                            workspace=workspace,
                        )
                    ),
                ),
            ),
            "working_directory_for_scope": lambda self, scope: workspace,
            "overlay_mount_path_for_scope": lambda self, scope: workspace,
        },
    )()
    daemon._hook_manager = HookManager(
        daemon._mg,
        workspace=workspace,
        repo_path=repo_path,
        socket_path=str(repo_path / "session-hook.sock"),
    )
    daemon._hook_manager.install_bindings(daemon._mg.bindings)

    with pytest.raises(ValueError, match="Unknown hook capabilities"):
        daemon._dispatch("get_state", {"hook_capabilities": ["unknown_capability"]})


def test_do_exec_normalizes_command_value_for_ipc(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core._session import SessionDaemon

    daemon = SessionDaemon(str(workspace))
    task = ScopeInfo(name="ground", ref="refs/vcscore/ground", instance_id="ground", creation_oid="")
    _patch_active_view(
        monkeypatch,
        _FakeActiveApp(
            {"ground": task},
            exec_outcome=RecordedCommandOutcome(
                oids=("deadbeef",),
                value={"payload": b"hello"},
            ),
        ),
    )
    daemon._mg = type(
        "FakeVcsCore",
        (),
        {"_repo_path": str(workspace), "store": _NoOpenOperationsStore()},
    )()

    result = daemon._dispatch("exec", _exec_ipc_params("value", "inspect"))

    assert result["oids"] == ["deadbeef"]
    assert result["value"] == {
        "payload": {
            "__type__": "bytes",
            "encoding": "base64",
            "data": "aGVsbG8=",
        }
    }


def test_do_exec_passes_nested_command_params_without_routing_collisions(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vcs_core._session import SessionDaemon

    daemon = SessionDaemon(str(workspace))
    task = ScopeInfo(name="ground", ref="refs/vcscore/ground", instance_id="ground", creation_oid="")
    app = _FakeActiveApp(
        {"ground": task},
        exec_outcome=RecordedCommandOutcome(oids=("deadbeef",), value=None),
    )
    _patch_active_view(monkeypatch, app)
    daemon._mg = type(
        "FakeVcsCore",
        (),
        {"_repo_path": str(workspace), "store": _NoOpenOperationsStore()},
    )()

    daemon._dispatch(
        "exec",
        {
            "binding": "runtime",
            "command": "route",
            "params": {"binding": "user-binding", "command": "user-command"},
        },
    )

    assert app.execute_calls == [
        (
            "runtime",
            "route",
            "ground",
            {"binding": "user-binding", "command": "user-command"},
        )
    ]
    assert app.execute_command_sources == ["typed-json"]
    assert app.execute_execution_options == [CommandExecutionOptions()]


def test_do_exec_normalizes_driver_ingress_result_for_ipc(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core._session import SessionDaemon
    from vcs_core.runtime_api import DriverIngressResult
    from vcs_core.spi import Diagnostic, ObservationDraft, TransitionDraft
    from vcs_core.types import DRIVER_INGRESS_RESULT_VALUE_SCHEMA

    daemon = SessionDaemon(str(workspace))
    task = ScopeInfo(name="ground", ref="refs/vcscore/ground", instance_id="ground", creation_oid="")
    _patch_active_view(
        monkeypatch,
        _FakeActiveApp(
            {"ground": task},
            exec_outcome=RecordedCommandOutcome(
                oids=(),
                value=DriverIngressResult(
                    observations=(
                        ObservationDraft(
                            observation_id="obs-1",
                            evidence_kind="test.evidence",
                            stable_observation={"path": "README.md"},
                        ),
                    ),
                    transitions=(
                        TransitionDraft(
                            transition_id="tr-1",
                            semantic_op="append",
                            payload={"schema": "test/transition/v1"},
                            observation_ids=("obs-1",),
                        ),
                    ),
                    diagnostics=(Diagnostic(code="note", message="hello", subject="tr-1"),),
                ),
            ),
        ),
    )
    daemon._mg = type(
        "FakeVcsCore",
        (),
        {"_repo_path": str(workspace), "store": _NoOpenOperationsStore()},
    )()

    result = daemon._dispatch("exec", _exec_ipc_params("value", "inspect"))

    assert result["oids"] == []
    value = result["value"]
    assert value["schema"] == DRIVER_INGRESS_RESULT_VALUE_SCHEMA
    assert value["summary"]["observation_count"] == 1
    assert value["summary"]["transition_count"] == 1
    assert value["summary"]["diagnostic_count"] == 1
    assert value["transitions"][0]["semantic_op"] == "append"


def test_do_operations_defaults_to_current_scope(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core._session import SessionDaemon

    daemon = SessionDaemon(str(workspace))
    experiment = ScopeInfo(
        name="experiment",
        ref="refs/vcscore/scopes/experiment",
        instance_id="scope_experiment",
        creation_oid="",
    )
    calls: list[tuple[str, object]] = []

    _patch_active_view(monkeypatch, _FakeActiveApp({"experiment": experiment}))
    daemon._current_scope_name = "experiment"
    daemon._mg = type(
        "FakeVcsCore",
        (),
        {
            "_repo_path": str(workspace),
            "visible_operations": lambda self, *, ref=None, max_count=50: (
                calls.append(("visible", ref))
                or [
                    OperationSummary(
                        operation_id="op_visible",
                        label="Visible Op",
                        kind="test.visible",
                        status="ok",
                        visibility="visible",
                        world_id=experiment.world_id,
                        world_name="experiment",
                        world_ref=experiment.ref,
                        carrier_ref="refs/vcscore/scopes/experiment",
                        effect_count=1,
                    )
                ]
            ),
        },
    )()

    result = daemon._dispatch("operations", {"mode": "visible", "max_count": 5})

    assert calls == [("visible", experiment.ref)]
    assert result == {
        "requested_mode": "visible",
        "scope": None,
        "visible": [
            _expected_operation_summary(
                operation_id="op_visible",
                label="Visible Op",
                kind="test.visible",
                world_id=None,
                world_name="experiment",
                world_ref=experiment.ref,
                carrier_ref="refs/vcscore/scopes/experiment",
                effect_count=1,
            )
        ],
        "open": [],
        "archived": [],
    }
    assert "scope_name" not in result["visible"][0]
    assert "scope_ref" not in result["visible"][0]


def test_do_operations_archived_defaults_repo_wide(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core._session import SessionDaemon

    daemon = SessionDaemon(str(workspace))
    experiment = ScopeInfo(
        name="experiment",
        ref="refs/vcscore/scopes/experiment",
        instance_id="scope_experiment",
        creation_oid="",
    )
    calls: list[tuple[str, object]] = []

    _patch_active_view(monkeypatch, _FakeActiveApp({"experiment": experiment}))
    daemon._current_scope_name = "experiment"
    daemon._mg = type(
        "FakeVcsCore",
        (),
        {
            "_repo_path": str(workspace),
            "archived_operations": lambda self, *, max_count=50, world_id=None, operation_id=None: (
                calls.append(("archived", world_id)) or []
            ),
        },
    )()

    result = daemon._dispatch("operations", {"mode": "archived", "max_count": 5})

    assert calls == [("archived", None)]
    assert result == {
        "requested_mode": "archived",
        "scope": None,
        "visible": [],
        "open": [],
        "archived": [],
    }


def test_do_operations_all_mode_uses_fixed_envelope(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core._session import SessionDaemon

    daemon = SessionDaemon(str(workspace))
    experiment = ScopeInfo(
        name="experiment",
        ref="refs/vcscore/scopes/experiment",
        instance_id="scope_experiment",
        creation_oid="",
        world_id="world_experiment",
    )
    calls: list[tuple[str, object]] = []

    _patch_active_view(monkeypatch, _FakeActiveApp({"experiment": experiment}))
    daemon._mg = type(
        "FakeVcsCore",
        (),
        {
            "_repo_path": str(workspace),
            "visible_operations": lambda self, *, ref=None, max_count=50: (
                calls.append(("visible", ref))
                or [
                    OperationSummary(
                        operation_id="op_visible",
                        label="Visible Op",
                        kind="test.visible",
                        status="ok",
                        visibility="visible",
                        world_id="world_experiment",
                        world_name="experiment",
                        world_ref=experiment.ref,
                        carrier_ref=experiment.ref,
                    )
                ]
            ),
            "open_operations": lambda self, *, scope=None: (
                calls.append(("open", scope.name if scope else None))
                or [
                    OperationSummary(
                        operation_id="op_open",
                        label="Open Op",
                        kind="test.open",
                        status="open",
                        visibility="staged",
                        world_id="world_experiment",
                        world_name="experiment",
                        world_ref=experiment.ref,
                        carrier_ref="refs/vcscore/operations/open/op_open",
                    )
                ]
            ),
            "archived_operations": lambda self, *, max_count=50, world_id=None, operation_id=None: (
                calls.append(("archived", world_id)) or []
            ),
        },
    )()

    result = daemon._dispatch("operations", {"mode": "all", "scope": "experiment", "max_count": 5})

    assert calls == [("visible", experiment.ref), ("open", "experiment"), ("archived", "world_experiment")]
    assert result == {
        "requested_mode": "all",
        "scope": "experiment",
        "visible": [
            _expected_operation_summary(
                operation_id="op_visible",
                label="Visible Op",
                kind="test.visible",
                world_id="world_experiment",
                world_name="experiment",
                world_ref=experiment.ref,
                carrier_ref=experiment.ref,
            )
        ],
        "open": [
            _expected_operation_summary(
                operation_id="op_open",
                label="Open Op",
                kind="test.open",
                status="open",
                visibility="staged",
                world_id="world_experiment",
                world_name="experiment",
                world_ref=experiment.ref,
                carrier_ref="refs/vcscore/operations/open/op_open",
            )
        ],
        "archived": [],
    }


def test_do_operation_history_defaults_to_repo_wide_selector_resolution(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vcs_core._session import SessionDaemon

    daemon = SessionDaemon(str(workspace))
    experiment = ScopeInfo(
        name="experiment",
        ref="refs/vcscore/scopes/experiment",
        instance_id="scope_experiment",
        creation_oid="",
    )
    calls: list[tuple[str, object]] = []

    _patch_active_view(monkeypatch, _FakeActiveApp({"experiment": experiment}))
    daemon._current_scope_name = "experiment"
    daemon._mg = type(
        "FakeVcsCore",
        (),
        {
            "_repo_path": str(workspace),
            "resolve_operation_history": lambda self, selector, *, scope=None, max_count=200: (
                calls.append((selector, scope))
                or OperationHistory(
                    summary=OperationSummary(
                        operation_id="op_show",
                        label="Show Op",
                        kind="test.show",
                        status="ok",
                        visibility="visible",
                        world_id=experiment.world_id,
                        world_name="experiment",
                        world_ref=experiment.ref,
                        carrier_ref="refs/vcscore/scopes/experiment",
                        effect_count=1,
                    ),
                    commits=(
                        CommitInfo(
                            oid="deadbeefcafebabe",
                            message="Marker",
                            timestamp=0.0,
                            metadata={"type": "Marker"},
                            parent_oids=[],
                        ),
                    ),
                )
            ),
        },
    )()

    result = daemon._dispatch("operation_history", {"selector": "show-op"})

    assert calls == [("show-op", None)]
    assert result == {
        "requested_selector": "show-op",
        "scope": None,
        "summary": _expected_operation_summary(
            operation_id="op_show",
            label="Show Op",
            kind="test.show",
            world_id=None,
            world_name="experiment",
            world_ref=experiment.ref,
            carrier_ref="refs/vcscore/scopes/experiment",
            effect_count=1,
        ),
        "commits": [
            {
                "oid": "deadbeefcafebabe",
                "message": "Marker",
                "timestamp": 0.0,
                "metadata": {"type": "Marker"},
                "parent_oids": [],
            }
        ],
        # capture_shadow_status_for_history returns None for histories with
        # no requested capture; the production envelope includes the slot.
        "capture_shadow": None,
    }


def test_do_recovery_serializes_snapshot(workspace: Path) -> None:
    from vcs_core._session import SessionDaemon

    daemon = SessionDaemon(str(workspace))
    daemon._mg = type(
        "FakeVcsCore",
        (),
        {
            "_repo_path": str(workspace),
            "recovery_snapshot": lambda self, archived_max_count=20: RecoverySnapshot(
                orphaned_scope_refs=("refs/vcscore/scopes/experiment",),
                open_operations=(
                    OperationSummary(
                        operation_id="op_open",
                        label="Open Op",
                        kind="test.open",
                        status="open",
                        visibility="staged",
                        world_id="world_experiment",
                        world_name="experiment",
                        world_ref="refs/vcscore/scopes/experiment",
                        carrier_ref="refs/vcscore/ops/op_open",
                    ),
                ),
            ),
        },
    )()

    result = daemon._dispatch("recovery", {"max_count": 3})

    assert result == {
        "orphaned_scope_refs": ["refs/vcscore/scopes/experiment"],
        "open_operations": [
            _expected_operation_summary(
                operation_id="op_open",
                label="Open Op",
                kind="test.open",
                status="open",
                visibility="staged",
                world_id="world_experiment",
                world_name="experiment",
                world_ref="refs/vcscore/scopes/experiment",
                carrier_ref="refs/vcscore/ops/op_open",
            )
        ],
        "archived_recovery_operations": [],
        "orphaned_operations": [],
        # serialize_recovery_snapshot includes the workspace-authority
        # pending slot; empty when no pending workspace authority exists.
        "workspace_authority_pending": [],
    }
    assert "scope_name" not in result["open_operations"][0]
    assert "scope_ref" not in result["open_operations"][0]


def test_overlay_status_uses_vcscore_overlay_accessors(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vcs_core._session import SessionDaemon

    daemon = SessionDaemon(str(workspace))
    task = ScopeInfo(name="task", ref="refs/vcscore/scopes/task", instance_id="task-iid", creation_oid="")
    _patch_active_view(monkeypatch, _FakeActiveApp({"task": task}))

    daemon._current_scope_name = "task"
    daemon._mg = type(
        "FakeVcsCore",
        (),
        {
            "_repo_path": str(workspace),
            "ground": ScopeInfo(name="ground", ref="refs/vcscore/ground", instance_id="ground", creation_oid=""),
            "overlay_changes_for_scope": lambda self, scope: [
                ("note.txt", b"hello"),
                ("bin/run.sh", b"#!/bin/sh\n", 0o100755),
                ("gone.txt", None),
            ],
            "overlay_mount_path_for_scope": lambda self, scope: workspace / "overlay" / scope.name,
        },
    )()
    result = daemon._dispatch("overlay_status", {})

    assert result == {
        "scope": "task",
        "mount_path": str(workspace / "overlay" / "task"),
        "changes": [
            {"path": "note.txt", "type": "modify"},
            {"path": "bin/run.sh", "type": "modify", "mode": 0o100755},
            {"path": "gone.txt", "type": "delete"},
        ],
    }
