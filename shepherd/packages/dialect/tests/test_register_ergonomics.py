"""Registration ergonomics: `register(fn)` / `run(fn)` acceptance matrix.

The hero spelling is `@sp.task` + `ws.tasks.register(fn)` + `ws.run(fn, ...)` — no
source-text blob. This covers the six-row matrix from the hero-ergonomics plan:
module-defined plain/decorated register and resolve; `__main__` bodyless captures at
definition scope; `__main__` bodied and bare-`__main__`-annotation refuse; locals
refuse; and callable lookup follows the default identity convention.

The one end-to-end jailed run (decorated bodied module task) proves the executing
artifact is the plain body, not the wrapper (which would fail WorkspaceNotConfigured).
"""

from __future__ import annotations

import linecache
import textwrap
from typing import TYPE_CHECKING

import pytest
from vcs_core import FilesystemSubstrate, MarkerSubstrate, Store, VcsCore, build_builtin_substrate_context
from vcs_core.runtime_substrate import TaskTraceSubstrateDriver

from shepherd_dialect.run_driver import ShepherdRunDriver
from shepherd_dialect.workspace_control import (
    ShepherdRunLedgerDriver,
    ShepherdTaskArtifactDriver,
    ShepherdTaskLedgerDriver,
    ShepherdWorkspace,
    TaskNotFoundError,
)
from shepherd_dialect.workspace_control.identities import coerce_task_ref, task_id_for_callable

pytestmark = pytest.mark.slow  # full-lifecycle suite: runs in the lifecycle-tests CI job

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

# Module-level so collection survives the dialect-only env (the container CI
# lane syncs just this package); test_register_ceiling_derivation imports this
# module, so the guard covers it too.
sp = pytest.importorskip("shepherd", reason="requires the shepherd meta package")


def _make_workspace(root: Path) -> ShepherdWorkspace:
    root.mkdir(parents=True, exist_ok=True)
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    store = Store(str(root / ".vcscore"))
    context = build_builtin_substrate_context(store=store, workspace=root)
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
    # Seed a selected workspace world so `git_repo()` / `run(...)` can hydrate a basis.
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
    """Define a function as if it lived in a run-as-script ``__main__`` module.

    Faithful to `python hero.py`: the function's ``__module__`` is ``__main__`` AND it
    is bound on ``sys.modules["__main__"]`` (so registration's import round-trip finds
    it), with its source in ``linecache`` for ``inspect.getsource``. Injected names are
    removed at teardown.
    """
    import sys

    main_module = sys.modules["__main__"]
    injected: list[str] = []

    def _define(src: str, name: str) -> Callable[..., object]:
        src = textwrap.dedent(src)
        filename = f"<main-sim-{name}>"
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


# --- a real importable module for the module-defined rows -----------------------

_MODULE_SOURCE = '''
import shepherd as sp

def plain_note(repo: sp.May[sp.GitRepo, sp.ReadWrite], text: str):
    """Plain module task."""

@sp.task
def decorated_note(repo: sp.May[sp.GitRepo, sp.ReadWrite], text: str) -> None:
    """Decorated bodyless module task."""

@sp.task
def decorated_bodied(repo: sp.May[sp.GitRepo, sp.ReadWrite], path: str) -> dict:
    """Decorated bodied module task."""
    repo.write(path, b"bodied artifact\\n")
    return {"wrote": path}
'''


@pytest.fixture
def hero_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    module_dir = tmp_path / "mod"
    module_dir.mkdir()
    (module_dir / "hero_ergo_tasks.py").write_text(_MODULE_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(module_dir))
    import importlib
    import sys

    sys.modules.pop("hero_ergo_tasks", None)
    importlib.import_module("hero_ergo_tasks")
    return "hero_ergo_tasks"


# =============================================================================
# Row 1/2: module-defined plain and decorated register
# =============================================================================


def test_module_plain_function_registers(workspace: ShepherdWorkspace, hero_module: str) -> None:
    import hero_ergo_tasks

    version = workspace.tasks.register(hero_ergo_tasks.plain_note, may_default="ReadWrite")
    assert version.task_id == "hero_ergo_tasks.plain_note"


def test_module_decorated_function_registers(workspace: ShepherdWorkspace, hero_module: str) -> None:
    import hero_ergo_tasks

    version = workspace.tasks.register(hero_ergo_tasks.decorated_note, may_default="ReadWrite")
    assert version.task_id == "hero_ergo_tasks.decorated_note"


def test_decorated_bodied_module_task_runs_as_plain_body_under_jail(
    workspace: ShepherdWorkspace, hero_module: str
) -> None:
    # The load-bearing execution test: a @sp.task-decorated *bodied* task must run its
    # own body in the confined runner. Before the unwrap fix the wrapper's __call__ ran
    # and raised WorkspaceNotConfigured.
    import hero_ergo_tasks

    workspace.tasks.register(hero_ergo_tasks.decorated_bodied, may_default="ReadWrite")
    run = workspace.run(hero_ergo_tasks.decorated_bodied, repo=workspace.git_repo(), args={"path": "marker.txt"})
    output = run.output()
    assert output.changeset().inspect()["changed_paths"] == ["marker.txt"]


# =============================================================================
# Row 3: __main__ bodyless -> definition-scoped generated capture
# =============================================================================


def test_main_bodyless_task_captures_at_definition_scope(workspace: ShepherdWorkspace, define_in_main) -> None:
    fn = define_in_main(
        '''
        import shepherd as sp

        @sp.task
        def write_note(repo: sp.May[sp.GitRepo, sp.ReadWrite], topic: str) -> None:
            """Bodyless task defined in a script."""
        ''',
        "write_note",
    )
    version = workspace.tasks.register(fn, may_default="ReadWrite")
    # The artifact is a synthetic module derived from the qualname, not "__main__".
    assert version.task_id == "shepherd_generated_write_note.write_note"


def test_main_bodyless_task_runs_end_to_end(workspace: ShepherdWorkspace, define_in_main) -> None:
    fn = define_in_main(
        '''
        import shepherd as sp

        @sp.task
        def write_note(repo: sp.May[sp.GitRepo, sp.ReadWrite], output_path: str, output_text: str) -> None:
            """Bodyless task defined in a script."""
        ''',
        "write_note",
    )
    workspace.tasks.register(fn, may_default="ReadWrite")
    run = workspace.run(
        fn,
        repo=workspace.git_repo(),
        args={"output_path": "NOTE.txt", "output_text": "hello\n"},
        runtime={"provider": "static"},
    )
    assert run.output().changeset().inspect()["changed_paths"] == ["NOTE.txt"]


# =============================================================================
# Row 4/5: __main__ bodied and bare-annotation refuse teachably
# =============================================================================


def test_main_bodied_task_refuses_with_module_remedy(workspace: ShepherdWorkspace, define_in_main) -> None:
    fn = define_in_main(
        '''
        import shepherd as sp

        @sp.task
        def bodied(repo: sp.May[sp.GitRepo, sp.ReadWrite], path: str) -> dict:
            """A real body in a script."""
            repo.write(path, b"x")
            return {}
        ''',
        "bodied",
    )
    with pytest.raises(Exception, match="importable module"):
        workspace.tasks.register(fn, may_default="ReadWrite")


def test_main_task_with_bare_local_annotation_refuses(workspace: ShepherdWorkspace, define_in_main) -> None:
    fn = define_in_main(
        '''
        import shepherd as sp

        class LocalThing:
            pass

        @sp.task
        def uses_local(repo: sp.May[sp.GitRepo, sp.ReadWrite], thing: LocalThing) -> None:
            """Bodyless, but a bare annotation only the script defines."""
        ''',
        "uses_local",
    )
    with pytest.raises(Exception, match="only its script defines"):
        workspace.tasks.register(fn, may_default="ReadWrite")


def test_main_task_with_string_forwardref_local_annotation_refuses(
    workspace: ShepherdWorkspace, define_in_main
) -> None:
    # Version-independent guard for the annotation fence. A *string* forward-ref is
    # never evaluated by the `def` statement on any Python, so an exec-only fence
    # misses it everywhere (and, since 3.14 defers all annotation evaluation, a bare
    # annotation slips through there too). The fence must force evaluation itself.
    fn = define_in_main(
        '''
        import shepherd as sp

        class LocalThing:
            pass

        @sp.task
        def uses_forwardref(repo: sp.May[sp.GitRepo, sp.ReadWrite], thing: "LocalThing") -> None:
            """Bodyless, with a string forward-ref only the script defines."""
        ''',
        "uses_forwardref",
    )
    with pytest.raises(Exception, match="only its script defines"):
        workspace.tasks.register(fn, may_default="ReadWrite")


# =============================================================================
# Row 6: locals refuse
# =============================================================================


def test_local_function_refuses(workspace: ShepherdWorkspace) -> None:
    @sp.task
    def local_task(repo: sp.May[sp.GitRepo, sp.ReadWrite]) -> None:
        """Defined inside a test function."""

    with pytest.raises(Exception, match=r"module scope|stable"):
        workspace.tasks.register(local_task, may_default="ReadWrite")


# =============================================================================
# run(fn) / task(fn) resolution and the unregistered refusal
# =============================================================================


def test_task_id_for_callable_matches_registration(hero_module: str) -> None:
    import hero_ergo_tasks

    assert task_id_for_callable(hero_ergo_tasks.decorated_note) == "hero_ergo_tasks.decorated_note"
    assert coerce_task_ref(hero_ergo_tasks.plain_note) == "hero_ergo_tasks.plain_note"


def test_run_unregistered_callable_refuses_with_register_hint(workspace: ShepherdWorkspace, define_in_main) -> None:
    fn = define_in_main(
        '''
        import shepherd as sp

        @sp.task
        def never(repo: sp.May[sp.GitRepo, sp.ReadWrite]) -> None:
            """Never registered."""
        ''',
        "never",
    )
    with pytest.raises(TaskNotFoundError, match="register it first"):
        workspace.run(fn, repo=workspace.git_repo(), args={}, runtime={"provider": "static"})


def test_run_callable_registered_under_custom_task_id_hints_the_explicit_id(
    workspace: ShepherdWorkspace, hero_module: str
) -> None:
    """`register(fn, task_id="custom")` then `run(fn)` misses — the derived id is not the
    custom one. The not-found message names the explicit-task_id case rather than leaving a
    bare 'no active task matches …'."""
    import hero_ergo_tasks

    workspace.tasks.register(hero_ergo_tasks.decorated_bodied, task_id="custom.artifact")
    with pytest.raises(TaskNotFoundError, match="resolved from a task callable"):
        workspace.run(
            hero_ergo_tasks.decorated_bodied,
            repo=workspace.git_repo(),
            args={"path": "x.txt"},
            runtime={"provider": "static"},
        )
