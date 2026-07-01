"""Spike 2: Input/Output Serialization Round-Trip Fidelity.

Validates that the Pydantic wrapper model strategy works for programmatic
task execution across a JSON serialization boundary.

Reference: design/SPIKES-programmatic-device-execution.md (Spike 2)
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path
from typing import Literal

import pydantic
import pytest
from pydantic import BaseModel, create_model
from shepherd_core._shared.coerce import _coerce_step_value
from shepherd_runtime.task.metadata import FieldInfo

# ---------------------------------------------------------------------------
# Shared fixtures: types under test
# ---------------------------------------------------------------------------


class Color(Enum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"


class Address(BaseModel):
    street: str
    city: str


class Person(BaseModel):
    name: str
    address: Address


# All (label, type_annotation, sample_value) triples for the matrix.
# Each entry drives both the input and output round-trip tests.
TYPE_MATRIX: list[tuple[str, type, object]] = [
    ("str", str, "hello"),
    ("int", int, 42),
    ("float", float, 3.14),
    ("bool", bool, True),
    ("list_str", list[str], ["a", "b", "c"]),
    ("dict_str_int", dict[str, int], {"x": 1, "y": 2}),
    ("set_str", set[str], {"alpha", "beta"}),
    ("tuple_int", tuple[int, ...], (10, 20, 30)),
    ("datetime", datetime, datetime(2025, 6, 15, 12, 30, 0)),
    ("date", date, date(2025, 6, 15)),
    ("time", time, time(12, 30, 0)),
    ("bytes", bytes, b"binary-data"),
    ("Path", Path, Path("/tmp/test/file.txt")),
    ("Enum", Color, Color.GREEN),
    ("Literal", Literal["a", "b"], "a"),
    ("BaseModel", Address, Address(street="123 Main", city="Springfield")),
    ("Optional_str", str | None, "present"),
    ("Optional_str_none", str | None, None),
    ("nested_BaseModel", Person, Person(name="Alice", address=Address(street="1 Elm", city="Gotham"))),
]


_PYTHON_MODE_FAILS_TYPES = ("datetime", "date", "time", "bytes", "Path", "set_str")


def _make_model(label: str, annotation: type) -> type[BaseModel]:
    """Dynamically create a Pydantic model with a single field of the given type."""
    return create_model(f"Model_{label}", value=(annotation, ...))


# ============================================================================
# Work Item 1 -- Input round-trip (mode="json")
# ============================================================================


class TestInputRoundTrip:
    """model_dump(mode='json') -> json.dumps -> json.loads -> model_validate."""

    @pytest.mark.spike
    @pytest.mark.parametrize(
        ("label", "annotation", "sample"),
        TYPE_MATRIX,
        ids=[t[0] for t in TYPE_MATRIX],
    )
    def test_input_roundtrip_json_mode(self, label: str, annotation: type, sample: object) -> None:
        Model = _make_model(label, annotation)
        instance = Model(value=sample)

        # Serialize with mode="json" -- produces JSON-safe Python objects
        dumped = instance.model_dump(mode="json")
        json_str = json.dumps(dumped)
        loaded = json.loads(json_str)
        restored = Model.model_validate(loaded)

        # For set, compare as set (order may differ)
        if isinstance(sample, set):
            assert set(restored.value) == sample
        elif isinstance(sample, tuple):
            # Pydantic may restore tuples as lists; compare contents
            assert tuple(restored.value) == sample
        elif isinstance(sample, bytes):
            assert restored.value == sample
        elif isinstance(sample, Path):
            assert Path(restored.value) == sample
        else:
            assert restored.value == sample

    # Types that mode="python" preserves as non-JSON-safe Python objects.
    # BaseModel and nested_BaseModel are excluded: Pydantic's default mode="python"
    # dumps them as plain dicts, which are JSON-safe.
    # Enum is excluded: mode="python" preserves Enum members, but json.dumps
    # calls str() on unknown types rather than raising TypeError.

    @pytest.mark.spike
    @pytest.mark.parametrize(
        ("label", "annotation", "sample"),
        [t for t in TYPE_MATRIX if t[0] in _PYTHON_MODE_FAILS_TYPES],
        ids=[t[0] for t in TYPE_MATRIX if t[0] in _PYTHON_MODE_FAILS_TYPES],
    )
    def test_python_mode_fails_json_dumps(self, label: str, annotation: type, sample: object) -> None:
        """Default mode='python' preserves native objects that break json.dumps."""
        Model = _make_model(label, annotation)
        instance = Model(value=sample)
        dumped = instance.model_dump()  # default mode="python"

        with pytest.raises(TypeError):
            json.dumps(dumped)


# ============================================================================
# Work Item 2 -- Output round-trip via Pydantic wrapper
# ============================================================================


def _container_side_serialize(value: object, annotation: type) -> object:
    """Simulate container-side serialization strategy from the parent design.

    BaseModel -> model_dump(mode="json")
    Enum      -> .value
    primitives / other -> identity (then json.dumps handles it)
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    # For types that are not JSON-safe on their own (set, tuple, datetime,
    # bytes, Path), we wrap in a temporary model and use model_dump(mode="json")
    # to get a JSON-safe representation. This mirrors what the container would
    # do: wrap outputs in a Pydantic model and call model_dump(mode="json").
    try:
        json.dumps(value)
        return value  # Already JSON-safe
    except TypeError:
        # Use Pydantic to serialize
        Wrapper = _make_model("_serialize", annotation)
        return Wrapper(value=value).model_dump(mode="json")["value"]


class TestOutputRoundTripPydanticWrapper:
    """Validate the Pydantic wrapper model approach for output deserialization."""

    @pytest.mark.spike
    @pytest.mark.parametrize(
        ("label", "annotation", "sample"),
        TYPE_MATRIX,
        ids=[t[0] for t in TYPE_MATRIX],
    )
    def test_output_roundtrip_via_wrapper(self, label: str, annotation: type, sample: object) -> None:
        # Container side: serialize the output value
        serialized = _container_side_serialize(sample, annotation)

        # JSON transport
        json_str = json.dumps({"value": serialized})
        loaded = json.loads(json_str)

        # Host side: construct a temporary Pydantic model and validate
        WrapperModel = create_model("OutputWrapper", value=(annotation, ...))
        restored = WrapperModel.model_validate(loaded)

        if isinstance(sample, set):
            assert set(restored.value) == sample
        elif isinstance(sample, tuple):
            assert tuple(restored.value) == sample
        elif isinstance(sample, bytes):
            assert restored.value == sample
        elif isinstance(sample, Path):
            assert Path(restored.value) == sample
        else:
            assert restored.value == sample


# ============================================================================
# Work Item 3 -- Pydantic wrapper constructibility from _task_meta.outputs
# ============================================================================


class TestPydanticWrapperFromTaskMeta:
    """Verify that create_model() works with FieldInfo from task metadata."""

    @pytest.mark.spike
    def test_create_model_from_field_info(self) -> None:
        """Simulate building a wrapper model from _task_meta.outputs."""
        # Simulate FieldInfo entries as they would appear in _task_meta.outputs
        outputs: dict[str, FieldInfo] = {
            "result_str": FieldInfo(name="result_str", inner_type=str, marker_type="output"),
            "result_int": FieldInfo(name="result_int", inner_type=int, marker_type="output"),
            "result_list": FieldInfo(name="result_list", inner_type=list[str], marker_type="output"),
            "result_dict": FieldInfo(name="result_dict", inner_type=dict[str, int], marker_type="output"),
            "result_model": FieldInfo(name="result_model", inner_type=Address, marker_type="output"),
            "result_enum": FieldInfo(name="result_enum", inner_type=Color, marker_type="output"),
            "result_dt": FieldInfo(name="result_dt", inner_type=datetime, marker_type="output"),
            "result_set": FieldInfo(name="result_set", inner_type=set[str], marker_type="output"),
            "result_bytes": FieldInfo(name="result_bytes", inner_type=bytes, marker_type="output"),
            "result_path": FieldInfo(name="result_path", inner_type=Path, marker_type="output"),
            "result_optional": FieldInfo(
                name="result_optional",
                inner_type=str | None,
                marker_type="output",
            ),
        }

        # Build create_model field tuples: (type, ...)
        field_definitions: dict[str, tuple[type, object]] = {}
        for name, fi in outputs.items():
            field_definitions[name] = (fi.inner_type, ...)

        # Construct the model
        WrapperModel = create_model("TaskOutputWrapper", **field_definitions)

        # Verify the model can be constructed
        assert issubclass(WrapperModel, BaseModel)
        assert set(WrapperModel.model_fields.keys()) == set(outputs.keys())

        # Verify it can validate a dict of serialized values
        test_data = {
            "result_str": "hello",
            "result_int": 42,
            "result_list": ["a", "b"],
            "result_dict": {"x": 1},
            "result_model": {"street": "123 Main", "city": "Springfield"},
            "result_enum": "green",
            "result_dt": "2025-06-15T12:30:00",
            "result_set": ["alpha", "beta"],
            "result_bytes": "YmluYXJ5LWRhdGE=",  # base64 of b"binary-data"
            "result_path": "/tmp/test",
            "result_optional": None,
        }

        restored = WrapperModel.model_validate(test_data)
        assert restored.result_str == "hello"
        assert restored.result_int == 42
        assert restored.result_list == ["a", "b"]
        assert restored.result_dict == {"x": 1}
        assert isinstance(restored.result_model, Address)
        assert restored.result_enum == Color.GREEN
        assert isinstance(restored.result_dt, datetime)
        assert isinstance(restored.result_set, set)
        assert isinstance(restored.result_bytes, bytes)
        assert isinstance(restored.result_path, Path)
        assert restored.result_optional is None

    @pytest.mark.spike
    def test_create_model_with_optional_fields(self) -> None:
        """Verify that optional output fields (with defaults) work."""
        outputs = {
            "required_field": FieldInfo(name="required_field", inner_type=str, marker_type="output", required=True),
            "optional_field": FieldInfo(
                name="optional_field",
                inner_type=str | None,
                marker_type="output",
                required=False,
                default=None,
            ),
        }

        field_definitions: dict[str, tuple[type, object]] = {}
        for name, fi in outputs.items():
            if fi.required:
                field_definitions[name] = (fi.inner_type, ...)
            else:
                field_definitions[name] = (fi.inner_type, fi.default)

        WrapperModel = create_model("TaskOutputWrapper2", **field_definitions)

        # Validate with only the required field
        restored = WrapperModel.model_validate({"required_field": "hello"})
        assert restored.required_field == "hello"
        assert restored.optional_field is None


# ============================================================================
# Work Item 4 -- Compare against _coerce_step_value()
# ============================================================================


# Types where _coerce_step_value returns the value unchanged (i.e., it does
# NOT coerce the JSON-deserialized form back to the annotated type).
# These are the "gaps" documented in the spike design.
_COERCE_KNOWN_GAPS = {
    "dict_str_int",  # no dict branch; returns raw dict without coercing values
    "set_str",  # no set branch; returns list unchanged
    "tuple_int",  # no tuple branch; returns list unchanged
    "datetime",  # no datetime branch; returns ISO string unchanged
    "date",  # no date branch; returns ISO string unchanged
    "time",  # no time branch; returns ISO string unchanged
    "bytes",  # no bytes branch; returns base64 string unchanged
    "Path",  # no Path branch; returns string unchanged
    "nested_BaseModel",  # BaseModel branch handles flat, but nested may lose type info
}


class TestCoerceStepValueComparison:
    """Compare _coerce_step_value against the Pydantic wrapper approach."""

    @pytest.mark.spike
    @pytest.mark.parametrize(
        ("label", "annotation", "sample"),
        TYPE_MATRIX,
        ids=[t[0] for t in TYPE_MATRIX],
    )
    def test_coerce_step_value_coverage(self, label: str, annotation: type, sample: object) -> None:
        """Document which types _coerce_step_value handles and which it misses.

        This test does NOT assert correctness -- it documents behavior.
        Types in _COERCE_KNOWN_GAPS are expected to return a different
        Python type than the annotation specifies (e.g. str instead of
        datetime, list instead of set).
        """
        # Simulate JSON round-trip to get deserialized value
        serialized = _container_side_serialize(sample, annotation)
        json_str = json.dumps(serialized)
        deserialized = json.loads(json_str)

        # Skip None -- _coerce_step_value raises on None for non-Optional types
        if deserialized is None:
            pytest.skip("None value -- _coerce_step_value raises StepOutputError")

        try:
            result = _coerce_step_value(deserialized, annotation)
        except Exception as exc:
            if label in _COERCE_KNOWN_GAPS:
                pytest.skip(f"_coerce_step_value raised {type(exc).__name__}: {exc}")
            raise

        # Check if the result matches the original type
        if label in _COERCE_KNOWN_GAPS:
            # Document the gap: result is not the expected type
            if isinstance(sample, set):
                assert not isinstance(result, set), (
                    f"Expected gap for {label}: _coerce_step_value should NOT produce a set, "
                    f"but it did -- remove from _COERCE_KNOWN_GAPS"
                )
            elif isinstance(sample, tuple):
                assert not isinstance(result, tuple), (
                    f"Expected gap for {label}: _coerce_step_value should NOT produce a tuple, "
                    f"but it did -- remove from _COERCE_KNOWN_GAPS"
                )
            elif isinstance(sample, datetime):
                assert not isinstance(result, datetime), (
                    f"Expected gap for {label}: _coerce_step_value should NOT produce datetime, "
                    f"but it did -- remove from _COERCE_KNOWN_GAPS"
                )
            elif isinstance(sample, date) and not isinstance(sample, datetime):
                assert not isinstance(result, date), (
                    f"Expected gap for {label}: _coerce_step_value should NOT produce date, "
                    f"but it did -- remove from _COERCE_KNOWN_GAPS"
                )
            elif isinstance(sample, time):
                assert not isinstance(result, time), (
                    f"Expected gap for {label}: _coerce_step_value should NOT produce time, "
                    f"but it did -- remove from _COERCE_KNOWN_GAPS"
                )
            elif isinstance(sample, bytes):
                assert not isinstance(result, bytes), (
                    f"Expected gap for {label}: _coerce_step_value should NOT produce bytes, "
                    f"but it did -- remove from _COERCE_KNOWN_GAPS"
                )
            elif isinstance(sample, Path):
                assert not isinstance(result, Path), (
                    f"Expected gap for {label}: _coerce_step_value should NOT produce Path, "
                    f"but it did -- remove from _COERCE_KNOWN_GAPS"
                )
        # For types _coerce_step_value claims to handle, verify type match
        elif isinstance(sample, bool):
            assert isinstance(result, bool)
        elif isinstance(sample, (BaseModel, Enum)):
            assert isinstance(result, type(sample))

    @pytest.mark.spike
    def test_coerce_gap_summary(self) -> None:
        """Summary: _coerce_step_value lacks branches for these types.

        This is evidence for why the Pydantic wrapper is preferred.
        _coerce_step_value was designed for coercing LLM text output, not
        lossless round-tripping of typed Python values through JSON.
        """
        gaps = sorted(_COERCE_KNOWN_GAPS)
        handled = sorted(
            label for label, _, _ in TYPE_MATRIX if label not in _COERCE_KNOWN_GAPS and label != "Optional_str_none"
        )

        # Document findings (these assertions serve as documentation)
        assert len(gaps) > 0, "Expected some gaps in _coerce_step_value"
        assert "str" in handled, "str should be handled"
        assert "int" in handled, "int should be handled"
        assert "float" in handled, "float should be handled"
        assert "bool" in handled, "bool should be handled"
        assert "Enum" in handled, "Enum should be handled"
        assert "BaseModel" in handled, "BaseModel should be handled"
        assert "Literal" in handled, "Literal should be handled"


# ============================================================================
# Work Item 5 -- Classify failures
# ============================================================================


class TestClassifyFailures:
    """Classify any types that fail the Pydantic wrapper round-trip.

    Current status: All common types pass. This test class documents the
    boundary and would collect xfail markers if any types were found to
    fail the round-trip.
    """

    @pytest.mark.spike
    def test_all_common_types_survive_roundtrip(self) -> None:
        """Verify that every type in TYPE_MATRIX survives the full round-trip.

        If a type fails, it should be moved to a separate xfail test with
        a classification (fixable, fundamental, or special-case).
        """
        failures: list[str] = []

        for label, annotation, sample in TYPE_MATRIX:
            try:
                Model = _make_model(label, annotation)
                instance = Model(value=sample)
                dumped = instance.model_dump(mode="json")
                json_str = json.dumps(dumped)
                loaded = json.loads(json_str)
                restored = Model.model_validate(loaded)

                if isinstance(sample, set):
                    assert set(restored.value) == sample
                elif isinstance(sample, tuple):
                    assert tuple(restored.value) == sample
                else:
                    assert restored.value == sample
            except Exception as exc:
                failures.append(f"{label}: {type(exc).__name__}: {exc}")

        assert not failures, "Types that failed round-trip:\n" + "\n".join(failures)

    @pytest.mark.spike
    def test_decimal_survives_roundtrip(self) -> None:
        """Decimal survives: Pydantic serializes as string, not float.

        The spike design predicted precision loss due to JSON float limitations.
        In practice, Pydantic v2 serializes Decimal as a string in mode="json",
        which preserves full precision through the JSON round-trip. This is a
        positive finding -- Decimal is NOT a fundamental limitation.
        """
        from decimal import Decimal

        Model = create_model("DecimalModel", value=(Decimal, ...))
        instance = Model(value=Decimal("3.141592653589793238"))
        dumped = instance.model_dump(mode="json")

        # Verify Pydantic serializes Decimal as string, not float
        assert isinstance(dumped["value"], str), f"Expected Decimal to serialize as string, got {type(dumped['value'])}"

        json_str = json.dumps(dumped)
        loaded = json.loads(json_str)
        restored = Model.model_validate(loaded)

        assert restored.value == Decimal("3.141592653589793238")

    @pytest.mark.spike
    def test_pydantic_version_info(self) -> None:
        """Record Pydantic version for reproducibility of spike results."""
        # This is informational -- ensures we know which Pydantic version
        # produced these results.
        assert pydantic.__version__ >= "2.0", f"Spike requires Pydantic v2+, got {pydantic.__version__}"
