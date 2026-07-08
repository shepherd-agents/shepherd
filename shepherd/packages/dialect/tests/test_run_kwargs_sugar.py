"""Call-shape sugar: task arguments pass as keywords, matching the spec's ``task.run(args)``.

``ws.run(task, repo=h, topic="x", output_path="y")`` is the natural spelling; ``args={...}``
remains the explicit equivalent and the escape hatch for a task parameter whose name collides
with a run option. This covers: the natural spelling end to end through both run facades, the
``args=`` escape hatch, the duplicate-key refusal, and the shadow-name guard (a task *value*
parameter named like a run option must route through ``args=``) with its disambiguation.
"""

from __future__ import annotations

import inspect
import linecache
import textwrap
from typing import TYPE_CHECKING

import pytest
import vcs_core._vcscore_lifecycle  # noqa: F401 — ensure runtime substrate registration
from vcs_core import FilesystemSubstrate, MarkerSubstrate, Store, VcsCore, build_builtin_substrate_context
from vcs_core.runtime_substrate import TaskTraceSubstrateDriver

from shepherd_dialect.run_driver import ShepherdRunDriver
from shepherd_dialect.workspace_control import (
    ShepherdRunLedgerDriver,
    ShepherdTaskArtifactDriver,
    ShepherdTaskLedgerDriver,
    ShepherdWorkspace,
    WorkspaceControlError,
)
from shepherd_dialect.workspace_control.task_handles import WorkspaceTask
from shepherd_dialect.workspace_control.workspace import _signature_schema, _task_value_param_names

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


def _make_workspace(root: Path) -> ShepherdWorkspace:
    root.mkdir(parents=True, exist_ok=True)
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    store = Store(str(root / ".vcscore"))
    context = build_builtin_substrate_context(store=store, workspace=root, config={"backend": "clonefile"})
    mg = VcsCore(
        str(root),
        substrates=[
            MarkerSubstrate(context),
            FilesystemSubstrate(context),
            TaskTraceSubstrateDriver(),
            ShepherdTaskLedgerDriver(),
            ShepherdTaskArtifactDriver(),
            ShepherdRunLedgerDriver(),
            ShepherdRunDriver(),
        ],
        store=store,
    )
    mg.activate()
    mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
    return ShepherdWorkspace(mg, trace_store_path=root / ".vcscore" / "shepherd" / "trace.sqlite", workspace_path=root)


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[ShepherdWorkspace]:
    ws = _make_workspace(tmp_path / "ws")
    try:
        yield ws
    finally:
        ws.close()


@pytest.fixture
def define_in_main() -> Iterator[Callable[[str, str], Callable[..., object]]]:
    """Define a function as if it lived in a run-as-script ``__main__`` module (the hero shape)."""
    import sys

    main_module = sys.modules["__main__"]
    injected: list[str] = []

    def _define(src: str, name: str) -> Callable[..., object]:
        src = textwrap.dedent(src)
        filename = f"<kwargs-sim-{name}>"
        linecache.cache[filename] = (len(src), None, src.splitlines(keepends=True), filename)
        namespace = main_module.__dict__
        before = set(namespace)
        exec(compile(src, filename, "exec"), namespace)
        injected.extend(set(namespace) - before)
        return namespace[name]  # type: ignore[return-value]

    try:
        yield _define
    finally:
        for name in injected:
            main_module.__dict__.pop(name, None)


_WRITE_NOTE = '''
import shepherd as sp

@sp.task
def write_note(repo: sp.GitRepo, topic: str,
               output_path: str, output_text: str) -> None:
    """Write one note about `topic` into the repository."""
'''

_UNANNOTATED_REPO = '''
def unannotated_repo(repo, output_path: str, output_text: str) -> None:
    """Unannotated repo is an ordinary value parameter, not a workspace handle."""
'''

# A task whose *value* parameter `runtime` shadows the run option of the same name.
_SHADOWING = '''
import shepherd as sp

@sp.task
def shadowing(repo: sp.GitRepo, runtime: str,
              output_path: str, output_text: str) -> None:
    """A task whose `runtime` value parameter shadows the `runtime` run option."""
'''

_SHADOWING_REPO = '''
import shepherd as sp

@sp.task
def shadow_repo(target: sp.GitRepo, repo: str,
                output_path: str, output_text: str) -> None:
    """A task whose `repo` value parameter shadows the `repo` run option."""
'''

_SHADOWING_BINDINGS = '''
import shepherd as sp

@sp.task
def shadow_bindings(target: sp.GitRepo, bindings: str,
                    output_path: str, output_text: str) -> None:
    """A task whose `bindings` value parameter shadows the `bindings` run option."""
'''

_SHADOWING_ARGS = '''
import shepherd as sp

@sp.task
def shadow_args(target: sp.GitRepo, args: str,
                output_path: str, output_text: str) -> None:
    """A task whose `args` value parameter shadows the `args` run option."""
'''


def _register(workspace: ShepherdWorkspace, define_in_main, source: str, name: str):
    fn = define_in_main(source, name)
    workspace.tasks.register(fn)
    return fn


# --- the natural spelling, end to end ----------------------------------------


def test_keyword_task_args_run_and_settle(workspace: ShepherdWorkspace, define_in_main) -> None:
    fn = _register(workspace, define_in_main, _WRITE_NOTE, "write_note")
    run = workspace.run(
        fn,
        repo=workspace.git_repo(),
        topic="shepherd",
        output_path="NOTE.txt",
        output_text="hello from kwargs\n",
        runtime={"provider": "static"},
    )
    output = run.output()
    assert output.changeset().inspect()["changed_paths"] == ["NOTE.txt"]
    assert output.read_text("NOTE.txt") == "hello from kwargs\n"
    output.select()


def test_task_facade_keyword_args(workspace: ShepherdWorkspace, define_in_main) -> None:
    fn = _register(workspace, define_in_main, _WRITE_NOTE, "write_note")
    task: WorkspaceTask = workspace.tasks.task(fn)
    run = task.run(
        repo=workspace.git_repo(),
        topic="shepherd",
        output_path="NOTE.txt",
        output_text="via task facade\n",
        runtime={"provider": "static"},
    )
    output = run.output()
    assert "NOTE.txt" in output.changeset().inspect()["changed_paths"]
    output.release()


def test_args_mapping_escape_hatch_still_works(workspace: ShepherdWorkspace, define_in_main) -> None:
    fn = _register(workspace, define_in_main, _WRITE_NOTE, "write_note")
    run = workspace.run(
        fn,
        repo=workspace.git_repo(),
        args={"topic": "x", "output_path": "NOTE.txt", "output_text": "via args mapping\n"},
        runtime={"provider": "static"},
    )
    assert run.output().changeset().inspect()["changed_paths"] == ["NOTE.txt"]


# --- the two fail-closed guards ----------------------------------------------


def test_duplicate_key_between_args_and_kwargs_refused(workspace: ShepherdWorkspace, define_in_main) -> None:
    fn = _register(workspace, define_in_main, _WRITE_NOTE, "write_note")
    with pytest.raises(WorkspaceControlError, match="both in args= and as keyword"):
        workspace.run(
            fn,
            repo=workspace.git_repo(),
            args={"topic": "a"},
            topic="b",
            output_path="NOTE.txt",
            output_text="x\n",
            runtime={"provider": "static"},
        )


def test_shadow_name_refused_with_args_remedy(workspace: ShepherdWorkspace, define_in_main) -> None:
    fn = _register(workspace, define_in_main, _SHADOWING, "shadowing")
    with pytest.raises(WorkspaceControlError, match=r"shadow reserved run option.*args=\{"):
        workspace.run(
            fn,
            repo=workspace.git_repo(),
            runtime="oops",  # binds to the run option; the task's `runtime` arg would vanish
            output_path="S.txt",
            output_text="s\n",
        )


def test_shadow_name_disambiguated_via_args(workspace: ShepherdWorkspace, define_in_main) -> None:
    fn = _register(workspace, define_in_main, _SHADOWING, "shadowing")
    run = workspace.run(
        fn,
        repo=workspace.git_repo(),
        args={"runtime": "a-value", "output_path": "S.txt", "output_text": "s\n"},
        runtime={"provider": "static"},
    )
    assert run.output().changeset().inspect()["changed_paths"] == ["S.txt"]


def test_repo_value_param_shadow_refused_with_args_remedy(workspace: ShepherdWorkspace, define_in_main) -> None:
    fn = _register(workspace, define_in_main, _SHADOWING_REPO, "shadow_repo")
    with pytest.raises(WorkspaceControlError, match=r"shadow reserved run option.*args=\{"):
        workspace.run(
            fn,
            repo=workspace.git_repo(),
            output_path="R.txt",
            output_text="r\n",
            runtime={"provider": "static"},
        )


def test_repo_value_param_disambiguated_via_args(workspace: ShepherdWorkspace, define_in_main) -> None:
    fn = _register(workspace, define_in_main, _SHADOWING_REPO, "shadow_repo")
    run = workspace.run(
        fn,
        repo=workspace.git_repo(),
        args={"repo": "value", "output_path": "R.txt", "output_text": "r\n"},
        runtime={"provider": "static"},
    )
    assert run.output().changeset().changed_paths == ("R.txt",)


def test_bindings_value_param_shadow_refused_before_target_validation(
    workspace: ShepherdWorkspace, define_in_main
) -> None:
    fn = _register(workspace, define_in_main, _SHADOWING_BINDINGS, "shadow_bindings")
    with pytest.raises(WorkspaceControlError, match=r"shadow reserved run option.*args=\{"):
        workspace.run(
            fn,
            repo=workspace.git_repo(),
            bindings="value",  # type: ignore[arg-type]
            output_path="B.txt",
            output_text="b\n",
            runtime={"provider": "static"},
        )


def test_args_value_param_shadow_refused_with_args_remedy(workspace: ShepherdWorkspace, define_in_main) -> None:
    fn = _register(workspace, define_in_main, _SHADOWING_ARGS, "shadow_args")
    with pytest.raises(WorkspaceControlError, match=r"shadow reserved run option.*args=\{"):
        workspace.run(
            fn,
            repo=workspace.git_repo(),
            args={"output_path": "A.txt", "output_text": "a\n"},
            runtime={"provider": "static"},
        )


def test_args_must_be_mapping(workspace: ShepherdWorkspace, define_in_main) -> None:
    fn = _register(workspace, define_in_main, _WRITE_NOTE, "write_note")
    with pytest.raises(WorkspaceControlError, match="args= must be a mapping"):
        workspace.run(
            fn,
            repo=workspace.git_repo(),
            args="topic=bad",  # type: ignore[arg-type]
            runtime={"provider": "static"},
        )


def test_bindings_must_be_mapping(workspace: ShepherdWorkspace, define_in_main) -> None:
    fn = _register(workspace, define_in_main, _WRITE_NOTE, "write_note")
    with pytest.raises(WorkspaceControlError, match="bindings= must be a non-empty mapping"):
        workspace.run(
            fn,
            bindings="not-a-mapping",  # type: ignore[arg-type]
            topic="shepherd",
            output_path="NOTE.txt",
            output_text="x\n",
            runtime={"provider": "static"},
        )


def test_unpassed_shadow_option_does_not_false_fire(workspace: ShepherdWorkspace, define_in_main) -> None:
    # `runtime={"provider": ...}` is not one of the shadowed values here; the task declares a
    # `runtime` value param but the caller supplies it via args=, and does not pass may/placement.
    fn = _register(workspace, define_in_main, _SHADOWING, "shadowing")
    run = workspace.run(
        fn,
        repo=workspace.git_repo(),
        args={"runtime": "a-value", "output_path": "S.txt", "output_text": "s\n"},
        runtime={"provider": "static"},
    )
    assert run.output().state is not None


# --- structural guards -------------------------------------------------------


def test_handle_param_is_excluded_from_value_params(define_in_main) -> None:
    fn = define_in_main(_WRITE_NOTE, "write_note")
    names = _task_value_param_names(_signature_schema(inspect.unwrap(fn)))
    assert names == {"topic", "output_path", "output_text"}
    assert "repo" not in names  # the May[GitRepo, ...] handle param, fed by repo=


def test_unannotated_repo_param_is_a_value_param(define_in_main) -> None:
    fn = define_in_main(_UNANNOTATED_REPO, "unannotated_repo")
    names = _task_value_param_names(_signature_schema(inspect.unwrap(fn)))
    assert names == {"repo", "output_path", "output_text"}


def test_unannotated_repo_param_refused_with_annotation_remedy(workspace: ShepherdWorkspace, define_in_main) -> None:
    fn = define_in_main(_UNANNOTATED_REPO, "unannotated_repo")
    with pytest.raises(WorkspaceControlError, match="annotate it as GitRepo"):
        workspace.run(
            fn,
            repo=workspace.git_repo(),
            output_path="R.txt",
            output_text="r\n",
            runtime={"provider": "static"},
        )


def test_run_signature_is_keyword_task_args_with_positional_only_ref() -> None:
    sig = inspect.signature(ShepherdWorkspace.run)
    assert sig.parameters["task_ref"].kind is inspect.Parameter.POSITIONAL_ONLY
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    task_sig = inspect.signature(WorkspaceTask.run)
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in task_sig.parameters.values())
