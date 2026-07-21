# under-test: vcs_core._substrate_driver
"""Unit tests for ``BaseSubstrateDriver`` (SPI v0.1 Phase A.1).

Validates that the mixin's default implementations behave per the SPI
doc §Proposed SPI Shape:

- ``capture_adapters(context)`` returns ``()``.
- ``validate_result(request, result)`` returns ``None``.
- Minimal subclass that overrides identity fields and implements
  ``prepare`` / ``describe`` / ``capabilities`` structurally satisfies
  the ``SubstrateDriver`` Protocol.

T3-final removed the legacy ``prepare_command`` Protocol method and the
mixin's default delegation to ``prepare``. Tests that exercised the
delegation chain were removed alongside the method.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Annotated

import pytest
from vcs_core._command_contract import CommandContractError
from vcs_core._driver_schema_validation import validate_driver_schema, validate_projectable_command
from vcs_core._substrate_driver import (
    BaseSubstrateDriver,
    CapabilitySet,
    CaptureAdapter,
    CommandRequest,
    CommandSpec,
    Diagnostic,
    DriverContext,
    DriverIngressResult,
    DriverSchema,
    IngressRequest,
    ParamSpec,
    ReduceRequest,
    ReductionBatch,
    SubstrateDriver,
    TransitionDraft,
    UnsupportedRequestError,
    command,
)
from vcs_core.spi import SubstrateStoreIdentity


@dataclass(frozen=True)
class _MinimalDriver(BaseSubstrateDriver):
    """Smallest valid subclass for exercising the mixin defaults."""

    driver_id: str = "test.minimal"
    driver_version: str = "v1"
    store_id: str = "store_test_minimal"
    binding: str = "minimal"
    role: str = "test.Minimal"

    @property
    def capabilities(self) -> CapabilitySet:
        return CapabilitySet(accepts=frozenset({CommandRequest}))

    def describe(self) -> DriverSchema:
        return DriverSchema(
            driver_id=self.driver_id,
            driver_version=self.driver_version,
            capabilities=self.capabilities,
            commands={
                "noop": CommandSpec(
                    description="No-op command for tests.",
                    params={"payload": ParamSpec(type="object")},
                ),
            },
        )

    def prepare(
        self,
        context: DriverContext,
        request: IngressRequest,
    ) -> DriverIngressResult:
        if isinstance(request, CommandRequest) and request.command == "noop":
            return DriverIngressResult(
                transitions=(
                    TransitionDraft(
                        transition_id="primary",
                        semantic_op="noop",
                        payload=dict(request.params),
                        observation_ids=(),
                    ),
                ),
            )
        # Anything else is unsupported; the coordinator's
        # capabilities-as-runtime-contract check would normally reject
        # this before reaching the driver, but the defensive path is
        # exercised directly by these tests.
        raise NotImplementedError(f"_MinimalDriver does not handle {type(request).__name__}")


def _context() -> DriverContext:
    return DriverContext(
        operation_id="op_test",
        binding="minimal",
        role="test.Minimal",
        store_identity=SubstrateStoreIdentity(
            store_id="store_test_minimal",
            kind="test.minimal",
            resource_id="minimal:test",
        ),
    )


class _PythonOnly:
    pass


def test_capture_adapters_defaults_to_empty_tuple() -> None:
    """Subclasses that don't override capture_adapters get an empty tuple."""
    driver = _MinimalDriver()
    adapters: tuple[CaptureAdapter, ...] = driver.capture_adapters(_context())
    assert adapters == ()


def test_validate_result_defaults_to_no_op() -> None:
    """Subclasses without semantic rules return None unconditionally."""
    driver = _MinimalDriver()
    result = DriverIngressResult(
        transitions=(
            TransitionDraft(
                transition_id="primary",
                semantic_op="noop",
                payload={},
                observation_ids=(),
            ),
        ),
    )
    assert driver.validate_result(CommandRequest(command="noop", params={}), result) is None


def test_minimal_subclass_satisfies_substrate_driver_protocol() -> None:
    """Structural conformance check via @runtime_checkable Protocol.

    The mixin itself doesn't define prepare / describe / capabilities, so a
    bare BaseSubstrateDriver() instance would not satisfy the Protocol —
    but any minimal subclass that adds the three required hooks does.
    """
    driver = _MinimalDriver()
    assert isinstance(driver, SubstrateDriver)


def test_subclass_can_override_capture_adapters() -> None:
    """Overriding capture_adapters works as expected for substrates that need it."""

    @dataclass(frozen=True)
    class _DriverWithCaptureAdapter(_MinimalDriver):
        def capture_adapters(self, context: DriverContext) -> tuple[CaptureAdapter, ...]:
            del context
            return ()  # In real usage would return concrete adapter instances.

    driver = _DriverWithCaptureAdapter()
    assert driver.capture_adapters(_context()) == ()


def test_subclass_can_override_validate_result() -> None:
    """Subclasses with semantic rules raise InvalidRepositoryStateError."""
    from vcs_core import InvalidRepositoryStateError

    @dataclass(frozen=True)
    class _StrictDriver(_MinimalDriver):
        def validate_result(
            self,
            request: IngressRequest,
            result: DriverIngressResult,
        ) -> None:
            del request
            if not result.transitions:
                raise InvalidRepositoryStateError("test driver requires at least one transition")

    driver = _StrictDriver()
    empty_result = DriverIngressResult()
    with pytest.raises(InvalidRepositoryStateError, match="at least one transition"):
        driver.validate_result(CommandRequest(command="noop", params={}), empty_result)


# T3-final: test_prepare_command_delegates_to_typed_prepare_via_command_request
# and test_subclass_can_override_prepare_command_for_legacy_semantics removed.
# The legacy ``prepare_command`` Protocol method (and its
# BaseSubstrateDriver default that delegated to ``prepare`` via
# ``CommandRequest``) was removed; the typed ``prepare(IngressRequest)``
# method is the canonical contract. Subclasses that need command-shaped
# semantics implement them inside their typed ``prepare`` match dispatch.


def test_identity_fields_have_empty_string_defaults_on_base() -> None:
    """The bare BaseSubstrateDriver() has empty identity fields, by design.

    Subclasses override via dataclass field redeclaration. The empty
    defaults are intentional — they make subclass-without-override a
    visible misconfiguration rather than silently inheriting workspace-
    driver values.
    """
    base = BaseSubstrateDriver()
    assert base.driver_id == ""
    assert base.driver_version == ""
    assert base.store_id == ""
    assert base.binding == ""
    assert base.role == ""
    assert base.materialization_class == "external"


@pytest.mark.xfail(
    sys.version_info < (3, 12),
    reason=(
        "owner: vcs-core — on 3.11 runtime_checkable isinstance uses hasattr, which executes the "
        "raising `capabilities` property stub; 3.12+ uses static lookup (python/cpython#102433). "
        "The bare-base isinstance contract cannot hold on 3.11 with a raising property stub."
    ),
    raises=NotImplementedError,
    strict=True,
)
def test_bare_base_driver_required_hooks_raise() -> None:
    """BaseSubstrateDriver provides stubs that raise NotImplementedError.

    The mixin is incomplete by design — subclasses MUST override
    ``prepare`` / ``describe`` / ``capabilities``. Stubs are present so
    that mypy sees the methods exist and ``isinstance(x, SubstrateDriver)``
    returns ``True`` structurally, but direct invocation on the bare
    base raises clearly. This matches the "abstract base without ABC"
    pattern in Python without forcing ``@runtime_checkable Protocol``
    plus ABC composition (which the SPI doc explicitly avoids).
    """
    base = BaseSubstrateDriver()
    # Structurally satisfies the Protocol (methods exist):
    assert isinstance(base, SubstrateDriver)
    # But calling any required hook raises:
    for raise_call in (
        lambda: base.capabilities,
        lambda: base.describe(),
        lambda: base.prepare(_context(), CommandRequest(command="noop", params={})),
    ):
        with pytest.raises(NotImplementedError) as exc_info:
            raise_call()
        assert "BaseSubstrateDriver" in str(exc_info.value) or "must implement" in str(exc_info.value)


def test_decorated_driver_derives_command_schema_without_invoking_method() -> None:
    @dataclass(frozen=True)
    class _DecoratedDriver(BaseSubstrateDriver):
        driver_id: str = "test.decorated"
        driver_version: str = "v1"
        binding: str = "decorated"

        @property
        def capabilities(self) -> CapabilitySet:
            return CapabilitySet(accepts=frozenset({CommandRequest}), selectable=False)

        @command("echo", required_one_of=(("message", "fallback"),))
        def echo(
            self,
            context: DriverContext,
            *,
            message: Annotated[str | None, {"description": "Message to echo."}] = None,
            fallback: str | None = None,
            count: Annotated[int, {"description": "Repeat count.", "choices": (1, 2)}] = 1,
            provider: _PythonOnly | None = None,
        ) -> DriverIngressResult:
            """Echo a message."""
            raise AssertionError("describe must not invoke decorated command methods")

    schema = _DecoratedDriver().describe()
    command_spec = schema.commands["echo"]

    assert command_spec.description == "Echo a message."
    assert command_spec.required_one_of == (("message", "fallback"),)
    assert command_spec.params["message"].description == "Message to echo."
    assert command_spec.params["message"].required is False
    assert command_spec.params["message"].type == "str?"
    assert command_spec.params["fallback"].type == "str?"
    assert command_spec.params["fallback"].has_default is True
    assert command_spec.params["fallback"].default is None
    assert command_spec.params["count"].required is False
    assert command_spec.params["count"].has_default is True
    assert command_spec.params["count"].default == 1
    assert command_spec.params["count"].choices == (1, 2)
    assert command_spec.params["provider"].projectable is False
    validate_driver_schema(schema)
    assert validate_projectable_command(schema, "echo").projectable is True


def test_command_decorator_rejects_noncanonical_metadata_shapes() -> None:
    with pytest.raises(ValueError, match="examples must be a tuple"):

        @command("bad", examples=["vcs-core sub bad run"])  # type: ignore[arg-type]
        def _bad_examples(*, message: str) -> DriverIngressResult:
            del message
            return DriverIngressResult()

    with pytest.raises(ValueError, match="required_one_of must be a tuple"):

        @command("bad", required_one_of=[["message", "fallback"]])  # type: ignore[arg-type]
        def _bad_required_one_of(*, message: str | None = None, fallback: str | None = None) -> DriverIngressResult:
            del message, fallback
            return DriverIngressResult()


def test_decorated_driver_rejects_noncanonical_mapping_choices_metadata() -> None:
    from vcs_core import InvalidRepositoryStateError

    @dataclass(frozen=True)
    class _DecoratedDriver(BaseSubstrateDriver):
        driver_id: str = "test.decorated"
        driver_version: str = "v1"

        @property
        def capabilities(self) -> CapabilitySet:
            return CapabilitySet(accepts=frozenset({CommandRequest}), selectable=False)

        @command("echo")
        def echo(
            self,
            *,
            count: Annotated[int, {"description": "Repeat count.", "choices": [1, 2]}],
        ) -> DriverIngressResult:
            del count
            return DriverIngressResult()

    with pytest.raises(InvalidRepositoryStateError, match="choices metadata must be a tuple"):
        _DecoratedDriver().describe()


def test_dispatch_decorated_command_is_explicit_opt_in_and_preserves_method_defaults() -> None:
    @dataclass(frozen=True)
    class _DecoratedDriver(BaseSubstrateDriver):
        driver_id: str = "test.decorated"
        driver_version: str = "v1"
        binding: str = "decorated"

        @property
        def capabilities(self) -> CapabilitySet:
            return CapabilitySet(accepts=frozenset({CommandRequest}), selectable=False)

        def prepare(self, context: DriverContext, request: IngressRequest) -> DriverIngressResult:
            return self.dispatch_decorated_command(context, request)

        @command("echo")
        def echo(self, context: DriverContext, *, message: str, count: int = 1) -> DriverIngressResult:
            return DriverIngressResult(
                diagnostics=(
                    Diagnostic(
                        code="echo",
                        message=message,
                        detail={"operation_id": context.operation_id, "count": count},
                    ),
                )
            )

    result = _DecoratedDriver().prepare(_context(), CommandRequest(command="echo", params={"message": "hello"}))

    assert result.diagnostics[0].message == "hello"
    assert result.diagnostics[0].detail == {"operation_id": "op_test", "count": 1}


def test_dispatch_decorated_command_recompiles_from_describe_and_requires_stability() -> None:
    @dataclass(frozen=True)
    class _UnstableDecoratedDriver(BaseSubstrateDriver):
        driver_id: str = "test.decorated"
        driver_version: str = "v1"
        binding: str = "decorated"
        describe_calls: int = 0

        @property
        def capabilities(self) -> CapabilitySet:
            return CapabilitySet(accepts=frozenset({CommandRequest}), selectable=False)

        def describe(self) -> DriverSchema:
            object.__setattr__(self, "describe_calls", self.describe_calls + 1)
            params = {"message": ParamSpec(type="str")}
            if self.describe_calls > 1:
                params["extra"] = ParamSpec(type="str")
            return DriverSchema(
                driver_id=self.driver_id,
                driver_version=self.driver_version,
                capabilities=self.capabilities,
                commands={"echo": CommandSpec(description="Echo.", params=params)},
            )

        def prepare(self, context: DriverContext, request: IngressRequest) -> DriverIngressResult:
            return self.dispatch_decorated_command(context, request)

        @command("echo")
        def echo(self, *, message: str) -> DriverIngressResult:
            return DriverIngressResult(diagnostics=(Diagnostic(code="echo", message=message),))

    driver = _UnstableDecoratedDriver()
    assert tuple(driver.describe().commands["echo"].params) == ("message",)

    with pytest.raises(CommandContractError, match="missing required parameter 'extra'"):
        driver.prepare(_context(), CommandRequest(command="echo", params={"message": "hello"}))

    assert driver.describe_calls == 2


def test_dispatch_decorated_command_supports_required_one_of_alternate_branch() -> None:
    @dataclass(frozen=True)
    class _DecoratedDriver(BaseSubstrateDriver):
        driver_id: str = "test.decorated"
        driver_version: str = "v1"
        binding: str = "decorated"

        @property
        def capabilities(self) -> CapabilitySet:
            return CapabilitySet(accepts=frozenset({CommandRequest}), selectable=False)

        def prepare(self, context: DriverContext, request: IngressRequest) -> DriverIngressResult:
            return self.dispatch_decorated_command(context, request)

        @command("configure", required_one_of=(("name", "alias"),))
        def configure(
            self, context: DriverContext, *, name: str | None = None, alias: str | None = None
        ) -> DriverIngressResult:
            return DriverIngressResult(
                diagnostics=(
                    Diagnostic(
                        code="configured",
                        message="configured",
                        detail={"operation_id": context.operation_id, "name": name, "alias": alias},
                    ),
                )
            )

    result = _DecoratedDriver().prepare(_context(), CommandRequest(command="configure", params={"alias": "fallback"}))

    assert result.diagnostics[0].detail == {"operation_id": "op_test", "name": None, "alias": "fallback"}


def test_dispatch_decorated_command_supports_keyword_only_context() -> None:
    @dataclass(frozen=True)
    class _DecoratedDriver(BaseSubstrateDriver):
        driver_id: str = "test.decorated"
        driver_version: str = "v1"
        binding: str = "decorated"

        @property
        def capabilities(self) -> CapabilitySet:
            return CapabilitySet(accepts=frozenset({CommandRequest}), selectable=False)

        def prepare(self, context: DriverContext, request: IngressRequest) -> DriverIngressResult:
            return self.dispatch_decorated_command(context, request)

        @command("echo")
        def echo(self, *, context: DriverContext, message: str) -> DriverIngressResult:
            return DriverIngressResult(
                diagnostics=(
                    Diagnostic(
                        code="echo",
                        message=message,
                        detail={"operation_id": context.operation_id},
                    ),
                )
            )

    result = _DecoratedDriver().prepare(_context(), CommandRequest(command="echo", params={"message": "hello"}))

    assert result.diagnostics[0].message == "hello"
    assert result.diagnostics[0].detail == {"operation_id": "op_test"}


def test_dispatch_decorated_command_requires_declared_schema_command() -> None:
    @dataclass(frozen=True)
    class _HiddenDecoratedDriver(BaseSubstrateDriver):
        driver_id: str = "test.decorated"
        driver_version: str = "v1"
        binding: str = "decorated"

        @property
        def capabilities(self) -> CapabilitySet:
            return CapabilitySet(accepts=frozenset({CommandRequest}), selectable=False)

        def describe(self) -> DriverSchema:
            return DriverSchema(
                driver_id=self.driver_id,
                driver_version=self.driver_version,
                capabilities=self.capabilities,
                commands={},
            )

        def prepare(self, context: DriverContext, request: IngressRequest) -> DriverIngressResult:
            return self.dispatch_decorated_command(context, request)

        @command("echo")
        def echo(self, *, message: str) -> DriverIngressResult:
            return DriverIngressResult(diagnostics=(Diagnostic(code="echo", message=message),))

    with pytest.raises(CommandContractError, match="has no command named 'echo'"):
        _HiddenDecoratedDriver().prepare(_context(), CommandRequest(command="echo", params={"message": "hello"}))


def test_decorated_command_is_inherited_when_method_is_not_overridden() -> None:
    @dataclass(frozen=True)
    class _BaseDriver(BaseSubstrateDriver):
        driver_id: str = "test.decorated"
        driver_version: str = "v1"
        binding: str = "decorated"

        @property
        def capabilities(self) -> CapabilitySet:
            return CapabilitySet(accepts=frozenset({CommandRequest}), selectable=False)

        def prepare(self, context: DriverContext, request: IngressRequest) -> DriverIngressResult:
            return self.dispatch_decorated_command(context, request)

        @command("echo")
        def echo(self, *, message: str) -> DriverIngressResult:
            return DriverIngressResult(diagnostics=(Diagnostic(code="echo", message=message),))

    @dataclass(frozen=True)
    class _ChildDriver(_BaseDriver):
        pass

    schema = _ChildDriver().describe()
    result = _ChildDriver().prepare(_context(), CommandRequest(command="echo", params={"message": "hello"}))

    assert tuple(schema.commands["echo"].params) == ("message",)
    assert result.diagnostics[0].message == "hello"


def test_undecorated_override_removes_inherited_decorated_command_binding() -> None:
    @dataclass(frozen=True)
    class _BaseDriver(BaseSubstrateDriver):
        driver_id: str = "test.decorated"
        driver_version: str = "v1"
        binding: str = "decorated"

        @property
        def capabilities(self) -> CapabilitySet:
            return CapabilitySet(accepts=frozenset({CommandRequest}), selectable=False)

        def prepare(self, context: DriverContext, request: IngressRequest) -> DriverIngressResult:
            return self.dispatch_decorated_command(context, request)

        @command("echo")
        def echo(self, *, message: str) -> DriverIngressResult:
            return DriverIngressResult(diagnostics=(Diagnostic(code="echo", message=message),))

    @dataclass(frozen=True)
    class _ChildDriver(_BaseDriver):
        def echo(self) -> DriverIngressResult:
            return DriverIngressResult(diagnostics=(Diagnostic(code="echo", message="child"),))

    driver = _ChildDriver()

    assert driver.derived_command_specs() == {}
    with pytest.raises(NotImplementedError):
        driver.describe()
    with pytest.raises(UnsupportedRequestError):
        driver.prepare(_context(), CommandRequest(command="echo", params={"message": "hello"}))


def test_redecorated_override_replaces_inherited_command_schema_and_dispatch() -> None:
    @dataclass(frozen=True)
    class _BaseDriver(BaseSubstrateDriver):
        driver_id: str = "test.decorated"
        driver_version: str = "v1"
        binding: str = "decorated"

        @property
        def capabilities(self) -> CapabilitySet:
            return CapabilitySet(accepts=frozenset({CommandRequest}), selectable=False)

        def prepare(self, context: DriverContext, request: IngressRequest) -> DriverIngressResult:
            return self.dispatch_decorated_command(context, request)

        @command("echo")
        def echo(self, *, message: str) -> DriverIngressResult:
            return DriverIngressResult(diagnostics=(Diagnostic(code="echo", message=message),))

    @dataclass(frozen=True)
    class _ChildDriver(_BaseDriver):
        @command("echo")
        def echo(self, *, count: int) -> DriverIngressResult:
            return DriverIngressResult(diagnostics=(Diagnostic(code="echo", message=str(count)),))

    driver = _ChildDriver()
    schema = driver.describe()
    result = driver.prepare(_context(), CommandRequest(command="echo", params={"count": 3}))

    assert tuple(schema.commands["echo"].params) == ("count",)
    assert result.diagnostics[0].message == "3"


def test_decorated_command_name_collisions_fail() -> None:
    @dataclass(frozen=True)
    class _BadDriver(BaseSubstrateDriver):
        driver_id: str = "test.bad"
        driver_version: str = "v1"

        @property
        def capabilities(self) -> CapabilitySet:
            return CapabilitySet(accepts=frozenset({CommandRequest}), selectable=False)

        @command("same")
        def first(self, context: DriverContext, *, value: str) -> DriverIngressResult:
            del context, value
            return DriverIngressResult()

        @command("same")
        def second(self, context: DriverContext, *, value: str) -> DriverIngressResult:
            del context, value
            return DriverIngressResult()

    from vcs_core import InvalidRepositoryStateError

    with pytest.raises(InvalidRepositoryStateError, match="decorated command name collision"):
        _BadDriver().derived_command_specs()


def test_reduce_request_on_minimal_driver_raises_not_implemented() -> None:
    """A minimal driver that hasn't overridden ``prepare`` raises NotImplementedError.

    The coordinator's capability check normally short-circuits unsupported
    request types before reaching the driver; this is the defensive
    fallback when a caller bypasses the coordinator (tests, internal
    paths) on a driver that hasn't wired the typed ``ReduceRequest`` arm.
    """
    driver = _MinimalDriver()
    try:
        driver.prepare(_context(), ReduceRequest(evidence_citations=ReductionBatch(citations=())))
    except NotImplementedError:
        pass
    else:
        raise AssertionError("expected NotImplementedError from minimal driver")
