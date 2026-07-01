"""Integration test: runtime-owned fuse-overlayfs helper slice in a real container.

Validates the full FuseOverlayManager lifecycle inside a Podman container with
/dev/fuse and fuse-overlayfs. The test injects a Python script that imports the
runtime-owned production code and exercises it directly.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
import time
import uuid

import pytest


def is_podman_available() -> bool:
    """Check podman CLI and VM SSH connectivity."""
    try:
        result = subprocess.run(
            ["podman", "version"],
            check=False,
            capture_output=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False
        vm_result = subprocess.run(
            ["podman", "machine", "ssh", "echo ok"],
            check=False,
            capture_output=True,
            timeout=10,
        )
        return vm_result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


requires_podman = pytest.mark.usefixtures("_requires_podman_machine")


@pytest.fixture
def _requires_podman_machine() -> None:
    if not is_podman_available():
        pytest.skip("Podman Machine not available")

pytestmark = [
    pytest.mark.container,
    pytest.mark.integration,
    pytest.mark.e2e,
    requires_podman,
]


def vm(cmd: str, *, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run a shell command inside the Podman Machine VM via SSH."""
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            result = subprocess.run(
                ["podman", "machine", "ssh", cmd],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            if attempt == attempts:
                raise
            time.sleep(1.0)
            continue

        if "kex_protocol_error" in result.stderr and attempt < attempts:
            time.sleep(1.0)
            continue
        break

    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            f"podman machine ssh: {cmd}",
            result.stdout,
            result.stderr,
        )
    return result


def container_exec(
    name: str,
    cmd: str,
    *,
    check: bool = True,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    """Run a command inside a container via podman exec."""
    result = subprocess.run(
        ["podman", "exec", name, "sh", "-c", cmd],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            f"podman exec {name}: {cmd}",
            result.stdout,
            result.stderr,
        )
    return result


def remove_container(name: str) -> None:
    """Force-remove a container by name. Ignores errors."""
    subprocess.run(
        ["podman", "rm", "-f", name],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="session")
def integration_root() -> str:
    """Create a root directory for this integration session in the VM."""
    root = f"/var/shepherd/integ-fuse-{uuid.uuid4().hex[:8]}"
    vm(f'sudo mkdir -p "{root}" && sudo chmod 777 "{root}"')
    yield root
    vm(
        f"sudo find {root} -type d -exec mountpoint -q {{}} \\; -exec sudo umount {{}} \\; 2>/dev/null",
        check=False,
    )
    vm(f'sudo rm -rf "{root}"', check=False)


@pytest.fixture(scope="session")
def package_sources() -> dict[str, str]:
    """Resolve package source directories for container mounts."""
    from shepherd_runtime.device.container.podman import _discover_dev_package_sources

    sources = _discover_dev_package_sources()
    required = {"shepherd-core", "shepherd-runtime"}
    missing = required - sources.keys()
    assert not missing, f"Missing package sources for: {sorted(missing)}"
    return {name: str(sources[name]) for name in sorted(required)}


@pytest.fixture
def container_env(integration_root: str, package_sources: dict[str, str]):
    """Create a container with fuse-overlayfs, workspace, and runtime packages mounted."""
    test_id = f"fo-integ-{uuid.uuid4().hex[:8]}"
    container_name = f"integ-{test_id}"
    vm_workspace = f"{integration_root}/{test_id}/workspace"
    vm_task_dir = f"{integration_root}/{test_id}/task"

    vm(
        f'mkdir -p "{vm_workspace}/src" "{vm_task_dir}/overlays" '
        f'&& echo "original-main" > "{vm_workspace}/src/main.py" '
        f'&& echo "original-utils" > "{vm_workspace}/src/utils.py" '
        f'&& echo "delete-me" > "{vm_workspace}/to_delete.txt"'
    )

    cmd = [
        "podman",
        "create",
        "--name",
        container_name,
        "--security-opt",
        "label=disable",
        "--device",
        "/dev/fuse",
        "--cap-add",
        "SYS_ADMIN",
        "-v",
        f"{vm_workspace}:/workspace-ro:ro",
        "-v",
        f"{vm_task_dir}:/task",
    ]

    for pkg_name, pkg_src in package_sources.items():
        cmd.extend(["-v", f"{pkg_src}:/packages/{pkg_name}/src:ro"])
    cmd.extend(["-e", "PYTHONPATH=/packages/shepherd-core/src:/packages/shepherd-runtime/src"])

    cmd.extend(["python:3.12-slim", "sleep", "infinity"])

    subprocess.run(cmd, check=True, capture_output=True, text=True)
    subprocess.run(["podman", "start", container_name], check=True, capture_output=True, text=True)

    container_exec(
        container_name,
        "apt-get update -qq && apt-get install -y -qq fuse-overlayfs fuse3 >/dev/null 2>&1",
        timeout=120,
    )

    yield {
        "name": container_name,
        "vm_workspace": vm_workspace,
        "vm_task_dir": vm_task_dir,
    }

    remove_container(container_name)
    vm(f'sudo rm -rf "{integration_root}/{test_id}"', check=False)


INTEGRATION_SCRIPT = textwrap.dedent(
    r"""
import importlib.util
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/packages/shepherd-core/src")
sys.path.insert(0, "/packages/shepherd-runtime/src")

module_path = Path("/packages/shepherd-runtime/src/shepherd_runtime/device/container/fuse_overlay.py")
spec = importlib.util.spec_from_file_location("runtime_fuse_overlay", module_path)
assert spec is not None and spec.loader is not None, f"Unable to load {module_path}"
runtime_fuse_overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime_fuse_overlay)

FuseOverlayManager = runtime_fuse_overlay.FuseOverlayManager
fuse_overlayfs_available = runtime_fuse_overlay.fuse_overlayfs_available

results = {"steps": [], "errors": []}


def log(step, msg):
    results["steps"].append({"step": step, "message": msg})


try:
    assert fuse_overlayfs_available(), "fuse-overlayfs not available"
    log("0_available", "fuse_overlayfs_available() = True")

    mgr = FuseOverlayManager()
    mgr.setup()
    log("1_setup", "FuseOverlayManager.setup() succeeded")

    assert os.path.isfile("/workspace/src/main.py"), "/workspace/src/main.py missing after setup"
    with open("/workspace/src/main.py") as f:
        assert f.read().strip() == "original-main", "main.py content mismatch"
    log("1_verify", "Baseline files visible at /workspace after setup")

    mgr.push_layer("toolu_create_001")
    with open("/workspace/new_file.py", "w") as f:
        f.write("print('hello from tool 1')\n")
    effects_1 = mgr.pop_and_merge("toolu_create_001")
    log("2_tool1", f"Tool 1 effects: {json.dumps(effects_1)}")

    assert os.path.isfile("/workspace/new_file.py"), "new_file.py missing after tool 1"
    assert os.path.isfile("/task/overlays/accumulated/new_file.py"), "new_file.py missing from accumulated"

    mgr.push_layer("toolu_modify_002")
    with open("/workspace/src/main.py", "w") as f:
        f.write("# Modified by tool 2\nprint('updated')\n")
    effects_2 = mgr.pop_and_merge("toolu_modify_002")
    log("3_tool2", f"Tool 2 effects: {json.dumps(effects_2)}")

    with open("/workspace/src/main.py") as f:
        content = f.read()
    assert "Modified by tool 2" in content, f"main.py not modified: {content!r}"

    mgr.push_layer("toolu_delete_003")
    os.unlink("/workspace/to_delete.txt")
    effects_3 = mgr.pop_and_merge("toolu_delete_003")
    log("4_tool3", f"Tool 3 effects: {json.dumps(effects_3)}")

    assert not os.path.exists("/workspace/to_delete.txt"), "to_delete.txt still exists after deletion"

    mgr.push_layer("toolu_crossmod_004")
    with open("/workspace/new_file.py", "w") as f:
        f.write("# Modified by tool 4\nprint('updated by tool 4')\n")
    effects_4 = mgr.pop_and_merge("toolu_crossmod_004")
    log("5_tool4", f"Tool 4 effects: {json.dumps(effects_4)}")

    with open("/workspace/src/utils.py") as f:
        utils_content = f.read().strip()
    assert utils_content == "original-utils", f"utils.py changed unexpectedly: {utils_content!r}"
    log("6_readonly", "Read-only access works without layer management")

    mgr.teardown()
    log("7_teardown", "FuseOverlayManager.teardown() succeeded")

    results["effects_by_tool"] = {
        "toolu_create_001": effects_1,
        "toolu_modify_002": effects_2,
        "toolu_delete_003": effects_3,
        "toolu_crossmod_004": effects_4,
    }

    accumulated_files = []
    for root, dirs, files in os.walk("/task/overlays/accumulated"):
        rel = os.path.relpath(root, "/task/overlays/accumulated")
        for name in files:
            accumulated_files.append(os.path.join(rel, name) if rel != "." else name)
    results["accumulated_files"] = sorted(accumulated_files)
    results["success"] = True
except Exception as e:
    import traceback

    results["success"] = False
    results["errors"].append(f"{type(e).__name__}: {e}\n{traceback.format_exc()}")

with open("/task/integration_results.json", "w") as f:
    json.dump(results, f, indent=2, default=str)

sys.exit(0 if results.get("success") else 1)
"""
)


class TestFuseOverlayIntegration:
    """End-to-end test of FuseOverlayManager inside a real Podman container."""

    def test_full_tool_call_lifecycle(self, container_env) -> None:
        ctr = container_env["name"]

        container_exec(ctr, f"cat > /task/run_test.py << 'SCRIPT_EOF'\n{INTEGRATION_SCRIPT}\nSCRIPT_EOF")

        result = container_exec(ctr, "python3 /task/run_test.py", check=False, timeout=60)
        results_raw = container_exec(ctr, "cat /task/integration_results.json", check=False)

        if results_raw.returncode != 0 or not results_raw.stdout.strip():
            pytest.fail(
                f"Integration script failed to produce results.\n"
                f"Exit code: {result.returncode}\n"
                f"Stdout:\n{result.stdout}\n"
                f"Stderr:\n{result.stderr}"
            )

        results = json.loads(results_raw.stdout)
        if not results.get("success"):
            errors = "\n".join(results.get("errors", ["unknown error"]))
            pytest.fail(f"Integration script failed:\n{errors}")

        effects = results["effects_by_tool"]

        create_effect = next((e for e in effects["toolu_create_001"] if e["effect_type"] == "file_create"), None)
        assert create_effect is not None
        assert create_effect["path"] == "new_file.py"
        assert create_effect["caused_by"] == "toolu_create_001"
        assert "hello from tool 1" in create_effect["content"]

        patch_effect = next((e for e in effects["toolu_modify_002"] if e["effect_type"] == "file_patch"), None)
        assert patch_effect is not None
        assert patch_effect["path"] == "src/main.py"
        assert patch_effect["caused_by"] == "toolu_modify_002"
        assert "original-main" in patch_effect["old_content"]
        assert "Modified by tool 2" in patch_effect["new_content"]

        delete_effect = next((e for e in effects["toolu_delete_003"] if e["effect_type"] == "file_delete"), None)
        assert delete_effect is not None
        assert delete_effect["path"] == "to_delete.txt"
        assert delete_effect["caused_by"] == "toolu_delete_003"
        assert "delete-me" in delete_effect.get("had_content", "")

        crosspatch = next((e for e in effects["toolu_crossmod_004"] if e["effect_type"] == "file_patch"), None)
        assert crosspatch is not None
        assert crosspatch["path"] == "new_file.py"
        assert crosspatch["caused_by"] == "toolu_crossmod_004"
        assert "hello from tool 1" in crosspatch["old_content"]
        assert "Modified by tool 4" in crosspatch["new_content"]

        accumulated_files = results["accumulated_files"]
        assert "new_file.py" in accumulated_files
        assert "src/main.py" in accumulated_files

        vm_task_dir = container_env["vm_task_dir"]
        host_accumulated = vm(f'ls "{vm_task_dir}/overlays/accumulated/" 2>/dev/null', check=False)
        assert host_accumulated.returncode == 0
        assert "new_file.py" in host_accumulated.stdout
