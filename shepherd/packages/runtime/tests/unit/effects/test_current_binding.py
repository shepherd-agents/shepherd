"""Contract-import test for C4 ``current_binding(T)``.

Satisfies CONTRACTS Maintenance Rule 3 for C4 by importing
``current_binding`` from ``shepherd_runtime.scope`` and exercising
the type-keyed lookup against real Scope bindings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

import pytest
from shepherd_core.context.kernel import ExecutionContextDefaults
from shepherd_core.types import ReversibilityLevel
from shepherd_runtime.scope import (
    AmbiguousBindingError,
    NoBindingForTypeError,
    Scope,
    current_binding,
)


@dataclass
class _BankingContext(ExecutionContextDefaults):
    """Test context that satisfies the ExecutionContext protocol.

    Until name-keyed ``scope.bind`` is deleted (DECISIONS D5), test
    contexts carry a ``__binding_name__`` and a minimal
    ``ExecutionContext`` lifecycle surface.
    """

    account_id: str = "ACC-001"
    transfer_limit: float = 0.0
    __binding_name__ = "banking"

    @property
    def context_id(self) -> str:
        return f"banking:{self.account_id}"

    @property
    def reversibility(self) -> ReversibilityLevel:
        return ReversibilityLevel.NONE

    def apply_effect(self, effect: object) -> Self:  # pragma: no cover - test stub
        return self

    def describe_limit(self) -> str:
        return f"{self.account_id}:{self.transfer_limit:g}"


@dataclass
class _SessionState(ExecutionContextDefaults):
    user: str = ""
    __binding_name__ = "session"

    @property
    def context_id(self) -> str:
        return f"session:{self.user}"

    @property
    def reversibility(self) -> ReversibilityLevel:
        return ReversibilityLevel.NONE

    def apply_effect(self, effect: object) -> Self:  # pragma: no cover - test stub
        return self


def test_current_binding_imports_from_runtime_scope() -> None:
    from shepherd_runtime.scope import (
        AmbiguousBindingError,
        NoBindingForTypeError,
        current_binding,
    )

    assert current_binding.__name__ == "current_binding"
    assert issubclass(AmbiguousBindingError, LookupError)
    assert issubclass(NoBindingForTypeError, LookupError)


def test_current_binding_finds_exact_type() -> None:
    with Scope(root=True) as scope:
        bc = _BankingContext(account_id="ACC-001", transfer_limit=10_000)
        scope.bind(bc)
        ref = current_binding(_BankingContext)
        assert ref.value is bc
        assert ref.account_id == "ACC-001"
        assert ref.transfer_limit == 10_000
        assert ref.describe_limit() == "ACC-001:10000"


def test_current_binding_raises_no_binding_for_type_on_miss() -> None:
    with Scope(root=True) as scope:
        scope.bind(_SessionState(user="alice"))
        with pytest.raises(NoBindingForTypeError):
            current_binding(_BankingContext)


def test_context_ref_re_resolves_on_access() -> None:
    """The ContextRef re-resolves on each attribute access.

    CONTRACTS C4 promises that the returned ``ContextRef`` "delegates
    attribute access to the current value; rebinding in the same Scope
    updates the reference transparently." This test exercises the
    re-resolution path against a single, stable binding (production
    rebinding semantics depend on the eventual deletion of name-keyed
    bind per DECISIONS D5; that's Tranche 7+ work). Re-resolving on
    every access is what the stub commits to today.
    """
    with Scope(root=True) as scope:
        ctx = _BankingContext(account_id="A", transfer_limit=1)
        scope.bind(ctx)
        ref = current_binding(_BankingContext)
        assert ref.account_id == "A"
        # Same ref re-resolves on each access.
        assert ref.value is ctx
        assert ref.account_id == "A"


def test_context_ref_sees_updated_context_for_same_name() -> None:
    with Scope(root=True) as scope:
        first = _BankingContext(account_id="A", transfer_limit=1)
        second = _BankingContext(account_id="B", transfer_limit=2)
        scope.bind("banking", first)
        ref = current_binding(_BankingContext)

        scope.update_context("banking", second)

        assert ref.value is second
        assert ref.account_id == "B"
        assert ref.describe_limit() == "B:2"


def test_current_binding_raises_ambiguous_binding_for_same_depth_matches() -> None:
    with Scope(root=True) as scope:
        scope.bind("primary", _BankingContext(account_id="A"))
        scope.bind("secondary", _BankingContext(account_id="B"))

        with pytest.raises(AmbiguousBindingError):
            current_binding(_BankingContext)


def test_no_active_scope_raises_no_binding_for_type() -> None:
    """Without an active Scope, the lookup chain is empty."""
    with pytest.raises(NoBindingForTypeError):
        current_binding(_BankingContext)


def test_bind_type_keyed_form_round_trips_through_current_binding() -> None:
    """CONTRACTS C5: ``scope.bind(T, value)`` registers under type ``T``.

    The type-keyed registration is the explicit-form counterpart of
    the bare ``scope.bind(value)`` (which infers ``T`` from
    ``type(value)``). ``current_binding(T)`` resolves both identically
    because it matches on context type, not registry name.
    """
    with Scope(root=True) as scope:
        bc = _BankingContext(account_id="ACC-007", transfer_limit=500)
        scope.bind(_BankingContext, bc)
        ref = current_binding(_BankingContext)
        assert ref.value is bc
        assert ref.account_id == "ACC-007"


def test_bind_type_keyed_form_rejects_value_of_wrong_type() -> None:
    """``bind(T, value)`` requires ``isinstance(value, T)``."""
    with (
        Scope(root=True) as scope,
        pytest.raises(TypeError, match=r"_BankingContext.*not a _BankingContext"),
    ):
        scope.bind(_BankingContext, _SessionState(user="alice"))


def test_bind_type_keyed_and_name_keyed_forms_coexist_at_same_depth() -> None:
    """Until name-keyed bind is deleted (D5), both forms must coexist.

    Two distinct contexts of different types, registered via different
    bind shapes, both resolve through ``current_binding(T)`` without
    ambiguity.
    """
    with Scope(root=True) as scope:
        bc = _BankingContext(account_id="ACC-X", transfer_limit=1)
        ss = _SessionState(user="bob")
        scope.bind(_BankingContext, bc)
        scope.bind("session", ss)

        assert current_binding(_BankingContext).value is bc
        assert current_binding(_SessionState).value is ss


def test_bind_rejects_non_string_non_type_first_argument() -> None:
    """``bind(<not-str>, value)`` and ``bind(<not-type>, value)`` raise."""
    with (
        Scope(root=True) as scope,
        pytest.raises(TypeError, match="must be a string name or a type"),
    ):
        scope.bind(42, _BankingContext())  # type: ignore[arg-type]


def test_bind_rejects_name_with_reserved_type_prefix() -> None:
    """Name-keyed ``bind`` rejects names starting with ``__type__:``.

    The ``__type__:`` namespace is owned by the synthetic registry slot
    used by the type-keyed form (CONTRACTS C5 / DECISIONS D2). User-side
    name-keyed binds must not collide with it.
    """
    bc = _BankingContext()
    with Scope(root=True) as scope:
        with pytest.raises(ValueError, match="reserved prefix"):
            scope.bind("__type__:foo.Bar", bc)
        with pytest.raises(ValueError, match="reserved prefix"):
            scope.bind("__type__:", bc)
