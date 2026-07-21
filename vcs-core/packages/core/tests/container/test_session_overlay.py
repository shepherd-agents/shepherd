# under-test: vcs_core._session
"""Overlay-backed session lifecycle tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner
from vcs_core._ipc import is_session_alive, read_session_info, send_request
from vcs_core.cli import main
from vcs_core.vcscore import VcsCore

pytestmark = [
    pytest.mark.container,
    pytest.mark.usefixtures("requires_local_bind"),
    pytest.mark.skipif(
        sys.platform != "linux" or os.geteuid() != 0,
        reason="Overlay session tests require Linux and root privileges.",
    ),
]

_requires_capture_overlay = pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() != 0 or shutil.which("cc") is None,
    reason="Capture overlay tests require Linux, root privileges, and a working `cc` compiler.",
)


@pytest.fixture
def session_workspace(tmp_path: Path) -> Path:
    """Workspace with a file for overlay testing."""
    ws = tmp_path / "project"
    ws.mkdir()
    (ws / "original.txt").write_text("original content")
    (ws / "vcscore.toml").write_text('[bindings.filesystem]\ntype = "filesystem"\nbackend = "kernel"\n')
    result = CliRunner().invoke(main, ["init", "--adopt", "worktree", "--all", str(ws)])
    assert result.exit_code == 0, result.output
    return ws


class TestOverlaySession:
    """Full session lifecycle tests with kernel overlayfs."""

    def test_session_foreground_start_stop(self, session_workspace: Path) -> None:
        import threading

        from vcs_core._session import SessionDaemon

        daemon = SessionDaemon(str(session_workspace))

        thread = threading.Thread(target=daemon._run, daemon=True)
        thread.start()

        repo_path = str(session_workspace / ".vcscore")
        deadline = time.time() + 5
        while time.time() < deadline:
            if is_session_alive(repo_path):
                break
            time.sleep(0.1)
        assert is_session_alive(repo_path), "Session daemon did not start"

        info = read_session_info(repo_path)
        assert info is not None
        assert info.pid == os.getpid()

        resp = send_request(info.socket_path, "stop")
        assert resp["ok"] is True
        thread.join(timeout=5)

    def test_overlay_captures_file_write(self, session_workspace: Path) -> None:
        import threading

        from vcs_core._session import SessionDaemon

        daemon = SessionDaemon(str(session_workspace))
        thread = threading.Thread(target=daemon._run, daemon=True)
        thread.start()

        repo_path = str(session_workspace / ".vcscore")
        deadline = time.time() + 5
        while time.time() < deadline:
            if is_session_alive(repo_path):
                break
            time.sleep(0.1)

        info = read_session_info(repo_path)
        assert info is not None

        try:
            resp = send_request(info.socket_path, "fork", {"name": "edit", "parent": "ground", "isolated": True})
            assert resp["ok"], resp.get("error")
            mount_path = resp["result"]["mount_path"]

            test_file = Path(mount_path) / "new_file.txt"
            with test_file.open("w") as f:
                f.write("hello from overlay")

            resp = send_request(info.socket_path, "merge", {"name": "edit"})
            assert resp["ok"], resp.get("error")

            from vcs_core.store import Store

            store = Store(repo_path)
            effects = store.filter_effects(effect_type="FileCreate")
            paths = [e.metadata.get("path") for e in effects]
            assert "new_file.txt" in paths

        finally:
            send_request(info.socket_path, "stop")
            thread.join(timeout=5)

    def test_switch_changes_mount_path(self, session_workspace: Path) -> None:
        import threading

        from vcs_core._session import SessionDaemon

        daemon = SessionDaemon(str(session_workspace))
        thread = threading.Thread(target=daemon._run, daemon=True)
        thread.start()

        repo_path = str(session_workspace / ".vcscore")
        deadline = time.time() + 5
        while time.time() < deadline:
            if is_session_alive(repo_path):
                break
            time.sleep(0.1)

        info = read_session_info(repo_path)
        assert info is not None

        try:
            resp = send_request(info.socket_path, "fork", {"name": "experiment", "parent": "ground", "isolated": True})
            assert resp["ok"], resp.get("error")
            experiment_path = resp["result"]["mount_path"]

            resp = send_request(info.socket_path, "switch", {"name": "ground"})
            assert resp["ok"], resp.get("error")
            ground_path = resp["result"]["mount_path"]

            assert experiment_path != ground_path
            assert "experiment" in experiment_path
            # Ground is the real working copy, never a carrier layer
            # (overlay_mount_path_for_scope): switching to ground surfaces the
            # actual workspace, not a ground overlay mount.
            assert ground_path == str(session_workspace.resolve())

            resp = send_request(info.socket_path, "switch", {"name": "experiment"})
            assert resp["ok"], resp.get("error")
            assert resp["result"]["mount_path"] == experiment_path

        finally:
            send_request(info.socket_path, "stop")
            thread.join(timeout=5)

    def test_session_switch_get_state_merge_produces_graph_history(self, session_workspace: Path) -> None:
        import threading

        from vcs_core._graph import render_graph
        from vcs_core._session import SessionDaemon
        from vcs_core.store import Store

        daemon = SessionDaemon(str(session_workspace))
        thread = threading.Thread(target=daemon._run, daemon=True)
        thread.start()

        repo_path = str(session_workspace / ".vcscore")
        deadline = time.time() + 5
        while time.time() < deadline:
            if is_session_alive(repo_path):
                break
            time.sleep(0.1)

        info = read_session_info(repo_path)
        assert info is not None

        try:
            resp = send_request(info.socket_path, "fork", {"name": "shell-task", "parent": "ground", "isolated": True})
            assert resp["ok"], resp.get("error")

            resp = send_request(info.socket_path, "switch", {"name": "shell-task"})
            assert resp["ok"], resp.get("error")

            resp = send_request(info.socket_path, "get_state")
            assert resp["ok"], resp.get("error")
            mount_path = Path(resp["result"]["mount_path"])
            assert resp["result"]["current_scope"] == "shell-task"

            with (mount_path / "notes.txt").open("w") as f:
                f.write("shell workflow\n")

            resp = send_request(info.socket_path, "merge", {"name": "shell-task"})
            assert resp["ok"], resp.get("error")

            store = Store(repo_path)
            lines = render_graph(store.log(max_count=10))
            assert any("FileCreate" in line and "scope:shell-task" in line for line in lines)
            assert any("ScopeMerge" in line and "scope:shell-task" in line for line in lines)
        finally:
            send_request(info.socket_path, "stop")
            thread.join(timeout=5)

    @_requires_capture_overlay
    def test_manual_preload_without_command_envelope_records_no_direct_effect(self, session_workspace: Path) -> None:
        import threading

        from vcs_core._fs_capture import ensure_fs_capture_shim
        from vcs_core._session import SessionDaemon
        from vcs_core.store import Store

        daemon = SessionDaemon(str(session_workspace))
        thread = threading.Thread(target=daemon._run, daemon=True)
        thread.start()

        repo_path = str(session_workspace / ".vcscore")
        deadline = time.time() + 5
        while time.time() < deadline:
            if is_session_alive(repo_path):
                break
            time.sleep(0.1)

        info = read_session_info(repo_path)
        assert info is not None

        try:
            resp = send_request(info.socket_path, "fork", {"name": "captured", "parent": "ground", "isolated": True})
            assert resp["ok"], resp.get("error")
            scope_ref = resp["result"]["ref"]
            mount_path = resp["result"]["mount_path"]
            scope_instance_id = resp["result"]["instance_id"]

            state_resp = send_request(info.socket_path, "get_state")
            assert state_resp["ok"], state_resp.get("error")
            hook_socket = state_resp["result"]["hook_socket"]
            shim_path = ensure_fs_capture_shim(Path(repo_path))

            env = {
                **os.environ,
                "VCS_CORE_SESSION": "1",
                "VCS_CORE_SCOPE": "captured",
                "VCS_CORE_SCOPE_INSTANCE_ID": scope_instance_id,
                "VCS_CORE_WORKSPACE": mount_path,
                "VCS_CORE_HOOK_SOCKET": hook_socket,
                "LD_PRELOAD": shim_path,
            }
            result = subprocess.run(
                ["/bin/bash", "-lc", "printf 'hello\\n' > note.txt"],
                cwd=mount_path,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, result.stderr

            hook_state = send_request(info.socket_path, "hook_state")
            assert hook_state["ok"], hook_state.get("error")
            assert hook_state["result"]["accepted_seq"] == 0
            assert hook_state["result"]["persisted_seq"] == 0
            assert hook_state["result"]["failed_seq"] == 0

            store = Store(repo_path)
            direct_effects = [
                effect
                for effect in store.filter_effects(
                    effect_type="FileCreate", ref=scope_ref, scope="captured", max_count=20
                )
                if effect.metadata.get("path") == "note.txt"
            ]
            assert direct_effects == []
        finally:
            send_request(info.socket_path, "stop")
            thread.join(timeout=5)

    @_requires_capture_overlay
    def test_capture_session_exec_records_complete_managed_command(
        self,
        session_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import contextlib
        import threading

        from vcs_core._session import SessionDaemon

        daemon = SessionDaemon(str(session_workspace))
        thread = threading.Thread(target=daemon._run, daemon=True)
        thread.start()

        repo_path = str(session_workspace / ".vcscore")
        deadline = time.time() + 5
        while time.time() < deadline:
            if is_session_alive(repo_path):
                break
            time.sleep(0.1)

        info = read_session_info(repo_path)
        assert info is not None

        try:
            monkeypatch.chdir(session_workspace)
            result = CliRunner().invoke(
                main,
                [
                    "session",
                    "exec",
                    "--scope",
                    "managed-capture",
                    "--create",
                    "--capture",
                    "--",
                    "/bin/bash",
                    "-lc",
                    "printf managed > managed.txt",
                ],
            )
            assert result.exit_code == 0, result.output

            mg = VcsCore.from_config(str(session_workspace))
            archived = [
                summary for summary in mg.archived_operations(max_count=20) if summary.kind == "vcs_core.session_exec"
            ]
            assert len(archived) == 1
            history = mg.resolve_operation_history(archived[0].operation_id)
            completed = history.commits[0].metadata["command"]
            assert completed["status"] == "success"
            assert completed["capture_status"] == "complete"
            assert completed["capture_stream_status"] == "drained"
            assert completed["capture_registered_processes"] >= 1
            assert completed["capture_finished_processes"] == completed["capture_registered_processes"]
            assert completed["capture_event_count"] >= 1

            reducer_history = mg.resolve_operation_history(f"red_{archived[0].operation_id}")
            assert reducer_history.summary.kind == "vcs_core.fs_capture_reduction"
            assert reducer_history.summary.effect_count == 1
        finally:
            with contextlib.suppress(Exception):
                send_request(info.socket_path, "stop")
            thread.join(timeout=5)

    # ------------------------------------------------------------------
    # Shepherd-integration spine: a real agent subprocess, captured via the
    # `session exec --capture` PRODUCT entry point, reduced through the
    # workspace substrate SPI driver (_shadow_workspace_capture_reduction),
    # and made reversible by merge (persist) / discard (revert). The `sh`
    # command is the deterministic stand-in for the Claude Agent SDK's child
    # process; swapping in the real SDK is provider-wiring, not architecture.
    # ------------------------------------------------------------------

    @_requires_capture_overlay
    def test_agent_session_exec_capture_merges_into_ground(
        self,
        session_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Real subprocess edits a file under capture; merge persists it to ground.

        v2-truth gate (assertions (a) and (b)) on the LD_PRELOAD-shim capture
        path — the production entry point ``shepherd.vcscore.run_in_vcscore_session``
        uses. Distinct capture mechanism from overlay-diff (the totalizing path
        covered in ``test_subprocess_overlay_e2e.py``); same gate logic.
        """
        import contextlib
        import threading

        from vcs_core._session import SessionDaemon
        from vcs_core.store import Store
        from vcs_core.substrates import (
            STRICT_TREE_BACKED_MATERIALIZATION_ENV,
            reset_scalar_fallback_invocations,
            scalar_fallback_invocations,
        )

        monkeypatch.setenv(STRICT_TREE_BACKED_MATERIALIZATION_ENV, "true")
        reset_scalar_fallback_invocations()

        daemon = SessionDaemon(str(session_workspace))
        thread = threading.Thread(target=daemon._run, daemon=True)
        thread.start()
        repo_path = str(session_workspace / ".vcscore")
        deadline = time.time() + 5
        while time.time() < deadline:
            if is_session_alive(repo_path):
                break
            time.sleep(0.1)
        info = read_session_info(repo_path)
        assert info is not None

        try:
            monkeypatch.chdir(session_workspace)
            result = CliRunner().invoke(
                main,
                [
                    "session",
                    "exec",
                    "--scope",
                    "agent-task",
                    "--create",
                    "--capture",
                    "--",
                    "/bin/bash",
                    "-lc",
                    "mkdir -p src && printf 'edited by agent' > src/agent.txt",
                ],
            )
            assert result.exit_code == 0, result.output

            # SPI-shaped: command-correlated capture reduced through the
            # fs_capture_reduction reducer (the WorkspaceSubstrateDriver path).
            mg = VcsCore.from_config(str(session_workspace))
            archived = [
                summary for summary in mg.archived_operations(max_count=20) if summary.kind == "vcs_core.session_exec"
            ]
            assert len(archived) == 1
            reducer_history = mg.resolve_operation_history(f"red_{archived[0].operation_id}")
            assert reducer_history.summary.kind == "vcs_core.fs_capture_reduction"

            # Reversible (success path): merge the captured scope into ground.
            merged = send_request(info.socket_path, "merge", {"name": "agent-task"})
            assert merged["ok"] is True, merged
        finally:
            with contextlib.suppress(Exception):
                send_request(info.socket_path, "stop")
            thread.join(timeout=5)

        store = Store(repo_path)
        assert store.read_workspace_file(Store.GROUND_REF, "src/agent.txt") == b"edited by agent"

        # v2-truth gate. Fresh VcsCore handle now that the daemon is stopped.
        mg_post = VcsCore.from_config(str(session_workspace))

        # (a) v2 substrate tree carries the captured bytes — the LD_PRELOAD-shim
        # capture path feeds the same tree-backed revision the overlay-diff
        # path does, validated against v2 truth (not GROUND_REF).
        v2_read = mg_post._read_v2_workspace_file_for_materialization("src/agent.txt")
        assert v2_read is not None, (
            "v2 substrate tree must carry the session-captured bytes; "
            "LD_PRELOAD-shim capture is not landing tree-backed truth"
        )
        content, _mode = v2_read
        assert content == b"edited by agent", f"v2 served wrong bytes for the session-capture flow: {content!r}"

        # (b) Strict-mode materialization completes without scalar fallback,
        # proving the materializer reads the v2 tree for every diff path on
        # the session-capture path.
        counter_before = scalar_fallback_invocations()
        mg_post.push()
        counter_after = scalar_fallback_invocations()
        assert counter_after == counter_before, (
            f"materialization fell back to scalar {counter_after - counter_before} time(s) "
            f"on the session-capture flow; v2 tree is not the sole authority"
        )
        assert (session_workspace / "src" / "agent.txt").read_bytes() == b"edited by agent"

    @_requires_capture_overlay
    def test_agent_session_exec_capture_discard_reverts(
        self,
        session_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Real subprocess edits a file under capture; discard reverts it from ground."""
        import contextlib
        import threading

        from vcs_core._session import SessionDaemon
        from vcs_core.store import Store

        daemon = SessionDaemon(str(session_workspace))
        thread = threading.Thread(target=daemon._run, daemon=True)
        thread.start()
        repo_path = str(session_workspace / ".vcscore")
        deadline = time.time() + 5
        while time.time() < deadline:
            if is_session_alive(repo_path):
                break
            time.sleep(0.1)
        info = read_session_info(repo_path)
        assert info is not None

        try:
            monkeypatch.chdir(session_workspace)
            result = CliRunner().invoke(
                main,
                [
                    "session",
                    "exec",
                    "--scope",
                    "agent-throwaway",
                    "--create",
                    "--capture",
                    "--",
                    "/bin/bash",
                    "-lc",
                    "printf 'should not survive' > scratch.txt",
                ],
            )
            assert result.exit_code == 0, result.output

            # The capture happened (so discard is reverting real captured work).
            mg = VcsCore.from_config(str(session_workspace))
            archived = [
                summary for summary in mg.archived_operations(max_count=20) if summary.kind == "vcs_core.session_exec"
            ]
            assert len(archived) == 1

            # Reversible (failure path): discard the captured scope.
            discarded = send_request(info.socket_path, "discard", {"name": "agent-throwaway"})
            assert discarded["ok"] is True, discarded
        finally:
            with contextlib.suppress(Exception):
                send_request(info.socket_path, "stop")
            thread.join(timeout=5)

        store = Store(repo_path)
        assert store.read_workspace_file(Store.GROUND_REF, "scratch.txt") is None

    @_requires_capture_overlay
    def test_manual_preload_without_command_envelope_records_no_delete_effect(self, session_workspace: Path) -> None:
        import contextlib
        import threading

        from vcs_core._fs_capture import ensure_fs_capture_shim
        from vcs_core._session import SessionDaemon
        from vcs_core.store import Store

        daemon = SessionDaemon(str(session_workspace))
        thread = threading.Thread(target=daemon._run, daemon=True)
        thread.start()

        repo_path = str(session_workspace / ".vcscore")
        deadline = time.time() + 5
        while time.time() < deadline:
            if is_session_alive(repo_path):
                break
            time.sleep(0.1)

        info = read_session_info(repo_path)
        assert info is not None

        try:
            resp = send_request(
                info.socket_path, "fork", {"name": "delete-adopted", "parent": "ground", "isolated": True}
            )
            assert resp["ok"], resp.get("error")
            mount_path = resp["result"]["mount_path"]
            scope_instance_id = resp["result"]["instance_id"]

            assert (Path(mount_path) / "original.txt").read_text() == "original content"

            state_resp = send_request(info.socket_path, "get_state")
            assert state_resp["ok"], state_resp.get("error")
            hook_socket = state_resp["result"]["hook_socket"]
            shim_path = ensure_fs_capture_shim(Path(repo_path))

            env = {
                **os.environ,
                "VCS_CORE_SESSION": "1",
                "VCS_CORE_SCOPE": "delete-adopted",
                "VCS_CORE_SCOPE_INSTANCE_ID": scope_instance_id,
                "VCS_CORE_WORKSPACE": mount_path,
                "VCS_CORE_HOOK_SOCKET": hook_socket,
                "LD_PRELOAD": shim_path,
            }
            result = subprocess.run(
                ["/bin/bash", "-lc", "rm original.txt"],
                cwd=mount_path,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, result.stderr

            hook_state = send_request(info.socket_path, "hook_state")
            assert hook_state["ok"], hook_state.get("error")
            assert hook_state["result"]["accepted_seq"] == 0
            assert hook_state["result"]["persisted_seq"] == 0
            assert hook_state["result"]["failed_seq"] == 0

            store = Store(repo_path)
            direct_deletes = [
                effect
                for effect in store.filter_effects(effect_type="FileDelete", scope="delete-adopted", max_count=20)
                if effect.metadata.get("path") == "original.txt"
            ]
            assert direct_deletes == []
        finally:
            with contextlib.suppress(Exception):
                send_request(info.socket_path, "stop")
            thread.join(timeout=5)

    @_requires_capture_overlay
    def test_manual_preload_without_command_envelope_records_no_quoted_path_effect(
        self, session_workspace: Path
    ) -> None:
        import threading

        from vcs_core._fs_capture import ensure_fs_capture_shim
        from vcs_core._session import SessionDaemon
        from vcs_core.store import Store

        daemon = SessionDaemon(str(session_workspace))
        thread = threading.Thread(target=daemon._run, daemon=True)
        thread.start()

        repo_path = str(session_workspace / ".vcscore")
        deadline = time.time() + 5
        while time.time() < deadline:
            if is_session_alive(repo_path):
                break
            time.sleep(0.1)

        info = read_session_info(repo_path)
        assert info is not None

        try:
            resp = send_request(info.socket_path, "fork", {"name": "quoted", "parent": "ground", "isolated": True})
            assert resp["ok"], resp.get("error")
            mount_path = resp["result"]["mount_path"]
            scope_instance_id = resp["result"]["instance_id"]

            state_resp = send_request(info.socket_path, "get_state")
            assert state_resp["ok"], state_resp.get("error")
            hook_socket = state_resp["result"]["hook_socket"]
            shim_path = ensure_fs_capture_shim(Path(repo_path))

            env = {
                **os.environ,
                "VCS_CORE_SESSION": "1",
                "VCS_CORE_SCOPE": "quoted",
                "VCS_CORE_SCOPE_INSTANCE_ID": scope_instance_id,
                "VCS_CORE_WORKSPACE": mount_path,
                "VCS_CORE_HOOK_SOCKET": hook_socket,
                "LD_PRELOAD": shim_path,
            }
            result = subprocess.run(
                [
                    "/bin/bash",
                    "-lc",
                    "python3 - <<'PY'\nfrom pathlib import Path\nPath('quote\"name.txt').write_text('hello\\n')\nPY",
                ],
                cwd=mount_path,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, result.stderr

            hook_state = send_request(info.socket_path, "hook_state")
            assert hook_state["ok"], hook_state.get("error")
            assert hook_state["result"]["accepted_seq"] == 0
            assert hook_state["result"]["persisted_seq"] == 0
            assert hook_state["result"]["failed_seq"] == 0

            store = Store(repo_path)
            direct_effects = [
                effect
                for effect in store.filter_effects(effect_type="FileCreate", scope="quoted", max_count=20)
                if effect.metadata.get("path") == 'quote"name.txt'
            ]
            assert direct_effects == []
        finally:
            send_request(info.socket_path, "stop")
            thread.join(timeout=5)

    @_requires_capture_overlay
    def test_manual_preload_without_command_envelope_ignores_paths_that_escape_workspace(
        self, session_workspace: Path
    ) -> None:
        import threading

        from vcs_core._fs_capture import ensure_fs_capture_shim
        from vcs_core._session import SessionDaemon
        from vcs_core.store import Store

        daemon = SessionDaemon(str(session_workspace))
        thread = threading.Thread(target=daemon._run, daemon=True)
        thread.start()

        repo_path = str(session_workspace / ".vcscore")
        deadline = time.time() + 5
        while time.time() < deadline:
            if is_session_alive(repo_path):
                break
            time.sleep(0.1)

        info = read_session_info(repo_path)
        assert info is not None

        try:
            resp = send_request(info.socket_path, "fork", {"name": "escaped", "parent": "ground", "isolated": True})
            assert resp["ok"], resp.get("error")
            scope_ref = resp["result"]["ref"]
            mount_path = resp["result"]["mount_path"]
            scope_instance_id = resp["result"]["instance_id"]

            state_resp = send_request(info.socket_path, "get_state")
            assert state_resp["ok"], state_resp.get("error")
            hook_socket = state_resp["result"]["hook_socket"]
            shim_path = ensure_fs_capture_shim(Path(repo_path))

            outside_dir = session_workspace.parent / "outside-capture-target"
            outside_dir.mkdir()
            env = {
                **os.environ,
                "VCS_CORE_SESSION": "1",
                "VCS_CORE_SCOPE": "escaped",
                "VCS_CORE_SCOPE_INSTANCE_ID": scope_instance_id,
                "VCS_CORE_WORKSPACE": mount_path,
                "VCS_CORE_HOOK_SOCKET": hook_socket,
                "LD_PRELOAD": shim_path,
                "OUTSIDE_DIR": str(outside_dir),
            }
            script = r"""
from pathlib import Path
import os

workspace = Path.cwd()
outside = Path(os.environ["OUTSIDE_DIR"])
(workspace / "escape-link").symlink_to(outside, target_is_directory=True)

Path("../outside-relative.txt").write_text("relative escape\n")
(outside / "absolute.txt").write_text("absolute escape\n")
(workspace / "escape-link" / "symlink.txt").write_text("symlink escape\n")

os.chmod("../outside-relative.txt", 0o755)
os.chmod(outside / "absolute.txt", 0o755)
os.chmod(workspace / "escape-link" / "symlink.txt", 0o755)

Path("../outside-relative.txt").unlink()
(outside / "absolute.txt").unlink()
(workspace / "escape-link" / "symlink.txt").unlink()
"""
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=mount_path,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, result.stderr

            hook_state = send_request(info.socket_path, "hook_state")
            assert hook_state["ok"], hook_state.get("error")
            assert hook_state["result"]["failed_seq"] == 0

            store = Store(repo_path)
            direct_effects = [
                effect
                for effect in store.filter_effects(ref=scope_ref, scope="escaped", max_count=20)
                if effect.metadata.get("capture_mode") == "direct"
            ]
            assert direct_effects == []
        finally:
            send_request(info.socket_path, "stop")
            thread.join(timeout=5)
