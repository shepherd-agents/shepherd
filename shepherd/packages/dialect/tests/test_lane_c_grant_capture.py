"""Lane C, LC-3a: per-binding grant capture.

The v0.1 compiler accepted ``May[GitRepo, ...]`` only on the injected ``repo`` parameter. LC-3a
lifts that gate so a grant is captured on *any* parameter (the param name rides ``grant_ref``),
which is what the multi-binding path (fenced until LC-4) reads. Single-binding capture is unchanged.
"""

from __future__ import annotations

from shepherd_runtime.nucleus import GitRepo

from shepherd_dialect.workspace_control import May, ReadOnly, ReadWrite
from shepherd_dialect.workspace_control.authority_declarations import compile_gitrepo_grant_from_annotation


def test_non_repo_param_captures_readonly_grant() -> None:
    descriptor = compile_gitrepo_grant_from_annotation(May[GitRepo, ReadOnly], parameter_name="docs")
    assert descriptor is not None  # would have raised on v0.1 (non-repo gate)
    assert descriptor.grant_ref == "signature:docs"
    assert len(descriptor.clauses) == 1
    assert descriptor.clauses[0].mutates is False  # ReadOnly


def test_non_repo_param_captures_readwrite_grant() -> None:
    descriptor = compile_gitrepo_grant_from_annotation(May[GitRepo, ReadWrite], parameter_name="backend")
    assert descriptor is not None
    assert descriptor.grant_ref == "signature:backend"
    assert descriptor.clauses[0].mutates is None  # ReadWrite = unconstrained mutates


def test_repo_param_capture_unchanged() -> None:
    descriptor = compile_gitrepo_grant_from_annotation(May[GitRepo, ReadOnly], parameter_name="repo")
    assert descriptor is not None
    assert descriptor.grant_ref == "signature:repo"


def test_non_may_annotation_is_not_a_grant() -> None:
    assert compile_gitrepo_grant_from_annotation(GitRepo, parameter_name="docs") is None
    assert compile_gitrepo_grant_from_annotation(str, parameter_name="issue") is None


# --- LC-3b: per-param reader + fail-closed join --------------------------------------------

import pytest

from shepherd_dialect.workspace_control.authority import (
    gitrepo_grant_descriptor_from_public_grant,
)
from shepherd_dialect.workspace_control.errors import WorkspaceControlError
from shepherd_dialect.workspace_control.workspace import (
    _join_bindings_to_grants,
    _workspace_gitrepo_grants_by_param,
)


def _grant(param: str, grant: object):
    return gitrepo_grant_descriptor_from_public_grant(grant, grant_ref=f"signature:{param}")


def test_grants_by_param_reads_every_granted_parameter() -> None:
    schema = {
        "parameters": [
            {"name": "docs", "gitrepo_grant": _grant("docs", ReadOnly).to_descriptor()},
            {"name": "backend", "gitrepo_grant": _grant("backend", ReadWrite).to_descriptor()},
            {"name": "issue"},  # ordinary param, no grant
        ]
    }
    by_param = _workspace_gitrepo_grants_by_param(schema)
    assert set(by_param) == {"docs", "backend"}


def test_join_exact_correspondence_returns_sorted_triples() -> None:
    grants = {"docs": _grant("docs", ReadOnly), "backend": _grant("backend", ReadWrite)}
    roots = {"docs": "/ws/docs", "backend": "/ws/backend"}
    joined = _join_bindings_to_grants(binding_roots=roots, grants_by_param=grants)
    assert [name for name, _root, _grant in joined] == ["backend", "docs"]
    assert [root for _name, root, _grant in joined] == ["/ws/backend", "/ws/docs"]


def test_join_granted_param_without_binding_fails_closed() -> None:
    grants = {"docs": _grant("docs", ReadOnly), "backend": _grant("backend", ReadWrite)}
    roots = {"backend": "/ws/backend"}  # docs is granted but never bound → would run ungranted
    with pytest.raises(WorkspaceControlError, match="no matching binding"):
        _join_bindings_to_grants(binding_roots=roots, grants_by_param=grants)


def test_join_binding_without_grant_fails_closed() -> None:
    grants = {"backend": _grant("backend", ReadWrite)}
    roots = {"docs": "/ws/docs", "backend": "/ws/backend"}  # docs bound but ungranted → silent authority
    with pytest.raises(WorkspaceControlError, match="no matching May"):
        _join_bindings_to_grants(binding_roots=roots, grants_by_param=grants)
