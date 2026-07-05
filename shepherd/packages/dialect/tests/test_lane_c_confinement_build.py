"""P-030 Lane C LC-3d — per-binding decision → BindingRootGrant → confinement (transform seam).

`resolve_per_binding_authority` is the LC-3d transform core: it consumes LC-3b's joined
`(name, root, grant)` triples, re-keys each captured clause to its binding name, resolves the
S1/S2 per-binding decision, and emits the `BindingRootGrant` sequence the jail lowering turns into
`writable_roots = ⋃(ReadWrite roots)`. These prove the transform at the seam (the run path stays
fenced through LC-3; end-to-end jail enforcement is LC-4/LC-5). The raw grants deliberately carry a
`binding_ref="repo"` so the re-keying to the binding name is exercised.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from shepherd_dialect.confinement import lower_grants_to_confinement
from shepherd_dialect.workspace_control.authority import GitRepoGrantClause, GitRepoGrantDescriptor
from shepherd_dialect.workspace_control.workspace_authority import resolve_per_binding_authority

if TYPE_CHECKING:
    from pathlib import Path


def _grant(mutates: bool | None) -> GitRepoGrantDescriptor:
    """A per-parameter grant with a deliberately non-binding `binding_ref` (re-keying is tested)."""
    return GitRepoGrantDescriptor(
        grant_ref="signature:param", clauses=(GitRepoGrantClause(binding_ref="repo", mutates=mutates),)
    )


def _real(path: Path) -> str:
    return os.path.realpath(str(path))


def _dirs(tmp_path: Path, *names: str) -> list[str]:
    out = []
    for name in names:
        d = tmp_path / name
        d.mkdir()
        out.append(_real(d))
    return out


def test_flagship_docs_ro_backend_rw(tmp_path: Path) -> None:
    docs, backend = _dirs(tmp_path, "docs", "backend")
    joined = [("docs", docs, _grant(False)), ("backend", backend, _grant(None))]
    decision, grants = resolve_per_binding_authority(task_default="Permissive", requested_may=None, joined=joined)
    # S2: per-binding authority is preserved, not collapsed.
    assert decision.repo_authority_by_binding() == {"docs": "readonly", "backend": "readwrite"}
    # The jail lowering: only backend is writable; docs is excluded (refused at the syscall).
    assert lower_grants_to_confinement(list(grants)).writable_roots == (backend,)


def test_readonly_ceiling_excludes_all(tmp_path: Path) -> None:
    docs, backend = _dirs(tmp_path, "docs", "backend")
    # Both requested ReadWrite, but a whole-run ReadOnly ceiling must clamp both (S1).
    joined = [("docs", docs, _grant(None)), ("backend", backend, _grant(None))]
    decision, grants = resolve_per_binding_authority(task_default="ReadOnly", requested_may=None, joined=joined)
    assert decision.repo_authority_by_binding() == {"docs": "readonly", "backend": "readonly"}
    assert lower_grants_to_confinement(list(grants)).writable_roots == ()


def test_both_readwrite_union(tmp_path: Path) -> None:
    a, b = _dirs(tmp_path, "a", "b")
    joined = [("a", a, _grant(None)), ("b", b, _grant(None))]
    _decision, grants = resolve_per_binding_authority(task_default="Permissive", requested_may=None, joined=joined)
    assert set(lower_grants_to_confinement(list(grants)).writable_roots) == {a, b}
