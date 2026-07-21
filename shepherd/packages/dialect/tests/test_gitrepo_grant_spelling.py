"""The bare-vs-explicit GitRepo grant spelling is recorded as registration provenance.

`repo: GitRepo` and `repo: May[GitRepo, ReadWrite]` compile to a *byte-identical*
grant descriptor (same content-addressed digest), so the compiled authority cannot tell
them apart — by design (P-030 §4: bare handle annotations default permissive-for-that-binding,
and the default must be countable/lintable). `gitrepo_grant_spelling` records which syntactic
form was written, in the signature schema, never in the descriptor.

Serde-safety contract exercised here: the spelling lives in `signature_schema`, and is kept out
of the content-addressed *authority* identity (the `GitRepoGrantDescriptor` digest is identical for
both spellings — asserted below). It *does* feed `schema_digest` (`_task_schema_digest` digests
`signature_schema`), so a 0.3 registration gets a new schema-version identity — but that orphans no
store state, because `schema_digest` is copy-forward provenance: every consumer reads the stored
value off the resolved version record and none recompute-and-compare it. A pre-0.3 record keeps its
stored digest and simply lacks the key (read as "unknown").
"""

from __future__ import annotations

import ast

import pytest

from shepherd_dialect.workspace_control.authority_declarations import (
    gitrepo_grant_spelling,
    gitrepo_grant_spelling_from_ast,
)
from shepherd_dialect.workspace_control.workspace import (
    _signature_schema,
    _signature_schema_from_ast,
)

# Module-level so collection survives the dialect-only env (the container CI
# lane syncs just this package; the sibling files import shepherd in-function).
sp = pytest.importorskip("shepherd", reason="requires the shepherd meta package")


def _param(schema: dict, name: str) -> dict:
    return next(p for p in schema["parameters"] if p["name"] == name)


# ── the classifiers, directly ────────────────────────────────────────────────


def test_runtime_spelling_bare_vs_may() -> None:
    assert gitrepo_grant_spelling(sp.GitRepo) == "bare"
    assert gitrepo_grant_spelling(sp.May[sp.GitRepo, sp.ReadWrite]) == "may"
    assert gitrepo_grant_spelling(sp.May[sp.GitRepo, sp.ReadOnly]) == "may"


def test_ast_spelling_bare_vs_may() -> None:
    bare = ast.parse("GitRepo", mode="eval").body
    sp_bare = ast.parse("sp.GitRepo", mode="eval").body
    may = ast.parse("May[GitRepo, ReadWrite]", mode="eval").body
    assert gitrepo_grant_spelling_from_ast(bare) == "bare"
    assert gitrepo_grant_spelling_from_ast(sp_bare) == "bare"
    assert gitrepo_grant_spelling_from_ast(may) == "may"
    assert (
        gitrepo_grant_spelling_from_ast(None) == "may"
    )  # precondition is "a grant exists"; never called on None in practice


# ── recorded in the runtime signature schema ─────────────────────────────────


def test_runtime_schema_records_bare_spelling() -> None:
    @sp.task
    def bare(repo: sp.GitRepo, topic: str) -> None:
        """Bare writable handle."""

    schema = _signature_schema(bare._fn if hasattr(bare, "_fn") else bare)
    repo_param = _param(schema, "repo")
    assert repo_param["gitrepo_grant_spelling"] == "bare"
    # A plain value parameter carries neither the grant nor the spelling.
    assert "gitrepo_grant" not in _param(schema, "topic")
    assert "gitrepo_grant_spelling" not in _param(schema, "topic")


def test_runtime_schema_records_may_spelling() -> None:
    @sp.task
    def explicit(repo: sp.May[sp.GitRepo, sp.ReadWrite], ro: sp.May[sp.GitRepo, sp.ReadOnly]) -> None:
        """Explicit grants."""

    schema = _signature_schema(explicit._fn if hasattr(explicit, "_fn") else explicit)
    assert _param(schema, "repo")["gitrepo_grant_spelling"] == "may"
    assert _param(schema, "ro")["gitrepo_grant_spelling"] == "may"


# ── the load-bearing invariant: spelling is the ONLY discriminator ───────────


def test_bare_and_explicit_readwrite_share_the_descriptor_but_differ_in_spelling() -> None:
    @sp.task
    def bare(repo: sp.GitRepo) -> None:
        """Bare."""

    @sp.task
    def explicit(repo: sp.May[sp.GitRepo, sp.ReadWrite]) -> None:
        """Explicit ReadWrite."""

    bare_param = _param(_signature_schema(bare._fn if hasattr(bare, "_fn") else bare), "repo")
    expl_param = _param(_signature_schema(explicit._fn if hasattr(explicit, "_fn") else explicit), "repo")

    # Identical compiled authority (this is why the spelling has to be recorded separately)...
    assert bare_param["gitrepo_grant"] == expl_param["gitrepo_grant"]
    # ...distinguishable only by the provenance marker.
    assert bare_param["gitrepo_grant_spelling"] == "bare"
    assert expl_param["gitrepo_grant_spelling"] == "may"


# ── recorded on the generated-source (AST) path too ──────────────────────────


def test_generated_source_schema_records_spelling() -> None:
    source = (
        "import shepherd as sp\n"
        "def write(repo: sp.GitRepo, ro: sp.May[sp.GitRepo, sp.ReadOnly], topic: str) -> None:\n"
        "    'generated'\n"
    )
    schema = _signature_schema_from_ast(ast.parse(source), module_name="gen", qualname="write")
    assert _param(schema, "repo")["gitrepo_grant_spelling"] == "bare"
    assert _param(schema, "ro")["gitrepo_grant_spelling"] == "may"
    assert "gitrepo_grant_spelling" not in _param(schema, "topic")


# ── back-compat: a pre-0.3 schema lacks the key and reads as "unknown" ───────


def test_absent_spelling_reads_as_unknown() -> None:
    # A record written before 0.3.0 carries the grant but no spelling; readers must treat
    # the absent field as "unknown", never infer a value.
    legacy_param = {"name": "repo", "gitrepo_grant": {"schema": "…", "grant_ref": "signature:repo", "clauses": []}}
    assert legacy_param.get("gitrepo_grant_spelling") is None
