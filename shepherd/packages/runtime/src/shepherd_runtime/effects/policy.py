"""Path-A effect-surface policy values.

This module owns the small structural ``Match`` / ``Plan`` core used by the
pre-launch conformance cut. It deliberately does not implement live
``run.control`` amendment, P-004 authority modes, YAML/schema loaders, or
handle-grant enforcement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

from shepherd_runtime.effects._match_model import (
    FieldPredicate as _FieldPredicate,
)
from shepherd_runtime.effects._match_model import (
    KindPattern as _KindPattern,
)
from shepherd_runtime.effects._match_model import (
    Node as _Node,
)
from shepherd_runtime.effects._match_model import (
    Predicate as _Predicate,
)
from shepherd_runtime.effects._match_model import (
    Subset,
)
from shepherd_runtime.effects._match_reasoning import (
    kind_pattern_subset as _reasoning_kind_subset,
)
from shepherd_runtime.effects._match_reasoning import (
    kind_patterns_disjoint as _reasoning_kind_disjoint,
)
from shepherd_runtime.effects._match_reasoning import (
    match_equivalent as _reasoning_equivalent,
)
from shepherd_runtime.effects._match_reasoning import (
    match_is_empty as _reasoning_is_empty,
)
from shepherd_runtime.effects._match_reasoning import (
    match_is_overbroad as _reasoning_is_overbroad,
)
from shepherd_runtime.effects._match_reasoning import (
    match_subset as _reasoning_subset,
)
from shepherd_runtime.effects.effect_kind import (
    effect_key_for_class,
    effect_key_for_event,
    parse_matcher_kind_sugar,
    tool_kind,
)

__all__ = [
    "EffectNotPermitted",
    "EffectSurfaceEmpty",
    "EffectSurfaceTooWide",
    "Installation",
    "Match",
    "OverbroadHandler",
    "Plan",
    "PlanNotExtractable",
    "Subset",
]

T = TypeVar("T")


class Match:
    """Immutable structural matcher for Path-A effect surfaces."""

    __slots__ = ("_node",)

    def __init__(self, node: _Node) -> None:
        self._node = _normalize(node)

    @classmethod
    def all(cls) -> Match:
        return cls(_Node("all"))

    @classmethod
    def nothing(cls) -> Match:
        return cls(_Node("nothing"))

    @classmethod
    def exact(cls, kind_or_class: str | type[Any]) -> Match:
        return cls(_Node("kind", (_kind_pattern("exact", kind_or_class),)))

    @classmethod
    def subtree(cls, kind_or_class: str | type[Any]) -> Match:
        return cls(_Node("kind", (_kind_pattern("subtree", kind_or_class),)))

    @classmethod
    def descendants(cls, kind_or_class: str | type[Any]) -> Match:
        return cls(_Node("kind", (_kind_pattern("descendants", kind_or_class),)))

    @classmethod
    def predicate(cls, fn: object, hint: Match | None = None) -> Match:
        base = cls(_Node("predicate", (_Predicate(fn),)))
        if hint is None:
            return base
        return base & hint

    @classmethod
    def field(cls, name: str, op: str, value: Any) -> Match:
        return cls(_Node("field", (_field_predicate(name=name, op=op, value=value),)))

    @classmethod
    def of(cls, value: MatcherForm) -> Match:
        if isinstance(value, Match):
            return value
        if isinstance(value, str):
            mode, kind = parse_matcher_kind_sugar(value)
            if mode == "subtree":
                return cls.subtree(kind)
            if mode == "descendants":
                return cls.descendants(kind)
            return cls.exact(kind)
        if isinstance(value, type):
            return cls.subtree(value)
        if isinstance(value, set | frozenset):
            result = cls.nothing()
            for item in value:
                result = result | cls.of(cast("MatcherForm", item))
            return result
        raise TypeError(f"cannot convert {type(value).__name__} to Match")

    def where(self, **constraints: Any) -> Match:
        result = self
        for raw_name, value in constraints.items():
            name, op = _split_field_operator(raw_name)
            result = result & self.field(name, op, value)
        return result

    def where_not(self, **constraints: Any) -> Match:
        return self - self.where(**constraints)

    def matches(self, event: object) -> bool:
        return _matches(self._node, event)

    def subset_of(self, other: Match) -> Subset:
        return _reasoning_subset(self._node, Match.of(other)._node)

    def equivalent_to(self, other: Match) -> Subset:
        return _reasoning_equivalent(self._node, Match.of(other)._node)

    def is_empty(self) -> Subset:
        return _reasoning_is_empty(self._node)

    def canonical(self) -> Match:
        return self

    def __or__(self, other: MatcherForm) -> Match:
        return Match(_Node("or", (self._node, Match.of(other)._node)))

    def __and__(self, other: MatcherForm) -> Match:
        return Match(_Node("and", (self._node, Match.of(other)._node)))

    def __sub__(self, other: MatcherForm) -> Match:
        return self & ~Match.of(other)

    def __invert__(self) -> Match:
        return Match(_Node("not", (self._node,)))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Match) and self._node == other._node

    def __hash__(self) -> int:
        return hash(self._node)

    def __repr__(self) -> str:
        return _repr_node(self._node)


MatcherForm = Match | str | type | set[object] | frozenset[object]


@dataclass(frozen=True)
class Installation:
    """A structural Plan installation."""

    kind: str
    matcher: Match
    ref: str | None = None
    fn: object | None = None


class Plan(Generic[T]):
    """Immutable Path-A policy plan.

    The pre-launch core supports permission stacking, composition, and
    introspection. Runtime activation and live amendment remain outside this
    structural value.
    """

    __slots__ = ("_installations",)

    def __init__(self, installations: tuple[Installation, ...] = ()) -> None:
        self._installations = installations

    def allow_only(self, matcher: MatcherForm, ref: str | None = None) -> Plan[T]:
        return self._append(Installation("allow_only", Match.of(matcher), ref=ref))

    def deny_kind(self, matcher: MatcherForm, ref: str | None = None) -> Plan[T]:
        return self._append(Installation("deny_kind", Match.of(matcher), ref=ref))

    def deny_tool(self, name: str, ref: str | None = None) -> Plan[T]:
        tool_kind(name)
        return self._append(Installation("deny_tool", Match.field("tool_name", "eq", name), ref=ref))

    def handle(self, matcher: MatcherForm, fn: object, ref: str | None = None) -> Plan[T]:
        match = Match.of(matcher)
        if _is_overbroad(match):
            raise OverbroadHandler(match)
        return self._append(Installation("handle", match, ref=ref, fn=fn))

    def observe(self, matcher: MatcherForm, fn: object, ref: str | None = None) -> Plan[T]:
        return self._append(Installation("observe", Match.of(matcher), ref=ref, fn=fn))

    def installations(self) -> tuple[Installation, ...]:
        return self._installations

    def installation(self, ref: str) -> Installation:
        for installation in self._installations:
            if installation.ref == ref:
                return installation
        raise KeyError(ref)

    def effective_surface(self) -> Match:
        allow: Match | None = None
        deny = Match.nothing()
        for installation in self._installations:
            if installation.kind == "allow_only":
                allow = installation.matcher if allow is None else allow & installation.matcher
            elif installation.kind == "deny_kind":
                deny = deny | installation.matcher
        surface = Match.all() if allow is None else allow
        return surface - deny

    def subset_of(self, other: Plan[Any]) -> Subset:
        return self.effective_surface().subset_of(other.effective_surface())

    def extract_may(self) -> Match:
        if not any(installation.kind == "allow_only" for installation in self._installations):
            raise PlanNotExtractable(self)
        return self.effective_surface()

    def _append(self, installation: Installation) -> Plan[T]:
        return Plan[T]((*self._installations, installation))

    def __or__(self, other: Plan[Any]) -> Plan[T]:
        if not isinstance(other, Plan):
            return NotImplemented
        return Plan[T]((*self._installations, *other._installations))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Plan) and self._installations == other._installations

    def __hash__(self) -> int:
        return hash(self._installations)

    def __repr__(self) -> str:
        return f"Plan(installations={self._installations!r})"


class EffectNotPermitted(Exception):  # noqa: N818
    """A task attempted an effect outside its effective surface."""

    def __init__(
        self,
        *,
        task: object | None = None,
        declared: Match | None = None,
        effective: Match | None = None,
        attempted: object | None = None,
        attempted_kind: str | None = None,
    ) -> None:
        self.task = task
        self.declared = declared
        self.effective = effective
        self.attempted = attempted
        self.attempted_kind = attempted_kind or _event_kind(attempted)
        super().__init__(f"effect kind {self.attempted_kind!r} is not permitted by {effective!r}")


class EffectSurfaceTooWide(Exception):  # noqa: N818
    """A child task's declared surface is wider than the caller's surface."""

    def __init__(
        self,
        *,
        caller: object | None = None,
        callee: object | None = None,
        caller_may: Match,
        callee_may: Match,
    ) -> None:
        self.caller = caller
        self.callee = callee
        self.caller_may = caller_may
        self.callee_may = callee_may
        self.excess = callee_may - caller_may
        super().__init__(f"callee surface {callee_may!r} is wider than caller surface {caller_may!r}")


class EffectSurfaceEmpty(Exception):  # noqa: N818
    """An effective surface admits no effects."""

    def __init__(
        self,
        *,
        task: object | None = None,
        declared: Match | None = None,
        policy_chain: tuple[Plan[Any], ...] = (),
        reason: str = "effective surface is empty",
    ) -> None:
        self.task = task
        self.declared = declared
        self.policy_chain = policy_chain
        self.reason = reason
        super().__init__(reason)


class PlanNotExtractable(ValueError):  # noqa: N818
    """A ``Plan`` used as ``may=`` lacks an ``allow_only`` surface."""

    def __init__(self, plan: Plan[Any], *, task: object | None = None) -> None:
        self.task = task
        self.plan = plan
        self.reason = (
            "Plan used as may= must declare an allow_only; add .allow_only(...) or pass a Match value directly."
        )
        super().__init__(self.reason)


class OverbroadHandler(ValueError):  # noqa: N818
    """An authoritative handler was installed over an overbroad matcher."""

    def __init__(self, matcher: Match, *, install_site: str = "unknown") -> None:
        self.matcher = matcher
        self.canonical_form = matcher.canonical()
        self.install_site = install_site
        super().__init__(
            f"OverbroadHandler at {install_site}: handle({matcher!r}, ...) installed with an overbroad matcher; "
            "use a narrower matcher or observe(...) for broad taps."
        )


_OPS = {
    "eq",
    "ne",
    "lt",
    "lte",
    "gt",
    "gte",
    "in",
    "not_in",
    "startswith",
    "endswith",
    "is_none",
    "is_not_none",
    "contains",
    "matches",
}


def _kind_pattern(mode: str, kind_or_class: str | type[Any]) -> _KindPattern:
    if isinstance(kind_or_class, str):
        sugar_mode, kind = parse_matcher_kind_sugar(kind_or_class)
        if sugar_mode != "exact":
            raise ValueError(
                f"wildcard matcher sugar {kind_or_class!r} is accepted only by Match.of(...), not Match.{mode}(...)"
            )
        return _KindPattern(mode=mode, kind=kind)
    if not isinstance(kind_or_class, type):
        raise TypeError(f"expected effect kind string or class; got {type(kind_or_class).__name__}")
    kind = effect_key_for_class(kind_or_class)
    return _KindPattern(mode=mode, kind=kind, cls=kind_or_class)


def _field_predicate(*, name: str, op: str, value: Any) -> _FieldPredicate:
    if not name or "__" in name:
        raise ValueError(f"field name must be a top-level field; got {name!r}")
    op = "eq" if op in {"", "eq"} else op
    if op not in _OPS:
        raise ValueError(f"unknown Match field operator {op!r}")
    if op in {"is_none", "is_not_none"} and value is not True:
        raise ValueError(f"{op} expects True as its value")
    if op in {"in", "not_in"} and isinstance(value, str):
        raise TypeError(f"{op} expects a finite non-string iterable")
    if op in {"in", "not_in"}:
        value = frozenset(value)
    return _FieldPredicate(name=name, op=op, value=value)


def _split_field_operator(raw_name: str) -> tuple[str, str]:
    for op in sorted(_OPS, key=len, reverse=True):
        suffix = f"__{op}"
        if raw_name.endswith(suffix):
            return raw_name[: -len(suffix)], op
    return raw_name, "eq"


def _normalize(node: _Node) -> _Node:
    if node.tag in {"all", "nothing", "kind", "field", "predicate"}:
        return node
    if node.tag == "not":
        inner = _normalize(node.args[0])  # type: ignore[arg-type]
        if inner.tag == "all":
            return _Node("nothing")
        if inner.tag == "nothing":
            return _Node("all")
        if inner.tag == "not":
            return inner.args[0]  # type: ignore[return-value]
        if _contains_predicate(inner):
            return _Node("not", (inner,))
        if inner.tag == "or":
            return _normalize(_Node("and", tuple(_Node("not", (term,)) for term in inner.args)))
        if inner.tag == "and":
            return _normalize(_Node("or", tuple(_Node("not", (term,)) for term in inner.args)))
        return _Node("not", (inner,))
    if node.tag in {"or", "and"}:
        terms: list[_Node] = []
        for raw in node.args:
            term = _normalize(raw)  # type: ignore[arg-type]
            if node.tag == "or" and term.tag == "all":
                return _Node("all")
            if node.tag == "and" and term.tag == "nothing":
                return _Node("nothing")
            if node.tag == "or" and term.tag == "nothing":
                continue
            if node.tag == "and" and term.tag == "all":
                continue
            if term.tag == node.tag:
                terms.extend(term.args)  # type: ignore[arg-type]
            else:
                terms.append(term)
        if any(_contains_predicate(term) for term in terms):
            return _normalize_predicate_mixed_boolean(node.tag, tuple(terms))
        unique = tuple(sorted(set(terms), key=_sort_key))
        if not unique:
            return _Node("nothing" if node.tag == "or" else "all")
        if len(unique) == 1:
            return unique[0]
        if node.tag == "or":
            negatives = {term.args[0] for term in unique if term.tag == "not"}
            if any(term in negatives and not _contains_field_predicate(term) for term in unique):
                return _Node("all")
        if node.tag == "and":
            negatives = {term.args[0] for term in unique if term.tag == "not"}
            if any(term in negatives and not _contains_field_predicate(term) for term in unique):
                return _Node("nothing")
            if _has_incompatible_kind_terms(unique):
                return _Node("nothing")
            absorbed = _absorb_and(unique)
            if len(absorbed) == 1:
                return absorbed[0]
            return _Node(node.tag, absorbed)
        absorbed = _absorb_or(unique)
        if len(absorbed) == 1:
            return absorbed[0]
        return _Node(node.tag, absorbed)
    raise ValueError(f"unknown Match node tag {node.tag!r}")


def _normalize_predicate_mixed_boolean(tag: str, terms: tuple[_Node, ...]) -> _Node:
    kept: list[_Node] = []
    for term in terms:
        if tag == "or" and term.tag == "nothing":
            continue
        if tag == "and" and term.tag == "all":
            continue
        if tag == "or" and term.tag == "all":
            return _Node("all")
        if tag == "and" and term.tag == "nothing":
            return _Node("nothing")
        kept.append(term)
    if not kept:
        return _Node("nothing" if tag == "or" else "all")
    if len(kept) == 1:
        return kept[0]
    return _Node(tag, tuple(kept))


def _contains_predicate(node: _Node) -> bool:
    if node.tag == "predicate":
        return True
    return any(isinstance(arg, _Node) and _contains_predicate(arg) for arg in node.args)


def _contains_field_predicate(node: _Node) -> bool:
    if node.tag == "field":
        return True
    return any(isinstance(arg, _Node) and _contains_field_predicate(arg) for arg in node.args)


def _sort_key(node: _Node) -> str:
    return _repr_node(node)


def _has_incompatible_kind_terms(terms: tuple[_Node, ...]) -> bool:
    kind_terms = [term.args[0] for term in terms if term.tag == "kind"]
    for index, left in enumerate(kind_terms):
        for right in kind_terms[index + 1 :]:
            if isinstance(left, _KindPattern) and isinstance(right, _KindPattern) and _kind_disjoint(left, right):
                return True
    negative_kind_terms = [
        term.args[0].args[0]
        for term in terms
        if term.tag == "not" and isinstance(term.args[0], _Node) and term.args[0].tag == "kind"
    ]
    for kind_term in kind_terms:
        for negative in negative_kind_terms:
            if (
                isinstance(kind_term, _KindPattern)
                and isinstance(negative, _KindPattern)
                and _kind_subset(kind_term, negative) is Subset.Yes
            ):
                return True
    return False


def _absorb_and(terms: tuple[_Node, ...]) -> tuple[_Node, ...]:
    kept: list[_Node] = []
    for term in terms:
        if term.tag == "or" and any(other in term.args for other in terms if other is not term):
            continue
        kept.append(term)
    return tuple(kept)


def _absorb_or(terms: tuple[_Node, ...]) -> tuple[_Node, ...]:
    kept: list[_Node] = []
    for term in terms:
        if term.tag == "and" and any(other in term.args for other in terms if other is not term):
            continue
        kept.append(term)
    return tuple(kept)


def _matches(node: _Node, event: object) -> bool:
    if node.tag == "all":
        return True
    if node.tag == "nothing":
        return False
    if node.tag == "kind":
        return _kind_matches(node.args[0], event)  # type: ignore[arg-type]
    if node.tag == "field":
        return _field_matches(node.args[0], event)  # type: ignore[arg-type]
    if node.tag == "predicate":
        predicate = node.args[0]
        return bool(predicate.fn(event))  # type: ignore[attr-defined]
    if node.tag == "or":
        return any(_matches(term, event) for term in node.args)  # type: ignore[arg-type]
    if node.tag == "and":
        terms = tuple(term for term in node.args if isinstance(term, _Node))
        structural_terms = tuple(term for term in terms if not _contains_predicate(term))
        opaque_terms = tuple(term for term in terms if _contains_predicate(term))
        return all(_matches(term, event) for term in (*structural_terms, *opaque_terms))
    if node.tag == "not":
        return not _matches(node.args[0], event)  # type: ignore[arg-type]
    raise ValueError(f"unknown Match node tag {node.tag!r}")


def _kind_matches(pattern: _KindPattern, event: object) -> bool:
    if pattern.cls is not None:
        if pattern.mode == "exact":
            return type(event) is pattern.cls
        if pattern.mode == "subtree":
            return isinstance(event, pattern.cls)
        return isinstance(event, pattern.cls) and type(event) is not pattern.cls
    kind = _event_kind(event)
    if pattern.mode == "exact":
        return kind == pattern.kind
    if pattern.mode == "subtree":
        return kind == pattern.kind or kind.startswith(f"{pattern.kind}.")
    return kind.startswith(f"{pattern.kind}.") and kind != pattern.kind


def _field_matches(predicate: _FieldPredicate, event: object) -> bool:
    value = _read_field(event, predicate.name)
    expected = predicate.value
    if predicate.op == "eq":
        return bool(value == expected)
    if predicate.op == "ne":
        return bool(value != expected)
    if predicate.op == "lt":
        return bool(value < expected)
    if predicate.op == "lte":
        return bool(value <= expected)
    if predicate.op == "gt":
        return bool(value > expected)
    if predicate.op == "gte":
        return bool(value >= expected)
    if predicate.op == "in":
        return bool(value in expected)
    if predicate.op == "not_in":
        return bool(value not in expected)
    if predicate.op == "startswith":
        return isinstance(value, str) and value.startswith(expected)
    if predicate.op == "endswith":
        return isinstance(value, str) and value.endswith(expected)
    if predicate.op == "is_none":
        return value is None
    if predicate.op == "is_not_none":
        return value is not None
    if predicate.op == "contains":
        return expected in value
    if predicate.op == "matches":
        return isinstance(value, str) and re.search(expected, value) is not None
    raise ValueError(f"unknown Match field operator {predicate.op!r}")


def _read_field(event: object, name: str) -> Any:
    if isinstance(event, dict):
        return event[name]
    return getattr(event, name)


def _kind_subset(left: _KindPattern, right: _KindPattern) -> Subset:
    return _reasoning_kind_subset(left, right)


def _kind_disjoint(left: _KindPattern, right: _KindPattern) -> bool:
    return _reasoning_kind_disjoint(left, right)


def _kind_matches_pattern(pattern: _KindPattern, kind: str) -> bool:
    if pattern.mode == "exact":
        return kind == pattern.kind
    if pattern.mode == "subtree":
        return kind == pattern.kind or kind.startswith(f"{pattern.kind}.")
    return kind.startswith(f"{pattern.kind}.") and kind != pattern.kind


def _kind_root_contains(parent: str, child: str) -> bool:
    return child == parent or child.startswith(f"{parent}.")


def _has_public_kind(pattern: _KindPattern) -> bool:
    return not pattern.kind.startswith("local.")


def _event_kind(event: object | None) -> str:
    if event is None:
        return "<unknown>"
    return effect_key_for_event(event)


def _is_overbroad(match: Match) -> bool:
    return _reasoning_is_overbroad(match._node)


def _repr_node(node: _Node) -> str:
    if node.tag in {"all", "nothing"}:
        return f"Match.{node.tag}()"
    if node.tag == "kind":
        pattern = node.args[0]
        return f"Match.{pattern.mode}({pattern.kind!r})"  # type: ignore[attr-defined]
    if node.tag == "field":
        predicate = node.args[0]
        return f"Match.field({predicate.name!r}, {predicate.op!r}, {predicate.value!r})"  # type: ignore[attr-defined]
    if node.tag == "predicate":
        return "Match.predicate(<fn>)"
    if node.tag == "not":
        return f"~{_repr_node(node.args[0])}"  # type: ignore[arg-type]
    sep = " | " if node.tag == "or" else " & "
    return "(" + sep.join(_repr_node(term) for term in node.args) + ")"  # type: ignore[arg-type]
