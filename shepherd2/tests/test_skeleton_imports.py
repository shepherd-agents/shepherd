from __future__ import annotations

import importlib
import subprocess
import sys

import pytest
from shepherd2.vnext import skeleton


def test_skeleton_import_does_not_load_vcs_core() -> None:
    code = (
        "import sys\n"
        "import shepherd2\n"
        "import shepherd2.vnext\n"
        "import shepherd2.vnext.skeleton\n"
        "print('\\n'.join(sorted(m for m in sys.modules if m == 'vcs_core' or m.startswith('vcs_core.'))))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    assert proc.stdout == "\n"


def test_skeleton_is_not_barrel_exported() -> None:
    code = (
        "import shepherd2\n"
        "import shepherd2.vnext as vnext\n"
        "print('skeleton' in shepherd2.__dict__)\n"
        "print('skeleton' in vnext.__dict__)\n"
        "print('Session' in vnext.__dict__)\n"
        "print('GitRepoHandle' in vnext.__dict__)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    assert proc.stdout.splitlines() == ["False", "False", "False", "False"]


def test_skeleton_flag_off_fails_before_custody(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(skeleton.SKELETON_ENV, raising=False)
    monkeypatch.setenv(skeleton.SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv(skeleton.NESTED_OPERATIONS_ENV, "1")

    with pytest.raises(skeleton.SkeletonUnavailableError, match=skeleton.SKELETON_ENV):
        skeleton.Session(object()).workspace_repo(object())


def test_skeleton_requires_vcs_core_seal_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(skeleton.SKELETON_ENV, "1")
    monkeypatch.delenv(skeleton.SEAL_AND_SELECT_ENV, raising=False)
    monkeypatch.setenv(skeleton.NESTED_OPERATIONS_ENV, "1")

    with pytest.raises(skeleton.SkeletonUnavailableError, match=skeleton.SEAL_AND_SELECT_ENV):
        skeleton.Session(object()).workspace_repo(object())


def test_skeleton_requires_nested_operations_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(skeleton.SKELETON_ENV, "1")
    monkeypatch.setenv(skeleton.SEAL_AND_SELECT_ENV, "1")
    monkeypatch.delenv(skeleton.NESTED_OPERATIONS_ENV, raising=False)

    with pytest.raises(skeleton.SkeletonUnavailableError, match=skeleton.NESTED_OPERATIONS_ENV):
        skeleton.Session(object()).workspace_repo(object())


def test_skeleton_missing_vcs_core_fails_at_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import_module = importlib.import_module

    def fake_import_module(name: str, package: str | None = None) -> object:
        if name == "vcs_core":
            raise ModuleNotFoundError("No module named 'vcs_core'")
        return real_import_module(name, package)

    monkeypatch.setenv(skeleton.SKELETON_ENV, "1")
    monkeypatch.setenv(skeleton.SEAL_AND_SELECT_ENV, "1")
    monkeypatch.setenv(skeleton.NESTED_OPERATIONS_ENV, "1")
    monkeypatch.setattr("shepherd2.vnext.skeleton.importlib.import_module", fake_import_module)

    with pytest.raises(skeleton.SkeletonUnavailableError, match="vcs_core is not importable"):
        skeleton.Session(object()).workspace_repo(object())
