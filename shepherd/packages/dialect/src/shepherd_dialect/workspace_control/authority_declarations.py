"""Workspace-control authority declaration compiler for the slice."""

from __future__ import annotations

import ast
from dataclasses import dataclass

from shepherd_runtime.nucleus import GitRepo

from shepherd_dialect.workspace_control.authority import (
    GitRepoGrant,
    GitRepoGrantDescriptor,
    GitRepoPath,
    ReadOnly,
    ReadWrite,
    _reject_path_scoped_clauses,
    gitrepo_grant_descriptor_from_may_annotation,
)


class AuthorityDeclarationError(ValueError):
    """Raised when an authority declaration cannot be compiled safely."""


@dataclass(frozen=True)
class CompiledParameterGrant:
    """One compiled per-parameter authority grant."""

    parameter_name: str
    grant_descriptor: GitRepoGrantDescriptor


@dataclass(frozen=True)
class CompiledWorkspaceAuthorityDeclaration:
    """Compiled authority declarations for one workspace-control task signature."""

    signature_grants: tuple[CompiledParameterGrant, ...]

    @property
    def workspace_gitrepo_grant(self) -> GitRepoGrantDescriptor | None:
        grants = [grant.grant_descriptor for grant in self.signature_grants]
        if not grants:
            return None
        if len(grants) != 1:
            raise AuthorityDeclarationError("workspace-control GitRepo grant v0 supports exactly one repo grant")
        return grants[0]


def compile_gitrepo_grant_from_annotation(
    annotation: object,
    *,
    parameter_name: str,
) -> GitRepoGrantDescriptor | None:
    """Compile a resolved runtime annotation into a GitRepo grant descriptor.

    Bare ``GitRepo`` is the ergonomic whole-workspace writable handle spelling.
    ``May[GitRepo, ...]`` remains the explicit permission spelling.
    """
    if annotation is GitRepo:
        return ReadWrite.to_descriptor(grant_ref=f"signature:{parameter_name}")
    try:
        descriptor = gitrepo_grant_descriptor_from_may_annotation(
            annotation,
            grant_ref=f"signature:{parameter_name}",
        )
    except (TypeError, ValueError) as exc:
        raise AuthorityDeclarationError(str(exc)) from exc
    # LC-3a: per-binding capture. A ``May[GitRepo, ...]`` grant is captured on any parameter, not
    # only the injected ``repo``; the parameter name rides ``grant_ref`` ("signature:<param>"). The
    # multi-binding run path (fenced until LC-4) reads these per-parameter grants; the single-binding
    # ``repo=`` path still resolves exactly one grant downstream.
    return descriptor


def compile_gitrepo_grant_from_ast_annotation(
    annotation: ast.expr | None,
    *,
    parameter_name: str,
) -> GitRepoGrantDescriptor | None:
    """Compile supported generated-source GitRepo authority syntax without ``eval``."""
    if annotation is None:
        return None
    if _expr_name(annotation) == "GitRepo":
        return ReadWrite.to_descriptor(grant_ref=f"signature:{parameter_name}")
    if not isinstance(annotation, ast.Subscript):
        return None
    root_name = _expr_name(annotation.value)
    if root_name not in {"May", "Annotated"}:
        return None
    args = _subscript_args(annotation)
    if len(args) < 2:
        if root_name == "May":
            raise AuthorityDeclarationError("May[...] requires exactly two arguments: May[HandleType, Grant]")
        return None
    handle, *metadata = args
    handle_name = _expr_name(handle)
    grant_exprs = [_expr for _expr in metadata if _ast_expr_is_public_gitrepo_grant(_expr)]
    if root_name == "May" or handle_name == "GitRepo" or grant_exprs:
        if handle_name != "GitRepo":
            raise AuthorityDeclarationError("GitRepo May grant metadata must annotate shepherd_runtime.nucleus.GitRepo")
        if len(metadata) != 1:
            raise AuthorityDeclarationError("May[GitRepo, ...] supports exactly one GitRepo grant in this slice")
        grant = _public_gitrepo_grant_from_ast(metadata[0])
        descriptor = grant.to_descriptor(grant_ref=f"signature:{parameter_name}")
        # P-030 v0.2 fence: reject path-scoped grants at the generated-source (AST) seam, the same
        # as the runtime seam. Keyed on the ``path_prefix`` field, honoring the private escape.
        try:
            _reject_path_scoped_clauses(descriptor.clauses)
        except ValueError as exc:
            raise AuthorityDeclarationError(str(exc)) from exc
        # LC-3a: per-binding capture (generated-source path) — captured on any parameter, not only
        # the injected ``repo``; the parameter name rides ``grant_ref``.
        return descriptor
    return None


def gitrepo_grant_spelling(annotation: object) -> str:
    """Classify a resolved GitRepo grant annotation's spelling: ``"bare"`` or ``"may"``.

    Recorded as registration provenance beside the compiled grant (P-030 §4 item 4: a bare
    handle annotation defaults permissive-for-that-binding and must be recorded so the default
    is countable and lintable). Provenance only — the compiled ``GitRepoGrantDescriptor`` and its
    content-addressed digest are *identical* for ``GitRepo`` and ``May[GitRepo, ReadWrite]``, so
    the spelling is deliberately **not** a descriptor field. Precondition: ``annotation`` already
    compiled to a GitRepo grant via :func:`compile_gitrepo_grant_from_annotation`.
    """
    return "bare" if annotation is GitRepo else "may"


def gitrepo_grant_spelling_from_ast(annotation: ast.expr | None) -> str:
    """AST (generated-source) twin of :func:`gitrepo_grant_spelling`.

    Mirrors :func:`compile_gitrepo_grant_from_ast_annotation`'s bare arm (a plain ``GitRepo`` /
    ``sp.GitRepo`` name); every other compiling shape is ``May[GitRepo, ...]``. Same precondition:
    the annotation already compiled to a grant.
    """
    return "bare" if annotation is not None and _expr_name(annotation) == "GitRepo" else "may"


def raw_annotation_looks_like_authority(annotation: object) -> bool:
    """Return whether an unresolved annotation appears to carry authority syntax."""
    if not isinstance(annotation, str):
        return False
    text = annotation.replace(" ", "")
    return (
        text in {"GitRepo", "sp.GitRepo"}
        or text.endswith(".GitRepo")
        or "May[" in text
        or ("Annotated[" in text and "GitRepo" in text)
    )


def _subscript_args(node: ast.Subscript) -> tuple[ast.expr, ...]:
    if isinstance(node.slice, ast.Tuple):
        return tuple(node.slice.elts)
    return (node.slice,)


def _expr_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _ast_expr_is_public_gitrepo_grant(node: ast.expr) -> bool:
    name = _expr_name(node)
    if name in {"ReadOnly", "ReadWrite"}:
        return True
    return isinstance(node, ast.Call) and _expr_name(node.func) == "GitRepoPath"


def _public_gitrepo_grant_from_ast(node: ast.expr) -> GitRepoGrant:
    name = _expr_name(node)
    if name == "ReadOnly":
        return ReadOnly
    if name == "ReadWrite":
        return ReadWrite
    if isinstance(node, ast.Call) and _expr_name(node.func) == "GitRepoPath":
        if len(node.args) != 1:
            raise AuthorityDeclarationError("GitRepoPath(...) requires exactly one path_prefix argument")
        path_prefix = _literal_str(node.args[0], "GitRepoPath path_prefix")
        binding_ref = "workspace"
        mutates: bool | None = True
        for keyword in node.keywords:
            if keyword.arg is None:
                raise AuthorityDeclarationError("GitRepoPath(...) does not support **kwargs in authority declarations")
            if keyword.arg == "binding_ref":
                binding_ref = _literal_str(keyword.value, "GitRepoPath binding_ref")
            elif keyword.arg == "mutates":
                mutates = _literal_bool_or_none(keyword.value, "GitRepoPath mutates")
            else:
                raise AuthorityDeclarationError(f"GitRepoPath(...) does not support keyword {keyword.arg!r}")
        try:
            return GitRepoPath(path_prefix, binding_ref=binding_ref, mutates=mutates)
        except (TypeError, ValueError) as exc:
            raise AuthorityDeclarationError(str(exc)) from exc
    rendered = ast.unparse(node)
    raise AuthorityDeclarationError(f"unsupported GitRepo May grant: {rendered}")


def _literal_str(node: ast.expr, field_name: str) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise AuthorityDeclarationError(f"{field_name} must be a string literal")


def _literal_bool_or_none(node: ast.expr, field_name: str) -> bool | None:
    if isinstance(node, ast.Constant) and (isinstance(node.value, bool) or node.value is None):
        return node.value
    raise AuthorityDeclarationError(f"{field_name} must be a boolean or None literal")
