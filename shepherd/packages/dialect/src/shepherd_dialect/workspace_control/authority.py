"""authority helpers for the workspace-control vertical slice."""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated as May
from typing import Literal, cast, get_args, get_origin

from shepherd_runtime.effects import Match
from shepherd_runtime.nucleus import GitRepo
from vcs_core.runtime_api import AuthorityDecision, AuthorityOutcome

_MATCH_DESCRIPTOR_SCHEMA = "shepherd.workspace-control.match.v1"
_AUTHORITY_SURFACE_PLAN_DESCRIPTOR_SCHEMA = "shepherd.workspace-control.authority-surface-plan.v1"
_GRANT_DESCRIPTOR_SCHEMA = "shepherd.workspace-control.gitrepo-grant.v1"
_CLAMP_DESCRIPTOR_SCHEMA = "shepherd.workspace-control.gitrepo-grant-clamp.v1"

FieldConstraint = dict[str, object]
FieldConstraintClause = tuple[FieldConstraint, ...]
GitRepoClassificationBasis = Literal["effect_record", "exact_tree_diff", "changed_paths_fallback", "unclassifiable"]
GitRepoCompleteness = Literal["complete", "advisory", "incomplete"]

_CLASSIFICATION_BASIS_VALUES = frozenset(
    {"effect_record", "exact_tree_diff", "changed_paths_fallback", "unclassifiable"}
)
_STRING_EQ_FIELDS = frozenset(
    {
        "domain",
        "route",
        "binding_ref",
        "kind",
        "action",
        "reversibility",
        "monitor_basis",
        "classification_basis",
    }
)
_BOOLEAN_EQ_FIELDS = frozenset({"mutates", "control_plane"})
_ALLOWED_FIELD_OPS: dict[str, frozenset[str]] = {
    **{field: frozenset({"eq"}) for field in _STRING_EQ_FIELDS},
    **{field: frozenset({"eq"}) for field in _BOOLEAN_EQ_FIELDS},
    "path": frozenset({"eq", "startswith"}),
}


# P-030 v0.2 grant fence.
#
# Path-scoped (``path_prefix``) GitRepo grants are NOT part of the v0.2 claim: jail enforcement is
# whole-root, and sub-root grants are ``target-spec`` (P-030 rev-5 phase iii). They remain sound,
# settlement-boundary internal machinery, so the two *public* ``May[...]`` acceptance seams
# (``gitrepo_grant_descriptor_from_public_grant`` for the runtime route and the AST recognizer in
# ``authority_declarations``) refuse them unless a caller explicitly opts in via the private
# escape below. The escape is deliberately not a public ``register``/``register_source``/
# ``update_source`` keyword. Descriptor construction and ``from_descriptor`` rehydration are out of
# bounds for the fence: persisted authority contexts legitimately carry ``path_prefix`` clauses.
_PATH_SCOPED_GRANT_REJECTION = (
    "path-scoped grants are not part of the P-030 v0.2 claim; use ReadOnly or ReadWrite "
    "(sub-root GitRepo grants are target-spec / settlement-boundary internal only)"
)
_ALLOW_PATH_PREFIX_GRANTS: ContextVar[bool] = ContextVar("_allow_path_prefix_grants", default=False)


@contextlib.contextmanager
def _allow_path_prefix_grants() -> Iterator[None]:
    """Private escape: permit path-scoped GitRepo grants through the public compile seams.

    Internal adoption-boundary / settlement-lane callers (and their tests) legitimately compile
    ``path_prefix`` grants. This context manager scopes that permission around a public compile
    call without exposing a public registration keyword. It is intentionally underscore-private.
    """
    token = _ALLOW_PATH_PREFIX_GRANTS.set(True)
    try:
        yield
    finally:
        _ALLOW_PATH_PREFIX_GRANTS.reset(token)


def _reject_path_scoped_clauses(clauses: Iterable[GitRepoGrantClause]) -> None:
    """Refuse any clause carrying a ``path_prefix`` unless the private escape is active.

    Keys on the field being set, never on the grant *type* — ``ReadOnly``/``ReadWrite`` are
    ``GitRepoGrant`` instances with ``path_prefix=None`` and always pass. Scans every clause, so a
    multi-clause descriptor with a single path-scoped clause is still refused.
    """
    if _ALLOW_PATH_PREFIX_GRANTS.get():
        return
    if any(clause.path_prefix is not None for clause in clauses):
        raise ValueError(_PATH_SCOPED_GRANT_REJECTION)


@dataclass(frozen=True)
class GitRepoAuthoritySurface:
    """Helper-built ``Match`` surface plus descriptors used as durable evidence."""

    label: str
    effective_match: Match
    effective_match_descriptor: dict[str, object]
    effective_match_digest: str
    authority_surface_plan_descriptor: dict[str, object]
    authority_surface_plan_digest: str
    path_sensitive: bool


@dataclass(frozen=True)
class GitRepoAuthorityDecisionPolicy:
    """Policy envelope for evaluating one flat GitRepo authority request."""

    routes: tuple[str, ...]
    binding_refs: tuple[str, ...] | None = None
    domain: str = "gitrepo.v0"
    allowed_classification_bases: tuple[GitRepoClassificationBasis, ...] = ("effect_record", "exact_tree_diff")
    allow_changed_paths_fallback: bool = False
    allow_changed_paths_fallback_for_path_sensitive: bool = False
    allow_control_plane: bool = False
    reason_code_subject: str = "gitrepo_authority"
    allow_reason_code: str = "gitrepo_authority_match"
    outside_match_reason_code: str = "gitrepo_authority_outside_effective_match"
    denied_when_mutates_outside_match: bool = False
    mutating_outside_match_reason_code: str = "gitrepo_authority_mutates_outside_effective_match"
    invalid_view_reason_code_prefix: str = "gitrepo_authority_match_view_invalid"
    match_evaluation_failed_reason_code_prefix: str = "gitrepo_authority_match_evaluation_failed"
    unclassifiable_reason_code: str = "unclassifiable_gitrepo_authority"
    path_fallback_incomplete_reason_code: str = "changed_paths_fallback_incomplete_for_path_authority"
    default_monitor_basis: str = "carrier_check_at_commit"

    def __post_init__(self) -> None:
        if not self.routes:
            raise ValueError("GitRepo authority decision policy requires at least one route")
        for route in self.routes:
            _require_non_empty_string(route, "GitRepo authority decision route")
        if self.binding_refs is not None:
            if not self.binding_refs:
                raise ValueError("GitRepo authority decision binding_refs must not be empty")
            for binding_ref in self.binding_refs:
                _require_non_empty_string(binding_ref, "GitRepo authority decision binding_ref")
        _require_non_empty_string(self.domain, "GitRepo authority decision domain")
        for basis in self.allowed_classification_bases:
            if basis not in _CLASSIFICATION_BASIS_VALUES:
                raise ValueError(f"GitRepo classification basis is unsupported: {basis!r}")

    def to_descriptor(self) -> dict[str, object]:
        """Return the stable JSON-shaped policy descriptor for trace/run evidence."""
        return {
            "schema": "shepherd.workspace-control.gitrepo-authority-policy.v1",
            "routes": list(self.routes),
            "binding_refs": None if self.binding_refs is None else list(self.binding_refs),
            "domain": self.domain,
            "allowed_classification_bases": list(self.allowed_classification_bases),
            "allow_changed_paths_fallback": self.allow_changed_paths_fallback,
            "allow_changed_paths_fallback_for_path_sensitive": self.allow_changed_paths_fallback_for_path_sensitive,
            "allow_control_plane": self.allow_control_plane,
            "reason_code_subject": self.reason_code_subject,
            "allow_reason_code": self.allow_reason_code,
            "outside_match_reason_code": self.outside_match_reason_code,
            "denied_when_mutates_outside_match": self.denied_when_mutates_outside_match,
            "mutating_outside_match_reason_code": self.mutating_outside_match_reason_code,
            "invalid_view_reason_code_prefix": self.invalid_view_reason_code_prefix,
            "match_evaluation_failed_reason_code_prefix": self.match_evaluation_failed_reason_code_prefix,
            "unclassifiable_reason_code": self.unclassifiable_reason_code,
            "path_fallback_incomplete_reason_code": self.path_fallback_incomplete_reason_code,
            "default_monitor_basis": self.default_monitor_basis,
        }


@dataclass(frozen=True)
class GitRepoAuthorityView:
    """Validated GitRepo v0 authority fact projected to the Match evaluator."""

    domain: str
    kind: str
    binding_ref: str
    action: str
    path: str
    mutates: bool
    reversibility: str
    control_plane: bool
    monitor_basis: str
    route: str
    classification_basis: GitRepoClassificationBasis

    @classmethod
    def from_mapping(cls, mapping: object) -> GitRepoAuthorityView:
        if not isinstance(mapping, Mapping):
            raise TypeError("GitRepo authority view must be a mapping")
        mutates = _required_view_bool(mapping, "mutates")
        path = _required_view_path(mapping, "path")
        if mutates and path == "":
            raise ValueError("GitRepo authority view mutating facts require a path")
        classification_basis = _required_view_classification_basis(mapping)
        return cls(
            domain=_required_view_string(mapping, "domain"),
            kind=_required_view_string(mapping, "kind"),
            binding_ref=_required_view_string(mapping, "binding_ref"),
            action=_required_view_string(mapping, "action"),
            path=path,
            mutates=mutates,
            reversibility=_required_view_string(mapping, "reversibility"),
            control_plane=_required_view_bool(mapping, "control_plane"),
            monitor_basis=_required_view_string(mapping, "monitor_basis"),
            route=_required_view_string(mapping, "route"),
            classification_basis=classification_basis,
        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "kind": self.kind,
            "binding_ref": self.binding_ref,
            "action": self.action,
            "path": self.path,
            "mutates": self.mutates,
            "reversibility": self.reversibility,
            "control_plane": self.control_plane,
            "monitor_basis": self.monitor_basis,
            "route": self.route,
            "classification_basis": self.classification_basis,
        }


@dataclass(frozen=True)
class GitRepoGrant:
    """Public GitRepo grant value for the workspace-control v0 surface.

    This is the first public spelling accepted by ``May[GitRepo, ...]``. It
    deliberately lowers to the same internal descriptor fragment as the
    existing authority slice: one GitRepo binding, optional path prefix, and an
    optional mutates equality constraint.
    """

    label: str = "GitRepoGrant"
    binding_ref: str = "workspace"
    path_prefix: str | None = None
    mutates: bool | None = None

    def __post_init__(self) -> None:
        _require_non_empty_string(self.label, "GitRepo grant label")
        _require_non_empty_string(self.binding_ref, "GitRepo grant binding_ref")
        if self.mutates is not None and not isinstance(self.mutates, bool):
            raise TypeError("GitRepo grant mutates must be a boolean or None")
        object.__setattr__(self, "path_prefix", _normalize_path_prefix(self.path_prefix))

    def to_descriptor(self, *, grant_ref: str | None = None) -> GitRepoGrantDescriptor:
        """Lower this public grant to the internal descriptor fragment."""
        ref = grant_ref or f"public:{self.label}"
        return GitRepoGrantDescriptor(
            grant_ref=ref,
            clauses=(
                GitRepoGrantClause(
                    binding_ref=self.binding_ref,
                    path_prefix=self.path_prefix,
                    mutates=self.mutates,
                ),
            ),
        )


def GitRepoPath(
    path_prefix: str,
    *,
    binding_ref: str = "workspace",
    mutates: bool | None = True,
) -> GitRepoGrant:
    """Return a public path-sensitive GitRepo grant for ``May[GitRepo, ...]``."""
    return GitRepoGrant(
        label=f"GitRepoPath:{path_prefix}",
        binding_ref=binding_ref,
        path_prefix=path_prefix,
        mutates=mutates,
    )


def gitrepo_grant_descriptor_from_public_grant(
    grant: object,
    *,
    grant_ref: str,
) -> GitRepoGrantDescriptor:
    """Normalize a public GitRepo grant value into the descriptor fragment."""
    if isinstance(grant, GitRepoGrantDescriptor):
        descriptor = GitRepoGrantDescriptor(grant_ref=grant_ref, clauses=grant.clauses)
    elif isinstance(grant, GitRepoGrant):
        descriptor = grant.to_descriptor(grant_ref=grant_ref)
    else:
        raise TypeError(f"unsupported GitRepo May grant: {grant!r}")
    _reject_path_scoped_clauses(descriptor.clauses)
    return descriptor


def gitrepo_grant_descriptor_from_may_annotation(
    annotation: object,
    *,
    grant_ref: str,
) -> GitRepoGrantDescriptor | None:
    """Extract a GitRepo grant descriptor from ``May[GitRepo, ...]`` annotation."""
    if get_origin(annotation) is not May:
        return None
    args = get_args(annotation)
    if len(args) < 2:
        return None
    handle_type = args[0]
    metadata = args[1:]
    if handle_type is GitRepo:
        if len(metadata) != 1:
            raise ValueError("May[GitRepo, ...] supports exactly one GitRepo grant in this slice")
        return gitrepo_grant_descriptor_from_public_grant(metadata[0], grant_ref=grant_ref)
    grants = [item for item in metadata if isinstance(item, GitRepoGrant | GitRepoGrantDescriptor)]
    if len(grants) > 1:
        raise ValueError("May[GitRepo, ...] supports exactly one GitRepo grant in this slice")
    if grants:
        raise TypeError("GitRepo May grant metadata must annotate shepherd_runtime.nucleus.GitRepo")
    return None


@dataclass(frozen=True)
class GitRepoGrantClause:
    """One internal scalar GitRepo authority clause.

    ``None`` means unconstrained for that field inside the supported fragment.
    This is an internal descriptor skeleton, not public ``May[...]`` syntax.
    """

    binding_ref: str
    path_prefix: str | None = None
    mutates: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.binding_ref, str) or not self.binding_ref:
            raise ValueError("GitRepo grant clause requires a binding_ref")
        object.__setattr__(self, "path_prefix", _normalize_path_prefix(self.path_prefix))

    def to_descriptor(self) -> dict[str, object]:
        descriptor: dict[str, object] = {"binding_ref": self.binding_ref}
        if self.path_prefix is not None:
            descriptor["path_prefix"] = self.path_prefix
        if self.mutates is not None:
            descriptor["mutates"] = self.mutates
        return descriptor

    @classmethod
    def from_descriptor(cls, descriptor: Mapping[str, object]) -> GitRepoGrantClause:
        """Rehydrate one GitRepo grant clause from its stable descriptor."""
        if not isinstance(descriptor, Mapping):
            raise TypeError("GitRepo grant clause descriptor must be an object")
        allowed_fields = {"binding_ref", "path_prefix", "mutates"}
        unknown = sorted(str(key) for key in descriptor if key not in allowed_fields)
        if unknown:
            raise ValueError(f"GitRepo grant clause descriptor has unsupported field(s): {', '.join(unknown)}")
        binding_ref = descriptor.get("binding_ref")
        if not isinstance(binding_ref, str):
            raise TypeError("GitRepo grant clause descriptor binding_ref must be a string")
        path_prefix = descriptor.get("path_prefix")
        if path_prefix is not None and not isinstance(path_prefix, str):
            raise TypeError("GitRepo grant clause descriptor path_prefix must be a string or null")
        mutates = descriptor.get("mutates")
        if mutates is not None and not isinstance(mutates, bool):
            raise TypeError("GitRepo grant clause descriptor mutates must be a boolean or null")
        return cls(binding_ref=binding_ref, path_prefix=path_prefix, mutates=mutates)

    def to_constraint_clauses(self, *, route: str | None = None) -> tuple[FieldConstraintClause, ...]:
        constraints: list[FieldConstraint] = [{"field": "domain", "op": "eq", "value": "gitrepo.v0"}]
        if route is not None:
            constraints.append({"field": "route", "op": "eq", "value": route})
        constraints.append({"field": "binding_ref", "op": "eq", "value": self.binding_ref})
        if self.mutates is not None:
            constraints.append({"field": "mutates", "op": "eq", "value": self.mutates})
        if self.path_prefix is None:
            return (tuple(constraints),)
        return (
            (*constraints, {"field": "path", "op": "eq", "value": self.path_prefix}),
            (*constraints, {"field": "path", "op": "startswith", "value": f"{self.path_prefix}/"}),
        )


@dataclass(frozen=True)
class GitRepoGrantDescriptor:
    """Internal grant descriptor for the current GitRepo authority fragment."""

    grant_ref: str
    clauses: tuple[GitRepoGrantClause, ...]

    def to_descriptor(self) -> dict[str, object]:
        return {
            "schema": _GRANT_DESCRIPTOR_SCHEMA,
            "grant_ref": self.grant_ref,
            "clauses": [clause.to_descriptor() for clause in self.clauses],
        }

    @classmethod
    def from_descriptor(cls, descriptor: object) -> GitRepoGrantDescriptor:
        """Rehydrate a GitRepo grant descriptor and re-run all authority validation."""
        if not isinstance(descriptor, Mapping):
            raise TypeError("GitRepo grant descriptor must be an object")
        allowed_fields = {"schema", "grant_ref", "clauses"}
        unknown = sorted(str(key) for key in descriptor if key not in allowed_fields)
        if unknown:
            raise ValueError(f"GitRepo grant descriptor has unsupported field(s): {', '.join(unknown)}")
        if descriptor.get("schema") != _GRANT_DESCRIPTOR_SCHEMA:
            raise ValueError("GitRepo grant descriptor schema is unsupported")
        grant_ref = descriptor.get("grant_ref")
        if not isinstance(grant_ref, str) or not grant_ref:
            raise ValueError("GitRepo grant descriptor grant_ref must be a non-empty string")
        raw_clauses = descriptor.get("clauses")
        if not isinstance(raw_clauses, list | tuple):
            raise TypeError("GitRepo grant descriptor clauses must be a list")
        return cls(
            grant_ref=grant_ref,
            clauses=tuple(GitRepoGrantClause.from_descriptor(clause) for clause in raw_clauses),
        )

    @property
    def digest(self) -> str:
        return _descriptor_digest(self.to_descriptor())


@dataclass(frozen=True)
class GitRepoGrantClamp:
    """Result of clamping a requested grant by a parent ceiling."""

    parent_ceiling: GitRepoGrantDescriptor
    requested: GitRepoGrantDescriptor
    effective: GitRepoGrantDescriptor
    descriptor: dict[str, object]
    digest: str

    def to_descriptor(self) -> dict[str, object]:
        """Return the stable JSON-shaped clamp descriptor."""
        return dict(self.descriptor)

    @classmethod
    def from_descriptor(cls, descriptor: Mapping[str, object]) -> GitRepoGrantClamp:
        """Rehydrate a grant clamp and verify its recorded digests."""
        if not isinstance(descriptor, Mapping):
            raise TypeError("GitRepo grant clamp descriptor must be an object")
        allowed_fields = {
            "schema",
            "parent_ceiling",
            "parent_ceiling_digest",
            "requested",
            "requested_digest",
            "effective",
            "effective_digest",
            "effective_clause_count",
            "digest",
        }
        unknown = sorted(str(key) for key in descriptor if key not in allowed_fields)
        if unknown:
            raise ValueError(f"GitRepo grant clamp descriptor has unsupported field(s): {', '.join(unknown)}")
        if descriptor.get("schema") != _CLAMP_DESCRIPTOR_SCHEMA:
            raise ValueError("GitRepo grant clamp descriptor schema is unsupported")
        parent_ceiling = GitRepoGrantDescriptor.from_descriptor(_required_descriptor(descriptor, "parent_ceiling"))
        requested = GitRepoGrantDescriptor.from_descriptor(_required_descriptor(descriptor, "requested"))
        effective = GitRepoGrantDescriptor.from_descriptor(_required_descriptor(descriptor, "effective"))
        expected_parent_digest = _required_descriptor_digest(descriptor, "parent_ceiling_digest")
        expected_requested_digest = _required_descriptor_digest(descriptor, "requested_digest")
        expected_effective_digest = _required_descriptor_digest(descriptor, "effective_digest")
        if parent_ceiling.digest != expected_parent_digest:
            raise ValueError("GitRepo grant clamp parent_ceiling digest disagrees with descriptor")
        if requested.digest != expected_requested_digest:
            raise ValueError("GitRepo grant clamp requested digest disagrees with descriptor")
        if effective.digest != expected_effective_digest:
            raise ValueError("GitRepo grant clamp effective digest disagrees with descriptor")
        expected_clause_count = descriptor.get("effective_clause_count")
        if not isinstance(expected_clause_count, int) or expected_clause_count < 0:
            raise ValueError("GitRepo grant clamp effective_clause_count must be a non-negative integer")
        if len(effective.clauses) != expected_clause_count:
            raise ValueError("GitRepo grant clamp effective clause count disagrees with descriptor")
        recomputed = clamp_gitrepo_grants(
            parent_ceiling=parent_ceiling,
            requested=requested,
            grant_ref=effective.grant_ref,
        )
        if recomputed.effective.to_descriptor() != effective.to_descriptor():
            raise ValueError("GitRepo grant clamp effective grant is not the lawful parent/requested intersection")
        normalized = recomputed.to_descriptor()
        expected_digest = _required_descriptor_digest(descriptor, "digest")
        digest = _required_descriptor_digest(normalized, "digest")
        if digest != expected_digest:
            raise ValueError("GitRepo grant clamp digest disagrees with descriptor")
        return cls(
            parent_ceiling=parent_ceiling,
            requested=requested,
            effective=effective,
            descriptor=normalized,
            digest=digest,
        )


def build_gitrepo_field_authority_surface(
    *,
    label: str,
    clauses: tuple[FieldConstraintClause, ...],
    **descriptor_fields: object,
) -> GitRepoAuthoritySurface:
    """Build a descriptor-backed field ``Match`` over flat GitRepo authority views."""
    normalized_clauses = tuple(tuple(_normalize_constraint(item) for item in clause) for clause in clauses)
    effective_match = _match_from_constraint_clauses(normalized_clauses)
    match_descriptor: dict[str, object] = {
        "schema": _MATCH_DESCRIPTOR_SCHEMA,
        "label": label,
        "clauses": [[dict(constraint) for constraint in clause] for clause in normalized_clauses],
    }
    match_descriptor.update(descriptor_fields)
    match_digest = _descriptor_digest(match_descriptor)
    surface_plan_descriptor = {
        "schema": _AUTHORITY_SURFACE_PLAN_DESCRIPTOR_SCHEMA,
        "label": label,
        "allow_only": match_digest,
    }
    return GitRepoAuthoritySurface(
        label=label,
        effective_match=effective_match,
        effective_match_descriptor=match_descriptor,
        effective_match_digest=match_digest,
        authority_surface_plan_descriptor=surface_plan_descriptor,
        authority_surface_plan_digest=_descriptor_digest(surface_plan_descriptor),
        path_sensitive=_has_path_constraint(normalized_clauses),
    )


def decide_gitrepo_authority_request(
    *,
    request: object,
    surface: GitRepoAuthoritySurface,
    policy: GitRepoAuthorityDecisionPolicy,
) -> AuthorityDecision:
    """Evaluate a classified flat GitRepo authority request against a surface."""
    request_reason = getattr(request, "reason_code", None)
    if request_reason is not None:
        return _authority_decision(
            request=request,
            surface=surface,
            policy=policy,
            mapping=_best_effort_match_mapping(request),
            outcome="refused",
            reason_code=str(request_reason),
            completeness="incomplete",
        )

    try:
        view = GitRepoAuthorityView.from_mapping(dict(request.match_view.as_mapping()))  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError) as exc:
        return _authority_decision(
            request=request,
            surface=surface,
            policy=policy,
            mapping=None,
            outcome="refused",
            reason_code=f"{policy.invalid_view_reason_code_prefix}:{type(exc).__name__}",
            completeness="incomplete",
        )
    mapping = view.as_mapping()

    if view.domain != policy.domain:
        return _authority_decision(
            request=request,
            surface=surface,
            policy=policy,
            mapping=mapping,
            outcome="refused",
            reason_code=_unsupported_reason(policy, "domain"),
            completeness="incomplete",
        )
    if view.route not in policy.routes:
        return _authority_decision(
            request=request,
            surface=surface,
            policy=policy,
            mapping=mapping,
            outcome="refused",
            reason_code=_unsupported_reason(policy, "route"),
            completeness="incomplete",
        )
    if policy.binding_refs is not None and view.binding_ref not in policy.binding_refs:
        return _authority_decision(
            request=request,
            surface=surface,
            policy=policy,
            mapping=mapping,
            outcome="refused",
            reason_code=_unsupported_reason(policy, "binding"),
            completeness="incomplete",
        )
    if view.control_plane and not policy.allow_control_plane:
        return _authority_decision(
            request=request,
            surface=surface,
            policy=policy,
            mapping=mapping,
            outcome="refused",
            reason_code=_unsupported_reason(policy, "control_plane"),
            completeness="incomplete",
        )
    if view.classification_basis == "unclassifiable":
        return _authority_decision(
            request=request,
            surface=surface,
            policy=policy,
            mapping=mapping,
            outcome="refused",
            reason_code=policy.unclassifiable_reason_code,
            completeness="incomplete",
        )
    if view.classification_basis == "changed_paths_fallback":
        if not policy.allow_changed_paths_fallback:
            return _authority_decision(
                request=request,
                surface=surface,
                policy=policy,
                mapping=mapping,
                outcome="refused",
                reason_code=_unsupported_reason(policy, "classification_basis"),
                completeness="incomplete",
            )
        if surface.path_sensitive and not policy.allow_changed_paths_fallback_for_path_sensitive:
            return _authority_decision(
                request=request,
                surface=surface,
                policy=policy,
                mapping=mapping,
                outcome="refused",
                reason_code=policy.path_fallback_incomplete_reason_code,
                completeness="incomplete",
            )
        completeness: GitRepoCompleteness = "advisory"
    elif view.classification_basis in policy.allowed_classification_bases:
        completeness = "complete"
    else:
        return _authority_decision(
            request=request,
            surface=surface,
            policy=policy,
            mapping=mapping,
            outcome="refused",
            reason_code=_unsupported_reason(policy, "classification_basis"),
            completeness="incomplete",
        )

    try:
        allowed = surface.effective_match.matches(mapping)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return _authority_decision(
            request=request,
            surface=surface,
            policy=policy,
            mapping=mapping,
            outcome="refused",
            reason_code=f"{policy.match_evaluation_failed_reason_code_prefix}:{type(exc).__name__}",
            completeness="incomplete",
        )
    if allowed:
        return _authority_decision(
            request=request,
            surface=surface,
            policy=policy,
            mapping=mapping,
            outcome="allowed",
            reason_code=policy.allow_reason_code,
            completeness=completeness,
            matched=True,
        )
    if view.mutates and policy.denied_when_mutates_outside_match:
        return _authority_decision(
            request=request,
            surface=surface,
            policy=policy,
            mapping=mapping,
            outcome="denied",
            reason_code=policy.mutating_outside_match_reason_code,
            completeness=completeness,
        )
    return _authority_decision(
        request=request,
        surface=surface,
        policy=policy,
        mapping=mapping,
        outcome="refused",
        reason_code=policy.outside_match_reason_code,
        completeness=completeness,
    )


def gitrepo_authority_surface_for_grant(
    grant: GitRepoGrantDescriptor,
    *,
    label: str,
    route: str | None = None,
) -> GitRepoAuthoritySurface:
    """Lower an internal GitRepo grant descriptor into a Match surface."""
    return build_gitrepo_field_authority_surface(
        label=label,
        grant_ref=grant.grant_ref,
        grant_digest=grant.digest,
        clauses=tuple(
            constraint_clause
            for clause in grant.clauses
            for constraint_clause in clause.to_constraint_clauses(route=route)
        ),
    )


def clamp_gitrepo_grants(
    *,
    parent_ceiling: GitRepoGrantDescriptor,
    requested: GitRepoGrantDescriptor,
    grant_ref: str,
) -> GitRepoGrantClamp:
    """Intersect two internal GitRepo grants without widening authority."""
    effective_clauses: list[GitRepoGrantClause] = []
    for parent_clause in parent_ceiling.clauses:
        for requested_clause in requested.clauses:
            effective = _intersect_gitrepo_clause(parent_clause, requested_clause)
            if effective is not None and effective not in effective_clauses:
                effective_clauses.append(effective)
    effective = GitRepoGrantDescriptor(grant_ref=grant_ref, clauses=tuple(effective_clauses))
    descriptor = _grant_clamp_descriptor(parent_ceiling=parent_ceiling, requested=requested, effective=effective)
    return GitRepoGrantClamp(
        parent_ceiling=parent_ceiling,
        requested=requested,
        effective=effective,
        descriptor=descriptor,
        digest=_required_descriptor_digest(descriptor, "digest"),
    )


def _match_from_constraint_clauses(clauses: tuple[FieldConstraintClause, ...]) -> Match:
    match = Match.nothing()
    for clause in clauses:
        match = match | _match_from_constraints(clause)
    return match


def _match_from_constraints(constraints: FieldConstraintClause) -> Match:
    match = Match.all()
    for constraint in constraints:
        match = match & Match.field(
            str(constraint["field"]),
            str(constraint["op"]),
            constraint["value"],
        )
    return match


def _normalize_constraint(constraint: FieldConstraint) -> FieldConstraint:
    field = constraint.get("field")
    op = constraint.get("op")
    if not isinstance(field, str) or not field:
        raise ValueError("GitRepo authority constraint requires a field")
    if field not in _ALLOWED_FIELD_OPS:
        raise ValueError(f"GitRepo authority constraint field is unsupported: {field!r}")
    if not isinstance(op, str) or op not in _ALLOWED_FIELD_OPS[field]:
        raise ValueError(f"GitRepo authority constraint op is unsupported for {field!r}: {op!r}")
    if "value" not in constraint:
        raise ValueError("GitRepo authority constraint requires a value")
    return {"field": field, "op": op, "value": _normalize_constraint_value(field, op, constraint["value"])}


def _normalize_constraint_value(field: str, op: str, value: object) -> object:
    if field in _BOOLEAN_EQ_FIELDS:
        if not isinstance(value, bool):
            raise TypeError(f"GitRepo authority constraint {field!r} requires a boolean value")
        return value
    if field == "path":
        return _normalize_path_constraint_value(value, op=op)
    if field not in _STRING_EQ_FIELDS:
        raise ValueError(f"GitRepo authority constraint field is unsupported: {field!r}")
    if not isinstance(value, str):
        raise TypeError(f"GitRepo authority constraint {field!r} requires a string value")
    if not value:
        raise ValueError(f"GitRepo authority constraint {field!r} must not be empty")
    if "\0" in value:
        raise ValueError(f"GitRepo authority constraint {field!r} must not contain NUL")
    if field == "domain" and value != "gitrepo.v0":
        raise ValueError(f"GitRepo authority constraint domain is unsupported: {value!r}")
    if field == "classification_basis" and value not in _CLASSIFICATION_BASIS_VALUES:
        raise ValueError(f"GitRepo authority classification basis is unsupported: {value!r}")
    return value


def _normalize_path_constraint_value(value: object, *, op: str) -> str:
    if not isinstance(value, str):
        raise TypeError("GitRepo authority path constraint requires a string value")
    if "\0" in value:
        raise ValueError("GitRepo authority path constraint must not contain NUL")
    if op == "startswith":
        if not value.endswith("/"):
            raise ValueError("GitRepo authority path startswith constraint must end with '/'")
        prefix = _normalize_path_prefix(value[:-1])
        if prefix is None:
            raise ValueError("GitRepo authority path startswith constraint must not target the workspace root")
        return f"{prefix}/"
    if value == "":
        return ""
    if value == "/":
        return ""
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ValueError("GitRepo authority path constraint must be workspace-relative")
    normalized = path.as_posix()
    if normalized in {"", "."}:
        return ""
    parts = PurePosixPath(normalized).parts
    if ".." in parts:
        raise ValueError("GitRepo authority path constraint must not escape the workspace")
    return normalized


def _has_path_constraint(clauses: tuple[FieldConstraintClause, ...]) -> bool:
    return any(constraint["field"] == "path" for clause in clauses for constraint in clause)


def _required_view_value(mapping: Mapping[object, object], field: str) -> object:
    if field not in mapping:
        raise ValueError(f"GitRepo authority view requires field {field!r}")
    return mapping[field]


def _required_view_string(mapping: Mapping[object, object], field: str) -> str:
    value = _required_view_value(mapping, field)
    if not isinstance(value, str):
        raise TypeError(f"GitRepo authority view field {field!r} must be a string")
    if not value:
        raise ValueError(f"GitRepo authority view field {field!r} must not be empty")
    if "\0" in value:
        raise ValueError(f"GitRepo authority view field {field!r} must not contain NUL")
    return value


def _required_view_path(mapping: Mapping[object, object], field: str) -> str:
    value = _required_view_value(mapping, field)
    if not isinstance(value, str):
        raise TypeError("GitRepo authority view path must be a string")
    return _normalize_path_constraint_value(value, op="eq")


def _required_view_bool(mapping: Mapping[object, object], field: str) -> bool:
    value = _required_view_value(mapping, field)
    if not isinstance(value, bool):
        raise TypeError(f"GitRepo authority view field {field!r} must be a boolean")
    return value


def _required_view_classification_basis(mapping: Mapping[object, object]) -> GitRepoClassificationBasis:
    value = _required_view_string(mapping, "classification_basis")
    if value not in _CLASSIFICATION_BASIS_VALUES:
        raise ValueError(f"GitRepo authority classification basis is unsupported: {value!r}")
    return cast("GitRepoClassificationBasis", value)


def _best_effort_match_mapping(request: object) -> dict[object, object] | None:
    try:
        return GitRepoAuthorityView.from_mapping(dict(request.match_view.as_mapping())).as_mapping()  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return None


def _authority_decision(
    *,
    request: object,
    surface: GitRepoAuthoritySurface,
    policy: GitRepoAuthorityDecisionPolicy,
    mapping: dict[object, object] | None,
    outcome: AuthorityOutcome,
    reason_code: str,
    completeness: GitRepoCompleteness,
    matched: bool = False,
) -> AuthorityDecision:
    request_id = getattr(request, "request_id", None)
    monitor_basis = policy.default_monitor_basis
    if mapping is not None:
        mapped_monitor_basis = mapping.get("monitor_basis")
        if isinstance(mapped_monitor_basis, str) and mapped_monitor_basis:
            monitor_basis = mapped_monitor_basis
    matched_grant_ref = None
    if matched:
        grant_ref = surface.effective_match_descriptor.get("grant_ref")
        if isinstance(grant_ref, str) and grant_ref:
            matched_grant_ref = grant_ref
    return AuthorityDecision(
        outcome=outcome,
        reason_code=reason_code,
        request_id=request_id if isinstance(request_id, str) else None,
        matched_grant_ref=matched_grant_ref,
        monitor_basis=monitor_basis,
        completeness=completeness,
    )


def _unsupported_reason(policy: GitRepoAuthorityDecisionPolicy, field: str) -> str:
    return f"unsupported_{policy.reason_code_subject}_{field}"


def _require_non_empty_string(value: object, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")


def _intersect_gitrepo_clause(
    parent: GitRepoGrantClause,
    requested: GitRepoGrantClause,
) -> GitRepoGrantClause | None:
    if parent.binding_ref != requested.binding_ref:
        return None
    mutates = _intersect_optional_eq(parent.mutates, requested.mutates)
    if mutates is _EMPTY:
        return None
    path_prefix = _intersect_path_prefix(parent.path_prefix, requested.path_prefix)
    if path_prefix is _EMPTY:
        return None
    return GitRepoGrantClause(
        binding_ref=parent.binding_ref,
        path_prefix=path_prefix,
        mutates=mutates,
    )


_EMPTY = object()


def _intersect_optional_eq(left: bool | None, right: bool | None) -> bool | None | object:
    if left is None:
        return right
    if right is None:
        return left
    if left == right:
        return left
    return _EMPTY


def _intersect_path_prefix(left: str | None, right: str | None) -> str | None | object:
    if left is None:
        return right
    if right is None:
        return left
    if _path_prefix_contains(parent=left, child=right):
        return right
    if _path_prefix_contains(parent=right, child=left):
        return left
    return _EMPTY


def _normalize_path_prefix(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("GitRepo path_prefix must be a string or None")
    if value == "":
        raise ValueError("GitRepo path_prefix must not be empty")
    if "\0" in value:
        raise ValueError("GitRepo path_prefix must not contain NUL")
    if value == "/":
        return None
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ValueError("GitRepo path_prefix must be workspace-relative")
    normalized = path.as_posix()
    if normalized in {"", "."}:
        return None
    parts = PurePosixPath(normalized).parts
    if ".." in parts:
        raise ValueError("GitRepo path_prefix must not escape the workspace")
    return normalized


def _path_prefix_contains(*, parent: str, child: str) -> bool:
    return child == parent or child.startswith(f"{parent}/")


ReadOnly = GitRepoGrant(label="ReadOnly", mutates=False)
ReadWrite = GitRepoGrant(label="ReadWrite")


def _descriptor_digest(descriptor: dict[str, object]) -> str:
    encoded = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _grant_clamp_descriptor(
    *,
    parent_ceiling: GitRepoGrantDescriptor,
    requested: GitRepoGrantDescriptor,
    effective: GitRepoGrantDescriptor,
) -> dict[str, object]:
    descriptor = {
        "schema": _CLAMP_DESCRIPTOR_SCHEMA,
        "parent_ceiling": parent_ceiling.to_descriptor(),
        "parent_ceiling_digest": parent_ceiling.digest,
        "requested": requested.to_descriptor(),
        "requested_digest": requested.digest,
        "effective": effective.to_descriptor(),
        "effective_digest": effective.digest,
        "effective_clause_count": len(effective.clauses),
    }
    descriptor["digest"] = _descriptor_digest(descriptor)
    return descriptor


def _required_descriptor(value: Mapping[str, object], field_name: str) -> Mapping[str, object]:
    raw = value.get(field_name)
    if not isinstance(raw, Mapping):
        raise TypeError(f"GitRepo grant clamp descriptor field {field_name!r} must be an object")
    return raw


def _required_descriptor_digest(value: Mapping[str, object], field_name: str) -> str:
    raw = value.get(field_name)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"GitRepo grant clamp descriptor field {field_name!r} must be a non-empty string")
    return raw
