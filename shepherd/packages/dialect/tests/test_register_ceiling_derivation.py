"""Task-ceiling derivation + registration provenance (P1/P2 of the deviation close-out).

The task-level `may` ceiling is derived from the signature's grants at one seam — the
grant-lattice join over the compiled per-parameter descriptors, uniform across every
registration spelling — and every registration records where its ceiling came from
(`ceiling_provenance`) plus, for callable sources, where the artifact came from
(`derived_from_callable`). An explicit `may_default=` still wins, loudly and recorded.
"""

from __future__ import annotations

import linecache
import textwrap
from typing import TYPE_CHECKING

import pytest

# Reuse the acceptance-matrix fixtures (workspace, define_in_main).
from test_register_ergonomics import _make_workspace

from shepherd_dialect.workspace_control.workspace import RunStartError, TaskRegistrationError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from shepherd_dialect.workspace_control import ShepherdWorkspace


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[ShepherdWorkspace]:
    ws = _make_workspace(tmp_path / "ws")
    try:
        yield ws
    finally:
        ws.close()


@pytest.fixture
def define_in_main() -> Iterator[Callable[[str, str], Callable[..., object]]]:
    """Define a function as if it lived in a run-as-script ``__main__`` module."""
    import sys

    main_module = sys.modules["__main__"]
    injected: list[str] = []

    def _define(src: str, name: str) -> Callable[..., object]:
        src = textwrap.dedent(src)
        filename = f"<main-sim-ceiling-{name}>"
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


_GRANT_MODULE_SOURCE = '''
from __future__ import annotations

import shepherd as sp

def ro_only(docs: sp.May[sp.GitRepo, sp.ReadOnly], note: str) -> None:
    """All grants read-only."""

def bare_rw(repo: sp.GitRepo, note: str) -> None:
    """Bare GitRepo is the writable workspace-handle spelling."""

def rw_only(repo: sp.May[sp.GitRepo, sp.ReadWrite], note: str) -> None:
    """One read-write grant."""

def mixed(docs: sp.May[sp.GitRepo, sp.ReadOnly], backend: sp.May[sp.GitRepo, sp.ReadWrite]) -> None:
    """Read-only + read-write: the join is read-write."""

def no_grant(note: str, count: int) -> None:
    """No authority grants at all."""
'''


@pytest.fixture
def grant_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    module_dir = tmp_path / "gmod"
    module_dir.mkdir()
    (module_dir / "ceiling_grant_tasks.py").write_text(_GRANT_MODULE_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(module_dir))
    import importlib
    import sys

    sys.modules.pop("ceiling_grant_tasks", None)
    importlib.import_module("ceiling_grant_tasks")
    return "ceiling_grant_tasks"


def _provenance(version) -> str | None:
    return version.metadata.get("shepherd.ceiling_provenance")


# --- P1: the grant-lattice join --------------------------------------------------


def test_all_readonly_signature_derives_readonly_ceiling(workspace, grant_module) -> None:
    import ceiling_grant_tasks

    version = workspace.tasks.register(ceiling_grant_tasks.ro_only)
    assert version.may_default == "ReadOnly"
    assert _provenance(version) == "derived"


def test_any_readwrite_grant_joins_to_readwrite(workspace, grant_module) -> None:
    import ceiling_grant_tasks

    bare = workspace.tasks.register(ceiling_grant_tasks.bare_rw, task_id="ceiling.bare_rw")
    assert bare.may_default == "ReadWrite"
    assert _provenance(bare) == "derived"

    rw = workspace.tasks.register(ceiling_grant_tasks.rw_only)
    assert rw.may_default == "ReadWrite"
    # The join: read-only + read-write ⇒ read-write (profile join today; Match union when
    # the algebra lands — P-030 §4's union of parameter grants, the same rule at a wider lattice).
    mixed = workspace.tasks.register(ceiling_grant_tasks.mixed, task_id="ceiling.mixed")
    assert mixed.may_default == "ReadWrite"
    assert _provenance(mixed) == "derived"


def test_no_grant_signature_keeps_the_workspace_default(workspace, grant_module) -> None:
    import ceiling_grant_tasks

    version = workspace.tasks.register(ceiling_grant_tasks.no_grant)
    assert version.may_default == "ReadWrite"  # DEFAULT_WORKSPACE_MAY_PROFILE — unchanged
    assert _provenance(version) == "default"


def test_explicit_kwarg_beats_derivation(workspace, grant_module) -> None:
    import ceiling_grant_tasks

    # An all-read-only signature would derive "ReadOnly"; the loud override wins.
    version = workspace.tasks.register(
        ceiling_grant_tasks.ro_only, may_default="Permissive", task_id="ceiling.ro_override"
    )
    assert version.may_default == "Permissive"
    assert _provenance(version) == "explicit"


def test_ceiling_derivation_is_uniform_across_registration_spellings(workspace, grant_module) -> None:
    """The same signature yields the same ceiling + provenance however it was registered.

    This is the assertion a per-path fork would have made impossible: `register(fn)` and
    `register_source` read authority through the one compiler at the one schema seam.
    """
    import ceiling_grant_tasks

    via_callable = workspace.tasks.register(ceiling_grant_tasks.ro_only, task_id="uniform.callable")
    via_source = workspace.tasks.register_source(
        task_id="uniform.source",
        module="uniform_source_tasks",
        source_text=textwrap.dedent(
            """
            import shepherd as sp

            def ro_only(docs: sp.May[sp.GitRepo, sp.ReadOnly], note: str) -> None:
                \"\"\"All grants read-only.\"\"\"
            """
        ),
        entrypoint="ro_only",
    )
    assert via_callable.may_default == "ReadOnly"
    assert via_source.may_default == "ReadOnly"
    assert _provenance(via_callable) == "derived"
    assert _provenance(via_source) == "derived"


def test_bare_gitrepo_derivation_is_uniform_across_registration_spellings(workspace, grant_module) -> None:
    """The clean writer spelling derives ReadWrite through runtime and AST paths."""
    import ceiling_grant_tasks

    via_callable = workspace.tasks.register(ceiling_grant_tasks.bare_rw, task_id="bare.callable")
    via_source = workspace.tasks.register_source(
        task_id="bare.source",
        module="bare_source_tasks",
        source_text=textwrap.dedent(
            """
            import shepherd as sp

            def bare_rw(repo: sp.GitRepo, note: str) -> None:
                \"\"\"Bare GitRepo is a writable workspace handle.\"\"\"
            """
        ),
        entrypoint="bare_rw",
    )
    assert via_callable.may_default == "ReadWrite"
    assert via_source.may_default == "ReadWrite"
    assert _provenance(via_callable) == "derived"
    assert _provenance(via_source) == "derived"


def test_derived_ceiling_bites_at_admission_before_the_body(workspace, grant_module) -> None:
    """The derived value is a task-level *ceiling* refused at admission, not the jail.

    An all-read-only task attempting a write under advisory placement is refused before its
    body runs — proving the ceiling layer bites, distinguishably from a syscall-jail refusal
    (which would happen inside a jailed run, after admission).
    """
    import ceiling_grant_tasks

    # Register the writer under a read-only ceiling (kwarg override, so the ceiling is
    # narrower than the body wants), then a run requesting ReadWrite is refused at
    # admission — before a run record or the body — with a widening error, which is a
    # distinct layer from a syscall-jail refusal inside a jailed run.
    workspace.tasks.register(ceiling_grant_tasks.rw_only, may_default="ReadOnly", task_id="ceiling.writer_ro")
    with pytest.raises(RunStartError, match="exceeds task may_default='ReadOnly'"):
        workspace.run("ceiling.writer_ro", repo=workspace.git_repo(), args={"note": "x"}, may="ReadWrite")


# --- P2: provenance + derived-from-callable metadata -----------------------------


def test_callable_registration_records_derived_from_callable(workspace, grant_module) -> None:
    import ceiling_grant_tasks

    version = workspace.tasks.register(ceiling_grant_tasks.rw_only)
    origin = version.metadata.get("shepherd.derived_from_callable")
    assert origin is not None
    assert origin["module"] == "ceiling_grant_tasks"
    assert origin["qualname"] == "rw_only"
    assert origin["source_file"] is not None
    assert origin["source_file"].endswith("ceiling_grant_tasks.py")


def test_generated_main_registration_records_originating_script(workspace, define_in_main) -> None:
    fn = define_in_main(
        """
        import shepherd as sp

        def scripted(repo: sp.May[sp.GitRepo, sp.ReadWrite], note: str) -> None:
            \"\"\"A bodyless task defined in a run-as-script module.\"\"\"
        """,
        "scripted",
    )
    version = workspace.tasks.register(fn)
    origin = version.metadata.get("shepherd.derived_from_callable")
    assert origin is not None
    assert origin["qualname"] == "scripted"
    # file_path is None for a generated artifact; the originating script is recorded instead.
    assert origin["source_file"] is not None


def test_register_source_records_ceiling_provenance_but_no_callable_origin(workspace) -> None:
    version = workspace.tasks.register_source(
        task_id="src.only",
        module="src_only_tasks",
        source_text=textwrap.dedent(
            """
            import shepherd as sp

            def w(repo: sp.May[sp.GitRepo, sp.ReadWrite], note: str) -> None:
                \"\"\"writer.\"\"\"
            """
        ),
        entrypoint="w",
    )
    assert _provenance(version) == "derived"
    assert "shepherd.derived_from_callable" not in version.metadata


def test_reserved_metadata_key_collision_refuses(workspace, grant_module) -> None:
    import ceiling_grant_tasks

    with pytest.raises(TaskRegistrationError, match="reserved"):
        workspace.tasks.register(
            ceiling_grant_tasks.rw_only,
            metadata={"shepherd.ceiling_provenance": "explicit"},
        )
