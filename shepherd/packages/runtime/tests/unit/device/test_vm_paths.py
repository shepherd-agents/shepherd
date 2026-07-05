"""Tests for VM path translation and command execution.

Unit tests mock subprocess calls for fast, reliable testing.
Integration tests (marked with @pytest.mark.integration) require a running
Podman Machine on macOS and are skipped otherwise.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from shepherd_runtime.device.container.vm_paths import (
    VMCommandRunner,
    VMPathTranslator,
    is_macos,
    is_vm_available,
)

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def mock_mount_output_standard():
    """Standard VirtioFS mount output (direct paths)."""
    return """
sysfs on /sys type sysfs (rw,nosuid,nodev,noexec,relatime)
proc on /proc type proc (rw,nosuid,nodev,noexec,relatime)
devtmpfs on /dev type devtmpfs (rw,nosuid,size=1965988k,nr_inodes=491497,mode=755,inode64)
virtiofs on /Users type virtiofs (rw,relatime)
virtiofs on /private type virtiofs (rw,relatime)
virtiofs on /Volumes type virtiofs (rw,relatime)
tmpfs on /run type tmpfs (rw,nosuid,nodev,size=393304k,nr_inodes=819200,mode=755,inode64)
"""


@pytest.fixture
def mock_mount_output_prefixed():
    """VirtioFS mount output with /mnt/host prefix."""
    return """
sysfs on /sys type sysfs (rw,nosuid,nodev,noexec,relatime)
virtiofs on /mnt/host/Users type virtiofs (rw,relatime)
virtiofs on /mnt/host/private type virtiofs (rw,relatime)
virtiofs on /mnt/host/Volumes type virtiofs (rw,relatime)
"""


@pytest.fixture
def mock_runner():
    """Mock VMCommandRunner for unit tests."""
    runner = MagicMock(spec=VMCommandRunner)
    runner.run.return_value = MagicMock(
        returncode=0,
        stdout="ok",
        stderr="",
    )
    return runner


# =============================================================================
# VMPathTranslator Unit Tests
# =============================================================================


class TestVMPathTranslatorParsing:
    """Tests for mount output parsing."""

    def test_parse_standard_mounts(self, mock_mount_output_standard):
        """Parse standard VirtioFS mounts (direct paths)."""
        mounts = VMPathTranslator._parse_mount_output(mock_mount_output_standard)

        assert Path("/Users") in mounts
        assert Path("/private") in mounts
        assert Path("/Volumes") in mounts
        # /tmp → /private/tmp mapping should be added
        assert Path("/tmp") in mounts

        # Standard mounts: VM path matches host path
        assert mounts[Path("/Users")] == Path("/Users")
        assert mounts[Path("/private")] == Path("/private")
        assert mounts[Path("/tmp")] == Path("/private/tmp")

    def test_parse_prefixed_mounts(self, mock_mount_output_prefixed):
        """Parse VirtioFS mounts with /mnt/host prefix."""
        mounts = VMPathTranslator._parse_mount_output(mock_mount_output_prefixed)

        assert Path("/Users") in mounts
        assert Path("/private") in mounts

        # Prefixed mounts: VM path has /mnt/host prefix
        assert mounts[Path("/Users")] == Path("/mnt/host/Users")
        assert mounts[Path("/private")] == Path("/mnt/host/private")

    def test_parse_empty_output(self):
        """Empty mount output should return empty dict."""
        mounts = VMPathTranslator._parse_mount_output("")
        assert mounts == {}

    def test_parse_no_virtiofs(self):
        """Mount output without VirtioFS should return empty dict."""
        output = """
sysfs on /sys type sysfs (rw,nosuid)
proc on /proc type proc (rw,nosuid)
"""
        mounts = VMPathTranslator._parse_mount_output(output)
        assert mounts == {}


class TestVMPathTranslatorTranslation:
    """Tests for path translation logic."""

    def test_host_to_vm_standard(self, mock_mount_output_standard):
        """Translate host path to VM path (standard mounts)."""
        mounts = VMPathTranslator._parse_mount_output(mock_mount_output_standard)
        translator = VMPathTranslator(virtio_mounts=mounts)

        # /Users path
        vm_path = translator.host_to_vm(Path("/Users/dcx/project"))
        assert vm_path == Path("/Users/dcx/project")

        # /tmp path (should map to /private/tmp)
        vm_path = translator.host_to_vm(Path("/tmp/test"))
        assert vm_path == Path("/private/tmp/test")

    def test_host_to_vm_prefixed(self, mock_mount_output_prefixed):
        """Translate host path to VM path (prefixed mounts)."""
        mounts = VMPathTranslator._parse_mount_output(mock_mount_output_prefixed)
        translator = VMPathTranslator(virtio_mounts=mounts)

        vm_path = translator.host_to_vm(Path("/Users/dcx/project"))
        assert vm_path == Path("/mnt/host/Users/dcx/project")

    def test_host_to_vm_unmapped_raises(self, mock_mount_output_standard):
        """Unmapped paths should raise ValueError."""
        mounts = VMPathTranslator._parse_mount_output(mock_mount_output_standard)
        translator = VMPathTranslator(virtio_mounts=mounts)

        with pytest.raises(ValueError, match="not under any VirtioFS mount"):
            translator.host_to_vm(Path("/nonexistent/path"))

    def test_host_to_vm_longest_match(self):
        """Should use longest matching mount prefix."""
        mounts = {
            Path("/Users"): Path("/Users"),
            Path("/Users/dcx"): Path("/home/dcx"),  # More specific
        }
        translator = VMPathTranslator(virtio_mounts=mounts)

        # Should match /Users/dcx (longer) not /Users
        vm_path = translator.host_to_vm(Path("/Users/dcx/project"))
        assert vm_path == Path("/home/dcx/project")

        # Should match /Users (only option)
        vm_path = translator.host_to_vm(Path("/Users/other/project"))
        assert vm_path == Path("/Users/other/project")

    def test_vm_to_host_standard(self, mock_mount_output_standard):
        """Translate VM path back to host path."""
        mounts = VMPathTranslator._parse_mount_output(mock_mount_output_standard)
        translator = VMPathTranslator(virtio_mounts=mounts)

        host_path = translator.vm_to_host(Path("/Users/dcx/project"))
        assert host_path == Path("/Users/dcx/project")

        host_path = translator.vm_to_host(Path("/private/tmp/test"))
        assert host_path == Path("/private/tmp/test")

    def test_vm_to_host_prefixed(self, mock_mount_output_prefixed):
        """Translate prefixed VM path back to host path."""
        mounts = VMPathTranslator._parse_mount_output(mock_mount_output_prefixed)
        translator = VMPathTranslator(virtio_mounts=mounts)

        host_path = translator.vm_to_host(Path("/mnt/host/Users/dcx/project"))
        assert host_path == Path("/Users/dcx/project")

    def test_vm_to_host_native_returns_none(self, mock_mount_output_standard):
        """VM-native paths should return None."""
        mounts = VMPathTranslator._parse_mount_output(mock_mount_output_standard)
        translator = VMPathTranslator(virtio_mounts=mounts)

        # /var is not on VirtioFS
        host_path = translator.vm_to_host(Path("/var/shepherd/overlays"))
        assert host_path is None

    def test_is_vm_native(self, mock_mount_output_standard):
        """Check if path is VM-native."""
        mounts = VMPathTranslator._parse_mount_output(mock_mount_output_standard)
        translator = VMPathTranslator(virtio_mounts=mounts)

        assert translator.is_vm_native(Path("/var/shepherd/overlays"))
        assert not translator.is_vm_native(Path("/Users/dcx/project"))

    def test_get_overlay_path_default_root(self, mock_mount_output_standard):
        """Overlay path uses default vm_overlays_root.

        Contract: get_overlay_path returns vm_overlays_root / task_id / context_name.
        This test verifies the default root and path structure.
        """
        mounts = VMPathTranslator._parse_mount_output(mock_mount_output_standard)
        translator = VMPathTranslator(virtio_mounts=mounts)

        path = translator.get_overlay_path("task-123", "workspace")

        # Verify path structure: root / task_id / context_name
        assert path.parent.name == "task-123"  # task_id is parent directory
        assert path.name == "workspace"  # context_name is leaf
        assert "task-123" in str(path)
        assert "workspace" in str(path)

    def test_get_overlay_path_custom_root(self, mock_mount_output_standard):
        """Overlay path respects custom vm_overlays_root.

        Contract: get_overlay_path returns vm_overlays_root / task_id / context_name.
        The root can be customized at construction time.
        """
        mounts = VMPathTranslator._parse_mount_output(mock_mount_output_standard)
        custom_root = Path("/custom/overlay/location")
        translator = VMPathTranslator(virtio_mounts=mounts, vm_overlays_root=custom_root)

        path = translator.get_overlay_path("my-task", "ctx")

        assert path == custom_root / "my-task" / "ctx"
        assert path == Path("/custom/overlay/location/my-task/ctx")

    def test_roundtrip_translation(self, mock_mount_output_standard):
        """host_to_vm → vm_to_host should return original."""
        mounts = VMPathTranslator._parse_mount_output(mock_mount_output_standard)
        translator = VMPathTranslator(virtio_mounts=mounts)

        original = Path("/Users/dcx/test/project")
        vm_path = translator.host_to_vm(original)
        recovered = translator.vm_to_host(vm_path)
        assert recovered == original


class TestVMPathTranslatorDiscovery:
    """Tests for VMPathTranslator.discover()."""

    def test_discover_success(self, mock_mount_output_standard):
        """Successful discovery with verification disabled."""
        with patch.object(VMCommandRunner, "run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=mock_mount_output_standard,
                stderr="",
            )

            translator = VMPathTranslator.discover(verify=False)

            assert Path("/Users") in translator.virtio_mounts
            mock_run.assert_called_once()

    def test_discover_vm_not_running(self):
        """Discovery should fail with clear message if VM not running."""
        with patch.object(VMCommandRunner, "run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "podman machine ssh", "", "connection refused")

            with pytest.raises(RuntimeError, match="Is Podman Machine running"):
                VMPathTranslator.discover(verify=False)

    def test_discover_timeout(self):
        """Discovery should fail with clear message on timeout."""
        with patch.object(VMCommandRunner, "run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("cmd", 10)

            with pytest.raises(RuntimeError, match="Timed out"):
                VMPathTranslator.discover(verify=False)

    def test_discover_no_mounts(self):
        """Discovery should fail if no VirtioFS mounts found."""
        with patch.object(VMCommandRunner, "run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="sysfs on /sys type sysfs (rw)\n",
                stderr="",
            )

            with pytest.raises(RuntimeError, match="No VirtioFS mounts found"):
                VMPathTranslator.discover(verify=False)


# =============================================================================
# VMCommandRunner Unit Tests
# =============================================================================


class TestVMCommandRunner:
    """Tests for VMCommandRunner.

    Note on command format tests:
    ----------------------------
    These tests verify exact command strings (e.g., ["podman", "machine", "ssh", ...]).
    This is INTENTIONAL - VMCommandRunner's contract is to execute commands via
    `podman machine ssh`. The command format is part of this contract because:

    1. Consumers depend on commands being executed in the VM via SSH
    2. Shell command syntax (mkdir -p, rm -rf, etc.) must be correct
    3. Path quoting must handle spaces and special characters

    If refactoring changes how commands are constructed, these tests ensure the
    observable behavior (what gets executed in the VM) remains correct.
    """

    def test_run_success(self):
        """Successful command execution via podman machine ssh.

        Contract: Commands are executed via `podman machine ssh <command>`.
        """
        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = MagicMock(
                returncode=0,
                stdout="hello world",
                stderr="",
            )

            runner = VMCommandRunner(timeout=10.0)
            result = runner.run("echo hello world")

            assert result.stdout == "hello world"
            # Verify exact command structure - this is the contract
            mock_subprocess.assert_called_once_with(
                ["podman", "machine", "ssh", "echo hello world"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10.0,
            )

    def test_run_failure_with_check(self):
        """Command failure should raise when check=True."""
        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="command not found",
            )

            runner = VMCommandRunner()
            with pytest.raises(subprocess.CalledProcessError):
                runner.run("invalid_command", check=True)

    def test_run_failure_without_check(self):
        """Command failure should not raise when check=False."""
        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="error",
            )

            runner = VMCommandRunner()
            result = runner.run("may_fail", check=False)
            assert result.returncode == 1

    def test_run_custom_timeout(self):
        """Custom timeout should be used."""
        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = MagicMock(returncode=0, stdout="", stderr="")

            runner = VMCommandRunner(timeout=30.0)
            runner.run("cmd", timeout=5.0)

            # Should use custom timeout, not default
            mock_subprocess.assert_called_once()
            assert mock_subprocess.call_args[1]["timeout"] == 5.0

    def test_run_batch(self):
        """Batch commands joined with && for sequential execution.

        Contract: Multiple commands are combined with ' && ' so they execute
        sequentially and stop on first failure. This is shell behavior that
        consumers depend on.
        """
        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = MagicMock(returncode=0, stdout="", stderr="")

            runner = VMCommandRunner()
            runner.run_batch(["echo a", "echo b", "echo c"])

            mock_subprocess.assert_called_once()
            cmd_arg = mock_subprocess.call_args[0][0][3]  # The shell command
            assert cmd_arg == "echo a && echo b && echo c"

    def test_mkdir_p(self):
        """mkdir_p creates directories with proper quoting.

        Contract: Uses `mkdir -p` with quoted paths to handle spaces correctly.
        """
        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = MagicMock(returncode=0, stdout="", stderr="")

            runner = VMCommandRunner()
            runner.mkdir_p(Path("/var/test/a"), Path("/var/test/b"))

            mock_subprocess.assert_called_once()
            cmd = mock_subprocess.call_args[0][0][3]
            assert "mkdir -p" in cmd
            assert '"/var/test/a"' in cmd
            assert '"/var/test/b"' in cmd

    def test_mkdir_p_empty(self):
        """mkdir_p with no paths should be no-op."""
        with patch("subprocess.run") as mock_subprocess:
            runner = VMCommandRunner()
            runner.mkdir_p()
            mock_subprocess.assert_not_called()

    def test_rm_rf(self):
        """rm_rf removes directory tree with proper quoting.

        Contract: Uses `rm -rf` with quoted path to handle spaces correctly.
        """
        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = MagicMock(returncode=0, stdout="", stderr="")

            runner = VMCommandRunner()
            runner.rm_rf(Path("/var/test/dir"))

            cmd = mock_subprocess.call_args[0][0][3]
            assert 'rm -rf "/var/test/dir"' in cmd

    def test_exists_true(self):
        """Exists should return True when path exists."""
        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = MagicMock(returncode=0, stdout="", stderr="")

            runner = VMCommandRunner()
            assert runner.exists(Path("/var/test"))

    def test_exists_false(self):
        """Exists should return False when path doesn't exist."""
        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="")

            runner = VMCommandRunner()
            assert not runner.exists(Path("/nonexistent"))

    def test_is_mounted_true(self):
        """is_mounted returns True for mount points.

        Contract: Uses `mountpoint -q` for robust mount detection (handles
        whitespace in paths better than parsing /proc/mounts).
        """
        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = MagicMock(returncode=0, stdout="", stderr="")

            runner = VMCommandRunner()
            assert runner.is_mounted(Path("/var/test/merged"))

            # Verify mountpoint command is used (more robust than grep)
            cmd = mock_subprocess.call_args[0][0][3]
            assert "mountpoint -q" in cmd

    def test_is_mounted_false(self):
        """is_mounted should return False when path is not mounted."""
        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="")

            runner = VMCommandRunner()
            assert not runner.is_mounted(Path("/var/test/merged"))

    def test_is_mounted_handles_spaces(self):
        """is_mounted quotes paths with spaces correctly.

        Contract: All path arguments are quoted to handle spaces and special
        characters. This is critical for correct shell behavior.
        """
        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = MagicMock(returncode=0, stdout="", stderr="")

            runner = VMCommandRunner()
            runner.is_mounted(Path("/var/test path/merged"))

            cmd = mock_subprocess.call_args[0][0][3]
            assert '"/var/test path/merged"' in cmd


# =============================================================================
# Module Helper Tests
# =============================================================================


class TestModuleHelpers:
    """Tests for module-level helper functions."""

    def test_is_macos(self):
        """is_macos should detect platform correctly."""
        import os

        # This is a simple sanity check - actual value depends on platform
        result = is_macos()
        assert isinstance(result, bool)
        if os.uname().sysname == "Darwin":
            assert result is True
        else:
            assert result is False

    def test_is_vm_available_not_macos(self):
        """is_vm_available should return False on non-macOS."""
        with patch("shepherd_runtime.device.container.vm_paths.is_macos", return_value=False):
            assert is_vm_available() is False

    def test_is_vm_available_success(self):
        """is_vm_available should return True when VM responds."""
        with (
            patch("shepherd_runtime.device.container.vm_paths.is_macos", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")
            assert is_vm_available() is True

    def test_is_vm_available_failure(self):
        """is_vm_available should return False when VM doesn't respond."""
        with (
            patch("shepherd_runtime.device.container.vm_paths.is_macos", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            assert is_vm_available() is False


# =============================================================================
# Integration Tests (require running Podman Machine)
# =============================================================================


@pytest.mark.integration
class TestVMPathTranslatorIntegration:
    """Integration tests that require a running Podman Machine.

    These tests are skipped if:
    - Not running on macOS
    - Podman Machine is not available
    """

    @pytest.fixture(autouse=True)
    def skip_if_unavailable(self):
        """Skip tests if VM is not available."""
        if not is_macos():
            pytest.skip("VM tests only run on macOS")
        if not is_vm_available():
            pytest.skip("Podman Machine not available")

    def test_discover_real_mounts(self):
        """Discovery should find real VirtioFS mounts."""
        translator = VMPathTranslator.discover(verify=False)

        # Should find at least /Users or similar
        assert len(translator.virtio_mounts) > 0

    def test_discover_with_verification(self):
        """Discovery with verification should pass."""
        # This will create a temp file and verify it's visible in VM
        translator = VMPathTranslator.discover(verify=True)
        assert len(translator.virtio_mounts) > 0

    def test_home_directory_translation(self):
        """Home directory should be translatable."""
        translator = VMPathTranslator.discover(verify=False)

        home = Path.home()
        vm_path = translator.host_to_vm(home)

        # Should be under one of the mounts
        assert vm_path is not None
        # Relative path should be preserved
        assert home.name in str(vm_path)

    def test_tmp_translation(self):
        """Handle /tmp → /private/tmp symlink correctly."""
        translator = VMPathTranslator.discover(verify=False)

        tmp_path = Path("/tmp/test-file")
        vm_path = translator.host_to_vm(tmp_path)

        # Should map to /private/tmp/... or /tmp/...
        assert "tmp" in str(vm_path)
        assert "test-file" in str(vm_path)

    def test_roundtrip_real_path(self):
        """Real path should roundtrip correctly."""
        translator = VMPathTranslator.discover(verify=False)

        original = Path.home() / "test-roundtrip"
        vm_path = translator.host_to_vm(original)
        recovered = translator.vm_to_host(vm_path)

        assert recovered == original


@pytest.mark.integration
class TestVMCommandRunnerIntegration:
    """Integration tests for VMCommandRunner."""

    @pytest.fixture(autouse=True)
    def skip_if_unavailable(self):
        """Skip tests if VM is not available."""
        if not is_macos():
            pytest.skip("VM tests only run on macOS")
        if not is_vm_available():
            pytest.skip("Podman Machine not available")

    def test_simple_command(self):
        """Simple command should execute and return output."""
        runner = VMCommandRunner(timeout=10.0)
        result = runner.run("echo hello")
        assert "hello" in result.stdout

    def test_batch_commands(self):
        """Batch commands should execute sequentially."""
        runner = VMCommandRunner(timeout=10.0)
        result = runner.run_batch(
            [
                "echo first",
                "echo second",
            ]
        )
        assert "first" in result.stdout
        assert "second" in result.stdout

    def test_mkdir_and_cleanup(self, tmp_path):
        """mkdir_p and rm_rf should work."""
        runner = VMCommandRunner(timeout=10.0)
        # Per-test unique dir (was a fixed /tmp path — collided under -n auto).
        test_dir = tmp_path / "shepherd-test-vm-paths"

        try:
            runner.mkdir_p(test_dir)
            assert runner.exists(test_dir)
        finally:
            runner.rm_rf(test_dir)
            assert not runner.exists(test_dir)

    def test_path_with_spaces(self, tmp_path):
        """Paths with spaces should be handled correctly."""
        runner = VMCommandRunner(timeout=10.0)
        # Keep the space in the leaf name; keep the parent unique per test.
        test_dir = tmp_path / "shepherd test dir"

        try:
            runner.mkdir_p(test_dir)
            assert runner.exists(test_dir)
        finally:
            runner.rm_rf(test_dir)
