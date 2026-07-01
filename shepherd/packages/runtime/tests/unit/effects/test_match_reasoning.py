"""Regression coverage for the private ``Match`` reasoning core."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from shepherd_runtime.effects import Ask, Match, OverbroadHandler, Plan, Subset, Tell


@dataclass(frozen=True)
class ReasoningRoot(Tell, kind="match_reasoning.root"):
    severity: int = 0
    message: str = ""


@dataclass(frozen=True)
class ReasoningChild(ReasoningRoot, kind="match_reasoning.root.child"):
    pass


@dataclass(frozen=True)
class ReasoningUnrelatedPublicChild(Tell, kind="match_reasoning.root.unrelated"):
    severity: int = 0
    message: str = ""


@dataclass(frozen=True)
class ReasoningSibling(Tell, kind="match_reasoning.sibling"):
    severity: int = 0
    message: str = ""


@dataclass(frozen=True)
class ReasoningOther(Tell, kind="match_reasoning.other"):
    severity: int = 0
    message: str = ""


def test_explicit_match_constructors_reject_wildcard_sugar() -> None:
    with pytest.raises(ValueError, match="wildcard"):
        Match.subtree("match_reasoning.root.*")
    with pytest.raises(ValueError, match="wildcard"):
        Match.exact("match_reasoning.root.**")
    with pytest.raises(ValueError, match="wildcard"):
        Match.descendants("match_reasoning.root.**")


def test_match_of_remains_the_wildcard_sugar_entrypoint() -> None:
    assert Match.of("match_reasoning.root.*") == Match.descendants("match_reasoning.root")
    assert Match.of("match_reasoning.root.**") == Match.subtree("match_reasoning.root")


def test_dict_is_batch_install_sugar_not_matcher_form() -> None:
    with pytest.raises(TypeError, match="cannot convert dict to Match"):
        Match.of({ReasoningRoot: object()})  # type: ignore[arg-type]


def test_multi_clause_boolean_emptiness_stays_unknown_without_full_algebra() -> None:
    root = Match.exact(ReasoningRoot)
    sibling = Match.exact(ReasoningSibling)
    other = Match.exact(ReasoningOther)
    expression = root & (sibling | other)

    assert not expression.matches(ReasoningRoot())
    assert expression.is_empty() is Subset.Unknown
    assert expression.equivalent_to(Match.nothing()) is Subset.Unknown


def test_minimum_difference_fragment_is_decidable() -> None:
    root = Match.subtree(ReasoningRoot)

    assert (root - root).is_empty() is Subset.Yes
    assert (Match.nothing() - root).is_empty() is Subset.Yes
    assert (Match.all() - Match.nothing()).equivalent_to(Match.all()) is Subset.Yes


def test_unknown_not_false_no_for_opaque_reasoning() -> None:
    opaque = Match.predicate(lambda event: True)

    assert (Match.exact(ReasoningRoot) & opaque).is_empty() is Subset.Unknown


def test_opaque_predicate_complement_is_not_collapsed_by_constructor() -> None:
    calls: list[object] = []

    def predicate(event: object) -> bool:
        calls.append(event)
        return True

    opaque = Match.predicate(predicate)
    event = ReasoningRoot()

    assert opaque | ~opaque != Match.all()
    assert (opaque | ~opaque).matches(event)
    assert (opaque | ~opaque).equivalent_to(Match.all()) is Subset.Unknown
    assert len(calls) == 1

    calls.clear()
    assert opaque & ~opaque != Match.nothing()
    assert not (opaque & ~opaque).matches(event)
    assert (opaque & ~opaque).is_empty() is Subset.Unknown
    assert (opaque & ~opaque).subset_of(Match.nothing()) is Subset.Unknown
    assert (opaque & ~opaque).equivalent_to(Match.nothing()) is Subset.Unknown
    assert len(calls) == 2


def test_stateful_opaque_predicate_is_not_treated_as_formula() -> None:
    event = ReasoningRoot()
    decisions = iter((True, False))

    def contradiction_predicate(_event: object) -> bool:
        return next(decisions)

    contradiction = Match.predicate(contradiction_predicate) & ~Match.predicate(contradiction_predicate)

    assert contradiction.matches(event)
    assert contradiction.is_empty() is Subset.Unknown
    assert contradiction.subset_of(Match.nothing()) is Subset.Unknown

    decisions = iter((False, True))

    def excluded_middle_predicate(_event: object) -> bool:
        return next(decisions)

    excluded_middle = Match.predicate(excluded_middle_predicate) | ~Match.predicate(excluded_middle_predicate)

    assert not excluded_middle.matches(event)
    assert excluded_middle.equivalent_to(Match.all()) is Subset.Unknown


def test_hinted_predicate_structural_filter_runs_before_predicate() -> None:
    calls: list[object] = []

    def predicate(event: object) -> bool:
        calls.append(event)
        return True

    hinted = Match.predicate(predicate, hint=Match.exact(ReasoningRoot))

    assert not hinted.matches(ReasoningSibling())
    assert calls == []

    root = ReasoningRoot()
    assert hinted.matches(root)
    assert calls == [root]


def test_authoritative_unhinted_predicate_handler_rejects() -> None:
    with pytest.raises(OverbroadHandler):
        Plan().handle(Match.predicate(lambda event: True), lambda event: event)


def test_authoritative_predicate_handler_requires_narrow_structural_hint() -> None:
    plan = Plan().handle(
        Match.predicate(lambda event: True, hint=Match.exact(ReasoningRoot)),
        lambda event: event,
    )
    assert plan.installations()[0].kind == "handle"

    with pytest.raises(OverbroadHandler):
        Plan().handle(Match.predicate(lambda event: True, hint=Match.all()), lambda event: event)


def test_observer_accepts_opaque_predicate() -> None:
    plan = Plan().observe(Match.predicate(lambda event: True), lambda event: None)

    assert plan.installations()[0].kind == "observe"


def test_semantic_equivalence_is_not_public_structural_equality() -> None:
    class_match = Match.exact(ReasoningRoot)
    string_match = Match.exact("match_reasoning.root")

    assert class_match.equivalent_to(string_match) is Subset.Yes
    assert class_match != string_match


def test_category_and_public_kind_evidence_remain_distinct() -> None:
    assert Match.subtree(Tell).matches(ReasoningRoot())
    assert not Match.subtree("tell").matches(ReasoningRoot())
    assert Match.subtree("match_reasoning.root").subset_of(Match.subtree(Tell)) is Subset.Unknown


def test_category_root_intersection_with_public_child_kind_preserves_runtime_matching() -> None:
    tell_match = Match.subtree(Tell) & Match.exact("match_reasoning.root")
    ask_match = Match.subtree(Ask) & Match.exact("match_reasoning.ask")

    @dataclass(frozen=True)
    class ReasoningAsk(Ask[str], kind="match_reasoning.ask"):
        prompt: str = ""

    assert tell_match.matches(ReasoningRoot())
    assert tell_match.is_empty() is Subset.Unknown
    assert tell_match.subset_of(Match.nothing()) is Subset.Unknown

    assert ask_match.matches(ReasoningAsk())
    assert ask_match.is_empty() is Subset.Unknown
    assert ask_match.subset_of(Match.nothing()) is Subset.Unknown


def test_category_descendants_intersection_with_public_child_kind_preserves_runtime_matching() -> None:
    tell_match = Match.descendants(Tell) & Match.exact("match_reasoning.root")
    ask_match = Match.descendants(Ask) & Match.exact("match_reasoning.ask_descendant")

    @dataclass(frozen=True)
    class ReasoningAskDescendant(Ask[str], kind="match_reasoning.ask_descendant"):
        prompt: str = ""

    assert tell_match.matches(ReasoningRoot())
    assert tell_match.is_empty() is Subset.Unknown
    assert tell_match.subset_of(Match.nothing()) is Subset.Unknown

    assert ask_match.matches(ReasoningAskDescendant())
    assert ask_match.is_empty() is Subset.Unknown
    assert ask_match.subset_of(Match.nothing()) is Subset.Unknown


def test_public_kind_descendant_does_not_imply_python_subclass() -> None:
    class_subtree = Match.subtree(ReasoningRoot)
    public_subtree = Match.subtree("match_reasoning.root")
    unrelated_public_child = ReasoningUnrelatedPublicChild()

    assert not class_subtree.matches(unrelated_public_child)
    assert public_subtree.matches(unrelated_public_child)
    assert class_subtree.subset_of(public_subtree) is Subset.Unknown
    assert public_subtree.subset_of(class_subtree) is Subset.Unknown
    assert public_subtree.equivalent_to(class_subtree) is Subset.Unknown


def test_public_kind_exact_minus_class_subtree_is_unknown_without_registry_owner_lookup() -> None:
    match = Match.exact("match_reasoning.root.child") & ~Match.subtree(ReasoningRoot)

    assert not match.matches(ReasoningChild())
    assert match.is_empty() is Subset.Unknown
    assert match.subset_of(Match.nothing()) is Subset.Unknown


def test_public_exact_and_class_subtree_intersection_stays_unknown_without_owner_lookup() -> None:
    match = Match.exact("match_reasoning.root.unrelated") & Match.subtree(ReasoningRoot)

    assert not match.matches(ReasoningUnrelatedPublicChild())
    assert match.is_empty() is Subset.Unknown
    assert match.subset_of(Match.nothing()) is Subset.Unknown


def test_class_descendant_region_is_not_proven_against_public_exact_child() -> None:
    class_descendants = Match.subtree(ReasoningRoot) - Match.exact(ReasoningRoot)

    assert class_descendants.matches(ReasoningChild())
    assert class_descendants.subset_of(Match.exact("match_reasoning.root.child")) is Subset.Unknown


def test_negative_class_public_complements_remain_unknown_when_open_world() -> None:
    not_public_child = ~Match.exact("match_reasoning.root.child")
    not_class_descendants = ~Match.descendants(ReasoningRoot)

    assert not_public_child.subset_of(not_class_descendants) is Subset.Unknown
    assert (~Match.subtree(ReasoningRoot)).subset_of(not_public_child) is Subset.Unknown


def test_exact_and_descendants_exclusions_cover_subtree() -> None:
    not_exact_or_descendants = ~Match.exact(ReasoningRoot) & ~Match.descendants(ReasoningRoot)

    assert not_exact_or_descendants.subset_of(~Match.subtree(ReasoningRoot)) is Subset.Unknown


def test_negated_kind_clause_disjointness_uses_subset_not_disjointness() -> None:
    right = (~Match.field("severity", "eq", 0) | ~Match.field("severity", "eq", 2)) & ~Match.exact(
        ReasoningChild
    )

    assert Match.exact(ReasoningRoot).subset_of(right) is Subset.Unknown


def test_field_eq_and_finite_in_reasoning_stays_decidable() -> None:
    exact = Match.field("severity", "eq", 2)
    finite = Match.field("severity", "in", {1, 2, 3})

    assert exact.subset_of(finite) is Subset.Yes


def test_overlapping_field_conjunction_does_not_prove_false_no() -> None:
    overlapping = Match.field("severity", "in", {1, 2}) & Match.field("severity", "in", {2, 3})
    exact_overlap = Match.field("severity", "eq", 2)

    assert overlapping.matches({"severity": 2})
    assert not overlapping.matches({"severity": 1})
    assert not overlapping.matches({"severity": 3})
    assert overlapping.subset_of(exact_overlap) is Subset.Unknown


def test_nullability_field_conjunction_stays_unknown_when_not_locally_proven() -> None:
    nullable_singleton = Match.field("severity", "in", {None, 1}) & Match.field("severity", "is_none", True)
    exact_none = Match.field("severity", "eq", None)

    assert nullable_singleton.matches({"severity": None})
    assert not nullable_singleton.matches({"severity": 1})
    assert nullable_singleton.subset_of(exact_none) is Subset.Unknown


def test_empty_finite_in_field_is_bottom() -> None:
    empty = Match.field("severity", "in", set())

    assert empty.is_empty() is Subset.Yes
    assert empty.equivalent_to(Match.nothing()) is Subset.Yes
    assert not empty.matches({"severity": 2})


def test_missing_field_runtime_behavior_is_not_changed_by_reasoning_core() -> None:
    with pytest.raises(KeyError):
        Match.field("severity", "eq", 2).matches({})


@pytest.mark.parametrize(
    ("matcher", "matching_event", "nonmatching_event", "wider_matcher"),
    [
        (
            Match.field("severity", "gte", 3),
            {"severity": 4, "message": "risk found"},
            {"severity": 2, "message": "risk found"},
            Match.field("severity", "gte", 0),
        ),
        (
            Match.field("severity", "ne", 0),
            {"severity": 4, "message": "risk found"},
            {"severity": 0, "message": "risk found"},
            Match.field("severity", "not_in", {0}),
        ),
        (
            Match.field("message", "startswith", "risk"),
            {"severity": 4, "message": "risk found"},
            {"severity": 4, "message": "low risk"},
            Match.field("message", "startswith", "r"),
        ),
        (
            Match.field("message", "contains", "risk"),
            {"severity": 4, "message": "low risk"},
            {"severity": 4, "message": "safe"},
            Match.field("message", "contains", "ri"),
        ),
        (
            Match.field("message", "matches", r"risk\s+found"),
            {"severity": 4, "message": "risk found"},
            {"severity": 4, "message": "risk absent"},
            Match.field("message", "contains", "risk"),
        ),
    ],
)
def test_unsupported_field_operators_match_but_do_not_prove_containment(
    matcher: Match,
    matching_event: dict[str, object],
    nonmatching_event: dict[str, object],
    wider_matcher: Match,
) -> None:
    assert matcher.matches(matching_event)
    assert not matcher.matches(nonmatching_event)
    assert matcher.subset_of(matcher) is Subset.Yes
    assert matcher.subset_of(wider_matcher) is Subset.Unknown


def test_field_complements_do_not_collapse_without_total_field_domain() -> None:
    field = Match.field("severity", "eq", 2)

    assert field | ~field != Match.all()
    assert field & ~field != Match.nothing()
    assert (field & ~field).is_empty() is Subset.Unknown
    assert (field & ~field).subset_of(Match.nothing()) is Subset.Unknown

    with pytest.raises(KeyError):
        (field | ~field).matches({})
    with pytest.raises(KeyError):
        (field & ~field).matches({})


_SEVERITIES = (0, 2, 4, None)
_UNIVERSE = tuple(
    event_type(severity=severity, message=f"{event_type.__name__}-{severity}")  # type: ignore[arg-type]
    for event_type in (ReasoningRoot, ReasoningChild, ReasoningUnrelatedPublicChild, ReasoningSibling, ReasoningOther)
    for severity in _SEVERITIES
)


def _match_strategy() -> st.SearchStrategy[Match]:
    field_atoms = (
        Match.field("severity", "in", set()),
        Match.field("severity", "eq", 0),
        Match.field("severity", "eq", 2),
        Match.field("severity", "eq", 4),
        Match.field("severity", "eq", None),
        Match.field("severity", "in", {0, 2}),
        Match.field("severity", "in", {2, 4}),
        Match.field("severity", "in", {None}),
        Match.field("severity", "is_none", True),
        Match.field("severity", "is_not_none", True),
    )
    composite_field_atoms = tuple(
        left & right
        for left, right in (
            (Match.field("severity", "in", {0, 2}), Match.field("severity", "in", {2, 4})),
            (Match.field("severity", "in", {None}), Match.field("severity", "is_none", True)),
            (Match.field("severity", "eq", 2), Match.field("severity", "in", {2, 4})),
            (Match.field("severity", "is_not_none", True), Match.field("severity", "in", {0, 2})),
        )
    )
    atoms = st.sampled_from(
        (
            Match.all(),
            Match.nothing(),
            Match.exact(ReasoningRoot),
            Match.exact(ReasoningChild),
            Match.exact(ReasoningUnrelatedPublicChild),
            Match.exact(ReasoningSibling),
            Match.exact(ReasoningOther),
            Match.exact("match_reasoning.root"),
            Match.exact("match_reasoning.root.child"),
            Match.exact("match_reasoning.root.unrelated"),
            Match.exact("match_reasoning.sibling"),
            Match.exact("match_reasoning.other"),
            Match.subtree(ReasoningRoot),
            Match.subtree("match_reasoning.root"),
            Match.descendants(ReasoningRoot),
            Match.descendants("match_reasoning.root"),
            *field_atoms,
            *composite_field_atoms,
            *(Match.subtree(ReasoningRoot) & field for field in field_atoms),
        )
    )

    return st.recursive(
        atoms,
        lambda children: st.one_of(
            st.tuples(children, children).map(lambda pair: pair[0] | pair[1]),
            st.tuples(children, children).map(lambda pair: pair[0] & pair[1]),
            st.tuples(children, children).map(lambda pair: pair[0] - pair[1]),
            children.map(lambda child: ~child),
        ),
        max_leaves=8,
    )


def _brute_subset(left: Match, right: Match) -> bool:
    return all(not left.matches(event) or right.matches(event) for event in _UNIVERSE)


def _brute_empty(match: Match) -> bool:
    return not any(match.matches(event) for event in _UNIVERSE)


@settings(max_examples=300, deadline=None)
@given(left=_match_strategy(), right=_match_strategy())
def test_subset_yes_no_results_are_sound_over_representative_finite_universe(left: Match, right: Match) -> None:
    result = left.subset_of(right)
    brute = _brute_subset(left, right)

    if result is Subset.Yes:
        assert brute
    elif result is Subset.No:
        assert not brute


@settings(max_examples=300, deadline=None)
@given(match=_match_strategy())
def test_empty_yes_no_results_are_sound_over_representative_finite_universe(match: Match) -> None:
    result = match.is_empty()
    brute = _brute_empty(match)

    if result is Subset.Yes:
        assert brute
    elif result is Subset.No:
        assert not brute
