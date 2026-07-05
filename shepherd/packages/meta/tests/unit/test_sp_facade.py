"""WS-A: the public ``import shepherd as sp`` facade contract.

Pins the v0.1.2 getting-started surface: the full manifest resolves, the offline
import stays light (no eager ``vcs_core``), the laziness is real, and the
out-of-scope surface stays absent.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import shepherd as sp

# The exact public manifest — single source of truth for the pin, ordered to
# match ``shepherd.__all__`` (teaching flow, not alphabetical).
EXPECTED_MANIFEST = [
    "__version__",
    # offline syntax nucleus
    "workspace",
    "Workspace",
    "task",
    "deliver",
    "handle",
    "ask",
    "tell",
    "Permissive",
    "current_binding",
    # runs / delivery
    "Run",
    "RunRef",
    "RunInProgress",
    "DeliveryException",
    "DeliveryFailed",
    "emit_artifact",
    "Artifact",
    # effect / permission algebra
    "Match",
    "Plan",
    "Subset",
    "EffectNotPermitted",
    "EffectSurfaceEmpty",
    "EffectSurfaceTooWide",
    "OverbroadHandler",
    "PlanNotExtractable",
    # handle surface
    "GitRepo",
    "GitRepoBasis",
    "open",
    "ShepherdWorkspace",
    "WorkspaceRun",
    "WorkspaceTask",
    "RunOutput",
    "Changeset",
    "ChangesetStat",
    "May",
    "ReadOnly",
    "ReadWrite",
    "GitRepoGrant",
    "Flow",
    "FlowControlClient",
]

# The substrate-pulling names that resolve lazily via module ``__getattr__``.
LAZY_NAMES = [
    "ShepherdWorkspace",
    "RunOutput",
    "WorkspaceRun",
    "WorkspaceTask",
    "Changeset",
    "ChangesetStat",
    "May",
    "ReadOnly",
    "ReadWrite",
    "GitRepoGrant",
    "Flow",
    "FlowControlClient",
]


def test_manifest_is_pinned() -> None:
    assert sp.__all__ == EXPECTED_MANIFEST


def test_every_manifest_name_resolves() -> None:
    for name in sp.__all__:
        assert getattr(sp, name) is not None


def test_lazy_handle_surface_resolves() -> None:
    for name in LAZY_NAMES:
        assert getattr(sp, name) is not None


def test_dir_advertises_lazy_names() -> None:
    listed = dir(sp)
    for name in LAZY_NAMES:
        assert name in listed


def test_open_is_the_substrate_entry_distinct_from_workspace() -> None:
    # `sp.workspace` is the offline, process-local context manager; `sp.open` is
    # the substrate-backed handle workspace. They must not be the same object.
    assert callable(sp.open)
    assert sp.open is not sp.workspace


def test_open_forwards_substrate_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from shepherd_dialect.workspace_control import ShepherdWorkspace

    sentinel = object()
    calls: list[tuple[str, bool, str | None]] = []

    def fake_discover(
        cls: type[ShepherdWorkspace],
        cwd: str = ".",
        *,
        activate: bool = True,
        backend: str | None = "clonefile",
    ) -> object:
        del cls
        calls.append((cwd, activate, backend))
        return sentinel

    monkeypatch.setattr(ShepherdWorkspace, "discover", classmethod(fake_discover))

    assert sp.open("repo", activate=False, backend="fuse") is sentinel
    assert calls == [("repo", False, "fuse")]


def test_unknown_attribute_raises() -> None:
    with pytest.raises(AttributeError):
        _ = sp.definitely_not_a_public_symbol  # type: ignore[attr-defined]


def test_out_of_scope_surface_stays_absent() -> None:
    # No framework combinators; the value noun stays a pure value.
    assert not hasattr(sp, "best_of_n")
    assert not hasattr(sp, "gather")
    for verb in ("write", "apply", "run"):
        assert not hasattr(sp.GitRepo, verb)
    # P-030 v0.2 fence: the path-scoped grant spelling is not part of the public facade.
    assert not hasattr(sp, "GitRepoPath")
    assert "GitRepoPath" not in sp.__all__


def test_offline_import_stays_light() -> None:
    # A bare `import shepherd` must not pull the vcs_core substrate engine, so the
    # offline @task on-ramp stays import-light and cross-platform.
    code = "import shepherd, sys; sys.exit(1 if 'vcs_core' in sys.modules else 0)"
    result = subprocess.run([sys.executable, "-c", code], check=False)
    assert result.returncode == 0, "import shepherd eagerly loaded vcs_core"


def test_touching_a_handle_symbol_loads_the_substrate() -> None:
    # The complement: accessing a lazy symbol *does* pull vcs_core — proving the
    # laziness is real, not that the surface is merely missing.
    code = (
        "import shepherd, sys\n"
        "assert 'vcs_core' not in sys.modules\n"
        "shepherd.ShepherdWorkspace\n"
        "sys.exit(0 if 'vcs_core' in sys.modules else 1)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], check=False)
    assert result.returncode == 0, "touching a handle symbol did not load vcs_core"
