from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

from vcs_core._hooks import HookEvent
from vcs_core._substrate_runtime import BuiltInRuntimeBinding, build_builtin_substrate_context
from vcs_core.git_substrate import GitSubstrate
from vcs_core.recording import RecordingPipeline
from vcs_core.store import Store
from vcs_core.types import EffectRecord, ScopeInfo
from vcs_core.vcscore import VcsCore

if TYPE_CHECKING:
    from pathlib import Path


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(workspace: Path) -> None:
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "vcs-core@example.com")
    _git(workspace, "config", "user.name", "Meta Git")
    (workspace / "README.md").write_text("seed\n")
    _git(workspace, "add", "README.md")
    _git(workspace, "commit", "-m", "seed")


def _init_vcscore_repo(workspace: Path) -> None:
    Store(str(workspace / ".vcscore")).create_root_commit()


def test_git_substrate_schema_exposed(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _init_vcscore_repo(tmp_path)

    substrate = GitSubstrate(build_builtin_substrate_context(VcsCore.from_config(str(tmp_path)).store))

    assert set(substrate.describe().commands) == {"branch", "checkout", "commit", "status"}
    assert not hasattr(substrate, "effects")


def test_git_substrate_authority_reports_partial(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _init_vcscore_repo(tmp_path)

    substrate = GitSubstrate(build_builtin_substrate_context(VcsCore.from_config(str(tmp_path)).store))
    report = substrate.authority()

    assert report.containment.regime == "none"
    assert report.provenance.regime == "partial"
    assert report.provenance.access_gated is False
    assert report.provenance.tier == "python"


def test_vcscore_exec_records_git_commit_effect(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _init_vcscore_repo(tmp_path)
    (tmp_path / "notes.txt").write_text("checkpoint\n")
    _git(tmp_path, "add", "notes.txt")

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate()
    try:
        task = mg.fork(mg.ground, "git-commit")
        outcome = mg.exec("git", "commit", scope=task, message="capture commit")

        assert len(outcome.oids) == 1
        mg.merge(task, mg.ground)

        effects = mg.filter_effects(effect_type="GitCommitCreated")
        assert any(effect.metadata.get("message") == "capture commit" for effect in effects)
        assert any(effect.metadata.get("substrate") == "git" for effect in effects)
    finally:
        mg.deactivate()


def test_subprocess_run_git_status_is_intercepted(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _init_vcscore_repo(tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate()
    try:
        task = mg.fork(mg.ground, "git-status")
        subprocess.run(
            ["git", "status", "--short"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )

        mg.merge(task, mg.ground)
        effects = mg.filter_effects(effect_type="GitStatusObserved")
        assert any(effect.metadata.get("substrate") == "git" for effect in effects)
        assert any("clean" in effect.metadata for effect in effects)
    finally:
        mg.deactivate()


def test_git_status_ignores_vcscore_internal_files(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _init_vcscore_repo(tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate()
    try:
        task = mg.fork(mg.ground, "git-status-clean")
        subprocess.run(
            ["git", "status", "--short"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )

        mg.merge(task, mg.ground)
        effects = mg.filter_effects(effect_type="GitStatusObserved")
        matching = [effect for effect in effects if effect.metadata.get("scope") == "git-status-clean"]

        assert len(matching) == 1
        assert matching[0].metadata["clean"] is True
        assert "summary" not in matching[0].metadata
    finally:
        mg.deactivate()


def test_non_git_subprocess_is_ignored(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _init_vcscore_repo(tmp_path)

    mg = VcsCore.from_config(str(tmp_path))
    mg.activate()
    try:
        task = mg.fork(mg.ground, "non-git")
        subprocess.run(
            [sys.executable, "-c", "print('hello')"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )

        mg.merge(task, mg.ground)
        effects = mg.filter_effects(substrate="git")
        assert all(effect.metadata.get("scope") != "non-git" for effect in effects)
    finally:
        mg.deactivate()


def test_git_switch_create_is_not_misclassified_as_checkout(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _init_vcscore_repo(tmp_path)

    substrate = GitSubstrate(build_builtin_substrate_context(VcsCore.from_config(str(tmp_path)).store))

    assert substrate._classify_invocation(["git", "switch", "-c", "feature/demo"], cwd=tmp_path) is None
    assert substrate._classify_invocation(["git", "switch", "--create", "feature/demo"], cwd=tmp_path) is None


def test_git_path_wrapper_ignores_switch_create_forms(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _init_vcscore_repo(tmp_path)

    substrate = GitSubstrate(build_builtin_substrate_context(VcsCore.from_config(str(tmp_path)).store))
    event = HookEvent(
        binding_name="git",
        hook_id="git-cli",
        kind="path_wrapper",
        phase="finish",
        scope="task",
        scope_instance_id="task-live",
        pid=123,
        proc_seq=1,
        timestamp_ns=42,
        cwd=str(tmp_path),
        argv=("git", "switch", "-c", "feature/demo"),
        exit_code=0,
        payload={},
    )

    assert substrate._translate_path_wrapper_event(event) is None


def test_execute_uses_scope_working_directory_for_isolated_scope(tmp_path: Path) -> None:
    repo_path = tmp_path / ".vcscore"
    repo_path.mkdir()
    substrate = GitSubstrate(build_builtin_substrate_context(Store(str(repo_path)), workspace=tmp_path))
    overlay_path = tmp_path / "overlay" / "scopes" / "task" / "merged"
    overlay_path.mkdir(parents=True)

    pipeline = RecordingPipeline(substrate._pipeline.store)
    substrate.bind_runtime(
        BuiltInRuntimeBinding(
            pipeline=pipeline,
            is_scope_or_ancestor_isolated=lambda _scope: True,
            overlay_base_scope_name=lambda _scope: "task",
            working_directory_for_scope=lambda _scope: overlay_path,
        )
    )

    seen_cwds: list[Path] = []

    def fake_run_git(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        seen_cwds.append(cwd)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def fake_effects_from_invocation(argv: list[str], *, cwd: Path) -> list[EffectRecord]:
        return [EffectRecord(effect_type="GitCommitCreated", metadata={"sha": "abc", "message": "capture"})]

    substrate._run_git = fake_run_git  # type: ignore[method-assign]
    substrate._effects_from_invocation = fake_effects_from_invocation  # type: ignore[method-assign]

    task = ScopeInfo(name="task", ref="refs/vcscore/scopes/task", instance_id="task", creation_oid="")
    outcome = substrate.execute("commit", task, message="capture")

    assert len(outcome.effects) == 1
    assert seen_cwds == [overlay_path]
