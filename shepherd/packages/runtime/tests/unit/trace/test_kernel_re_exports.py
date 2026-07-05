"""Plan 02 WP1+WP2 contract tests: shared types and kernel re-exports.

Pinned by ``docs/design/proposed/260505-plans/02-trace-record-schema/``:

- WP1 ships ``Ref``, ``RunRef``, and ``SubTag`` shared identity types.
- WP2 re-exports the 13 kernel record types and the ``KernelRecord``
  union from ``shepherd_kernel_v3_reference.trace.records`` without forking.

These tests pin those guarantees so that later WPs (surface families,
Trace container, serde) can build on a stable schema base.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest


def test_subtag_enum_has_exactly_the_section_07_taxonomy() -> None:
    from shepherd_runtime.trace import SubTag

    values = {t.value for t in SubTag}
    assert values == {"control", "run", "branch", "proposal", "artifact"}


def test_runref_is_frozen_dataclass_with_string_id() -> None:
    from shepherd_runtime.nucleus import RUN_REF_SCHEMA
    from shepherd_runtime.trace import RunRef

    ref = RunRef(id="run_01HZX")
    assert ref.id == "run_01HZX"
    assert str(ref) == "run_01HZX"
    assert ref.to_payload()["schema"] == RUN_REF_SCHEMA
    assert RunRef.from_payload(ref.to_payload()) == ref
    # Frozen: cannot reassign id.
    with pytest.raises(FrozenInstanceError):
        ref.id = "run_other"  # type: ignore[misc]
    # Hashable; equality structural.
    assert ref == RunRef(id="run_01HZX")
    assert hash(ref) == hash(RunRef(id="run_01HZX"))


def test_trace_runref_is_nucleus_runref() -> None:
    from shepherd_runtime.nucleus import RunRef as NucleusRunRef
    from shepherd_runtime.trace import RunRef

    assert RunRef is NucleusRunRef


def test_ref_alias_matches_kernel_v3_reference() -> None:
    """Ref must be the same alias used by kernel records, not a parallel definition."""
    from shepherd_kernel_v3_reference.kernel.ir import Ref as KRef
    from shepherd_runtime.trace import Ref

    assert Ref is KRef


def test_kernel_record_re_exports_preserve_identity() -> None:
    """Re-exports must be the same classes; no forks."""
    import shepherd_kernel_v3_reference.trace as kernel_trace
    from shepherd_runtime.trace import (
        ContinuationDelay,
        ContinuationPending,
        ContinuationResume,
        EffectCapture,
        EffectDeclaration,
        ForkBranch,
        ForkSummary,
        HandlerForward,
        HandlerSelection,
        ResumeReturn,
        ResumptionHandle,
        SelectionClosed,
        TerminalResumeResult,
    )

    pairs = [
        (ContinuationDelay, kernel_trace.ContinuationDelay),
        (ContinuationPending, kernel_trace.ContinuationPending),
        (ContinuationResume, kernel_trace.ContinuationResume),
        (EffectCapture, kernel_trace.EffectCapture),
        (EffectDeclaration, kernel_trace.EffectDeclaration),
        (ForkBranch, kernel_trace.ForkBranch),
        (ForkSummary, kernel_trace.ForkSummary),
        (HandlerForward, kernel_trace.HandlerForward),
        (HandlerSelection, kernel_trace.HandlerSelection),
        (ResumeReturn, kernel_trace.ResumeReturn),
        (ResumptionHandle, kernel_trace.ResumptionHandle),
        (SelectionClosed, kernel_trace.SelectionClosed),
        (TerminalResumeResult, kernel_trace.TerminalResumeResult),
    ]
    for runtime_cls, reference_cls in pairs:
        assert runtime_cls is reference_cls


def test_kernel_record_count_is_thirteen() -> None:
    """Pin the §07 kernel-record family size at 13."""
    from typing import get_args

    from shepherd_runtime.trace import KernelRecord

    members = get_args(KernelRecord)
    assert len(members) == 13


def test_facade_exports_match_documented_set() -> None:
    """The __all__ must match the documented public surface.

    WP1+WP2: shared types and kernel re-exports.
    Tranche 6 (PR 17): adds Trace container (E4) + surface base.
    """
    from shepherd_runtime import trace

    expected = {
        # types
        "Ref",
        "RunRef",
        "SubTag",
        # typed runtime surface records
        "ArtifactEmitted",
        "DeliveryCompleted",
        "EffectRequested",
        "HandlerReturned",
        "HandlerSelected",
        "ProviderCallCompleted",
        "ProviderCallRequested",
        "SubstrateRefused",
        # kernel records
        "ContinuationDelay",
        "ContinuationPending",
        "ContinuationResume",
        "EffectCapture",
        "EffectDeclaration",
        "ForkBranch",
        "ForkSummary",
        "HandlerForward",
        "HandlerSelection",
        "KernelRecord",
        "ResumeReturn",
        "ResumptionHandle",
        "SelectionClosed",
        "TerminalResumeResult",
        # surface records (PR 17)
        "SurfaceBase",
        "SurfaceRecord",
        "SURFACE_REGISTRY",
        "RuntimeSurfaceEvent",
        "RuntimeTraceRecorder",
        "RuntimeTraceRecorderError",
        "active_trace_recorder",
        "pop_trace_recorder",
        "push_trace_recorder",
        # Trace container (PR 17, E4)
        "Trace",
        "SCHEMA_VERSION",
    }
    assert set(trace.__all__) == expected


def test_trace_package_does_not_depend_on_runtime_internals() -> None:
    """Static check: trace modules must not import from runtime lifecycle/scope/etc.

    The intent is that ``shepherd_runtime.trace`` is a thin schema layer that
    other lanes (storage adapter, proof envelope, provider interposition)
    can consume without dragging in the runtime execution stack. We check the
    module sources directly rather than what's loaded — Python's package
    import order will always load ``shepherd_runtime/__init__.py`` first,
    which is unrelated to the trace package's own dependencies.
    """
    import pathlib

    import shepherd_runtime.trace as trace_pkg

    forbidden_prefixes = (
        "shepherd_runtime.lifecycle",
        "shepherd_runtime.handlers",
        "shepherd_runtime.scope",
        "shepherd_runtime.cache",
        "shepherd_runtime.combinators",
        "shepherd_runtime.device",
        "shepherd_runtime.execution",
        "shepherd_runtime.persistence",
        "shepherd_runtime.materialization",
    )

    pkg_dir = pathlib.Path(trace_pkg.__file__).parent
    sources = list(pkg_dir.glob("*.py"))
    assert sources, f"expected source files under {pkg_dir}"

    leaked: list[tuple[str, str]] = []
    for src in sources:
        text = src.read_text()
        for prefix in forbidden_prefixes:
            if f"from {prefix}" in text or f"import {prefix}" in text:
                leaked.append((src.name, prefix))
    assert leaked == [], f"trace package leaked imports: {leaked}"
